# Pulsara Round 5A.2：Durable Provider Replay 与跨进程线程续接实施规格

> 状态：**ACTIVATED**
>
> 记录日期：2026-08-19
>
> 当前代码基线：`a39e537fa56f6685c677496d0eb11628337675c0`
>
> 激活证据：[round5a2_durable_provider_replay_and_cross_restart_thread_continuation_activation.json](benchmarks/suites/core/v1/round5a2_durable_provider_replay_and_cross_restart_thread_continuation_activation.json)
>
> 上位架构：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 产品能力索引：[POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 前置实现：[Round 3 compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5A.1 provider output termination](ROUND_5A_1_PROVIDER_NEUTRAL_MODEL_OUTPUT_TERMINATION_IMPLEMENTATION_SPEC.zh.md)
>
> 后续依赖：[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)必须在本文激活以后实施；Round 5B只能消费本文恢复的exact replay prefix，不能另建summary专用reasoning persistence。
>
> 本文只支持`openai_chat_completions`与`openai_responses`两种wire family。本文不实现Anthropic Messages、Gemini、Claude、Google或任何vendor命名分支，也不建立provider下拉框、供应商能力表、在线探测或用户填写的“是否需要回放思考”选项。

---

## 0. 执行结论

Pulsara从本文开始对用户作出一条明确的产品承诺：

> 一条已经canonical接受的线程，可以在Pulsara进程退出、Host replacement、操作系统重启或机器关机以后继续。下一Host不得仅因旧Host的process-local reasoning carrier消失，就把同一已接受历史静默降级为另一份provider input。

这条承诺不等于恢复旧generation/reducer/recovery graph，也不等于持久化每一次HTTP请求。最小正确实现是：

```text
completed provider response
    -> freeze public assistant projection
    -> freeze exact native replay carrier, if one exists
    -> accept assistant row + blocks + replay row in one transaction
    -> FULL confirms the exact composite

process/OS/power loss
    -> discard live stream, sockets, tasks and continuity epoch

next Host / next model call
    -> exact-read canonical semantic history
    -> exact-read entry-bound replay manifest metadata
    -> compile a new cold semantic epoch
    -> resolve target and select only matching assistant groups
    -> hydrate only those selected, replay-compatible native bodies
    -> replace the selected groups with their native replay carrier
    -> open the same compatible wire target
```

本文冻结以下结论：

1. canonical conversation truth仍由`transcript_entries`、`assistant_message_blocks`、ToolResult等现有relations拥有。
2. 新增唯一的`provider_assistant_replay_fragments` relation，保存已经完成、已经public-projection校验、并与exact assistant winner同事务接受的private provider-wire continuation fact。
3. replay row不是assistant正文、Thinking UI、memory、artifact、context snapshot、event payload或request audit；它只服务下一次provider wire materialization。
4. Chat只认识固定的三个OpenAI-compatible response字段：`reasoning_content`、`reasoning`与`reasoning_details`。只持久化上游在完整response中实际返回的字段；不补空值、不注入占位思考块、不从普通text推导reasoning。
5. Responses只持久化Round 5A.1已冻结的exact ordered `reasoning | message | function_call` output items；不依赖`previous_response_id`，也不持久化hosted/effect-bearing unknown item。
6. 不根据`provider == deepseek | kimi | openrouter | dashscope | ...`选择行为。唯一分支是`openai_chat_completions | openai_responses`的wire codec分支。
7. 不要求用户判断某个兼容端点是否“必须回放思考”。Runtime保存所有closed、actual-observed、completed native carrier；若没有返回carrier，就没有可保存或补造的carrier。
8. 不在Chat与Responses之间翻译reasoning；不把Responses item改写成`reasoning_content`，也不把Chat字段包装成Responses reasoning item。
9. replay只对同一exact scope、assistant entry、public projection、replay target与replay contract有效。这里的replay target是比普通resolved target更窄的provider-wire compatibility identity；target不兼容时不得使用旧carrier。
10. 已完成并接受的前缀可以跨重启恢复；尚在stream中的partial reasoning/text/tool arguments仍是disposable live state。
11. 本文不承诺恢复正在执行的provider stream、physical tool、Terminal process或未完成turn。writer takeover与Round 7 outcome继续表达这些中断；本文只保证已接受历史不会丢失其必要wire carrier。
12. 每个assistant entry都冻结`provider_wire_api`与closed replay disposition：`PUBLIC_SEMANTIC_ONLY | NATIVE_REPLAY`。`NATIVE_REPLAY`与exact replay row是一个不可分割的canonical composite；不得出现“assistant声称NATIVE_REPLAY、carrier只在旧Host内存”的新写入。
13. assistant ACK-unknown仍由现有shielded settlement owner确认；confirmation同时检查assistant、blocks、event、turn terminal disposition与optional replay row。
14. 新Host从PostgreSQL canonical rows重建，不从CommittedEvent replay、remote response ID、日志、activation evidence或provider server session恢复。
15. durable payload有严格top-level item、nested JSON、单row与selected-hydration working-set上界；越界在assistant mutation前typed拒绝，不允许silent truncation。
16. hidden reasoning仍是低authority opaque data。持久化不把它升级为permission、Plan、memory、tool-effect或conversation semantic truth。
17. 不新增CommittedEvent、LiveEvent、subject、append guard、durable job、receipt、checkpoint、repair、replay worker或background GC。
18. schema只增加一张subordinate relation与`transcript_entries`上的closed wire API/disposition/exact pointer字段。目标oracle为`31 Committed / 24 Live / 13 subjects / 2 guards / 26 product relations / 1 durable job`。

---

## 1. 产品承诺的精确边界

### 1.1 “线程可以继续”是什么意思

以下时序必须成立：

```text
用户提交 U1
provider完整返回 assistant A1
A1 canonical FULL
Pulsara进程退出或机器关机
用户重新打开同一个session并提交 U2
下一次provider input仍能表达 U1/A1 的合法native历史
```

若A1具有closed native replay carrier，下一Host必须使用持久化carrier；若A1没有任何实际返回的private carrier，canonical public assistant lowering本身就是完整可恢复历史。

“继续”不承诺恢复下列瞬时状态：

- 尚未收到explicit `COMPLETED` terminal的stream；
- incomplete/failed/cancelled response中的partial text、reasoning或tool arguments；
- 已经开始但尚未canonical结算的physical tool invocation；
- provider侧仅由remote response ID拥有、但本地没有exact item history的状态；
- 进程内live event cursor、interaction waiter、socket、task或deadline。

这些边界不会破坏线程：旧canonical前缀仍在；未完成的当前turn由既有interruption/takeover truth终结，下一真实用户消息可以继续。

### 1.2 crash线性化

本文只允许三个物理结果：

| crash位置 | canonical结果 | 下一Host行为 |
|---|---|---|
| provider完成前 | 无assistant、无replay | 从最后一个accepted entry继续 |
| assistant transaction前 | 无assistant、无replay | 同上 |
| assistant transaction commit后 | exact assistant + replay disposition + required exact row均存在 | 读取并恢复 |

不存在合法的第四种结果：

```text
assistant disposition == NATIVE_REPLAY
AND expected replay was only process-local
```

### 1.3 target变化

同一compatible replay target保持native replay。若用户主动改变API、endpoint、model、semantic transport binding、codec或provider replay contract，旧carrier保留在数据库中但不跨target使用。这里不存在开放式“replay-relevant request options”集合；唯一compatibility输入由§6.1的closed carrier定义。

target变化时允许建立显式cold semantic epoch：

- canonical thread仍可读取；
- compiler可按新target从public semantic history构造输入；
- 旧private carrier不翻译、不注入；
- 这一行为是target rebase，不得伪装成same-target exact resume。

若当前target与历史entry的replay target兼容、该entry被本次compiler实际选择，而它所需的row损坏或缺失，则provider open为0并报告typed canonical corruption；不得悄悄semantic fallback。未被本次compiler选择或已由明确不兼容target走cold semantic continuation的private body不参与本次hydrate，也不能仅因其正文损坏而阻止合法调用。

---

## 2. 为什么5A.1的process-local边界已经过时

Round 5A.1正确解决了：

- explicit provider terminal；
- whole-response atomicity；
- Chat closed reasoning fields；
- Responses exact output item allowlist；
- assistant FULL前不执行tool；
- same-epoch actual-wire strict prefix；
- caller cancellation下的assistant ACK confirmation。

但其生命周期冻结为：

```text
Host loss / cold reset
    -> discard ProviderAssistantReplayFragment
    -> generic semantic rebuild
```

这与本文的新产品承诺冲突。它会造成：

1. Codex Desktop式“关机后继续同一线程”只能恢复公开text/tool call，不能恢复provider native continuation；
2. 同一endpoint、model与wire contract在重启前后收到不同历史；
3. same-Host strict-prefix很强，跨Host却静默退化；
4. provider恰好要求历史reasoning carrier时，重启后的第一轮可能失败；
5. Round 5B若在重启后开始summary，也拿不到旧epoch本来已经接受的native work carrier。

本文因此只推翻5A.1的**生命周期/存储结论**，不推翻其terminal、allowlist、bounds、public-projection compare、wire planning或settlement owner。

---

## 3. Authority与数据分类

### 3.1 三层事实

| 层 | 示例 | authority | durable |
|---|---|---|---|
| conversation semantic truth | user text、assistant text/tool calls、ToolResult | canonical conversation relations | 是 |
| provider replay truth | Chat exact assistant message + observed reasoning fields；Responses exact ordered output items | `provider_assistant_replay_fragments` | 是 |
| live telemetry | streaming Thinking、partial text、token/usage delta | process-local Live plane | 否 |

provider replay truth是canonical row，但不是conversation semantic truth。这里的“canonical”只表示：它是数据库中唯一、immutable、可exact-confirm的provider-wire事实，而不是“模型思考成为产品事实”。

### 3.2 replay row不能拥有的能力

replay row不能：

- 改变assistant public text或tool calls；
- 创建、批准或重写ToolExecutionAttempt；
- 授予permission；
- 创建memory candidate/fact；
- 改变Plan/TODO/subagent/Terminal authority；
- 作为用户可见reasoning history；
- 被`artifact_read`、memory tools、Inspector或Protocol读取；
- 进入compaction summary正文；
- 作为provider request/response审计日志；
- 触发event replay或background execution。

### 3.3 “sealed”的含义

本文中的sealed表示：

- immutable；
- 不进入普通repository read surface；
- 不进入日志、error text、repr、evidence或provider-visible Runtime observation；
- 只允许assistant settlement写入、provider input reader读取。

它不宣称新增application-level encryption key owner。数据库卷加密、备份加密与operator访问控制属于deployment security；本文不为此发明key relation或恢复机制。

---

## 4. Prior art的批判性吸收

### 4.1 Codex

本地Codex代码表明，Responses reasoning item不是只能活在一次进程里的临时对象：

- protocol模型能够表达reasoning item及`encrypted_content`；
- rollout持久化完整response items；
- resume reconstruction从rollout重新构造model history。

相关代码见：

- [`codex-rs/protocol/src/models.rs`](../codex/codex-rs/protocol/src/models.rs)
- [`codex-rs/rollout/src/policy.rs`](../codex/codex-rs/rollout/src/policy.rs)
- [`codex-rs/core/src/session/rollout_reconstruction.rs`](../codex/codex-rs/core/src/session/rollout_reconstruction.rs)

值得吸收的是“completed native item可以持久化并用于resume”。不吸收其完整rollout/event机制；Pulsara已有relational canonical conversation，只需一张entry-bound replay relation。

### 4.2 Anybox窄探针

按用户要求，本轮由独立subagent只读探索Anybox后端`packages/anyboxagent`的provider/session/LLM/persistence路径，未探索前端、未修改文件、未发真实provider请求。

探针确认Anybox是明显的provider-aware/vendor-aware系统：

- 通过`model.api.npm`选择OpenAI、OpenAI-compatible、DeepSeek、OpenRouter、Google、Anthropic等SDK adapter；
- `provider/transform.ts`直接按OpenAI、DeepSeek、Google生成不同options；
- DeepSeek可被强制切换到专用SDK；
- SQLite保存`sessions / turns / messages / parts`；
- reasoning持久化为normalized `ReasoningPart(text, providerMetadata)`；
- resume时把parts重新组装成AI SDK通用`type: reasoning`，具体wire字段由SDK翻译。

主要证据：

- [`provider/provider.ts`](../anybox/packages/anyboxagent/src/provider/provider.ts)
- [`provider/transform.ts`](../anybox/packages/anyboxagent/src/provider/transform.ts)
- [`session/core/message.ts`](../anybox/packages/anyboxagent/src/session/core/message.ts)
- [`session/core/processor.ts`](../anybox/packages/anyboxagent/src/session/core/processor.ts)
- [`database/Sqlite.ts`](../anybox/packages/anyboxagent/src/database/Sqlite.ts)

探针没有发现Anybox持久化原始`reasoning_content`、`reasoning_details`、Responses完整`output[]`、`encrypted_content`、`previous_response_id`或raw provider response。它证明“语义message/part可以跨重启恢复”，但不能证明exact provider-wire replay。

本文只吸收它的两点：

1. semantic rows与runtime trace分层；
2. reasoning不塞进assistant public text。

本文明确不吸收：

- vendor分支；
- provider下拉/能力枚举；
- 把通用reasoning text交给SDK隐式翻译；
- 用normalized metadata替代exact observed native carrier。

### 4.3 ccswitch/Kimi案例

用户提供的ccswitch更新说明表明：向Kimi历史注入占位思考块、或发出上游未要求的非标准`reasoning_content`，会扰乱模型；DeepSeek/MiMo的服务端契约又可能不同。

该案例不推出“为每个vendor写分支”，而是推出更小的共同规则：

```text
do not infer
do not translate
do not synthesize
persist only actual observed closed carrier
replay only to the same compatible target
```

如果上游没有返回某字段，Pulsara不会因为endpoint名称而补造它。

---

## 5. Closed wire-family contract

### 5.1 支持矩阵

| wire API | accepted replay codec | durable内容 |
|---|---|---|
| `openai_chat_completions` | `CHAT_CLOSED_REASONING_FIELDS` | 一条exact assistant message，包含public `content/tool_calls`及实际出现的known reasoning字段 |
| `openai_responses` | `RESPONSES_EXACT_OUTPUT_ITEMS` | exact ordered `reasoning/message/function_call` items |

以下值在production代码、配置与测试中都不是合法5A.2分支：

```text
anthropic
claude
gemini
google
deepseek
kimi
moonshot
qwen
openrouter
dashscope
```

这些字符串可以作为用户的opaque endpoint/model/provider label存在于配置诊断中，但不得出现在replay选择的条件分支、enum或dispatch table中。

### 5.2 Chat

Chat closed field registry保持：

```text
reasoning_content -> TEXT_CONCAT
reasoning         -> TEXT_CONCAT
reasoning_details -> ORDERED_ARRAY_APPEND
```

Round 5A.2收紧一处语义：registry recognition不再受`ThinkingReplayPolicy`、tool-call presence、vendor preset或用户选择控制。

```text
COMPLETED + at least one actual known field
    -> freeze exact assistant replay message

COMPLETED + no actual known field
    -> no replay row; canonical public assistant is sufficient
```

request-side thinking enablement、reasoning effort与generic extra body仍可属于resolved request shape；它们不能决定历史是否被可靠保存。

禁止：

- 生成`"tool call"`、空字符串或其他placeholder；
- 把`reasoning_content`复制到`reasoning`；
- 把`reasoning_details`flatten为text；
- 从`<think>`正文提取private field；
- 在tool result以后才补写assistant carrier；
- 把未知字段持久化为open-world blob并盲目回放。

Round 5A.1对unknown non-empty字段的fail-closed/public-final-text边界保持；5A.2不扩大closed registry。

### 5.3 Responses

Responses durable item allowlist保持exact：

```text
reasoning
message
function_call
```

对一个已经接受的Responses assistant entry，exact ordered output items是required replay row。禁止只保存reasoning item再从canonical blocks补function call，也禁止只保存public message而丢掉encrypted reasoning。

以下内容不进入正确性：

- `previous_response_id`；
- remote response/session ID；
- provider-side stored conversation；
- SDK object identity；
- raw SSE event顺序；
- unknown/hosted/effect-bearing output item。

### 5.4 payload编码

`ordered_items`使用现有frozen JSON vocabulary。durable `payload_bytes`是整个ordered tuple的canonical JSON array bytes，而不是Python pickle、SDK对象或HTTP raw body。

这保留：

- array item order；
- object key/value语义；
- opaque string内容；
- call/item IDs；
- encrypted content字符串；
- public message/function call的exact logical shape。

它不承诺保留HTTP JSON object key order、whitespace或chunk boundaries；这些从来不是OpenAI JSON wire的语义identity。

---

## 6. Pure DTO

### 6.1 durable candidate

5A.1的`ProviderAssistantReplayFragment`升级为v2语义，并去掉“只属于same-epoch”的生命周期假设：

```python
@dataclass(frozen=True, slots=True)
class PreparedDurableProviderAssistantReplay:
    replay_id: str
    session_id: str
    workspace_id: str
    assistant_entry_id: str
    wire_api: Literal[
        "openai_chat_completions",
        "openai_responses",
    ]
    codec_kind: Literal[
        "CHAT_CLOSED_REASONING_FIELDS",
        "RESPONSES_EXACT_OUTPUT_ITEMS",
    ]
    provider_replay_contract_fingerprint: str
    replay_target_fingerprint: str
    public_projection_fingerprint: str
    ordered_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    payload_bytes: bytes = field(repr=False)
    payload_digest: str
    payload_size: int
    item_count: int
    fragment_fingerprint: str
```

assistant settlement另冻结：

```python
class ProviderReplayDisposition(StrEnum):
    PUBLIC_SEMANTIC_ONLY = "PUBLIC_SEMANTIC_ONLY"
    NATIVE_REPLAY = "NATIVE_REPLAY"
```

closed matrix：

| wire API | actual carrier | disposition | replay row |
|---|---|---|---|
| Chat | 无known private field | `PUBLIC_SEMANTIC_ONLY` | absent |
| Chat | 至少一个actual known field | `NATIVE_REPLAY` | required |
| Responses | exact allowed output | `NATIVE_REPLAY` | required |

这使cold reader能区分“Chat本来就没有private carrier”与“Responses/native replay row缺失”；不得靠当前config或payload absence猜测。

closed invariants：

- codec与wire API一一对应；
- Chat恰好一个assistant message；
- Responses item type exact allowlist；
- `payload_bytes == canonical_json_bytes(ordered_items)`；
- `payload_digest == sha256(payload_bytes)`；
- `payload_size == len(payload_bytes)`；
- `item_count == len(ordered_items)`，且这里只表示canonical JSON array的top-level item count；
- public projection重新计算后exact等于candidate；
- `replay_id`与`fragment_fingerprint`由domain-separated stable builder生成；
- `ordered_items`、`payload_bytes`以及任何后续decoded/hydrated private body字段都必须使用`field(repr=False)`；异常与日志不得通过container repr间接打印正文。

`provider_replay_contract_fingerprint`只覆盖closed field/item allowlist、accumulation、canonical encoding/decoding、public-projection compare与adapter replay materialization contract。physical byte/item/JSON bounds继续由独立hard-bound contract执行，不进入target compatibility；提高本地安全上界不能使既有carrier无意义失效，降低上界则可能在selected hydration时得到typed resource boundary。该fingerprint不能直接复用整个provider profile/request-shape fingerprint。

本轮冻结唯一closed target carrier：

```python
@dataclass(frozen=True, slots=True)
class ProviderReplayTargetCompatibilityFact:
    wire_api: Literal[
        "openai_chat_completions",
        "openai_responses",
    ]
    endpoint_identity_fingerprint: str
    normalized_model_identity_fingerprint: str
    transport_binding_id: str
    codec_kind: Literal[
        "CHAT_CLOSED_REASONING_FIELDS",
        "RESPONSES_EXACT_OUTPUT_ITEMS",
    ]
    provider_replay_contract_fingerprint: str
    compatibility_contract_version: Literal[
        "pulsara.provider-replay-target-compatibility.v1"
    ]
    replay_target_fingerprint: str
```

`replay_target_fingerprint`只覆盖以上closed字段，不接受自由`request_shape`、`request_defaults`或`extra_body`映射。以下内容明确**不进入**compatibility：

- model-call purpose与ROOT/child role；
- `tool_choice`、provider tools及permission；
- stream、usage/include、remote response ID与server-side state选项；
- temperature/top-p/seed等sampling controls；
- reasoning effort/thinking enablement及maximum output tokens等output controls；
- context budget、safety margin、token estimator与local logical limits；
- timeout、retry、concurrency、watchdog、credential、headers、client/socket；
- 与closed replay codec无关的generic request defaults/extra body。

各字段的身份边界同样closed：

- `endpoint_identity_fingerprint`复用canonical endpoint builder：拒绝userinfo/query/fragment，覆盖normalized scheme、host、port与base path；credential/headers不参与，相同网络目标在fresh process中必须得到相同值；
- `normalized_model_identity_fingerprint`来自adapter实际发送的normalized model identifier，不使用展示label或provider枚举；
- `transport_binding_id`是稳定的semantic adapter binding ID，例如Chat/Responses adapter常量，不是client、socket、registry object或本次process生成的ID；
- `codec_kind`与`provider_replay_contract_fingerprint`共同封闭accepted carrier及其重放算法。

因此新Host重新创建transport/client不会改变compatibility；若一个所谓binding ID不能跨进程稳定重建，就没有资格进入本fact。完整transport contract version也不进入本fact，因为它可能因streaming、telemetry或其他与historical replay无关的adapter变化而升级；真正改变历史carrier materialization的变化必须由`provider_replay_contract_fingerprint`诚实表达。provider profile名称、供应商名称、UI下拉值均不进入该身份。

若未来某个配置确实改变accepted historical carrier的字段、item、encoding或materialization语义，adapter必须显式升级`provider_replay_contract_fingerprint`；不得通过把整个open-world request options hash塞入target fingerprint来“保险”。因此普通`AGENT_MODEL_LOOP`产生的carrier可以由同endpoint/model/semantic transport binding/codec/replay contract的`CONTEXT_COMPACTION_SUMMARY`消费，即使后者固定发送`tool_choice=none`。完整`ResolvedModelTargetFact.target_fingerprint`继续服务normal dispatch/continuity exact join，但不得直接写入durable replay row或作为跨重启replay compatibility gate。

writer必须从产生该completed response的exact `PreparedKernelModelCall/ResolvedModelTarget`调用唯一`provider_replay_target_fingerprint(...)` builder；reader对current resolved call调用同一builder。repository不从endpoint字符串、model label或任意JSON自行重算，调用方也不能手填一个独立fingerprint。candidate factory同时验证其`provider_replay_contract_fingerprint`与target fingerprint builder输入一致，避免两份compatibility identity漂移。

### 6.2 read carrier

```python
@dataclass(frozen=True, slots=True)
class FrozenDurableProviderReplayManifest:
    replay_id: str
    assistant_entry_id: str
    wire_api: str
    codec_kind: str
    provider_replay_contract_fingerprint: str
    replay_target_fingerprint: str
    public_projection_fingerprint: str
    payload_digest: str
    payload_size: int
    item_count: int
    fragment_fingerprint: str
    manifest_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenDurableProviderReplayManifestCut:
    session_id: str
    scope: ProviderInputContinuityScope
    context_binding_revision_id: str
    provider_input_through_sequence: int
    manifests: tuple[FrozenDurableProviderReplayManifest, ...]
    aggregate_manifest_utf8_bytes: int
    cut_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenCanonicalProviderDispatchRead:
    compile_snapshot: FrozenCanonicalCompileSnapshot
    replay_manifest_cut: FrozenDurableProviderReplayManifestCut
    composite_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenSelectedDurableProviderReplayHydration:
    scope: ProviderInputContinuityScope
    source_manifest_cut_fingerprint: str
    replay_target_fingerprint: str
    selected_message_placements_fingerprint: str
    selected_manifests: tuple[FrozenDurableProviderReplayManifest, ...]
    fragments: tuple[ProviderAssistantReplayFragment, ...] = field(repr=False)
    aggregate_payload_bytes: int
    hydration_fingerprint: str
```

`FrozenDurableProviderReplayManifest.manifest_fingerprint`覆盖该manifest除自身fingerprint以外的全部metadata字段。`FrozenDurableProviderReplayManifestCut`满足：

- `session_id == scope.session_id`；
- `cut_fingerprint`以独立domain/version覆盖`session_id`、完整`scope`、`context_binding_revision_id`、`provider_input_through_sequence`、ordered manifest fingerprints与`aggregate_manifest_utf8_bytes`；
- manifests只来自该session、exact ROOT/child scope、effective source floor之后且不超过该through-sequence的assistant entries，并按canonical provider order排列。

因此`source_manifest_cut_fingerprint`不是调用方随意提供的opaque字符串，而是对exact session/scope/cut及其完整manifest序列的机械承诺。

`compile_snapshot`与metadata-only `replay_manifest_cut`由同一个REPEATABLE READ reader transaction构造。该事务只读取entry pointer与replay metadata，不SELECT、decode或物化`payload_bytes`。transaction结束后，现有target resolver冻结normal target/compile binding并派生当前`replay_target_fingerprint`；compiler消费`compile_snapshot`；wire planner从最终compiled messages选择exact assistant entries，并只对其中target-compatible的manifest执行第二阶段hydration。

selected hydration使用新的read-only、bounded repository transaction，但查询必须携带`scope.session_id`，并按ordered exact `session_id + replay_id + assistant_entry_id + payload_digest + fragment_fingerprint`读取body。`ProviderInputContinuityScope`已经唯一拥有session ID，因此DTO不重复保存第二个`session_id`标量。这里的“新的transaction”不表示新的planning owner或deadline，见下文。replay relation没有UPDATE/DELETE production privilege且row immutable，因此不需要跨compiler长期持有数据库transaction，也不存在可被合法writer改变的TOCTOU窗口。hydrate以后仍须重新验证canonical JSON、digest/size、JSON structural bounds、codec union、public projection与fragment fingerprint。

hydration DTO只在至少一个selected、target-compatible `NATIVE_REPLAY` manifest需要native replacement时存在，且`selected_manifests`/`fragments`均非空。若本次compiled input没有这种entry，则不执行body query、不构造empty hydration，wire plan的hydration fingerprint为`None`。

唯一factory必须同时接收source manifest cut、current compiled input/placements、current replay-target fact与returned rows，并证明：

1. `scope`逐字段等于`replay_manifest_cut.scope`，且`replay_manifest_cut.session_id == scope.session_id`；repository query的session ID只能取`scope.session_id`；
2. `source_manifest_cut_fingerprint == replay_manifest_cut.cut_fingerprint`；
3. selected manifests是该cut中无重复、保持原顺序的exact子集；
4. 每个selected manifest的assistant entry都在current compiled input中形成唯一、连续、ordinal完整的assistant placement group；
5. `selected_message_placements_fingerprint`由这些exact `FrozenCompiledMessagePlacement.placement_fingerprint`按provider message order通过domain-separated builder生成；
6. 每个manifest、fragment与current `replay_target_fingerprint`、codec、public projection exact join；
7. `hydration_fingerprint`覆盖完整scope（其中已含session）、source-cut fingerprint、replay-target fingerprint、selected-placement fingerprint、ordered manifest/fragment fingerprints与aggregate byte quote，但不序列化private正文。

`FrozenProviderWireInputPlan.message_placements_fingerprint`与replacement identities必须再次exact join该compiled input；因此不能把另一个session、ROOT/child scope、cut或assistant group的合法fragment移植进当前wire plan。

Round 5A.2同时给现有`FrozenProviderWireInputPlan`增加一个process-local proof引用：

```python
provider_replay_hydration_fingerprint: str | None
```

native replacements为空时该值必须为`None`；存在任意native replacement时必须等于本次`FrozenSelectedDurableProviderReplayHydration.hydration_fingerprint`。plan fingerprint与continuity CAS覆盖该字段；每个replacement中的assistant entry、fragment fingerprint和message placement必须在hydration中有exact one对应项。5A.2激活以后，不允许绕过cut-bound selection fact直接把裸`ProviderAssistantReplayFragment`塞进wire plan；same-Host可复用已经FULL的immutable body bytes作为物理优化，但仍必须先对current manifest cut/placements构造同一hydration proof。

continuity append candidate只能从该final wire plan生成，并必须exact join其`plan_fingerprint`与`provider_replay_hydration_fingerprint`；CAS安装semantic epoch view与actual-wire proof时一次性安装两者。CAS失败、steer重试、target变化或compiler重建都会使旧hydration和旧wire plan一起失效，不能把旧hydration重新挂到新candidate。

selected hydration仍属于创建当前model call时唯一的`PROVIDER_DISPATCH_PLANNING` attempt。initial RR read、target/compile binding、compiler trials、manifest selection、selected hydration、decode/validation与final wire-plan quote共享同一个absolute deadline。repository call只取得fresh read-only transaction，不调用deadline factory、不把remaining time重置为120秒；其physical deadline不得晚于caller已有planning deadline。deadline过期时provider open为0、planning state/borrow释放，不能consume steer或安装continuity。deadline是process-local调用参数，不进入hydration DTO、fingerprint或canonical row。

`FrozenSelectedDurableProviderReplayHydration`只属于本次wire-plan attempt；未选中、target不兼容或PUBLIC_SEMANTIC_ONLY的entry不进入该对象。不得建立fingerprint到body的mutable cache、durable hydration receipt或background repair owner。

### 6.3 不再保留的选择字段

`ProviderReasoningReplayScope.NEVER | TOOL_RESPONSES | ALL_COMPLETED_RESPONSES`不得继续决定durable retention。5A.2 implementation可删除该scope，或将旧配置仅保留为deprecated request-side compatibility input，但不得让它造成相同actual response有时落盘、有时丢失。

同理，`required_on_selected_response`不能成为“是否保存actual field”的前置。它若保留，只能作为completed response的wire validation，不得让Runtime合成缺失字段。

---

## 7. Canonical schema

### 7.1 新relation

clean-v0目标schema新增：

```sql
CREATE TABLE pulsara_v3.provider_assistant_replay_fragments (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    wire_api text NOT NULL CHECK (wire_api IN (
        'openai_chat_completions',
        'openai_responses'
    )),
    codec_kind text NOT NULL CHECK (codec_kind IN (
        'CHAT_CLOSED_REASONING_FIELDS',
        'RESPONSES_EXACT_OUTPUT_ITEMS'
    )),
    provider_replay_contract_fingerprint text NOT NULL,
    replay_target_fingerprint text NOT NULL,
    public_projection_fingerprint text NOT NULL,
    payload_bytes bytea NOT NULL,
    payload_digest text NOT NULL,
    payload_size bigint NOT NULL,
    item_count integer NOT NULL,
    fragment_fingerprint text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, assistant_entry_id),
    UNIQUE (session_id, assistant_entry_id, id),
    UNIQUE (session_id, assistant_entry_id, wire_api, id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    CHECK ((wire_api = 'openai_chat_completions') =
           (codec_kind = 'CHAT_CLOSED_REASONING_FIELDS')),
    CHECK (payload_size = octet_length(payload_bytes)),
    CHECK (payload_size BETWEEN 2 AND 16777216),
    CHECK (
        (wire_api = 'openai_chat_completions' AND item_count = 1)
        OR
        (wire_api = 'openai_responses' AND item_count BETWEEN 1 AND 4096)
    ),
    CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (provider_replay_contract_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (replay_target_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (public_projection_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (fragment_fingerprint ~ '^sha256:[0-9a-f]{64}$')
);
```

`transcript_entries`增加：

```sql
provider_wire_api text,
provider_replay_disposition text,
provider_replay_fragment_id text,
UNIQUE (session_id, id, provider_wire_api, provider_replay_fragment_id),
CHECK (
    (
        entry_kind IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
        AND provider_wire_api IN (
            'openai_chat_completions',
            'openai_responses'
        )
        AND provider_replay_disposition IN (
            'PUBLIC_SEMANTIC_ONLY',
            'NATIVE_REPLAY'
        )
        AND (provider_replay_fragment_id IS NOT NULL) =
            (provider_replay_disposition = 'NATIVE_REPLAY')
        AND (
            provider_wire_api <> 'openai_responses'
            OR provider_replay_disposition = 'NATIVE_REPLAY'
        )
    )
    OR
    (
        entry_kind NOT IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
        AND provider_wire_api IS NULL
        AND provider_replay_disposition IS NULL
        AND provider_replay_fragment_id IS NULL
    )
)
```

并建立deferred cyclic exact join：

```sql
ALTER TABLE pulsara_v3.transcript_entries
ADD CONSTRAINT transcript_entries_provider_replay_fk
FOREIGN KEY (
    session_id, id, provider_wire_api, provider_replay_fragment_id
)
REFERENCES pulsara_v3.provider_assistant_replay_fragments (
    session_id, assistant_entry_id, wire_api, id
)
DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE pulsara_v3.provider_assistant_replay_fragments
ADD CONSTRAINT provider_replay_assistant_entry_fk
FOREIGN KEY (session_id, assistant_entry_id, wire_api, id)
REFERENCES pulsara_v3.transcript_entries (
    session_id, id, provider_wire_api, provider_replay_fragment_id
)
DEFERRABLE INITIALLY DEFERRED;
```

这使数据库直接表达：

```text
assistant disposition NATIVE_REPLAY
    <=> exact replay pointer PRESENT
    <=> exact replay row PRESENT

assistant disposition PUBLIC_SEMANTIC_ONLY
    <=> Chat wire API
    <=> replay pointer/row ABSENT
```

不需要constraint trigger、receipt或repair job。

### 7.2 为什么不复用artifact/blob

replay payload不得进入artifact/read surface。直接放入bounded `bytea` row有三个优点：

- 不会获得artifact handle；
- 不会被ToolResult GC或artifact pagination误读；
- assistant transaction与exact confirmation更小。

payload最大16 MiB，与5A.1 completed-response aggregate bound一致。若canonical array encoding将响应推过上界，必须在assistant mutation前typed payload-limit失败；不能先接受assistant再省略carrier。

`item_count`只计算durable canonical array的top-level元素：Chat的array固定只有一个exact assistant object；Responses复用adapter已经冻结的4096个top-level output item上限。Chat accumulator内部最多65,536个reasoning fragments/array elements是另一条nested construction bound，不能写成durable top-level `item_count`。cold decode/hydrate还必须复用现有JSON node 65,536、depth 128、single string 16 MiB与Chat nested-item上界；数据库CHECK不替代pre-parse bounded decoder。

### 7.3 reset策略

本项目仍处于clean-v0开发阶段。本文实施时直接修改baseline与manifest，使用真实本地PostgreSQL进行fresh、repeat、deep verify与reset-required测试；不建立在线migration chain、legacy backfill或dual-read。

本文激活前创建的旧数据库不受新承诺保护，必须reset。激活以后，任何新accepted assistant都满足pointer/replay不变量。

---

## 8. Assistant settlement与ACK unknown

### 8.1 candidate必须先完整冻结

`PreparedAssistantMessageSettlement`在任何repository mutation前必须同时冻结：

- existing cut；
- stable assistant entry/blocks/content；
- exact provider wire API与replay disposition；
- stable assistant entry ID，以及`NATIVE_REPLAY`分支预先生成的stable replay ID；
- complete_turn disposition；
- optional `PreparedDurableProviderAssistantReplay`；
- composite candidate fingerprint。

fragment public projection必须与assistant blocks exact compare。不能让repository重新解析provider payload或让adapter直接写库。

### 8.2 单事务

`commit_assistant_message()`扩展为：

```text
lock exact RUNNING turn/binding
insert assistant transcript entry once, already containing final:
    provider wire API
    replay disposition
    replay pointer (pre-generated ID for NATIVE_REPLAY; NULL otherwise)
insert ordered assistant blocks
if disposition == NATIVE_REPLAY:
    insert replay row
insert existing assistant/subagent/turn events
commit deferred exact FKs
```

`transcript_entries`的CHECK是immediate，因此`NATIVE_REPLAY` entry第一次INSERT时pointer就必须非空；deferred cyclic FK只延迟跨表存在性检查，不延迟该CHECK。生产writer继续只有`SELECT | INSERT`，不得为本文授予`UPDATE transcript_entries`，也不得通过“先插空pointer、再UPDATE”破坏entry immutability。replay row可在entry之后INSERT，最终由deferred双向FK在commit时证明composite完整；任一步失败都会回滚两者。

replay row没有独立CommittedEvent。它是assistant acceptance的subordinate physical representation，与`assistant_message_blocks`相同，不拥有单独业务occurrence。

### 8.3 FULL | NONE | CONFLICT

`confirm_assistant_message_winner()`必须把optional replay纳入同一confirmation：

```text
NONE
    no assistant row
    no replay row

FULL
    exact assistant row
    exact blocks/event/terminal disposition
    exact wire API + replay disposition
    PUBLIC_SEMANTIC_ONLY with exact replay absence
    OR NATIVE_REPLAY with exact replay row

CONFLICT
    assistant identity differs
    OR blocks/event differ
    OR expected replay missing
    OR unexpected replay exists
    OR wire API/disposition differs
    OR replay metadata/body/fingerprint differs
```

不得把“assistant存在、replay缺失”确认成FULL，也不得另发第二个repair write。

### 8.4 process-local install

当前Host仍在canonical mutation前reserve process-local replay capacity，以保证它自己可以继续当前tool loop。FULL以后promote同一个candidate进入continuity owner。

关键变化是：

- crash发生在commit与promote之间时，durable row已经完整；
- 当前Host若无法promote，必须cold-discard该scope并从durable composite重新读取；
- 下一Host无需知道旧reservation/token；
- reservation不是durable owner，也不写表。

tool authorize/attempt/invoke仍必须等待assistant composite FULL；不允许仅因row durable就绕过5A.1 settlement顺序。

---

## 9. Cold read与provider wire reconstruction

### 9.1 one-cut read

`CanonicalProviderInputReader`增加一个dispatch bundle入口，在同一REPEATABLE READ transaction中：

1. 读取exact `PreparedProviderInputCut`；
2. 读取现有canonical compile snapshot；
3. 读取同scope、effective source floor之后、cut sequence以内assistant entries的wire API、replay disposition、pointer与关联replay row的metadata columns；
4. 验证disposition/pointer/manifest cyclic join及metadata shape，但不读取`payload_bytes`、不parse private JSON，也不把`payload_size`计入尚未发生的body hydration；
5. 冻结`FrozenCanonicalProviderDispatchRead`。

cut以后的assistant/replay对本次读取不可见，不是corruption。旧snapshot floor以前的replay row也不进入本次active read。

manifest query必须有与canonical item cut相同的entry数量/deadline/metadata-byte bounds。它可以在metadata阶段发现pointer与row identity的结构冲突；body digest、canonical JSON与public projection只在该manifest真正被本次wire plan选择并通过target gate以后验证。由此，五个最终未入选的16 MiB body不会提前占用80 MiB，也不会让current target根本不会消费的私有正文成为semantic cold continuation的前置条件。

### 9.2 compiler与wire planner分工

```text
canonical semantic snapshot + resolved target/compile binding
    -> normal compiler budget/variant selection
    -> final semantic messages + placements

durable replay manifest cut
    -> select only assistant entries actually present in final messages
    -> derive current replay-target fingerprint
    -> classify selected manifests as compatible native | semantic-only/incompatible
    -> freeze exact selected assistant-placement fingerprint
    -> hydrate only compatible native exact IDs under aggregate bound
    -> validate body/digest/JSON/public projection
    -> freeze cut/scope/target/placement-bound hydration fact
    -> replace generic assistant group with native carrier
    -> FrozenProviderWireInputPlan
```

未被final semantic input选择的old replay row继续留在数据库，但不强行注入、不造成unused-fragment conflict。cold epoch可以按当前预算选择新前缀；一旦该epoch安装，Round 3.1继续保证其actual wire prefix只追加suffix。

若selected manifest与current replay target不兼容，该entry使用public semantic lowering并形成显式cold semantic continuation；旧body不hydrate、不翻译。若selected manifest兼容且disposition为`NATIVE_REPLAY`，body缺失、overbound或校验失败才是本次provider open前的typed corruption/resource boundary。不得因为一次compatible hydrate失败而退回generic semantic wire。

### 9.3 exact target gate

一个durable fragment只有全部匹配才可用：

```text
scope
assistant entry
public projection
wire API
codec
provider replay contract fingerprint
replay target fingerprint
selected assistant placement fingerprint
```

same-schema physical reconnect可以更换client/socket，但上述semantic target identity不变。`replay_target_fingerprint`必须来自§6.1的closed `ProviderReplayTargetCompatibilityFact`，不能使用完整resolved-target或open-world request-shape fingerprint；仅调整purpose、`tool_choice`、sampling/output controls、stream/usage、context budget、local estimator、watchdog或role不会撤销合法carrier。任何真正的replay compatibility不匹配都禁止把carrier放入wire plan，并走§1.3的cold semantic continuation。

### 9.4 Chat与Responses materialization

Chat：

- final semantic input必须有exact one-message assistant group；
- replay row替换该group；
- subsequent tool result/user/runtime messages按normal lowering追加。

Responses：

- replay row的ordered output items替换对应generic assistant group；
- 不重复function call；
- 不用`previous_response_id`；
- retained `message/function_call` public projection必须等于canonical blocks。

### 9.5 compaction

Round 5B adoption是cold epoch boundary：

- summary call必须复用本文的manifest selection、replay-target gate、selected-body hydration与现有`FrozenProviderWireInputPlan` materialization，使用当前Host已安装或重启后重建的exact old-prefix replay；不得把`materialized_messages()`直接当作最终physical request；
- adoption后，snapshot floor以前的replay rows不再active materialize；
- rows不在5A.2中物理删除；当前产品没有session purge API或对应DELETE privilege，本文只承诺经过validated local clean-v0 reset整库清理；
- retained post-floor assistant entries若仍在new semantic input，可继续使用自己的durable replay；
- summary正文绝不复制hidden carrier。

未来若产品增加session deletion，必须在该独立能力中冻结child-first delete顺序、writer privilege、concurrent read/settlement fence及验证；当前`ON DELETE RESTRICT`是诚实边界，不得在5A.2中含糊声称已有级联删除生命周期。

---

## 10. Bounds、privacy与failure policy

### 10.1 bounds

```text
single payload canonical JSON bytes <= 16 MiB
Chat top-level ordered items == 1
Responses top-level ordered items <= 4,096
Chat nested reasoning fragments/array elements <= 65,536
decoded JSON nodes <= 65,536; depth <= 128; single string <= 16 MiB
canonical compile snapshot bytes + selected hydrated replay payload bytes <= 64 MiB per dispatch
Host installed/prepared resident bound = existing continuity hard bound
```

manifest read与selected-body hydration都必须在累积过程中执行各自working-set fence，不能先无限物化再做quote。64 MiB composite只计算canonical compile snapshot、manifest metadata与**本次最终选择且target-compatible**的hydrated payload；未选中或不兼容body保持未读。selected aggregate overbound得到typed provider-input resource boundary，provider open为0；不得静默遗漏某个selected required carrier。

这些是physical safety bounds，不是普通model product quota。Round 5B未来应在接近上界前主动compact，但5A.2不实现compaction。

### 10.2 日志与展示

生产日志、异常、evidence与TUI最多记录：

- codec；
- item count；
- byte count；
- fingerprint；
- FULL/NONE/CONFLICT；
- typed failure kind。

禁止记录：

- payload body；
- reasoning text/summary；
- encrypted content；
- 完整provider output；
- credentials/headers；
- full prompt。

所有持有private正文的dataclass/exception carrier，包括5A.1的`ProviderAdapterCompletedReplayPayload`、durable writer candidate、selected hydration与wire replacement body，都必须把正文成员声明为`field(repr=False)`。仅仅约束业务logger不够：architecture test必须把private sentinel放入每一层carrier并证明`repr()`、validation exception、structured log与activation evidence均不出现sentinel。

### 10.3 corruption

下列情况在相关manifest被本次final semantic input选择且target-compatible、因而需要native replay时，是typed canonical corruption/invariant failure，不是provider retry：

- pointer有值但row缺失；
- replay row存在但entry未反向引用；
- disposition、pointer与wire API matrix不成立；
- digest/size/fingerprint不匹配；
- public projection不匹配；
- codec与wire API不匹配；
- Responses expected row缺失。

metadata cyclic identity本身在RR manifest read时仍必须结构合法。private body则只在selected hydration时校验；未选中或target不兼容body不因本次调用被读取或全库审计。不得自动删除row、补造placeholder、重跑旧provider response或从event/log修复。

---

## 11. Failure matrix

| 场景 | assistant | replay row | 下一步 |
|---|---:|---:|---|
| Chat completed，无known carrier | FULL + PUBLIC_SEMANTIC_ONLY | none | canonical semantic resume |
| Chat completed，有actual known carrier | FULL | exact | same-target native resume |
| Chat carrier过界 | 0 | 0 | typed payload limit |
| Responses completed、allowlist exact | FULL | exact required | native resume |
| Responses unknown/effect-bearing item | 0 | 0 | provider contract failure |
| OUTPUT_INCOMPLETE | 0 | 0 | existing interruption path |
| provider/transport failure | 0 | 0 | existing retry/failure policy |
| caller cancel before DB commit | NONE or later confirmation | same as assistant | shielded owner settles |
| ACK unknown，both rows exact | FULL | exact | promote/rehydrate |
| assistant exact、expected replay missing | CONFLICT | missing | stop; no tool effect |
| process dies before transaction commit | 0 | 0 | reopen prior prefix |
| process dies after commit | FULL + exact disposition | exact/none | next Host cold-read |
| same-target physical reconnect | existing | existing | rebind transport only |
| target/API/profile incompatible | existing | retained but unused | explicit cold semantic epoch |
| selected + target-compatible durable payload corrupt | existing | corrupt | typed invariant failure/open=0 |
| unselected或target-incompatible private body损坏 | existing | retained but unread | 本次semantic continuation不做body审计 |
| compaction removes old entry from active base | existing historical | retained historical | not materialized |

---

## 12. Implementation slices

### R5A.2-0：contract cleanup

- upgrade replay fragment fingerprint/domain；
- canonical whole-array payload quote；
- remove replay retention dependence on`ThinkingReplayPolicy`；
- keep only Chat/Responses codec branch；
- update 5A.1 supersession wording。

### R5A.2-1：schema与repository

- add relation and entry pointer；
- update manifest/privileges；`transcript_entries`仍只有SELECT/INSERT，replay relation只有assistant writer INSERT与narrow reader SELECT；
- central stable replay candidate builder；
- pre-generate replay ID，entry首次INSERT即携带final disposition/pointer，再插replay row；禁止entry UPDATE；
- exact composite confirmation。

### R5A.2-2：reader与cold hydration

- one-cut semantic + metadata-only manifest read；
- final-message selection与窄replay-target gate；
- selected exact-session/cut/scope/placement bounded payload hydration/decode；
- selected hydration复用本次唯一dispatch-planning absolute deadline，不重新取120秒；
- cold wire overlay planning；
- exact replay-target/contract/public-projection gate。

### R5A.2-3：settlement与restart

- preserve precommit capacity reservation；
- FULL promote or cold rehydrate；
- crash-after-commit integration；
- Host close/takeover regression。

### R5A.2-4：docs与evidence

- update Gap Index、README与Round 5B dependency；
- add activation evidence without bodies；
- update closed oracle；
- remove obsolete architecture gate that forbids repository replay rows。

---

## 13. Test plan

### 13.1 deterministic codec tests

Chat：

1. each known field independently；
2. all known fields together；
3. tool response and final-text response；
4. no known field -> no row；
5. empty/null -> no synthetic field；
6. contraction/chunk accumulation remains exact；
7. payload boundary-1/boundary/boundary+1；
8. canonical encode/decode roundtrip；
9. no placeholder text；
10. no vendor-name branch。
11. durable top-level item count固定为1，nested fragment 65,536与JSON structural bounds独立验证；
12. every private-body carrier repr omits sentinel。

Responses：

1. reasoning + message；
2. reasoning + one/multiple function calls；
3. message-before-function-call order；
4. encrypted content roundtrip；
5. unknown item reject；
6. canonical encode/decode roundtrip；
7. no response ID dependency。
8. top-level item 4,096通过、4,097在assistant mutation前拒绝；
9. every private-body carrier repr omits sentinel。

### 13.2 PostgreSQL transaction tests

1. assistant + NATIVE_REPLAY row FULL；
2. Chat PUBLIC_SEMANTIC_ONLY + no row FULL；
3. deferred cyclic FK rejects partial insert；
4. immediate CHECK通过的NATIVE_REPLAY entry在first INSERT已经携带pre-generated pointer；
5. production grant不允许UPDATE transcript entry，也不存在pointer UPDATE SQL；
6. ACK-unknown FULL/NONE/CONFLICT；
7. unexpected/missing/mutated row conflict；
8. ROOT与child exact scope；
9. turn completion与subagent occurrence unchanged；
10. rollback leaves neither row；
11. clean-v0 fresh/repeat/deep verify/reset-required。

### 13.3 selected hydration与working-set

1. RR manifest read对`payload_bytes`执行次数为0；
2. compiler选择以后只SELECT selected + replay-compatible IDs；
3. 五个未选中的16 MiB body不计入64 MiB selected hydration；
4. incompatible target不读取旧body并建立cold semantic continuation；
5. selected compatible body missing/digest mismatch/JSON overbound使provider open为0；
6. selected payload aggregate在64 MiB boundary-1/boundary/boundary+1精确结算；
7. manifest、hydrate与wire-plan ordered IDs/fingerprints exact join。
8. 另一个session、ROOT/child scope或cut中的manifest/body即便自身合法也无法构造当前hydration；
9. manifest subset乱序、重复、缺少对应placement或跨assistant group均fail closed；
10. hydration fingerprint对source cut、replay target或selected placement任一变化敏感；
11. initial read耗掉大部分planning budget后，selected hydration只得到remaining time；steer retry/trial不重置deadline；
12. planning deadline在hydration前/中到期时provider open为0、consume steer为0、continuity unchanged。
13. zero compatible native replacement不执行body query、不构造empty hydration，wire-plan hydration fingerprint为`None`；
14. native replacement非空时wire plan、append candidate与continuity CAS逐层绑定同一个hydration fingerprint；换cut、换placement或CAS失败后的旧proof不能复用。

### 13.4 real restart tests

必须使用两个独立Host/process实例和真实PostgreSQL：

```text
Host A
  -> completed response
  -> assistant/replay FULL
  -> process exits without exporting continuity memory

Host B
  -> acquires writer / resumes session
  -> reads same thread
  -> next provider wire contains exact durable carrier
```

分别覆盖Chat与Responses，以及：

- graceful Host close；
- abrupt process kill after DB commit；
- OS-style fresh process with no shared Python object；
- same endpoint/model/replay contract；
- replay-incompatible target does not reuse or hydrate carrier；
- output cap/context budget/estimator-only change keeps replay-target compatible while normal dispatch still forms a cold epoch as required；
- ordinary AGENT response replay target与same endpoint/model/contract的summary `tool_choice=none` replay target完全相等；
- sampling、stream/usage、reasoning effort、output-control或与replay无关的transport-version-only变化不改变replay target；API、endpoint、normalized model、semantic transport binding、codec或replay contract变化必须改变；
- public messages remain exact；
- no duplicate tool call；
- no old remote response ID。

### 13.5 strict-prefix

同一个rehydrated epoch内：

```text
SYSTEM[n+1] == SYSTEM[n]
tools[n+1] == tools[n]
wire_input[n+1] == wire_input[n] || suffix
```

跨Host本身是cold epoch，不要求复用旧epoch nonce；但对同一cut、target与contracts，durable replay replacement必须structure-equal，不能从generic semantic lowering静默退化。

### 13.6 architecture gates

机器证明：

```text
only assistant settlement writes provider_assistant_replay_fragments
assistant and replay share one writer transaction
NATIVE_REPLAY entry first INSERT already contains its final pointer
no UPDATE privilege or UPDATE path for transcript replay fields
confirmation exact-checks optional replay
provider replay relation is absent from artifact/memory/protocol/event surfaces
Chat and Responses are the only replay wire APIs
no vendor name participates in replay selection
no previous_response_id correctness path
no placeholder/synthetic reasoning
no replay payload in logs/evidence/repr
raw EOF/incomplete cannot create durable replay
tool attempt cannot precede assistant composite FULL
cold manifest reader never selects payload_bytes
selected hydration binds exact cut/scope/entry/replay-target/contract
selected hydration repository query always includes exact session_id
selected hydration exact-joins ordered assistant placements
selected hydration reuses one dispatch-planning absolute deadline
manifest-cut fingerprint covers exact session/scope/revision/through-sequence/ordered manifests
native wire replacement iff wire plan carries the exact selected-hydration fingerprint
continuity append candidate/CAS exact-joins the final wire-plan and hydration fingerprints
full resolved target fingerprint is absent from durable replay compatibility
open-world request defaults/extra body are absent from replay target compatibility
no CommittedEvent/LiveEvent/subject/guard/job added
oracle == 31 / 24 / 13 / 2 / 26 / 1
```

### 13.7 real provider dogfood

真实remote dogfood不是correctness gate，但可以覆盖已配置的OpenAI-compatible Chat与Responses endpoints。记录仅限：

- wire API；
- completed terminal；
- carrier present/absent；
- item/byte count；
- process restart happened；
- second request succeeded；
- cache usage字段（若provider返回）。

不得记录API key、DSN、prompt、reasoning、tool arguments、encrypted content、headers或完整response。

---

## 14. Explicit non-goals

本文不实现：

- Anthropic Messages；
- Gemini/Google native protocol；
- vendor preset或provider capability database；
- 用户选择“是否回放思考”的UI；
- online provider probe；
- Chat/Responses carrier互转；
- placeholder thinking；
- partial stream persistence/resume；
- response ID/server-side state authority；
- exact historical HTTP request audit；
- durable compiled-input snapshots；
- tool/Terminal/subagent physical execution recovery；
- compaction；
- replay payload artifact access；
- replay relation GC job；
- session deletion/purge API、DELETE privilege或级联清理契约；当前只允许validated clean-v0 reset；
- encryption key management；
- old database backfill/migration chain；
- event replay/reducer/checkpoint/receipt/repair graph。

---

## 15. Definition of Done

Round 5A.2只有在以下条件全部成立后才能标记`ACTIVATED`：

1. 已接受assistant的optional native replay与assistant在同一transaction原子提交；NATIVE_REPLAY entry首次INSERT已经携带pre-generated final pointer且不存在后续UPDATE；
2. database constraints与confirmation都能拒绝partial composite，并区分PUBLIC_SEMANTIC_ONLY与missing native row；
3. Chat只保存actual observed closed fields，无provider/vendor分支；
4. Responses保存exact ordered allowlisted output items；
5. 关进程/重启机器等价测试能在fresh Host恢复同一线程；
6. same replay-target resume不再generic semantic降级；closed compatibility fact排除purpose、tool_choice、sampling/output controls、stream/usage、budget与estimator，无关变化不撤销carrier；
7. incompatible target不误用或hydrate旧carrier，而是显式cold semantic continuation；
8. partial/incomplete/failed output永不落replay row；
9. replay manifest与body采用两阶段读取，只有final selected + target-compatible body被bounded hydrate；hydration exact绑定session、scope、manifest cut、replay target与selected assistant placements；
10. selected hydration只开启fresh read-only transaction，不创建fresh deadline；normal dispatch贯穿同一`PROVIDER_DISPATCH_PLANNING` absolute deadline；
11. manifest-cut fingerprint机械覆盖exact session/scope/revision/through-sequence/ordered manifests；native replacement、final wire plan、append candidate与continuity CAS exact join同一个hydration proof，零replacement不创建empty hydration；
12. replay不进入public/canonical semantic body、artifact、memory、Protocol、event、日志、repr或evidence；
13. strict-prefix与Round 5A.1 whole-response atomicity回归全部通过；
14. PostgreSQL、全量pytest、Ruff、compileall、Protocol、Go与schema验证通过；
15. oracle为`31 / 24 / 13 / 2 / 26 / 1`；
16. activation evidence不包含任何carrier正文；
17. Round 5B明确复用同一closed replay-target fact、selected hydration与`FrozenProviderWireInputPlan`发送summary actual wire prefix；summary `tool_choice=none`不改变replay compatibility，也不从generic `materialized_messages()`直接open。

完成后，Pulsara的边界将是：

> live generation仍可丢失；已完成并canonical接受的线程历史不可因进程、操作系统或电源边界失去其必要provider-native replay语义。
