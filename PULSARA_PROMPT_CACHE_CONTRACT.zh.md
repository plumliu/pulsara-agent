# Pulsara Prompt Cache Contract

> 状态：设计草案，作为 ResolvedModelCall hard-cut 之后的候选大章。
>
> 本文不阻塞当前 code review、修复与验证；当前不修改 runtime 行为。
>
> 记录日期：2026-07-11
>
> **权威更新（2026-07-19）：** ContextSource ownership、provider input generation、append-only
> lifecycle、timing placement与实施顺序已由
> `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`
> 取代。本文继续作为provider cache术语、remote continuation禁令与usage观察的背景契约；
> “volatile suffix可在下一次调用被替换”的旧解释不再有效。
>
> **ROAC hard cut（2026-07-20）：** 动态事实不再以mid-history system/developer hint发送。Human input、runtime request与runtime observation使用三种typed
> user-wire envelope；clock按调用追加，memory/capability相同semantic snapshot为no-op，变化时追加完整replacement。Standalone `auxiliary_frame_rebase`已删除；只有root/tool/
> compatibility变化、typed source-disposition rewrite或confirmed Long-Horizon rewrite允许重建generation。每个historical replacement head必须有显式retain/replace/empty/terminal/rewrite disposition；absence不是删除。API对象prefix与adapter-final token-template cache命中必须分别观测。

## 0. 目标

Pulsara 面向长程 agent workload。随着 transcript、tool schema、memory projection 和运行时上下文增长，即使每次模型调用都没有超过 context window，反复发送相同前缀仍会带来显著的 prefill 延迟与输入 token 成本。

本契约要解决的不是“把上下文藏到 provider”，而是：

1. 让相同的模型可见前缀在连续 model call 之间保持稳定；
2. 允许 provider 的 implicit prefix/KV cache 命中；
3. 在 provider 支持时，以 typed、可审计方式提供显式 cache hint；
4. 记录 provider 实际报告的 cached input usage；
5. 保证 cache miss、cache eviction 或 provider 不支持 cache 时，Pulsara 的正确性完全不受影响；
6. 不绕过 ResolvedModelCall、ContextCompiler、token estimator、event log 与 durable replay。

## 1. 先冻结四个不同概念

### 1.1 Provider implicit prefix cache

Pulsara 每次仍发送完整上下文。Provider 根据相同的模型、租户和请求前缀，自行复用已计算的 prefix/KV state。

特点：

- 不需要 `previous_response_id`；
- Pulsara 本地仍拥有完整输入事实；
- estimator 仍能估算完整输入；
- cache hit 只是性能优化，不是正确性依赖；
- provider 可能通过 usage 返回 `cached_tokens`，也可能完全不报告。

这是 V1 的首选方向。

### 1.2 Provider explicit cache hint

部分 provider/API 允许调用方标记 cache boundary、提供 cache key 或选择 cache retention policy。

这仍然属于“发送完整本地上下文，给 provider 一个缓存提示”。它可以作为后续能力加入，但必须经过 Pulsara-owned typed contract，不能从 `request_defaults` 或 `request_extra_body` 自由注入。

### 1.3 Provider remote continuation state

例如请求只传：

```json
{
  "previous_response_id": "resp_123"
}
```

然后由 provider 从服务器恢复历史上下文。

这不是普通 prompt cache，而是远端 conversation/context ownership。它会引入：

- 本地 estimator 看不到的输入；
- provider retention/expiry；
- resume 与 replay 无法只凭 Pulsara event log 重建；
- provider-side truncation 或 context mutation；
- 远端 identity、删除和迁移问题。

V1 继续禁止 `previous_response_id`、`conversation`、`prompt`、`context_management` 等 remote-context ingress。未来如果要支持，必须另立 `RemoteContinuationContract`，不能作为 prompt cache 的快捷实现。

不使用 `previous_response_id` 不等于关闭 KV cache。只要 Pulsara 重发相同前缀，provider 仍然可以进行 implicit prefix caching。

### 1.4 Pulsara local render/cache

ContextLifecycleCoordinator、tool-result render decision cache、memory recall cache 等属于 Pulsara 本地计算缓存。它们减少本地重复计算，但不等同于 provider prompt cache。

本地 render cache 可以帮助产生稳定的 provider-visible bytes，却不能被当成 provider 已命中 prefix cache 的证明。

## 2. 核心哲学

### 2.1 Cache 是优化，不是真源

Pulsara 的事实顺序保持为：

```text
durable events / artifacts / canonical memory
-> ContextCompiler
-> complete LLMContext
-> provider request
```

任何 cache 都不能成为缺失事实的唯一持有者。

### 2.2 正确性不能依赖 cache hit

同一个 model call 在以下情况下必须具有相同语义结果边界：

- provider cache hit；
- provider cache miss；
- cache 被逐出；
- provider 不支持 prompt cache；
- usage 不返回 cached token；
- Pulsara 主动关闭 cache hint。

Cache miss 不是 runtime error，也不触发 retry、compaction 或 provider fallback。

### 2.3 Pulsara 只相信实际发送的输入

Context budget、pre-send validation、inspection 和 replay 都以本次完整 provider-visible input 为准，不因 provider 声称可缓存而扣除 cached tokens。

即：

```text
input_budget_tokens
```

约束完整输入，而不是：

```text
input_tokens - cached_input_tokens
```

Cached tokens 影响性能/计费观察，不影响 context window 正确性。

### 2.4 Exact prefix stability 优先于“看起来相似”

Prompt prefix cache 通常要求 provider-visible token prefix 相同。空格、字段顺序、tool schema 顺序、动态时间戳、随机 ID、render degradation 都可能改变 token prefix。

因此 Pulsara 不能只比较 section identity；必须能比较 adapter lowering 后的 canonical provider input segments。

## 3. 当前代码真相

### 3.1 已经具备的基础

当前代码已有几个有利条件：

- `ModelTokenUsageFact.cached_input_tokens` 已能接收 provider 报告的 cached token；
- OpenAI-compatible usage normalizer 已读取 `cached_tokens`；
- built-in tools 在 permission mode 切换时保持 exposure-visible，gate 决定执行许可，因此 tool array 不会仅因 permission mode 改变；
- ResolvedModelCall 已冻结 model target、transport binding、provider request shape、effective output budget 与 estimator；
- provider remote-context keys 已从 extension allowlist 中拒绝；
- tool-result renderer 已有本地 render decision cache；
- compaction 和 durable replay 都以 Pulsara 本地事件、artifact 和 summary 为事实源。

### 3.2 当前缺口

当前还没有正式的 prompt-cache contract：

- 没有 provider-visible prefix manifest；
- 没有稳定 prefix fingerprint；
- 没有 cache boundary/policy DTO；
- 没有 predicted cache-break reason；
- 没有 inspector prompt-cache projection；
- 没有跨 model call 的 prefix stability regression；
- 没有 typed provider cache hint；
- 没有区分“本地 render cache hit”和“provider cached token reported”的统一观察口径。

### 3.3 当前 ContextCompiler 与 prefix cache 的冲突

当前 compiler 会在 transcript 前插入 `leading_user` runtime context：

```text
leading runtime context
-> prior transcript
-> recovery
-> current user
-> current run tail
```

memory projection、capability catalog、runtime context 等内容如果每轮变化，会让它们后面的全部 transcript 失去稳定前缀。

此外，timing overlay 会把本次 `compiled_at_utc` 和动态 `age` 渲染到多个 system、handoff 和 leading-user section。即使 section body 没变化，model-visible header 每轮也会变化，导致 prefix 从第一个动态 timing header 起失效。

因此当前的“时间感知正确性”与“prompt prefix 稳定性”需要在本契约实施时重新协调，不能各自独立优化。

### 3.4 MCP tool catalog 也是 cache boundary

MCP server tools 的加入、移除、schema 修改或顺序变化会改变 provider-visible tool definitions。即使 transcript 完全相同，tool catalog 变化也可能使 provider cache miss。

MCP snapshot generation 不能直接当作 cache identity；应以 canonical tool schema fingerprint 为真。Generation 只用作解释变化原因。

### 3.5 Typed user-carrier protocol 也是 cache boundary

Provider-visible human input、runtime request与runtime observation使用同一套closed carrier protocol。Generation compatibility必须绑定完整carrier protocol fingerprint，包括kind/producer registry、typed payload schema、canonical codec与root interpretation fragment。只要这些wire语义之一变化，就必须显式rollover；不能仅因root prose字面量未变而继续复用旧generation。

## 4. V1 产品决策

### 4.1 默认 policy

V1 冻结：

```text
prompt_cache_policy = implicit_prefix
```

含义：

- Pulsara 发送完整 LLMContext；
- 不使用 remote continuation；
- 不要求 provider 支持显式 cache key；
- 尽量保持 provider-visible prefix 稳定；
- 记录 provider usage 中实际报告的 cached tokens；
- 不报告 cached usage 时显示 `missing`，不推测为零。

同时保留：

```text
prompt_cache_policy = disabled
```

用于诊断、对照实验和 provider compatibility，但 disabled 只表示 Pulsara 不提供显式 hint/不承诺稳定性优化，不要求主动随机化输入以阻止 provider 的隐式缓存。

显式 provider cache hints 作为后续扩展：

```text
prompt_cache_policy = explicit_hint
```

它不应在第一轮 hard cut 中强制落地。

### 4.2 禁止 remote continuation

以下字段继续禁止从生产 provider extension 注入：

- `previous_response_id`
- `conversation`
- `prompt`
- `context_management`
- provider-side automatic truncation controls
- 任何会加载本地 estimator 不可见历史的字段

### 4.3 Cache miss 不触发 runtime mutation

不得因为 cache miss：

- 写 failed model event；
- 触发 retry；
- 切换 model；
- 改变 context；
- 提前 compaction；
- 让 agent 重新执行 tool。

## 5. Provider-visible prefix lanes

建议 ContextCompiler 最终形成以下逻辑 lanes：

```text
stable instructions
stable tool catalog
durable append-only transcript prefix
volatile turn context
current time observation
current user
current run assistant/tool tail
```

这不是要求所有 provider 使用相同 JSON shape，而是要求 adapter 能把自己的 wire input 映射回同一组语义 segment。

### 5.1 Stable instructions

包括稳定 system/developer contract。不得包含：

- 当前时间；
- context ID / call ID / run ID；
- 随机 nonce；
- 每轮变化的 diagnostic；
- permission mode 的动态说明；
- provider response identity。

如果核心 prompt/version 变化，应明确产生 `system_instructions_changed` cache break。

### 5.2 Stable tool catalog

所有 built-in tool 的 schema、description 和顺序必须 canonicalize。

现有“所有工具始终 exposure-visible，permission gate 决定是否允许执行”的设计应保留，因为它同时满足：

- capability surface 稳定；
- permission 安全；
- prompt prefix cache 稳定。

动态 MCP tools 单独纳入 tool schema fingerprint。MCP 变化是真实 cache boundary，不应为了命中率向模型展示过期 schema。

### 5.3 Durable append-only transcript

正常连续 model call 应尽量只在 transcript 尾部追加新 message，不重新渲染已经发送过的历史 message。

如果旧 tool result 因预算压力从 full 降级成 compact，prefix 将从该 tool result 开始变化。V1 应冻结：

- 已发给同一 target 的旧 tool-result canonical render decision 尽量 sticky；
- 只有 compaction、明确的 context lifecycle boundary 或必须满足 hard cap 时才允许重写旧 render；
- 重写时写明 predicted break reason，不能静默假装 prefix 仍稳定。

### 5.4 Volatile turn context

以下内容通常每轮可能变化，应放在 durable transcript 之后、current user 之前：

- memory recall/projection；
- capability diagnostics；
- subagent handoff/result projection；
- current runtime state；
- recovery hint；
- dynamic permission/status explanation。

这样它们的变化不会使更早的 durable transcript prefix 失效。

该顺序需要单独验证 provider tool-call/tool-result pairing 与模型遵循效果，不能只为 cache 命中率机械重排。

### 5.5 Current time observation

`compiled_at_utc`、local date 和“现在距离观察过去多久”必然每次 compile 变化。

V1 建议：

- section 的绝对 source timestamp 可以作为稳定事实随 section 保存；
- 不在每个稳定 section 的 header 中重复当前 `compiled_at_utc`；
- 将本次 current clock 作为一个 bounded、单独的 volatile suffix section；
- `age_seconds` 尽量由模型结合 source time/current time理解，或只在 volatile suffix 中提供；
- timing metadata 仍可在 inspector 完整保存，不要求全部进入稳定 model-visible prefix。

这样既保留时间感知，也避免一个每轮变化的时间戳出现在 system prompt 前部并使整个 transcript cache 失效。

### 5.6 Current run tail

当前 run 中的 assistant/tool-call/tool-result 必须保持 provider pairing，并自然追加在 current user 后。Universal tool observation timing 来源于 durable event，已经发生的 absolute timestamp 应在 replay 中保持不变，不能在每次 compile 时重新生成。

## 6. Canonical Provider Input Plan

不能从 `ContextSection` 文本直接推断 provider prefix；最终 cache identity 必须基于 adapter 实际要发送的输入。

建议新增纯函数 seam：

```python
lower_provider_input_plan(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> ProviderInputPlan
```

`ProviderInputPlan` 同时供以下路径消费：

- adapter payload builder；
- final token estimator/validator；
- prompt cache manifest builder；
- inspector diagnostic；
- golden payload tests。

禁止 adapter 在生成 plan 后再次插入 instructions、tools、messages、remote context 或 cache-affecting字段。

建议 DTO：

```python
@dataclass(frozen=True, slots=True)
class ProviderInputSegment:
    index: int
    kind: Literal[
        "system_instructions",
        "tool_catalog",
        "durable_transcript",
        "volatile_context",
        "current_user",
        "current_run_tail",
        "provider_framing",
    ]
    canonical_sha256: str
    estimated_tokens: int
    cache_stability: Literal["stable", "append_only", "volatile"]


@dataclass(frozen=True, slots=True)
class PromptCacheInputFact:
    contract_version: Literal["prompt-cache-input:v1"]
    policy: Literal["disabled", "implicit_prefix", "explicit_hint"]
    cache_identity_fingerprint: str
    provider_input_fingerprint: str
    segments: tuple[ProviderInputSegment, ...]
    stable_prefix_segment_count: int
    stable_prefix_estimated_tokens: int
    predicted_cache_break_reason: str | None
```

Event-safe fact 只保存 hash、kind、token estimate 和 bounded diagnostic，不保存一份重复的完整 prompt。

## 7. Fingerprint 规则

### 7.1 Cache identity fingerprint

`cache_identity_fingerprint` 至少覆盖：

- target fingerprint；
- requested model identity；
- reported model identity cohort（有报告时用于 observation 分组）；
- provider API kind；
- transport binding/contract version；
- adapter request-shape fingerprint；
- tokenizer/estimator contract version；
- prompt cache policy version；
- tool schema serialization version。

Cache miss 可能由 provider 内部 model snapshot 更新导致。`accept_reported` model identity policy 下，这属于允许的性能变化，不是 correctness error。

### 7.2 Provider input fingerprint

`provider_input_fingerprint` 必须来自 canonical provider-visible input plan，不得包含：

- API key；
- Authorization/header/cookie；
- URL query/userinfo；
- raw workspace path；
- raw user/account identifier；
- call/session/run UUID，除非该值真实发送给模型。

### 7.3 Canonical JSON

需要冻结：

- UTF-8；
- stable key order；
- compact separators；
- Unicode normalization policy；
- tuple/list normalization；
- tool schema order；
- absent 与 explicit null 的区别；
- float/number encoding；
- provider-specific message lowering version。

Fingerprint 必须描述实际语义输入；不能直接 hash Python `repr()`。

## 8. Cache boundary 与 break reason

Pulsara 只能预测本地 prefix 是否变化，不能宣称 provider 一定命中。因此字段应叫：

```text
predicted_cache_break_reason
```

建议稳定枚举：

- `first_call`
- `target_changed`
- `transport_binding_changed`
- `adapter_contract_changed`
- `system_instructions_changed`
- `tool_schema_changed`
- `mcp_tool_catalog_changed`
- `history_rewritten`
- `tool_result_render_changed`
- `context_compaction_boundary`
- `volatile_context_moved_into_prefix`
- `timing_prefix_changed`
- `explicit_cache_policy_changed`
- `unknown`

普通 append-only 新 turn 不应标记 break；它应显示之前 prefix 仍匹配，只增加 suffix。

Compaction 会用 summary 替换旧 transcript。这是合法的 cache boundary。Compaction 后的第一次 call 预期 cache miss，之后应以新的 summary + retained tail 建立稳定前缀。

## 9. Provider cache hints

如果未来启用 `explicit_hint`：

1. cache hint 必须是 `ResolvedModelTarget/Call` 的 typed 字段；
2. 必须进入 provider request-shape fingerprint；
3. adapter 显式声明是否支持；
4. payload builder 只能从 typed fact 生成 provider-native字段；
5. `request_defaults` / `request_extra_body` 仍不得注入 cache key、cache boundary 或 remote context；
6. 不支持时 resolution fail，不能 silent omit；
7. cache retention/TTL 是可选性能参数，不得成为 resume 保证。

若 provider 需要 cache key，建议从 scoped、opaque material 派生：

```text
HMAC(
  pulsara_cache_key_secret,
  cache_scope + target_fingerprint + stable_prefix_fingerprint
)
```

不得直接发送 workspace path、memory domain id、conversation id 或用户文本作为 cache key。

V1 显式 cache scope 最保守可限定为：

```text
runtime_session + target_fingerprint
```

未来跨 session 共享必须有明确的 privacy/domain contract。

## 10. Usage 与可观测性

Provider usage 是 cache hit 的唯一外部观察来源之一：

```python
ModelTokenUsageFact(
    input_tokens=...,
    cached_input_tokens=...,
    output_tokens=...,
    ...,
)
```

现有 invariant 保留：

```text
0 <= cached_input_tokens <= input_tokens
```

语义冻结：

- `cached_input_tokens=None`：provider 未报告；
- `cached_input_tokens=0`：provider 明确报告没有 cached input；
- `cached_input_tokens>0`：provider 报告本次部分输入使用 cache；
- 不允许把 missing 归一化成 0；
- 不允许用本地 prefix fingerprint 推断 provider cached token 数量。

Inspector 可派生：

```text
cache_report_status = reported | missing
cached_input_ratio = cached_input_tokens / input_tokens
predicted_stable_prefix_tokens
provider_reported_cached_input_tokens
predicted_break_reason
```

预测稳定 prefix 与 provider 实际 cached token 不相等不是 contract error。Provider 可能有最小缓存长度、block alignment、TTL、租户隔离或内部 eviction。

## 11. Inspector normalized shape

建议每次 model call 展示：

```json
{
  "prompt_cache": {
    "policy": "implicit_prefix",
    "cache_identity_fingerprint": "sha256:...",
    "provider_input_fingerprint": "sha256:...",
    "stable_prefix_estimated_tokens": 42000,
    "predicted_cache_break_reason": null,
    "usage_status": "reported",
    "provider_reported_cached_input_tokens": 40960,
    "cached_input_ratio": 0.81,
    "segments": [
      {
        "index": 0,
        "kind": "system_instructions",
        "estimated_tokens": 2200,
        "cache_stability": "stable"
      }
    ]
  }
}
```

Inspector 只展示本次 durable/compiled facts，不根据当前 prompt 或当前时间重新计算历史 fingerprint、age 或 cache ratio。

## 12. Failure 与降级语义

### 12.1 Fingerprint/manifest 构造失败

生产 hard-cut 路径中，如果 adapter 已准备发送 provider payload，但无法构造与之完全对应的 input plan/manifest，应在网络请求前 fail closed。否则 inspect 记录的 cache contract 与真实 payload 可能分叉。

### 12.2 Provider 不报告 cache usage

正常完成 model call，usage 显示 missing。不能把它视为 cache 功能失败。

### 12.3 显式 cache hint 被拒绝

如果 hint 是可选优化，可以在 resolution 前根据 provider capability 选择 `implicit_prefix`；一旦 `ResolvedModelCall` 已冻结为 `explicit_hint`，provider/adapter 不支持必须作为 target resolution/configuration error，不在 transport 内 silent omit。

### 12.4 Prefix 发生变化

正常发送完整新上下文，并记录 predicted break reason。不得为了保住 cache 而展示过期 memory、tool schema、permission facts 或 runtime state。

## 13. 安全与隐私边界

- Pulsara 不建立跨用户共享的 raw prompt cache；
- event log 不重复保存完整 provider payload；
- fingerprint 不可包含 secret；
- cache key 不可暴露 workspace/user/conversation identity；
- credential rotation 不应因为 secret 值进入 fingerprint 而改变 cache semantic identity；
- provider 的组织级隐式 cache isolation 属于 provider contract，Pulsara 只能记录配置与观察，不能伪装成自己保证的隔离；
- remote provider retention 不替代 Pulsara 的 explicit close/delete 语义。

## 14. 与其他子系统的关系

### 14.1 ResolvedModelCall

Prompt-cache fact 必须引用同一个 target/call contract。不得在 adapter 内根据 provider 默认再次决定 model、reasoning、tools、remote context 或 cache policy。

### 14.2 ContextCompiler

Compiler 负责语义 lanes 和 model-visible content；adapter 负责 provider-native lowering。二者通过 `ProviderInputPlan` 对齐，不能各自计算一套 prefix fingerprint。

### 14.3 Context compaction

Compaction 是显式 history rewrite/cache boundary。它仍由 context budget 决定，不能因为 cached tokens 较高而推迟超过 context window。

### 14.4 Universal tool observation timing

已经完成的 tool observation 使用 durable absolute timestamp，replay 时稳定。当前 compile wall-clock/age overlay 需要迁移到 volatile suffix，避免污染稳定 prefix。

### 14.5 MCP

MCP tool schema 变化是真实 cache break。MCP capability refresh 应通过 canonical tool schema fingerprint 判断变化，而不是仅依据 refresh generation。

### 14.6 Permission mode

工具 exposure 保持稳定，gate 决定是否执行。Permission 切换不应通过删除 tool schema 来优化安全，因为这既制造 capability 双真源，也破坏 prompt prefix。

## 15. 建议 PR 顺序

### PR0：Contract 与观测基线

- 冻结术语、policy、segment、fingerprint 和 break reason；
- 增加 `PromptCacheInputFact` / `ProviderInputSegment`；
- Inspector 展示现有 `cached_input_tokens`；
- 不改变 compiler ordering，不添加 provider hint。

### PR1：Canonical ProviderInputPlan

- adapter payload builder 与 cache manifest 消费同一个 input plan；
- 冻结 provider-native canonical serialization；
- pre-send 验证 manifest 与实际 payload input 一致；
- 禁止 payload builder 后置注入 model-visible字段。

### PR2：Context lanes 与 timing 协调

- stable instructions；
- durable transcript；
- volatile turn context suffix；
- current clock 单独放在 volatile suffix；
- section source timing 保持 absolute/stable；
- 验证 tool-call/tool-result pairing 不受影响。

### PR3：Tool/schema/render stability

- canonical tool ordering/schema fingerprint；
- permission mode 切换保持 tool catalog 不变；
- MCP tool schema 变化产生明确 break reason；
- 旧 tool-result render decision sticky；
- compaction/lifecycle rewrite 产生明确 boundary。

### PR4：Typed provider cache hints（可选）

- provider capability；
- typed cache policy；
- opaque scoped cache key；
- request-shape fingerprint；
- 不支持时 resolution fail；
- 继续禁止 arbitrary provider extension 注入。

### PR5：Metrics 与 dogfood

- Inspector prompt-cache projection；
- per target/provider cache ratio；
- predicted prefix tokens vs provider cached tokens；
- long-running REPL/tool loop dogfood；
- compaction 前后 cache boundary dogfood；
- missing usage provider 回归。

## 16. 测试矩阵

至少覆盖：

- 相同 call target + 相同 compiled context 产生完全相同 provider input fingerprint；
- 新增 current user 只追加 suffix，旧 prefix fingerprint 保持稳定；
- permission mode 切换不改变 tool schema fingerprint；
- `compiled_at_utc` 变化不改变 stable instructions/durable transcript prefix；
- memory recall 变化只改变 volatile suffix；
- durable tool observation absolute timing replay 稳定；
- 旧 tool result 不因下一 turn 宽/窄预算静默改写；
- compaction 明确产生 `context_compaction_boundary`；
- MCP schema 不变但 generation 变化不产生 tool-schema break；
- MCP schema 变化产生 `mcp_tool_catalog_changed`；
- provider reported cached tokens 正确进入 usage；
- usage missing 保持 `None`；
- cached tokens 大于 input tokens 被 schema 拒绝；
- cache miss 不触发 retry/compaction/model fallback；
- `previous_response_id` / `conversation` 继续被生产配置拒绝；
- explicit cache hint 不能通过 request defaults/extra body 注入；
- fingerprint/event facts 不含 secret、raw path 或 raw user identity；
- adapter payload 与 ProviderInputPlan 不一致时在网络前 fail closed。

Real dogfood 建议使用一个包含多轮 filesystem、terminal、terminal_process 和 MCP tool call 的长程任务，观察：

- 前缀是否按 append-only 方式增长；
- provider 是否报告 cached input；
- dynamic timing/memory 是否只影响 suffix；
- compaction 后是否出现一次预期 cache reset；
- compaction 后的新 prefix 是否重新获得缓存命中。

## 17. 非目标

本章不负责：

- 把远端 provider conversation 当作 Pulsara 真源；
- 用 cached tokens 绕过 context window；
- 保证 provider 一定命中 cache；
- 实现跨用户共享 prompt cache；
- 用 cache 替代 compaction；
- 用 cache 替代 durable event/artifact/memory；
- 为了命中率保留过期 tool schema、memory 或 permission事实；
- 在当前 ResolvedModelCall review 中顺手混入大规模 compiler reorder。

## 18. 当前结论

Pulsara 下一步应补的是“完整本地上下文之上的稳定 prefix contract”，而不是 `previous_response_id` 式远端上下文捷径。

短期优先事项是：

```text
canonical provider input plan
-> stable prefix lanes
-> timing/volatile context suffix化
-> tool schema/render stability
-> cached usage observability
```

显式 provider cache hint 可以后置。只要完整 prompt 的前缀稳定，即使不传远端 continuation ID，provider 仍可复用 KV/prefix cache；同时 Pulsara 保留完整预算控制、durable replay 和 inspect 能力。
