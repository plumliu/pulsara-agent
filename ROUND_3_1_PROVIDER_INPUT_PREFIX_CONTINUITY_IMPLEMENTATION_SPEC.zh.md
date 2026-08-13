# Pulsara Round 3.1：Process-local Provider-Input Prefix Continuity 实施规格

> 状态：**ACTIVATED**
>
> 记录日期：2026-08-12；激活复核：2026-08-13（post-review deadline、Plan occurrence、密封continuity permit、steer共享base计量与first-party source closed union复核）
>
> 编码基线：`a71aa195f2469701fb078d79f78f4fe234bc0d46`（`feat: restore plan workflow and run permissions`）
>
> 激活证据：[`round3_1_provider_input_prefix_continuity_activation.json`](benchmarks/suites/core/v1/round3_1_provider_input_prefix_continuity_activation.json)
>
> 本文是 [`ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md`](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md) 的连续增量，命名为 **Round 3.1**。Round 3已经恢复provider-neutral typed compiler；Round 3.1主要修复同一Host、同一精确conversation scope内的provider-visible prefix continuity，并在该因果append边界上闭合一个窄的双入口输入产品契约：busy时steer当前turn，或显式排队到后续new turn。它不撤销Round 3 activation，也不恢复hard-cut前的durable `ProviderInputGeneration`、exact request audit或execution recovery graph。
>
> 上位架构：[`PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md`](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 产品能力索引：[`POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md`](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 直接历史证据：[`PULSARA_PROMPT_CACHE_CONTRACT.zh.md`](archived_docs/PULSARA_PROMPT_CACHE_CONTRACT.zh.md)、[`PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md)、[`PULSARA_PROVIDER_INPUT_CAUSAL_ORDER_AND_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_PROVIDER_INPUT_CAUSAL_ORDER_AND_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md)、[`PULSARA_RUNTIME_OBSERVATION_AND_AUXILIARY_CONTEXT_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_RUNTIME_OBSERVATION_AND_AUXILIARY_CONTEXT_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md)

---

## 0. 执行结论

Round 3恢复了正确的编译层，却没有恢复调用之间的prefix生命周期。当前每次model call仍重新收集全部source、重新选择render variant，并把动态source放在整个canonical transcript之前。尤其：

- `RUNTIME_CLOCK`每次调用重新采样，并以`LEADING_OBSERVATION`出现在历史最前；
- `RUNTIME_ENVIRONMENT`、`RUN_PERMISSION`、`PLAN_HANDOFF`、`PLAN_WORKFLOW`、`CAPABILITY_CATALOG`和`ACTIVE_SKILL`进入每次重建的`system_prompt`；
- permission snapshot fingerprint、Plan workflow ID/revision/digest、cwd和clock都会合法变化；
- 历史tool result在每次compile中重新参与`FULL/COMPACT/REF_ONLY`选择。

因此当前路径虽然语义可用，却不满足下面这个必须恢复的同进程不变量：

```text
same Host
+ same exact conversation scope
+ same provider-input epoch

system_root[n + 1] == system_root[n]
tools[n + 1]       == tools[n]
messages[n + 1]    == messages[n] || append_only_suffix
```

这里的`||`表示provider-visible有序语义项追加，不要求HTTP JSON文档的闭合括号也构成原始字节前缀。adapter必须对相同`LLMContext`执行确定性lowering；此前已经发送的system/tool/message语义及文本字节不可变化、移动、降级或重新渲染。

Round 3.1冻结以下结论：

1. **continuity是Host-scoped process-local input ownership，不是durability。**
2. **canonical reader仍是conversation truth唯一owner。**continuity owner只能证明“本进程此前给provider准备了什么表示”，不能创造、覆盖或恢复canonical entry。
3. **一个ROOT scope和每个`SUBAGENT_TASK(task_id)`各自拥有独立epoch。**不同session、ROOT/child、不同child之间禁止join或共享mutable owner。
4. **同一epoch只允许稳定root、稳定tool surface和尾部追加。**普通turn、run、clock、permission、Plan、skill、cwd变化及cache miss均不是reset理由。
5. **除`BASE_SYSTEM`外，当前所有会变化的first-party context source都改为typed user-role runtime observation。**它们按因果位置追加，不能继续重建system前缀。
6. **旧source表示一旦安装到epoch即冻结。**后续预算压力不能回头把历史tool result或source从`FULL`改成`COMPACT/REF_ONLY`。
7. **同一Host内的客户端detach/reattach继续复用ROOT epoch；Host replacement后，即使attach同一session也只从canonical rows冷启动。future session fork同样默认cold start。**不恢复旧prefix accumulator；provider侧是否仍命中缓存属于best-effort observation。
8. **Long-horizon compaction是未来唯一正常的历史rewrite/reset owner。**Round 3.1只冻结handoff seam，不提前实现PHC-07。
9. **busy输入采用双入口。**普通`Enter`向exact active ROOT turn提交steer；显式`Tab`提交`NEW_TURN` follow-up。多个同turn steer在下一个provider safe point保持独立canonical user entries、按steer lane FIFO吸收满足item、UTF-8 body与resulting-epoch quote三重上限的最长前缀，并只触发一个后续model call；不得拼成一个字符串或伪造多个turn。
10. **不得新增表、migration、Committed/Live event、subject slot、append guard、durable job或Protocol command kind。**现有`SUBMIT_PROMPT | STEER_ACTIVE_TURN`、`NEW_TURN | STEER_ACTIVE_TURN`、`USER_MESSAGE | USER_STEER`已足够承载该产品契约。
11. **不得恢复`runtime/provider_input/`旧package、persistent vector、source-head reducer、replay binding、receipt、checkpoint、repair、abandonment event或exact context-input artifact。**

最终产品边界是：

```text
PostgreSQL canonical rows
    -> exact canonical cut
    -> current process-local source facts
    -> Host-scoped append planner
    -> pure incremental compiler
    -> complete local LLMContext
    -> provider adapter

Host crash
    -> epoch disappears
    -> unfinished turn按canonical规则interrupted
    -> 新Host从canonical rows冷启动
```

---

## 1. 为什么Round 3之后仍需要Round 3.1

### 1.1 Round 3已经正确完成的部分

Round 3已经建立并验证：

- repeatable-read canonical compile snapshot；
- provider-neutral immutable input DTO；
- exact canonical transcript lowering与tool pairing；
- typed source registry、预算、degradation和physical bounds；
- scope-filtered tool surface及authorize/attempt/invoke exact join；
- transport-bearing target只存在于Kernel adapter；
- compiler无数据库、provider transport、EventLog或execution recovery authority；
- final adapter validation与resolved target estimator一致。

这些能力继续有效。Round 3.1不得另建第二个compiler，也不得把conversation reader并入continuity owner。

### 1.2 当前代码中的具体回归

[代码确认] [`context_sources.py`](src/pulsara_agent/conversation_kernel/context_sources.py)当前binding为：

| Source | 当前channel | 当前变化原因 | Prefix影响 |
|---|---|---|---|
| `BASE_SYSTEM` | `SYSTEM` | 配置/版本变化 | 合法epoch reset |
| `RUNTIME_ENVIRONMENT` | `SYSTEM` | cwd、timezone capture | 重写system root |
| `RUNTIME_CLOCK` | `LEADING_OBSERVATION` | 每call当前时间 | 在历史前第一个message处变化 |
| `RUN_PERMISSION` | `SYSTEM` | 每turn snapshot及fingerprint | 重写system root |
| `PLAN_HANDOFF` | `SYSTEM` | transition/ID/digest | 重写system root |
| `PLAN_WORKFLOW` | `SYSTEM` | workflow/revision | 重写system root |
| `CAPABILITY_CATALOG` | `SYSTEM` | discovery/catalog变化 | 重写system root |
| `ACTIVE_SKILL` | `SYSTEM` | configured/textual active skill projection | 重写system root |

[代码确认] [`compiler.py`](src/pulsara_agent/model_input/compiler.py)当前最终布局是：

```text
system_prompt = join(all SYSTEM sources)
messages      = leading observations + full canonical transcript + trailing observations
```

这意味着即使73,728 bytes canonical history完全相同，只把clock从`01:00:00`改为`01:00:01`，当前production DTO probe仍得到：

```text
system_equal          = true
tools_equal           = true
message_lcp           = 0
provider-text byte lcp = 181
first differing item  = user-role RUNTIME_CLOCK
```

历史正文没有进入可复用prefix。

### 1.3 Provider侧观察信号

用户提供的DeepSeek控制台观察显示：

| 日期 | 命中缓存input | 未命中input | input cache ratio |
|---|---:|---:|---:|
| 2026-08-11 | 388,224 | 56,497 | 约87.3% |
| 2026-08-12 | 116,352 | 788,297 | 约12.9% |

该数据是调查信号，不是单独的correctness proof。provider可能异步建立、逐出或按租户/model分区缓存；Round 3.1的hard gate必须是本地adapter-final strict-prefix proof，remote usage只作为dogfood observation。

### 1.4 hard-cut前真正值得保留的结论

`5b7ad9f7`之前的prefix hard cut源于真实故障：retained generation与fresh rebuild使用不同ordering，曾把user request移动到由它触发的assistant/tool之后。应保留：

- ordered canonical transcript是唯一因果顺序；
- current user不能在后续call中移动或重发；
- tool call/result pair的provider表示一经使用即冻结；
- clock和其他动态事实只能在尾部形成新observation；
- root/tool/provider compatibility变化才允许cache break；
- cache usage不是authority。

不应保留：

- durable `ProviderInputGeneration`；
- generation Started/Append/Rollover/Closed/Abandoned events；
- persistent vector、artifact-backed prefix chunks和resident restore；
- source-head/event reducer；
- historical binding registry与exact replay；
- 跨Host continuation、prepared owner recovery与reconciliation latch；
- 每call context-input manifest/pages/root audit plane。

编码时可从`5b7ad9f7`只读参考以下边界：

- `src/pulsara_agent/runtime/provider_input/causal.py`中的canonical order与tool pairing校验；
- `src/pulsara_agent/runtime/context_input/stable_transcript.py`中的stable transcript lowering分层；
- `tests/test_runtime_observation_prefix_continuity.py`中的typed runtime observation与causal append用例；
- `tests/test_provider_input_prefix_benchmark.py`中的prefix规模探针；
- `tests/test_provider_input_hard_cut.py`中关于旧prefix不被改写的回归意图。

这些路径只是历史产品证据，不是可复制实现。`runtime/provider_input/planner.py`直接依赖generation events，`store.py | continuation.py | coordinator.py | recovery.py | resident.py | vector.py`以及`context_input/audit_* | commit.py | replay.py | event_slice.py`均属于本轮禁止恢复的durability/recovery或proof graph。

### 1.5 Codex active-turn steer证据与本轮采用范围

[代码确认] 本轮简要核对本地Codex源码commit `6138909d6ec5`（2026-07-10）。以下首先描述的是**Codex Core正常接受steer的路径**，不是其TUI全部race/interrupt恢复语义。其产品面把两个用户意图显式分开：

- `Enter`是普通submit key，`Tab`是queue key：`../codex/codex-rs/tui/src/bottom_pane/chat_composer.rs:566-567`；
- active turn存在时，`UserTurn`路由为`turn/steer`，携带exact `expected_turn_id`；没有active turn才调用`turn/start`：`../codex/codex-rs/tui/src/app/thread_routing.rs:575-669`；
- `turn/steer`控制协议携带`Vec<UserInput>`、optional client user-message ID及required active-turn precondition：`../codex/codex-rs/app-server-protocol/src/protocol/v2/turn.rs:167-194`；
- Core把它保存为turn-local `TurnInput::UserInput`，追加到active turn pending input，而不是修改已经打开的provider request：`../codex/codex-rs/core/src/session/input_queue.rs:12-31`、`../codex/codex-rs/core/src/session/mod.rs:3867-3946`；
- 当前sampling及in-flight tool futures闭合后，Agent loop在正常路径一次drain当前pending inputs，逐项记录为普通user history，再发下一次sampling：`../codex/codex-rs/core/src/session/turn.rs:217-237,2466-2472`；
- provider-visible载体不是特殊steer role，而是标准`Message { role: "user", content: InputText/... }`：`../codex/codex-rs/protocol/src/models.rs:1714-1732`；
- `sleep`、`wait_agent`等显式等待工具可订阅`InputQueueActivity::Steer`提前结束等待，但这不是任意physical tool的异步取消：`../codex/codex-rs/core/src/tools/handlers/sleep.rs:97-131`。

Codex TUI还拥有Core之外的best-effort race UX：`expected_turn_id` mismatch时可用server报告的active turn重试一次；active turn消失时可回退到`turn/start`：`../codex/codex-rs/tui/src/app/thread_routing.rs:575-669`。turn被interrupt后，尚未确认的pending steer也可能恢复到composer或按UI policy重新提交：`../codex/codex-rs/tui/src/chatwidget/input_restore.rs:145-210`。因此不得把“exact target永不重绑、绝不降级、任何中断路径都保持多条独立消息”宣传为Codex完整TUI契约。

因此本轮只借鉴Codex Core正常accepted-steer path的核心语义：**当前已经发送的provider call不可变；steer在同一logical turn内等待下一个安全sampling边界。**该正常路径中的多项typed input保持多个user message，而不是在Runtime内字符串拼接；同一safe point可以把它们作为一个append-only suffix交给一次后续model call。provider是否在内部把相邻同role message等价处理不属于Runtime契约。

Round 3.1采用这个产品边界，但不复制Codex的process-local TUI draft owner，也不引入provider thread identity：

1. Pulsara继续让每条已提交输入进入现有canonical `prompt_queue_items`，保留detach/reattach、ACK-unknown query和accepted permission semantics。
2. `STEER_ACTIVE_TURN`精确绑定当前ROOT turn；`NEW_TURN`没有target并冻结自己的send-time permission snapshot。
3. 相同target turn的steer按自己的lane FIFO在provider safe point优先吸收；future `NEW_TURN`按自己的lane FIFO等待当前turn terminal。queue sequence保留全局occurrence/audit order，但不再宣称两种lane之间存在单一delivery FIFO。
4. stale/non-steerable target必须typed reject，不能静默降级成`NEW_TURN`，也不能重绑后来出现的active turn。这是Pulsara基于canonical queue、stable command与ACK-unknown confirmation作出的**更严格产品选择**，不是对Codex TUI race fallback的逐项复刻。
5. 不新增Protocol枚举；当前Python/Go已经具备`SUBMIT_PROMPT | STEER_ACTIVE_TURN`。本轮只补齐显式queue-next-turn交互、lane调度、exact accepted-entry carrier和provider-prefix验收。

---

## 2. 继承且不得改变的架构决策

### 2.1 Canonical conversation仍是真源

Round 3.1只能消费：

- `FrozenCanonicalCompileSnapshot`；
- `CanonicalModelInputSnapshot`；
- exact `context_binding_revision_id`；
- exact `provider_input_through_sequence`；
- ordered immutable `FrozenProviderInputItem`；
- one-cut permission、Plan workflow、handoff和approved-plan facts。

continuity state不能被用于证明任何row、turn、tool result、permission或Plan transition已经commit。reopen继续只读canonical rows。

### 2.2 Provider call仍发送完整输入

Round 3.1不是delta transport，也不使用：

- `previous_response_id`；
- provider conversation/thread identity；
- provider-side automatic context management；
- server-owned truncation；
- 只发送本轮suffix的私有协议。

每次调用仍构造完整`LLMContext(system_prompt, tools, messages)`。增量只描述Pulsara本地如何保证旧部分不被重新解释或改写。

### 2.3 Crash语义不扩大

Host crash后：

- process-local epoch与pending candidate全部消失；
- raw provider/live stream消失；
- 未完成turn变为`INTERRUPTED`；
- 不补造historical Start/End；
- 不恢复旧model call、transport cursor或prefix generation；
- 新Host按当前compiler contract从canonical rows冷启动。

### 2.4 Exact input audit继续“不承诺”

不得持久化：

- compiled full prompt；
- per-call source variants；
- adapter-final request body；
- prefix chunks/vector/root；
- source placement receipt；
- historical clock/cwd/capability rendering；
- provider cache key或remote continuation identity。

允许的operational observation只能包含bounded count、opaque fingerprint、reset reason和provider reported usage，不得包含prompt正文、tool arguments、path、secret或private URL。

### 2.5 Oracle保持不变

Round 4激活后的closed oracle继续是：

```text
product relations       = 26
Committed event types   = 34
Live event types        = 23
subject slots           = 15
append guards           = 2
durable job handlers    = 4
```

Round 3.1各项净变化必须为0。

---

## 3. 术语与精确不变量

### 3.1 Conversation scope

```python
class ProviderInputContinuityScope:
    session_id: str
    scope_kind: Literal["ROOT", "SUBAGENT_TASK"]
    scope_subagent_task_id: str | None
```

约束：

- `ROOT -> scope_subagent_task_id is None`；
- `SUBAGENT_TASK -> scope_subagent_task_id is not None`；
- scope只在当前Host owner内有意义；
- session ID不同永不join；
- ROOT与child永不join；
- 两个child task ID永不join；
- task完成后其epoch可立即discard，不承诺再次复用。

### 3.2 Prefix epoch

Prefix epoch是一个process-local时期，在该时期内以下provider-visible root保持不变：

```python
class ProviderInputEpochCompatibility:
    compiler_contract_version: str
    base_system_semantic_fingerprint: str
    tool_surface_fingerprint: str
    model_target_fingerprint: str
    estimator_fingerprint: str
    provider_message_lowering_contract: str
    context_base_semantic_identity: str
```

实现可以附加process-local nonce/revision用于防stale candidate，但nonce、Host owner identity、surface borrow token、callback或coordinator不得进入provider-visible metadata。

`context_base_semantic_identity`只表达当前canonical compiler采用的历史base：`FULL_HISTORY`使用versioned常量；`SNAPSHOT`使用exact snapshot content/contract identity。普通新turn虽然会创建新的revision-0 row，但仍采用同一个`FULL_HISTORY` base，不能因此reset。

`resolved_model_call_id`、每call UUID、deadline、普通turn的`context_binding_revision_id`、permission snapshot ID、Plan row ID、event ID、writer generation及provider request trace均不得作为epoch compatibility；否则每call/turn都会伪造reset。

### 3.3 Strict prefix

同一epoch内，对于任意已经进入dispatch admission的call `n`和后继call `n+1`：

```python
next.system_prompt == previous.system_prompt
next.tools == previous.tools
next.messages[:len(previous.messages)] == previous.messages
```

还必须满足：

- 每个旧message的role、content、thinking、tool call ID/name/arguments、tool result attribution完全相同；
- 旧source observation envelope与正文完全相同；
- 旧tool result render mode不可改变；
- canonical message相对顺序不可改变；
- adapter不得在旧prefix之前注入per-call动态message；
- provider-visible root以外的temperature、timeout等参数不构成message prefix，但若adapter声明其影响provider cache partition，必须进入compatibility cohort并触发显式reset。

### 3.4 Canonical frontier

每个epoch保存当前process已安装的canonical frontier：

```python
class ProcessLocalCanonicalFrontier:
    latest_context_binding_revision_id: str
    context_base_semantic_identity: str
    through_sequence: int
    ordered_item_fingerprints: tuple[str, ...]
```

直接复用当前中央pure helper：

```python
provider_input_item_fingerprint(item) -> str
```

该helper已经覆盖`FrozenProviderInputItem`的provider语义、origin与tool-result attribution。Round 3.1不得再增加第二个近似算法；若新增字段改变语义，必须在同一helper中升级domain separator并同步所有reader/golden。后继snapshot必须证明：

```text
new ordered item fingerprints
    starts_with
installed ordered item fingerprints
```

同一entry identity内容变化、旧item消失、顺序变化或同sequence替换均为`CANONICAL_PREFIX_CONFLICT`，不能用epoch reset掩盖canonical corruption。

为区分普通turn revision与真正的history rewrite，canonical reader必须在现有repeatable-read中额外冻结一个provider-neutral fact，不得让continuity owner二次查库：

```python
class FrozenContextBindingCompileFact:
    binding_revision_id: str
    revision_ordinal: int
    base_kind: Literal["FULL_HISTORY", "SNAPSHOT"]
    context_snapshot_id: str | None
    source_through_sequence: int
    context_base_semantic_identity: str
```

`FULL_HISTORY`的base identity由versioned lowering contract决定，不包含turn/revision ID；`SNAPSHOT`的base identity覆盖snapshot content digest、compiler/prompt/model contract及source cut。该fact进入现有`FrozenCanonicalCompileSnapshot` fingerprint，但不新增数据库列。

`CONTEXT_SNAPSHOT`由未来PHC-07显式采用，且`FrozenContextBindingCompileFact`证明base从`FULL_HISTORY`或旧snapshot切换到新的accepted snapshot identity时，才允许进入新epoch。普通新turn的revision ID变化、同一base上的mid-turn attribution更新均不得reset。

### 3.5 Source lifecycle head

process-local source head只用于决定本次是否需要追加新observation：

```python
class ProcessLocalSourceHead:
    source_kind: ContextSourceKind
    presence: Literal["VALUE", "CLEARED", "UNAVAILABLE"]
    semantic_fingerprint: str
    installed_observation_fingerprint: str
    last_emitted_turn_id: str | None
    last_emitted_model_call_index: int
```

不存在head表示该source尚未在当前epoch安装。head只在包含对应observation的append candidate完成CAS install后推进；prepare、allocation omission、preflight失败或CAS conflict均不得推进。它不是source truth；source truth仍来自本次one-cut facts与collector。Host替换后source head丢失，首次调用重新发当前snapshot。

---

## 4. Source contract hard cut

### 4.1 只有稳定root可以进入SYSTEM

Round 3.1完成后，first-party source只有`BASE_SYSTEM`可以进入`system_prompt`：

```text
system_prompt = versioned stable base root
```

stable base root包含：

- agent的长期行为契约；
- tool/runtime observation envelope的稳定解释规则；
- 同一source的后续`SNAPSHOT | TURN`按顺序取代旧current-state解释，`CLEARED`明确使旧状态失效，`CALL`只描述其后紧邻physical call的观测时点，`ONE_SHOT`只描述一次transition而不成为永久current state；
- “model-visible permission/Plan guidance不替代physical policy enforcement”的固定说明；
- 不含当前时间、cwd、permission、Plan、skill、catalog、session/turn/call ID或diagnostic。

base root contract需要显式从v1升级；同一Host若root真正变化，开启新epoch。不得在旧epoch中修改system字符串。

### 4.2 Runtime observation统一使用user role

其他source统一lower为一个canonical JSON user message；下面只展示语义对象，不是可字符串插值的模板：

```json
{
  "pulsara_runtime_observation": {
    "body": "<JSON string; arbitrary bounded UTF-8 text>",
    "contract": "<closed version>",
    "lifecycle": "SNAPSHOT|CLEARED|UNAVAILABLE|TURN|ACTIVATION|CALL|ONE_SHOT",
    "source": "<closed source kind>",
    "trust": "<closed provenance class>"
  }
}
```

唯一codec必须使用central `canonical_json_bytes()`等价编码，固定UTF-8、key ordering与JSON string escaping；禁止手写起止marker或直接插入body。validator必须拒绝unknown top-level/member、非string body、非法union与超限正文，并证明`encode(decode(encode(x))) == encode(x)`。skill正文、workspace path或future source中即使包含旧closing marker、C0/C1或引号，也只能成为`body`字符串内容，不能逸出carrier。该codec是pure provider carrier，不是durable registry，也不给human伪造文本任何physical authority。

production channel union同步hard-cut为：

```python
class ContextChannel(StrEnum):
    SYSTEM = "SYSTEM"
    RUNTIME_OBSERVATION = "RUNTIME_OBSERVATION"
```

旧`LEADING_OBSERVATION | TRAILING_OBSERVATION`从production contract删除。runtime observation的真实位置由causal append planner决定，不再由“每次全量布局时放在history前/后”的channel决定。

provider-visible envelope不得携带opaque database ID、event ID、fingerprint、nonce或secret。内部candidate仍保留exact identity/fingerprint用于join。

provider-visible render identity与internal occurrence identity必须分离。特别是`PLAN_HANDOFF`正文可以只展示closed kind/status/permission语义，但`ONE_SHOT` domain identity必须覆盖既有`transition_semantic_digest`以及carrier/workflow/revision/interaction identity；两个显示文本相同的Plan transition仍是两个causal occurrence。不得再用variant/render fingerprint冒充canonical transition identity，否则后续revise/approve会被旧head吞掉。

该role选择是cache与provider compatibility边界，不是授权机制：

- user输入可以伪造相似文本，因此工具授权不能相信prompt；
- physical tool effect继续由frozen permission snapshot与typed policy port控制；
- Plan read-only继续由central tool gate强制；
- skill/capability prompt只影响模型选择，不给executor新增权限。

现有trust/channel invariant需要同步收窄为closed矩阵，不能只改renderer绕过DTO validation：

| Source族 | trust class | channel |
|---|---|---|
| `BASE_SYSTEM` | `ROOT_INSTRUCTION` | `SYSTEM` |
| environment/clock | `TRUSTED_RUNTIME_FACT` | `RUNTIME_OBSERVATION` |
| permission/Plan handoff/Plan workflow | 新closed `AUTHORIZED_RUNTIME_GUIDANCE` | `RUNTIME_OBSERVATION` |
| capability catalog/active skill | `AUTHORIZED_CAPABILITY_CONTEXT` | `RUNTIME_OBSERVATION` |

`ROOT_INSTRUCTION`仍只能使用SYSTEM；`UNTRUSTED_OBSERVATION`仍不能使用SYSTEM。`AUTHORIZED_*`只说明producer/provenance，不能被tool executor、policy port或provider adapter解释为physical authority。

collector必须显式表达“当前有值”和“当前无值”，不能通过candidate是否出现在tuple中让continuity owner猜测：

```python
class ContextSourceLifecycle(StrEnum):
    EPOCH_ROOT = "EPOCH_ROOT"
    SNAPSHOT_ON_CHANGE = "SNAPSHOT_ON_CHANGE"
    CALL_APPEND = "CALL_APPEND"
    TURN_APPEND = "TURN_APPEND"
    TURN_SNAPSHOT = "TURN_SNAPSHOT"
    ACTIVATION_SNAPSHOT = "ACTIVATION_SNAPSHOT"
    ONE_SHOT = "ONE_SHOT"


@dataclass(frozen=True, slots=True)
class ContextSourceValueCandidate:
    source_kind: ContextSourceKind
    lifecycle: ContextSourceLifecycle
    domain_semantic_fingerprint: str
    variants: tuple[ContextRenderVariant, ...]
    # existing trust/budget/placement/contract fields remain


@dataclass(frozen=True, slots=True)
class ContextSourceAbsentFact:
    source_kind: ContextSourceKind
    lifecycle: ContextSourceLifecycle
    absence_kind: Literal["NOT_APPLICABLE", "EXPLICIT_EMPTY", "UNAVAILABLE"]
    domain_semantic_fingerprint: str
    # closed contract/provenance fields; no model-visible free text
```

`CollectedContextSources`对registry中的每个first-party source恰好返回一个VALUE或ABSENT branch。这个exactly-one约束由pure compiler再次执行，而不是只依赖正式collector的调用约定；ABSENT还必须逐字段重验registry冻结的contract version/fingerprint、trust、budget、placement、degradation、lifecycle及该source允许的closed absence disposition。continuity owner依据下表决定no-op、clear或typed unavailable；pure compiler只消费最终将被append的VALUE/CLEARED/UNAVAILABLE observation candidate。`UNAVAILABLE`的产品处置由source binding封闭，不能被当成普通empty。

### 4.3 Closed lifecycle矩阵

| Source | Lifecycle | 何时append | absence语义 | 普通变化是否reset |
|---|---|---|---|---|
| `BASE_SYSTEM` | `EPOCH_ROOT` | epoch创建一次 | 不允许absence | 是 |
| `RUNTIME_ENVIRONMENT` | `SNAPSHOT_ON_CHANGE` | 首call及workspace/cwd/timezone语义变化 | 不允许absence；采集失败fail typed | 否 |
| `RUNTIME_CLOCK` | `CALL_APPEND` | 每个完成CAS installation的dispatch candidate一次 | 采集失败本call省略并diagnostic | 否 |
| `RUN_PERMISSION` | `TURN_APPEND` | 每个新turn/automatic continuation首次call一次 | 不允许absence | 否 |
| `PLAN_HANDOFF` | `ONE_SHOT` | exact handoff transition对应的continuation一次 | 没有transition即不发 | 否 |
| `PLAN_WORKFLOW` | `SNAPSHOT_ON_CHANGE` | 首次ACTIVE、revision语义变化及退出`CLEARED` | 曾VALUE后消失必须发`CLEARED` | 否 |
| `CAPABILITY_CATALOG` | `SNAPSHOT_ON_CHANGE` | 首call、catalog语义变化及empty transition | 曾VALUE后empty必须发`CLEARED` | 否 |
| `ACTIVE_SKILL` | `ACTIVATION_SNAPSHOT` | 每个scope新turn评估一次；同turn每个accepted human steer batch再评估一次；configured始终参与，textual只取exact ROOT human initial/latest steer trigger | empty按3×3矩阵形成CLEARED/no-op | 否 |

skill activation subject同步冻结为closed process-local union：

```text
ROOT_HUMAN_PROMPT       -> configured names + exact human USER_MESSAGE/USER_STEER textual names
ROOT_NON_HUMAN_TRIGGER -> configured names only
SUBAGENT_OBJECTIVE      -> configured names only
```

因此child仍能得到Host configured skill；Plan continuation、Terminal observation、subagent/job result、tool result、clock和runtime source均不能触发textual skill。新non-human turn必须安装configured-only snapshot，或在configured set为空时安装`CLEARED`，不能继续沿用前一ROOT human turn的textual activation。同turnnon-human/tool loop不重新评估skill；同turnhuman steer batch只使用latest accepted steer正文更新snapshot。activation subject必须来自explicit dispatch anchor/batch，不能再由`_activation_subject()`反向扫描全部current-turn history。该union只属于compiler input，不新增event或source kind。

### 4.4 Runtime observation placement ordinal

所有runtime observation进入同一个user-role suffix，但同一append candidate内仍需要closed placement ordinal。该顺序只表达provider-visible因果布局，不能复用budget/degradation priority：

| Source | placement ordinal |
|---|---:|
| `RUNTIME_ENVIRONMENT` | 10 |
| `RUN_PERMISSION` | 20 |
| `PLAN_HANDOFF` | 30 |
| `PLAN_WORKFLOW` | 40 |
| `CAPABILITY_CATALOG` | 50 |
| `ACTIVE_SKILL` | 60 |
| `RUNTIME_CLOCK` | 90 |

同一source的`VALUE | CLEARED | UNAVAILABLE`使用同一ordinal；同ordinal内再按closed source kind排序，不能依赖registry、dict或plugin注册顺序。clock固定最后，使它最接近本次request-like trigger。placement ordinal不得决定source是否required、先降级谁或谁具有更高权限。

### 4.5 Provider-visible payload最小化

当前render中的下列字段移出provider-visible正文，只保留在internal attribution或operational digest中：

- permission snapshot ID/fingerprint；
- Plan workflow/interaction row ID；
- transition semantic digest；
- arbitrary event/revision fingerprint；
- Host owner/writer generation；
- source candidate fingerprint。

provider只看真正影响行为的typed事实，例如：

```text
permission: requested/effective mode + overlay
plan: active/read-only + allowed control tools + approved/cancelled/revise handoff
environment: workspace kind/root + current cwd + timezone
clock: local date + bounded observed timestamp
skill: selected skill body or explicit cleared
catalog: current bounded catalog or explicit cleared
```

这项最小化不能删除模型真正需要的workspace path、Plan draft引用或tool usage指导。

---

## 5. Causal append planner

### 5.1 唯一顺序owner

新增一个Host-owned process-local owner，推荐位置：

```text
src/pulsara_agent/conversation_kernel/input_continuity.py
```

pure DTO/validator位于：

```text
src/pulsara_agent/model_input/continuity.py
```

依赖方向必须保持：

```text
conversation_kernel
    -> model_input pure contracts/compiler

model_input
    -X-> conversation_kernel
    -X-> database
    -X-> provider transport
```

Host owner负责scope map、lock、epoch lifecycle和candidate installation；pure compiler负责lower、预算与strict-prefix验证。context source collector不访问continuity state。

### 5.2 Host ownership

continuity owner必须由`ConversationKernelHost`创建，并注入所有runner；不得由单个`ConversationKernelRunner`拥有，否则每个turn/automatic continuation都会冷启动。

Host最多同时保留：

- 1个ROOT epoch；
- 每个active subagent task至多1个epoch；
- 当前`MAXIMUM_LIVE_SUBAGENTS=4`，因此最多5个active epoch。

subagent task terminal后立即discard其epoch。Host close在现有provider/task drain完成后清空全部epoch；清空不写数据库，也不等待consumer receipt。

### 5.3 Canonical delta

对于已有epoch：

```text
new canonical snapshot
    -> validate old item fingerprint prefix
    -> canonical_delta = items[old_count:]
```

只lower`canonical_delta`。旧canonical item不重新进入`lower_canonical_item()`，旧tool result不重新选择render variant。

canonical reader还必须给出closed provider-group boundary。一个包含tool request的assistant message，与其全部ordered tool result或provider-only interruption closure，构成不可分割的provider transcript group。append planner不得把clock、permission、Plan、skill或其他runtime observation插入该group内部。

若准备下一次model call时，`canonical_delta`以尚未闭合的tool request结束，或group identity/result attribution不完整，则返回typed `CANONICAL_DELTA_NOT_PROVIDER_SAFE`，provider open count必须为0。不得为了维持prefix而伪造tool result、移动runtime observation或跳过`require_provider_safe_turn()`。

对于cold bootstrap：

```text
canonical_delta = all current canonical items
```

但仍必须识别本次request-like trigger，将当前source observations插在其真实因果位置，而不是放到整个历史之前。

### 5.4 Trigger anchor

append planner不得从全部history或`canonical_delta`正文中猜测“最后一个request-like item”。runner/Host scheduler必须为每次dispatch冻结一个process-local closed anchor：

```python
class NewTriggerAnchor:
    source_entry_id: str
    provider_input_item_fingerprint: str
    provider_group_boundary_fingerprint: str


class NoNewTriggerAnchor:
    predecessor_frontier_fingerprint: str | None


ProviderInputDispatchAnchor = NewTriggerAnchor | NoNewTriggerAnchor
```

#### 5.4.1 Busy输入双入口

Protocol与canonical vocabulary已经足够，Round 3.1不得再造同义command/event：

| 用户操作 | 无active ROOT turn | 有exact steerable ROOT turn | Canonical ingress |
|---|---|---|---|
| `Enter`普通发送 | `SUBMIT_PROMPT`，开始/排队`NEW_TURN` | `STEER_ACTIVE_TURN(target_turn_id=exact active turn)` | 分别形成`USER_MESSAGE`或`USER_STEER` |
| `Tab`显式queue next turn | `SUBMIT_PROMPT`；若idle可立即开始 | `SUBMIT_PROMPT`，明确等待当前turn terminal | 始终是`NEW_TURN`/`USER_MESSAGE` |
| headless caller | 必须显式选择command kind | 必须显式选择command kind及exact target | 不按文本或当前时序猜测 |

Go TUI当前已经把busy `Enter`映射为`STEER_ACTIVE_TURN`，但没有显式queue-next-turn key；本轮允许且要求只在既有Protocol v3命令上增加`Tab`入口、footer/help hint与对应测试。`Tab`在idle时不制造人为等待，只是走`SUBMIT_PROMPT`的普通new-turn路径。open Plan interaction、draft review、controller capability不足或其他non-steerable state继续由各自typed UI/admission gate拥有，不能被该快捷键绕过。ROOT TUI不得target `SUBAGENT_TASK`。

每个提交都是独立immutable semantic command candidate，不能把generic `query_command(command_id)`返回的任意历史outcome当作compatible winner：

```python
@dataclass(frozen=True, slots=True)
class PreparedPromptIngressCommand:
    session_id: str
    command_id: str
    queue_item_id: str                  # stable from session + command
    client_submission_id: str
    delivery_mode: PromptDeliveryMode
    target_turn_id: str | None          # exact closed union with delivery_mode
    permission_snapshot_id: str | None  # NEW_TURN only, stable
    requested_permission_mode: PermissionMode | None
    content_digest: str                 # computed from exact UTF-8 text before I/O
    content_size: int
    semantic_digest: str
```

`semantic_digest`使用既有`pulsara:queue-prompt-command:v1`算法，至少覆盖queue/client identity、delivery mode、exact target、content identity以及NEW_TURN的send-time permission candidate。`STEER_ACTIVE_TURN`的permission字段必须全部为空；`NEW_TURN`必须冻结permission snapshot ID与requested mode。正文publication只能安装与candidate exact-equal的content identity。

repository必须提供closed compatible-query/write disposition：

```text
FULL_COMPATIBLE(canonical current outcome)
NONE
CONFLICT

write rejection:
    COMMAND_CONFLICT
  | TARGET_STALE_OR_NON_STEERABLE
  | CAPACITY_EXHAUSTED
```

首次写入前、write exception/ACK unknown后及caller retry时都使用同一个candidate查询`session_commands.semantic_digest + target_queue_item_id`及exact queue row。`FULL_COMPATIBLE`才可返回既有winner；`NONE`只重写相同candidate；`CONFLICT`必须返回typed `COMMAND_CONFLICT`。Host不得先调用只按command ID映射状态的fast path，因为同一ID从busy `Tab/NEW_TURN`误复用到busy `Enter/STEER`时必须冲突。repository也不得把所有`ConversationKernelConflict`笼统投影为target stale；target race与global queue capacity必须保持各自typed public outcome。上述分类不新增Protocol command kind或durable receipt。

target在commit前terminal/replaced时，steer返回typed stale/non-steerable结果；不得自动重绑、合并到另一条消息或降级为new turn。

#### 5.4.2 两条delivery lane与bounded steer batch

当前repository把两种delivery mode解释为一个global delivery FIFO：较早`NEW_TURN`会阻止后续current-turn steer；与此同时assistant acceptance只要看到任意targeted pending steer就保持turn RUNNING。这会形成`NEW_TURN head -> steer无法消费 -> turn因pending steer不terminal`的真实闭环阻塞。Round 3.1明确supersede该cross-mode规则：

```text
ACTIVE_TURN_STEER lane
    key   = exact target_turn_id
    order = queue_sequence, id
    drain = provider safe point, before next input freeze

FUTURE_NEW_TURN lane
    key   = session_id
    order = queue_sequence, id
    drain = no active ROOT turn + existing Plan/admission gates allow
```

同lane严格FIFO；cross-lane没有delivery FIFO。active-turn steer可以越过已经accepted但尚未开始的future `NEW_TURN`，因为前者属于当前turn的因果suffix，后者属于未来turn。global `queue_sequence`仍是“何时接受”的audit order，不证明“何时消费”。当前turn terminal后，所有仍指向它的steer确定性`REJECTED(TARGET_TURN_TERMINAL)`，随后future lane才能开始下一turn。不得新增priority字段、第二张queue表、durable lease或receipt。

这里的“不阻塞”严格限定为：**两条row均已accepted为PENDING以后，future lane的head不能阻塞current-turn steer delivery。**现有`pending_prompt_hard_items (=128)`继续作为两条lane合计的session admission cap；本轮不预留steer quota，也不增加priority。若128个future item已经占满capacity，后来steer可以在admission时得到typed `CAPACITY_EXHAUSTED`，但不能被接受后再因future head卡住。UI/headless caller必须区分“未被admit的capacity rejection”和“已accepted但等待safe point”。

#### 5.4.3 Pre-consumption planning、stable consumption与batch admission

三重quote不能由steer consumer自己临时猜测，也不能在consume之后才收集target/source/tool事实。每个safe point先冻结closed predecessor：

```python
@dataclass(frozen=True, slots=True)
class EmptyProviderInputPredecessor:
    expected_epoch_revision: Literal[0] = 0
    predecessor_frontier_fingerprint: None = None
    predecessor_prefix_fingerprint: None = None


@dataclass(frozen=True, slots=True)
class InstalledProviderInputPredecessor:
    expected_epoch_revision: int       # >= 1
    predecessor_frontier_fingerprint: str
    predecessor_prefix_fingerprint: str


ProviderInputAdmissionPredecessor = (
    EmptyProviderInputPredecessor | InstalledProviderInputPredecessor
)
```

`EMPTY`是合法初始状态：ROOT initial entry已经canonical、首个provider input尚未安装时到达的steer与initial `USER_MESSAGE`共同进入一次首次model call。此时不得伪造frontier字符串；initial message位于`canonical_delta_before_trigger`，batch最后一个steer是唯一`NEW_TRIGGER`。`INSTALLED` branch才允许非空frontier/prefix fingerprint。

然后Host执行唯一的pre-consumption planning phase：

```text
1. freeze exact predecessor/current epoch view
2. freeze/read current canonical base cut + one-cut permission/Plan facts
3. prepare exact model target and scope-filtered tool surface；acquire/pin borrow
4. freeze one RuntimeTemporalCapture and all non-trigger source inputs
5. bounded-read up to128 exact target-lane PENDING metadata in lane FIFO order；
   use content_size before hydration and hydrate at most16 MiB candidate bodies
6. derive stable prospective entry/event identities for each pending fact
7. evaluate bounded FIFO prefixes longest-first with the same pure compiler
   - trigger-dependent ACTIVE_SKILL derives from that prefix's last exact steer fact
   - configured/catalog/temporal/domain facts are never resampled
   - rejected trial outputs are immediately discarded; immutable base/prefix is shared
8. freeze the first fitting PreparedSteerSuffixAdmissionPlan
9. only now consume/confirm its selected candidates
10. re-read canonical cut; prove base frontier + exact selected entries only
11. promote the already prepared compiled input; do not recollect/recompile
12. continue DirectModel preflight -> continuity install -> open_once
```

步骤2的canonical base snapshot与步骤5的pending facts无需把PostgreSQL transaction跨process-local target/source工作保持打开；plan必须绑定base cut/control revisions，消费transaction重验其仍兼容。步骤5先读取identity/sequence/target/content manifest与size；只有加入16 MiB raw-body gate的FIFO candidates才hydrate inline/blob正文，不得为quote一次性加载128 MiB。longest-first search至多评估128个prefix、同时只保留一个trial output，并共享immutable base/prefix；全部planning受一个absolute deadline约束。compiler的fingerprint、lowering、degradation、layout与first-party estimator循环必须在bounded item seam合作式检查同一deadline；compatible append复用installed epoch的token breakdown，不得为每个trial重新估算完整prefix。`256 MiB` cumulative canonical planning-work quote只对immutable base计一次，并对本cut中实际hydrate/物化的候选suffix计一次；不得因尝试N个nested prefix而把同一base重复收费N次，导致在到达合法短前缀前产生虚假resource exhaustion。超过unique-work quote仍须在consume前typed fail。deadline在任何consume前耗尽时零canonical mutation，按existing operation-timeout terminalization处理；worker必须物理退出后logical waiter和Host close才可返回。步骤5以后到达的新steer不属于该plan，留给下一safe point。若base/control、prepared target、surface borrow或source contract在首次canonical mutation前失效，整个plan无副作用discard并从步骤1重建。步骤9以后发现不兼容只能由post-consumption exact join/preflight按failure matrix明确terminalize，不能采集第二套facts补洞。

planning DTO至少冻结：

```python
@dataclass(frozen=True, slots=True)
class SteerSuffixAdmissionQuote:
    selected_item_count: int
    selected_canonical_utf8_bytes: int
    prospective_snapshot_hydrated_bytes: int
    resulting_epoch_logical_bytes: int
    resulting_target_estimate: TokenEstimate
    quote_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedSteerSuffixAdmissionPlan:
    scope: ProviderInputContinuityScope
    predecessor: ProviderInputAdmissionPredecessor
    base_cut_fingerprint: str
    base_canonical_frontier_fingerprint: str
    base_compile_snapshot_fingerprint: str
    target_binding_fingerprint: str
    tool_surface_fingerprint: str
    source_facts_fingerprint: str
    ordered_pending_queue_fingerprints: tuple[str, ...]
    selected_consumption_candidates: tuple["PreparedSteerConsumptionCandidate", ...]
    quote: SteerSuffixAdmissionQuote
    prospective_compiled_input: FrozenCompiledModelInput
    plan_fingerprint: str
```

`quote_fingerprint`使用`pulsara:steer-suffix-admission-quote:v1`，覆盖selected candidate fingerprints、protected-prefix summary、item/body/snapshot/epoch measures、完整`TokenEstimate`、effective target budget及estimator fact fingerprint，排除自身。central factory先生成全部stable candidate，再完成plan。`plan_fingerprint`使用`pulsara:prepared-steer-suffix-admission-plan:v1`，覆盖上述全部semantic fields和prospective compiled-input fingerprint；不覆盖transport、callback、writer guard、deadline或opaque borrow token。Host另持有与`target_binding_fingerprint` exact join的既有`PreparedKernelModelCall`，以及与`tool_surface_fingerprint` exact join的process-local borrow；本call始终使用这两个frozen/pinned authority object，不因registry/config随后变化重新resolve/reborrow。该plan不是canonical truth；只有步骤9 FULL并经步骤10 exact join后，prospective input才能注册为`PreparedProviderInputAppendCandidate`。

Host不能在每次consume调用中临时生成entry/event ID。每个待消费lane head由central factory冻结process-local candidate：

```python
@dataclass(frozen=True, slots=True)
class PreparedSteerConsumptionCandidate:
    session_id: str
    queue_item_id: str
    queue_sequence: int
    command_id: str
    exact_target_turn_id: str
    content: CanonicalContent             # immutable inline/blob identity, no copy
    new_entry_id: str                  # stable from session + queue item
    occurred_at: datetime              # frozen across retry
    actor_id: str
    predecessor: ProviderInputAdmissionPredecessor
    prompt_consumed_occurrence: CommittedEventDraft
    user_steer_accepted_occurrence: CommittedEventDraft
    candidate_fingerprint: str
```

candidate由exact current target-lane head生成；write transaction必须重新lock并校验该row仍为相同lane head、`PENDING`、target仍RUNNING且全部content identity相同。`new_entry_id`使用closed deterministic namespace，例如`stable_id("steer-entry", session_id, queue_item_id)`；两个event ID分别使用`stable_id("event", queue_item_id, "PromptConsumed")`与`stable_id("event", new_entry_id, "UserSteerAccepted")`。两个`CommittedEventDraft`冻结exact type、typed subject、occurred_at、actor、sensitivity、projection profile及canonical frozen payload；retry不得换ID、time、actor、payload或predecessor。

`candidate_fingerprint`使用`pulsara:prepared-steer-consumption:v1`并覆盖session、queue identity/sequence/command、exact target、完整canonical content manifest、entry identity、predecessor closed union以及两个`_event_manifest()`；排除writer generation、deadline和event sequence（sequence只由成功transaction分配并由winner返回）。central factory必须重算fingerprint，caller不能自报。

消费write发生exception/ACK unknown后，Host必须对同一candidate执行stateless exact confirmation：

```text
FULL(AcceptedSteerDispatchEntry)
    queue row == CONSUMED + exact consumed_entry_id
    USER_STEER entry == exact turn/content/sequence identity
    PromptConsumed occurrence == exact frozen event ID + queue -> entry edge
    UserSteerAccepted occurrence == exact frozen event ID + entry/source/actor

NONE
    exact queue row仍PENDING且candidate entry/events均不存在

CONFLICT
    partial、foreign entry、identity drift或不兼容terminal winner
```

`FULL`加入本次Host accumulator；`NONE`只允许在本次safe-point absolute I/O deadline内重试相同candidate；`CONFLICT`fail closed并按现有canonical terminalization规则结束turn。deadline耗尽后使用独立bounded terminalization deadline，不能复用已经过期的operation deadline。若数据库故障使terminalization也无法确认，Host session进入fail-closed/degraded并继续pin active slot直到close/takeover处理，不能谎称turn已结算。确认不写receipt，也不证明provider执行。Host-owned ROOT runner在所有已开始candidate均达到`FULL | NONE-retry-exhausted-and-terminalized | CONFLICT-terminalized`之前不得退出并释放active slot；caller cancellation只能交给Host close/runner的bounded terminalization settlement，不能留下canonical `RUNNING` turn而没有physical runner。

safe point只能消费满足以下三个gate的target-lane FIFO最长前缀：

```text
ITEM GATE
    maximum steer items / safe point = 128

CANONICAL BYTE GATE
    cumulative candidate UTF-8 body bytes <= 16 MiB
    prospective whole canonical snapshot hydrated bytes
        <= existing reader 16 MiB
    cumulative longest-first canonical planning work <= 256 MiB

RESULTING DISPATCH QUOTE
    resulting epoch logical working-set bytes
        <= existing compiler 64 MiB
    resulting exact target estimate
        <= effective provider input budget
```

三个gate共同组成`SteerSuffixAdmissionQuote`；quote必须覆盖已有protected prefix、prospective ordered `USER_STEER` items、message/envelope overhead、minimum required runtime observations和frozen tool surface。它使用已accepted queue rows中的canonical content identity/body做只读prospective lowering，不把queue row变成第二套conversation truth，也不打开provider。consume FULL后，canonical reader必须exact join相同entry/content/order；由canonical join提升的compiled-input fingerprint与全部计量必须等于plan quote。这里没有第二次compile；任一不一致均为typed conflict且provider open=0。

如果已经选出非空prefix，第一项不再满足本批剩余budget时保持`PENDING`，由下一safe point重新quote。若lane head单独相对当前fixed prefix也无法满足prospective snapshot、epoch或target budget，则不能持续返回empty造成RUNNING turn空转：该head确定性`REJECTED(STEER_INPUT_RESOURCE_EXHAUSTED)`，当前turn以`PROVIDER_INPUT_RESOURCE_EXHAUSTED` interrupted；不得把它降级为future `NEW_TURN`或隐式触发compaction。

这两个terminal transition必须由一个prepared rejection candidate及**单一Host-writer transaction**完成：

```python
@dataclass(frozen=True, slots=True)
class PreparedSteerResourceRejection:
    source_plan_fingerprint: str
    queue_item_id: str
    queue_sequence: int
    exact_target_turn_id: str
    expected_content_digest: str
    reason: Literal["STEER_INPUT_RESOURCE_EXHAUSTED"]
    occurred_at: datetime
    actor_id: str
    prompt_rejected_occurrence: CommittedEventDraft
    turn_interrupted_occurrence: CommittedEventDraft
    candidate_fingerprint: str
```

central factory为两个occurrence冻结deterministic event ID和完整event manifest：`PromptRejected`使用`stable_id("event", queue_item_id, "PromptRejected:STEER_INPUT_RESOURCE_EXHAUSTED")`，`TurnInterrupted`使用`stable_id("event", exact_target_turn_id, queue_item_id, "TurnInterrupted:PROVIDER_INPUT_RESOURCE_EXHAUSTED")`。candidate fingerprint使用`pulsara:prepared-steer-resource-rejection:v1`覆盖全部字段。Host在进入transaction前证明plan仍由相同continuity predecessor、frozen target和active surface borrow拥有；borrow在transaction结束前不得撤销。existing Host-writer transaction入口先通过guard锁session row（同一行拥有entry/event allocators），随后按queue row -> target turn的现有repository顺序加锁并重新证明：row仍为target-lane head/PENDING且content identity相同、turn仍RUNNING、canonical base/control frontier与source plan绑定相同。随后在同一commit中：

1. queue row改为`REJECTED(STEER_INPUT_RESOURCE_EXHAUSTED)`；
2. 写既有`PromptRejected` occurrence；
3. 执行existing interrupt语义要求的open interaction cleanup；
4. turn改为`INTERRUPTED(PROVIDER_INPUT_RESOURCE_EXHAUSTED)`；
5. 写既有`TurnInterrupted` occurrence。

commit ACK unknown使用该candidate的queue/turn rows、两个event drafts及受影响的open-interaction terminal状态做`FULL | NONE | CONFLICT` exact confirmation；`FULL`要求全部required rows/events一致，`NONE`要求queue仍PENDING、turn仍RUNNING、相关interaction未被本candidate改变且两个event均不存在，partial/foreign winner均为`CONFLICT`。剩余指向该turn的steer只在该transaction FULL以后按现有bounded terminal-target cleanup收口。不得先commit queue rejection再调用独立`interrupt_turn()`。

普通consume confirmation conflict与post-consumption quote/join mismatch不是“resource rejection”：已经`CONSUMED`的entry不可回滚或改写为REJECTED。若exact read证明turn已terminal，返回historical terminal settlement；若turn仍RUNNING，则以reason `PROVIDER_INPUT_PLAN_CONFLICT`和`stable_id("event", turn_id, source_plan_fingerprint, "TurnInterrupted:PROVIDER_INPUT_PLAN_CONFLICT")`冻结另一个`TurnInterrupted` draft，调用existing atomic turn-interrupt transaction并exact-confirm，随后清理剩余pending steer。无法确认canonical terminal winner时Host fail closed并pin active slot，不伪造成功。

不能把该批次压成count，也不能把正文拼成一个message。process-local successful-consumption carrier为：

```python
@dataclass(frozen=True, slots=True)
class AcceptedSteerDispatchEntry:
    queue_item_id: str
    queue_sequence: int
    entry_id: str
    entry_sequence: int
    target_turn_id: str
    content_digest: str
    content_size: int
    prompt_consumed_event_id: str
    prompt_consumed_event_sequence: int
    user_steer_event_id: str
    user_steer_event_sequence: int


@dataclass(frozen=True, slots=True)
class AcceptedSteerDispatchBatch:
    entries: tuple[AcceptedSteerDispatchEntry, ...]  # 1..128, exact FIFO order
    canonical_utf8_bytes: int
    resulting_epoch_logical_bytes: int
    batch_fingerprint: str
```

`batch_fingerprint`使用domain separator `pulsara:accepted-steer-dispatch-batch:v1`，覆盖session、exact target turn、三重quote summary及ordered `(queue_item_id, queue_sequence, entry_id, entry_sequence, content_digest/size, prompt_consumed_event_id/sequence, user_steer_event_id/sequence)`；carrier不复制正文、permission、callback或repository handle。validator直接用carrier证明lane FIFO、byte total与event order，不为补回`queue_sequence`再次查询repository。

本轮冻结为bounded atomic per-item transactions，贴合现有repository owner且避免再造batch receipt：Host按quote选出的FIFO prefix逐项构造上述stable candidate；每项都走`FULL | NONE | CONFLICT` confirmation，再加入process-local accumulator。Host accumulator必须保留已经FULL commit的exact entries，异常/cancel不能把已接受prefix遗失或重新消费。每条steer的queue transition、`USER_STEER` entry及既有committed occurrences仍由同一Host writer transaction原子接受；batch/quote本身不落库。未来若证据证明单transaction batch有必要，必须先冻结等价的stable batch candidate与exact confirmation，不能在本轮实现者临场切换。

无steer返回empty/`None` branch，不伪造entry。canonical reader必须证明batch entries按相同顺序出现在current turn的new delta中，全部origin为`HUMAN_STEER`且target等于当前turn。每个entry分别lower成独立provider-visible user message；batch只共享下一次dispatch，不共享message identity。batch最后一项生成本call的`NewTriggerAnchor`并提供唯一textual skill activation subject；前序steer属于`canonical_delta_before_trigger`，仍完整进入provider input但不重复激活skill。这样保留当前“latest accepted steer wins textual activation”语义。

一次safe point吸收`N`项steer后只启动一次后续model call。若在该call freeze之后又接受新steer，它属于下一safe point，不得修改已安装candidate。assistant/tool-request与matching ordered tool results或provider-only interruption closure仍必须先闭合；steer不能插入tool group内部，也不能取消已经physical dispatch的普通tool。未来若增加显式可中断wait tool，必须像Codex一样通过独立typed process-local wake contract接入，不得让普通steer获得通用effect cancellation authority。

`NEW_TRIGGER`只允许指向本次run/steer/continuation admission已经接受的exact canonical item：

- human `USER_MESSAGE`；
- human `USER_STEER`；
- `PLAN_CONTINUATION`；
- `TERMINAL_OBSERVATION`；
- `SUBAGENT_OBJECTIVE`；
- accepted subagent/job result形成的ROOT entry。

closed origin/item-kind mapping与group boundary由canonical reader在同一frozen snapshot中exact join，不能按正文、最大sequence或“最后一个USER”猜测。新ROOT/child turn的首次call使用该turn canonical `initial_entry_id`；steer consumer按上述bounded batch、automatic/external continuation producer按single accepted entry把identity交给runner。普通同turntool loop使用`NO_NEW_TRIGGER`。

同进程发生tool/model/lowering compatibility reset时，reset candidate必须携带旧epoch predecessor frontier用于定位真正new delta；不能因为新epoch从全history materialize就丢掉因果anchor。cold Host首次call没有predecessor frontier，但仍必须使用当前accepted turn的exact admission entry；若无法证明anchor，typed fail且provider open=0。

若存在trigger：

```text
old prefix
+ canonical_delta_before_trigger
+ newly emitted runtime observations
+ trigger_and_remaining_delta
```

若不存在trigger，例如同一turn的tool loop：

```text
old prefix
+ canonical_delta
+ newly emitted runtime observations
```

canonical items的相对顺序永远不变；planner只在provider transcript group边界中插入runtime-owned observation。trigger若位于tool group内，anchor必须提升到整个group之前或之后的合法边界，不能把assistant tool request与matching result拆开。

### 5.5 No-op、clear与call observation

- `SNAPSHOT_ON_CHANGE`：按5.6完整presence/fingerprint矩阵决定append/no-op。
- `CLEARED | UNAVAILABLE`与`VALUE`都是正式head presence，不能用“没有candidate”暗示。
- `TURN_APPEND/TURN_SNAPSHOT`：同turn只发一次；新turn重新发，即使effective value相同。
- `CALL_APPEND`：每个完成CAS installation的dispatch candidate发一次；retry同一one-shot execution复用同一clock正文。该observation表达Runtime接受的dispatch时点，不证明provider已经读到request；open失败后也不回滚。
- `ONE_SHOT`：按canonical handoff identity在本进程至多发一次；Host replacement可在新epoch重新投影当前必要事实，不承诺旧one-shot历史复现。

`ONE_SHOT`的canonical handoff identity来自reader冻结的transition fact，而不是provider-visible body。显示正文相同但`transition_semantic_digest`或carrier identity不同必须append；同一exact transition的重复collect才可no-op。approved-plan exact materialization只能在该新occurrence被接受后执行，不能被前一个workflow的显示等价head抑制。

source collector失败不得让planner自行沿用“当前值”并假装fresh。required source失败使compile typed fail；optional source按其closed failure disposition省略或追加typed unavailable observation。

### 5.6 Stateful replacement与invalidation

任何已经在epoch安装过head的`SNAPSHOT_ON_CHANGE | TURN_APPEND | TURN_SNAPSHOT | ACTIVATION_SNAPSHOT` source，都必须按以下完整3×3矩阵处理。表中“same”表示presence和effective semantic fingerprint都相同；“different”包括body、closed reason、contract或lifecycle occurrence identity任一变化：

| Previous installed head | Current `VALUE` | Current `CLEARED` | Current `UNAVAILABLE` |
|---|---|---|---|
| `VALUE` | same → no-op；different → append VALUE | append CLEARED | append UNAVAILABLE |
| `CLEARED` | append VALUE | same → no-op；different → append CLEARED | append UNAVAILABLE |
| `UNAVAILABLE` | append VALUE | append CLEARED | same → no-op；different → append UNAVAILABLE |

对于`TURN_APPEND | TURN_SNAPSHOT`，internal lifecycle occurrence key包含当前turn identity，因此跨turn即使provider-visible body相同也属于different并append；同turn的重复model loop才可no-op。`ACTIVATION_SNAPSHOT`的occurrence key为new-turn identity或latest accepted human-steer entry identity；同一batch/普通tool loop重复collect为no-op。`SNAPSHOT_ON_CHANGE`不把普通call/turn identity混入fingerprint，只有domain state真正变化才append。

该minimum variant必须足以告诉模型旧current-state不再有效；它可以省略catalog详情，但不能让catalog A继续冒充current B。若minimum replacement/invalidation无法进入预算，compile以`STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET`失败且provider open=0。

只有**当前epoch从未安装该optional source**时，首次VALUE/UNAVAILABLE candidate才可按Round 3 policy被完全省略；省略不创建或推进head，后续call仍可重试。`CALL_APPEND` clock和`ONE_SHOT` transition不是current-state head，不适用旧值invalidation。所有head只随exact installed candidate推进。

---

## 6. Process-local DTO与状态机

### 6.1 Epoch snapshot

```python
@dataclass(frozen=True, slots=True)
class FrozenProviderInputEpochView:
    scope: ProviderInputContinuityScope
    epoch_nonce: str                    # process-local only
    epoch_revision: int
    compatibility: ProviderInputEpochCompatibility
    system_prompt: str
    tools: tuple[FrozenToolSpec, ...]
    messages: tuple[LLMMessage, ...]
    canonical_frontier: ProcessLocalCanonicalFrontier
    source_heads: tuple[ProcessLocalSourceHead, ...]
    logical_utf8_bytes: int
    semantic_prefix_fingerprint: str
```

约束：

- frozen view不含transport、callback、repository、lease或writer guard；
- opaque Host/tool-surface authority仍由existing borrow单独持有；
- `semantic_prefix_fingerprint`从system/tools/messages重算，不能由caller自报；
- current view是process-local优化和ordering guard，不是historical audit record。

### 6.2 Planning input与Prepared append candidate

continuity owner首先在scope lock内冻结一个不含compiler结果的planning input：

```python
@dataclass(frozen=True, slots=True)
class FrozenProviderInputAppendPlanningInput:
    scope: ProviderInputContinuityScope
    predecessor: ProviderInputAdmissionPredecessor
    predecessor_view: FrozenProviderInputEpochView | None
    dispatch_anchor: ProviderInputDispatchAnchor
    canonical_delta_fingerprints: tuple[str, ...]
    planning_fingerprint: str
```

`predecessor`与`predecessor_view`必须exact对应：EMPTY iff view为`None`；INSTALLED iff view的revision/frontier/prefix全部相等。它只是immutable predecessor/anchor snapshot，不进入`PREPARED` owner state，也不包含空占位`FrozenCompiledModelInput`。在steer路径，它从5.4.3已完成post-consumption exact join的`PreparedSteerSuffixAdmissionPlan`提升而来，必须复用plan中的prospective compiled input和facts；不得重新调用collector/compiler。无steer路径则由相同planning owner直接构造。caller随后才构造以下candidate并以planning fingerprint做CAS registration：

```python
@dataclass(frozen=True, slots=True)
class PreparedProviderInputAppendCandidate:
    scope: ProviderInputContinuityScope
    epoch_nonce: str
    expected_epoch_revision: int
    predecessor_prefix_fingerprint: str | None
    dispatch_anchor: ProviderInputDispatchAnchor
    resulting_compiled_input: FrozenCompiledModelInput
    resulting_canonical_frontier: ProcessLocalCanonicalFrontier
    resulting_source_heads: tuple[ProcessLocalSourceHead, ...]
    appended_message_count: int
    reset_reason: ProviderInputEpochResetReason | None
    candidate_fingerprint: str
```

candidate覆盖完整resulting input，但不持久化。它必须冻结本次dispatch anchor、clock/source variant、canonical delta lowering和tool-result render decision；retry不得重新采样clock、重选variant或改换anchor。registration必须重验owner仍处于planning input声明的`EMPTY`或exact predecessor `INSTALLED` revision；若期间已改变，只丢弃本次compiler结果并fail stale，不覆盖winner。

### 6.3 Closed owner state

```text
EMPTY
  -> PREPARED(initial)
  -> INSTALLED(v1)

INSTALLED(vN)
  -> PREPARED(successor, retains vN)
  -> INSTALLED(vN+1)

PREPARED(initial) --discard--> EMPTY
PREPARED(successor) --discard--> INSTALLED(vN)

EMPTY | PREPARED | INSTALLED -> CLOSED
CLOSED -> terminal
```

`PREPARED(successor)`必须同时保留old installed view与new candidate；new candidate在CAS install前不是current epoch。每个scope同时最多一个`PREPARED` candidate。borrow acquisition、DirectModel preflight、caller cancellation或validation在install前失败，origin runner必须调用exact `discard(candidate_fingerprint)`并走上述返回边；foreign/stale discard fail conflict，不能清掉另一candidate。并发第二次registration、stale epoch revision或candidate fingerprint mismatch必须fail typed，不能覆盖。close从任意非terminal状态原子丢弃candidate/current view并进入`CLOSED`。

### 6.4 Dispatch linearization

当前`KernelModelPort.stream()`把identity/binding/thaw/final estimate validation藏在async generator首次迭代中，并紧接着打开transport，无法满足continuity CAS的插入位置。Round 3.1必须把DirectModel接口hard-cut为显式两阶段：

```python
class KernelModelPort(Protocol):
    def prepare_call(...) -> PreparedKernelModelCall: ...
    def preflight_execution(
        request: KernelModelExecutionRequest,
        *,
        expected_append_candidate_fingerprint: str,
    ) -> PreparedKernelModelExecution: ...


class PreparedKernelModelExecution:
    # process-local, transport-bearing, one-shot, never serialized
    final_context: LLMContext
    execution_fingerprint: str

    def open_once(
        self,
        permit: ProcessLocalProviderInputInstallPermit,
    ) -> AsyncIterator[ProviderStreamPayload]: ...
```

`preflight_execution()`必须完成现有`stream()`在`open_stream()`之前的全部工作：request/cut/compile binding/scope exact join、完整tool binding revalidation、schema thaw、final `LLMContext`冻结、transport-aware validation及compiler/final estimate equality。它不得打开transport或产生live event。

one-shot execution使用closed process-local状态：

```text
PREFLIGHTED -> OPENING -> STREAMING -> PHYSICALLY_CLOSED
PREFLIGHTED -> DISCARDED
```

`open_once()`必须原子完成`PREFLIGHTED -> OPENING`；caller在CAS后取消且尚未open时只能`DISCARDED`，不能稍后由另一task恢复。已open路径继续使用现有physical completion drain，不能只关闭async iterator就释放borrow。

continuity CAS成功后返回opaque、process-local `ProcessLocalProviderInputInstallPermit`，覆盖scope、epoch revision、candidate fingerprint与execution fingerprint。公开字段只用于correlation，不能构成authority：Host continuity owner必须登记exact permit对象，并把一个密封、owner-bound verifier交给本次preflight execution；`open_once()`只消费该owner签发的同一对象且只能消费一次。手工构造或`replace()`得到的same-shape permit、第二次open、foreign permit及candidate mismatch均fail closed。permit不是receipt、callback、durable capability或recovery token，Host close时直接消失。

最终调用顺序：

```text
1. Host scheduler交付exact initial/continuation admission或steer wake；尚不consume steer
2. continuity owner冻结EMPTY | INSTALLED predecessor/current epoch view
3. freeze/read current canonical base cut与one-cut permission/Plan facts
4. prepare exact target、scope-filtered tool surface并acquire/pin process-local borrow
5. freeze one temporal/non-trigger source capture
6. bounded-read target-lane PENDING facts；central factory生成stable entry/event candidates
7. 使用相同pure compiler从最长到最短评估FIFO prefix，冻结
   PreparedSteerSuffixAdmissionPlan + prospective FrozenCompiledModelInput
   （无pending steer时对base canonical delta执行一次普通compile）
8. 对selected consumption candidates逐项write/exact-confirm；失败按5.4.3 settlement
9. re-read canonical cut，证明只在base current-scope frontier后追加exact selected entries；
   以相同order/content/event identity提升dispatch anchor与precompiled input
10. 构造完整PreparedProviderInputAppendCandidate并CAS register为PREPARED
11. DirectKernelModelPort.preflight_execution完成全部pre-open validation并冻结one-shot execution
12. continuity owner CAS install candidate并签发process-local install permit
13. PreparedKernelModelExecution.open_once(permit)打开physical provider stream
```

第12步是process-local prefix installation linearization point。此后无论provider返回error、caller cancellation还是transport open失败，都不回滚epoch：canonical输入已经存在，保留它只可能降低一次cache命中，不会丢失或伪造conversation fact。

第8步之前失败没有canonical steer mutation或registered append candidate；第8步任一FULL后失败必须先完成全部started consume confirmation，再按failure matrixterminalize，不能释放RUNNING owner。第10步之前仍没有registered append candidate；第10至第12步之间失败必须exact discard并恢复`EMPTY`或old `INSTALLED`。transport内部retry必须复用同一个已经安装的`LLMContext`，不能重新调用collector/compiler。

existing `ProcessLocalToolSurfaceBorrow`从第4步开始pin exact quote surface，并覆盖后续planning、provider stream、assistant canonical acceptance，以及该assistant响应产生的全部tool authorize、attempt acceptance、physical invoke、tool-result settlement。无tool call时可在assistant terminal acceptance后释放；有tool batch时只能在全部result已canonical settle或异常路径physical work已drain后释放。第8步之前plan无副作用discard时可以释放；任一steer FULL以后若borrow失效，必须terminalize而不能重借另一代surface继续。不得在provider stream结束时提前释放。continuity owner不能成为tool authority。

### 6.5 Cancellation与Host close

| 位置 | disposition |
|---|---|
| planning/quote阶段、任何consume FULL前cancel | 无canonical steer mutation/registered append candidate；释放borrow，EMPTY/old INSTALLED保持 |
| 第一个consume attempt开始后cancel/exception | cancellation只detach caller；Host完成每项FULL/NONE/CONFLICT confirmation；已有FULL则exact join后继续或明确interrupt，不释放active slot |
| consume FULL后post-read/quote join/preflight失败 | accepted steer保持canonical；用fresh bounded terminalization transaction interrupt exact turn，epoch不推进；不能重新采集facts继续 |
| PREPARED(initial)在install前cancel/fail | exact discard回EMPTY；已取得borrow先释放 |
| PREPARED(successor)在install前cancel/fail | exact discard恢复old INSTALLED；已取得borrow先释放 |
| DirectModel preflight失败 | execution未形成；按initial/successor exact discard并释放borrow |
| install后、transport open前cancel | epoch保持新prefix；turn按现有规则终结 |
| provider stream中cancel/error | epoch保持；live stream按现有规则abort；不恢复 |
| assistant已接受、tool batch执行中cancel/error | epoch保持；borrow在physical tool/result settlement完成或drain后释放 |
| Host close | 先按现有顺序drain/cancel provider与runner task，再清空epoch map |
| Host crash | epoch直接丢失；新Host cold bootstrap |

continuity owner本身不得启动background worker、timer、receipt monitor或close retry task。`PreparedKernelModelExecution`只允许由origin runner持有；caller cancellation不会把它转交background recovery owner。

---

## 7. Epoch reset contract

### 7.1 合法reset reason

```python
class ProviderInputEpochResetReason(StrEnum):
    COLD_HOST_BOOTSTRAP = "COLD_HOST_BOOTSTRAP"
    BASE_SYSTEM_CHANGED = "BASE_SYSTEM_CHANGED"
    TOOL_SURFACE_CHANGED = "TOOL_SURFACE_CHANGED"
    MODEL_TARGET_CHANGED = "MODEL_TARGET_CHANGED"
    PROVIDER_LOWERING_CHANGED = "PROVIDER_LOWERING_CHANGED"
    CONTEXT_BINDING_REWRITE = "CONTEXT_BINDING_REWRITE"
    EXPLICIT_TEST_RESET = "EXPLICIT_TEST_RESET"
```

`CONTEXT_BINDING_REWRITE`只能由未来PHC-07已经接受的context snapshot/revision触发；Round 3.1不得自行创建snapshot。

### 7.2 明确非法的reset reason

以下变化只追加observation或形成no-op：

- 新turn/run；
- current clock变化；
- cwd变化；
- permission preset/overlay变化；
- Plan enter/question/revise/approve/cancel/exit；
- active skill/catalog变化；
- normal tool call/result；
- provider error/cancel；
- provider reported cache miss/eviction；
- compile count或random context ID变化；
- tool-surface borrow/lease identity轮换但public descriptor/schema/executor binding不变。

### 7.3 Reset不是历史修复

reset只创建新的process-local epoch并重新materialize当前canonical cut。它不能：

- 修补canonical prefix conflict；
- 修改旧entry；
- 恢复旧Host输入；
- 声称历史provider实际看到新的表示；
- 触发provider retry；
- 写durable reset occurrence。

所有reset reason只进入bounded operational diagnostics。

---

## 8. Budget、degradation与Long-horizon seam

### 8.1 历史prefix成为protected input

已安装epoch中的system、tools和messages均为protected：

- 不再参与source degradation heap；
- 不再重新lower canonical tool result；
- 不允许因为history增长把旧`FULL`改为`COMPACT`；
- 不允许删除旧clock/permission/skill observation；
- 不允许把旧source重新移动到最新位置。

本次compiler只可对**尚未安装的suffix**选择variant。

### 8.2 Suffix allocation

预算顺序：

```text
fixed epoch prefix cost
+ required new canonical delta cost
+ current tool schema/root cost（必须与epoch相同）
+ new source observation candidates
```

新tool result和**从未在当前epoch安装过head**的new optional source仍可按Round 1/3 policy在首次进入prefix前选择`FULL/COMPACT/REF_ONLY/OMITTED_BODY`。一旦candidate install，该决定冻结。已有stateful head的replacement/invalidation必须遵守5.6的minimum MUST_KEEP规则，不得被完全省略。

### 8.3 Prefix pressure

若：

```text
fixed prefix + minimum required suffix > effective input budget
```

Round 3.1必须返回closed failure，例如：

```text
PREFIX_EPOCH_BUDGET_EXHAUSTED
```

并保证provider open count为0。不得：

- 回头降级旧prefix；
- 因cache miss重建；
- 静默裁剪canonical user/assistant/tool；
- 自行写compaction snapshot；
- 自动开启无authority的新epoch来逃避预算。

PHC-07实施后，runner可在provider safe point请求明确的Long-horizon snapshot；snapshot canonical acceptance和binding revision切换完成后，以`CONTEXT_BINDING_REWRITE`开启新epoch。

### 8.4 Physical memory bound

continuity owner只保留每个active scope的当前epoch view和至多一个prepared successor，不保留历史版本链。

冻结logical bounds：

```text
maximum logical bytes / epoch       = existing 64 MiB compile working-set cap
maximum active ROOT epochs / Host   = 1
maximum active child epochs / Host  = existing MAXIMUM_LIVE_SUBAGENTS (=4)
maximum Host resident epoch bytes   = 5 * 64 MiB = 320 MiB
maximum concurrent prepare bytes    = 5 * 64 MiB = 320 MiB
maximum aggregate logical bytes     = 640 MiB
```

实现应共享immutable `LLMMessage`引用，prepared successor只新增suffix，不能深拷贝旧正文。上面的640 MiB是五个scope同时达到现有单compile极限时的logical fail-safe，不是常态内存目标，也不包含Python对象开销；activation必须提供peak-RSS负载探针并记录实际值。closed child优先释放；active epoch不得为内存优化被静默淘汰。达到Host cap时拒绝新的prepare/subagent model call并返回typed resource pressure，不把它升级为durable job。

### 8.5 Incremental decision与budget report

`FrozenCompiledModelInput`继续携带完整`system_prompt/tools/messages`与final estimate，但Round 3.1后decision/report必须明确分层：

```text
protected_prefix_summary
    message_count
    logical_utf8_bytes
    token/component cost
    semantic_prefix_fingerprint

current_suffix_source_decisions
current_suffix_tool_result_decisions
current_suffix_budget_summary
final_total_estimate
```

`source_decisions`与`tool_result_decisions`若保留原字段名，其contract version也必须明确为“本次尚未安装suffix”，否则应在v2 DTO中重命名。degraded/omitted count同样只统计current suffix；protected prefix只保留aggregate count/cost/fingerprint，不保存历史逐source/tool-result decision链。

decision hard bound只作用于本次suffix；cold bootstrap时全history是本次suffix，仍受现有physical bound。`compiled_semantic_fingerprint`继续覆盖完整system/tools/messages、final estimate、protected summary与current decisions，不能因为不保留历史decision而遗漏真正provider input。operational report不得暗示suffix count是全会话累计值。

---

## 9. Provider adapter与cache observation

### 9.1 Adapter职责

`DirectKernelModelPort`继续拥有：

- transport-bearing `ResolvedModelCall`；
- `preflight_execution()`中的final target/tool/scope validation；
- privately owned ephemeral thaw与sealed final `LLMContext`；
- 唯一physical stream open；
- provider usage normalization。

preflight产生的mutable tool-schema dict只封存在one-shot execution内部，caller不能取得引用；`open_once()`不得重新thaw、重新估算或重新materializemessages。若transport adapter会原地修改context，它必须先复制自己的private request payload且不得反向改变execution fingerprint。

它不得：

- 在system/messages前注入动态prompt；
- 重新排序messages或tools；
- 重写source envelope；
- 按provider重新compile历史；
- 读取continuity source truth；
- 把cache miss反馈成Runtime mutation。

### 9.2 Adapter-final proof

hard correctness gate至少证明：

1. provider-neutral`LLMContext`满足strict prefix；
2. Chat Completions adapter对同一旧`LLMMessage`产生相同role/content/tool-call shape；
3. Responses adapter对同一旧`LLMMessage`产生相同input item shape；
4. system和tool array在epoch内完全相等；
5. adapter-native ordered input projection本身也满足old-item prefix，不得通过相邻user message coalescing改写上一call最后一个wire item；
6. adapter request extension不能加入provider-visible remote continuation或dynamic pre-prefix content。

可以增加pure test projection或transport payload capture seam；不得为证明prefix把provider-native payload持久化。

### 9.3 Operational observation

允许process-local/bounded telemetry：

```python
class ProviderInputContinuityObservation:
    scope_kind: str
    epoch_revision: int
    reset_reason: str | None
    predecessor_message_count: int
    appended_message_count: int
    strict_prefix_verified: bool
    input_tokens: int | None
    cached_input_tokens: int | None
    uncached_input_tokens: int | None
    opaque_prefix_fingerprint: str
```

禁止字段：session/turn/task raw ID、prompt正文、source正文、tool arguments、path、API key、base URL query、private URL、MCP secret。

provider不报告cached usage时为`None`，不能推断为0。cached tokens不从context budget扣除。

---

## 10. ROOT、subagent、reattach与future fork

### 10.1 ROOT

ROOT epoch在同一Host的多个turn、automatic Plan continuation、Terminal monitor autonomous continuation之间持续存在。Host不能因为runner task完成就清空ROOT epoch。

### 10.2 Subagent

每个`SUBAGENT_TASK(task_id)`创建独立epoch：

```text
ROOT epoch
child A epoch
child B epoch
```

三者system/tool/messages/source heads互不join。child结束即discard。Host结束时active child按现有规则`INTERRUPTED`，不恢复child epoch。

未来若产品决定让subagent成为跨Host durable work，也必须以新attempt读取canonical child transcript并冷启动新epoch；不能据此恢复旧provider generation。

### 10.3 Detach/reattach

客户端detach不影响Host epoch；只要Host仍存活，同一session继续使用它。Host被替换后，无论session ID是否相同，均cold bootstrap。

### 10.4 Future session fork

Round 3.1不实现session fork，也不允许普通scope lookup共享另一session的epoch。

未来fork的正确性依赖：

- canonical branch cut；
- 新session/writer/queue/live owner；
- fork cut之前的immutable canonical history；
- 新session自己的permission和后续turn。

fork不依赖跨进程prefix recovery。若未来同一Host显式fork希望优化provider cache，可以另行设计一个**显式、只读、exact-cut fork seed**；它必须由fork operation验证parent scope/cut，生成独立child epoch，不能成为通用跨session cache或durable artifact。Host replacement后的fork仍可cold start。

---

## 11. Failure matrix

| 故障 | Canonical truth | Epoch disposition | Provider disposition | 禁止行为 |
|---|---|---|---|---|
| optional clock采集失败 | 不变 | 本call不追加clock，diagnostic | 可继续 | 用旧clock冒充fresh |
| required environment/permission fact失败 | 不变 | candidate不安装 | open=0 | 猜测fallback |
| source contract/fingerprint invalid | 不变 | candidate不安装 | open=0 | 接受caller自报身份 |
| 已安装stateful source变化但minimum replacement无法容纳 | 不变 | head与candidate均不推进 | open=0 | 省略新值并让旧值继续生效 |
| canonical snapshot不是旧frontier扩展 | canonical可能损坏/contract drift | fail conflict | open=0 | reset掩盖冲突 |
| dispatch anchor缺失/不在exact delta/group boundary | 不变 | candidate不安装 | open=0 | 从全history猜最后一个trigger |
| pre-consumption plan在首次canonical mutation前发现base/source/target/surface drift | queue rows保持PENDING，turn保持RUNNING | plan discard；释放borrow后从fresh predecessor重建 | open=0 | consume后重新采集facts |
| duplicate prompt command ID但semantic candidate不兼容 | 既有winner保持 | epoch不变 | 不触发call | generic command fast path返回foreign winner |
| steer target在accept前已terminal/replaced | command/queue按existing contract typed reject | epoch不变 | 不触发call | 重绑新active turn或降级成NEW_TURN |
| 两条lane合计已达到128项admission cap | 新command未accepted，existing rows不变 | epoch不变 | 不触发call | 冒充delivery deadlock或静默挤掉future item |
| future NEW_TURN早于current-turn steer accepted | 两条row/event均保留各自audit sequence | steer先进入当前turn suffix；NEW_TURN等待turn terminal | 一次steer follow-up call | 用global FIFO让两条lane互锁 |
| steer consume commit ACK unknown | exact confirmation得到FULL/NONE/CONFLICT | FULL加入carrier；NONE重试same candidate；CONFLICT fail closed | 未确认前open=0 | 换entry ID/time、释放active slot或仅按queue status猜测 |
| bounded steer drain中途失败且已有entry FULL commit | 已接受entry保持canonical，其余queue row保持PENDING | 不丢失accepted carrier；未形成完整append candidate前open=0 | retry从exact frontier继续 | 重复消费、回滚entry或只保留count |
| 后续steer超出本safe-point count/body/quote剩余量 | 已选FIFO prefix可接受；该项及其后保持PENDING | 只安装已选prefix | 一次bounded follow-up call | 先消费全部再让compiler失败 |
| lane head单项相对fixed prefix无法容纳 | 同一transaction令head REJECTED + turn INTERRUPTED并写两个既有occurrence；其余target steer随后收口 | epoch不推进 | open=0 | 两个transaction间留下RUNNING turn、永久PENDING空转、降级NEW_TURN或隐式compaction |
| resource rejection commit ACK unknown | exact candidate确认queue/turn/two events FULL/NONE/CONFLICT | FULL终结；NONE重写same candidate；CONFLICT fail closed | open=0 | 只确认queue row或另调interrupt_turn |
| consume FULL后actual reader与prepared plan不一致 | 已FULL entries仍是canonical facts | precompiled input不提升；Host用stable turn-interrupt candidate明确terminalize且不释放悬空RUNNING owner | open=0 | 回滚entries、重新采集/recompile或继续发送不同payload |
| suffix compile超physical bound | 不变 | candidate不安装 | open=0 | 截断protected history |
| fixed prefix超过budget | 不变 | pressure，等待PHC-07或interrupt | open=0 | 隐式重建/降级旧prefix |
| tool surface schema变化 | 不变 |合法新epoch | 新完整call | 同epoch替换tools |
| opaque Host borrow变化但public surface相同 | 不变 | epoch保持；重验borrow | 通过才open | 把lease ID放入prefix |
| compile后borrow失败 | 不变 | registered candidate exact discard回EMPTY/old INSTALLED | open=0 | 遗留PREPARED或借用同名新executor |
| DirectModel preflight validation失败 | 不变 | registered candidate exact discard，execution不形成 | open=0 | install后才做首次validation |
| initial candidate在install前失败/cancel | 不变 | exact discard回到EMPTY | open=0 | scope永久停在PREPARED |
| successor candidate在install前失败/cancel | 不变 | exact discard恢复old INSTALLED | open=0 | 丢失old epoch或写durable abandonment |
| install后provider open失败 | 不变 | epoch保持resulting prefix | error | 回滚或自动重试tool |
| provider reported cache miss | 不变 | epoch保持 | 正常完成 | rollover/compaction/retry |
| telemetry/hook失败 | 不变 | epoch保持 | 不受影响 | 反向否定call |
| Host crash | canonical turn按规则interrupted | epoch消失 | 无stream replay | recovery graph |
| new Host attach | 读取canonical rows | cold epoch | 新call | 恢复旧prefix accumulator |

---

## 12. 实施切片

### R3.1-A：Failing oracle与architecture guards

在production mutation前先加入当前必然失败的回归：

1. 73 KiB history + 1秒clock变化，当前`message_lcp=0`；目标要求旧messages全部为prefix。
2. Round 4 permission/Plan revision变化不得改变system root或旧messages。
3. tool-result在后续call不得从FULL降为COMPACT。
4. ROOT/两个child scope不得join。
5. stateful catalog A→B在B full variant超预算时必须安装minimum replacement或fail，不能静默保留A。
6. DirectModel当前async-generator validation/open seam必须先由failing test证明无法插入CAS。
7. 当前global FIFO下`NEW_TURN head + later active-turn steer`可让turn保持RUNNING却无法消费steer；目标必须证明两条lane不互锁。
8. busy `Enter`、busy `Tab`、idle `Enter/Tab`及stale target形成closed command matrix；每个steer保持独立entry/message。
9. 当前consume临时entry ID + commit ACK unknown可形成canonical RUNNING turn却无physical runner；目标必须exact-confirm same candidate且不提前释放active slot。
10. 65项×1 MiB steer不能在consume后才撞64 MiB working set；目标必须只接受满足三重quote的FIFO prefix。
11. 同command ID跨`NEW_TURN | STEER_ACTIVE_TURN`复用必须`COMMAND_CONFLICT`，不能由Host generic query fast path返回旧winner。
12. 当前先consume后prepare target/source/tool的顺序无法产生exact quote；目标必须pre-plan、consume、exact join、promote同一compiled candidate。
13. 首次provider install前steer必须使用EMPTY predecessor并与initial prompt共享一次call。
14. 单项resource rejection与turn interruption必须同transaction；两个occurrence使用prepared deterministic identity。
15. AST/import guard禁止旧provider-input durability graph复活。

新增fixture必须只描述provider-neutral语义，不复制旧EventLog fixture。

### R3.1-B：Source channel与lifecycle hard cut

- base root升级为唯一SYSTEM source；
- 其他source改为runtime observation；
- 增加VALUE/CLEARED/UNAVAILABLE及closed lifecycle；
- runtime observation改用canonical JSON codec与fixed-point validator；
- stateful source变化必须有minimum replacement/invalidation；
- active skill每scope每turn重算，configured/textual activation分离；
- provider-visible render移除opaque IDs/fingerprints；
- source registry与compiler policy继续exact equal；
- 更新Round 3/4 source tests，不降低permission/Plan physical enforcement。

### R3.1-C：Host-scoped owner与incremental compiler

- 新增scope/compatibility/frontier/source-head/prefix proof DTO；
- Host持有ROOT/child epoch map；
- canonical item central fingerprint与prefix validation；
- 复用现有`provider_input_item_fingerprint()`，不新增第二算法；
- exact `NEW_TRIGGER | NO_NEW_TRIGGER` dispatch anchor与reset predecessor frontier；
- prompt repository按exact target steer lane与future new-turn lane分别FIFO，不再以cross-mode global delivery FIFO互锁；
- prompt ingress先用完整semantic candidate做compatible-winner query，closed区分command conflict、target stale与capacity exhausted；
- 增加EMPTY | INSTALLED closed predecessor；首次call前的steer与initial entry共享一个prospective plan；
- 在任何consume前冻结base cut、target、surface borrow、one-cut source capture和pending lane facts，以同一pure compiler建立`PreparedSteerSuffixAdmissionPlan`；
- steer consumer冻结含两个deterministic event drafts的stable per-item consumption candidate，并以canonical rows/events执行`FULL | NONE | CONFLICT` exact confirmation；
- planner按item count、canonical UTF-8 bytes与resulting epoch quote选择FIFO最长前缀，post-consume reader只exact join并promote相同precompiled input；返回bounded ordered accepted-entry batch而不是count，且保留queue/event sequences；latest entry唯一拥有dispatch/textual-skill anchor；
- single-head resource exhaustion在一个Host-writer transaction内原子写queue rejection、turn interruption与两个既有occurrence；
- 同一safe point的steer entries分别lower为user messages，只共享一次后续dispatch；
- 只lowercanonical delta；
- 旧compiled messages作为protected prefix；
- suffix-only degradation；
- protected-prefix aggregate report + current-suffix bounded decisions；
- cold bootstrap与closed reset reasons；
- 64 MiB/epoch、320 MiB resident/Host、640 MiB resident+in-flight aggregate logical bounds。

### R3.1-D：Runner、DirectModel与dispatch settlement

- continuity owner跨ROOT runner生命周期复用；
- subagent runner按task scope取得独立owner；
- 将DirectModel拆为`preflight_execution -> CAS install permit -> open_once`；
- predecessor/base read/target+borrow/source capture/pending quote/consume/exact join/promote/preflight/install/open顺序闭合；
- surface borrow覆盖assistant acceptance及完整tool batch/result settlement；
- transport retry复用同一candidate；
- consume/confirmation/terminalization settlement完成前，Host-owned ROOT task不得释放active slot；
- Host close/child terminal确定性释放；
- bounded operational observation接入现有usage observer，不成为gate。
- 既有Protocol v3 `SUBMIT_PROMPT | STEER_ACTIVE_TURN`保持不变；Go TUI补齐busy `Enter=steer`、`Tab=queue next turn`的双入口、hint与typed stale outcome，不新增wire type。

### R3.1-E：Dogfood、evidence与文档activation

- Chat Completions与Responses adapter payload projection回归；
- 12-call以上真实tool loop，逐call保存redacted local prefix proof和provider usage count；
- 同turntool loop、跨turn permission、Plan enter/exit、cwd、skill clear、Terminal autonomous continuation和subagent路径；
- busy期间连续3项steer一次safe-point吸收为3个user messages/1次model call；两个lane均已accepted时，已排队NEW_TURN不阻塞current-turn steer，current turn terminal后才开始future turn；
- 大steer backlog只消费满足三重quote的FIFO prefix；single head无法容纳时typed reject/terminalize且无RUNNING orphan；
- 无provider cached usage时仍可完成；
- 更新README的prompt construction简述；
- 生成`benchmarks/suites/core/v1/round3_1_provider_input_prefix_continuity_activation.json`；
- 通过后将本文标为`ACTIVATED`，并把Gap Index PHC-17更新为完整恢复。

各slice都必须保持可collection、可运行；不得用大批skip/xfail掩盖中间状态。

---

## 13. 预计修改面

### 13.1 Production

预期允许：

```text
src/pulsara_agent/model_input/contracts.py
src/pulsara_agent/model_input/compiler.py
src/pulsara_agent/model_input/lowering.py
src/pulsara_agent/model_input/continuity.py                 # new, pure
src/pulsara_agent/conversation_kernel/context_sources.py
src/pulsara_agent/conversation_kernel/input_continuity.py   # new, Host-owned
src/pulsara_agent/conversation_kernel/reader.py
src/pulsara_agent/conversation_kernel/repository.py
src/pulsara_agent/conversation_kernel/runner.py
src/pulsara_agent/conversation_kernel/direct_model.py
src/pulsara_agent/conversation_kernel/host.py
src/pulsara_agent/conversation_kernel/subagent.py            # only lifecycle wiring if needed
clients/terminal/internal/kernelapp/model.go                 # existing command kinds only
```

### 13.2 Tests/evidence/docs

```text
tests/test_round3_1_provider_input_prefix_continuity.py
tests/test_round3_structured_model_input_compiler.py
tests/test_stage2_canonical_reader.py
tests/test_stage2_conversation_runner.py
tests/test_stage2_direct_model.py
tests/test_stage2_conversation_kernel_postgres.py
tests/test_stage2_protocol_v3.py
tests/test_round4_plan_workflow.py
tests/test_round4_plan_postgres.py
tests/test_kernel_dogfood_suite.py
benchmarks/suites/core/v1/round3_1_provider_input_prefix_continuity_activation.json
README.md
README.zh-CN.md
POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md
clients/terminal/internal/kernelapp/model_test.go
```

### 13.3 明确禁止修改

除非review先证明存在独立产品契约冲突，Round 3.1不得修改：

```text
storage migration / clean-v0 SQL
conversation_kernel committed vocabulary
LiveAgentEvent vocabulary
subject slots / append guards
durable job catalog
Terminal Protocol v3 schema / generated Python-Go bindings / protocol major
memory schema/recall
tool attempt/result canonical transaction
Plan canonical workflow schema
```

---

## 14. Architecture guards

实施必须增加或更新以下静态/动态guard：

1. `model_input`不得import`conversation_kernel`、storage、event writer或provider transport。
2. production不存在`runtime.provider_input`、`ProviderInputGeneration`、`ProviderInputAppendCommitted`、`provider_input_recovery`等旧owner/import。
3. `ContextChannel.SYSTEM`只允许`BASE_SYSTEM`。
4. 所有dynamic first-party source必须lower为typed user-role observation。
5. provider-visible source body不含`*_id`、event sequence、fingerprint、writer generation或process nonce，除非closed allowlist证明它是模型操作所需的canonical reference。
6. continuity owner不得写数据库、event或blob，不得启动background worker。
7. scope key必须包含session + ROOT/child + exact task ID union。
8. old compiled messages不能重新进入source/tool degradation owner。
9. provider cache usage不能触发reset、retry、compaction或canonical mutation。
10. Host replacement测试必须观察cold bootstrap，而不是recovered epoch。
11. committed/live/subject/guard/relation/job oracle保持`34/23/15/2/26/4`。
12. `previous_response_id`、provider conversation/context management ingress继续被拒绝。
13. runtime observation production encoder只能使用closed canonical JSON codec；禁止closing-marker字符串插值。
14. existing stateful head变化后只能安装replacement/invalidation或fail；allocation omission不得推进head。
15. `KernelModelPort`必须暴露preflight与one-shot open seam；validation不得继续只藏在async generator首次迭代。
16. surface borrow不得在provider stream结束时释放，必须覆盖该response的assistant/tool batch settlement。
17. prefix identity只使用中央`provider_input_item_fingerprint()`；production不得出现第二个同义helper/domain。
18. steer consumer不得只返回count；每个item必须先冻结含deterministic entry ID、两个event drafts与closed predecessor的stable consumption candidate，并可由queue/entry/events exact-confirm；successful entries再以包含queue/event sequences的exact target-lane FIFO carrier交给reader/anchor validator。
19. safe-point batch必须在任何consume前，以同一base cut、target、pinned surface、one-cut source capture及pure compiler同时证明128 items、16 MiB cumulative canonical UTF-8 body、16 MiB prospective canonical snapshot、64 MiB resulting epoch及exact provider budget；production不得先consume再收集这些facts，或consume超界rows后依赖compiler失败。
20. `NEW_TURN`与`STEER_ACTIVE_TURN`只在各自lane内承诺delivery FIFO；production不得恢复cross-mode head blocking。该保证只覆盖已accepted rows，session-wide 128 admission cap保持有效。
21. 每个accepted steer必须保留独立queue item、canonical entry、provider item identity；禁止在repository/compiler/adapter内把batch字符串拼接为一个user message。
22. prompt command compatible winner必须exact compare既有semantic digest/target；generic command-ID status query不得绕过该检查，command conflict、target stale与capacity exhausted不得合并。
23. Protocol command vocabulary保持不变；busy `Enter`只能target exact active ROOT，显式`Tab`只能提交无target的`NEW_TURN`。stale steer不得自动重绑或降级。
24. `PREPARED(initial|successor)`所有pre-install失败路径必须exact discard回EMPTY/old INSTALLED；不得遗留PREPARED。
25. started steer-consumption attempt未得到exact FULL/NONE/CONFLICT settlement或明确canonical terminalization前，Host不得清除active ROOT slot。
26. EMPTY predecessor必须以revision 0 + nullable frontier/prefix的closed branch表达；不得为pre-first-call steer伪造frontier，initial prompt与该batch只允许一次首次provider open。
27. post-consumption path只能exact join并promote`PreparedSteerSuffixAdmissionPlan.prospective_compiled_input`；不得重新采样clock/source、重借surface或重新compile。
28. single-head resource exhaustion必须在一个Host-writer transaction内提交queue rejection、turn interruption、`PromptRejected`与`TurnInterrupted`；production不得串联两个repository transaction。
29. steer candidate与resource-rejection candidate的event IDs、event manifests及fingerprint domain/coverage必须由central factory确定；普通random `_event()`不得用于这些prepared paths。

---

## 15. Test matrix

### 15.1 Pure prefix tests

- identical canonical history + changed clock：previous messages exact prefix；
- 100 KiB/1 MiB multibyte history：UTF-8-safe，prefix item exact equal；
- source no-op不重复append；
- clock每installed dispatch candidate一次，same one-shot execution/transport retry不重新采样；
- VALUE→CLEARED→CLEARED只追加一次clear；
- VALUE(A)→VALUE(B)/UNAVAILABLE在minimum replacement被省略时typed fail，head保持A；
- VALUE/CLEARED/UNAVAILABLE完整3×3 same/different迁移矩阵全覆盖；
- optional source首次candidate被省略时不创建head，后续可以重试；
- new user前插入current source bundle，但不移动任何prior item；
- tool loop无new trigger时先appendcanonical tool delta，再appendcall observation；
- runtime observation不得拆开assistant tool request与matching ordered results；
- unresolved tool group使prepare typed fail且provider open count为0；
- previous tool result render mode永久冻结；
- suffix budget pressure只降级new unit；
- canonical old item drift fail closed；
- cold bootstrap、compatible append与mid-turn reset分别使用exact anchor，不扫描最后一个USER。
- initial USER_MESSAGE已canonical但epoch仍EMPTY时接受1..N项steer：closed EMPTY predecessor、latest steer为trigger、只compile/open一次；
- 2项与128个小型steer保持target-lane FIFO，latest entry作为唯一dispatch/skill anchor，前序项位于delta before trigger；每项仍是独立user message，batch只形成一次dispatch；
- 65×1 MiB及multibyte steer backlog只消费同时满足item/body/snapshot/epoch/target quote的最长FIFO prefix，其余保持PENDING；
- target/source/surface/clock capture只发生在pre-consumption plan；consume FULL后没有第二次collector/compiler调用；
- plan freeze至first consume间注入base/control/target/surface drift：零canonical mutation、discard/replan；
- lane head单项相对fixed prefix无法容纳时同一PostgreSQL transaction完成typed resource rejection + turn terminalization，重复wake不空转；
- 两条row均已accepted时，较早future `NEW_TURN`不阻塞current-turn steer；current turn terminal后future lane仍按自身FIFO启动；全局128 admission cap耗尽则新steer明确`CAPACITY_EXHAUSTED`；

### 15.2 Product source tests

- runtime cwd变化追加snapshot，不改system；
- permission mode每turn动态变化追加control observation，physical gate使用exact same snapshot；
- Plan enter/revision/approve/cancel产生正确VALUE/CLEARED/handoff顺序；
- configured skill在ROOT human、ROOT non-human与child turn均生效；
- textual skill只由exact ROOT human USER_MESSAGE/USER_STEER激活，下一non-human turn降为configured-only或clear；
- 同turn多steer只用batch latest正文更新textual skill；其后tool/non-human call不反向扫描旧human entry重新激活；
- Go/headless command matrix：idle Enter/Tab均为NEW_TURN；busy Enter为exact-target steer；busy Tab为future NEW_TURN；Plan interaction与SUBAGENT_TASK不能被快捷键绕过；
- capability catalog变化追加新snapshot；
- skill/path正文包含旧closing marker、引号、C0/C1时canonical codec仍fixed-point且不可逸出；
- source envelope正文不能让human伪造physical authorization；
- opaque Plan/permission IDs不进入provider text。

### 15.3 Scope/lifecycle tests

- ROOT连续多turn复用同epoch；
- automatic Plan continuation不清空ROOT epoch；
- Terminal monitor autonomous continuation不清空ROOT epoch；
- child A/B各自从revision 0开始，互不join；
- child完成释放；
- Host close释放全部；
- detach/reattach同Host保持；
- Host replacement同session cold bootstrap；
- 不同session和future fork不得通过普通scope lookup共享state。

### 15.4 Dispatch/failure tests

- compile fail：epoch不推进、provider open=0；
- tool borrow fail：epoch不推进、provider open=0；
- DirectModel preflight每一种identity/binding/estimate failure都发生在CAS前且provider open=0；
- stale candidate CAS：fail、provider open=0；
- initial/successor在borrow、preflight、cancel失败时分别回EMPTY/old INSTALLED；
- valid CAS permit只能open_once一次；same-shape forged copy、foreign/stale permit均无法通过Host-owner identity join；
- install后transport fail：epoch保持；
- provider stream结束后、assistant tool batch尚未settle时borrow仍active；全部result settle后才释放；
- provider internal retry使用同一clock和messages；
- caller cancellation各linearization位置符合failure matrix；
- observer/telemetry异常不影响run；
- cache miss usage不产生任何Runtime mutation。
- stale/replaced/non-steerable target typed reject，stable command query可确认compatible winner，且不重绑/不降级；
- 相同command ID以相同semantic candidate重试得到同一winner；以`NEW_TURN`/`STEER`、target、text或permission任一不同candidate重用得到`COMMAND_CONFLICT`；
- queue capacity、command conflict与target stale三个repository disposition不会被Host合并；
- 每个consume linearization位置注入commit ACK unknown：FULL exact recover、NONE重试same entry ID/time、CONFLICT fail closed；任一路径均不留下RUNNING turn而无active task；
- exact confirmation逐字段校验两个deterministic event drafts；carrier保留queue sequence及两个event ID/sequence并直接证明lane/event order；
- per-item batch第N项ACK unknown/cancel时，前N-1项carrier不丢失、不重复，N及以后按exact confirmation/PENDING收口；
- resource-rejection transaction在queue update、PromptRejected、turn interrupt、TurnInterrupted每个statement/commit边界注入故障；只允许全NONE或全FULL，ACK unknown按同一candidate确认；
- consume FULL后强制post-read mismatch：不得重compile或reject consumed entry，必须stable interrupt exact turn；
- steer safe-point drain发生在provider freeze之前且在完整tool group边界之外；freeze之后到达的steer只进入下一candidate；

### 15.5 Adapter与real provider

- Chat Completions request capture：old role/content/tool calls严格相同；
- Responses request capture：old input items严格相同；
- 连续两个user-role suffix不得触发跨call边界merge/coalesce而改写旧wire item；
- 同一safe point的3项steer在Chat/Responses capture中保持3个ordered user items，并只增加1次physical provider open；
- system/tools within-epoch exact equal；
- controlled exact replay证明provider cache可用；
- 12-call real agent trajectory hard-assert本地strict prefix 11/11；
- 记录input/cached/uncached/output tokens与predicted reset；
- remote cache ratio只作evidence，不作为flaky CI断言；若本地prefix全绿但remote仍低，单独报告provider cohort/TTL问题，不能修改canonical input规避。

---

## 16. Definition of Done

Round 3.1只有同时满足以下条件才可标记`ACTIVATED`：

1. 同一Host、同scope、同epoch的连续调用满足system/tools相等和messages strict prefix。
2. clock、cwd、permission、Plan、catalog、skill变化均只append，不重建前序。
3. old canonical item和old tool-result representation从不重新lower/degrade。
4. stateful source旧值只能被installed replacement/CLEARED/UNAVAILABLE取代；预算省略不能让stale state继续冒充current。
5. ROOT/child/session隔离通过；configured skill覆盖各scope，textual skill不跨human turn泄漏。
6. cold/reset/compatible路径均使用exact dispatch anchor；不从全history猜trigger。
7. 多项steer只消费满足item/body/snapshot/epoch/target quote的target-lane FIFO最长前缀；每项用stable candidate及FULL/NONE/CONFLICT exact confirmation闭合，保持独立canonical/provider message，latest accepted steer唯一承担dispatch/textual-skill anchor，整个batch只触发一次后续model call。
8. 双入口闭合：busy Enter steer exact active ROOT，busy Tab排队future NEW_TURN；已accepted rows的两条delivery lane不互锁，session-wide 128 admission cap仍typed生效；stale steer不重绑/降级，compatible command winner不绕过semantic digest，既有Protocol vocabulary不变。
9. initial/successor candidate可从所有pre-install失败路径确定回到EMPTY/old INSTALLED；无stuck PREPARED。
10. DirectModel preflight、Host-owner密封CAS permit与one-shot open形成可执行接口；公开字段相同的伪造permit不能打开transport，all validation在CAS前完成。
11. surface borrow覆盖provider、assistant acceptance及完整tool effect/result settlement。
12. Host replacement明确cold bootstrap；无cross-Host restore代码。
13. fixed prefix预算压力fail typed并在provider open前终止；不隐式裁剪。
14. 合法root/tool/model/provider/compaction compatibility变化才reset，并有closed reason。
15. Round 4 Plan read-only physical enforcement和send-time permission snapshot继续全绿。
16. Round 1 artifact、Round 2 Terminal、Round 3 compiler、Round 4 Plan retained suites全绿。
17. PostgreSQL full suite全绿，但Round 3.1不新增SQL/migration。
18. Chat/Responses adapter deterministic-prefix tests全绿。
19. full pytest、ruff、compileall、Go test/vet/module verify、protocol generator、`uv lock --check`、`git diff --check`通过。
20. 新增skip/xfail为0。
21. oracle保持`34/23/15/2/26/4`，new durable authority为0。
22. activation evidence记录基线HEAD、文档SHA、local prefix trajectory、双入口trajectory与redacted provider usage。
23. steer quote由pre-consumption plan在同一base/target/pinned surface/one-cut source capture上产生；consume FULL后只exact join并promote同一compiled input，collector/compiler调用次数测试闭合。
24. EMPTY predecessor的pre-first-call steer与initial message只形成一次provider open；所有prepared steer event ID/draft/fingerprint均稳定可确认，accepted carrier保留queue/event order identity。
25. single-head resource exhaustion以单一transaction提交queue rejection、turn interruption及两个既有occurrence；ACK unknown与statement-level fault injection不能留下部分winner或RUNNING orphan。
26. longest-first规划对同一immutable canonical base只计一次physical work并能到达合法的更短FIFO prefix；first-party source registry中每个kind必须exactly one `VALUE | ABSENT`，伪造或缺失的ABSENT policy在provider open前fail closed。

---

## 17. 建议验证命令

实际文件名可以随实现小幅调整，但handoff必须提供等价门控：

```bash
uv run pytest -q \
  tests/test_round3_1_provider_input_prefix_continuity.py \
  tests/test_round3_structured_model_input_compiler.py \
  tests/test_stage2_conversation_runner.py \
  tests/test_stage2_direct_model.py \
  tests/test_round4_plan_workflow.py \
  tests/test_round4_plan_postgres.py

uv run pytest -q

uv run pytest -q -m postgres

uv run ruff check .
uv run python -m compileall -q src tests
uv run python tools/generate_terminal_protocol_contract.py --check

(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)
(cd clients/terminal && go mod verify)

uv lock --check
git diff --check
```

Architecture grep至少包含：

```bash
rg -n \
  "ProviderInputGeneration|ProviderInputAppendCommitted|provider_input_recovery|runtime\.provider_input|previous_response_id" \
  src tests

rg -n \
  "ContextChannel\.SYSTEM|LEADING_OBSERVATION|TRAILING_OBSERVATION" \
  src/pulsara_agent/conversation_kernel/context_sources.py \
  src/pulsara_agent/model_input
```

第一条production结果必须为旧owner/remote continuation 0；第二条必须证明SYSTEM只剩stable base，旧leading/trailing全量重建路径已删除或明确退出production。

---

## 18. Handoff摘要

编码agent应把本轮理解为：

> 在现有Round 3 pure compiler前后增加一个非常窄的Host-scoped append lifecycle，使已经进入dispatch的provider-visible input在同scope内永远不被重写；所有动态事实成为新的typed suffix。同时闭合既有`SUBMIT_PROMPT | STEER_ACTIVE_TURN`的双入口：busy Enter在当前turn下一safe point追加，Tab排队future turn；多项steer以stable consumption candidate和三重physical quote选择FIFO最长前缀，保持多个user messages但共享一次后续dispatch。已accepted rows的两条delivery lane不互锁，global admission cap仍有效。不要重建旧ProviderInput durability subsystem，也不要新增Protocol/event vocabulary。

若实现需要新增以下任一项，应停止并回到文档review：

- PostgreSQL relation或migration；
- Committed/Live event；
- 第三种append guard；
- durable job handler；
- prompt/full input artifact；
- cross-Host prefix restore；
- 新prompt queue relation、priority column或第二套steer receipt/lease；
- provider remote continuation；
- background receipt/recovery worker；
- 以cache miss触发的compaction或Runtime mutation。

Round 3.1完成后，PHC-17才同时具备：

```text
typed structured compilation
+ exact canonical cut
+ frozen tool authority
+ process-local append-only prefix continuity
```

随后PHC-07 Long-horizon context window/compaction可以通过显式`CONTEXT_BINDING_REWRITE`接入，而无需再次发明prompt builder或provider-input recovery state machine。
