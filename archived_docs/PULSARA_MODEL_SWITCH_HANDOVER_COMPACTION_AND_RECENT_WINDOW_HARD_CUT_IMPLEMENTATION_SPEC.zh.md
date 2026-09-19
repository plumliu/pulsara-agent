# Pulsara 模型切换 Handover Compaction 与 Destination-side Projection Compaction Hard-cut 实施规范

> Epoch-boundary hard-cut 覆盖（2026-09-19）：provider-input epoch 的当前唯一权威是 [`PULSARA_PROVIDER_INPUT_EPOCH_BOUNDARIES_AND_CANONICAL_REPROJECTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_PROVIDER_INPUT_EPOCH_BOUNDARIES_AND_CANONICAL_REPROJECTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。本文中以 A/B same-epoch incompatibility 本身解释或授权 cold reset 的内容均为 historical/superseded；模型切换只能走显式 model-switch cold 子路径或 adopted compaction successor，不保留兼容路径。其余三档 handover 与不冲突语义继续有效。

> 状态：生产 hard cut 已实施；K4 Kernel 验收于 2026-09-15 通过，证据见 [K4 验收记录](PULSARA_KERNEL_IMAGE_INPUT_K4_ACCEPTANCE.zh.md)；U1/U2 浏览器链路另行验收。
>
> 图片输入修订（2026-09-14）：
> `PULSARA_KERNEL_IMAGE_INPUT_AND_OPENAI_WIRE_ADAPTER_DESIGN.zh.md` 第 8.1–8.9 节
> 已修订本文的 typed content、recent window 与 destination projection 规则。已知含 `text`
> 且不含 `image` 的 B 才执行 P；Tier 2 只在原规则选出的 recent 含真实 Image 时将整个
> recent 置空，Tier 3 的纯文本图片交接固定 recent=0；视觉或 unknown B 不执行 P，也不因
> Image 清空 recent。生产实现已沿原三档、safe boundary、一次采用与 PRE_FULL/POST_FULL
> 事务边界完成该 hard cut，不保留本文旧 text-only carrier 或双读路径。真实 provider、
> isolated wheel 与完整矩阵仍属于 K4，不能据此标记图片输入已激活。
>
> Conversation Fork 表示补充（2026-09-08）：destination projection 的 prior handoff 包含整个
> ordered typed `retained_historical_requests`。Tier-3 若省略 predecessor snapshot base，
> 新 carrier 必须原样保留旧列表，不把未进入 summary 输入的历史当成已吸收；之后 Fork 再将本轮
> `SNAPSHOT_EXACT` 请求追加到末尾。包含整个 base 的 adopted summary 才将列表清空。
> 这是 `PULSARA_CONVERSATION_FORK_EFFECTIVE_CONTEXT_COPY_SPEC.zh.md` 第 7.3.1 节的唯一
> carrier hard cut，不改变本文三档选择、admission 或合法 provider-prefix rebase 边界。
>
> 范围：Conversation Kernel、模型连接切换、provider-input continuity、context compaction、Web 会话页
>
> 权威关系：本文扩展
> `PULSARA_ROUTE_WIRE_API_MODEL_UNIVERSE_AND_ADAPTER_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`
> 的会话模型选择语义，并只在“已安装 source target 与下一条 `NEW_TURN` 的 destination target
> 不同”这条显式路径上扩展
> `ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md` 和
> `PULSARA_COMPACTION_FINAL_WIRE_ESTIMATION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`。
> 普通 automatic/manual/mid-turn compaction 继续遵守原规范，不获得 cross-model fallback。
>
> 核心决定：**模型切换只有三档。第一档按新 target 的 exact final-wire 直接切换；第二档由旧
> target 只执行一次 handover compaction，且从一开始就以
> `retained_tool_groups = 0` 生成唯一 summary candidate；第三档先把 canonical-derived history
> 降维成新 target 能读取的临时 projection source，再由新 target 自己立即执行一次 compaction
> summary，成功后才安装 successor 并处理当前用户请求。第二档不逐级试
> `3 -> 2 -> 1 -> 0`。第三档不总结第二档失败的 summary，也不把 projection 本身安装为 cold
> base；它总结的是从 current effective lineage 重新构造的来源忠实投影。临时 source 只受新
> target 的 resolved automatic trigger 与既有 physical hard admission 约束，不新增 `0.55`
> landing cap、固定比例分仓或任意 per-result size cap。**

---

## 0. 最终产品语义

Pulsara 不维护一个在每轮请求中悄悄向前移动的 provider-input 滑动窗口。同一 epoch 内继续要求：

- `SYSTEM` 与 provider `tools` byte-identical；
- `messages` 只按 suffix append；
- 只有 new cold epoch 和 explicitly adopted compaction successor 可以重建 provider-input root。

模型切换使用以下三档：

```text
Tier 1 — direct cold switch
    prospective B exact final-wire < B automatic trigger and hard-admitted
    -> 不压缩，直接建立 B cold epoch

Tier 2 — A handover compaction
    prospective B does not satisfy Tier 1
    (tokens >= B automatic trigger or another hard admission fails)
    -> A 以 retained_tool_groups = 0 生成唯一 handover summary
    -> B 对唯一 successor 做 exact final-wire measurement
    -> candidate < B automatic trigger 且 hard-admitted，立即采用

Tier 3 — B-side projected-source compaction
    唯一 handover candidate 不能产生可采用的 B successor
    -> 从current effective lineage构造来源忠实的dialogue backbone
    -> 按B exact summary-call final-wire选择最长safe suffix
    -> 在剩余空间内优先补入exact wire cost较小的tool-result引用
    -> B立即总结该临时source，成功后建立普通summary successor
    -> 再处理当前用户请求，并给用户一次克制的降级提示
```

这里的 `retained_tool_groups = 0` 不表示删除工具记录，也不表示只保留零条消息。它表示
不为已完成的旧 tool group保留额外 exact protected tail：A 尽量总结完整 safe prefix，
不可切分的当前 active request、未闭合tool group、required replay 与其他不安全跨越的后缀仍由
safe-boundary/continuation 契约精确保留。因此每个stable fenced capture的Tier 2只产生一个逻辑A
summary candidate；无repair时发出一个A provider request，summary违规调用tool时可追加既有唯一repair
request。A summary terminal后只产生一个pre-adoption B dry successor measurement。Canonical FULL后
始终另行measure一次no-Hook final base，并可选measure一次Hook sibling；这些不是第二个logical summary
candidate。Drift后的fresh capture重新计数。整个switch attempt只有canonical adoption与最终B
install/open保持至多一次。不达标就进入Tier 3。

Tier 3中的projection不是provider-visible epoch的最终形态。它只服务于当前stable fenced capture的
一个逻辑B compaction summary candidate（可附既有唯一repair）的临时输入；B不会先从这份塞近
trigger的projection正常回答几句、调用工具后再被动触发ordinary
mid-turn compaction。B必须先完成该summary candidate；successor通过B exact dry admission、atomic
adoption与post-FULL final selection后，才允许首次normal provider open处理当前请求。

所有档位都保留 PostgreSQL 中完整、逐字、逐序的 canonical rows。差别只在下一 installed epoch
使用哪一个 provider-visible context base。

---

## 1. 当前代码真值

### 1.1 当前 ordinary compaction 已有、但 handover 不再沿用的候选收缩行为

当前生产实现的 ordinary same-target compaction 具备以下行为：

1. `ResolvedCompactionPolicy.auto_trigger_ratio` 默认 `0.85`；
2. `post_compaction_target_ratio` 默认 `0.55`，只属于 ordinary same-target compaction；
3. `maximum_retained_tool_groups` 默认 `3`；
4. `enumerate_safe_summary_prefixes()` 枚举完整 safe boundaries，按最长 prefix 优先，不使用任意
   `MAX_PREFIX_TRIALS`；
5. `_execute_compaction_fenced_once()` 先寻找在 source target 上可执行的首个 summary prefix；
6. summary terminal 后，successor 通过唯一 cold assembler、普通 compiler、adapter materializer与
   local estimator 做真实 admission；
7. 如果真实 successor 因预算、post-target、required context 或 protected transcript 等可收缩原因
   失败，`_CompactionFencedRestart` 会把 `maximum_retained_tool_groups` 降到
   `selected_retained_count - 1`，fresh recapture 后重新规划；
8. 收缩持续到某个候选成功或 exact protected tool tail 已到 `0`。

所以现有 ordinary 路径的 candidate-shrink 准确含义是：

```text
normal candidate
    = 当前 exact cut 上，尽量保留更丰富完整 tail 的首个可执行候选

aggressive fallback search
    = 前一个候选真实 measurement 失败后，严格减少 exact protected tail，
      让后续 summary 覆盖更多历史，再重新构造 successor
```

它更激进的地方是“保留更少 exact tail、让 summary 覆盖更多 source”，不是切换 prompt、模型、
provider、reasoning effort 或 tokenizer。本文不删除这个 ordinary same-target 契约；只是明确
`MODEL_SWITCH_HANDOVER` 不从默认`3`开始也不重试，而是直接以`0`规划唯一candidate。

### 1.2 不应误认成额外 handover compaction 的现有机制

以下现有机制继续保留，但都不是第二档的第二次压缩：

- context compiler 在单个候选内部按统一 degradation priority 把允许降级的 source 从
  `FULL -> COMPACT -> ...`，并降低 tool-result render mode；
- `COMPACTION_RUNTIME_HANDOFF.full_text/compact_text` 只是在 byte bound 内对 current live state
  选择完整或更紧凑的表示；
- summary model 返回 tool call 时的一次 repair 只是协议纠正；
- post-FULL no-Hook base 与 Hook sibling 的选择只是 optional Hook fallback；
- retained Skill 的 duplicate removal 只消除已由 exact FULL tail 覆盖的重复正文。

这些机制会自然参与每个 B successor 的普通 compile，但不得被包装成新的 handover stage。

### 1.3 当前缺失的核心能力

当前 `validate_compaction_wire_transition()` 要求 source/successor 的 target、route wire profile、
wire API、estimator 与 effective budget完全相同。当前 summary call也绑定当前 turn 的 exact target。

因此当前代码只能表达：

```text
A summary -> A successor
```

不能表达本文需要的两条新路径：

```text
A summary -> B successor exact admission

canonical-derived B-readable projection
    -> B compaction summary
    -> B successor exact admission
```

当 installed A 是 1M context、新选择的 B 是 256K context 时，现有 B cold compile可能先以
over-budget终止，旧 A 没有机会为 B 生成 handover；即使A handover不能产生合格successor，B也没有
一条先读取降维source、再生成自己summary successor的显式路径。这是本文要修复的边界；context
compiler、Chat/Responses adapter、replay与canonical reader本身不需要另起体系。

---

## 2. 术语与冻结事实

### 2.1 Source target A

`A` 是 current installed provider-input epoch 所属的 exact target，包括：

- model connection identity；
- endpoint、wire API、model ID；
- adapter/replay contract；
- A effective input budget；
- A local estimator fact。

A 只负责读取和总结自己已经安装、能够解释的旧 provider context。它不是新增的 summarizer配置，
也不是已经删除的 FLASH 模型职责。

### 2.2 Destination target B

`B` 是首条使用新模型的 `NEW_TURN` 在 admission 时冻结的 exact target。B 唯一决定：

- direct-switch quote；
- handover successor quote；
- automatic trigger；
- hard input/token 与 final-wire byte admission；
- final wire lowering/replay compatibility；
- 最终 continuity install 与 ordinary provider open。

用户在这之后再次修改 session selector，只影响更晚、尚未 admission 的 `NEW_TURN`，不能改写本次
handover 的 frozen B。

### 2.3 Effective canonical source

Handover只处理 current binding 已经定义的有效历史：

```text
FULL_HISTORY + canonical suffix
or
current SNAPSHOT + canonical suffix
```

它不从 genesis 重新加入已经被旧 compaction覆盖的 rows。完整 rows 仍可供 UI/inspector读取，但
canonical durability 不等于 provider-visible context 自动“解压”。

### 2.4 Destination trigger

```text
B_trigger = floor(B.effective_input_budget_tokens * policy.auto_trigger_ratio)
```

默认 ratio 当前是 `0.85`，但实现必须读取 resolved policy，不得在 model-switch代码中再写一个
`0.85` 常量。

当前 automatic trigger 在 `quote >= B_trigger` 时成立。因此本文所有“可直接切换/可采用”在代码中
精确定义为：

```text
quote.final_wire_estimated_input_tokens < B_trigger
AND quote.final_wire_exact_utf8_bytes <= resolved physical bound
AND all remaining hard admission checks pass
```

这与口语中的“能放进 85%”只有边界一点的差别，却避免 exact equality 的 successor刚安装就立即
再次触发 automatic compaction。Token条件成立但byte或其他hard admission失败仍不属于Tier 1；master
compaction开启时进入Tier 2，关闭时按第2.5节typed拒绝。

### 2.5 既有compaction开关

Model switch不是ordinary automatic compaction，也不是用户点击manual compaction按钮，但仍属于
compaction subsystem：

- `policy.enabled = false`时只允许Tier 1。若B prospective quote不能直接通过Tier 1，则返回typed
  `MODEL_SWITCH_REQUIRES_COMPACTION`，不调用A/B summary、不adopt，并保持installed A；
- `policy.enabled = true`时三档完整可用；
- `automatic_enabled`只控制ordinary automatic入口，不关闭用户选择新target后所必需的Tier 2/3；
- `manual_enabled`只控制ordinary manual入口，不控制model-switch handover；
- `auto_trigger_ratio`在本文仍作为resolved destination operating threshold读取，不因
  `automatic_enabled = false`而改用hard context limit。

本文不新增model-switch专用开关。若master compaction被关闭，不能把provider context error、临时window
或Tier 3偷渡成第四条兼容路径。

---

## 3. Closed model-switch 状态机

### 3.1 触发条件

只有以下条件同时成立才进入本文路径：

1. 当前 scope 有 installed provider-input epoch；
2. 当前 `NEW_TURN` 已冻结可执行 destination binding B；
3. B 与 installed A 不具备同 epoch compatibility，因此本来就需要 cold reset；
4. 变化不只是同一 connection 上的 reasoning selection；
5. 当前 turn尚未对 provider产生 semantic output或工具副作用。

同 model ID 但 connection、endpoint或 wire API改变仍是 target switch。同一 target只改变
reasoning selection不进入该路径。

### 3.2 必须先 quote，不能先让 fit-only compile 报错

首条 B-bound turn必须在普通 executable compile/open前构造一个允许 over-budget 的 prospective
B cold candidate，并通过普通 B adapter完成 replay selection/hydration、materialization与本地
estimation。

该 quote：

- final-wire bytes 是 exact bytes；
- final-wire tokens 是统一 local estimator 的估算；
- provider usage不参与；
- 允许 over-budget、over-byte-bound且不可执行；
- 不取得 continuity/install/transport authority；
- 同一未漂移 candidate只 hydrate/materialize一次。

不得先让 B 的 fit-only compiler或provider返回 context error，再以错误触发 handover。

### 3.3 三档转换

```text
PENDING_B_TURN
    |
    | prospective B exact final-wire quote
    v
TIER_1_DIRECT
    | quote < B_trigger and hard-admitted
    -> one B cold install/open
    |
    | otherwise and policy.enabled = false
    -> typed MODEL_SWITCH_REQUIRES_COMPACTION; keep A

TIER_2_HANDOVER
    | otherwise
    -> freeze retained_tool_groups = 0
    -> per stable fenced capture:
       one logical A summary candidate + one pre-adoption B dry successor quote
       | candidate < B_trigger and hard-admitted
       -> one dry proof + one adoption
          -> post-FULL no-Hook/Hook final selection
          -> at most one B install/open
       |
       | candidate not acceptable / no valid summary
       -> TIER_3_B_PROJECTION_COMPACTION

TIER_3_B_PROJECTION_COMPACTION
    -> deterministic dialogue backbone from current effective lineage
    -> longest exact-fit safe dialogue-unit suffix under B summary wire
    -> small exact tool-result evidence fill under the same B trigger
    -> one logical B summary candidate per stable fenced capture
       (existing single repair may add one request; drift fresh capture counts separately)
       | B successor < B trigger and hard-admitted
       -> one dry proof + one adoption
          -> post-FULL no-Hook/Hook final selection
          -> at most one B install/open + one transient toast
       |
       | B summary/successor unavailable
       -> typed failure; no projection install and no fourth tier
```

Canonical/source/target drift不是 Tier 3 条件。Drift使prepared work失效并按既有规则 fresh recapture；
用户取消、Host close或 ownership conflict按其原生命周期结束。

---

## 4. Tier 1：Direct cold switch

Direct path使用 B 对当前 effective canonical source、当前 accepted user request、current context sources与
B tool surface构造的真实 cold materialization。

当且仅当：

1. B final-wire tokens `< B_trigger`；
2. B final-wire bytes不超过既有physical bound；
3. candidate拥有普通 hard admission；
4. target/cut/binding未漂移；

它才可以直接把 measurement 生成的 executable plan线性转移给 B cold install/open。

Direct path：

- summary provider call为 `0`；
- context snapshot/binding adoption为 `0`；
- 不显示 compaction divider；
- 不显示降级 toast；
- 不重新 materialize同一 winner。

既有 canonical working-set physical headroom仍是独立边界；本文不删除或伪装该物理约束。

---

## 5. Tier 2：旧模型 Handover Compaction

### 5.1 一个算法、一个逻辑候选

Tier 2必须复用现有 compaction planner、summary prompt、safe-boundary enumeration与cold
assembler。不得实现另一套“model-switch summarizer”，也不得在handover路径进入现有
ordinary candidate-shrink loop。Tier 3稍后由B执行的summary仍复用同一compaction summary contract；
它不是新的prompt或模型职责。

唯一 handover candidate 必须按以下方式生成：

- A 使用自己已安装的 SYSTEM、tools、messages/replay；
- handover令`MODEL_SWITCH_HANDOVER` attempt-local planner ceiling固定为`0`；immutable
  `ResolvedCompactionPolicy.maximum_retained_tool_groups`保持原值且继续满足其正数约束；
- planner在零个额外exact protected complete tool group的前提下，仍枚举全部safe
  summary prefixes，选择A final-wire可执行的最长prefix；
- summary由 A 生成；
- snapshot + 不可安全跨越的exact suffix + current sources由B cold assembler组装；
- B adapter对真实最终 wire做 measurement。

如果该B quote `< B_trigger`且hard-admitted，它立即成为winner。即使它高于ordinary
same-target的`0.55` post target，也不得再压缩。如果它不达标，释放该candidate的所有
process-local materialization与authority，直接进入Tier 3；不得改为`3 -> 2 -> 1 -> 0`搜索，
也不得再发起另一个不同coverage的summary candidate。

### 5.2 为什么 handover 直接从零个 protected complete tool group 开始

Ordinary same-target compaction偏向尽可能保留近期工具交互的exact wire，因此先尝试更丰富的
protected tail是合理的。Model switch handover则有不同产品目标：用户已明确选择另一个
target，第二档应以一次最紧的连续性转移优先，避免为了保留旧exact tail连续支付多次
provider latency和cost。

`0`只放弃“额外保护已完成旧tool group为exact tail”这一优先级，不放弃以下契约：

1. summary仍只能跨越完整safe boundary，不拆分canonical origin、tool request/result或required
   replay group；
2. 当前active request即使落在summary coverage中，也由existing continuation carrier机械地保留
   exact text，不依赖summary prose恢复用户请求；
3. safe boundary不允许跨越的后缀仍作为exact canonical suffix交给B；
4. PostgreSQL中完整canonical rows不会被删除。

这是明确的model-switch产品取舍，不改变ordinary automatic/manual compaction的保留算法。
现有summary tool-call repair若被触发，仍属于同一逻辑candidate的协议修复，不构成第二档内的
另一级compaction。

### 5.3 Summary call仍完全属于 A

A summary必须：

- 使用 A target、wire API、adapter、estimator与hard budget；
- 保持 A installed prefix/replay contract；
- 使用现有 compaction summary request；
- 不读取 B 的composer reasoning selection；
- 不执行工具，不取得 tool executor borrow；
- 使用完整 safe-boundary search，不恢复 pre-summary successor lower-bound gate；
- provider stream只受connect/write/read-idle等transport watchdog约束，无total lifetime cap。

Summary output可能长也可能短。Pulsara不以 provider usage、字符数或未经materialize的semantic estimate
预判它能否装入 B；只能在 terminal后构造真实 B final wire再判断。

### 5.4 Successor proof完全属于 B

唯一 handover candidate的 B successor必须使用：

- B resolved target与route wire profile；
- B tool/capability surface；
- B replay selection与hydration；
- B adapter wire materialization；
- B local estimator；
- B effective input budget与`B_trigger`；
- 唯一 `KernelColdEpochInputAssembler`。

通用 compiler在候选内部可以按既有规则选择 source `FULL/COMPACT/...`与tool-result render modes。
这些选择以 B 预算发生，且必须随候选 quote一起冻结；不能先用 A compiler选择，再把结果冒充B输入。

### 5.5 Same-target reclaim与cross-target admission分开

普通 same-target compaction继续完整要求 source/successor exact join同一个
target/profile/wire API/estimator/budget/cut，并继续使用`minimum_reclaim_tokens`与普通`0.55` post target。

Cross-model handover不得伪造 A/B相等，也不得计算：

```text
A final-wire estimated tokens - B final-wire estimated tokens
```

A/B wire形状和 estimator输入可能不同。Handover需要两个独立证明：

```text
SourceProof(A)
    exact A target/profile/wire/replay/estimator/budget
    exact source prefix/cut
    executable A summary wire plan

DestinationProof(B)
    exact B target/profile/wire/replay/estimator/budget
    exact snapshot + canonical suffix + current sources
    B final-wire quote
    quote < B_trigger
    hard-admitted pre-adoption dry B wire plan
    no post-FULL execution authority
```

二者只 join session、turn、scope、canonical cut、summary snapshot、零保留tail proof与drift
preconditions。现有 same-target validator应保持原样，或拆出不放宽语义的target-local helper；不能
为了 handover 删除 ordinary compaction的相等约束。

### 5.6 最终只能 adoption/install/open一次

唯一 handover candidate在胜出前只存在于process-local planning：

- 不写intermediate snapshot；
- 不写binding revision；
- 不写event/checkpoint/job；
- 不注册continuity；
- 不安装tool-result delivery；
- 不physical open B。

该candidate合格时才复用现有atomic transaction提交一个snapshot、一个binding revision、current
turn pointer和一个现有`CompactionAdopted` event。Canonical FULL后，post-FULL no-Hook base与Hook
sibling仍各自做final-wire measurement，最终只选择、安装、打开一个B successor。

---

## 6. Tier 3：Destination-side Projected-source Compaction

### 6.1 唯一触发条件

Tier 3只允许发生在一个stable model-switch handover attempt中，并且：

- `retained_tool_groups = 0`的唯一handover candidate没有形成有效B successor；或
- 该candidate的真实B final-wire quote不低于`B_trigger`或无法通过hard admission。

只有以下closed typed outcome允许Tier 2释放全部A candidate authority后进入Tier 3：

- 全部A safe prefixes都完成exact search但没有可执行summary wire；
- A summary transport在没有主turn semantic output或工具副作用的前提下typed失败，包括A额度耗尽、
  provider服务端不可用、超时或其他已经归一化的provider execution failure；
- A summary为空、schema非法，且既有唯一tool-call repair也未形成合法terminal summary；
- A summary合法terminal，但按B构造的pre-adoption dry successor不低于`B_trigger`、超过physical byte
  bound或未通过其他B hard admission。

以下结果不得伪装成“旧模型压缩不够”：

- canonical/source/A/B drift或stale：丢弃prepared work并fresh recapture同一档位；
- replay corruption、source digest mismatch、typed invariant/ownership failure：立即typed fail；
- 用户取消、Host close或主turn已经产生semantic output/工具副作用：按原生命周期停止；
- A candidate枚举尚未完成就耗尽planning deadline：typed planning failure，不能采用当前碰巧fit的
  partial search result，也不能进入Tier 3。

Tier 3不是普通automatic/manual compaction的fallback，也不能先physical open B 的normal request，
再把B的provider context error当作补做handover的信号；catalog refresh或UI convenience同样不能直接
调用它。Tier 2已实际尝试A summary而A发生前述typed transport failure时，进入Tier 3是本档位的正常
closed fallback。它是model downshift时的显式destination-side rescue：先让B读取一份
在B预算内的canonical-derived projection，再由B自己完成正式compaction，最后才处理当前用户请求。

### 6.2 Projection只是B summary call的临时source

Tier 3不把recent-dialogue projection直接安装成cold epoch。它创建两个严格分开的process-local值：

```text
DestinationProjectionSource
    从current effective lineage确定性派生
    只取得一个逻辑B compaction summary candidate的initial execution authority
    existing single repair仍属于该candidate
    不取得snapshot/adoption/continuity/normal transport authority

BCompactionCandidate
    B summary terminal后产生
    以B summary + exact active continuation构造普通successor
    只有pre-adoption dry successor通过B exact final-wire admission后才能atomic adopt
    不携带post-FULL install authority
```

因此不存在“B先从塞近trigger的projection正常回答一点，再在工具调用之间被动触发ordinary
mid-turn compaction”的中间状态。B的首次normal provider open必须发生在Tier 3 summary successor
成功adopt之后。

以一段已经idle的历史为例：

```text
U1
G2 -> M2
G3 -> M3
U4
G5 -> M5
G6 -> M6
U7 -> M8
```

用户选择更小的B并提交`US`后，Tier 1与Tier 2均失败时，Tier 3构造的临时summary wire在概念上是：

```text
B SYSTEM/tools/current sources
+ projected HISTORY(U1 ... M8)
+ exact active continuation US
+ existing compaction summary instruction
```

这里`US`计入真实wire admission，也允许B理解当前延续目标，但不属于可被summary替代的历史coverage；
existing continuation carrier必须在最终successor中机械地恢复同一exact `US`。上式只是可读简写，真实
判断必须包含adapter最终发送的全部context-bearing wire materialization。

本文冻结Tier 3 adoption boundary为fence内的**完整exact safe canonical head**。因此驱动本turn的`US`
满足`entry_sequence <= source_through_sequence`，必须沿用既有
`CompactionActiveRequestLocation.SNAPSHOT_EXACT`分支：

- exact entry ID、sequence与UTF-8正文只写入`continuation.active_request`；
- `US`不进入projection turns、`recent_user_messages`或post-snapshot canonical suffix；
- B summary wire与post-FULL successor都只从该validated carrier取得Runtime-owned authoritative active
  request placement；
- 不允许同时生成`CANONICAL_SUFFIX` marker，也不允许把summary prose当作`US`的恢复来源。

这是本文对active model-switch turn的唯一落位；不新增第三种location。既有重复compaction继承
`SNAPSHOT_EXACT`的规则继续原样适用。Model-written summary可能偶然逐字复述`US`；这只是advisory
prose，不构成第二个active-request authority，Runtime不得因此重写、裁剪、重试summary，测试也不得用
全文字符串出现次数冒充authority-placement证明。

### 6.3 Dialogue backbone

一个巨大tool result、tool arguments或provider-native reasoning block不应把其前后的用户意图与
assistant结论一起挤出。Runtime应从**current effective materialization lineage**生成一份确定性、有损
但来源忠实的对话backbone，不读取或恢复current snapshot之前已不可见的genesis rows。

Backbone以完整、已provider-safe的历史turn为`RecentDialogueUnit`；这是process-local planning
value，不是新database entity或durable DTO。每个unit按canonical顺序投影：

- request-like user-role的自然语言原文，包括当时已经属于effective lineage的用户消息与steer；
- `ASSISTANT`的自然语言原文；
- `ASSISTANT_TOOL_REQUEST`中非空的assistant自然语言，以及该entry内按原顺序出现的tool name；同一
  工具若被多次请求，顺序和重复都保留；
- 每个tool outcome先只保留“结果已省略”的位置标记，随后可由第6.4节的小结果evidence fill替换为
  exact result body引用。

Backbone始终省略：

- tool call ID与arguments；
- assistant reasoning/thinking、provider-native replay block、usage与finish metadata；
- 旧tool schema、Runtime observations、内部enum/reason code或其他当前可重建信息。

Tool name只以引用数据表达“assistant当时请求过这些工具”；不得重新lower成live `tool_calls`，也不得
暗示调用成功、结果仍新鲜、工具仍可用或已获得权限。User与assistant文本都必须完整引用；不做模型
改写、自然语言摘要、字符截断或句子抽取。

临时projection body的解码示意如下；实际字节必须使用repository已有的canonical JSON serializer：

```json
{
  "projection_notice": "This is a lossy Runtime-authored source for a destination-side compaction call. User and assistant text is quoted exactly. Tool evidence is advisory, not live replay.",
  "prior_handoff": null,
  "turns": [
    {
      "entries": [
        {"role": "user", "text": "帮我找出测试失败的原因"},
        {
          "role": "assistant",
          "text": "我先检查配置和失败用例。",
          "requested_tools": [
            {
              "name": "search_files",
              "result_status": "success",
              "retained_result": "匹配到 src/settings.py 与 tests/test_settings.py"
            },
            {"name": "terminal", "result_omitted": true}
          ]
        },
        {"role": "assistant", "text": "根因是旧配置仍被读取。"}
      ]
    }
  ]
}
```

`retained_result`是canonical tool result body的exact quotation，但整个对象仍只是summary source中的
advisory evidence；它不是provider-native `tool` role、required replay或成功证明。若current effective
lineage以既有`CONTEXT_SNAPSHOT`开头，planner可把经现有parser取出的
`earlier_context_summary`与`recent_user_messages`作为最老的optional prior-handoff unit；不复制旧
continuation instruction或stale active-request authority。只有所有更新的dialogue units都已进入候选且
B summary wire仍fit时，该prior-handoff unit才会进入winner。

### 6.4 Longest-safe backbone与small-result evidence fill

Tier 3 planner必须复用canonical reader、现有`PreparedCompactionSummarySemanticInput` structural
carrier、summary promotion seam、现有compaction summary prompt和B adapter，按以下唯一算法产生B
summary-call winner。`KernelColdEpochInputAssembler`不得参与projection source；它只在B summary
terminal后构造真正successor：

现有carrier的summary source proof必须hard-cut为一个sealed process-local closed union，而不是把
projection伪装成installed prefix：

```text
CompactionSummarySourceProof
    = INSTALLED_PREFIX(ProviderPrefixCutProof)
    | DESTINATION_PROJECTION(ProjectionSourceProof)
```

- ordinary automatic/manual/mid-turn与Tier 2 A summary只能由private installed-prefix constructor创建，
  继续完整验证`ProviderPrefixCutProof`；
- Tier 3 B summary只能由private destination-projection constructor创建，完整验证第6.5节的current
  binding/base、cumulative source digest、safe-head boundary、projection selection与exact active
  continuation；
- 两个分支都不得nullable、互相fallback或伪造对方proof；repair必须继承同一sealed branch与exact
  source proof；
- promotion factory按branch exact join carrier、measurement decision、target/profile/native tools与
  final-wire admission，并继续要求`decision.candidate is semantic`；
- union、两个proof和constructor都不写库、不新增fingerprint/registry，也不放宽ordinary prefix
  validation。

1. 在fence内冻结B、current exact canonical safe head、current effective lineage与accepted active
   request；
2. 将active request从历史backbone排除；它继续作为exact continuation出现在B summary wire和最终
   successor中；
3. 按turn boundary冻结所有可用`RecentDialogueUnit`，backbone候选只能是这些unit的连续最近后缀；
4. 按“包含的完整近期turn最多”从完整backbone到empty suffix枚举**全部**safe suffix boundaries。每个
   候选最初省略所有tool result body；empty候选仍包含B SYSTEM/tools、current sources、fixed
   projection notice、exact active continuation与existing compaction summary instruction；不使用固定
   消息数、字符数或任意trial cap；
5. 每个不同候选构造自己的structural summary semantic input，只执行一次B replay
   selection/hydration、summary-call final-wire materialization与local estimation。Semantic estimate
   可以over-budget，不得先经过ordinary semantic-budget validator；
6. 某个候选仅因final-wire tokens不低于`B_trigger`或bytes越physical bound时，释放其短生命周期
   materialization并继续下一个safe suffix。Stale/corrupt/digest/resource/invariant failure不是
   over-budget，必须按typed语义restart或失败，不能静默跳过；
7. 第一个`< B_trigger`且hard-admitted的候选就是backbone winner；完整backbone若fit自然直接胜出。
   即使empty候选不fit，也不能推断更长候选必然不fit；只有完整exact search中全部suffix均因真实
   token/byte bound失败时，才按第6.6节typed fail closed；
8. 只把backbone winner所含turn中的complete tool outcomes列为evidence候选。不得脱离其user/
   assistant语境保留更老的孤立result，也不得保留仍未terminal的outcome；
9. 对当前尚未选入的每个result，构造“只新增该exact result quotation”的不同候选，并计算相对当前
   winner的exact incremental final-wire tokens与bytes；无法继续满足`< B_trigger`或physical byte
   bound的候选淘汰；
10. 在可加入项中按`incremental tokens -> incremental bytes -> canonical sequence由新到旧 ->
    outcome ordinal`选择最小者，加入后重新计算剩余项，直到没有任何result还能fit；
11. 选择顺序只决定哪些result获得稀缺空间；最终projection必须按原canonical时间顺序render。每个
    result-set candidate只materialize一次，measurement不得被重复规划或冒充另一candidate的proof；
12. Backbone与evidence search共享一个fresh、有限的Tier 3 projection-planning deadline；候选循环不得
    刷新它。Deadline在search得出确定winner前耗尽时返回typed planning failure，不能把尚未完成比较的
    当前backbone或部分evidence集合提升为winner；
13. 只有最终winner才通过existing promotion seam取得`PreparedCompactionSummaryCall`。同一次
    measurement产生的executable B summary plan线性转移给第6.5节，不重新hydrate/materialize。

这里故意不新增`context * 0.5`、ordinary `0.55` post target、固定2K/4K per-result threshold或“近期raw
tail分一半”等cap。`B_trigger`是这次临时summary source的唯一产品预算线，physical byte bound仍是独立
硬边界。Projection可以被尽量填近trigger，因为它不会被安装为长期prefix；它的唯一用途就是让B在
正式回答前尽可能多地读取历史并立刻压缩。

Tier 3不携带前面失败候选的A-generated summary。若该summary加exact suffix能够低于B trigger，它本应
已经在Tier 2胜出；若不能，B继续总结它既浪费空间，也把“B总结canonical-derived projection”退化成
“B再次总结A的失败summary”。

### 6.5 B立即执行compaction并生成真正successor

Projection winner只取得一个逻辑B compaction summary candidate的initial execution authority；既有
single repair仍从同一sealed source branch派生。该candidate必须：

- 使用B target、wire API、adapter、replay contract、estimator与effective budget；
- 复用现有compaction summary prompt与禁止执行用户工具的契约，不新增creative/aggressive prompt；
- 把projection明确标成Runtime-authored、有损、advisory source；
- 让exact active request作为continuation可见但不纳入summary coverage；
- provider stream只受connect/write/read-idle等transport watchdog约束，不增加total lifetime cap。

执行前必须冻结一份process-local `ProjectionSourceProof(B)`，至少逐项携带：

```text
exact B target / route wire profile / wire API
exact B estimator fact / effective budget / resolved trigger
exact current binding revision / CompactionSourceLineageBase
exact source_through_sequence = fenced safe canonical head
existing cumulative source_digest over the bounded post-base range
exact dialogue-unit suffix / selected tool outcomes
exact active continuation
exact canonical projection body / summary request / semantic placements
```

该proof是pre-measurement完整typed source value，不循环嵌入wire decision。与它exact绑定的
`PreparedWireMeasurementDecision`另行携带actual B summary-call materialization、exact final-wire bytes与
locally estimated final-wire tokens；只有matching sealed branch才能promotion。两者均不新增fingerprint
或registry。Provider usage无论reported、missing或与本地估算不一致，都不得改变source selection、
summary execution或successor admission。

B summary terminal后签发新的pre-adoption successor-planning deadline。Runtime以B summary、
`SNAPSHOT_EXACT` active continuation、current sources与B tool surface调用唯一
`KernelColdEpochInputAssembler`，构造普通B **no-Hook dry successor**并完成真实final-wire
measurement。当且仅当该dry successor `< B_trigger`且hard-admitted时，才允许准备canonical adoption。
该dry plan只证明write前存在可行B base；它不取得post-FULL continuity/install/open authority。

Tier 3 adoption必须exact join current binding/base、`source_through_sequence = fenced safe canonical
head`与Round 5B既有cumulative `source_digest`。Repository继续从current
`CompactionSourceLineageBase`对bounded post-base canonical range计算并在transaction内复验同一digest，
不新增projection digest。Digest证明从lineage floor到safe head的每一条canonical row都被本次lossy
transform有意覆盖；它不要求每条row逐字进入projection。被projection丢弃的更老unit、未入选result和
被省略reasoning同样位于这一closed coverage内，不构成prefix hole。

随后复用现有atomic transaction提交一个snapshot、一个binding revision、current turn pointer和一个
既有`CompactionAdopted` event。Snapshot的continuation固定为第6.2节的`SNAPSHOT_EXACT`，因此exact
`US`进入snapshot一次，`recent_user_messages`排除它，post-boundary canonical suffix也不再复制它。

Canonical FULL后，pre-FULL dry dispatch、wire plan、source heads与physical borrow全部释放，不得转移给
install。Coordinator先按既有生命周期运行PostCompact；若turn仍RUNNING，再签发fresh post-FULL base
planning deadline，旋转reader并从真实adopted binding/revision/cut重读current Tool surface、current
sources与frozen no-Hook facts。No-Hook base与optional Hook sibling分别完成自己的B final-wire
materialization/measurement；Hook使用独立probe deadline，失败时保留已证明的base。最终selected
sibling再取得fresh bind/install deadline，并把**该post-FULL candidate自身**同一次measurement产生的
plan线性转移给唯一continuity register/install/open。若turn已经不再active，则snapshot仍是historical
winner而provider open为0。

Tier 3不使用ordinary same-target的`0.55`作为success gate，也不新增landing target。A的失败candidate、
临时projection与B summary均不提前写snapshot或注册continuity。首次normal B response必须发生在上述
adoption FULL以及post-FULL sibling selection之后；因此Tier 3是显式destination-side compaction，不是
把压缩延迟到B回答中途。

### 6.6 Failure与成功后的固定base

如果B summary transport失败、summary为空或非法，或者pre-adoption B dry successor仍不低于
`B_trigger`或无法hard-admit，则：

- 不安装临时projection；
- 不采用A的失败summary；
- 不打开normal B response；
- 不新增第四档、在线协议试探或另一个summary prompt；
- 按既有typed provider/turn failure语义结束本次attempt，保留current canonical rows与installed A
  binding，允许用户之后重试或选择其他模型。

如果canonical adoption已经FULL，但post-FULL no-Hook base无法重建/通过B admission，或最终selected
install因shared canonical/target/base漂移失败，则B-generated snapshot与binding仍是canonical winner：

- 不回滚、删除或改写已经FULL的snapshot/binding/event；
- 不恢复A为canonical current binding；
- 不安装临时projection，也不打开错误的B surface；
- 当前active continuation按既有post-FULL typed failure结束；下一次dispatch从current adopted binding
  normal cold read。

如果B successor成功adopt，它就是普通、固定的compaction base。之后同一B epoch内SYSTEM/tools保持
byte-identical，messages只append suffix；达到ordinary automatic trigger后走普通same-target
compaction。后续再切模型仍从current adopted base开始，不自动恢复genesis full history。

只有第6.4节从完整backbone到empty suffix的**全部**候选均完成exact measurement，且每个候选都因真实
final-wire token/byte admission失败时，才返回typed
`NO_EXECUTABLE_DESTINATION_PROJECTION_SOURCE`。这里的empty候选仍完整包含B SYSTEM/tools、current
sources、projection notice、exact active continuation与summary instruction；不得从其中任一拆出的
“最小组件和”推导其他候选必然不fit，也不得由此发明第四档model-switch产品语义。

---

## 7. Canonical、prefix、replay与provider neutrality

### 7.1 Canonical rows始终完整

三档均不得删除、覆盖或重排canonical transcript rows。Tier 3临时projection中的历史截取与工具详情
省略只描述B summary call能够读取的source，不是数据库数据丢失。成功后provider-visible base保存的是
B生成的正常summary successor，不是该临时projection。

完整rows也不会让未来大模型自动绕过current binding恢复旧full history。恢复或fork若成为产品能力，
必须另写明确规范。

### 7.2 A summary prefix continuity

- A summary复用A installed SYSTEM、tools和provider-native replay carrier；
- synthetic summary request只作为临时user-role suffix追加；
- Chat/Responses不互译native reasoning blocks；
- summary prefix必须与proof逐项一致；
- boundary不能拆分同一canonical origin或tool group；
- retained tail与summary coverage不得重叠或留洞。

### 7.3 B cold successor

- Tier 2的A-generated summary snapshot，或Tier 3的B-generated summary snapshot，都与exact active
  continuation及current sources一起走唯一cold assembler；
- B adapter重新lower全部context-bearing wire；
- 只有B replay contract允许的native carrier才能hydrate；
- old native reasoning block不得伪装成B native replay；
- Tier 3临时source中的assistant历史与retained tool results都只是advisory引用数据，不伪装成
  provider-native assistant replay、live tool call或tool-role result；
- 临时projection不得成为最终B prefix；最终prefix只能来自B summary的普通snapshot successor；
- Chat/Responses选错时报告协议错误，不自动换wire API。

### 7.4 不增加provider/model分支

Handover只读取resolved A/B target facts，不匹配provider display name、provider ID、model family、model
ID前缀或OpenRouter/DeepSeek/Zhipu/MiniMax等名称。

models.dev/user-declared connection继续提供endpoint、model ID、limits与reasoning options；通用
Chat/Responses adapter继续拥有request lowering、stream normalization、tool correlation、terminal和
replay。

---

## 8. Ownership、并发与deadline

### 8.1 单一线性owner

一个model-switch attempt只有一个process-local owner，顺序持有：

```text
frozen A source target
frozen B destination target
one summary lane + exact-scope fence
current A summary candidate authority
current B projection-source measurement/execution authority
current B summary candidate authority
current B pre-adoption dry successor proof (no install authority)
one canonical adoption resource set
post-FULL no-Hook base owner + optional handle-free Hook sibling
one selected B continuity/install/open authority
```

Tier 2 candidate失败并转入Tier 3前，其handle、borrow、materialization、reservation与execution
authority必须恰好一次关闭。Tier 3 projection winner只把summary execution authority线性转移给B
summary call；summary terminal后的dry successor只允许取得adoption proof，FULL后必须关闭。Post-FULL
重新构造的no-Hook/Hook candidate中只有最终selected sibling可以把自己的plan线性转移给install。不得
alias、double-close或泄漏，也不得同时保留A handover与B projected-source两个可执行winner。

### 8.2 Fresh recapture

Direct precheck不占用summary lane或scope fence。真正进入Tier 2后先释放precheck observation，再在
fence内fresh recapture A、B与canonical cut，并直接冻结零保留tail proof。

不达标的Tier 2 B successor不发起smaller-tail restart。只有canonical/source/A/B drift使prepared work
失效时，才按既有restart语义fresh recapture并仍以`retained_tool_groups = 0`重新计划；不得使用旧
summary。进入Tier 3后必须从同一fresh effective lineage重建projection，不能复用drift前的unit或
result-cost measurement。Pre-compact Hook在同一logical attempt中只dispatch一次。

### 8.3 并发

- 当前attempt始终使用turn frozen B；
- 用户再次改selector只影响更晚未admission的turn；
- queue保持FIFO，不因target重排；
- fence只保护会改变exact source cut/binding的producer；
- 其他scope、Terminal physical work与无关subagent继续运行；
- drift丢弃prepared candidate并recapture，不使用stale summary、projection或result-cost排序。

### 8.4 Deadline

- planning watchdog只覆盖有物理边界的局部capture/hydration/materialization；
- Tier 2以closed fallback-eligible outcome转入Tier 3并释放A authority后，为完整backbone/evidence
  search签发fresh projection-planning deadline；该deadline不得按suffix或result candidate刷新，超时不
  采用partial winner；
- 每个A或B summary stream只受transport connect/write/read-idle watchdog约束，不增加total lifetime
  cap；
- A summary terminal后签发新的B dry-successor planning deadline；Tier 3 B summary terminal后另行签发
  新的pre-adoption B dry-successor planning deadline；
- publication/adoption/confirmation继续使用各自既有fresh deadline；
- FULL后no-Hook base reconstruction、Hook probe与最终selected bind/install分别使用不同fresh deadline；
- 不给整个model switch、turn、task或健康provider stream增加累计wall-clock cap。

---

## 9. UI语义

### 9.1 Compaction divider

只要Tier 2或Tier 3通过现有canonical compaction adoption提交，timeline沿用当前灰色实线divider和
产品文案：

```text
上下文已压缩
```

- Tier 1不显示divider；
- Tier 2成功时只显示一个divider；
- Tier 3同样只显示一个divider；
- 不新增“模型切换压缩”“窗口”等divider；
- divider继续来自canonical context-adoption read model，不由前端猜测。

### 9.2 只有Tier 3显示toast

Tier 3 adoption FULL且B successor确定被选中后，右下角显示一次：

```text
模型已切换。由于上下文长度变化，接下来的回答可能不如之前连贯。
```

要求：

- 使用现有toast视觉和生命周期；
- 不阻塞B继续工作；
- 不显示token、ratio、candidate、被省略条数、数据库或内部reason code；
- 不增加确认modal；
- selector click/precheck/失败candidate/取消attempt不得提前toast；
- toast是process-local presentation，允许crash后丢失，不新增delivery receipt/event。

Tier 2成功时现有divider已经足够，不显示toast。用户不需要看到零保留tail、ratio或
handover内部语义。

---

## 10. Persistence与hard-cut边界

### 10.1 不增加数据库体系

继续复用：

- `sessions.model_call_binding`；
- `prompt_queue_items.model_call_binding`；
- `turns.model_call_binding`；
- `context_snapshots`；
- `turn_context_binding_revisions`；
- `turns.current_context_binding_revision_id`；
- 既有`CompactionAdopted` event。

不增加model-switch attempt/stage/candidate/window表、列、relation、event、job、checkpoint或receipt。
A handover candidate、B projection source、tool-result选择与B summary candidate都只存在于当前
process-local owner。

### 10.2 Projection不持久化；B summary复用现有snapshot carrier

Tier 3不新增`RecentWindowSnapshotCarrier`、`RecentDialogueProjectionCarrier`或projection blob。
`DestinationProjectionSource`及其dialogue units、tool-result选择和wire measurement都只属于当前
process-local summary-call owner；它们在B summary terminal或失败后释放。

成功adoption继续原样复用现有`CompactionSnapshotCarrier`、snapshot/blob columns与parser：

- `continuation`表达continuation mode与active request机械事实；
- `earlier_context_summary`保存B实际生成的model-written summary，不保存Runtime projection JSON；
- `continuation.active_request`固定保存一次`SNAPSHOT_EXACT` US，`recent_user_messages`必须排除它；
- `recent_user_messages`中的其他历史human quotations继续按现有compaction planner选择；
- `source_through_sequence`固定覆盖fenced完整safe head，boundary之后的新canonical suffix按既有reader
  规则出现；adoption时不存在第二份US suffix；
- `source_digest`继续使用current binding/base与bounded post-base range计算的既有cumulative lineage
  digest，不编码projection选择，也不新增另一枚digest。

Snapshot row记录真实B summary prompt/model provenance；不新增“无summary prompt/model”常量，也不需要
把base system中的`CONTEXT_SNAPSHOT`改写成Runtime-authored projection。临时projection的advisory
notice只存在于B summary request中。

不得添加DTO fingerprint；既有snapshot content digest是跨restart的真实内容完整性边界，继续保留。

### 10.3 Restart

- adoption前crash：无canonical effect，下次从current binding/source重规划；
- Tier 3 B summary terminal但adoption前crash：summary与projection均可丢失，下次fresh重规划；
- adoption FULL后crash：existing snapshot/binding reader恢复同一B-generated summary base；
- 不因process-local projection state丢失而安装projection或fallback full history；
- transient toast丢失可接受。

---

## 11. 最小生产代码范围

实施时以真实symbol为准，不为匹配文档创建空抽象。

### 11.1 Model-switch precheck与dispatch

```text
src/pulsara_agent/conversation_kernel/runner.py
src/pulsara_agent/conversation_kernel/provider_dispatch.py
src/pulsara_agent/conversation_kernel/direct_model.py
src/pulsara_agent/model_input/compiler.py
src/pulsara_agent/llm/resolution.py
```

职责：

- 在ordinary fit-only compile前识别A->B cold transition；
- 构造B over-budget-capable structural quote；
- 每个turn只进入一次closed三档状态机，不从失败档位倒退或并行竞速；
- master `policy.enabled`关闭时只允许Tier 1；`automatic_enabled`/`manual_enabled`不误关model-switch
  path；
- reasoning-only变化保持原路；
- frozen binding与single B open不变。

### 11.2 Handover planner/coordinator

```text
src/pulsara_agent/conversation_kernel/compaction/contracts.py
src/pulsara_agent/conversation_kernel/compaction/planner.py
src/pulsara_agent/conversation_kernel/compaction/model_call.py
src/pulsara_agent/conversation_kernel/compaction/coordinator.py
src/pulsara_agent/conversation_kernel/cold_epoch.py
```

职责：

- 增加process-local `MODEL_SWITCH_HANDOVER` trigger/facts；
- A summary与B successor分别绑定自己的target-local proof；
- handover以attempt-local ceiling `0`调用现有planner，不构造非法resolved policy；
- B successor不达标时直接进入Tier 3，不进入ordinary smaller-tail restart search；
- drift restart仍fresh recapture，并重新冻结零保留tail proof；
- handover acceptance读取B trigger ratio，不使用ordinary `0.55` gate；
- ordinary same-target validator行为不变；
- 将summary source proof收口为installed-prefix / destination-projection sealed union与两个private
  constructor；promotion exact join对应branch，ordinary prefix proof不放宽；
- Tier 3按完整历史turn构造确定性dialogue backbone，枚举全部safe dialogue-unit suffix，并在
  backbone winner内按exact incremental B wire cost补入尽可能多的小tool-result引用；
- Tier 3 projection candidates走existing structural summary input/measurement/promotion seam，不走cold
  assembler；winner直接驱动一个逻辑B summary candidate，可附既有唯一repair；
- B summary terminal后cold assembler只构造pre-adoption dry proof；FULL后从adopted exact cut重建
  no-Hook/Hook final siblings，临时projection与dry plan均不得取得install authority；
- 一次final adoption/install/open。

### 11.3 Snapshot与Web

```text
src/pulsara_agent/conversation_kernel/compaction/prompt.py
src/pulsara_agent/conversation_kernel/_repository/conversation.py
src/pulsara_agent/model_input/lowering.py
src/pulsara_agent/web_app/session_controller.py
src/pulsara_agent/web_app/http_server.py
frontend/lib/runtime-adapter.ts
frontend/components/workbench-view.tsx
frontend/app/styles/workbench.css
```

职责：

- 临时projection只进入B summary request，不写snapshot或新增provenance；
- 成功后用真实B summary prompt/model provenance复用现有carrier/parser与snapshot transaction；
- adoption复用existing cumulative source lineage digest，active US固定为`SNAPSHOT_EXACT`且只落位一次；
- Tier 2/Tier 3复用现有divider；
- 只传递一次non-durable Tier 3 toast；
- 不公开内部candidate或reason code。

数据库clean-v0 schema与expected catalog应保持不增加对象；若实现发现现有columns已足够，不得为了
查询projection kind增加冗余列。

---

## 12. 实施顺序

1. 为installed A、turn-frozen B和prospective B cold quote建立测试fixture。
2. 在fit-only ordinary compile前接入B structural final-wire precheck。
3. 保持same-target validator不变，新增A source proof与B destination proof的组合验证。
4. 冻结master `enabled`与两个入口子开关的model-switch语义，让现有compaction coordinator接收
   model-switch trigger与frozen B。
5. 在model-switch trigger下将attempt-local retained group ceiling直接固定为`0`，不改immutable
   policy；沿用现有A
   summary/safe-prefix算法，以B quote和B trigger验收唯一candidate。
6. B验收失败时关闭candidate并直接进入Tier 3；禁用handover的smaller-tail restart，保留
   ordinary same-target shrink与drift-only fresh recapture。
7. 实现Tier 3 deterministic dialogue backbone与包含empty在内的longest-safe exact search；对winner内
   complete tool outcomes按exact incremental B summary-wire cost执行small-result evidence fill。
8. 将summary source proof改为installed-prefix / destination-projection sealed union及两个private
   constructor；让projection走existing `PreparedCompactionSummarySemanticInput`/promotion seam，把
   winner measurement产生的唯一executable plan直接转移给B summary call；不得先normal reply、走
   cold assembler或把projection写入snapshot。
9. B summary terminal后用cold assembler构造pre-adoption dry proof，复用既有cumulative
   `source_digest`、完整safe-head boundary与`SNAPSHOT_EXACT` active US执行atomic adoption。
10. FULL后释放dry plan，从adopted exact cut重建并分别measure no-Hook/Hook sibling，只把最终selected
    plan转移给single install/open。
11. 接入existing divider与Tier-3-only toast。
12. 删除“target一变化就直接进入fit-only B compile”的over-budget死路，不保留feature flag或旧分支。
13. 完成focused、PostgreSQL、frontend与real-provider验证。

---

## 13. Required semantic and test matrix

### 13.1 Tier 1

- A/B不同，但prospective B final-wire `< B_trigger`：summary `0`、adoption `0`、B open `1`；
- prospective B tokens低于trigger但final-wire bytes越physical bound：不得进入Tier 1；
- A切到更大context B：同上；
- quote exact equality `== B_trigger`进入Tier 2，不造成安装后立刻automatic compact；
- `policy.enabled = false`且Tier 1 fit：仍直接切换；Tier 1不fit：typed
  `MODEL_SWITCH_REQUIRES_COMPACTION`且A binding不变、summary/adoption/open均为0；
- `policy.enabled = true`、`automatic_enabled = false`或`manual_enabled = false`：model-switch Tier 2/3
  仍按本文运行，ordinary入口开关行为不变；
- 同connection只改reasoning：不handover，下一`NEW_TURN`使用frozen新control；
- 同model ID但connection/wire API不同：是否handover只由B exact quote决定。

### 13.2 Tier 2 唯一零保留候选

- 1M A -> 256K B，当前B input过trigger但A可读取：每个stable fenced capture恰好一个logical A summary
  candidate；无repair时A provider request一次，既有repair可再增加一次request；A terminal后
  pre-adoption B dry successor measurement一次；
- 即使source中有多个complete tool groups，handover planner选中的retained group count仍为`0`；
- 不保留额外complete tool group时仍枚举全部safe boundaries，不拆分canonical origin或tool
  request/result；
- active request位于summary coverage时，continuation carrier仍机械地携带exact text；
- A summary wire只使用A endpoint/model/wire/replay/budget；
- B successor只使用B endpoint/model/wire/replay/budget；
- A/B不要求target、budget、wire API、estimator或wire bytes相等；
- token quote低于B trigger但final-wire bytes越bound：Tier 2不得adopt；
- B successor在`0.55..0.85`之间仍立即成功，不触发额外candidate；
- B successor不达标时不试`3/2/1`、不生成第二个A handover summary，直接进入Tier 3；
- 无tool-call repair的正常样例恰好一次summary provider call；现有一次repair若被触发，仍只属于
  同一逻辑candidate；
- provider usage missing/reported/inconsistent不改变结果；
- summary不执行tool或用户任务；
- winner之前snapshot/binding/event/continuity/open数量均为0；
- adoption FULL后pre-adoption dry plan关闭且不复用；post-FULL no-Hook measurement一次，有Hook时Hook
  sibling另measure一次，未选final sibling恰好一次释放；
- 最终snapshot/binding/event各1，selected B final plan install/open各1；
- candidate owner、wire materialization、borrow、reservation都恰好一次close/transfer；
- ordinary same-target compaction仍保留原有`3 -> 2 -> 1 -> 0`行为；
- pre-compact Hook在同一logical attempt中只发一次。

### 13.3 Tier 3

- 唯一零保留handover candidate不能低于B trigger或无法hard-admit：进入B-side projected-source
  compaction；
- B在Tier 3 summary adoption前normal response call为0、用户工具执行为0；
- Tier 2已经实际调用A summary但successor不合格时，Tier 3在同一stable fenced capture只再产生一个
  逻辑B summary candidate，且B不读取A失败summary；无repair时每个逻辑candidate各一次provider
  request，既有唯一repair与drift后的fresh recapture分别计数，不增加累计call/retry cap；
- 零历史B summary wire包含B SYSTEM/tools、current sources、exact active continuation与existing
  summary instruction；任何一项都不能从measurement中漏掉；
- 一个巨大tool result被省略后，同一历史turn的user原话、assistant自然语言与ordered tool names仍可
  进入backbone；
- user/assistant文本逐字保留，空assistant文本不伪造内容；
- 同一assistant request中多个或重复tool name的原顺序保留；arguments、reasoning、call IDs与
  provider-native replay均不出现；
- tool names与retained results只出现在advisory JSON引用中，不产生live `tool_calls`、tool-role result
  或成功证明；
- backbone只按完整历史turn的连续最近后缀选择，不产生孤立assistant回复；
- 选择满足B exact summary-call final-wire的最长dialogue-unit suffix，不是固定消息数/字符数；
- empty suffix measurement不fit、某个非空suffix因合法re-lowering/degradation反而fit：必须选中该非空
  候选，证明planner未假定长度单调性；
- 完整backbone能fit时不无故截断；不能fit时才按完整turn suffix收缩；
- small-result fill只考虑winner所含turn中的complete outcomes，不保留backbone外的孤立旧result；
- small-result fill按动态exact incremental tokens、bytes、新近sequence与ordinal确定选择，最终仍按
  canonical顺序render；
- 多个小result能够fit而一个大result不能fit时，小result被保留、大result继续显示omitted；
- 不存在固定per-result bytes/tokens cap、`context * 0.5`分仓或ordinary `0.55` landing gate；
- 每个不同backbone/result-set candidate只hydrate/materialize一次；最终winner plan直接转移给B
  summary call，不重复规划；
- 同一evidence迭代中存在多个fit challenger：每个未选decision/plan/materialization都恰好一次释放，
  当前best由selection owner唯一持有，最终selected plan不重新物化地转移；
- projection candidate使用`PreparedCompactionSummarySemanticInput`与summary promotion seam；不得
  构造cold-epoch seed或被ordinary semantic-budget validator提前拒绝；cold assembler调用数在B
  summary terminal前为0；
- ordinary installed-prefix与Tier 3 destination-projection proof只能经各自private constructor进入
  sealed union；null proof、branch替换、伪造prefix proof及repair时改变branch均typed拒绝；
- Chat与Responses分别证明Tier 3 measurement的exact bytes等于各自actual summary transport payload中
  context-bearing wire，local token estimate来自同一materialization；
- token estimate低于B trigger但final-wire bytes over-bound时，Tier 3 backbone/evidence/summary
  successor均不得误放行；
- active user request不进入历史projection coverage，固定为`SNAPSHOT_EXACT`；只有
  `continuation.active_request`拥有Runtime authoritative placement，projection turns、recent quotes与
  canonical suffix均无第二个authority。测试检查typed placement/source identity，不按全文搜索阻止
  model-written summary偶然复述US；
- adoption exact join current binding/base、完整safe-head boundary与既有cumulative `source_digest`；
  被projection省略的rows仍在closed coverage中，digest mismatch不得进入Tier 3或write；
- predecessor snapshot只能作为最老optional unit，不携带其stale continuation authority；
- 包含与分隔符、JSON key或prompt标记相同文本的user/result仍被canonical escaping忠实表达，不能
  伪造projection boundary；
- failed A handover summary不进入projection body；临时projection在B summary terminal后释放且不写
  snapshot；
- parallel tool calls、重复同名tool calls、closure/typed failure与cut后late result分别证明outcome按
  original request/ordinal关联；只保留winner turn内的complete evidence，不产生孤立result；
- B summary使用B endpoint/model/wire/replay/budget，summary成功后B successor再次exact measurement；
- pre-FULL B dry successor只取得adoption proof，FULL后绝不复用其messages/source heads/wire/plan；
- post-FULL no-Hook与Hook sibling分别materialize/measure，每个真正final candidate各一次；Hook失败安全
  使用已证明base，selected sibling的plan才被唯一install/open消费；
- B successor `< B_trigger`且hard-admitted时snapshot/binding/event各1，B normal install/open各1；
- Tier 3成功后持久化的是B-generated summary与真实prompt/model provenance，不是projection JSON；
- canonical transcript row count、内容和顺序完全不变；
- B summary terminal但adoption前crash：A binding保持current且下次fresh规划；adoption FULL后crash：恢复
  同一B-generated fixed summary base与同一authoritative `SNAPSHOT_EXACT` US placement，不fallback full
  history；
- 后续同epoch只append，不每轮滑动。

### 13.4 Drift、cancel与错误

- canonical/source/A/B drift：释放candidate并fresh recapture，不window fallback；
- drift后fresh attempt仍只使用`retained_tool_groups = 0`，不回到默认`3`；
- session selector再次变化：当前turn仍使用frozen B；
- user cancel/Host close：停止，不adopt、不toast；
- A无可执行summary prefix或summary transport失败（包括额度耗尽、服务端不可用或超时）：可以进入
  Tier 3；该失败必须发生在主turn尚无semantic output或工具副作用时；
- replay corruption、source digest mismatch、invariant/ownership failure：typed fail，不进入Tier 3；
- A/B drift：fresh recapture；每个stable capture各最多一个逻辑A/B summary candidate；无repair时每个
  candidate各一个provider request，repair只增加request、不增加logical candidate；drift可产生新capture，
  不施加累计candidate/request cap；
- Tier 3完整suffix/evidence search的planning deadline耗尽：typed fail且不采用partial winner；
- Tier 2在前一阶段消耗任意planning时间后进入Tier 3：projection search取得fresh完整deadline并可成功；
  该deadline在B summary open前结束，健康B summary stream不继承它；
- Tier 3 B summary transport失败、summary非法或pre-adoption dry successor仍不合格：不安装projection、
  不normal open B，按typed failure结束且installed A binding保持不变；
- 所有summary-source suffix（包括empty）均无法hard-admit：typed target/input failure，不建立第四档；
- adoption FULL后的base/Hook/install失败：snapshot与B binding保持canonical winner，不回滚A，B normal
  open为0；下一dispatch从current binding normal cold read；
- semantic output后不做transparent provider/model/effort retry；
- Chat/Responses协议选错时不自动换API。

### 13.5 Compiler、prefix与replay

- B handover successor与Tier 3 summary-source candidates允许existing source
  `FULL -> COMPACT`和tool-result degradation；
- runtime-handoff COMPACT不被计为第二个summary stage；
- A summary prefix与installed A prefix exact；
- summary coverage与exact tail无重叠、无缺口；
- old provider-native reasoning不伪装成B replay；
- same-target ordinary compaction的`0.55`、minimum reclaim与exact join测试保持不变；
- normal send、A summary、B projected-source summary与B successor都复用adapter-local
  lowering/materializer/estimator。

### 13.6 UI

- Tier 1无divider/toast；
- Tier 2成功时只有一个现有灰色divider、无toast；
- Tier 3只有一个现有divider和一个右下角toast；
- toast不含token、ratio、stage、fallback、数据库或被省略数量；
- selector click不提前toast；
- reconnect不补发历史toast。

### 13.7 Architecture guards

- table/column/event kind/relation/job数量不增加；
- 无new DTO fingerprint、projection/window registry、receipt、checkpoint或feature flag；
- 无独立fallback summary模型、新summary prompt或`stage1/stage2` durable状态；Tier 3只使用turn-frozen B
  执行一次现有compaction summary contract；
- 无provider/model name branch；
- 无provider token-count API或vendor tokenizer；
- 无total model-switch/turn/task/provider-stream wall-clock cap；
- ordinary compaction与非模型切换子系统行为不变。

---

## 14. 验证命令

实施时根据实际测试文件名补充focused node，至少运行：

```bash
uv run pytest -q \
  tests/test_stage2_conversation_runner.py \
  tests/test_stage2_conversation_kernel_postgres.py \
  tests/test_round5_long_horizon_postgres.py \
  tests/test_round5a2_durable_provider_replay.py \
  tests/test_llm_model_target.py \
  tests/test_stage2_direct_model.py

uv run ruff check src tests
uv run pytest -q tests/test_stage3_5_architecture.py

npm --prefix frontend test -- --run
npm --prefix frontend run build

git diff --check
```

PostgreSQL验证必须使用verified local disposable DSN重建clean-v0，覆盖model binding、context snapshot、
零保留handover、Tier 3、drift restart与再次ordinary compaction。不得重置未经核验或remote数据库。

Real-provider dogfood至少覆盖：

1. 两个真实saved model connections，A context显著大于B；
2. A context包含assistant reasoning和complete tool groups；
3. 首个B-bound turn先由A handover；
4. 零保留handover candidate低于B trigger，B继续同一用户任务；
5. trace保留A/B target、wire API、本地quote、summary正文、normalized blocks与最终回答；
6. 只精确scrub API key value，不遮蔽其他诊断内容。

Tier 3主要由确定性fake/local provider证明；不得篡改真实provider输出以制造“dogfood通过”。

---

## 15. 明确禁止

1. 每轮发送前删除最老消息形成live sliding window。
2. selector一变化就在idle后台调用provider。
3. 修改running/queued turn的frozen binding。
4. 先让B fit-only compile/open或provider context error失败，再启动handover。
5. 用A budget或A wire quote批准B successor。
6. 放宽ordinary same-target transition validator来假装A/B相等。
7. 对A/B做minimum-reclaim token subtraction。
8. 把Tier 2实现成“A总结一次，B再总结A的summary一次”。
9. 新增creative/aggressive第二套summary prompt。
10. 把runtime-handoff FULL/COMPACT、Hook sibling或tool repair当成另一次handover compaction。
11. Model-switch handover先试任何非零retained group count，或在B验收失败后进行
    `3 -> 2 -> 1 -> 0`、`2 -> 1 -> 0`或任何smaller-tail search。
12. 零保留candidate失败后改变coverage复用旧summary，或再生成另一价handover summary。
13. 为未胜出candidate写snapshot/event/checkpoint或注册continuity。
14. 在semantic output后按错误字符串换provider/model/API/effort重跑。
15. 根据provider usage、HTTP 200/400或provider名称做切换决策。
16. Tier 3重放tool arguments/reasoning/provider-native replay，把retained tool result lower成live
    tool-role message，或把历史tool name重新lower成live `tool_calls`。允许的result只能是projection
    JSON中的exact advisory quotation。
17. Tier 3拆分历史turn、重排user/assistant顺序，或跳过较新unit去保留更旧unit。
18. Projection selection使用固定N条消息、字符数、固定per-result cap、`context * 0.5`分仓或canonical
    bytes作为fit proof。
19. Tier 3 projection携带一个已经不能帮助candidate达标的failed A summary。
20. Projected-source compaction成为ordinary compaction的silent fallback。
21. 把临时projection直接adopt为cold base，或让B先normal reply再被动等待mid-turn compaction。
22. 切回大模型时自动绕过current binding恢复genesis full history。
23. 新增projection/window表、omitted-message rows、model-switch event或toast receipt。
24. 新增target/quote/projection DTO fingerprint或registry。
25. 按provider/model名称维护handover分支。
26. 向用户展示candidate、ratio、token、fallback或数据库内部信息。
27. 给健康summary stream、model switch、turn或task增加total wall-clock cap。
28. 保留旧over-budget dead path、feature flag、v1/v2双路径或legacy alias。
29. 因empty projection candidate不fit就提前断言全部非空suffix也不fit，或对safe suffix/evidence
    candidates使用未经证明的单调性shortcut。
30. 用`KernelColdEpochInputAssembler`伪装Tier 3 projection summary source，或让ordinary semantic-budget
    validator在final-wire measurement前拒绝它；也不得伪造/nullable
    `ProviderPrefixCutProof`来承载destination projection。
31. 把pre-adoption dry successor的messages、source heads、borrow、wire plan或execution authority复用到
    post-FULL install。
32. 为Tier 3新增projection/source digest，绕开existing cumulative lineage digest，或让exact US同时
    出现在`SNAPSHOT_EXACT`与canonical suffix/recent quotations。
33. 把replay corruption、digest mismatch、invariant/ownership failure或未完成的deadline search伪装成
    fallback-eligible size failure。
34. 用final-wire全文中`US`字符串只能出现一次的断言重写或拒绝合法model summary；唯一性只约束
    Runtime-owned active-request authority placement。

---

## 16. Definition of Done

只有同时满足以下条件才算完成：

1. 首个B-bound `NEW_TURN`在fit-only ordinary compile前完成B final-wire precheck。
2. B quote低于resolved trigger时直接cold open，不调用summary。
3. 过trigger时由A读取旧context，由B exact wire与trigger验收successor。
4. Tier 2从一开始就使用`retained_tool_groups = 0`的唯一candidate，不使用ordinary
   `0.55`作为handover成功门槛。
5. 唯一candidate失败后不运行smaller-tail search，直接进入Tier 3；drift只能fresh
   recapture同一零保留算法。
6. 每个stable fenced capture的Tier 2最多一个逻辑A summary candidate、无repair时一个A provider
   request、一个pre-adoption B dry measurement；FULL后独立measure一次no-Hook与可选一次Hook sibling，
   drift fresh capture另计。即使随后进入Tier 3，整个switch attempt仍最多一次canonical adoption与最终
   selected B install/open。
7. Ordinary automatic/manual/mid-turn compaction的`3 -> 2 -> 1 -> 0`候选收缩契约不变。
8. Tier 3以完整历史turn生成保留user原话、assistant自然语言与ordered tool names的deterministic
   backbone，按B exact summary-call final-wire选择longest-safe suffix，并在同一winner范围内按exact
   incremental cost补入尽可能多的小tool-result advisory quotations。
9. 每个stable fenced capture的Tier 3 projection只驱动一个逻辑B summary candidate，可附既有唯一
   repair；drift fresh capture另计。B在summary successor FULL adoption前不normal reply、不执行用户
   工具，projection本身不持久化、不安装。
10. Projection候选走existing structural summary measurement/promotion seam；完整suffix搜索包含empty且不
    假定单调性，summary source proof以installed-prefix/destination-projection sealed branch表达，cold
    assembler只在B summary terminal后用于successor。
11. Tier 3成功后安装的是B-generated normal summary successor；它只按B trigger与hard admission验收，
    不新增`0.55` landing cap或第四档。
12. Adoption exact复用current binding/base、完整safe-head boundary与existing cumulative
    `source_digest`；active US固定`SNAPSHOT_EXACT`并只有一个Runtime authoritative placement，model
    summary的偶然文本复述不改变authority。
13. Pre-adoption dry plan只证明write可行；FULL后从adopted exact cut重建no-Hook/Hook final siblings，
    只有selected post-FULL plan可install。Post-FULL失败保留B snapshot且不回滚A。
14. Master compaction关闭时只允许Tier 1；两个ordinary入口子开关不改变model-switch三档语义。
15. 所有路径canonical transcript rows逐字、逐序不变。
16. Tier 2/Tier 3各只显示一个existing compaction divider。
17. 只有Tier 3显示一次克制toast。
18. Ordinary automatic/manual/mid-turn compaction的其他契约不变。
19. Chat/Responses lowering、replay、tools与estimator保持provider/model-neutral。
20. 无新增数据库结构、event、job、checkpoint、registry或不必要fingerprint。
21. Prefix、tool group、replay、ownership、deadline、restart及完整测试矩阵通过。

---

## 17. 外部代码真源的有限参考

Codex在model downshift时会比较old/new context window与active tokens；超过新模型阈值时优先用旧模型
运行compaction，再让新模型继续，而不是直接丢弃旧history。本文只吸收“old target先handover、
destination按自己的budget接收”这一产品语义，不复制Codex固定retained-token常量、rollout格式或UI
warning位置。

OpenCode把完整durable messages与provider active projection分层，并使用summary + recent tail；但其
model switch没有本文的old-target downshift handover。本文只吸收canonical/projection分层和按预算选择
recent suffix，不复制固定2K/8K/20K常量或provider error后被动fallback。

最终边界是：**三档是用户可理解的产品结果；第二档内部只使用一个零保留A handover candidate，
不进行protected-tail逐级收缩；第三档牺牲一部分source fidelity，把current effective lineage降维成B
可读的临时projection，并让B在正式回答前立即生成自己的normal compaction successor。Projection只为
B summary call服务，绝不成为长期cold base。**
