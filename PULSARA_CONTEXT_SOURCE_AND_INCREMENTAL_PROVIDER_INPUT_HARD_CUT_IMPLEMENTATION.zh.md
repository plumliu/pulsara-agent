# Pulsara ContextSource 与 Incremental ProviderInput 两阶段 Hard Cut 实施规格

> 状态：下一阶段权威实施规格草案，待 review 后冻结。
>
> 记录日期：2026-07-19。
>
> 本文取代 `PULSARA_NEXT_FIVE_HARD_CUT_STAGES_PLAN.zh.md` 中原阶段五、阶段六的实施顺序；旧文仍保留历史背景。
>
> 本文同时取代 `PULSARA_PROMPT_CACHE_CONTRACT.zh.md` 中“可变 suffix 每轮重建”的 lane 设计与旧 PR 顺序；其中 cache 不是 authority、完整本地输入、禁止 remote continuation、usage 只作观察等原则继续有效。
>
> **后续因果顺序hard cut（2026-07-19）：** ordered transcript projection、context frame placement、
> strict-prefix frontier、pending continuation exact join与rollover authority的最终契约由
> `PULSARA_PROVIDER_INPUT_CAUSAL_ORDER_AND_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md`
> 收紧。本文中的source ownership与generation foundation继续有效；任何与后续文档冲突的lane重排、run/window
> scope或fuzzy continuation描述均以后续文档为准。

---

## 0. 执行结论

下一阶段不再按“先完成 ContextSource，再另做 Prompt Cache”的原顺序实施，而拆成两个连续 hard cut：

```text
阶段 A：Append-aware ContextSource Ownership Hard Cut
    先冻结增量输入生命周期
    再迁移全部 non-transcript source ownership

阶段 B：Incremental ProviderInput Generation Hard Cut
    复用阶段 A 的 source identity/lifecycle
    建立 generation、append、rollover、recovery 与 exact replay
```

阶段 A 不承诺 provider prefix 已经稳定，但它必须产出阶段 B 可以直接消费的最终 source contract。阶段 B 不再修改 source ownership，只替换 provider input 的构造与持久化方式。

最终模型是：

```text
Canonical EventLog / Artifacts / Memory
                 |
                 v
        ContextFactSnapshot
                 |
                 +--------------------------+
                 |                          |
                 v                          v
       Normalized Transcript        ContextSource Registry
                 |                          |
                 +-------------+------------+
                               v
                 ProviderInput Append Planner
                               |
                               v
            ProviderInputGeneration + Append Batches
                               |
                               v
                 Canonical ProviderInputPlan
                               |
                               v
                       Provider Adapter
```

Pulsara 每次仍向 provider 发送完整输入。增量构造指 Pulsara 不再重新解释、重排或重渲染已经提交的 provider-visible prefix，不表示只向 provider 发送 delta，也不引入 `previous_response_id`。

---

## 1. 为什么需要重排阶段

### 1.1 当前 source ingress 只解决了 typed carrier，没有解决生命周期

当前 `ContextSectionCandidate` 已保存：

- source kind；
- source refs/artifacts；
- priority 与 required；
- stability；
- lowering kind；
- 完整 `model_visible_text` 对应的 payload。

但是当前 collector 仍由 `AgentRuntime` 传入 system、memory hook、capability、plan 等字符串；compiler 每次重新排序所有 candidate、重新应用 timing overlay，再和完整 transcript 一起 lower。

因此当前 `stable | run | step | ephemeral` 只描述本地 lifecycle cache，并不能回答：

- 已经发送过的 candidate 下一次是否必须保留；
- source 更新是追加 revision、原地替换还是 generation rollover；
- candidate 被预算省略后是否允许下一次重新插入到历史位置；
- 动态时间是否可以改写旧 section；
- tool schema 或 system contract 变化是否必须开启新 generation。

### 1.2 当前 timing 会主动破坏 prefix

当前 compiler 使用本次 `compiled_at_utc` 为 transcript 与 non-transcript section 生成：

- `compiled_at_utc`；
- local date；
- `age_seconds`；
- section timing header。

即使 source body 完全不变，旧 section 也会在下一次 compile 得到不同 provider-visible bytes。把它迁移进 ContextSource 但保留同一渲染所有权，只会把错误生命周期正式冻结。

### 1.3 完整增量构造又不能先于 source ownership

若现在直接对 legacy 字符串做 diff，增量 planner 无法证明：

- 字符串变化来自新的 canonical fact，还是 renderer 漂移；
- 两段相同文字是否由同一个 source 拥有；
- source revision 的顺序与 supersession 是否可 replay；
- 哪个 contract 有权决定 provider placement；
- 当前 process-local text 是否与历史 durable fact 一致。

因此正确顺序不是二选一，而是：

```text
先冻结 append-aware source contract
-> 完成 source ownership
-> 再切换增量 provider input owner
```

---

## 2. 权威关系

### 2.1 继续有效的上游契约

本文不改变以下已经完成的 hard cut：

- `ResolvedModelCall` 是 model target/request contract 的唯一真值；
- `ContextFactSnapshot` 不暴露 mutable LoopState；
- normalized transcript/tool-result units 是 transcript compiler 输入；
- accepted terminal projection + durable control disposition 决定 canonical assistant output；
- Long-Horizon window/rollup/compaction 决定历史重写边界；
- Context Evidence Cursor 只优化 transcript evidence read，不持有 provider input truth；
- model stream raw segment events不是 transcript semantic truth；
- provider remote continuation继续 fail closed 禁用。

### 2.2 本文新增的权威层

本文新增两类 derived authority：

1. `ContextSourceCandidate`：non-transcript model-visible fact 的唯一 source-owned semantic carrier；
2. `ProviderInputGeneration`：已经发送或准备发送的 provider-visible ordered projection。

两者都不能成为 canonical domain fact 的替代品：

```text
canonical facts
    -> deterministic source/reducer contracts
    -> source candidates / provider input projection
```

缺失 materialization cache 时可以从 canonical facts 与 historical contract重建；重建不一致时 fail closed，不能信任 cache 自证。

### 2.3 本文与 Prompt Cache 的边界

增量 ProviderInput 是正确的输入 ownership，不只是 provider cache 优化。

Prompt cache 后续只负责：

- provider-specific hint；
- cached usage normalization；
- predicted break reason；
- provider cohort metrics；
- dogfood 与 Inspector。

即使 provider 完全不支持 cache，本文的 generation/append/replay contract 仍必须成立。

---

## 3. 术语

### 3.1 Provider-visible semantic sequence

Provider-visible semantic sequence 是 adapter 实际发送给模型、会影响 token prefix 的有序语义单元，包括：

- system/developer instructions；
- tool definitions；
- user/assistant/tool messages；
- model-visible runtime observations；
- provider input framing 中确实影响模型输入的字段。

HTTP header、credential、request trace ID、连接参数和 provider response ID 不属于该 sequence。

本文所说的“prefix 相同”指 provider-visible semantic/token sequence 相同，不要求两次 HTTP JSON 文档在字节层面满足字符串前缀关系。JSON 数组结束符等传输包装不参与该不变量。

### 3.2 ProviderInputGeneration

`ProviderInputGeneration` 是一个typed-scope的输入世代：main/subagent使用session-window scope，direct/window summarizer/governance使用one-shot scope。对session-window generation：

- 跨越多个 model call；
- 跨越同一 session 中的多个 user turn；
- 不绑定单个 run、segment 或 process；
- system/tool/input-lowering compatibility 在 generation 内固定；
- 已提交 provider-visible unit 只可保留或继续追加。

One-shot generation只绑定一个resolved operation与attempt，仍使用相同root/append/plan/lifecycle contract，但不会伪造window identity，也不跨下一operation复用。

以下变化通常开启新 generation：

- Long-Horizon compaction/history rewrite；
- system instruction semantic contract变化；
- tool catalog/schema/order变化；
- requested model/input-lowering contract不兼容；
- provider request shape 中影响 tokenization 的字段变化；
- 已提交历史必须被删除、重排或降级；
- generation 达到 resolved context budget，必须构造新 baseline。

普通 user turn 不自动开启新 generation。

### 3.3 Append batch

Append batch 是一次 model input preparation 中新增的有序 provider units。它可以包含：

- 上一次 ACCEPTED model output；
- tool call/result pairing group；
- 新 user message；
- source candidate/revision；
- current clock observation；
- recovery/status observation。

Append batch 是逻辑原子序列，不要求每个 batch 对应单独 PostgreSQL transaction；writer 可以在不改变顺序与 stable candidate identity 的前提下物理合批。

### 3.4 Rollover

Rollover 是显式关闭旧 generation 并创建新 generation。Rollover 允许用 summary/baseline 替换旧历史，因此是合法的 prefix break。

Rollover 不是普通 source refresh，也不能由 compiler 为了局部预算偷偷触发。

### 3.5 Source revision

Canonical source revision是对同一逻辑source key的generation-neutral新观察。它的ID、ordinal与canonical predecessor只由source domain事实决定，不表示“已经发送给哪个generation”。

Provider generation另外维护committed source head。Planner把canonical candidate与该head比较，决定no-op、append、stale、not-ready或rollover；只有真正append时才生成provider-visible supersession关系。旧provider revision保留在generation中，并使用静态、版本化的“latest appended revision wins”语义。

安全执行许可不依赖模型理解 revision。Permission/tool gate继续使用 canonical runtime policy。

---

## 4. 总体不变量

### 4.1 Prefix 单调性

设同一 generation 中第 `n` 次 model call 的输入为 `I_n`，该调用被 durable ACCEPTED 的 canonical model output 为 `O_n`，之后新增事实为 `A_(n+1)`：

```text
I_(n+1) = I_n || O_n || A_(n+1)
```

允许以下退化情况：

- retry 使用完全相同的 `I_n`；
- provider error/cancel/runtime error没有 accepted output，下一次 preparation从原 committed prefix继续；
- rollover 后不要求新 generation 以旧 generation 为 prefix。

禁止：

- 删除 `I_n` 中任何 unit；
- 移动旧 unit；
- 用新 renderer重写旧 unit；
- 修改旧 tool-result render variant；
- 用本次 wall clock更新旧 timing header；
- 在旧 unit 之前插入新 memory/plan/runtime section。

### 4.2 单一 source ownership

每个 model-visible unit 必须能追到唯一owner：

```text
transcript projection
| ContextSource registry binding -> source semantic candidate -> source attribution
| CapabilityToolCatalogRootFact
-> snapshot authority horizons
-> provider append unit
-> ModelStart input reference
```

ContextSource只拥有non-transcript message/section projection；provider tool definitions只由 `CapabilityToolCatalogRootFact`拥有；transcript由normalized transcript/terminal projection拥有。同一 byte不得跨owner重复出现。

### 4.3 Source 不拥有 provider-native lowering

ContextSource：

- 只消费授权的 immutable facts；
- 只产 typed semantic payload 与 attribution；
- 不构造 `LLMMessage`；
- 不构造 provider JSON；
- 不决定 tool/message framing；
- 不读取其他 source 输出；
- 不读取 lifecycle/render cache作为真值。

ProviderInput planner/compiler统一拥有：

- candidate selection；
- budget admission；
- pairing-safe ordering；
- append/supersession/rollover判定；
- lowering contract；
- final ProviderInputPlan。

### 4.4 已发送内容不能重新参与可选 selection

Optional candidate 只能在首次 append 前省略。一旦 candidate 已进入 committed prefix：

- 后续 allocation 必须把它视为 retained prefix cost；
- 不能因 priority变化而删除；
- 不能重新执行省略策略；
- budget不足时只能拒绝新 optional candidate、触发 typed pressure，或走 Long-Horizon rollover。

### 4.5 时间是事实，不是旧内容的渲染参数

冻结三种时间：

1. source absolute time：随 source fact首次出现，稳定保存；
2. compile wall clock：只进入 manifest/Inspector；
3. model-visible current clock：作为新的 runtime clock candidate追加。

禁止 model-visible `age_seconds` 随每次 compile重算旧 section。

### 4.6 Exact replay 不读取当前 binding或当前时间

Replay 必须使用历史 durable：

- source contract ID/version/fingerprint；
- lowering contract ID/version/fingerprint；
- generation root与append chain；
- exact source/transcript refs；
- model-visible clock observation；
- ResolvedModelCall target/request shape；
- ProviderInputPlan fingerprint。

缺少 historical binding时 fail closed，不允许用当前 renderer近似恢复。

### 4.7 Local complete input

每次 provider call仍发送完整本地输入。禁止：

- `previous_response_id`；
- provider conversation state；
- provider-side hidden history；
- provider automatic truncation；
- 本地 estimator不可见的 remote prompt引用。

---

## 5. 公共 fingerprint 与 schema 纪律

### 5.1 一层 DTO 只拥有一个自身 fingerprint

继续使用已经冻结的纪律：

- semantic payload DTO拥有 `semantic_fingerprint`；
- attribution/materialization DTO拥有 `fact_fingerprint`；
- reference DTO拥有 `reference_fingerprint`；
- 外层复制的 nested fingerprint只作 equality join，不是外层第二个自身 fingerprint。

禁止一个 DTO 同时自行计算 semantic、fact、reference三套 fingerprint。

### 5.2 Contract fingerprint 与 build fingerprint分离

Durable fact保存：

- contract ID；
- contract version；
- declarative contract fingerprint。

Process-local binding可额外保存：

- implementation build fingerprint。

Build fingerprint只用于诊断，不进入 durable compatibility判断。

任何相同输入产生不同 source semantic payload或provider lowering的修改：

```text
必须升级 version
并且 contract fingerprint 必须改变
```

### 5.3 Canonicalization

统一冻结：

- UTF-8；
- Unicode normalization policy；
- stable key order；
- compact JSON separators；
- absent 与 explicit null区分；
- tuple/list canonicalization；
- tool schema排序；
- provider message/content block排序；
- float/number编码；
- domain-separated SHA-256 fingerprint。

### 5.4 公共reference与lowering intent

```python
class LedgerAuthorityHorizonFact(FrozenFactBase):
    schema_version: Literal["ledger_authority_horizon.v1"]
    runtime_session_id: str
    through_sequence: int
    ledger_event_count_through: int
    ledger_continuity_accumulator_through: str
    horizon_fingerprint: str


class LedgerSequenceRangeFact(FrozenFactBase):
    schema_version: Literal["ledger_sequence_range.v1"]
    runtime_session_id: str
    first_sequence: int
    last_sequence: int
    range_fingerprint: str


class LedgerAuthorityHorizonSetNodeReferenceFact(FrozenFactBase):
    schema_version: Literal["ledger_authority_horizon_set_node_reference.v1"]
    node_kind: Literal["leaf", "internal"]
    first_runtime_session_id: str
    last_runtime_session_id: str
    subtree_horizon_count: int
    subtree_horizon_accumulator: str
    artifact_reference: ContextArtifactReferenceFact
    reference_fingerprint: str


class LedgerAuthorityHorizonSetReferenceFact(FrozenFactBase):
    schema_version: Literal["ledger_authority_horizon_set_reference.v1"]
    horizon_count: int
    ordered_horizon_accumulator: str
    root_node_ref: LedgerAuthorityHorizonSetNodeReferenceFact | None
    set_contract_fingerprint: str
    reference_fingerprint: str
```

`authority_horizons`必须按 `runtime_session_id`排序且唯一。每个event ref必须找到同ledger horizon，并满足 `event.sequence <= through_sequence`；horizon continuity必须由对应ledger的bounded/raw snapshot proof验证。跨parent/child ledger不得压成单个整数high-water。

Generation root/state/plan/ModelStart不重复内嵌完整tuple，而保存 `LedgerAuthorityHorizonSetReferenceFact`。该set使用固定fanout、content-addressed immutable nodes与COW path；root reference大小固定。每个source/unit/append仍保存自己实际引用的bounded exact horizons，set builder只能从这些nested horizons求canonical union，不能由caller自报aggregate。Empty set固定为count 0、empty accumulator、null root。

```python
class ContextArtifactReferenceFact(FrozenFactBase):
    schema_version: Literal["context_artifact_reference.v1"]
    artifact_id: str
    media_type: str
    content_sha256: str
    content_bytes: int
    artifact_contract_fingerprint: str
    reference_fingerprint: str
```

Artifact reference只属于attribution/materialization。若artifact正文会改变模型可见语义，对应content semantic fingerprint必须同时存在于owning payload semantic中并与hydrated bytes精确join。

```python
class ContextCandidateLoweringIntentFact(FrozenFactBase):
    schema_version: Literal["context_candidate_lowering_intent.v1"]
    intent_kind: Literal[
        "system_instruction",
        "leading_context",
        "paired_observation",
        "trailing_observation",
        "status_observation",
    ]
    role_constraint: Literal["system", "user", "tool", "runtime"] | None
    pairing_constraint: Literal["none", "must_follow_open_tool_call"]
    intent_contract_fingerprint: str
    intent_fingerprint: str
```

Lowering intent不是provider-native role/message。最终role与framing仍由ProviderInput lowering contract决定；source无权直接实例化 `LLMMessage`。

---

# 第一阶段：Append-aware ContextSource Ownership Hard Cut

## 6. 第一阶段目标

第一阶段完成后：

- 所有non-tool、non-transcript model-visible message/section内容由registry-owned source产生；
- provider tool definitions由独立 `CapabilityToolCatalogRootFact`拥有；
- legacy `ContextCandidateCollectionInput` 字符串 facade物理删除；
- candidate已经携带阶段二所需的最终 lifecycle；
- source timing只保存 absolute事实；
- current clock成为独立 typed candidate；
- compiler仍可暂时为每次调用完整 materialize provider input，但 source不再需要二次迁移。

第一阶段不宣称：

- provider cache 已命中；
- previous input 已成为下次 input 的严格 prefix；
- generation owner 已落地；
- full compiler reconstruction 已删除。

## 7. ContextSource registry

### 7.1 Source ID

```python
class ContextSourceId(StrEnum):
    SYSTEM = "system"
    RUNTIME_ENVIRONMENT = "runtime_environment"
    RUNTIME_CLOCK = "runtime_clock"
    MEMORY_INSTRUCTION = "memory_instruction"
    MEMORY_PROJECTION = "memory_projection"
    CAPABILITY_CATALOG = "capability_catalog"
    ACTIVE_SKILL = "active_skill"
    PLAN = "plan"
    RECOVERY = "recovery"
    ROLLOUT_STATUS = "rollout_status"
    SUBAGENT_HANDOFF = "subagent_handoff"
    SUBAGENT_RESULT = "subagent_result"
    MCP_DIAGNOSTIC = "mcp_diagnostic"
    WORKSPACE_SKILL = "workspace_skill"
```

`SYSTEM` 与 capability tool schema必须区分。Tool schema属于 generation root/provider tools，不得混入 prose catalog。

### 7.2 Declarative source contract

```python
class ContextSourceContractFact(FrozenFactBase):
    schema_version: Literal["context_source_contract.v1"]
    source_id: ContextSourceId
    source_version: str
    lifecycle_contract_fingerprint: str
    selection_contract_fingerprint: str
    lowering_intent_contract_fingerprint: str
    contract_fingerprint: str
```

Registry规则：

- `source_id`唯一；
- ID/version/fingerprint精确 rebind；
- ordered registry entries由 `source_id` canonical排序；
- duplicate ID或同 ID/version不同fingerprint启动失败；
- unknown historical source fail closed；
- implementation build fingerprint不进入 durable candidate。

`accepted_input_kinds`、`accepted_input_schema_fingerprints` 与
`emitted_payload_kinds` 不进入 durable contract。它们若只被声明、不由执行路径验证，
会成为半有效的第二真源。每个 binding 的 discriminated input type 与
`ContextSourceBindingPolicy` 才是唯一可执行的 input/payload/lifecycle/priority/
required/lowering 矩阵；registry 必须从该 policy 构造窄输入并逐字段校验，caller
不得提交自报的 policy 字段。

### 7.3 Process-local binding

```python
@dataclass(frozen=True, slots=True)
class ContextSourceBinding:
    contract: ContextSourceContractFact
    implementation_build_fingerprint: str | None
    source: ContextSource
```

```python
class ContextSource(Protocol):
    def collect(
        self,
        *,
        source_input: ContextSourceCollectInput,
    ) -> tuple[ContextSectionCandidate, ...]: ...
```

Source不得执行 storage/network I/O。需要 artifact正文时由 live collector在 source调用前通过 session-owned bounded I/O冻结进typed source input。

### 7.4 可执行的source授权输入

阶段一原子升级 `ContextFactSnapshot`，物理删除保存预渲染 `model_visible_text` 的 `ContextCandidateAuthorityFact`。只有composition-root `ContextSourceInputBuilder`可以接收完整snapshot，并按source contract构造最小typed input。

```python
class ContextSourceInputAuthorityFact(FrozenFactBase):
    schema_version: Literal["context_source_input_authority.v1"]
    source_id: ContextSourceId
    source_contract_id: str
    source_contract_version: str
    source_contract_fingerprint: str
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    physical_input_policy_fingerprint: str
    input_dependency_fingerprint: str
    authority_fingerprint: str


class ResolvedContextSourcePhysicalInputPolicyFact(FrozenFactBase):
    schema_version: Literal["resolved_context_source_physical_input_policy.v1"]
    resolved_model_input_token_limit: int
    resolved_max_provider_input_units: int
    tokenizer_or_estimator_contract_fingerprint: str
    canonical_codec_contract_fingerprint: str
    conservative_utf8_bytes_per_token_numerator: int
    conservative_utf8_bytes_per_token_denominator: int
    canonical_encoding_expansion_numerator: int
    canonical_encoding_expansion_denominator: int
    structural_overhead_bytes_per_unit: int
    max_token_budget_admissible_utf8_bytes: int
    max_canonical_materialization_bytes: int
    max_inline_item_utf8_bytes: int
    max_hydrated_working_set_bytes: int
    max_source_entries: int
    artifact_page_bytes: int
    policy_fingerprint: str
```

唯一整数公式：

```text
max_token_budget_admissible_utf8_bytes = ceil(
    resolved_model_input_token_limit
    * conservative_utf8_bytes_per_token_numerator
    / conservative_utf8_bytes_per_token_denominator
)

max_canonical_materialization_bytes = ceil(
    max_token_budget_admissible_utf8_bytes
    * canonical_encoding_expansion_numerator
    / canonical_encoding_expansion_denominator
) + resolved_max_provider_input_units * structural_overhead_bytes_per_unit
```

所有分子、分母与limits必须为正；`max_source_entries >= resolved_max_provider_input_units`，inline threshold不得超过hydrated working-set budget，artifact page必须能容纳最大合法codec atom。Doctor枚举全部production model/adapter/tokenizer组合，并证明上述quote可由artifact pages、persistent roots与operation deadlines承载。Unsupported tokenizer/codec expansion不能猜默认值，配置启动fail closed。

```python


class InlineContextSourceInputTextFact(FrozenFactBase):
    schema_version: Literal["inline_context_source_input_text.v1"]
    content_kind: Literal["inline"]
    text: str
    chars: int
    utf8_bytes: int
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    content_sha256: str
    input_text_fingerprint: str


class ArtifactContextSourceInputTextSemanticFact(FrozenFactBase):
    schema_version: Literal["artifact_context_source_input_text_semantic.v1"]
    content_kind: Literal["artifact_text"]
    content_sha256: str
    expected_chars: int
    expected_utf8_bytes: int
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    codec_contract_fingerprint: str
    semantic_fingerprint: str


class ArtifactContextSourceInputTextReferenceFact(FrozenFactBase):
    schema_version: Literal["artifact_context_source_input_text_reference.v1"]
    content_kind: Literal["artifact_reference"]
    semantic: ArtifactContextSourceInputTextSemanticFact
    artifact_reference: ContextArtifactReferenceFact
    reference_fingerprint: str


ContextSourceInputContentFact = (
    InlineContextSourceInputTextFact
    | ArtifactContextSourceInputTextReferenceFact
)


class RuntimeClockSourceContractFact(FrozenFactBase):
    schema_version: Literal["runtime_clock_source_contract.v1"]
    contract_id: str
    contract_version: str
    timezone_resolution_contract_fingerprint: str
    proposal_reason_matrix_fingerprint: str
    contract_fingerprint: str


class ContextMemoryProjectionEntryFact(FrozenFactBase):
    schema_version: Literal["context_memory_projection_entry.v1"]
    memory_id: str
    memory_revision: int
    scope: Literal["user", "workspace", "session"]
    status: Literal["active", "superseded", "contradicted"]
    canonical_statement: ContextSourceInputContentFact
    memory_semantic_fingerprint: str
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...]
    entry_fingerprint: str


class ContextMemoryProjectionInputFact(FrozenFactBase):
    schema_version: Literal["context_memory_projection_input.v1"]
    ordered_entries: tuple[ContextMemoryProjectionEntryFact, ...]
    selection_contract_fingerprint: str
    projection_semantic_fingerprint: str
    input_fact_fingerprint: str


class CapabilityProseSourceEntryFact(FrozenFactBase):
    schema_version: Literal["capability_prose_source_entry.v1"]
    projection_entry_id: str
    projection_kind: Literal[
        "catalog_entry",
        "active_skill_injection",
        "workspace_skill_injection",
        "provider_prompt_fragment",
    ]
    stable_name: str
    provider_id: str
    source_kind: Literal["builtin", "mcp", "workspace", "user", "bundled", "custom"]
    content: ContextSourceInputContentFact
    source_content_fingerprint: str
    source_artifact_ref: ContextArtifactReferenceFact | None
    entry_fingerprint: str


class CapabilityProseProjectionInputFact(FrozenFactBase):
    schema_version: Literal["capability_prose_projection_input.v1"]
    projection_kind: Literal["catalog", "active_skill", "workspace_skill"]
    ordered_entries: tuple[CapabilityProseSourceEntryFact, ...]
    source_entry_count: int
    omitted_entry_count: int
    projection_contract_fingerprint: str
    projection_semantic_fingerprint: str
    input_fact_fingerprint: str


class ContextPlanProjectionInputFact(FrozenFactBase):
    schema_version: Literal["context_plan_projection_input.v1"]
    workflow_id: str | None
    active: bool
    canonical_plan_revision: int
    plan_decision: Literal["enter", "continue", "revise", "exit", "inactive"]
    plan_content: ContextSourceInputContentFact | None
    plan_event_refs: tuple[ContextEventReferenceFact, ...]
    plan_semantic_fingerprint: str
    input_fact_fingerprint: str


class ContextRecoveryProjectionEntryFact(FrozenFactBase):
    schema_version: Literal["context_recovery_projection_entry.v1"]
    recovery_kind: Literal[
        "run_resume",
        "model_stream_recovered",
        "tool_resume",
        "window_recovered",
    ]
    stable_status_code: str
    content: ContextSourceInputContentFact
    source_event_ref: ContextEventReferenceFact
    recovery_semantic_fingerprint: str
    entry_fingerprint: str


class ContextRecoveryProjectionInputFact(FrozenFactBase):
    schema_version: Literal["context_recovery_projection_input.v1"]
    ordered_entries: tuple[ContextRecoveryProjectionEntryFact, ...]
    projection_contract_fingerprint: str
    projection_semantic_fingerprint: str
    input_fact_fingerprint: str


class ContextSubagentProjectionEntryFact(FrozenFactBase):
    schema_version: Literal["context_subagent_projection_entry.v1"]
    source_kind: Literal["handoff", "result"]
    child_runtime_session_id: str
    spawn_or_completion_semantic_fingerprint: str
    delivery_semantic_fingerprint: str | None
    result_state: Literal["success", "error", "interrupted"] | None
    content: ContextSourceInputContentFact
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...]
    entry_fingerprint: str


class ContextSubagentProjectionInputFact(FrozenFactBase):
    schema_version: Literal["context_subagent_projection_input.v1"]
    ordered_entries: tuple[ContextSubagentProjectionEntryFact, ...]
    graph_semantic_source_fingerprint: str
    selection_fingerprint: str
    projection_semantic_fingerprint: str
    input_fact_fingerprint: str


class McpInstalledSnapshotDiagnosticEntryFact(FrozenFactBase):
    schema_version: Literal["mcp_installed_snapshot_diagnostic_entry.v1"]
    server_id: str
    status: Literal["starting", "ready", "degraded", "failed", "disabled"]
    stable_diagnostic_code: str | None
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    entry_fingerprint: str


class McpInstalledSnapshotDiagnosticInputFact(FrozenFactBase):
    schema_version: Literal["mcp_installed_snapshot_diagnostic_input.v1"]
    installation_id: str
    ordered_entries: tuple[McpInstalledSnapshotDiagnosticEntryFact, ...]
    sanitization_contract_fingerprint: str
    installed_snapshot_semantic_fingerprint: str
    input_fact_fingerprint: str


class SystemSourceInput(FrozenFactBase):
    schema_version: Literal["system_source_input.v1"]
    source_kind: Literal["system"]
    authority: ContextSourceInputAuthorityFact
    instruction: ContextStaticInstructionFact
    input_fingerprint: str


class RuntimeEnvironmentSourceInput(FrozenFactBase):
    schema_version: Literal["runtime_environment_source_input.v1"]
    source_kind: Literal["runtime_environment"]
    authority: ContextSourceInputAuthorityFact
    environment: ContextRuntimeEnvironmentFact
    input_fingerprint: str


class RuntimeClockSourceInput(FrozenFactBase):
    schema_version: Literal["runtime_clock_source_input.v1"]
    source_kind: Literal["runtime_clock"]
    authority: ContextSourceInputAuthorityFact
    clock_proposal: RuntimeClockProposalPayloadFact
    clock_contract: RuntimeClockSourceContractFact
    input_fingerprint: str


class MemoryInstructionSourceInput(FrozenFactBase):
    schema_version: Literal["memory_instruction_source_input.v1"]
    source_kind: Literal["memory_instruction"]
    authority: ContextSourceInputAuthorityFact
    instruction: ContextStaticInstructionFact
    input_fingerprint: str


class MemoryProjectionSourceInput(FrozenFactBase):
    schema_version: Literal["memory_projection_source_input.v1"]
    source_kind: Literal["memory_projection"]
    authority: ContextSourceInputAuthorityFact
    projection: ContextMemoryProjectionInputFact
    input_fingerprint: str


class CapabilityCatalogSourceInput(FrozenFactBase):
    schema_version: Literal["capability_catalog_source_input.v1"]
    source_kind: Literal["capability_catalog"]
    authority: ContextSourceInputAuthorityFact
    projection: CapabilityProseProjectionInputFact
    input_fingerprint: str


class ActiveSkillSourceInput(FrozenFactBase):
    schema_version: Literal["active_skill_source_input.v1"]
    source_kind: Literal["active_skill"]
    authority: ContextSourceInputAuthorityFact
    projection: CapabilityProseProjectionInputFact
    input_fingerprint: str


class WorkspaceSkillSourceInput(FrozenFactBase):
    schema_version: Literal["workspace_skill_source_input.v1"]
    source_kind: Literal["workspace_skill"]
    authority: ContextSourceInputAuthorityFact
    projection: CapabilityProseProjectionInputFact
    input_fingerprint: str


class PlanSourceInput(FrozenFactBase):
    schema_version: Literal["plan_source_input.v1"]
    source_kind: Literal["plan"]
    authority: ContextSourceInputAuthorityFact
    plan_projection: ContextPlanProjectionInputFact
    input_fingerprint: str


class RecoverySourceInput(FrozenFactBase):
    schema_version: Literal["recovery_source_input.v1"]
    source_kind: Literal["recovery"]
    authority: ContextSourceInputAuthorityFact
    recovery_projection: ContextRecoveryProjectionInputFact
    input_fingerprint: str


class RolloutStatusSourceInput(FrozenFactBase):
    schema_version: Literal["rollout_status_source_input.v1"]
    source_kind: Literal["rollout_status"]
    authority: ContextSourceInputAuthorityFact
    rollout_status: LongHorizonRolloutStatusCandidateFact
    input_fingerprint: str


class SubagentHandoffSourceInput(FrozenFactBase):
    schema_version: Literal["subagent_handoff_source_input.v1"]
    source_kind: Literal["subagent_handoff"]
    authority: ContextSourceInputAuthorityFact
    selection: ContextCandidateSourceSelectionFact
    projection: ContextSubagentProjectionInputFact
    input_fingerprint: str


class SubagentResultSourceInput(FrozenFactBase):
    schema_version: Literal["subagent_result_source_input.v1"]
    source_kind: Literal["subagent_result"]
    authority: ContextSourceInputAuthorityFact
    selection: ContextCandidateSourceSelectionFact
    projection: ContextSubagentProjectionInputFact
    input_fingerprint: str


class McpDiagnosticSourceInput(FrozenFactBase):
    schema_version: Literal["mcp_diagnostic_source_input.v1"]
    source_kind: Literal["mcp_diagnostic"]
    authority: ContextSourceInputAuthorityFact
    installed_snapshot: McpInstalledSnapshotDiagnosticInputFact
    input_fingerprint: str


ContextSourceCollectInput = (
    SystemSourceInput
    | RuntimeEnvironmentSourceInput
    | RuntimeClockSourceInput
    | MemoryInstructionSourceInput
    | MemoryProjectionSourceInput
    | CapabilityCatalogSourceInput
    | ActiveSkillSourceInput
    | WorkspaceSkillSourceInput
    | PlanSourceInput
    | RecoverySourceInput
    | RolloutStatusSourceInput
    | SubagentHandoffSourceInput
    | SubagentResultSourceInput
    | McpDiagnosticSourceInput
)
```

每个source binding只能接受与自己 `source_id`匹配的union分支。Source模块architecture guard禁止import `ContextFactSnapshot`，因此 `authorized_snapshot_fact_kinds`不再只是声明性字符串。

授权与fingerprint规则：

- input只携带该source允许的facts；
- `input_dependency_fingerprint`覆盖所有输入semantic identities与source contract；
- source output必须回指同一个authority fingerprint；
- physical policy只进入input/candidate attribution与manifest，不进入canonical source revision或payload semantic；同一source fact在不同model policy下semantic identity不变；
- 每个event ref的ledger必须出现在 `authority_horizons`；
- source不得枚举其他source input/output；
- source不读取ProviderInput generation state或committed source heads；
- source canonical revision完全来自自己的domain facts；
- source input与snapshot authority horizons不一致时builder返回stale或authority mismatch，不构造半授权input。
- capability三个input分支只携带已经hydrate的prose/skill projection；`CapabilityExecutionSurfaceIdentityFact`、descriptor JSON schema、execution binding与完整 `CapabilityExposureSnapshotFact`一律不得进入ContextSource模块。独立 `CapabilityToolCatalogRootFact` builder拥有这些tool facts。

Source-input中央不变量：

- inline content由唯一factory从正文重算chars、UTF-8 bytes、SHA与fingerprint；超过resolved inline threshold的内容必须保持artifact reference，不能为了source collection先全量hydrate；
- `ResolvedContextSourcePhysicalInputPolicyFact`由model input limit、tokenizer/estimator、codec expansion与structural overhead确定性派生；配置doctor必须证明任何token-budget可接纳的合法source都可被inline或artifact carrier表达；
- source collection只计入当前bounded hydrated working set，最终provider materializer在budget selection后按artifact pages流式hydrate；resident admission失败退化为bounded exact path，不把physical cache cap变成第二context window；
- 所有ordered entries按各领域稳定key排序且唯一，count与tuple长度逐项相等；
- entry refs所属ledger必须存在于外层authority horizons且sequence不超过对应through sequence；
- memory/plan/recovery/subagent/MCP semantic fingerprint均从nested typed entries重算，不允许caller自报；
- plan的 `active/workflow/content/decision` 使用all-null/all-present矩阵；inactive时content必须为空；
- subagent handoff禁止result state/delivery，subagent result必须同时拥有result state与delivery；
- MCP status与diagnostic code使用versioned matrix，禁止raw exception、URL或secret；
- capability projection kind必须与外层source discriminator精确对应，且不得出现tool schema字段；
- 所有input facts均为 `frozen=True`、`extra="forbid"`，只能通过composition-root factory构造。

## 8. Candidate DTO hard cut

### 8.1 不创建第二个同名 ingress

现有 `ContextSectionCandidate` 原子升级 schema；旧 `context-candidate:v1`物理删除，不保留 wrapper facade。

### 8.2 Typed payload union

```python
ContextSourcePayloadSemanticFact = (
    SystemInstructionPayloadFact
    | RuntimeEnvironmentPayloadFact
    | RuntimeClockProposalPayloadFact
    | MemoryInstructionPayloadFact
    | MemoryProjectionPayloadFact
    | CapabilityCatalogPayloadFact
    | ActiveSkillPayloadFact
    | PlanRevisionPayloadFact
    | RecoveryObservationPayloadFact
    | RolloutStatusPayloadFact
    | SubagentHandoffPayloadFact
    | SubagentResultPayloadFact
    | McpDiagnosticPayloadFact
    | WorkspaceSkillPayloadFact
)
```

任意 JSON data只允许出现在领域 DTO明确声明的 data字段中。不得用 `FrozenJsonObjectFact`代替 source domain schema。

公共model-visible content carrier：

```python
class InlineContextSourceContentSemanticFact(FrozenFactBase):
    schema_version: Literal["inline_context_source_content_semantic.v1"]
    content_kind: Literal["inline_text"]
    text: str
    chars: int
    utf8_bytes: int
    media_type: Literal["text/plain", "text/markdown"]
    semantic_fingerprint: str


class ArtifactContextSourceContentSemanticFact(FrozenFactBase):
    schema_version: Literal["artifact_context_source_content_semantic.v1"]
    content_kind: Literal["artifact_text"]
    content_sha256: str
    expected_chars: int
    expected_utf8_bytes: int
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    codec_contract_fingerprint: str
    semantic_fingerprint: str


ContextSourceContentSemanticFact = (
    InlineContextSourceContentSemanticFact
    | ArtifactContextSourceContentSemanticFact
)
```

Artifact semantic不保存artifact ID；physical artifact reference只存在于candidate attribution，并要求SHA/bytes/media type/codec逐字段join。

逐source payload：

```python
class SystemInstructionPayloadFact(FrozenFactBase):
    schema_version: Literal["system_instruction_payload.v1"]
    instruction_source_id: str
    instruction_contract_version: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class RuntimeEnvironmentPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_environment_payload.v1"]
    workspace_kind: str
    model_visible_workspace_root: str
    terminal_current_cwd: str
    session_timezone: str | None
    rendering_contract_fingerprint: str
    semantic_fingerprint: str


class MemoryInstructionPayloadFact(FrozenFactBase):
    schema_version: Literal["memory_instruction_payload.v1"]
    instruction_contract_version: str
    memory_scope_policy_fingerprint: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class MemoryProjectionPayloadFact(FrozenFactBase):
    schema_version: Literal["memory_projection_payload.v1"]
    projection_semantic_fingerprint: str
    ordered_memory_semantic_fingerprints: tuple[str, ...]
    selection_contract_fingerprint: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class CapabilityCatalogPayloadFact(FrozenFactBase):
    schema_version: Literal["capability_catalog_payload.v1"]
    prose_projection_semantic_fingerprint: str
    ordered_projection_entry_semantic_fingerprints: tuple[str, ...]
    projection_contract_fingerprint: str
    prose_content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class ActiveSkillPayloadFact(FrozenFactBase):
    schema_version: Literal["active_skill_payload.v1"]
    skill_projection_semantic_fingerprint: str
    ordered_active_skill_semantic_fingerprints: tuple[str, ...]
    projection_contract_fingerprint: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class PlanRevisionPayloadFact(FrozenFactBase):
    schema_version: Literal["plan_revision_payload.v1"]
    workflow_id: str | None
    active: bool
    canonical_plan_revision: int
    plan_decision: Literal["enter", "continue", "revise", "exit", "inactive"]
    plan_semantic_fingerprint: str
    content: ContextSourceContentSemanticFact | None
    semantic_fingerprint: str


class RecoveryObservationPayloadFact(FrozenFactBase):
    schema_version: Literal["recovery_observation_payload.v1"]
    recovery_kind: Literal[
        "run_resume",
        "model_stream_recovered",
        "tool_resume",
        "window_recovered",
    ]
    stable_status_code: str
    recovery_semantic_fingerprint: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class RolloutStatusPayloadFact(FrozenFactBase):
    schema_version: Literal["rollout_status_payload.v1"]
    rollout_account_semantic_fingerprint: str
    phase: Literal["exploration", "finalization", "exhausted"]
    completed_model_calls: int
    completed_tool_invocations: int
    status_policy_fingerprint: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class SubagentHandoffPayloadFact(FrozenFactBase):
    schema_version: Literal["subagent_handoff_payload.v1"]
    child_runtime_session_id: str
    spawn_semantic_fingerprint: str
    handoff_semantic_fingerprint: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class SubagentResultPayloadFact(FrozenFactBase):
    schema_version: Literal["subagent_result_payload.v1"]
    child_runtime_session_id: str
    completion_semantic_fingerprint: str
    delivery_semantic_fingerprint: str
    result_state: Literal["success", "error", "interrupted"]
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str


class McpDiagnosticEntryFact(FrozenFactBase):
    schema_version: Literal["mcp_diagnostic_entry.v1"]
    server_id: str
    status: Literal["starting", "ready", "degraded", "failed", "disabled"]
    stable_diagnostic_code: str | None
    entry_fingerprint: str


class McpDiagnosticPayloadFact(FrozenFactBase):
    schema_version: Literal["mcp_diagnostic_payload.v1"]
    installed_snapshot_semantic_fingerprint: str
    ordered_entries: tuple[McpDiagnosticEntryFact, ...]
    rendering_contract_fingerprint: str
    semantic_fingerprint: str


class WorkspaceSkillPayloadFact(FrozenFactBase):
    schema_version: Literal["workspace_skill_payload.v1"]
    ordered_skill_semantic_fingerprints: tuple[str, ...]
    projection_contract_fingerprint: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: str
```

中央validator矩阵：

- `SystemInstructionPayloadFact`逐字段等于 `ContextStaticInstructionFact` hydrate结果；
- runtime environment逐字段等于 `ContextRuntimeEnvironmentFact`的model-visible projection，workspace identity attribution不进入semantic；
- memory projection逐项等于canonical memory selection/projection，不从rendered prose反推memory IDs；
- capability prose的ordered projection-entry semantic fingerprints逐项等于prose-only exposure projection，但禁止携带descriptor fingerprint、tool JSON schema或execution binding；
- active skill顺序等于capability snapshot；
- plan active与workflow/content nullability严格join `ContextPlanSnapshotFact`；
- recovery status code来自versioned enum/registry，不接受任意异常文本；
- rollout fields逐项等于 `LongHorizonRolloutStatusCandidateFact`；
- subagent handoff/result跨ledger refs必须落在各自authority horizons；
- MCP entries排序唯一，diagnostic不包含secret/URL/raw exception；
- 所有tuple都有schema-level count/bytes上界；
- 所有payload `frozen=True`、`extra="forbid"`，只通过唯一factory构造。

### 8.3 Lifecycle discriminated union

```python
class GenerationRootLifecycleFact(FrozenFactBase):
    schema_version: Literal["generation_root_lifecycle.v1"]
    lifecycle_kind: Literal["generation_root"]
    on_semantic_change: Literal["rollover"]
    lifecycle_fingerprint: str


class AppendOnceLifecycleFact(FrozenFactBase):
    schema_version: Literal["append_once_lifecycle.v1"]
    lifecycle_kind: Literal["append_once"]
    duplicate_semantic_identity: Literal["no_op"]
    conflicting_same_key: Literal["contract_mismatch"]
    lifecycle_fingerprint: str


class AppendRevisionLifecycleFact(FrozenFactBase):
    schema_version: Literal["append_revision_lifecycle.v1"]
    lifecycle_kind: Literal["append_revision"]
    supersession_semantics: Literal["latest_revision_wins"]
    continuity_kind: Literal["complete_snapshot", "strict_delta"]
    source_revision_contract_fingerprint: str
    lifecycle_fingerprint: str


class AuditOnlyLifecycleFact(FrozenFactBase):
    schema_version: Literal["audit_only_lifecycle.v1"]
    lifecycle_kind: Literal["audit_only"]
    model_visible: Literal[False]
    lifecycle_fingerprint: str
```

```python
ContextSourceLifecycleFact = (
    GenerationRootLifecycleFact
    | AppendOnceLifecycleFact
    | AppendRevisionLifecycleFact
    | AuditOnlyLifecycleFact
)
```

禁止 model-visible `replace_in_place` 或“下一次 compile自动消失”的 ephemeral lifecycle。

### 8.4 Semantic 与 attribution分层

Source revision是generation-neutral canonical identity：

```python
class ImmutableSourceRevisionFact(FrozenFactBase):
    schema_version: Literal["immutable_source_revision.v1"]
    revision_kind: Literal["immutable"]
    source_revision_id: str
    source_state_semantic_fingerprint: str
    revision_fingerprint: str


class EventSourceRevisionFact(FrozenFactBase):
    schema_version: Literal["event_source_revision.v1"]
    revision_kind: Literal["event"]
    source_revision_id: str
    producer_event_semantic_fingerprint: str
    revision_fingerprint: str


class SnapshotSourceRevisionFact(FrozenFactBase):
    schema_version: Literal["snapshot_source_revision.v1"]
    revision_kind: Literal["complete_snapshot"]
    source_revision_id: str
    source_revision_ordinal: int
    predecessor_source_revision_id: str | None
    predecessor_source_revision_fingerprint: str | None
    source_state_semantic_fingerprint: str
    revision_fingerprint: str


class DeltaSourceRevisionFact(FrozenFactBase):
    schema_version: Literal["delta_source_revision.v1"]
    revision_kind: Literal["strict_delta"]
    source_revision_id: str
    source_revision_ordinal: int
    predecessor_source_revision_id: str
    predecessor_source_revision_fingerprint: str
    delta_semantic_fingerprint: str
    resulting_source_state_semantic_fingerprint: str
    revision_fingerprint: str


CanonicalContextSourceRevisionFact = (
    ImmutableSourceRevisionFact
    | EventSourceRevisionFact
    | SnapshotSourceRevisionFact
    | DeltaSourceRevisionFact
)
```

Revision ID/ordinal/predecessor由source domain contract产生，与ProviderInput generation、append index和“是否已经发送”无关。

```python
class ContextSourceCandidateSemanticFact(FrozenFactBase):
    schema_version: Literal["context_source_candidate_semantic.v1"]
    source_id: ContextSourceId
    source_instance_id: str
    candidate_key: str
    source_revision: CanonicalContextSourceRevisionFact
    payload: ContextSourcePayloadSemanticFact
    lifecycle: ContextSourceLifecycleFact
    priority: int
    required: bool
    lowering_intent: ContextCandidateLoweringIntentFact
    model_visible_timing_semantic: ContextSourceTimingSemanticFact | None
    semantic_fingerprint: str
```

```python
class ContextSourceCandidateAttributionFact(FrozenFactBase):
    schema_version: Literal["context_source_candidate_attribution.v1"]
    semantic: ContextSourceCandidateSemanticFact
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...]
    source_absolute_timing: ContextSourceAbsoluteTimingFact | None
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    source_input_authority_fingerprint: str
    physical_input_policy_fingerprint: str
    source_contract_id: str
    source_contract_version: str
    source_contract_fingerprint: str
    fact_fingerprint: str
```

```python
class ContextSectionCandidate(FrozenFactBase):
    schema_version: Literal["context_section_candidate.v2"]
    attribution: ContextSourceCandidateAttributionFact
    candidate_fingerprint: str
```

`candidate_id` 若仅用于 durable carrier定位，放在 attribution/reference层；不得让随机 ID污染 semantic fingerprint。

`ContextSourceCandidateSemanticFact.semantic_fingerprint`描述source selection、canonical revision与lifecycle身份，不直接等于provider content fingerprint。真正provider-visible内容由nested payload semantic、实际model-visible timing semantic与lowering结果计算。Priority、required、source refs与ledger horizons不得仅因存在于candidate而污染provider prefix semantic。

### 8.5 Revision invariant

Canonical source revision必须满足：

- immutable/event revision不读取generation head；
- complete snapshot ordinal由canonical domain revision提供；
- complete snapshot可以跳过未发送的旧snapshot，planner按generation committed head决定是否append；
- strict delta必须精确引用canonical predecessor，缺口为not-ready或authority mismatch；
- 相同source revision ID + 相同revision fingerprint为幂等；
- 相同source revision ID + 不同内容为contract mismatch；
- source attribution可以变化，但revision semantic不能由provider append index、cache或process-local counter推断；
- Provider generation中的 `latest_revision` 与 `supersedes`由generation reducer/planner拥有，不写回source candidate。

## 9. Source ownership matrix

| Source | Canonical input | Lifecycle | 变化处理 |
|---|---|---|---|
| Base system | static instruction fact | generation root | semantic变化 rollover |
| Provider tool schema | capability exposure semantic | `CapabilityToolCatalogRootFact`独立owner | schema/order变化 rollover |
| Memory instruction | versioned static fact | generation root | contract变化 rollover |
| Runtime environment | durable environment fact | append revision | 追加新观察 |
| Runtime clock proposal | frozen compile clock | generation-neutral observation | Provider planner按committed clock head决定是否追加 |
| Memory projection | canonical memory projection | append revision | 追加 superseding revision |
| Capability prose catalog | exposure projection | generation root或append revision，由contract固定 | tool schema变化仍rollover |
| Active skill | capability snapshot | append revision | 追加 enable/disable revision |
| Plan | durable plan revision | append revision | 追加 revision/tombstone |
| Recovery | recovery event/fact | append once | 每个恢复事实一次 |
| Rollout status | durable rollout account | append revision | 中性状态观察 |
| Subagent handoff | spawn/handoff fact | append once | 不原地修改 |
| Subagent result | accepted completion/delivery fact | append once | selection前可省略，发送后保留 |
| MCP diagnostic | installed snapshot diagnostic | append revision | tool schema变化另触发rollover |
| Workspace skill | durable active-skill projection | append revision | 追加 revision |

如果某 source 的旧值与新值共存会产生安全风险或无法用 typed supersession表达，该 source必须使用 generation root/rollover，不得自行删除旧值。

## 10. Timing hard cut

### 10.1 Stable source time

`ContextSourceAbsoluteTimingFact`只保存已发生事实：

```python
class ContextSourceAbsoluteTimingFact(FrozenFactBase):
    schema_version: Literal["context_source_absolute_timing.v1"]
    observed_at_utc: str | None
    source_started_at_utc: str | None
    source_ended_at_utc: str | None
    source_sequence_ranges: tuple[LedgerSequenceRangeFact, ...]
    clock_source: Literal[
        "event_created_at",
        "terminal_observation",
        "artifact_metadata",
        "host_clock",
        "mixed",
    ]
    freshness_kind: Literal[
        "static",
        "current_turn",
        "current_run_tail",
        "historical_replay",
        "compacted_history",
    ]
    timing_contract_fingerprint: str
    fact_fingerprint: str
```

`LedgerSequenceRangeFact`包含runtime session ID、first/last sequence与range fingerprint；每个range必须被candidate的matching authority horizon覆盖。

- observed/start/end UTC；
- source sequence range；
- clock source；
- freshness kind；
- timing contract identity。

该 fact随 candidate首次生成后不再改变。

若absolute timing真实进入provider payload，必须先映射为独立semantic层：

```python
class ContextSourceTimingSemanticFact(FrozenFactBase):
    schema_version: Literal["context_source_timing_semantic.v1"]
    rendered_absolute_time: str
    timing_semantic_kind: Literal[
        "observed_at",
        "source_interval",
        "terminal_observation",
    ]
    rendering_contract_fingerprint: str
    semantic_fingerprint: str
```

若模型不可见，candidate semantic中的该字段必须为 `None`，raw absolute timing只保存在attribution中。

### 10.2 Compile time

`compiled_at_utc`、compiled local date与Inspector age：

- 进入 `ContextCompileInputManifest`；
- 不进入旧 source candidate正文；
- 不进入 generation root；
- 不重写历史 provider unit；
- replay显示历史冻结值，不调用当前时钟。

### 10.3 Model-visible clock

```python
class RuntimeClockProposalPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_clock_proposal_payload.v1"]
    observed_at_utc: str
    timezone_name: str
    local_date: str
    proposal_reason: Literal[
        "compile",
        "user_turn",
        "long_operation_completed",
        "local_date_changed",
        "explicit_temporal_requirement",
    ]
    semantic_fingerprint: str
```

Source只生成generation-neutral proposal。Provider planner读取committed generation clock head与resolved policy后，生成最终provider observation：

```python
class ProviderRuntimeClockObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_runtime_clock_observation_semantic.v1"]
    proposal: RuntimeClockProposalPayloadFact
    append_reason: Literal[
        "generation_start",
        "user_turn",
        "staleness_threshold",
        "long_operation_completed",
        "local_date_changed",
        "explicit_temporal_requirement",
    ]
    supersedes_clock_unit_semantic_fingerprint: str | None
    semantic_fingerprint: str
```

Clock policy作为 resolved、versioned fact冻结，由Provider planner执行：

- 首个 generation call必须有 clock observation；
- 新 user turn默认追加一次；
- blocking operation超过 resolved threshold后追加；
- local date变化必须追加；
- source不读取generation clock head，也不自行分配supersession；
- 同一 resolved model call retry复用原 observation；
- 不为每个旧 section生成 `age_seconds`；
- clock unit不得插入未闭合 tool-call/result pairing group中间。

阶段一 compatibility compiler可以暂时只渲染本次 selected clock candidate，但必须把它放在 dynamic tail，不再为历史 section生成 timing header。阶段二开始保留 generation内全部已发送 clock revision。

## 11. Source collection algorithm

### 11.1 输入冻结

Live collector先冻结：

1. `ContextFactSnapshot`与canonical per-ledger authority horizons；
2. required artifacts/documents；
3. source registry fact/bindings；
4. compile clock fact；
5. per-ledger authority horizons；
6. 每个source对应的discriminated `ContextSourceCollectInput`。

Source函数运行期间不得重新读取 ledger、memory、graph或当前 policy。

### 11.2 Deterministic collection

```text
for source binding ordered by source_id:
    build the exact typed source-input branch
    validate source/input discriminators and authority horizons
    collect typed candidate semantic + attribution
    validate source contract exact binding
    validate revision/lifecycle matrix
    append to candidate set

validate unique (source_id, source_instance_id, candidate_key, source_revision)
apply candidate collection policy before provider append
freeze collection decision
```

Source输出顺序不直接决定 provider order。Provider planner按固定 lane、pairing group、priority和semantic key统一排序。

### 11.3 Candidate cache

现有 session-owned lifecycle cache继续是可丢弃优化：

- key覆盖source contract、semantic input、lifecycle policy；
- cache hit必须与 freshly derived attribution/semantic identity精确join；
- cache异常等价普通 miss；
- cache不保存当前 clock替代品；
- entry/bytes双上界LRU；
- eviction不影响 correctness或replay。

## 12. 第一阶段 PR 顺序

### CS0：增量生命周期公共契约

- 新增 source semantic/attribution/lifecycle DTO；
- 冻结 generation root、append once、append revision、audit-only matrix；
- 冻结 clock observation contract；
- 冻结resolved source physical policy、inline/artifact carrier与production model doctor；
- 新增 registry binding API；
- architecture tests禁止新的 model-visible ephemeral candidate；
- 不改变 production payload。

### CS1：稳定 source迁移

- base system；
- memory static instruction；
- capability prose catalog与独立tool catalog root attribution；
- runtime environment static部分；
- shadow compare旧/新语义payload。

### CS2：动态 source迁移

- memory projection；
- active skill/workspace skill；
- plan/recovery；
- rollout status；
- subagent handoff/results；
- MCP diagnostics；
- 完整 canonical revision/predecessor测试；generation supersession留给IP planner。

### CS3：Timing ownership hard cut

- 删除 transcript/non-transcript动态 timing header；
- compile time移到 manifest/Inspector；
- model-visible clock成为独立 runtime source；
- exact replay不重算 clock；
- timing payload变化的golden tests显式更新。

### CS4：Production registry switch

- snapshot/live collector只调用registry；
- compiler只接收统一 `ContextSectionCandidate v2`；
- lifecycle/budget/lowering消费typed payload；
- manifest与Inspector保存source semantic + attribution；
- old/new shadow parity完成后切换唯一生产入口。

### CS5：删除 legacy ownership

- 删除 `ContextCandidateCollectionInput`；
- 删除 `_ContextCandidateSourceText`；
- 删除 AgentRuntime component prompt strings；
- 删除旧 `build_context_candidate_authorities()`字符串重包装；
- 删除 legacy source wrappers与tests；
- 加 module-level import/AST/grep guards。

每个 PR 必须独立全绿；CS0-CS3期间的shadow代码只读，不写 durable candidate，不改变selection/latch/retry。

## 13. 第一阶段完成定义

- 每个non-tool、non-transcript model-visible unit有唯一ContextSource ID、contract与canonical refs；
- 每个provider tool definition只归属 `CapabilityToolCatalogRootFact`；
- source不返回 `str` section facade、`LLMMessage`或provider JSON；
- production不存在AgentRuntime直接拼接non-transcript prompt；
- source candidate生命周期足以无修改供阶段二消费；
- source canonical revision与semantic identity不读取generation head，同一canonical source fact跨generation保持相同identity；
- model-visible动态 timing header为零；
- current clock只有独立typed candidate；
- candidate semantic不包含event sequence、artifact placement或process build fingerprint；
- source registry输出顺序确定；
- unknown historical source contract fail closed；
- full ContextSource测试、全量pytest、Ruff与diff-check通过。

---

# 第二阶段：Incremental ProviderInput Generation Hard Cut

## 14. 第二阶段目标

第二阶段完成后：

- 每个连续 model call复用上一调用的 committed provider prefix；
- 新 context只作为 append batch加入；
- accepted model output按terminal projection + disposition追加；
- history rewrite只能通过显式 rollover；
- provider adapter与manifest消费同一份canonical input plan；
- retry/restart不调用当前时钟或当前source renderer重建历史；
- full compiler reconstruction不再是production输入owner。

## 15. Generation compatibility

### 15.1 Compatibility fact

```python
class ProviderVisibleInputCompatibilityFact(FrozenFactBase):
    schema_version: Literal["provider_visible_input_compatibility.v1"]
    requested_model_identity: str
    provider_api_kind: str
    adapter_input_contract_id: str
    adapter_input_contract_version: str
    adapter_input_contract_fingerprint: str
    tool_order_contract_fingerprint: str
    transcript_lowering_contract_fingerprint: str
    context_source_lowering_contract_fingerprint: str
    provider_input_framing_contract_fingerprint: str
    semantic_fingerprint: str


class ProviderInputGenerationCompatibilityFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_compatibility.v1"]
    provider_visible: ProviderVisibleInputCompatibilityFact
    system_instruction_semantic_fingerprint: str
    tool_catalog_semantic_fingerprint: str
    compatibility_fingerprint: str
```

Global source registry与historical decoder/source/lowering bindings都不进入generation compatibility。它们属于replay attribution，而且只覆盖该generation实际引用的schemas/contracts：

```python
class ProviderInputReplayBindingIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_input_replay_binding_identity.v1"]
    binding_kind: Literal[
        "event_schema",
        "context_source",
        "provider_lowering",
        "artifact_codec",
    ]
    contract_id: str
    contract_version: str
    schema_or_contract_fingerprint: str
    identity_fingerprint: str


class ProviderInputReplayBindingSetNodeReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_replay_binding_set_node_reference.v1"]
    node_kind: Literal["leaf", "internal"]
    first_identity_fingerprint: str
    last_identity_fingerprint: str
    subtree_binding_count: int
    subtree_binding_accumulator: str
    artifact_reference: ContextArtifactReferenceFact
    reference_fingerprint: str


class ProviderInputReplayBindingSetReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_replay_binding_set_reference.v1"]
    binding_count: int
    ordered_binding_accumulator: str
    root_node_ref: ProviderInputReplayBindingSetNodeReferenceFact | None
    set_contract_fingerprint: str
    reference_fingerprint: str
```

Binding set同样使用content-addressed persistent nodes。新增无关AgentEvent/schema不会改变现有generation compatibility，也不触发rollover；只有实际被root或unit引用的新binding才通过append COW加入replay set。Historical binding无法rebind时exact replay fail closed，但provider-visible prefix semantic不变。

不得包含：

- resolved model call ID；
- model call index；
- run/session随机ID，除非真实发给模型；
- `compiled_at_utc`；
- credential/endpoint secret；
- reported model identity；
- artifact/event物理sequence；
- output usage；
- implementation build fingerprint。

Output cap、timeout等不改变provider-visible input tokenization的字段不进入prefix semantic；若provider cache partition contract明确要求，可进入单独cache cohort identity，不应无理由强迫generation rollover。

### 15.2 Generation scope

```python
class SessionWindowGenerationScopeFact(FrozenFactBase):
    schema_version: Literal["session_window_generation_scope.v1"]
    scope_kind: Literal["session_window"]
    runtime_session_id: str
    context_window_id: str
    context_window_generation: int
    scope_fingerprint: str


class OneShotGenerationScopeFact(FrozenFactBase):
    schema_version: Literal["one_shot_generation_scope.v1"]
    scope_kind: Literal["one_shot"]
    operation_kind: Literal[
        "direct_model_call",
        "window_summarizer",
        "governance_model_call",
    ]
    operation_id: str
    attempt_index: int
    scope_fingerprint: str


ProviderInputGenerationScopeFact = (
    SessionWindowGenerationScopeFact
    | OneShotGenerationScopeFact
)


class ProviderInputGenerationFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation.v1"]
    generation_id: str
    call_lane: Literal[
        "main_agent",
        "subagent",
        "direct_one_shot",
        "window_summarizer",
        "governance_one_shot",
    ]
    scope: ProviderInputGenerationScopeFact
    compatibility: ProviderInputGenerationCompatibilityFact
    predecessor_generation_id: str | None
    predecessor_generation_fingerprint: str | None
    rollover_reason: ProviderInputRolloverReason | None
    generation_fingerprint: str
```

Main agent与subagent各自RuntimeSession拥有 `SessionWindowGenerationScopeFact`。Direct/window/governance V1使用 `OneShotGenerationScopeFact`，不创建synthetic window identity，也不强制跨调用复用。

同一generation同一时刻最多一个model dispatch reservation。需要并行model calls时必须使用不同call lane/generation，不能从同一revision分叉后同时提交。

Generation ID、scope、call lane业务归因与predecessor identity属于durable generation attribution，不进入provider-visible semantic fingerprint。Provider内容相同的两个generation可以拥有相同root/input semantic identity，但仍拥有不同的 `generation_fingerprint`。

## 16. Generation root

### 16.1 Root内容

Generation root包含generation内不可变的provider-visible基础：

- system/developer instructions；
- tool catalog与canonical order；
- adapter/provider input framing semantics；
- compaction后normalized transcript baseline refs；
- generation开始时必须存在的source root candidates；
- lowering contract identities。

当前时间、run ID、call ID、dynamic memory、plan、status不得进入root。

Provider tool definitions由独立owner冻结：

```python
class ContextToolSpecReferenceFact(FrozenFactBase):
    schema_version: Literal["context_tool_spec_reference.v1"]
    descriptor_id: str
    descriptor_fingerprint: str
    model_tool_name: str
    input_schema_semantic_fingerprint: str
    result_render_contract_fingerprint: str
    materialized_schema_artifact_ref: ContextArtifactReferenceFact
    reference_fingerprint: str


class CapabilityToolCatalogRootSemanticFact(FrozenFactBase):
    schema_version: Literal["capability_tool_catalog_root_semantic.v1"]
    ordered_tool_spec_semantic_fingerprints: tuple[str, ...]
    tool_order_contract_fingerprint: str
    tool_schema_lowering_contract_fingerprint: str
    semantic_fingerprint: str


class CapabilityToolCatalogRootFact(FrozenFactBase):
    schema_version: Literal["capability_tool_catalog_root.v1"]
    semantic: CapabilityToolCatalogRootSemanticFact
    capability_exposure_semantic_fingerprint: str
    tool_spec_refs: tuple[ContextToolSpecReferenceFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    fact_fingerprint: str
```

ContextSource的capability prose catalog不得复制tool JSON schema。`ContextMaterializedToolSpecInput`、tool root semantic与adapter最终tools三方必须逐项join；tool schema/order变化触发rollover。

### 16.2 Semantic与materialization分离

```python
class ProviderInputGenerationRootSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_root_semantic.v1"]
    root_unit_count: int
    root_ordered_unit_accumulator: str
    root_unit_vector_semantic_fingerprint: str
    root_lowering_contract_fingerprint: str
    tool_catalog_root_semantic_fingerprint: str
    root_semantic_fingerprint: str
```

```python
class ProviderInputGenerationRootReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_root_reference.v1"]
    generation: ProviderInputGenerationFact
    root_semantic: ProviderInputGenerationRootSemanticFact
    tool_catalog_root: CapabilityToolCatalogRootFact
    initial_unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    root_artifact_id: str
    root_artifact_sha256: str
    root_artifact_bytes: int
    root_artifact_contract_fingerprint: str
    reference_fingerprint: str
```

Artifact/root layout不进入semantic fingerprint。

### 16.3 Committed core、preparation ownership与attribution

```python
class ProviderInputReconciliationReason(StrEnum):
    COMMIT_OUTCOME_PARTIAL = "commit_outcome_partial"
    COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"
    COMMITTED_EVENT_CONFLICT = "committed_event_conflict"
    MODEL_START_JOIN_MISMATCH = "model_start_join_mismatch"
    REDUCER_STATE_MISMATCH = "reducer_state_mismatch"
    REQUIRED_ARTIFACT_UNTRUSTED = "required_artifact_untrusted"


class ProviderInputTranscriptFrontierFact(FrozenFactBase):
    schema_version: Literal["provider_input_transcript_frontier.v1"]
    transcript_window_semantic_fingerprint: str
    stable_entry_count: int
    stable_entry_accumulator: str
    last_stable_entry_semantic_fingerprint: str | None
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    frontier_fingerprint: str


class ProviderInputCommittedSourceHeadFact(FrozenFactBase):
    schema_version: Literal["provider_input_committed_source_head.v1"]
    source_id: ContextSourceId
    source_instance_id: str
    candidate_key: str
    canonical_source_revision: CanonicalContextSourceRevisionFact
    candidate_semantic_fingerprint: str
    appended_unit_semantic_fingerprint: str
    committed_append_index: int
    head_fingerprint: str


class ProviderInputClockHeadFact(FrozenFactBase):
    schema_version: Literal["provider_input_clock_head.v1"]
    observation_semantic_fingerprint: str
    observed_at_utc: str
    committed_append_index: int
    head_fingerprint: str


class ProviderInputPendingContinuationFact(FrozenFactBase):
    schema_version: Literal["provider_input_pending_continuation.v1"]
    resolved_model_call_id: str
    terminal_projection_reference: TerminalProjectionReferenceFact
    accepted_disposition_event_ref: ContextEventReferenceFact
    continuation_semantic_fingerprint: str
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    continuation_fingerprint: str


class ProviderInputAwaitingControlDispositionFact(FrozenFactBase):
    schema_version: Literal["provider_input_awaiting_control_disposition.v1"]
    resolved_model_call_id: str
    terminal_projection_reference: TerminalProjectionReferenceFact
    model_terminal_event_ref: ContextEventReferenceFact
    terminal_projection_committed_event_ref: ContextEventReferenceFact
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    awaiting_fingerprint: str


class ProviderInputPreparationOwnershipFact(FrozenFactBase):
    schema_version: Literal["provider_input_preparation_ownership.v1"]
    preparation_id: str
    ownership_kind: Literal["initial_start", "existing_append", "rollover_start"]
    generation_id: str
    scope_fingerprint: str
    expected_predecessor_scope_binding_fingerprint: str
    resulting_scope_binding_fingerprint: str
    expected_committed_core_state_fingerprint: str | None
    expected_revision: int
    append_batch_reference_fingerprint: str
    provider_input_plan_fingerprint: str
    resolved_model_call_id: str
    stable_companion_event_ids: tuple[str, ...]
    ownership_fingerprint: str


class ProviderInputPreparationOwnershipAttributionFact(FrozenFactBase):
    schema_version: Literal["provider_input_preparation_ownership_attribution.v1"]
    ownership: ProviderInputPreparationOwnershipFact
    context_compiled_event_ref: ContextEventReferenceFact
    attribution_fingerprint: str


class CommittedProviderInputGenerationCoreStateFact(FrozenFactBase):
    schema_version: Literal["committed_provider_input_generation_core_state.v1"]
    generation: ProviderInputGenerationFact
    root_reference: ProviderInputGenerationRootReferenceFact
    status: Literal["open", "closing", "closed", "reconciliation_latched"]
    revision: int
    next_append_index: int
    committed_prefix_fingerprint: str
    unit_count: int
    unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    committed_authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    transcript_frontier: ProviderInputTranscriptFrontierFact
    committed_source_heads: tuple[ProviderInputCommittedSourceHeadFact, ...]
    clock_head: ProviderInputClockHeadFact | None
    awaiting_control_disposition: ProviderInputAwaitingControlDispositionFact | None
    accepted_but_not_appended_continuation: ProviderInputPendingContinuationFact | None
    reconciliation_reason: ProviderInputReconciliationReason | None
    core_state_fingerprint: str


class ProviderInputGenerationAttributionStateFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_attribution_state.v1"]
    core_state: CommittedProviderInputGenerationCoreStateFact
    latest_model_start_event_ref: ContextEventReferenceFact | None
    latest_model_start_committed_core_fingerprint: str | None
    close_or_rollover_event_ref: ContextEventReferenceFact | None
    attribution_fingerprint: str
```

Core state invariant：

- `next_append_index == revision + 1`；
- source heads按 `(source_id, source_instance_id, candidate_key)`排序且唯一；
- 每个source head的append index小于 `next_append_index`；
- unit count/vector root/prefix accumulator三方一致；
- committed authority horizon set等于root与全部committed unit attribution horizons按ledger取最大through sequence后的persistent canonical union；
- replay binding set等于root与全部committed unit实际引用bindings的persistent canonical union；
- transcript frontier的每个exact horizon都必须被committed authority horizon set中的matching ledger horizon覆盖；
- clock head必须对应vector中的最后一个committed clock unit；
- awaiting disposition与pending continuation各最多一个且互斥；completed terminal FULL先安装awaiting，ACCEPTED再消费它并建立pending continuation，SUPPRESSED只消费awaiting；
- reducer处理disposition只使用core中awaiting terminal reference与disposition call/control identity，不回查EventLog；
- closed state不接受新append；若仍有accepted continuation，必须保留其fingerprint并由successor root/canonical transcript显式接管；
- reconciliation-latched state禁止新prepare；
- `status=reconciliation_latched`当且仅当 `reconciliation_reason`非空；其他三个status必须为 `None`；
- PARTIAL/UNKNOWN只使用对应reason；event/payload/artifact/reducer冲突使用各自稳定reason，禁止自由字符串；
- generation root、vector root、prefix与core state fingerprint由唯一factory构造；core fingerprint禁止覆盖ModelStart、ContextCompiled、close或rollover event ref。

Attribution invariant：

- outer attribution fingerprint覆盖core fingerprint与可空event refs；
- ModelStart payload只保存resulting core state fingerprint，不保存outer attribution fingerprint；
- latest ModelStart ref在commit FULL后构造，因此不能反向参与同一ModelStart payload或core fingerprint；
- latest ModelStart ref与copied committed-core fingerprint all-null/all-present，并逐字段等于hydrated `CommittedProviderInputReferenceFact`；后续terminal/disposition可推进current core，不要求它仍等于latest-start core；
- close/rollover refs同理只改变attribution envelope，不改已经冻结的provider semantic/core identity。

唯一有向fingerprint DAG：

```text
committed core fingerprint
    -> append committed event fingerprint
    -> CommittedProviderInputReferenceFact
    -> ModelStart payload/stored-event fingerprint
    -> ModelStart ContextEventReferenceFact
    -> generation attribution fingerprint
```

任何反向edge均由schema validator与architecture test拒绝。

Preparation ownership不属于committed generation core。`ProviderInputPreparationOwnershipFact`不含ContextCompiled event ref，因此可以在构造ContextCompiled stable payload前重算；其outer attribution只在ContextCompiled FULL后由reducer安装。

Ownership也不得保存outer `PreparedProviderInputAppendCandidateFact.candidate_fingerprint`；prepared candidate嵌套ownership，反向保存candidate fingerprint会形成第二个递归环。Ownership通过batch、plan、call、scope与stable companion IDs唯一确定，outer candidate单向引用ownership fingerprint。

Ownership kind矩阵：

- `initial_start`：expected committed core必须为null，expected revision为0，scope当前无active generation；
- `existing_append`：expected core required，generation ID等于active generation，revision/prefix由该core确定；
- `rollover_start`：generation ID是new generation，expected core required且属于old active generation；old/new identity的完整双边CAS保存在rollover guard；
- 三类ownership均要求append/plan/call/companion IDs逐项相等；不同kind不得通过null字段模拟；
- ContextCompiled安装ownership时CAS predecessor scope binding，并产生ownership内预计算的resulting binding；commit guard的expected scope binding必须等于该resulting fingerprint；

Preparation CAS basis固定为二元组：

```text
(expected committed core fingerprint, expected preparation ownership fingerprint)
```

ContextCompiled FULL只让第二项成为durable active ownership，不改变第一项。LLMRuntime `commit_start()`必须同时比较两项；只比较其中一项或把ownership attribution fingerprint误当core均为contract mismatch。

Process-local physical commit owner单独保存，不伪装成durable preparation ownership：

```python
@dataclass(frozen=True, slots=True)
class ProviderInputPendingAppendOwner:
    generation_id: str
    expected_committed_core_state_fingerprint: str | None
    expected_preparation_ownership_fingerprint: str
    expected_revision: int
    stable_append_candidate_fingerprint: str
    append_batch_reference: ProviderInputAppendBatchReferenceFact
    resolved_model_call_id: str
    phase: Literal[
        "prepared",
        "commit_inflight",
        "reconciliation_required",
    ]
```

RuntimeSession live view由committed core、attribution envelope、durable preparation ownership与process-local physical owner组成。ContextCompiled FULL后的preparation ownership不可丢弃；commit UNKNOWN时physical owner同样保留到confirmation。Restart先恢复ownership attribution，再按stable event IDs确认ModelStart/append/abandonment，不从process-local owner猜测结果。

### 16.4 唯一 reducer

Rollovers会在一个atomic batch内同时改变old/new generation，因此production唯一入口必须是batch reducer，而不是让caller逐event手工路由：

```python
class ProviderInputGenerationScopeBindingFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_scope_binding.v1"]
    scope_fingerprint: str
    active_generation_id: str | None
    latest_closed_generation_id: str | None
    active_preparation_id: str | None
    binding_fingerprint: str


@dataclass(frozen=True, slots=True)
class ProviderInputGenerationReducerWorkingSet:
    generation_core_states: tuple[CommittedProviderInputGenerationCoreStateFact, ...]
    generation_attributions: tuple[ProviderInputGenerationAttributionStateFact, ...]
    preparation_ownerships: tuple[ProviderInputPreparationOwnershipAttributionFact, ...]
    scope_bindings: tuple[ProviderInputGenerationScopeBindingFact, ...]
    folded_authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    working_set_fingerprint: str


def reduce_provider_input_generation_batch(
    working_set: ProviderInputGenerationReducerWorkingSet,
    committed_events: tuple[FrozenStoredEvent, ...],
) -> ProviderInputGenerationReducerWorkingSet: ...
```

`generation_core_states`按generation ID排序且只包含本批可能触及的active/old/new cores；它是RuntimeSession reducer的immutable working set，不要求把所有历史closed generation常驻内存。Store按exact generation ID加载本批predecessor core/attribution，batch FULL后原子写回core、attribution、ownership与scope binding。Closed states可进入bounded LRU；durable materialized row与EventLog仍可恢复。

所有ordinary/initial/rollover prepared identity统一保存在独立 `preparation_ownerships`；scope binding只保存deterministic `active_preparation_id`。Existing append的ownership引用expected committed core，initial为null，rollover同时引用old core并携带new generation identity。两处不得重复嵌套同一ownership；同一scope最多一个prepared candidate，candidate中的generation/preparation ID在prepare时已经稳定分配，禁止ModelStart失败后生成另一组ID。

`preparation_id = H(scope fingerprint, generation ID, resolved model call ID, append index, ownership kind)`，不覆盖ownership或scope-binding fingerprint。Ownership与resulting scope binding都单向引用该ID；scope binding不得直接嵌入ownership fingerprint。

Reducer消费：

- 包含prepared append candidate的 `ContextCompiledEvent`；
- generation started/append/rollover/closed events；
- `ProviderInputPreparationAbandonedEvent` union；
- 与append同批的ModelStart；
- terminal projection committed；
- model control disposition resolved；
- relevant compaction/window terminal facts。

Reducer职责：

- generation started建立root/vector/prefix genesis，并从generation-root ContextSource units初始化committed source heads（append index 0）；
- append FULL推进revision、vector、frontier、source heads与clock head；
- ContextCompiled FULL安装唯一preparation ownership attribution并推进scope binding；相同ownership幂等，不同ownership在同scope冲突；
- append + ModelStart FULL在同一working-set副本中消费matching ownership、推进committed core，并在core已冻结后生成ModelStart attribution envelope；整个batch一次发布；
- pre-start abandonment只终结exact preparation ownership，不推进committed revision/prefix/source heads；
- completed terminal projection先建立awaiting disposition；ACCEPTED disposition消费awaiting并建立pending continuation；
- SUPPRESSED disposition消费awaiting且不建立continuation；
- 后续append精确消费并清除pending continuation；
- suppressed/error/cancel不创建continuation；
- rollover batch按 `old close -> rollover resolved -> new start -> initial append -> ModelStart`验证，在working-set副本中关闭旧state并建立/推进new state；批中间态不发布；
- payload conflict、double append、continuation duplicate或unknown domain event fail closed。

Terminal/disposition中间态矩阵：

| 输入 | predecessor | resulting core |
|---|---|---|
| main/subagent completed terminal projection FULL | awaiting为空 | 安装exact terminal refs到awaiting |
| ACCEPTED disposition FULL | awaiting.call ID与disposition完全一致 | 清awaiting，建立pending continuation |
| SUPPRESSED disposition FULL | awaiting.call ID与disposition完全一致 | 清awaiting，不建continuation |
| provider_error/cancelled/runtime_error terminal | awaiting为空 | 不建awaiting/continuation |
| one-shot completed terminal + generation close FULL | one-shot scope | 不建awaiting/continuation；operation-specific result由其owner消费 |
| disposition缺awaiting、call ID冲突或第二个completed terminal | 任意 | contract mismatch / latch |

`ModelCallControlDispositionResolvedEvent`无需复制完整terminal projection；它必须携带resolved call ID与terminal control identity，reducer使用core中冻结的exact projection reference完成join。RunEnd或下一ModelStart在awaiting非空时fail closed。Direct/window/governance one-shot不伪造Agent control disposition，并在terminal batch内直接关闭generation。

Live commit fold、restart recovery、exact replay、Inspector projection与PostgreSQL materialized rows全部调用同一个batch reducer。单event replay也必须包装成长度1的batch，不另建第二入口。Committed-core row、attribution row与preparation-ownership row都只是reducer cache，必须保存各自fingerprint与latest carrier event ID；不允许其他路径手工更新source heads/frontier/prefix或event attribution。

## 17. Provider input unit

### 17.1 Unit semantic

```python
class ProviderInputUnitSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_semantic.v1"]
    unit_kind: Literal[
        "transcript_message",
        "tool_pair",
        "context_source",
        "runtime_clock",
        "rollup_observation",
        "recovery_observation",
    ]
    provider_content_semantic_fingerprint: str
    lowering_contract_id: str
    lowering_contract_version: str
    lowering_contract_fingerprint: str
    provider_lane: str
    pairing_group_id: str | None
    semantic_fingerprint: str
```

Owner semantic identity来源：

- transcript：normalized message/pair/terminal projection semantic；
- context source：`ContextSourceCandidateSemanticFact`；
- clock：runtime clock source semantic；
- rollup：durable rollup semantic；
- accepted assistant：terminal projection semantic + ACCEPTED disposition semantic join。

Owner semantic identity保存在unit attribution层。`provider_content_semantic_fingerprint`只覆盖实际provider-visible typed content。Source priority、required、revision number与attribution若不改变实际rendered content，不得改变它；若revision marker或supersession text真实发送给模型，则对应marker必须进入provider content semantic。

### 17.2 Unit attribution

```python
class ProviderInputUnitAttributionFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_attribution.v1"]
    semantic: ProviderInputUnitSemanticFact
    owner_semantic_fingerprint: str
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...]
    fact_fingerprint: str
```

逐 unit authority 不变量：

- `source_event_refs` 与 `source_artifact_refs` 必须是该 unit 正文的 exact refs，禁止留空后复制全局 attribution；
- 每个event ref的ledger必须且只能由该unit的 `authority_horizons` 覆盖；horizon来自canonical ledger-prefix proof，不来自selection query布局；
- transcript lowering必须保持每个provider message到normalized source message/tool-result/rollup member refs的process-local映射，再冻结进unit；不得把整条lane的refs复制给lane内所有message；
- source unit逐字段采用对应 `CompiledProviderSourceFragment.candidate.attribution`；tool unit采用descriptor exact source event；
- append aggregate horizons只由predecessor与new units的per-unit horizons做canonical union；`ProviderInputAppendCommittedEvent`不得保存一份可与nested append reference分叉的outer tuple，若V1保留该字段则必须严格相等。

### 17.3 Provider materialization

```python
class ProviderInputTextBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_text_block.v1"]
    block_kind: Literal["text"]
    text: str
    utf8_bytes: int
    semantic_fingerprint: str


class ProviderInputDataBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_data_block.v1"]
    block_kind: Literal["data"]
    media_type: str
    canonical_data: FrozenJsonValue
    semantic_fingerprint: str


class ProviderInputToolCallBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_tool_call_block.v1"]
    block_kind: Literal["tool_call"]
    tool_call_id: str
    model_tool_name: str
    arguments_state: Literal["valid_object", "invalid_json", "non_object_json"]
    canonical_arguments: FrozenJsonObjectFact | None
    raw_arguments_json: str
    parse_error_code: str | None
    semantic_fingerprint: str


class ProviderInputToolResultBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_tool_result_block.v1"]
    block_kind: Literal["tool_result"]
    tool_call_id: str
    model_tool_name: str
    result_state: Literal["success", "error", "interrupted", "denied"]
    terminal_projection_semantic_fingerprint: str
    content: tuple[ProviderInputTextBlockFact | ProviderInputDataBlockFact, ...]
    semantic_fingerprint: str


ProviderInputContentBlockFact = (
    ProviderInputTextBlockFact
    | ProviderInputDataBlockFact
    | ProviderInputToolCallBlockFact
    | ProviderInputToolResultBlockFact
)


class ProviderSystemInstructionFragmentFact(FrozenFactBase):
    schema_version: Literal["provider_system_instruction_fragment.v1"]
    fragment_kind: Literal["system_instruction"]
    content_blocks: tuple[ProviderInputContentBlockFact, ...]
    semantic_fingerprint: str


class ProviderMessageFragmentFact(FrozenFactBase):
    schema_version: Literal["provider_message_fragment.v1"]
    fragment_kind: Literal["message"]
    role: Literal["system", "user", "assistant", "tool"]
    name: str | None
    content_blocks: tuple[ProviderInputContentBlockFact, ...]
    semantic_fingerprint: str


class ProviderToolCatalogFragmentFact(FrozenFactBase):
    schema_version: Literal["provider_tool_catalog_fragment.v1"]
    fragment_kind: Literal["tool_catalog"]
    tool_catalog_root: CapabilityToolCatalogRootSemanticFact
    semantic_fingerprint: str


ProviderInputTypedFragmentFact = (
    ProviderSystemInstructionFragmentFact
    | ProviderMessageFragmentFact
    | ProviderToolCatalogFragmentFact
)


class InlineProviderInputFragmentFact(FrozenFactBase):
    schema_version: Literal["inline_provider_input_fragment.v1"]
    fragment_kind: str
    provider_content_semantic_fingerprint: str
    canonical_typed_fragment: ProviderInputTypedFragmentFact
    utf8_bytes: int
    fragment_fingerprint: str


class ArtifactProviderInputFragmentReferenceFact(FrozenFactBase):
    schema_version: Literal["artifact_provider_input_fragment_reference.v1"]
    fragment_kind: str
    provider_content_semantic_fingerprint: str
    artifact_reference: ContextArtifactReferenceFact
    codec_contract_fingerprint: str
    reference_fingerprint: str


ProviderInputFragmentCarrierFact = (
    InlineProviderInputFragmentFact
    | ArtifactProviderInputFragmentReferenceFact
)
```

Tool-call argument nullability必须与arguments state精确join；tool-result content必须逐项jointerminal projection document。Provider-specific wire JSON只能由historical adapter binding从上述typed fragment生成，不得成为第二semantic truth。

Typed fragment中央不变量：

- text的UTF-8 bytes与semantic fingerprint从正文重算；data只允许canonical `FrozenJsonValue`且media type来自typed owner；
- tool call的 `valid_object`要求canonical arguments非空且parse error为空，另外两态要求canonical arguments为空并保存bounded raw JSON与稳定parse code；
- tool result的name/state/content逐项等于hydrated terminal projection，禁止从serialized result重新推断；
- message role/name/content与system/tool fragment使用versioned nullability矩阵；
- block/fragment tuple有固定count与aggregate bytes上界；超限内容转artifact carrier，不截断semantic content；
- 每个concrete block/fragment都由唯一factory重算自身fingerprint，全部 `frozen=True`、`extra="forbid"`。

```python
class ProviderInputUnitMaterializationFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_materialization.v1"]
    attribution: ProviderInputUnitAttributionFact
    canonical_provider_fragment: ProviderInputFragmentCarrierFact
    estimated_tokens: int
    materialization_fingerprint: str
```

Provider fragment必须是typed message/tool/instruction结构，不得退回自由 JSON。Artifact只存放bounded/root之外的大内容，hydrator必须按historical codec/contract验证。

### 17.4 Compiler allocation与provider payload唯一真源

Compiler在section allocation、compact与omit完成后，必须把每个被接纳的non-transcript source冻结为process-local typed carrier：

```python
@dataclass(frozen=True, slots=True)
class CompiledProviderSourceFragment:
    candidate: ContextSectionCandidate
    render_mode: ContextRenderMode
    provider_lane: str
    message: LLMMessage
    estimated_tokens: int
```

`ProviderInput` planner只允许消费这些exact fragments与compiler冻结的transcript provider projection；禁止再次遍历 `prepared_candidates`、再次render source正文或忽略 `included/render_mode`。若已committed source在后续compile被省略且不能与旧prefix安全共存，planner必须typed rollover，不得悄悄保留一套compiler未计量的wire payload。

Plan artifact materialize出的 `RecursivelyImmutableProviderInputCarrier` 是最终provider payload与budget真源。Manifest、`ContextCompiledEvent.budget`、Long-Horizon pressure/compaction measurement及 `provider_neutral_payload_fingerprint` 必须从同一carrier重算；exact replay也调用同一个pure carrier-binding helper。Compiler的pre-plan estimate只能用于allocation，不得作为最终wire estimate继续持久化。

## 18. Append batch 与prefix accumulator

### 18.1 Append batch

```python
class ProviderInputAppendSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_input_append_semantic.v1"]
    ordered_unit_semantic_fingerprints: tuple[str, ...]
    append_ordering_contract_fingerprint: str
    semantic_fingerprint: str
```

```python
class ProviderInputAppendBatchReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_append_batch_reference.v1"]
    generation: ProviderInputGenerationFact
    expected_generation_revision: int
    append_index: int
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    append_semantic: ProviderInputAppendSemanticFact
    batch_artifact_id: str
    batch_artifact_sha256: str
    batch_artifact_bytes: int
    changed_vector_node_refs: tuple[ProviderInputUnitVectorNodeReferenceFact, ...]
    resulting_unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    resulting_authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    new_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...]
    resulting_replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    predecessor_prefix_fingerprint: str
    resulting_prefix_fingerprint: str
    reference_fingerprint: str
```

### 18.2 Prefix hash chain

```text
prefix_0 = H(
    "provider-input-prefix:v1",
    provider_visible_compatibility_semantic_fingerprint,
    generation_root_semantic_fingerprint,
)

prefix_(n+1) = H(
    "provider-input-prefix:v1",
    prefix_n,
    append_semantic_fingerprint,
)
```

Append semantic fingerprint只覆盖ordered provider unit semantics与ordering contract，因此：

- batch内顺序变化必然改变prefix；
- generation ID/revision、append index与source authority horizons不污染prefix semantic；
- artifact ID/layout变化不改变semantic prefix；
- materialization变化必须改变lowering contract或unit semantic，不能只改artifact；
- prefix hash chain不是完整wire payload fingerprint的替代品。

`provider_visible_compatibility_semantic_fingerprint`必须精确等于generation compatibility中nested `ProviderVisibleInputCompatibilityFact.semantic_fingerprint`。Outer generation compatibility、source registry attribution或generation ID不得代替它。

### 18.3 Full ProviderInputPlan

```python
PROVIDER_INPUT_VECTOR_LEAF_MAX_UNITS = 128
PROVIDER_INPUT_VECTOR_INTERNAL_MAX_CHILDREN = 64
PROVIDER_INPUT_VECTOR_MAX_HEIGHT = 8
PROVIDER_INPUT_APPEND_MAX_UNITS = 512
PROVIDER_INPUT_APPEND_MAX_CHANGED_LEAVES = 5
PROVIDER_INPUT_APPEND_MAX_CHANGED_NODES = 41
PROVIDER_INPUT_APPEND_MAX_ARTIFACTS = 42
PROVIDER_INPUT_ROOT_REFERENCE_MAX_BYTES = 65_536


class ResolvedProviderInputVectorPhysicalPolicyFact(FrozenFactBase):
    schema_version: Literal["resolved_provider_input_vector_physical_policy.v1"]
    max_unit_materialization_reference_bytes: int
    max_node_reference_bytes: int
    leaf_structural_overhead_bytes: int
    internal_node_structural_overhead_bytes: int
    append_artifact_structural_overhead_bytes: int
    max_vector_node_artifact_bytes: int
    max_append_batch_artifact_bytes: int
    max_changed_node_artifact_bytes: int
    max_append_operation_artifact_bytes: int
    max_confirmation_batches: int
    policy_fingerprint: str


class ProviderInputUnitVectorLeafFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_vector_leaf.v1"]
    first_ordinal: int
    unit_materializations: tuple[ProviderInputUnitMaterializationFact, ...]
    accumulator_before: str
    accumulator_after: str
    leaf_fingerprint: str


class ProviderInputUnitVectorInternalNodeFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_vector_internal_node.v1"]
    height: int
    first_ordinal: int
    last_ordinal: int
    child_refs: tuple[ProviderInputUnitVectorNodeReferenceFact, ...]
    subtree_unit_count: int
    subtree_accumulator: str
    node_fingerprint: str


ProviderInputUnitVectorNodeFact = (
    ProviderInputUnitVectorLeafFact
    | ProviderInputUnitVectorInternalNodeFact
)


class ProviderInputUnitVectorNodeReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_vector_node_reference.v1"]
    node_kind: Literal["leaf", "internal"]
    first_ordinal: int
    last_ordinal: int
    subtree_unit_count: int
    subtree_accumulator: str
    artifact_reference: ContextArtifactReferenceFact
    reference_fingerprint: str


class ProviderInputUnitVectorRootReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_vector_root_reference.v1"]
    unit_count: int
    tree_height: int
    root_node_ref: ProviderInputUnitVectorNodeReferenceFact | None
    ordered_unit_accumulator: str
    vector_contract_fingerprint: str
    vector_semantic_fingerprint: str
    reference_fingerprint: str


class ProviderInputSemanticIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_input_semantic_identity.v1"]
    input_unit_count: int
    ordered_unit_accumulator: str
    unit_vector_semantic_fingerprint: str
    system_instruction_fingerprint: str
    tool_catalog_fingerprint: str
    provider_message_sequence_fingerprint: str
    semantic_fingerprint: str


class CanonicalProviderInputPlanFact(FrozenFactBase):
    schema_version: Literal["canonical_provider_input_plan.v1"]
    resolved_model_call_fact: ResolvedModelCallFact
    generation_root_reference: ProviderInputGenerationRootReferenceFact
    resulting_prefix_fingerprint: str
    resulting_generation_revision: int
    unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    provider_input_semantic_identity: ProviderInputSemanticIdentityFact
    plan_fingerprint: str
```

Vector使用固定fanout、固定leaf unit/bytes cap与copy-on-write path。Append只写new/changed leaves、到root的 `O(log n)` nodes与new root；root/event-safe plan均为bounded reference。Empty vector必须满足 `unit_count=0`、`tree_height=0`、`root_node_ref=None`与canonical empty accumulator。

V1 tree contract固定：

- leaf最多128 units，internal node最多64 children，height最多8；ordinal为unsigned 64-bit且从0连续分配；
- 原vector尾叶可能未满，因此单append的changed leaves上界是 `1 + ceil((512 - 1) / 128) = 5`：一个被修改的旧tail leaf加最多四个new leaves；
- 包含root-height split的保守changed-node上界为 `1 + changed_leaves * max_tree_height = 41`；再加一个append batch artifact，单operation artifact上界为42；
- `max_vector_node_artifact_bytes = max(leaf overhead + 128 * max unit-ref bytes, internal overhead + 64 * max node-ref bytes)`；
- `max_append_batch_artifact_bytes = append overhead + 512 * max unit-ref bytes + 41 * max node-ref bytes`；
- `max_changed_node_artifact_bytes = 41 * max_vector_node_artifact_bytes`；`max_append_operation_artifact_bytes = max_changed_node_artifact_bytes + max_append_batch_artifact_bytes`；所有乘加使用checked integers；
- 最大可表示units为 `128 * 64 ** 7`，doctor必须证明所有resolved physical/token policies远小于该值；
- root reference canonical bytes不得超过64 KiB；root artifact只保存同一bounded root metadata与contract identity，不保存完整node-ref列表或unit正文；
- append artifact只保存本次最多512个new units及其bounded COW refs；超过单append上界时planner在ModelStart前返回typed physical admission failure，不拆成多个可见顺序不确定的append；
- resolved physical burst contract必须精确join `ResolvedProviderInputVectorPhysicalPolicyFact`，使用5/41/42与上述bytes公式证明confirmation batch count和deadline；不允许沿用无tail-leaf场景的经验值。

不得为每次model call再写一份完整ordered list或full plan artifact。Root与append artifact是immutable shared chunks；`ProviderInputAppendBatchReferenceFact.changed_vector_node_refs`只保存本次bounded changed path，`resulting_unit_vector_root`保存新root。

发送时使用process-local runtime value：

```python
@dataclass(frozen=True, slots=True)
class RecursivelyImmutableProviderInputCarrier:
    ordered_fragments: tuple[ProviderInputTypedFragmentFact, ...]
    tool_catalog_root: CapabilityToolCatalogRootSemanticFact
    canonical_request_input_options: FrozenJsonObjectFact
    provider_input_semantic_fingerprint: str
    carrier_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedCanonicalProviderInputPlan:
    fact: CanonicalProviderInputPlanFact
    vector_lease: ProviderInputUnitVectorLease
    materialized_input: RecursivelyImmutableProviderInputCarrier
```

Materializer遍历persistent vector并hydrate exact fragments，构造完整request；该完整runtime object不作为新artifact持久化。Resident cache可结构共享nodes，cache miss则按vector refs exact hydrate。

Carrier factory必须递归复制所有输入并把list/dict转换为tuple/`FrozenJsonValue`；不得保存caller拥有的 `LLMContext`、tool parameter dict或SDK object引用。Pre-send validator从immutable carrier重算semantic/carrier fingerprint。Adapter需要mutable SDK payload时只能在dispatch边界创建一次fresh deep copy，不能回写carrier；retry继续复用同一immutable carrier。

Adapter payload builder与manifest必须消费同一 `CanonicalProviderInputPlan`。Adapter不得在plan之后插入model-visible message/tool/instruction/options。

### 18.4 Root/append/plan完整join

唯一factory/validator必须证明：

- generation root `root_unit_count/accumulator/vector semantic`等于initial vector root；
- tool catalog root semantic/reference/materialized tool specs逐项一致；
- append semantic ordered units等于batch artifact中新units；
- new replay bindings等于new units实际引用、但predecessor binding set尚未包含的canonical set difference；
- changed vector nodes都由predecessor root + newunits确定性COW产生；
- resulting vector count等于predecessor count + append unit count；
- resulting vector accumulator等于逐项fold后的accumulator；
- predecessor/resulting prefix满足第18.2公式；
- append expected revision/index等于generation state；
- every unit attribution ref被matching exact authority horizon覆盖；resulting aggregate horizon set从predecessor set与new unit horizons做persistent union；
- resulting replay binding set从predecessor set与new replay bindings做persistent union；
- plan vector root等于resulting generation state vector root；
- plan horizon/binding set roots分别等于resulting generation core中的persistent set roots；
- plan semantic identity的unit count/accumulator/vector fingerprint等于vector root；
- plan system/tool/message fingerprints从hydrated typed fragments重算；
- `ResolvedModelCall`、generation compatibility与adapter binding三方精确join。

任一层不能只相信caller提供的aggregate fingerprint。Live ingress完整验证new/changed path；exact replay/doctor可深度遍历整个vector。

## 19. Ordering算法

### 19.1 固定规则

Generation root顺序：

```text
system/developer ContextSource units
-> CapabilityToolCatalogRootFact
-> leading-context generation-root source units
-> normalized baseline/history units
-> trailing/status generation-root source units
```

Tool catalog在多数provider中不是普通message；上述顺序表示canonical provider tokenization/lowering lane，具体wire字段位置由 `ProviderVisibleInputCompatibilityFact`绑定的adapter contract实现。Memory instruction等system/leading intent不得被统一放到history之后。

每次append顺序由typed lane固定，不由source注册顺序偶然决定：

```text
previous ACCEPTED model continuation
-> required tool result pairing closure
-> newly selected source revisions/observations
-> runtime clock observation
-> current trigger unit
```

实际调用若由tool result触发，tool call/result必须形成连续pairing group。Clock、memory、status不得插在未闭合tool pair中间。

当前user trigger默认保持为append batch最后一个user unit。若provider contract要求其他顺序，必须由versioned lowering contract明确，不得由adapter临时重排。

### 19.2 Accepted output

只有以下条件全部满足，model output才进入下一append batch：

- `ModelEnd.model_terminal_outcome == completed`；
- terminal projection document/reference完整join；
- `ModelCallControlDispositionResolvedEvent == ACCEPTED`；
- run activation与control predicate有效；
- output尚未加入当前generation prefix。

Provider error、cancelled、runtime error、suppressed-by-termination、suppressed-by-recovery只供audit/UI，不进入canonical provider continuation。

### 19.3 Tool pairing

- closed tool call没有ACCEPTED disposition时不得进入可执行append；
- accepted tool call与terminal tool result按pairing contract连续lower；
- MCP suspension保留pending pair，不产生伪terminal result；
- resume/deny/cancel使用原requirement与capture policy；
- external result必须引用原committed requirement；
- pairing不完整时当前call preparation返回typed not-ready，不绕过pairing插入其他tool result。

### 19.4 Source head planning

Pure planner同时接收generation-neutral candidates与 `CommittedProviderInputGenerationCoreStateFact.committed_source_heads`：

```text
generation_root candidate
    same semantic as root -> no-op
    changed semantic -> rollover required

append_once candidate
    exact candidate semantic already committed -> no-op
    unseen canonical event revision -> append
    same source revision ID but payload conflict -> contract mismatch

append_revision + complete_snapshot
    same source state semantic as committed head -> no-op
    newer canonical ordinal -> append provider revision that supersedes committed head
    older canonical ordinal -> stale source snapshot, reprepare
    same ordinal with conflict -> authority mismatch

append_revision + strict_delta
    predecessor == committed canonical source revision -> append
    candidate already committed -> no-op
    predecessor is older than committed head -> stale/no-op after exact lineage validation
    predecessor is unknown/newer/forked -> not-ready or authority mismatch
```

Provider-visible revision/supersession wrapper由planner产生并进入unit content semantic；它可以引用generation committed head。Source candidate本身不改变，也不读取generation。

Source heads只有append event FULL后由唯一reducer推进。Prepared、artifact confirmed或provider transport started都不能提前修改head。

## 20. Generation owner

### 20.1 RuntimeSession ownership

新增 service-owned：

```text
ProviderInputGenerationStore
ProviderInputGenerationCoordinator
ProviderInputMaterializationService
ProviderInputRecoveryService
ProviderInputResidentCache
```

RuntimeSession拥有：

- generation revision/CAS；
- active generation identity；
- pending append candidate；
- artifact physical operations；
- reconciliation owner；
- close drain；
- process-local resident cache accounting。

共享executor/pool可以位于Host/process composition root，但operation registry、serialization queue、deadline与drain仍由RuntimeSession拥有。

### 20.2 Lock域

冻结两个锁域：

1. generation state lock：同步、无await，只保护generation/revision/pending owner安装与fold；
2. async preparation lock：串行同generation的prepare，不持有state lock执行I/O。

禁止：

- state lock内artifact/DB I/O；
- state lock内observer callback；
- state lock内等待publisher；
- manager callback在manager lock内反向获取generation lock；
- 未经CAS同时安装两个相同expected revision的append batch。

### 20.3 Preparation流程

```text
1. freeze ContextFactSnapshot H
2. capture active generation + revision R
3. collect/validate transcript delta and ContextSource candidates
4. resolve compatibility
5. if incompatible: prepare rollover
6. pure plan append units against (generation, R)
7. enforce budget and pairing
8. materialize/confirm content-addressed artifacts outside lock
9. build stable generation/append companion candidates + commit guard
10. persist ContextCompiled manifest with prepared append identity
11. ContextCompiled FULL: synchronous reducer fold installs preparation ownership + scope binding
12. hand PreparedCanonicalProviderInputPlan + companions to LLMRuntime
13. LLMRuntime commit_start(ModelStart + companions)
14. FULL: RuntimeSession synchronous reducer atomically consumes ownership and installs committed core revision R+1 + attribution
15. lock外 ordered publication
16. only after FULL continue provider transport
```

ContextCompiled NONE重试同一candidate；PARTIAL/UNKNOWN保留preparation owner并latch，禁止hand-off。步骤12只有在步骤11的ownership/core双CAS basis可读且与guard完全一致时合法。

若步骤4到10之间generation revision改变，candidate成为stale：

- 未commit时丢弃candidate并重新prepare；
- orphan content-addressed artifacts进入GC；
- 不把stale解释为authority corruption；
- 已出现UNKNOWN/PARTIAL时保留owner并latch，不重新生成不同candidate。

### 20.4 ModelStart writer ownership

LLMRuntime继续是完整model stream与 `ModelStartEvent` 的唯一durable writer。ProviderInputGenerationCoordinator不得直接调用EventLog writer提交ModelStart或generation events。

```python
@dataclass(frozen=True, slots=True)
class ProviderInputStartCompanionBundle:
    prepared_plan: PreparedCanonicalProviderInputPlan
    stable_companion_event_candidates: tuple[AgentEvent, ...]
    generation_commit_guard: ProviderInputGenerationCommitGuardFact
    prepared_append_candidate_fingerprint: str
```

```python
class ProviderInputDispatchBarrierIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_input_dispatch_barrier_identity.v1"]
    barrier_id: str
    scope_fingerprint: str
    old_generation_id: str
    installed_at_core_revision: int
    attempt_id: str
    identity_fingerprint: str


class InitialGenerationCommitGuardFact(FrozenFactBase):
    schema_version: Literal["initial_generation_commit_guard.v1"]
    guard_kind: Literal["initial_start"]
    new_generation_id: str
    new_generation_fingerprint: str
    new_root_reference_fingerprint: str
    expected_scope_binding_fingerprint: str
    expected_preparation_ownership_fingerprint: str
    expected_authority_horizon_set_reference_fingerprint: str
    expected_revision: Literal[0]
    resolved_model_call_id: str
    guard_fingerprint: str


class ExistingAppendCommitGuardFact(FrozenFactBase):
    schema_version: Literal["existing_append_commit_guard.v1"]
    guard_kind: Literal["existing_append"]
    generation_id: str
    expected_committed_core_state_fingerprint: str
    expected_preparation_ownership_fingerprint: str
    expected_revision: int
    expected_committed_prefix_fingerprint: str
    expected_transcript_frontier_fingerprint: str
    expected_awaiting_disposition_fingerprint: str | None
    expected_pending_continuation_fingerprint: str | None
    expected_scope_binding_fingerprint: str
    resolved_model_call_id: str
    guard_fingerprint: str


class RolloverGenerationCommitGuardFact(FrozenFactBase):
    schema_version: Literal["rollover_generation_commit_guard.v1"]
    guard_kind: Literal["rollover"]
    old_generation_id: str
    expected_old_core_state_fingerprint: str
    expected_old_revision: int
    expected_old_prefix_fingerprint: str
    new_generation_id: str
    new_generation_fingerprint: str
    new_root_reference_fingerprint: str
    expected_scope_binding_fingerprint: str
    expected_preparation_ownership_fingerprint: str
    rollover_authority_horizon_set_reference_fingerprint: str
    dispatch_barrier_identity: ProviderInputDispatchBarrierIdentityFact
    resolved_model_call_id: str
    guard_fingerprint: str


ProviderInputGenerationCommitGuardFact = (
    InitialGenerationCommitGuardFact
    | ExistingAppendCommitGuardFact
    | RolloverGenerationCommitGuardFact
)
```

三种guard没有跨分支nullable拼装。Initial证明scope仍无active generation；existing append同时CAS committed core与preparation ownership；rollover同时CAS old core、new generation/root、scope binding、authority set与dispatch barrier/attempt。Guard不读取process-local segment ID作为durable identity。

该bundle通过现有model lifecycle bundle/commit port交给LLMRuntime：

```text
GenerationCoordinator.prepare()
    -> ProviderInputStartCompanionBundle

LLMRuntime.start_stream(..., lifecycle_bundle)
    -> commit_start(ModelStart + stable companions)
    -> one RuntimeSession atomic writer batch
```

职责矩阵：

- Context compiler/manifest：保存prepared append candidate与plan/vector root identity；
- Generation coordinator：pure prepare、artifact confirmation、stable companions与guard；
- LLMRuntime：ModelStart/semantic/terminal lifecycle唯一writer；
- RuntimeSession ProviderInput recovery owner：仅在exact Start/append confirmed absent时写prepared-abandonment terminal；
- one-shot generation close作为LLMRuntime terminal batch companion提交，不由另一个owner事后补写；
- RuntimeSession commit path：ledger confirm后同步调用唯一generation reducer；
- ModelStart：保存committed provider input reference；
- ordered publisher：只发布FULL后fold完成的notification。

禁止新增第二个ModelStart writer、让coordinator先单独commit append，或在LLMRuntime commit后由另一个事务补generation event。

Live caller在hand-off给LLMRuntime之前取消时，可请求同一个RuntimeSession recovery owner提交abandonment；hand-off之后必须先等待LLMRuntime physical commit outcome。FULL采用已提交ModelStart，NONE才允许abandon，UNKNOWN/PARTIAL保留prepared owner并latch。

## 21. Durable events

建议新增：

```text
ProviderInputGenerationStartedEvent
ProviderInputAppendCommittedEvent
ExistingGenerationPreparationAbandonedEvent
ScopedGenerationPreparationAbandonedEvent
ProviderInputGenerationRolloverResolvedEvent
ProviderInputGenerationClosedEvent
```

完整event schema：

```python
class ProviderInputGenerationStartedEvent(AgentEvent):
    schema_version: Literal["provider_input_generation_started_event.v1"]
    generation: ProviderInputGenerationFact
    root_reference: ProviderInputGenerationRootReferenceFact
    initial_vector_root: ProviderInputUnitVectorRootReferenceFact
    initial_prefix_fingerprint: str
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    expected_initial_append_event_id: str
    expected_model_start_event_id: str
    genesis_core_state_fingerprint: str


class ProviderInputAppendCommittedEvent(AgentEvent):
    schema_version: Literal["provider_input_append_committed_event.v1"]
    generation_id: str
    generation_fingerprint: str
    expected_revision: int
    resulting_revision: int
    append_batch_reference: ProviderInputAppendBatchReferenceFact
    consumed_preparation_id: str
    consumed_preparation_ownership_fingerprint: str
    consumed_pending_continuation_fingerprint: str | None
    predecessor_core_state_fingerprint: str
    resulting_core_state_fingerprint: str
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    resolved_model_call_id: str
    expected_model_start_event_id: str


class ExistingGenerationPreparationAbandonedEvent(AgentEvent):
    schema_version: Literal["existing_generation_preparation_abandoned_event.v1"]
    abandonment_kind: Literal["existing_append"]
    generation_id: str
    preparation_id: str
    preparation_ownership_fingerprint: str
    context_compiled_event_ref: ContextEventReferenceFact
    resolved_model_call_id: str
    expected_committed_core_state_fingerprint: str
    abandonment_reason: Literal[
        "caller_cancelled_before_start",
        "run_terminated_before_start",
        "prepared_candidate_stale",
        "resolved_target_invalidated_before_start",
        "recovery_confirmed_not_started",
    ]
    predecessor_preparation_attribution_fingerprint: str
    predecessor_scope_binding_fingerprint: str
    resulting_scope_binding_fingerprint: str


class ScopedGenerationPreparationAbandonedEvent(AgentEvent):
    schema_version: Literal["scoped_generation_preparation_abandoned_event.v1"]
    abandonment_kind: Literal["initial_start", "rollover_start"]
    scope_fingerprint: str
    proposed_generation_id: str
    preparation_id: str
    old_generation_id: str | None
    expected_old_core_state_fingerprint: str | None
    preparation_ownership_fingerprint: str
    context_compiled_event_ref: ContextEventReferenceFact
    resolved_model_call_id: str
    abandonment_reason: Literal[
        "caller_cancelled_before_start",
        "run_terminated_before_start",
        "prepared_candidate_stale",
        "resolved_target_invalidated_before_start",
        "recovery_confirmed_not_started",
    ]
    predecessor_preparation_attribution_fingerprint: str
    predecessor_scope_binding_fingerprint: str
    resulting_scope_binding_fingerprint: str


ProviderInputPreparationAbandonedEvent = (
    ExistingGenerationPreparationAbandonedEvent
    | ScopedGenerationPreparationAbandonedEvent
)


class ProviderInputGenerationRolloverResolvedEvent(AgentEvent):
    schema_version: Literal["provider_input_generation_rollover_resolved_event.v1"]
    old_generation_id: str
    old_generation_fingerprint: str
    old_final_core_state_fingerprint: str
    new_generation: ProviderInputGenerationFact
    new_root_reference: ProviderInputGenerationRootReferenceFact
    rollover_reason: ProviderInputRolloverReason
    rollover_authority_refs: tuple[ContextEventReferenceFact, ...]
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    expected_old_close_event_id: str
    expected_new_start_event_id: str
    expected_initial_append_event_id: str
    expected_model_start_event_id: str


class ProviderInputGenerationClosedEvent(AgentEvent):
    schema_version: Literal["provider_input_generation_closed_event.v1"]
    generation_id: str
    generation_fingerprint: str
    final_revision: int
    final_prefix_fingerprint: str
    final_vector_root: ProviderInputUnitVectorRootReferenceFact
    close_reason: Literal["rollover", "session_close", "one_shot_terminal"]
    successor_generation_id: str | None
    unconsumed_continuation_fingerprint: str | None
    predecessor_core_state_fingerprint: str
    resulting_closed_core_state_fingerprint: str
```

Nullability matrix：

- abandonment event只有在exact ModelStart与append均confirmed absent时合法；UNKNOWN/PARTIAL不得写abandonment；
- existing abandonment必须匹配committed core但不改变它；scoped initial不允许old generation字段，scoped rollover要求old generation/core两字段全有；
- abandonment不改变generation revision、prefix、vector、frontier、source heads或clock，只终结matching preparation ownership并CAS scope binding；
- `close_reason=rollover`要求successor generation存在，并与同批rollover event一致；
- `session_close | one_shot_terminal`要求successor为 `None`；
- rollover时若old state有unconsumed continuation，new root/initial append必须精确消费或由canonical compaction baseline证明已包含；
- generation started永远与initial append + ModelStart同批；
- started event描述batch内revision 0/prefix_0的genesis中间态；initial append必须非空并将new state推进到revision 1，genesis中间态在整个batch验证成功前不得发布；
- V1不允许持久化“已started但从未绑定ModelStart”的空generation；
- ordinary append只与ModelStart同批；
- rollover batch固定包含old close + rollover + new start + initial append + ModelStart；
- same-batch关联只使用deterministic event IDs，禁止事件payload fingerprint互相引用形成环。

`ModelStartEvent` schema-level required新增bounded `provider_input_reference: CommittedProviderInputReferenceFact`，不得以nullable字段加production外围validator模拟hard cut。它至少join：

- generation fact/root reference；
- generation revision；
- append batch reference或same-input retry marker；
- resulting prefix fingerprint；
- canonical provider input plan fingerprint；
- source/transcript aggregate `authority_horizon_set` root；exact per-unit horizons仍由vector refs验证；
- resolved model call identity。

First generation start/append与对应ModelStart必须在同一atomic EventLog batch中提交；ordinary append同样如此；rollover使用上面的五事件原子矩阵。Artifact在batch前confirmed，但只有event FULL后才成为reachable root。

Stable event ID从generation ID/revision/model call ID确定性派生。NONE重试原candidate bytes；FULL fold；PARTIAL/UNKNOWN保留owner并latch。

## 22. Rollover

### 22.1 Rollover reason

```python
class ProviderInputRolloverReason(StrEnum):
    CONTEXT_COMPACTION = "context_compaction"
    WINDOW_GENERATION_CHANGED = "window_generation_changed"
    SYSTEM_INSTRUCTION_CHANGED = "system_instruction_changed"
    TOOL_CATALOG_CHANGED = "tool_catalog_changed"
    INPUT_LOWERING_CONTRACT_CHANGED = "input_lowering_contract_changed"
    REQUEST_INPUT_SHAPE_CHANGED = "request_input_shape_changed"
    RETAINED_PREFIX_BUDGET_UNREACHABLE = "retained_prefix_budget_unreachable"
    REQUIRED_SOURCE_REQUIRES_REWRITE = "required_source_requires_rewrite"
    EXPLICIT_ADMINISTRATIVE_RESET = "explicit_administrative_reset"
```

`unknown`不得作为production正常reason。无法分类的compatibility drift为contract mismatch。

### 22.2 Rollover流程

```text
close new model dispatch admission for old generation
-> drain/resolve active model append owner
-> freeze canonical compaction/baseline authority
-> build and confirm new root artifacts
-> prepare initial append + CanonicalProviderInputPlan for first new-generation call
-> hand five-event companions + plan to LLMRuntime
-> atomic old close + rollover + new start + initial append + ModelStart
-> synchronous reducer fold
-> reopen model dispatch admission
```

普通EventLog producer不需要全部暂停；barrier只阻止使用旧generation启动新的model call。Rollover authority horizons通过per-ledger CAS固定。

UNKNOWN/PARTIAL时barrier与owner保留，Host close不得破坏性teardown。

V1不允许先提交空的新generation再等待未来dispatch。若新root/initial append/ModelStart preparation失败，旧generation保持open且barrier可安全释放；若atomic batch UNKNOWN/PARTIAL，旧/new状态由reconciliation owner按stable event IDs确认，不能重新生成另一new generation ID。

### 22.3 Compaction

Compaction仍由Long-Horizon token/window policy决定，不由provider cache miss触发。

新generation从：

- canonical compaction summary/baseline；
- retained pairing-safe tail；
- 当前有效generation-root sources；
- 必须保留的new source revisions；

构造新root。旧generation仍可Inspector/replay，但不再接受append。

## 23. Budget与物理安全

### 23.1 Token budget

Planner对完整输入估算：

```text
retained committed prefix tokens
+ accepted output continuation tokens
+ selected new source/transcript units
+ tool catalog/envelope tokens
+ output reservation
<= resolved context window policy
```

Cached input tokens不得从budget中扣除。

### 23.2 New candidate admission

- optional candidate在append前可省略；
- required candidate无法容纳时返回typed pressure；
- retained prefix不允许降级；
- old tool result render不能因新budget静默变化；
- rewrite需要Long-Horizon compaction/rollover；
- finalization reserve继续由Stage 4 resolved budget policy拥有。

### 23.3 Physical bounds

物理安全限制与token budget分离，但不能形成不可恢复的第二历史窗口：

- 单append batch有units/bytes/artifact refs上界；
- source physical quote由 `ResolvedContextSourcePhysicalInputPolicyFact`按resolved model/tokenizer/codec派生，不使用全模型通用1/4 MiB常量；
- 大payload在selection/budget前保持content-addressed artifact/reference，selection后才paged hydrate；
- event-safe reference有固定上界；
- aggregate ledger horizons与replay bindings使用persistent set root，ModelStart不重复完整tuple；
- vector append使用resolved 5 leaves / 41 nodes / 42 artifacts与bytes公式；
- full provider input不逐call复制进EventLog；
- process-local resident prefix cache有全进程bytes/chunks/generations预算；
- resident admission失败退化为exact materialization，不fail closed；
- physical cap必须由resolved model input上界与canonical encoding expansion静态证明可行；
- inline/resident cap可以更小，但必须有artifact-backed exact path；任何总physical contract不得拒绝token-budget内合法maximal model call或required source payload。

### 23.4 复杂度口径

V1目标：

- source collection只处理new/changed facts；
- generation prefix identity按hash chain O(new units)推进；
- immutable resident chunks结构共享；
- DB读取使用latest generation + bounded append range/exact IDs；
- 不从session sequence 1扫描。

但每次provider request仍发送并序列化完整上下文，因此不声称网络与最终adapter serialization严格 O(delta)。

## 24. Failure、cancellation与recovery

### 24.1 Pre-manifest/pre-start failure

Source collection、append planning、materialization、budget、pairing或compatibility failure发生在ModelStart前时：

- 写typed ContextCompiled/input failure audit；
- 不推进generation revision；
- ContextCompiled success尚未FULL时不安装preparation ownership；
- ContextCompiled success已经FULL但ModelStart confirmed absent时，必须通过typed abandonment event终结ownership，不能直接从scope binding删除；
- 不发布prepared prefix；
- confirmed orphan artifacts交GC；
- ledger untrusted时禁止伪造audit并latch。

### 24.2 Commit outcome

```text
NONE
    保留stable candidate
    在原absolute deadline/confirmation generation内重试

FULL
    fold generation reducer
    安装new revision/prefix
    发布committed notification
    允许provider dispatch

PARTIAL | UNKNOWN
    保留owner、candidate与artifacts
    latch session
    禁止第二candidate与破坏性close
```

Cancel-after-FULL必须消费writer返回的physical outcome，完成fold/owner adoption后再传播cancellation。

### 24.3 Provider terminal outcome

- input prefix在ModelStart FULL时已commit；
- provider error/cancel/runtime error不回滚prefix；
- terminal candidate必须稳定重试，不能把provider outcome改写为runtime error；
- completed output只有ACCEPTED disposition后成为next continuation；
- suppressed output永不进入generation continuation；
- unresolved completed disposition阻止下一model call/RunEnd正常完成。

### 24.4 Restart recovery

Recovery按indexed generation ID读取：

1. latest generation start/rollover；
2. ordered append events/revisions；
3. ModelStart input refs；
4. prepared ContextCompiled/abandonment lifecycle；
5. terminal projection与control disposition；
6. exact referenced artifacts。

然后重建：

- generation fact/root semantic与reference；
- current revision；
- prefix hash chain；
- accepted-but-not-yet-appended continuation；
- pending append/ModelStart owner；
- active rollover barrier。

One-shot recovery矩阵：

- normal live path：ModelEnd/ReplyEnd/usage/settlement terminal batch必须同时包含 `ProviderInputGenerationClosedEvent(close_reason="one_shot_terminal")`；
- Start-without-End：ModelStreamRecoveryService构造稳定recovered terminal candidate时，必须在同一atomic terminal batch加入one-shot generation close；
- terminal已FULL但close缺失属于结构不完整，不得把scope继续视为open；recovery以exact terminal identity构造唯一close candidate，UNKNOWN/PARTIAL保留owner并latch；
- close FULL前不得释放one-shot scope binding或允许同operation ID重新创建generation。

禁止full session ledger scan、current source重新render、current clock替代历史clock。

### 24.5 Missing artifacts

- process-local cache miss：exact hydrate；
- derived materialization artifact缺失：用canonical refs + historical binding重建并比较fingerprint；
- confirmed absent且可重建：写回新的物理artifact，semantic identity不变；
- hash conflict/historical binding缺失/canonical reducer mismatch：authority untrusted latch；
- cache corruption只丢cache后exact restore，不直接latch。

## 25. Manifest、replay与Inspector

### 25.1 Context input manifest

Prepared与committed carrier严格分离：

```python
class PreparedProviderInputAppendCandidateFact(FrozenFactBase):
    schema_version: Literal["prepared_provider_input_append_candidate.v1"]
    generation_id: str
    preparation_ownership: ProviderInputPreparationOwnershipFact
    expected_committed_core_state_fingerprint: str | None
    append_batch_reference: ProviderInputAppendBatchReferenceFact
    provider_input_plan: CanonicalProviderInputPlanFact
    stable_companion_event_ids: tuple[str, ...]
    generation_commit_guard: ProviderInputGenerationCommitGuardFact
    candidate_fingerprint: str


class CommittedProviderInputReferenceFact(FrozenFactBase):
    schema_version: Literal["committed_provider_input_reference.v1"]
    generation_id: str
    committed_generation_revision: int
    resulting_generation_core_state_fingerprint: str
    append_committed_event_identity: StableEventIdentityFact
    resulting_prefix_fingerprint: str
    resulting_unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    provider_input_plan_fingerprint: str
    reference_fingerprint: str
```

ContextCompiled manifest保存prepared candidate与ownership core；FULL后generation reducer只安装preparation ownership attribution，不推进committed core。ModelStart保存committed reference，并且只引用resulting core fingerprint。两者通过ownership、append candidate、stable companion event IDs、ResolvedModelCall与plan fingerprint精确join。Prepared manifest不能在restart时自动补写ModelStart。

`ContextCompiledEvent(status="compiled")` schema-level required
`prepared_provider_input`；`pressure | failed` schema-level禁止携带。测试facade必须构造最小合法one-shot/generation fixture，不得通过nullable默认值或test-only writer绕过这两条不变量。

Prepared candidate validator还必须证明：outer expected core等于ownership expected core；guard expected preparation等于ownership fingerprint；guard expected core等于同一outer/ownership core；plan resulting prefix/revision/vector等于append batch resulting values。任一处不能依赖caller自报aggregate。

`CommittedProviderInputReferenceFact`不携带ModelStart attribution envelope fingerprint；否则会形成 `core -> ModelStart payload -> core`递归。ModelStart FULL后，reducer另行生成 `ProviderInputGenerationAttributionStateFact.latest_model_start_event_ref`。

每次model call manifest新增：

- generation fact/root reference；
- generation revision；
- predecessor/resulting prefix fingerprint；
- prepared append candidate与append batch reference；
- ordered new source candidate semantic/attribution refs；
- accepted continuation refs；
- clock observation ref；
- rollover reason/reference（如有）；
- canonical ProviderInputPlan bounded fact/fingerprint；
- compiled_at仅作invocation audit。

Manifest不得复制整个prefix正文或ordered unit列表。

### 25.2 Exact replay

Replay：

```text
canonical ledger/artifacts
-> restore generation root
-> replay append chain through requested ModelStart
-> hydrate exact units under historical bindings
-> rebuild CanonicalProviderInputPlan
-> compare manifest/start/provider-input fingerprints
```

比较层次：

- committed core由generation/root/append/terminal/disposition纯fold重算，严格比较core fingerprint；
- ModelStart/close/rollover event refs只在attribution envelope比较，不参与core semantic identity；
- preparation ownership从ContextCompiled/abandonment/ModelStart lifecycle独立fold并比较；
- semantic source与ordered unit identity严格相等；
- artifact placement允许materialization-equivalent；
- final provider messages/tools/instructions与plan fingerprint严格相等；
- current process build fingerprint不参与；
- current time不参与。

### 25.3 Inspector

每个model call展示：

- generation ID/revision/scope（session window或one-shot）；
- committed core fingerprint、preparation ownership fingerprint与ModelStart attribution fingerprint分栏；
- awaiting-control-disposition / pending-continuation状态；
- root/actually-used source and lowering binding identities；
- authority horizon-set count/root与actual replay-binding-set count/root；
- retained prefix units/tokens；
- new append units/tokens；
- source owner与revision/supersedes；
- clock observation与refresh reason；
- predicted rollover/cache-break reason；
- provider reported cached input usage；
- exact replay status；
- resident cache hit/miss仅作operational diagnostic。

历史Inspector只读durable facts；process-local resident状态通过可空live diagnostics provider显示，不伪造历史值。

Run Inspector不得仅用 `run_events` 重建generation。它必须先从完整session ledger重建generation start/core/append/close链，再按当前run中ContextCompiled/ModelStart/rollover等exact refs筛选。generation start位于先前run时仍可报告 `exact_replay`；若当前run引用core/append而session ledger中缺少root/start join，只能报告 `incomplete | contract_mismatch`，不得伪造exact。

## 26. 第二阶段 PR 顺序

### IP0：Generation/append DTO与pure planner

- generation compatibility/scope/root/unit/batch/plan DTO；
- committed generation core、preparation ownership、attribution envelope、scope bindings与唯一batch reducer；
- persistent unit vector、horizon set、replay-binding set与bounded COW contract；
- 三种commit guard、两类abandonment与terminal-awaiting-disposition中间态；
- prefix hash chain；
- pure append planner；
- source lifecycle到append/rollover matrix；
- golden ordering/pairing tests；
- production继续旧full path，new planner只读shadow。

### IP1：Canonical ProviderInputPlan

- adapter与manifest消费统一plan；
- pre-send plan/payload semantic join；
- 禁止adapter后置注入model-visible字段；
- tools/system/message canonical ordering；
- one-shot direct/window/governance接入同一plan contract。

### IP2：RuntimeSession generation owner

- generation store/coordinator/materialization service；
- start/append/rollover events；
- prepared manifest fold、pre-start abandonment与exact recovery owner；
- atomic ModelStart join；
- FULL/NONE/PARTIAL/UNKNOWN语义；
- cancellation与close drain；
- indexed restart recovery；
- one-shot Start-without-End terminal + close同批recovery。

### IP3：Main/subagent production append switch

- main agent model loop；
- subagent model loop；
- accepted output continuation；
- tool pairing；
- ContextSource append/revision；
- runtime clock append；
- source/current-user/tool-loop trajectories。

### IP4：Long-Horizon rollover

- window compaction -> new generation；
- baseline/retained tail；
- budget pressure；
- sticky tool render；
- rollover barrier/recovery；
- old generation Inspector/replay。

### IP5：Hard cut旧full reconstruction

- 删除production每call完整candidate/transcript重排入口；
- 删除dynamic/volatile suffix replacement语义；
- 删除旧provider projection full rerender facade；
- pure full rebuild只保留offline doctor/exact replay allowlist；
- architecture guards模块级禁止回流。

### IP6：Cache observation与dogfood

- DeepSeek `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`归一化；
- OpenAI-compatible cached usage；
- stable prefix/break reason Inspector；
- deterministic multi-call benchmark；
- real long dogfood；
- compaction前后一次expected rollover；
- provider hint仍可后置，不阻塞DoD。

## 27. 代码落点

建议新增：

```text
src/pulsara_agent/primitives/context_source.py
src/pulsara_agent/primitives/provider_input.py

src/pulsara_agent/runtime/context_input/sources/__init__.py
src/pulsara_agent/runtime/context_input/sources/registry.py
src/pulsara_agent/runtime/context_input/sources/input.py
src/pulsara_agent/runtime/context_input/sources/system.py
src/pulsara_agent/runtime/context_input/sources/runtime.py
src/pulsara_agent/runtime/context_input/sources/memory.py
src/pulsara_agent/runtime/context_input/sources/capability.py
src/pulsara_agent/runtime/context_input/sources/plan.py
src/pulsara_agent/runtime/context_input/sources/recovery.py
src/pulsara_agent/runtime/context_input/sources/subagent.py

src/pulsara_agent/runtime/context_input/provider_input/planner.py
src/pulsara_agent/runtime/context_input/provider_input/vector.py
src/pulsara_agent/runtime/context_input/provider_input/horizon_set.py
src/pulsara_agent/runtime/context_input/provider_input/replay_bindings.py
src/pulsara_agent/runtime/context_input/provider_input/physical_policy.py
src/pulsara_agent/runtime/context_input/provider_input/store.py
src/pulsara_agent/runtime/context_input/provider_input/service.py
src/pulsara_agent/runtime/context_input/provider_input/reducer.py
src/pulsara_agent/runtime/context_input/provider_input/recovery.py
src/pulsara_agent/runtime/context_input/provider_input/materialize.py
src/pulsara_agent/runtime/context_input/provider_input/resident.py
```

主要修改：

```text
src/pulsara_agent/primitives/context.py
src/pulsara_agent/primitives/events.py
src/pulsara_agent/event.py
src/pulsara_agent/event_log/protocol.py
src/pulsara_agent/event_log/serialization.py
src/pulsara_agent/event_log/postgres.py
src/pulsara_agent/runtime/session.py
src/pulsara_agent/runtime/agent.py
src/pulsara_agent/runtime/context_input/candidate.py
src/pulsara_agent/runtime/context_input/compiler.py
src/pulsara_agent/runtime/context_input/live.py
src/pulsara_agent/runtime/context_input/manifest.py
src/pulsara_agent/runtime/context_input/replay.py
src/pulsara_agent/runtime/context_input/provider_projection.py
src/pulsara_agent/runtime/context_engine/types.py
src/pulsara_agent/llm/input.py
src/pulsara_agent/llm/runtime.py
src/pulsara_agent/llm/commit.py
src/pulsara_agent/llm/adapters/openai/chat_completions.py
src/pulsara_agent/llm/adapters/openai/responses.py
src/pulsara_agent/inspector/service.py
```

长期contract同步：

```text
contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md
contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md
contracts/CAPABILITY_SURFACE_CONTRACT.zh.md
contracts/LLM_TRANSPORT_CONTRACT.zh.md
contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md
contracts/RECOVERY_CONTRACT.zh.md
contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md
```

实际实施以代码真值为准；新增模块不代表必须建立新public package facade。

## 28. 测试矩阵

### 28.1 ContextSource DTO/registry

- 每种source contract ID/version/fingerprint exact binding；
- duplicate source ID拒绝；
- unknown historical source拒绝；
- 每个source只接收对应discriminated input，source模块import完整 `ContextFactSnapshot`拒绝；
- capability prose/skill input不含execution surface、descriptor schema或完整exposure；tool catalog使用独立builder；
- input text/entry count/aggregate bytes与per-ledger ref bounds全部由factory重算；
- 所有 `FrozenFactBase` concrete DTOs显式注册schema version，registry/constructor零missing-version；
- 每个production model/tokenizer/codec组合的resolved physical policy doctor quote可行；
- 超inline threshold的大source保持artifact-backed，token-budget内合法输入不被固定bytes cap提前拒绝；
- source不执行I/O；
- payload union不接受schema-free domain JSON；
- one-own-fingerprint validator；
- build fingerprint不进入durable fact。

### 28.2 Source producer

- system generation root与独立tool catalog root；
- memory/plan/status revision连续；
- 相同canonical source fact跨两个generation产生相同candidate semantic/revision；
- source不读取generation revision、prefix或committed source heads；
- pure planner仅在generation state中分配supersedes/append head；
- subagent selection cap=0/no eligible/policy omitted；
- source refs/per-ledger horizons完整join；
- compiler omitted/compact source在ProviderInput carrier中逐字保持omit/compact结果，planner不得full rerender；
- candidate semantic不受event sequence/artifact ID影响；
- candidate attribution变化不污染provider semantic；
- cache异常等价miss；
- oversized optional候选省略；
- oversized required候选typed failure。

### 28.3 Timing

- 两次compile wall clock不同，旧source/transcript unit semantic不变；
- 不再出现历史section `age_seconds`；
- first generation/user turn/date rollover/long operation产生clock revision；
- retry复用同一clock；
- clock不插入tool pair；
- replay不调用当前时钟；
- compiled_at仍完整进入manifest/Inspector。

### 28.4 Prefix invariant

- same generation连续call满足旧unit sequence精确prefix；
- accepted assistant output只追加一次；
- provider error/cancel/suppressed output不追加；
- same-input retry不推进revision；
- memory/plan/runtime更新追加revision，不改旧unit；
- optional candidate发送后不能重新省略；
- current user与tool tail保持provider pairing/order；
- system/tool schema变化触发rollover；
- 相同root但provider framing compatibility不同，`prefix_0`必须不同；
- generation attribution改变但provider-visible compatibility/root相同，不改变semantic prefix；
- root ordering固定为system/developer、tools、leading、baseline/history、trailing/status；
- persistent vector append只改写bounded COW path，不写per-call完整ordered list；
- 尾叶只剩1个slot时追加512 units，必须计为5 changed leaves、最多41 tree nodes与42 artifacts；
- vector/root/plan hydrate结果与旧full canonical payload逐字段相等；
- random IDs不进入provider semantic input。

### 28.5 Budget/compaction

- retained prefix计入完整budget；
- cached tokens不从budget扣除；
- required append不可达触发typed pressure；
- compaction创建new generation；
- old tool render不在generation内降级；
- new generation baseline与canonical compaction projection一致；
- rollover后首call明确break，后续重新稳定append。

### 28.6 Commit/cancellation/recovery

- generation batch reducer覆盖committed core、awaiting disposition、pending continuation、preparation ownership、attribution envelope、scope binding与reconciliation status；
- core fingerprint DAG检查证明不含ModelStart/ContextCompiled/close refs；ModelStart只引用resulting core fingerprint；
- durable ContextCompiled只安装独立preparation ownership；安装ownership不改变expected committed core CAS basis；
- Initial/existing/rollover三种guard逐分支拒绝非法字段，并分别CAS完整所需identity；
- pre-start cancel只有在Start/append confirmed absent时写abandonment并清除同一candidate；UNKNOWN不得abandon；
- existing与scoped initial/rollover abandonment事件矩阵各自可编码，initial不要求predecessor core；
- ordinary append按 `append + ModelStart`由LLMRuntime同批提交；
- rollover按 `old close + rollover + new start + initial append + ModelStart`由LLMRuntime一次提交；
- five-event rollover reducer批中间态不可见，old/new state与scope binding一次安装；
- direct/window/governance使用one-shot scope且不生成synthetic window ID；
- LLMRuntime之外任何production ModelStart writer为architecture failure；
- generation append NONE稳定重试；
- FULL后cancel完成fold/adoption；
- PARTIAL/UNKNOWN保留owner并latch；
- artifact FULL/event NONE orphan GC；
- Event FULL/provider未启动restart terminalization；
- one-shot Start-without-End恢复terminal与generation close同批FULL，scope不残留open；
- completed terminal与accepted disposition分两批时先安装awaiting、再唯一转换为continuation；suppressed只清awaiting；
- disposition mismatch或RunEnd时仍awaiting必须fail closed，reducer不得回查EventLog；
- close drain pending materialization/write；
- rollover barrier UNKNOWN不释放；
- restart不full scan session ledger。
- parent/child source refs分别受各自ledger horizon约束，裸single high-water schema拒绝。
- 每个source/transcript/tool unit保存自己的exact refs与derived horizons；append outer/nested horizon分叉在DTO validator拒绝。
- 1,000+ child ledgers下ModelStart只保存bounded horizon-set root；durable carrier bytes不随完整ledger tuple线性重复；

### 28.7 Replay/Inspector

- manifest/generation/start三方fingerprint join；
- historical source/lowering binding exact replay；
- 新增未被当前generation引用的event schema/source contract不触发rollover；实际引用binding按persistent replay set增长；
- materialization-equivalent artifact relocation允许；
- semantic drift fail closed；
- process cache corruption丢弃后exact restore；
- Inspector source->candidate->append->ModelStart可追踪；
- run Inspector从session generation history重建后再按run refs筛选；跨run generation不得误报missing或伪造exact；
- historical Inspector不伪造live resident cache。

### 28.8 Deterministic benchmark

固定sanitized provider stream与多轮context fixture，至少报告：

- calls/generation；
- retained prefix estimated tokens；
- new append tokens/call；
- old unit rerender count，必须为0；
- rollover count/reasons；
- context prepare CPU/I/O；
- provider input semantic longest-common-prefix；
- manifest/artifact bytes；
- horizon-set root/ModelStart bytes随ledger数量保持bounded；
- exact restore wall time。

Real provider cached token仅作最终观察，不作为correctness gate。

## 29. Architecture/grep gates

阶段一完成后production零匹配或明确allowlist：

```text
ContextCandidateCollectionInput
_ContextCandidateSourceText
build_context_candidate_authorities(
AgentRuntime direct non-transcript prompt concatenation
ContextSource returning LLMMessage
ContextSource returning provider-native dict
ContextSource importing ContextFactSnapshot
ContextSource importing CapabilityExecutionSurfaceIdentityFact
ContextSource importing tool descriptor/schema DTO
source calling EventLog/ArtifactStore/MemoryStore directly
model-visible historical timing header
model-visible recomputed age_seconds
```

阶段二完成后production零匹配或offline allowlist：

```text
per-call replacement of committed provider units
volatile candidate disappearing after ModelStart
adapter post-plan model-visible injection
utc_now() inside pure provider lowering/replay
sequence-1 provider generation restore
previous_response_id
provider conversation/context_management ingress
current source registry used for historical replay
full provider input body duplicated in EventLog
ProviderInputGenerationCoordinator writing ModelStart directly
per-call full ordered provider-unit artifact
synthetic context-window identity for one-shot generation
single integer authority high-water on provider input facts
full authority horizon tuple embedded in ModelStart/plan/core state
global historical decoder/source registry fingerprint in generation compatibility
ModelStart reference inside committed generation core fingerprint
prepared ownership field inside committed generation core
raw mutable LLMContext/dict retained by PreparedCanonicalProviderInputPlan
FrozenFactBase concrete DTO without schema_version
```

Guard应采用module-level默认禁止 + repair/doctor精确allowlist，不能只检查少数函数直接AST调用。

## 30. Dogfood轨迹

至少覆盖：

1. 多轮filesystem/terminal/tool loop，验证每轮只追加accepted output/tool result/new facts；
2. memory projection与plan连续修订，验证旧revision保留、latest-wins标记稳定；
3. 长tool operation跨clock refresh threshold；
4. MCP tool schema不变但refresh generation变化，不rollover；
5. MCP tool schema真实变化，明确rollover；
6. permission变化但tool exposure schema稳定，gate仍正确且不伪造schema break；
7. provider error/cancel后retry，不追加失败输出；
8. completed但suppressed disposition，不进入下一prefix；
9. window compaction前后恰好一次rollover；
10. restart后继续同generation append；
11. subagent parent/child各自generation与tool pairing；
12. DeepSeek/OpenAI-compatible cached usage missing/reported矩阵。

Dogfood必须同时检查correctness与prefix结构；单看provider控制台cache hit率不能替代本地exact prefix证明。

## 31. 非目标

本文不负责：

- provider remote continuation；
- 跨用户共享raw prompt cache；
- 用cached tokens绕过context window；
- 保证provider一定命中cache；
- 用append-only替代Long-Horizon compaction；
- 为了cache保留过期tool schema；
- 让模型承担permission/tool gate安全责任；
- 让ContextSource决定provider-native JSON；
- 对所有one-shot summarizer/governance calls强制跨调用generation复用；
- 在V1实现durable delta coalescing以外的新provider协议。

## 32. 两阶段总完成定义

全部完成必须满足：

- non-tool、non-transcript model-visible内容100%归属唯一ContextSource；
- provider tool definitions 100%归属唯一 `CapabilityToolCatalogRootFact`；
- transcript/model output继续由normalized projection + accepted disposition拥有；
- `ContextSectionCandidate v2`生命周期无需再为增量输入修改；
- 同generation旧provider units从不被删除、移动、重渲染或重新计时；
- current time只以新clock observation追加；
- generation root/tool/system变化有typed rollover reason；
- compaction是显式generation boundary；
- ModelStart持有exact provider input reference；
- ModelStart只引用committed core fingerprint，event attribution不反向进入core identity；
- preparation ownership与committed core物理分离，guard同时CAS二者；
- aggregate horizons/replay bindings使用bounded persistent set roots；
- token-budget合法的大source可保持artifact-backed，不受固定inline bytes第二窗口限制；
- adapter与manifest消费同一canonical plan；
- retry/restart使用historical facts与bindings；
- provider remote context继续禁用；
- production full reconstruction与legacy string ownership物理删除；
- deterministic prefix benchmark通过；
- full pytest、Real-LLM标准与dogfood、Ruff、diff-check、architecture gates全绿。

---

## 33. 中心边界冻结表

| 边界 | 唯一authority/owner | Durable或typed carrier | 禁止旁路 |
|---|---|---|---|
| Canonical source revision | source domain contract | `CanonicalContextSourceRevisionFact` | 从generation head、cache或compile counter分配revision |
| Generation source head | generation batch reducer | `ProviderInputCommittedSourceHeadFact` | source读取或修改committed head |
| Generation live/replay state | `reduce_provider_input_generation_batch()` | committed core + preparation ownership + attribution + scope binding | live/recovery/Inspector各自手写状态机 |
| Ordered provider prefix | persistent unit vector + prefix chain | bounded root/COW append refs | 每个model call写完整ordered plan artifact |
| Vector physical quote | resolved vector policy | 5 changed leaves / 41 nodes / 42 artifacts + bytes formula | 忽略partial tail leaf或沿用4-leaf quote |
| Model lifecycle start | LLMRuntime | ModelStart + stable generation companions | coordinator或caller成为第二ModelStart writer |
| Cross-ledger authority | each canonical ledger | per-unit exact horizons + persistent `LedgerAuthorityHorizonSetReferenceFact` | ModelStart重复完整ledger tuple或用裸high-water代替 |
| Source authorization | composition-root input builder | discriminated `ContextSourceCollectInput` | source接收完整 `ContextFactSnapshot` |
| Tool definitions | capability tool-catalog builder | `CapabilityToolCatalogRootFact` | ContextSource prose复制tool schema或execution binding |
| Prefix compatibility | provider-visible compatibility contract | `ProviderVisibleInputCompatibilityFact` | 只hash root内容而忽略framing/tokenization |
| Prepared append lifecycle | generation reducer/recovery owner | independent preparation ownership + typed abandonment terminal | 混入committed core、prepared后重选或UNKNOWN时abandon |
| Commit CAS | LLMRuntime + RuntimeSession writer | initial/existing/rollover discriminated guards | 一套nullable guard模拟三种transition |
| Terminal control handoff | generation batch reducer | awaiting disposition -> pending continuation | disposition回查EventLog或跳过中间态 |
| Replay compatibility | exact referenced historical bindings | persistent replay-binding set | 无关global registry变化触发rollover |
| Source physical materialization | resolved model/tokenizer/codec policy | inline-or-artifact content carrier | 固定1/4 MiB成为独立输入窗口 |
| Rollover | LLMRuntime atomic start batch + generation reducer | old close + rollover + new start + append + ModelStart | durable空generation或分事务补写 |
| One-shot operation | operation owner | `OneShotGenerationScopeFact` | 伪造Long-Horizon window ID |

任一实施PR若暂时需要违反此表，说明阶段切分仍不独立全绿；不得用“后续PR会补”作为production旁路。

## 34. 最终实施顺序

```text
CS0  append-aware contract foundation
CS1  stable sources
CS2  dynamic sources
CS3  timing ownership hard cut
CS4  registry production switch
CS5  legacy source deletion

IP0  generation/append DTO + pure planner
IP1  canonical ProviderInputPlan
IP2  RuntimeSession generation owner/recovery
IP3  main/subagent production append switch
IP4  Long-Horizon rollover
IP5  full reconstruction hard cut
IP6  cache observation + deterministic/real dogfood
```

进入CS0前不得继续按旧阶段五S3的“每次compile统一重做timing overlay/lowering”实现；进入IP0前，CS5必须全绿且source contract不得再依赖legacy字符串facade。
