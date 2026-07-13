# Pulsara Context Compiler Immutable Input Hard Cut 实施规格

_状态：C0–C5 已实施并通过 hard-cut 验收；保留为阶段三实现与复审依据_

_创建：2026-07-12_

_目标阶段：阶段三 C0–C5_

_完成：2026-07-13_

---

## 0. 文档目的

本文档定义 Pulsara 下一阶段的 **Context Compiler Immutable Input Hard Cut**。

本阶段要完成的不是一次 context 文案重写，也不是完整的 `ContextSource` registry。它要做的是把
context compiler 的输入边界从 mutable runtime working state 硬切为可序列化、可 fingerprint、可 replay
的 typed facts，并在进入 compiler 前完成 transcript/tool-result 的结构化规范化。

本阶段完成后，生产编译路径必须满足：

```text
committed run entry / continuation facts
        +
bounded durable event slice
        +
current model-step typed facts
        |
        v
immutable ContextFactSnapshot
        +
TranscriptCompileInput
        +
ToolResultRenderUnit[]
        +
ContextSectionCandidate[]
        |
        v
PreparedToolResultRenderInput
        +
PreparedContextCandidateSet
        |
        v
pure provider-neutral context compiler
        |
        v
CompiledContext + ContextCompiledEvent audit
```

生产 compiler 不再接收、导入或间接读取：

- `LoopState`；
- `LoopState.messages`；
- `LoopState.scratchpad`；
- live MCP supervisor / manager；
- session permission default；
- mutable `CapabilityExposurePlan`；
- 预渲染的 `ContextCompileInputs`；
- 以 `Msg.metadata` 作为唯一来源的 timing/render facts；
- current-user/history/recovery 的猜测式 fallback。

本规格是阶段四 **Long-Horizon Context Windows** 的硬前置。没有本阶段，后续 rollup、current-run
micro-compaction、pairing-safe LLM compaction 和 window generation 都只能修改字符串投影，无法证明
事实、顺序和 pairing 没有被破坏。

---

## 1. 范围冻结

### 1.1 本阶段必须完成

本阶段只包含以下五类工作：

1. **Immutable `ContextFactSnapshot`**
   - 从 committed run entry、optional committed continuation boundary 与本 model step typed facts 构造；
   - live/replay 产生同形状事实；
   - 不持有 mutable runtime object。

2. **Normalized transcript input**
   - 引入 `TranscriptCompileInput`；
   - 保留 message/block order、assistant tool call、tool result pairing、compaction/recovery provenance；
   - current user 只来自 typed `RunStartEvent.current_user_message`。

3. **Normalized tool-result units**
   - 引入 `ToolResultRenderUnit`；
   - tool-result body、artifact、timing、render profile、source event attribution 在 renderer 前已结构化；
   - renderer 不再反查 `Msg.metadata` 或按 payload/name 猜 tool domain。
   - 完整resolved render policy与cache hints在pure compiler前冻结为prepared input。
   - semantics builder的声明式semantic contract作为durable truth；具体wheel/Git/build identity只作为process-local
     diagnostic，不进入descriptor、event、requirement或允许判断。
   - delayed external completion在original committed requirement中冻结essential capture policy；result ingress不得读取
     当前runtime/session policy，也不得由外部caller回显Pulsara authority字段。

4. **最小 typed `ContextSectionCandidate` ingress**
   - 代替 `component_prompts: tuple[tuple[str, str], ...]`；
   - 本阶段允许 AgentRuntime-side collector 暂时把现有 subsystem facts 转换成 candidate；
   - lifecycle/collection decisions冻结为`PreparedContextCandidateSet`；
   - compiler 不再接受裸 component string或mutable cache port。

5. **Compiler API / legacy facade hard cut**
   - 删除 `ContextCompileRequest.state`；
   - 删除 production `ContextCompileInputs`；
   - 删除 current-user/history/scratchpad cache fallback；
   - 删除 production `build_llm_context()`、`msg_to_llm_messages()` 旧入口。

### 1.2 本阶段明确不完成

以下工作属于后续阶段，不得借本章扩大范围：

- 完整 `ContextSource` registry；
- 将 system、runtime、memory、capability、plan、recovery、subagent producer ownership 全部迁出
  `AgentRuntime`；
- Long-Horizon 的跨 tool-result rollup、micro-compaction、pairing-safe LLM compaction；
- dynamic context window generation；
- rollout budget / finalization reserve；
- prompt cache lane、provider cache controls、`ProviderInputPlan`；
- 全局 RuntimeEventWriter / governance UOW 重构；
- system prompt 内容设计调整。

因此，本阶段的 `ContextSectionCandidate` 是 **typed ingress contract**，不是最终 source ownership。
正式 source registry 与“每个 model-visible non-transcript byte 的唯一 producer attribution”仍属于阶段五。

### 1.3 阶段依赖顺序

```text
ResolvedModelCall Hard Cut                  已完成
Host Run Boundary Safe Point Hard Cut       已完成
MCP Startup Latency Hard Cut                已完成
             |
             v
Context Compiler Input Hard Cut             本文
             |
             v
Long-Horizon Context Windows
             |
             v
ContextSource Ownership Hard Cut
             |
             v
Prompt Cache
```

Prompt Cache 不允许直接建立在旧字符串 facade 或 mutable `LoopState` 上。

---

## 2. 当前代码真值

本节不是历史描述，而是本轮 hard cut 必须删除的真实接缝。

### 2.1 `ContextCompileRequest` 直接暴露 mutable runtime

当前 `src/pulsara_agent/runtime/context_engine/types.py` 中的 request 直接持有：

```python
state: LoopState
current_user_message: Msg | None
current_user_input: str
tools: tuple[ToolSpec, ...]
exposure: CapabilityExposurePlan | None
budget: LoopBudget
```

这意味着 compiler/source 即使换文件，仍可读取：

- `state.messages`；
- `state.memory_projection`；
- `state.scratchpad`；
- pending/stop/recovery mutable state；
- live exposure object；
- rollout budget 中与 compile 无关的控制字段。

文件边界并没有形成 ownership 边界。

### 2.2 `ContextCompileInputs` 是预渲染 compatibility facade

当前 `src/pulsara_agent/runtime/context_engine/compiler.py` 的 `ContextCompileInputs` 接收：

- 已经 lowering 的 `LLMMessage`；
- system prompt string；
- component prompt string tuples；
- recovery message；
- tool render decisions/report dict。

pairing、source event、artifact、timing、compaction attribution 在进入 compiler 前已经被压平或塞进
arbitrary dict。compiler 无法验证这些字符串是否来自同一 durable facts。

### 2.3 `runtime/context.py` 同时拥有推断、渲染、cache 与 compile

当前 facade 会：

1. 从 `state.memory_projection` 拼 memory string；
2. 从 scratchpad 查找或创建 tool-result render cache；
3. 从 `state.messages` 推断 current-user anchor；
4. 直接调用 `render_segmented_llm_messages()`；
5. 从 mutable `Msg` 重新提取 user text/timing；
6. 构造 `ContextCompileRequest` 与 `ContextCompileInputs`；
7. commit process-local render cache。

这不是一个 compiler input adapter，而是多个真源混合的第二 orchestration layer。

### 2.4 tool-result renderer 仍读取 mutable message projection

当前 renderer 从 `list[Msg]`：

- 查找 assistant tool call；
- 查找 result block；
- 从 `Msg.metadata["tool_observation_timing_by_call_id"]` 恢复 timing；
- 从 JSON payload 识别 terminal/background-process essential envelope；
- 在同一步中完成 pairing、allocation、body clipping 和 provider-neutral lowering。

因此 renderer 无法区分：

- durable fact 缺失；
- replay projection 丢字段；
- payload 恰好长得像 terminal JSON；
- cache 中的旧字符串与当前 unit 语义不一致。

### 2.5 compiler 内仍有 fallback

当前 compiler 仍会：

- 从 `request.state.messages` 重新 split transcript；
- current-user anchor 缺失时退回 legacy history；
- 从 `request.state.memory_projection` 计算 dependency fingerprint；
- 从 mutable state 构造 recovery message；
- 接受 current user 为空或推断失败后继续。

hard cut 后这些情况必须变成 typed contract error，不能继续生成“看起来可用”的 context。

### 2.6 `ContextSection` 只是浅 frozen

当前 dataclass 虽然 `frozen=True`，但 `provenance`、`metadata` 仍是 mutable dict，`text` 也没有
content fingerprint/size/source reference invariant。它不能直接作为 event-safe compile input。

### 2.7 current durable facts 已具备的前置

以下 hard cut 已为本阶段提供基础：

- `RunStartEvent.current_user_message` 是 required typed durable truth；
- `RunStartEvent` required 持有 host/subagent run entry；
- `CommittedHostRunEntry | CommittedSubagentRunEntry` 已存在；
- `RunWorkingSet` 已持有 run-frozen target、permission、capability basis/exposure；
- `ResolvedModelCallFact` 与 estimator/limits 已单一真源；
- `ToolResultEndEvent` / `ExternalExecutionResultEvent` required universal timing；
- Host/child ledger 可以通过 `EventLogLocator` 定位；
- compaction boundary 与 summary artifact 已 durable。

本阶段必须消费这些 typed facts，而不是再从 process-local string/dict 重建它们。

---

## 3. 核心不变量

### 3.1 Durable truth 与 compile projection

```text
AgentEvent / artifact       durable truth
ContextEventSlice           canonical immutable authority view
TranscriptProjectionWindow  model-visible transcript selection
Context*Fact                event-safe compile facts
Context* runtime wrapper    process-local immutable bindings/caches
CompiledContext             one model-call provider-neutral projection
```

`Msg` 仍可作为 UI/streaming projection，但不能再作为 production compiler 的输入真源。

### 3.2 One snapshot, one bounded ledger view

一次 compile attempt 必须先冻结一个 `source_through_sequence`。所有 transcript、tool-result、resume、
exposure、memory/subagent attribution 都只能读取：

```text
sequence <= source_through_sequence
```

同一次 compile 中禁止各 collector 分别调用 `event_log.iter()` 得到不同 high-water。

### 3.3 Committed entry only

snapshot builder required 接收：

```python
CommittedRunEntry = CommittedHostRunEntry | CommittedSubagentRunEntry
```

可选 continuation required 使用 committed carrier：

```python
CommittedInteractionResumeBoundary
```

不接受 draft/prepared boundary，不接受裸 event，不从 `LoopState` 推断 RunStart。

### 3.4 Host/child 对称但不伪造边界

- Host run 使用 `NewRunBoundaryFact`；
- child run 使用 `SubagentRunEntryFact`；
- child 不创建假的 Host boundary；
- continuation 不是新 run entry；
- snapshot 的 run-entry union 必须保持该差异。

### 3.5 Current user exactly once

一次 compile input 中必须有且仅有一个 current-user anchor，并满足：

```text
TranscriptCompileInput.current_user_anchor
  == RunStartEvent.current_user_message.message_id

normalized current-user text/hash/observed_at
  == RunStartEvent.current_user_message
```

不允许从 `metadata.user_input`、API input、`state.messages` 或 ID naming convention 补造。

### 3.6 Pairing before rendering

tool call/result pairing 必须在 `TranscriptCompileInput` / `ToolResultRenderUnit` 构造期完成。compiler 不得
通过字符串、tool name 或 block adjacency 猜 pairing。

### 3.7 Compiler purity

给定：

```text
same ContextFactSnapshotFact
same TranscriptCompileInput
same PreparedToolResultRenderInput semantic payload
same PreparedContextCandidateSet
same compiler contract version
same estimator contract
```

必须得到 byte-equivalent provider-neutral `LLMContext` 与相同 allocation/render decisions。

compile wall-clock 不能在 compiler 内读取；时间必须由 snapshot 中的 `compiled_at_utc` 显式传入。

### 3.8 Cache is not truth

process-local cache 只允许减少计算，不得改变语义输出。cache miss、eviction 或进程重启必须得到同一
compiled result。cache payload 与 canonical recomputation 不一致时必须丢弃/报 diagnostic，不能继续复用。

### 3.9 No silent compatibility

最终 C5 后：

- schema 缺失不是 `None`；
- malformed timing/render profile 不是 `unknown`；
- missing current user 不是 empty string；
- pairing gap 不是 prose warning 后继续；
- old constructor 不做 alias/fallback；
- 不从 scratchpad 推断新 facts。

未上线环境可 reset DB 或执行一次显式 migration；不保留长期 dual reader。

---

## 4. 模块与依赖方向

### 4.1 新模块

建议新增：

```text
src/pulsara_agent/primitives/context.py
src/pulsara_agent/runtime/context_input/
    __init__.py
    event_slice.py
    snapshot.py
    transcript.py
    tool_results.py
    candidates.py
    invocation.py
src/pulsara_agent/runtime/context_engine/compiler.py
src/pulsara_agent/runtime/context_engine/cache.py
```

### 4.2 依赖方向

```text
primitives.*
   -> message.schema / event.schema
   -> event log / replay
   -> runtime.context_input projectors
   -> runtime.context_engine compiler
   -> AgentRuntime / HostSession orchestration
```

`primitives.context` 禁止 import：

- `runtime.*`；
- `message.Msg`；
- MCP manager/supervisor；
- `CapabilityExposurePlan`；
- `ResolvedModelCall` process-local object；
- cache implementation。

### 4.3 event-safe 与 process-local 分层

必须明确区分：

```python
class ContextFactSnapshotFact(BaseModel):
    """可序列化、可持久化、可 fingerprint 的 compile facts。"""

@dataclass(frozen=True, slots=True)
class ContextFactSnapshot:
    """process-local immutable invocation binding。"""
    fact: ContextFactSnapshotFact
    resolved_call: ResolvedModelCall
    materialized_tool_specs: tuple[ContextMaterializedToolSpecInput, ...]
```

其中 `ContextMaterializedToolSpecInput` 只持有对应 `ContextToolSpecFact` 与已hash-validated的
`FrozenJsonObjectFact` schema，不持有callable或mutable `ToolSpec.parameters`。runtime wrapper只额外持有
resolved call/estimator calculation binding和immutable schema materialization。
禁止持有 manager、session、event log、tool callable、workspace path 或 mutable state。

wrapper 构造时必须验证：

```text
resolved_call.fact == fact.resolved_model_call
materialized_tool_specs facts/fingerprints == fact.tool_specs
```

最终 `ToolSpec` 在compiler lowering末端从 `ContextToolSpecFact` thaw成新owned对象；process-local wrapper不长期持有
当前 `ToolSpec.parameters` mutable dict。compile policy直接读取snapshot内的immutable fact，不建立第二runtime对象。

---

## 5. Canonical JSON 与 fingerprint

### 5.0 recursive immutable JSON

新 context DTO禁止直接暴露 `dict`/`list`。C0定义canonical immutable JSON：

```python
FrozenJsonScalar = str | int | float | bool | None

class FrozenJsonArrayFact(BaseModel):
    items: tuple["FrozenJsonValue", ...]

class FrozenJsonEntryFact(BaseModel):
    key: str
    value: "FrozenJsonValue"

class FrozenJsonObjectFact(BaseModel):
    entries: tuple[FrozenJsonEntryFact, ...]

FrozenJsonValue = FrozenJsonScalar | FrozenJsonArrayFact | FrozenJsonObjectFact
```

object entries按key严格排序且唯一；float必须finite；转换helper拥有`freeze_json()`/`thaw_json()`，wire/event边界只在
最后一步thaw。`MappingProxyType`不进入Pydantic/event JSON，避免inspect/serialization失败。

现有fact若内部仍含mutable JSON，不能直接嵌入新snapshot；必须转换为本章定义的context-specific frozen fact或只保存
typed identity/fingerprint。

### 5.1 统一 canonicalization

所有 context fact fingerprint 使用同一个 helper：

```python
canonical_json_bytes(
    payload,
    *,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
)
```

要求：

- UTC timestamp 规范化为 `YYYY-MM-DDTHH:MM:SS.ffffffZ`；
- map key 必须是 string；
- tuple/list 保持顺序；
- float 必须 finite；
- `None` 是否参与必须由 DTO schema 固定，不能调用者自行删除；
- fingerprint 使用 `sha256:<hex>`；
- recursive payload 不允许 Python object repr。

### 5.2 identity 与 semantic fingerprint 分离

带随机identity的顶层input同时有两类fingerprint：

- `*_semantic_fingerprint`：排除随机instance/storage ID，但包含全部model-visible语义、compile timing与policy；
- `*_fact_fingerprint`：包含instance ID、context/source attribution与semantic fingerprint，只排除自身fingerprint字段。

随机identity不得污染semantic equality，但durable manifest/event confirmation必须使用fact fingerprint，避免两个不同
snapshot ID落到同一artifact ID却拥有不同bytes。没有随机identity的transcript/unit/candidate可只保留一个fact-level
fingerprint；其stable source IDs参与hash。

### 5.3 Aggregate input fingerprint

最终 compile input fingerprint：

```text
sha256(
  snapshot_fact_fingerprint,
  transcript_fingerprint,
  prepared_tool_result_render_input_fingerprint,
  prepared_context_candidate_set_fingerprint,
  compiler_contract_version,
)
```

candidate/unit 顺序通过各prepared fingerprint参与aggregate。cache hints fingerprint明确排除；禁止先转set/sorted后
掩盖ordering bug。

---

## 6. `ContextEventSlice`

### 6.1 为什么需要 authority slice

只定义 immutable DTO 仍不够。如果 transcript projector、memory collector、subagent collector 分别读取 event
log，它们可能看到不同的 high-water。Stage 4 window rewrite 会因此不可复现。

这里的 bounded 首先表示 **有确定的 upper sequence bound**，不是本阶段新增任意event-count截断。必须分开：

- **canonical authority slice**：为 snapshot、join、pairing与projection提供同一high-water下的事实证据；
- **transcript projection window**：决定summary、retained historical tail与protected current run如何进入模型。

compaction window不得决定authority slice的起点。V1 authority起点是当前RunStart、latest validated
compaction terminal、非空retained-history range的起点与本次compile所有required local source refs中的最早sequence；
subagent eligible selection在引入durable graph checkpoint前使用parent ledger `sequence 1..source_through_sequence`专用source
range，并由同一个frozen slice运行pure reducer；无法证明更窄起点时从sequence 1
读取。不得为了性能静默丢弃required authority events。固定大小/window selection属于阶段四。

### 6.2 API

```python
@dataclass(frozen=True, slots=True, init=False)
class FrozenStoredEvent:
    event_id: str
    event_type: str
    sequence: int
    created_at_utc: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str

    def decode_owned(self) -> AgentEvent:
        """Deserialize a new owned object on every call."""

    @classmethod
    def from_stored_event(cls, event: AgentEvent) -> "FrozenStoredEvent": ...

@dataclass(frozen=True, slots=True)
class ContextEventSlice:
    runtime_session_id: str
    from_sequence: int
    through_sequence: int
    events: tuple[FrozenStoredEvent, ...]
    event_ids_fingerprint: str
    event_payloads_fingerprint: str

class ContextEventSliceReader(Protocol):
    async def read_through_current_high_water(
        self,
        *,
        runtime_session_id: str,
        minimum_sequence: int,
    ) -> ContextEventSlice: ...

    async def read_through(
        self,
        *,
        runtime_session_id: str,
        through_sequence: int,
    ) -> ContextEventSlice: ...
```

`ContextEventSlice`禁止保存 `AgentEvent` object reference。tuple只能冻结容器，不能冻结event内的metadata、tool calls、
execution results或artifact lists。`canonical_payload_bytes`包含stored sequence在内的完整strict JSON；bytes本身不可变。
projector每次通过统一serialization registry反序列化独立owned event，或直接从bytes构造context fact，不能修改slice。

authority range与transcript projection由两个typed plan分别决定：

```python
class TranscriptProjectionWindowFact(BaseModel):
    window_kind: Literal["uncompacted", "preflight_compaction", "mid_turn_compaction"]
    compaction_terminal_ref: ContextEventReferenceFact | None
    compaction_summary_artifact_id: str | None
    compacted_through_sequence: int | None
    keep_after_sequence: int | None
    retained_history_from_sequence: int | None
    retained_history_through_sequence: int | None
    protected_run_start_sequence: int
    protected_run_through_sequence: int
    window_fingerprint: str

class ContextAuthoritySlicePlan(BaseModel):
    through_sequence: int
    authority_from_sequence: int
    required_local_event_refs: tuple[ContextEventReferenceFact, ...]
    required_source_from_sequence: int | None
    transcript_window: TranscriptProjectionWindowFact
    plan_fingerprint: str

def finalize_context_authority_slice_plan(
    *,
    event_slice: ContextEventSlice,
    required_local_event_refs: tuple[ContextEventReferenceFact, ...],
    run_start_ref: ContextEventReferenceFact,
    latest_compaction_terminal_ref: ContextEventReferenceFact | None,
) -> ContextAuthoritySlicePlan: ...
```

`authority_from_sequence` 等于RunStart、optional terminal、optional retained-history from与
`required_local_event_refs.sequence`与optional `required_source_from_sequence`的minimum。required refs至少包含当前RunStart；有compaction时还包含terminal
event及重建typed input所需的其他local refs。所有refs均必须不超过`through_sequence`并位于authority slice内。

transcript projector单独消费window：

```text
optional compaction summary
-> retained historical events:
     keep_after_sequence + 1 .. min(compacted_through_sequence, RunStart.sequence - 1)
-> protected current run:
     RunStart.sequence .. authority through_sequence
```

`preflight_compaction`的compaction terminal早于current RunStart；`mid_turn_compaction`的terminal可晚于RunStart。两者都必须
把RunStart/current-user/current-run tool tail作为protected segment完整投影，不要求它们位于historical retained tail。
window ranges重叠时按source event ID去重且保留上述segment order；出现gap、倒序或protected current-run event缺失时fail closed。

window validator：

- `uncompacted`：compaction fields全空，retained history覆盖authority start到RunStart前一sequence（无历史时range成对为空）；
- compacted branch：terminal/artifact/compacted-through/keep-after全部required，`keep_after <= compacted_through`；
- retained range非空时`from == keep_after + 1`且`through == min(compacted_through, RunStart - 1)`；结果为空时from/through
  同时为空；
- `preflight_compaction` required `terminal.sequence < RunStart.sequence`；`mid_turn_compaction` required
  `terminal.sequence > RunStart.sequence`；
- `protected_run_start_sequence == RunStart.sequence`，`protected_run_through_sequence == authority through_sequence`；
- compacted historical range不得与protected current-run range交叉；compaction terminal本身是authority/audit fact，不当作model transcript message。

reader的`minimum_sequence`在读取前由durable RunStart/compaction boundary数值与required refs的minimum决定；
read boundary原子得到through sequence和
canonical bytes后，上述pure finalizer再冻结最终plan/window。禁止先单独读high-water来构造plan，也禁止在
projector内临时选另一个window。live/replay必须共用该finalizer。

### 6.3 原子读取语义

`read_through_current_high_water()` 必须在同一 EventLog read boundary 内得到：

- high-water；
- `<= high-water` 的 ordered event tuple。

PostgreSQL 实现使用 repeatable-read transaction 或等价 snapshot。InMemory test adapter 使用同一 lock 同时复制 high-water
和 events。不能先 `next_sequence()` 再无锁 `iter()`。

reader必须在上述read boundary内调用唯一`FrozenStoredEvent.from_stored_event()` factory，把stored rows/event
objects转成canonical bytes；离开lock/transaction后不再持有EventLog内部对象引用。factory先strict serialize，再用统一
serialization registry重新decode，并逐项验证：

```text
wrapper.event_id       == decoded.id
wrapper.event_type     == decoded.type
wrapper.sequence       == decoded.sequence
wrapper.created_at_utc == normalize_utc(decoded.created_at)
wrapper.payload_fingerprint == sha256(canonical_payload_bytes)
```

factory不对外暴露未验证constructor；`decode_owned()`也在返回前重做identity/fingerprint验证。range/order与projector
因此消费同一payload identity，不会分别相信wrapper与bytes。aggregate fingerprint直接基于已验证bytes计算，
不基于后续mutable decode结果。

C1同步收紧InMemory EventLog ownership：append/extend保存owned canonical copy；`iter()`、`get_by_id()`、`replay()`
每次返回重新deserialize的copy。即使InMemory只服务pytest，也不能让测试路径以共享mutable reference掩盖production
snapshot contract。PostgreSQL同样保证每次read得到新对象。

该read port是async contract。当前同步PostgreSQL driver不得直接阻塞event loop；V1可使用dedicated bounded read executor/
`to_thread`，但数据库transaction必须配置statement/connection deadline并在finally关闭connection。cancellation后的read worker
可以完成并丢弃结果，因为它不修改durable state；它不得回写snapshot/candidate，也不得持有HostSession ownership。
后续全局async event-log章节可替换实现，但不能改变本章read snapshot语义。

### 6.4 slice validation

必须验证：

- sequence 连续，严格递增；
- event ID 唯一；
- first/through 与 tuple 一致；
- 每个wrapper identity、created_at、payload fingerprint与decoded canonical event完全一致；
- RunStart 已包含且 sequence 不超过 through；
- optional resume boundary sequence 不超过 through；
- authority plan的required local refs全部存在；transcript window仅选择projection，不改变authority range；
- committed entry event canonical payload 与 slice 中同 ID bytes byte-equivalent；
- EventLog structural reconciliation latch 时禁止读取 compile slice。

空洞、duplicate、同 ID 不同 payload 均为 ledger contract error，不进入 compiler。

最低mutation regression：

- 修改 `decode_owned()` 返回event的nested metadata/list，不改变slice bytes/fingerprint；
- 修改 `event_log.iter()` 返回event，不改变后续read或stored ledger；
-两个projector从同一slice decode后互不影响；
-修改原append candidate对象不改变已存InMemory event。
- wrapper sequence/type/created_at/event ID任一与canonical bytes不同均拒绝。

### 6.5 child cross-ledger references

child compile 的 primary slice 来自 child runtime session。parent-owned MCP installation、subagent task/result facts通过
typed cross-ledger reference 与 `EventLogLocator` 读取额外 **named slices**。每个 named slice 也必须冻结自己的
owner runtime session、through sequence 和 fingerprint。

禁止在 compiler 中临时跨 ledger query。

---

## 7. 低层 DTO：snapshot

以下 DTO 落在 `primitives/context.py`。字段可按 Pydantic 语法实现，但语义不得改变。

所有本章 `BaseModel` 示例实际继承统一基类：

```python
class FrozenContextFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
```

字段中的JSON只允许第5.0节的frozen representation；`frozen=True`不被视为nested dict/list已不可变。

### 7.1 基础引用

```python
class ContextEventReferenceFact(BaseModel):
    runtime_session_id: str
    event_id: str
    sequence: int
    event_type: str
    payload_fingerprint: str

class ContextEventRangeFact(BaseModel):
    runtime_session_id: str
    first_sequence: int
    through_sequence: int
    event_count: int
    event_ids_fingerprint: str
    event_payloads_fingerprint: str
```

invariant：

- sequence >= 1；
- first <= through；
- primary contiguous slice要求 `event_count == through_sequence - first_sequence + 1`；
- named filtered attribution使用单独的ordered `ContextEventReferenceFact` tuple，不伪装成contiguous range；
- ID aggregate与stored-event canonical payload aggregate fingerprints都必须重算一致；
- 单event payload fingerprint 必须是 canonical SHA-256。

### 7.2 run-entry reference

```python
class ContextRunEntryReferenceFact(BaseModel):
    run_entry_kind: Literal["host", "subagent"]
    run_start: ContextEventReferenceFact
    stable_terminal_event_id: str
    run_entry: RunEntryFact
```

validator：

- `host` required `NewRunBoundaryFact`；
- `subagent` required `SubagentRunEntryFact`；
- IDs 与 RunStart payload 一致；
- stable terminal ID 与 RunStart 一致。

### 7.3 continuation reference

```python
class ContextContinuationReferenceFact(BaseModel):
    resume_boundary: ContextEventReferenceFact
    boundary: InteractionResumeBoundaryFact
    suspended_run_id: str
    suspended_state_token_fingerprint: str
```

raw suspended state token是process-local ABA guard，禁止进入snapshot/manifest。live collector可用raw token验证
`sha256_fingerprint("suspended-state-token:v1", raw)`等于durable boundary fingerprint，随后立即丢弃raw value；replay与
pure builder只消费fingerprint。

continuation不替换`run_entry`。snapshot保存最新boundary的完整fact，并保存全部committed continuation的ordered refs：

```python
continuation: ContextContinuationReferenceFact | None
continuation_refs: tuple[ContextEventReferenceFact, ...]
continuation_count: int
```

refs按sequence严格递增；`continuation_count == len(continuation_refs)`；count为0时latest必须为空；非0时最后一个ref
必须等于`continuation.resume_boundary`。这样多次ACTIVE→WAITING_USER→resume可审计，且不持久化ABA token。

### 7.4 permission snapshot fact

新增低层：

```python
class RunPermissionSnapshotFact(BaseModel):
    snapshot_id: str
    runtime_session_id: str
    run_id: str
    mode: PermissionMode
    expanded_policy: FrozenJsonObjectFact
    expanded_policy_fingerprint: str
    source: Literal["session_default", "plan_mode", "child_profile"]
    plan_restriction_active: bool
    fingerprint: str
```

runtime `RunPermissionSnapshot` 改为消费该 fact，不复制 preset mapping。snapshot builder 从 RunStart required fields
构造并与 `RunWorkingSet.permission_snapshot` 比较；不得重新读取 HostSession stored default。

thaw后的expanded policy必须等于low-level preset expansion；snapshot/runtime/run IDs必须与RunStart一致；
`plan_restriction_active=True` 时 mode 必须为 `read-only` 且 source 为 `plan_mode`。

### 7.5 compile policy

compiler/collector不能再接收整个`LoopBudget`，也不能保存其中的optional配置源值。必须在invocation preparation前
解析成全部非空的最终值：

```python
class ToolResultEnvelopeRenderPolicyFact(BaseModel):
    envelope_renderer_version: str
    truncation_marker_version: str
    artifact_envelope_version: str
    timing_header_version: str
    full_string_cap_chars: int
    compact_string_cap_chars: int
    minimal_string_cap_chars: int
    ultra_minimal_string_cap_chars: int
    max_process_summaries: int
    compact_process_summaries: int
    process_summary_string_cap_chars: int
    policy_fingerprint: str

class ToolResultRenderPolicyBasisFact(BaseModel):
    policy_version: str
    total_context_chars: int
    body_context_chars: int
    envelope_context_chars: int
    prior_history_context_chars: int
    current_run_tail_context_chars: int
    current_user_context_chars: int
    legacy_history_context_chars: int
    per_tool_cap_chars: int
    per_message_cap_chars: int
    per_envelope_cap_chars: int
    latest_result_reserved_chars_per_unit: int
    max_tool_results_per_context: int
    minimum_essential_envelope_chars: int
    max_artifact_refs_per_unit: int
    max_data_placeholder_chars: int
    envelope_render: ToolResultEnvelopeRenderPolicyFact
    basis_fingerprint: str

class ResolvedToolResultRenderPolicyFact(BaseModel):
    basis: ToolResultRenderPolicyBasisFact
    ordered_unit_ids: tuple[str, ...]
    latest_tail_unit_ids: tuple[str, ...]
    latest_reserved_unit_ids: tuple[str, ...]
    latest_reserved_total_chars: int
    current_tail_normal_context_chars: int
    protected_current_tail_total_chars: int
    initial_prior_remaining_chars: int
    initial_current_tail_remaining_chars: int
    initial_current_user_remaining_chars: int
    initial_legacy_remaining_chars: int
    unit_order_fingerprint: str
    policy_fingerprint: str

class ContextCandidateCollectionPolicyFact(BaseModel):
    policy_version: str
    projection_token_budget: int
    max_subagent_results_per_parent_compile: int
    max_inline_candidate_chars: int
    max_aggregate_candidate_chars: int
    max_candidate_source_refs: int
    max_candidate_artifact_refs: int
    max_input_manifest_chars: int
    policy_fingerprint: str

class ContextAllocationPolicyFact(BaseModel):
    section_policy_version: str
    required_section_ids: tuple[str, ...]
    optional_section_priority_order: tuple[str, ...]
    lifecycle_policy_version: str
    timing_header_policy_version: str
    fingerprint: str

class ContextCompilePolicyFact(BaseModel):
    compiler_contract_version: str
    tool_result_basis: ToolResultRenderPolicyBasisFact
    candidate_collection: ContextCandidateCollectionPolicyFact
    allocation: ContextAllocationPolicyFact
    fingerprint: str
```

`resolve_context_compile_policy(loop_budget)`只允许在AgentRuntime composition/invocation preparation层调用一次。它必须
完全复现当前`_ToolResultRenderAllocator.from_loop_budget()`的所有derivation，包括body/envelope split、prior/current split、
legacy、per-tool/per-message defaults、per-envelope minimum、max units和minimum essential；输出中不得有`None`。

normalized transcript/units形成后，再调用：

```python
resolve_tool_result_render_policy(
    basis: ToolResultRenderPolicyBasisFact,
    transcript: TranscriptCompileInput,
    units: tuple[ToolResultRenderUnit, ...],
) -> ResolvedToolResultRenderPolicyFact
```

它冻结latest tail/reserved unit集合与所有initial segment balances。renderer只消费resolved policy，不自行重新推导。
候选collector只消费`ContextCandidateCollectionPolicyFact`，不得回读`LoopBudget.projection_token_budget`或
`max_subagent_results_per_parent_compile`。

policy validator至少要求：所有char/count非负，minimum essential >=1；body<=total；envelope<=total-body；V1
`current_user_context_chars == current_run_tail_context_chars`；ordered unit IDs精确等于normalized units；latest/reserved
IDs是保持原order的subsequence；`latest_reserved_total == per_unit * len(reserved_ids)`；normal current与protected total按
上述公式精确重算；initial balances与resolved segment caps一致；所有renderer version与numeric caps进入fingerprint。

V1可继续解析出当前36K aggregate policy，但它只是typed、完整的resolved input。阶段四将policy resolver改成从
`ResolvedModelContextBudgetFact`派生dynamic soft projection target；不得把36K或当前default derivation复制进compiler。

### 7.6 tool specs

```python
class ContextInlineToolSchemaFact(BaseModel):
    kind: Literal["inline"]
    schema: FrozenJsonObjectFact
    schema_chars: int
    schema_fingerprint: str

class ContextArtifactToolSchemaFact(BaseModel):
    kind: Literal["artifact"]
    schema_artifact_id: str
    schema_chars: int
    schema_fingerprint: str

ContextToolSchemaFact = ContextInlineToolSchemaFact | ContextArtifactToolSchemaFact

class ContextToolSpecFact(BaseModel):
    model_tool_name: str
    descriptor_id: str
    descriptor_fingerprint: str
    descriptor_render_attribution: CapabilityDescriptorRenderAttributionFact
    result_render_contract_fingerprint: str
    input_schema: ContextToolSchemaFact
    description: str
    source_binding_fingerprint: str

@dataclass(frozen=True, slots=True)
class ContextMaterializedToolSpecInput:
    fact: ContextToolSpecFact
    materialized_schema: FrozenJsonObjectFact
```

要求：

- tool specs 来自 run-frozen effective exposure；
- model-visible name 唯一；
- schema strict JSON，recursive frozen；
- schema超过inline safety cap时required artifact化，compiler invocation准备期校验hash后materialize；
- materialized schema hash/chars必须等于fact中的schema contract；
- descriptor/binding 与 `CapabilityExposureSnapshotFact` 对应；
- descriptor render attribution/contract fingerprint与run-frozen exposure内的同一descriptor对应；
- compiler runtime wrapper 中的 `ToolSpec` 必须与 fact byte-equivalent；
- callable 不进入 fact。

### 7.7 typed projection references

本阶段不迁移 producer ownership，但 snapshot 必须明确读取了哪些 facts：

```python
class ContextProjectionReferenceFact(BaseModel):
    projection_kind: Literal[
        "memory",
        "subagent_results",
        "recovery",
        "runtime_context",
        "capability_catalog",
        "capability_active_skill",
        "plan",
    ]
    owner_runtime_session_id: str
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_ids: tuple[str, ...]
    semantic_fingerprint: str
```

它只做 attribution，不决定 Stage 5 的 source registry ownership。

plan state也不能直接嵌入当前含mutable preset dict的`PlanWorkflowStateFact`。新增context-specific fact：

```python
class ContextPlanSnapshotFact(BaseModel):
    workflow_id: str | None
    active: bool
    revision: int
    entered_event: ContextEventReferenceFact | None
    entry_run_id: str | None
    stored_default_permission_mode: PermissionMode
    stored_default_permission_fingerprint: str
    accepted_plan_artifact_id: str | None
    fact_fingerprint: str
```

它从durable plan events/`PlanWorkflowStateFact`规范化构造，按active/inactive branch校验，不携带mutable policy dict。

### 7.8 static instruction 与 runtime environment

base system instruction和runtime context不能在compiler/collector中重新读取live defaults。新增：

```python
class ContextStaticInstructionFact(BaseModel):
    source_id: Literal[
        "base_system_instruction",
        "runtime_policy_instruction",
        "memory_scope_instruction",
    ]
    contract_version: str
    content_artifact_id: str
    content_fingerprint: str
    chars: int
    fact_fingerprint: str

class ContextRuntimeEnvironmentFact(BaseModel):
    workspace_identity_fingerprint: str
    workspace_kind: str
    model_visible_workspace_root: str
    terminal_current_cwd: str
    session_timezone: str | None
    observed_at_utc: str
    fact_fingerprint: str
```

composition root在model step freeze前把允许model可见的路径/环境转成该fact。snapshot/manifest保存该值；compiler
不读取 `Path.cwd()`、terminal manager或environment variables。secret paths/credential values禁止进入fact。

`runtime_context`正文必须由`ContextRuntimeEnvironmentFact + ContextCompileTimingFact`通过单一纯renderer生成，AgentRuntime
不得预渲染后以字符串传入。memory hook prompt若非空，必须先持久化为
`ContextStaticInstructionFact(source_id="memory_scope_instruction")`；其版本、artifact、content hash进入snapshot authority。
不得把当前`memory_context_prompt()`返回值直接当成自证正文。

terminal cwd允许随已提交tool result在不同model step间变化，因此它是step fact，不是run-frozen fact。其
`observed_at_utc`由显式host clock observation给出。

### 7.9 timing fact

```python
class ContextCompileTimingFact(BaseModel):
    compiled_at_utc: str
    session_timezone: str | None
    compiled_local_date: str | None
    current_user_observed_at_utc: str
    clock_source: Literal["host_clock"]
```

所有字段在 builder final freeze 时生成。compiler 不调用 `utc_now()`。

### 7.10 snapshot identity 与主体

```python
class ContextInputIdentityFact(BaseModel):
    snapshot_id: str
    schema_version: Literal["context-input:v1"]
    compiler_contract_version: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    context_id: str
    model_call_index: int
    compile_attempt_index: int
    context_retry_index: int
    source_through_sequence: int

class ContextFactSnapshotFact(BaseModel):
    identity: ContextInputIdentityFact
    run_entry: ContextRunEntryReferenceFact
    continuation: ContextContinuationReferenceFact | None
    continuation_refs: tuple[ContextEventReferenceFact, ...]
    continuation_count: int
    current_user_message: CurrentUserMessageFact
    permission_snapshot: RunPermissionSnapshotFact
    resolved_model_call: ResolvedModelCallFact
    capability_snapshot: CapabilityExposureSnapshotFact
    plan_snapshot: ContextPlanSnapshotFact
    mcp_installation_id: str
    mcp_installation_owner_runtime_session_id: str
    static_instructions: tuple[ContextStaticInstructionFact, ...]
    runtime_environment: ContextRuntimeEnvironmentFact
    compile_policy: ContextCompilePolicyFact
    tool_specs: tuple[ContextToolSpecFact, ...]
    projections: tuple[ContextProjectionReferenceFact, ...]
    candidate_source_selections: tuple[ContextCandidateSourceSelectionFact, ...]
    candidate_authorities: tuple[ContextCandidateAuthorityFact, ...]
    timing: ContextCompileTimingFact
    authority_slice_plan: ContextAuthoritySlicePlan
    primary_event_range: ContextEventRangeFact
    named_event_ranges: tuple[ContextEventRangeFact, ...]
    snapshot_semantic_fingerprint: str
    snapshot_fact_fingerprint: str
```

### 7.11 snapshot validator

必须验证：

1. identity 与 RunStart run/turn/reply 一致；
2. current user 与 RunStart typed fact完全一致；
3. permission、target、MCP owner 与 RunStart 一致；
4. resolved call target 等于 run target，call ID/index 属于当前 model step；
5. capability snapshot 属于当前 run entry 或 latest continuation；
6. continuation run ID 与 original run entry 一致；
7. continuation count/ordered refs/latest ref满足7.3节，且只保存token fingerprint；
8. source high-water >= 所有 source refs sequence；
9. primary event range owner 等于 runtime session，range与authority plan from/through完全一致；
10. authority required refs全部存在，transcript projection window与`TranscriptCompileInput.projection_window`完全一致；
11. tool names/descriptor IDs 唯一；
12. compile policy、tool specs 与 snapshot semantic/fact fingerprints 自洽；
13. static instruction source ID唯一、artifact/hash/version完整；
14. runtime environment workspace fingerprint与run capability basis workspace identity一致；
15. timestamp 是 UTC ISO，`compiled_at >= current_user_observed_at`且不早于runtime environment observation；
16. 每个candidate source selection的policy fingerprint与snapshot policy一致，source through等于snapshot high-water；
17. subagent selection满足`eligible == selected + omitted`、selected不超过cap、有omitted时selected必须填满cap；
18. subagent selected非空时projection/authority均required，selected为空时两者均禁止存在。
16. 所有 tuple 已 canonical ordered，不接受 caller unordered collection。

---

## 8. Snapshot builder

### 8.1 API

```python
class ContextSnapshotBuildInput(BaseModel):
    """Fully event-safe/frozen input; no runtime objects or raw ABA token."""

    identity: ContextInputIdentityFact
    run_entry: ContextRunEntryReferenceFact
    continuation: ContextContinuationReferenceFact | None
    continuation_refs: tuple[ContextEventReferenceFact, ...]
    current_user_message: CurrentUserMessageFact
    permission_snapshot: RunPermissionSnapshotFact
    resolved_model_call: ResolvedModelCallFact
    capability_snapshot: CapabilityExposureSnapshotFact
    plan_snapshot: ContextPlanSnapshotFact
    mcp_installation_id: str
    mcp_installation_owner_runtime_session_id: str
    static_instructions: tuple[ContextStaticInstructionFact, ...]
    runtime_environment: ContextRuntimeEnvironmentFact
    compile_policy: ContextCompilePolicyFact
    tool_specs: tuple[ContextToolSpecFact, ...]
    projections: tuple[ContextProjectionReferenceFact, ...]
    timing: ContextCompileTimingFact
    authority_slice_plan: ContextAuthoritySlicePlan
    primary_event_range: ContextEventRangeFact
    named_event_ranges: tuple[ContextEventRangeFact, ...]

async def collect_live_context_inputs(
    *,
    committed_entry: CommittedRunEntry,
    working_set: RunWorkingSet,
    continuation: CommittedInteractionResumeBoundary | None,
    raw_suspended_state_token_for_validation: str | None,
    resolved_call: ResolvedModelCall,
    event_slice: ContextEventSlice,
    named_slices: tuple[ContextEventSlice, ...],
    authority_slice_plan: ContextAuthoritySlicePlan,
    identity: ContextInputIdentityFact,
    compile_timing: ContextCompileTimingFact,
) -> ContextSnapshotBuildInput: ...

async def collect_replay_context_inputs(
    *,
    input_manifest: ContextCompileInputManifestFact,
    event_slice: ContextEventSlice,
    named_slices: tuple[ContextEventSlice, ...],
) -> ContextSnapshotBuildInput: ...

def build_context_snapshot(
    build_input: ContextSnapshotBuildInput,
) -> ContextFactSnapshotFact: ...

def bind_context_invocation(
    *,
    fact: ContextFactSnapshotFact,
    resolved_call: ResolvedModelCall,
    materialized_tool_specs: tuple[ContextMaterializedToolSpecInput, ...],
) -> ContextFactSnapshot: ...
```

只有live collector可以读取`RunWorkingSet`/committed process-local carrier；它必须把值规范化为
`ContextSnapshotBuildInput`。pure builder只消费该DTO。replay collector从manifest与durable facts构造同形状DTO，
不伪造`RunWorkingSet`。binding是最后一个process-local identity check，不参与fact fingerprint。

live collector validator：continuation为空时raw token必须为空；continuation非空时raw token required且hash必须等于durable
fingerprint。raw token不进入return DTO、diagnostic metadata或exception message。

### 8.2 builder 权限

live/replay collectors 可以读取各自明确授权的输入；pure builder只读取`ContextSnapshotBuildInput`。live collector可以读取：

- committed carrier；
- `RunWorkingSet` 中已经 run-frozen 的 facts；
- explicit event slices；
- explicit resolved call；
- composition-root 提供的 compile policy；
- frozen execution surface 中的 event-safe tool descriptor facts。

collectors/pure builder 禁止读取：

- `LoopState`；
- HostSession permission default；
- current MCP supervisor snapshot；
- live capability registry；
- mutable session manifest；
- scratchpad；
- wall clock（timing 必须由调用者传入）。

### 8.3 working set 只作 join，不作新真源

`RunWorkingSet` 是 process-local owner。只有live collector使用它取得runtime binding并转成event-safe values；所有
facts必须与 committed RunStart/exposure/continuation event交叉验证。pure builder与replay路径不接收working set。

出现：

```text
working_set fact != durable fact
```

时 fail closed，reason code `context_working_set_durable_fact_mismatch`。不得选择“较新”一方继续。

### 8.4 compile retry identity

同一 resolved model call 的 pressure/compaction retry：

- `resolved_model_call_id` 不变；
- `model_call_index` 不变；
- `compile_attempt_index` 递增；
- `context_retry_index` 递增；
- 每次重新读取新的 event slice 并创建新的 `snapshot_id`；
- `context_id` 每次 compile 可以变化；
- old snapshot 不可 mutation。

如果 retry 没有新增 durable facts，fingerprint 仍可能因 `compiled_at_utc` 改变；这是正确的，因为 model-visible timing
header 可能变化。

### 8.5 live/replay 共用 builder core

只有collection不同，`build_context_snapshot(ContextSnapshotBuildInput)`的canonical validation/fingerprint实现必须相同。
禁止维护live/replay两套snapshot constructor。replay是否能bind process-local `ResolvedModelCall`只影响是否可执行exact
compiler replay，不影响event-safe snapshot fact equality。

---

## 9. `TranscriptCompileInput`

### 9.1 目标

normalized transcript 必须保留 provider-neutral turn structure，而不是先变成 prose section 或 final
`LLMMessage`。最终 provider-neutral lowering 只发生一次，并由 compiler 拥有。

### 9.2 DTO

```python
class TranscriptTextBlockFact(BaseModel):
    kind: Literal["text"]
    block_id: str
    text: str
    content_fingerprint: str
    source_events: tuple[ContextEventReferenceFact, ...]

class TranscriptThinkingBlockFact(BaseModel):
    kind: Literal["thinking"]
    block_id: str
    thinking: str
    lowering_policy: Literal["provider_neutral_structured"]
    content_fingerprint: str
    source_events: tuple[ContextEventReferenceFact, ...]

class TranscriptDataPlaceholderFact(BaseModel):
    kind: Literal["data_placeholder"]
    block_id: str
    name: str | None
    media_type: str
    source_kind: str
    artifact_ids: tuple[str, ...]
    source_events: tuple[ContextEventReferenceFact, ...]

class TranscriptToolCallFact(BaseModel):
    kind: Literal["tool_call"]
    tool_call_id: str
    model_tool_name: str
    raw_arguments_json: str
    arguments_status: Literal[
        "valid_object",
        "invalid_json",
        "non_object_json",
    ]
    parsed_arguments: FrozenJsonObjectFact | None
    parse_error_code: ToolArgumentsParseErrorCode | None
    state: str
    source_events: tuple[ContextEventReferenceFact, ...]

class TranscriptToolResultRefFact(BaseModel):
    kind: Literal["tool_result_ref"]
    tool_call_id: str
    tool_result_unit_id: str
    source_events: tuple[ContextEventReferenceFact, ...]

TranscriptBlockFact = (
    TranscriptTextBlockFact
    | TranscriptThinkingBlockFact
    | TranscriptDataPlaceholderFact
    | TranscriptToolCallFact
    | TranscriptToolResultRefFact
)

class TranscriptMessageFact(BaseModel):
    message_id: str
    role: Literal["system", "user", "assistant"]
    name: str | None
    run_id: str | None
    turn_id: str | None
    reply_id: str | None
    created_at_utc: str | None
    finished_at_utc: str | None
    segment: Literal[
        "compaction_summary",
        "prior_history",
        "current_user",
        "current_run_tail",
        "recovery_note",
        "terminal_lifecycle_note",
    ]
    blocks: tuple[TranscriptBlockFact, ...]
    source_sequence_start: int
    source_sequence_end: int
    message_fingerprint: str

class ToolInteractionPairFact(BaseModel):
    tool_call_id: str
    model_tool_name: str
    call_message_id: str
    call_block_index: int
    result_message_id: str
    result_block_index: int
    call_sequence: int
    result_sequence: int
    pairing_status: Literal["completed", "external_completed"]
    pair_fingerprint: str

class CompactedWindowReferenceFact(BaseModel):
    compaction_id: str
    summary_artifact_id: str
    compacted_through_sequence: int
    keep_after_sequence: int
    summary_message_id: str
    source_event: ContextEventReferenceFact

class TranscriptCompileInput(BaseModel):
    schema_version: Literal["transcript-input:v1"]
    runtime_session_id: str
    through_sequence: int
    current_user_anchor: str
    projection_window: TranscriptProjectionWindowFact
    messages: tuple[TranscriptMessageFact, ...]
    tool_pairs: tuple[ToolInteractionPairFact, ...]
    compacted_windows: tuple[CompactedWindowReferenceFact, ...]
    stripped_unfinished_call_ids: tuple[str, ...]
    omitted_non_model_block_ids: tuple[str, ...]
    transcript_fingerprint: str
```

### 9.3 Thinking blocks

V1保持当前 provider-neutral structured behavior：`ThinkingBlock` 进入独立
`TranscriptThinkingBlockFact`，lowering 到 `LLMMessage.thinking`，不得转成 natural-language user/system text。
是否由具体adapter发送仍受既有transport contract控制。未来若改变reasoning continuation政策，应升级typed block/
compiler contract version，不能复用text block或静默改变本version行为。

`HintBlock` 等当前不进入model payload的block必须列入 `omitted_non_model_block_ids`，不能因projector分支遗漏而
无审计消失。

### 9.4 malformed tool arguments

Pulsara当前合法支持provider产生invalid JSON或non-object JSON：runtime写配对的error tool result，然后继续下一model
step。normalized transcript必须保留原assistant tool call，不能强制转成object或canonical重写：

- `valid_object`：`parsed_arguments` required，`parse_error_code=None`；
- `invalid_json`：`parsed_arguments=None`，required stable code `invalid_json_syntax`；
- `non_object_json`：`parsed_arguments=None`，required stable code `json_root_not_object`；
- `raw_arguments_json`在所有分支required并逐字保留provider output；
- final lowering始终发送原始arguments string，不使用`json.dumps(parsed_arguments)`替代；
- malformed/non-object call必须存在对应error result pair，pair缺失fail closed；
- exact replay比较raw string bytes，空白/key ordering差异不得被canonicalization掩盖。

### 9.5 order invariant

- messages 按 source sequence 与 block order稳定排列；
- message ID 唯一；
- block ID 在 message 内唯一；
- current-user message 出现一次；
- `current_user` 之前只能是 compaction summary/prior history/recovery note；
- `current_user` 之后为 current-run tail；
- projection window的protected run range必须包含current-user与当前run的全部model-visible events；
- historical retained range与protected run range分别投影、按event ID去重，不得因mid-turn compaction删除RunStart；
- current-run tail 内 assistant tool call 与 result保持 provider-required order；
- `call_sequence <= result_sequence`；
- result ref 必须指向唯一 unit；
- pair ID/tool name 与 unit 一致。

### 9.6 unfinished tool call

既有 replay 会对 failed/aborted historical run strip unfinished tool calls。本阶段保留该长期契约，但要求：

- strip 发生在 transcript projector；
- `stripped_unfinished_call_ids` 必须审计；
- current active run 的 unfinished call 不允许进入 compile；
- waiting-user run 不执行 model compile；
- orphan result 不允许静默 strip。

### 9.7 External result

`ExternalExecutionResultEvent` 仍是 supported external ingress，不是 orphan。normalized projector 必须：

- 校验 result IDs 唯一；
- 从`ExternalToolResultIngressFact` 读取typed timing/semantics，不读metadata timing map；
- 通过durable requirement ref关联此前 `RequireExternalExecutionEvent.external_tool_calls`；
- 验证descriptor attribution、render contract、selected variant与requirement完全一致；
- 生成 `pairing_status="external_completed"`；
- 无对应 call 时 fail closed。

### 9.8 compaction/recovery

- 最新有效 completed compaction boundary生成一个 `compaction_summary` message；
- historical tail只读window的retained history range；current run则始终从protected run range读取；
- summary artifact ID/sequence 保存在 window reference；
- recovery/terminal lifecycle note有独立 segment，不伪装成普通 user message；
- Stage 3 不改变 compaction boundary选择算法。
- preflight/mid-turn共用同一projector，区别只来自`window_kind`与compaction terminal相对RunStart的sequence。

### 9.9 live/replay equality

同一 event slice：

```text
live_transcript_projector(slice)
== replay_transcript_projector(slice)
```

不允许 live projector 使用尚未 durable 的 `state.messages` tail。进入下一 model step 前，所有 model-visible tool results
必须先 full commit；未确认 commit 时 run 进入 reconciliation/repair，不 compile。

---

## 10. `ToolResultRenderUnit`

### 10.0 timing primitive 下沉

当前 `ToolObservationTiming` 定义在 `event/events.py`。`primitives.context`不能反向import event schema，因此C0新增：

```text
src/pulsara_agent/primitives/tool_observation.py
    ToolObservationTimingFact
```

它冻结现有UTC/finite/non-negative/origin/freshness/clock-source invariant。C2让`ToolResultEndEvent`、
`ExternalExecutionResultEvent`、suspension seed与`ToolResultRenderUnit`统一消费该低层fact，并删除event-local class。
`event/__init__.py`若需要短期re-export只允许在C2迁移PR内存在，C5 grep gate不得保留第二类型。

### 10.1 render profile 必须在事实边界产生

renderer 不得通过 tool name前缀或 JSON shape猜 terminal/MCP/generic。新增 durable：

render profile、essential union与External entry facts落在低层
`src/pulsara_agent/primitives/tool_result.py`；event schema和context projector都单向import该模块。

```python
class CapabilityDescriptorRenderAttributionFact(BaseModel):
    owner_runtime_session_id: str
    exposure_id: str
    exposure_fact_fingerprint: str
    descriptor_set_fingerprint: str
    descriptor_id: str
    descriptor_fingerprint: str
    result_render_contract_fingerprint: str
    descriptor_source_event_id: str
    descriptor_source_sequence: int
    descriptor_source_payload_fingerprint: str
    attribution_fingerprint: str

class ToolResultRenderProfileFact(BaseModel):
    profile_version: str
    selected_variant: "CapabilityResultRenderVariantFact"
    tool_origin: Literal[
        "builtin",
        "terminal",
        "mcp",
        "subagent",
        "workflow",
        "custom",
        "unknown",
    ]
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact | None
    render_contract_fingerprint: str
    profile_fingerprint: str

```

profile的生产contract不能靠executor检查`call.name`。同步扩展capability descriptor：

```python
class CapabilityResultRenderVariantFact(BaseModel):
    variant_code: ToolResultRenderVariantCode
    operational_kind: ToolResultOperationalKind
    essential_envelope_kind: ToolResultEssentialEnvelopeKind
    allowed_result_states: tuple[ToolResultStateFact, ...]
    execution_phase: Literal["pre_execution", "executed", "post_execution"]
    terminal_payload_timing_requirement: Literal[
        "required", "optional", "forbidden"
    ]
    variant_fingerprint: str

class ToolResultSemanticsBuilderContractFact(BaseModel):
    schema_version: Literal["tool-result-semantics-builder-contract:v1"]
    builder_id: str
    builder_version: str
    input_schema_fingerprints: tuple[str, ...]
    output_schema_fingerprint: str
    variant_table_fingerprint: str
    classifier_policy_fingerprint: str
    normalization_contract_versions: tuple[str, ...]
    contract_fingerprint: str

class CapabilityResultRenderContractFact(BaseModel):
    allowed_operational_kinds: tuple[ToolResultOperationalKind, ...]
    allowed_essential_envelope_kinds: tuple[ToolResultEssentialEnvelopeKind, ...]
    allowed_variants: tuple[CapabilityResultRenderVariantFact, ...]
    semantics_builder_id: str
    semantics_builder_version: str
    semantics_builder_contract: ToolResultSemanticsBuilderContractFact
    semantics_builder_contract_fingerprint: str
    pre_execution_denial_variant_code: ToolResultRenderVariantCode
    contract_fingerprint: str
```

独立kind tuples必须分别等于`allowed_variants`中kind的sorted unique projection；真正允许的组合以variant tuple为准，
不允许对两个kind集做任意笛卡尔积。`pre_execution_denial_variant_code`必须指向一个allowed error/deny
variant。它进入descriptor event payload/fingerprint。

`ToolResultStateFact` 是低层stable enum，V1只允许`success|error|interrupted|denied`，不包含`running`。
variant validator要求state tuple非空、sorted unique；`pre_execution_denial_variant_code`指向的variant还必须：

```text
execution_phase == pre_execution
allowed_result_states is a non-empty subset of {denied, error}
terminal_payload_timing_requirement == forbidden
```

因此普通success block不能选deny variant，denied block不能选executed-success variant；不用variant code命名约定
推断这些语义。

builder contract 是声明式语义fact，`contract_fingerprint`必须用第5节统一canonical JSON helper从除
`contract_fingerprint`自身之外的上述字段重算；provider/builder不接受一个手写的任意hash。其中：

- input schema fingerprints是恰好六项的ordered tuple，顺序固定为：normalized arguments、runtime typed-result union、
  external domain-submission union、`ToolObservationTimingFact`、`TerminalPayloadTimingFact`、
  `ToolResultEssentialCapturePolicyFact`；不得排序、遗漏或追加未命名schema；
- output schema fingerprint覆盖`ToolResultExecutionSemanticsFact`及essential union schema；
- variant table fingerprint必须等于descriptor `allowed_variants`的canonical ordered aggregate；
- classifier policy fingerprint覆盖variant selection、state/phase/timing/essential branch决策规则；
- normalization contract versions按稳定顺序包含arguments、terminal domain、capture/truncation及profile normalization版本。

builder semantic contract明确不包含Python source hash、wheel hash、Git commit、module/file path、dependency lock hash或
其他build identity。`CapabilityResultRenderContractFact.semantics_builder_id/version`必须等于inner contract，
`semantics_builder_contract_fingerprint == semantics_builder_contract.contract_fingerprint`。任何会改变“同一输入产生何种
semantics”的修改，必须 **同时升级builder version并改变contract fingerprint**；不允许只改其中一个。

V1 descriptor至少冻结：

```text
terminal:
  execute -> terminal_command_executed
          -> terminal_command / terminal_command
          -> states=(success,error,interrupted), phase=executed, terminal_timing=required
  malformed_arguments -> terminal_command_malformed_arguments
          -> terminal_command_error / terminal_command_error
          -> states=(error), phase=pre_execution, terminal_timing=forbidden
  permission_or_policy_deny -> terminal_command_denied
          -> terminal_command_error / terminal_command_error
          -> states=(denied), phase=pre_execution, terminal_timing=forbidden
  adapter_initialization_error -> terminal_command_adapter_error
          -> terminal_command_error / terminal_command_error
          -> states=(error), phase=pre_execution, terminal_timing=forbidden

terminal_process:
  list -> terminal_process_inventory
       -> terminal_process_inventory / terminal_process_inventory
       -> states=(success), phase=executed, terminal_timing=required
  log|poll|wait|kill|write|submit|close_stdin
       -> terminal_process_observation
       -> terminal_process_observation / terminal_process_observation
       -> states=(success,interrupted), phase=executed, terminal_timing=required
  missing_id|unsupported_action|permission_or_policy_deny
       -> terminal_process_error
       -> terminal_process_error / terminal_process_error
       -> states=(error,denied), phase=pre_execution, terminal_timing=forbidden
  adapter/domain error after invocation -> terminal_process_adapter_error
       -> terminal_process_error / terminal_process_error
       -> states=(error), phase=executed, terminal_timing=optional

generic MCP/custom:
  normal -> generic_result -> generic / none
       -> states=(success,error,interrupted), phase=executed, terminal_timing=forbidden
  pre-execution deny -> generic_denied -> generic / none
       -> states=(denied,error), phase=pre_execution, terminal_timing=forbidden
```

支持deferred/external completion的descriptor必须另行注册`execution_phase="post_execution"`的variant；不允许ingress
在原`executed` variant上临时改phase。post-execution variant的state/timing要求由descriptor显式冻结，没有对应variant时
external submission fail closed。

#### 10.1.1 semantics builder binding

ID/version不是callable binding。C0定义process-local port，C2将其接入composition root：

```python
class ToolResultSemanticsRuntimeInput(Protocol):
    semantics_input_kind: str

    def to_frozen_domain_submission(self) -> ToolResultDomainSubmissionFact | None: ...

class ToolResultSemanticsBuilder(Protocol):
    builder_id: str
    builder_version: str

    def build(
        self,
        *,
        descriptor: CapabilityDescriptor,
        selected_variant: CapabilityResultRenderVariantFact,
        normalized_arguments: FrozenJsonObjectFact | None,
        typed_result: ToolResultSemanticsRuntimeInput | None,
        domain_submission: ToolResultDomainSubmissionFact | None,
        observation_timing: ToolObservationTimingFact,
        terminal_payload_timing: TerminalPayloadTimingFact | None,
        essential_capture_policy: ToolResultEssentialCapturePolicyFact | None,
    ) -> ToolResultExecutionSemanticsFact: ...

@dataclass(frozen=True, slots=True)
class ToolResultSemanticsBuilderBinding:
    builder_id: str
    builder_version: str
    builder_contract: ToolResultSemanticsBuilderContractFact
    implementation_build_fingerprint: str | None
    builder: ToolResultSemanticsBuilder

class ToolResultSemanticsBuilderRegistry:
    def register(self, binding: ToolResultSemanticsBuilderBinding) -> None: ...
    def resolve_binding(
        self,
        builder_id: str,
        builder_version: str,
    ) -> ToolResultSemanticsBuilderBinding: ...
    def freeze(self) -> None: ...
```

registry ownership/invariant：

- composition root先注册versioned builtin/generic/MCP semantics builders，然后才允许provider构造descriptor；
- descriptor provider注册/resolve capability时，必须同步调用registry `resolve_binding(id, version)`验证完整binding存在；
- key为exact `(builder_id, builder_version)`；缺失、重复register（即使callable相同）、binding自报identity不符全部
  fail closed；同一key注册不同`builder_contract.contract_fingerprint`是registry configuration conflict；
- binding的`builder_id/version`、`builder_contract.builder_id/version`与callable自报identity必须三者完全一致；registry
  不返回裸callable，避免调用者绕过contract comparison；
- registry freeze后不允许mutation；run-frozen `BoundaryExecutionHandles` / `ToolExecutor`持有该immutable registry
  snapshot，child使用自己execution handles中同一binding；
- descriptor/event不持久化callable、Python module path或build identity，只保存声明式builder contract fact；
- normal execution、pre-execution deny与external ingress全部调用resolved builder；禁止任何路径重新编写
  `if tool_name == ...` classifier；
- normal execution传`typed_result`，external ingress传`domain_submission`；两者互斥，不允许同时非空；
- replay/Inspector验证已durable semantics时不执行callable；但新external submission需要该version builder，
  无法rebind时拒绝接受result。

允许执行新semantics前必须完成下面的三方精确比较：

```text
descriptor builder ID/version/contract fingerprint
    == registry binding ID/version/builder-contract fingerprint
    == External requirement frozen builder ID/version/contract fingerprint
```

normal execution与pre-execution deny只比较前两项；external delayed completion必须比较全部三项。任一不一致均
fail closed，不允许退回generic builder、当前descriptor或当前system policy。`ExternalToolCallRequirementFact`通过其
完整`result_render_contract`冻结第三项，不需要另存一份可能漂移的builder contract副本。

`implementation_build_fingerprint`只用于process-local诊断，可由wheel、Git commit或其他build artifact identity生成；
它不进入descriptor、event、External requirement、manifest、semantic/fact fingerprint，也不参与normal/deny/external
continuation的允许判断。相同semantic contract由不同build产物实现是合法的；Inspector只能把该值标记为“当前进程诊断值”，
不得伪装为历史durable fact。已持久化result的replay完全不需要当前builder binding。

两类fingerprint的规范边界冻结如下；不得再引入第三个含义模糊的`implementation_contract_fingerprint`：

| identity | 真源 | durable | 参与允许判断 | 变化语义 |
|---|---|---:|---:|---|
| `builder_contract.contract_fingerprint` | 声明式`ToolResultSemanticsBuilderContractFact`的canonical JSON | 是 | 是 | 同输入的semantics contract发生变化；必须同步升级builder version |
| `implementation_build_fingerprint` | 当前进程加载的wheel、Git commit或build artifact | 否 | 否 | 仅说明当前进程运行了哪个构建；不代表历史事实，也不改变兼容性 |

descriptor持久化完整声明式contract及其fingerprint；registry binding持有同一contract，再额外持有可空的process-local build
fingerprint。External requirement通过冻结完整`result_render_contract`继承同一builder contract。三方比较只读取
ID/version/contract fingerprint，绝不读取build fingerprint。若同一`(builder_id, builder_version)`出现不同contract
fingerprint，必须在descriptor发布、registry注册或external ingress三者中最早可见的边界fail closed。

发布纪律为：任何会改变相同输入所产生semantics的修改，都必须同时升级`builder_version`并改变声明式
`contract_fingerprint`。fingerprint是检测忘记升级version的机器守卫，不是版本管理的替代品；相同
`(builder_id, builder_version)`观察到不同contract fingerprint必须fail closed。

声明式contract fingerprint不是对Python实现正确性的证明。C0/C2必须从同一contract中的variant table、
classifier policy与normalization versions生成稳定conformance vectors，对每个registered callable执行positive/
negative一致性测试；未通过的build不得进入composition root。这些测试结果和build provenance只属于
release/process-local diagnostics，不进入descriptor、event、requirement或semantic fingerprint。

`semantics_builder_id/version`是versioned request/result classifier：它同时消费normalized invocation arguments、typed execution
result与descriptor contract，产出本次调用的actual profile/essential。不允许另外一个未指纹classifier用tool name或JSON
shape重新分类。process-local execution result增加明确carrier：

```python
class ToolResultExecutionSemanticsFact(BaseModel):
    render_profile: ToolResultRenderProfileFact
    result_state: ToolResultStateFact
    essential_capture_policy: ToolResultEssentialCapturePolicyFact | None
    essential_result: ToolResultEssentialFact | None
    terminal_payload_timing: TerminalPayloadTimingFact | None

class ToolExecutionResult:
    ...
    semantics: ToolResultExecutionSemanticsFact
```

terminal adapters从原typed result对象构造semantics，`ToolExecutor`要求actual profile是descriptor `allowed_variants`之一，再用
provider origin与capture policy验证并完成`ToolResultEndEvent`。generic tools使用required
`generic_tool_result_semantics()` factory，不能省略字段后由executor猜测。

所有known descriptor路径必须实际调用同一个`ToolResultSemanticsBuilderBinding.builder.build()`：normal execution、
exposure/permission/policy deny、workflow runtime result、MCP suspend/resume terminal result和external ingress都不得手工拼
`ToolResultExecutionSemanticsFact`。`descriptor-missing`是唯一允许使用unknown/generic semantics的生产情况；builder missing、
variant mismatch或typed domain submission缺失均fail closed。

pre-execution exposure/permission/policy deny不得绕过该contract。tool-loop在gate前已持有resolved descriptor，deny helper API必须改为：

```python
build_pre_execution_denial_semantics(
    *,
    descriptor: CapabilityDescriptor,
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact,
    requested_arguments: FrozenJsonObjectFact | None,
    message: str,
    result_state: ToolResultStateFact,
    reason_code: str,
    failure_stage: str,
    capture_policy: ToolResultEssentialCapturePolicyFact,
    registry: ToolResultSemanticsBuilderRegistry,
    observation_timing: ToolObservationTimingFact,
) -> ToolResultExecutionSemanticsFact
```

`agent.py` / `tool_loop.py` 的exposure deny、permission deny、unsupported action与missing process ID全部传入原descriptor并使用
`pre_execution_denial_variant_code`。terminal_process的这些分支因此生成typed process-error essential，不会被降成generic。
descriptor-missing fail-closed denial只能生成stable `unknown_denied`、`tool_origin="unknown"`、generic/no-essential并带
stable reason；该variant只属于显式fail-closed fallback contract，`descriptor_attribution=None`，但仍必须携带
system fallback render-contract/variant fingerprints。
任何层都不得按tool name前缀或result JSON选择builder。

`ToolExecutor`的capture policy属于run-frozen execution semantics输入。并发batch创建per-batch executor时必须原样传递
`semantics_registry`与`essential_capture_policy`；deny helper、normal execution与resume不得重新读取default policy。

actual profile validator先重算embedded selected variant fingerprint：

```text
selected_variant.variant_fingerprint
  == hash(
       variant_code,
       operational_kind,
       essential_envelope_kind,
       allowed_result_states,
       execution_phase,
       terminal_payload_timing_requirement,
     )
```

普通production profile required descriptor attribution，且attribution内的descriptor/render-contract fingerprints必须与
profile/source tool call一致。
emitter在append前用run-frozen descriptor验证`render_contract_fingerprint`及selected variant membership；replay使用
`owner_runtime_session_id + descriptor_source_event_id/sequence/payload_fingerprint`定位原exposure/descriptor fact并重做相同验证。
禁止查询current capability registry/current exposure补造该join。

event/semantics validator还必须交叉验证：

- `semantics.result_state` 与`ToolResultEndEvent.state` / external result block state相等；
- result state必须属于`selected_variant.allowed_result_states`；
- timing requirement为`required`时terminal payload timing非空，`forbidden`时必须为空，`optional`时两者均可；
- `execution_phase="pre_execution"` 的command/process deny/error只有universal observation timing，terminal payload timing
  required forbidden；
- essential branch的`execution_started`/error stage必须与selected execution phase一致。

production `ToolResultEndEvent` required 保存该 fact。external path则在committed requirement中冻结descriptor/render contract，
并在`ExternalToolResultIngressFact` 中一次性保存result block/timing/actual semantics/requirement attribution，避免多个
parallel tuples形成新的join漂移。

unknown origin 只允许 typed fail-closed denial / legacy reset migration 明确入口；普通 production result 不能依赖 unknown。

### 10.2 content facts

```python
class ToolResultTextContentFact(BaseModel):
    block_id: str
    text: str
    chars: int
    content_fingerprint: str
    source_events: tuple[ContextEventReferenceFact, ...]

class ToolResultDataContentFact(BaseModel):
    block_id: str
    name: str | None
    media_type: str
    source_kind: str
    inline_data_forbidden: Literal[True]
    artifact_ids: tuple[str, ...]
    source_events: tuple[ContextEventReferenceFact, ...]

class ToolResultContentFact(BaseModel):
    text_blocks: tuple[ToolResultTextContentFact, ...]
    data_blocks: tuple[ToolResultDataContentFact, ...]
    content_fingerprint: str
```

binary/data body 不进入 model input；只产生 typed placeholder/artifact refs。

artifact refs也不能直接复用当前带nested mutable `read_more` dict的message DTO。新增：

```python
class ContextToolResultPreviewFact(BaseModel):
    preview_policy: Literal["full", "head_tail", "head_tail_huge"]
    preview_chars: int
    original_chars: int
    original_bytes: int
    omitted_middle_chars: int
    visible_head_chars: int
    visible_tail_chars: int
    read_more: FrozenJsonObjectFact

class ContextToolResultArtifactRefFact(BaseModel):
    artifact_id: str
    role: str
    media_type: str
    size_bytes: int
    stored_complete: bool
    loss_reason: str | None
    preview: ContextToolResultPreviewFact | None
    ref_fingerprint: str
```

projector从durable message artifact ref复制并freeze；compiler不持有原mutable Pydantic对象。

### 10.3 essential facts

```python
class ToolResultEssentialCapturePolicyFact(BaseModel):
    policy_version: str
    max_error_chars: int
    max_process_summaries: int
    max_process_command_chars: int
    max_process_cwd_chars: int
    policy_fingerprint: str

class TerminalPayloadTimingFact(BaseModel):
    observed_at_utc: str
    duration_seconds: float | None
    freshness: Literal[
        "current_tool_observation",
        "background_process_observation",
        "historical_tool_observation",
    ]
    clock_source: Literal["tool_payload", "tool_runtime_metadata", "mixed"]
    command_started_at_utc: str | None
    process_started_at_utc: str | None
    last_output_at_utc: str | None
    timing_fingerprint: str

class ToolResultErrorPreviewFact(BaseModel):
    text: str
    original_chars: int
    truncated: bool

class TerminalProcessSummaryFact(BaseModel):
    process_id: str
    status: str
    exit_code: int | None
    command: str | None
    cwd: str | None
    terminal_session_id: str
    backend_type: str
    io_mode: str | None
    timed_out: bool
    stdin_closed: bool | None
    duration_seconds: float | None
    summary_fingerprint: str

class TerminalCommandEssentialFact(BaseModel):
    kind: Literal["terminal_command"]
    capture_policy_fingerprint: str
    action: Literal["execute"]
    execution_started: Literal[True]
    command: str
    status: str
    exit_code: int | None
    cwd: str
    timed_out: bool
    output_truncated: bool
    error: ToolResultErrorPreviewFact | None
    process_id: str | None
    yielded_to_background: bool
    terminal_session_id: str
    backend_type: str
    io_mode: str | None
    stdin_closed: bool | None
    policy_code: str | None
    suggested_args: FrozenJsonObjectFact | None

class TerminalCommandErrorEssentialFact(BaseModel):
    kind: Literal["terminal_command_error"]
    capture_policy_fingerprint: str
    requested_action: Literal["execute"]
    requested_command: str | None
    failure_stage: Literal[
        "malformed_arguments",
        "exposure_gate",
        "permission_gate",
        "policy_gate",
        "adapter_initialization",
    ]
    status: Literal["denied", "error"]
    execution_started: Literal[False]
    error: ToolResultErrorPreviewFact
    policy_code: str | None
    suggested_args: FrozenJsonObjectFact | None
    observed_cwd: str | None
    terminal_session_id: str | None
    backend_type: str | None
    io_mode: str | None

class TerminalProcessObservationEssentialFact(BaseModel):
    kind: Literal["terminal_process_observation"]
    capture_policy_fingerprint: str
    action: Literal[
        "log", "poll", "wait", "kill", "write", "submit", "close_stdin"
    ]
    process_id: str
    status: str
    exit_code: int | None
    command: str | None
    cwd: str | None
    timed_out: bool
    output_truncated: bool
    error: ToolResultErrorPreviewFact | None
    yielded_to_background: bool
    terminal_session_id: str
    backend_type: str
    io_mode: str | None
    stdin_closed: bool | None
    policy_code: str | None
    duration_seconds: float | None

class TerminalProcessInventoryEssentialFact(BaseModel):
    kind: Literal["terminal_process_inventory"]
    capture_policy_fingerprint: str
    action: Literal["list"]
    status: str
    live_process_count: int
    finished_process_count: int
    process_summaries: tuple[TerminalProcessSummaryFact, ...]
    omitted_process_count: int
    summaries_truncated: bool

class TerminalProcessErrorEssentialFact(BaseModel):
    kind: Literal["terminal_process_error"]
    capture_policy_fingerprint: str
    requested_action: str
    process_id: str | None
    status: str
    error: ToolResultErrorPreviewFact
    policy_code: str | None
    terminal_session_id: str | None
    backend_type: str | None

class ArtifactEssentialResultFact(BaseModel):
    kind: Literal["artifact"]
    capture_policy_fingerprint: str
    primary_artifact_id: str | None
    output_truncated: bool
    output_preview_available: bool

ToolResultEssentialFact = (
    TerminalCommandEssentialFact
    | TerminalCommandErrorEssentialFact
    | TerminalProcessObservationEssentialFact
    | TerminalProcessInventoryEssentialFact
    | TerminalProcessErrorEssentialFact
    | ArtifactEssentialResultFact
)
```

essential fact在execution/ingress boundary由typed result与tool descriptor构造，不从tool-result JSON反向解析。
execution boundary只消费versioned、system-wide `ToolResultEssentialCapturePolicyFact`，不依赖未来某次compile的render policy。
capture caps必须不小于所有supported V1 render envelope caps。error preview/process summaries在事实生成时做deterministic
bounded capture并保存original/truncated/omitted counts；compiler再按`ToolResultEnvelopeRenderPolicyFact`进一步降级。
duration必须finite/non-negative，terminal timing timestamps必须UTC，counts非负，inventory不要求单一command/cwd。
observation branch的`process_id` required non-empty；missing/invalid process ID只能使用`TerminalProcessErrorEssentialFact`，
不允许以`observation(process_id=None)`表达。terminal command的process ID只在yielded/background branch required。
`TerminalCommandEssentialFact`只允许`execution_started=True`后的typed result；pre-execution deny、malformed arguments或
adapter initialization failure必须使用`TerminalCommandErrorEssentialFact`。error fact不允许伪造cwd/session/backend；这些
执行身份只在当时确实已observation到时填写，否则为`None`。
`failure_stage="malformed_arguments"`时requested command可空；exposure/permission/policy gate时requested command required；
`execution_started=False`时exit code、duration、process ID不存在于该DTO，禁止以零或伪ID表达“未执行”。

durable carrier required：

```python
class ToolResultEndEvent(EventBase):
    render_profile: ToolResultRenderProfileFact
    essential_capture_policy: ToolResultEssentialCapturePolicyFact | None
    essential_result: ToolResultEssentialFact | None
    terminal_payload_timing: TerminalPayloadTimingFact | None

class FrozenToolResultBlockFact(BaseModel):
    tool_call_id: str
    model_tool_name: str
    result_state: ToolResultStateFact
    canonical_block_payload: FrozenJsonObjectFact
    block_payload_fingerprint: str

class ExternalExecutionRequirementReferenceFact(BaseModel):
    owner_runtime_session_id: str
    require_event_id: str
    require_event_sequence: int
    require_event_payload_fingerprint: str
    tool_call_id: str
    requirement_fingerprint: str

class ExternalToolCallRequirementFact(BaseModel):
    tool_call_id: str
    model_tool_name: str
    raw_arguments_json: str
    tool_origin: Literal["builtin", "terminal", "mcp", "subagent", "workflow", "custom"]
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact
    result_render_contract: CapabilityResultRenderContractFact
    essential_capture_policy: ToolResultEssentialCapturePolicyFact | None
    requirement_fingerprint: str

ToolResultDomainSubmissionFact = (
    TerminalCommandDomainSubmissionFact
    | TerminalCommandErrorDomainSubmissionFact
    | TerminalProcessObservationDomainSubmissionFact
    | TerminalProcessInventoryDomainSubmissionFact
    | TerminalProcessErrorDomainSubmissionFact
    | ArtifactDomainSubmissionFact
)

class ExternalToolResultSubmissionFact(BaseModel):
    result_block: FrozenToolResultBlockFact
    observation_timing: ToolObservationTimingFact
    selected_variant_code: ToolResultRenderVariantCode
    domain_result: ToolResultDomainSubmissionFact | None
    terminal_payload_timing: TerminalPayloadTimingFact | None
    submission_fingerprint: str

class ExternalToolResultIngressFact(BaseModel):
    requirement_ref: ExternalExecutionRequirementReferenceFact
    result_block: FrozenToolResultBlockFact
    observation_timing: ToolObservationTimingFact
    execution_semantics: ToolResultExecutionSemanticsFact
    ingress_fingerprint: str

class RequireExternalExecutionEvent(EventBase):
    external_tool_calls: tuple[ExternalToolCallRequirementFact, ...]

class ExternalExecutionResultEvent(EventBase):
    external_results: tuple[ExternalToolResultIngressFact, ...]
```

`FrozenToolResultBlockFact.canonical_block_payload`是message-schema `ToolResultBlock`的strict canonical payload，不是tool output文本
中的JSON。validator必须thaw为owned `ToolResultBlock`并验证ID/name/state与外层一致；禁止从其text output解析
profile/essential。

`*DomainSubmissionFact` 是C0中显式定义的frozen discriminated models；它们与对应`*EssentialFact`拥有相同的
domain fields/validators，但故意删除`capture_policy_fingerprint`、descriptor/exposure attribution、contract/variant
fingerprints等Pulsara authority字段。generic/no-essential variant required `domain_result=None`。normal execution builder使用
该次run/execution boundary已经解析并冻结的capture policy；external delayed-completion builder只能使用original committed
requirement中的`essential_capture_policy`，不得读取当前system policy。两者都将domain submission deterministic转成
`ToolResultEssentialFact`。不允许用一个`dict[str, Any]` 代替该union。

External result IDs必须精确对应同一committed `RequireExternalExecutionEvent.external_tool_calls`，并按tool call ID
排序唯一。profile selected variant的
`essential_envelope_kind="none"` iff essential为空；terminal command/command-error/process/inventory/error profile必须匹配
对应union branch。
`ToolResultExecutionSemanticsFact`/event output中的capture policy与essential必须同时为空或同时非空，inner fingerprint必须
等于outer policy。这个output invariant不等于requirement policy可空：essential-capable external contract必须预先冻结policy，
即使最终选择的具体variant不生成essential，builder也只是在本次output中将policy/essential同时置空；
`ToolResultRenderUnit.essential`只从这些durable fields复制。缺失是unsupported schema，不允许replay解析body JSON补造。
若profile带descriptor attribution，其embedded `selected_variant`还必须按full fact/fingerprint精确命中该descriptor
render contract的一个`allowed_variants`；独立kind set、同pair的另一variant code/state/phase/timing policy或同一descriptor
下的其他合法variant都不能代替该精确组合。

`RequireExternalExecutionEvent` 本身只能由run-frozen tool call + descriptor exposure构造；每个requirement的
tool-call ID/name/raw arguments、descriptor attribution、full render contract和capture policy必须与该run exposure/execution
boundary完全一致，IDs唯一且tuple按tool-call order排列。requirement event full commit之前不得对外发布执行请求。

`ExternalToolCallRequirementFact.essential_capture_policy`是delayed completion的唯一capture-policy authority，并进入
`requirement_fingerprint`。V1把该requirement可选择的variant精确定义为其full render contract中
`execution_phase="post_execution"`的variants；该集合必须非空，submission不得选择其他phase。validator冻结：

- 可选择的post-execution variants全部`essential_envelope_kind="none"`时，policy必须为`None`；
- 任一可选择的post-execution variant可能生成essential envelope时，policy required；即使调用方当前预计选择
  no-essential variant，也不得
  省略未来合法result variant所需的capture contract；
- policy schema/version/fingerprint必须由同一canonical helper验证，并与builder contract声明的capture/normalization schema
  兼容；
- requirement创建后，runtime default policy升级、配置reload或进程重启都不能改变该policy；
- 当前进程无法支持requirement冻结的builder contract或capture-policy version时，external result ingress fail closed，不能
  用当前policy重算、静默升级或解析result body补造essential。

上述规则必须实现成requirement构造期的单一validator，而不是散落在Host、adapter和builder中的约定：

```text
post_execution_variants = result_render_contract.allowed_variants
    filtered by execution_phase == "post_execution"

post_execution_variants == empty
    -> reject requirement

all variant.essential_envelope_kind == "none"
    -> essential_capture_policy must be None

any variant.essential_envelope_kind != "none"
    -> essential_capture_policy is required
    -> policy schema/version/fingerprint must match builder contract
```

`requirement_fingerprint`必须覆盖完整`result_render_contract`和`essential_capture_policy`的canonical payload；同一tool call只要
冻结policy不同，requirement fingerprint就必须不同。external ingress调用builder时只允许把
`requirement.essential_capture_policy`传给`essential_capture_policy`参数；禁止传入runtime default、session current policy、
submission附带值或根据result body推导的替代policy。若冻结policy版本不能rebind，必须在调用builder前fail closed。

External ingress producer唯一落点为Host/runtime-owned builder，不是event constructor的caller自由拼接：

```python
class ExternalToolResultIngressBuilder:
    def bind_submission(
        self,
        *,
        requirement_event: RequireExternalExecutionEvent,
        requirement: ExternalToolCallRequirementFact,
        submission: ExternalToolResultSubmissionFact,
    ) -> ExternalToolResultIngressFact: ...
```

builder只接受已full-committed requirement event的owned canonical copy，并执行：

1. 以requirement event ID/sequence/payload fingerprint构造durable reference；
2. 验证result ID/name与requirement tool call一致；
3. 以submission的selected variant code从requirement contract取得唯一full variant，验证result state/phase/timing requirement；
4. 通过semantics builder registry的`resolve_binding()`取得完整binding，精确比较requirement中冻结的builder
   ID/version/contract fingerprint，再由requirement注入descriptor attribution、contract/variant fingerprints与
   `essential_capture_policy`；
5. 验证timing embedded tool-call ID、essential branch和terminal timing；
6. 生成canonical `ToolResultExecutionSemanticsFact`/ingress fingerprint，然后才允许构造
   `ExternalExecutionResultEvent`。

external executor/API只提交result block、universal timing、selected variant code、typed domain result与optional terminal
payload timing；它不回传descriptor/exposure、contract/variant fingerprint或capture-policy authority。builder以committed
requirement为唯一真源注入这些字段；不查current exposure，不解析result payload推断semantics。
required builder ID/version在requirement的render contract中冻结；进程无法rebind该version时拒绝ingress，不退回generic。
同ID/version但contract fingerprint不同同样拒绝；`implementation_build_fingerprint`不同不影响允许判断。

上述fail-closed使用稳定`ExternalToolResultIngressReasonCode`，V1至少包含：

```text
requirement_not_committed
requirement_identity_mismatch
external_variant_not_allowed
builder_binding_missing
builder_identity_mismatch
builder_contract_mismatch
capture_policy_required
capture_policy_must_be_none
capture_policy_unsupported
domain_submission_mismatch
timing_contract_mismatch
```

不得把这些原因降格为自由字符串adapter error。

### 10.4 normalized unit

```python
class ToolResultRenderUnit(BaseModel):
    schema_version: Literal["tool-result-unit:v1"]
    unit_id: str
    tool_call_id: str
    model_tool_name: str
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact | None
    render_contract_fingerprint: str
    render_variant_fingerprint: str
    call_message_id: str
    result_message_id: str
    call_position: int
    result_position: int
    result_state: ToolResultStateFact
    content: ToolResultContentFact
    artifacts: tuple[ContextToolResultArtifactRefFact, ...]
    observation_timing: ToolObservationTimingFact
    terminal_payload_timing: TerminalPayloadTimingFact | None
    render_profile: ToolResultRenderProfileFact
    essential_capture_policy: ToolResultEssentialCapturePolicyFact | None
    essential: ToolResultEssentialFact | None
    source_sequence_start: int
    source_sequence_end: int
    source_event_ids: tuple[str, ...]
    unit_fingerprint: str
```

### 10.5 unit validator

- `tool_call_id` 与 timing embedded ID一致；
- unit/pair/result block tool name一致；
- model tool name必须来自frozen exposure/descriptor fact；renderer不再调用name normalizer或截断；
- unit attribution/contract/variant fingerprints与profile完全一致，并可通过source event ref重建原descriptor；
- `descriptor_attribution=None`只允许explicit unknown-denial/reset-migration branch，普通production/external result required non-null；
- call/result positions非负且 call < result；
- sequence range覆盖所有 source events；
- artifact IDs唯一；
- render profile required essential时 essential非空；
- capture policy/essential同时为空或非空，fingerprint精确匹配；
- terminal profile只能携带 terminal essential；
- terminal payload timing的required/optional/forbidden完全由selected variant决定；pre-execution terminal deny/error必须为空；
- non-terminal profile的selected variant timing requirement必须为`forbidden`，terminal payload timing必须为空；
- generic profile不能偷偷携带 terminal essential；
- duration finite/non-negative，timestamp UTC；
- content chars/hash自洽；
- unit ID唯一，fingerprint自洽。
- finalized unit不得为 `running`；background process是否仍运行由typed essential payload表达，tool result event本身仍是
  completed observation。

### 10.6 renderer API

```python
class ToolResultRenderDiagnosticFact(BaseModel):
    code: ToolResultRenderDiagnosticCode
    severity: Literal["info", "warning", "error"]
    attributes: tuple[tuple[str, FrozenJsonValue], ...]

class ToolResultRenderDecisionFact(BaseModel):
    unit_id: str
    tool_call_id: str
    source_message_id: str
    source_assistant_message_id: str | None
    segment: Literal[
        "prior_history", "current_user", "current_run_tail", "legacy_history"
    ]
    render_order: int
    state: str
    render_source_fingerprint: str
    artifact_fingerprint: str
    original_chars: int
    body_candidate_chars: int | None
    body_candidate_source: ToolResultBodyCandidateSource
    minimum_envelope_kind: ToolResultMinimumEnvelopeKind
    latest_reserved_candidate: bool
    latest_reserved_applied: bool
    latest_reserved_reason: ToolResultLatestReserveReasonCode
    visible_body_chars: int
    rendered_tool_observation: ToolObservationTimingFact | None
    observation_timing_policy: Literal["full", "minimal", "omitted", "not_applicable"]
    rendered_terminal_payload_timing: TerminalPayloadTimingFact | None
    terminal_payload_timing_policy: Literal["full", "minimal", "omitted", "not_applicable"]
    rendered_header_chars: int
    rendered_envelope_chars: int
    rendered_total_chars: int
    framing: Literal["pulsara_tool_result_header", "pulsara_tool_result_envelope"]
    payload_preserved: bool
    payload_format: ToolResultPayloadFormat
    body_budget_remaining: int
    message_body_budget_remaining: int
    envelope_budget_remaining: int
    primary_artifact_id: str | None
    artifact_ids: tuple[str, ...]
    body_policy: ToolResultBodyPolicy
    envelope_policy: ToolResultEnvelopePolicy
    reason_code: ToolResultRenderReasonCode
    clipped_envelope_fields: tuple[str, ...]
    read_more: FrozenJsonObjectFact | None
    diagnostics: tuple[ToolResultRenderDiagnosticFact, ...]
    decision_fingerprint: str

class ToolResultRenderOperationalFact(BaseModel):
    unit_id: str
    cache_status: Literal["hit", "miss", "invalidated", "not_configured"]
    cache_key: str | None
    diagnostics: tuple[ToolResultRenderDiagnosticFact, ...]

class ToolResultRenderCacheHint(BaseModel):
    unit_id: str
    cache_key: str
    rendered_text: str
    rendered_text_fingerprint: str
    decision: ToolResultRenderDecisionFact
    hint_fingerprint: str

@dataclass(frozen=True, slots=True)
class PreparedToolResultRenderInput:
    units: tuple[ToolResultRenderUnit, ...]
    resolved_policy: ResolvedToolResultRenderPolicyFact
    cache_hints: tuple[ToolResultRenderCacheHint, ...]
    render_input_fingerprint: str
    cache_hints_fingerprint: str

def prepare_tool_result_render_input(
    *,
    units: tuple[ToolResultRenderUnit, ...],
    transcript: TranscriptCompileInput,
    policy_basis: ToolResultRenderPolicyBasisFact,
    cache: ToolResultRenderDecisionCachePort | None,
) -> PreparedToolResultRenderInput: ...

def render_tool_result_units(
    *,
    prepared: PreparedToolResultRenderInput,
    transcript: TranscriptCompileInput,
    estimator: TokenEstimator,
) -> ToolResultRenderOutput:
    ...
```

universal observation timing属于result identity，不是只有“完整body”才享有的装饰。generic body因预算被clip时，只要timed
header仍能装入essential envelope，就必须保留`observed_at`/duration/freshness/origin；若必须切换compact envelope，则写入
`pulsara_tool_observation`结构。不得因为`payload_preserved=False`无条件退回不含timing的basic header。

renderer内部的payload/envelope builder必须返回typed inclusion结果，例如
`observation_included: bool`与`terminal_payload_timing_included: bool`。header选择与decision fact只消费这些flags；禁止扫描
最终字符串中的`observed_at=`、`pulsara_tool_observation`或`timing`字样来反推是否已经渲染。工具正文即使包含同名伪造文本，
也不能抑制runtime生成的真实universal timing。

多artifact的primary selection只运行一次：优先有preview的text/JSON/XML/YAML artifact，其次同类无preview artifact；binary/image
永远不能成为`primary_artifact_id`。同一selected primary必须同时驱动canonical decision、compact/minimal model payload与
fallback read-more locator。compact payload把primary排在第一位，非primary refs删除`read_more`；minimal只保留primary。
没有text-like primary时允许展示bounded binary attribution，但decision/fallback的`primary_artifact_id`保持`None`。

output：

```python
@dataclass(frozen=True, slots=True)
class RenderedToolResultFragment:
    unit_id: str
    tool_call_id: str
    source_message_id: str
    source_message_index: int
    content_block_index: int
    segment: str
    text: str
    rendered_text_fingerprint: str

@dataclass(frozen=True, slots=True)
class PreparedToolResultRenderOutput:
    fragments: tuple[RenderedToolResultFragment, ...]
    canonical_decisions: tuple[ToolResultRenderDecisionFact, ...]
    operational_facts: tuple[ToolResultRenderOperationalFact, ...]
    tool_result_render_decisions: tuple[dict[str, object], ...]
    tool_result_budget_report: dict[str, object]
    cache_write_candidates: tuple[ToolResultRenderCacheWriteCandidate, ...]
```

renderer 不再接受 `list[Msg]` 或 `LoopBudget`，也不得返回 `LLMMessage`、assistant tool-call message或完整
provider-neutral message sequence。fragment只表达单个normalized result unit在既定policy下的可见body/envelope；compiler按
`TranscriptCompileInput.messages[].blocks`的原始顺序把assistant tool call、对应result fragment及普通文本统一lower。

上述 `ToolResultBodyCandidateSource` / `ToolResultMinimumEnvelopeKind` / `ToolResultLatestReserveReasonCode` /
`ToolResultPayloadFormat` / `ToolResultBodyPolicy` / `ToolResultEnvelopePolicy` / `ToolResultRenderReasonCode` 与
diagnostic code 都是 `primitives/tool_result.py` 中的稳定 `StrEnum`。它们的取值集必须覆盖当前 renderer 已有
decision/reason 分支；不允许以自由字符串、`unknown` fallback 或 arbitrary metadata 代替。新增取值要升级对应
contract version 并同步 serializer/replay/Inspector。

cache port只在`prepare_tool_result_render_input()`中读取并立即转换成immutable hints；pure renderer/compiler不持有或调用
mutable cache。resolved policy与unit order进入`render_input_fingerprint`；cache hints不进入semantic/input aggregate
fingerprint，也不写manifest，因为它们不能改变canonical output。

canonical decision不得包含cache hit/miss；否则同input的decision会因process cache状态变化。cache status单独进入
`ToolResultRenderOperationalFact`，只用于本次diagnostics/metrics，不参与provider payload、decision fingerprint或manifest。

### 10.7 cache contract

cache key 必须覆盖所有会改变单unit fragment的输入：

```text
unit_fingerprint
transcript segment
tool_result_policy_basis_fingerprint
```

`unit_fingerprint`已经覆盖actual render profile/essential/timing/artifact facts；renderer仍必须fresh计算aggregate allocation和
canonical decision，再与hint逐字段比较，因此cache key不把一次compile的aggregate order伪装成per-unit identity。

cache value只保存 canonical rendered fragment + decision fingerprint。cache owner属于 process-local
`RuntimeSession.tool_result_render_cache`，是bounded LRU，不得放在 `LoopState.scratchpad`。

hint使用前必须验证unit/policy/estimator/compiler key、rendered hash、decision caps与unit ID；任一不符按cache miss重算并
记录operational diagnostic。无hint exact replay必须得到相同rendered text/decision。cache hit/miss信息不进入
provider-visible payload或input manifest semantic fingerprint。

只有matching `ContextCompiledEvent(status="compiled")`取得durable FULL acknowledgement后才commit cache candidate；
`EventPublicationAfterCommitError`必须从`committed_events`识别该FULL commit后再提交。pre-commit失败、pressure、failed compile、
UNKNOWN/PARTIAL confirmation均不得写cache。cache写入是第二阶段process-local optimization，失败不回滚durable compile fact；
不得缓存低保真结果作为宽预算 canonical output。V1 admission predicate固定为四项同时成立：

```text
body_policy == full_visible
envelope_policy == full_envelope
reason_code == within_budget
payload_preserved == true
```

`clipped`、`artifact_preview`、`omitted_non_artifact`、`omitted_artifact`、`compact_envelope`、`minimal`与
`budget_exhausted`全部不得生成write candidate。cache read/write异常只产生bounded process-local operational diagnostic；
不得改变canonical decision、provider payload或阻止已经durable FULL的compile继续发起model call。

---

## 11. 最小 `ContextSectionCandidate`

### 11.1 目的

Stage 3 只要求 compiler 不再接收裸字符串。本阶段仍允许一个 composition-root collector 调用现有 subsystem
projection function，但 collector 输出必须是 typed candidate。

### 11.2 DTO

```python
class ContextSourceTimingFact(BaseModel):
    observed_at_utc: str | None
    source_started_at_utc: str | None
    source_ended_at_utc: str | None
    source_sequence_start: int | None
    source_sequence_end: int | None
    freshness: Literal[
        "current_turn",
        "current_run_tail",
        "historical_replay",
        "compacted_history",
        "memory_projection",
        "current_tool_observation",
        "cached_snapshot",
        "background_process_observation",
        "subagent_result",
        "unknown",
    ]
    clock_source: Literal[
        "event_created_at",
        "message_created_at",
        "tool_observation_fact",
        "host_clock",
        "mixed",
    ]
    timing_fingerprint: str

class ContextInlineTextFact(BaseModel):
    text: str
    chars: int
    content_fingerprint: str

class ContextArtifactTextFact(BaseModel):
    artifact_id: str
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    content_fingerprint: str
    expected_chars: int

ContextSectionPayloadFact = ContextInlineTextFact | ContextArtifactTextFact

class ContextCandidateAuthorityFact(BaseModel):
    source_instance_id: str
    source_kind: ContextCandidateSourceKind
    source_fact_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_ids: tuple[str, ...]
    channel: ContextChannelFact
    priority: int
    required: bool
    stability: Literal["stable", "run", "step", "ephemeral"]
    lowering_kind: ContextCandidateLoweringKind
    lifecycle_dependency_fingerprint: str | None
    model_visible_text: str
    model_visible_content_fingerprint: str
    model_visible_chars: int
    source_timing: ContextSourceTimingFact
    authority_fingerprint: str

class ContextCandidateSourceSelectionFact(BaseModel):
    source_instance_id: Literal["subagent:results"]
    eligible_source_count: int
    selected_source_ids: tuple[str, ...]
    omitted_source_count: int
    reason_code: Literal[
        "no_eligible_sources",
        "selected_all",
        "policy_limit",
    ]
    policy_fingerprint: str
    source_from_sequence: int
    source_through_sequence: int
    selection_fingerprint: str

class ContextSectionCandidate(BaseModel):
    schema_version: Literal["context-candidate:v1"]
    candidate_id: str
    source_kind: Literal[
        "system",
        "runtime_context",
        "memory_projection",
        "capability_catalog",
        "capability_active_skill",
        "plan",
        "recovery",
        "subagent_results",
    ]
    source_instance_id: str
    source_fact_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_ids: tuple[str, ...]
    channel: ContextChannelFact
    priority: int
    required: bool
    stability: Literal["stable", "run", "step", "ephemeral"]
    lifecycle_dependency_fingerprint: str | None
    lowering_kind: Literal[
        "system_instruction",
        "leading_user_context",
        "handoff_hint",
    ]
    payload: ContextSectionPayloadFact
    source_timing: ContextSourceTimingFact
    semantic_fingerprint: str
    candidate_fingerprint: str
```

timing invariant：所有timestamp均为UTC ISO；start/end成对时start<=end；sequence start/end成对出现且start<=end；
event-attributed candidate required sequence range；`clock_source="host_clock"`只能用于本次显式model-step observation，
不能冒充historical source time；age始终由compiler使用snapshot `compiled_at_utc`派生，不写回source fact。

`ContextChannelFact` 在 C0 定义为固定 enum：

```text
system
leading_user
handoff_hint
```

它不包含 history/current-user/current-run-tail；这些只能来自 `TranscriptCompileInput`。

`ContextFactSnapshotFact.candidate_authorities` required保存本次compile允许出现的完整source set。authority在snapshot
freeze时由静态指令、committed projection/exposure/plan/subagent facts与统一high-water生成；它不是candidate collector稍后
自行声明的metadata。candidate materialization、lifecycle prepare与compiler allocation三个边界都调用同一个
`validate_candidate_against_snapshot()`。

authority是model-visible正文、source timing与归因的唯一真源，但不拥有producer selection。Selection由独立的
`ContextCandidateSourceSelectionFact`冻结，即使最终没有model-visible candidate也必须进入snapshot/manifest。
Stage-3 collector的production API只接收snapshot authority/selection与cache port；
不得再接收一份并行source字符串。production invocation preparation使用不含路线阶段号的
`ContextCandidateCollectionInput`收集尚未迁移的producer输入；collector只能把snapshot authority materialize为candidate并执行collection
policy。candidate的`payload`、`source_timing`必须逐字段等于authority；collection decision的selected/omitted必须来自
snapshot selection fact。即使caller重算全部candidate
fingerprint，也不能改变freshness或时间header。

V1 required保存`subagent:results` selection fact：无pending时为`no_eligible_sources`；cap=0且有N个pending时保存
`eligible=N, selected=(), omitted=N, reason=policy_limit`。selected为空时不得制造空正文authority/projection；collector仍必须
生成omitted-only `ContextCandidateCollectionDecisionFact`，使manifest区分“没有pending”和“全部被policy省略”。

Subagent selection不得由Agent先后读取live graph state再绑定较晚EventLog high-water。唯一允许算法为：先冻结canonical parent
event slice，再在该slice上运行pure subagent reducer，一次性得到ordered eligible IDs，随后按frozen policy切分selected/omitted。
`source_from/source_through`必须等于实际reducer slice range。V1在没有durable graph checkpoint时从sequence 1读取；未来若缩窄，
必须新增可验证checkpoint/range contract，不能只改性能实现。

authority producer按source冻结：memory必须先找到当前run最新`ProjectionRequestedEvent`，再以
`projection_id + role + scope`匹配该request之后唯一terminal；terminal为`ProjectionReadyEvent`时才从
`summary + projection_kind`纯重建正文，terminal为`ProjectionFailedEvent`时不生成memory candidate，terminal缺失或不唯一
则fail closed，绝不回退到较旧ready。subagent
正文从有序`SubagentRunCompletedEvent` facts纯重建；两者timing均来自对应frozen event wrapper的真实created_at/sequence。
plan revision正文必须从当前run最新`PlanExitResolvedEvent(decision="revise")`重建并引用该event；禁止读取
`LoopState.scratchpad["plan_revision_feedback"]`。runtime context正文与timing来自环境/compile timing facts，
system/capability正文必须匹配各自durable artifact/hash。禁止把当前
user observation time写入历史memory/subagent source，同时声称`clock_source="event_created_at"`。

### 11.3 内容读取

如果 payload 是 artifact ref，artifact body必须在进入 pure compiler 前通过 `ContextCandidateMaterializer` 读取并
校验 hash，产出新的 immutable inline materialized candidate。compiler 本身不执行 I/O。

因此公开API的类型名保持 `ContextSectionCandidate`，但compiler入口validator只接受
`ContextInlineTextFact` payload；artifact variant只存在于collector/materialization阶段。input aggregate fingerprint与
manifest都基于最终materialized inline candidate，确保exact replay比较的是实际compiler bytes。该重复内容受本章
candidate/manifest aggregate safety cap约束，不得无界增长。

### 11.4 candidate invariant

- candidate ID 在一次 compile input 内唯一；
- required candidate不能被 lifecycle policy静默省略；
- inline chars/hash自洽；
- artifact ref必须有 expected hash/size；
- source refs顺序稳定且不超过 source high-water；
- stable/run candidate required dependency fingerprint；
- ephemeral可无 dependency fingerprint；
- `source_kind` 与 lowering kind使用固定允许矩阵；
- arbitrary metadata/provenance dict不得进入 input DTO。
- source event refs为空时，必须有至少一个source artifact或snapshot内的versioned source fact；system/config text不能
  只靠当前进程代码默认为replay truth。

candidate还必须通过snapshot authorization join：

```text
system
  -> matching ContextStaticInstructionFact artifact/hash/version
runtime_context
  -> matching ContextRuntimeEnvironmentFact fingerprint
memory_projection
  -> latest ProjectionRequested + unique matching Ready terminal；latest Failed means absent
capability_catalog / capability_active_skill
  -> matching CapabilityExposureSnapshotFact projection artifact/fingerprint
plan
  -> workflow匹配ContextPlanSnapshotFact；revision匹配最新durable revise event
recovery
  -> matching recovery terminal/decision event refs
subagent_results
  -> matching parent graph result IDs/sequences/artifact refs
```

candidate引用snapshot未授权source、较新于high-water的event、不同exposure artifact或未交付subagent result时，
`context_candidate_source_join_mismatch` fail closed。compiler不接受“caller已经验证过”的隐含约定。

join是逐字段精确比较，不只是“存在某种attribution”：

```text
source_instance_id / source_kind
event refs / artifact ids
channel / priority / required / stability / lowering_kind
lifecycle dependency fingerprint
materialized content fingerprint / chars
```

固定channel/lowering矩阵为：

| source kind | channel | lowering kind |
|---|---|---|
| system | system | system_instruction |
| runtime_context | leading_user | leading_user_context |
| memory_projection | leading_user | leading_user_context |
| capability_catalog | leading_user | leading_user_context |
| capability_active_skill | system | system_instruction |
| plan | leading_user | leading_user_context |
| recovery | handoff_hint | handoff_hint |
| subagent_results | leading_user | leading_user_context |

DTO validator与compiler lowering都必须消费该矩阵；不得只把`lowering_kind`持久化却继续按source name/channel硬编码。
system正文必须匹配`ContextStaticInstructionFact`的artifact/content hash；capability projection必须匹配frozen exposure的
rendered prompt artifact/fingerprint；event refs必须命中primary或named authority range中相同runtime owner的sequence。

### 11.5 Stage 3 collector

新增临时、显式命名：

```python
class LegacySubsystemContextCandidateCollector:
    """Stage-3 composition adapter; must be deleted in Stage 5."""
```

它可以调用当前 runtime context、memory projection、capability projection、recovery、subagent result renderer，
但必须：

- 从 snapshot/event slice授权 facts读取；
- 不读取 `LoopState`；
- 不执行 recall/governance/tool/network；
- 不返回 provider-native `LLMMessage`；
- 不自行分配最终 token budget；
- memory projection event/baseline budget必须等于`policy.projection_token_budget`；
- pending subagent selection最多`policy.max_subagent_results_per_parent_compile`，并保存selected/omitted counts；
- `max_subagent_results_per_parent_compile <= 0`时必须返回空选择；`ContextFactSnapshotFact` validator再次要求
  `len(selected_result_ids) <= frozen policy`，不得只依赖collector；
- selection fact与authority分离：selected非空才生成subagent projection/authority；无正文时仍保存selection和collection decision；
- 为每段文本生成 source refs/hash/timing。

Stage 5 将逐个 producer替换该 collector并建立正式 registry。本阶段不得把它命名成最终 `ContextSourceRegistry`。
Stage 5复用本章的candidate ingress，不得再创建第二个同名不兼容DTO；若正式source ownership需要新增required字段，
必须升级schema version并在同PR迁移compiler/manifest/event，而不是让两个candidate shape并存。

---

## 12. Lifecycle cache

### 12.1 当前问题

当前 `ContextLifecycleCoordinator` 的 key读取 `ContextCompileRequest`，缓存浅 frozen `ContextSection`，并用
mutable dict deepcopy。hard cut 后它不能继续成为 compiler 对 mutable request 的旁路。

### 12.2 新 contract

```python
class ContextCandidateLifecycleKeyFact(BaseModel):
    source_instance_id: str
    candidate_id: str
    stability: str
    scope_id: str
    dependency_fingerprint: str
    policy_version: str

class ContextLifecycleCachePort(Protocol):
    def get(self, key: ContextCandidateLifecycleKeyFact) -> ContextSectionCandidate | None: ...
    def put(self, key: ContextCandidateLifecycleKeyFact, candidate: ContextSectionCandidate) -> None: ...

class ContextCandidateLifecycleDecisionFact(BaseModel):
    candidate_id: str
    status: Literal["freshly_collected", "reused", "not_cacheable"]
    reason_code: ContextLifecycleReasonCode
    cache_key: ContextCandidateLifecycleKeyFact | None
    replaced_candidate_fingerprint: str | None
    decision_fingerprint: str

class ContextCandidateInvalidationFact(BaseModel):
    candidate_id: str
    old_candidate_fingerprint: str
    new_candidate_fingerprint: str
    reason_code: ContextLifecycleReasonCode
    invalidation_fingerprint: str

class ContextCandidateCollectionDecisionFact(BaseModel):
    source_kind: str
    selected_source_ids: tuple[str, ...]
    omitted_source_count: int
    reason_code: ContextCandidateCollectionReasonCode
    policy_fingerprint: str
    decision_fingerprint: str

class PreparedContextCandidateEntryFact(BaseModel):
    candidate: ContextSectionCandidate
    lifecycle: ContextCandidateLifecycleDecisionFact

class PreparedContextCandidateSet(BaseModel):
    policy: ContextCandidateCollectionPolicyFact
    entries: tuple[PreparedContextCandidateEntryFact, ...]
    collection_decisions: tuple[ContextCandidateCollectionDecisionFact, ...]
    invalidations: tuple[ContextCandidateInvalidationFact, ...]
    candidate_set_fingerprint: str

@dataclass(frozen=True, slots=True)
class ContextLifecycleCacheWriteCandidate:
    key: ContextCandidateLifecycleKeyFact
    candidate: ContextSectionCandidate

def prepare_context_candidates(
    *,
    candidates: tuple[ContextSectionCandidate, ...],
    policy: ContextCandidateCollectionPolicyFact,
    cache: ContextLifecycleCachePort | None,
) -> tuple[
    PreparedContextCandidateSet,
    tuple[ContextLifecycleCacheWriteCandidate, ...],
    tuple[ContextLifecycleCacheOperationalDiagnostic, ...],
]: ...
```

scope由 snapshot typed identity推导：

- stable -> runtime session；
- run -> run ID；
- step -> run ID + model call index + compile attempt；
- ephemeral -> not cacheable。

cache read发生在pure compile前的invocation preparation；输出required的`PreparedContextCandidateSet`与process-local
cache write candidates，prepare期间不mutation cache。entries按
candidate order稳定排列，collection decisions、lifecycle status/reason/cache attribution与invalidations全部进入set
fingerprint和manifest。
cache不得接收整个snapshot或访问其他source。compiler不再重新apply lifecycle，也不读取cache port。validated compile完成后
caller才commit write candidates；cache write失败不改变compiled payload，write candidates不进入manifest semantic fingerprint。

V1 process-local implementation必须是bounded LRU，同时限制entry count与aggregate payload chars。默认值属于runtime配置而非
semantic fact；eviction只影响命中率，不影响output。cache至少暴露bounded `entry_count/total_chars/max_entries/max_chars/
eviction_count/skipped_oversize_entries`诊断。step key包含model-call/compile-attempt，不能成为无界session字典。read异常的
canonical lifecycle仍是普通`freshly_collected/cache_miss`；异常只进入process-local operational diagnostic，不进入
`PreparedContextCandidateSet`、manifest或任何semantic fingerprint。write异常同理。

单entry大于`max_chars`时必须在mutation前拒绝并增加`skipped_oversize_entries`；不得先插入后循环驱逐，否则一个oversized
candidate会清空全部已有LRU entry后再驱逐自己。

### 12.3 timing overlay

source timing参与 candidate semantic fingerprint；compile-time age/header由 snapshot timing派生，不写回 cached
candidate。因此不同 compile time不会污染 cached source payload。

overlay是纯函数且顺序冻结为：candidate collection/materialization -> lifecycle prepare -> compiler authority validation ->
timing overlay -> budget allocation -> lowering。它为每个compiled section写结构化`metadata["timing"]`；candidate section按
policy生成model-visible `timing_header_text`，transcript section保留structured timing metadata但不把header插入历史消息正文。
memory与subagent分别使用`memory_projection`、`subagent_result` freshness，不能统一冒充`current_turn`。Inspector与provider
payload回归必须同时证明结构化timing和可见header存在。age只由snapshot `compiled_at_utc`与source fact计算；cache中不得保存
compile age或rendered timing header。

---

## 13. 新 compiler API

### 13.1 production API

```python
def compile_context(
    *,
    facts: ContextFactSnapshot,
    transcript: TranscriptCompileInput,
    tool_results: PreparedToolResultRenderInput,
    section_candidates: PreparedContextCandidateSet,
) -> CompiledContext:
    ...
```

`facts` 是第 4.3 节的 process-local immutable wrapper；其 `fact` 是 event-safe snapshot。

### 13.2 compile 顺序

冻结为：

```text
1. validate aggregate input identity/fingerprints
2. validate snapshot/transcript/current-user join
3. validate transcript/tool-result pairing join
4. validate every candidate against snapshot authority
5. validate tool specs against resolved target capability
6. render normalized tool-result units
7. lower normalized transcript while preserving pairing
8. validate prepared candidate lifecycle/invalidation facts
9. overlay explicit compile timing
10. allocate typed section candidates
11. lower system/leading-user/handoff candidates by lowering_kind
12. construct provider-neutral LLMContext
13. estimate final payload with resolved estimator
14. validate input budget and compiler/pre-send estimate identity
15. return CompiledContext + cache candidates/audit facts
```

### 13.3 compiler 禁止行为

compiler 不得：

- 调用 wall clock；
- query EventLog/artifact/DB；
- mutate cache；
- mutate runtime state；
- resolve capability/permission/model；
-执行 memory recall；
-执行 compaction；
-按 tool name猜 origin；
-从 arbitrary metadata补字段；
-在 current user缺失时继续；
-自行产生新的 durable fact ID。

### 13.4 lowering ownership

最终 `LLMMessage` 只由 compiler生成。transcript projector、candidate collector、tool renderer都不得返回完整 final
provider-neutral message list。

tool renderer可返回 unit body/envelope fragment，但 assistant tool call/result message sequencing由 compiler根据
`TranscriptCompileInput` lowering。唯一公开seam为：

```python
def lower_transcript_for_context(
    *,
    transcript: TranscriptCompileInput,
    rendered_tool_results: PreparedToolResultRenderOutput,
) -> LoweredTranscriptMessages: ...
```

它必须按message/block index验证每个fragment恰好被消费一次，并对每个result执行四方identity join：
`TranscriptToolResultRefFact`、`ToolInteractionPairFact`、`ToolResultRenderUnit`、`RenderedToolResultFragment`的
tool-call ID、unit ID、call/result message ID、block index、global position与segment必须完全相等。任何跨call替换都fail
closed。lowering保持provider原始tool arguments、call/result pairing、message order和segment attribution。renderer模块不得
import `LLMMessage`或`LLMToolCall`；compiler不得接受已经lowering的transcript message list。

### 13.5 budget

- final input budget来自 snapshot resolved call；
- tool-result aggregate/segment/unit policy来自`PreparedToolResultRenderInput.resolved_policy`，并join snapshot basis；
- candidate collection limits来自`PreparedContextCandidateSet.policy`，并join snapshot policy；
- section allocation只消费 typed candidate priority/required；数值越小优先级越高；
- `required=True`是唯一must-keep标志，`source_kind="system"`本身不获得隐式保留；
- collection、degrade、omit与final lowering都必须使用同一稳定顺序：required优先、priority升序、source ID作为tie-break；
- 压力下从priority最大（最低优先级）的optional candidate开始degrade/omit；
- transcript/current user/pairing structure是 must-keep；
- Stage 3 不实现新 long-horizon degradation；
- current contract无法容纳时仍进入现有 pressure/compaction retry；
- retry后仍超限 fail closed。

### 13.6 compile output

`CompiledContext` 增加：

```python
input_audit: ContextCompileInputAuditFact
transcript_fingerprint: str
tool_result_units_fingerprint: str
tool_result_render_policy_fingerprint: str
prepared_candidate_set_fingerprint: str
compiler_contract_version: str
tool_result_render_decisions: tuple[ToolResultRenderDecisionFact, ...]
tool_result_render_operational: tuple[ToolResultRenderOperationalFact, ...]
```

现有 sections/tool specs/render decisions/budget仍保留，但 event-visible dict逐步替换成 typed facts。

---

## 14. `ContextCompileInputAuditFact`

完整 tool bodies与user text不应在每次 `ContextCompiledEvent` 重复存储。event 保存 bounded join fact：

```python
class ContextCompileInputAuditFact(BaseModel):
    snapshot_id: str
    snapshot_semantic_fingerprint: str
    snapshot_fact_fingerprint: str
    snapshot_schema_version: str
    compiler_contract_version: str
    source_runtime_session_id: str
    authority_from_sequence: int
    source_through_sequence: int
    authority_slice_plan_fingerprint: str
    transcript_projection_window_fingerprint: str
    run_start_event_id: str
    run_start_sequence: int
    continuation_event_id: str | None
    continuation_sequence: int | None
    continuation_count: int
    resolved_model_call_id: str
    model_call_index: int
    compile_attempt_index: int
    context_retry_index: int
    transcript_fingerprint: str
    transcript_message_count: int
    transcript_pair_count: int
    tool_result_units_fingerprint: str
    tool_result_unit_count: int
    tool_result_render_policy_fingerprint: str
    tool_result_render_input_fingerprint: str
    prepared_candidate_set_fingerprint: str
    section_candidate_count: int
    input_aggregate_fingerprint: str
    input_manifest_artifact_id: str
    input_manifest_fingerprint: str
    input_manifest_schema_version: Literal["context-input-manifest:v1"]
    input_manifest_write_outcome: Literal["stored", "confirmed_existing"]
```

validator要求counts非负、IDs/index与outer `ContextCompiledEvent`一致；continuation ID/sequence成对出现，count=0时
两者为空，count>0时两者等于snapshot latest continuation。authority from/through、plan fingerprint与window
fingerprint必须分别等于snapshot primary range/plan与transcript projection window；Inspector不得把authority range当作
model-visible transcript window。

`ContextCompiledEvent` 在 C3 schema switch 后必须拥有可解释的 input carrier，但完整 audit 与 early failure
是显式union，不能用 nullable 字段随意组合：

```python
class ContextCompileInputFailureFact(BaseModel):
    failure_stage: Literal[
        "event_slice",
        "snapshot_build",
        "transcript_normalization",
        "tool_result_normalization",
        "candidate_collection",
        "candidate_materialization",
        "tool_result_policy_resolution",
        "render_cache_prepare",
        "candidate_lifecycle_prepare",
        "input_manifest_write",
    ]
    context_id: str
    resolved_model_call_id: str
    model_call_index: int
    compile_attempt_index: int
    context_retry_index: int
    snapshot_id: str | None
    source_through_sequence: int | None
    available_component_fingerprints: tuple[tuple[str, str], ...]
    input_aggregate_fingerprint: str | None
    manifest_candidate_artifact_id: str | None
    manifest_candidate_content_fingerprint: str | None
    manifest_candidate_metadata_fingerprint: str | None
    manifest_write_outcome: Literal[
        "not_attempted",
        "confirmed_absent",
        "conflict",
        "outcome_unknown",
        "deadline_exceeded",
    ]
    reason_code: ContextInputFailureReasonCode

class ContextCompiledEvent(EventBase):
    input_audit: ContextCompileInputAuditFact | None
    input_failure: ContextCompileInputFailureFact | None
```

invariant：

- `status in {compiled, pressure}`：`input_audit` required，`input_failure=None`；
- `status=failed` 且 manifest已确认stored/identical：`input_audit` required，`input_failure=None`；
- `status=failed` 且 aggregate input或manifest尚未形成：`input_failure` required，`input_audit=None`；
- `status=failed` 且 aggregate input已形成但manifest未确认：仍必须使用`input_failure`，不得使用full audit；
- 两者不得同时非空或同时为空；
- failure fact中尚未形成的measurement使用 `None`，不能用 `0`/empty fingerprint伪装。

manifest branch invariant：

- full `input_audit` required `input_manifest_write_outcome in {stored, confirmed_existing}`；
- `failure_stage != input_manifest_write` 时 `manifest_write_outcome=not_attempted`，三个candidate fields必须为空；
- `failure_stage=input_manifest_write` 时candidate ID/content/metadata fingerprints全部required，write outcome不得为
  `not_attempted`；
- aggregate已形成时`input_aggregate_fingerprint` required；早期失败时可空；
- `confirmed_absent` / `conflict` / `outcome_unknown` / `deadline_exceeded` 只描述write/confirmation结果，不声称
  artifact存在；Inspector可用candidate ID诊断，不将它当作replay artifact ref。

### 14.1 为什么需要 input manifest

event ranges可以重建 transcript/tool-result facts，但以下 non-transcript input 不一定能仅靠 event log逐字恢复：

- versioned base system instruction；
- workspace/runtime context的最终typed candidate；
- materialized memory/capability/subagent artifact body；
-当时使用的candidate lifecycle结果。

只保存fingerprint会让Inspector只能证明“某个hash曾存在”，不能重建相同compiler input。因此 C3 required写入一个
bounded deterministic input manifest artifact：

```python
class ContextCompileInputManifestFact(BaseModel):
    schema_version: Literal["context-input-manifest:v1"]
    input_aggregate_fingerprint: str
    snapshot: ContextFactSnapshotFact
    prepared_candidate_set: PreparedContextCandidateSet
    transcript_fingerprint: str
    tool_result_units_fingerprint: str
    tool_result_render_policy: ResolvedToolResultRenderPolicyFact
    tool_result_render_input_fingerprint: str
    compiler_contract_version: str
    manifest_fingerprint: str
```

manifest不重复保存transcript/tool-result body或cache hints；units由named event ranges/artifacts重建。manifest保存完整
snapshot、resolved render policy与prepared candidate set（含lifecycle/invalidation），因为这些内容可能来自versioned
code/config/materialization，而不是单一ledger event。replay重建units后必须重算render input fingerprint并与manifest比较。

manifest validator还要求：resolved render policy basis等于snapshot `tool_result_basis`；prepared candidate policy等于snapshot
`candidate_collection`；ordered unit IDs/fingerprints与transcript pairs精确对应；cache hints/operational cache status不得出现。

artifact ID按以下规则确定：

```text
context-input-manifest:<sha256(manifest schema + input aggregate fingerprint)>
```

写入使用 `put_if_absent_or_confirm_identical()`：同 ID、同 bytes、同 semantic metadata视为幂等成功；任一不同
则 `ArtifactContentConflict`。manifest先写，随后 `ContextCompiledEvent` 引用它；event commit失败可以留下可回收的
orphan artifact，但不允许 durable event引用缺失artifact。

artifact metadata至少包含 runtime session、run、context、resolved call、compiler version、content hash；不得包含
credential、provider secret或live filesystem object。

### 14.2 manifest size与secret边界

- manifest只保存provider-neutral compile facts，不保存API key、endpoint query、cookies；
- tool output正文仍由原events/artifacts拥有，不复制进manifest；
- tool schema来自snapshot，若超过bounded inline cap则使用descriptor artifact ref + hash；
- capability projection优先引用既有projection artifacts；
- candidate inline payload有明确per-candidate与aggregate safety cap，超限必须先artifact化；
- manifest aggregate cap超限是compile input contract error，不能静默截断。

### 14.3 manifest write acknowledgement

manifest write是model call前的durable dependency，必须使用稳定candidate bytes/ID与结构化结果：

```python
class ContextInputManifestWriteResult(BaseModel):
    outcome: Literal["stored", "confirmed_existing"]
    artifact_id: str
    content_fingerprint: str

class ContextInputManifestWriteConflict(RuntimeError): ...
class ContextInputManifestConfirmedAbsent(RuntimeError): ...
class ContextInputManifestWriteOutcomeUnknown(RuntimeError): ...
class ContextInputManifestWriteDeadlineExceeded(RuntimeError): ...
```

算法：

```text
prebuild stable manifest bytes/id/semantic metadata
-> put_if_absent_or_confirm_identical
-> success/identical: continue compile
-> confirmed absent/pre-commit failure: emit early failed fact, no model call
-> same ID different bytes/metadata: latch fail closed
-> cancellation/ack unknown: query deterministic artifact ID
     absent -> failed/no model
     identical -> continue only if owning segment is still active; otherwise terminalize run
     conflict/lookup failure -> latch, no model
```

若底层write task可能在caller cancellation后继续，必须转移给session-owned bounded pending manifest-write owner；close要drain
该owner。不能让迟到manifest没有owner，也不能仅凭Python exception判断未写入。

只有`ContextInputManifestWriteResult`返回后才能构造full `ContextCompileInputAuditFact`。任何其他结果如果ledger
仍可信，只能构造`ContextCompileInputFailureFact(failure_stage="input_manifest_write")`；如果同时发现ledger
structural latch，则不得为记录failure继续append。

### 14.4 `ContextInputManifestWriteService`

C3新增明确process owner，落点：

```text
src/pulsara_agent/runtime/context_input/manifest.py
```

```python
@dataclass(frozen=True, slots=True)
class ContextInputManifestWriteCandidate:
    runtime_session_id: str
    run_id: str
    context_id: str
    artifact_id: str
    canonical_bytes: bytes
    semantic_metadata: FrozenJsonObjectFact
    content_fingerprint: str
    metadata_fingerprint: str

class ContextInputManifestAttemptState(StrEnum):
    PENDING = "pending"
    WRITING = "writing"
    CONFIRMING = "confirming"
    STORED = "stored"
    ABSENT = "absent"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"

class ContextInputManifestPhysicalDrainState(StrEnum):
    IDLE = "idle"
    DRAINING = "draining"
    DRAINED = "drained"

class ContextInputManifestPostTerminalVerificationState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    VERIFIED = "verified"
    CONSISTENCY_FAILED = "consistency_failed"
    UNKNOWN = "unknown"

class ContextInputManifestPhysicalOperationKind(StrEnum):
    WRITE = "write"
    CONFIRM = "confirm"

class ContextInputManifestPhysicalOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    EXITED = "exited"

@dataclass(slots=True)
class ContextInputManifestPhysicalOperation:
    operation_id: str
    artifact_id: str
    started_by_generation: int
    kind: ContextInputManifestPhysicalOperationKind
    state: ContextInputManifestPhysicalOperationState
    executor_future: concurrent.futures.Future[object] | None
    observer_task: asyncio.Task[None] | None
    submitted_at_monotonic: float
    deadline_monotonic: float
    exited_at_monotonic: float | None
    result_status: str | None

@dataclass(slots=True)
class PendingContextInputManifestWrite:
    candidate: ContextInputManifestWriteCandidate
    attempt_generation: int
    attempt_id: str
    logical_state: ContextInputManifestAttemptState
    physical_drain_state: ContextInputManifestPhysicalDrainState
    post_terminal_verification_state: ContextInputManifestPostTerminalVerificationState
    current_operation_id: str | None
    physical_operation_ids: set[str]
    completion: asyncio.Future[ContextInputManifestWriteResult]
    attempt_deadline_monotonic: float
    last_error_code: str | None
    provisional_confirmation: Literal["absent"] | None

class ContextInputManifestWriteService:
    async def persist(
        self,
        candidate: ContextInputManifestWriteCandidate,
        *,
        deadline_monotonic: float,
    ) -> ContextInputManifestWriteResult: ...

    async def retry_confirmation(
        self,
        *,
        artifact_id: str,
        expected_generation: int,
        deadline_monotonic: float,
    ) -> ContextInputManifestWriteResult | Literal["absent", "conflict"]: ...

    async def drain_pending(self, *, deadline_monotonic: float) -> None: ...
    def pending_count(self) -> int: ...
    def inflight_operation_count(self) -> int: ...
```

ownership规则：

- service是logical attempt、shared completion与所有physical operations的唯一owner；caller只能
  `await asyncio.shield(attempt.completion)`，一个waiter
  取消不得取消shared Future或worker；
- caller deadline通过`wait_for(shield(completion), remaining)`实现；waiter timeout只返回typed outcome-unknown/
  deadline failure，attempt和Future仍由service拥有，不在waiter `finally`中删除；
- 每个artifact ID同时最多一个current generation；相同candidate join同一completion；不同bytes/metadata
  立即conflict；
- initial generation logical state转移只允许
  `pending -> writing -> confirming -> stored|absent|conflict|unknown`；retry只允许
  `unknown(g) -> confirming(g+1) -> stored|absent|conflict|unknown`；不允许跨过confirming凭write exception判断absent；
- 每次DB submit必须先在service lock内创建`ContextInputManifestPhysicalOperation`并加入
  service-wide operation registry，然后才提交executor；submit/start失败也要原子记录EXITED；
- 无operation时physical state为`idle`或已经过drain的`drained`；第一个operation注册时转`draining`，最后一个
  operation真实EXIT时才可转`drained`；
- `executor_future`是物理operation handle；`observer_task`被取消、waiter超时或logical generation切换均不代表
  DB operation结束。service只在executor future真实done callback中标记EXITED；
- executor done callback不从worker thread直接mutation asyncio state；它使用owner loop的`call_soon_threadsafe()`
  调度exact operation finalizer，finalizer再在service lock内标记EXITED/触发final confirmation；
- logical state转移、physical drain/post-terminal verification转移、设置completion或删除entry必须在service
  lock内比较artifact ID + exact
  attempt generation + attempt ID；旧generation迟到完成不得直接改写新logical state，但必须从physical registry正常
  EXIT并触发后续final confirmation；
- cancellation、worker/observer `BaseException`、confirmation read exception都由service保留exact candidate和所有
  operation handles，并将logical state转为`confirming`或`unknown`，不能只挂consume callback；
- write完成或结果不确定后service按artifact ID读取并比较bytes、media type、owner与semantic metadata，再解析
  stored/absent/conflict；
- confirmation读到identical可完成logical `stored`；若仍有physical operations，同时设置
  `physical_drain_state=draining` / `post_terminal_verification_state=pending`；若已无operation，则设置
  `drained` / `verified`；
- confirmation读到conflict可完成logical `conflict`并立即latch consistency；若仍有operation则physical state仍是
  `draining`，不提前释放Host resources；
- confirmation读到absent时，只要该artifact仍有任何queued/running `WRITE` operation（不论属于哪generation），
  该结果只能记为`provisional_confirmation="absent"`，不能完成Future、转为terminal ABSENT或删除entry；
- 最后一个inflight WRITE真实EXIT后，service必须安装一次新generation final confirmation；只有该次在
  `inflight WRITE count == 0`的lock内revalidation后仍读到absent，才可完成terminal `absent`；
- 即使logical result早已`stored`，old write EXIT也必须触发final identical/conflict verification；该verification不把logical
  state从STORED改回CONFLICT，也不重新complete Future。identical转`post_terminal_verification_state=verified`；
  冲突转`consistency_failed`并latch artifact consistency；读取不确定转`unknown`并保持close blocker；
- post-terminal `consistency_failed`发生时，session latch立即禁止新compile/model/tool mutation；若原segment仍活跃，
  run owner安装architecture-fault termination intent并bounded cancel/terminalize，不允许继续消费之前的logical STORED
  acknowledgement扩散更多事实；
- `stored`完成shared Future并返回ack；terminal `absent`以typed confirmed-absent failure完成waiters；
  `conflict`完成stable conflict并latch artifact consistency；`unknown`不删除entry且保持close blocker；
- `unknown`的current observer/operation即使已失败/完成也不复用。next safe point或close调用
  `retry_confirmation()`，以generation + 1安装新confirmation worker；仅exact-generation CAS能切换owner；
- artifact candidate的shared `completion` 跨retry generation保持同一identity；generation切换只替换attempt ID/
  worker/deadline/state，不得替换Future导致已有waiter永久等待旧对象；
- confirmation得到absent后entry可终结并删除；若仍有活跃compile caller需要重写，必须以新generation显式创建
  write attempt，不能把已完成的失败task重新await；
- caller已取消但artifact随后成功，只完成owner并留下可回收artifact；service不得擅自继续model loop；
- pending registry有明确上限（V1 default 8/session）；达到上限时拒绝新的compile attempt，不能无界保留bytes/tasks；
- physical operation registry也有独立上限（V1 default 16/session）；同一artifact最多一个queued/running
  confirmation，任何write未EXIT时不启动新write；达到cap时只允许drain，拒绝新attempt；
- entry只有logical result已是stored/absent/conflict，`physical_drain_state=drained`，post-terminal state不是
  `pending|unknown`，且operation registry已清空后才能移除；consistency-failed先转移到bounded session latch
  diagnostic/tombstone，不静默丢弃；
- logical `absent`只能与`drained + verified`组合；logical `stored`允许短暂与`draining + pending`组合；
  `consistency_failed`一旦出现必须有session latch，不论original logical result是stored还是conflict；
- `pending_count()`包含logical unknown和terminal-but-physically-draining/post-verification entries；Inspector/HostSession status分开
  显示`logical_state`、`physical_drain_state`、`post_terminal_verification_state`、inflight operation
  count/kind/generation与oldest operation age；不用单一`stored`隐藏draining/consistency-failed。

### 14.5 blocking PostgreSQL I/O 与deadline

当前artifact/EventLog PostgreSQL implementation是sync API。C3不允许在event loop中直接调用。除manifest writer自己的
confirmation state machine外，RuntimeSession必须拥有统一bounded `ContextInputIoService`，负责event-slice range read、
static-instruction artifact put/confirm、candidate artifact materialization及compaction/subagent context所需的ledger reads：

- service使用RuntimeSession-owned bounded `ThreadPoolExecutor`，不使用无界default executor；每个physical operation以稳定
  operation ID登记，caller cancellation/timeout不移除ownership；
- connect timeout、PostgreSQL `statement_timeout`与transaction deadline必须不超过attempt剩余绝对deadline；
- `PostgresEventLog.read_range_snapshot(..., deadline_monotonic=...)`在同一repeatable-read transaction中设置local
  `statement_timeout`，返回high-water与canonical bytes；不得只在async外层设软timeout；
- static system instruction不得在每次model step直接同步`archive.put_text()`；首次persist通过I/O service，成功后在
  RuntimeSession保存deterministic frozen fact，后续compile只复用该fact；
- `asyncio.wait_for(to_thread(...))`不是硬取消；外层deadline到达后真实worker thread仍由service持有，直到DB
  call真正返回，期间logical state是`unknown`、physical state是`draining`，且仍为close blocker；
- `HostSession.aclose()`必须先bounded drain context-input I/O，再drain manifest service；失败时保留session/resources供retry，
  不得继续破坏性teardown；
- 每个write/read/confirm都接收剩余deadline，connection必须在worker `finally`中关闭；
- executor排队也计入deadline，不得让资源耗尽把bounded close变成无界等待。
- `drain_pending()`同时等待logical resolution、所有physical executor futures真实EXIT与post-terminal verification
  收口；它不会因logical
  stored/absent/conflict就忽略旧write thread。deadline到达时及时抛close blocker，但service/Host resources必须保留；
- 旧write EXIT后即使自报failure，也必须再做一次final confirmation，因为client acknowledgement不能代表DB commit
  outcome。最终confirmation收口之前HostCore不得释放session/lease/workspace/executor。

service由`RuntimeSession` wiring拥有，生命周期覆盖所有run segments。Host close顺序冻结为：

```text
detach observers / stop new run admission
-> drain active run and sync tool execution owners
-> terminalize suspended run
-> ContextInputManifestWriteService.drain_pending(deadline)
-> compaction pending terminalization drain
-> child drain
-> MCP pending/supervisor drain
-> RuntimeSession close / terminal lease / workspace teardown
```

manifest drain超时或unknown必须抛`PendingContextInputManifestWriteError`；`HostSession.aclose()`不得继续`close()`，
`HostCore`必须保留session、lease、workspace和同一service供retry。sync `HostSession.close()`只允许pending count为0。

---

## 15. AgentRuntime orchestration

### 15.1 model step 顺序

生产 model step hard cut 为：

```text
resolved call already owned by model step
-> obtain committed run entry + working set
-> freeze ContextEventSlice high-water
-> build ContextFactSnapshot
-> project TranscriptCompileInput + ToolResultRenderUnit[] from same slice
-> collect/materialize typed ContextSectionCandidate[]
-> resolve complete tool-result render policy from snapshot basis + normalized units
-> convert process-local render cache reads into immutable validated hints
-> apply process-local candidate lifecycle cache into PreparedContextCandidateSet
-> compute aggregate input fingerprint
-> idempotently persist ContextCompileInputManifest candidate
     stored/confirmed_existing -> build full input audit and continue
     absent/conflict/unknown/deadline -> emit candidate-aware input failure; no compiler/model call
-> compile_context(...)
-> emit ContextCompiledEvent(input_audit=...)
-> after FULL ContextCompiled commit, commit render/lifecycle cache write candidates
-> pre-send validator checks same resolved call/estimate
-> LLMRuntime.stream(call=..., context=...)
```

从event slice开始到manifest candidate形成前，每个stage都必须经过统一的`ContextInputPreparationError`包装，并保存
`ContextInputPreparationProgress`中已经形成的component fingerprints。只要ledger structural state仍可信，snapshot、transcript、
tool-unit、policy、cache prepare、candidate collection/materialization任一步失败都要写
`ContextCompiledEvent(status="failed", input_failure=...)`；不得只写`RunEnd`。只有ledger本身已UNKNOWN/PARTIAL/latch时禁止
追加该audit。

### 15.2 pressure/compaction retry

```text
snapshot A -> compile pressure
-> emit pressure event with A audit
-> compact using existing service
-> wait terminal compaction fact full commit
-> new event slice
-> snapshot B
-> compile retry with same resolved call
```

不能 mutate snapshot A 或 transcript A 后重用同一 fingerprint。

### 15.3 pending interactions

- waiting-user状态不 compile；
- resume boundary full commit后才创建 continuation snapshot；
- MCP pending lease仍由 Supervisor拥有，不进入 snapshot runtime object；
- snapshot只保存 installation/binding identity事实；
- permission/capability continuation narrowing必须先在 run-boundary/working-set层完成，compiler不重算 gate。

### 15.4 subagent

- child使用 `CommittedSubagentRunEntry`；
- child primary slice来自 child ledger；
- parent-owned installation/subagent attribution用 named range；
- child不会读取 parent `LoopState`；
- parent memory是否注入由 child run entry/context policy typed candidate决定；
- child task ID可空的 primitive child语义保持不变。

### 15.5 delivery acknowledgement

subagent pending result只有在相应 compiled context成功写入且 matching `ModelCallStartEvent` full commit后才标记 delivered。
candidate被 allocation omitted时不得标记 delivered。delivery记录使用 compiled section/candidate ID join，不依赖字符串搜索。

---

## 16. 错误与 fail-closed 语义

### 16.1 typed errors

新增：

```python
class ContextInputContractError(RuntimeError): ...
class ContextEventSliceError(ContextInputContractError): ...
class ContextSnapshotMismatch(ContextInputContractError): ...
class TranscriptNormalizationError(ContextInputContractError): ...
class ToolResultPairingError(ContextInputContractError): ...
class ToolResultProfileMissing(ContextInputContractError): ...
class ContextCandidateMaterializationError(ContextInputContractError): ...
class ContextCompilerDeterminismError(ContextInputContractError): ...
```

reason code使用稳定 enum/registry，不用自由字符串决定控制流。

### 16.2 failure stage

`ContextCompiledEvent.status="failed"` 增加/冻结 `failure_stage`：

```text
event_slice
snapshot_build
transcript_normalization
tool_result_normalization
candidate_collection
candidate_materialization
tool_result_policy_resolution
render_cache_prepare
candidate_lifecycle_prepare
input_manifest_write
tool_result_render
section_allocation
final_payload
```

如果失败发生在 context ID/resolved call存在后，应写 failed event及可用 audit。若 snapshot尚未形成，保存
`ContextCompileInputFailureFact`，其中字段按 stage允许为空，不能用零伪装未测量。

例外：event slice已证明ledger structural untrusted、writer reconciliation latched或commit outcome unknown时，不得为了
记录compile failure继续append同一ledger。此时failure fact由Host/session diagnostic owner暂存并阻止model call；close/reopen
后由专门ledger recovery解释。`failure_stage="event_slice"` durable event只用于ledger结构仍可信的输入读取/unsupported
schema failure。

### 16.3 ledger structural fault

event slice出现 partial/non-contiguous/ID conflict时：

- 设置 ledger structural reconciliation latch；
- 禁止 model call；
- 普通 reducer rebuild不能清 latch；
- V1要求 close/reopen或专门 ledger repair；
- 不构造“best effort transcript”。

### 16.4 cache failure

- cache read exception：记录 diagnostic，按无 cache重算；
- cache write exception：不改变 compiled result，可记录 diagnostic；
- same key different payload：`ContextCompilerDeterminismError`，evict并 fail closed；
- cache不参与 durable correctness。

### 16.5 cancellation

snapshot/normalization/compile本身是 read-only；取消后由 committed run segment terminalizer负责 RunEnd。

如果 `ContextCompiledEvent` 写入处于 commit acknowledgement未知状态，必须沿用 RuntimeSession stable batch ID +
`confirm_batch()` 的 NONE/FULL/PARTIAL/UNKNOWN协议。不能因 Python `CancelledError` 推断未提交。

### 16.6 artifact materialization

artifact缺失、hash mismatch、media type不符均 fail closed。不得将 artifact ID本身当成文本继续 compile。

---

## 17. Replay、recovery 与 Inspector

### 17.1 replay authority

Inspector/replay通过 `ContextCompileInputAuditFact`：

1. 定位 owner runtime session；
2. 按authority from/through读取canonical slice，不用transcript window替代authority range；
3. 定位 RunStart/continuation/exposure/projection facts；
4. 在同一parent graph source range上重跑pure reducer，重建并精确比较candidate source selection；
5. 从durable static/capability artifacts、memory/plan/subagent events重建projection与authority；
6. 重新materialize prepared candidate facts/collection decisions；cache lifecycle只作历史审计，不作为重建真源；
7. 用同slice-plan finalizer重建snapshot，再按window重建transcript/units；
8. 重算 fingerprints并与manifest/`ContextCompiledEvent`比较。

Replay禁止直接信任manifest中的selection、authority或prepared candidate payload。连续且结构合法的slice无法被reducer
一致折叠时报告`ledger_untrusted`；从合法slice重建出的selection与manifest不一致时报告`contract_mismatch`，稳定reason code为
`context_input_candidate_selection_mismatch`。其他ledger-derived重建结果不一致同样必须按结构损坏与契约漂移分别归类，不得继续
标记`exact_replay`。

### 17.2 replay等级

Inspector报告：

```text
exact_replay
fact_replay_only
artifact_missing
contract_mismatch
ledger_untrusted
```

- `exact_replay`：所有内容/artifact/compiler version可用，compiled payload与canonical render decisions hash一致；
  cache operational facts允许不同且明确排除比较；
- `fact_replay_only`：facts可join但旧 compiler code unavailable，只验证input；
- `artifact_missing`：引用body不可取；
- `contract_mismatch`：fingerprint/join不一致；
- `ledger_untrusted`：sequence/ID structural fault。

不得把不能exact replay报告为成功。

### 17.3 Inspector fields

每个 compiled context展示：

- snapshot ID/schema/semantic fingerprint/fact fingerprint；
- compiler contract version；
- primary/named source ranges；
- authority plan fingerprint、transcript window kind/summary/retained history/protected run ranges；
- run-entry kind与RunStart ref；
- continuation ref；
- resolved call/target fingerprint；
- bounded candidate source selections与collection decisions，含各自truncated标志；
- 明确区分`no_eligible_sources`与`policy_limit(selected=(), omitted=N)`；
- transcript message/pair counts；
- stripped unfinished calls；
- tool unit count及profile分布；
- descriptor source exposure/event refs、render-contract/variant fingerprints及actual pair分布；
- durable builder ID/version/declarative contract fingerprint及registry join status；
- 当前进程`implementation_build_fingerprint`作为明确标注的process-local diagnostic；该字段不得显示在historical
  durable fact栏，也不得参与historical replay verdict；
- external requirement/result attribution、frozen capture-policy与builder-contract join status；
- resolved render policy/basis fingerprints与latest reserved unit IDs；
- candidate count/source kind/lifecycle status/invalidation分布；
- cache operational hit/miss（明确不属于semantic replay）；
- aggregate input fingerprint；
- manifest candidate/write acknowledgement outcome或failed candidate diagnostics；
- replay status与diagnostics。

### 17.4 recovery

session reopen后 dangling RunStart先按 Host/child recovery contract修复或恢复 owner，再允许 compile snapshot。snapshot builder
不承担 RunEnd repair。recovery note必须来自durable recovery decision/event并成为typed candidate/message segment。

---

## 18. Event schema migration

### 18.1 `ToolResultEndEvent`

C2 hard cut：

```python
render_profile: ToolResultRenderProfileFact
essential_capture_policy: ToolResultEssentialCapturePolicyFact | None
essential_result: ToolResultEssentialFact | None
terminal_payload_timing: TerminalPayloadTimingFact | None
```

不再藏在 arbitrary metadata。所有 production emitter、deny/error helper、MCP resume terminal path、terminal tools、
subagent/workflow tools同时迁移。profile/essential必须满足第10.3节branch matrix；renderer不得再调用
`_parse_tool_result_json()`/`_is_terminal_like_payload()`生成essential。
profile内的descriptor attribution、render-contract/variant fingerprints是required durable join（除explicit unknown-denial）；
event emitter必须在append前验证当时run-frozen descriptor，不把该校验推迟给renderer。
outer event state必须属于selected variant allowed states，terminal payload timing必须满足variant的
required/optional/forbidden约束；pre-execution denial只有universal observation timing。

### 18.2 `ExternalExecutionResultEvent`

C2 hard cut：

```python
class RequireExternalExecutionEvent(EventBase):
    external_tool_calls: tuple[ExternalToolCallRequirementFact, ...]

class ExternalExecutionResultEvent(EventBase):
    external_results: tuple[ExternalToolResultIngressFact, ...]
```

requirement/result tuples均按tool call ID排序且唯一。result必须通过`ExternalToolResultIngressBuilder`绑定一个
full-committed requirement；block/timing/semantics/descriptor attribution/contract/variant/state/phase/timing policy全部valid；
requirement中builder declarative contract与capture policy required durable；embedded tool-call ID一致；
duplicate/missing/extra result IDs拒绝。C2同步删除metadata timing map与三个并行tuple的旧constructor，assembler/reducer从
`external_results[].result_block` thaw owned block。

### 18.3 `ContextCompiledEvent`

C3 hard cut：

```python
input_audit: ContextCompileInputAuditFact | None
input_failure: ContextCompileInputFailureFact | None
failure_stage: ContextCompileFailureStage | None
tool_result_render_decisions: tuple[ToolResultRenderDecisionFact, ...]
tool_result_render_operational: tuple[ToolResultRenderOperationalFact, ...]
```

compiled/pressure/failed invariant：

- compiled required full input audit + final payload budget；
- pressure required full input audit + measured stage；
- failed按failure stage required恰好一个audit/failure fact；
- outer context/call/index与audit完全一致。
- canonical decisions与operational cache facts按unit ID一一对应；只有canonical decisions参与replay payload equality。

### 18.4 database strategy

项目未上线，推荐 C3：

- schema migration增加required JSON fields/typed serialization；
- existing dev DB可明确reset；
- 若保留数据，提供一次性 migration只接受能从existing events完整重建的rows；
- 无法补render profile/current user的old rows标记unsupported dataset，不自动猜值。

Host startup只verify schema version；migration仍由privileged command执行。

---

## 19. C0–C5 实施顺序

每个 PR 必须独立全绿。不得先更新长期 contract、后让代码数个PR处于不满足contract的状态。

垂直切换状态：

| PR | production snapshot | production transcript/render | production compiler | event schema |
|---|---|---|---|---|
| C0 | old | old | old | unchanged |
| C1 | old + new shadow validation | old | old | unchanged |
| C2 | new shadow | normalized units/fragment renderer仅做shadow equality；production仍完整走old path | old | render profile + essential hard cut |
| C3 | new only | new only | new only | input audit/failure hard cut |
| C4 | new only | new only | new only | Inspector/replay接入 |
| C5 | new only | new only | new only | 删除全部compatibility |

C2不把normalized transcript/render output重新包装成旧`ContextCompileInputs`。新projector/renderer只做shadow contract验证；
C3在同一PR原子切换renderer与compiler并删除旧path。测试也分别验证old production与new shadow，不提供可被production复用的
legacy conversion helper，避免短期bridge演变成dual truth。

### 19.1 C0：低层 facts 与纯 validator（additive）

内容：

- 新增 `primitives/context.py`；
- 定义 canonical JSON/fingerprint helper；
- 定义 snapshot、transcript、tool unit、candidate、audit facts；
- 定义完整resolved render/candidate policies、prepared input facts与essential union；
- 定义terminal command executed/error分支、descriptor render attribution与external ingress facts；
- 定义声明式`ToolResultSemanticsBuilderContractFact`，canonical contract fingerprint不包含任何build identity；
- 定义 `RunPermissionSnapshotFact` 并让 runtime wrapper可从fact构造；
- 定义 process-local `ContextFactSnapshot` wrapper；
- 定义 typed errors/reason codes；
- 不切 production compiler；
- 不修改 required event schema。

验收：

- DTO positive/negative tests；
- multi-variant result-render contract/code+pair validator；
- variant allowed-result-state/execution-phase/terminal-timing matrix validator；
- pre-execution denial variant不允许success/running/required terminal timing；
- semantics builder registry missing/duplicate/version mismatch/frozen mutation拒绝；
- declarative builder contract canonical fingerprint重算，手写错误fingerprint拒绝；
- 同一builder ID/version注册不同contract fingerprint拒绝；语义变化只改version或只改fingerprint均违反发布contract；
- declarative contract生成的conformance vectors覆盖variant selection、state/phase/timing、capture policy与
  normalization；声明相同contract但产出不同semantics的callable build拒绝进入composition root；
- `implementation_build_fingerprint`变化不改变descriptor/event/requirement/semantic fingerprints；
- terminal pre-execution error fact不要求执行身份；
- actual profile contract/variant/source attribution fingerprint validators；
- recursive immutability tests；
- NaN/inf/invalid UTC拒绝；
- fingerprint deterministic；
- primitives architecture import guard。

### 19.2 C1：event slice 与 snapshot builder（shadow）

内容：

- PostgreSQL/InMemory `ContextEventSliceReader`；
- `FrozenStoredEvent` canonical bytes，禁止slice持有AgentEvent引用；
- canonical authority slice与`TranscriptProjectionWindowFact`分离；
- preflight/mid-turn compaction使用同一high-water并保护current RunStart/tail；
- frozen wrapper identity与canonical bytes唯一factory双向验证；
- InMemory append/read ownership hard cut；
- atomic high-water tests；
- live/replay collectors、pure `ContextSnapshotBuildInput` builder与invocation binder；
- Host/subagent committed entry支持；
- live/replay共用builder core；
- AgentRuntime在现有compile旁生成shadow snapshot并比较核心facts；
- mismatch仅在test/dev hard fail，生产切换前收集diagnostic。

本PR长期 contract只新增 snapshot/input定义，不宣称production compiler已hard cut。

验收：

- concurrent append不会进入同一slice；
- preflight compaction的terminal早于RunStart时authority/window join正确；
- mid-turn compaction的terminal晚于RunStart时RunStart/current-user/current-run tail仍在protected projection；
- run/continuation/current-user/permission/target/exposure join；
- Host/child equality；
- nested mutation不能改变ledger/slice/另一projector；
- raw suspended ABA token验证后丢弃，manifest只含fingerprint；
- no `LoopState` import in new builder；
- shadow dogfood不改变trajectory。

### 19.3 C2：normalized transcript/tool units 与 render-profile hard cut

内容：

- transcript projector；
- tool pair/unit projector；
- malformed/non-object raw tool arguments preserving projector；
- durable render-profile + per-call essential + terminal-domain timing facts；
- descriptor allowed render variants + versioned declarative semantics builder contract/classifier；
- composition-root `ToolResultSemanticsBuilderRegistry.resolve_binding()` binding hard cut；
- pre-execution deny/error helpers required descriptor-aware semantics，normal/deny/workflow/MCP/external全部调用resolved builder；
- `RequireExternalExecutionEvent` descriptor contract freeze + `ExternalToolResultIngressBuilder`；
-完整render policy resolver与immutable cache hints；
- 同PR迁移所有 production emitters/ingress/replay；
- renderer shadow API切换为units并只返回per-unit fragments，不返回`LLMMessage`；
- C2结束前不存在normalized->legacy test/production adapter；
- render cache移出scratchpad，归AgentRuntime/session-owned cache owner；
- MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT同步更新。

验收：

- live/replay DTO equality；
- multiple/parallel tool pairing；
- external result；
- artifact/timing/profile validation；
- terminal-like JSON不能伪造terminal profile；
- terminal_process list/observation/error actual variant都必须命中descriptor allowed pair；
- observation process ID required，missing ID只进入typed error branch；
- permission/exposure deny保留terminal_process error semantics而非generic fallback；
- custom counting builder在normal/deny/MCP resume路径的调用次数与actual result数一致；known descriptor路径builder调用为0时测试失败；
- terminal command deny/malformed/adapter-init error使用command-error essential且不伪造cwd/session/backend；
- success result + deny variant、denied result + executed variant、pre-execution terminal timing非空全部拒绝；
- builder binding缺失/重复/version不匹配时normal/deny/external三路均fail closed；
- descriptor、registry binding与External requirement的builder ID/version/contract fingerprint三方不一致均fail closed；
- 同semantic contract下仅process-local build fingerprint变化不拒绝执行，且不改变任何durable payload；
- descriptor/event/External requirement/manifest序列化中不存在`implementation_build_fingerprint`，Inspector historical view也不展示它；
- actual profile通过durable exposure/descriptor source ref可exact replay；
- external submission只能绑定full-committed requirement，current exposure改变不影响join；
- external submission不接收descriptor/exposure/contract/capture-policy authority字段，builder从原requirement注入；
- no-essential-only external selectable variants的capture policy必须为空，任一post-execution variant可生成essential时
  policy required；
- requirement创建后升级runtime capture policy或重启进程，delayed result仍使用原冻结policy；不支持该policy version时
  fail closed；
- 相同tool call与render contract只改变冻结capture policy时，requirement fingerprint必须改变；只改变当前runtime default
  policy或process-local build fingerprint时，既有requirement fingerprint与delayed result semantics不得改变；
- command/process/inventory essential无需解析result JSON即可replay；
- invalid/non-object arguments exact raw replay + paired error result；
- resolved policy与当前LoopBudget allocator parity；
- current user exact join；
- provider-native ordering tests。

### 19.4 C3：compiler API与event schema原子 hard cut

内容：

- 新 `compile_context(*, facts, transcript, tool_results, section_candidates)`；
- 最小 Stage-3 candidate collector；
- lifecycle cache typed port；
- `PreparedContextCandidateSet` lifecycle/invalidation carrier；
- deterministic `ContextInputManifestWriteService`、generation/CAS pending owner、shielded waiter与confirmation retry；
- AgentRuntime model loop切换；
- compiler独占normalized transcript/tool-call/result fragment lowering；
- RuntimeSession-owned render cache采用prepare-read/FULL-commit-write两阶段协议；
- RuntimeSession-owned bounded context-input I/O service承接sync PostgreSQL context reads/writes；
- `ContextCompiledEvent` required input audit/failure union；
-删除 `ContextCompileRequest.state`；
-删除production `ContextCompileInputs`；
-删除current user/history/recovery fallback；
- contracts同步迁移：Agent loop、message/context、LLM transport、Inspector input audit。

该PR是production schema switch。所有producer/caller/serializer必须同PR完成，不保留 overload。

验收：

- production compiler module无法import `LoopState`/`Msg`；
- AgentRuntime不构造component string tuple；
- renderer module不import/construct `LLMMessage`/`LLMToolCall`；
- candidate collection/allocation/lowering统一使用required-first、priority升序，degrade/omit从最低优先级optional开始；
- compile/pre-send estimate完全一致；
- pressure retry产生新snapshot；
- pure compiler不调用任何cache port；
- manifest包含resolved render policy与prepared lifecycle facts；
- full input audit只能在manifest stored/identical acknowledgement后构造；
- manifest absent/conflict/unknown使用candidate-aware input failure，不引用未确认artifact；
- manifest write cancellation/unknown由session owner以新generation bounded confirmation drain；
- sync PostgreSQL artifact calls使用bounded executor与DB-level deadline；
- static instruction artifact put与event-slice PostgreSQL read不在event loop同步执行；
- blocking I/O caller cancellation后physical owner仍被Host close drain；
- pre-manifest每个preparation stage失败都有matching `ContextCompiledEvent.input_failure`；
- logical generation retry保留所有old physical operation handles；
- provisional absent在任何old write存活时不能terminalize，last write EXIT后必须final confirm；
- logical/physical-drain/post-terminal-verification状态正交，STORED不隐藏draining或consistency failure；
- plan/MCP resume/subagent compile通过。

### 19.5 C4：Replay / Inspector / recovery equality

内容：

- Inspector input join与replay status；
- context payload fingerprint comparison；
- source range/units/candidates projection；
- restart/recovery reconstruction；
- PostgreSQL schema/index/query优化；
- no artifact/body duplication audit。

C4不补写C3历史event，也不改变input事实生产责任。

验收：

- live/replay exact equality fixture；
- process restart equality；
- missing artifact/contract mismatch明确分类；
- child cross-ledger join；
- Inspector不能把partial replay报告为exact。

### 19.6 C5：删除旧 facade与兼容路径

删除：

- `ContextCompileInputs`；
- old `ContextCompileRequest`；
- `build_llm_context()`；
- `msg_to_llm_messages()`；
- old `build_compiled_context(state=...)`；
- `render_segmented_llm_messages(list[Msg], ...)`；
- scratchpad render cache key；
- legacy current-user anchor inference；
- compiler `request.state` reads；
- compiler-side recovery projection；
- component prompt tuple adapter；
- test-only production aliases。
- external result raw block + metadata timing-map constructor。

测试改为构造 typed snapshot/transcript/units/candidates，不保留旧 constructor helper冒充production。

验收：

- grep gates全空；
- full pytest/ruff；
- real LLM core suite；
- opt-in dogfood全量；
- PostgreSQL replay/Inspector；
- no behavior drift trajectory comparison。

---

## 20. 具体修改文件

### C0

- `src/pulsara_agent/primitives/context.py`（新增）
- `src/pulsara_agent/primitives/tool_observation.py`（新增）
- `src/pulsara_agent/primitives/tool_result.py`（新增）
- `src/pulsara_agent/primitives/permission.py`
- `src/pulsara_agent/primitives/__init__.py`
- `src/pulsara_agent/capability/result_semantics.py`（新增，additive registry/protocol）
- `tests/test_context_input_facts.py`（新增）
- `tests/test_context_input_architecture.py`（新增）

### C1

- `src/pulsara_agent/runtime/context_input/event_slice.py`（新增）
- `src/pulsara_agent/runtime/context_input/snapshot.py`（新增）
- `src/pulsara_agent/runtime/context_input/invocation.py`（新增）
- `src/pulsara_agent/event_log/protocol.py`
- `src/pulsara_agent/event_log/in_memory.py`
- `src/pulsara_agent/event_log/postgres.py`
- `src/pulsara_agent/runtime/run_entry.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/runtime/agent.py`
- `tests/test_context_snapshot_builder.py`（新增）

### C2

- `src/pulsara_agent/runtime/context_input/transcript.py`（新增）
- `src/pulsara_agent/runtime/context_input/tool_results.py`（新增）
- `src/pulsara_agent/runtime/context_input/external_results.py`（新增）
- `src/pulsara_agent/message/assembler.py`
- `src/pulsara_agent/message/reducer.py`
- `src/pulsara_agent/runtime/transcript.py`
- `src/pulsara_agent/runtime/context_engine/tool_results.py`
- `src/pulsara_agent/capability/descriptor.py`
- `src/pulsara_agent/capability/builtin_provider.py`
- `src/pulsara_agent/capability/providers/mcp.py`
- `src/pulsara_agent/capability/result_semantics.py`
- `src/pulsara_agent/tools/base.py`
- `src/pulsara_agent/event/events.py`
- `src/pulsara_agent/event/__init__.py`
- `src/pulsara_agent/event_log/serialization.py`
- `src/pulsara_agent/tools/executor.py`
- `src/pulsara_agent/runtime/tool_loop.py`
- `src/pulsara_agent/runtime/execution_handles.py`
- `src/pulsara_agent/runtime/wiring.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/tools/builtins/terminal.py`
- `src/pulsara_agent/tools/builtins/terminal_process.py`
- `src/pulsara_agent/tools/adapters/mcp.py`
- all tool-result production emitters/helpers
- `tests/test_context_normalization.py`（新增）
- `tests/test_external_execution_ingress.py`（新增）
- `tests/test_event_message_system.py`
- `tests/test_agent_runtime_loop.py`

### C3

- `src/pulsara_agent/runtime/context_input/candidates.py`（新增）
- `src/pulsara_agent/runtime/context_input/manifest.py`（新增）
- `src/pulsara_agent/runtime/context_engine/types.py`
- `src/pulsara_agent/runtime/context_engine/compiler.py`
- `src/pulsara_agent/runtime/context_engine/lifecycle.py`
- `src/pulsara_agent/runtime/context_engine/cache.py`（新增）
- `src/pulsara_agent/runtime/context.py`
- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/runtime/subagent/runtime.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/runtime/wiring.py`
- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/host/core.py`
- `src/pulsara_agent/memory/foundation/protocols.py`
- `src/pulsara_agent/memory/artifacts/archive.py`
- `src/pulsara_agent/memory/artifacts/postgres_archive.py`
- `src/pulsara_agent/event/events.py`
- relevant contracts
- `tests/test_context_engine.py`
- `tests/test_context_input_manifest.py`（新增）
- `tests/test_host_lifecycle_contract.py`
- `tests/test_runtime_wiring.py`

### C4

- `src/pulsara_agent/inspector/service.py`
- `src/pulsara_agent/inspector/diagnostics.py`
- `src/pulsara_agent/inspector/store.py`
- `src/pulsara_agent/runtime/recovery.py`
- `src/pulsara_agent/runtime/transcript.py`
- PostgreSQL migration/index files
- `tests/test_inspector.py`
- `tests/test_host_resume.py`
- `tests/test_context_replay_equality.py`（新增）

### C5

- 删除/收缩旧 facade/export；
- `src/pulsara_agent/runtime/__init__.py`
- `src/pulsara_agent/runtime/context_engine/__init__.py`
- 所有旧 tests/helper/import；
- architecture/grep gates；
- docs/contracts最终清理。

---

## 21. 测试矩阵

### 21.1 facts/fingerprint

- snapshot recursive mutation impossible；
- tuple order changes fingerprint；
- random snapshot ID不改变semantic canonical helper的预期范围；
- same payload same fingerprint；
- invalid timestamp、NaN、inf、negative count拒绝；
- duplicate tool/candidate/message/unit IDs拒绝；
- process-local objects不可序列化进入fact。

### 21.2 event slice

- atomic high-water + events；
- canonical authority range与transcript projection window彼此独立；
- preflight compaction terminal before RunStart + protected current run；
- mid-turn compaction terminal after RunStart + protected current run；
- concurrent append excluded from frozen slice；
- non-contiguous range fail closed；
- duplicate ID/different payload fail closed；
- structural latch blocks compile；
- child named parent slice正确归属；
- decoded nested mutation不改变slice bytes/fingerprint；
- InMemory `iter()`/`get_by_id()`返回值mutation不改变stored event；
-两个projector decode owned copies互不影响。
- wrapper event ID/type/sequence/created_at任一与bytes不同均fail closed。

### 21.3 snapshot

- Host new run；
- Host resume；
- task-backed child；
- primitive child with `subagent_task_id=None`；
- permission mismatch；
- target mismatch；
- MCP installation mismatch；
- exposure/working-set mismatch；
- current user text/hash/timing mismatch；
- continuation belongs to different run；
- continuation history count/order/latest join；
- raw suspended token永不进入build input/snapshot/manifest；
- live raw token fingerprint mismatch fail closed；
- compile retry creates new immutable snapshot。

### 21.4 transcript

- plain user/assistant；
- multiple model replies；
- parallel tool calls/results；
- valid-object arguments保留exact raw string；
- valid-object whitespace/key-order不被canonical rewrite；
- invalid JSON arguments保留raw + stable parse error + paired result；
- non-object JSON arguments保留raw + stable parse error + paired result；
- external execution result；
- error/deny/cancel result；
- artifact/data placeholder；
- compaction summary + tail；
- recovery note；
- historical unfinished tool call strip audit；
- active orphan result fail closed；
- current user exactly once；
- live/replay equality。

### 21.5 tool-result units

- universal timing required；
- render profile required；
- tool ID/name mismatch；
- terminal profile/essential match；
- terminal command/process observation/inventory essential branches；
- terminal command pre-execution deny/malformed/adapter-init error essential branches；
- terminal command deny未提供cwd/session/backend仍合法，executed branch缺失身份拒绝；
- terminal_process descriptor三类actual variant与allowed pair矩阵；
- success block + deny variant / denied block + executed variant均拒绝；
- pre-execution variant携带terminal payload timing拒绝，executed required-timing variant缺失timing拒绝；
- optional/forbidden/required timing与external post-execution variant矩阵；
- semantics builder registry missing/duplicate/version mismatch/freeze-after-mutation负向测试；
- builder contract canonical recompute与descriptor/binding/requirement三方identity equality；
- 同一ID/version不同contract fingerprint registry conflict；
- semantics不变但`implementation_build_fingerprint`变化时durable payload/fingerprint完全相同；
- 已durable result replay在registry无当前builder binding时仍可完成fact validation；
- pre-execution exposure/permission/policy deny构造descriptor-aware process error essential；
- observation `process_id=None` schema拒绝；
- terminal error capture truncation/original chars与process summary omitted counts；
- terminal_process unsupported/missing-id/policy-deny使用typed error essential并保留requested action；
- terminal process inventory无single command/cwd也合法；
- missing durable essential rejects replay without parsing JSON；
- render contract/selected variant/source exposure attribution任一fingerprint mismatch拒绝；
- external requirement/result typed ingress live/replay equality；
- external result引用current exposure而非original requirement时拒绝；
- external submission DTO JSON不包含descriptor/exposure/contract/variant fingerprints或capture policy；
- ingress builder从committed requirement注入authority并产出完整semantics；
- no-essential-only post-execution variants的external requirement拒绝非空capture policy；essential-capable requirement拒绝
  空policy；
- delayed external completion跨runtime capture-policy升级/进程重启仍使用original requirement policy；
- unsupported frozen capture-policy version fail closed，不读取current policy fallback；
- terminal-like generic JSON remains generic；
- aggregate budget；
- all current `LoopBudget` optional/default combinations resolve to complete non-null policy；
- resolved policy output与old allocator golden matrix完全一致；
- essential envelope survives body omission；
- primary artifact preview preserved；
- image-first/text-second的compact/minimal envelope与decision选择同一text primary；非primary无read_more；
- binary-only artifact不生成primary_artifact_id；
- cache hit/miss provider payload与canonical decisions identical，operational status可不同；
- low-budget cache not reused as wide canonical render；
- clipped/omitted/compact/budget-exhausted render不产生cache write candidate；
- cache read/write exception只产生operational diagnostic且不阻止model call；
- 工具正文包含伪造`observed_at=`/`pulsara_tool_observation`仍保留真实timing；
- process restart no-cache output identical。

### 21.6 candidates

- system/runtime/memory/capability/plan/recovery/subagent typed candidate；
- inline content hash；
- source timing UTC/range/clock-source invariants；
- artifact materialization hash mismatch；
- duplicate candidate ID；
- required candidate omitted fails；
- lifecycle cache does not include compile age；
- prepared set持有fresh/reused/not-cacheable与invalidation facts；
- projection token budget/max subagent result selection与collection decisions一致；
- manifest round-trip preserves prepared lifecycle decisions；
- collector cannot import/read LoopState。
- candidate正文、artifact、event refs、channel/lowering任一偏离snapshot authority均拒绝；
- candidate source_timing/freshness改变即使重算全部fingerprint仍拒绝；
- memory/subagent正文忽略并行caller字符串，只从canonical events重建；timing使用event created_at/sequence；
- 合法artifact ID配伪造inline正文仍拒绝；
- memory/subagent timing freshness不冒充current_turn；
- lifecycle cache超过entry/char上限按LRU eviction并保持compiled payload不变。
- cache read exception与普通miss产生相同candidate-set/manifest fingerprint；
- oversized lifecycle entry在mutation前skip，不驱逐已有entries。

### 21.7 orchestration

- normal user run；
- model tool follow-up；
- pressure -> compaction -> retry same resolved call/new snapshot；
- pending approval resume；
- plan revise/approve/cancel；
- MCP input-required resume；
- immediate/dependency subagent；
- child WAITING_USER terminalization保持既有contract；
- cancellation before/after ContextCompiled commit；
- cancellation during manifest write transfers exact pending owner；
- manifest waiter cancellation不取消shared Future/worker；
- stale generation worker completion不得删除新confirmation attempt；
- old blocking write跨generation仍保留physical operation owner；
- newer confirmation读到absent但old write未EXIT时只记provisional absent；
- old write迟到commit后final confirmation得到identical，Host close不提前成功；
- old write失败EXIT后final confirmation再次读到absent才终结；
- logical stored + physical draining + post-verification pending Inspector状态；
- post-terminal conflict不反向改写logical Future，但转consistency-failed latch并阻止teardown；
- failed worker进入unknown后close以新generation confirmation收口；
- manifest confirmed absent/conflict/unknown failed event不引用artifact；
- manifest confirmed stored/identical才允许full input audit；
- blocking PostgreSQL write/read超时时Host close及时返回blocker，worker真实结束后可retry；
- input manifest same-ID/same-bytes/same-metadata idempotent；
- input manifest metadata-only conflict fail closed；
- concurrent PostgreSQL manifest writers confirm one canonical artifact；
- manifest confirmation read failure remains close blocker；
- pending manifest owner cap blocks new compile without unbounded retention；
- Host close drain timeout preserves session/lease/workspace and retry succeeds；
- subagent delivery only after matching ModelCallStart。

### 21.8 real LLM

至少覆盖：

- normal tool call；
- terminal background process observation；
- MCP disabled/enabled optional路径；
- plan workflow；
- subagent result handoff；
- context pressure/compaction retry；
- long PR4 trajectory回归，确认无ordered publisher gap；
- live trajectory中的 `ContextCompiledEvent.input_audit` 可replay。

---

## 22. Grep 与 architecture gates

C5 必须满足：

```bash
rg -n "ContextCompileInputs" src tests
rg -n "ContextCompileRequest" src tests
rg -n "build_llm_context\(" src tests
rg -n "msg_to_llm_messages\(" src tests
rg -n "build_compiled_context\(.*state" src tests
rg -n "tool_result_render_decision_cache" src/pulsara_agent/runtime/state.py src/pulsara_agent/runtime/context.py
rg -n "request\.state|state\.scratchpad|state\.messages" src/pulsara_agent/runtime/context_engine
rg -n "component_prompts" src/pulsara_agent
rg -n "project_recovery_from_state" src/pulsara_agent/runtime/context_engine
rg -n "events: tuple\[AgentEvent" src/pulsara_agent/runtime/context_input
rg -n "suspended_state_token: str" src/pulsara_agent/primitives/context.py src/pulsara_agent/runtime/context_input
rg -n "LoopBudget" src/pulsara_agent/runtime/context_engine
rg -n "ContextLifecycleCachePort|ToolResultRenderDecisionCachePort" src/pulsara_agent/runtime/context_engine/compiler.py
rg -n "_is_terminal_like_payload|_terminal_essential_envelope" src/pulsara_agent/runtime/context_engine
rg -n "tool_observation_timing_by_call_id|execution_results" src/pulsara_agent/event/events.py src/pulsara_agent/runtime/context_input
rg -n "implementation_contract_fingerprint" src tests
rg -n "implementation_build_fingerprint" src/pulsara_agent/primitives src/pulsara_agent/event
```

期望：production命中为零；若测试需要低层unit fixture，应使用新typed builders，不得保留旧 facade。

architecture guard：

- `primitives.context` 不import runtime/message/MCP；
- `context_engine.compiler` 不import `LoopState`、`Msg`、EventLog、artifact store；
- compiler/renderer不import或调用mutable cache ports；
- `context_input` projector不import provider adapters；
- `AgentRuntime`不直接构造 `ContextSection`/raw component tuples；
- Stage-3 collector显式标注待Stage 5删除。

---

## 23. 长期契约同步

按代码落地PR同步修改：

### C1

- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`
  - context read snapshot原子high-water；
  - stored/read objects不共享mutable references；
  - canonical bytes/fingerprint语义；
  - authority slice与transcript projection window分离；
  - stored-event wrapper/canonical payload identity完全一致。

### C2

- `contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md`
  - durable event -> normalized transcript -> compiler；
  - render profile、essential capture policy与typed essential required；
  - malformed/non-object tool arguments保留raw assistant payload并与error result配对；
  - External execution supported ingress；
  - external requirement冻结descriptor/render contract与essential capture policy，result使用typed ingress并回指original
    requirement；delayed completion不得读取当前system policy；
  - external submission不携带Pulsara authority，builder从committed requirement注入；
  - `Msg` 不再是 compiler input。
- `contracts/CAPABILITY_SURFACE_CONTRACT.zh.md`
  - descriptor required allowed result-render variants与versioned semantics builder；
  - variant required result-state/execution-phase/terminal-timing matrix；
  - composition-root registry是builder ID/version/declarative contract到callable的唯一binding seam；
  - descriptor、registry binding与External requirement的builder ID/version/contract fingerprint必须精确相等；
  - implementation build fingerprint仅是process-local diagnostic，不得进入durable或semantic identity；
  - executor按descriptor/provider构造actual profile，不按tool name猜测；
  - pre-execution permission/exposure/policy deny继续消费原descriptor contract。
  - actual result profile携带render-contract/variant fingerprints与durable exposure/descriptor source attribution。

### C3

- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`
  - model step input freeze顺序；
  - compiler不读LoopState；
  - pressure retry新snapshot。
- `contracts/LLM_TRANSPORT_CONTRACT.zh.md`
  - compiled payload/input audit/resolved call join；
  - pre-send estimate使用同一payload。
- `contracts/CAPABILITY_SURFACE_CONTRACT.zh.md`
  - tool specs从run-frozen exposure fact进入snapshot；
  - compiler不重新resolve。
- `contracts/HOST_RESUME_CONTRACT.zh.md`
  - pending manifest write是Host close blocker；
  - drain失败保留session/lease/workspace并允许同owner、新generation retry；
  - logical availability、physical drain与post-terminal verification状态独立；
  - manifest full audit必须晚于durable artifact acknowledgement。

### C4

- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`
  - input audit join；
  - exact/fact-only/missing/mismatch状态；
  - child cross-ledger ranges；
  - durable builder semantic contract与当前进程implementation build diagnostic分栏展示，后者不得冒充historical fact。
- `contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md`
  - compaction summary/window reference进入normalized transcript；
  - Stage 3不改变compaction算法。

---

## 24. 完成定义

阶段三只有在以下条件全部满足时才算 hard cut 完成：

1. production compiler无法在类型或import层访问 `LoopState`；
2. production model call不构造 `ContextCompileInputs`；
3. current user只来自RunStart typed fact；
4. compile读取单一high-water的canonical authority slice，transcript window不改变事实读取起点；
5. frozen-event wrapper identity与canonical bytes不可分裂；
6. Host与child的live collector从`CommittedRunEntry`生成build input，pure builder/replay不接收`RunWorkingSet`；
7. live/replay产生同形状、同fingerprint input；
8. raw suspended ABA token只在live collection验证，snapshot/manifest只保存fingerprint；
9. transcript保留message/block order、tool pairing与malformed raw arguments；result ref/pair/unit/fragment四方identity与
   block position精确join，跨call替换fail closed；
10. tool-result renderer只消费normalized units与完整non-null resolved policy；
11. descriptor allowed variants完整冻结state/phase/timing matrix，event actual profile可验证terminal/terminal_process
    多形态及pre-execution deny；
12. semantics builder声明式contract可通过run-frozen composition-root registry解析为唯一binding，descriptor、binding与
    External requirement的ID/version/contract fingerprint精确一致；process-local build fingerprint不进入durable truth；
13. pre-execution terminal error不伪造执行身份，actual profile可join原descriptor/exposure；
14. external result通过typed ingress回指committed requirement，caller不回传Pulsara authority；requirement冻结唯一
    essential capture policy，delayed completion不读当前system policy；
15. timing/render profile/typed essential/artifact provenance不从payload/name猜测；renderer使用explicit inclusion flags，不扫描
   工具正文判断timing是否存在；
16. non-transcript input至少通过typed candidate ingress，且逐项join snapshot-owned candidate authority；authority拥有唯一正文、
   source timing与selection facts，collector不接受并行字符串；memory/subagent从canonical events构造；
17. candidate的source/channel/lowering固定矩阵在schema与compiler两层生效；
18. candidate collection limits显式来自`ContextCandidateCollectionPolicyFact`；
19. lifecycle/cache preparation产出immutable prepared carriers，pure compiler不调用cache port；render cache只接纳
   full-visible/full-envelope/within-budget/payload-preserved结果，lifecycle cache是entry+chars双上界LRU；cache异常只进入
   process-local diagnostics，oversized entry在mutation前skip；
20. input manifest保存resolved render policy与prepared candidate lifecycle/invalidation facts；
21. full input audit只引用confirmed manifest，未确认candidate只进入input failure；
22. manifest logical、physical drain与post-terminal verification状态独立，迟到writer EXIT前Host不释放资源；
23. `ContextCompiledEvent`可join snapshot/source ranges/normalized counts，并投影每个section的structured timing；
24. same inputs + compiler version产生相同provider-neutral payload；
25. schema缺失/ledger gap/pairing mismatch全部fail closed；
26.旧facade、constructor、fallback reader与production aliases已删除；
27. full pytest、ruff、PostgreSQL、core real-LLM与opt-in dogfood全绿。

---

## 25. 为阶段四提供的稳定边界

阶段三完成后，Long-Horizon 只能改写以下明确对象：

```text
TranscriptCompileInput
  -> select/roll up windows
  -> preserve ToolInteractionPairFact
  -> preserve current-user anchor
  -> preserve protected current-run units

ToolResultRenderUnit[]
  -> artifact-aware thinning
  -> cross-unit soft projection allocation
  -> deterministic micro-compaction
  -> re-resolve PreparedToolResultRenderInput for the selected window

ContextCompilePolicyFact
  -> derive dynamic targets from ResolvedModelContextBudgetFact
```

它不再需要操作：

- mutable `LoopState.messages`；
- pre-rendered `LLMMessage`；
- arbitrary tool-result JSON string；
- scratchpad cache；
- current-user naming guess。

这正是本阶段必须先于 Long-Horizon 的原因。

---

## 26. 实施决策摘要

本章冻结以下不可回退的决定：

- **event slice先于snapshot**：一次compile只有一个source high-water；
- **authority与window分离**：compaction只改变transcript projection，不删除RunStart等join authority；
- **stored-event identity双向验证**：wrapper和canonical bytes不能成为两个事实源；
- **event-safe fact与runtime binding分层**：estimator/cache不塞进durable DTO；
- **committed entry only**：Host/child都不从state推断RunStart；
- **current user typed-only**：无metadata/API/state fallback；
- **pairing before renderer**：tool call/result是结构化事实；
- **render profile durable**：不按name/payload猜domain；
- **descriptor允许集、event实际值**：同一tool的多结果形态由versioned semantics classifier裁决；
- **variant是完整矩阵**：kind、result state、execution phase与terminal timing policy共同参与fingerprint；
- **builder semantic/build identity分层**：声明式contract fact是durable truth，composition-root registry返回完整binding；
  process-local implementation build fingerprint只做当前进程诊断；
- **deny不伪造execution identity**：terminal command executed/error essential为独立branches；
- **actual profile durable join**：contract/variant fingerprints与original exposure source ref一起持久化；
- **external ingress typed-only**：requirement冻结builder contract与essential capture policy，result回指原requirement且不读
  当前system policy；
- **external authority单一**：caller不回显Pulsara descriptor/exposure/fingerprint authority；
- **candidate ingress typed**：阶段三不再传component string tuple；
- **compiler pure**：无clock、I/O、cache mutation、runtime mutation；
- **cache不是真源**：restart/miss不改变output；
- **manifest acknowledgement先于audit**：full input audit绝不引用未确认artifact；
- **manifest worker由session拥有**：generation/CAS、shielded waiter与DB deadline共同封住取消/迟到完成；
- **logical/physical ownership分离**：absent只在所有old write EXIT后的final confirmation中才能成为终态；
- **logical/drain/verification正交**：logical STORED不隐藏physical draining，迟到conflict进一致性latch；
- **C3原子schema switch**：caller/event/contract同PR迁移；
- **C5删除兼容层**：未上线，不保留dual truth；
- **完整source ownership留在阶段五**：本章不制造假的最终registry。
