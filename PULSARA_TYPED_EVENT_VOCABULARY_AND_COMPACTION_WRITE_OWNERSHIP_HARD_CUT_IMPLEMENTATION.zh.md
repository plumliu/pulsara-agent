# Pulsara Typed Event Vocabulary 与 Compaction Write Ownership Hard Cut 实施规格

状态：**IMPLEMENTATION READY**

本文冻结下一阶段 event vocabulary 与 compaction write ownership 的一次性硬切：

1. 将 7 个 production `CustomEvent` 全部替换为有界、可验证的 typed event；
2. 物理删除 `CustomEvent`、`EventType.CUSTOM` 及其默认 decoder；
3. 删除 compaction 对 `EventLog.append()` 的 direct production fallback；
4. 删除 Host 与 mid-turn compactor 的 sequence post-scan / 二次 publication；
5. 对 production direct `EventLog` event-row/checkpoint mutation 建立精确到函数和
   receiver 的 allowlist，并单独盘点非 event-row maintenance capability；
6. 将 MCP input-required 的 source suspension 与 Host resolution process owner一并
   hard-cut 为 typed、不可别名修改的 authority。

这六项必须被视为同一条 ownership 链，而不是六个互不相关的清理：

```text
typed candidate
    -> RuntimeSession event writer
    -> materialization account / committed reducers
    -> ordered publication
    -> exact process-local commit receipt
    -> Host / Agent local notification
```

硬切完成后，production 不再存在以下第二真源：

```text
dict-shaped CustomEvent
direct EventLog append
sequence-range post-scan discovery
post-scan republish
pytest-only recovery direct write branch
```

---

## 1. 为什么现在做

`RuntimeSession` 已经拥有稳定的 event write service：

- FIFO serialization；
- expected high-water / materialization account CAS；
- committed reducer fold；
- publication queue；
- cancellation 后 FULL / NONE / UNKNOWN confirmation；
- publication-after-commit 的 typed outcome。

因此剩余问题不再是“缺少 writer”，而是部分业务代码仍绕过或重复解释这个 writer：

```text
Agent emits free-form CustomEvent
Compaction may append directly to EventLog
Host scans what compaction might have written
Host republishes scanned events
LLM recovery keeps pytest-only direct EventLog writes
```

继续保留这些路径会产生四类风险：

1. `CustomEvent.name + dict` 没有字段、大小、枚举和 join invariant；
2. direct compaction port 绕过 reducer、publication 与 materialization ownership；
3. post-scan 可能混入同一 sequence 区间内其他 writer 的事件；
4. publication failure、caller cancellation 与 scan race 可能把一个已 FULL 的事实误判为失败或重复发布。

---

## 2. 代码真值

### 2.1 当前唯一 production `CustomEvent` vocabulary

静态搜索确认 production 只构造以下 7 个稳定名称：

| 旧名称 | 当前 producer | 当前 payload |
|---|---|---|
| `mcp_input_required_expired` | `runtime/agent.py` | interaction/tool/server/round |
| `mcp_input_required_resolved` | `runtime/agent.py` | interaction/tool/server/cancelled/response keys/round |
| `mcp_input_required_binding_changed` | `runtime/agent.py` | interaction/tool/server/free-form reason |
| `mcp_input_required_resume_failed` | `runtime/agent.py` | interaction/tool/server/error type/message |
| `compaction_requested` | `runtime/agent.py` | empty dict |
| `mid_turn_compaction_skipped` | `runtime/compaction/inline.py` | free-form reason/run/sequence |
| `tool_result_persistence_failed` | `runtime/agent.py` | error type/raw message |

其中最后一项名称不准确：canonical ToolResult 在此之前已经 durable。失败的是可选
`ExecutionEvidencePersistenceHook` 对 execution-evidence ledger 的投影，而不是 ToolResult
本身的持久化。

仓库中没有需要继续保留 generic diagnostic `CustomEvent` 的 production 使用者。因此 V1
不保留受限 diagnostic carrier，直接物理删除整个类型。

### 2.2 当前 compaction direct fallback

`runtime/compaction/commit.py` 同时包含：

```text
RuntimeSessionCompactionEventCommitPort
DirectEventLogCompactionEventCommitPort
```

后者通过 `asyncio.to_thread(event_log.append, event)` 直接写 ledger。

`ContextCompactionService.__post_init__()` 在没有注入 commit port 时会静默创建 direct
port。虽然 production wiring 已注入 `RuntimeSessionCompactionEventCommitPort`，但 service
本身仍是 fail-open 的，component test 也持续训练这条错误 ownership。

`PendingCompactionEventCommit` 因此保留了两套 mutually exclusive owner：

```text
runtime_session
event_log
```

以及两套 confirmation 算法。

### 2.3 当前有两处 compaction post-scan publication

Host：

```text
before_sequence = event_log.next_sequence()
await service.compact(...)
scan [before_sequence, current high-water]
filter hard-coded compaction event classes
runtime_session.publish_stored_events(...)
```

实现位于 `host/session.py`：

- `_publish_compaction_events_after()`；
- `_compaction_events_after()`；
- `_latest_terminal_compaction_event()`。

mid-turn compactor 在 `runtime/compaction/inline.py` 重复同一模式，并将扫描结果返回给
Agent stream。

两者都必须删除。只删除 Host 版本会让 mid-turn 路径继续拥有第二个 publication owner。

### 2.4 当前 direct EventLog event-row/checkpoint mutation sites

排除 `event_log/` physical adapter implementation 后，当前 source tree 中的
event-row/checkpoint mutation call sites只有：

| 文件/owner | mutation |
|---|---|
| `runtime/session.py::RuntimeSession._commit_reduce_enqueue` | `event_log.extend` |
| `runtime/session.py::RuntimeSession._persist_runtime_projection_checkpoint` | `event_log.write_runtime_projection_checkpoint` |
| `runtime/authority_materialization/account.py::LedgerMaterializationCoordinator._commit_atomic` | `event_log.extend_with_materialization_state` |
| `runtime/long_horizon/checkpoint_doctor.py::verify_or_rebuild_subagent_graph_checkpoint` | privileged offline `event_log.append` |
| `runtime/compaction/commit.py::DirectEventLogCompactionEventCommitPort` | 待删除 `event_log.append` |
| `llm/recovery.py` | pytest-only unbootstrapped `event_log.extend` |
| `llm/control_recovery.py` | pytest-only unbootstrapped `event_log.extend` |

当前 architecture test 只按文件后缀放行 `append/extend`，没有精确到 class、function、
receiver 和 method，也没有覆盖 checkpoint/account mutation 或 bound-method escape。

`EventLog` 还暴露三类不属于上述 event-row/checkpoint mutation 集合的 maintenance
capability：

| capability | 当前 production owner | 本阶段处理 |
|---|---|---|
| `ensure_runtime_session_owner()` | `RuntimeSession.__post_init__`、child RuntimeSession composition | 保留；由 session bootstrap contract 精确 guard |
| `repair_run_projection()` | `host/resume.py::repair_dangling_runs_for_resume` | 保留；仅 privileged/quiescent recovery |
| `adopt_materialization_account_state_for_test()` | RuntimeSession pytest bootstrap branch | 保留 test-only；production composition fail closed |

它们不得被误算为“新的 live event producer”，但也不能从 architecture inventory 消失。
第 13 节同时冻结 event-row mutation allowlist与 maintenance inventory guard。

### 2.5 当前 MCP source 与 resolution 仍是 mutable dict authority

当前 `ToolExecutionSuspendedEvent` 仍保存：

```python
interaction_kind: str
payload: dict[str, Any]
```

`runtime/agent.py::_suspend_tool_execution()` 再从该 payload 中读取 binding、pending lease、
request、deadline 与 round。restart/reducer 因而必须重新解释自由 dict；在 resume 时补建
typed source fact，不能改变 source authority仍由自由 payload产生的事实。

另一侧，`McpInputRequiredInteractionResolution` 虽是 frozen dataclass，内部
`responses: dict[str, dict[str, Any]]` 仍可变。`HostIngressCoordinator` 当前保存同一个
payload alias，resume boundary 与 MCP adapter随后分别消费它。若 caller在第一次 await
后修改 nested dict，durable resolution fingerprint 与 adapter实际收到的 resolution可能
漂移。

本阶段必须从 suspension producer和 ingress admission同时收口，不能只替换最终七个
`CustomEvent`。

---

## 3. 范围与非目标

### 3.1 本阶段范围

- 7 个 production CustomEvent 的 typed hard cut；
- MCP input-required lifecycle 的 durable attribution 收紧；
- `ToolExecutionSuspendedEvent` 的 MCP branch typed hard cut；
- MCP resolution 的 pre-await immutable preparation owner；
- RuntimeSession shared absolute-deadline write API与 publication reconciliation latch；
- compaction request / skip / evidence projection failure 的 typed semantics；
- `CustomEvent` schema 的物理删除；
- compaction commit port 单一化；
- compaction exact attempt receipt；
- Host 和 mid-turn post-scan 删除；
- direct EventLog mutation exact allowlist；
- tests、Inspector、contracts、registry 与 reset workflow 同步。

### 3.2 非目标

- 不重写 `RuntimeSession` event writer；
- 不在本阶段实现 native async PostgreSQL driver；
- 不改变 compaction summary / artifact / memory-candidate 的领域算法；
- 不把 7 个 replacements、MCP closure或 typed suspension加入 transcript semantic domain；
- 不增加 generic “diagnostic event” 逃生口；
- 不为旧 `CUSTOM` ledger 提供 decoder compatibility；
- 不把 EventLog read API 纳入 mutation allowlist；
- 不借本阶段重构全部 AgentRuntime coordinator。
- 不把 compaction memory candidate projection并入 compaction core attempt；它继续由
  既有 memory projection outbox独立拥有；
- 不以 typed evidence-projection failure audit冒充 durable hook retry job。

---

## 4. 中央不变量

### EV-I1：每个 production event type 只有一个 typed schema

禁止：

```python
CustomEvent(name="...", value={...})
```

所有 durable semantic 字段必须是 typed field 或 typed nested fact。`EventBase.metadata`
只能保存非权威 attribution，不能藏入 reason、state transition 或 replay 必需字段。

### EV-I2：无 generic production escape hatch

硬切后：

- `EventType.CUSTOM` 不存在；
- `CustomEvent` 不存在；
- `AgentEvent` union 不含 generic branch；
- default registry 不注册 `CUSTOM`；
- historical decoder 不允许读取旧 `CUSTOM`；
- production/tests 不通过字符串复活旧名称。

### EV-I3：八个新增/替换事件与 typed suspension全部是 explicit non-transcript

它们不改变：

- normalized transcript；
- provider-visible message history；
- transcript semantic accumulator；
- compaction summary semantics。

它们会改变完整 event schema/domain registry fingerprint；这是预期的 hard cut。

### EV-I4：所有 online compaction event 只经 RuntimeSession writer

`ContextCompactionService` 必须显式取得：

```text
same RuntimeSession
same runtime_session_id
RuntimeSessionCompactionEventCommitPort
```

缺少、错绑或跨 ledger 时 composition fail closed。service 不得从 `EventLog` 自行构造
commit port。

### EV-I5：commit receipt 是 post-commit discovery 的唯一 process-local 真源

调用方只能从 `ContextCompactionAttemptResult.core_committed_events` 获得本次 attempt
写入的 compaction core events。memory candidate proposal从独立 projection
receipt/owner观察。禁止通过 sequence range猜测“刚才写了什么”。

### EV-I6：RuntimeSession writer 是 online publication 唯一 owner

Host、Agent 和 compactor 可以把 exact committed event 返回给本地 caller/stream，但不得
再次调用 `publish_stored_events()`。

### EV-I7：direct EventLog mutation 默认拒绝

除第 13.1 节的四个 exact call site 外，production business code 不得调用或保存以下
event-row/checkpoint mutation capability：

```text
append
extend
extend_with_materialization_state
write_runtime_projection_checkpoint
```

### EV-I8：MCP source authority 从 typed suspension 开始

MCP input-required 的 binding、pending lease、request envelope、deadline、round与
predecessor resolution必须首先出现在 typed `ToolExecutionSuspendedEvent` branch中。
resolution、terminal disposition、recovery与 Inspector只能 exact-join该 source；禁止从
`LoopState.pending_interaction_payload` 或任意自由 dict重新推导 durable identity。

### EV-I9：Host ingress 不能持有 caller-owned MCP response dict

`resolve_mcp_input_required()` 在第一次 await 前必须同步构造唯一
`PreparedMcpInputRequiredResolution`。Host ingress、resume boundary与 adapter invocation
都消费该 owner。任何 caller-owned dict/list都不得跨越 ingress admission。

### EV-I10：compaction core 与 candidate projection 是两个 owner

`ContextCompactionAttemptResult` 只证明 compaction core：

```text
ContextCompactionStartedEvent
    -> ContextCompactionCompletedEvent | ContextCompactionFailedEvent
```

`ContextCompactionMemoryCandidatesProposedEvent`、transactional outbox与 candidate-pool
projection继续由 `MemoryCandidateProjectionCommitPort` 拥有。proposal失败或迟到不能
改写已经 FULL 的 compaction terminal。

### EV-I11：MCP process lease不能跨 SESSION_REOPEN伪造

durable suspension证明历史上存在 pending lease reservation，不证明新进程仍持有原
Supervisor owner。`SESSION_REOPEN` 缺 exact live lease时必须 typed terminalize
interaction/run；禁止重新 acquire同名 binding或保留一个永远无法恢复的 WAITING_USER。

### EV-I12：每个 stable candidate只有一个 frozen deadline budget

mandatory audit owner在第一次 admission冻结 ordinary deadline与 terminal maintenance
deadline。Compaction不使用attempt-wide budget；Started、Completed、Failed各自在自己的
第一次writer admission冻结candidate budget。相同candidate的所有NONE retry、
confirmation、account/reducer/publication步骤只能使用既有绝对值；任何 wrapper或
nested owner都不能通过再次调用RuntimeSession刷新期限。

### EV-I13：publication unavailable只安装 latch，不虚构 live catch-up

critical fact durable FULL但 publication unavailable时，RuntimeSession阻止新 dispatch，
只允许 exact terminal maintenance和 bounded close。本阶段不回退 publisher high-water，
也不宣称存在 pending publication interval owner；durable catch-up继续属于 D3。

### EV-I14：compaction publication terminalization必须冻结 active-run scope

compaction attempt admission必须在 Host/RuntimeSession state lock内冻结
`CompactionPublicationTerminalizationScope`。publication latch后的唯一动作取决于该
scope：

- pre-run/manual且没有 active run：完成 compaction自身 terminal maintenance后关闭
  session，不伪造 `RunEnd`；
- mid-turn且存在 active run：关闭 exact window/account，并写 publication-latched aborted
  `RunEnd`；
- scope与实际 active run/window/account不一致：fail closed，不能在 latch后重新猜测。

### EV-I15：historical Inspector不能从 durable absence猜测 process-local receipt

`CompactionCandidateProjectionReceipt` 只属于 live Host/RuntimeSession owner。历史
Inspector只能展示 durable producer event/outbox/candidate-pool能够证明的状态；没有 durable
authority时必须显示 `not_durably_observable`。不得把“没有 outbox row”解释成
`not_requested`、`preparation_failed`、`owner_installation_failed`、`owner_installed`或
`candidate_frozen`。

### EV-I16：terminal-maintenance lease必须是不可伪造的 borrower handle

`PublicationTerminalMaintenanceLeaseIdentity` 只描述 join identity，不授予写权限。真正
capability必须是 RuntimeSession私有签发、borrower-scoped、按对象身份与 ISSUED generation
校验的 `PublicationTerminalMaintenanceLease` handle。仅复制 identity、lease ID或 owner
kind不能通过 writer admission。

---

## 5. Fingerprint 与 bounded DTO 通则

新增 durable facts 统一继承 `FrozenFactBase`，每个 class 必须有：

- literal `schema_version`；
- `extra="forbid"`；
- 中央 `register_durable_fact(...)`；
- domain-separated canonical JSON SHA-256；
- 唯一 factory；
- nested fingerprint join；
- round-trip / tamper / max-bound tests。

8 个新 outer event class 也必须显式声明 `ConfigDict(extra="forbid")`。不能依赖
`EventBase` 当前的默认 extra 行为，否则 decoder 可能静默忽略未知 durable 字段。

这 8 个 event 的 producer candidate 必须以空 `metadata` 构造；需要 durable 的
correlation/attribution 一律进入 typed field。`RuntimeSession._with_default_metadata()` 是
唯一允许在 commit 前注入 metadata 的 owner，用于 child runtime 已冻结的
`default_event_metadata` attribution。最终 stored event 必须满足：

```text
stored.metadata == recursively_frozen(RuntimeSession.default_event_metadata)
```

main RuntimeSession 的默认 metadata 为空，因此 stored metadata 也为空。producer 自填、
覆盖或增加任意 metadata key 必须拒绝；RuntimeSession 注入值必须通过现有
`_require_default_metadata_present()` exact validation。semantic/reducer factory不得从该
attribution map读取 lifecycle、reason或replay authority。这样既保留 child attribution，
也不会借 inherited free-form metadata恢复第二个 `CustomEvent`。

`RuntimeSession.__post_init__()` 必须对 `default_event_metadata` 做 recursively immutable
owned copy；后续 caller修改原 dict不能改变 event payload。candidate validation发生在
default attribution注入前，stored validation发生在注入后。

统一编码：

```text
fingerprint =
    SHA-256(
        UTF8(domain_separator)
        || 0x00
        || canonical_json_bytes(payload_without_own_fingerprint)
    )
```

禁止：

- caller 自报 fingerprint；
- unordered dict/list 直接进入 semantic identity；
- raw exception message 无界进入 ledger；
- raw MCP response values 进入 lifecycle event；
- 一个字段同时保存可由另一个字段精确派生的第二真值。

统一物理上限：

| 项目 | V1 上限 |
|---|---:|
| identifier | 512 UTF-8 bytes |
| tool/server name | 256 UTF-8 bytes |
| error type | 128 UTF-8 bytes |
| redacted diagnostic message | 1,024 UTF-8 bytes |
| MCP response keys | 64 |
| one key | 256 UTF-8 bytes |
| MCP input requests per round | 64 |
| total user-visible MCP input request JSON | 64 KiB |
| total prepared MCP response JSON（process-local） | 64 KiB |
| tool terminal references per evidence attempt | resolved parallel-tool cap，绝对不超过 128 |
| publication-latched RunEnd source refs | 16 |
| canonical event | 256 KiB existing hard cap |

超过上限必须在 candidate prepare 阶段产生 typed failure，不得依赖 final serialization 才
失败。

---

## 6. 七个 CustomEvent replacements 与 MCP closure event

### 6.1 一次性映射

| 旧 CustomEvent | 新 EventType | 新 class |
|---|---|---|
| `mcp_input_required_expired` | `MCP_INPUT_REQUIRED_EXPIRED` | `McpInputRequiredExpiredEvent` |
| `mcp_input_required_resolved` | `MCP_INPUT_REQUIRED_RESOLUTION_SUBMITTED` | `McpInputRequiredResolutionSubmittedEvent` |
| `mcp_input_required_binding_changed` | `MCP_INPUT_REQUIRED_BINDING_CHANGED` | `McpInputRequiredBindingChangedEvent` |
| `mcp_input_required_resume_failed` | `MCP_INPUT_REQUIRED_RESUME_FAILED` | `McpInputRequiredResumeFailedEvent` |
| `compaction_requested` | `CONTEXT_COMPACTION_REQUESTED` | `ContextCompactionRequestedEvent` |
| `mid_turn_compaction_skipped` | `MID_TURN_CONTEXT_COMPACTION_SKIPPED` | `MidTurnContextCompactionSkippedEvent` |
| `tool_result_persistence_failed` | `TOOL_RESULT_EVIDENCE_PROJECTION_FAILED` | `ToolResultEvidenceProjectionFailedEvent` |
| 无旧 CustomEvent；新增 lifecycle closure | `MCP_INPUT_REQUIRED_INTERACTION_CLOSED` | `McpInputRequiredInteractionClosedEvent` |

`resolved` 改名为 `resolution_submitted` 是语义修正：durable fact 只证明 Host 接受了一次
resolution，不证明 MCP request 已成功恢复或 tool call 已 terminal。

所有 8 类 event 必须显式传入 deterministic ID，禁止使用 `EventBase` default UUID：

| event | stable ID basis |
|---|---|
| resolution submitted | resume boundary event ID + interaction round + attempt ordinal |
| expired | resolution-submitted event ID + `"expired"` |
| binding changed | resolution-submitted event ID + `"binding_changed"` |
| resume failed | resolution-submitted event ID + `"resume_failed"` |
| interaction closed | suspension event ID + optional resolution/resume-failed event IDs + closure reason |
| compaction requested | run ID + current tool-result batch accumulator |
| mid-turn skipped | process segment/safe-point attempt ID + reason |
| evidence projection failed | projection contract ID + current tool-result batch accumulator |

ID 使用 domain-separated SHA-256 的 canonical suffix。first prepared candidate由 boundary、
terminal或hook attempt owner冻结；NONE重试同一 candidate，FULL adoption使用同一 ID，
UNKNOWN保留 owner。不得在 publication failure/caller retry时重新生成 UUID。

### 6.2 MCP typed suspension source

```python
class McpInputRequiredInteractionSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_interaction.v1"]
    interaction_id: str
    tool_call_id: str
    tool_name: str
    server_id: str
    round_count: int
    interaction_semantic_fingerprint: str
```

```python
class McpUserVisibleInputRequestFact(FrozenFactBase):
    schema_version: Literal["mcp_user_visible_input_request.v1"]
    key: str
    method: str
    user_visible_params: FrozenJsonObjectFact
    params_semantic_fingerprint: str
    request_fingerprint: str


class McpInputRequiredRequestEnvelopeFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_request_envelope.v1"]
    protocol_version: str | None
    ordered_user_visible_input_requests: tuple[
        McpUserVisibleInputRequestFact, ...
    ]
    original_request_semantic_fingerprint: str
    request_state_semantic_fingerprint: str | None
    request_envelope_semantic_fingerprint: str


class McpPendingLeaseReservationIdentityFact(FrozenFactBase):
    schema_version: Literal["mcp_pending_lease_reservation_identity.v1"]
    reservation_id: str
    interaction_id: str
    binding_identity: McpBindingIdentityFact
    reservation_fingerprint: str


class McpInputRequiredSuspensionFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_suspension.v1"]
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope: McpInputRequiredRequestEnvelopeFact
    rollout_reservation_id: str
    rollout_reservation_fingerprint: str
    source_mcp_installation_id: str
    durable_deadline_utc: str | None
    deadline_policy_fingerprint: str
    predecessor_resolution_submitted_event_reference: (
        ContextEventReferenceFact | None
    )
    suspension_fact_fingerprint: str
```

Invariant：

- `1 <= round_count <= MAX_MCP_INPUT_REQUIRED_ROUNDS`；
- input request keys 必须 sorted、unique，且每轮不超过 64；
- durable request只保存经过 MCP input-request schema验证、实际可展示给用户的 typed
  requests；
- `original_request` 与 opaque `request_state` 的 raw value不进入 EventLog或 ArtifactStore，
  只保存 canonical semantic fingerprint；
- user-visible request总 canonical JSON不超过 64 KiB，超过时 fail closed；
- pending lease reservation中的 interaction/binding必须与 outer fields相等；
- interaction/tool/server identity 在同一 round 内不可改变；
- round 1 的 predecessor resolution必须为空；
- next suspended round 必须是 `round_count + 1`，复用同一个 pending lease reservation，
  并引用上一轮 exact `McpInputRequiredResolutionSubmittedEvent`。

`ToolExecutionSuspendedEvent` 同阶段 hard cut为：

```python
class ToolExecutionSuspendedEvent(EventBase):
    type: Literal[EventType.TOOL_EXECUTION_SUSPENDED]
    interaction_kind: Literal["mcp_input_required"]
    tool_call_id: str
    tool_name: str
    suspension: McpInputRequiredSuspensionFact
```

validator要求 outer call/name与 `suspension.interaction` 相等。旧 `payload:
dict[str, Any]` 物理删除；本仓库当前没有第二种 production tool suspension branch，未来若
新增，必须先增加另一 named discriminated fact，禁止恢复 generic payload。

suspension event ID必须从
`rollout_reservation_id + interaction_id + round_count + suspension_fact_fingerprint`
domain-separated确定性生成，禁止 default UUID。下一轮 predecessor ref因此可以稳定
exact-join，不依赖 scan。

MCP adapter在返回 `ToolExecutionSuspended` 前构造唯一 process-local
`PreparedMcpInputRequiredSuspension`，它拥有：

```text
typed suspension fact
owned opaque original-request canonical bytes
owned opaque request-state canonical bytes
owned user-visible typed input requests
pending lease reservation handle
stable suspension event candidate identity
```

Agent只转交该 prepared owner，不再 `dict(suspended.payload)`、填默认字段或调用
`.get(...)` 推断 authority。pending lease在 suspension event FULL 后 confirm；NONE
abort/retry使用同一 prepared owner；UNKNOWN由 tool-terminal owner保留。

### 6.3 MCP source authority

```python
class McpInputRequiredSourceAuthorityFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_source_authority.v1"]
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope_semantic_fingerprint: str
    rollout_reservation_id: str
    rollout_reservation_fingerprint: str
    source_mcp_installation_id: str
    durable_deadline_utc: str | None
    deadline_policy_fingerprint: str
    predecessor_resolution_submitted_event_reference: (
        ContextEventReferenceFact | None
    )
    source_suspension_fact_fingerprint: str
    source_suspension_event_reference: ContextEventReferenceFact
    original_run_start_event_reference: ContextEventReferenceFact
    source_authority_fingerprint: str
```

唯一 factory 必须 exact-read `ToolExecutionSuspendedEvent` 并验证：

- same runtime ledger；
- same run/turn/reply；
- event type与 `interaction_kind == "mcp_input_required"`；
- source authority中的 typed identity逐字段等于 event nested suspension；
- same tool call/name、interaction ID/server/round；
- exact binding、pending lease reservation、request envelope、deadline与 predecessor ref；
- source sequence 不晚于 resume boundary preparation high-water。

source authority保存 request envelope semantic fingerprint，不复制 user-visible request
正文；唯一 factory通过 source suspension event exact-read验证 fingerprint与 typed request
内容。opaque original request/request-state从未成为 durable recovery carrier。

MCP suspension candidate preparation同时冻结：

- process-local monotonic deadline，用于 live timer；
- canonical `deadline_utc`，写入 suspension payload并由 source authority exact验证；
- deadline policy fingerprint。

V1 deadline policy是固定 composition-root constant：

```text
context_fingerprint(
    "mcp-input-required-deadline-policy:v1",
    "live=monotonic;durable=canonical-utc;restart=terminalize-lease-unavailable"
)
```

`McpInputRequiredExpiredEvent` 只允许 source authority中的 `durable_deadline_utc` 非空，
且 event `created_at` 不早于该 UTC。
因此 expiry不是 Agent 临时自报的自由 reason。

`PendingMcpInputRequired` 改为保存 exact suspension reference与 typed
`McpInputRequiredSuspensionFact`，并通过 process-local prepared owner访问 opaque
request/request-state bytes。live suspend只有在 `ToolExecutionSuspendedEvent` FULL、
publication summary为 `completed | enqueued` 且没有 publication errors后才安装并向 Host
暴露 WAITING_USER。FULL但 publication unavailable走第 8.3 节
`suspension_publication_unavailable` closure，不得安装 pending interaction。

`SESSION_REOPEN` 不恢复 `PendingMcpInputRequired` 给用户继续填写：原
`McpPendingLeaseOwner` 已随进程消失，长期 MCP contract禁止重新 acquire同名 binding冒充
原 lease。reopen recovery必须执行第 7.6 节 typed lease-unavailable terminalization，
不得从旧 `LoopState.pending_interaction_payload` 猜测 raw protocol state。

transcript reducer 的 pending-interaction state必须保留该 exact reference与 suspension
fingerprint，restart从 reducer snapshot恢复。

Host boundary fold 还必须把 committed
`McpInputRequiredResolutionSubmittedEvent` 的 `ContextEventReferenceFact` 安装进：

- `CommittedInteractionResumeBoundary`；
- `RunWorkingSet.latest_mcp_input_required_resolution_ref`；
- 交给 Agent resume driver 的 process-local owner。

后续 expired/binding-changed/resume-failed candidate只能消费该 exact ref，禁止按
interaction ID 回扫 EventLog。

### 6.4 Resolution receipt 与 immutable process owner

```python
class McpInputRequiredResolutionSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_resolution.v1"]
    cancelled: bool
    ordered_response_keys: tuple[str, ...]
    response_payload_receipt_fingerprint: str
    resolution_semantic_fingerprint: str
```

规则：

- keys 必须 sorted + unique；
- 最多 64 个；
- response payload 先递归 freeze，再用独立 domain hash；
- raw values 不进入 event；
- `response_payload_receipt_fingerprint` 是 producer commitment，不是可重建 payload 的 proof；
- crash 后不得从 hash 伪造 response，existing Host boundary recovery 只能继续已冻结的
  process owner，或 typed terminalize，不能重新调用 provider。

Host public DTO只活到 synchronous admission factory。第一次 await 前必须构造：

```python
@dataclass(frozen=True, slots=True)
class PreparedMcpResponseEntry:
    key: str
    canonical_response_json_bytes: bytes
    response_semantic_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedMcpInputRequiredResolution(FrozenRuntimeStateBase):
    source_suspension_event_reference: ContextEventReferenceFact
    source_suspension_fact_fingerprint: str
    interaction_id: str
    cancelled: bool
    ordered_response_entries: tuple[PreparedMcpResponseEntry, ...]
    resolution_semantic: McpInputRequiredResolutionSemanticFact
    prepared_resolution_fingerprint: str
```

factory必须：

1. exact-match当前 `PendingMcpInputRequired`；
2. recursively validate JSON values；
3. 对每个 response生成 owned canonical JSON `bytes`；
4. 丢弃 caller dict/list alias；
5. 从 exact bytes构造唯一 resolution semantic；
6. 在调用 `HostIngressCoordinator.submit()` 前完成。

Host ingress `payload`、resume boundary preparation与 Agent resume router只允许持有
`PreparedMcpInputRequiredResolution`。MCP adapter调用前从 canonical bytes解析一个全新的
mutable provider DTO，并立即重算每项及 aggregate fingerprint；校验与 adapter handoff
之间不得出现 await。adapter拥有该新 copy，Host/Agent不得再修改。

retry复用同一个 prepared owner。若 process在 resolution-submitted FULL 后、adapter接管前
崩溃，event中的 commitment不能恢复 raw response或 pending lease；SESSION_REOPEN走
`session_reopen_lease_unavailable` closure并关闭 interaction/run，禁止伪造 response。

同一 suspension round允许 live retry，但每次 resolution必须有独立 attempt identity：

```python
class McpInputRequiredResolutionAttemptFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_resolution_attempt.v1"]
    round_count: int
    attempt_ordinal: int
    predecessor_resolution_submitted_event_reference: (
        ContextEventReferenceFact | None
    )
    predecessor_resume_failed_event_reference: ContextEventReferenceFact | None
    attempt_fingerprint: str
```

first attempt要求 ordinal 1且两个 predecessor为空。只有 lifecycle处于
`resume_failed` 时才能提交 ordinal + 1；此时两个 predecessor都 required，并分别 exact
指向上一 attempt的 resolution与 failure。新 suspension round重新从 attempt 1开始；
跨 round predecessor只由 suspension fact拥有。

```python
class McpInputRequiredResolutionSubmittedEvent(EventBase):
    type: Literal[EventType.MCP_INPUT_REQUIRED_RESOLUTION_SUBMITTED]
    source: McpInputRequiredSourceAuthorityFact
    resolution: McpInputRequiredResolutionSemanticFact
    attempt: McpInputRequiredResolutionAttemptFact
    resume_boundary_event_identity: StableEventIdentityFact
```

### 6.5 MCP terminal/failure events

```python
class McpInputRequiredExpiredEvent(EventBase):
    type: Literal[EventType.MCP_INPUT_REQUIRED_EXPIRED]
    resolution_submitted_event_reference: ContextEventReferenceFact
    terminal_tool_result_event_identity: StableEventIdentityFact


class McpInputRequiredBindingChangedEvent(EventBase):
    type: Literal[EventType.MCP_INPUT_REQUIRED_BINDING_CHANGED]
    resolution_submitted_event_reference: ContextEventReferenceFact
    terminal_tool_result_event_identity: StableEventIdentityFact
    source_binding: McpBindingIdentityFact
    effective_binding: McpBindingIdentityFact


class McpInputRequiredResumeFailedEvent(EventBase):
    type: Literal[EventType.MCP_INPUT_REQUIRED_RESUME_FAILED]
    resolution_submitted_event_reference: ContextEventReferenceFact
    failure_reason: Literal[
        "adapter_resume_error",
        "adapter_protocol_error",
    ]
    diagnostic: BoundedRuntimeFailureDiagnosticFact


class McpInputRequiredInteractionClosedEvent(EventBase):
    type: Literal[EventType.MCP_INPUT_REQUIRED_INTERACTION_CLOSED]
    source_suspension_event_reference: ContextEventReferenceFact
    source_resolution_submitted_event_reference: ContextEventReferenceFact | None
    source_resume_failed_event_reference: ContextEventReferenceFact | None
    closure_reason: Literal[
        "suspension_publication_unavailable",
        "resume_boundary_publication_unavailable",
        "resume_failed_publication_unavailable",
        "session_reopen_lease_unavailable",
        "child_pending_unsupported",
        "live_pending_lease_unavailable",
    ]
    terminal_tool_result_event_identity: StableEventIdentityFact


class PublicationLatchedRunTerminationFact(FrozenFactBase):
    schema_version: Literal["publication_latched_run_termination.v1"]
    reason: Literal[
        "mcp_active_interaction_publication_unavailable",
        "mcp_terminal_disposition_publication_unavailable",
        "mcp_closure_publication_unavailable",
        "mandatory_runtime_audit_publication_unavailable",
        "compaction_publication_unavailable",
    ]
    source_event_references: tuple[ContextEventReferenceFact, ...]
    source_events_accumulator: str
    termination_fact_fingerprint: str
```

`interaction_remains_open` 由 event type 派生，不保存冗余 bool；Inspector 由 event type
投影该状态。只有 `McpInputRequiredResumeFailedEvent` 允许继续 open，而且提交前后
Supervisor必须仍证明 exact pending lease owner存在。

expired、binding-changed与 resume-failed不重复嵌入 `source`；它们通过 exact
`resolution_submitted_event_reference` 唯一 join source authority。validator从 active
lifecycle snapshot验证该 ref，避免 source在多层 payload中形成 dual truth。

closure event的 nullability：

- `suspension_publication_unavailable`：resolution/resume-failed refs均为空；
- `resume_boundary_publication_unavailable`：resolution ref required；
- `resume_failed_publication_unavailable`：resolution与 exact resume-failed ref均 required；
- `session_reopen_lease_unavailable`：若历史已有 resolution则引用 latest，否则为空；
- `child_pending_unsupported`：child尚未 resolution时为空；
- `live_pending_lease_unavailable`：引用触发本次 resume的 resolution。

除 `resume_failed_publication_unavailable` 外，resume-failed ref必须为空。SESSION_REOPEN若
active lifecycle恰处于 `resume_failed`，则必须同时引用 latest resolution与 latest
resume-failed；不能只按 closure reason丢掉 attempt chain。

closure必须与 error ToolResult terminal同一个 accounted transaction；terminal output只
使用 typed source中可公开的 request摘要，不包含 opaque request state。RunEnd稍后通过
required closure ref关闭 run lifecycle，见第 7.6 节。

`RunEndEvent` 同阶段增加：

```python
mcp_input_required_closure_event_reference: ContextEventReferenceFact | None
publication_latched_termination: PublicationLatchedRunTerminationFact | None
```

后者只用于 publication latch触发的 aborted RunEnd。source refs必须是触发 latch的 exact
terminal disposition/ToolResult terminal、closure、mandatory audit或 compaction
Started/final terminal committed refs，最多 16 个且按 sequence严格递增。普通 RunEnd两字段
均为空；MCP closure RunEnd第一字段 required；terminal disposition已经关闭 interaction、
但 publication unavailable时不写第二个 closure，只要求
`publication_latched_termination`。`reason="compaction_publication_unavailable"` 时 MCP
closure ref必须为空，source refs按第 8.3 节精确包含 Started（若存在）与唯一最终
Completed/Failed。

`BoundedRuntimeFailureDiagnosticFact`：

```python
class BoundedRuntimeFailureDiagnosticFact(FrozenFactBase):
    schema_version: Literal["bounded_runtime_failure_diagnostic.v1"]
    error_type: str
    redacted_message: str
    redaction_profile_id: str
    redaction_contract_fingerprint: str
    diagnostic_fingerprint: str
```

message 统一经过 MCP redactor、UTF-8 bound 与 control-character normalization。

diagnostic factory 使用 closed redaction profile registry：

```text
mcp_input_required_resume_error.v1
execution_evidence_projection_error.v1
compaction_candidate_projection_preparation_error.v1
compaction_candidate_projection_owner_installation_error.v1
```

profile ID/contract fingerprint进入 diagnostic。unknown profile fail closed；禁止直接传入
`str(exc)`。

### 6.6 Context compaction request

```python
class ContextCompactionRequestFact(FrozenFactBase):
    schema_version: Literal["context_compaction_request.v1"]
    source: Literal["memory_hook_should_compact"]
    safe_point: Literal["after_tool_results"]
    basis_tool_result_terminal_event_references: tuple[
        ContextEventReferenceFact, ...
    ]
    basis_event_ids_accumulator: str
    request_semantic_fingerprint: str


class ContextCompactionRequestedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_REQUESTED]
    request: ContextCompactionRequestFact
```

该事件只表示 hook 请求，不表示 compaction 已执行或完成。

Agent 在每次 tool terminal batch后构造一个 process-local
`CurrentToolResultBatchReceipt`：

```python
class CurrentToolResultReceiptItem(FrozenRuntimeStateBase):
    result_block: ToolResultBlock
    tool_result_end_reference: ContextEventReferenceFact
    terminal_projection_reference: ContextEventReferenceFact
    tool_call_id: str
    result_semantic_fingerprint: str
    item_fingerprint: str


class CurrentToolResultBatchReceipt(FrozenRuntimeStateBase):
    ordered_items: tuple[CurrentToolResultReceiptItem, ...]
    ordered_item_fingerprints_accumulator: str
```

receipt 只包含本次刚 FULL 的结果，按 provider tool-call order排列，并受 resolved
parallel-tool cap约束。每个 `result_block` 必须是 owned deep copy，不能与 mutable
LoopState alias。

每个 item的中央 factory必须 exact-read并验证：

- `tool_result_end_reference` 指向唯一 `ToolResultEndEvent`；
- `terminal_projection_reference` 指向唯一
  `ToolResultTerminalProjectionCommittedEvent`；
- 两个 event与 `result_block.id/tool_call_id` 相同；
- terminal projection中的 canonical result block tool name/state/content semantic与
  `result_block` 相同；
- `result_semantic_fingerprint` 从 terminal projection semantic重算；
- event refs的 runtime、run、turn、reply与当前 batch authority相同。

平行 `result_blocks` / `terminal_event_references` tuple禁止保留；它允许合法元素发生
cross-pair。compaction request与 evidence projection hook都消费 ordered items；不得从
累计 `state.tool_results` 重新推断“本次 batch”。

`basis_tool_result_terminal_event_references` 必须非空并与
`tuple(item.tool_result_end_reference for item in receipt.ordered_items)` exact相等；
`basis_event_ids_accumulator` 由 ordered refs中央重算。

当前 `state.compacted = True` 必须从该 producer 删除。`LoopState.compacted` 只允许由
`RuntimeContextCompactor` 在收到 exact `ContextCompactionCompletedEvent` 后设置。若未来
确实需要 run-local pending request，使用独立 process-local `compaction_requested` state，
不得复用 completed bool。

request event交给第 6.9 节 mandatory audit owner。只有 receipt FULL 后才启动对应
compaction preparation；NONE/cancellation不得通过重新执行 hook产生第二个 event ID。

### 6.7 Mid-turn compaction skip

```python
class MidTurnCompactionSkipFact(FrozenFactBase):
    schema_version: Literal["mid_turn_context_compaction_skip.v1"]
    reason: Literal[
        "current_run_start_missing",
        "no_compactable_prefix_before_current_run",
        "current_run_tail_missing",
        "current_run_rendered_tail_missing",
    ]
    current_run_start_event_reference: ContextEventReferenceFact | None
    safe_point: Literal["before_followup_model_call"]
    skip_semantic_fingerprint: str


class MidTurnContextCompactionSkippedEvent(EventBase):
    type: Literal[EventType.MID_TURN_CONTEXT_COMPACTION_SKIPPED]
    skip: MidTurnCompactionSkipFact
```

Invariant：

- `current_run_start_missing` 是唯一允许 RunStart ref 为 `None` 的 branch；
- 其他 branch 必须有 exact RunStart ref；
- `max_compactable_sequence` 不持久化，由
  `current_run_start_event_reference.sequence - 1` 精确派生；
- process-local guard reasons（`state_not_running`、`pending_interaction` 等）不写 durable
  event，继续只存在于 `MidTurnCompactionResult`；
- 不接受任意 reason string。

一旦 typed skip candidate已冻结，就交给第 6.9 节 mandatory audit owner；safe point只在
FULL后返回 durable skipped，不能把 NONE降级为本地 diagnostic。

### 6.8 ToolResult evidence projection failure

```python
class ToolResultEvidenceProjectionSourceFact(FrozenFactBase):
    schema_version: Literal["tool_result_evidence_projection_source.v1"]
    tool_call_id: str
    tool_result_end_reference: ContextEventReferenceFact
    terminal_projection_reference: ContextEventReferenceFact
    result_semantic_fingerprint: str
    source_fingerprint: str


class ToolResultEvidenceProjectionFailureFact(FrozenFactBase):
    schema_version: Literal["tool_result_evidence_projection_failure.v1"]
    projection_contract_id: Literal["execution_evidence_persistence"]
    projection_contract_version: Literal["1"]
    ordered_tool_result_sources: tuple[
        ToolResultEvidenceProjectionSourceFact, ...
    ]
    ordered_source_fingerprints_accumulator: str
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    failure_semantic_fingerprint: str


class ToolResultEvidenceProjectionFailedEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_EVIDENCE_PROJECTION_FAILED]
    failure: ToolResultEvidenceProjectionFailureFact
```

语义：

- `ExecutionEvidencePersistenceHook.after_tool_results()` 改为接收第 6.6 节
  `CurrentToolResultBatchReceipt`，不再每轮消费累计 `state.tool_results`；
- current invocation 最大结果数由 resolved parallel-tool policy约束，绝对不超过 128；
- 每个 durable source fact从对应 `CurrentToolResultReceiptItem` 唯一构造，End ref、
  projection ref、call ID与 result semantic必须逐项相等；
- ordered source accumulator中央重算；
- referenced ToolResult terminal facts 已经 FULL；
- event 不声称 execution-evidence ledger 完全没有部分写入；
- event FULL 后 Agent 可以继续；
- event由第 6.9 节 mandatory audit owner提交；NONE重试同一 candidate，
  UNKNOWN/PARTIAL安装 audit latch，不得静默丢失 mandatory failure audit；
- Inspector 显示 `canonical_tool_result_status=committed` 与
  `evidence_projection_status=failed`，不能显示“ToolResult persistence failed”。

### 6.9 Session-owned mandatory audit owner

以下四类 event不是 compaction core terminal或 ToolResult atomic companion，但本规格要求
live operation在继续前证明其 audit FULL：

```text
McpInputRequiredResumeFailedEvent
ContextCompactionRequestedEvent
MidTurnContextCompactionSkippedEvent
ToolResultEvidenceProjectionFailedEvent
```

它们统一使用一个 `RuntimeSessionMandatoryAuditOwner`，禁止每个 producer各写一套
`while retry`：

```python
MandatoryRuntimeAuditKind = Literal[
    "mcp_input_required_resume_failed",
    "context_compaction_requested",
    "mid_turn_context_compaction_skipped",
    "tool_result_evidence_projection_failed",
]


@dataclass(frozen=True, slots=True)
class MandatoryRuntimeAuditReceipt(FrozenRuntimeStateBase):
    owner_id: str
    audit_kind: MandatoryRuntimeAuditKind
    candidate_event_id: str
    candidate_payload_fingerprint: str
    attempt_generation: int
    status: Literal["full", "reconciliation_required"]
    committed_event_reference: ContextEventReferenceFact | None
    publication_summary: Literal[
        "completed",
        "enqueued",
        "unavailable",
        "failed_after_commit",
    ] | None
    publication_errors: tuple[EventPublicationError, ...]
```

session-owned attempt状态固定为：

```text
CANDIDATE_FROZEN
    -> WRITING(g)
    -> CONFIRMING(g)
    -> FULL
       | RETRYABLE_NONE -> WRITING(g + 1)
       | RECONCILIATION_REQUIRED
```

owner由 `RuntimeSession` composition构造，只依赖 typed event write service，不持有
`EventLog` mutation capability。Agent、Host hook与 compactor取得同一个 session owner
port，不得各自创建实例。

规则：

- owner key为 `(runtime_session_id, audit_kind, stable event id)`；
- 第一次 admission冻结完整 payload、stable ID与第 6.11 节 deadline budget；
- retry只能推进 attempt generation，不能重新构造 candidate；
- 每次 physical write都调用第 6.10 节
  `RuntimeSession.write_events_with_deadline(...)`，传入第一次 admission冻结的同一个
  `ordinary_deadline_monotonic`；
- 同 key/same payload加入 shared completion；same key/different payload fail closed；
- caller cancellation只 detach waiter，不能取消 physical owner；
- `NONE` 在同一 ordinary deadline内 bounded backoff重试；
- `UNKNOWN/PARTIAL` 进入 reconciliation并安装 session audit latch；
- durable FULL但 publication为 unavailable/failed-after-commit时不重写 candidate；
  receipt保留 FULL，RuntimeSession publication latch阻止下一次 dispatch；
- `status == "full"` 时 committed ref/publication summary required；
  `reconciliation_required` 时两者可空但 stable candidate identity仍 required；
- deadline耗尽时不得继续 model/tool dispatch；owner保留为 reconciliation required；
- Host close先停止 admission，再用共享 close deadline drain全部 owner；
- RuntimeSession committed reducer从 exact receipt fold，不允许 producer另查 EventLog。

这是一项 process-lifetime mandatory audit保证，不是 durable hook job。若进程在 candidate
首次 FULL 前崩溃，V1不伪造 audit；该 crash-to-job/outbox能力仍属于 D3。evidence
projection failure event只记录 failure authority，不负责重试 graph projection本身。

### 6.10 Shared absolute-deadline RuntimeSession API

当前 `RuntimeSession.write_event()` / `write_events()` 每次调用都会创建新 deadline，无法
实现第 6.9 节的“同一 candidate全程不续期”。本阶段新增唯一高层入口：

```python
async def write_event_with_deadline(
    self,
    event: AgentEvent,
    *,
    deadline_monotonic: float,
    expected_last_sequence: int | None = None,
    state: LoopState | None = None,
    publication_terminal_maintenance_lease: (
        PublicationTerminalMaintenanceLease | None
    ) = None,
) -> EventWriteResult:
    ...


async def write_events_with_deadline(
    self,
    events: Sequence[AgentEvent],
    *,
    deadline_monotonic: float,
    expected_last_sequence: int | None = None,
    state: LoopState | None = None,
    transaction_companion: EventLogTransactionCompanion | None = None,
    publication_terminal_maintenance_lease: (
        PublicationTerminalMaintenanceLease | None
    ) = None,
) -> EventWriteResult:
    ...
```

现有 `write_event(s)` 只负责生成一次 default deadline并委托新接口。新接口必须复用现有：

```text
terminal projection preparation
candidate validation
materialization account / physical reservation
committed reducers
publication handoff
typed FULL/NONE/UNKNOWN outcome
```

同一个由 frozen deadline budget选出的 absolute deadline传入本次 write的全部步骤和
confirmation；任何 nested owner不得再次调用 `new_deadline_monotonic()`。deadline到期后
返回/抛现有 typed NONE或UNKNOWN outcome，
不能只取消 waiter而留下使用更晚 deadline的 physical writer。

mandatory audit owner和 `RuntimeSessionCompactionEventCommitPort` 必须调用该接口；它们
不得直接调用低层 event-write service。每个 mandatory stable candidate与每个 compaction
attempt只冻结一次第 6.11 节 budget，NONE retry、terminalization与 confirmation只能选择
其中预先冻结的 ordinary/terminal deadline。

`PublicationTerminalMaintenanceLease` 只能由 `RuntimeSession` 在安装第 8.3 节
publication latch后签发。它是 process-local、borrower-scoped capability；durable-like
identity与实际 capability handle必须分离：

```python
PublicationTerminalMaintenanceOwnerKind = Literal[
    "mcp_interaction_closure_bundle",
    "mcp_publication_latched_run_termination_bundle",
    "mandatory_audit_publication_latched_run_termination_bundle",
    "compaction_started_publication_failed_bundle",
    "compaction_publication_latched_run_termination_bundle",
]


@dataclass(frozen=True, slots=True)
class PublicationTerminalMaintenanceLeaseIdentity(FrozenRuntimeStateBase):
    lease_id: str
    runtime_session_id: str
    publication_latch_generation: int
    owner_kind: PublicationTerminalMaintenanceOwnerKind
    ordered_candidate_event_ids: tuple[str, ...]
    ordered_candidate_payload_fingerprints: tuple[str, ...]
    transaction_companion_fingerprint: str | None
    exact_ordered_batch_fingerprint: str
    terminal_deadline_monotonic: float


class PublicationTerminalMaintenanceLease:
    """Opaque process-local borrower capability; RuntimeSession constructs it."""

    __slots__ = (
        "__identity",
        "__issued_attempt_generation",
        "__issuer_token",
        "__valid",
    )

    def __init__(
        self,
        *,
        _issuer_token: object,
        identity: PublicationTerminalMaintenanceLeaseIdentity,
    ) -> None:
        ...

    @property
    def identity(self) -> PublicationTerminalMaintenanceLeaseIdentity: ...

    @property
    def issued_attempt_generation(self) -> int: ...

    @property
    def is_valid(self) -> bool: ...

    def __copy__(self) -> NoReturn:
        raise TypeError("publication maintenance lease is not copyable")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("publication maintenance lease is not copyable")

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        raise TypeError("publication maintenance lease is not serializable")


def issue_publication_terminal_maintenance_lease(
    self: RuntimeSession,
    *,
    owner_kind: PublicationTerminalMaintenanceOwnerKind,
    ordered_events: Sequence[AgentEvent],
    transaction_companion: EventLogTransactionCompanion | None,
    deadline_budget: RuntimeEventOperationDeadlineBudget,
) -> PublicationTerminalMaintenanceLease:
    ...
```

`PublicationTerminalMaintenanceLease` 的 production concrete class必须位于 RuntimeSession
内部模块，使用私有 constructor token，并拒绝 copy、pickle、dataclass/asdict与通用
serialization。公开类型只允许调用方把 opaque handle原样交还 writer；不能从 identity
重建 handle，也不能让 test fake实现同名 protocol冒充 capability。

issuer API必须在 RuntimeSession writer/coordinator lock内：

1. 验证 publication latch active且 generation精确相等；
2. 从 `ordered_events` 的稳定 ID与 canonical payload fingerprint中央重算 exact batch；
3. 验证 owner kind与第 8.3 节允许的 terminal transition一致；
4. 将 exact handle对象、identity与 `ISSUED(0)` 一起注册进 coordinator；
5. 返回 borrower handle，不暴露 coordinator token。

owner kind与 batch一一对应：

| owner kind | exact batch |
|---|---|
| `mcp_interaction_closure_bundle` | closure + error ToolResult terminal/projection + settlement |
| `mcp_publication_latched_run_termination_bundle` | window/account close + MCP closure/disposition RunEnd |
| `mandatory_audit_publication_latched_run_termination_bundle` | window/account close + mandatory-audit RunEnd |
| `compaction_started_publication_failed_bundle` | exact `started_publication` Failed + account/bookkeeping |
| `compaction_publication_latched_run_termination_bundle` | exact active window/account close + compaction RunEnd |

一个 owner kind不得接收另一个 row的 payload，即使 event IDs与 byte size碰巧相同。

RuntimeSession-owned coordinator维护以下状态，而不是把“one-shot”解释成 admission时立即
销毁：

```text
ISSUED(g)
    -> IN_FLIGHT(g)
         |-> NONE -> ISSUED(g + 1)
         |-> FULL -> CONSUMED
         |-> UNKNOWN/PARTIAL -> RECONCILIATION_REQUIRED
    -> INVALIDATED
```

规则：

- writer必须验证 `handle is coordinator_handle_by_id[lease_id]`，不能只比较值相等；
- handle.identity必须与 coordinator保存的 identity逐字段相等；
- `handle.issued_attempt_generation` 必须等于 coordinator当前 `ISSUED(g)`；stale generation
  或已经 invalidated/consumed的 handle一律拒绝；
- lease runtime/session与当前 writer相同；
- lease generation等于当前 publication latch generation；
- incoming ordered candidate IDs、payload fingerprints、transaction companion与
  `exact_ordered_batch_fingerprint` 必须逐项完全相等，禁止 subset；
- owner kind与 terminal-maintenance transition一致；
- caller deadline不晚于 lease deadline；
- writer admission以 CAS执行 `ISSUED(g) -> IN_FLIGHT(g)`；
- durable confirmation为 NONE时，同一 lease ID与 exact batch回到
  `ISSUED(g + 1)`；coordinator原子推进同一 opaque handle的可用 generation，不得重建
  candidates、创建值相等的新 handle或续期；
- 只有 FULL才进入 `CONSUMED` 并永久失效该 borrower handle；
- UNKNOWN/PARTIAL后进入 `RECONCILIATION_REQUIRED` 并永久拒绝该 handle；不得签发替代
  lease绕过 reconciliation；
- waiter cancellation只 detach，physical writer owner负责完成上述 transition。

closure + error ToolResult + settlement是一个 exact lease batch；随后的
window/account close + RunEnd使用第二张独立 lease。任何只写 closure、不写 ToolResult，
或只写 RunEnd、不关闭 window/account的 partial batch都在 precommit被拒绝。

ordinary producer、test fake和 maintenance purpose string都不能自行构造该 lease。
RuntimeSession close必须在 terminal owner drain后原子失效所有仍处于
`ISSUED` 的 handles并进入 `INVALIDATED`；若仍有 `IN_FLIGHT` physical owner则先按 close
deadline drain或进入 reconciliation，不能在写入中途假装 release。关闭后的 borrower不得
再进入 writer。

### 6.11 Terminal-maintenance deadline reserve

“不续期”不等于把全部时间消耗在 ordinary publication后再要求不可能的 terminal write。
V1 将当前 30 秒 Runtime event operation timeout解析成一次 process-local deadline budget：

```python
@dataclass(frozen=True, slots=True)
class RuntimeEventOperationDeadlineBudget(FrozenRuntimeStateBase):
    admitted_at_monotonic: float
    ordinary_deadline_monotonic: float
    terminal_deadline_monotonic: float
    terminal_maintenance_reserve_seconds: float
    policy_fingerprint: str
```

默认 policy固定为：

```text
total operation timeout                 = 30 seconds
terminal maintenance reserve            = 10 seconds
ordinary deadline                       = admitted_at + 20 seconds
terminal deadline                       = admitted_at + 30 seconds
```

custom timeout必须由 composition doctor证明 ordinary window至少 10 秒、terminal reserve
至少 5 秒且不超过 total timeout的一半；否则 production composition fail closed。两个
absolute timestamps在 stable candidate第一次writer admission时一起冻结，任何retry都
不能向后移动。Compaction模型调用开始和attempt scope冻结都不提前启动尚未构造的
Completed/Failed candidate预算。

- ordinary candidate write/publication/confirmation只使用 ordinary deadline；
- mandatory closure、`started_publication` Failed、window/account close与 RunEnd可以使用
  terminal deadline；
- Completed publication failure不需要新 compaction terminal，剩余 tail只用于 latch close；
- terminal deadline耗尽仍无法 FULL时，不承诺 live terminalization：owner进入
  reconciliation，session停止 admission并 bounded close；
- `SESSION_REOPEN` 使用 Host-open owner新冻结的独立 recovery deadline完成 canonical
  recovery；这不是原 live attempt续期。

所有可能触发第 8.3 节 terminalization matrix的 critical writer都必须先取得该 budget：

```text
MCP suspension/resolution/resume-failed/disposition/closure
mandatory runtime audit
compaction Started/Completed/Failed
publication-latched RunEnd
```

这些 owner禁止调用会直接消耗完整 30 秒的 legacy `write_event(s)` default deadline入口；
architecture test要求它们调用第 6.10 节 API。非 critical ordinary event仍可使用现有
default wrapper。

---

## 7. MCP input-required lifecycle 与原子边界

### 7.1 唯一状态机

```text
SUSPENDED(round)
    |
    +-> RESOLUTION_SUBMITTED(round, attempt=1)
    |       |
    |       +-> RESUME_FAILED(attempt=1)
    |       |       |
    |       |       +-> RESOLUTION_SUBMITTED(round, attempt=2..N)
    |       |
    |       +-> NORMAL_TOOL_RESULT_TERMINAL(exact resolution ref)
    |       +-> EXPIRED / BINDING_CHANGED(exact resolution ref + ToolResult)
    |       +-> NEXT_SUSPENSION(round+1, predecessor resolution ref)
    |       +-> CLOSED(resume_boundary_publication_unavailable)
    |
    +-> CLOSED(session_reopen_lease_unavailable)
    +-> CLOSED(child_pending_unsupported)
```

每个 transition都由 exact source event reference证明。call ID、sequence相邻或“当前只有一个
pending interaction”都不能替代 reference join。

### 7.2 Host boundary owns resolution submission

`resolve_mcp_input_required()` 已在 Host 持有：

- `PendingMcpInputRequired`；
- pre-await冻结的 `PreparedMcpInputRequiredResolution`；
- exact resume boundary identity；
- current MCP installation / exposure；
- RuntimeSession writer。

因此 `McpInputRequiredResolutionSubmittedEvent` 从 Agent producer 移到 Host resume boundary
bundle：

```text
pending MCP installation audits
+ CapabilityExposureResolvedEvent
+ RunInteractionResumeBoundaryEvent
+ McpInputRequiredResolutionSubmittedEvent
```

Host `_prepare_and_commit_resume_boundary()` 增加 typed resolution input，仅
`interaction_kind == "mcp_input_required"` 时 required。其他 resume branch 必须为 `None`。
`HostIngressAttemptOwner.payload` 保存 prepared owner而不是 public resolution DTO；runner、
boundary和 adapter从该 owner取得同一 semantic/payload identity。

同批 validator 强制：

- resolution event 引用 exact boundary candidate stable identity；
- interaction/source suspension 与 pending owner 完全一致；
- resolution semantic与 prepared canonical response bytes exact一致；
- attempt 1或 `resume_failed -> attempt + 1` predecessor chain合法；
- candidate order 固定；
- boundary event 必须紧邻 resolution-submitted event之前，replay 验证
  `resolution.sequence == boundary.sequence + 1`；
- FULL/NONE/UNKNOWN 与整个 boundary batch 一致；
- 不允许 boundary FULL、resolution event NONE。

`_fold_committed_resume_boundary()` 必须从 exact committed batch 提取 resolution event，
验证 stable identity并安装第 6.3 节的 working-set ref。找不到、重复或 payload
不一致均按 boundary batch structural failure处理，不能在 fold 后补写。

boundary FULL后，只有 publication summary为 `completed | enqueued` 且没有
`publication_errors`，Host才允许调用 MCP adapter。若 summary为
`unavailable | failed_after_commit`：

1. 安装第 8.3 节 `publication_reconciliation_required` latch；
2. 不调用 adapter；
3. 由 terminal maintenance owner提交
   `McpInputRequiredInteractionClosedEvent(
   closure_reason="resume_boundary_publication_unavailable")` + error ToolResult；
4. RunEnd引用该 closure并关闭 run。

不得把 interaction恢复成 WAITING_USER后让用户再次提交同一 resolution；该路径已经失去
可信的 live delivery owner。

### 7.3 Terminal MCP dispositions 与 ToolResult 同批

`expired` 与 `binding_changed` 不是独立日志，而是 terminal ToolResult 的原因 authority。
它们必须作为 `_emit_tool_result_and_record()` 的 typed companion candidate，与：

- ToolResult start/delta/end；
- ToolResult terminal projection；
- rollout settlement（若存在）；
- physical settlement；

在同一 RuntimeSession/accounted transaction 提交。

disposition event 保存 prepared ToolResult terminal stable identity；reducer 以同批 candidate
验证 exact call ID、ToolResult state、event-type disposition 和 terminal identity。

exact replay 还必须通过 materialization settlement/account transition证明 disposition 与
ToolResult terminal属于同一个 accounted one-shot batch；仅仅在 ledger 中相邻不构成
原子提交证明。

所有从 suspension恢复得到的 ToolResult必须持久化 exact source：

```python
class McpInputRequiredTerminalSourceFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_terminal_source.v1"]
    source_suspension_event_reference: ContextEventReferenceFact
    source_resolution_submitted_event_reference: ContextEventReferenceFact | None
    source_fingerprint: str
```

该 fact进入 `ToolResultEndEvent` 与 terminal projection semantic join；不能只保存在
process-local registry。normal resumed terminal要求 resolution ref非空，从而 reducer无需
按 call ID或时序猜测它属于哪次 attempt。

`ToolExecutionTerminalRegistry.freeze_terminal()` 增加：

```text
source_mcp_suspension_event_reference
source_mcp_resolution_submitted_event_reference
required_mcp_disposition_event_identity
required_mcp_closure_event_identity
```

matrix：

| path | suspension ref | resolution ref | disposition | closure |
|---|---|---|---|---|
| ordinary MCP call without suspension | - | - | - | - |
| normal resumed terminal | required | required | - | - |
| expired/binding-changed | required | required | required | - |
| suspension-publication closure | required | empty | - | required |
| boundary/resume-failed-publication/live-lease closure | required | required | - | required |
| SESSION_REOPEN/child closure before resolution | required | optional/empty | - | required |

commit port验证 companion与 source refs exact。这样不能通过绕过 Agent helper单独提交
terminal ToolResult。

禁止：

```text
disposition FULL
ToolResult NONE
```

以及：

```text
ToolResult FULL
disposition NONE
```

若该 atomic terminal batch durable FULL但 publication unavailable，interaction已经由
canonical ToolResult/disposition关闭。不得再写 `McpInputRequiredInteractionClosedEvent`；
第 8.3 节 terminal-maintenance owner只负责 window/account close与 publication-latched
RunEnd。

### 7.4 Resume failure

MCP adapter `resume_input_required()` 抛错时：

- restore original pending interaction process owner；
- supervisor重新证明 exact pending lease owner仍存在且 confirmed；
- freeze bounded redacted diagnostic；
- 将 stable `McpInputRequiredResumeFailedEvent` 交给第 6.9 节 mandatory audit owner；
- only after FULL且 publication summary为 `completed | enqueued` return WAITING_USER；
- NONE由该 owner使用同一 stable candidate retry；
- UNKNOWN 保留 owner并 latch interaction resume；
- 不产生 ToolResult terminal。

FULL后 state进入 `resume_failed`；下一次用户 resolution提交新的 Host boundary与新的
resolution event，必须使用第 6.4 节 attempt predecessor chain。若 lease borrow本身失败，
不能写 resume-failed继续等待，改走
`McpInputRequiredInteractionClosedEvent(closure_reason="live_pending_lease_unavailable")`。
若 ResumeFailed durable FULL但 publication unavailable，不向用户重新暴露 WAITING_USER，
改走第 8.3 节 `resume_failed_publication_unavailable` closure。

### 7.5 Lifecycle validator

新增唯一纯 validator：

```python
class McpInputRequiredLifecycleSnapshot(FrozenRuntimeStateBase):
    runtime_session_id: str
    source: McpInputRequiredSourceAuthorityFact
    state: Literal[
        "suspended",
        "resolution_submitted",
        "resume_failed",
        "closure_committed",
        "terminal",
    ]
    latest_resolution_attempt_ordinal: int | None
    latest_resolution_submitted_event_reference: ContextEventReferenceFact | None
    latest_resume_failed_event_reference: ContextEventReferenceFact | None
    terminal_disposition_event_reference: ContextEventReferenceFact | None
    terminal_closure_event_reference: ContextEventReferenceFact | None
    terminal_tool_result_event_reference: ContextEventReferenceFact | None
    terminal_run_end_event_reference: ContextEventReferenceFact | None
    terminal_run_end_publication_latched_fact_fingerprint: str | None
    ledger_through_sequence: int


def validate_mcp_input_required_lifecycle_batch(
    *,
    committed_prefix: McpInputRequiredLifecycleSnapshot,
    candidate_batch: tuple[AgentEvent, ...],
) -> McpInputRequiredLifecycleSnapshot:
    ...
```

snapshot 按一个 interaction round作用域构造，只保存 active/latest/terminal refs，不保存
完整 attempt历史 tuple，因此不会随 session无限增长。attempt predecessor由 incoming
event与 latest refs局部验证。normal ToolResult terminal与 next-round
`ToolExecutionSuspendedEvent` 也必须被 reducer识别：前者按 exact resolution ref关闭当前
round，后者验证 `round_count + 1` 与 predecessor后创建新的 scoped snapshot。

RuntimeSession committed reducer只常驻 active snapshot map；terminal fold后移除 resident
entry。Inspector 的历史视图按 event refs/有界 query重建，不把全历史重新塞回 live state。

state/nullability matrix 固定为：

```text
suspended            resolution=- failure=- terminal disposition=- tool result=-
resolution_submitted resolution=Y attempt=Y failure=- closure=- tool result=-
resume_failed        resolution=Y attempt=Y failure=Y closure=- tool result=-
closure_committed    closure=Y tool result=Y RunEnd=-
terminal             tool result=Y
                     disposition=Y only for expired/binding-changed
                     closure=Y iff special closure path
                     RunEnd=Y iff closure=Y or publication-latched fact=Y
                     publication-latched fact=Y iff RunEnd由 publication latch触发
```

它被以下 owner 共用：

- Host candidate preparation；
- RuntimeSession precommit validation；
- restart exact replay；
- Inspector projection。

禁止 Host、Agent、Inspector 各自实现不同的 string-name reducer。

### 7.6 SESSION_REOPEN 与 child pending terminalization

`SESSION_REOPEN` 一旦发现 durable active MCP suspension，必须先检查 exact
`McpPendingLeaseOwner`。新进程中该 owner必然不存在，因此不向 Host安装
`PendingMcpInputRequired`，而由 recovery terminalization owner冻结：

```text
error ToolResult terminal
+ McpInputRequiredInteractionClosedEvent(
      closure_reason="session_reopen_lease_unavailable"
  )
+ rollout/physical settlement
```

该 bundle FULL后，构造 `RunEndEvent`，其新增
`mcp_input_required_closure_event_reference` required指向 exact closure。RunEnd FULL后才
从 reducer移除 active interaction。NONE使用同一 ordered candidates、同一 maintenance
lease ID的下一 attempt generation重试；UNKNOWN/PARTIAL latch ledger reconciliation并
阻止 Host open。

closure bundle之后，第二个 accounted batch必须按固定顺序包含：

```text
ContextWindowClosedEvent
+ RolloutBudgetAccountClosedEvent
+ RunEndEvent(exact closure ref)
```

不能先写裸 RunEnd再补 window/account close。

child进入 MCP `WAITING_USER` 时执行同一算法，closure reason改为
`child_pending_unsupported`。child仍有 live lease，因此 only after closure + ToolResult
bundle FULL调用 `complete_pending_lease()`，随后 RunEnd引用 closure。禁止
`fail_committed_run()` 直接清空 pending state再单独写 RunEnd。

child process也可能在 suspension FULL、上述 live closure尚未提交时崩溃。child reopen
不得把它交给 generic dangling-child repair。唯一顺序为：

```text
fold child model-stream / control state
    -> fold exact child MCP lifecycle
    -> active child MCP suspension
    -> child_pending_unsupported closure
       + error ToolResult terminal
       + rollout/physical settlement
    -> child context-window close
       + child subaccount close
       + RunEnd(exact child closure ref)
    -> parent subagent-graph terminal reference
    -> generic dangling-child projection repair
```

reopen时原 child pending lease owner已经不存在，因此该路径不调用
`complete_pending_lease()`，也不重新 acquire binding；closure FULL本身是 interaction的
canonical terminal authority。child closure仍使用
`closure_reason="child_pending_unsupported"`，其 source suspension ref必须指向 child
ledger中的 exact active suspension。

`repair_dangling_children()` 必须在构造 `RECOVERED_INTERRUPTED` RunEnd前 fold child MCP
lifecycle。发现 active suspension时返回 `delegated_to_child_mcp_recovery`，不得写
RunEnd、不得先更新 parent graph。child RunEnd precommit使用与 main run相同的 active-MCP
guard；只有 closure + ToolResult FULL且 child window/subaccount close与 RunEnd同批 FULL
后，parent graph terminal reference才可提交。

specialized `ChildMcpLifecycleRecoveryOwner` 必须通过 child RuntimeSession writer或同等
accounted typed recovery port提交上述两批；不得复用 generic
`commit_quiescent_accounted_batch()` 直接拼一个无 closure的 RunEnd。generic helper只保留给
已经证明不存在 active MCP lifecycle的 child。

initial-suspension/resume-boundary/resume-failed publication failure与
live-lease-unavailable closure也使用相同两阶段 terminalization。
`RunEndEvent.mcp_input_required_closure_event_reference` 在不存在 MCP closure时必须为空；
存在 closure时 required且 exact runtime/run匹配。

`SESSION_REOPEN` 的唯一 pipeline为：

```text
model-stream / model-control recovery
    -> fold exact MCP lifecycle at recovered high-water
    -> MCP active-lifecycle recovery
    -> closure + error ToolResult + settlement
    -> window/account close
    -> RunEnd
    -> generic run-projection repair
```

`repair_dangling_runs_for_resume()` 遇到 active MCP lifecycle必须 delegate/skip，由上述 MCP
recovery owner先收口；它不得生成普通 recovered-interrupted RunEnd。RunEnd precommit
validator读取同一 committed MCP lifecycle snapshot：active suspension尚未具有 normal
terminal ToolResult或 closure FULL时，任何 caller（包括 generic repair）提交 RunEnd都
fail closed。只有 specialized lifecycle terminalization FULL后，generic projection repair
才能运行。

main-run与 child-run recovery使用同一个 typed MCP reducer，但各自从对应 runtime ledger
取得 exact refs与 account authority。禁止 parent ledger替 child写 closure，或用 parent
sequence/high-water证明 child lifecycle。

---

## 8. Compaction commit ownership hard cut

### 8.1 删除 direct port

物理删除：

- `DirectEventLogCompactionEventCommitPort`；
- `PendingCompactionEventCommit.event_log`；
- direct confirmation branch；
- `ContextCompactionService.__post_init__()` 的 fallback；
- `__all__` export；
- 所有 production/test import。

`ContextCompactionService` 改为 required fields：

```python
@dataclass(slots=True)
class ContextCompactionService:
    event_log: EventLog                 # read-only use
    archive: ArtifactStore
    llm_runtime: LLMRuntime
    runtime_session_id: str
    runtime_session: RuntimeSession
    event_commit_port: RuntimeSessionCompactionEventCommitPort
    ...
```

`__post_init__()` 验证：

```text
runtime_session.runtime_session_id == runtime_session_id
event_commit_port.runtime_session is runtime_session
runtime_session.event_log is event_log
```

测试不得通过缺省值恢复 direct writer。需要真实 commit 行为的 component test 使用
`InMemoryEventLog + RuntimeSession + RuntimeSessionCompactionEventCommitPort`。只验证 pure
service control flow 的测试可使用 `tests/support` 下显式 test port，但该 port 必须返回
typed `CompactionEventCommitResult`，且不得进入 `src/`。

### 8.2 正确归一化 publication-after-commit

`RuntimeSession.write_event()` / `write_events()` 的真实 contract是正常返回
`EventWriteResult`；它们不会仅因为 `publication_errors` 自动抛
`EventPublicationAfterCommitError`。只有 convenience wrappers `emit()` / `emit_many()`
执行该 raise。compaction port不得围绕不存在的 exception建立正常路径。

正常返回后必须直接检查：

```python
write = await runtime_session.write_event_with_deadline(
    event,
    deadline_monotonic=owner.deadline_budget.ordinary_deadline_monotonic,
)
publication_summary = (
    "failed_after_commit"
    if write.publication_errors
    else write.publication_status
)
```

只有 caller cancellation、commit exception或 legacy wrapper error进入 exception branch；
该 branch必须调用 `RuntimeSession.resolved_event_write_outcome(error)`，按
`FULL | NONE | UNKNOWN`消费原 candidate。FULL从 resolved result产生同一 receipt，NONE
保留同一 owner重试，UNKNOWN进入 reconciliation。

统一 receipt为：

```python
CompactionEventCommitResult(
    committed_event=...,
    publication_summary=...,
    publication_errors=...,
)
```

其中 summary closed union为：

```text
not_applicable
completed
enqueued
unavailable
failed_after_commit
```

`not_attempted` 使用 `not_applicable`，不伪造一个 publication status。`unavailable` 是
RuntimeSession真实状态：durable commit可能 FULL，但当前没有可证明的 publication
handoff；它不等于 `enqueued`，也不允许被压成无 error的 success。

本阶段同时 hard-cut `ContextCompactionFailedEvent.failure_stage`。closed union与唯一
ordering为：

```python
ContextCompactionFailureStage = Literal[
    "planning",
    "summarizer_resolution",
    "summarizer_input_build",
    "summarizer_provider_input_prepare",
    "started_append",
    "started_publication",
    "model_validation",
    "model_stream",
    "summary_validation",
    "artifact_write",
    "completed_append",
    "recovery_terminalization",
]
```

`summarizer_provider_input_prepare` 是当前 production service已经会产生、但旧 event schema
遗漏的真实 stage；本次必须与 `started_publication` 一起进入 event class、validator、
decoder golden和 Inspector projection，不能让 service发送 schema无法表达的 literal。
它仍是 pre-Started failure：`started_event_id` 必须为空，resolved summarizer target/call、
context ID、input estimate与 budget required，usage/reported-model fields为空，
`termination_kind="failed"`。

`started_publication` 是一个特殊的 post-commit/pre-model stage，validator不能简单沿用
`stage_index >= summarizer_input_build` 的通用 required-field规则。它的字段矩阵固定为：

```text
failure_stage                         = started_publication
started_event_id                      = required
termination_kind                     = failed
target_estimate                       = required
summarizer_target                     = empty
summarizer_call                       = empty
summarizer_context_id                 = empty
summarizer_input_estimated_tokens     = empty
summarizer_input_budget_tokens        = empty
summarizer_usage_status               = missing
summarizer_usage                      = empty
summarizer_estimated_input_tokens     = empty
summarizer_reported_model_id          = empty
```

这些 summarizer attribution已经由 exact `started_event_id` 指向的
`ContextCompactionStartedEvent` 拥有，Failed不得复制第二份。validator exact-read/fold
Started后验证同 runtime、compaction、window、target、estimate、terminal event identity与
Host boundary attribution。`started_publication` 之前不得存在 ModelStart；之后不得启动
summarizer model call。

其 stable Failed ID从 `Started.terminal_event_id + "started_publication"` 确定性派生。
同一个 Started至多一个 terminal；若 Started publication later被证明可用，也不能撤销已
FULL Failed或重新启动 summarizer。尚未 committed 的 one-shot summarizer provider-input
preparation必须由原 preparation owner typed-abandon。

compaction attempt在第一次 admission、任何 event write之前，必须在 Host/RuntimeSession
state lock内冻结 publication terminalization scope：

```python
@dataclass(frozen=True, slots=True)
class CompactionPublicationTerminalizationScope(FrozenRuntimeStateBase):
    scope_kind: Literal[
        "pre_run_without_active_run",
        "manual_without_active_run",
        "mid_turn_active_run",
    ]
    runtime_session_id: str
    active_run_id: str | None
    active_context_window_id: str | None
    active_rollout_account_id: str | None
    host_state_generation: int
    scope_fingerprint: str
```

nullability与 admission规则：

- `pre_run_without_active_run` / `manual_without_active_run`：三个 active identity必须为空；
- `mid_turn_active_run`：三个 active identity全部 required，并与当时 committed run/window/
  account精确匹配；
- trigger名为 manual但实际在 active run中执行时，仍分类为
  `mid_turn_active_run`，不能利用 “manual” 绕过 RunEnd；
- scope安装后不因 caller cancellation、Host状态变化或 publication failure重新计算；
- terminal owner若发现 frozen scope与当前 committed lifecycle冲突，进入 reconciliation，
  不猜测另一个分支。

该 DTO只进入 process-local attempt owner/result；durable Started与最终 Completed/Failed
继续携带既有 Host boundary/run attribution。terminal maintenance必须 exact join两者，
不能只相信 process-local scope。

publication failure/unavailable不能授权写一个与已 FULL completed event冲突的 failed
event，也不能被无声吞掉。唯一控制规则：

- Started FULL 后 summary为 `completed | enqueued`：允许启动 summarizer；
- Started FULL 后 summary为 `unavailable | failed_after_commit`：禁止启动 summarizer；
  原 compaction terminalization owner提交
  `ContextCompactionFailedEvent(failure_stage="started_publication")`，并阻止后续 model
  dispatch；
- Completed FULL 后 summary为 `completed | enqueued`：才允许同步安装并启动 independent
  candidate projection owner；
- Completed FULL 后 summary为 `unavailable | failed_after_commit`：Completed仍是
  canonical terminal，不再写 Failed；不得安装/启动 candidate projection owner，
  prepared input由 attempt owner typed-abandon，receipt记为
  `suppressed_by_publication_latch`，caller/Host进入 publication reconciliation；
- collector先形成 exact `ContextCompactionAttemptResult`；
- caller随后收到 `ContextCompactionPublicationFailedAfterCommit(result)`，不得继续下一次
  model dispatch；
- RuntimeSession安装第 8.3 节 publication latch。

memory proposal不参与 core publication aggregate；它有第 9.3 节独立 receipt。publication
latch不得向 memory projection签发 terminal-maintenance lease，不能把非终结性的 candidate
mutation伪装成 close maintenance。

### 8.3 Publication reconciliation V1

本阶段不实现 live publisher catch-up。当前 publisher在 loop unavailable时会推进
`enqueued_through_sequence`，没有保留可重试 interval；规格不得声称不存在的 catch-up
owner。

`RuntimeSession` 新增 process-local：

```text
_publication_reconciliation_required: bool
_publication_latch_generation: int
publication_reconciliation_required property
latch_publication_reconciliation_required(...)
```

它进入 `RuntimeSession.reconciliation_required` 总 gate。以下 critical facts出现
`publication_status == "unavailable"` 或非空 `publication_errors` 时安装 latch：

```text
MCP suspension/resolution/disposition/closure
mandatory audit events
compaction Started/Completed/Failed
RunEnd carrying MCP closure or publication-latched termination
```

MCP、compaction 与 RunEnd terminalization matrix固定为：

按最具体 event branch匹配；`McpInputRequiredResumeFailedEvent` 使用专用 MCP row，不落入
“其他 mandatory audit”。

| FULL但 publication unavailable/failed 的 event | interaction状态 | terminal maintenance |
|---|---|---|
| 初次 `ToolExecutionSuspendedEvent` | active，live lease仍在 | 写 `suspension_publication_unavailable` closure + error ToolResult + settlement；FULL后释放 lease；再以第二张 lease关闭 window/account并写 aborted RunEnd |
| `McpInputRequiredResolutionSubmittedEvent` / resume boundary | active，live lease仍在 | 写 `resume_boundary_publication_unavailable` closure bundle；FULL后释放 lease；第二张 lease写 window/account close + RunEnd |
| `McpInputRequiredResumeFailedEvent` | active，live lease仍在 | 写 `resume_failed_publication_unavailable` closure bundle，exact引用 ResumeFailed；FULL后释放 lease；第二张 lease写 window/account close + RunEnd |
| expired/binding disposition + ToolResult terminal batch | interaction已经 terminal | 不写 closure；直接以 terminal-maintenance lease写 window/account close + aborted RunEnd，携带 `mcp_terminal_disposition_publication_unavailable` fact |
| 任意 MCP closure + ToolResult terminal batch | interaction已经 terminal | 不写第二个 closure；FULL authority足以释放 live lease；写 RunEnd并携带 closure ref；若 closure本身 publication unavailable，再携带 `mcp_closure_publication_unavailable` fact |
| 其他 mandatory audit | 由原 domain state决定，但不得继续 dispatch | 不伪造 domain success；关闭 window/account并写携带 `mandatory_runtime_audit_publication_unavailable` fact 的 aborted RunEnd |
| compaction Started | attempt尚无 terminal | 先用 `compaction_started_publication_failed_bundle` lease写 exact `started_publication` Failed；随后按 frozen compaction scope选择“无 active run关闭 session”或“mid-turn关闭 window/account + aborted RunEnd” |
| compaction Completed/Failed terminal | attempt已经 terminal | 不写 conflicting compaction terminal；按 frozen compaction scope选择“无 active run关闭 session”或“mid-turn关闭 window/account + aborted RunEnd” |
| RunEnd | durable run已经 terminal | 不再写任何 event；只执行 bounded publisher/session close，reopen/Inspector读取 canonical RunEnd |

前三个 active-interaction row的 RunEnd同时携带 exact closure ref与
`mcp_active_interaction_publication_unavailable` fact；该 fact source refs至少包含最初触发
latch的 event和 closure event。closure自身 publication unavailable的 row使用
`mcp_closure_publication_unavailable`。

所有由 terminal-maintenance owner新写的 RunEnd均使用
`status="aborted"`、`stop_reason=ABORTED`、
`terminalization_kind=HOST_TEARDOWN`、`abort_kind="host_teardown"`。RunEnd precommit必须
exact验证 publication-latched fact的 source refs已经 FULL。active MCP closure bundle与
后续 window/account-close + RunEnd是两次独立原子 transaction，分别使用第 6.10 节的
exact-batch lease；不得把 lease拆成逐 event写入。

compaction publication分支进一步冻结为：

```text
PRE_RUN / manual without active run
    Started publication unavailable
        -> ContextCompactionFailedEvent(started_publication) FULL
        -> close session
        -> no RunEnd
    Completed/Failed publication unavailable
        -> keep existing terminal
        -> close session
        -> no RunEnd

MID_TURN with active run
    Started publication unavailable
        -> ContextCompactionFailedEvent(started_publication) FULL
        -> window close + account close
           + RunEnd(
                 publication_latched_termination.reason
                     = "compaction_publication_unavailable"
             )
    Completed/Failed publication unavailable
        -> keep existing terminal
        -> window close + account close
           + same publication-latched aborted RunEnd
```

mid-turn `PublicationLatchedRunTerminationFact.source_event_references` 必须按 sequence严格包含：

1. attempt有 Started时，exact `ContextCompactionStartedEvent`；
2. exact最终 `ContextCompactionCompletedEvent` 或
   `ContextCompactionFailedEvent`。

pre-Started planning/input failure若没有 Started，则只引用 exact Failed terminal；不得伪造
Started ref。source accumulator从该 ordered tuple中央重算。RunEnd batch使用
`compaction_publication_latched_run_termination_bundle` lease，并 exact绑定 frozen
run/window/account identities。无 active run分支不得创建空 window/account close或 synthetic
RunEnd。

若第 6.11 节 terminal tail不足以完成上述 live terminalization，stable owner进入
reconciliation，禁止继续 dispatch；reopen按第 7.6/12.3 节用独立 recovery deadline收口，
而不是在 live attempt中续期。

latch后：

- 禁止新的 Host ingress、model dispatch、tool admission与 ordinary domain mutation；
- 只允许已经冻结的 terminal maintenance与 bounded Host close；
- 不回退 publisher high-water，不尝试在 live session补发；
- committed event仍是 canonical truth，Inspector/reopen从 EventLog解释；
- session terminal maintenance收口后关闭，不能清 latch继续运行。

terminal maintenance使用 RuntimeSession签发的 process-local
`PublicationTerminalMaintenanceLease`，绑定 latch generation、exact ordered batch、
owner kind与 terminal deadline，并遵循第 6.10 节 ISSUED/IN_FLIGHT 状态机；普通 caller
不能通过 boolean/purpose string绕过 latch。

durable subscriber retry、pending publication interval与跨 restart catch-up继续属于 D3，
本 hard cut不提前关闭。

### 8.4 cancellation owner

`PendingCompactionEventCommit` 只保留：

```text
candidate_event
RuntimeSession writer task
RuntimeSession
candidate deadline budget / attempt identity
frozen publication terminalization scope
```

每个Started/Completed/Failed candidate在其第一次writer admission时分别冻结一次
deadline budget。Started/Completed ordinary writes及同一candidate的NONE retry使用该
candidate的ordinary deadline；`started_publication` Failed是由Started authority预留的
terminal-maintenance分支，使用Started budget的terminal deadline。其他新terminal
candidate在首次writer admission取得自己的budget，不得继承模型调用开始时间，也不得在
retry/pending drain时续期。

状态：

```text
CANDIDATE_FROZEN
    -> COMMITTING
         |-> FULL
         |-> NONE -> COMMITTING
         |-> UNKNOWN/PARTIAL -> RECONCILIATION_REQUIRED
```

- caller cancellation 只 detach；
- pending task 是唯一 physical owner；
- FULL 使用 RuntimeSession resolved outcome；
- NONE 可以用同一 candidate、attempt generation + 1重试；
- 所有 COMMITTING attempt调用第 6.10 节 API并只能使用当前stable candidate首次
  writer admission冻结的 ordinary/terminal deadline；
- UNKNOWN/PARTIAL 不得转交 direct EventLog query owner。

---

## 9. Exact compaction attempt result

### 9.1 process-local DTO

```python
@dataclass(frozen=True, slots=True)
class CompactionCandidateProjectionRequestIdentity(FrozenRuntimeStateBase):
    request_id: str
    compaction_id: str
    expected_completed_event_id: str
    extractor_id: str
    extractor_version: str
    extractor_contract_fingerprint: str
    projection_policy_fingerprint: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedCompactionCandidateProjectionInput(FrozenRuntimeStateBase):
    request_identity: CompactionCandidateProjectionRequestIdentity
    owner_id: str
    summary_artifact_id: str
    summary_artifact_content_fingerprint: str
    owned_summary_canonical_utf8_bytes: bytes
    prepared_input_fingerprint: str


@dataclass(frozen=True, slots=True)
class CompactionCandidateProjectionReceipt(FrozenRuntimeStateBase):
    completed_compaction_event_reference: ContextEventReferenceFact
    request_identity: CompactionCandidateProjectionRequestIdentity | None
    status: Literal[
        "not_requested",
        "preparation_failed",
        "owner_installation_failed",
        "suppressed_by_publication_latch",
        "owner_installed",
        "candidate_frozen",
        "producer_bundle_full",
        "projection_applied",
        "reconciliation_required",
    ]
    owner_id: str | None
    prepared_input_fingerprint: str | None
    failure_stage: Literal[
        "prepared_input_factory",
        "owner_installation",
    ] | None
    failure_diagnostic: BoundedRuntimeFailureDiagnosticFact | None
    producer_event_id: str | None
    producer_payload_fingerprint: str | None
    producer_event_reference: ContextEventReferenceFact | None
    outbox_item_accumulator: str | None
    reconciliation_from_status: Literal[
        "owner_installed",
        "candidate_frozen",
        "producer_bundle_full",
        "projection_applied",
    ] | None


@dataclass(frozen=True, slots=True)
class ContextCompactionAttemptResult:
    attempt_id: str
    compaction_id: str | None
    terminal_event_deadline_budget: RuntimeEventOperationDeadlineBudget | None
    publication_terminalization_scope: (
        CompactionPublicationTerminalizationScope | None
    )
    status: Literal["not_attempted", "completed", "failed"]
    not_attempted_reason: Literal[
        "disabled",
        "manual_disabled",
        "auto_disabled",
        "failure_circuit_open",
        "below_threshold",
        "empty_source",
        "no_plan",
    ] | None
    core_committed_events: tuple[AgentEvent, ...]
    terminal_event: ContextCompactionCompletedEvent | ContextCompactionFailedEvent | None
    committed_through_sequence: int | None
    publication_summary: Literal[
        "not_applicable",
        "completed",
        "enqueued",
        "unavailable",
        "failed_after_commit",
    ]
    publication_errors: tuple[EventPublicationError, ...]
    candidate_projection_receipt: CompactionCandidateProjectionReceipt | None
```

`terminal_event_deadline_budget`只表示terminal candidate第一次writer admission冻结的预算：
`completed/failed` required，`not_attempted`必须为`None`。它必须来自terminal
`CompactionEventCommitResult.candidate_deadline_budget`，不得用“最近一个receipt”的预算覆盖。

这是 `FrozenRuntimeStateBase` / dataclass process-local result，不是 durable fact，不进入
event payload。

Core invariant：

- core committed events只来自本 attempt 的 compaction commit receipts；
- `not_attempted` 的 publication terminalization scope为空；任何已经冻结 durable candidate
  的 `completed/failed` result都必须携带 attempt admission时的 exact scope，caller不得
  覆盖或按终态重算；
- collector保存 recursively owned deep copies，并立即重算每个
  `ContextEventReferenceFact`；不得把 RuntimeSession 返回的 mutable event alias直接交给
  Host/Agent；
- sequence 严格递增；
- 每个 event 的 `compaction_id` 相同；
- `not_attempted` 没有 core event/terminal/candidate projection receipt，publication为
  `not_applicable`；
- `completed` 恰有一个 Started、一个 Completed terminal，并可有独立 projection receipt；
- `failed` 恰有一个 Started或零个 Started、一个 Failed terminal；
- `ContextCompactionMemoryCandidatesProposedEvent` 绝不进入 `core_committed_events`；
- terminal event 必须属于 `core_committed_events`；
- publication errors 只描述 delivery，不改变 durable terminal status。

aggregate publication status 的唯一计算规则：

```text
not_attempted                         -> not_applicable
任一 receipt 有 publication_errors -> failed_after_commit
否则任一 receipt 为 unavailable     -> unavailable
否则任一 receipt 为 enqueued       -> enqueued
否则                               -> completed
```

Candidate projection receipt invariant：

- `not_requested` 除 completed ref外全部字段为空；
- 其他状态都要求 request identity；
- `preparation_failed` 只要求 request identity、
  `failure_stage="prepared_input_factory"` 与 bounded diagnostic；owner/prepared/producer
  fields为空；
- `owner_installation_failed` 要求 request identity、prepared-input fingerprint、
  `failure_stage="owner_installation"` 与 bounded diagnostic；owner/producer fields为空；
- `suppressed_by_publication_latch` 要求 request identity与 prepared-input fingerprint，
  owner/failure/producer fields为空；
- `owner_installed` 及之后才要求 owner ID与 prepared-input fingerprint；
- `owner_installed` 只要求 stable owner ID与 prepared-input fingerprint；producer event ID、
  producer payload fingerprint、event ref与 outbox accumulator必须为空；
- `candidate_frozen` 才要求 deterministic producer event ID与 producer payload fingerprint，
  但 durable event ref/outbox accumulator仍为空；
- `producer_bundle_full | projection_applied` 必须有 exact proposed event ref与 outbox
  accumulator；
- `reconciliation_required` 必须保存 `reconciliation_from_status`，并保留该已完成 phase
  按上述矩阵能够知道的全部字段，禁止伪造尚未形成的 producer identity；
- 非 reconciliation状态的 `reconciliation_from_status` 必须为空；
- failure fields只允许出现在两个 explicit failure status；
- receipt只报告独立 owner在 core result冻结时的状态，不改变 `completed | failed`。

outer attempt-result join：

- `suppressed_by_publication_latch` 只允许
  `publication_summary in {"unavailable", "failed_after_commit"}`；
- `owner_installation_failed | owner_installed | candidate_frozen |
  producer_bundle_full | projection_applied | reconciliation_required` 只允许 core
  publication为 `completed | enqueued`；
- `preparation_failed` 可以与任一 core publication summary共存，因为 failure发生在
  Completed write之前，outer summary仍独立报告 delivery。

`CompactionCandidateProjectionRequestIdentity` 在任何可能失败的 preparation前先由纯
factory冻结，因此 preparation failure也有稳定 identity。随后
`PreparedCompactionCandidateProjectionInput` 在 compaction caller第一次 await Completed
write前构造。factory递归拥有 summary bytes，不借用 caller string/buffer；校验 artifact
content fingerprint、extractor contract、policy与 expected Completed ID。bytes只活在
process owner中，不进入 core attempt receipt或 durable event。其最大尺寸受现有
compaction summary artifact policy约束。

两个 failure diagnostic使用 closed redaction profiles：

```text
compaction_candidate_projection_preparation_error.v1
compaction_candidate_projection_owner_installation_error.v1
```

它们不安装 owner、不写 producer event/outbox，也不把 canonical Completed改写为 Failed。

### 9.2 service API

```python
async def compact_if_needed(**kwargs: object) -> ContextCompactionAttemptResult:
    ...


async def compact(**kwargs: object) -> ContextCompactionAttemptResult:
    ...
```

manual 调用若要保留 exception UX，使用：

```python
class ContextCompactionInvocationFailed(RuntimeError):
    result: ContextCompactionAttemptResult


class ContextCompactionPublicationFailedAfterCommit(RuntimeError):
    result: ContextCompactionAttemptResult
```

`ContextCompactionInvocationFailed` 只能在 failed terminal 已 FULL 后抛出。Host 先从
`result.terminal_event` 通知 local listener，再向 CLI/caller 投影错误。

`ContextCompactionPublicationFailedAfterCommit` 同样只能在 attempt 已经具有 exact
terminal后抛出；该 terminal可以是 Completed 或 Failed。Host/Agent先消费 exact result做
本地通知，再执行 RuntimeSession publication-failure policy。触发条件是
`publication_summary in {"unavailable", "failed_after_commit"}`，而不只检查 exception。

### 9.3 attempt collector

service 为每次调用创建一个 process-local **core collector**。`_commit_event()` 必须：

1. 调用 RuntimeSession port；
2. 验证 receipt candidate identity；
3. 仅将 exact Started/Completed/Failed event加入当前 attempt；
4. 累积 publication status/error；
5. 返回 committed event。

禁止 completed 后扫描 ledger寻找 core event或 proposal。

memory candidate proposal改为独立 owner：

```text
freeze optional CompactionCandidateProjectionRequestIdentity
    -> prepare PreparedCompactionCandidateProjectionInput
       |-> preparation_failed receipt candidate
    -> commit ContextCompactionCompletedEvent
ContextCompactionCompletedEvent FULL
    -> classify publication
       |-> unavailable/failed_after_commit
       |      -> typed-abandon prepared input
       |      -> SUPPRESSED_BY_PUBLICATION_LATCH
       |
       |-> completed/enqueued
              -> synchronously install CompactionCandidateProjectionOwner
                 |-> OWNER_INSTALLATION_FAILED
                 |-> OWNER_INSTALLED
                        -> owner prepares deterministic producer bundle
                        -> CANDIDATE_FROZEN
                        -> MemoryCandidateProjectionCommitPort
                        -> producer event + account + outbox atomic FULL
                        -> PRODUCER_BUNDLE_FULL
                        -> outbox dispatcher projects candidate pool rows
                        -> PROJECTION_APPLIED
```

prepared input必须在 Completed write前拥有 raw summary canonical bytes、summary artifact
identity/content fingerprint、extractor contract与policy fingerprint；不能借用 caller
局部 mutable string/dict。request identity/preparation outcome由 compaction attempt owner
持有到 Completed publication分类完成，不能只存在于 stack frame。

publication outcome拥有优先权：

- preparation已失败：返回 `preparation_failed`，不安装 owner；
- prepared成功但 Completed publication unavailable/failed：返回
  `suppressed_by_publication_latch`，同步 typed-abandon bytes/handles；
- 只有 prepared成功且 publication为 completed/enqueued，才尝试安装 owner；
- owner installation失败：返回 `owner_installation_failed`；
- memory projection永远不取得 publication terminal-maintenance lease。

成功的 owner admission不 await candidate parse或额外 artifact I/O，并立即形成只含
request identity、owner、Completed ref与 prepared-input fingerprint的
`CompactionCandidateProjectionReceipt(status="owner_installed")`。后续 parse完成并由唯一
factory冻结 exact producer bundle后，owner才进入 `candidate_frozen`，此时才允许
receipt出现 producer event ID/payload fingerprint。caller cancellation只 detach；迟到的
FULL/NONE/UNKNOWN由该 owner和现有
`MemoryCandidateProjectionCommitPort`消费，本地 compaction collector不再接收迟到
outcome。

`NONE` 在 projection owner内部用相同 producer bundle重试；UNKNOWN/PARTIAL进入该 owner
的 reconciliation状态。Host/RuntimeSession close分别 drain：

1. process-local compaction candidate projection owners；
2. 已 durable 的 `memory_candidate_projection_outbox` rows；
3. candidate projection dispatcher。

restart只恢复并 drain已经 durable的 producer/outbox authority，不允许按 compaction
sequence post-scan伪造 proposal。producer event尚未 FULL前的 crash-to-durable-job窗口
仍属于 D5 compaction-memory extension债务。

proposal preparation/owner-install/commit/projection failure均不能把 canonical Completed
改写为 compaction Failed，也不能改变 `ContextCompactionAttemptResult.status`。前两者使用
本节 explicit failure receipts；后两者更新 independent owner receipt。它们只安装
process-local compaction-candidate-projection reconciliation状态与 Host live diagnostics，
不得借 publication latch获得新的 mutation capability。historical Inspector只按第 12.2
节读取 durable producer/outbox authority。

restart terminalization 不冒充新 attempt。它有自己的 recovery owner与 receipt collector，
通过 RuntimeSession publication 正常交付。

---

## 10. 删除 post-scan 与二次 publication

### 10.1 Host

删除：

```text
HostSession._publish_compaction_events_after
HostSession._compaction_events_after
HostSession._latest_terminal_compaction_event
before_sequence tracking
hard-coded compaction event class filter
```

`compact_now()`：

```text
result = await service.compact(...)
notify(result.terminal_event)
render result
```

`_compact_if_needed_and_notify()`：

```text
result = await service.compact_if_needed(...)
notify(result.terminal_event)
return result
```

Host listener 是 process-local best-effort observer，不是 event publication。它只能收到 exact
attempt terminal receipt。

### 10.2 Mid-turn compactor

删除：

```text
RuntimeContextCompactor._compaction_events_after
before_sequence tracking
runtime_session.publish_stored_events(compaction_events)
_latest_completed(scanned_events)
```

`RuntimeContextCompactor` 使用 `RunWorkingSet.run_start_event_id/run_start_sequence`
构造 typed RunStart reference，不再为定位 current run 执行 `event_log.iter()`。latest
completed boundary 改用
`read_raw_events_by_type(EventType.CONTEXT_COMPACTION_COMPLETED, limit=1,
through_sequence=H)`，并通过 current runtime/run、compaction ID 与 payload fingerprint
重验。这两个 read cleanup 是生成第 6.7 节 exact skip authority 的必要条件。

改为：

```text
attempt = await service.compact_if_needed(...)
MidTurnCompactionResult.events = attempt.core_committed_events
completed = attempt.terminal_event if completed branch else None
```

Agent stream 可以 yield exact receipt events；这不是再次 publication。

### 10.3 不允许 sequence-range discovery fallback

无论 publication error、cancellation、listener failure或test fake，Host/Agent 都不得重新引入：

```text
next_sequence before
read range after
filter event classes
```

若 exact attempt owner 丢失，分类为 process ownership bug / reconciliation required，不允许用
range scan 猜测。

---

## 11. CustomEvent schema hard cut

### 11.1 物理删除

在同一个 atomic migration 中：

1. 新增 8 个 `EventType`；
2. 新增 8 个 event classes；
3. 将既有 `ToolExecutionSuspendedEvent` payload hard-cut为 typed MCP suspension；
4. `ToolResultEndEvent` / terminal projection增加 conditional-required
   `McpInputRequiredTerminalSourceFact`；
5. `RunEndEvent` 增加 conditional-required MCP closure reference与
   `PublicationLatchedRunTerminationFact`，并拒绝 active MCP lifecycle的 generic end；
6. `ContextCompactionFailedEvent` hard-cut failure-stage union与 validator matrix；
7. 加入 `AgentEvent` union；
8. 迁移所有 production producers/consumers；
9. 迁移 tests；
10. 删除 `EventType.CUSTOM`；
11. 删除 `CustomEvent`；
12. 删除 `event/__init__.py` export；
13. `AGENT_EVENT_SCHEMA_VERSION: 5 -> 6`；
14. 更新 default schema registry golden vectors。

禁止先发布“新 event + 旧 CustomEvent fallback”的双写或 dual-read 阶段。

architecture guard 的 production forbidden set 固定为：

```text
CustomEvent
EventType.CUSTOM
"CUSTOM"
"mcp_input_required_expired"
"mcp_input_required_resolved"
"mcp_input_required_binding_changed"
"mcp_input_required_resume_failed"
"compaction_requested"
"mid_turn_compaction_skipped"
"tool_result_persistence_failed"
ToolExecutionSuspendedEvent.payload
```

`"CUSTOM"` 只在精确 event-type/symbol AST context中判定，避免误伤普通文字。旧 literal可在
本实施规格和 negative architecture test中出现，但不得出现在 `src/pulsara_agent`。

### 11.2 reset 规则

该 hard cut 改变：

- full event schema registry fingerprint；
- full event-domain registry fingerprint；
- checkpoint binding；
- provider-input/replay attribution 中绑定的 registry identity。

V1 不迁移旧 `CUSTOM` ledger，必须在 production switch 前通过 privileged workflow reset：

```text
PostgreSQL runtime/EventLog world
provider-input/checkpoint/projection durable state
memory candidate/outbox state tied to old event refs
Oxigraph derived graph world
```

PostgreSQL physical schema migration head只有在表结构实际变化时才前进；event-world reset 与
SQL migration version 是两个独立概念。

---

## 12. Reducer、Inspector 与 recovery

### 12.1 Event-domain registry

8 个新类型与 hard-cut后的 `ToolExecutionSuspendedEvent` 显式测试为
`non_transcript`。虽然当前 registry 对未知类型 default non-transcript，gate仍必须逐一
断言，防止未来误加入 transcript set。

### 12.2 Inspector

Inspector 增加 typed projections：

```text
mcp_input_required_lifecycle
context_compaction_requests
mid_turn_compaction_skips
tool_result_evidence_projection_failures
mandatory_runtime_audit_reconciliation
compaction_candidate_projection_durable_status
```

Inspector 不再展示：

```text
custom_event.name
custom_event.value
tool_result_persistence_failed
```

MCP lifecycle projection使用第 7.5 节的同一 reducer；缺失 source ref、重复 terminal
disposition、binding identity冲突均是 typed diagnostic，不得按 string name猜测。projection
必须显示 round/attempt ordinal、previous resolution/resume-failed chain、normal terminal
source resolution、closure reason与 RunEnd closure join；`SESSION_REOPEN` closure不得显示为
仍可 resume。

compaction core状态与 memory candidate projection状态分栏显示。`Completed` + projection
pending/failed是合法组合，Inspector不得把后者改写成 compaction failed。
`started_publication` 属于 compaction core Failed，而不是 projection failure。

必须区分两个观察面：

```text
Host live diagnostics
    may show:
        not_requested
        preparation_failed
        owner_installation_failed
        suppressed_by_publication_latch
        owner_installed
        candidate_frozen
        producer_bundle_full
        projection_applied
        reconciliation_required

Historical Inspector (PostgreSQL/Oxigraph only)
    may show:
        not_durably_observable
        producer_bundle_full
        projection_applied
        reconciliation_required
            only when durable outbox authority proves it
```

Host live diagnostics只能从仍存活的 exact
`CompactionCandidateProjectionOwner/Receipt`读取 process-local状态，不写入 EventLog或
伪装成 Inspector projection。历史 Inspector只消费：

- exact `ContextCompactionMemoryCandidatesProposedEvent`；
- transactionally joined `memory_candidate_projection_outbox` row/status；
- candidate-pool applied rows及其 producer/outbox refs。

如果这些 durable authority不存在，历史 Inspector统一显示
`not_durably_observable`；不得从 Completed后“没有 outbox row”推断
`not_requested`、`preparation_failed`、`owner_installation_failed`、
`suppressed_by_publication_latch`、`owner_installed`或 `candidate_frozen`。这项限制明确保留
D5 compaction-memory extension ownership为 OPEN；本阶段不为 process-local receipt新增
durable audit event。

### 12.3 Recovery

删除：

```text
ModelStreamRecoveryService.allow_unbootstrapped_test_events direct extend branch
ModelCallControlRecoveryService.allow_unbootstrapped_test_events direct extend branch
```

production recovery 必须：

- 使用 accounted recovery coordinator；
- 或通过 RuntimeSession writer/专用 typed recovery port；
- 缺 materialization account 时 fail closed。

MCP recovery只消费 typed suspension fact、resolution attempt chain与 exact lifecycle
refs；旧 `pending_interaction_payload` 不得作为 durable source。`SESSION_REOPEN` 不恢复
process-local pending lease或 opaque request state。若 lifecycle仍 active，必须按第 7.6
节提交 lease-unavailable closure + error ToolResult + RunEnd；resolution raw bytes若已随
进程丢失，也走该 closure，不得伪造成 `resume_failed` 后继续等待。

reopen coordinator拥有一个共享 recovery deadline，并严格执行第 7.6 节 pipeline。
`repair_dangling_runs_for_resume()` 在 MCP lifecycle recovery之后才运行；其 query/validator
必须识别 active MCP interaction并返回 `delegated_to_mcp_recovery`，不得直接提交
`RECOVERED_INTERRUPTED` RunEnd。`repair_run_projection()` 只在 canonical RunEnd FULL后
执行。

child reopen使用同一优先级，但在 child ledger与 subaccount内执行：

```text
fold child MCP lifecycle
    -> specialized child MCP closure + ToolResult + settlement
    -> child window/subaccount close + RunEnd
    -> parent graph terminal reference
    -> generic dangling-child repair
```

`SubagentRuntime.repair_dangling_children()` 必须在其现有 “no terminal -> construct
RECOVERED_INTERRUPTED” 分支之前调用 child MCP lifecycle classifier。active suspension返回
`delegated_to_child_mcp_recovery`；generic branch不得写 child RunEnd或 parent graph result。
若 child closure/ToolResult或 child RunEnd为 NONE，specialized owner继续持有相同
candidates；UNKNOWN/PARTIAL阻止 parent graph repair。只有 child RunEnd FULL后才能把
terminal reference投影到 parent graph。

compaction recovery分别处理：

- core Started无 terminal的 terminalization；
- durable candidate projection outbox的 dispatch；
- process-local projection owner的 Host close drain。

禁止为了补齐 candidate proposal扫描 Completed后面的 sequence range。pre-durable
projection owner crash窗口继续记录为 D5，不在本阶段伪装成已闭环。

unit tests 必须 bootstrap canonical materialization account，或使用 `tests/support` 的
明确 test harness。production module 不得为 pytest 保存 direct EventLog mutation。

### 12.4 Test-only event fixtures

删除测试对 production `CustomEvent` 的依赖：

- 默认 registry/RuntimeSession tests 使用中央
  `tests/support/events.py::non_transcript_projection_request_fixture()`；它构造最小合法
  `ProjectionRequestedEvent`，通过 projection ID/role/scope表达 test variation；
- 测试 schema collision/legacy decoder 时，使用 isolated registry 与 test-local literal
  event type，例如 `"TEST_ONLY_EVENT"`；
- test-local event 不得加入 default `AgentEvent` union；
- 不新增 production `TestEvent`。

---

## 13. Direct EventLog mutation exact allowlist

### 13.1 最终 allowlist

排除 `src/pulsara_agent/event_log/` 内 physical adapter implementation 后，唯一允许：

```python
DIRECT_EVENT_LOG_MUTATION_ALLOWLIST = {
    (
        "runtime/session.py",
        "RuntimeSession",
        "_commit_reduce_enqueue",
        "self.event_log",
        "extend",
    ),
    (
        "runtime/session.py",
        "RuntimeSession",
        "_persist_runtime_projection_checkpoint",
        "self.event_log",
        "write_runtime_projection_checkpoint",
    ),
    (
        "runtime/authority_materialization/account.py",
        "LedgerMaterializationCoordinator",
        "_commit_atomic",
        "self.event_log",
        "extend_with_materialization_state",
    ),
    (
        "runtime/long_horizon/checkpoint_doctor.py",
        None,
        "verify_or_rebuild_subagent_graph_checkpoint",
        "event_log",
        "append",
    ),
}
```

说明：

- 前三项是 online canonical writer internals；
- 第四项是 quiescent、exclusive-authority、offline repair；
- 没有 compaction、Agent、Host、tools、memory、governance或LLM recovery direct writer。

### 13.2 Maintenance inventory

非 event-row maintenance capability 使用独立 inventory，不得混入 canonical writer
allowlist：

```python
DIRECT_EVENT_LOG_MAINTENANCE_INVENTORY = {
    (
        "runtime/session.py",
        "RuntimeSession",
        "__post_init__",
        "self.event_log",
        "ensure_runtime_session_owner",
        1,
    ),
    (
        "runtime/subagent/runtime.py",
        "SubagentRuntime",
        "_create_child_runtime_session",
        "event_log",
        "ensure_runtime_session_owner",
        1,
    ),
    (
        "host/resume.py",
        None,
        "repair_dangling_runs_for_resume",
        "log",
        "repair_run_projection",
        2,
    ),
    (
        "runtime/session.py",
        "RuntimeSession",
        "_adopt_unbootstrapped_in_memory_account_for_test",
        "self.event_log",
        "adopt_materialization_account_state_for_test",
        1,
    ),
}
```

tuple 最后一项是 exact call-site count；同一函数内悄悄增加第二次 mutation 也必须使 gate
失败。

规则：

- `ensure_runtime_session_owner` 只能发生在 RuntimeSession composition/bootstrap，不能由
  domain producer调用；
- `repair_run_projection` 必须继续受 verified Postgres provider、quiescent resume repair
  与 privileged recovery contract约束；
- `adopt_materialization_account_state_for_test` 必须同时满足 pytest、
  `InMemoryEventLog` 与 explicit `allow_unbootstrapped_test_events`，production wiring不得把
  该 flag 暴露为配置；
- maintenance inventory不授权 append event，也不能被业务模块当作通用 EventLog writer。

### 13.3 AST guard

替换当前 suffix-based architecture test。scanner 覆盖 `src/pulsara_agent/**/*.py`，排除
physical `event_log/` package，并记录：

```text
relative path
enclosing class
enclosing function
receiver AST normalization
method
line
```

必须扫描：

- direct call：`event_log.extend(...)`；
- `self.event_log.*` / `self._event_log.*`；
- bound method escape：`writer = event_log.extend`；
- `getattr(event_log, "extend")`；
- alias receiver 可静态解析的赋值；
- event-row/checkpoint mutation method全集；
- 第 13.2 节 maintenance method全集与 exact multiplicity。

allowlist 是 exact tuple，禁止：

- file-wide exception；
- suffix exception；
- 只按 receiver 包含 `"log"`；
- 新增任意 method 后自动放行；
- test flag 控制 production direct mutation。

### 13.4 Offline doctor guard

checkpoint doctor 必须同时满足：

- explicit privileged CLI；
- exclusive maintenance permit；
- closed/quiescent runtime ledger；
- no live HostSession；
- exact confirmation；
- 不触发 online publication；
- Inspector 标记 offline repair attribution。

缺少任一条件时，该 allowlist entry 不能被其他 caller复用。

---

## 14. Physical accounting

新增事件会进入现有 materialization account。实施时必须重新推导：

### 14.1 MCP suspension carrier

`ToolExecutionSuspendedEvent` 的 reservation必须覆盖：

```text
typed interaction/binding/pending-lease wrapper
+ bounded user-visible typed request envelope
+ original request/request-state semantic fingerprints
+ predecessor resolution reference
+ rollout/account bookkeeping
```

reservation使用 exact canonical event bytes。opaque original request与 request state不落
EventLog/ArtifactStore，因此不计入 durable payload；process-local prepared owner仍受 MCP
request hard bound约束。

### 14.2 Host MCP resume boundary

boundary burst 增加一个 `McpInputRequiredResolutionSubmittedEvent`：

```text
pending MCP audits
+ exposure
+ boundary
+ resolution submitted（含 attempt/predecessor refs）
+ account bookkeeping
```

reservation 必须使用 canonical candidate bytes，不得沿用旧最大 event count。

### 14.3 MCP terminal bundle

normal resumed terminal必须计入 `McpInputRequiredTerminalSourceFact`。expired、
binding-changed与 special closure terminal bundle还要增加一个 typed companion：

```text
typed disposition或 interaction closure
+ ToolResult semantic events
+ terminal projection
+ rollout settlement
+ physical settlement/bookkeeping
```

`_emit_tool_result_and_record()` 的 burst contract与
`RuntimeSessionToolExecutionEventCommitPort` validator 同步更新。

special closure后的 RunEnd是第二个 bounded transaction；reservation额外覆盖 exact closure
reference以及可选 `PublicationLatchedRunTerminationFact`。terminal disposition publication
failure没有 closure，但 RunEnd reservation必须覆盖 ordered source event refs与 accumulator。
publication latch路径使用 terminal-maintenance lease，但不能绕过相同的 physical account
与 burst doctor。

doctor分别证明两种 exact maximum batch：

```text
closure + error ToolResult + terminal projection + settlement
window close + account close + RunEnd
```

lease batch fingerprint使用相同 candidate ordering与 canonical bytes公式，不能另设较小
估算。

compaction publication latch另外证明：

```text
started_publication Failed + account/bookkeeping
mid-turn window close + account close
    + RunEnd(compaction_publication_unavailable)
```

后一批的 reservation必须覆盖 Started（若存在）与最终 Completed/Failed两条 exact source
refs、ordered accumulator及 frozen scope join。pre-run/manual no-active-run分支不预留
synthetic RunEnd，但仍须为必要 `started_publication` Failed保留 terminal tail。

### 14.4 其余事件

`ContextCompactionRequestedEvent`、`MidTurnContextCompactionSkippedEvent` 和
`ToolResultEvidenceProjectionFailedEvent` 仍是一进一替换，但 payload bounds 与 canonical
bytes 必须进入 settlement measurement。

`started_publication` Failed复用现有 compaction terminal event count；其 payload计入 exact
Started reference，不复制 summarizer attribution。projection receipt与 prepared input是
process-local，不进入 durable event reservation；producer bundle仍由 memory projection
commit port独立计费。

preparation/owner-install diagnostics与 suppressed receipt同样是 process-local，不进入 event
reservation。

### 14.5 Doctor gates

新增 0/max 边界 tests：

- 64/65 MCP response keys；
- 64/65 MCP input requests；
- 64 KiB / 64 KiB + 1 user-visible request-envelope boundary；
- 128/129 terminal refs；
- 16/17 publication-latched RunEnd source refs；
- 1,024/1,025 diagnostic UTF-8 bytes；
- 256 KiB canonical event boundary；
- max boundary companion count；
- max terminal bundle count/bytes；
- 20-second ordinary / 10-second terminal reserve boundary；
- invalid custom deadline-budget policy；
- exact maintenance batch fingerprint与 subset rejection；
- maintenance handle object identity、ISSUED generation、invalidate/consume边界；
- compaction Started + terminal source-ref count/bytes与 no-active-run/mid-turn batch分支。

---

## 15. Failure 与 crash matrix

| 窗口 | 必须结果 |
|---|---|
| MCP adapter冻结 suspension，suspension batch NONE | 不安装 WAITING_USER；prepared owner保留同一 candidate重试，或在 deadline内 typed-abort pending lease |
| suspension FULL，publication unavailable | 不安装 WAITING_USER；closure + error ToolResult exact batch FULL后释放 lease；第二批关闭 window/account并写 RunEnd |
| suspension FULL 后 caller修改原 request dict | durable source与resume adapter input均不变 |
| MCP resume boundary candidate prepared，commit NONE | 不激活 continuation；保留原 pending owner |
| Host ingress admission后 caller修改 nested response dict | boundary fingerprint与adapter payload均使用 prepared bytes，不漂移 |
| boundary commit FULL，publication unavailable/failed | fold exact committed batch；安装 publication latch；不调用 adapter；用 terminal-maintenance lease提交 closure + error ToolResult，RunEnd exact引用 closure |
| resolution submitted FULL，adapter resume fails | 写 typed resume-failed；interaction仍 open |
| ResumeFailed FULL，publication unavailable | 不重新暴露 WAITING_USER；写 exact引用 ResumeFailed的 closure bundle，释放 lease，再写 RunEnd |
| resume-failed FULL 后用户再次提交 resolution | 新 boundary提交 attempt ordinal + 1，并 exact引用 previous resolution与 resume-failed |
| normal resumed ToolResult缺失/错误 resolution ref | 整个 terminal batch fail closed；不得按 call ID或时序猜测 |
| SESSION_REOPEN发现 active MCP suspension | 不恢复 pending lease；提交 lease-unavailable closure + error ToolResult + settlement，再让 RunEnd exact引用 closure |
| generic dangling-run repair遇到 active MCP suspension | delegate/skip；MCP recovery先 closure/ToolResult、window/account close、RunEnd，最后才 projection repair |
| child进入 MCP WAITING_USER | 提交 child-pending-unsupported closure + error ToolResult；bundle FULL后才完成 live lease并写 RunEnd |
| child suspension FULL、child closure前进程崩溃 | reopen先 fold child MCP lifecycle；提交 child-pending-unsupported closure + ToolResult + settlement，再写 child window/subaccount close + RunEnd，最后才写 parent graph terminal ref |
| `repair_dangling_children()` 遇到 active child MCP suspension | 返回 `delegated_to_child_mcp_recovery`；不得先写 `RECOVERED_INTERRUPTED` RunEnd或 parent graph terminal |
| expired/binding terminal batch FULL，publication unavailable | interaction已 terminal，不写 closure；terminal-maintenance owner直接写 publication-latched aborted RunEnd |
| closure bundle FULL，publication unavailable | 不写第二个 closure；durable closure释放 lease，RunEnd携带 closure ref与 publication-latched fact |
| RunEnd FULL，publication unavailable | durable run保持 terminal；不再写 event，只 bounded close |
| maintenance exact batch NONE | 同一 lease ID进入 `ISSUED(g+1)`并重试同一 ordered batch；FULL前不消费 |
| maintenance caller只提交 exact batch的 subset | precommit拒绝；closure/ToolResult/settlement或 window/account/RunEnd不得拆写 |
| mandatory audit NONE | session owner复用 exact candidate，bounded retry；caller cancellation只 detach |
| mandatory audit NONE多次重试 | 每次使用首次 admission冻结的同一 ordinary deadline；不得通过再次调用 writer续期 |
| mandatory audit deadline/UNKNOWN | 安装 audit latch并阻止下一次 dispatch |
| process在 mandatory audit首次 FULL前崩溃 | 不伪造 audit；从 canonical domain state恢复并标记 audit gap，D3仍 OPEN |
| expired/binding companion batch NONE | ToolResult 与 disposition 都不存在；同一 stable candidate可重试 |
| expired/binding companion UNKNOWN/PARTIAL | 保留 terminal owner并 latch；不得分别补写 |
| evidence projection hook fails，failure event FULL | canonical ToolResult保持 FULL，run继续 |
| failure audit event UNKNOWN | RuntimeSession reconciliation required；不得静默继续 |
| compaction Started FULL，caller cancelled | pending RuntimeSession task/terminalization owner接管 |
| compaction Started FULL，publication unavailable | 不启动 summarizer；terminalization owner写 `started_publication` Failed，其 exact引用 Started且不复制 summarizer call/usage |
| pre-run/manual compaction final terminal FULL，publication unavailable | 完成必要 compaction terminal maintenance后关闭 session；因为没有 active run，不写 synthetic RunEnd |
| mid-turn compaction final terminal FULL，publication unavailable | exact保留最终 Completed/Failed；关闭 active window/account并写 `compaction_publication_unavailable` aborted RunEnd，source refs绑定 Started（若存在）与最终 terminal |
| Started直到 ordinary deadline末尾才确认 FULL | 使用预留 terminal tail写 required Failed；tail仍失败则 reconciliation + close，reopen用独立 recovery deadline收口 |
| compaction Completed FULL，publication fails | Completed保持 canonical；不得写 conflicting Failed或安装 projection owner；prepared input typed-abandon并返回 suppressed receipt |
| candidate projection preparation失败 | Completed可保持 canonical；返回 `preparation_failed` receipt，不要求 owner/prepared fingerprint |
| candidate projection owner安装失败 | 返回 `owner_installation_failed` receipt；不写 producer/outbox |
| projection owner已安装、candidate尚未冻结时 caller取消 | owner继续持有 prepared-input；receipt不得伪造 producer event ID/payload fingerprint |
| compaction proposal caller cancelled | independent projection owner继续；core attempt receipt不等待迟到 outcome |
| compaction proposal NONE | projection owner重试 exact producer bundle；Completed不变 |
| compaction commit UNKNOWN | pending owner/reconciliation；不得 post-scan猜测 |
| Host listener throws | durable/publication状态不变；只记录 process-local observer error |
| process restart | compaction recovery从 typed lifecycle terminalize未终结 Started；reopen/Inspector解释已 committed publication事实，不执行 live publisher catch-up |

---

## 16. 实施阶段

每个阶段都必须同步更新它改变的长期 contract；不得把 contract migration 全拖到最后。

### EV0：Additive facts 与 pure factories

工作：

- 新增 typed primitive facts、enums、fingerprint factories；
- 新增 MCP suspension/request/lease、resolution-attempt、terminal-source与 closure facts；
- 新增 immutable resolution factory；
- 新增 bounded diagnostic factory；
- 新增 MCP lifecycle pure validator；
- 新增 mandatory audit owner pure state machine；
- 新增 shared absolute-deadline RuntimeSession API；
- 新增 frozen deadline-budget factory、closed maintenance owner-kind enum、
  RuntimeSession-only opaque lease handle/issuer与 lease state machine；
- 新增 compaction core attempt DTO/collector、
  frozen publication terminalization scope、projection request/prepared-input DTO与完整
  independent projection receipt union；
- 补齐 compaction failure-stage closed union及 nullability pure validator；
- 新增 `tests/support` typed non-transcript fixture；
- 不修改 default `AgentEvent` union，不改变 registry。

Gate：

- all fact round-trip/tamper/bounds tests；
- static type/ruff；
- default registry fingerprint不变；
- production仍运行旧路径，仅作为短暂开发阶段。

### EV1：Compaction writer 与 publication hard cut

工作：

- 删除 direct compaction port；
- service dependency required；
- publication-after-commit normalization；
- `started_publication` terminalization与 typed preparation abandonment；
- RuntimeSession publication reconciliation latch与 terminal-maintenance lease；
- compaction pre-run/manual-vs-mid-turn publication terminalization matrix；
- Completed publication outcome先于 projection owner installation；
- preparation/owner-install/suppressed receipts；
- exact core attempt result；
- compaction candidate projection owner与 core collector拆分；
- Host/mid-turn post-scan与republish删除；
- cancellation/recovery owner迁移；
- 同步 compaction/event publishing contracts。

Gate：

- component tests只能通过 RuntimeSession port提交；
- injected publication failure不产生 conflicting terminal；
- `started_publication` Failed exact引用 Started且 summarizer fields为空；
- ordinary/terminal deadline reserve与 exhausted-tail recovery；
- maintenance lease NONE/FULL/UNKNOWN及 exact-batch tests；
- copied identity/fake handle/stale generation/invalidated handle均被拒绝；
- pre-run/manual publication failure不生成 RunEnd，mid-turn精确关闭 active run；
- compaction publication-latched RunEnd source refs绑定 Started与最终 terminal；
- concurrent unrelated event不能进入 attempt result；
- owner-installed receipt不提前出现 producer event/payload identity；
- Completed publication failure不安装 projection owner/outbox；
- preparation/owner-install failure receipts可独立构造；
- caller cancellation后 projection outbox owner继续持有 stable proposal；
- Host/mid-turn不调用 `publish_stored_events`；
- no `before_sequence` discovery；
- cancellation FULL/NONE/UNKNOWN tests。

### EV2：七事件 vocabulary atomic hard cut

工作：

- 一次性迁移全部 7 个 CustomEvent producers并新增 typed closure producer；
- `ToolExecutionSuspendedEvent` 同批切到 typed MCP suspension branch；
- Host ingress切到 `PreparedMcpInputRequiredResolution`；
- Host boundary接管 resolution-submitted；
- `resume_failed -> resolution_submitted` attempt chain；
- MCP terminal companion atomicity；
- normal resumed ToolResult exact引用 resolution；
- suspension/boundary/resume-failed publication、SESSION_REOPEN与 child-pending typed closure；
- child reopen MCP closure先于 `repair_dangling_children()` 与 parent graph terminal ref；
- terminal disposition publication failure不写 closure；
- RunEnd exact引用 MCP closure/publication-latched termination；
- generic dangling-run repair在 MCP recovery之后运行；
- mandatory audit owner接管四类 audit event；
- Inspector/recovery/reducer同步；
- 删除 `CustomEvent` 与 `EventType.CUSTOM`；
- bump event schema generation；
- 执行 PostgreSQL/Oxigraph world reset；
- 同步 Agent/MCP/EventLog/Inspector contracts。

Gate：

- `rg "CustomEvent|EventType\\.CUSTOM|mcp_input_required_resolved|..." src` 为零；
- default registry golden tests；
- non-transcript classification；
- MCP crash matrix；
- caller-alias mutation、typed request-envelope bounds、attempt retry、normal terminal exact ref、
  full publication terminalization matrix、SESSION_REOPEN与 child closure matrix；
- generic repair delegate/skip与 active-MCP RunEnd rejection；
- child suspension FULL/closure前 crash与 dangling-child delegate回归；
- mandatory audit NONE/cancellation/close drain；
- injected clock证明 mandatory/compaction NONE retry不移动 frozen deadline budget；
- critical publication-aware owner legacy-writer architecture guard；
- physical reservation bounds；
- all non-network tests。

### EV3：Direct EventLog write allowlist

工作：

- 删除两个 LLM recovery pytest direct branches；
- tests bootstrap account或使用test support；
- 安装 exact AST guard；
- 更新 EventLog/Recovery contracts。

Gate：

- scanner observed set exact等于四项 allowlist；
- maintenance observed multiset exact等于第 13.2 节 inventory；
- bound-method/getattr/alias mutation negative fixtures；
- runtime recovery tests；
- offline checkpoint doctor authority tests。

### EV4：Final audit 与验证

工作：

- Inspector fixture/golden update；
- historical Inspector只投影 durable outbox authority；pre-durable状态显示
  `not_durably_observable`，Host live diagnostics单独验证；
- architecture debt doc只将 D2 的四项标记 closed；
- 明确保留 D3 durable Hook/projection job outbox为 `OPEN`；
- 明确保留 D5 compaction-memory extension ownership为 `OPEN`；
- grep、schema、contract consistency audit；
- 全量非联网 tests；
- core dogfood。

EV4 不允许补回 compatibility fallback来“修测试”；fixture 必须适配新 architecture。

---

## 17. 测试矩阵

### 17.1 Typed schema

- 每个 event valid minimum/max；
- wrong enum/reason rejected；
- extra fields rejected；
- fingerprint tamper rejected；
- nested source ref mismatch rejected；
- canonical serialization golden；
- all 8 plus typed suspension explicit non-transcript。

### 17.2 MCP

- suspension source不再含自由 payload；
- binding/request/pending lease/deadline/predecessor source exact join；
- typed user-visible request envelope FULL/NONE/bounds；
- source suspension exact join；
- boundary + resolution same batch；
- ingress admission后 nested request/response mutation不改变 prepared bytes；
- adapter payload与 resolution commitment byte-exact；
- wrong interaction/tool/server/round rejected；
- sorted unique response keys；
- opaque original request/request-state values不出现在 event或 artifact；
- raw response values不出现在 event JSON；
- expired + ToolResult atomic；
- binding source/effective identity join；
- resume failure keeps pending；
- resume-failed后 attempt ordinal + predecessor refs递增；
- normal resumed terminal缺失/错误 resolution ref被拒绝；
- suspension FULL + publication unavailable不安装 WAITING_USER；
- boundary FULL + publication unavailable不调用 adapter，并提交 closure terminal bundle；
- ResumeFailed FULL + publication unavailable提交 exact failure-ref closure；
- expired/binding publication unavailable不产生第二个 closure；
- closure publication unavailable只写 RunEnd，RunEnd publication unavailable不再写 event；
- closure bundle与 window/account/RunEnd分别使用两张 exact-batch lease；
- child pending unsupported关闭 suspension、lease与 RunEnd；
- child suspension FULL、closure前 crash由 specialized child MCP recovery收口；
- child RunEnd FULL前 parent graph terminal reference被拒绝；
- generic dangling repair遇到 active MCP lifecycle delegate/skip；
- generic dangling-child repair遇到 active child MCP lifecycle delegate/skip；
- RunEnd validator拒绝未 closure/terminal的 active MCP suspension；
- restart reducer state不恢复 process-local lease；
- restart raw resolution/lease unavailable typed closure + ToolResult + RunEnd；
- FULL/NONE/UNKNOWN/publication-failure。

### 17.3 Compaction

- service missing RuntimeSession port fails construction；
- cross-session port fails construction；
- exact Started/Completed-or-Failed core receipts；
- Proposed只出现在 independent projection receipt/outbox；
- planning failure exact terminal；
- `summarizer_provider_input_prepare` 与 `started_publication` schema round-trip；
- completed/enqueued/unavailable/failed-after-commit normalization；
- Started unavailable不启动 summarizer；
- `started_publication` requires Started ref/failed termination and rejects every summarizer field；
- terminal reserve可完成 `started_publication`；reserve耗尽进入 recovery；
- no duplicate failed after completed；
- publication terminalization scope的三分支/nullability/fingerprint tamper tests；
- pre-run/manual publication failure关闭 session且不写 RunEnd；
- mid-turn publication failure关闭 exact window/account并写
  `compaction_publication_unavailable` RunEnd；
- compaction RunEnd source refs缺 Started、缺 final terminal、乱序或 accumulator drift均拒绝；
- Completed publication unavailable返回 `suppressed_by_publication_latch`且无 owner/outbox；
- `preparation_failed` 只要求 request identity + diagnostic；
- `owner_installation_failed` 只要求 request/prepared identity + diagnostic；
- projection receipt status与 outer publication summary mismatch rejected；
- cancellation pending owner；
- owner-installed receipt只有 owner/completed/prepared-input identity；
- candidate-frozen之前 producer event ID/payload fingerprint均被拒绝；
- producer-bundle-full要求 exact event ref与 outbox accumulator；
- cancellation后 projection owner继续并在 close drain；
- proposal failure不改变 Completed；
- historical Inspector无 durable outbox时显示 `not_durably_observable`；
- historical Inspector不从 absence生成任何 process-local receipt status；
- durable producer/outbox/candidate-pool join分别投影
  `producer_bundle_full/projection_applied/reconciliation_required`；
- Host listener receives exact terminal；
- unrelated concurrent event excluded；
- mid-turn stream uses exact receipts；
- grep confirms post-scan helpers removed。

### 17.4 Mandatory audit owner

- four allowed audit kinds only；
- duplicate same candidate joins shared completion；
- same ID/different payload rejected；
- NONE retries same payload/ID with increasing attempt generation；
- waiter cancellation detach；
- UNKNOWN/PARTIAL reconciliation latch；
- frozen ordinary/terminal deadlines不在 retry时移动；
- writer/account/reducer/publication/confirmation共享本次选择的 injected deadline；
- critical publication-aware owners不调用 legacy full-timeout writer wrapper；
- publication latch只允许 RuntimeSession签发的 exact terminal-maintenance lease；
- copied lease identity、伪造 handle、值相等的替代 handle、stale attempt generation与
  consumed/invalidated handle均 rejected；
- closed maintenance owner kind与实际 transition mismatch rejected；
- lease `NONE -> ISSUED(g+1)`、`FULL -> CONSUMED`、`UNKNOWN -> reconciliation`；
- exact ordered batch/transaction companion mismatch与 subset均 rejected；
- publication unavailable不声称 live catch-up；
- Host close admission stop + drain；
- audit FULL前不得继续相应 model/tool operation。

### 17.5 Direct write guard

- current four call sites accepted；
- same file/wrong function rejected；
- same function/wrong receiver rejected；
- new method rejected；
- bound method escape rejected；
- `getattr` rejected；
- production test flag direct mutation rejected；
- wrong maintenance owner/receiver/multiplicity rejected；
- physical EventLog adapter implementation excluded only by exact package boundary。

### 17.6 Full gates

```text
uv run ruff check .
uv run pytest -q
```

Real LLM 不用于证明 durable ownership。最终只需跑冻结的 core dogfood，验证：

- MCP resume（若环境具备）；
- tool result evidence projection正常路径；
- preflight/mid-turn compaction；
- Host stream无重复 compaction event。

---

## 18. 修改面

至少包括：

### Event / primitives

- `src/pulsara_agent/event/events.py`
- `src/pulsara_agent/event/__init__.py`
- `src/pulsara_agent/event_log/serialization.py`
- `src/pulsara_agent/event_log/transcript_prefix.py`
- `src/pulsara_agent/primitives/frozen.py`
- 新建 `src/pulsara_agent/primitives/runtime_event_vocabulary.py`
- `src/pulsara_agent/primitives/run_boundary.py`

### MCP / Agent / Host

- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/runtime/event_write_service.py`
- `src/pulsara_agent/runtime/hooks.py`
- `src/pulsara_agent/runtime/state.py`
- `src/pulsara_agent/runtime/run_entry.py`
- `src/pulsara_agent/runtime/plan.py`
- `src/pulsara_agent/runtime/tool_execution.py`
- 新建 `src/pulsara_agent/runtime/mandatory_audit.py`
- `src/pulsara_agent/runtime/mcp/types.py`
- `src/pulsara_agent/runtime/mcp/supervisor.py`
- `src/pulsara_agent/tools/adapters/mcp.py`
- `src/pulsara_agent/runtime/subagent/runtime.py`
- `src/pulsara_agent/primitives/mcp.py`
- `src/pulsara_agent/memory/hooks/runtime_persistence.py`
- `src/pulsara_agent/host/run_boundary.py`
- `src/pulsara_agent/host/ingress.py`
- `src/pulsara_agent/host/resume.py`
- `src/pulsara_agent/host/session.py`

### Compaction

- `src/pulsara_agent/runtime/compaction/commit.py`
- `src/pulsara_agent/runtime/compaction/service.py`
- `src/pulsara_agent/runtime/compaction/inline.py`
- `src/pulsara_agent/runtime/wiring.py`
- `src/pulsara_agent/memory/candidates/projection_outbox.py`
- `src/pulsara_agent/runtime/compaction/candidates.py`

### Recovery / Inspector / architecture

- `src/pulsara_agent/llm/recovery.py`
- `src/pulsara_agent/llm/control_recovery.py`
- `src/pulsara_agent/inspector/service.py`
- `src/pulsara_agent/runtime/authority_materialization/account.py`
- `src/pulsara_agent/runtime/authority_materialization/contracts.py`
- `tests/test_runtime_event_architecture.py`
- `tests/test_context_compaction.py`
- `tests/test_agent_runtime_loop.py`
- `tests/test_mcp_host_lifecycle.py`
- `tests/test_host_resume.py`
- `tests/test_runtime_publisher.py`
- 新建 `tests/test_memory_candidate_projection_outbox.py`
- 现有所有 `CustomEvent` fixture tests
- 新建 `tests/support/events.py`

### Long-term contracts

- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`
- `contracts/RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md`
- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`
- `contracts/HOST_RESUME_CONTRACT.zh.md`
- `contracts/MCP_CAPABILITY_CONTRACT.zh.md`
- `contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md`
- `contracts/RECOVERY_CONTRACT.zh.md`
- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`
- `contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md`
- `contracts/RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md`
- `contracts/GRAPH_JSONLD_STORAGE_CONTRACT.zh.md`
- `PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md`

---

## 19. Definition of Done

只有全部满足，才能判定本阶段完成：

### Vocabulary

- [x] production 7 个 CustomEvent 全部 typed；
- [x] `CustomEvent` class 已删除；
- [x] `EventType.CUSTOM` 已删除；
- [x] default AgentEvent union/decoder 无 CUSTOM；
- [x] old seven string literals 在 `src/` 为零；
- [x] 8 个新类型与 typed suspension均 explicit non-transcript；
- [x] event schema generation 已 bump；
- [x] reset workflow 已执行并记录。

### MCP lifecycle

- [x] `ToolExecutionSuspendedEvent` MCP branch不再保存 `dict[str, Any]`；
- [x] suspension typed fact完整冻结 binding/request/pending lease/deadline/predecessor；
- [x] durable request只含 typed user-visible requests与 opaque request/state fingerprints；
- [x] SESSION_REOPEN 不恢复已丢失 lease，closure + ToolResult + RunEnd ordering已验证；
- [x] Host ingress只保存 `PreparedMcpInputRequiredResolution`；
- [x] adapter实际 payload与 prepared canonical bytes exact一致；
- [x] resolution-submitted 与 Host resume boundary同批；
- [x] `resume_failed -> resolution_submitted` 使用 attempt ordinal与双 predecessor exact refs；
- [x] normal resumed ToolResult durable引用 exact resolution event；
- [x] expired/binding-changed 与 ToolResult terminal同批；
- [x] suspension/boundary/resume-failed publication failure均由正确 typed closure收口；
- [x] terminal disposition publication failure不生成第二个 closure；
- [x] closure与 RunEnd publication failure遵守 terminalization matrix；
- [x] child pending unsupported通过 closure + ToolResult + RunEnd收口；
- [x] child suspension FULL/closure前 crash先由 child MCP recovery收口；
- [x] `repair_dangling_children()` 对 active MCP lifecycle delegate/skip；
- [x] parent graph terminal ref只在 child closure/ToolResult/RunEnd FULL后产生；
- [x] generic dangling repair在 MCP lifecycle recovery之后运行；
- [x] active MCP suspension不能被普通 RunEnd跳过；
- [x] resume failure有 bounded redacted diagnostic；
- [x] source suspension、boundary、resolution、terminal exact join；
- [x] restart/Inspector复用唯一 reducer。

### Mandatory audits

- [x] resume-failed/request/skip/evidence-failure均由统一 session owner提交；
- [x] NONE复用同一 stable candidate；
- [x] mandatory/compaction owner使用 `write_events_with_deadline()` 与 frozen deadline budget；
- [x] ordinary deadline为 terminal maintenance保留 bounded tail；
- [x] 所有 critical publication-aware owner均禁用 legacy full-timeout writer wrapper；
- [x] caller cancellation只 detach；
- [x] UNKNOWN/PARTIAL/deadline安装 audit latch；
- [x] critical publication unavailable安装 explicit latch，不宣称 live catch-up；
- [x] latch后只有 exact terminal-maintenance lease可以继续 bounded terminal write；
- [x] maintenance lease绑定 exact ordered batch，NONE可重试且只有 FULL才 consumed；
- [x] maintenance lease由 RuntimeSession唯一签发，closed owner kind、opaque handle对象身份与
  ISSUED generation均被强校验；
- [x] copied identity、fake/stale/invalidated handle不能绕过 latch；
- [x] Host close停止 admission并 drain owner；
- [x] typed failure audit未被误报为 durable projection retry job。

### Compaction ownership

- [x] `DirectEventLogCompactionEventCommitPort` 已删除；
- [x] service不能缺省构造 writer；
- [x] pending owner无 EventLog branch；
- [x] publication-after-commit不会生成 conflicting terminal；
- [x] `started_publication` stage及特殊 nullability matrix已进入 durable schema；
- [x] compaction attempt冻结 no-active-run/mid-turn publication terminalization scope；
- [x] pre-run/manual publication failure关闭 session且不伪造 RunEnd；
- [x] mid-turn publication failure关闭 exact window/account并写
  `compaction_publication_unavailable` aborted RunEnd；
- [x] compaction RunEnd source refs exact绑定 Started（若存在）与最终 Completed/Failed；
- [x] service返回 exact core attempt receipts；
- [x] candidate proposal使用独立 projection receipt/owner；
- [x] Completed publication失败 suppresses prepared projection且不安装 owner/outbox；
- [x] preparation/owner-install failure均有合法 bounded receipt；
- [x] owner-installed与 candidate-frozen是两个可验证 phase；
- [x] proposal迟到/失败不改变 canonical Completed；
- [x] historical Inspector不展示 process-local projection receipt状态；
- [x] 无 durable producer/outbox authority时显示 `not_durably_observable`；
- [x] Host live diagnostics与 historical Inspector观察面已明确分离；
- [x] close分别 drain projection owner与 durable outbox；
- [x] Host post-scan helpers已删除；
- [x] mid-turn post-scan helpers已删除；
- [x] Host/mid-turn不二次调用 `publish_stored_events()`；
- [x] concurrent unrelated writes不会污染 compaction result。

### Direct EventLog mutation

- [x] 两个 LLM recovery direct test branches已删除；
- [x] exact AST observed set等于四项 allowlist；
- [x] maintenance AST observed multiset等于 frozen inventory；
- [x] no suffix/file-wide exception；
- [x] offline doctor仍受 exclusive authority约束。

### Quality

- [x] bounds/accounting doctor全绿；
- [x] crash matrix全绿；
- [x] Inspector typed projection全绿；
- [x] long-term contracts同阶段更新；
- [x] `ruff` 全绿；
- [x] 全量非联网 pytest全绿；
- [x] core dogfood无重复 compaction publication。
- [x] debt rebase只关闭 D2 四项，D3与D5保持 OPEN。

---

## 20. 最终架构

```text
Domain producer / prepared immutable ingress owner
    |
    v
Typed bounded event candidate
    |
    v
RuntimeSession write service
    |
    +--> EventLog atomic commit
    +--> materialization account
    +--> committed reducers
    +--> ordered publisher
    |
    v
Exact commit receipt
    |
    +--> Agent stream return
    +--> Host local listener
    +--> Compaction core attempt result
    |
    +--> publication completed/enqueued
    |       -> ordinary continuation
    |
    +--> publication unavailable/failed
            -> publication latch
            -> exact terminal-maintenance lease
            -> MCP / mandatory audit terminalization
            -> compaction frozen-scope branch
                 |-> no active run: close session, no RunEnd
                 |-> mid-turn: close window/account + aborted RunEnd
            -> bounded close / reopen recovery

ContextCompactionCompletedEvent
    |
    +--> publication completed/enqueued
    |       -> Independent compaction candidate projection owner
    |       -> MemoryCandidateProjectionCommitPort
    |       -> transactional outbox
    |       -> candidate pool dispatcher
    |
    +--> publication unavailable/failed
            -> typed-abandon prepared projection
            -> suppressed_by_publication_latch receipt

Compaction projection observability
    |
    +--> live Host owner
    |       -> full process-local receipt state
    |
    +--> historical Inspector
            -> durable producer/outbox/candidate-pool proof only
            -> otherwise not_durably_observable
```

允许的 direct EventLog mutation 被压缩为：

```text
RuntimeSession canonical writer
Materialization account atomic primitive
Runtime projection checkpoint primitive
Privileged offline checkpoint doctor
```

session-owner bootstrap、privileged run-projection repair与 pytest-only account adoption仍存在，
但它们属于第 13.2 节单独计数、单独授权的 maintenance surface；它们不具备 append typed
business event 的能力。

除此之外，任何 production 模块都只能：

```text
prepare typed candidate
call typed writer port
consume exact commit receipt
```

这才是 typed event vocabulary 真正完成的定义：不仅 event class 有类型，event 的 writer、
publication、recovery 和 discovery owner 也必须只有一个。

本 hard cut完成时，D2四项关闭；durable Hook/projection job（D3）与完整
compaction-memory extension ownership（D5）仍保持独立 OPEN，不得因新增 typed audit或
复用现有 projection outbox而提前宣告闭环。

---

## 21. 2026-07-23 实施与审计回执

本节记录 EV0–EV4 实际落地结果。它是第 19 节勾选项的可复核证据，不改变前文契约。

### 21.1 阶段门控

| 阶段 | 结果 |
|---|---|
| EV0 | additive facts、absolute-deadline writer、publication latch与opaque maintenance lease门控通过 |
| EV1 | compaction唯一 RuntimeSession writer、exact attempt receipt及独立 candidate projection owner门控通过 |
| EV2 | typed vocabulary、MCP lifecycle/recovery、mandatory audit owner门控通过 |
| EV3 | exact AST EventLog mutation allowlist与maintenance inventory门控通过 |
| EV4 | Inspector、contracts、debt rebase、reset-only world、全量非联网测试与core dogfood通过 |

阶段合并回归门控结果为 `729 passed`；architecture/static subset为 `44 passed`；
最终受影响回归为 `160 passed`。最后一轮 code review修复后又执行了 focused gate：
Agent loop `96 passed`、compaction `106 passed`、其余 lifecycle/architecture/recovery
`314 passed`，合计 `516 passed`。

### 21.2 Reset-only durable world

执行了 PostgreSQL/EventLog、provider-input、ToolResult projection、monitor/account及
Oxigraph world reset。因为本地 `vector` extension由平台角色拥有，reset保留已安装
extension，仅删除 Pulsara owned relations/function并重新执行 packaged migrations
`0000`–`0004`。

```text
migration status: migrated
migration head: 4
registry prefix:
  sha256:15a224ceebb327d24b5f36c38bd9da0305defb71dff98f48aa8223647c8444e1

deep verify: verified
expected/observed deep catalog:
  sha256:9df3727ae30f23bf46f04c4386882246d48218e278fdf2e0cdcb32c15c7f1d9c

db status: up_to_date
Oxigraph triples after reset: 0
```

本地开发环境的 admin/runtime role相同，因此 verifier正确报告 runtime role具备
`public` schema DDL capability；这不是 schema drift，生产部署仍应使用 restricted
runtime role。

### 21.3 静态与离线验证

```text
uv run ruff check .
  PASS

uv run pytest -q
  2455 passed, 2 skipped, 188 warnings in 1539.60s

git diff --check
  PASS
```

两个 skip均为环境条件测试，不是 failure。最终 source audit确认：

- `CustomEvent`、`EventType.CUSTOM`及七个旧 production literal在 `src/` 中为零；
- `DirectEventLogCompactionEventCommitPort`为零；
- Host/compaction/Agent生产路径不再二次调用 `publish_stored_events()`；
- exact AST mutation observed set与四项allowlist相等；
- 8个新 typed events及typed MCP suspension均为 explicit non-transcript。

### 21.4 Core dogfood

只运行冻结的 `manual-compaction-trail`，未重跑全量real-LLM历史测试：

```text
status: passed
elapsed: 91.1s
model calls: 10
tool calls: 7
total tokens: 242765
cached input tokens: 151680
result:
  /private/tmp/pulsara-core-dogfood-20260723T164910Z/
```

Durable evidence显示：

- `CONTEXT_COMPACTION_STARTED=1`；
- `CONTEXT_COMPACTION_COMPLETED=1`；
- `CONTEXT_COMPACTION_FAILED=0`；
- `TOOL_RESULT_END=7`且`TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED=7`；
- `RUN_START=2`且`RUN_END=2`；
- Inspector error diagnostics为空；
- compact后禁止重新读文件的hidden verifier通过。

因此真实Host路径没有重复 compaction publication，ToolResult evidence projection正常，
并验证了manual/preflight compaction后的durable continuation。冻结core suite当前没有
MCP input-required provider fixture，故没有伪造real MCP resume；该分支由确定性
lifecycle、crash和reopen测试覆盖，符合第 17 节“若环境具备”的边界。

### 21.5 债务边界

`PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md`仅关闭 D2 的四项：

1. 七个 production CustomEvent typed化；
2. compaction direct production fallback删除；
3. Host compaction post-scan/double publication删除；
4. direct EventLog mutation exact allowlist。

D3 durable Hook/projection job outbox与D5 compaction-memory extension ownership继续为
`OPEN`。

### 21.6 2026-07-24 最终 reviewer finding 收口

最终静态复审提出的六项问题已补齐：

1. durable diagnostic改为closed sanitizer registry；非MCP profile只持久化固定消息，
   MCP显式消息也必须经过central secret scrubber，任何路径都不再回退`str(error)`；
2. compaction模型调用与event write deadline物理分离，Started、Completed、Failed各自在
   第一次write admission冻结自己的budget，pending drain复用原budget；
3. Host SESSION_REOPEN在任何repair前冻结唯一absolute deadline，并贯穿main/child
   recovery、SQL、MCP closure、RunEnd与projection repair；fresh open不继承该deadline，
   而是在前置资源准备完成、RuntimeSession bootstrap admission时独立冻结startup deadline；
4. live MCP lifecycle reducer删除全历史与`_history`，无关batch走O(1) fast path，
   terminal/RunEnd后prune；Inspector只在有界历史query期间启用临时terminal snapshot sink；
5. mandatory audit与compaction的`NONE` retry使用10ms到250ms的确定性bounded backoff，
   completed mandatory owner从resident attempt map退休；
6. publication-latched RunEnd在RuntimeSession precommit中按reason exact rebind真实stored
   events，并验证compaction pairing及MCP suspension/resolution/ToolResult/closure chain。
7. `ContextCompactionAttemptResult`删除含义漂移的attempt-wide/latest-receipt budget，只保留
   exact terminal candidate首次writer admission的`terminal_event_deadline_budget`；
   completed/failed required，not-attempted为`None`。

按用户要求，本轮发现测试错误后没有重新运行全量pytest，只重跑失败node及直接受影响的
focused tests。对应新增/更新回归覆盖raw secret、45秒模型调用、orphan terminal drain、
expired shared reopen deadline、2000个无关event constant-space fold、Inspector临时终态
projection、positive bounded backoff与tampered exact source rejection。
