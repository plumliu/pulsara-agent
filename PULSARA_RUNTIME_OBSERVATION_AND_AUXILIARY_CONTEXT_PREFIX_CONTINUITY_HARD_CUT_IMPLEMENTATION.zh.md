# Pulsara Human/Runtime User Carrier、Runtime Observation 与 Auxiliary Context Prefix Continuity P1 Hard Cut 实施规格

> 状态：供 reviewer 反向审阅的实施规格草案；完成 review 前，不得继续宣称 DeepSeek provider token-prefix continuity 已闭环。
>
> 记录日期：2026-07-20。
>
> 本文是以下规格的纠偏与收口层：
>
> - `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`
> - `PULSARA_PROVIDER_INPUT_CAUSAL_ORDER_AND_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md`
> - `PULSARA_PROMPT_CACHE_CONTRACT.zh.md`
>
> 凡涉及 human-input/runtime-request/runtime-observation user carrier、mid-history `system` / `developer` hint、runtime clock、memory/capability complete snapshot、lifecycle note、`auxiliary_frame_rebase` 与 provider token-prefix continuity 的条款，以本文为准。

---

## 0. 执行结论

真实 long PR4 dogfood 与随后三个隔离实验已经证明：

1. Pulsara 在 OpenAI Chat API 对象层能够保持同 generation 的 `messages` 严格追加；
2. 但 API 数组位置不等于 provider 最终 token-template 位置；
3. DeepSeek对 mid-history `system` message的实际cache行为等价于privileged-channel normalization/重排；具体内部template机制仍是推断；
4. 因此尾部追加一条 runtime clock `system` message，会让此前稳定 transcript 无法作为 token prefix复用；
5. 将完全相同的 clock 内容改成尾部 `user` message 后，DeepSeek 立即恢复对完整旧前缀的缓存命中；
6. `auxiliary_frame_rebase` 又会主动删除旧 ContextSource units、重建 generation，并清空本来可以继续命中的 provider prefix；
7. memory 当前甚至会因为新的 projection event ID，在 model-visible 正文逐字相同时重复追加完整 snapshot；
8. capability prose在变化时同样追加完整 snapshot，但真正的 tool schema变化仍必须 rollover；
9. lifecycle note 已经接近正确的一次性 causal fact，但目前仍以 `system` semantic/wire role进入 history。

立即冻结以下 hard cut：

1. 每次 provider call 只能有一个稳定的 root policy carrier；Chat API 中是最前面的 root `system` message；
2. ordered history 中禁止任何动态 `system` 或 `developer` hint；
3. Provider user-wire carrier是三分union：human-authored输入使用 `pulsara_human_input`，runtime创建的当前任务/请求使用 `pulsara_runtime_request`，runtime背景事实/指导使用 `pulsara_runtime_observation`；三者都使用 `user` wire role，但内部ownership、lifecycle与typed outer envelope严格互斥；
4. runtime clock继续允许每次 model invocation追加，但改为紧凑的 append-once causal observation；
5. memory不实施 delta V1；只与前一个 committed effective semantic snapshot比较：相同 no-op，不同则追加完整 replacement snapshot；
6. capability prose遵循相同 semantic no-op规则；真正 tool catalog schema变化必须显式 rollover；
7. lifecycle observation继续作为 source-event-owned、append-once causal fact，但 wire role改为 `user`；
8. `AUXILIARY_FRAME_REBASE` 从正常生产 rollover reason中物理删除；
9. source消失必须由 typed empty/revocation/terminal fact表示，禁止用“本轮没选中”触发 generation重建；
10. 只有 system root/tool catalog/provider compatibility改变、confirmed Long-Horizon rewrite、confirmed repair或administrative reset可以重建 generation；
11. 真正 context pressure必须先取得 Long-Horizon rewrite authority，不能伪装成 auxiliary cleanup；
12. prefix continuity必须在 adapter-final wire projection层验证，不能只比较 provider-neutral `LLMMessage` 或 API `messages` tuple；
13. runtime observation必须由中央protocol穷尽声明kind、trust class、lifecycle、typed payload、codec与escaping；
14. event ID/sequence/artifact placement只属于attribution，wire只使用可从semantic content/lineage重算的 `observation_semantic_id`；
15. replacement source head必须保留可hydrate的exact committed unit，使rollover不依赖当前source重新渲染；
16. 删除standalone rebase后，runtime observations必须进入有typed proof/protection matrix的Long-Horizon rewrite domain，不得形成无界第三条历史。
17. Runtime Observation kind不得再与 `ContextSourceId` 强制一对一；registry必须以producer-aware union表达ContextSource transition、transcript-derived lifecycle与Long-Horizon rewrite三类producer；
18. observation rewrite的protected/eligible/retained/rewritten集合必须由唯一reducer冻结，并以bounded paged/Merkle proof证明完整分区；planner不得自行声明保护集合；
19. rewrite replacement必须携带generation-neutral causal placement，并与transcript compaction、tool pairing和current tail经过同一个ordered-provider-projection validator合并；
20. generation semantic core只能保存semantic document identity；inline正文、artifact locator、event refs、authority horizons、append/vector placement与GC ownership全部属于独立attribution state。
21. 每个runtime observation在**首次append**时就必须冻结generation-neutral causal placement；rewrite只能消费已committed placement，禁止按当前transcript重新猜测；
22. rewrite unit semantic只能保存transitive coverage semantic，不得嵌入Merkle page/artifact locator；rewrite fact必须携带可恢复的完整bounded source stable state，而不是孤立hash；
23. 先前rewrite projection允许被后续confirmed Long-Horizon rewrite再次覆盖；coverage lineage必须传递闭合，旧proof/artifact在successor FULL前保持pinned；
24. V1不建立 `RecentWorkingContextSource`：现有 `working_context|mixed` summary prose不得被解析拆分或并入memory snapshot，近期活动由canonical transcript提供。

---

## 1. 问题是如何被发现的

### 1.1 初始信号：DeepSeek cache hit ratio在 7 月 16 日后显著下降

DeepSeek 控制台显示输入 token规模上升，但 cache hit token占比大幅下降。最初可能原因包括：

- provider cache服务故障；
- cache建立需要等待；
- system prompt改变；
- tool schema改变；
- compile timestamp改写历史；
- ProviderInput generation rollover；
- API messages发生重排；
- provider对 role进行二次 lowering。

这个信号最初是成本/性能异常，但后续调查同时暴露了 provider-visible role和因果 placement问题，因此必须按 P1 semantic hard cut处理。

### 1.2 long PR4 的真实 production payload捕获

在不改变 Agent/Host/LLMRuntime生产路径的前提下，于 OpenAI Chat adapter最终 dispatch边界捕获 long PR4 的实际请求 payload。捕获内容包括：

- `model`；
- ordered `messages`；
- ordered `tools`；
- `stream_options`；
- output budget；
- provider extension body；
- provider报告的 input/cached/output usage；
- provider-input generation ID/revision。

捕获不包含 API key、HTTP authorization header或其他 credential。

对同 generation的相邻调用验证：

```text
old.messages == new.messages[:len(old.messages)]
old.tools == new.tools
old.non_message_payload == new.non_message_payload
```

除显式 rollover边界外，以上 API-object-level strict-prefix均成立。

### 1.3 捕获中暴露的 mid-history system messages

真实 Call 1 的 message roles是：

```text
system
user(runtime context)
user(capability catalog)
user(memory snapshot)
system(runtime clock #1)
user(actual user request)
```

真实 Call 2 在 Call 1完整前缀后追加：

```text
assistant(tool calls)
tool
tool
user(memory revision)
system(runtime clock #2)
```

因此 Pulsara确实把新的 clock放在 API `messages` 尾部。但 DeepSeek只继续报告约 640 cached tokens，没有复用 Call 1的大部分输入。

### 1.4 排除“cache没有建立”

曾先做真实 payload exact replay：

```text
send payload A
wait 30 seconds
send byte/semantic-identical payload A
```

第二次请求得到接近 99% 的 cache hit。随后使用独立 namespace重新做隔离实验，结果仍然一致。

因此可以排除：

- DeepSeek不支持该 payload形状；
- tools或thinking设置天然不可缓存；
- cache必须等待更长时间；
- Pulsara usage normalizer错误地把 hit报告为 0。

### 1.5 三组隔离实验

实验使用真实 PR4 Call 1 / Call 2 adapter-final payload。每个实验组执行：

```text
1. 在 root system最前部注入该组独有的固定 namespace nonce；
2. 发送 warm payload；
3. 等待 30 秒；
4. 发送 test payload；
5. 记录 DeepSeek prompt_cache_hit_tokens / miss_tokens。
```

nonce位于 root开头，保证三个实验组不会互相复用此前缓存；同组 warm/test使用完全相同 nonce。

按讨论决定，不再重复运行完整原始 long PR4。原始 production轨迹已经提供 `system clock` 分支；新增三个受控分支如下：

| 分组 | Warm input | Test input | Test cached | Test ratio |
|---|---:|---:|---:|---:|
| Exact replay Call 1 | 6,972 | 6,972 | 6,912 | 99.14% |
| Call 2移除新增 clock | 6,973 | 7,630 | 6,912 | 90.59% |
| Call 2将新增 clock改为 `user` | 6,971 | 7,808 | 6,912 | 88.52% |

三个 warm请求均报告 0 hit，证明 namespace隔离生效。

`6,912` 是 64-token cache block的整数倍，也是三个分组都完整复用的旧 cacheable prefix。后两组 ratio低于 exact replay，只因为 test请求合法追加了新的 uncached suffix；它们没有丢失旧 prefix。

真实 production Call 2保留新增 clock为 `system` 时：

```text
input_tokens  = 7,722
cached_tokens =   640
```

将完全相同 clock正文仅改变 wire role为 `user` 后：

```text
input_tokens  = 7,808
cached_tokens = 6,912
```

因此问题不是 clock正文、等待时间、tools、模型或非 message参数，而是 mid-history privileged role。

### 1.6 可以证明什么，不能证明什么

实验能够证明：

- API-object-level strict append不足以保证 provider token-prefix continuity；
- DeepSeek对 mid-history `system` role的处理会破坏此前 transcript cache identity；
- 相同内容改为尾部 `user` carrier后，旧 cacheable prefix完整恢复；
- exact replay可以稳定命中，因此 provider cache本身正常。

实验不能直接读取 DeepSeek内部 tokenizer template，因此本文把以下描述标记为 source-backed inference：

```text
DeepSeek很可能将所有 system内容合并/提升进 privileged prefix，
或使用具有相同 cache效果的 role-partitioned lowering。
```

无论内部实现是“提升”“合并”还是“role partition”，产品结论相同：mid-history `system` 不是可移植、可缓存、可审计的时序 carrier。

### 1.7 `auxiliary_frame_rebase` 的真实代价

同一次 PR4 中，Call 7 -> Call 8发生 `auxiliary_frame_rebase`：

| | Call 7 | Call 8 |
|---|---:|---:|
| Message count | 29 | 23 |
| Message bytes | 36,630 | 21,843 |
| Input tokens | 14,781 | 11,108 |
| Cached tokens | 1,280 | 768 |
| Provider latency | 2.73s | 3.09s |

它确实删除了约 3,673 input tokens，但同时重建了剩余约 11K-token输入。当前 system-role bug使两个调用本来就只有很低 cache hit，因此尚且掩盖了 rebase的完整损失；在 role修复后，rebase会把原本可缓存的大前缀重新变成 miss。

单次 latency样本不能作为统计 gate，但足以否定“更短 payload必然更快”的未经测量假设。

---

## 2. 当前生产机制与根因

### 2.1 Runtime observation carrier错误使用 privileged role

当前 provider-neutral内部已有：

```text
MessageRole.RUNTIME_OBSERVATION
```

但 binding规则是：

```text
openai_chat_completions -> system
openai_responses        -> developer
```

Chat adapter于是把 runtime clock等 observation编码为 `role="system"`。Responses adapter则编码为 `role="developer"`。

这错误地把“runtime拥有、不是用户原话”解释成“必须使用 privileged provider role”。Ownership与wire authority是两个不同维度：

```text
runtime-owned != provider-system-role
```

### 2.2 Clock被建模成 complete snapshot revision

当前每次 compile都会创建新的 clock proposal：

```text
observed_at_utc = compiled_at_utc
candidate_key   = exact timestamp
lifecycle       = append_revision
continuity      = complete_snapshot
wire role       = system (Chat)
```

每条正文约 134 bytes，但 provider-visible revision envelope使完整 message约 568 bytes。问题不在于时间本身，而在于：

- 每次都被标记为“旧 clock失效、latest wins”的 snapshot；
- 使用 privileged role；
- 携带远大于正文的通用 revision wrapper；
- 为后续 auxiliary rebase制造 droppable frame。

### 2.3 Memory把 projection event identity误当成 provider semantic change

当前 memory source使用：

```text
lifecycle       = append_revision
continuity      = complete_snapshot
revision_id     = ProjectionReadyEvent.id
selection_rule  = latest revision wins
```

因此即使 model-visible正文完全相同，只要新建了 `ProjectionReadyEvent`，就会生成新的 source revision并完整追加。

真实 PR4 已确认：

```text
Call 1: empty recalled-memory body, revision A, about 709 bytes
Call 2: byte-identical body,       revision B, about 709 bytes
```

两条 message唯一差异是随机/事件型 revision ID。

后续 working-context snapshot也多次出现约 1.6 KiB正文完全相同、只改变 revision ID的重复追加。

`latest_revision_wins` 只是写给模型看的自然语言/JSON声明；provider不会真的删除旧 snapshot，也不会免除其 token成本。

### 2.4 Capability只在语义变化时追加，但追加的是完整 snapshot

Capability prose包括：

- capability/skill catalog说明；
- active skill正文；
- provider `tools`之外的模型可见路由信息。

它们同样使用 `append_revision + complete_snapshot`。与 memory相比，capability semantic fingerprint没变时通常能 no-op；但变化时追加整份 catalog或active skill，不是 delta。

真正 provider tool schema单独存在于 `tools`。当 MCP异步连接后工具从 26变成 29，DeepSeek内部 tool prompt也会改变，因此必须 rollover。该 rollover通常发生在 session早期，属于可接受的真实 compatibility变化。

### 2.5 Lifecycle note已经是 causal fact，但 role错误

当前 transcript reducer对以下 durable event生成稳定 leaf：

- `RunEndEvent(status != finished)` -> recovery/interrupted/teardown note；
- `TerminalProcessCompletedEvent` -> terminal lifecycle note。

其优点已经成立：

- 一个 source event只生成一次；
- message ID由 source event ID确定；
- exact event refs进入 causal attribution；
- 后续 compile只复用，不会每轮重建。

但 provider semantic仍是：

```text
role = system
name = pulsara
```

因此它会遭遇与 clock相同的 provider privileged-role重排风险。

### 2.6 `auxiliary_frame_rebase` 是补偿错误，而非根因修复

当前 planner发现某个 previously committed dynamic source本轮未被 selected时，会触发：

```text
AUXILIARY_FRAME_REBASE
```

它保留 canonical transcript，删除旧 context source/runtime clock units，使用当前 selected sources重建 generation。

这形成了错误循环：

```text
用 event identity制造重复 snapshot
-> 旧 snapshots累积
-> auxiliary rebase清理
-> provider cache失效
-> 再次累积
-> 再次清理
```

它清理了 provider payload，却没有修复 source revision identity和snapshot频率；因此在缓存系统中很容易适得其反。

### 2.7 当前 system-role producer审计

实施前必须逐项处理，不允许只修改 clock adapter：

| 当前 producer | 当前行为 | V2决定 |
|---|---|---|
| human-authored user message lowering | raw正文直接作为 `role=user` content | 统一编码为 `pulsara_human_input` typed outer envelope；用户正文只能位于escaped `text`字段 |
| subagent task/current-run task | runtime构造，但通常复用普通user message | 独立 `MessageRole.RUNTIME_REQUEST` + `subagent_task/current_run_task` carrier；不得产生human attribution |
| compaction/governance/reflection/summarizer one-shot input | runtime构造，但通常复用普通user message | 独立one-shot runtime-request kind/owner；不进入observation stable state/rewrite |
| `llm/runtime_observation.py` | Chat绑定 `system`，Responses绑定 `developer` | 两者统一绑定 `user` |
| `llm/adapters/openai/chat_completions.py` | `RUNTIME_OBSERVATION -> system` | `RUNTIME_OBSERVATION -> user` |
| `llm/adapters/openai/responses.py` | `RUNTIME_OBSERVATION -> developer` | `RUNTIME_OBSERVATION -> user` |
| memory/capability/skill/plan/recovery/rollout/subagent/MCP ContextSource lowering | 普通 `user` + legacy `pulsara_context` / generic wrapper | 全部迁移为中央registered runtime-observation kind与canonical outer envelope |
| `authority_materialization/transcript_reducer.py::_append_run_recovery_note` | canonical lifecycle semantic role为 `system` | internal runtime observation，wire `user` |
| `authority_materialization/transcript_reducer.py::_append_terminal_completion_note` | canonical lifecycle semantic role为 `system` | internal runtime observation，wire `user` |
| `context_input/transcript.py` legacy lifecycle helpers | 重建 `system` lifecycle note | 退出生产并删除 |
| `context_input/compiler.py` generation-root source lowering | 生成稳定 root system fragment | 保留，但只能进入 generation root |
| compaction/governance/reflection one-shot prompt | 调用 `LLMMessage.system()`作为该 one-shot的根指令 | 允许，但应收敛到唯一 `LLMContext.system_prompt` root，不得进入 ordered history |
| tests直接构造 mid-history `LLMMessage.system()` | 模拟旧恢复路径 | 迁移为 typed runtime observation fixture |

Architecture guard必须区分：

```text
allowed:
    one stable root system/instruction carrier
    one-shot model call的唯一 root instruction

forbidden:
    ordered history中的任意 dynamic system/developer message
    raw human正文作为顶层user content
    未注册的legacy runtime-owned user wrapper
```

---

## 3. 中央不变量

### 3.1 Root privileged carrier唯一性

每个 provider call只允许一个 privileged root policy：

```text
Chat Completions:
    messages[0] = one stable root system message

Responses:
    one stable root instruction/developer policy owned by generation root
```

允许进入 root的内容仅包括：

- stable product/system instruction；
- generation-root environment policy；
- provider compatibility要求的固定 instruction；
- 在 generation开始前已经冻结、变化会触发 rollover的内容。

### 3.2 Mid-history privileged role禁令

以下内容一律不得在 ordered history中编码为 `system` 或 `developer`：

- runtime clock；
- run interrupted/failed/teardown note；
- terminal process lifecycle note；
- recovery hint；
- memory snapshot；
- capability prose revision；
- active skill content；
- plan/status hint；
- context compaction observation；
- subagent/handoff runtime note；
- permission/capability status observation；
- 任何在第一次 model call之后才产生的 runtime-owned文本。

唯一例外是 provider API自身的非 message root instruction字段；它必须属于 generation root，不能出现在历史中间。

### 3.3 Internal ownership、human attribution与wire role分离

Runtime observation不得伪装成人类原话。内部必须保留：

```text
MessageRole.RUNTIME_OBSERVATION
source_kind
source_event_refs
runtime attribution
```

Runtime创建的“本次任务/请求”同样不得伪装成人类原话，也不能伪装成可被observation rewrite处理的背景事实。内部新增独立身份：

```text
MessageRole.RUNTIME_REQUEST
request_kind
request_owner
request lifecycle
runtime-request attribution
```

只有 adapter-final lowering使用：

```json
{
  "role": "user",
  "content": "{\"pulsara_runtime_observation\":{...canonical JSON...}}"
}
```

`content` 由第 4 节 central codec生成，不允许producer自行拼接XML/tag/header。

Canonical user transcript查询必须能区分：

- human-authored user message；
- runtime-owned observation using user wire carrier。

这种区分必须一直延伸到adapter-final bytes，而不能只存在于Pulsara内部对象。所有human-authored user message必须由中央factory编码为：

```text
role=user
content=canonical_json({"pulsara_human_input": {...typed text...}})
```

所有runtime-owned observation必须编码为结构互斥的：

```text
role=user
content=canonical_json({"pulsara_runtime_observation": {...typed fact/guidance...}})
```

所有runtime-authored current task/request必须编码为第三种结构：

```text
role=user
content=canonical_json({"pulsara_runtime_request": {...typed task/request...}})
```

顶层outer key、protocol version、typed payload和完整canonical bytes都进入provider wire semantic fingerprint。用户提交与任一runtime envelope逐字相同的JSON时，该文本仍只能作为escaped `pulsara_human_input.text`存在；它不能成为顶层runtime carrier。

三者语义边界冻结为：

```text
human_input:
    真实human-authored request/content

runtime_request:
    runtime创建、要求模型在本次调用或child run中执行的当前任务

runtime_observation:
    runtime提供的背景事实、状态或root-policy授权的bounded guidance
```

### 3.4 Same-generation token-prefix continuity

同 generation的 adapter-final input必须满足：

```text
wire_input[n+1] = wire_input[n] || append_only_suffix
```

这里的 `wire_input` 是 provider adapter canonical lowering后的逻辑 token-template projection，不只是 Python `LLMContext` equality。

对隐式 cache provider，必须至少验证：

- root policy完全相同；
- tools完全相同；
- old ordered wire messages是 new messages严格前缀；
- 旧 message的 resolved wire role和正文完全相同；
- 同generation新增request/observation unit只能形成append-only suffix，其causal placement不得要求插入或改写既有wire unit；
- provider extension中不存在会改写历史的 remote continuation/state。

### 3.5 Absence不是删除 authority

本轮 source没有被 selected，不能解释为：

- 删除旧 provider unit；
- auxiliary rebase；
- source自动失效；
- generation rollover。

需要失效的 source必须产生 typed explicit transition：

- empty replacement snapshot；
- terminal/closed fact；
- confirmed rewrite authority。

### 3.6 Cache不是正确性真源

所有请求仍发送完整本地 materialized context。Cache miss不得改变：

- semantic selection；
- token budget；
- retry；
- model target；
- compaction decision；
- tool execution；
- replay结果。

---

## 4. Human / Runtime Request / Runtime Observation User Carrier Protocol

### 4.1 Event attribution不得进入 wire semantic identity

Durable source event reference只属于 attribution。Provider wire不得出现 raw `event_id`、ledger sequence或artifact placement。

所有 observation wire message使用稳定的：

```text
observation_semantic_id
```

它由 semantic内容和lifecycle lineage确定性派生：

```text
replacement snapshot:
    H(
        runtime-observation-semantic-id:v3,
        kind,
        source_instance_id,
        payload_semantic_fingerprint,
        predecessor_observation_semantic_id | genesis,
    )

causal append-once observation:
    H(
        runtime-observation-semantic-id:v3,
        kind,
        source_instance_id,
        causal_occurrence_semantic_fingerprint,
    )
```

同一 replacement snapshot由不同事件重新确认时，若 effective head内容相同，不生成新 observation，也不生成新 wire bytes。`A -> B -> A` 则因为 predecessor lineage不同，最后一个 A是新的 replacement observation。

Clock、lifecycle等本来就表示不同 causal occurrence的 source，可以为每次真实 occurrence生成不同 semantic ID；wire仍不暴露 raw event ID。

`source_instance_id` 必须由第 9 节 lifecycle registry指定的scope与canonical business identity确定性派生；不得使用event ID、artifact ID、随机UUID、compile ID或process-local counter。

Provider unit semantic fingerprint必须覆盖 canonical encoder生成的**全部最终 wire bytes**，包括 observation semantic ID、kind、authority class、lifecycle和payload。不得从 fingerprint中排除 wire可见字段。

#### 4.1.1 首次append即冻结generation-neutral causal placement

Generation-neutral placement不是rewrite阶段才补出的metadata。每条clock、memory replacement、skill、lifecycle及其他runtime observation在第一次进入provider projection时，就必须由唯一factory同时生成以下三层：

```python
class RuntimeObservationWireSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_wire_semantic.v2"]
    protocol_version: Literal["2"]
    observation_kind: str
    observation_semantic_id: Fingerprint
    source_instance_id: str
    authority_class: Literal[
        "runtime_fact",
        "runtime_guidance",
        "runtime_fact_and_guidance",
    ]
    lifecycle_class: Literal[
        "immutable_append_once",
        "causal_append_once",
        "replacement_snapshot",
    ]
    payload: RuntimeObservationPayloadFact
    payload_schema_version: str
    payload_schema_fingerprint: Fingerprint
    payload_semantic_fingerprint: Fingerprint
    canonical_wire_utf8_sha256: Fingerprint
    canonical_wire_utf8_bytes: NonNegativeInt
    wire_semantic_fingerprint: Fingerprint


class RuntimeObservationCausalPlacementSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_causal_placement_semantic.v1"]
    causal_scope_kind: Literal[
        "runtime_session",
        "run",
        "model_invocation",
        "workflow",
        "subagent",
        "operation",
    ]
    causal_scope_semantic_id: Fingerprint
    placement_phase: Literal[
        "before_model_call",
        "after_model_call",
        "after_tool_result",
        "after_run_terminal",
        "status_at_frontier",
    ]
    stable_predecessor_transcript_node: ProviderTranscriptNodeIdentityFact | None
    source_occurrence_semantic_fingerprint: Fingerprint
    intra_boundary_order: NonNegativeInt
    placement_contract_fingerprint: Fingerprint
    placement_semantic_fingerprint: Fingerprint


class RuntimeObservationSourceAttributionFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_source_attribution.v3"]
    observation_semantic_fingerprint: Fingerprint
    producer: RuntimeObservationProducerFact
    transition_kind: ObservationTransitionKind | None
    protection_scope_kind: Literal[
        "runtime_session", "run", "workflow", "subagent", "operation"
    ]
    protection_scope_semantic_id: Fingerprint
    owning_run_protection_scope_semantic_id: Fingerprint | None
    source_event_references: tuple[ContextEventReferenceFact, ...]
    source_artifact_references: tuple[ContextArtifactReferenceFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    attribution_fingerprint: Fingerprint


class PreparedRuntimeObservationProviderUnitFact(FrozenFactBase):
    schema_version: Literal["prepared_runtime_observation_provider_unit.v1"]
    wire_semantic: RuntimeObservationWireSemanticFact
    causal_placement: RuntimeObservationCausalPlacementSemanticFact
    source_attribution: RuntimeObservationSourceAttributionFact
    unit_causal_semantic_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint
```

`unit_causal_semantic_fingerprint` 只覆盖 `wire_semantic.wire_semantic_fingerprint + causal_placement.placement_semantic_fingerprint`；source events、artifacts、horizons和后续append/vector placement只进入outer fact/attribution fingerprint。

这里复用现有 `ProviderTranscriptNodeIdentityFact` 的generation-neutral semantic identity，不使用旧generation vector ordinal、append index或compile-time section。`stable_predecessor_transcript_node=None` 只允许用于明确的transcript-before-first-node边界；其他phase必须携带可由同一ordered transcript projection重算的exact predecessor node。

Prepared append、`ProviderInputAppendCommittedEvent`、provider vector leaf和committed source head必须逐层保存或精确引用同一 `wire_semantic + causal_placement`。FULL fold后，唯一observation stable-state reducer从首次committed placement推进；NONE不推进；UNKNOWN/PARTIAL latch。Rewrite planner只能消费该committed placement，禁止根据当前transcript、旧vector ordinal或当前compile section重新推断。

### 4.2 中央 protocol contract

```python
class ContextSourceObservationProducerFact(FrozenFactBase):
    schema_version: Literal["context_source_observation_producer.v1"]
    producer_kind: Literal["context_source"]
    source_id: ContextSourceId
    transition_kinds: tuple[
        Literal[
            "observation",
            "snapshot_update",
            "explicit_empty",
            "guidance",
            "status_update",
            "terminal",
            "handoff",
            "delivery",
            "diagnostic_update",
        ],
        ...,
    ]
    producer_fingerprint: Fingerprint


class TranscriptLifecycleObservationProducerFact(FrozenFactBase):
    schema_version: Literal["transcript_lifecycle_observation_producer.v1"]
    producer_kind: Literal["transcript_lifecycle"]
    event_domain_contract_id: str
    event_domain_contract_version: str
    event_domain_contract_fingerprint: Fingerprint
    supported_source_event_contract_set_fingerprint: Fingerprint
    reducer_contract_fingerprint: Fingerprint
    producer_fingerprint: Fingerprint


class LongHorizonRewriteObservationProducerFact(FrozenFactBase):
    schema_version: Literal["long_horizon_rewrite_observation_producer.v1"]
    producer_kind: Literal["long_horizon_rewrite"]
    rewrite_contract_id: str
    rewrite_contract_version: str
    rewrite_contract_fingerprint: Fingerprint
    producer_fingerprint: Fingerprint


RuntimeObservationProducerFact: TypeAlias = Annotated[
    ContextSourceObservationProducerFact
    | TranscriptLifecycleObservationProducerFact
    | LongHorizonRewriteObservationProducerFact,
    Field(discriminator="producer_kind"),
]


class RuntimeObservationKindContractFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_kind_contract.v1"]
    kind: str
    producer: RuntimeObservationProducerFact
    authority_class: Literal[
        "runtime_fact",
        "runtime_guidance",
        "runtime_fact_and_guidance",
    ]
    lifecycle_class: Literal[
        "immutable_append_once",
        "causal_append_once",
        "replacement_snapshot",
    ]
    payload_schema_version: str
    payload_schema_fingerprint: Fingerprint
    maximum_payload_utf8_bytes: PositiveInt
    rewrite_eligibility: Literal[
        "never",
        "after_causal_close",
        "superseded_only",
        "long_horizon_rewrite",
    ]
    protection_policy: Literal[
        "always",
        "protect_current_run",
        "protect_effective_head",
        "protect_until_closed",
    ]
    instruction_policy: Literal[
        "fact_only_not_instruction",
        "runtime_guidance_under_root_policy",
        "typed_fact_with_bounded_guidance",
    ]
    kind_contract_fingerprint: Fingerprint


class RuntimeObservationCanonicalCodecContractFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_canonical_codec_contract.v1"]
    codec_id: Literal["pulsara.runtime-observation.canonical-json"]
    codec_version: Literal["1"]
    encoding: Literal["utf-8"]
    object_key_order: Literal["lexicographic"]
    unicode_normalization: Literal["NFC"]
    string_escaping: Literal["json"]
    non_finite_numbers: Literal["forbidden"]
    unknown_fields: Literal["forbidden"]
    maximum_wire_utf8_bytes: PositiveInt
    codec_contract_fingerprint: Fingerprint


class RuntimeObservationProtocolContractFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_protocol_contract.v2"]
    protocol_id: Literal["pulsara.runtime-observation"]
    protocol_version: Literal["2"]
    wire_role: Literal["user"]
    codec_contract: RuntimeObservationCanonicalCodecContractFact
    ordered_kind_contracts: tuple[RuntimeObservationKindContractFact, ...]
    source_lifecycle_registry_contract_fingerprint: Fingerprint
    unknown_kind_policy: Literal["reject_before_adapter"]
    unknown_contract_policy: Literal["reject_before_adapter"]
    protocol_contract_fingerprint: Fingerprint


class HumanInputProtocolContractFact(FrozenFactBase):
    schema_version: Literal["human_input_protocol_contract.v1"]
    protocol_id: Literal["pulsara.human-input"]
    protocol_version: Literal["1"]
    wire_role: Literal["user"]
    envelope_key: Literal["pulsara_human_input"]
    codec_contract_fingerprint: Fingerprint
    raw_text_policy: Literal["escaped_typed_text_field_only"]
    unsupported_multimodal_policy: Literal["reject_until_typed_block_contract"]
    maximum_text_utf8_bytes: PositiveInt
    protocol_contract_fingerprint: Fingerprint


class RuntimeRequestKindContractFact(FrozenFactBase):
    schema_version: Literal["runtime_request_kind_contract.v1"]
    request_kind: Literal[
        "subagent_task",
        "current_run_task",
        "compaction_request",
        "window_compaction_request",
        "governance_request",
        "reflection_request",
        "summarizer_request",
    ]
    instruction_policy: Literal["task_under_root_policy"]
    lifecycle_class: Literal[
        "child_run_entry",
        "current_run_transcript",
        "one_shot_invocation",
    ]
    transcript_persistence: Literal[
        "persist_child_canonical_transcript",
        "persist_current_run_canonical_transcript",
        "invocation_scoped_only",
    ]
    allowed_owner_kinds: tuple[
        Literal[
            "subagent_spawn",
            "current_run",
            "compaction_operation",
            "window_compaction_operation",
            "governance_batch",
            "reflection_job",
            "summarizer_operation",
        ],
        ...,
    ]
    payload_schema_version: str
    payload_schema_fingerprint: Fingerprint
    maximum_payload_utf8_bytes: PositiveInt
    observation_rewrite_policy: Literal["never"]
    kind_contract_fingerprint: Fingerprint


class RuntimeRequestProtocolContractFact(FrozenFactBase):
    schema_version: Literal["runtime_request_protocol_contract.v1"]
    protocol_id: Literal["pulsara.runtime-request"]
    protocol_version: Literal["1"]
    wire_role: Literal["user"]
    envelope_key: Literal["pulsara_runtime_request"]
    codec_contract_fingerprint: Fingerprint
    ordered_kind_contracts: tuple[RuntimeRequestKindContractFact, ...]
    unknown_kind_policy: Literal["reject_before_adapter"]
    unknown_contract_policy: Literal["reject_before_adapter"]
    protocol_contract_fingerprint: Fingerprint


class RuntimeTaskRequestPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_task_request_payload.v1"]
    payload_kind: Literal["task"]
    task_text: str
    task_text_utf8_sha256: Fingerprint
    task_text_utf8_bytes: NonNegativeInt
    ordered_context_fragments: tuple[ProviderInputTypedFragmentFact, ...]
    payload_semantic_fingerprint: Fingerprint


class RuntimeOperationRequestPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_operation_request_payload.v1"]
    payload_kind: Literal["operation"]
    operation_kind: Literal[
        "compaction",
        "window_compaction",
        "governance",
        "reflection",
        "summarizer",
    ]
    objective_contract_fingerprint: Fingerprint
    ordered_model_visible_fragments: tuple[
        ProviderInputTypedFragmentFact, ...
    ]
    input_document_semantic_fingerprints: tuple[Fingerprint, ...]
    output_contract_fingerprint: Fingerprint
    payload_semantic_fingerprint: Fingerprint


RuntimeRequestPayloadFact: TypeAlias = Annotated[
    RuntimeTaskRequestPayloadFact | RuntimeOperationRequestPayloadFact,
    Field(discriminator="payload_kind"),
]


class ProviderUserCarrierProtocolContractFact(FrozenFactBase):
    schema_version: Literal["provider_user_carrier_protocol_contract.v2"]
    human_input_protocol: HumanInputProtocolContractFact
    runtime_request_protocol: RuntimeRequestProtocolContractFact
    runtime_observation_protocol: RuntimeObservationProtocolContractFact
    root_interpretation_fragment_semantic_fingerprint: Fingerprint
    user_item_policy: Literal["exactly_one_registered_outer_envelope"]
    contract_fingerprint: Fingerprint


class HumanInputWireSemanticFact(FrozenFactBase):
    schema_version: Literal["human_input_wire_semantic.v1"]
    protocol_version: Literal["1"]
    human_input_semantic_id: Fingerprint
    causal_occurrence_semantic_fingerprint: Fingerprint
    text: str
    text_utf8_sha256: Fingerprint
    text_utf8_bytes: NonNegativeInt
    canonical_wire_utf8_sha256: Fingerprint
    canonical_wire_utf8_bytes: NonNegativeInt
    semantic_fingerprint: Fingerprint


class RuntimeRequestWireSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_request_wire_semantic.v1"]
    protocol_version: Literal["1"]
    request_kind: Literal[
        "subagent_task",
        "current_run_task",
        "compaction_request",
        "window_compaction_request",
        "governance_request",
        "reflection_request",
        "summarizer_request",
    ]
    request_semantic_id: Fingerprint
    business_occurrence_semantic_fingerprint: Fingerprint
    instruction_policy: Literal["task_under_root_policy"]
    lifecycle_class: Literal[
        "child_run_entry",
        "current_run_transcript",
        "one_shot_invocation",
    ]
    payload: RuntimeRequestPayloadFact
    payload_schema_version: str
    payload_schema_fingerprint: Fingerprint
    payload_semantic_fingerprint: Fingerprint
    canonical_wire_utf8_sha256: Fingerprint
    canonical_wire_utf8_bytes: NonNegativeInt
    semantic_fingerprint: Fingerprint


class SubagentTaskRuntimeRequestOwnerFact(FrozenFactBase):
    schema_version: Literal["subagent_task_runtime_request_owner.v1"]
    owner_kind: Literal["subagent_spawn"]
    runtime_session_id: str
    parent_run_id: str
    child_run_id: str
    spawn_event_reference: ContextEventReferenceFact
    owner_fingerprint: Fingerprint


class CurrentRunRuntimeRequestOwnerFact(FrozenFactBase):
    schema_version: Literal["current_run_runtime_request_owner.v1"]
    owner_kind: Literal["current_run"]
    runtime_session_id: str
    run_id: str
    turn_id: str | None
    request_occurrence_semantic_fingerprint: Fingerprint
    owner_fingerprint: Fingerprint


class OneShotRuntimeRequestOwnerFact(FrozenFactBase):
    schema_version: Literal["one_shot_runtime_request_owner.v1"]
    owner_kind: Literal[
        "compaction_operation",
        "window_compaction_operation",
        "governance_batch",
        "reflection_job",
        "summarizer_operation",
    ]
    runtime_session_id: str
    operation_semantic_id: Fingerprint
    source_event_references: tuple[ContextEventReferenceFact, ...]
    owner_fingerprint: Fingerprint


RuntimeRequestOwnerFact: TypeAlias = Annotated[
    SubagentTaskRuntimeRequestOwnerFact
    | CurrentRunRuntimeRequestOwnerFact
    | OneShotRuntimeRequestOwnerFact,
    Field(discriminator="owner_kind"),
]


class RuntimeRequestAttributionFact(FrozenFactBase):
    schema_version: Literal["runtime_request_attribution.v1"]
    request_semantic_fingerprint: Fingerprint
    request_kind: Literal[
        "subagent_task",
        "current_run_task",
        "compaction_request",
        "window_compaction_request",
        "governance_request",
        "reflection_request",
        "summarizer_request",
    ]
    owner: RuntimeRequestOwnerFact
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    attribution_fingerprint: Fingerprint


class RuntimeDerivedObservationCarrierContractFact(FrozenFactBase):
    schema_version: Literal["runtime_derived_observation_carrier.v2"]
    carrier_id: Literal["pulsara.runtime_observation.user_message"]
    carrier_version: Literal["v2"]
    provider_api: str
    internal_role: Literal["runtime_observation"]
    wire_role: Literal["user"]
    user_carrier_protocol_contract_fingerprint: Fingerprint
    wire_shape_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


class RuntimeRequestCarrierContractFact(FrozenFactBase):
    schema_version: Literal["runtime_request_carrier_contract.v1"]
    carrier_id: Literal["pulsara.runtime_request.user_message"]
    carrier_version: Literal["v1"]
    provider_api: str
    internal_role: Literal["runtime_request"]
    wire_role: Literal["user"]
    user_carrier_protocol_contract_fingerprint: Fingerprint
    wire_shape_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint
```

Kind registry必须按 `kind` 排序且唯一；每个kind恰好一个producer。ContextSource producer的 `transition_kinds` 必须非空、排序且唯一，因此一个source可以通过不同transition生成多个kind，一个kind也可以覆盖同一source的update/empty等多个transition。Derived producer不得伪造 `ContextSourceId`。

`ProviderUserCarrierProtocolContractFact` 只能由一个composition-root factory构造。该factory必须证明human、runtime request与runtime observation三个envelope key两两互斥、三者使用同一canonical JSON基础codec、root interpretation fragment精确绑定三个protocol，并将第9.2节source lifecycle registry fingerprint写入runtime-observation protocol。任一producer、transition、request kind、codec、carrier或root interpretation变化都必须改变联合contract fingerprint并触发合法compatibility rollover。

每个 `RuntimeRequestKindContractFact.payload_schema_fingerprint` 必须绑定一个具体、historically rebindable的 `FrozenFactBase` payload DTO；禁止 `dict[str, Any]`、free-form prompt string或producer自拼outer JSON。`RuntimeRequestWireSemanticFact` 是该typed payload编码后的semantic identity，`RuntimeRequestAttributionFact` 保存owner与ledger attribution，两者不得混为一个fingerprint。

V1 runtime request matrix冻结如下：

| Request kind | Owner | Lifecycle | Transcript persistence |
|---|---|---|---|
| `subagent_task` | `subagent_spawn` | child_run_entry | persist_child_canonical_transcript |
| `current_run_task` | `current_run` | current_run_transcript | persist_current_run_canonical_transcript |
| `compaction_request` | `compaction_operation` | one_shot_invocation | invocation_scoped_only |
| `window_compaction_request` | `window_compaction_operation` | one_shot_invocation | invocation_scoped_only |
| `governance_request` | `governance_batch` | one_shot_invocation | invocation_scoped_only |
| `reflection_request` | `reflection_job` | one_shot_invocation | invocation_scoped_only |
| `summarizer_request` | `summarizer_operation` | one_shot_invocation | invocation_scoped_only |

Runtime request是当前任务，不进入 `RuntimeObservationProjectionStableStateFact`，不得被runtime-observation rewrite、replacement或tombstone处理。Subagent task一旦进入child canonical transcript，其runtime-request semantic与attribution必须持久化；one-shot request只进入对应invocation provider plan与ModelStart input identity，但仍必须有稳定owner、retry identity和exact replay carrier。

Central validator还必须冻结：`subagent_task|current_run_task` 只能使用 `RuntimeTaskRequestPayloadFact`；其余kind只能使用 `RuntimeOperationRequestPayloadFact`，且 `operation_kind` 与request kind/owner逐项匹配。Ordered fragments、input document count、单fragment bytes与总canonical wire bytes都由kind contract和resolved provider physical policy共同限制。所有fragments必须recursively immutable，dispatch前从frozen carrier深拷贝并重算wire semantic。

Provider message fragment schema在ROAC1同步升级，internal role union必须显式包含 `runtime_request`；不得继续把该分支压成普通 `user` semantic后再靠name/content猜测ownership。

V2 kind/authority matrix冻结如下。未在表中的 kind不得由 production binding产生：

| Kind | Producer | Authority class | Instruction policy | Lifecycle |
|---|---|---|---|---|
| `runtime_clock` | context_source(`RUNTIME_CLOCK`, observation) | runtime_fact | fact_only_not_instruction | causal_append_once |
| `recalled_memory_snapshot` | context_source(`MEMORY_PROJECTION`, snapshot_update/explicit_empty) | runtime_fact | fact_only_not_instruction | replacement_snapshot |
| `capability_prose_snapshot` | context_source(`CAPABILITY_CATALOG`, snapshot_update/explicit_empty) | runtime_fact_and_guidance | typed_fact_with_bounded_guidance | replacement_snapshot |
| `active_skill_snapshot` | context_source(`ACTIVE_SKILL`, snapshot_update/explicit_empty) | runtime_guidance | runtime_guidance_under_root_policy | replacement_snapshot |
| `workspace_skill_snapshot` | context_source(`WORKSPACE_SKILL`, snapshot_update/explicit_empty) | runtime_fact_and_guidance | typed_fact_with_bounded_guidance | replacement_snapshot |
| `plan_status_snapshot` | context_source(`PLAN_STATUS`, status_update/terminal) | runtime_fact | fact_only_not_instruction | replacement_snapshot |
| `plan_guidance` | context_source(`PLAN_GUIDANCE`, guidance) | runtime_guidance | runtime_guidance_under_root_policy | causal_append_once |
| `recovery_guidance` | context_source(`RECOVERY`, guidance) | runtime_fact_and_guidance | typed_fact_with_bounded_guidance | causal_append_once |
| `rollout_status_snapshot` | context_source(`ROLLOUT_STATUS`, status_update/terminal) | runtime_fact | fact_only_not_instruction | replacement_snapshot |
| `subagent_handoff` | context_source(`SUBAGENT_HANDOFF`, handoff) | runtime_fact_and_guidance | typed_fact_with_bounded_guidance | causal_append_once |
| `subagent_result_delivery` | context_source(`SUBAGENT_RESULT`, delivery) | runtime_fact | fact_only_not_instruction | immutable_append_once |
| `mcp_diagnostic_snapshot` | context_source(`MCP_DIAGNOSTIC`, diagnostic_update/terminal) | runtime_fact | fact_only_not_instruction | replacement_snapshot |
| `lifecycle_observation` | transcript_lifecycle(registered event domain) | runtime_fact | fact_only_not_instruction | causal_append_once |
| `compaction_replacement_summary` | long_horizon_rewrite(confirmed rewrite contract) | runtime_fact | fact_only_not_instruction | causal_append_once |
| `long_horizon_rollup_observation` | long_horizon_rewrite(confirmed rewrite contract) | runtime_fact | fact_only_not_instruction | causal_append_once |
| `runtime_observation_rewrite_projection` | long_horizon_rewrite(confirmed rewrite contract) | runtime_fact | fact_only_not_instruction | immutable_append_once |

`SYSTEM`、`RUNTIME_ENVIRONMENT`和`MEMORY_INSTRUCTION`属于 generation root composition，不是 mid-history runtime observation kind。Provider tool definitions由独立 capability tool catalog root拥有，也不得伪装成 observation。

`runtime_observation_rewrite_projection` 的 `immutable_append_once` 只表示该次committed rewrite occurrence自身不可原地修改；其kind contract的 `rewrite_eligibility` 必须是 `long_horizon_rewrite`，允许后续confirmed rewrite通过transitive coverage替代其active provider projection。

Kind contract的 `payload_schema_fingerprint` 必须指向可 historical rebind的 typed DTO binding，不得指向 `dict[str, Any]`、未约束 JSON object或仅有文字说明的 schema。Root interpretation fragment与上表由同一 composition-root factory生成；任一 authority/instruction/lifecycle变化都必须改变 protocol contract与generation compatibility。

### 4.3 Canonical wire codec与注入防线

V2不使用可被 payload正文闭合的 XML wrapper。每条 observation是一个完整 canonical JSON object：

```json
{
  "pulsara_runtime_observation": {
    "authority_class": "runtime_fact",
    "kind": "clock",
    "lifecycle": "causal_append_once",
    "observation_semantic_id": "sha256:...",
    "protocol_version": "2",
    "source_instance_id": "runtime:clock",
    "payload": {
      "observed_at_utc": "2026-07-20T14:32:18+08:00",
      "timezone_name": "CST"
    }
  }
}
```

Payload只能由该 kind的typed DTO经中央 codec生成。普通文本只能出现在typed JSON string字段中，必须执行 JSON escaping、Unicode normalization、control-character validation和bytes bound。Payload中的伪造 key、闭合标签、JSON fragment或未知字段不能改变外层结构。

### 4.4 Human input、Runtime Request carrier与可执行信任边界

只把runtime observation降为user role仍然不够。如果human正文继续raw发送，用户可以直接提交一份完整的runtime-observation JSON；provider看到的role/content便与Pulsara生成的runtime authority完全相同。因此V2必须同时hard cut human-input carrier。

每条human-authored text message都由Pulsara编码为：

```json
{
  "pulsara_human_input": {
    "human_input_semantic_id": "sha256:...",
    "protocol_version": "1",
    "text": "...raw user text encoded as one JSON string..."
  }
}
```

`human_input_semantic_id` 由canonical human-message causal occurrence semantic与text content digest确定性派生；event ID、sequence、compile ID和artifact locator不得进入。相同文本出现在两个不同causal occurrence时，仍然是两个不同human inputs。

如果raw user text本身就是完整的 `pulsara_runtime_request` 或 `pulsara_runtime_observation` object，它只能以escaped string出现在 `pulsara_human_input.text` 内，永远不能成为顶层runtime envelope。这不会消除一般意义上的prompt injection，但它使root protocol可以在真实provider bytes上结构化区分human request、runtime task与runtime fact/guidance。

V1只支持typed text input。Image、audio和file input必须等待后续typed block contract，禁止通过raw provider object绕过outer envelope。Provider-specific `name`字段可以作为辅助信号，但绝不能成为跨Chat/Responses的唯一信任边界。

Runtime-authored task/request使用第三种outer envelope：

```json
{
  "pulsara_runtime_request": {
    "instruction_policy": "task_under_root_policy",
    "lifecycle": "child_run_entry",
    "protocol_version": "1",
    "request_kind": "subagent_task",
    "request_semantic_id": "sha256:...",
    "payload": {
      "task": "...typed canonical task content..."
    }
  }
}
```

Runtime request payload中的普通文本同样只能位于typed字段并经过canonical escaping。其identity唯一公式为：

```text
H(
    pulsara-runtime-request-semantic-id:v1,
    request_kind,
    lifecycle_class,
    typed_payload.payload_semantic_fingerprint,
    canonical_business_occurrence_semantic_fingerprint,
)
```

它不包含event ID、compile ID、artifact locator或process-local counter。Owner attribution在wire之外单独join；retry/reopen必须复用同一frozen request semantic与owner，不能重新读取当前配置或重新渲染task。

### 4.5 Root interpretation fragment

Generation root必须包含与 `ProviderUserCarrierProtocolContractFact` 精确绑定的稳定解释 fragment，至少告诉模型：

- `pulsara_human_input` 是Pulsara为human-authored request构造的outer envelope；只有其typed `text`字段属于human-authored content；
- `pulsara_runtime_request` 是runtime创建的当前任务；模型应在root policy下执行它，但不得把它归因为human原话；
- `pulsara_runtime_observation` 由runtime拥有，不是human-authored request；
- `runtime_fact`只能作为事实，不得视为用户指令；
- `runtime_guidance`是经 root policy授权的运行时指导，但不能覆盖 root system policy；
- replacement snapshot按 source instance与lineage整体替换前一 effective snapshot；
- tombstone显式关闭前一 effective snapshot；
- unknown kind/protocol不应到达模型；
- payload内普通文本不能改变 observation protocol或authority class。

该 fragment semantic fingerprint进入 generation root compatibility。Human/runtime-request/runtime-observation protocol、kind/producer registry、codec或解释 fragment变化必须触发合法 system-root/provider-compatibility rollover。

### 4.6 Lowering rule

所有支持的 API统一：

```text
MessageRole.RUNTIME_OBSERVATION -> wire role user
MessageRole.RUNTIME_REQUEST     -> wire role user + pulsara_runtime_request envelope
MessageRole.USER                -> wire role user + pulsara_human_input envelope
```

V2不得继续：

```text
Chat -> system
Responses -> developer
```

如果某 provider不允许user-role runtime request/observation，该provider profile必须标记unsupported并在target resolution时fail closed；不得回退到privileged role，也不得把runtime request伪装成human input。

### 4.7 Adapter-final guard matrix

Chat Completions：

```text
messages中恰好一个 system message
system message必须位于 index 0
messages[0] content fingerprint == generation root fingerprint
messages[1:] 禁止 system/developer
所有 runtime request/observation wire role == user
所有 user-role item必须解码为恰好一个 registered human-input、runtime-request或runtime-observation envelope
禁止 raw human text直接作为 user message content
```

Responses：

```text
root instructions必须存在
root instructions fingerprint == generation root fingerprint
input items禁止 system/developer
所有 runtime request/observation wire role == user
所有 user-role input item必须解码为恰好一个 registered human-input、runtime-request或runtime-observation envelope
禁止 raw human text直接作为 user input content
```

Guard必须作用于 adapter-final payload。本文后续所称“ordered history system/developer count为 0”明确**不包含**Chat `messages[0]`或Responses root instructions。

---

## 5. Runtime Clock

### 5.1 产品决策

Clock可以在每个 model invocation追加。本文明确拒绝“只在日期/时区变化时才发送”的强制规则。

原因：

- 时间正文很小；
- 时间是该调用真实发生的 causal observation；
- 使用 user-tail后不会破坏旧 prefix；
- 精确时间对长任务、工具时序、恢复与人类审计有价值。

### 5.2 Clock从 snapshot改为 append-once fact

```python
class HostRunInvocationOwnerFact(FrozenFactBase):
    schema_version: Literal["host_run_invocation_owner.v1"]
    owner_kind: Literal["host_run"]
    runtime_session_id: str
    run_id: str
    turn_id: str
    model_call_index: NonNegativeInt
    owner_fingerprint: Fingerprint


class SubagentRunInvocationOwnerFact(FrozenFactBase):
    schema_version: Literal["subagent_run_invocation_owner.v1"]
    owner_kind: Literal["subagent_run"]
    runtime_session_id: str
    run_id: str
    subagent_run_id: str
    model_call_index: NonNegativeInt
    owner_fingerprint: Fingerprint


class DirectSubsystemInvocationOwnerFact(FrozenFactBase):
    schema_version: Literal["direct_subsystem_invocation_owner.v1"]
    owner_kind: Literal["direct_subsystem"]
    subsystem_kind: Literal[
        "compaction",
        "governance",
        "reflection",
        "summarization",
        "direct_model_call",
    ]
    operation_id: str
    runtime_session_id: str | None
    owner_fingerprint: Fingerprint


RuntimeObservationInvocationOwnerFact: TypeAlias = Annotated[
    HostRunInvocationOwnerFact
    | SubagentRunInvocationOwnerFact
    | DirectSubsystemInvocationOwnerFact,
    Field(discriminator="owner_kind"),
]


class RuntimeClockObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_clock_observation_semantic.v2"]
    observed_at_utc: str
    timezone_name: str
    local_date: str
    rendering_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class RuntimeClockObservationAttributionFact(FrozenFactBase):
    schema_version: Literal["runtime_clock_observation_attribution.v1"]
    resolved_model_call_id: str
    invocation_owner: RuntimeObservationInvocationOwnerFact
    semantic: RuntimeClockObservationSemanticFact
    attribution_fingerprint: Fingerprint
```

唯一性 key：

```text
(invocation_owner.owner_fingerprint, resolved_model_call_id)
```

同一 stable ModelStart retry必须复用同一 clock candidate；不得读取新时钟改写 payload。

### 5.3 Clock wire shape

Clock使用第 4 节中央 JSON codec。它的 `observation_semantic_id`由 invocation owner、resolved call、frozen time semantic确定性派生，不暴露 raw model-call/event ID。Payload只包含 model-visible时间事实。

禁止携带：

- `latest_revision_wins`；
- `supersedes=all_prior_revisions`；
- candidate cache internals；
- physical event sequence；
- 与模型语义无关的 revision JSON。

旧 clock不再被定义为 stale snapshot。它表示历史调用发生时的时间，因此可以合法留在 causal history。

---

## 6. Memory Snapshot V2

### 6.1 V1不实施 memory delta

本文撤回“memory只追加紧凑 delta”的强制要求。Memory recall是 query-relative selection，不等同于 canonical memory mutation：

```text
old recall = [A, B]
new recall = [A, C]
```

它不一定表示 B被删除、C刚创建。把它硬解释为 `Remove(B) + Add(C)` 会混淆 memory truth与本次召回结果。

V1冻结：

```text
same normalized snapshot -> no provider append
different snapshot       -> append one complete replacement snapshot
```

### 6.2 Semantic identity与attribution分离

```python
class RecalledMemorySnapshotSemanticFact(FrozenFactBase):
    schema_version: Literal["recalled_memory_snapshot_semantic.v2"]
    projection_kind: Literal["memory"]
    ordered_memory_item_semantic_fingerprints: tuple[Fingerprint, ...]
    normalized_model_visible_content_fingerprint: Fingerprint
    selection_contract_id: str
    selection_contract_version: str
    selection_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class RecalledMemorySnapshotAttributionFact(FrozenFactBase):
    schema_version: Literal["recalled_memory_snapshot_attribution.v1"]
    semantic: RecalledMemorySnapshotSemanticFact
    projection_ready_event_reference: ContextEventReferenceFact
    source_memory_event_references: tuple[ContextEventReferenceFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    attribution_fingerprint: Fingerprint
```

Semantic fingerprint必须排除：

- `ProjectionReadyEvent.id`；
- event sequence/high-water；
- projection生成时间；
- artifact ID/placement；
- PostgreSQL row identity；
- source refs的物理顺序；
- 只用于审计的 timing。

### 6.3 Deterministic normalization

比较前必须：

- 按 stable memory semantic identity确定性排序；
- 使用相同 canonical renderer；
- 统一空白/Unicode/JSON canonicalization；
- 绑定 selection policy/version；
- 对完整 model-visible正文计算 fingerprint。

仅查询返回顺序变化不得形成新 snapshot。

### 6.4 Append算法

```text
current = normalize(new projection)
head    = committed generation memory head

if head.semantic_fingerprint == current.semantic_fingerprint:
    provider append = none
    committed semantic head remains unchanged
    optional attribution observation may be recorded outside provider identity
else:
    revision = head.revision + 1, or 1 if absent
    append complete replacement snapshot
    bind predecessor semantic fingerprint
    advance committed memory head atomically with ModelStart
```

Snapshot wire shape：

```json
{
  "pulsara_runtime_observation": {
    "authority_class": "runtime_fact",
    "kind": "recalled_memory_snapshot",
    "lifecycle": "replacement_snapshot",
    "observation_semantic_id": "sha256:...",
    "protocol_version": "2",
    "source_instance_id": "memory:projection",
    "payload": {
      "entries": [],
      "predecessor_observation_semantic_id": "sha256:...",
      "replacement_scope": "entire_recalled_memory_projection",
      "revision": 2
    }
  }
}
```

`projection_ready_event_reference`只存在于 attribution和effective-head reference中，不进入以上 wire bytes。完整 provider unit semantic fingerprint必须从这份 canonical JSON bytes重算。

### 6.5 Empty snapshot matrix

```text
previous empty + current empty
    -> no-op

previous non-empty + current empty
    -> append explicit empty replacement/tombstone

previous absent + current empty
    -> V1 may no-op only if no earlier memory head exists
```

显式 empty replacement必须让模型知道旧 recalled memory不再属于当前有效 projection。

### 6.6 Recalled Memory与Recent Working Context：V1 hard-cut决策

当前 `Recalled Memory and Recent Working Context` 混合了：

- canonical/retrieved memory；
- 已经存在于 transcript/tool history中的近期活动；
- process-local working context projection。

现有 `ProjectionReadyEvent(projection_kind="working_context"|"mixed")` 只保存一段合并后的summary prose，没有typed memory-entry / working-entry partition，也没有能证明拆分结果的source authority。V1因此**不建立** `RecentWorkingContextSource`，也不新增对应 `ContextSourceId`、runtime-observation kind或lifecycle entry。

V1冻结为：

```text
ProjectionReadyEvent(projection_kind="recalled_memory")
    -> 可以进入 RecalledMemorySnapshotSource

ProjectionReadyEvent(projection_kind="working_context"|"mixed")
    -> 禁止进入 MEMORY_PROJECTION provider source
    -> 不得解析summary字符串猜测memory/working partition
    -> 可保留为operational/diagnostic artifact，但不是model-visible authority

recent assistant/tool/user activity
    -> 只由canonical transcript与confirmed Long-Horizon projection提供
```

ROAC1的schema hard cut必须让memory producer输出typed recalled-memory entries，或明确的typed empty snapshot；旧 `working_context|mixed` carrier不得通过fallback进入新protocol。ROAC2只对这份typed recalled-memory snapshot实施semantic no-op/replacement。

未来若确实存在canonical transcript之外、必须model-visible的recent working fact，应另立hard cut：先定义typed source event、payload、authority与causal placement，再新增正式 `ContextSourceId`/kind/lifecycle。禁止从旧summary prose迁移推断。

---

## 7. Capability 与 Active Skill

### 7.1 Capability prose

Capability prose采用与 memory相同的 content-semantic no-op规则：

```text
same normalized prose snapshot -> no append
changed prose snapshot         -> append complete replacement snapshot
```

V1不要求 capability delta。Skill安装/删除频率通常远低于 model call频率，完整 replacement更简单、可审计。

Semantic fingerprint必须由：

- ordered visible capability/skill semantic identities；
- normalized rendered prose；
- projection contract；
- stable ordering contract；

共同计算，不能由 artifact ID或snapshot generation自报。

### 7.2 Provider tool schema

Provider `tools` 是独立 authority，不属于 capability prose message。

```text
tool catalog semantic fingerprint unchanged
    -> retain generation

tool catalog semantic fingerprint changed
    -> TOOL_CATALOG_SEMANTIC_CHANGED rollover
```

MCP server异步连接完成常常发生在 session早期。V1接受此时的一次显式 rollover。Composition root可以提供 bounded startup stabilization/debounce，但不得无限等待，也不得为了缓存隐藏已确认可用的工具。

### 7.3 Active skill

V1 的 capability producer给出的是“完整 active-skill 集合”，不是可独立归约的
per-skill activation log。因此 `ACTIVE_SKILL` 必须冻结为 aggregate
`replacement_snapshot`：

- 非空集合产生 `snapshot_update`，正文是完整、确定排序的 active-skill snapshot；
- 与 committed effective head 语义相同则 no-op，不追加重复 unit；
- 集合变空时产生 `explicit_empty` replacement，并精确引用 predecessor observation；
- source absence只表示 `retain_effective_head`，绝不表示 deactivation；
- wire role为 `user` runtime observation；
- source artifact/content-addressed skill identity必须固定。

只有未来把 producer hard-cut 为 per-skill/per-turn activation facts 后，才允许重新引入
causal activation/deactivation reducer；不得把 aggregate snapshot同时解释为 append-once history。

---

## 8. Lifecycle Observation

### 8.1 保留一次性 causal fact

本文冻结：lifecycle observation保留，不降级为 process-local metadata，也不重复生成 snapshot。

支持的 V1 source包括：

- failed/aborted/host-teardown `RunEndEvent`；
- `TerminalProcessCompletedEvent`；
- typed recovery/continuation event；
- 其他经过 registry显式允许的 lifecycle event。

### 8.2 DTO

```python
class LifecycleObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["lifecycle_observation_semantic.v2"]
    observation_kind: Literal[
        "run_failed",
        "run_interrupted",
        "host_teardown",
        "terminal_process_completed",
        "recovery_guidance",
    ]
    bounded_text: str
    rendering_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class LifecycleObservationAttributionFact(FrozenFactBase):
    schema_version: Literal["lifecycle_observation_attribution.v1"]
    semantic: LifecycleObservationSemanticFact
    source_event_reference: ContextEventReferenceFact
    canonical_position: NonNegativeInt
    attribution_fingerprint: Fingerprint
```

V1明确不新增 `RuntimeObservationCommittedEvent`。`RunEndEvent`、`TerminalProcessCompletedEvent`或其他注册 source event本身就是唯一 durable owner；transcript reducer从它确定性投影 observation leaf与semantic ID。ProviderInput append/ModelStart事件只负责证明该既有 leaf何时进入provider generation，不创造第二份业务事实。

### 8.3 Invariants

- 一个 source event最多一个 canonical observation；
- stable semantic observation ID由 source event的historical semantic identity确定性派生，但 raw event ID不进入 wire；
- retry/replay不得生成不同正文；
- observation保留原 causal位置；
- internal role为 `runtime_observation`；
- wire role为 `user`；
- 不参与 latest-wins supersession；
- 不触发 auxiliary rebase；
- 多个真实 completion/interrupt event可以各有一条 observation，因为它们是不同事实。

### 8.4 当前 legacy路径

当前同时存在 transcript reducer和legacy context transcript lifecycle-note构造路径。Hard cut后必须只有 durable transcript reducer是唯一 producer；legacy helper只能留在 migration/doctor allowlist，最终应删除。

---

## 9. 删除 Standalone Auxiliary Frame Rebase

### 9.1 删除的生产概念

以下必须从生产 schema、planner、store、recovery和Inspector中删除：

```text
ProviderInputRolloverReason.AUXILIARY_FRAME_REBASE
ProviderAuxiliaryFrameRebaseAuthorityFact
_removed_dynamic_source_keys()
_auxiliary_frame_rebase_rollover_intent()
```

不得保留“预算不足时 fallback到 auxiliary rebase”的隐藏入口。

### 9.2 Source closure替代 absence-based rebase

每种 source必须由一个穷尽、版本化 registry定义唯一 lifecycle：

```python
class ContextSourceObservationKindBindingFact(FrozenFactBase):
    schema_version: Literal["context_source_observation_kind_binding.v1"]
    transition_kind: Literal[
        "observation",
        "snapshot_update",
        "explicit_empty",
        "guidance",
        "status_update",
        "terminal",
        "handoff",
        "delivery",
        "diagnostic_update",
    ]
    observation_kind: str
    binding_fingerprint: Fingerprint


class ContextSourceLifecycleRegistryEntryFact(FrozenFactBase):
    schema_version: Literal["context_source_lifecycle_registry_entry.v2"]
    source_id: ContextSourceId
    lifecycle_class: Literal[
        "generation_root",
        "immutable_append_once",
        "causal_append_once",
        "replacement_snapshot",
    ]
    source_instance_scope: Literal[
        "runtime_session",
        "continuity_cohort",
        "run",
        "turn",
        "workflow",
        "model_call",
        "subagent",
        "operation",
    ]
    absence_semantics: Literal[
        "forbidden",
        "no_new_fact",
        "retain_effective_head",
    ]
    closure_kind: Literal[
        "none",
        "empty_replacement",
        "typed_terminal_snapshot",
        "root_rollover",
    ]
    rollover_materialization: Literal[
        "rebuild_root_from_exact_reference",
        "reuse_effective_snapshot_reference",
        "copy_immutable_causal_unit",
        "consume_runtime_observation_rewrite",
    ]
    rewrite_eligibility: Literal[
        "never",
        "superseded_only",
        "after_causal_close",
        "long_horizon_rewrite",
    ]
    observation_kind_bindings: tuple[
        ContextSourceObservationKindBindingFact, ...
    ]
    entry_fingerprint: Fingerprint


class ContextSourceLifecycleRegistryContractFact(FrozenFactBase):
    schema_version: Literal["context_source_lifecycle_registry_contract.v2"]
    registry_id: Literal["pulsara.context-source-lifecycle"]
    registry_version: Literal["2"]
    ordered_entries: tuple[ContextSourceLifecycleRegistryEntryFact, ...]
    registry_fingerprint: Fingerprint
```

Composition root必须证明：

```text
set(registry.source_ids) == set(ContextSourceId)
每个 source ID恰好一条 entry
generation-root entries have no observation kind bindings
every legal transition of every non-root source has exactly one kind binding
every context-source binding exactly matches one context_source producer
transcript_lifecycle and long_horizon_rewrite producers use their own registries
derived producers never fabricate a ContextSourceId
```

ContextSource set-equality只覆盖 `producer_kind="context_source"` 分支。`lifecycle_observation` 与 `runtime_observation_rewrite_projection` 分别对其transcript lifecycle event-domain contract和Long-Horizon rewrite contract做独立完备性校验；不得为了通过source基数检查而伪造 `ContextSourceId`。

当前 `ContextSourceId.PLAN` 同时承载 replacement workflow status和append-once revision guidance，不满足唯一 lifecycle。V2物理拆分为：

```text
PLAN_STATUS
PLAN_GUIDANCE
```

旧 `PLAN` ID删除，不提供运行时猜测。

穷尽 registry冻结如下：

| ContextSourceId | Lifecycle | Scope | Absence | Closure | Rewrite/rollover |
|---|---|---|---|---|---|
| `SYSTEM` | generation_root | runtime_session | forbidden | root_rollover | exact root ref |
| `RUNTIME_ENVIRONMENT` | generation_root | runtime_session | forbidden | root_rollover | exact root ref |
| `MEMORY_INSTRUCTION` | generation_root | runtime_session | retain_effective_head | root_rollover | exact root ref |
| `RUNTIME_CLOCK` | causal_append_once | model_call | no_new_fact | none | Long-Horizon；保护current run/latest |
| `MEMORY_PROJECTION` | replacement_snapshot | continuity_cohort | retain_effective_head | empty_replacement | superseded可rewrite；head受保护 |
| `CAPABILITY_CATALOG` | replacement_snapshot | runtime_session | retain_effective_head | empty_replacement或tool rollover | superseded可rewrite；head受保护 |
| `ACTIVE_SKILL` | replacement_snapshot | continuity_cohort | retain_effective_head | empty_replacement | superseded可rewrite；effective head受保护 |
| `WORKSPACE_SKILL` | replacement_snapshot | runtime_session | retain_effective_head | empty_replacement | superseded可rewrite；head受保护 |
| `PLAN_STATUS` | replacement_snapshot | workflow | retain_effective_head | typed_terminal_snapshot | superseded可rewrite；active head受保护 |
| `PLAN_GUIDANCE` | causal_append_once | workflow | no_new_fact | none | workflow closed后可rewrite |
| `RECOVERY` | causal_append_once | run | no_new_fact | none | current run受保护，后续可rewrite |
| `ROLLOUT_STATUS` | replacement_snapshot | run | retain_effective_head | typed_terminal_snapshot | latest/terminal head受保护 |
| `SUBAGENT_HANDOFF` | causal_append_once | subagent | no_new_fact | none | child active时受保护 |
| `SUBAGENT_RESULT` | immutable_append_once | subagent | no_new_fact | none | delivery/compaction authority决定 |
| `MCP_DIAGNOSTIC` | replacement_snapshot | runtime_session | retain_effective_head | typed_terminal_snapshot | latest head受保护；tool变化另行rollover |

Source selection未返回某项只能依 registry解释为 `no_new_fact` 或 `retain_effective_head`，绝不能表示删除。

`MEMORY_PROJECTION` entry在V1只接受第6.6节typed recalled-memory payload/empty transition。Registry中故意不存在 `RecentWorkingContextSource`；`working_context|mixed` legacy projection不能借用 `MEMORY_PROJECTION` kind/lifecycle。

`observation_kind_bindings` 必须是第 4.2 节producer-aware kind matrix的精确逆映射。例如 `ACTIVE_SKILL` 的snapshot-update/explicit-empty都绑定同一个typed `active_skill_snapshot` kind；`RECOVERY` 只绑定context-source `recovery_guidance`，而由RunEnd等事件派生的 `lifecycle_observation` 属于transcript-lifecycle producer；Long-Horizon rewrite则不出现在任何source entry中。

### 9.3 合法 rollover matrix

| Reason | Authority | 新 generation 可重物化的内容 |
|---|---|---|
| system root semantic changed | exact old/new root semantic authority | 新root + exact effective snapshots + 全部未被rewrite的causal units |
| tool catalog semantic changed | exact old/new tool catalog authority | 新tools + exact effective snapshots + 全部未被rewrite的causal units |
| provider-visible compatibility changed | exact binding/contract authority | 按新codec重编码同一canonical active projection，不得自行删除facts |
| explicit Long-Horizon rewrite | confirmed compaction/rewrite authority | 可使用confirmed transcript/observation replacement projection |
| confirmed offline repair | committed repair event/artifact | 仅repair authority明确授权的range/state |
| explicit administrative reset | operator/reset authority | 按reset contract明确定义，不得默认继承为语义rewrite |
| auxiliary frames存在或变多 | 无 | 禁止 |
| selected source暂时缺失 | 无 | 禁止 |
| cache hit ratio低 | provider observation而非rewrite authority | 禁止 |

合法 rollover构造新 generation时：

- generation-root source从 exact root reference重物化；
- replacement source只物化最新 effective snapshot，旧revision已由lineage语义显式取代；
- immutable/causal source必须复制所有仍属active/protected projection的exact committed units；
- 已经confirmed Long-Horizon rewrite的ranges使用其replacement projection；
- root/tool/compatibility改变只解释“为什么cache可以失效”，不自动解释“为什么causal fact可以删除”。

因此，必要rollover可自然省略已superseded的replacement revisions，但不得在没有rewrite authority时丢弃旧clock、未闭合lifecycle或其他causal units。

### 9.4 Context pressure

如果旧 snapshot/observation最终使 context接近预算：

```text
measure actual ProviderInput preview
-> enter Long-Horizon planning
-> obtain confirmed rewrite authority
-> close old generation
-> build rewritten generation
```

预算不足本身不能授权静默删除 canonical transcript。若固定 root+tools已经不可容纳，应判定 target infeasible并 fail closed。

### 9.5 Runtime Observation Projection Rewrite

删除通用 rebase不等于允许 observations永久无界增长。Clock、superseded snapshots和closed lifecycle facts形成第三条 append-only causal history，必须进入 Long-Horizon rewrite domain。

新增：

```python
class RuntimeObservationProjectionPhysicalPolicyFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_physical_policy.v1"]
    leaf_max_entries: PositiveInt
    leaf_max_canonical_bytes: PositiveInt
    internal_max_fanout: PositiveInt
    maximum_tree_height: PositiveInt
    maximum_event_root_bytes: PositiveInt
    maximum_changed_nodes_per_rewrite: PositiveInt
    maximum_artifact_batches_per_rewrite: PositiveInt
    operation_deadline_seconds: PositiveInt
    policy_fingerprint: Fingerprint


class RuntimeObservationProjectionSetNodeReferenceFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_set_node_reference.v1"]
    node_kind: Literal["leaf", "internal"]
    height: PositiveInt
    member_count: PositiveInt
    first_causal_key: Fingerprint
    last_causal_key: Fingerprint
    ordered_semantic_accumulator: Fingerprint
    ordered_causal_accumulator: Fingerprint
    artifact_reference: ContextArtifactReferenceFact
    reference_fingerprint: Fingerprint


class RuntimeObservationProjectionSetReferenceFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_set_reference.v1"]
    set_kind: Literal[
        "active",
        "protected",
        "eligible",
        "retained",
        "rewritten",
        "open_lifecycle",
        "pending_dependency",
    ]
    member_count: NonNegativeInt
    ordered_semantic_accumulator: Fingerprint
    ordered_causal_accumulator: Fingerprint
    root_node_reference: RuntimeObservationProjectionSetNodeReferenceFact | None
    set_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class RuntimeObservationEffectiveHeadSetReferenceFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_effective_head_set_reference.v1"]
    head_count: NonNegativeInt
    ordered_head_accumulator: Fingerprint
    root_node_reference: RuntimeObservationProjectionSetNodeReferenceFact | None
    set_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class RuntimeObservationProjectionStableStateFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_stable_state.v1"]
    state_revision: NonNegativeInt
    source_generation_id: str
    source_generation_core_fingerprint: Fingerprint
    authority_horizon_set_reference: LedgerAuthorityHorizonSetReferenceFact
    active_observations: RuntimeObservationProjectionSetReferenceFact
    protected_observations: RuntimeObservationProjectionSetReferenceFact
    eligible_observations: RuntimeObservationProjectionSetReferenceFact
    open_lifecycle_observations: RuntimeObservationProjectionSetReferenceFact
    pending_dependency_observations: RuntimeObservationProjectionSetReferenceFact
    effective_heads: RuntimeObservationEffectiveHeadSetReferenceFact
    classification_contract_fingerprint: Fingerprint
    physical_policy_fingerprint: Fingerprint
    stable_state_fingerprint: Fingerprint


class RuntimeObservationProjectionPartitionProofFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_partition_proof.v1"]
    source_stable_state_fingerprint: Fingerprint
    active_set_reference: RuntimeObservationProjectionSetReferenceFact
    protected_set_reference: RuntimeObservationProjectionSetReferenceFact
    retained_set_reference: RuntimeObservationProjectionSetReferenceFact
    rewritten_set_reference: RuntimeObservationProjectionSetReferenceFact
    eligible_set_reference: RuntimeObservationProjectionSetReferenceFact
    merkle_partition_proof_reference: ContextArtifactReferenceFact
    partition_contract_fingerprint: Fingerprint
    proof_fingerprint: Fingerprint


class RuntimeObservationRewriteCoverageSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_rewrite_coverage_semantic.v1"]
    direct_member_count: PositiveInt
    transitive_original_observation_count: PositiveInt
    ordered_original_semantic_accumulator: Fingerprint
    ordered_original_causal_accumulator: Fingerprint
    transitive_coverage_root_fingerprint: Fingerprint
    coverage_contract_fingerprint: Fingerprint
    coverage_semantic_fingerprint: Fingerprint


class RuntimeObservationRewriteUnitSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_rewrite_unit_semantic.v1"]
    observation_semantic_id: Fingerprint
    canonical_provider_fragment: ProviderInputTypedFragmentFact
    lowering_lane: Literal["runtime_observation"]
    causal_placement: RuntimeObservationCausalPlacementSemanticFact
    coverage_semantic: RuntimeObservationRewriteCoverageSemanticFact
    unit_semantic_fingerprint: Fingerprint


class RuntimeObservationRewriteUnitAttributionFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_rewrite_unit_attribution.v1"]
    unit_semantic_fingerprint: Fingerprint
    rewritten_source_set_reference: RuntimeObservationProjectionSetReferenceFact
    source_stable_state_fingerprint: Fingerprint
    partition_proof_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


class PreparedRuntimeObservationRewriteProjectionUnitFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_runtime_observation_rewrite_projection_unit.v2"
    ]
    semantic: RuntimeObservationRewriteUnitSemanticFact
    attribution: RuntimeObservationRewriteUnitAttributionFact
    fact_fingerprint: Fingerprint


class PreparedRuntimeObservationRewriteProjectionReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_runtime_observation_rewrite_projection_reference.v1"
    ]
    unit_count: NonNegativeInt
    ordered_unit_semantic_accumulator: Fingerprint
    ordered_causal_placement_accumulator: Fingerprint
    root_artifact_reference: ContextArtifactReferenceFact | None
    projection_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class RuntimeObservationProjectionRewriteFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_rewrite.v3"]
    rewrite_id: str
    parent_long_horizon_rewrite_event_reference: ContextEventReferenceFact
    source_stable_state: RuntimeObservationProjectionStableStateFact
    partition_proof: RuntimeObservationProjectionPartitionProofFact
    prepared_replacement_projection: (
        PreparedRuntimeObservationRewriteProjectionReferenceFact
    )
    resulting_effective_heads: RuntimeObservationEffectiveHeadSetReferenceFact
    coverage_lineage_contract_fingerprint: Fingerprint
    unified_ordered_projection_contract_fingerprint: Fingerprint
    resulting_ordered_provider_projection_fingerprint: Fingerprint
    physical_policy_fingerprint: Fingerprint
    rewrite_policy_id: str
    rewrite_policy_version: str
    rewrite_policy_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint
```

第4.1.1节的causal placement是原始observation首次append时已经committed的semantic，不是rewrite renderer的输出。Rewrite unit的placement必须由被覆盖members的committed placements与coverage contract唯一归约；placement fingerprint覆盖完整nested node identity、phase、scope、source occurrence和intra-boundary order。

`RuntimeObservationRewriteUnitSemanticFact` 只保存最终provider fragment、causal placement与 `RuntimeObservationRewriteCoverageSemanticFact`。Coverage semantic由original-observation count、ordered semantic/causal accumulators及transitive coverage root组成；它不得嵌入set node reference、artifact locator、page layout、partition proof或source stable-state reference。改变Merkle fanout、page boundary、artifact ID或physical policy而保持相同coverage/content/placement时，unit semantic fingerprint必须保持不变。

`RuntimeObservationRewriteUnitAttributionFact` 才保存rewritten set reference与proof/stable-state joins。Central validator必须证明其 `unit_semantic_fingerprint` 等于nested semantic，并从rewritten set的direct members递归展开已有rewrite units的coverage，重算同一transitive coverage semantic；planner不能自报coverage root。

`RuntimeObservationProjectionRewriteFact.source_stable_state` 是完整但event-safe bounded的durable carrier，不再只是hash。Validator必须逐字段证明：

```text
partition_proof.source_stable_state_fingerprint
    == source_stable_state.stable_state_fingerprint

partition_proof.active/protected/eligible references
    == source_stable_state对应set references

每个rewrite-unit attribution.source_stable_state_fingerprint
    == source_stable_state.stable_state_fingerprint

每个rewrite-unit attribution.partition_proof_fingerprint
    == partition_proof.proof_fingerprint
```

Stable state自身只含bounded roots/counts/accumulators与contract identities；其root artifacts仍必须先FULL并由session-owned artifact service负责confirmation、recovery与GC pin。

Stable-state reducer的输入domain只包含registered `pulsara_runtime_observation` units及既有rewrite projection units。`pulsara_human_input`、`pulsara_runtime_request`、root/tool units与canonical transcript units若进入active/protected/eligible set，必须立即判定contract mismatch；runtime request由transcript或invocation owner独立管理。

`RuntimeObservationProjectionRewriteFact` 不新建独立的 live rollover reason或第二 writer。它是 `EXPLICIT_LONG_HORIZON_REWRITE` authority branch的 required nested fact，由已 FULL的 Long-Horizon rewrite/compaction event授权。Partition proof、set nodes与prepared projection的所有content-addressed artifacts必须先经 session-owned artifact service得到 FULL confirmation，然后才能构造 stable rollover candidate。

LLMRuntime继续是 ModelStart唯一 writer。当 rewrite实际用于下一次 call时，以下内容作为同一 stable lifecycle batch提交：

```text
old generation close
+ rollover resolved(EXPLICIT_LONG_HORIZON_REWRITE, nested observation rewrite)
+ new generation start/root
+ rewritten initial append
+ ModelStart
```

NONE保留同一 rollover candidate和artifact reference；FULL后 reducer一次性安装 resulting effective heads；UNKNOWN/PARTIAL latch，不得重新调用 rewrite renderer。Cancellation/close必须由现有 preparation/reconciliation owner drain该 candidate。Artifact confirmed missing/hash conflict属于 `authority_untrusted`，不得 fallback到旧 observation完整重放。

Rewrite必须证明：

- stable state由唯一observation reducer在同一authority high-water下冻结，planner不得自报protected/eligible集合；
- active observations被 `protected + retained + rewritten` 完整、互斥分区，且rewritten是eligible的子集；
- Merkle partition proof从active root、各partition root、count和ordered accumulators重算；
- durable ledger中的原 observation facts不删除、不改写；
- 所有 replacement source的最新 effective head出现在 resulting set；
- source/result head lineage连续；
- protected units不属于 rewritten set；
- replacement projection完整表达被移除历史对当前模型仍必要的语义；
- 每个replacement unit持有generation-neutral causal placement，不用旧vector ordinal作为新generation的semantic anchor；
- 每个replacement unit只能覆盖causally contiguous的source subset，其predecessor/phase/intra-boundary order必须从member placements唯一派生；
- 跨current-user、assistant/tool-call、tool-result或run-terminal boundary的source必须拆成多个replacement units；
- rewrite只能作为 confirmed Long-Horizon rewrite的nested authority提交；
- 不得恢复独立 `AUXILIARY_FRAME_REBASE` reason。

Rewrite validator还必须证明 `source_stable_state.effective_heads -> resulting_effective_heads` 的唯一 transition：未在 rewritten set中的head逐字节保留；被改写的head必须由replacement projection或exact copied unit承接；不得仅根据fingerprint自报最新effective content。

该证明必须在 durable rollover commit reducer 中实际执行，而不是仅由 planner
预检查。Reducer以 old committed core、old lifecycle reducer state、new initial append、
内嵌 stable state/partition proof、resulting core 与 resulting observation units 为输入，
重新构造 canonical active/protected/eligible sets、partition roots、coverage lineage和
effective-head set。任一 drop、重复、错误成员、错误 resulting head 或 proof/artifact join
漂移都使整个 rollover batch fail closed；`resulting_effective_heads` 不是仅供 Inspector
展示的自报字段。

`runtime_observation_rewrite_projection` 是immutable committed occurrence，但不是永久不可再压缩的terminal object。后续confirmed Long-Horizon rewrite可以把一个或多个既有rewrite units纳入新的rewritten set；新coverage必须递归展开并继承其transitive original-observation coverage，禁止coverage overlap、drop或double count。旧stable-state/proof/projection artifacts在successor rewrite及其rollover batch确认FULL前持续pinned；FULL fold后以同一reducer transition将reachable roots切换到successor，UNKNOWN/PARTIAL继续保留旧roots并latch。这样rewrite projection不会成为另一条永久增长的append-only历史。

旧generation vector ordinal只属于物理source-membership attribution。最终placement只能由唯一ordered-provider-projection validator生成；该validator共同合并canonical transcript、transcript compaction replacement、runtime-observation replacement、retained/protected observations与current transcript tail。

```text
generation root/tools
+ canonical transcript projection
+ transcript compaction replacement units
+ runtime-observation replacement units
+ retained/protected runtime observations
+ current transcript tail
-> one ordered causal projection
-> one provider vector / ModelStart reference
```

Validator必须联合校验current-user placement、assistant reply、tool-call/result pairing、lifecycle source event、clock invocation boundary与rewrite-unit anchor。Runtime-observation rewrite不得把自己无条件追加到initial append尾部。

V1保护矩阵：

```text
always protect:
    每个 replacement source的最新 effective head
    current run/window内的 runtime observations
    最新 clock observation
    未闭合 lifecycle/terminal process observation
    active skill / active plan / active rollout status
    pending tool/control/continuation引用的 observation

eligible after proof:
    superseded memory/capability/workspace-skill snapshots
    旧 clock observations
    已闭合且已被replacement projection表达的 lifecycle observations
    已关闭 workflow/skill/rollout observations
```

Event-safe rewrite fact只嵌入bounded root references、counts和accumulators，不嵌入完整member/protected tuples。Leaf/internal pages的大小、fanout、maximum height、single-operation changed nodes、artifact batch count和deadline由 `RuntimeObservationProjectionPhysicalPolicyFact` 冻结并由doctor证明可行。超限时进入typed planning failure，不得fallback到无界event payload。

Context budget planner必须同时测量 transcript、tools、root和runtime-observation projection。Observation pressure可以触发 Long-Horizon planning，但不能直接触发无证据 rewrite。

---

## 10. ProviderInput 与 Reducer Join

### 10.1 Committed source head

Generation core不能只保存 fingerprint。Rollover可能发生在当前 compile未再选中该 source时，因此 reducer必须能从已提交状态中恢复上一份 exact effective snapshot，不得重新渲染当前 source。

同时，exact append event reference不得进入 generation semantic core，否则会形成：

```text
append event payload
-> resulting generation core
-> source head
-> append event reference
-> append event payload
```

因此 V2物理拆成三层：

1. semantic-only unit document identity与committed head，进入generation core；
2. inline/artifact hydration、source authority与vector placement attribution，全部位于core之外；
3. reducer/Inspector构造的joined source head。

```python
class ProviderInputUnitSemanticMaterializationFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_semantic_materialization.v1"]
    unit_semantic: ProviderInputUnitSemanticFact
    canonical_provider_fragment: ProviderInputTypedFragmentFact
    canonical_wire_utf8_sha256: Fingerprint
    canonical_wire_utf8_bytes: NonNegativeInt
    canonical_content_digest: Fingerprint
    lowering_contract_fingerprint: Fingerprint
    wire_codec_contract_fingerprint: Fingerprint
    semantic_materialization_fingerprint: Fingerprint


class ProviderInputUnitSemanticDocumentIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_semantic_document_identity.v1"]
    document_schema_version: str
    document_contract_fingerprint: Fingerprint
    semantic_materialization_fingerprint: Fingerprint
    canonical_document_sha256: Fingerprint
    canonical_document_bytes: NonNegativeInt
    canonical_wire_utf8_sha256: Fingerprint
    canonical_wire_utf8_bytes: NonNegativeInt
    document_semantic_fingerprint: Fingerprint


class EffectiveProviderSourceSemanticSnapshotFact(FrozenFactBase):
    schema_version: Literal["effective_provider_source_semantic_snapshot.v1"]
    source_id: ContextSourceId
    source_instance_id: str
    lifecycle_class: Literal["replacement_snapshot"]
    committed_revision: NonNegativeInt
    observation_semantic_id: Fingerprint
    predecessor_observation_semantic_id: Fingerprint | None
    snapshot_semantic_fingerprint: Fingerprint
    canonical_wire_semantic_fingerprint: Fingerprint
    wire_semantic: RuntimeObservationWireSemanticFact
    causal_placement: RuntimeObservationCausalPlacementSemanticFact
    unit_causal_semantic_fingerprint: Fingerprint
    effective_status: Literal[
        "active_snapshot",
        "explicit_empty_snapshot",
        "source_closed",
    ]
    unit_document_identity: ProviderInputUnitSemanticDocumentIdentityFact
    semantic_snapshot_fingerprint: Fingerprint


class CommittedRuntimeObservationSemanticHeadFact(FrozenFactBase):
    schema_version: Literal["committed_runtime_observation_semantic_head.v1"]
    effective_snapshot: EffectiveProviderSourceSemanticSnapshotFact
    semantic_head_fingerprint: Fingerprint


class InlineProviderInputUnitHydrationAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "inline_provider_input_unit_hydration_attribution.v1"
    ]
    hydration_kind: Literal["inline"]
    semantic_document_identity_fingerprint: Fingerprint
    semantic_materialization: ProviderInputUnitSemanticMaterializationFact
    hydration_fact_fingerprint: Fingerprint


class ArtifactProviderInputUnitHydrationAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "artifact_provider_input_unit_hydration_attribution.v1"
    ]
    hydration_kind: Literal["artifact"]
    semantic_document_identity_fingerprint: Fingerprint
    artifact_reference: ContextArtifactReferenceFact
    artifact_document_contract_fingerprint: Fingerprint
    observed_document_sha256: Fingerprint
    observed_document_bytes: NonNegativeInt
    hydrate_proof_fingerprint: Fingerprint
    hydration_fact_fingerprint: Fingerprint


ProviderInputUnitHydrationAttributionFact: TypeAlias = Annotated[
    InlineProviderInputUnitHydrationAttributionFact
    | ArtifactProviderInputUnitHydrationAttributionFact,
    Field(discriminator="hydration_kind"),
]


class ProviderInputUnitPlacementAttributionFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_placement_attribution.v1"]
    semantic_head_fingerprint: Fingerprint
    hydration_attribution: ProviderInputUnitHydrationAttributionFact
    origin_generation_id: str
    committed_append_event_reference: ContextEventReferenceFact
    committed_append_index: NonNegativeInt
    committed_vector_root_reference: ProviderInputUnitVectorRootReferenceFact
    vector_ordinal: NonNegativeInt
    source_event_references: tuple[ContextEventReferenceFact, ...]
    source_artifact_references: tuple[ContextArtifactReferenceFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...]
    closure_event_reference: ContextEventReferenceFact | None
    fact_fingerprint: Fingerprint


class CommittedRuntimeObservationSourceHeadFact(FrozenFactBase):
    schema_version: Literal["committed_runtime_observation_source_head.v1"]
    semantic_head: CommittedRuntimeObservationSemanticHeadFact
    placement_attribution: ProviderInputUnitPlacementAttributionFact
    fact_fingerprint: Fingerprint
```

Central factory必须重算以下 join：

- semantic materialization从typed provider fragment、unit semantic、lowering contract和第4节最终wire bytes重算；
- semantic document identity只覆盖canonical document digest/bytes、wire digest/bytes和semantic/materialization contract；
- semantic core中不得出现event refs、authority horizons、replay bindings、append/vector placement、concrete artifact locator或GC ownership；
- inline hydration从nested semantic materialization重算document identity；
- artifact hydration必须读取concrete locator，验证artifact bytes/contract，再得到完全相同的document identity；
- `canonical_wire_utf8_sha256` 必须对第4节central codec的完整最终wire bytes计算；
- runtime-observation snapshot中的wire semantic与causal placement必须逐字段等于首次committed `PreparedRuntimeObservationProviderUnitFact`，并重算同一unit causal fingerprint；
- `observation_semantic_id`、predecessor、revision和 effective status必须满足 lifecycle matrix；
- `source_closed` 必须有 closure event，其他状态必须没有；
- placement中的 append index/vector ordinal必须在 referenced append event与vector root中唯一定位该 exact unit；
- source refs、artifact refs、horizons和replay bindings必须与append/vector leaf中的原unit attribution完全相等。

Inline/artifact选择由generation-frozen physical carrier policy决定，但不改变semantic document identity。超过inline bound的合法unit必须在preparation阶段先写入content-addressed artifact；semantic core仍只保存digest/document identity，concrete artifact reference只进入hydration attribution。不得因为head DTO上限拒绝token budget内的合法snapshot。Artifact只要仍被open generation、effective head、pending rollover或rewrite proof引用，就必须进入attribution-owned reachable/GC root set。

Generation committed core只保存有序的 `CommittedRuntimeObservationSemanticHeadFact`；`ProviderInputGenerationAttributionStateFact`保存与之逐项join的hydration、source authority与placement attribution。`CommittedRuntimeObservationSourceHeadFact`是reducer/Inspector对两者的typed joined view，不再参与provider prefix fingerprint。

FULL fold的唯一顺序为：

```text
commit append + ModelStart
-> confirm FULL
-> fold semantic head into committed core
-> derive exact append event reference and vector placement
-> install attribution join
-> publish joined source head
```

NONE不推进任何一层；UNKNOWN/PARTIAL不得生成猜测attribution，必须latch/reconcile。Restart从exact append event、vector leaf与inline/artifact hydration attribution恢复joined head，并重算semantic document identity；不依赖最新source重新渲染。

Memory/capability no-op比较只读semantic head中的snapshot semantic identity；不得读process-local lifecycle cache、最新event ID或placement attribution代替。Rollover必须通过joined head的hydration attribution恢复与semantic document identity完全一致的unit，不得重新调用ContextSource renderer。

Artifact missing、hash conflict、codec binding无法 rebind或append-event/vector-placement不一致均属于 `authority_untrusted`，不得当作 source absent或普通 cache miss。

V2会物理删除当前只保存 semantic fingerprint和append index的 `ProviderInputCommittedSourceHeadFact`，不提供 fallback。

### 10.2 Historical source disposition

Compiler在 allocation 前必须对“当前 source authority + generation 中全部 historical
replacement heads”冻结一张完整、按 `(source_id, source_instance_id)` 排序且唯一的
`ContextSourceDispositionFact`：

```text
retain
replace
explicit_empty
terminal
rewrite_required
```

```python
class ContextSourceDispositionFact(FrozenFactBase):
    schema_version: Literal["context_source_disposition.v2"]
    source_id: ContextSourceId
    source_instance_id: str
    disposition: Literal[
        "retain", "replace", "explicit_empty", "terminal", "rewrite_required"
    ]
    reason: Literal[
        "candidate_available",
        "projection_failed",
        "allocation_omitted",
        "source_terminal",
        "source_explicit_empty",
        "no_new_fact",
        "semantic_noop",
    ]
    candidate_semantic_fingerprint: Fingerprint | None
    candidate_payload_semantic_fingerprint: Fingerprint | None
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    disposition_fingerprint: Fingerprint


class ProviderSourceDispositionRewriteAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "provider_source_disposition_rewrite_authority.v1"
    ]
    authority_kind: Literal["source_disposition_rewrite"]
    predecessor_generation_id: str
    predecessor_core_state_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    rewrite_dispositions: tuple[ContextSourceDispositionFact, ...]
    rewritten_predecessor_source_head_fingerprints: tuple[Fingerprint, ...]
    authority_fingerprint: Fingerprint
```

其唯一解释为：

- projection 暂时失败但存在 committed head：`retain/projection_failed`；
- 本轮没有新事实且 registry 的 absence semantics 是 retain：`retain/no_new_fact`；
- candidate 与 effective head 的 model-visible semantic相同：`retain/semantic_noop`；
- 新完整 snapshot入选：`replace/candidate_available`；
- memory/skill等集合确认为空：`explicit_empty/source_explicit_empty`；
- plan/rollout/MCP等 owner确认终态：`terminal/source_terminal`；
- changed optional replacement candidate 被本轮 allocation省略、而旧 head又不能继续代表
  当前 truth：`rewrite_required/allocation_omitted`。

Absence本身不得表达 empty、terminal或删除。Planner必须拒绝任何没有 disposition 的
historical head，也不得像旧实现一样无条件复制全部 head。`rewrite_required` 只能触发
`SOURCE_DISPOSITION_REWRITE_REQUIRED` typed rollover；authority精确保存 dispositions、
predecessor generation/core、ordered projection identity与被移除 predecessor-head
fingerprints。Fresh generation上从未提交过的 omitted source不产生 phantom rollover。

Rollover reducer必须以 old core 与 new initial append重新验证上述集合转换；普通 append
不得删除 head，typed rollover不得保留被明确 rewrite 的旧 head。这样 memory failure、
plan exit、active-skill empty与预算省略都拥有显式 durable语义，而不由 source absence猜测。

### 10.3 Attribution-only refresh

如果新 projection拥有更新 event refs/high-water，但 model-visible semantic snapshot完全相同：

- 不追加 provider unit；
- 不改变 prefix fingerprint；
- 不改变 provider semantic head revision；
- 不触发 ModelStart preparation；
- 如确需审计，可写 bounded operational attribution supplement；
- supplement不得进入 provider semantic identity或rollover判断。

### 10.4 Stable candidate/retry

Clock、memory replacement、capability replacement、lifecycle observation一旦进入 prepared append candidate，必须冻结：

- exact semantic payload；
- exact wire envelope；
- append ordinal；
- source attribution；
- ModelStart join fingerprint。

NONE重试同一 candidate；FULL fold后推进 source head；UNKNOWN/PARTIAL latch并由 session-owned reconciliation owner接管。

每个 one-shot generation也必须在其 initial append中包含恰好一个 runtime clock。
其 stable occurrence由 `(operation_kind, operation_id, attempt_index, observed_at_utc)`
冻结；clock位于 runtime request之前，retry/reopen复用同一 prepared carrier，禁止重新读取
当前时钟。Governance、reflection、direct call、window compaction与summarizer不得以
`clock_head=None` 启动一个合法 ModelStart。

---

## 11. Implementation Phases

### ROAC0：实验 fixture、DTO 与 guards（additive）

- 将本次真实 PR4 Call 1/2脱敏 payload形状固化为 deterministic fixture；
- 固化 exact/no-clock/user-clock三组缓存实验说明和结果；
- 新增human/runtime-request/runtime-observation三分user-carrier protocol、producer-aware kind/codec contract、clock/memory/lifecycle DTO；
- 在next-schema module/fixture中新增穷尽 ContextSource lifecycle registry与 `PLAN_STATUS` / `PLAN_GUIDANCE`，暂不改变production enum；
- 新增原始observation wire/causal-placement/source-attribution三层、semantic document identity、hydration/placement attribution、observation stable-state/partition/rewrite coverage DTO；
- 新增 adapter-final privileged-role scanner与protocol validator；
- production行为暂不切换。

Gate：

- DTO fingerprint/invariant测试；
- fixture证明 user-clock保持 old messages strict prefix；
- guard可识别 mid-history system/developer hint；
- V2 fixture registry与V2 source enum set equality，context-source transitions与kind producers双向对应，derived producers独立绑定；
- joined source head可从semantic document identity和inline/artifact hydration attribution恢复，不依赖当前ContextSource；
- 当前 production违规以 shadow diagnostic报告，不先 fail closed。

### ROAC1：Human/Runtime Request/Runtime Observation carrier 全producer垂直 hard cut

- 所有human-authored user message改用 `pulsara_human_input` typed envelope；
- 所有runtime-authored current task/request改用 `pulsara_runtime_request` typed envelope并保留独立 `MessageRole.RUNTIME_REQUEST`；
- subagent task/current-run task、compaction、window compaction、governance、reflection与summarizer请求全部绑定closed V1 request-kind/owner matrix；
- Chat `RUNTIME_OBSERVATION -> user`；
- Responses `RUNTIME_OBSERVATION -> user`；
- Chat/Responses `RUNTIME_REQUEST -> user`，禁止降为human input或observation；
- generation root安装human/runtime-request/runtime-observation联合interpretation fragment并绑定user-carrier/request-kind/observation-kind/producer/codec fingerprint；
- memory、capability prose、active/workspace skill、plan、recovery、rollout、subagent与MCP diagnostic全部迁移到central runtime-observation envelope；
- compiler对全部historical replacement heads冻结 `retain | replace | explicit_empty | terminal | rewrite_required` disposition；projection failure、plan terminal与allocation omission禁止再由absence表达；
- production enum在同一hard cut中将旧 `PLAN` 拆为 `PLAN_STATUS` / `PLAN_GUIDANCE`；
- 删除全部legacy `pulsara_context` wrapper、generic runtime-user wrapper及对应lowering helper；
- producer-aware kind registry在本阶段切为production authoritative；所有已迁移producer必须携带精确kind/producer/payload/codec binding，不能等待ROAC3再补；
- lifecycle transcript semantic从 `system`迁移为 typed runtime observation；
- 一次性迁移并删除 transcript reducer、legacy transcript helper、recovery、terminal/teardown中所有旧 lifecycle producer；
- 明确使用原 `RunEndEvent` / `TerminalProcessCompletedEvent` / registered source event作为唯一 durable owner，不新增 observation event；
- clock从 `append_revision/complete_snapshot`改为 append-once call observation，删除 latest-wins envelope；
- clock stable candidate绑定 discriminated invocation owner与 resolved model call；
- host/subagent/direct subsystem/compaction/governance/reflection/summarizer所有已支持 owner走同一 candidate factory；one-shot initial append也必须提交一个retry-stable clock；
- 每个原始runtime observation在首次append时提交generation-neutral causal placement；append event/vector leaf/stable reducer逐层join同一placement；
- memory producer在本阶段hard cut为typed `recalled_memory` entries/empty；`working_context|mixed` summary不得进入provider source，也不得解析字符串猜测拆分；
- retry/reopen复用首次 frozen time，不读取新时钟；
- 保留 root system唯一性；
- adapter pre-send guard以 Chat/Responses精确矩阵切 production fail closed；
- 每个adapter-final user item恰好是一个human-input、runtime-request或runtime-observation envelope；
- unknown envelope/kind/producer/contract在adapter前fail closed。
- 同步更新 `contracts/LLM_TRANSPORT_CONTRACT.zh.md`、`contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md` 与 `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`；代码与contract必须同PR合入。

该阶段需要 schema hard cut；不兼容旧 transcript/provider-input projection。测试数据库与本地 PostgreSQL允许重置，不提供旧 schema fallback。

ROAC1是不可再拆分部署的垂直migration：三类user carrier、全部non-root runtime producer、root interpretation、request/kind registry、adapter lowering与pre-send guard必须在同一合入点切换。实现可以在一个工作分支内拆小commit，但任何中间commit都不得单独发布；ROAC2只优化replacement semantic identity，ROAC3只收紧transition/closure，不得再承担遗漏carrier的迁移。

Gate：不存在“新protocol/guard + 旧runtime-owned user carrier”的production组合；legacy wrapper/lowering grep为零。Human、runtime request、memory、capability、skill、plan、recovery、rollout、subagent、MCP、lifecycle与clock fixture全部使用central carrier。Subagent task不会被归因为human；one-shot request不会进入observation stable state。Same-generation tool loop中每个clock只追加一次，全部preceding wire messages保持byte/semantic-identical。Adapter pre-send必须从 `LLMMessage` 持有的typed semantic fact + binding重新编码并逐字节比较，任意只伪造合法SHA形状的wire ID均被拒绝。

### ROAC2：Memory/capability semantic no-op 与 exact head

- 建立 normalized model-visible snapshot factory；
- memory比较 committed effective semantic head；
- 相同内容即使 event ID不同也不追加；
- 不同内容追加完整 replacement snapshot；
- 实施 explicit empty replacement；
- capability prose应用同一规则；
- active-skill aggregate snapshot应用同一规则；非空到空必须提交 explicit-empty replacement，不采用append-once deactivation旁路；
- 只接纳typed recalled-memory entries；明确拒绝旧 `working_context|mixed` provider projection，不创建 `RecentWorkingContextSource`；
- FULL fold后安装semantic document identity与hydration/placement attribution；
- rollover从joined head hydrate exact semantic unit，不调用当前source renderer。
- 同步更新ProviderInput source-head contract、`contracts/ARTIFACT_STORE_CONTRACT.zh.md` 与 `contracts/RECOVERY_CONTRACT.zh.md`。

Gate：真实 PR4式连续 projection中，正文相同的 memory事件不增加 provider unit count；memory候选暂时缺席后触发tool/root rollover，仍能byte-identical恢复上一 effective snapshot。Memory projection failure显式retain旧head；changed candidate被预算省略时只允许typed source-disposition rollover，既不能偷偷保留旧正文，也不能越过compiler allocation发送新正文。

### ROAC3：穷尽 producer-transition lifecycle 与 closure hard cut

- 在ROAC1已authoritative的producer-aware kind registry之上，启用穷尽ContextSource lifecycle registry与transition/closure交叉验证；
- active/workspace skill、plan、recovery、rollout、subagent和MCP diagnostic的每个transition只能使用其registered kind/lifecycle；
- memory/active-skill empty、plan/rollout/MCP closure等 typed transition全量接线；
- source absence 只表示 no-new-fact 或 retain-effective-head；
- absence 不再生成 removed-source set；
- root source 与 tool catalog 保持独立 owner。
- generation store维护唯一增量observation lifecycle reducer state；它从registered transition推进effective heads、latest clock、closed workflow/run/child scopes与pending dependencies，Long-Horizon planner只消费其冻结snapshot。
- 同步更新ContextSource lifecycle长期contract与composition-root registry contract。

Gate：ContextSource set-equality只检查context_source producer；transcript lifecycle与Long-Horizon rewrite各自绑定derived producer contract。每个source transition恰好一条kind binding。Plan terminal、subagent result delivery、run/workflow terminal能把对应 causal observations从open转为eligible；active/effective head、current run与pending dependency仍protected。该阶段不引入新rewrite authority，旧absence-based rebase触发已不再可达。

### ROAC4：物理删除 auxiliary rebase 与接入 observation rewrite

- 删除 reason/authority/planner/store/recovery/Inspector 分支；
- rollover allowlist收紧；
- 将 runtime observations纳入 Long-Horizon rewrite domain，实施 protected ranges/effective heads/replacement projection强 join；
- `RuntimeObservationProjectionRewriteFact` 只通过 explicit Long-Horizon rollover authority持久化；
- rewrite fact直接携带bounded source stable state；rewrite unit semantic只携带transitive coverage semantic，physical set/proof进入attribution；
- 允许后续rewrite覆盖既有rewrite projection，并实施transitive coverage与artifact pin/GC transition；
- Long-Horizon rewrite成为压力重建唯一正常 authority。
- rollover commit reducer重新构造source stable state、partition proof、coverage与resulting effective heads；proof只在planner中成立而未通过fold验证视为contract failure。
- 同步更新Long-Horizon、compaction continuity与EventLog rollover/rewrite contract。

Gate：production `auxiliary_frame_rebase` grep为零，repair/archived docs不计；长 observation history在保护当前head/run/lifecycle的前提下可经confirmed rewrite有界收缩；篡改partition root、member coverage或resulting head的rollover batch均被reducer拒绝。

### ROAC5：Inspector、dogfood与最终contract一致性审计

- Inspector展示 root privileged carrier、runtime-user observations、source semantic heads、no-op count和rollover reason；
- 对ROAC1–ROAC4已经随owner代码更新的长期contracts做最终cross-document一致性检查；本阶段不得延迟补写已经生效的production contract；
- 跑 deterministic prefix fixture；
- 跑真实 long PR4；
- 记录每次 model call input/cached tokens、generation和adapter-final prefix relation。

不得仅凭单次 cache ratio宣称完成；必须同时证明 semantic invariants和provider usage改善。

#### ROAC5 实施验证记录（2026-07-20）

本轮 hard cut 完成后，先执行完整 non-real suite，再只执行用户指定的 long PR4 real-LLM dogfood。全量 real-LLM suite 与其他 dogfood 开关未在本轮重跑；这是一项明确的验证范围约束，不应被表述成“全量 real-LLM 已通过”。

确定性与静态 gate：

- non-real suite 单次执行 `.venv/bin/pytest -q -m "not real_llm and not retrieval_live"`，结果为 `2298 passed, 3 skipped, 76 deselected in 1056.42s`，无失败；
- `git diff --check` 通过；
- `python -m compileall -q src tests` 通过；
- `ruff check src tests` 通过；
- production Python 中 `auxiliary_frame_rebase` 与 legacy `<pulsara_context` carrier grep 为零；唯一 root-system 构造仍由 compiler/provider-input planner 持有。

long PR4：

- 用例：`tests/test_real_llm_dogfood_pr4.py::test_real_pr4_dogfood_llm_user_long_session`；
- 结果：`1 passed in 87.23s`；
- model calls：12；
- generation：全程同一个 generation，revision 从 1 连续推进到 12；
- rollover：0；
- 相邻 11 组 provider-input vector 全部满足 old-units 是 new-units 的严格前缀；
- provider 12/12 次均报告正 cached input；
- 总 input tokens：`131169`；
- 总 cached input tokens：`117504`；
- 聚合 reported cache ratio：`0.895821`。

逐 call 观测如下。`prefix` 表示相对上一 call 的 same-generation strict-prefix 校验；首个 call 没有前驱，因此记为 `n/a`。

| revision | units | input tokens | cached tokens | cache ratio | provider latency (s) | prefix |
|---:|---:|---:|---:|---:|---:|:---:|
| 1 | 33 | 6762 | 640 | 0.094647 | 1.611459 | n/a |
| 2 | 37 | 7398 | 6784 | 0.917005 | 2.283569 | yes |
| 3 | 40 | 7805 | 7296 | 0.934785 | 4.084173 | yes |
| 4 | 43 | 8545 | 8192 | 0.958689 | 2.342472 | yes |
| 5 | 46 | 9028 | 8704 | 0.964112 | 2.646383 | yes |
| 6 | 51 | 11017 | 8960 | 0.813289 | 2.176964 | yes |
| 7 | 54 | 11998 | 11136 | 0.928155 | 3.070366 | yes |
| 8 | 59 | 12782 | 11904 | 0.931310 | 2.375270 | yes |
| 9 | 62 | 13029 | 12672 | 0.972600 | 2.756370 | yes |
| 10 | 67 | 13808 | 13184 | 0.954809 | 2.133708 | yes |
| 11 | 70 | 14206 | 13952 | 0.982120 | 1.361913 | yes |
| 12 | 75 | 14791 | 14080 | 0.951930 | 1.643291 | yes |

该结果同时证明了两件不同的事：本地 append-only continuity invariant 成立，且本次真实 provider trajectory 确实复用了旧 prefix。首 call 的低比例与后续新增 suffix 属于正常现象；provider cache 仍是 best effort，不能把本次比例升级为所有调用的稳定 SLA。

---

## 12. 测试矩阵

### 12.1 Role/lowering

- Chat adapter-final payload恰好一个 system，且只能在 `messages[0]`；
- Chat `messages[0]` content/fingerprint与generation root精确相等；
- Chat `messages[1:]` 中任意 system/developer都立即 fail closed；
- Responses instructions存在且与generation root精确相等；
- Responses input中任意 system/developer都立即 fail closed；
- human-authored text只能出现在 `pulsara_human_input.text` typed field；
- adapter-final的每个user-role item恰好解码为human-input、runtime-request或runtime-observation三者之一；
- raw human user content在Chat和Responses均fail closed；
- human text中的完整runtime-observation JSON被转义在text字段内，不能伪造top-level authority；
- subagent task/current-run task使用runtime-request，不能产生human attribution；
- compaction/governance/reflection/summarizer one-shot input使用runtime-request，不能进入observation stable state；
- runtime request不能伪装成runtime observation，也不能被observation rewrite选择；
- child canonical transcript中的subagent task保留runtime-request semantic与owner attribution；
- runtime clock wire role是 user；
- recovery note wire role是 user；
- terminal completion note wire role是 user；
- Responses runtime observation不再是 developer；
- runtime observation不能进入 human-user transcript attribution。

### 12.2 Protocol/trust/codec

- 每个 production observation kind在kind registry中恰好一条binding；
- 每个production runtime-request kind在request registry中恰好一条binding，owner/lifecycle/persistence矩阵精确匹配；
- `ACTIVE_SKILL` snapshot-update/explicit-empty分别绑定同一个closed snapshot kind；
- `RECOVERY` context-source guidance与transcript-derived lifecycle使用不同producer branch；
- Long-Horizon rewrite kind不伪造ContextSource ID；
- context-source transition bindings与kind producer matrix可双向重算；
- unknown kind、unknown protocol version、unknown payload schema在adapter前fail closed；
- `runtime_fact`不能声明指令权限；`runtime_guidance`不能覆盖root policy；
- 包含 `</runtime-observation>`、伪造header、JSON braces、control characters的payload只能成为typed string内容，不能逸出外层object；
- canonical codec重复编码byte-identical，decode -> validate -> encode为fixed point；
- 修改任意最终wire-visible byte必须改变unit semantic fingerprint；
- 仅修改durable event reference不得改变observation semantic ID或wire bytes；
- protocol/kind/codec/root interpretation fingerprint变化必须触发typed compatibility rollover。
- human/request/observation任一outer envelope互换必须改变wire fingerprint并由owner validator拒绝错误attribution。
- 将runtime-observation的semantic ID改为另一个格式合法的SHA时，shape validator与typed semantic rebind联合拒绝；公开构造的wire string不能自报runtime authority。

#### 12.2.1 原始observation causal placement

- clock、memory replacement、skill、lifecycle等每个原始observation首次prepared时都包含wire semantic、generation-neutral causal placement与source attribution；
- unit causal fingerprint只覆盖wire semantic与causal placement，不包含event/artifact/vector placement；
- FULL append event、vector leaf、committed reducer snapshot中的placement逐字段相等；
- NONE不安装placement，UNKNOWN/PARTIAL不允许rewrite或restart猜测placement；
- 仅改变append index、vector ordinal、artifact locator不得改变causal placement semantic；
- rollover、restart、transcript compaction前后，同一未rewrite observation保持同一causal placement；
- rewrite unit placement只能由member committed placements归约，删除首次placement或出现多个合法anchor时fail closed。

### 12.3 Clock

- 每个 model call恰好一个 frozen clock observation；
- retry复用同一 observed timestamp；
- next call只在尾部追加新 clock；
- clock正文/role变化不会改写旧 unit；
- host、subagent、direct subsystem、compaction、governance和summarizer owner均能生成稳定唯一键；
- reflection与所有one-shot owner的initial append也恰好包含一个clock，且位于runtime request之前；
- 不属于Host run的clock不需要伪造run ID或model-call index；
- 100-call fixture证明无 auxiliary rebase。

### 12.4 Memory

- 相同正文、不同 ProjectionReady event ID -> no append；
- 相同semantic snapshot由新event重新确认 -> observation semantic ID与wire bytes不变；
- 相同 items、不同查询返回顺序 -> no append；
- attribution/high-water变化、semantic相同 -> no append；
- `[A,B] -> [A,C]` -> 一份完整 replacement；
- `A -> B -> A` 生成新lineage semantic ID，但payload A内容fingerprint保持一致；
- non-empty -> empty -> 显式 empty replacement；
- empty -> empty -> no-op；
- `working_context|mixed` ProjectionReady summary不得进入memory provider snapshot；
- 禁止从legacy summary prose解析memory/working partition；
- recent assistant/tool facts只由canonical transcript提供，不出现重复RecentWorkingContext source；
- restart后从semantic head + hydration attribution恢复joined head并得到相同判定；
- memory candidate缺席后触发tool/root rollover -> 从head恢复的memory wire byte-identical；
- semantic core不含event/artifact/horizon/replay/GC fields；
- inline与artifact attribution hydrate后生成相同semantic document/wire fingerprints；
- 只改变artifact locator、append event、vector ordinal或authority horizon不改变semantic head fingerprint；
- artifact missing/hash conflict/placement mismatch -> authority_untrusted，不得当作absence；
- NONE/FULL/UNKNOWN/PARTIAL保持 stable candidate ownership。

### 12.5 Capability

- unchanged prose -> no append；
- changed skill catalog prose -> one full replacement；
- active skill non-empty aggregate -> one full replacement snapshot；
- unchanged active-skill aggregate -> semantic no-op；
- active skill non-empty -> empty -> explicit-empty replacement with exact predecessor；
- tool schema unchanged -> retain generation；
- 26 -> 29 tools -> typed early rollover；
- tool ordering-only drift经 canonicalization后不得误触发 rollover。

### 12.6 Lifecycle

- one RunEnd -> one recovery observation；
- repeated compile不重复生成；
- two different RunEnd events -> two distinct causal observations；
- one TerminalProcessCompleted -> one observation；
- exact event refs与canonical position join；
- source RunEnd/TerminalProcessCompleted event是唯一durable business owner，不存在第二observation event；
- ROAC1 fixture中所有legacy helper/system-role producer已清零；
- no lifecycle observation uses system/developer wire role。

### 12.7 Source lifecycle/rollover

- lifecycle registry source ID set与 `ContextSourceId` 精确相等，无缺失、重复或mixed lifecycle；
- `PLAN_STATUS`与`PLAN_GUIDANCE`的replacement/causal规则不可互换；
- 每个historical replacement head都有且只有一个compile-time disposition；
- memory projection失败 -> retain已提交head；首次失败不创建phantom head；
- plan exit -> typed terminal snapshot，不能以candidate absence保留旧active plan；
- changed replacement candidate被allocation省略 -> typed `SOURCE_DISPOSITION_REWRITE_REQUIRED` rollover；
- 首次出现但被allocation省略的source不触发phantom rollover；
- selected dynamic source absent只按registry的retain/no-new-fact语义处理，不直接触发 rollover；
- old auxiliary unit count/bytes不触发 rollover；
- cache miss不触发 rollover；
- root/tool/compatibility变化触发正确 typed authority；
- Long-Horizon rewrite后允许重建且保留 rewrite proof；
- no standalone auxiliary rebase event/schema。

### 12.8 Observation rewrite

- stable-state reducer在同一authority horizon下冻结active/protected/eligible/open/pending sets；
- `protected + retained + rewritten == active` 且三者互斥，`rewritten <= eligible`；
- 从paged roots/counts/accumulators/Merkle proof可重算完整partition；
- rewrite fact内嵌的bounded source stable state可独立恢复authority horizons、active/protected/eligible/open/pending roots与classification contract；
- partition proof与每个unit attribution逐项join同一source stable-state fingerprint；
- superseded memory/capability/workspace-skill snapshots可被confirmed rewrite收缩；
- current run/window、latest clock、effective replacement heads、unclosed lifecycle与pending continuation始终protected；
- protected unit与rewritten ranges不相交；
- source/result effective-head transition可从exact unit refs重算；
- rollover fold独立重算old core、source stable state、partition roots、coverage、new initial append与resulting core；
- durable raw observation facts在rewrite前后保持不变；
- replacement projection artifact在rollover batch前必须FULL；
- event-safe rewrite payload大小与active-history member count无关，只保存bounded roots/counts/accumulators；
- rewrite unit semantic只包含transitive coverage semantic，不包含set/artifact/page/proof reference；
- 相同coverage/content/placement仅改变page layout、fanout、artifact locator或physical policy时unit semantic fingerprint不变；
- prior rewrite projection可被successor rewrite覆盖，transitive original-observation count/accumulators保持闭合且无overlap/drop/double count；
- successor FULL前旧stable-state/proof/projection artifacts保持pinned，UNKNOWN/PARTIAL不得切换GC roots；
- old vector ordinal只进入source attribution，replacement unit使用generation-neutral causal placement；
- rewrite/transcript compaction/current tail经同一ordered-provider-projection validator合并；
- rewrite不能跨current-user、assistant/tool-call、tool-result或terminal boundary错位合并；
- rewrite NONE复用stable candidate，UNKNOWN/PARTIAL latch；
- 单纯observation增长、absence或cache miss不能自行构造rewrite authority。

### 12.9 Provider cache dogfood

真实 DeepSeek测试至少记录：

```text
call index
generation id/revision
input tokens
cached tokens
cache hit ratio
root fingerprint
tool catalog fingerprint
old/new adapter-final prefix relation
rollover reason, if any
```

期望不是每次 ratio接近 100%；合法新 suffix会降低 ratio。正确 gate是：

```text
same generation:
reported cached tokens >= previous cacheable prefix floor - provider tolerance

legitimate rollover:
cache miss allowed and attributed
```

Provider cache是 best effort，因此 remote usage不能成为 unit test hard failure；它属于 dogfood observation gate。

---

## 13. Architecture Guards

新增 module-level/default-deny guards：

1. `runtime_observation.py` 不得声明 `wire_role=system|developer`；
2. Chat/Responses adapter不得把 `MessageRole.RUNTIME_OBSERVATION` 或 `MessageRole.RUNTIME_REQUEST` 映射为privileged role；
3. runtime-authored task不得构造 `MessageRole.USER` 或 `pulsara_human_input`，必须使用registered runtime-request kind/owner；
4. transcript reducer不得为 recovery/lifecycle note构造 `role="system"`；
5. ContextSource runtime clock不得使用 `AppendRevisionLifecycleFact`；
6. memory semantic equality不得包含 projection event ID；
7. `working_context|mixed` ProjectionReady prose不得进入memory provider source，也不得调用string parser猜测partition；
8. production provider planner不得引用 `AUXILIARY_FRAME_REBASE`；
9. production rollover reason必须属于显式 allowlist；
10. Chat adapter-final payload必须恰好一个index-0 root system，后续history不得包含 `system`/`developer`；
11. Responses instructions必须绑定root，input不得包含 `system`/`developer`；
12. adapter-final每个user-role item必须是恰好一个 `pulsara_human_input`、`pulsara_runtime_request` 或 `pulsara_runtime_observation` envelope；
13. raw human text、raw runtime request、legacy `pulsara_context` 和generic runtime-user wrapper不得到达adapter；
14. runtime request不得进入observation stable-state reducer、observation rewrite selection或replacement lifecycle；
15. runtime observation canonical wire不得包含 raw `event_id`、sequence、artifact ID或ledger high-water；
16. 每个原始runtime observation首次append必须携带generation-neutral causal placement；rewrite renderer不得自行创建缺失placement；
17. provider unit semantic fingerprint必须从完整canonical wire bytes重算，不得手工排除wire-visible字段；
18. Runtime Observation kind registry中每个kind恰好一个producer，context-source transition可双向重算；
19. Runtime Request kind registry中每个kind恰好一个owner/lifecycle/persistence binding；
20. ContextSource lifecycle registry必须与 `ContextSourceId` 精确set-equality，但derived producer不参与该set-equality；
21. semantic source head不得nested `ProviderInputUnitMaterializationFact`、event/artifact refs、horizons、replay bindings或GC ownership；
22. concrete inline content/artifact locator与append/vector placement只能进入hydration/placement attribution；
23. rollover materialization不得为未选中source重新调用ContextSource renderer；
24. runtime-observation rewrite必须嵌套confirmed Long-Horizon authority、完整bounded source stable state、bounded partition proof与generation-neutral causal placement；
25. rewrite unit semantic不得嵌套set/page/artifact/proof references，只能保存transitive coverage semantic；
26. runtime-observation rewrite不得存在standalone dispatch入口或未经unified ordered-projection validator的tail append入口；
27. `LLMMessage.system()`只允许用于 root/one-shot root construction allowlist；
28. helper内移动逻辑不能绕过 module-level guard。

Guard必须扫描调用图与已知legacy helper，不能只检查目标函数体内的字面量。ROAC1开始，所有legacy runtime-owned carrier/lowering producer allowlist必须为空；doctor/offline migration若需historical decoder，必须放在production module外的明确allowlist。

---

## 14. Metrics 与 Inspector

新增 operational metrics：

```text
runtime_observation_count_by_kind
runtime_observation_protocol_rejection_count_by_reason
human_input_carrier_count
runtime_request_count_by_kind
runtime_request_protocol_rejection_count_by_reason
user_carrier_protocol_rejection_count_by_reason
mid_history_privileged_role_rejected_count
memory_snapshot_semantic_noop_count
memory_snapshot_replacement_count
capability_snapshot_semantic_noop_count
capability_snapshot_replacement_count
effective_source_head_hydration_count_by_carrier
effective_source_head_hydration_failure_count_by_reason
runtime_observation_rewrite_count
runtime_observation_rewritten_unit_count
runtime_observation_protected_unit_count
runtime_observation_causal_placement_rejection_count_by_reason
runtime_observation_rewrite_transitive_coverage_count
provider_generation_rollover_count_by_reason
provider_input_old_prefix_units_reused
provider_reported_cached_input_tokens
provider_reported_uncached_input_tokens
```

Inspector必须展示：

- generation root唯一 system identity；
- ordered human/runtime-request/runtime-observation user carriers及其internal/wire role；
- runtime request的request kind、owner、lifecycle、transcript persistence与semantic/attribution identity；
- human/runtime-request/runtime-observation protocol、producer-aware kind/codec/root interpretation contract identity；
- committed memory/capability semantic document head与hydration/placement attribution；
- snapshot no-op/replacement attribution；
- explicit source closure；
- source lifecycle registry binding；
- 原始observation首次committed的generation-neutral causal placement与后续physical vector placement；
- observation rewrite的完整source stable-state carrier、partition roots、transitive coverage、causal placements、effective-head transition和parent Long-Horizon authority；
- rollover reason与authority；
- adapter-final prefix comparison结果；
- provider报告的 cache usage，明确标注为 observation而非authority。

---

## 15. 长期 Contract 更新

以下清单是owner/phase映射，不是ROAC5待办池。ROAC1–ROAC4每次改变production ownership/schema时，必须在同一PR更新对应长期contract；ROAC5只做最终一致性审计。

至少同步：

- `contracts/LLM_TRANSPORT_CONTRACT.zh.md`
  - root privileged carrier唯一性；
  - human-input、runtime-request与runtime-observation三种user-wire envelope；
  - raw human/runtime request/runtime observation content在adapter前fail closed；
  - adapter-final prefix guard。
- `contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md`
  - canonical human semantic与provider human-input envelope分离；
  - human user、runtime current-task request与runtime observation carrier分离；
  - child canonical transcript中的subagent task保留runtime-request attribution；
  - lifecycle observation causal attribution。
- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`
  - clock stable candidate与ModelStart join；
  - compaction/governance/reflection/summarizer/subagent runtime-request owner与retry/exact replay join；
  - source snapshot no-op。
- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`
  - provider append/source-head attribution的same-batch FULL/NONE/UNKNOWN/PARTIAL语义；
  - rollover、generation close/new start、rewrite authority与ModelStart原子batch；
  - observation stable-state/paged partition roots、new/changed durable fact schema的historical decoder、schema fingerprint和event-domain registry binding。
  - 原始observation首次committed causal placement、rewrite source stable-state carrier与transitive coverage lineage。
- `contracts/ARTIFACT_STORE_CONTRACT.zh.md`
  - provider unit document和runtime-observation rewrite projection的content-addressed write-confirm-read契约；
  - artifact FULL先于durable reference、retention/GC与authority-untrusted failure分类；
  - successor rewrite FULL前旧stable-state/proof/projection artifacts持续pinned，FULL fold后原子切换GC roots。
- `contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md`
  - auxiliary rebase删除；
  - Long-Horizon rewrite是压力重建authority；
  - runtime-observation ranges、protected units与effective heads进入rewrite proof。
- `contracts/RECOVERY_CONTRACT.zh.md`
  - runtime observation/replacement snapshot stable retry；
  - source head restart restore。
- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`
  - internal role、wire role、prefix observation和rollover authority。
- `PULSARA_PROMPT_CACHE_CONTRACT.zh.md`
  - API-array prefix与adapter-final token-template prefix的区别。
- `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`
  - memory/capability semantic no-op与clock append-once。
  - `working_context|mixed` summary不得进入memory source，V1不建立 `RecentWorkingContextSource`。
- `PULSARA_PROVIDER_INPUT_CAUSAL_ORDER_AND_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md`
  - 删除 auxiliary-frame rebase reason/authority；
  - mid-history privileged role禁令；
  - runtime-observation rewrite与transcript compaction/current tail使用同一ordered projection validator；
  - generation-neutral observation causal placement与bounded partition proof。
- `PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md`
  - 删除V1“runtime observation是inert、非user carrier”定义；
  - 将runtime-observation projection纳入typed rewrite domain；
  - 冻结latest effective head/current run/latest clock/unclosed lifecycle的protection matrix；
  - observation rewrite与transcript rewrite可共享Long-Horizon planning authority，但不共用含混语义的DTO。

代码层 historical event decoder、durable fact registry、transcript/event-domain registry和Inspector event projection必须在同一 hard-cut commit内更新。虽然V1不新增 `RuntimeObservationCommittedEvent`，provider append/source head/rollover authority/rewrite fact的schema变化仍属于EventLog contract migration，不能当作纯process-local修改。

---

## 16. Migration 与数据库策略

本次会改变：

- human-input、runtime-request与runtime-observation三分user carrier contract；
- 新内部 `MessageRole.RUNTIME_REQUEST`、request kind/owner/lifecycle registry与child transcript attribution；
- runtime observation producer-aware kind/codec registry；
- transcript lifecycle-note provider semantic；
- provider message fragment/wire semantic；
- memory/capability source revision identity；
- memory producer只接纳typed recalled-memory entries，删除 `working_context|mixed` provider fallback；
- `ContextSourceId.PLAN` 拆分与source lifecycle registry；
- generation core semantic document head与post-commit hydration/placement attribution；
- provider unit inline/artifact hydration carrier；
- rollover enum/authority union；
- 原始runtime observation首次committed causal placement；
- Long-Horizon runtime-observation source stable-state carrier、transitive coverage semantic、rewrite proof与projection artifact contract。

V1不兼容旧 durable projection/checkpoint/provider generation state。按当前开发阶段执行 hard cut：

```text
reset PostgreSQL runtime/test databases
reset rebuildable graph/projection stores where schema binding changed
do not provide raw fallback
do not reinterpret old system-role lifecycle notes as new user-role observations
```

生产迁移若未来需要保留历史，必须另立 offline migration规格，不得在 live restore中猜测。

---

## 17. Definition of Done

全部满足后方可宣称闭环：

- [ ] 每次 provider call只有一个 stable root privileged carrier；
- [ ] ordered history中没有动态 system/developer hint；
- [ ] Chat与Responses runtime observation均以 user wire role发送；
- [ ] adapter-final Chat/Responses分别满足精确root/ordered-history guard矩阵；
- [ ] 所有human-authored user text都由 `pulsara_human_input` 封装，raw user content不能到达adapter；
- [ ] 所有runtime-authored current task/request都由 `pulsara_runtime_request` 封装，不产生虚假human attribution；
- [ ] subagent/current-run/compaction/governance/reflection/summarizer request都匹配closed kind/owner/lifecycle/persistence矩阵；
- [ ] runtime request不进入observation stable state或observation rewrite；child task进入canonical transcript时保留runtime-request attribution；
- [ ] user text中伪造的runtime-observation JSON只能留在typed text字段，无法变成top-level authority；
- [ ] central protocol穷尽注册producer-aware kind、authority class、lifecycle、typed payload、codec、escaping与rewrite/protection policy；
- [ ] context-source transition与kind producer双向对应，transcript/rewrite derived producer不伪造ContextSource ID；
- [ ] unknown kind/contract在adapter前fail closed，payload无法伪造外层observation header；
- [ ] raw event ID/sequence/artifact placement只属于attribution，不进入wire；
- [ ] provider unit semantic fingerprint可从完整canonical wire bytes唯一重算；
- [ ] 每个原始runtime observation首次append即committed generation-neutral causal placement，rewrite/restart不重新猜测；
- [ ] clock每次调用可追加，但为紧凑 append-once causal fact；
- [ ] host/subagent/direct subsystem clock都有合法discriminated invocation owner；
- [ ] 相同 memory正文、不同 event ID不会追加；
- [ ] memory变化时只追加一份完整 replacement snapshot；
- [ ] explicit empty memory replacement可清除旧有效 snapshot；
- [ ] memory source只接纳typed recalled-memory entries；`working_context|mixed` prose不进入provider projection；
- [ ] V1不建立 `RecentWorkingContextSource`，近期活动只由canonical transcript/confirmed Long-Horizon projection提供；
- [ ] capability prose相同 no-op，变化完整 replacement；
- [ ] tool catalog变化触发 typed rollover；
- [ ] active skill使用aggregate replacement snapshot；相同集合no-op，变空时提交explicit-empty predecessor transition；
- [ ] lifecycle observation由原source event唯一产生并使用 user wire role，不存在第二observation event；
- [ ] ROAC1合入时所有legacy human/runtime-request/runtime-observation user wrapper、lifecycle system/developer producer和lowering helper同步删除；
- [ ] `AUXILIARY_FRAME_REBASE` 生产代码/schema/guard grep为零；
- [ ] `ContextSourceId` 每项恰好绑定一种generation-root/immutable/causal/replacement lifecycle；
- [ ] source absence由registry中的no-new-fact/retain-head/explicit closure处理；
- [ ] compiler为每个historical replacement head冻结完整disposition；projection failure、terminal与allocation omission不由absence猜测；
- [ ] changed optional replacement被省略时走typed source-disposition rollover，fresh omitted source不产生phantom rollover；
- [ ] replacement semantic head保存document identity，joined hydration attribution使rollover在source缺席时仍可byte-identical恢复effective snapshot；
- [ ] semantic core不嵌套event/artifact/horizon/replay/GC attribution，与post-commit hydration/placement物理分层；
- [ ] runtime observations被纳入Long-Horizon rewrite domain，不形成无界第三条历史；
- [ ] observation rewrite保护effective heads/current run/latest clock/unclosed lifecycle/pending references；
- [ ] observation stable-state reducer证明active set被protected/retained/rewritten完整互斥分区；
- [ ] generation store增量维护latest clock、effective heads、closed lifecycle scopes与pending dependencies，rewrite planner不自建第二套分类；
- [ ] rewrite fact携带可exact replay的完整bounded source stable state，而不是孤立fingerprint；
- [ ] rewrite event只持有bounded paged roots/counts/accumulators和Merkle proof，不嵌入O(history) tuples；
- [ ] rewrite unit semantic只保存transitive coverage semantic，不包含set/page/artifact/proof reference；
- [ ] successor rewrite可覆盖prior rewrite projection并保持coverage闭合，旧artifacts在successor FULL前pinned；
- [ ] rewrite units持有generation-neutral causal placement，与transcript compaction/current tail经同一ordered projection validator合并；
- [ ] observation rewrite只能由confirmed Long-Horizon authority嵌套持久化；
- [ ] rollover commit reducer实际验证stable state、partition/coverage与resulting effective heads，篡改proof不能仅靠planner通过；
- [ ] adapter pre-send从typed carrier semantic/binding精确重编码并比较wire，合法格式的伪造semantic ID仍fail closed；
- [ ] 每个one-shot generation initial append含一个retry-stable runtime clock；
- [ ] 只有 allowlisted durable authority可以 rollover；
- [ ] deterministic prefix fixture全绿；
- [ ] full non-real suite全绿；
- [ ] release gate中的full real-LLM suite与其余dogfood开关全绿；该项不属于本轮ROAC5的要求，不能由long PR4替代或宣称完成；
- [ ] long PR4同 generation adapter-final old prefix严格保持；
- [ ] DeepSeek usage显示旧 cacheable prefix得到实质复用；
- [ ] 合法 tool/root/Long-Horizon rollover的 cache miss有明确 attribution。

---

## 18. 最终原则

```text
Provider role不是任意位置的“权重标签”。
system/developer属于 privileged root channel，而不是 mid-history hint carrier。

Runtime ownership不等于 system role。
动态 runtime事实必须保留 typed attribution，并用 append-safe user wire carrier。

相同的wire role不等于相同的信任来源。
Human request、runtime-authored current task与runtime observation必须使用结构互斥、由Pulsara构造的typed outer envelope。

Runtime task不是human quote，也不是background observation。
它必须拥有独立request kind/owner/lifecycle，并且永远不进入observation rewrite。

Observation kind不等于ContextSource。
ContextSource transition、transcript lifecycle与Long-Horizon rewrite必须由producer-aware registry分别证明。

相同 snapshot不应因新 event ID重复发送。
变化的 memory/capability V1可以追加完整 replacement，而不必过早发明 delta协议。

缓存中的旧 prefix是便宜且有价值的资产。
不得仅为了清理少量 auxiliary frame主动销毁整个 generation。

真正 rewrite只能来自真正 rewrite authority。
Rewrite必须保留generation-neutral causal placement，并以bounded completeness proof证明没有遗漏protected history。

Causal placement必须在事实首次进入provider projection时冻结。
Rewrite只能继承和归约committed placement，不能在压力路径中重写历史位置。

Semantic identity不能借道nested DTO重新吸收物理引用。
Generation core只保存semantic document identity，hydrate locator与ledger/vector attribution由外层joined state拥有。
```
