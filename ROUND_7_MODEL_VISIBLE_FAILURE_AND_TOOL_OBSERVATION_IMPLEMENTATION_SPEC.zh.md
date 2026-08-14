# Pulsara Round 7：Model-visible Failure Continuity 与 Tool Observation Timing/Freshness 实施规格

> 状态：**ACTIVATED — 2026-08-14**
>
> 记录日期：2026-08-14
>
> 代码真值基线：`acf8cbede97ba9e19146f0e7cb01d3245e64dbea`（`refactor: modularize conversation kernel repository`）
>
> 工作树说明：起草时另有一组尚未提交的 `src/pulsara_agent/host/` compatibility-layer 删除变更；本文不拥有、覆盖或评价该变更。coding agent 必须在第一个 production diff 前重新记录 clean checkpoint HEAD 与本文 SHA-256。
>
> 本文合并恢复 [PHC-13：跨 turn 失败/中断提示](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md#12-phc-13跨-turn-失败中断提示) 与 [PHC-14：Model-visible tool observation timing/freshness](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md#13-phc-14model-visible-tool-observation-timing-与-freshness)。两项共享 canonical reader 与 structured compiler 接缝，但不共享 durable authority。
>
> 上位架构：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 前置规格：[Round 3 compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A long-horizon envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)
>
> 历史产品证据：[HOST_TRANSCRIPT_FAILURE_NOTE_PLAN.zh.md](archived_docs/HOST_TRANSCRIPT_FAILURE_NOTE_PLAN.zh.md)、[PULSARA_UNIVERSAL_TOOL_OBSERVATION_TIMING_PLAN.zh.md](archived_docs/PULSARA_UNIVERSAL_TOOL_OBSERVATION_TIMING_PLAN.zh.md)
>
> 激活证据：[round7_model_visible_failure_and_tool_observation_activation.json](benchmarks/suites/core/v1/round7_model_visible_failure_and_tool_observation_activation.json)

---

## 0. 执行结论

Round 7 恢复两项相邻但不同的产品语义：

1. 当紧邻当前 turn 的上一 turn 未完成时，模型得到一条 bounded、脱敏、typed runtime guidance，知道用户输入与已接受的 canonical 内容仍然存在，并能区分用户停止、Runtime/provider 失败、Host 生命周期结束、writer takeover、resource boundary 与未知中断。
2. 每条 canonical tool result 都携带不可由 tool body 伪造的 immutable observation timing；每个新 turn 再追加一个小型 freshness frontier，使模型能把 tool result 判定为当前 turn、紧邻上一 turn tail 或更早历史，而不回写旧 result。

这两项必须建立在当前减法架构上：

~~~text
turn/tool canonical rows
    -> own accepted semantic truth

selective committed occurrences
    -> retain audit/extension occurrence truth
    -> are not replay input for these features

canonical reader in one REPEATABLE READ cut
    -> freezes transcript + permission/Plan + predecessor outcome + freshness frontier

structured compiler
    -> appends typed runtime observations and newly visible canonical items
    -> never rewrites an installed provider prefix
~~~

Round 7 在同一 Host、exact scope 与 compatible process-local epoch 内的硬约束仍是：

~~~text
SYSTEM[n+1]   == SYSTEM[n]
tools[n+1]    == tools[n]
messages[n+1] == messages[n] || append_only_suffix
~~~

因此以下方案全部禁止：

- 在下一 turn 重新渲染旧 tool result，把 `CURRENT` 原地改成 `HISTORICAL`；
- 把当前时钟、当前 turn ID 或 mutable freshness 塞进旧 tool-result fingerprint；
- 从 `TurnInterrupted` / `ToolResultAccepted` event replay 恢复 prompt；
- 恢复 `RunEnd`、`ToolResultStart/End` durable execution grammar；
- 用 raw exception、tool arguments、command、cwd、private URL、MCP requestState 或 provider transport detail 构造失败提示；
- 因 context source、TUI、hook 或 diagnostic consumer 失败否定已经成立的 canonical commit；
- 为 failure note 或 freshness 新增 relation、event、job、append guard、receipt、checkpoint、reducer 或 repair owner。

本轮同时收口一项已经由当前代码审计确认的provider-wire卫生问题：内部
contract/version/fingerprint不得因为“方便复用内部DTO”而被序列化给模型。它们仍可存在于
canonical row、process-local fact、validator、fingerprint与golden fixture中，但provider projection
只保留模型推理所需的产品语义、append-only历史的显式时域语义与后续调用所需的opaque handle。
`lifecycle`不是版本证明字段：它告诉模型某条已安装observation何时仍然适用，因此必须保留。
该清理与本轮既有compiler/lowering contract bump共用一次cold epoch，不形成独立产品机制。

本轮允许且要求的唯一 durable schema 扩展位于现有 `tool_results` row：

~~~text
observed_at timestamptz NOT NULL
observation_duration_microseconds bigint NULL
observation_origin_kind text NOT NULL
tool_reported_duration_microseconds bigint NULL
~~~

它们是 tool result 的 immutable semantic attributes，不是新 authority。`tool_results.observed_at`
是Runtime冻结known result的绝对观察时刻；`observation_duration_microseconds`由physical
invocation owner使用单一monotonic clock冻结；`observation_origin_kind`由exact execution
binding/producer在接纳时冻结；`tool_results.accepted_at`仍是PostgreSQL acceptance time。
`tool_execution_attempts.started_at`只保留effect admission audit语义，不参与跨时钟duration
算术。

Round 7 不增加 product relation、Committed/Live event、subject slot、append guard 或 durable job kind。activation oracle 继续为：

~~~text
Committed AgentEvent     34
Live AgentEvent          23
subject slots            15
append guards             2
product relations        26
durable job kinds         4
~~~

---

## 1. 基线、历史真值与当前代码真值

### 1.1 起草输入

~~~text
PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md
cb3e7b0a9f33e5e4c5b17850d47e1af580a3f23f094f868076351bb17a6a6e80

POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
217e520dd00549ae7d2ba63fb9dd607b54ea7366c629cc20880b98f8e4e9e541

ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md
1a996f8dda8c767043e4c84bf7d414724129dbd3d890d5cf3bb5463922cae6e6

ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md
9ee6cfca09869a67903a2164c2c2025d7c836998bd26a459336cee90658e34c2

ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md
608ecfdd8e4f20acc62c012fb39569c19c4a34f6bf981d8c96df0aa293f48832

archived_docs/HOST_TRANSCRIPT_FAILURE_NOTE_PLAN.zh.md
59297b4e87231ad55d68e32dead031470766800256b57a997a1dc99df95f50d1

archived_docs/PULSARA_UNIVERSAL_TOOL_OBSERVATION_TIMING_PLAN.zh.md
e7cc0398e739c0936a501ccec148eb75851c4094666ef3c56efc2c2d6144c83c
~~~

这些 hash 只标识起草输入，不是 activation evidence。实施时必须重新记录实际 checkpoint。

### 1.2 hard-cut 前 failure-note 真值

历史基线：`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`。

[历史代码确认] `5b7ad9f7:src/pulsara_agent/runtime/recovery.py` 已经实现：

- failed 与 user-aborted 使用不同 guidance；
- Host teardown 不冒充 user stop；
- 用户输入已保存；
- unfinished tool calls 区分 pending approval、started without completed result 与 ambiguous；
- wording 只列最多三项 tool name，不暴露 arguments；
- read-only、bounded-write、terminal 与 unknown effect 使用不同保守程度。

[历史代码确认] `5b7ad9f7:src/pulsara_agent/runtime/transcript.py` 只对最后一个 recoverable terminal run 注入 note；更新的 successful run 会使旧 note 不再重复；unfinished tool call 会被处理成 provider-safe pairing。

[历史测试确认] `5b7ad9f7:tests/test_host_core.py` 覆盖 failed with/without reply、partial/audit-only assistant、aborted、Plan aborted、unfinished tool、late result 与 newer successful successor。

这些历史语义值得恢复，但旧实现的事实来源必须删除：

- 不读取 `RunEndEvent`；
- 不 replay `ToolCallStart/ToolResultStart/ToolResultEnd`；
- 不重建 coroutine、run activation state 或 EventLog transcript；
- 不恢复“旧 assistant event fragment 可能成为 canonical partial message”的假设。

当前 assistant message 只有在 provider completion 后才原子提交。新 note 必须准确描述为：**已接受的 assistant entry 是完整 accepted message，但整个任务可能未完成；尚未提交的 live fragment 不属于 canonical transcript。**

### 1.3 hard-cut 前 timing/freshness 真值

[历史代码确认] `5b7ad9f7:src/pulsara_agent/primitives/tool_observation.py` 的 `ToolObservationTimingFact` 已拥有：

- UTC `observed_at`；
- optional source start/end；
- non-negative observation duration；
- optional trusted tool-reported duration；
- current/background/historical/suspended freshness；
- typed origin 与 tool-call identity。

[历史代码确认] `5b7ad9f7:src/pulsara_agent/runtime/tool_executor.py::build_tool_observation_timing()` 从 Runtime-owned start/end 时刻构造 timing，只对白名单 Terminal producer采用 tool-reported duration；任意 tool body 中名为 `timing` 的字段不具有 authority。

[历史代码确认] `5b7ad9f7:src/pulsara_agent/runtime/context_input/render.py` 在 full、compact、artifact-locator 与 essential projection 中保留 bounded timing envelope。

Round 7吸收其“Runtime-owned elapsed + typed origin”意图，但不照搬可跨时钟误解的absolute
source start/end：model-visible absolute time只保留`observed_at`，elapsed使用monotonic测量；
需要的近似起点只能由二者在同一fact内派生。

历史实现需要拒绝的部分包括：

- EventLog start/end 是 timing 真源；
- suspended/resumed seed、receipt、projection state 或 replay reducer；
- cache/projection proof 决定 timing 是否成立；
- compaction 时回写旧 observation 的 freshness；
- tool business payload 自报任意 timing 即被信任。

### 1.4 当前 canonical truth

[当前代码确认] [clean-v0 baseline](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql) 已保存：

- `turns.status / terminal_reason / terminal_at`；
- `tool_execution_attempts.started_at`；
- `tool_results.result_state / accepted_at / result_entry_id / artifact fields`；
- transcript entry sequence、scope、turn 与 exact tool-call FK。

[当前代码确认] [reader.py](src/pulsara_agent/conversation_kernel/reader.py) 已在一个 `REPEATABLE READ` transaction 中冻结 canonical transcript、permission、Plan workflow/handoff 与 exact tool pairing。

[当前代码确认] reader 已正确区分：

~~~text
tool call + result at exact cut
    -> exact TOOL_RESULT

tool call + no attempt
    -> interrupted_before_dispatch closure

tool call + attempt + no result
    -> interrupted_may_have_partially_executed closure

result accepted after historical assistant cut
    -> typed late outcome observation
    -> never back-insert into that historical provider call
~~~

[当前代码确认] [compiler.py](src/pulsara_agent/model_input/compiler.py)、[continuity.py](src/pulsara_agent/model_input/continuity.py) 与 Host-owned epoch 已保证 installed canonical item 不重新 lower/degrade，dynamic source 只能追加 `VALUE | CLEARED | UNAVAILABLE` observation。

[当前代码确认] 当前中断 producer 至少包括：

| raw canonical reason | 当前 producer | 产品含义 |
|---|---|---|
| `FOREGROUND_EXECUTION_INTERRUPTED` | runner exception/cancellation | Runtime/provider/tool path 未正常完成；当前也错误吞并 user stop |
| `SESSION_CLOSED` | Host/session close | Host 生命周期结束 |
| `HOST_TAKEOVER` | writer claim/takeover | 旧 Host 失去 canonical authority |
| `PROVIDER_INPUT_PLAN_CONFLICT` | prompt/steer admission | canonical input planning conflict |
| `PROVIDER_INPUT_RESOURCE_EXHAUSTED` | bounded steer/resource path | 当前 provider-input resource boundary |
| `PLAN_CONTINUATION_NOT_BOUND` | Plan successor settlement | committed successor 未绑定 physical runner |
| `PLAN_FORCE_EXIT:<digest>` | explicit Plan force exit | intentional Plan transition；不得再发 generic failure note |

[当前代码确认] `KernelHostSession.stop_current_turn()` 只调用 `task.cancel()`；runner 将它与真正 failure 一并写成 `FOREGROUND_EXECUTION_INTERRUPTED`。Round 7 必须修正这个产品语义，但不新增 durable cancellation owner。

---

## 2. Scope 与 non-goals

### 2.1 Round 7 activation scope

必须完成：

- ROOT 与 SUBAGENT_TASK scope 的 immediate-predecessor outcome projection；
- explicit user stop 与 generic execution failure 的 canonical reason 区分；
- Host close、Host takeover、resource/input conflict、Plan successor failure 的 public normalization；
- accepted assistant prefix 与 unfinished tool disposition 的 bounded summary；
- tool result canonical `observed_at`、monotonic observation duration、immutable origin与trusted reported duration；
- tool result full/compact/ref-only/omitted-body/late-outcome统一 timing envelope；
- per-turn freshness frontier；
- 清除Pulsara-owned provider carrier中无模型语义的contract/version/fingerprint与重复内部identity，
  同时保留`lifecycle`、actionable opaque handles和external body；
- Round 3/3.1 compiler、continuity、planning quote 与 native adapter strict-prefix回归；
- clean-v0 reset-only schema与deep verifier更新；
- deterministic、PostgreSQL 与 real-provider dogfood evidence。

### 2.2 Non-goals

- Round 5B compaction、summary、snapshot adoption或prefix rebase；
- memory candidate、preference extraction或memory freshness；
- 自动判断网页、文件、MCP resource 的业务 TTL；
- 自动刷新“可能过期”的tool result；
- 跨 Host 恢复同一个 provider-input epoch；
- exact context-input durable audit；
- TUI/Inspector历史页面或新Protocol类型；
- 把 raw provider failure、HTTP body、exception traceback 暴露给模型；
- 对 interrupted attempt 自动重试；
- durable user-stop receipt、cancellation generation或recovery coordinator；
- 恢复 suspended/resumed timing seed；
- 用 event occurrence 替代 canonical row query。

### 2.3 语义owner不合并

~~~text
Previous-turn outcome owner
    canonical turns + transcript/tool rows
    -> reader freezes fact
    -> context source renders guidance

Tool observation timing owner
    canonical tool_results + tool_execution_attempts
    -> reader freezes immutable result metadata
    -> tool-result lowering renders envelope

Tool freshness owner
    current/predecessor scope frontier from canonical turns
    -> context source appends one turn boundary

Provider prefix owner
    existing HostProviderInputContinuityOwner
    -> installs only complete append candidate

Provider projection owner
    internal frozen facts + canonical rows
    -> narrow pure allowlisted renderer
    -> shared provider-neutral LLMContext
~~~

它们共用 one-cut reader 与 compiler，但不得产生一个新的“recovery/timing service”。

---

## 3. 核心语义模型

### 3.1 Canonical truth 与 read-time projection

Round 7 不持久化 provider wording。数据库只保存：

- turn 终局与 raw internal reason；
- accepted transcript entries；
- tool call、attempt、result、observation time与optional trusted duration。

以下都是 bounded read-time DTO：

- `FrozenPreviousTurnOutcomeCompileFact`；
- `FrozenToolObservationFreshnessCompileFact`；
- `FrozenToolObservationTimingFact`；
- model-visible JSON envelope。

它们不是第二套 durable projection，也不能被其他 consumer 的成功/失败证明。

### 3.2 Previous turn 的唯一选择规则

“previous turn”只能是：

1. 与当前 turn 属于同一 `session_id`；
2. `conversation_scope_kind` 与 `scope_subagent_task_id` exact equal；
3. 其 `initial_entry.entry_sequence` 小于当前 turn initial-entry sequence；
4. 在满足以上条件的 turn 中 initial-entry sequence 最大的一项。

禁止：

- 查找“最近一次失败”而跳过中间 successful turn；
- 按 `accepted_at` 猜顺序；
- 把 ROOT failure 注入 child scope，或反之；
- 把另一个 subagent task 的终局注入当前 child；
- 从 committed event sequence 重放决定 predecessor。

### 3.3 Freshness 不写回旧 result

每个 tool result 只保存 immutable observation facts：

~~~text
source_turn_ref
observed_at_utc
observation_duration_microseconds?
duration_disposition
tool_reported_duration_microseconds?
observation_origin
~~~

每个 turn 追加一次独立 frontier：

~~~text
current_turn_ref
immediate_predecessor_turn_ref?
~~~

模型按固定规则理解：

~~~text
result.source_turn_ref == current_turn_ref
    -> CURRENT_TURN

result.source_turn_ref == immediate_predecessor_turn_ref
    -> PREVIOUS_TURN_TAIL

otherwise
    -> HISTORICAL
~~~

绝对 age 由 result 的 `observed_at_utc` 与现有 `RUNTIME_CLOCK` 推导。若 clock source unavailable，timing仍成立，只是当前 age 不能精确计算。

这种分解有三个结果：

- result fingerprint永远不因进入下一 turn而变化；
- freshness变化只追加一个新 frontier；
- 未来 compaction只需重新物化最新frontier，不需要修改历史 result。

### 3.4 Provider-visible turn reference

不得直接把 internal turn ID 当作必要模型语义。统一使用：

~~~text
provider_turn_ref = context_fingerprint(
    "pulsara:provider-visible-turn-ref:v1",
    {"session_id": session_id, "turn_id": turn_id},
)
~~~

`source_turn_ref`、`current_turn_ref` 与 `immediate_predecessor_turn_ref`必须调用同一个 pure helper。它不成为 durable identity，也不进入 event metadata。

### 3.5 Internal contract 与provider projection必须分层

当前代码已经证明，仅靠“内部fact是frozen/closed”并不能阻止内部证明字段进入provider wire：

- `RuntimeObservation`的`contract_version`与`lifecycle`都被直接编码进user-role JSON；其中前者是
  应删除的内部版本证明，后者是必须保留的provider时域语义；
- environment/permission/Plan/clock body又重复携带`contract="...v1"`；
- tool closure、late outcome、Terminal observation与binary placeholder携带`schema_version`；
- MCP catalog reference/result携带`catalog_fingerprint`；
- approved Plan materialization marker携带content digest；
- 部分builtin descriptor使用“exact generation / argument contract / event stream”等Runtime实现术语。

Round 7冻结两个不同的closed carrier：

~~~text
InternalRuntimeObservation
    source kind
    trust class
    lifecycle
    presence
    source contract version/fingerprint
    semantic fingerprint
    body

ProviderRuntimeObservation
    source
    trust
    lifecycle
    presence
    body
~~~

唯一provider wire形状为：

~~~json
{
  "pulsara_runtime_observation": {
    "body": "{\"local_date\":\"2026-08-14\",\"timezone\":\"Asia/Shanghai\",\"utc_offset_minutes\":480}",
    "lifecycle": "CALL",
    "presence": "VALUE",
    "source": "RUNTIME_CLOCK",
    "trust": "TRUSTED_RUNTIME_FACT"
  }
}
~~~

内部carrier用于collector、allocator、source-head transition与fixed-point校验；provider carrier只用于
`LLMMessage`。`contract_version`、source fingerprint与generation不得为了从provider message反向恢复
内部registry而回到wire；continuity owner已经持有process-local source head，provider bytes不是内部
registry的恢复介质。`lifecycle`例外：它不是恢复介质，而是append-only observation的显式有效范围。

BASE_SYSTEM必须按以下closed语义解释provider-visible lifecycle；不得依赖source名称的隐式映射：

- `SNAPSHOT`：同source的较新`VALUE`取代较早current state；`CLEARED`或`UNAVAILABLE`终止旧current state；
- `TURN`：只服务其因果锚定的当前turn，不成为随后turn的current guidance；
- `CALL`：只服务紧随其后的单次model dispatch；
- `ACTIVATION`：只服务当前capability/skill activation，下一次activation snapshot或clear终止它；
- `ONE_SHOT`：只描述一次已经发生的transition，不可在后续turn中解释为仍然current的状态；
- `CLEARED`：同source先前current state已明确失效；
- `UNAVAILABLE`：本次无法提供可信current value，且先前current state不得继续沿用。它不表示产品事实为
  empty，也不授权模型猜测旧值。

closed lifecycle/presence矩阵为：`VALUE`只允许`SNAPSHOT | TURN | CALL | ACTIVATION | ONE_SHOT`；
`CLEARED`只允许`lifecycle=CLEARED`且body为空；`UNAVAILABLE`只允许
`lifecycle=UNAVAILABLE`且body为空。encoder、decoder、provider projector与golden tests必须共同验证该
矩阵，不能生成诸如`SNAPSHOT + CLEARED`的双解载体。

因此`PLAN_HANDOFF`即使作为历史suffix永久留在prefix中，`ONE_SHOT`也会明确告诉模型它只描述那次
APPROVE/REVISE/CANCEL/ENTER transition。后续`NOT_APPLICABLE`无需为了覆盖旧文本而制造虚假的
`CLEARED`。

provider仍可看到以下opaque product handle，因为它们可用于后续工具调用或跨message比较：

- `artifact_id`、Terminal `process_id`/`monitor_id`、subagent task ID、MCP `server_id`与opaque cursor；
- native `tool_call_id`；
- 本轮定义的`provider_turn_ref`。

它们与contract version/fingerprint不同：模型不需要解释其内部结构，但需要原样引用。JSON Schema的
`type`、`enum`、`minimum`、`maximum`等模型调用约束也不是内部contract metadata，不得误删。

禁止对任意user/tool/MCP remote body做字符串或key名regex scrub；外部内容可以合法包含
`schema_version`或`contract`。清理只发生在Pulsara自己拥有的renderer/projection中，tool body继续
作为escaped untrusted string承载。

---

## 4. Typed contracts

### 4.1 Turn interruption public taxonomy

~~~python
class PreviousTurnOutcomeKind(StrEnum):
    USER_STOPPED = "USER_STOPPED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    HOST_SESSION_CLOSED = "HOST_SESSION_CLOSED"
    HOST_REPLACED = "HOST_REPLACED"
    PROVIDER_INPUT_CONFLICT = "PROVIDER_INPUT_CONFLICT"
    RESOURCE_BOUNDARY = "RESOURCE_BOUNDARY"
    PLAN_CONTINUATION_FAILED = "PLAN_CONTINUATION_FAILED"
    UNKNOWN_INTERRUPTION = "UNKNOWN_INTERRUPTION"
~~~

Raw reason只在中央 projector 中解释：

| raw reason | public kind | guidance |
|---|---|---|
| `USER_STOPPED` | `USER_STOPPED` | 用户明确停止；继续前核对未闭合effect |
| `FOREGROUND_EXECUTION_INTERRUPTED` | `EXECUTION_FAILED` | Runtime/provider/tool execution 未正常完成 |
| `SESSION_CLOSED` | `HOST_SESSION_CLOSED` | Host生命周期结束，不冒充用户停止 |
| `HOST_TAKEOVER` | `HOST_REPLACED` | previous Host失去writer authority |
| `PROVIDER_INPUT_PLAN_CONFLICT` | `PROVIDER_INPUT_CONFLICT` | provider input planning未能继续 |
| `PROVIDER_INPUT_RESOURCE_EXHAUSTED` | `RESOURCE_BOUNDARY` | 当前输入或资源达到安全边界，未能继续 |
| `PLAN_CONTINUATION_NOT_BOUND` | `PLAN_CONTINUATION_FAILED` | committed successor没有继续执行 |
| unknown non-empty reason | `UNKNOWN_INTERRUPTION` | generic、脱敏说明 |
| `PLAN_FORCE_EXIT:*` | no generic fact | Plan handoff source拥有语义 |

`COMPLETED` turn永远不产生 outcome VALUE。

Raw reason不得直接进入 provider-visible body、diagnostic detail或hook public payload。

### 4.2 Previous-turn fact

~~~python
class AcceptedAssistantDisposition(StrEnum):
    NONE_ACCEPTED = "NONE_ACCEPTED"
    ACCEPTED_PREFIX_PRESENT = "ACCEPTED_PREFIX_PRESENT"


@dataclass(frozen=True, slots=True)
class FrozenPreviousTurnOutcomeCompileFact:
    session_id: str
    workspace_id: str
    current_turn_id: str
    current_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None

    predecessor_turn_id: str
    predecessor_initial_entry_sequence: int
    predecessor_terminal_at_utc: str
    outcome_kind: PreviousTurnOutcomeKind

    accepted_assistant_disposition: AcceptedAssistantDisposition
    accepted_assistant_entry_count: int
    definitely_not_dispatched_tool_count: int
    outcome_unknown_tool_count: int
    bounded_tool_name_samples: tuple[str, ...]

    user_input_preserved: Literal[True]
    canonical_entries_preserved: Literal[True]
    fact_fingerprint: str
~~~

约束：

- predecessor 必须 terminal 且 status=`INTERRUPTED`；
- counts 均 non-negative；
- samples最多3项、按 assistant block ordinal/entry sequence稳定排序、每项最多128 UTF-8 bytes；
- samples只来自存在 unresolved tool call 的 provider-visible tool name；
- Plan control interaction/tool不计入unfinished effect；
- `definitely_not_dispatched` = call存在、attempt不存在、result不存在；
- `outcome_unknown` = attempt存在、result不存在；
- 已有result，无论SUCCESS/ERROR/CANCELLED，都不属于unfinished；
- result是否“已有”必须以其canonical result entry为visibility fence：只有
  `result_entry.entry_sequence <= prepared_cut.provider_input_through_sequence`
  才能参与本次counts、samples与timing；cut之后的row在本次读取中等同absent，
  不是corruption；
- late accepted result只会在第一份覆盖该result entry sequence的后续cut中重新计算counts；
- assistant count来自accepted `ASSISTANT_MESSAGE | ASSISTANT_TOOL_REQUEST` entries，不读取live blocks；
- fingerprint覆盖全部字段，不覆盖rendered prose。

### 4.3 Tool freshness frontier fact

~~~python
@dataclass(frozen=True, slots=True)
class FrozenToolObservationFreshnessCompileFact:
    session_id: str
    workspace_id: str
    current_turn_id: str
    current_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    current_turn_ref: str
    current_initial_entry_sequence: int
    immediate_predecessor_turn_id: str | None
    immediate_predecessor_turn_ref: str | None
    classification_contract: Literal[
        "pulsara.tool-observation-freshness.v1"
    ]
    fact_fingerprint: str
~~~

它对每个合法 current turn 必须存在，包括该turn尚未产生tool result时。predecessor union exactly-one：两项 predecessor 字段同时为空或同时非空。
`classification_contract`只参与内部fact/fingerprint校验，不进入provider body。

### 4.4 Tool observation timing fact

~~~python
class ToolObservationDurationDisposition(StrEnum):
    MEASURED = "MEASURED"
    NO_PHYSICAL_ATTEMPT = "NO_PHYSICAL_ATTEMPT"
    MEASUREMENT_UNAVAILABLE = "MEASUREMENT_UNAVAILABLE"


class ToolObservationOrigin(StrEnum):
    BUILTIN = "BUILTIN"
    TERMINAL_PROCESS = "TERMINAL_PROCESS"
    MCP_REMOTE = "MCP_REMOTE"
    POLICY = "POLICY"
    PLAN_CONTROL = "PLAN_CONTROL"
    CUSTOM_OR_UNKNOWN = "CUSTOM_OR_UNKNOWN"


@dataclass(frozen=True, slots=True)
class FrozenToolObservationTimingFact:
    source_turn_ref: str
    observed_at_utc: str
    observation_duration_microseconds: int | None
    duration_disposition: ToolObservationDurationDisposition
    tool_reported_duration_microseconds: int | None
    observation_origin: ToolObservationOrigin
    fact_fingerprint: str
~~~

projection规则：

~~~text
result_origin_kind == PHYSICAL_ATTEMPT
and process-local physical invocation outcome carries monotonic elapsed
    -> MEASURED
    -> duration = frozen monotonic elapsed microseconds

result_origin_kind == PHYSICAL_ATTEMPT
and monotonic elapsed is unavailable
    -> MEASUREMENT_UNAVAILABLE
    -> duration = null
    -> preserve the known tool outcome; never retry or use wall-clock fallback

result_origin_kind in {POLICY_NO_ATTEMPT, PLAN_CONTROL}
    -> NO_PHYSICAL_ATTEMPT
    -> duration = null
~~~

`duration_disposition`由`result_origin_kind + observation_duration_microseconds nullability`
确定性派生，不新增重复的disposition column。

`attempt.started_at`来自PostgreSQL，`observed_at`来自Host；两者不得相减、比较先后或用于
duration disposition。数据库与Host的wall clock偏差、NTP跳变均不能改变elapsed duration。
physical attempt的monotonic measurement若因implementation defect缺失，已知tool outcome仍
不得被否定或重跑；投影为`MEASUREMENT_UNAVAILABLE`并产生bounded operational diagnostic。
architecture tests必须证明所有normal physical invocation owners都提供measurement；不得静默
退回wall-clock subtraction。

### 4.5 Canonical UTC 与duration encoding

所有model-visible timestamps调用同一个pure helper：

~~~text
timezone = UTC
timespec = microseconds
suffix   = Z

example:
2026-08-14T03:04:05.123456Z
~~~

duration只用non-negative integer microseconds，不使用binary float参与fingerprint或JSON。renderer可额外给出人类可读毫秒值，但canonical payload与golden vector必须以microseconds为准。

若UI确实需要展示近似起点，只能在同一个observation fact内部由
`observed_at_utc - observation_duration_microseconds`派生并明确标注为derived；不得展示或
比较PostgreSQL `attempt.started_at`来冒充Host物理调用起点。

### 4.6 Composite one-cut extension

现有 `FrozenCanonicalCompileSnapshot` 增加：

~~~python
previous_turn_outcome_fact: FrozenPreviousTurnOutcomeCompileFact | None
tool_observation_freshness_fact: FrozenToolObservationFreshnessCompileFact
~~~

`canonical_compile_snapshot_fingerprint()`必须覆盖二者。collector只能消费这两个frozen facts，禁止访问repository或connection。

这里的“one-cut”是一个closed composite，而不是声称两个数据库事务天然共享同一
PostgreSQL snapshot：

- `PreparedProviderInputCut`冻结transcript through-sequence；
- reader随后建立的单一RR transaction冻结该cut下允许读取的canonical row facts；
- 所有具有canonical entry的事实必须再通过entry sequence visibility fence；
- 没有entry sequence的attempt state只能在provider-safe-point读取，且该safe point期间
  不允许新的attempt admission。

composite fingerprint必须覆盖prepared cut identity与全部RR-derived facts；不得把RR开始时
已经更大的session head冒充为prepared cut的一部分。

---

## 5. Canonical schema 与事务契约

### 5.1 `tool_results` schema extension

clean-v0 baseline在现有relation增加：

~~~sql
observed_at timestamptz NOT NULL,
observation_duration_microseconds bigint,
observation_origin_kind text NOT NULL CHECK (observation_origin_kind IN (
    'BUILTIN', 'TERMINAL_PROCESS', 'MCP_REMOTE',
    'POLICY', 'PLAN_CONTROL', 'CUSTOM_OR_UNKNOWN'
)),
tool_reported_duration_microseconds bigint,

CHECK (
    observation_duration_microseconds IS NULL
    OR (
        result_origin_kind = 'PHYSICAL_ATTEMPT'
        AND observation_duration_microseconds >= 0
        AND observation_duration_microseconds <= 31536000000000
    )
),
CHECK (
    tool_reported_duration_microseconds IS NULL
    OR (
        result_origin_kind = 'PHYSICAL_ATTEMPT'
        AND tool_reported_duration_microseconds >= 0
        AND tool_reported_duration_microseconds <= 31536000000000
    )
),
CHECK (
    (result_origin_kind = 'POLICY_NO_ATTEMPT'
        AND observation_origin_kind = 'POLICY'
        AND observation_duration_microseconds IS NULL
        AND tool_reported_duration_microseconds IS NULL)
    OR
    (result_origin_kind = 'PLAN_CONTROL'
        AND observation_origin_kind = 'PLAN_CONTROL'
        AND observation_duration_microseconds IS NULL
        AND tool_reported_duration_microseconds IS NULL)
    OR
    (result_origin_kind = 'PHYSICAL_ATTEMPT'
        AND observation_origin_kind IN (
            'BUILTIN', 'TERMINAL_PROCESS', 'MCP_REMOTE', 'CUSTOM_OR_UNKNOWN'
        ))
)
~~~

`31_536_000_000_000µs`是一年，只是两个duration字段的typed integer/abuse bound，不是tool
watchdog或process lifetime承诺。

不得：

- 新增 `tool_result_observations` relation；
- 把 timing 放进自由 JSONB；
- 从 `agent_events.occurred_at`反查；
- 将 `accepted_at`重命名为observed；
- 让tool body提供 `observed_at`。
- 让reader根据当前tool registry、工具名或`mcp__`前缀重新分类origin。

### 5.2 Clock-domain 与duration contract

| 字段 | owner | 含义 |
|---|---|---|
| `tool_execution_attempts.started_at` | PostgreSQL attempt acceptance transaction | effect admission audit time；不是Host physical start clock |
| `tool_results.observed_at` | physical outcome owner or nonphysical result producer | Runtime取得并冻结exact result的UTC时刻 |
| `tool_results.observation_duration_microseconds` | process-local physical invocation owner | 同一Host monotonic clock上的elapsed；nonphysical或unavailable时null |
| `tool_results.accepted_at` | PostgreSQL | canonical row实际被数据库接受的时刻 |

physical invocation owner必须在进入exact adapter边界前冻结`monotonic_start`；在获得known
outcome或确定exception的同一个linearization point冻结UTC `observed_at`与同一monotonic
clock上的elapsed microseconds，并把两者装入process-local outcome carrier。artifact
preparation、live End、canonical write或confirmation不得后移该observed_at。carrier不包含
wall-clock start，也不持久化owner identity。

禁止进行以下算术：

~~~text
observed_at - attempt.started_at
accepted_at - attempt.started_at
accepted_at - observed_at  # 不代表physical duration
~~~

这避免把Host/PostgreSQL时钟偏差、NTP跳变、blob publication、confirmation/retry或数据库
排队误报为tool execution duration。

### 5.3 Prepared candidate extension

`PreparedToolResultAcceptance`增加：

~~~python
observed_at: datetime
observation_duration_microseconds: int | None
observation_origin_kind: ToolObservationOrigin
trusted_tool_reported_duration_microseconds: int | None
~~~

physical path在调用exact adapter前冻结一个窄carrier：

~~~python
@dataclass(frozen=True, slots=True)
class PhysicalToolObservationSupplement:
    observed_at: datetime
    elapsed_microseconds: int | None
    observation_origin_kind: ToolObservationOrigin
~~~

`observed_at + elapsed_microseconds`只由包围exact physical invocation的owner在同一outcome
boundary填写；physical `PreparedToolResultAcceptance.observed_at`必须exact复用该值；
`observation_origin_kind`由本次`PreparedToolExecutionBinding.execution_policy`、sealed executor
binding kind与producer kind中央投影。该carrier不序列化、不跨Host、不含transport capability。
policy/Plan producer不创建physical carrier，分别直接冻结`POLICY`/`PLAN_CONTROL`。

现有 `occurred_at` 与 `observed_at` 不得成为两个不同时间真源。最小实现应删除candidate中重复的自由`occurred_at`，或冻结：

~~~text
tool_result_occurrence.occurred_at == observed_at
~~~

并让candidate manifest/fingerprint覆盖：

- observed_at canonical UTC；
- process-local monotonic observation duration；
- exact immutable observation origin；
- trusted reported duration；
- 既有 IDs/result/artifact/side branch/event draft 全部字段。

ACK unknown exact-confirm必须校验：

- tool_results row 的 observed_at、observation duration、origin与reported duration；
- result entry/content；
- artifact edge；
- ToolResultAccepted event identity/occurred_at；
- memory side branch全有或全无。

NONE只允许重写同一个candidate；CONFLICT不得创建第二个result或重跑tool。

### 5.4 所有 tool-result producer 必须闭合

修改面至少包括当前 [tools repository module](src/pulsara_agent/conversation_kernel/_repository/tools.py) 与 [Plan repository module](src/pulsara_agent/conversation_kernel/_repository/plans.py) 的全部 `INSERT INTO pulsara_v3.tool_results`。

| producer | attempt | observed_at | monotonic duration | canonical origin | reported duration |
|---|---|---|---|---|---|
| physical builtin/Terminal/MCP result | required | frozen result observation time | physical owner measured or explicit unavailable | exact execution binding | trusted producer optional |
| permission deny / invalid args / unavailable | absent | policy result freeze time | null | `POLICY` | null |
| Plan control result | absent/Plan-controlled | Plan result freeze time | null | `PLAN_CONTROL` | null |
| late physical result | required | exact late observation time | original invocation owner measurement | original exact execution binding | trusted producer optional |

不得依赖column default补 observed_at/duration/origin。每个producer必须显式绑定同一candidate值。

### 5.5 Trusted tool-reported duration seam

`ToolExecutionResult.metadata`是tool-owned通用JSON，不得直接成为duration authority。

新增窄的process-local carrier，例如：

~~~python
@dataclass(frozen=True, slots=True)
class TrustedToolObservationSupplement:
    duration_microseconds: int | None
~~~

规则：

- 只有`DirectKernelToolPort`与sealed first-party executor binding能创建/采用；
- Terminal manager只可从exact、唯一target process state提供duration；
- arbitrary builtin output JSON、MCP response content/annotations、custom tool metadata均不得提升；
- reported duration absent时仍保留physical owner的monotonic observation duration；
- supplemental duration不改变result state、effect semantics或retry policy。

Terminal action必须服从closed matrix：

| action shape | trusted process duration |
|---|---|
| `terminal` exact launched process | optional sealed supplement |
| `terminal_process` exact single-process `log/poll/wait` observation | optional sealed supplement；只描述该exact process在此次observation时可证明的duration |
| `terminal_process write/submit/close_stdin/kill` control | null |
| `terminal_process list`或任何multi-process aggregate | null |
| `terminal_monitor register/list/cancel` | null |
| 纯registration、subscription、cursor或control result | null |

不得任选一个process、取max/avg，或把control action latency描述为process lifetime。matrix
返回null时，不影响独立的monotonic observation duration。

### 5.6 Reset-only migration universe

当前只有clean-v0 migration universe；本轮不得添加`0001`增量migration。

必须：

- 修改 `0000_conversation_kernel_baseline.sql`；
- 更新 catalog/grant/contract fingerprints 与 golden vectors；
- fresh empty DB install成功；
- repeat migrate为no-op；
- 旧universe/baseline返回typed `RESET_REQUIRED`且DDL count=0；
- deep verify覆盖column type/nullability/check；
- activation使用可删除的ephemeral DB或用户明确允许reset的开发DB。

---

## 6. Reader：Prepared transcript cut 与单一 RR hydration snapshot

### 6.1 One-cut 读取顺序

`PreparedProviderInputCut`的freeze transaction与reader transaction当前是两个独立事务；
`REPEATABLE READ`不会把后一个事务自动变成前一个事务的snapshot。Round 7必须保持这个
事实清晰，而不是用“同一RR cut”掩盖它。

在现有 `CanonicalProviderInputReader.read_frozen_compile_snapshot()` 的单个
`REPEATABLE READ` hydration transaction中：

1. 锁定/验证current turn binding与prepared provider-input cut；
2. 读取current initial entry与scope；
3. 选出exact immediate predecessor；
4. 读取predecessor terminal outcome与以prepared cut围住、output/deadline-bounded的aggregate；
5. 读取canonical transcript items；
6. 读取以canonical result entry围住的observed/duration/origin columns与attempt existence；
7. 构造permission/Plan/outcome/freshness composite；
8. 计算覆盖prepared cut与RR-derived facts的单一composite fingerprint；
9. 关闭transaction后返回frozen carrier。

禁止collector二次查询turn outcome，也禁止tool lowering二次查询timing。

exact visibility contract：

~~~text
transcript entry visible
    iff entry_sequence <= prepared_cut.provider_input_through_sequence

tool result visible to this composite
    iff its canonical result entry exists
   and result_entry.entry_sequence <= prepared_cut.provider_input_through_sequence

tool result row exists but result entry is after prepared cut
    -> treat result as ABSENT for this read
    -> do not hydrate timing
    -> do not decrement unfinished counts
    -> do not emit correction
    -> not corruption
~~~

`tool_execution_attempts`没有entry sequence，禁止用`ToolAttemptAccepted` event补一个
replay cursor。其read-time合法性依赖既有provider-safe-point：freeze/read期间不得接纳新的
attempt；已在cut中的assistant tool request，其attempt状态在该safe point是稳定canonical
state。若这个safe-point invariant无法证明，provider open count必须为0，而不是从event补真值。

### 6.2 Predecessor SQL contract

不得把全部`turns` join到initial entries后排序。必须复用现有
`idx_pulsara_v3_entries_session_scope_sequence`做反向index scan，同时只接受由
`turns.initial_entry_id`证明为initial entry的candidate：

~~~sql
SELECT e.turn_id, e.entry_sequence
FROM pulsara_v3.transcript_entries AS e
JOIN pulsara_v3.turns AS t
  ON t.session_id = e.session_id
 AND t.id = e.turn_id
 AND t.initial_entry_id = e.id
WHERE e.session_id = :session_id
  AND e.conversation_scope_kind = :scope_kind
  AND e.scope_subagent_task_id IS NOT DISTINCT FROM :scope_task_id
  AND e.entry_sequence < :current_initial_entry_sequence
ORDER BY e.entry_sequence DESC
LIMIT 1;

then:
    load exact predecessor turn by returned turn_id
    revalidate same session/scope and returned initial sequence
~~~

禁止先取上一条任意exact-scope entry再直接采用其`turn_id`：旧turn的late result可能拥有
比真正predecessor更大的entry sequence。只有`e.id == t.initial_entry_id`的entry参与排序；
因此结果仍严格等价于“最大initial-entry sequence小于current initial”的定义。

该plan可能反向跳过一个很长predecessor turn中的普通entries，因此只承诺index path、
output bound与DB deadline，不承诺constant scanned rows；这正是无需新增column/index时的
克制边界。

不要求实际SQL逐字相同，但必须有测试证明：

- ROOT/child isolation；
- two child task isolation；
- accepted_at乱序不影响选择；
- older turn late result排在newer turn之后时仍选择newer initial entry；
- successful immediate predecessor遮蔽更早failure；
- 不存在predecessor时fact absent/frontier predecessor null；
- 大量历史turn下使用exact-scope sequence index反向取一项，不全量sort/scan `turns`。

### 6.3 Output-bounded failure aggregation under a DB deadline

聚合使用SQL counts与最多3项stable sample，不把整个predecessor transcript复制进fact。
这里的“bounded”只承诺输出大小与foreground canonical DB deadline有界，不虚假承诺输入
扫描行数恒定：一个合法predecessor turn本身可以非常长。

物理边界：

~~~text
assistant count                 integer aggregate
unresolved call counts          integer aggregate
tool name samples               <= 3
single sample UTF-8             <= 128 bytes
rendered FULL note              <= 4 KiB
rendered COMPACT note           <= 1536 bytes
~~~

query必须以exact predecessor turn/session/scope为predicate，并尽量使用现有turn/entry/call
indexes；不得为Round 7新增predecessor projection、summary table或durable cursor。若deadline
到期、canonical row违反FK/closed state或sample无法形成UTF-8-safe投影，reader fail closed，
provider open count=0。普通“没有失败”不是error，返回`None`。

### 6.4 Tool-state query extension

现有 `_load_tool_state()`必须接收exact prepared cut或其
`provider_input_through_sequence`，join增加：

~~~text
b.tool_name
r.result_origin_kind
r.observed_at
r.observation_duration_microseconds
r.observation_origin_kind
r.accepted_at
r.tool_reported_duration_microseconds
result entry turn/scope/sequence
~~~

必须建立唯一pure visibility helper，例如：

~~~python
visible_tool_result_at_cut(
    result_row,
    *,
    provider_input_through_sequence: int,
) -> VisibleToolResult | None
~~~

ordinary result、late outcome、predecessor unfinished aggregate与timing hydration全部复用该helper：

- overall prepared cut之后的result一律返回`None`；
- overall cut内、但晚于historical assistant attribution cut的result可成为typed late outcome；
- 只有helper返回visible result时，`_tool_result_metadata()`才构造immutable timing fact；
- query可在SQL中提前过滤，也可在bounded row hydration后过滤，但不得让裸`result exists`
  参与任何fact。

这项规则必须同时覆盖ROOT与SUBAGENT_TASK scope，并验证result entry与call的same-session、
same-scope canonical join。

### 6.5 No event replay

reader不得查询：

- ToolAttemptAccepted event来证明attempt；
- ToolResultAccepted event来取得observed time；
- TurnInterrupted event来取得reason；
- event suffix来判断previous turn。

这些occurrence仍服务audit/hook/TUI，但canonical rows已经是model-input truth。

---

## 7. Previous-turn guidance source

### 7.1 新 ContextSourceKind

~~~text
PREVIOUS_TURN_OUTCOME
internal contract    pulsara.previous-turn-outcome.v1
channel              RUNTIME_OBSERVATION
trust                AUTHORIZED_RUNTIME_GUIDANCE
budget               MUST_KEEP
placement            45
degradation priority 10
modes                 FULL | COMPACT
lifecycle             TURN_APPEND
absence               EXPLICIT_EMPTY
~~~

`EXPLICIT_EMPTY`很重要：若epoch里曾安装failure VALUE，而紧邻predecessor后来是successful或intentional Plan transition，compiler必须追加一次CLEARED observation；不能让旧note继续适用。

### 7.2 Provider wording contract

FULL与COMPACT都由typed fields确定性渲染，不保存prose。建议body使用canonical JSON：

~~~json
{
  "outcome": "EXECUTION_FAILED",
  "user_input_preserved": true,
  "canonical_entries_preserved": true,
  "accepted_assistant_disposition": "ACCEPTED_PREFIX_PRESENT",
  "accepted_assistant_entry_count": 2,
  "definitely_not_dispatched_tool_count": 1,
  "outcome_unknown_tool_count": 1,
  "bounded_tool_name_samples": ["read_file", "terminal"],
  "guidance": "The previous turn ended before task completion. Continue from preserved canonical input if requested. Do not assume an attempted tool without a result failed or succeeded; verify before retrying."
}
~~~

`pulsara.previous-turn-outcome.v1`只属于source registry、fact fingerprint、validator与golden
fixture，不进入body。outer `ProviderRuntimeObservation.source=PREVIOUS_TURN_OUTCOME`已经提供唯一
类型判别；重复`schema_version`不会增加模型可用语义。

不同outcome只改变closed、reviewed guidance。不得拼接raw reason。

### 7.3 Assistant content wording

新架构必须使用：

~~~text
NONE_ACCEPTED
    No complete assistant message from the previous turn was accepted.

ACCEPTED_PREFIX_PRESENT
    Accepted assistant messages are complete canonical entries,
    but they may represent only an incomplete task trajectory.
~~~

禁止继续照抄旧文案“canonical assistant text may be partial”。只有Live delta可能partial，Host crash后它不进入历史。

### 7.4 Unfinished effect wording

~~~text
definitely_not_dispatched_tool_count > 0
    These calls had no accepted physical attempt and were not dispatched.

outcome_unknown_tool_count > 0
    These calls had an accepted attempt but no accepted result.
    Their physical outcome is unknown. Do not automatically retry.
~~~

tool call closure仍承担provider wire pairing；previous-turn note只给turn-level解释。二者同时存在不是重复authority。

### 7.5 Successful successor 与late result

- 当前 turn 的 immediate predecessor `COMPLETED`：source absent EXPLICIT_EMPTY；若旧head是VALUE则append CLEARED。
- interrupted predecessor后来接受late exact result：旧prepared cut继续得到原fact；第一份
  `provider_input_through_sequence >= result_entry.entry_sequence`的新cut才允许fact fingerprint
  变化，并在同一current turn append新的VALUE replacement、令unknown count下降。
- late result不得删除旧observation；replacement以append-only suffix纠正当前head。
- newer successful turn成为immediate predecessor后，更早failure不再重新出现。

---

## 8. User stop 与 interruption producer

### 8.1 Per-exact-turn process-local cancellation cause

新增一个Host-owned、process-local、非序列化carrier：

~~~python
class ForegroundCancellationCause(StrEnum):
    USER_REQUEST = "USER_REQUEST"
    HOST_SESSION_CLOSE = "HOST_SESSION_CLOSE"


@dataclass(slots=True)
class ActiveTurnCancellationIntent:
    turn_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    cause: ForegroundCancellationCause | None = None
~~~

每个正在执行的exact turn必须有一个且仅一个intent；它不是session-wide mutable cause：

- ROOT Host slot安装ROOT turn intent；
- automatic Plan continuation在同一Host lock内把`_active_turn_id`切到successor时，必须同时
  退役origin intent并安装绑定successor turn的新intent；不得让successor沿用origin identity；
- ROOT initial runner与每次successor `run_accepted_turn()`必须接收slot中同一个exact intent；
- 每个child `_LiveTask`持有自己的SUBAGENT_TASK intent，并把同一个对象传给child runner；
- 只有拥有该task slot的Host/child manager可在lock内set-once cause；
- runner只读取传入carrier，不持有callback/coordinator，也不得自行猜cause。

intent的`turn_id + scope union`必须与runner当前exact turn一致；identity mismatch在任何
canonical settlement前fail closed。

### 8.2 Stop/close/failure矩阵

| path | cancel intent | canonical terminal reason |
|---|---|---|
| ROOT user Stop command | exact ROOT intent=`USER_REQUEST`, then `task.cancel()` | `USER_STOPPED` |
| child `stop_agent` | exact child intent=`USER_REQUEST`, then child task cancel | `USER_STOPPED` |
| ROOT/child Host close | each exact intent=`HOST_SESSION_CLOSE`, then physical close/cancel | `SESSION_CLOSED` |
| ordinary exception/provider failure | none | `FOREGROUND_EXECUTION_INTERRUPTED` |
| stale writer/takeover | DB winner | `HOST_TAKEOVER` |
| atomic steer resource rejection | existing transaction | `PROVIDER_INPUT_RESOURCE_EXHAUSTED` |

Host close/`stop_agent`不得先cancel再补cause。cause install与task identity必须在各自owner
lock临界区完成；set-once loser采用已经安装的cause，不得覆盖。

### 8.3 Canonical settlement confirmation

当前 `interrupt_turn()` ACK unknown后不能只检查“turn已terminal”。新增或扩展stateless read：

~~~python
read_turn_terminal_outcome(session_id, turn_id)
    -> RUNNING
     | INTERRUPTED(reason, terminal_at)
     | COMPLETED
     | ABSENT
~~~

settlement规则：

- FULL exact reason：成功；
- RUNNING under current writer：重写同一个semantic interruption candidate；
- INTERRUPTED different reason：采用canonical winner，不覆盖；
- COMPLETED：采用completed winner，不再注入interruption；
- stale writer：停止写，由new writer/DB canonical状态决定；
- 不得创建durable settlement receipt。

### 8.4 Child scope

当前child manager已经有显式`stop_agent`与Host close原因；Round 7必须接通，而不是继续用
generic runner failure覆盖它：

1. `_LiveTask`在child turn开始前安装exact child intent；
2. `run_subagent_turn()`接收同一个intent；
3. `stop_agent`在manager lock内set-once `USER_REQUEST`，然后cancel；
4. child Host close在manager lock内set-once `HOST_SESSION_CLOSE`，然后cancel；
5. child runner发现explicit cancellation intent后不得独立调用`interrupt_turn()`；它把结算权
   交还持有`_LiveTask`的manager；
6. manager调用一个Host-writer atomic transaction，同时终结exact child turn与
   `subagent_tasks`，并append既有`TurnInterrupted`与`SubagentTaskStatusAccepted`；
7. canonical child turn与`subagent_tasks`不得分别表达generic failure和user/Host cancellation。

同一cause的closed projection为：

| process-local cause | child turn | `subagent_tasks` |
|---|---|---|
| `USER_REQUEST` | `INTERRUPTED / USER_STOPPED` | `CANCELLED / USER_CANCELLED` |
| `HOST_SESSION_CLOSE` | `INTERRUPTED / SESSION_CLOSED` | `INTERRUPTED / HOST_CLOSING` |

atomic repository contract必须：

~~~text
settle_cancelled_subagent_turn_and_task(
    exact task_id,
    exact child turn_id,
    exact scope SUBAGENT_TASK(task_id),
    set-once cause,
    stable occurred_at/event drafts,
)
    lock/revalidate subagent task ACTIVE and owned by current writer
    lock/revalidate child turn RUNNING and exact scope
    abort any OPEN child QUESTION interaction using existing rule
    update child turn terminal status/reason
    update subagent task terminal status/reason
    append existing TurnInterrupted
    append existing SubagentTaskStatusAccepted
    commit once
~~~

ACK unknown必须通过stable candidate exact-confirm两行与两个existing occurrences全有且exact；
不得退化回两个事务。事务前进程退出时，replacement Host沿既有takeover路径统一收口；事务
FULL后进程退出时，两项canonical truth已经一起成立。无需新event type、relation、receipt、
repair或recovery owner。

`stop_agent`也可能早于child turn admission FULL。manager必须先等待/确认既有Host-owned
child admission attempt：

- admission FULL且turn RUNNING：使用上述双row atomic transaction；
- admission confirmed NONE且intent已set：只终结task并append既有task status occurrence，
  不伪造`TurnInterrupted`；
- turn已经COMPLETED：completed canonical winner胜出，取消不得覆盖；
- admission CONFLICT或writer stale：采用canonical winner/takeover，不启动第二个turn。

这只是复用existing admission settlement，不新增child cancellation generation。

这只是现有`_LiveTask`的窄process-local字段，不是child bus、durable cancellation state或
recovery owner。

---

## 9. Tool observation timing 与model-visible envelope

### 9.1 ProviderToolResultContextMetadata extension

现有metadata增加：

~~~python
timing: FrozenToolObservationTimingFact
~~~

`provider_input_item_fingerprint()`必须覆盖timing fact全部字段。它们均来自immutable canonical rows，因此同一item在后续read中保持相等。

### 9.2 Observation origin projection

origin在result candidate preparation时由exact producer/execution binding一次冻结；reader只读
`tool_results.observation_origin_kind`，不得按当前binary registry重新分类：

~~~text
policy result producer
    -> POLICY

Plan control producer
    -> PLAN_CONTROL

physical + exact sealed Terminal executor binding
    -> TERMINAL_PROCESS

physical + MCP generation-bound execution policy/binding
    -> MCP_REMOTE

physical + exact first-party builtin execution binding
    -> BUILTIN

physical + admitted custom/other sealed binding
    -> CUSTOM_OR_UNKNOWN
~~~

`PreparedToolExecutionBinding.execution_policy`与sealed executor kind是accept-time input；
provider-facing tool name、`mcp__`前缀、当前builtin registry、MCP annotation、tool description或
output JSON都不能决定或改变origin。binary replacement、tool退役/改名、reopen与未来compaction
必须读取同一个immutable stored value。

### 9.3 Closed outer envelope

所有tool-result representation都使用同一个outer JSON codec：

~~~json
{
  "pulsara_tool_result": {
    "body": "exact selected FULL/COMPACT/REF_ONLY/OMITTED body",
    "observation": {
      "source_turn_ref": "sha256:...",
      "observed_at_utc": "2026-08-14T03:04:05.123456Z",
      "observation_duration_microseconds": 2123456,
      "duration_disposition": "MEASURED",
      "tool_reported_duration_microseconds": 2000000,
      "observation_origin": "TERMINAL_PROCESS"
    },
    "result_state": "SUCCESS"
  }
}
~~~

`body`必须作为JSON string编码。即使tool output包含closing marker、另一个`pulsara_tool_result`对象、ESC/OSC或名为`timing`的字段，也只能留在body字符串内，不能覆盖outer observation。

encoder/decoder必须通过fixed-point golden vectors。

### 9.4 Native tool grouping

- ordinary result仍lower为provider-native tool-role/tool-result item，tool_call_id不变；
- outer JSON只是该native result的content；
- Chat Completions与Responses不得把result改成普通user message；
- late outcome仍是typed user-role observation，但复用同一个inner timing projection；
- closure不是known result，不伪造observed_at/duration。

### 9.5 FULL/COMPACT/REF_ONLY/OMITTED contract

Timing是essential metadata：

- FULL body可以退化为COMPACT；
- COMPACT可退化为artifact ref；
- body可被OMITTED；
- timing envelope不得被省略；
- tool result decision/estimator必须计入outer envelope bytes/tokens；
- metadata本身若违反hard bound，compile typed fail，provider open=0。

固定物理边界：

~~~text
tool observation metadata envelope <= 2 KiB UTF-8
timestamps                         fixed canonical size
turn refs                          fixed SHA-256 form
tool reported duration             optional bounded int64
~~~

### 9.6 Freshness source

新增：

~~~text
TOOL_OBSERVATION_FRESHNESS
internal contract    pulsara.tool-observation-freshness.v1
channel              RUNTIME_OBSERVATION
trust                TRUSTED_RUNTIME_FACT
budget               MUST_KEEP
placement            70
degradation priority 10
modes                 FULL
lifecycle             TURN_APPEND
absence               forbidden
~~~

rendered body只包含`current_turn_ref`与optional `immediate_predecessor_turn_ref`，不携带
`classification_contract`、source contract version/fingerprint或历史result列表。分类算法由Pulsara
内部lowering contract冻结，模型只需按两个ref比较。

### 9.7 Same-turn tool loop

初次model call在trigger前安装turn freshness frontier。后续tool loop：

~~~text
installed prefix
|| assistant tool request
|| newly accepted tool result envelope(s)
|| later runtime observations if any
~~~

同一turn不重复frontier。新result的`source_turn_ref == current_turn_ref`，模型可判定CURRENT_TURN。

### 9.8 New turn

新turn只追加：

~~~text
old installed prefix
|| canonical delta before trigger
|| PREVIOUS_TURN_OUTCOME VALUE/CLEARED if required
|| other changed/turn sources
|| TOOL_OBSERVATION_FRESHNESS for new turn
|| RUNTIME_CLOCK
|| exact request-like trigger
~~~

旧tool result message不变；新frontier使其成为PREVIOUS_TURN_TAIL或HISTORICAL。

---

## 10. Compiler、continuity 与版本边界

### 10.1 Closed source registry更新

同步修改：

- `ContextSourceKind`；
- `ContextSourceRegistry._BINDINGS`；
- compiler `_SOURCE_POLICY`；
- `_SOURCE_ABSENCE_POLICY`；
- exactly-one VALUE/ABSENT validator；
- source count/registry fingerprints；
- public diagnostics allowlist（只在必要时增加closed code）。

每次collection必须：

~~~text
PREVIOUS_TURN_OUTCOME
    exactly one candidate or EXPLICIT_EMPTY

TOOL_OBSERVATION_FRESHNESS
    exactly one candidate
~~~

### 10.2 Provider-visible projection清理

Round 7 activation必须在实际最终`LLMContext`与provider adapter payload上完成下列清理，而不是只改
内部dataclass名称：

| Pulsara-owned carrier | 当前provider可见冗余 | Round 7 provider projection |
|---|---|---|
| generic runtime observation | `contract` | exact keys仅`source/trust/lifecycle/presence/body`；lifecycle是模型可见时域语义 |
| runtime environment | XML `contract` wrapper，重复source identity | semantic environment JSON；FULL可另含`relative_workdir_base` |
| run permission | XML `contract` wrapper | requested/effective mode、overlay与实际filesystem/terminal/approval policy，加固定不可扩权提示 |
| Plan workflow/handoff source | XML `contract` wrapper | status/transition/resume mode与固定workflow guidance；无workflow/interaction UUID |
| runtime clock | XML `contract`与重复authority文字 | local date、timezone、offset、optional observed UTC |
| tool-result closure | `schema_version`及content内重复`tool_call_id` | native tool-result pairing保持；body仅closed `disposition`或固定Plan-abort文字 |
| late tool outcome | `schema_version` | 保留`tool_call_id/result_state/result`与本轮observation；它是user-role carrier，call identity不可省略 |
| Terminal observation | canonical `schema_version`、`host_scoped`、observation ID/ordinal | 保留actionable `process_id/monitor_id`、status/exit/output、gap与coverage；内部canonical validator字段不透传 |
| Plan continuation/result | workflow/interaction/entry/tool IDs、request/content digest与size | 保留transition、human feedback/answer及exact approved plan；native pairing已提供call identity |
| approved Plan marker | `digest=...` | 只保留明确的untrusted approved-plan边界与exact正文 |
| non-UTF-8 placeholder | `schema_version`与content digest | `kind=binary_content`、codec与size；integrity digest留在internal content identity |
| MCP catalog source/result | 重复`source/trust`、`catalog_fingerprint`、`exact execution generation`措辞 | server status/count/name/instructions与permission note；fingerprint只可封装在必须回传的opaque cursor中 |
| builtin tool descriptors | `exact generation/contract/event stream/canonical durable`等实现术语 | 只描述用户可观察行为、输入、输出、边界与下一步操作 |
| current memory tool projection | `semantic_digest`、raw desired/applied generation | 保留memory payload/ID与closed freshness disposition/reason；本轮不重设计memory authority |

Plan durable row若已有typed workflow/interaction/content-identity columns，writer应停止在canonical textual
body中复制这些内部identity；confirmation改为同时验证typed columns与lean semantic body。Terminal
observation可继续在canonical content中保存其closed internal schema，但reader/lowering必须通过pure
projector生成lean provider view。两种做法都只有一个canonical truth，不建立第二张表或durable
projection。

Plan provider projection不得继续使用`[RUNTIME_PLAN_CONTINUATION]`、
`[UNTRUSTED_APPROVED_PLAN]`或任何可由正文伪造的closing marker。统一使用canonical JSON
（UTF-8、sorted keys、compact separators），并冻结以下closed union；这些是provider DTO，不是
durable contract/version字段：

~~~text
ProviderPlanContinuation =
    EnteredPlan {
        transition: "ENTERED_PLAN",
        status: "ACTIVE"
    }
  | RevisionRequested {
        transition: "REVISION_REQUESTED",
        status: "ACTIVE",
        feedback: Absent | Present{text: string}
    }
  | ApprovedPlan {
        transition: "APPROVED_PLAN",
        status: "APPROVED",
        approved_plan: string
    }

Absent  = {"presence":"ABSENT"}
Present = {"presence":"PRESENT","text":<exact UTF-8 string>}
~~~

wire的唯一outer key为`pulsara_plan_continuation`。`approved_plan`与`feedback.text`必须是JSON string，
不得被拼接成delimiter正文；其中的`[/RUNTIME_PLAN_CONTINUATION]`、引号、换行、ESC/OSC或任意
类似marker只能作为escaped string data存在。human-authored内容可以表达用户意图，但不能扩大已冻结的
permission/tool authority。

例如ENTER的exact wire为
`{"pulsara_plan_continuation":{"status":"ACTIVE","transition":"ENTERED_PLAN"}}`；APPROVE只在同一
object中增加`approved_plan` string。除对应union branch列出的keys外不得出现额外member。

`CANCELLED_PLAN | FORCE_EXITED_PLAN`不伪装成`PLAN_CONTINUATION` entry；它们继续通过
`PLAN_HANDOFF`（internal source contract `pulsara.plan-handoff.v2`）的typed transition/status表达，并受
`lifecycle=ONE_SHOT`约束。

Plan native tool-result也必须由closed projector生成，不透传canonical storage JSON：

~~~text
ProviderPlanToolResult =
    {status:"success", plan_control:"ENTERED_PLAN"|"PLAN_ALREADY_ACTIVE"}
  | {status:"success", plan_control:"DRAFT_SUBMITTED_FOR_REVIEW"}
  | {status:"success", plan_control:"QUESTION_ANSWERED",
     answer:{kind:"OPTION", ordinal:int, label:string}}
  | {status:"success", plan_control:"QUESTION_ANSWERED",
     answer:{kind:"FREE_TEXT", text:string}}
  | existing closed rejection/cancel result
~~~

workflow/interaction/entry/tool UUID、request/content digest、size与descriptor binding仍只参与canonical
confirmation。native tool call/result pairing已经给出call identity，provider DTO不得复制这些字段。
continuation projector必须从typed handoff/status columns与既有central plan-content extractor取得字段；不得
从canonical textual body猜transition、重新解析legacy marker，或用storage JSON序列化结果直接充当wire。

MCP cursor可以在opaque token内部绑定catalog fingerprint以拒绝stale page，但不得再把同一fingerprint
作为顶层结果字段或prompt source正文展示。MCP server返回的remote body不属于Pulsara-owned carrier，
不得因其中存在协议字段而被删除。

建议统一实现窄pure renderer，而不是让adapter自行删字段：

~~~python
project_runtime_observation_for_provider(internal) -> ProviderRuntimeObservation
project_terminal_observation_for_provider(canonical) -> FrozenJsonObjectFact
project_plan_carrier_for_provider(canonical_fact) -> FrozenJsonObjectFact
project_binary_content_for_provider(content_fact) -> str
~~~

每个projector拥有exact output-key allowlist与canonical golden vector。Chat Completions与Responses复用
同一provider-neutral结果；adapter不得重新加入internal metadata。

### 10.3 Compiler/lowering contract bump

全局compiler/lowering identity不能替代source-specific renderer identity。仅generic outer envelope变化、
source body不变时由全局lowering contract承载；source自己的body shape或BASE_SYSTEM语义发生变化时，
必须同时升级该source contract。Round 7冻结以下矩阵：

| source | current source contract | Round 7 source contract | 原因 | collector implementation contract |
|---|---|---|---|---|
| `BASE_SYSTEM` | `pulsara.base-system.prefix-continuity.v2` | `pulsara.base-system.prefix-continuity.v3` | 补全`ACTIVATION`、`CLEARED`、`UNAVAILABLE`及五字段observation解释 | `pulsara.base-system-collector.v1`不变 |
| `RUNTIME_ENVIRONMENT` | `pulsara.runtime-environment.v1` | `pulsara.runtime-environment.v2` | XML/contract wrapper改为lean canonical JSON | `pulsara.runtime-environment-collector.v1`不变 |
| `RUNTIME_CLOCK` | `pulsara.runtime-clock.v1` | `pulsara.runtime-clock.v2` | XML/authority wrapper改为lean canonical JSON | `pulsara.runtime-clock-collector.v1`不变 |
| `RUN_PERMISSION` | `pulsara.run-permission.v1` | `pulsara.run-permission.v2` | XML/contract wrapper改为closed semantic JSON | `pulsara.run-permission-collector.v1`不变 |
| `PLAN_HANDOFF` | `pulsara.plan-handoff.v1` | `pulsara.plan-handoff.v2` | XML/contract wrapper改为closed transition JSON | `pulsara.plan-handoff-collector.v1`不变 |
| `PLAN_WORKFLOW` | `pulsara.plan-workflow.v1` | `pulsara.plan-workflow.v2` | XML/contract wrapper改为closed workflow JSON | `pulsara.plan-workflow-collector.v1`不变 |
| `CAPABILITY_CATALOG` | `pulsara.capability-catalog.v1` | `pulsara.capability-catalog.v2` | builtin descriptor正文移除Runtime实现术语 | `pulsara.capability-catalog-collector.v1`不变 |
| `MCP_CATALOG` | `pulsara.mcp-catalog.v1` | `pulsara.mcp-catalog.v2` | 删除重复source/trust、顶层fingerprint和generation措辞 | `pulsara.mcp-catalog-collector.v1`不变 |
| `ACTIVE_SKILL` | `pulsara.active-skill.v1` | 不变 | source body未改变；只继承global outer codec | `pulsara.active-skill-collector.v1`不变 |
| `PREVIOUS_TURN_OUTCOME` | none | `pulsara.previous-turn-outcome.v1` | Round 7新source | Round 7新collector v1 |
| `TOOL_OBSERVATION_FRESHNESS` | none | `pulsara.tool-observation-freshness.v1` | Round 7新source | Round 7新collector v1 |

collector implementation contract只在采集算法、事实来源或归一化逻辑变化时升级；本表中的既有source只
改变provider renderer，因此implementation contract保持v1。registry、compiler `_SOURCE_POLICY`、
golden vectors与source-count fingerprint必须同时采用本表，禁止出现“新renderer仍自称旧v1”的混合态。

此外provider-visible tool-result、Plan carrier与generic runtime observation codec发生变化，因此：

~~~text
COMPILER_CONTRACT_VERSION
    pulsara.structured-model-input-compiler.prefix-continuity.v3

PROVIDER_MESSAGE_LOWERING_CONTRACT
    pulsara.provider-message-lowering.prefix-continuity.v2
~~~

activation后首次Host使用cold epoch。禁止在一个已安装epoch中悄悄切换codec。

### 10.4 Strict-prefix proof

至少对Chat Completions与Responses证明：

~~~text
call n:
    system_n
    tools_n
    messages_n

call n+1:
    system_n
    tools_n
    messages_n || exact suffix
~~~

suffix可含：

- newly accepted canonical item；
- previous outcome VALUE/CLEARED；
- new turn freshness frontier；
- call clock；
- 既有Round 3/4/6 runtime sources。

不得因：

- 上一turn从CURRENT变HISTORICAL；
- clock推进；
- late result使unknown count变化；
- tool reported duration现在可用；
- MCP physical reconnect；

而重写任何旧message。

### 10.5 Installed tool-result protection

Round 3.1已有规则继续成立：

- installed canonical item不重新lower；
- installed FULL/COMPACT/REF selection不重新优化；
- canonical reader recomputation必须得到相同item fingerprint；
- 若同一canonical row的timing字段漂移，视为canonical corruption/prefix conflict，provider open=0；
- 不得通过新epoch掩盖同Host scope里的unexpected row mutation。

### 10.6 Source transition matrix

`PREVIOUS_TURN_OUTCOME`至少覆盖：

| prior head | current fact | action |
|---|---|---|
| none | interrupted VALUE | append VALUE |
| VALUE A | same current turn/same A | no-op |
| VALUE A | late-result corrected VALUE B | append VALUE B |
| VALUE | successful/Plan-owned empty | append CLEARED |
| CLEARED | successful/empty | no-op |
| CLEARED | interrupted VALUE | append VALUE |

compatible append对`EXPLICIT_EMPTY -> CLEARED`必须按已安装的presence去重：一旦head已经是
`CLEARED`，后续successful/empty turn不得因为新的turn identity、dispatch anchor或
occurrence semantic fingerprint再追加一个空observation。turn identity仍可参与VALUE与
首次CLEARED的candidate fingerprint，但不能成为“再次清空”的理由。

`TOOL_OBSERVATION_FRESHNESS`是TURN_APPEND：每个新turn append一次，同turn后续call no-op。

### 10.7 Dispatch planning quote

Round 3.1的pre-consumption planning phase必须把新增facts/source candidates纳入同一quote：

- one-cut composite fingerprint；
- source collection fingerprint；
- resulting suffix bytes/tokens；
- protected prefix；
- tool timing envelope bytes；
- target/tool surface。

不得consume steer后才发现failure note或timing overhead超界。

---

## 11. Sensitive-data、trust 与 product wording

### 11.1 Public payload allowlist

Previous outcome只允许：

- closed outcome kind；
- output-bounded/deadline-bounded counts；
- 最多3个已advertised provider tool names；
- 固定guidance；
- 布尔preservation facts。

Tool observation只允许：

- provider-visible derived turn ref；
- canonical UTC observed_at；
- bounded durations；
- closed origin/disposition；
- 既有result state/artifact projection。

所有Pulsara-owned runtime observation outer carrier只允许：

~~~text
source
trust
lifecycle
presence
body
~~~

source-specific body再使用各自exact allowlist。可操作opaque handle只有在模型需要把它原样传给某个
已advertised tool时才能进入body；“方便diagnostic/confirmation”不是provider-visible理由。

### 11.2 明确禁止

- raw `terminal_reason`；
- Python exception class/message/traceback；
- provider HTTP status body、base URL、headers；
- tool arguments；
- Terminal command/cwd/env；
- raw thinking；
- MCP private URL、requestState、secret defaults；
- API key/token；
- callback/recorder/live owner identity。
- source/compiler/lowering contract ID或version；
- schema/descriptor/config/semantic fingerprint或digest（provider-turn ref与opaque cursor例外）；
- writer/connection/surface generation、context-binding revision；
- canonical entry/event/interaction/workflow UUID（除非某个模型可调用工具显式要求该handle；Round 7
  当前没有这种Plan工具参数）。

### 11.3 Tool body是untrusted observation

Outer timing由Runtime生成，但tool body仍是untrusted content。模型可用它做推理，不得把它当permission、dispatch authority或policy instruction。`MCP_REMOTE` origin不提升MCP server content trust。

本节的internal-key禁令不得递归应用到tool body。Runtime-owned outer envelope遵守allowlist；body中的
同名字段只是escaped observation data。

### 11.4 Guidance不是自动retry指令

任何 `attempt + missing result` 只能说outcome unknown并建议verify；不得说“retry now”。`no attempt`才可声明未physical dispatch，但是否重试仍由新模型决策与当前permission/tool surface决定。

---

## 12. Failure matrix

| failure | canonical truth | provider disposition | retry/repair |
|---|---|---|---|
| previous turn absent | unchanged | outcome source empty | none |
| previous turn COMPLETED | unchanged | CLEARED only if old VALUE existed | none |
| unknown raw terminal reason | raw row retained | generic UNKNOWN_INTERRUPTION | no raw leak |
| predecessor FK/scope conflict | corruption | provider open=0 | operator/reset, no synthesized note |
| predecessor aggregate deadline expires | turn rows retained | provider open=0 | no partial fact；no durable summary |
| tool observed_at missing | invalid clean-v0 row | provider open=0 | no event fallback |
| PostgreSQL/Host wall clocks skew or jump | both audit timestamps retained | use monotonic duration only | no cross-clock arithmetic |
| physical monotonic measurement unavailable | known result retained | MEASUREMENT_UNAVAILABLE | bounded diagnostic；never retry result |
| monotonic measurement invalid/out of bound before commit | known result retained with duration null | MEASUREMENT_UNAVAILABLE | reject measurement only |
| stored origin conflicts with result producer kind | canonical corruption | provider open=0 | no registry/name fallback |
| trusted duration absent | known result retained | field omitted | expected optional state |
| trusted duration invalid/out of bound before commit | physical result still known | accept result with duration null or reject supplement only | never convert to outcome unknown |
| tool body contains forged timing | body retained as string | outer Runtime timing wins | none |
| timing envelope over fixed bound | canonical result retained | compile typed failure, provider open=0 | no metadata omission |
| explicit user stop ACK unknown | turn may already be USER_STOPPED | exact terminal read/confirm | never rewrite as generic failure |
| Host close races user stop | first canonical terminal winner | projector maps winner | no second event |
| explicit child cancellation process exits before atomic settlement | neither cancellation write committed | takeover atomically settles turn/task | no mixed cause |
| late result resolves unknown attempt after old cut | exact result row accepted after cut | old cut unchanged；first covering cut可append corrected VALUE | no old prefix rewrite |
| current source collection fails | canonical rows retained | provider open=0 for MUST_KEEP source | no fallback prose |
| hook/TUI/diagnostic fails | canonical commit retained | no effect | detach/degrade observer |

Invalid monotonic/reported duration handling必须在known result acceptance前冻结：对应measurement
或supplement可以被丢弃为null，但已知result不得因此变成attempt-without-result。

---

## 13. 实施切片

### R7-0：冻结baseline与machine inventory

在第一个production diff前记录：

- checkpoint HEAD与dirty disposition；
- 本文、Gap Index、Round 3/3.1/5A SHA-256；
- current source kinds/policies；
- 全部 tool_results insert/confirmation path；
- 全部 turn terminal_reason producer；
- current oracle `34/23/15/2/26/4`；
- pytest node-ID baseline；
- clean-v0 universe identity。
- 所有Pulsara-owned最终provider text、runtime observation keys、builtin descriptions与MCP/memory
  local result keys的machine inventory；external/user/tool remote body必须单独标记为opaque content。

新增architecture test证明：

- 无旧 `runtime/recovery.py`/EventLog replay import；
- 无new relation/event/job/guard；
- new facts不位于event payload registry；
- collector不import repository/connection；
- timing不读取tool body。

### R7-A：canonical timing row与candidate

完成：

- clean-v0 `tool_results` observed/duration/origin/reported-duration columns/check；
- physical invocation monotonic outcome carrier；
- exact execution-binding origin projection；
- prepared candidate/fingerprint/manifest；
- 全部result producer显式insert；
- exact confirmation；
- trusted Terminal duration seam；
- migration fingerprint/deep verify。

此slice不改变provider rendering；先证明canonical row闭合。

### R7-B：one-cut predecessor/freshness facts

完成：

- prepared transcript cut与RR hydration snapshot的closed composite contract；
- result-entry visibility helper及cut-after-result race closure；
- exact-scope sequence-index predecessor SQL与large-history EXPLAIN evidence；
- output-bounded/deadline-bounded aggregate；
- raw reason projector；
- `FrozenPreviousTurnOutcomeCompileFact`；
- `FrozenToolObservationFreshnessCompileFact`；
- composite fingerprint；
- tool-state timing hydration。

所有RR-derived fact必须在同一个reader transaction冻结；所有result-derived fact还必须
exact join prepared transcript cut。不得把两次事务误述为同一个PostgreSQL snapshot。

### R7-C：compiler source与tool envelope

完成：

- 两个ContextSourceKind；
- closed registry/policies/absence；
- deterministic render；
- generic runtime observation的internal DTO/provider projection分层；
- BASE_SYSTEM补全全部provider-visible lifecycle与presence语义；
- 删除未被production调用、仍渲染`contract=...`文本的legacy source renderer，避免形成第二条wire路径；
- environment/permission/Plan/clock/Terminal/binary/MCP catalog的lean provider projector；
- source-specific contract严格按10.3矩阵升级，既有collector implementation contract保持不变；
- Plan canonical textual body去除已由typed columns承载的内部identity；
- Plan continuation/result改为无delimiter的closed canonical JSON union；
- memory tool projection去除semantic digest与raw index generation；
- builtin provider descriptions去除generation/contract/event-stream等实现术语；
- canonical outer tool-result JSON codec；
- FULL/COMPACT/REF/OMITTED/late lowering；
- global lowering/compiler与source-specific renderer contract按冻结矩阵bump；
- estimator/planning quote；
- strict-prefix tests。

### R7-D：explicit stop语义

完成：

- per-exact-turn ROOT/child process-local cancellation intent；
- Plan automatic successor在Host lock内换代intent；
- child `_LiveTask`与runner共享同一intent；
- explicit child cancellation原子终结turn/task并append既有两类occurrence；
- stop vs close vs runtime failure mapping；
- exact terminal settlement confirmation；
- ROOT/child/lifecycle races；
- no new durable cancellation state。

### R7-E：activation与evidence

完成：

- reset-only DB install/repeat/deep verify；
- full pytest/PostgreSQL；
- Chat/Responses payload probes；
- real-provider multi-turn dogfood；
- activation evidence；
- Gap Index只在全部gate通过后把PHC-13/14标成restored；
- 规格状态改为ACTIVATED。

---

## 14. 测试矩阵

### 14.1 Previous outcome unit tests

- failed predecessor without assistant entry；
- interrupted predecessor with accepted assistant text；
- accepted assistant entry被描述为complete accepted prefix，不称partial canonical message；
- explicit user stop；
- runtime/provider failure；
- session close；
- Host takeover；
- provider input conflict；
- resource exhausted；
- Plan continuation not bound；
- Plan force exit不产生generic note；
- unknown raw reason映射generic code且raw text不泄漏；
- latest successful predecessor遮蔽older failure；
- ROOT/child/task isolation；
- large session history的predecessor query使用exact-scope sequence index反向`LIMIT 1`；
- very long predecessor aggregate受DB deadline约束，超时不返回partial fact。

### 14.2 Unfinished tool tests

- call + no attempt => definitely not dispatched；
- attempt + no result => outcome unknown；
- permission pending/deny不冒充physical attempt；
- Plan interaction abort排除；
- known success/error/cancelled result均不算unfinished；
- late result使unknown count下降；
- freeze cut N -> commit late result entry N+1 -> read old cut仍为unknown -> read new cut才下降；
- predecessor aggregate、ordinary result、late result与timing hydration对同一cut给出一致visibility；
- sample稳定、最多3项、UTF-8 bound；
- args、commands、URLs与raw errors不出现。

### 14.3 Timing tests

- UTC canonicalization与microsecond golden；
- physical monotonic elapsed => MEASURED；
- no physical attempt => NO_PHYSICAL_ATTEMPT；
- missing physical measurement => MEASUREMENT_UNAVAILABLE且known result仍接受；
- PostgreSQL clock分别领先/落后Host五分钟不改变monotonic duration；
- Host wall-clock/NTP跳变不改变monotonic duration；
- reader不执行`observed_at - attempt.started_at`或任何cross-clock subtraction；
- delayed artifact preparation/confirmation不后移physical observed_at或扩大monotonic duration；
- observation duration valid/null/out-of-bound；
- tool-reported durationvalid/null/out-of-bound；
- Terminal trusted supplement保存并rehydrate；
- exact single-process action可提供trusted duration；
- `terminal_process list`、monitor register/list/cancel与multi-process aggregate必须为null；
- arbitrary tool body `{"timing": ...}`不能伪造；
- MCP body/annotation不能伪造；
- origin由exact execution binding/producer冻结并持久化；
- builtin退役/改名、registry变化与`mcp__`相似名称不改变历史origin；
- ordinary与late result调用同一timing helper；
- observed_at来自tool_results，不来自event；
- artifact/preview disposition不改变timing。

### 14.4 Rendering tests

- outer JSON encode/decode fixed point；
- generic runtime observation exact keys为`source/trust/lifecycle/presence/body`；
- lifecycle既参与source-head/fingerprint也作为provider时域语义进入wire；internal contract/version不进入wire；
- BASE_SYSTEM完整解释SNAPSHOT/TURN/CALL/ACTIVATION/ONE_SHOT/CLEARED/UNAVAILABLE；ONE_SHOT handoff不会在后续turn被误当current state；
- source registry、compiler policy与golden vectors exact采用10.3版本矩阵，既有collector implementation version不变；
- environment/permission/Plan/clock body不重复source/trust/contract wrapper；
- closure无schema version与重复call ID；late outcome无schema version但保留call ID；
- Terminal provider projection无schema version/host-scoped/observation identity，actionable process/monitor handle仍在；
- Plan continuation/result无workflow/interaction/entry/tool identity与digest，feedback/answer/approved plan仍完整；
- Plan continuation与approved plan只使用closed canonical JSON；所有旧closing marker均为0，正文中的伪marker只能作为escaped string；
- binary placeholder无schema version/digest，仍明确codec/size；
- MCP source/list result无顶层catalog fingerprint，opaque pagination cursor仍能拒绝stale snapshot；
- memory provider result无semantic digest与raw desired/applied generation；
- builtin tool descriptions不出现内部generation/contract/event-stream措辞；
- external/user/tool/MCP remote body中同名`schema_version/contract`保持原样且被escaped；
- malicious closing marker；
- quotes/backslashes/newlines/Unicode；
- ESC/OSC作为body string，不逃逸carrier；
- FULL/COMPACT/REF_ONLY/OMITTED均保留timing；
- envelope bytes计入budget；
- native tool_call_id/grouping不变；
- late outcome user-role contract包含同一observation fact。

### 14.5 Prefix tests

对Chat与Responses分别覆盖：

- same-turn tool result append；
- failed turn -> next human turn outcome note；
- user stop -> next turn；
- successful successor -> CLEARED；
- failure VALUE -> success CLEARED -> another success严格不再追加CLEARED，并断言exact消息数量；
- late result -> appended replacement note；
- current -> previous -> historical freshness without old message rewrite；
- clock变化只appendclock；
- MCP physical reconnect same semantics不reset prefix；
- installed compact tool result不被重新full render；
- exact assertions：SYSTEM equal、tools equal、messages prefix equal。

### 14.6 Database tests

- fresh clean-v0；
- repeat migrate；
- old universe RESET_REQUIRED/DDL 0；
- observed_at NOT NULL；
- observation/reported duration CHECK；
- observation_origin_kind NOT NULL/closed producer CHECK；
- every result origin insert path；
- candidate ACK FULL/NONE/CONFLICT；
- event occurred_at exact join；
- canonical row tamper由deep verifier/test fail closed。

### 14.7 Interruption race tests

- user stop immediately before provider open；
- user stop during provider stream；
- user stop during tool physical invocation；
- Host close races user stop；
- writer takeover races settlement；
- interruption commit ACK lost；
- canonical COMPLETED wins over late cancel；
- request waiter cancellation不变成user stop；
- user Stop during automatic Plan successor命中successor intent而非origin；
- child `stop_agent`使child turn与subagent task都表达user stop；
- child Host close使child turn与subagent task都表达session close；
- child cancellation transaction在任一row/event write注入失败时整体rollback；
- child cancellation ACK unknown exact-confirm两行与两个existing occurrences；
- child stop before turn admission FULL先settle admission；confirmed NONE只终结task；
- child turn COMPLETED与cancel竞态时completed winner胜出；
- process exit before atomic commit后由takeover把turn/task一起收口为HOST_TAKEOVER。

### 14.8 Real-provider dogfood

至少一条可审计但不记录完整prompt/secret的trajectory：

1. turn A 调用一个快速tool与一个可控中断tool；
2. 明确Stop或注入bounded provider failure；
3. turn B 用户发送“继续”；
4. 模型明确识别previous outcome；
5. 模型引用tool observation time/freshness；
6. 不自动重试unknown effect；
7. payload probe证明turn B wire input严格追加turn A prefix。

dogfood只证明产品closure，不用remote cache-hit百分比作为correctness gate。

---

## 15. Architecture guards

必须建立静态/动态guard：

1. `ContextSourceKind`、registry与compiler policy集合exact equal。
2. PREVIOUS_TURN_OUTCOME exactly candidate XOR absent；freshness exactly candidate。
3. collector不能import`conversation_kernel._repository`、connection provider或SQL。
4. reader不得读取agent_events构造两项fact。
5. `ProviderToolResultContextMetadata.timing`不可为空。
6. every tool_results INSERT显式包含observed_at、observation duration、origin与reported duration。
7. reported duration只能来自sealed trusted supplement。
8. raw terminal reason不进入rendered body。
9. tool body不能控制outer timing keys。
10. compiler/lowering version与golden payload一致。
11. same Host/scope installed input满足strict prefix。
12. no new durable relation/event/job/guard/subject。
13. no skip/xfail用于掩盖Round 7 regression。
14. every result-derived fact必须通过同一个result-entry cut visibility helper。
15. provider-safe-point期间不得并发接纳新的tool attempt。
16. ROOT continuation与每个child runner的cancellation intent exact join当前turn/scope。
17. installed `CLEARED`对后续`EXPLICIT_EMPTY`按presence no-op。
18. duration projector不得对PostgreSQL/Host wall-clock字段做减法。
19. observation origin不得读取当前registry、tool name prefix或tool body重新分类。
20. explicit child cancellation必须在一个writer transaction终结turn/task并append两类existing occurrence。
21. predecessor lookup必须使用exact-scope transcript sequence index path；不得全量排序turns。
22. Pulsara-owned runtime observation provider keys必须exact equal
    `source/trust/lifecycle/presence/body`；internal contract/version/fingerprint不得进入wire。
23. runtime-authored provider text/tool descriptors不得包含source/compiler/lowering contract、schema version、
    fingerprint/digest、writer/connection/surface generation或无操作意义的canonical UUID。
24. internal metadata scanner只检查Pulsara-owned renderer与descriptor，禁止递归扫描/改写user、tool或
    MCP remote body。
25. runtime observation只有一个production provider projector；legacy text renderer与adapter-local
    renderer均为0。
26. registry source contract必须exact equal 10.3 bump矩阵；collector implementation contract不得因纯
    renderer变化被误升级。
27. Plan provider carrier必须来自closed JSON projector；production lowering中的旧Plan delimiter marker为0。

推荐 targeted gate：

~~~bash
uv run pytest -q \
  tests/test_round7_model_visible_failure_and_tool_observation.py \
  tests/test_round3_structured_model_input_compiler.py \
  tests/test_round3_1_provider_input_prefix_continuity.py \
  tests/test_round5_long_horizon_execution_envelope.py

uv run pytest -q -m postgres \
  tests/test_round7_model_visible_failure_and_tool_observation.py \
  tests/test_stage2_conversation_kernel_postgres.py

uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src tests tools
uv run python tools/generate_terminal_protocol_contract.py --check
(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)
uv lock --check
git diff --check
~~~

文件名以实际仓库test布局为准，但不得降低语义覆盖。

---

## 16. Definition of Done

Round 7只有满足以下全部条件才可标记ACTIVATED：

- immediate predecessor由same-scope canonical order唯一选择；
- every result-derived fact以canonical result entry exact join prepared transcript cut；
- interrupted predecessor产生bounded typed guidance；
- successful predecessor使旧guidance不再适用；
- explicit user stop不再冒充runtime/provider failure；
- ROOT Plan successor与child cancellation均由per-exact-turn process-local intent传递真实cause；
- explicit child cancellation的turn/task canonical settlement原子成立；
- accepted assistant message wording符合atomic commit真值；
- unfinished tool区分no-attempt与attempt-no-result；
- absolute timing、monotonic duration与immutable origin由canonical rows生成且tool body不可伪造；
- observation duration不跨PostgreSQL/Host时钟域计算；
- trusted tool-reported duration可选、bounded、持久化于existing row；
- trusted Terminal duration仅来自唯一exact process action，aggregate/control action保持null；
- per-turn frontier表达CURRENT/PREVIOUS/HISTORICAL而不回写result；
- all tool-result render modes保留essential timing；
- Pulsara-owned provider projection通过closed allowlist；internal contract/version/fingerprint不再作为
  model tokens发送，provider-semantic lifecycle必须保留；
- source-specific contract、registry、compiler policy与golden vector按10.3矩阵一致；
- Plan continuation/result使用closed canonical JSON，正文无法逃逸typed transition/trust边界；
- actionable opaque handles、native tool grouping与external observation body保持不变；
- Chat/Responses strict-prefix machine assertions通过；
- clean-v0/reset-only contract通过；
- full suite与PostgreSQL suite通过；
- real-provider dogfood通过；
- oracle仍为`34/23/15/2/26/4`；
- activation evidence存在并包含checkpoint hashes、schema identity、test counts与dogfood redaction statement；
- Gap Index的PHC-13/14只在上述证据完成后更新。

建议activation evidence：

~~~text
benchmarks/suites/core/v1/
round7_model_visible_failure_and_tool_observation_activation.json
~~~

---

## 17. 明确保留给后续轮次的边界

### 17.1 Round 5B compaction

未来compaction必须：

- 从canonical tool rows重新物化immutable timing；
- 在rebase后的新epoch安装最新freshness frontier；
- 保留previous outcome的当前head语义；
- 不得把summary生成时间冒充tool observed_at；
- 不得把historical result重新标成current；
- 不得恢复timing seed/replay graph。

这些是future compatibility contract，不是Round 7 activation gate。

### 17.2 Memory

Round 7不把failure或timing自动写进memory candidate。未来memory设计可以消费canonical facts，但不能反向成为prompt timing/outcome成立条件。

### 17.3 业务级 freshness policy

本轮只提供事实：何时观察、经历多久、属于哪个turn horizon。网页TTL、git状态、文件系统、MCP resource或deployment health多久算stale，是各tool/domain未来policy，不应塞进通用compiler。

### 17.4 UI

Go TUI未来可以显示同一timing与turn outcome，但只读取canonical/projection DTO；UI failure不能阻塞provider call。Round 7不新增wire UI contract。

---

## 18. 最终冻结

Round 7 的终局不是恢复旧“failure recovery system”或“universal timing event system”，而是：

~~~text
canonical turn outcome
    -> one-cut previous-turn fact
    -> append-only model guidance

canonical tool result observed-at / monotonic-duration / origin
    -> immutable observation fact
    -> exact tool-result envelope

current canonical scope frontier
    -> one small turn-appended freshness observation

all three
    -> structured compiler
    -> strict provider prefix continuity
~~~

长期不变量是：

- canonical row回答“发生了什么、何时被观察”；
- committed event回答“何时接受了该transition”，不用于prompt replay；
- live event回答“当前进程正在发生什么”，Host crash后消失；
- previous failure与freshness都是read-time typed projection；
- 每个source与tool envelope有closed owner、schema、budget与sensitivity；
- internal contract/version/fingerprint用于Runtime证明，不是provider prompt内容；lifecycle用于模型解释
  append-only observation的有效范围，必须进入provider prompt；
- provider只接收可推理产品事实、必要的trust边界与可操作opaque handle；
- 任何新的时效提示只能追加，不能改写已安装历史；
- attempt without result永远是unknown，不自动重跑；
- 没有必要为这两项产品能力恢复durable execution recovery machinery。
