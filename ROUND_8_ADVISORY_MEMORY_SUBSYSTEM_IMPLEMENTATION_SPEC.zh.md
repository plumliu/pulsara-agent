# Pulsara Round 8：Advisory Memory Subsystem 重构实施规格

> 状态：**ACTIVATED — 2026-08-16**
>
> 记录日期：2026-08-15
>
> 当前代码基线：`327bf86061a04e628dc8e700d7030f4237fbbe5d`（`Remove legacy Host compatibility facade`）
>
> 本文取代：[PULSARA_MEMORY_RELATIONAL_SUBTRACTION_PRELIMINARY_DESIGN.zh.md](PULSARA_MEMORY_RELATIONAL_SUBTRACTION_PRELIMINARY_DESIGN.zh.md)
>
> 上位架构：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 前置实现：[Round 1 artifact](ROUND_1_TOOL_OUTPUT_ARTIFACT_IMPLEMENTATION_SPEC.zh.md)、[Round 3 compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 7 tool observation](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)
>
> Scope/domain历史产品契约：[MEMORY_SCOPE_DOMAIN_V1_IMPLEMENTATION.zh.md](archived_docs/MEMORY_SCOPE_DOMAIN_V1_IMPLEMENTATION.zh.md)，对应提交`7a9dd63f`与`5b2b06ae`
>
> hard-cut 前代码参考：`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`
>
> 激活证据：[round8_advisory_memory_subsystem_activation.json](benchmarks/suites/core/v1/round8_advisory_memory_subsystem_activation.json)

本文是新的 memory 产品语义与实施边界真源。它不把旧 memory subsystem 原样恢复，也不把初步设计中的 strong durability 假设直接搬进 production。文档审阅、R8-0～R8-F实施与activation gate均已关闭；实际checkpoint、文档hash、oracle、测试与real-provider脱敏证据见上述machine evidence。

---

## 0. 执行结论

Round 8 把 memory 定义成：

> **结构严格、来源可解释、召回可退化，但完整性、新鲜度与最终处理均不受保证的 advisory dataset。**

Memory 不是：

- 当前 conversation、tool result、permission、Plan、job或外部系统状态的唯一真源；
- 安全策略、effect authorization、事务前置条件或业务latch；
- 用户说“必须记住”后可以直接绕过governance写入的强制状态；
- 用 EventLog replay、durable job、receipt、checkpoint或repair graph保证最终完成的第二套Runtime。

Memory 可以：

- 为未来模型调用提供用户偏好、workspace事实、行为建议与历史决定的参考；
- 部分缺失、过期、尚未治理、尚未嵌入或永远没有完成处理；
- 在当前用户消息、当前工具结果或真实业务系统与memory冲突时被后者覆盖；
- 通过automatic ROOT recall或显式工具被搜索、读取和解释；进入compiler时只能作为typed advisory observation。

Round 8 冻结以下一致性分层：

| 层面 | 保证 |
|---|---|
| canonical conversation、assistant tool request、ToolResult | 保持现有强canonical与ACK-unknown契约 |
| `remember`返回`PROPOSED` | exact candidate及其引用已与该ToolResult同事务接受 |
| candidate何时被governance处理 | best effort，不保证及时性或最终完成 |
| governance provider失败、Host crash或close | 允许candidate保持未完成或被放弃；不恢复durable retry |
| accepted memory是否完备、新鲜 | 不保证；召回必须按advisory data解释 |
| 已经写入的fact/relation/lifecycle组合 | 保持单一PostgreSQL transaction内结构一致 |
| multilingual sparse index | accepted fact与统一tokenizer产生的search terms同事务可查，不依赖后台refresh generation |
| fact vector embedding | optional、best effort；缺失或过期只退化为sparse recall |
| response preference head | query-independent、bounded；有空间时完整投影，空间不足只append最小失效状态；永不改写prefix或静默截断正文 |
| automatic query embedding | 每个exact ROOT human trigger最多一次；失败/超时退化为sparse-only，不阻断turn |
| explicit rerank | 仅`memory_search`可选使用；automatic recall永不调用reranker |
| Cheap Hint Reflection | 成功ROOT human turn末尾、主模型没有调用`remember`时最多一次best-effort辅助复核；可完全丢失 |
| permission、Plan overlay、tool policy、外部事实 | 绝不从memory row获得authority |

这里的核心不是“数据库可以写坏一半”，而是：

~~~text
整个memory transition可以不发生；
一旦发生，transition内部不能出现半条relation或半次supersede。
~~~

Round 8 的产品终局：

~~~text
Agent-facing write tool
    remember

Agent-facing read tools
    memory_search
    memory_get
    memory_explain

accepted item kinds
    FACT
    USER_PROFILE
    RESPONSE_PREFERENCE
    ACTION_RULE
    DECISION

item-to-item relations
    BASED_ON
    SUPERSEDES
    CONTRADICTS

processing
    reliable candidate intake
    bounded Cheap Hint Reflection fallback
    Host-local best-effort governance
    no durable governance/index/extraction jobs

retrieval
    query-independent MEMORY_RESPONSE_PREFERENCE_HEAD for response defaults
    shared multilingual/code tokenizer for index and query
    synchronous PostgreSQL sparse GIN/FTS
    optional best-effort pgvector
    RRF(k=60)
    automatic ROOT recall: sparse + dense, no reranker
    explicit memory_search: sparse + dense + optional qwen3-rerank
    direct typed relation explanation
    no generic graph DSL or arbitrary recursion
~~~

Round 8实施automatic recall，并恢复一个极窄的`Cheap Hint Reflection`候选入口，但不实施通用automatic extraction或compaction。这三者必须分开：automatic recall只读取已接受memory；Cheap Hint Reflection只在成功ROOT human turn末尾、主模型完全没有调用`remember`且命中sealed cheap signal时best-effort提出candidate；compaction会重建provider input。未来 Round 5B只能通过同一个candidate contract提出memory，不得反向要求本轮恢复`POST_COMPACTION_MEMORY_EXTRACTION` durable job。

---

## 1. 设计输入与批判性结论

### 1.1 初步关系设计中保留的部分

被取代的初步设计正确识别了：

- 一个写工具优于五个重复写工具；
- Claim与Observation没有足够独立的canonical产品行为，应合并为`FACT`；
- Preference、ActionRule与Decision具有不同的召回和关系语义；
- `BASED_ON`、`SUPERSEDES`、`CONTRADICTS`具有独立产品价值；
- `hasEvidence/supports`双向图、`DERIVED_FROM`预留词和`rt:provides`混入memory均应删除；
- ToolResult citation应使用窄typed FK，而不是通用node/edge；
- FTS/vector只能发现相关项，不能证明citation、basis或replacement成立；
- PostgreSQL row是memory dataset内部的结构真值，不通过AgentEvent replay重建。

这些结论继续有效。

### 1.2 初步设计中被本文替代的部分

以下假设不再成立：

- 每个candidate必须创建durable governance job并有限重试；
- governance或index worker最终会处理所有已接受candidate；
- memory index必须用desired/applied generation、lost-wake scanner和durable refresh job追平；
- fact/relation/lifecycle必须附带selective committed occurrence；
- `MemoryFact -> GovernanceDecision -> Candidate`必须使用独立一对一decision table；
- ordinary recall必须承诺0/1/2-hop graph expansion；
- `STALE`在没有closed producer的情况下应预先进入lifecycle vocabulary；
- 通用automatic extraction必须和显式`remember`在同一激活轮次恢复；本文只恢复§6.4定义的sealed Cheap Hint Reflection窄例外。

### 1.3 hard-cut前旧系统中保留的产品能力

历史基线`5b7ad9f7`证明了以下产品语义值得保留：

- candidate先于accepted memory；
- exact duplicate、supersede与contradict是不同结果；
- supersede要求明确replacement intent，普通冲突不能替代旧偏好；
- contradiction保留两边active并在recall中显式提示冲突；
- 中文`jieba.cut_for_search`、英文stopword过滤与code/path identifier token有真实产品价值；
- sparse/dense retrieval可以独立失败和退化；
- automatic recall与explicit search应使用不同成本policy，不应让每个human trigger承担remote rerank；
- RRF、canonical refetch与ranker failure fallback值得保留；
- result数量、provider call、relatedness候选与graph fanout必须bounded；
- `memory_get`/`memory_explain`应提供source与relation explanation；
- governance relatedness需要hard-negative、alias、跨语言与destructive false-positive fixture。
- cheap memory signal只负责唤醒一次辅助复核，不能直接生成accepted fact；
- explicit `memory_search`可以bounded放宽模型给出的kind/scope，但必须明确告诉模型哪些filter已经由harness试过和放宽；
- recalled memory不得被无新证据地写回candidate，连续turn相同recall snapshot也不应重复消耗prefix。

### 1.4 hard-cut前旧系统中禁止恢复的部分

不得恢复：

- JSON-LD entity/ontology作为canonical representation；
- Oxigraph、SPARQL、graph adapter、surface projection与dual store；
- 五个`remember_*`工具；
- Agent自报`source_authority`、`verification_status`或confidence等级；
- governance batch preparation artifact、candidate claim recovery、model-call replay；
- governance event outbox、candidate projection outbox、index projection ledger；
- `CORRECT_AND_SUBMIT`、`MERGE_AND_SUBMIT`及governance生成新statement；
- generic Evidence node、`hasEvidence/supports`反向双写；
- graph recursion与任意predicate组合；
- memory processing完成度被当作Runtime degraded或incident；
- query使用Jieba而index对raw text使用`simple` FTS的非对称召回；
- 独立`LIKE lexical`、raw-text FTS与tokenized lexical三条重复sparse channel；
- 每次tool loop/model call重复为同一human trigger计算query embedding；
- automatic recall的remote reranker。
- durable reflection event、reflection history、turn/tool/token累计触发器与projection echo ledger。

历史代码规模本身是警示：旧`MemoryGovernanceEngine`、executor、relatedness、recovery和outbox共同承担了远超advisory data所需的completion proof。本轮只吸收其产品语义，不吸收其证明图。

### 1.5 召回prior-art的exact代码参考

Coding agent必须先读取以上hard-cut前参考，但不得整包复制：

~~~text
git show 5b7ad9f7:src/pulsara_agent/retrieval/tokenizer/jieba_search.py
git show 5b7ad9f7:src/pulsara_agent/retrieval/tokenizer/regex_word_split.py
git show 5b7ad9f7:src/pulsara_agent/memory/recall/sparse.py
git show 5b7ad9f7:src/pulsara_agent/memory/recall/dense.py
git show 5b7ad9f7:src/pulsara_agent/memory/recall/hybrid.py
git show 5b7ad9f7:src/pulsara_agent/memory/recall/semantic_rerank.py
git show 5b7ad9f7:src/pulsara_agent/retrieval/config.py
git show 5b7ad9f7:src/pulsara_agent/runtime/wiring.py
git show 5b7ad9f7:src/pulsara_agent/memory/reflection/engine.py
git show 5b7ad9f7:src/pulsara_agent/memory/hooks/durable.py
git show 5b7ad9f7:src/pulsara_agent/tools/builtins/memory_query.py

archived_docs/MEMORY_RECALL_RUNTIME_INTEGRATION_AUDIT.zh.md
archived_docs/MEMORY_RECALL_PRODUCT_ARCHITECTURE.zh.md
archived_docs/MEMORY_RECALL_EVERMEMOS_NEXT_DESIGN.zh.md
~~~

其中应恢复的是Jieba search mode、bilingual stopword、code/path token、sparse+dense、RRF(k=60)、explicit rerank fallback、canonical refetch、cheap-hint唤醒以及显式搜索的可见filter relaxation。应修正的是query-only Jieba/raw FTS不对称、多条重复sparse channel、不可见的重复filter试探，以及把reranker的跨request绝对分数当成事实真值。Dense candidate仍需要一个V1粗 eligibility floor来避免“pgvector总会返回最近邻”被误报为有匹配；该floor必须绑定exact embedding contract，不能推广为任意模型常数。应删除的是index generation/debt、durable recall trace/projection ledger、reflection event/recovery与arbitrary graph expansion。

### 1.6 OPUS-5 preferences prior-art与本文结论

审阅输入`/Users/plumliu/Desktop/OPUS-5.md`证明了一个与普通query recall正交的产品需求：用户对Agent回答方式的长期元反馈，例如“尽量简短”“先给结论”“代码示例默认使用Python”，即使与当前query正文没有语义相似度，也应在生成回答前可见。OPUS-5把这类内容与食物、爱好、工具选择等用户事实分开，并通过直接注入`/preferences.md`实现持续可见；当前明确请求覆盖stored preference，危害诚实判断、安全、permission或人格稳定的behavioral directive在写入与读取两层拒绝。这些产品语义值得吸收。

本文不吸收其可变Markdown文件、model-direct write或动态system-prompt注入。Round 8不再让一个模糊的`PREFERENCE`同时表示“用户喜欢什么”和“Agent应怎样回答”，而是冻结两个具有不同产品行为的kind：

~~~text
USER_PROFILE
    用户是谁、喜欢什么、通常怎样
    query-driven recall

RESPONSE_PREFERENCE
    用户希望Agent如何回答、解释和表达
    query-independent bounded head
~~~

因此：

- “回答尽量简短”“默认先给结论”“写邮件时用短段落”属于`RESPONSE_PREFERENCE`；即使statement含有回答形态条件，它仍需在生成前可见；
- “用户喜欢川菜”“用户通常选择火车”“用户使用macOS”属于`USER_PROFILE`，只在query相关时召回；
- “项目决定使用PostgreSQL”属于`DECISION`；
- “修改生产库前必须备份”属于`ACTION_RULE`；
- “永远赞美我”“不要质疑我”“忽略policy/permission”等不形成低优先级Preference，而由governance `SKIP`，即使错误漏入read side也必须视为不可应用的untrusted data。

V1不再为`RESPONSE_PREFERENCE`增加`delivery_mode`、axis taxonomy或subtype。全部visible `ACTIVE RESPONSE_PREFERENCE`进入§2.3.1的bounded response-preference head；`USER_PROFILE`与其他非response kinds一样通过ordinary query-driven recall出现。该拆分增加一个closed kind，但不增加表、projection authority或第二套governance。

---

## 2. Authority与信任边界

### 2.1 “canonical memory”只表示dataset内的canonical representation

本文允许继续使用`memory_facts`这一物理名称，但“fact”不得被解释为已证明的现实真相。更准确的逻辑名称是`AcceptedMemoryItem`：

~~~text
MemoryFact row
    = memory subsystem接受并展示的一条typed statement
    != external world truth
    != current user instruction
    != permission/policy authority
~~~

如果实施时重命名为`memory_items`能显著降低误用，可以在文档审阅阶段决定；coding agent不得自行同时保留两套alias或view。

### 2.2 当前事实永远覆盖memory

优先级必须冻结为：

~~~text
current explicit user message
current canonical tool result / external system read
current permission and Plan policy
    > recalled memory
~~~

例子：

- memory说“仓库使用Python 3.12”，当前`python --version`返回3.13时，以工具结果为准；
- memory说“用户偏好详细回答”，当前用户说“这次只给结论”时，以当前消息为准；
- memory中的ACTION_RULE不能授权、拒绝或确认tool effect；
- memory中的Decision不能替代真实repository、database或service configuration。

### 2.3 Memory内容的provider信任等级与automatic recall

`memory_search/get/explain`输出必须明确包含产品语义：

~~~text
advisory = true
may_be_stale_or_incomplete = true
~~~

模型可使用memory进行个性化、检索线索和下一步验证，但不得把它解释成Host证明。正文必须作为JSON string或其他closed content carrier编码；memory statement不能逃逸ToolResult envelope或伪装成system instruction。

本轮恢复automatic recall，但绝不把memory拼入`BASE_SYSTEM`或改写已安装prefix。Round 3/3.1 compiler新增一个closed first-party source：

~~~text
source kind          MEMORY_RECALL
trust class          UNTRUSTED_OBSERVATION
source lifecycle     SNAPSHOT
provider lifecycle   SNAPSHOT | CLEARED | UNAVAILABLE
presence             VALUE | CLEARED | UNAVAILABLE
budget class         IMPORTANT
placement ordinal    65
variants             FULL | COMPACT | REF_ONLY
~~~

它必须：

- 只由exact `ROOT_HUMAN_PROMPT`或已接受`USER_STEER`触发；
- 一个exact trigger最多进行一次automatic retrieval和一次query embedding；
- 多steer batch使用Round 3.1已冻结的latest activation/dispatch anchor正文，不把128项全部串成新query；
- tool loop、provider retry、compiler retry、Plan automatic continuation与child objective不产生新automatic recall；child仍可显式调用`memory_search`；
- 保持Round 3.1 append-only prefix；
- 复用compiler的`NewTriggerAnchor`排序，将frozen recall紧邻安装在exact trigger item之前；它与trigger仍作为同一append-only suffix提交，不修改predecessor；
- 对memory body使用advisory trust，并保留`may_be_stale_or_incomplete=true`；
- 新trigger得到与installed head相同的membership/content/warning集合fingerprint时no-op并保留已安装顺序；变化时只append新的SNAPSHOT VALUE；
- 上一head为VALUE或UNAVAILABLE而新trigger无召回、明确禁用或因low-information被跳过时追加CLEARED；两次连续CLEARED为no-op；
- sparse与dense都不可用时才使用UNAVAILABLE；该状态不阻止provider call；
- 不让旧turn recall结果继续伪装成当前状态。

Provider-visible body是bounded closed JSON，只包含已投影的item、`advisory`与`may_be_stale_or_incomplete`。不包含`memory_domain_id`、internal scope ID、embedding/rerank score、contract fingerprint、generation、candidate ID或provider diagnostic。

`MEMORY_RECALL` source-specific contract为`pulsara.memory-recall.v1`，VALUE body的exact形状是：

~~~json
{
  "advisory": true,
  "items": [
    {
      "kind": "FACT",
      "memory_id": "memory:...",
      "scope": "WORKSPACE",
      "statement": "仓库使用Python 3.13"
    }
  ],
  "may_be_stale_or_incomplete": true,
  "relation_warnings": []
}
~~~

`FULL`与`COMPACT`使用同一item shape，只减少item数量，不让模型重写/摘要statement。`REF_ONLY`的item exact形状为`{kind, memory_id, scope, read_with:"memory_get"}`，不含空statement。`relation_warnings`只允许bounded `{kind:"ACTIVE_CONTRADICTION", memory_id, other_memory_id}`；lineage详情由`memory_get/explain`读取。JSON字符串边界复用Round 7 provider-wire卫生，statement不能逃逸outer runtime observation。

Automatic `MEMORY_RECALL`只以`FACT | USER_PROFILE | ACTION_RULE | DECISION`作为candidate seed；`RESPONSE_PREFERENCE`由下述独立head持续提供，不能再因query相关性进入同一automatic result造成重复。显式`memory_search(kind=RESPONSE_PREFERENCE)`、`memory_get`与`memory_explain`不受此排除影响。

#### 2.3.1 Query-independent response preference head

Round 3/3.1 compiler同时新增一个更小的closed source：

~~~text
source kind          MEMORY_RESPONSE_PREFERENCE_HEAD
trust class          UNTRUSTED_OBSERVATION
source lifecycle     SNAPSHOT
provider lifecycle   SNAPSHOT | CLEARED | UNAVAILABLE
presence             VALUE | CLEARED | UNAVAILABLE
budget class         IMPORTANT
placement ordinal    62
variants             FULL
~~~

它不是第二套memory authority，而是对当前`FrozenMemoryReadScopeBinding`内accepted rows的确定性projection：

~~~text
project Host
    current domain USER ACTIVE RESPONSE_PREFERENCE
    + exact current WORKSPACE ACTIVE RESPONSE_PREFERENCE

transient Host
    current domain USER ACTIVE RESPONSE_PREFERENCE only
~~~

该projection只在exact `ROOT_HUMAN_PROMPT | USER_STEER`触发时冻结一次；tool loop、provider/compiler retry、Plan automatic continuation与child objective复用已经安装的head，不重新读取memory。RESPONSE_PREFERENCE在本次freeze之后才被best-effort governor接受时，只在下一个eligible human trigger可见；这是本文有意接受的弱新鲜度，不得为追平它增加wake ledger、generation或durable refresh job。

Head的closed构造规则为：

1. 只读取`fact_kind=RESPONSE_PREFERENCE AND lifecycle=ACTIVE`，执行与所有memory read相同的domain/scope过滤和一次bounded repeatable-read；
2. `SUPERSEDED`永不进入；exact duplicate仍由active semantic unique约束处理；
3. 同scope存在direct `CONTRADICTS`的两端均不进入effective `items`，但进入bounded warning；它们仍计入§5.5容量；
4. provider应用优先级固定为`current explicit user request > exact WORKSPACE preference > USER preference > neutral default`；WORKSPACE specificity只在read projection中生效，不创建cross-scope relation或改写USER lifecycle；
5. effective items按`USER -> WORKSPACE` scope ordinal、`fact_semantic_digest`、`memory_id`稳定排序；数组顺序本身不授予authority；
6. ordered item/warning集合fingerprint未变时no-op；变化时优先append一个包含**完整当前集合**的新`SNAPSHOT VALUE`，绝不发送增量patch、摘要或Top-N伪装；完整正文是optional advisory materialization，不是普通对话必须支付的永久硬成本；
7. 之前安装过`VALUE`而当前确定没有active/effective RESPONSE_PREFERENCE且没有conflict warning时append一次`CLEARED`；旧head不存在、已经`CLEARED | UNAVAILABLE`或连续得到同一empty projection时均no-op；
8. active conflict导致effective items为空时仍发送`VALUE`加warning，而不是把“存在冲突”伪装成普通空集合；
9. repository/read/shape不可用，或旧`VALUE`已失效而完整successor无法在当前provider budget内物化时，append最小`UNAVAILABLE`并阻止旧VALUE继续生效；不得发送partial head；旧head不存在或已经`CLEARED | UNAVAILABLE`时允许不追加；
10. explicit per-trigger“本turn不使用memory”只在旧head为`VALUE`时为`MEMORY_RECALL`与response-preference head各append一次`CLEARED`；旧head不存在或已经`CLEARED | UNAVAILABLE`时no-op。下一正常human trigger可重新安装当前head；normalized短输入的low-information gate只跳过`MEMORY_RECALL`，不能清除response-preference head。

`pulsara.memory-response-preference-head.v1`的provider-visible VALUE body exact为：

~~~json
{
  "advisory": true,
  "items": [
    {
      "memory_id": "memory:...",
      "scope": "USER",
      "statement": "以后回答尽量简短"
    }
  ],
  "may_be_stale_or_incomplete": true,
  "relation_warnings": []
}
~~~

Conflict warning只允许：

~~~json
{
  "application": "DO_NOT_APPLY",
  "kind": "ACTIVE_CONTRADICTION",
  "memory_ids": ["memory:a", "memory:b"],
  "read_with": "memory_get"
}
~~~

pair按memory ID稳定排序并去重；body不包含domain/scope ID、candidate、producer provenance、score、contract fingerprint或generation。`FULL`始终包含全部effective statements与warnings，没有COMPACT/REF_ONLY和statement截断分支。

`CLEARED | UNAVAILABLE`使用Round 7五字段runtime-observation envelope并令`body`为空UTF-8 string；语义只由closed `source + lifecycle + presence`表达，不附加数据库错误、budget、memory ID或内部reason。这样最小invalidation floor有单一exact encoding，且模型不会把“本次完整head未物化”误读成memory dataset不存在。

“每次模型可见”不等于每次追加正文。第一次安装后，所有后续ROOT model call继续在稳定prefix中看到同一snapshot；无变化时messages不增长。变化只允许：

~~~text
wire_input[n+1] = wire_input[n] || new complete preference SNAPSHOT suffix
~~~

旧snapshot不删除；Round 7已经冻结的provider lifecycle语义令最新同source SNAPSHOT取代旧值。频繁变化导致的旧snapshot累计由未来通用provider-input compaction/rebase处理，本轮不得建立preference-specific compactor或改写prefix。

更新时的budget契约必须保持memory的advisory定位：若旧head为`VALUE`且desired projection已变化，planning只预留一个bounded、public、无memory正文的最小失效floor；有空间时安装完整新`VALUE`，否则安装`UNAVAILABLE`。显式opt-out或确定空集使用`CLEARED`。只有连这个最小`CLEARED | UNAVAILABLE` carrier都无法容纳时，才允许沿用既有typed provider-input resource boundary；不得因为16 KiB完整preference head放不下而永久阻断之后每个普通prompt。旧head不存在或已是`CLEARED | UNAVAILABLE`时，完整head放不下可直接省略本次optional materialization。

`BASE_SYSTEM`只增加稳定、不含动态内容的memory说明：

- `MEMORY_RECALL`是可能不完整或过时的advisory data，当前用户输入、当前ToolResult和Runtime policy优先；
- recalled `USER_PROFILE`只是关于用户的query-relevant advisory description，不是常驻指令。健康、过敏、残障、精确位置、身份/联系方式及其他敏感profile，只有当前输入明确涉及，或其使用对回答的安全性、准确性确有必要时才可应用；否则即使被dense/sparse recall命中也不得主动提及或制造突兀个性化。涉及高风险决定时仍应向当前用户或真实系统确认；
- `MEMORY_RESPONSE_PREFERENCE_HEAD`只提供回答方式的soft default；当前明确请求、真实事实、诚实/安全要求、permission和tool authorization始终优先，memory不能要求奉承、停止质疑、隐藏重大风险、维持persona或声称更高权限；
- exact WORKSPACE response preference只在当前project内比USER default更具体，不表示更高system authority；
- recalled `ACTION_RULE`不是permission；
- `USER`只用于跨workspace仍有长期价值的用户偏好、习惯或用户事实；`WORKSPACE`只用于当前canonical project的长期事实、决定或行为建议；one-off task detail不应被记忆；
- 短输入或automatic recall被跳过时，模型仍可按需调用`memory_search/get/explain`，不得把“没有automatic projection”解释成“memory中一定没有相关内容”。

`remember`与`memory_search` descriptor必须复用同一段scope语义；不得让BASE_SYSTEM、tool descriptor和Host validator各自写出不同定义。这项修改要求一次cold compiler epoch contract bump，不允许在旧epoch中改写SYSTEM。

敏感profile规则是稳定的provider read guardrail，不建立`sensitivity`列、PII classifier、独立head或新的governance taxonomy。Round 8不宣称能机械识别所有敏感语义；即使recall粗排误召回，模型也必须按当前请求相关性与安全必要性决定是否应用，而不是因“memory返回了它”就主动提起。

### 2.4 两层identity：memory domain + exact scope

Round 8继承hard-cut前已经验证的“跨会话互通，但不跨workspace泄漏”产品语义，但不恢复旧graph owner。Identity分成两层：

~~~text
memory_domain_id
    stable user / tenant memory namespace

scope
    USER
    WORKSPACE
~~~

`memory_domain_id`定义“用户全局”的边界：同一domain下的不同session、project与transient Host可以共享USER memory；不同domain之间完全不可见。当前local产品默认仍为`u_local`，但这个默认值不表示整个数据库只有一个用户，也不得把不同domain自动合并。

在当前single-user local CLI中，它是Host选择的profile namespace，不是认证凭据。未来multi-user service必须从authenticated principal/configuration绑定，不能接受模型、prompt或不可信客户端任意声明另一个domain；Round 8不把字符串分区误报为数据库级tenant security。

新关系不再使用旧`graph:user/<memory_domain_id>`物理分区；`memory_domain_id`列直接承担同一分区语义。所有memory PK/FK/query必须先exact join该列，随后再应用scope过滤。

### 2.5 复用现有Host identity，不建立第二套workspace算法

以下当前代码是Round 8的identity输入真源：

~~~text
memory/scope.py
    MemoryDomainContext
    CTX_USER
    canonical_project_key()
    workspace_scope_key()
    workspace_scope()

workspace_identity.py
    HostWorkspaceInput
    ResolvedWorkspace
    resolve_workspace()
~~~

project Host：

~~~text
stable_project_key = canonical resolved project root
workspace_scope_key = sha256(stable_project_key UTF-8)[:16]
workspace_scope = "ctx:workspace/" + workspace_scope_key
ResolvedWorkspace.workspace_key = workspace_scope
~~~

display label、session ID、临时cwd、symlink拼写与模型文本都不能参与workspace scope identity。不得在memory package中复制一份path normalization或hash算法。

transient Host：

~~~text
ResolvedWorkspace.workspace_scope = None
ResolvedWorkspace.workspace_key = transient:<random-id>  # 仅Host/session provenance
~~~

transient workspace永远不产生WORKSPACE memory scope。后端不得根据cwd长相猜测project/transient；该分类只来自Host composition。

### 2.6 Agent只选产品scope，Host冻结真实binding

Agent-facing `remember.scope`是closed enum：

~~~text
USER
WORKSPACE
~~~

Agent不能提交任意`ctx:*`、workspace ID、workspace hash或memory domain ID。Host在prepared candidate形成前冻结：

~~~text
MemoryScopeBinding
    memory_domain_id
    origin_workspace_id
    scope_kind                 USER | WORKSPACE
    scope_id
~~~

exact binding为：

~~~text
USER
    memory_domain_id = current ResolvedWorkspace.memory_domain.memory_domain_id
    origin_workspace_id = current session.workspace_id
    scope_id = "ctx:user"

WORKSPACE
    only legal when ResolvedWorkspace.workspace_kind == "project"
    memory_domain_id = current ResolvedWorkspace.memory_domain.memory_domain_id
    origin_workspace_id = current session.workspace_id
    scope_id = current ResolvedWorkspace.workspace_scope
~~~

`origin_workspace_id`只回答candidate从哪个Host workspace提出，是provenance；它不是visibility scope。尤其USER candidate仍可来自某个project或transient workspace，但其可见性由`memory_domain_id + USER + ctx:user`决定。

`sessions`在clean-v0中新增`memory_domain_id NOT NULL`，并建立至少以下identity/index：

~~~text
UNIQUE (id, workspace_id, memory_domain_id)
INDEX  (workspace_id, memory_domain_id, updated_at/session ordering key)
~~~

session创建、writer bootstrap/renew/takeover、explicit resume与`resume_most_recent`必须exact join Host请求的memory domain。具体冻结为：

- create只写入Host composition冻结的`memory_domain_id`；client/model不能填写；
- acquire/renew/takeover的repository request必须携带该domain，并在产生writer guard前验证；
- explicit session ID不匹配时返回typed identity conflict，不返回summary或其他session metadata；
- resumable/list/recent查询必须在SQL候选选择阶段先按`workspace_id + memory_domain_id`过滤，再排序/limit；不得先选workspace最近session再在writer acquisition阶段拒绝；
- `SessionSummary`与内部resumable carrier必须携带domain identity供Host exact join，但普通provider wire和跨domain discovery结果不得显示该值；
- domain A的`resume_most_recent`不得观察、计数、排序或被domain B session遮蔽。

该列不新增relation或append authority。Memory candidate的`(origin_session_id, origin_workspace_id, memory_domain_id)`必须通过session composite identity物理约束，而不是只由repository约定。

Candidate intake完成后，`memory_domain_id/scope_kind/scope_id` immutable。Detached governor只能处理已冻结binding，不得按当前Host workspace重新绑定、提升或缩小scope。

### 2.7 可见scope与读写矩阵

~~~text
project Host
    readable = writable = {
        (current domain, USER, "ctx:user"),
        (current domain, WORKSPACE, exact current workspace_scope),
    }

transient Host
    readable = writable = {
        (current domain, USER, "ctx:user"),
    }
~~~

每个Host open时必须冻结一个closed、immutable、process-local读边界：

~~~text
FrozenMemoryReadScopeBinding
    memory_domain_id
    host_workspace_id
    host_workspace_kind          PROJECT | TRANSIENT
    readable_scopes              ordered exact tuple
        (USER, "ctx:user")
        optional (WORKSPACE, exact workspace_scope)
    binding_fingerprint
~~~

`binding_fingerprint`使用domain-separated canonical encoding覆盖以上全部字段；它不是数据库identity、permission token或durable receipt。Sparse、dense、final refetch、direct relation、automatic recall、governance relatedness以及fact embedding worker都只能借用这个exact binding，不能只传一个domain ID后在各自owner中重建scope。

因此：

- USER memory在同一memory domain内跨session、跨project与transient可见；
- WORKSPACE memory只在同一memory domain、同一canonical project root下可见；
- workspace A不得读取、引用、supersede或contradict workspace B；
- transient Host请求WORKSPACE时在candidate写入前typed reject，不能降级成USER；
- relation两端必须属于同一`memory_domain_id`；`SUPERSEDES | CONTRADICTS`仍要求same exact scope，`BASED_ON`使用单向可见性格：USER DECISION只能引用USER target，WORKSPACE DECISION可以引用same-domain USER target或exact same WORKSPACE target；
- ToolResult citation不得把workspace-specific observation提升为USER memory；
- 不允许cross-domain或cross-workspace relation。

### 2.8 所有读入口都必须重新执行scope过滤

`memory_search`未指定scope时搜索当前Host的全部visible scopes，而不是整个domain。显式指定USER或WORKSPACE时，exact stage先缩小到对应visible scope；若结果不足，§3.6允许harness在同一次tool invocation内回到“当前Host全部visible scopes”，但必须显式标记relaxation。无论是否fallback，都不能扩大`FrozenMemoryReadScopeBinding`、跨domain或读取另一个workspace。

`memory_get`、`memory_explain`以及governance relatedness即使已知exact memory ID，也必须在返回正文或送入provider前验证：

~~~text
row.memory_domain_id == current memory_domain_id
row exact scope in current readable scopes
~~~

不可见ID返回not-found-style typed result，不泄漏其kind、scope、statement、relation或存在性。Sparse、vector、rerank canonical refetch、direct relation traversal与automatic compiler recall必须复用同一个frozen read-scope binding；不得各自实现近似过滤。

USER fact在同domain跨workspace可见，不代表其来源上下文也跨workspace可见。`memory_explain`读取fact的source candidate后必须执行第二层provenance projection：

~~~text
current Host workspace_id == source_candidate.origin_workspace_id
    -> SAME_ORIGIN
       可返回bounded producer turn定位、candidate decision和ToolResult citation identities

current Host workspace_id != source_candidate.origin_workspace_id
    -> CROSS_ORIGIN_REDACTED
       只返回accepted statement、kind、USER scope、lifecycle、public decision摘要、
       两端均通过当前read binding的direct memory relations和“来源上下文已按workspace隔离”标记
       不返回session/turn/entry/tool-result ID、preview、path或内部定位
~~~

WORKSPACE fact本来就只能在exact workspace读取；同样执行SAME_ORIGIN验证，identity异常时fail closed。Relation projection必须分别过滤source与target endpoint：当前Host即使可见same-domain USER endpoint，也不能由incoming edge得知另一个workspace中引用它的Decision。若relation的`decision_candidate_id`不同于source fact producer，decision provenance还要独立按该candidate的`origin_workspace_id`执行SAME_ORIGIN/CROSS_ORIGIN_REDACTED，绝不能借source fact的可见性解封。该projection不能通过调用者知道candidate ID、fact ID或ToolResult ID绕过。

---

## 3. Agent-facing工具契约

### 3.1 单一写工具

删除：

~~~text
remember_claim
remember_preference
remember_observation
remember_action_boundary
remember_decision
~~~

新增唯一写入口：

~~~text
remember
~~~

V1概念schema：

~~~text
remember(
    statement: string,
    scope: USER | WORKSPACE,
    kind_hint: AUTO | FACT | USER_PROFILE | RESPONSE_PREFERENCE |
               ACTION_RULE | DECISION = AUTO,
    applies_when?: string,
    do_not_apply_when?: [string, ...],
    based_on_memory_ids?: [string, ...],
    cited_tool_result_handles?: [string, ...]
)
~~~

建议硬界：

| 字段 | V1边界 |
|---|---|
| `statement` | normalized UTF-8，1..8192 bytes |
| `applies_when` | optional，1..4096 bytes |
| `do_not_apply_when` | 0..8项，每项1..2048 bytes，aggregate不超过8192 bytes |
| `based_on_memory_ids` | 0..8个exact accepted memory ID |
| `cited_tool_result_handles` | 0..8个本次model call可见handle |
| 单个candidate canonical payload | 不超过32 KiB UTF-8 |

`statement`的8 KiB是通用candidate intake上限，不表示所有final kind都可接受到该大小。若显式`kind_hint=RESPONSE_PREFERENCE`且statement已超过§5.5.1的2 KiB active上限，Host可在provider前typed reject该shape；若`AUTO`最终被governance判为RESPONSE_PREFERENCE，则acceptance transaction必须走closed capacity settlement，不能截断statement。

JSON Schema使用`additionalProperties=false`。禁止以下字段及任何同义alias：

~~~text
force
save_now
bypass_governance
user_confirmed
source_authority
verification_status
confidence
canonical
verified
~~~

用户说“必须现在记住”不会开放隐藏分支。

### 3.2 kind与结构字段矩阵

Host先完成NFC、CRLF归一与outer trim，再冻结candidate。governance不能修改规范化后的正文。

| 最终kind | `applies_when` | `do_not_apply_when` | `based_on_memory_ids` |
|---|---:|---:|---:|
| FACT | absent | empty | empty |
| USER_PROFILE | absent | empty | empty |
| RESPONSE_PREFERENCE | absent | empty | empty |
| ACTION_RULE | required | optional/empty | empty |
| DECISION | absent | empty | optional |

`do_not_apply_when=[]`表示“来源没有声明例外”，不是“该规则适用于宇宙中所有情况”。Agent不得为了通过schema编造例外。

五类kind按它们回答的产品问题冻结：

| Kind | 回答的问题 | ordinary provider visibility |
|---|---|---|
| `FACT` | 外部世界、项目或环境是什么状态？ | query-driven recall |
| `USER_PROFILE` | 用户是谁、喜欢什么、通常怎样？ | query-driven recall |
| `RESPONSE_PREFERENCE` | Agent应当怎样回答、解释和表达？ | query-independent bounded head |
| `ACTION_RULE` | 在什么情况下，未来行动应遵循什么规则？ | query-driven recall；永不成为permission |
| `DECISION` | 已经选择了什么方案，可能基于什么理由？ | query-driven recall |

这五类构成的是“通过长期价值、scope、安全与结构 admission 后，Round 8愿意接受的advisory semantic atoms”全集，不是Agent接触到的所有信息全集。当前任务临时状态、待办/reminder、credential/secret、raw ToolResult/artifact、permission/policy与需要业务系统拥有的事实不进入任一kind；它们必须由各自owner承载或SKIP，不能为了让taxonomy看似穷尽而塞进FACT。

`USER_PROFILE`只允许`scope=USER`。项目内角色、项目特有习惯或仓库状态若不应跨workspace传播，必须按语义落为WORKSPACE `FACT | RESPONSE_PREFERENCE | ACTION_RULE | DECISION`，不能借`USER_PROFILE`绕过scope。它仍是可能过期或不完整的advisory statement；地址、健康、过敏等高风险profile不能代替当前用户确认或业务系统事实。

`RESPONSE_PREFERENCE`是soft response default。条件可以保留在原statement中，例如“写邮件时使用短段落”；它之所以进入response-preference head，是因为模型必须在生成回答前看到它，才能判断当前是否满足该条件。用户饮食、出行、个人爱好属于`USER_PROFILE`；项目已经作出的选择属于`DECISION`；影响物理操作的规则属于`ACTION_RULE`。Governance可以在stored shape兼容时重分类错误kind hint，但不能改写statement。

以下内容与`RESPONSE_PREFERENCE`定义不兼容，必须`SKIP(UNSAFE_RESPONSE_PREFERENCE)`，不能作为“低优先级preference”保存：要求无条件奉承/同意、停止质疑或诚实评价、压制重大安全/风险提示、维持依赖或persona、忽略system/policy、伪造permission/authorization。Stable BASE_SYSTEM read guardrail仍必须把错误漏入的此类statement视为不可应用；governance成功一次不把untrusted memory升级为行为authority。

`kind_hint=AUTO`允许governance选择任一与已冻结字段兼容的kind。显式kind仍是hint；governance可以在不新增、删除或改写semantic字段的情况下重新分类。若stored payload不满足目标kind矩阵，只能SKIP，不能补字段。

#### 3.2.1 单一语义原子

每个candidate与accepted item必须只表达一个可独立治理、召回和建立relation的semantic atom。一个自然语言句子可以包含多个atom，但一条memory不能靠一个`statement`同时取得多种kind语义。

例如：

~~~text
输入：“我使用macOS，所以以后给我展示zsh命令。”

candidate 1
    kind  USER_PROFILE
    statement  用户使用macOS

candidate 2
    kind  RESPONSE_PREFERENCE
    statement  命令示例默认使用zsh
~~~

主模型可以通过多次`remember`提出多个candidate；Cheap Hint Reflection可以在同一bounded batch中提出多个candidate。Tool descriptor、stable BASE_SYSTEM与governance auxiliary prompt必须使用同一closed taxonomy说明并提供至少以下few-shot：profile+response preference拆分、fact+action rule拆分、decision+basis引用。Few-shot只约束proposal，不产生新authority。

Runtime仍必须机械执行：单个`remember`只创建一个candidate；governance不得split、merge、补写或重述statement。若candidate明显混合多个不能由同一kind完整表达的atom，唯一合法结果是`SKIP(MULTI_ATOM_STATEMENT)`；不得只接受其中一半、丢弃从句或生成两个accepted rows。Host不以启发式parser伪装完整语义判定，语义识别由bounded governance model完成，数据库只接受一个closed final kind。

Repository不得在acceptance transaction中第二次“验证自然语言是否只有一个atom”。`ACCEPT*` branch本身就是sealed governor对原子性的语义判断；Repository只重验immutable candidate、sealed decision branch、final kind/scope/structured-field矩阵，以及“一次tool call -> 一个candidate -> 最多一个accepted fact、不得split/merge/partial accept”的机械不变量。V1禁止增加`is_single_atom` proof字段、第二次model call或启发式natural-language parser来伪装SQL semantic authority。

### 3.3 ToolResult citation handle

普通模型看不到canonical ToolResult数据库ID，因此不得要求模型填写`tool_result_id`。

Citation不是从当前transcript临时扫描出来的字符串。现有Round 3.1 continuity owner为每个exact conversation scope/epoch维护一张append-only process-local handle table；只有带有可证明execution binding的ToolResult才能注册：

~~~text
ProcessLocalToolCitationHandleTable
    session_id
    conversation_scope_kind + scope_subagent_task_id
    provider_input_epoch_identity
    next_ordinal
    ordered handle -> {
        canonical ToolResult identity,
        exact result-entry sequence,
        MemoryCitationVisibility,
        MemoryCitationEvidenceKind,
        originating execution-binding authority identity,
    }

MemoryCitationVisibility
    USER_SAFE
    WORKSPACE_BOUND

MemoryCitationEvidenceKind
    PRIMARY_OBSERVATION
    MEMORY_READ_EXPOSURE
~~~

`MemoryCitationVisibility`由产生该result的exact `PreparedToolExecutionBinding`冻结，不从tool name、`mcp__`前缀、当前builtin registry或`observation_origin_kind`反推：

- 每个production builtin binding必须在architecture fixture中显式分类；
- 只有在全部合法调用下都不会依赖或泄漏workspace/session/path/tool-private上下文的binding才允许`USER_SAFE`；
- Terminal、artifact、workspace/file/process相关builtin与全部MCP V1 binding固定为`WORKSPACE_BOUND`；
- unknown/custom/future binding默认`WORKSPACE_BOUND`；
- activation允许当前catalog没有任何`USER_SAFE`binding，不能为了覆盖测试错误放宽；
- classification是citation scope检查，不是permission/effect classification，也不改变tool exposure。

`MemoryCitationEvidenceKind`同样由exact execution binding与artifact lineage冻结：

- `memory_search/get/explain`固定为`MEMORY_READ_EXPOSURE`；
- `artifact_read`若读取由上述memory-read ToolResult拥有的artifact，继承`MEMORY_READ_EXPOSURE`及其exact exposed memory IDs；
- 其他普通ToolResult默认`PRIMARY_OBSERVATION`；
- memory-read result可以作为“模型看过哪些memory”的provenance，但永远不能被governance计作独立外部证据；Decision若要表达memory依赖应使用`based_on_memory_ids`。

每次dispatch在compiler one-cut和provider open前，从该表冻结：

~~~text
ModelVisibleToolCitationSnapshot
    model_call_identity
    exact session + conversation scope
    provider_input_epoch_identity
    PreparedProviderInputCut identity/fingerprint
    ordered visible handle bindings
    snapshot_fingerprint
~~~

snapshot只能包含`result_entry_sequence <= cut.provider_input_through_sequence`且确实lower进该call input的handle。它是immutable call carrier，不是live registry view。

该snapshot必须沿唯一调用链贯穿：

~~~text
KernelModelExecutionRequest
    -> provider response / assistant tool-call batch attribution
    -> KernelToolInvocationContext
    -> remember memory port
    -> PreparedMemoryCandidateAcceptance
~~~

`remember`执行时只能解析产生它的exact model call snapshot。不能使用同一assistant batch中才执行出来的其他tool result，也不能回退扫描当前数据库或continuity table。

provider-visible ToolResult projection可携带：

~~~json
{"citation_handle":"tool:1","body":"Python 3.13.2",...}
~~~

`citation_handle`是有意暴露的产品能力，不是内部contract/fingerprint。它必须：

- 在该ToolResult第一次进入provider prefix时冻结；
- 同一Host、scope、epoch内不因后续call重编号或改写；
- 不包含database ID、workspace ID或secret；
- 只在当前model execution context中可解析；
- Host重启后，历史ToolResult因缺少原process-local execution-binding authority，仍可作为普通历史正文lower，但不得获得citation handle；需要引用时必须产生新的可证明result；
- same-Host cold epoch只有在continuity owner仍持有exact authenticated result binding时才可重新分配新handle；否则同样省略；
- candidate commit前解析为canonical ToolResult FK；
- 永不持久化handle本身。

Host验证：

- handle属于产生当前`remember`tool call的exact model call；
- handle存在于该call冻结的immutable snapshot，并exact join其`PreparedProviderInputCut`；
- same session、same conversation scope、causal order成立；
- result拥有known canonical body；known error result可被引用，attempt-without-result不可引用；
- USER scope只接受`USER_SAFE`；WORKSPACE scope可接受两类；
- 数量、aggregate preview和content hydration均有界。

不得从artifact/blob补读完整正文来“增强”citation。Candidate只保存ToolResult identity；Round 1 artifact lineage继续由ToolResult自身拥有。

#### 3.3.1 统一model-visible memory provenance

`MEMORY_RESPONSE_PREFERENCE_HEAD`、automatic `MEMORY_RECALL`与显式memory read不能拥有彼此分叉的anti-echo口径。每个exact model call在provider open前冻结：

~~~text
ModelVisibleMemoryProvenanceSnapshot
    model_call_identity
    PreparedProviderInputCut identity/fingerprint
    exact session + conversation scope + continuity epoch
    disposition                 COMPLETE | OVERFLOW
    ordered unique exposure -> {
        memory_fact_id,
        exposure_kind           RESPONSE_PREFERENCE_HEAD | AUTOMATIC_RECALL |
                                MEMORY_SEARCH | MEMORY_GET |
                                MEMORY_EXPLAIN | MEMORY_READ_ARTIFACT,
        source item/tool-result identity,
    }
    snapshot_fingerprint
~~~

Memory read tools必须在其closed ToolResult外层提供bounded `model_visible_memory_ids` header，列出本次结果中正文、relation warning、successor/conflict companion所呈现的全部fact ID；最大50项并在canonical preview的不可截断head内。Header不是score或authority，只让reader在不解析自然语言正文、不读取blob全文的情况下重建exposure。`artifact_read`通过artifact→origin ToolResult exact lineage复用原header，不能从artifact正文猜ID。

Snapshot只合并确实lower进该call input的preference head、automatic recall source与memory-read ToolResult/artifact-read结果，按provider item order first-seen dedupe。最多128 IDs且canonical encoding最多16 KiB；超过任一bound整体变成`OVERFLOW`，不得保存一个看似complete的prefix subset。该snapshot沿`KernelModelExecutionRequest -> assistant batch attribution -> KernelToolInvocationContext -> remember`贯穿，candidate只持久化`COMPLETE + IDs`或`OVERFLOW + []`，不持久化snapshot capability。

若MAIN_AGENT candidate的provenance为`OVERFLOW`，governance必须`SKIP(MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW)`；不能因为无法证明candidate不是echo而猜测接受。Reflection输入完全排除memory source/read result，因此固定为`COMPLETE + []`。

### 3.4 BASED_ON引用

`based_on_memory_ids`使用`memory_search/get`返回的stable product ID。Host在candidate intake与governance acceptance两个时刻都验证：

- target已经是accepted memory；
- target与candidate属于same memory domain，并满足source-scope到target-scope的closed可见性格：
  - source为USER时target必须为USER；
  - source为WORKSPACE时target可以是same-domain USER，或exact same WORKSPACE；
- target lifecycle在acceptance时为ACTIVE；
- 数量和ordinal有界；
- candidate不是引用自己或另一个candidate。

禁止：

- candidate-to-candidate BASED_ON；
- USER source引用WORKSPACE target；
- WORKSPACE source引用另一个workspace的target；
- governance根据embedding发现或替换target；
- accepted后异步backpatch；
- target drift时静默删掉relation继续接受Decision。

target在governance前漂移时，candidate终止为`ABANDONED_REFERENCE_DRIFT`。Relation一旦合法接受，target未来被supersede不会删除历史`BASED_ON`。

因此WORKSPACE `DECISION("项目脚本采用zsh")`可以引用same-domain USER `USER_PROFILE("用户使用macOS")`；反向的USER Decision不得引用WORKSPACE fact，因为该Decision随后会在其他workspace可见，而其依据不可见。

### 3.5 ToolResult产品文案

成功candidate intake返回：

~~~json
{
  "status": "proposed_for_review",
  "candidate_id": "memory-candidate:...",
  "saved_memory_id": null,
  "governance_pending": true,
  "completion_guaranteed": false
}
~~~

不得返回governance job ID，因为V1没有durable governance job。

Agent面向用户只能说“已提交记忆候选”或等价文案，不能说“已经永久记住”。

### 3.6 读工具

继续保留：

- `memory_search(query, scope?: USER | WORKSPACE, kind?: FACT | USER_PROFILE |
  RESPONSE_PREFERENCE | ACTION_RULE | DECISION, limit?)`；
- `memory_get(memory_id)`；
- `memory_explain(memory_id)`。

删除Agent-facing `max_hops`。V1不让模型选择图遍历深度。

`memory_search.scope`是产品枚举而非`ctx:*`字符串：

- absent：搜索当前Host全部visible scopes；
- USER：只搜索当前domain的`ctx:user`；
- WORKSPACE：只搜索exact current project scope；transient Host typed reject；
- 不存在“任意scope ID”或“搜索整个domain”分支。

`memory_search.query`必须非空且最大32 KiB UTF-8；`limit`默认5、范围1..50。这是tool/canonical input bound，不等于remote embedding/rerank输入一定可用；remote branch还必须通过§9的model-aware preflight，失败后退化不改变该tool query。

`scope`与`kind`是显式搜索的优先filter，而不是security boundary。Security/visibility始终由`FrozenMemoryReadScopeBinding`封闭；V1允许在exact组合结果少于`min(limit, 3)`时，按以下唯一顺序bounded放宽，最多四个unique stage：

~~~text
0  requested scope (or all visible) + requested kind (or any)
1  same scope + any kind                         # only if kind was supplied
2  all visible scopes + requested kind           # only if scope was supplied
3  all visible scopes + any kind                  # only if both were supplied
~~~

每个stage只补充此前未返回、并已通过该stage scope/kind及canonical refetch的fact；exact stage结果永远排在relaxed stage之前。达到`min(limit, 3)`即可停止继续放宽；若全部stage完成仍不足，则返回已有结果。Query normalization、tokenization和query embedding每次tool invocation只执行一次；不同stage只改变bounded SQL scope/kind predicate。Reranker最多调用一次，且只能在同一relaxation ordinal内部重排，不能把relaxed item提升到exact item之前。

ToolResult必须明确告诉模型harness已经做过什么，至少包含：

~~~json
{
  "requested_filters": {"scope": "WORKSPACE", "kind": "DECISION"},
  "exact_result_count": 1,
  "fallback_applied": true,
  "relaxed_fields": ["kind", "scope"],
  "attempted_stages": [
    {"ordinal": 0, "scope": "REQUESTED", "kind": "REQUESTED", "new_results": 1},
    {"ordinal": 1, "scope": "REQUESTED", "kind": "ANY", "new_results": 0},
    {"ordinal": 2, "scope": "ALL_VISIBLE", "kind": "REQUESTED", "new_results": 2}
  ]
}
~~~

每个returned item携带`filter_match = EXACT | KIND_RELAXED | SCOPE_RELAXED | KIND_AND_SCOPE_RELAXED`。Provider-visible值只使用`USER | WORKSPACE | ALL_VISIBLE`产品词，不泄漏domain或internal scope ID。Transient Host的`ALL_VISIBLE`仍只有USER；任何fallback都不能跨memory domain、突破Plan/permission或读取不可见workspace。这样模型不会因为exact组合结果很少而再次手工重复一组harness已经执行过的更宽查询；如果它需要真正strict查询，可只采用`filter_match=EXACT`的items。

`memory_search`返回：

- bounded active seed items；
- exact memory ID、kind、scope、statement；
- direct contradiction/supersede warning；
- retrieval channels (`SPARSE_FTS`，optional `VECTOR`、optional `RERANK`)；
- `advisory=true`与`may_be_stale_or_incomplete=true`。
- requested filter、实际fallback stage与逐item `filter_match`，不得把relaxed结果伪装成exact hit。

`memory_search`是explicit full-recall policy：它与automatic recall共用sparse/dense candidate与RRF，但只有这条路径可以将Top-20 candidate发送给configured `qwen3-rerank`。Reranker不可用、超时或失败时，工具使用RRF ordering正常成功，不得把recall降级成tool failure。

`memory_get`返回一个可见的exact item及direct relations。`memory_explain`额外返回fact source candidate terminal decision；relation若由后来的`APPLIED_TO_EXISTING` candidate创建，可返回bounded public decision disposition，但不能把它误标为fact producer。只有§2.8对相应source/decision candidate都判定为`SAME_ORIGIN`时才返回producer turn定位和bounded ToolResult citation identities，`CROSS_ORIGIN_REDACTED`只能返回redacted provenance。默认不复制原始ToolResult正文。两者必须先执行domain/scope与provenance projection，不能因调用方知道memory/candidate/ToolResult ID而绕过隔离。

Provider-visible结果只显示`scope=USER | WORKSPACE`；内部`memory_domain_id`、`ctx:workspace/<hash>`与`origin_workspace_id`均不进入普通ToolResult。

---

## 4. Candidate provenance

### 4.1 `source_entry_id`改为closed producer provenance

当前代码把发出`remember_*`tool call的assistant entry写入`source_entry_id`。这条事实只证明candidate由哪里提出，不能证明哪条用户消息在语义上支持statement。Round 8同时恢复Cheap Hint Reflection，因此producer不能再被假设为永远是一条assistant tool call。

V1明确保存：

~~~text
origin_session_id
producer_kind                  MAIN_AGENT_REMEMBER | CHEAP_HINT_REFLECTION
producer_entry_id              nullable assistant entry
producer_tool_call_id          nullable tool call
trigger_user_entry_id          nullable exact ROOT user/steer entry
producer_candidate_ordinal     nullable 0..3
~~~

closed branch为：

~~~text
MAIN_AGENT_REMEMBER
    producer_entry_id          required
    producer_tool_call_id      required
    trigger_user_entry_id      null
    producer_candidate_ordinal null

CHEAP_HINT_REFLECTION
    producer_entry_id          null
    producer_tool_call_id      null
    trigger_user_entry_id      required
    producer_candidate_ordinal required
~~~

MAIN_AGENT branch使用现有assistant tool-call composite FK：

~~~text
(origin_session_id, producer_entry_id, producer_tool_call_id)
    -> assistant_message_blocks(session_id, assistant_entry_id, tool_call_id)
~~~

Reflection branch使用existing transcript composite identity exact引用一个`USER_MESSAGE | USER_STEER` entry，并由deferred constraint验证它属于同一ROOT scope与成功完成的human-triggered turn。`producer_candidate_ordinal`只区分同一次bounded reflection输出的0..4个candidate，不是durable reflection identity或receipt。

字段命名不得继续暗示“exact causal source entry”。`trigger_user_entry_id`只证明该fallback由哪条用户输入唤醒；即使reflection模型据此提出statement，它仍不是Host对statement蕴含关系的机械证明。

### 4.2 producer provenance与semantic citation正交

~~~text
producer provenance
    谁在何处提出candidate

ToolResult citation
    candidate声称参考了哪些canonical result

BASED_ON
    accepted Decision显式依赖哪些既有accepted memory
~~~

三者不得混在一个generic subject union中。

V1不增加UserMessage citation表。Reflection branch的`trigger_user_entry_id`属于producer provenance，不是通用semantic citation。仅same-origin `memory_explain`可以定位producer turn并读取bounded transcript context，但只能描述为“candidate提出时的turn context”，不能声称某一用户句子是机械证明。跨origin workspace只能获得§2.8的redacted provenance。

### 4.3 replacement intent

`ACCEPT_AND_SUPERSEDE`需要明确replacement intent。仅有新statement与任一related item相似、更新、分类不同或冲突并不足够。

Host为governance提供：

- exact candidate；
- bounded producer turn projection；
- bounded related active memory items；
- candidate引用的ToolResult/basis public projection。

governance可以依据producer turn中“改成、以后不要、停止使用Y并使用Z”等语义选择supersede，但：

- 这仍是advisory semantic judgment，不是Host证明；
- target必须来自Host提供的allowlist；
- ordinary replacement使用`SAME_KIND_REPLACEMENT`；只有新candidate与旧target表达同一semantic atom、差异仅是旧accepted kind误分类时，governance才可显式签发`TAXONOMY_CORRECTION`；
- taxonomy correction不得顺便修改scope、statement、structured payload或把两个相关但不同的atom合并；
- target漂移时不得换绑；
- 没有明确replacement时最多contradict或accept coexist。

---

## 5. PostgreSQL逻辑模型

本轮修改clean-v0 baseline；不设计online migration、dual write或旧memory数据导入。旧数据库必须typed `RESET_REQUIRED`，migration universe fingerprint与catalog/grant fingerprint随新baseline更新。

### 5.1 目标relation集合

V1 memory产品关系建议为六张：

~~~text
memory_candidates
memory_candidate_tool_result_refs
memory_candidate_basis_refs
memory_facts                  # 或review后一次性改名memory_items
memory_relations
memory_embeddings
~~~

删除：

~~~text
memory_governance_decisions
memory_search_index
memory_vector_index
memory_index_state
~~~

独立governance decision并入candidate terminal columns；FTS并入fact自身由sealed insert trigger同步维护的ordinary column/index；vector表改成不承诺追平的optional embedding cache。

### 5.2 `memory_candidates`

至少包含：

~~~text
id
memory_domain_id
origin_workspace_id
origin_session_id
producer_kind                  MAIN_AGENT_REMEMBER | CHEAP_HINT_REFLECTION
producer_entry_id              nullable
producer_tool_call_id          nullable
trigger_user_entry_id          nullable
producer_candidate_ordinal     nullable
scope_kind                   USER | WORKSPACE
scope_id                     ctx:user | exact ctx:workspace/<hash>
kind_hint                    AUTO | FACT | USER_PROFILE | RESPONSE_PREFERENCE |
                             ACTION_RULE | DECISION
statement
applies_when                 nullable
do_not_apply_when            bounded text array / closed JSON array
candidate_acceptance_digest
model_visible_memory_provenance_disposition COMPLETE | OVERFLOW
model_visible_memory_fact_ids bounded array, max 128
status                       PENDING | PROCESSING | ACCEPTED |
                             APPLIED_TO_EXISTING | SKIPPED | ABANDONED
decision_kind                nullable closed union
final_kind                   nullable FACT | USER_PROFILE | RESPONSE_PREFERENCE |
                             ACTION_RULE | DECISION
decision_reason_code         nullable closed union
decision_public_summary      nullable bounded text
related_target_fact_id       nullable
duplicate_winner_fact_id      nullable
accepted_fact_id             nullable
applied_existing_fact_id     nullable
accepted_at                  nullable
processing_started_at        nullable
decided_at                   nullable
~~~

约束：

- `memory_domain_id`必须与origin session冻结的domain一致；
- `origin_workspace_id`必须与closed producer branch所属session workspace一致，但只作provenance；
- MAIN_AGENT与CHEAP_HINT_REFLECTION字段必须满足§4.1的exactly-one branch；reflection只能引用同一session/ROOT scope内的exact user entry；
- `model_visible_memory_fact_ids`合并产生MAIN_AGENT tool call的exact model call里可见的`MEMORY_RESPONSE_PREFERENCE_HEAD`、automatic `MEMORY_RECALL`与所有已lower的`memory_search/get/explain`结果；reflection输入不包含任何memory projection/read result，因此该branch固定`COMPLETE + []`；
- COMPLETE时ordered-deduped IDs最多128且aggregate canonical encoding不超过16 KiB；超过任一bound只保存`OVERFLOW + []`，不得静默保留prefix subset；它是anti-echo provenance，不是BASED_ON、visibility grant或generic relation；
- `scope_kind=USER -> scope_id='ctx:user'`；
- `scope_kind=WORKSPACE -> scope_id`必须通过现有`is_valid_scope()`等价的closed SQL约束，并且等于candidate intake时的exact project workspace scope；
- transient origin不得形成`scope_kind=WORKSPACE`candidate；
- `kind_hint=USER_PROFILE`只能与`scope_kind=USER`组合；AUTO/其他hint若governance最终选择USER_PROFILE，也必须在acceptance transaction重验USER scope；
- `statement`和structured fields在candidate接受后immutable；
- processing/terminal transition只能单向；
- terminal columns与status形成closed union；
- `status=ACCEPTED` iff `accepted_fact_id IS NOT NULL AND accepted_at IS NOT NULL`；
- `status=APPLIED_TO_EXISTING` iff `accepted_fact_id IS NULL`、`accepted_at IS NULL`、`applied_existing_fact_id IS NOT NULL`、`decided_at IS NOT NULL`，且decision branch为`ACCEPT_AND_SUPERSEDE | ACCEPT_AND_CONTRADICT`；它表示该candidate没有创建第二个fact，但其sealed relation intent已原子应用到exact existing ACTIVE source；
- `ACCEPTED`必须`applied_existing_fact_id IS NULL`；`PENDING/PROCESSING/SKIPPED/ABANDONED`必须同时令`accepted_fact_id`与`applied_existing_fact_id`为NULL；
- `applied_existing_fact_id`使用candidate domain/scope + fact identity composite FK；它不改变existing fact的`source_candidate_id`，也不能作为第二条fact provenance；
- `related_target_fact_id`只表示sealed supersede/contradict target；`duplicate_winner_fact_id`只用于typed duplicate terminal disposition。两者不得互相代用；APPLIED_TO_EXISTING需要前者与`applied_existing_fact_id`，plain/basis duplicate SKIP只允许`duplicate_winner_fact_id`，`SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT`则保存duplicate winner与原sealed relation target但不保存`applied_existing_fact_id`；
- `APPLIED_TO_EXISTING`保留原sealed `decision_kind=ACCEPT_AND_SUPERSEDE | ACCEPT_AND_CONTRADICT`，不能伪装成SKIP；`SKIPPED_DUPLICATE*` reason要求`status=SKIPPED`、`decision_kind=SKIP`、`duplicate_winner_fact_id`非空且所有candidate-owned relation rows为0；其他terminal reason必须令`duplicate_winner_fact_id=NULL`；
- `duplicate_winner_fact_id`与`applied_existing_fact_id`都使用candidate domain+exact scope composite FK；settlement transaction还必须逐字段重验winner的final kind/statement/structured fields与candidate prepared fact draft，FK本身不被误称为semantic equality proof；
- `decision_public_summary`不保存provider raw response、prompt或secret；
- 不保存retry count、lease generation、next attempt、receipt或repair state。

`supersede_mode`不在candidate terminal columns复制成第二真源；ACCEPT_AND_SUPERSEDE的historical mode由`decision_candidate_id`指向该candidate的immutable outgoing `SUPERSEDES` row读取。普通ACCEPTED branch的source fact由该candidate创建；APPLIED_TO_EXISTING branch的source fact由`applied_existing_fact_id`指定。Prepared acceptance与ACK confirmation仍覆盖该relation mode，缺row或mode不同均为CONFLICT。

`candidate_acceptance_digest`与fact dedupe不是同一identity。它使用domain separator `pulsara:memory-candidate-acceptance:v1`，覆盖冻结的domain/workspace/scope binding、closed producer branch、kind hint、全部normalized proposal fields、ordered ToolResult/basis canonical refs以及model-visible memory provenance disposition/IDs。MAIN_AGENT branch用于ToolResult side-branch ACK confirmation；CHEAP_HINT_REFLECTION branch用于best-effort batch insert的stable identity。它不得被拿来判断两个accepted facts语义相等。

### 5.3 `memory_candidate_tool_result_refs`

~~~text
candidate_id
origin_session_id
tool_result_id
ordinal
evidence_kind                 PRIMARY_OBSERVATION | MEMORY_READ_EXPOSURE
~~~

使用exact composite FK，unique candidate+ordinal与candidate+result。建议0..7 ordinal。`evidence_kind`由candidate intake从exact ToolResult execution binding/artifact lineage冻结，caller不能自由填写；它是governance证据分类，不是effect/permission。Relation只保存canonical identity，不复制body、preview、artifact ID或blob metadata。

### 5.4 `memory_candidate_basis_refs`

~~~text
candidate_id
memory_domain_id
source_scope_kind
source_scope_id
target_scope_kind
target_scope_id
target_fact_id
ordinal
~~~

该表只保存candidate显式提出的existing accepted memory targets。`source_scope_*`只能从locked candidate复制；`target_scope_*`只能从locked target复制。Candidate endpoint与target endpoint分别使用composite FK，普通caller不得自由填写任何scope identity。Intake与acceptance都执行§3.4的单向可见性格；它不是accepted `BASED_ON` relation，只有最终kind为DECISION且candidate被接受时，才在同一transaction将其lower为`memory_relations(BASED_ON)`。

### 5.5 `memory_facts`

至少包含：

~~~text
id
memory_domain_id
scope_kind
scope_id
source_candidate_id UNIQUE
lifecycle                    ACTIVE | SUPERSEDED
fact_kind                    FACT | USER_PROFILE | RESPONSE_PREFERENCE |
                             ACTION_RULE | DECISION
statement
applies_when                 nullable
do_not_apply_when            bounded empty-or-nonempty collection
fact_semantic_digest
accepted_at
updated_at
search_contract_id
search_contract_version
search_terms                 bounded ordered-deduped text[]
search_document              ordinary tsvector NOT NULL; sealed trigger-derived
~~~

V1删除`STALE`：当前没有closed producer、TTL或确认流程。未来若需要staleness，必须先定义产品行为，不能把index lag或时间经过自动解释成semantic stale。

`fact_payload`若保留，必须是由以上closed columns投影出的versioned carrier，不能成为任意JSON escape hatch。优先使用可由SQL CHECK验证的显式列。

`search_terms`是statement、`applies_when`与ordered `do_not_apply_when`的确定性派生值，不是Agent或governor自由填写的第二份正文。Fact acceptance transaction在insert fact前必须通过同一个pure tokenizer计算；repository同时重验token count、single-token byte bound、aggregate byte bound与contract ID/version。V1中statement与structured fields immutable，因此同一contract下`search_terms`也immutable。未来tokenizer contract升级必须显式定义reset/reindex行为，不得在query时把新tokenizer与旧terms混用。

#### 5.5.1 RESPONSE_PREFERENCE head capacity

`RESPONSE_PREFERENCE`的产品承诺是进入§2.3.1的完整head，因此不能先接受无限ACTIVE rows、再在compiler中静默截断。V1冻结：

~~~text
single ACTIVE RESPONSE_PREFERENCE statement      <= 2 KiB UTF-8
USER ACTIVE RESPONSE_PREFERENCE                  <= 16 items
USER canonical head item projection     <= 7 KiB UTF-8
each exact WORKSPACE ACTIVE RESPONSE_PREFERENCE  <= 16 items
each WORKSPACE canonical projection      <= 7 KiB UTF-8
combined provider VALUE body             <= 32 items / 16 KiB UTF-8
~~~

Item count和scope projection byte bound计算全部ACTIVE RESPONSE_PREFERENCE，包括处于`CONTRADICTS`关系中的items；只排除已经`SUPERSEDED`的row。这样冲突不能成为绕过容量的入口。`canonical head item projection`使用与provider body相同的canonical JSON item/warning codec，不按PostgreSQL `octet_length(statement)`近似猜测最终大小。

所有可能新增或退休ACTIVE RESPONSE_PREFERENCE的`ACCEPT | ACCEPT_AND_CONTRADICT | ACCEPT_AND_SUPERSEDE`事务，必须在写fact前以fixed namespace + exact`memory_domain_id/scope_kind/scope_id`取得transaction-scoped PostgreSQL advisory lock，然后在同一transaction按最终状态重算scope容量。后者包括以`TAXONOMY_CORRECTION`把RESPONSE_PREFERENCE改为另一kind，或从另一kind改成RESPONSE_PREFERENCE。§8的exact ACTIVE duplicate routing先于“新增fact”容量判断；existing source不能被重复计量成第17条。空scope也必须串行化，不能只`FOR UPDATE`已有rows而留下phantom race。该lock只保守串行化acceptance，不是durable lease、relation、generation或completion promise；hash collision最多造成额外串行，不得扩大读写scope。

`ACCEPT_AND_SUPERSEDE`按“旧target同事务转为SUPERSEDED之后”的最终集合计量，因此可以原子释放并复用一个slot。没有明确replacement intent时不得为腾容量自动supersede、merge或摘要旧RESPONSE_PREFERENCE。Provider决定接受但final locked scope已经超界时，同一transaction不得插入partial fact，而应把candidate终结为：

~~~text
status               SKIPPED
decision_kind        SKIP
decision_reason_code RESPONSE_PREFERENCE_CAPACITY_EXCEEDED
accepted_fact_id     NULL
~~~

`PreparedMemoryGovernanceAcceptance`必须把“exact acceptance或该closed capacity settlement”冻结为唯一compatible winner union；commit ACK unknown分别按完整fact/relations或exact SKIP terminal columns确认，不能重跑governance provider。不得把超界candidate自动改成`FACT`，也不得接受后只让它留在query recall中——这两种行为都会改变已冻结的RESPONSE_PREFERENCE语义。

Scope约束与candidate相同，并以SQL CHECK/constraint trigger证明`fact_kind=USER_PROFILE -> scope_kind=USER AND scope_id='ctx:user'`。建议为读侧与relation endpoint建立exact composite identity：

~~~text
(memory_domain_id, scope_kind, scope_id, id)
~~~

另建立`UNIQUE (memory_domain_id, id)`，它只为不复制scope列的`memory_embeddings(memory_domain_id, fact_id)`提供可引用candidate key；它不得被解释成省略scope过滤的读入口。所有产品读取仍必须使用完整visible-scope predicate。

USER与WORKSPACE都必须走同一composite key；不得因为USER是“全局”而省略`memory_domain_id`。

Candidate与fact必须形成可由PostgreSQL在commit时证明的双向一对一关系，不能只靠fact侧`source_candidate_id UNIQUE`。clean-v0冻结以下等价物理约束：

~~~text
memory_candidates
    UNIQUE (id, accepted_fact_id)
    FOREIGN KEY (id, accepted_fact_id)
        -> memory_facts(source_candidate_id, id)
        DEFERRABLE INITIALLY DEFERRED

memory_facts
    UNIQUE (source_candidate_id, id)
    FOREIGN KEY (source_candidate_id, id)
        -> memory_candidates(id, accepted_fact_id)
        DEFERRABLE INITIALLY DEFERRED
~~~

配合candidate status CHECK，这同时证明：

- 每个ACCEPTED candidate在同一transaction结束时有exact fact；
- 每个fact的source candidate已是ACCEPTED且反向指向自己；
- APPLIED_TO_EXISTING candidate不能成为任何fact的`source_candidate_id`；其`applied_existing_fact_id`只是same-scope existing source reference，是否真正应用由§5.6的candidate↔relation deferred invariant证明；
- SKIPPED/ABANDONED candidate不能被fact或candidate-owned relation引用；
- repository可在一个transaction内先insert fact再update candidate，不要求插入顺序。

若PostgreSQL对目标组合约束的具体实现需要deferred constraint trigger，可以使用等价trigger，但不得把任一方向降成application assertion。

`fact_semantic_digest`使用domain separator `pulsara:memory-fact-semantic:v1`与唯一canonical JSON encoding，只覆盖：

~~~text
final fact_kind
normalized statement
normalized applies_when presence/value
ordered normalized do_not_apply_when values
~~~

它不覆盖candidate ID、producer、citation、decision摘要、accepted time、lifecycle或scope；exact scope由unique index的前置列表达。建立：

~~~text
UNIQUE (
    memory_domain_id,
    scope_kind,
    scope_id,
    fact_semantic_digest
)
WHERE lifecycle = 'ACTIVE'
~~~

这是exact duplicate的最终并发winner。它不做近似dedupe，也不把相似、contradict或不同结构字段压成同一fact。

### 5.6 `memory_relations`

closed kind：

~~~text
BASED_ON
SUPERSEDES
CONTRADICTS
~~~

至少包含：

~~~text
memory_domain_id
decision_candidate_id
source_scope_kind
source_scope_id
source_fact_id
relation_kind
target_scope_kind
target_scope_id
target_fact_id
supersede_mode         nullable；仅SUPERSEDES使用
ordinal                 nullable；仅BASED_ON使用
created_at
~~~

约束：

- 两端same memory domain；source与target各自用包含domain+scope+fact ID的composite FK物理约束，不能用一组共享scope列掩盖可见性方向；
- `decision_candidate_id` NOT NULL并以same-domain composite FK指向exact governance candidate；它记录“哪次sealed candidate decision创建了这条relation”，不是source fact的producer provenance、delivery receipt、retry token或completion promise；
- source != target；
- relation row immutable；
- `BASED_ON`: source kind DECISION，target在acceptance时ACTIVE；USER source只能指向same-domain USER target，WORKSPACE source可以指向same-domain USER target或exact same WORKSPACE target；target可为任一accepted kind；
- `SUPERSEDES`: source/target必须same exact scope，target在acceptance时ACTIVE并于同一事务转为SUPERSEDED；`supersede_mode` closed为`SAME_KIND_REPLACEMENT | TAXONOMY_CORRECTION`；
  - `SAME_KIND_REPLACEMENT`要求source/target同kind；
  - `TAXONOMY_CORRECTION`要求source/target不同kind，且只能来自governor明确判断“同一semantic atom此前被错误分类”的branch；它不能改scope、statement或structured payload；
- `CONTRADICTS`: source/target必须同kind、同exact scope，五个kind均允许且两端保持ACTIVE；
- `CONTRADICTS`使用unordered pair唯一约束，只存一行；
- 每个ACCEPTED或APPLIED_TO_EXISTING candidate最多一个supersede或contradict target；
- `BASED_ON`最多8条，ordinal由candidate refs决定。

Deferred invariant必须冻结relation decision provenance的closed union：

~~~text
candidate.status = ACCEPTED
    relation.source_fact.source_candidate_id == decision_candidate_id
    candidate.accepted_fact_id == relation.source_fact_id

candidate.status = APPLIED_TO_EXISTING
    relation.relation_kind in {SUPERSEDES, CONTRADICTS}
    candidate.applied_existing_fact_id == relation.source_fact_id
    candidate.accepted_fact_id IS NULL
~~~

其他candidate status不能被`decision_candidate_id`引用。APPLIED_TO_EXISTING每个candidate只允许exact一条relation；普通ACCEPTED DECISION仍可由同一candidate创建0..8条BASED_ON。`decision_candidate_id`不进入relation semantic uniqueness：同一source/kind/target/mode已经存在时，新candidate不得插入第二行或抢写归因。Relation的provider/public projection不暴露`decision_candidate_id`；`memory_explain`读取其governance provenance时重新执行candidate origin-workspace redaction。

Runtime不能把provider给出的relation verb直接翻译成未校验SQL。Governance负责对producer turn/candidate语义作bounded replacement/contradiction/taxonomy-correction judgment；Prepared acceptance冻结该closed decision、source/target exact identity、kind、scope、`supersede_mode`与expected lifecycle。Repository在同一transaction锁定两端并机械重验scope visibility、mode/kind矩阵、target ACTIVE、prepared identity和branch compatibility。Runtime不自行用embedding或字符串规则“证明”语义，但也不能在target drift时换绑或把普通ACCEPT升级为relation。`SUPERSEDES`必须来自明确replacement judgment；相似度、时间更晚或容量压力都不足以构成replacement。`TAXONOMY_CORRECTION`只修正已接受atom的kind，不是CORRECT/MERGE、scope migration或statement rewrite。`CONTRADICTS`只表示两个same-kind atom不能同时成立，不自动选择winner，也不能作为变相merge。

若需要在relation row复制endpoint kind以建立composite FK + CHECK，复制值只能由repository从locked endpoint读取；普通caller不得传自由kind。clean-v0 CHECK/constraint trigger必须证明上述closed matrix，包括`supersede_mode`的required/forbidden presence、cross-kind correction与BASED_ON单向scope lattice；这不是governance prompt独占的约定。

Relation read同样是visibility operation。返回outgoing或incoming edge前，source与target两个endpoint都必须分别通过当前`FrozenMemoryReadScopeBinding`；例如Project B读取一个USER target时，不得因此发现Project A WORKSPACE DECISION指向它。不可见另一端时省略整条edge，不返回redacted existence hint。

为此至少建立outgoing `(memory_domain_id, source_scope_kind, source_scope_id, source_fact_id, relation_kind)` 与incoming `(memory_domain_id, target_scope_kind, target_scope_id, target_fact_id, relation_kind)` 索引；query必须先带当前visible endpoint predicates再读取bounded direct edges，不能先按fact ID跨scope拉全量后在Python过滤。

典型taxonomy correction：旧ACTIVE row把“用户喜欢川菜”误存为USER `RESPONSE_PREFERENCE`；之后同scope candidate以相同语义提出`USER_PROFILE`。Governor可以签发`TAXONOMY_CORRECTION`，同一transaction插入新profile、写`SUPERSEDES(mode=TAXONOMY_CORRECTION)`并退休旧row。若正确USER_PROFILE已由另一candidate成为ACTIVE winner，则§8.5不再插入第二fact，而以该winner为source原子补上同一SUPERSEDES并把当前candidate终结为APPLIED_TO_EXISTING。下一eligible trigger的response-preference head因此移除旧项；这不是删除历史、改写statement或建立新的correction relation。

### 5.7 `memory_embeddings`

~~~text
memory_domain_id
fact_id
fact_semantic_digest
embedding_contract_id
embedding_contract_version
embedding vector(1024)
embedded_at
~~~

查询只使用同时满足以下条件的row：

- fact仍ACTIVE；
- stored fact digest等于当前fact digest；
- contract ID/version等于当前embedding配置。

Primary identity为`(memory_domain_id, fact_id)`；FK exact引用`memory_facts UNIQUE(memory_domain_id, id)`。HNSW使用`vector_cosine_ops`。Scope/kind/lifecycle不复制进embedding row，查询与最终refetch都必须join `memory_facts`并借用同一个`FrozenMemoryReadScopeBinding`执行可见性过滤；仅凭embedding PK读取正文是禁止路径。

Dense KNN的物理排序表达式冻结为bare cosine distance ASC：

~~~sql
ORDER BY memory_embeddings.embedding <=> $query_vector::vector ASC
LIMIT $bounded_overfetch
~~~

不得写成`ORDER BY 1 - distance DESC`，也不得在HNSW inner node中先加入fact-ID secondary sort。bounded KNN candidate物化后，外层才按`distance ASC, fact_id ASC`稳定排序并裁成policy Top-K。分数只用于当次rank，不存储usage/trace。

Embedding input必须是与§9.1相同的closed retrieval text projection，不包含candidate provenance、scope ID、accepted time、relation或diagnostic。`fact_semantic_digest`与embedding contract共同证明vector对应的exact语义载体；不另外保存provider request、raw response或rerank score。

不满足即视为missing。不得建立desired/applied generation、refresh debt、Runtime degraded或durable repair job。

---

## 6. Reliable candidate intake

### 6.1 为什么保留这一条可靠边界

Memory整体是弱完整性数据，但ToolResult必须诚实：

~~~text
ToolResult says proposed
    => exact candidate exists
~~~

因此复用现有prepared tool-result acceptance side branch，而不是引入第二transaction或Host-local-only queue。

### 6.2 Prepared candidate

process-local frozen DTO至少覆盖：

~~~text
PreparedMemoryCandidateAcceptance
    candidate identity
    workspace/domain/scope binding
    producer kind = MAIN_AGENT_REMEMBER
    producer assistant entry + tool call
    exact model-call identity
    PreparedProviderInputCut identity/fingerprint
    ModelVisibleToolCitationSnapshot identity/fingerprint
    ModelVisibleMemoryProvenanceSnapshot identity/fingerprint
        COMPLETE + ordered visible fact IDs | OVERFLOW
    normalized closed proposal
    resolved ToolResult refs in exact order
    resolved existing memory refs in exact order
    candidate_acceptance_digest
    ToolResult acceptance candidate identity
~~~

候选、citation rows、ToolResult transcript entry、tool_results row与`ToolResultAccepted`仍使用当前tool result transaction原子接受。Memory candidate本身不新增committed event。

`ModelVisibleToolCitationSnapshot`只作为prepared candidate的process-local证明输入；数据库最终只保存已解析的canonical ToolResult refs，不保存snapshot、handle或model-call capability。

ACK unknown必须通过candidate ID、`candidate_acceptance_digest`、producer identity、reference rows与ToolResult winner做stateless exact confirmation。NONE只重试同一个frozen candidate；CONFLICT中断turn，绝不生成新candidate ID或删除citation。

### 6.3 不允许的弱化

禁止：

- 先返回`proposed`再异步尝试INSERT candidate；
- candidate成功但ToolResult失败；
- ToolResult成功但citation rows部分缺失；
- commit ACK unknown后重新生成UUID；
- candidate intake失败时把引用静默删掉并接受无citation版本。

弱一致从governance processing开始，不从产品ToolResult诚实性开始。

### 6.4 Cheap Hint Reflection：唯一的automatic candidate fallback

Round 8必须恢复Cheap Hint Reflection，但只恢复“廉价规则唤醒一次辅助复核”的产品能力，不恢复旧reflection event、history、projection ledger、turn/tool/token counter或durable extraction job。

`CheapMemoryHintSetV1`是pure、sealed、versioned matcher。它至少继承历史中已验证的中英signal族，但删除裸否定、裸偏好词和泛化指令词，避免普通命令触发review：

~~~text
记住 / 别忘了 / 从现在开始 / 以后都 / 不要再 / 我的意思是
我更喜欢 / 我喜欢 / 我不喜欢 / 我通常 / 我习惯
我们决定 / 已经决定 / 决定采用
remember / don't forget / from now on / going forward
I prefer / I like / I usually / never / stop doing / what I meant was
~~~

Exact signal fixture必须从`5b7ad9f7:src/pulsara_agent/memory/reflection/engine.py`盘点后sealed落盘；不得只复制上面摘要。

Round 8冻结两个职责不重叠、Python类型名不携带version的pure matcher；各自内部`contract_id`继续版本化：

- `MemoryWriteOptOut`只匹配明确禁止保存当前entry的command-like结构，并要求明确当前内容或memory宾语，例如`don't save this message`、`please don't remember what I just said`、`don't add this to memory`、`不要记住这条消息`、`别把这件事写入记忆`。`I don't remember...`、`don't save files...`、`不要记录日志`、`不要保存这个文件`必须不命中；
- `TurnMemoryUseOptOut`只匹配带current run/answer限定或明确saved/agent-memory对象的禁用，例如`don't use saved memory for this answer`、`answer without using memory`、`本轮不使用记忆`、`这次不要参考历史记忆`、`不用记忆回答`。技术语义`memory mapping`与中文`内存`必须不命中。

正向`don't forget`、`不要忘记`、`别忘了`进入两个matcher的explicit negative fixture，必须保持可触发hint。两个gate都执行NFC/casefold/whitespace normalization，但不访问provider、repository或memory。

`MemoryUsePolicy`是每个真实ROOT user run的frozen closed policy：

~~~text
ENABLED
WRITE_DISABLED_BY_USER
ALL_DISABLED_BY_USER
~~~

每个真实ROOT `USER_MESSAGE`开启新policy epoch并从`ENABLED`重新计算。一个ordered steer batch逐entry计算后按`ALL_DISABLED > WRITE_DISABLED > ENABLED`聚合；`USER_STEER`、tool loop及Plan/Terminal automatic continuation只能继承或加强，不能在同一run内隐式恢复。Policy作为exact model-call memory context贯穿provider execution、authorize与invoke，不改变advertised tool surface。下一条真实ROOT `USER_MESSAGE`才允许重置。

Matcher只处理未被opt-out的entry，执行NFC、casefold和whitespace normalization；signal按长度降序，normalized空间中的overlap只保留最长match。输出只包含bounded `signal_code`与最多2048 normalized code points的`normalized_excerpt`/digest，不承诺original span、byte offset或原文excerpt。Normalized offset只供pure matcher本次切片使用，不进入stable identity，因而无需伪造`ß -> ss`、NFC组合或whitespace collapse到原文坐标的映射。Matcher不判断statement、kind、scope或是否值得长期保留，也不访问provider、repository或memory。

#### 6.4.1 唯一admission条件

只有同时满足以下条件才允许一次review：

1. exact ROOT human-triggered turn以`COMPLETED`成功结束；USER_STOPPED、FAILED、INTERRUPTED、HOST_CLOSING、resource boundary与未完成Plan interaction均不触发；
2. 该turn内没有任何entry命中`TurnMemoryUseOptOut`；命中即整次review call/data egress为0；否则逐entry执行`MemoryWriteOptOut`，被明确禁止持久化的entry在matcher及任何auxiliary data egress前移除；剩余exact `USER_MESSAGE | USER_STEER`至少一条命中V1 hint set；多steer按canonical entry sequence收集，最多8条entry/16个non-overlap hint；全部被移除时call count=0；
3. 该human turn没有成功提交`remember` candidate；schema/permission/user-policy denial不把一个未产生candidate的请求冒充成功写入，也不能阻止其他eligible、未opt-out entry的reflection；
4. 该run冻结的permission snapshot允许`memory_write`且没有Plan read-only overlay；permission denial绝不能被reflection绕过；
5. 同一exact human-triggered turn此前没有安装或完成过reflection attempt。

Turn terminalization FULL后，runner只返回immutable `PreparedCheapHintReflectionHandoff`，覆盖exact turn、eligible entry/hint identities、permission snapshot与provider-trust-domain fact；它不调用provider、不等待auxiliary lane。ROOT wrapper在Host lock内先把handoff作为DORMANT非阻塞交给memory owner（owner满/关闭可直接丢弃），再结算并释放exact ROOT active slot，最后将已adopt handoff标记RUNNABLE。Memory owner只有看到RUNNABLE后才可安装`CheapHintReflectionAttempt`并等待auxiliary lane；因此下一条prompt/queue delivery不等待最多120秒的reflection。

Wake、handoff、attempt、provider output和candidate batch都可丢失；Host重启不扫描历史turn补做。Waiter cancellation只detach foreground result，不接管已adopt handoff；Host close停止handoff admission、丢弃DORMANT、cancel RUNNABLE/IN_FLIGHT并physical join exact auxiliary call。一个turn最多一个auxiliary call，tool/provider retry和automatic Plan continuation不另行触发。该handoff是process-local ownership transfer，不是durable receipt、queue或job。

#### 6.4.2 Auxiliary review输入与输出

复用§7.1.1 purpose-neutral `AuxiliaryJsonModelPort`，但purpose为`MEMORY_HINT_REVIEW`：无tools、无continuity、finite total 120s、provider input最大64 KiB、output最大8 KiB。输入只含：

- 最多8条eligible ROOT user entry的bounded public projection，每条带call-local `user:1..8` handle、matched signal code与normalized excerpt；entry <=16 KiB时可使用exact text，超界时使用UTF-8-safe head/tail加hint excerpt，不宣称完整；opt-out entry绝不进入packet；
- 每条entry之前紧邻的assistant semantic text和该turn最终assistant semantic text的bounded public projection，用于理解“不是这个意思”等纠正语义；
- current Host可写产品scope `USER | optional WORKSPACE`及稳定scope说明；
- closed candidate schema与“hint可能是假阳性；没有长期价值就返回空”instruction。

输入明确排除`MEMORY_RESPONSE_PREFERENCE_HEAD`、`MEMORY_RECALL` projection、memory_search结果、producer transcript之外的全session历史、ToolResult/arguments/artifact、MCP、path/env/secret、permission细节与internal IDs。V1 reflection不得从tool output提取事实；需要ToolResult citation的场景由主模型`remember`拥有。

输出是0..4个candidate proposal，每个必须引用exact一个`user:N` source handle，并满足普通statement/kind/scope/structured-field bound；statement与结构字段必须由该user entry自身支持，adjacent assistant text只能用于理解纠正语义，不能成为candidate来源；`based_on_memory_ids`与`cited_tool_result_handles`固定为空。Host解析handle为canonical `trigger_user_entry_id`，绑定exact domain/workspace/scope并生成stable candidate ID/digest。Provider不能返回database ID、raw scope ID、authority、confidence或“直接accept”指令。

候选以一个fresh、bounded Host-writer transaction best-effort插入PENDING rows；没有ToolResult、committed event或durable wake。写前冻结`PreparedCheapHintReflectionCandidateBatch`，覆盖exact turn、ordered user-entry/hint identities、model output digest、candidate IDs/ordinals与scope binding。Commit ACK unknown可以用stable IDs/digests作stateless exact query，但waiter cancel、Host close或deadline耗尽允许不确认；绝不重跑auxiliary model或生成另一组candidate。任一candidate invalid时whole batch拒绝，避免模型输出被部分解释成另一套语义。

这些candidate随后进入与MAIN_AGENT相同的origin-workspace governor、dedupe、scope和acceptance contract。Reflection只补足“主模型漏调remember”的产品缺口，不获得更强的governance或durability。

---

## 7. Best-effort governance

### 7.1 Host-local owner

新增一个Host-scoped、process-local `AdvisoryMemoryGovernor`：

- 只持有repository read/write port、model port与可选relatedness provider；
- 一个Host内最多一个governance provider call；
- candidate commit只发送可丢失wake hint；
- Host open与低频maintenance可bounded扫描PENDING rows；
- close停止新claim、cancel当前provider call并bounded join；
- 不创建durable job、attempt、lease、receipt、checkpoint或repair state。

#### 7.1.1 Purpose-neutral auxiliary JSON model port

当前`DirectKernelJobModel`已经提供“单次、无tools、finite-total、JSON object”的物理能力，但其名称和validation把它错误绑定到durable job owner。Round 8必须先抽出purpose-neutral leaf：

~~~text
AuxiliaryJsonModelPort
DirectKernelAuxiliaryJsonModel
PreparedAuxiliaryJsonModelCall
~~~

`DirectKernelJobModel`若仍由剩余`BACKGROUND_COMPACTION`使用，只能成为该leaf的窄adapter；`AdvisoryMemoryGovernor`直接依赖`AuxiliaryJsonModelPort`，不得导入durable job executor、job attempt或job claim。

同一Host的`MEMORY_GOVERNANCE`与`MEMORY_HINT_REVIEW`共享一个process-local auxiliary-model admission lane，最多一个physical provider call；hint review等待lane时可被丢弃，不能阻塞foreground或抢占正在治理的candidate。两种purpose各自使用closed request/response schema，不能把reflection output交给governance parser。

Memory governance attempt冻结：

~~~text
purpose                         MEMORY_GOVERNANCE
tool surface                    empty
provider-input continuity       none
conversation runner/live stream none
maximum input                   128 KiB UTF-8 + target estimator
maximum output                  8 KiB UTF-8
attempt total                   300 seconds
~~~

closed watchdog owner新增：

~~~text
MEMORY_GOVERNANCE_ATTEMPT       300 seconds absolute attempt
MEMORY_HINT_REVIEW_ATTEMPT      120 seconds absolute attempt
MEMORY_GOVERNOR_CLOSE           120 seconds standalone disable/replacement join
~~~

`MEMORY_GOVERNANCE_ATTEMPT`从candidate claim FULL之后、任何relatedness读取或remote query embedding之前签发，依次覆盖bounded sparse/dense relatedness、packet assembly、auxiliary JSON model call以及acceptance/confirmation settlement；不能在relatedness之后重新取得一个完整300秒。`MEMORY_HINT_REVIEW_ATTEMPT`从Host决定安装exact turn attempt开始，覆盖packet assembly、auxiliary call、parse与best-effort candidate batch insert/confirmation。每个子operation取得owner remaining budget与自身物理上限的较小值。Dense query embedding失败或没有足够remaining budget时，governance relatedness退化为sparse-only；若最终model/settlement没有剩余budget，candidate允许保持`PROCESSING`。

transport connect/write/pool/read-idle分别取现有对应物理上界与remaining governance total的较小值，且`total_seconds=remaining governance total`。不能复用foreground无total transport，也不能借用durable job attempt identity。该attempt是process-local watchdog，不写入candidate或数据库。

provider waiter取消只请求cancel并等待exact physical call退出。独立关闭/替换governor时使用`MEMORY_GOVERNOR_CLOSE`；Host session close时不得重新签发该120秒，而是停止新claim、cancel当前call并使用既有single `HOST_SESSION_CLOSE` absolute deadline在同一Host-owned close task中physical join。logical close deadline耗尽可以向waiter返回诊断，但不能detach仍持有transport的task。失败后candidate可以永久停留`PROCESSING`，不得因此生成retry owner。

### 7.2 Claim语义

claim transaction：

~~~text
SELECT bounded oldest PENDING
    WHERE memory_domain_id = current Host domain
      AND origin_workspace_id = current Host workspace_id
    FOR UPDATE SKIP LOCKED
UPDATE exact row -> PROCESSING
RETURN frozen candidate + refs
commit
~~~

V1每次只claim一个candidate，避免恢复batch coverage、partial decision与recovery contract。若未来以eval证明batch价值，可另行规格化。

PENDING candidate只能由exact `origin_workspace_id`的Host claim。project A提出的USER candidate在成功接受前仍是A的private provenance，project B或transient Host都不得读取、claim或送入governance provider；成功接受为USER fact后，fact正文才按domain-global scope跨workspace可见。transient Host可以claim自己提出的USER candidate，仍不能形成WORKSPACE candidate。

Claim transaction必须同时验证candidate的`origin_session_id + origin_workspace_id + memory_domain_id`仍与session composite identity一致，并且frozen scope是该origin workspace可写的合法scope。Claim只冻结原binding，不能按当前Host重新绑定、提升或缩小scope。

这条origin fence有意牺牲completion：origin Host永不重开时，PENDING candidate可以永久不被治理。不得为跨workspace代办恢复handoff、lease、durable job或provenance解密通道。

`PROCESSING`没有lease、expiry或automatic requeue。Host在provider call中崩溃时，该candidate可以永久停留在PROCESSING；这正是本文接受的weak completion边界。

### 7.3 Governance输入

由于§7.2已经证明governor与candidate同origin workspace，bounded input才允许包含：

- exact stored candidate；
- producer turn的bounded public projection；
- cited ToolResult的bounded canonical preview与observation timing；
- exact prebound `based_on` items；
- MAIN_AGENT producer call的`ModelVisibleMemoryProvenanceSnapshot`：COMPLETE时scope-safe refetch并投影全部exposure IDs；OVERFLOW只传closed overflow reason；
- optional exact ACTIVE semantic winner，作为`existing_source`单独投影，不进入relation target allowlist；
- same exact scope top-N all-kind related active targets，排除existing source并携带exact kind；cross-kind target只可用于`TAXONOMY_CORRECTION`；
- closed decision instruction。

MAIN_AGENT的producer turn由assistant entry确定；CHEAP_HINT_REFLECTION的producer turn由exact `trigger_user_entry_id`确定。两条branch使用同一个bounded public projection规则，不得为reflection回扫全session或把auxiliary model raw response当成producer transcript。

建议上界：

| 项 | 上界 |
|---|---:|
| producer turn projection | 32 KiB UTF-8 |
| cited ToolResult aggregate preview | 64 KiB UTF-8 |
| all model-visible memory projection | 128 items / 64 KiB UTF-8 aggregate；超界直接SKIP，不截断subset |
| exact existing source | 0..1 item / 4 KiB UTF-8 |
| related memories | 8 items / 32 KiB UTF-8 aggregate |
| governance total provider input | 128 KiB UTF-8并受target token estimator再次约束 |
| governance output | 8 KiB UTF-8 |

128 KiB是最终encoded auxiliary context的总上界，包含closed instruction、candidate和所有projection，而不是各项上界之和。Candidate proposal、decision schema以及COMPLETE model-visible memory的全部statement/kind/scope投影为MUST_KEEP；后者自身超过64 KiB或与其他MUST_KEEP无法容纳时直接`SKIP(MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW)`，不能只挑“最相关8条”制造anti-echo盲区。其余超界按`related memories -> producer turn detail -> cited preview body`顺序确定性缩减，citation identity仍保留。Candidate/instruction本身无法容纳时，在provider open前将candidate best-effort终结为resource-bound ABANDONED；不得截断statement后继续治理。

不读取artifact/blob全文，不注入private URL、environment、tool arguments或secret。Relatedness candidate不进入accepted relation，除非governance从Host allowlist选择exact supersede/contradict target。

同domain USER related memory可能来自另一个workspace；其provider projection必须先执行§2.8，只携带accepted fact的public statement/kind/lifecycle与必要relation摘要，不携带另一个workspace的candidate、producer turn或ToolResult provenance。

governance input builder仍必须把current Host workspace与candidate origin做exact join；不能把“claim已经做过检查”当成第二次provider open前可以省略的假设。任何不一致都在provider open前fail closed。

Anti-echo规则是governance decision contract的一部分：

- provenance为OVERFLOW时必须`SKIP(MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW)`；
- candidate只是对任一model-visible memory statement的原样或语义等价复述，且producer turn没有与candidate语义相关的新human assertion、correction或`PRIMARY_OBSERVATION` ToolResult evidence时，必须`SKIP(RECALLED_MEMORY_ECHO)`；
- `MEMORY_READ_EXPOSURE` ToolResult及其artifact_read后代只能证明模型看过memory，永远不能解除echo拦截；
- 仅仅附带一个无关的primary ToolResult也不能解除拦截；governance必须判断新assertion/evidence确实支持candidate statement或其structured fields。

即使candidate closed shape已经唯一决定final kind且Host观察到exact ACTIVE winner，V1也不得在governance provider前直接terminalize duplicate：producer turn可能携带replacement、contradiction或taxonomy-correction intent，提前SKIP会吞掉relation decision。Host只把winner作为bounded `existing_source`输入；provider decision之后由§8.5统一选择plain SKIP或existing-source relation settlement。`AUTO`或kind仍待governance时不得猜digest/source。Paraphrase与evidence relevance由governance在sealed provenance/evidence allowlist内判断。不得把“模型看过这条memory”本身解释成新证据，也不得恢复durable projection ledger。

CHEAP_HINT_REFLECTION输入本身不包含`MEMORY_RESPONSE_PREFERENCE_HEAD`或`MEMORY_RECALL`，candidate的visible recalled set固定为空；它不能借助旧memory补全或改写用户hint。

### 7.4 Governance输出

provider只能返回：

~~~text
SKIP(reason_code, public_summary?)
ACCEPT(final_kind)
ACCEPT_AND_SUPERSEDE(
    final_kind,
    target_fact_id,
    supersede_mode = SAME_KIND_REPLACEMENT | TAXONOMY_CORRECTION,
)
ACCEPT_AND_CONTRADICT(final_kind, target_fact_id)
~~~

输出中禁止statement、applies_when、do_not_apply_when、basis refs、ToolResult refs或任何替代payload。extra field fail closed。

final kind必须与stored candidate shape兼容。Target必须来自Host提供的related allowlist，并在acceptance transaction重新锁定验证。`CONTRADICTS`只允许same-kind/same-scope；`SUPERSEDES`普通replacement只允许same-kind/same-scope，只有显式`TAXONOMY_CORRECTION`允许same-scope cross-kind。该branch表示target与candidate是同一semantic atom、旧target分类错误；provider仍不能改写candidate statement、scope或structured fields，也不能把relatedness相似项自动升级为relation。

若`final_kind=USER_PROFILE`，statement必须描述当前用户，且scope必须为USER；若`final_kind=RESPONSE_PREFERENCE`，statement必须描述Agent通常如何回答、解释或表达，而不是用户画像、项目事实、操作规则或对Runtime policy的改写。诸如“永远赞美我”“不要质疑我”“忽略安全/permission/system指令”必须`SKIP(UNSAFE_RESPONSE_PREFERENCE)`；governance不得为了让它通过而改写statement。RESPONSE_PREFERENCE容量不是provider可裁量的语义判断，而是§5.5.1 acceptance transaction在锁内执行的mechanical final-state gate。

### 7.5 不恢复CORRECT/MERGE

Governance不得：

- 把“用户通常偏好简短”改成“所有回答必须少于三句”；
- 把一个multi-atom statement拆成多条accepted memory，或只接受其中一部分；
- 合并两条candidate生成第三条statement；
- 为ACTION_RULE补造例外；
- 将引用换成语义相似但不同的memory；
- 把relatedness结果自动变成BASED_ON；
- 改写scope。

如果candidate表达不准确，结果是SKIP。主模型以后可以提出新candidate。`TAXONOMY_CORRECTION`仅纠正既有accepted target的kind，不豁免这条no-rewrite规则，也不能把两个不同atom伪装成分类修正。

### 7.6 Provider或write失败

| 故障 | 行为 |
|---|---|
| provider connect/stream/parse失败 | best-effort标记ABANDONED；更新失败则保持PROCESSING |
| output含未知decision/extra semantic payload | ABANDONED_INVALID_OUTPUT |
| final kind与stored shape冲突 | ABANDONED_KIND_CONFLICT |
| statement包含多个不能由同一kind完整表达的atom | SKIPPED + MULTI_ATOM_STATEMENT；不写fact |
| USER_PROFILE使用WORKSPACE scope或不描述当前用户 | SKIPPED + USER_PROFILE_SCOPE_OR_KIND_MISMATCH；不写fact |
| RESPONSE_PREFERENCE语义属于unsafe/core-behavior override | SKIPPED + UNSAFE_RESPONSE_PREFERENCE；不写fact |
| RESPONSE_PREFERENCE locked final scope超过容量 | SKIPPED + RESPONSE_PREFERENCE_CAPACITY_EXCEEDED；不写fact |
| prebound basis漂移 | ABANDONED_REFERENCE_DRIFT |
| CONTRADICTS跨kind/scope，SUPERSEDES的mode/kind/scope矩阵冲突，或prepared branch不兼容 | ABANDONED_RELATION_CONTRACT_CONFLICT；不写fact/relation |
| supersede/contradict target漂移 | whole acceptance rollback；best-effort ABANDONED_TARGET_DRIFT |
| relation branch插入新fact命中exact ACTIVE duplicate | 不重跑provider；SUPERSEDES/CONTRADICTS进入prepared existing-source settlement |
| duplicate Decision携带不同BASED_ON set | SKIPPED_DUPLICATE_BASIS_UNAPPLIED；0 post-hoc relation write |
| existing source relation已由另一candidate exact存在 | SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT；不覆盖decision provenance |
| existing source/target在settlement前漂移 | exact relation/full winner优先confirm；否则重试原acceptance或ABANDONED_TARGET_DRIFT，绝不换绑 |
| canonical acceptance commit ACK unknown | stateless exact query；不得重跑provider来生成新decision |
| Host close/cancel | candidate可以保持PROCESSING；不handoff |

不自动重试governance provider。用户可再次调用`remember`提出新的candidate。

### 7.7 Stable governance acceptance candidate与ACK unknown

Provider decision通过closed validator后、任何canonical write前，governor必须冻结：

~~~text
PreparedMemoryGovernanceAcceptance
    candidate_id
    candidate_acceptance_digest
    expected candidate status/head          PROCESSING + immutable proposal identity
    memory domain/origin workspace/source exact scope
    governance decision + bounded public metadata
    compatible settlement branch              ACCEPTANCE |
                                               RESPONSE_PREFERENCE_CAPACITY_SKIP |
                                               EXACT_DUPLICATE_SKIP |
                                               EXISTING_SOURCE_RELATION
    prepared_fact_id                        nullable for SKIP
    fact_semantic_digest                    nullable for SKIP
    complete immutable fact draft           nullable for SKIP
    ordered immutable relation drafts including endpoint scopes + supersede mode
    exact target fact identities + expected scopes/kinds/lifecycles
    candidate terminal-column draft
    candidate_fingerprint
~~~

`candidate_fingerprint`使用domain-separated canonical encoding覆盖decision branch、final kind、source/target endpoint scope、target identity/lifecycle、`supersede_mode`、完整relation drafts与candidate terminal draft。ACK-unknown confirmation不得把`SAME_KIND_REPLACEMENT`与`TAXONOMY_CORRECTION`，或WORKSPACE→USER与WORKSPACE→WORKSPACE basis winner视为同一candidate。

`EXISTING_SOURCE_RELATION`不是provider输出时就能猜出的source ID。Original acceptance因ACTIVE unique winner回滚后，Host先执行fresh bounded read并冻结第二层process-local carrier：

~~~text
PreparedExistingSourceRelationSettlement
    parent PreparedMemoryGovernanceAcceptance fingerprint
    candidate_id + expected exact PROCESSING head
    existing source fact exact identity/scope/kind/semantic digest/immutable fields
    expected source lifecycle                 ACTIVE
    exact target identity/scope/kind/lifecycle
    exact SUPERSEDES(mode) | CONTRADICTS relation draft
        decision_candidate_id = candidate_id
    settlement disposition                    APPLY_NEW_RELATION |
                                               CONFIRM_EXISTING_RELATION
    APPLY_NEW_RELATION:
        expected target lifecycle transition
        candidate APPLIED_TO_EXISTING terminal draft
    CONFIRM_EXISTING_RELATION:
        exact existing relation composite identity
        exact existing relation decision_candidate_id
        exact already-applied target lifecycle
        candidate SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT terminal draft
    settlement_fingerprint
~~~

它只允许从原decision为`ACCEPT_AND_SUPERSEDE | ACCEPT_AND_CONTRADICT`的prepared acceptance派生；不能为plain ACCEPT或BASED_ON refs构造。Factory必须证明existing source的final kind、scope、normalized immutable payload与`fact_semantic_digest`全部exact等于原prepared fact draft，不能只比较digest字符串。该carrier不持久化、不重新调用provider，也不开放“给任意existing fact补relation”的public repository API。

Fresh read已经看到exact semantic relation时只能冻结`CONFIRM_EXISTING_RELATION`，并把relation的source/target/kind/mode、原`decision_candidate_id`与已经发生的target lifecycle一并纳入fingerprint；不得把它伪装成当前candidate将要写入的row。Fresh read尚未看到relation时冻结`APPLY_NEW_RELATION`。若后者写入时输给并发winner，整个事务回滚，重新读取后再冻结前者；不能在原carrier中接纳一个事先未知的foreign decision candidate。

所有fact/relation ID都由中央factory在write前冻结。`candidate_fingerprint`使用domain separator `pulsara:prepared-memory-governance-acceptance:v1`和唯一canonical encoding，覆盖上述全部semantic identity，但不持久化为receipt。physical deadline、connection、writer generation与provider transport不进入semantic candidate；每次write attempt只绑定当前writer guard。

ACK unknown先执行stateless exact confirmation：

~~~text
FULL
    candidate terminal columns exact match
    SKIP/ABANDONED branch没有由该candidate拥有的fact/relation row
    ACCEPTED branch exact反向指向prepared fact，且
        fact immutable columns/source candidate exact match
        expected immutable relation rows完整存在
        required superseded target transition已发生
    APPLIED_TO_EXISTING branch exact指向existing source，且
        original prepared new fact不存在
        exact relation row存在并由decision_candidate_id指向该candidate
        expected target lifecycle transition已发生
    SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT branch还要求
        exact foreign relation composite identity与原decision_candidate_id仍匹配
        target lifecycle exact符合该relation已经产生的效果

NONE
    candidate仍是exact PROCESSING head
    original prepared new fact不存在
    普通acceptance的prepared relation rows不存在
    APPLY_NEW_RELATION branch的source fact仍exact匹配，但
        decision_candidate_id指向本candidate的relation不存在，且
        target仍是expected pre-transition lifecycle
    CONFIRM_EXISTING_RELATION branch的candidate仍为PROCESSING，且
        exact foreign relation与其decision_candidate_id仍存在
        target lifecycle仍与already-applied effect一致

CONFLICT
    candidate已被不同decision终结、fact identity/payload不符、
    relation集合不符或target transition不兼容
~~~

FULL不得要求新fact或existing source仍为ACTIVE：它们可能在ACK丢失后被后续合法candidate supersede；confirmation只验证prepared acceptance/settlement已经完整发生及其immutable lineage。对于RESPONSE_PREFERENCE，FULL还允许prepared union中唯一的`RESPONSE_PREFERENCE_CAPACITY_SKIP` compatible winner：candidate exact终结为SKIPPED、reason exact为`RESPONSE_PREFERENCE_CAPACITY_EXCEEDED`且不存在prepared fact/relation。NONE使用same prepared candidate与fresh writer guard重试，绝不重跑provider。CONFLICT不得覆盖winner，记录bounded operational diagnostic后结束本次governor attempt。Host crash后没有owner执行confirmation时，candidate允许永久保持`PROCESSING`；本文不承诺跨Host恢复。

---

## 8. Canonical acceptance transaction

所有可能创建fact的branch在candidate lock后，必须先按exact scope + prepared `fact_semantic_digest`查询ACTIVE winner。已观察到winner时，本transaction不做canonical mutation，释放后按该winner冻结§8.5 settlement并进入fresh writer transaction；不能先把它按“再新增一条fact”计入RESPONSE_PREFERENCE容量。未观察到winner不构成并发证明，后续insert仍由ACTIVE partial unique给出最终winner。这样existing-source relation settlement不会被capacity SKIP提前吞掉。

### 8.1 ACCEPT

~~~text
lock exact PROCESSING candidate
revalidate immutable proposal + refs
revalidate prebound basis targets
revalidate sealed governance branch + final kind/scope/structured-field matrix
revalidate one candidate -> at most one fact; no split/merge/partial acceptance rows
if exact ACTIVE semantic winner exists: route §8.5
if final kind=RESPONSE_PREFERENCE:
    acquire transaction advisory lock for exact domain/scope
    revalidate sealed final-kind branch, response-preference structural shape,
        exact scope and final active capacity
    if capacity exceeded: atomically SKIP(RESPONSE_PREFERENCE_CAPACITY_EXCEEDED), commit, stop
insert prepared accepted fact from stored candidate fields
insert BASED_ON relations with decision_candidate_id=candidate if final kind=DECISION
update candidate -> ACCEPTED + accepted_fact_id + decision metadata
commit
~~~

### 8.2 ACCEPT_AND_SUPERSEDE

~~~text
lock exact candidate
lock exact related target
validate same exact scope + target ACTIVE
validate prepared supersede mode/kind matrix:
    SAME_KIND_REPLACEMENT -> final kind == target kind
    TAXONOMY_CORRECTION   -> final kind != target kind
validate prepared governance branch carries explicit replacement/taxonomy judgment
if exact ACTIVE semantic winner exists: route §8.5
if final kind=RESPONSE_PREFERENCE or target kind=RESPONSE_PREFERENCE:
    acquire transaction advisory lock for exact domain/scope
    recompute final response-preference set after target becomes SUPERSEDED
    if capacity exceeded: atomically SKIP(RESPONSE_PREFERENCE_CAPACITY_EXCEEDED), commit, stop
insert new exact-kind fact from immutable candidate
insert new --SUPERSEDES(mode, decision_candidate_id=candidate)--> old
update old.lifecycle -> SUPERSEDED
update candidate -> ACCEPTED + accepted_fact_id
commit
~~~

失败必须整体回滚，不能出现old已SUPERSEDED而new不存在。

### 8.3 ACCEPT_AND_CONTRADICT

~~~text
lock exact candidate
lock exact related target
validate same final kind + same exact scope + target ACTIVE
validate prepared governance branch carries explicit contradiction judgment
if exact ACTIVE semantic winner exists: route §8.5
if final kind=RESPONSE_PREFERENCE:
    acquire transaction advisory lock for exact domain/scope
    recompute final response-preference set with both facts remaining ACTIVE
    if capacity exceeded: atomically SKIP(RESPONSE_PREFERENCE_CAPACITY_EXCEEDED), commit, stop
insert new exact-kind fact
insert one unordered CONTRADICTS row with decision_candidate_id=candidate
keep both ACTIVE
update candidate -> ACCEPTED + accepted_fact_id
commit
~~~

### 8.4 SKIP

只更新candidate terminal decision；不写fact/relation。Duplicate、temporary、low-value、multi-atom、unsafe response preference、unsupported structure与locked final-state response-preference capacity exceeded均可SKIP。

### 8.5 Exact duplicate的并发winner

Provider可以在sealed decision中对plain duplicate返回SKIP，但Host不得仅凭provider前观察到winner提前terminalize candidate；relation intent必须先有机会形成。最终并发winner只由§5.5的ACTIVE partial unique index决定：

~~~text
(memory_domain_id, scope_kind, scope_id, fact_semantic_digest)
WHERE lifecycle = 'ACTIVE'
~~~

两个Host同时接受相同fact时，最多一个acceptance transaction成功。unique violation必须回滚整个原transaction；随后不得重跑provider。Host以fresh bounded read按exact scope + `fact_semantic_digest`定位ACTIVE winner，并逐字段证明它与prepared final fact draft一致；只有digest相同但kind、statement或structured fields不同属于corruption/conflict，不能成为source。

Settlement按原sealed decision分成三个closed branch：

1. plain `ACCEPT`且没有BASED_ON refs：fresh短事务锁定candidate与winner，将candidate终结为`SKIPPED_DUPLICATE`，设置`duplicate_winner_fact_id=winner`；不写relation；
2. plain `ACCEPT`带BASED_ON refs：若existing Decision的ordered BASED_ON set已与prepared set exact相同，按普通`SKIPPED_DUPLICATE`结算；否则V1不向existing Decision事后union或重排basis，candidate终结为`SKIPPED_DUPLICATE_BASIS_UNAPPLIED`并记录`duplicate_winner_fact_id`；两者均0 relation write。该限制避免恢复post-hoc basis writer；
3. `ACCEPT_AND_SUPERSEDE | ACCEPT_AND_CONTRADICT`：构造§7.7的`PreparedExistingSourceRelationSettlement`，进入下述exact existing-source transaction。不得退化成普通`SKIPPED_DUPLICATE`而吞掉已冻结relation intent。

构造该carrier前先查询exact semantic relation。若已经存在，冻结`CONFIRM_EXISTING_RELATION`并在短事务中锁定candidate、source、target和该relation，逐字段确认其原decision attribution及already-applied lifecycle后，将当前candidate终结为`SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT`；不得覆盖relation的`decision_candidate_id`。只有不存在时才冻结`APPLY_NEW_RELATION`并进入下述写事务：

~~~text
lock exact PROCESSING candidate
lock exact existing source row
lock exact prepared target
prove existing source immutable fields/scope/kind/digest == prepared final fact draft
prove original decision is SUPERSEDES(mode) | CONTRADICTS
prove exact semantic relation absent for APPLY_NEW_RELATION carrier
revalidate source ACTIVE, relation matrix, target expected lifecycle and decision_candidate identity
if either endpoint kind=RESPONSE_PREFERENCE:
    acquire exact domain/scope response-preference advisory lock
if SUPERSEDES:
    insert existing source --SUPERSEDES(mode, decision_candidate_id=candidate)--> target
    update target.lifecycle -> SUPERSEDED
if CONTRADICTS:
    insert unordered CONTRADICTS(decision_candidate_id=candidate)
    keep both facts ACTIVE
update candidate -> APPLIED_TO_EXISTING
    applied_existing_fact_id = existing source
    related_target_fact_id = prepared target
    accepted_fact_id = NULL
commit
~~~

该transaction不得修改existing source fact、其`source_candidate_id`或原producer provenance。`APPLY_NEW_RELATION`在insert时命中exact concurrent relation winner必须整体回滚，fresh read后改冻`CONFIRM_EXISTING_RELATION`，不能直接接纳未知foreign attribution。后者确定性写`SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT`并记录`duplicate_winner_fact_id + related_target_fact_id`，但不覆盖原`decision_candidate_id`。若target已被不同source/mode终结，则`ABANDONED_TARGET_DRIFT`。若winner在settlement前不再ACTIVE且当前scope已无ACTIVE exact duplicate，复用原`PreparedMemoryGovernanceAcceptance`重试原acceptance；若出现新的exact winner，冻结新的existing-source carrier。所有路径都不重跑provider。

Existing-source settlement只支持显式、singular-target `SUPERSEDES | CONTRADICTS`。`BASED_ON`不支持该分支，因为它是ordered 0..8 relation set，向旧Decision事后union会产生新的basis ownership与ordinal语义；未来若确有产品需求必须另行规格化。Repository不暴露按任意source/target调用的relation writer，所有relation insert仍只能发生在exact PROCESSING candidate的唯一terminal transaction。

该分支只给现有`memory_candidates`与`memory_relations`增加closed status/FK columns；不增加relation kind、表、event、job、guard、receipt、repair或recovery owner，六张memory relation与最终oracle数量保持不变。

普通duplicate skip与existing-source settlement都必须支持stable candidate confirmation。它们不删除candidate、不创建merge relation，也不把语义相似项视作exact duplicate。

### 8.6 不写committed occurrence

Round 8删除：

~~~text
MemoryFactAccepted
MemoryFactLifecycleChanged
MemoryRelationAccepted
~~~

原因：

- memory row本身是advisory dataset内部真值；
- 没有合法consumer需要用event replay重建memory；
- candidate ToolResult已记录“提出发生”；
- UI/inspect可查询memory rows；
- best-effort notification不应升级成durable occurrence。

同时删除memory fact/relation subject slots。不得以“architecture oracle保持不变”为由保留无consumer事件；数量oracle不是长期架构真理。

---

## 9. Recall、index与automatic compiler projection

### 9.1 恢复召回质量，不恢复旧projection machinery

Hard-cut前的有效产品思路是：多语种lexical、dense embedding、RRF、optional rerank与canonical refetch。但旧实现同时存在一个必须修正的不对称：query的lexical channel使用Jieba，PostgreSQL FTS却对raw text使用`simple` parser。这使得中文query/index不是同一search contract。

Round 8冻结一个唯一的pure `MemoryRetrievalTokenizerV1` facade，fact index与query必须调用同一实现；本次semantic `search_contract_version`升级为`2`，旧clean-v0数据库按既有规则要求reset，不建立online reindex/migration：

1. 对statement、`applies_when`与ordered `do_not_apply_when`建立closed retrieval-text projection；
2. NFC、CRLF归一、outer trim，Latin case-fold；
3. CJK span只使用package-private、一次初始化的`jieba.Tokenizer()`实例，并调用该实例的`cut_for_search(..., HMM=False)`；
4. 全文另用closed regex抽取snake_case、kebab-case、dotted identifier、version、path segment与ASCII word；
5. 使用versioned bilingual stopword set，但不删除有意义的code/version token；
6. 在只供lexical派生的副本上展开英文contraction：`won't -> will not`、`can't -> can not`、`shan't -> shall not`、通用`n't -> not`，并展开`'re/'ve/'ll/'m/'d`；`'s`只保留主体词，不修改canonical memory/query正文；
7. 丢弃不含任何Unicode letter、number或下划线的独立token；Jieba拆出的`/ . : ， 。`等标点不得进入index，同时完整`C++`、`C#`、version、identifier与path token继续保留；
8. 按第一次出现顺序dedupe，冻结每token与aggregate UTF-8 bound。

V2 stopword fixture必须让以下语义词重新成为term，避免肯定/否定、先后或范围表达塌缩为相同sparse query：

~~~text
English:
can could may might more most no nor not once only other out over own same
should then through under until up very will would

中文：
有 不 就 都 一 一个 上 也 很 要 会 没有 好 自己 或 下 后 前
~~~

物理上限保持256个terms、单term 128 UTF-8 bytes、aggregate 16 KiB。Jieba package version、default dictionary digest、`HMM=False`、contraction rules、regex、stopword fixture与normalization algorithm都进入`search_contract_id/version`的machine fixture。Private tokenizer必须在加载sealed default dictionary后完成一次初始化，随后只暴露pure tokenize facade；不得暴露`add_word`/`del_word`/dictionary mutation，不得复用`jieba.dt`或module-level `jieba.cut_for_search`。进程中其他代码调用`jieba.add_word()`不得改变memory golden output。

Bound不使用静默截断：fact派生超界时不接受fact，candidate以closed `ABANDONED_RETRIEVAL_INPUT_UNSUPPORTED`结束；explicit query派生超界时typed `QUERY_RESOURCE_BOUND`；automatic trigger超界时该sparse channel不可用，但不终止turn。不得因为memory召回是advisory就把一半terms伪装成完整index。

`memory_facts.search_terms`在fact acceptance transaction前同步计算。`search_document`是普通`tsvector NOT NULL`列，不是generated column：PostgreSQL 17中`array_to_string(anyarray,text)`为`STABLE`，不能出现在generated expression中。clean-v0建立sealed `BEFORE INSERT` trigger；普通repository INSERT必须省略`search_document`，trigger无条件以`NEW.search_terms`覆盖该列：

~~~sql
NEW.search_document := to_tsvector(
    'pg_catalog.simple'::regconfig,
    array_to_string(NEW.search_terms, ' ')
);
~~~

Trigger function固定schema/search path、无PUBLIC EXECUTE，trigger始终enabled；fact immutable invariant同时拒绝独立更新`search_terms`或`search_document`。Deep verifier必须比较function body、trigger definition/enabled state以及column不是generated。调用者提交伪造`search_document`的repository contract测试必须在SQL admission前拒绝；即使绕过repository，trigger也不得采用caller值。

GIN index建在`search_document`上，并为`search_terms`建立array GIN。Query不把原始用户文本直接交给`to_tsquery`；先通过同一tokenizer得到bounded ordered-deduped`query_terms`，再由baseline中的closed `memory_terms_to_tsquery(text[])`对每个term调用`plainto_tsquery('simple', term)`并以OR折叠。该function body进入deep verifier。Sparse WHERE必须同时包含：

~~~sql
memory_facts.search_terms && $query_terms::text[]
AND memory_facts.search_document @@ memory_terms_to_tsquery($query_terms)
~~~

raw-term overlap是candidate eligibility，FTS rank只在eligible rows内排序。比如query term `snake_case`不得仅因PostgreSQL parser将其拆为`snake & case`而命中只含两个独立raw terms的row。这同时保证：

- index/query共用同一token boundary与`simple` parser；
- user query不取得websearch/raw tsquery DSL；
- 有一个有意义term匹配就能进入bounded candidate set，不被多词AND过早清空；
- code/path term与其他term使用同一exact overlap语义，不恢复第二条`LIKE lexical` owner。

Sparse query必须先exact过滤`memory_domain_id`和§2.7 frozen visible scopes，再限制`lifecycle=ACTIVE`。候选顺序冻结为以下lexicographic tuple：

~~~text
matched_distinct_query_term_count DESC
ts_rank_cd(search_document, query) DESC
fact_id ASC
~~~

Terms已ordered-dedupe，matched count按exact raw-term set intersection计算且必须大于0；0 query terms不进入sparse query。不加recency、scope或memory kind的hidden boost；kind/scope filter只决定候选集。原始query为空或过大typed reject；query在stopword/tokenization后为空只使sparse channel缺席，不影响dense channel。

### 9.2 Fact embedding是optional弱一致cache

Round 8恢复`retrieval/embedding`process-local provider。V1只支持一个semantic vector space，不允许用任意model/dimension配置复用同一HNSW：

~~~text
embedding_contract_id          pulsara.memory.embedding.dashscope-text-embedding-v4-1024.v1
provider_family                DASHSCOPE_BAILIAN_OPENAI_COMPATIBLE
model                          text-embedding-v4
dimensions                     1024
distance                       cosine
batch_size                     <= 10
max_tokens_per_item            8,192
pulsara_local_aggregate_token_ceiling  81,920  # 10 * 8,192; local derived bound
max_concurrent                 5
~~~

DashScope远端文档在V1只被解释为“最多10行、每行最多8,192 tokens”。`81,920`是Pulsara由两项相乘得到的本地防御性aggregate ceiling，不是provider单独声明的request contract；字段名、diagnostic与测试不得把它标成remote-advertised limit。

Base URL/region和sealed API key可配置；provider family、model、dimension与distance不是V1自由参数。resolved config任一项不匹配时，dense capability整体`DISABLED_CONTRACT_MISMATCH`，不得发送remote request，sparse product path保持完整。未来切换model、dimension或embedding semantics必须发布新contract并在activation/reset中清空optional `memory_embeddings` cache；不得runtime hot-switch后让两个space共享旧HNSW，也不得增加generation/debt追平协议。

Provider必须是Host-owned、single-event-loop、长期复用的client，不得为每次query新建TCP/TLS connection。Host-local embedding worker：

- fact acceptance后接收可丢失wake；
- admission时借用当前Host的exact `FrozenMemoryReadScopeBinding`；
- bounded扫描只读取该binding可见的ACTIVE fact：同domain USER加exact current WORKSPACE，transient仅同domain USER；SQL scope filter必须先于正文hydrate；
- 扫描缺少current fact digest+embedding contract的eligible fact；
- 单次最多100 facts，provider batch exact最大10，最多5个physical requests并发；
- provider或DB失败直接结束本次attempt；
- upsert前使用同一个binding重新验证domain、exact scope、fact digest与lifecycle；
- close时cancel/join exact requests与client；
- 不创建job、generation、debt或lost-wake repair。

Worker不得以“最终SQL会scope-filter”为由先hydrate或发送其他workspace/domain正文。Host A可以计算同domain USER fact（它本来对A可见）和A的exact WORKSPACE fact；不得扫描workspace B的WORKSPACE fact或foreign domain。Binding在attempt中途失效/owner close时，剩余candidate丢弃，不重绑另一Host scope。

多个Host重复计算同一fact embedding允许；exact PK/upsert收敛。向量缺失、旧digest、旧contract或dimension mismatch都只意味着该fact从本次dense channel缺席。

Embedding前必须用与exact V1 contract绑定的estimator证明单条retrieval text/query不超过8,192 tokens、batch不超过10条且`sum(item_estimated_tokens) <= pulsara_local_aggregate_token_ceiling(81,920)`。Encoded request另受既有HTTP body bound约束。任何逐项或aggregate preflight失败都发生在remote open前；fact batch按FIFO prefix缩短，单个超界fact只保持missing vector，query超界只使dense channel缺席。不得截断或依赖provider silent truncation。

Embedding response是不可信远端输入：必须验证index在range内且exactly-once、返回集合完整、reported model compatible、dimension exact 1024、所有component finite，并以float64 accumulator证明L2 norm finite且`> 0`。Query vector和fact vector使用同一validator；zero vector、NaN或Infinity使整个physical request失败，不写row、不执行cosine SQL，也不接受partial vectors。

Dense PostgreSQL read固定使用pgvector 0.8.1+的bounded iterative HNSW contract：

~~~text
SET LOCAL hnsw.iterative_scan = strict_order
SET LOCAL hnsw.max_scan_tuples = 20000
overfetch_factor = 4
bounded_overfetch = min(policy_dense_top_k * 4, 120)
inner ORDER BY = embedding <=> query ASC
outer stable order = distance ASC, fact_id ASC
~~~

这些设置在short read transaction中`SET LOCAL`，不能污染connection pool。Scope/domain/ACTIVE/digest/contract filter必须出现在同一查询中；不得改写成similarity expression DESC。返回达到policy K时标记`BOUNDED_TOP_K`；该名称只表示本次bounded approximate HNSW query交付了K项，不声称它们是全局exact KNN。少于K时执行一个deadline-bounded、scope-safe eligibility existence probe。只有该probe证明没有其他eligible row时才标记`EXHAUSTED_VISIBLE_SET`，发现剩余row或probe无法在deadline内证明穷尽时标记`PARTIAL_BOUNDED_SCAN`。Partial仍可参与RRF，但必须映射为explicit结果的`vector_cache=PARTIAL`，不能伪装完整Top-K。只有显式sequential exact-scan test/diagnostic path才可使用`EXACT_TOP_K`一词；production V1不因评测需要自动退回顺序扫描。

pgvector只要可见集合非空就会返回“最近”的row，因此Top-K本身不能证明存在语义匹配。Round 8不等待完整标注集，先冻结一个粗糙但诚实的`DenseCandidateEligibilityPolicyV1`，仅绑定上面的exact DashScope `text-embedding-v4`/1024/cosine contract：

~~~text
cosine_similarity = 1.0 - cosine_distance

AUTOMATIC_ROOT         minimum_similarity = 0.55
EXPLICIT_SEARCH        minimum_similarity = 0.20
GOVERNANCE_RELATEDNESS minimum_similarity = 0.40
~~~

distance/non-finite validation完成后、channel rank与RRF之前丢弃低于对应floor的row；等于floor合法。所有dense rows低于floor时，该channel结果是`NO_ELIGIBLE_MATCH`而不是failure，也不得为了凑K继续无界扫描。Threshold不进入provider-visibleToolResult、memory row或score trace；explicit结果只可用`dense_match_policy=COARSE_V1`说明使用了粗eligibility policy，不能暴露每row cosine score。

这些数值继承hard-cut前`automatic=0.55 / explicit=0.20`的经验量级，`governance=0.40`是本轮明确新增的保守中间值；三者都不是跨embedding模型真理。必须物理拆分：

~~~text
EmbeddingSemanticContract
    provider family / model / dimensions / normalization / retrieval text projection
    owns memory_embeddings compatibility

DenseEligibilityPolicy
    automatic / explicit / governance minimum similarity
    owns query-time candidate admission only
~~~

只有`EmbeddingSemanticContract`变化才需要activation/reset清空optional vectors。仅threshold/policy identity变化时复用现有compatible vectors，只更新policy golden、process-local retrieval policy identity，并在同Host禁止hot-patch正在运行的automatic recall attempt；下一cold retrieval/compiler epoch采用新policy。不得把threshold改动伪装成vector stale、generation debt或re-embedding工作。未来有标注集后可以替换本policy，但Round 8 activation不伪造“已校准”结论。

### 9.3 三种recall policy与唯一fusion

V1冻结：

~~~text
AUTOMATIC_ROOT
    sparse top 20 || dense top 20
    RRF(k=60)
    final top 5
    remote reranker calls = 0

EXPLICIT_SEARCH
    sparse top 40 || dense top 30
    RRF(k=60)
    optional qwen3-rerank over fused top 20
    final default 5, caller max 50

GOVERNANCE_RELATEDNESS
    sparse top 30 || dense top 30
    RRF(k=60)
    final top 5
    remote reranker calls = 0
~~~

`AUTOMATIC_ROOT`在sparse query、dense eligibility、fusion与final canonical refetch四处都必须强制`fact_kind <> RESPONSE_PREFERENCE`。这不是依靠最终renderer去重：RESPONSE_PREFERENCE不得占用automatic Top-20/Top-5 candidate slot，也不得发送给query reranker（automatic本来也禁止reranker）。`USER_PROFILE`必须与FACT/ACTION_RULE/DECISION一样参与automatic sparse+dense；`EXPLICIT_SEARCH`与`GOVERNANCE_RELATEDNESS`仍可按各自purpose读取全部五类，前者服务用户/模型主动查询，后者在same exact scope提供all-kind related allowlist：同kind可用于ordinary supersede/contradict，cross-kind只能用于显式taxonomy-correction supersede。

Sparse与dense在各自取得admission后并行执行。RRF是唯一fusion，不保留“RRF或其他更小算法”的实施自由：

~~~text
rrf_score(id) = Σchannel 1 / (60 + one_based_rank(channel, id))
~~~

只对出现的channel求和，stable tie-break为fact ID。Sparse-only、dense-only与两者共存都是正常success；只有两个channel都发生真实operation failure才是retrieval unavailable。“没有匹配”不是failure。

Provider/operation默认deadline：

~~~text
MEMORY_AUTO_QUERY_EMBEDDING       3s absolute attempt
MEMORY_EXPLICIT_QUERY_EMBEDDING   4s absolute attempt
MEMORY_EXPLICIT_RERANK            4s absolute attempt
MEMORY_EXPLICIT_RECALL_TOTAL      8s absolute attempt
MEMORY_FACT_EMBEDDING_BATCH      30s absolute attempt
MEMORY_RETRIEVAL_DISABLE_CLOSE  120s standalone disable/replacement join
~~~

这些由closed watchdog owner签发，不接受call site自由seconds。`GOVERNANCE_RELATEDNESS`不签发独立query-embedding总期限；它必须消费§7.1.1同一个`MEMORY_GOVERNANCE_ATTEMPT`的remaining budget，随后auxiliary model只能使用剩余时间。Timeout/cancel必须physical join exact request，不detach HTTP owner。Automatic dense timeout后立即使用sparse result；explicit embedding/rerank timeout也分别退化，不把已取得的候选丢弃。Real-provider latency数据记入activation evidence，但外部网络p50/p95不是deterministic correctness gate。

`MEMORY_RETRIEVAL_DISABLE_CLOSE`只供运行中独立禁用/replacement retrieval subsystem；Host session close必须复用已经冻结的single `HOST_SESSION_CLOSE` deadline，依次停止admission、cancel/join recall/embedding/rerank requests并close clients，不能为memory重新签发额外120秒。

不得为并行表象在remote embedding/rerank期间持有PostgreSQL transaction。Sparse candidate、vector candidate可来自独立bounded read；RRF/rerank只是process-local候选计算，唯一产品权限边界是最后canonical refetch。Memory是advisory dataset，本轮不伪造跨remote I/O的单一RR snapshot。

### 9.4 Automatic ROOT recall与prefix continuity

Automatic recall的query是Round 3.1的exact latest human dispatch anchor，而不是从全transcript重建或将多steer拼成一条新消息。它不能直接成为当前同步context-source collector的callback：Round 3.1会在canonical steer consumption前执行最多128个prospective prefix trial，而Host-owned embedding client属于单一event loop。任何trial、worker thread collector或未被最终选择的steer正文都不得发起remote embedding。

`MEMORY_RESPONSE_PREFERENCE_HEAD`不依赖query，也不进行embedding/rerank。Host在pre-consumption planning阶段以当前frozen memory read-scope做一次bounded repeatable-read，冻结完整`PreparedMemoryResponsePreferenceHead`（ordered items、warnings、presence、body/fingerprint与source contract），随后所有prospective trial和final compiler只能消费该immutable carrier，不能再次查库。它属于不依赖prospective trigger正文的shared one-cut fact；governance在freeze之后提交的新RESPONSE_PREFERENCE有意延迟到下一个eligible human trigger。其FULL-only exact body最多16 KiB；prepared carrier保存真实FULL encoded item/byte/token成本，但prospective steer quote只强制计入prior VALUE发生语义变化时的minimal invalidation floor，不能把optional FULL成本变成steer admission barrier。

若head从未安装且当前FULL无法容纳，允许省略这个optional advisory source；若installed head与新head相同则no-op。只有旧head为`VALUE`且本次发生内容变化、清空、显式memory opt-out或读取失败时，才必须终止旧语义：优先安装完整replacement `VALUE`，放不下时安装最小`UNAVAILABLE`；确定空集或显式opt-out使用最小`CLEARED`。旧head不存在或已经`CLEARED | UNAVAILABLE`时，新的FULL放不下允许no-op。只有连最小失效carrier都无法容纳时provider open才以既有typed input resource boundary结束。不得继续让旧RESPONSE_PREFERENCE冒充当前head，也不能截断、Top-N或摘要。该边界不增加preference-specific compactor；未来由通用provider-input compaction处理历史snapshot累计。

Automatic recall在任何remote data egress前消费§6.4冻结的exact `MemoryUsePolicy`，并独立计算low-information disposition：

~~~text
ALL_DISABLED_BY_USER
    automatic recall = disabled
    response-preference head = disabled
    explicit memory tools = disabled
    Cheap Hint Reflection = disabled

WRITE_DISABLED_BY_USER
    automatic recall + preference head = enabled
    remember = disabled
    Cheap Hint逐entry排除write-opt-out正文

ENABLED
    ordinary memory behavior

SKIPPED_LOW_INFORMATION
    len(normalized Unicode code points) < 8
    only automatic recall is skipped
~~~

`ALL_DISABLED_BY_USER`优先于length分类：previous `MEMORY_RECALL`为VALUE/UNAVAILABLE时append一次CLEARED；previous `MEMORY_RESPONSE_PREFERENCE_HEAD`只有为VALUE时append一次CLEARED；对应head原本不存在或已CLEARED/UNAVAILABLE时no-op。Embedding、rerank、memory query与Cheap Hint auxiliary call exact 0。`remember`、`memory_search`、`memory_get`、`memory_explain`全部在local authorize、attempt acceptance及physical invoke之前拒绝；invoke重复验证同一frozen call policy防止内部绕过。普通工具仍可执行，tool surface保持完全相同。

`WRITE_DISABLED_BY_USER`不清除任何memory source，automatic recall与preference head保持正常；`remember`在local authorize与attempt前拒绝。Cheap Hint只过滤命中`MemoryWriteOptOut`的对应entry，若还有其他eligible entry仍可best-effort review，全部过滤时0 call。

`SKIPPED_LOW_INFORMATION`与三态write/use policy正交：只对previous `MEMORY_RECALL`追加CLEARED/no-op；`MEMORY_RESPONSE_PREFERENCE_HEAD`继续按本次frozen current head执行unchanged/no-op或changed/replacement，四个显式memory tool均保持可用。Stable BASE_SYSTEM和`memory_search` descriptor必须告诉模型：面对“部署呢？”之类短但可能依赖历史的请求，可以主动调用`memory_search`；不允许harness偷偷对短输入执行dense后再决定是否跳过。

`MEMORY_RECALL`是stateful SNAPSHOT而非每turn必须重复的日志。Ranking决定一个新snapshot第一次安装时的provider展示顺序，但微小ANN/RRF排序抖动本身不值得重复追加相同正文。Final canonical refetch后分别冻结presentation与stable membership identity：

~~~text
presentation_items = current ranked ordered projection

recall_membership_fingerprint = H(
    sort_by(memory_id,
        (memory_id, fact_semantic_digest, kind, scope,
         canonical_sorted projected relation warnings)),
    provider variant-independent semantic identity,
)
~~~

若新membership/content/warning fingerprint与installed VALUE head完全相同，则保留已安装presentation order并no-op，不因新rank顺序追加；membership、fact digest或warning集合变化时才以当前`presentation_items` append replacement VALUE。空结果、explicit disable或low-information从`MEMORY_RECALL` VALUE转CLEARED；连续CLEARED为no-op；真实双channel failure从VALUE转UNAVAILABLE。这里的low-information不作用于`MEMORY_RESPONSE_PREFERENCE_HEAD`。Fingerprint不含query文本、trigger entry、rank/order、score、latency或provider disposition，否则相同事实集合会因每turn排序抖动重复注入。当前human input仍作为canonical suffix单独进入模型，所以source no-op不会吞掉用户消息。

V1冻结以下两阶段linearization：

#### Phase A：用户steer优先，只预留必要state invalidation

1. freeze Round 3.1 predecessor/base cut、target、tool surface与不依赖prospective trigger的shared one-cut facts；ACTIVE_SKILL等已由Round 3.1定义的trigger-dependent source仍可对每个prospective prefix做pure、local、bounded推导，但不得借此执行remote I/O；
2. 读取target steer lane的bounded pending rows，但不消费；
3. 读取continuity owner中已安装的exact `MEMORY_RECALL`与`MEMORY_RESPONSE_PREFERENCE_HEAD`。Recall previous head为`VALUE | UNAVAILABLE`时建立`MemoryRecallInvalidationFloor`；preference previous head只有为`VALUE`且本次frozen desired projection不同、empty、opt-out或unavailable时才建立`MemoryResponsePreferenceInvalidationFloor`。其余preference state的floor为`NONE`；
4. 两个floor都不是VALUE placeholder。每个只冻结其source可用的`CLEARED`与minimal public `UNAVAILABLE` wire carrier exact encoding，并以同一target estimator计算逐项ceiling：`provider_item_count=1`、`encoded_utf8_bytes=max(legal variants)`、`estimated_input_tokens=max(legal variants)`、source/envelope contract fingerprint。它们不含query、memory IDs、statement或remote结果，不进入provider wire；
5. longest-prefix 128→1 trial只编译immutable base、prospective steer suffix、对应pure trigger-dependent facts以及两个floor的aggregate quote；optional memory VALUE的cost按0处理。Remote embedding call count必须为0；最终selected plan冻结这些非memory source fingerprints与两个floor quote；
6. 选择并consume/confirm不受optional memory阻塞的最长FIFO prefix。只有`head steer + 所有必要invalidation floors`本身也无法容纳时，才沿用Round 3.1 atomic reject+turn terminalization；其余row保持PENDING；
7. selected prefix冻结后，以其exact `SteerSuffixAdmissionQuote`、effective target budget与64 MiB epoch bound计算剩余空间。先在始终保留`MemoryRecallInvalidationFloor`的前提下，为frozen preference carrier选择`FULL | CLEARED | UNAVAILABLE | OMITTED`并冻结exact final cost；再以剩余空间建立`PreparedMemoryRecallMaterializationBudget`：

~~~text
prior_head_presence
invalidation_floor item/byte/token ceiling
maximum_final_provider_items     0..1
maximum_recalled_memory_items    0..5
maximum_final_encoded_utf8_bytes 0..32 KiB including envelope
maximum_final_input_tokens       exact remaining target budget
maximum_final_epoch_bytes        exact remaining epoch budget
estimator_fingerprint
budget_fingerprint
~~~

`maximum_final_*`包含recall mandatory floor本来占用的空间，因为final recall VALUE或invalidation会替换planning floor，而不是两者同时进入wire。Preference final carrier的exact cost已先从remaining budget扣除；optional preference FULL不能侵占recall floor。Provider item count与observation body中的recalled-memory item count分别计量，不能混成一个“items”字段。Round 3.1的`SteerSuffixAdmissionQuote`必须增加这些typed reservations及其fingerprint coverage；不能把任意32 KiB或随手构造的`TokenEstimate`塞进现有exact estimate字段。Selected steer/nonmemory compile仍exact；两个memory final carriers分别证明actual cost不超过其frozen disposition与总provider-item/body-item/byte/token/epoch budget。

具体兼容现有quote contract的物理形状冻结为：`resulting_target_estimate`继续只表示已经真实编译的base+steer+nonmemory exact estimate；新增nullable `memory_recall_reservation`与`memory_response_preference_reservation`字段。后者还绑定frozen desired head disposition/fingerprint和完整FULL encoded cost，但只有minimal floor进入mandatory quote。Quote validator分别证明：

~~~text
exact_estimate.total_input_tokens + sum(reservations.invalidation_token_ceiling)
    <= effective_target_budget
exact_epoch_bytes + sum(reservations.invalidation_byte_ceiling)
    <= 64 MiB
~~~

每个reservation的item/byte/token/epoch ceiling、prior head fingerprint、desired disposition/fingerprint、source contract与estimator fingerprint全部进入`quote_fingerprint`。因此既不伪造exact `TokenEstimate`，也不允许final materializer换estimator或偷用未报价维度。

`MEMORY_RESPONSE_PREFERENCE_HEAD`的完整FULL仍为`IMPORTANT` optional materialization。Final allocator在同一frozen carrier上先尝试完整FULL：能容纳则安装完整successor；不能容纳且prior为VALUE时，desired为确定空集/`ALL_DISABLED_BY_USER`则安装prepared `CLEARED`，desired为changed nonempty/read unavailable则安装prepared `UNAVAILABLE`；prior不存在或已`CLEARED | UNAVAILABLE`时省略。只有prepared floor本身无法容纳才是typed resource boundary，16 KiB FULL本身绝不能永久阻断对话。

`MEMORY_RECALL`的ordinary VALUE仍是`IMPORTANT`并可降级/省略；previous head为VALUE或UNAVAILABLE时，这一个call的state invalidation floor在allocator中临时作为`MUST_KEEP`处理。该提升只终止旧snapshot语义，不提升memory内容的信任、permission或长期priority。

Phase B按剩余budget尝试`FULL -> COMPACT -> REF_ONLY`。没有任何VALUE variant可容纳时：prior head为VALUE或UNAVAILABLE则安装exact `CLEARED`或新的`UNAVAILABLE`失效载体；prior head不存在/CLEARED则整个optional source省略。Unused budget不得回头多消费queue row，但optional memory也绝不能成为本来合法user steer的拒绝原因。

#### Phase B：FULL之后只对final trigger召回一次

1. selected steer consumption达到FULL或exact-confirm后，通过`ProviderSafePointCoordinator`原子rotate当前handle：旧handle保持active，coordinator lock内用fresh canonical deadline取得新`PreparedProviderInputHandle`并一次性swap generation；不得先暴露“无active handle”窗口；随后用新handle重新读取canonical cut，证明它只比base多出exact selected ordered suffix；initial ROOT prompt直接沿用其已经active的initial handle；
2. final accepted batch的最后一项是dispatch/activation trigger，前序accepted steer只属于`canonical_delta_before_trigger`；initial ROOT prompt没有steer时，以其exact admission entry为trigger；
3. Host event loop安装或复用一个keyed by exact trigger+cut+scope的`AutomaticMemoryRecallAttempt`，再通过long-lived async client执行一次召回；
4. attempt冻结结果后，把一个immutable `MEMORY_RECALL` source candidate交给同步collector；collector只渲染该carrier，不访问memory repository、不调用AsyncClient、不跨event loop；
5. 新`PreparedProviderInputHandle`从rotate/initial freeze开始，持续覆盖async recall、final refetch、compile、continuity CAS、provider preflight与`begin_model_operation`/physical provider open linearization；Terminal observation、external result及其他canonical producer在此期间保持pending；
6. 重验selected plan中target/tool及所有非memory source fingerprints仍exact一致，再使用这些facts与materialized memory source执行一次final compile、continuity CAS与provider preflight。只有安装成功并在同一handle上开始model operation后才允许physical provider open。

Safe-point handle是唯一需要跨remote I/O持有的process-local exclusion owner；不得同时持有PostgreSQL transaction、Host session lock或coordinator lock等待embedding。Handle rotation只在bounded canonical I/O期间短暂持有coordinator lock。普通recall timeout/failure按advisory fallback继续；只有rotation、cut proof、source contract或budget proof失败才复用现有post-consumption conflict settlement。用户取消使用exact per-turn cancellation owner终结，不另造memory recovery path。

Host为每个exact trigger建立bounded process-local `PreparedAutomaticMemoryRecall`：

~~~text
trigger entry identity/fingerprint
memory domain + frozen read-scope binding
embedding contract identity
query embedding (optional result)
ordered recalled item projection
retrieval channel disposition
compiler source fingerprint
~~~

该owner保证：

- `AutomaticMemoryRecallAttempt`是Host-owned process-local single-flight，closed状态至少覆盖`PREPARED | IN_FLIGHT | FROZEN | RELEASED`；waiter cancel请求cancel并physical join，Host close/feature disable必须join后release，不detach remote owner；
- 同一trigger的final compiler retry、provider preparation retry与后续tool loop复用FROZEN结果，不重复调用embedding；prospective trial的call count始终为0；
- 一旦`MEMORY_RECALL` suffix安装进continuity epoch，后续只能append，不能因memory lifecycle/vector/rerank变化修改它；
- Host crash可丢失cache，重开Host可重算；不持久化query embedding、recall snapshot或provider request ID；
- 自动召回永不调用reranker，architecture test要求call count exact 0；
- 每个automatic result在编译前重新执行§2.7 scope、ACTIVE lifecycle与fact digest canonical refetch；被删除/替代的candidate只被drop，不自动转向successor。

Retrieval failure、timeout或malformed vector冻结为sparse-only或minimal `UNAVAILABLE` carrier。Final source必须逐项证明不超过`PreparedMemoryRecallMaterializationBudget`；VALUE放不下时按上述规则降级/省略，不能把budget不足升级成planning conflict。只有mandatory invalidation无法容纳、safe-point rotation/cut proof失败、source contract violation或actual越过已冻结四维budget才是typed post-consumption conflict，provider open exact 0；不得消费更多steer、换trigger或在collector内重新召回。

Compiler variants：`FULL`最多5 items/32 KiB UTF-8 encoded body，`COMPACT`最多3 items/16 KiB，`REF_ONLY`最多5 refs/4 KiB且只保留stable memory ID、kind、USER/WORKSPACE scope和`memory_get`提示。FULL/COMPACT无法容纳一条exact statement时整个source降为REF_ONLY，不截断statement。Memory recall的registry与compiler `_SOURCE_POLICY`必须同时冻结上述trust/lifecycle/budget/placement，不能只改一边。它可被budget降级；若previous head为VALUE或UNAVAILABLE，即使正文被省略也必须容纳minimal CLEARED/UNAVAILABLE invalidation，不得让旧snapshot被误解为当前recall状态。Provider body不包含retrieval score或远端service详情。

### 9.5 Explicit `memory_search`与rerank

Explicit search先按§3.6建立最多四项、ordered-deduped `MemorySearchFilterStagePlan`。每个stage冻结scope/kind predicate与relaxation ordinal；同一query tokenizer output和query embedding在全部stage复用，不能把fallback实现成四次remote embedding。每个stage执行自己的bounded sparse/dense/RRF并只补充first-seen fact；达到`min(limit, 3)`停止。全部stage共享`MEMORY_EXPLICIT_RECALL_TOTAL=8s`，不能为fallback重新签发完整deadline。

Configured `qwen3-rerank`最多调用一次，只接收最终first-seen union的Top-20 query与bounded candidate retrieval text，不接收producer transcript、citation body、domain/scope ID、relation或internal metadata。Reranker rank只在相同relaxation ordinal内生效；final stable sort先按stage ordinal，再按该tier内rerank/RRF rank，最后fact ID。Reranker：

- 只重排Top-20，不使用跨request绝对score threshold删除强exact/sparse candidate；
- 不保存score、response、request ID、trace或durable usage row；
- 失败、超时、未配置时按RRF order返回；
- requested limit大于20时，先返回reranked Top-20，然后按原RRF order追加未参与rank的候选；
- 不用于governance relatedness或automatic recall。

Rerank response必须验证index在candidate range内、不重复、结果集合与requested `top_n` 一致、score finite。任一未知/缺失/重复/malformed row使整个rerank branch `FAILED_FALLBACK`，不部分采用远端排序。

Rerank preflight绑定exact`qwen3-rerank`contract：query最大8 KiB UTF-8且不超过4,000 estimated tokens；每candidate remote projection最大8 KiB UTF-8且不超过4,000 estimated tokens；candidate count最多20；encoded request aggregate最大192 KiB；token aggregate必须满足provider公式：

~~~text
query_tokens * candidate_count + sum(candidate_tokens) <= 120,000
~~~

Candidate过长时使用Round 1 UTF-8-safe head/omission/tail仅作为remote ranking projection，不改写canonical statement；projection完成后仍须重新估算逐项与aggregate tokens。Query、任一candidate或aggregate request过界时，整个rerank分支`NOT_APPLICABLE`并使用exact RRF order，不截断用户query、不发送partial candidate batch，也不依赖provider silent truncation。

最终结果必须再做一次canonical refetch：exact domain/scope、ACTIVE lifecycle、fact semantic digest、embedding contract与visible relation重验。远端模型只能改变候选顺序，不能创建memory ID、突破scope或使SUPERSEDED fact重新可见。

### 9.6 Direct relation行为

- ordinary search只从ACTIVE rows选seed；不会搜索SUPERSEDED正文，也不会把只命中旧statement的query自动改投successor；
- 若一个ACTIVE seed本身拥有direct outgoing `SUPERSEDES` relation，search可以附带bounded lineage annotation；
- `memory_get/explain`读取一个仍可见的exact SUPERSEDED fact时，可以展开其direct ACTIVE successor；这是exact-ID explanation，不是search seed重定向；
- 命中任一有`CONTRADICTS`的active memory时，可附带最多一个bounded same-kind active conflict companion与warning；
- Decision的BASED_ON只在`memory_get/explain`展开，ordinary search不自动扩展为更多seed；
- 任意relation读取均有fanout limit；
- 没有recursive CTE、`max_hops`工具参数或任意predicate组合。

### 9.7 Freshness与展示口径

Memory query不返回index desired/applied generation或`PARTIAL_UNAVAILABLE` incident。Explicit `memory_search`可以返回：

~~~text
retrieval_channels = [SPARSE_FTS]
                   | [VECTOR]
                   | [SPARSE_FTS, VECTOR]
                   | [SPARSE_FTS, RERANK]
                   | [VECTOR, RERANK]
                   | [SPARSE_FTS, VECTOR, RERANK]
vector_cache       = AVAILABLE | PARTIAL | NOT_AVAILABLE
rerank             = APPLIED | NOT_CONFIGURED | FAILED_FALLBACK | NOT_APPLICABLE
dense_match_policy = COARSE_V1 | NOT_APPLICABLE
filter_fallback    = NOT_NEEDED | KIND | SCOPE | KIND_AND_SCOPE | EXHAUSTED
advisory            = true
may_be_stale_or_incomplete = true
~~~

`AVAILABLE`只表示dense path得到`BOUNDED_TOP_K | EXHAUSTED_VISIBLE_SET`；它不承诺global exact KNN。`PARTIAL`专指bounded HNSW scan未能证明完整candidate set，仍可使用已取得candidate；`NOT_AVAILABLE`覆盖disabled/missing/timeout/invalid query vector。不得把`PARTIAL`升级成Runtime incident、durable debt或automatic retry。

这些是产品展示语义，不是Runtime health status。`filter_fallback`必须与§3.6的attempted stage carrier一致，并同时报告exact/relaxed result count；不能只给一个布尔值让模型猜测已放宽哪项。Automatic compiler projection只显示advisory item，不把channel disposition、coarse threshold或provider failure显示给模型，除非两个channel都不可用而需要typed UNAVAILABLE。

Sparse rank、cosine distance、RRF score与rerank relevance score都是单次query内的process-local排序信号，不进入ToolResult、compiler body、memory relation或PostgreSQL usage row。Provider-visible结果只保留ordered item和上述channel disposition。

### 9.8 配置、数据外发与resource ownership

Round 8恢复并验证：

~~~text
PULSARA_EMBEDDING_API_KEY
PULSARA_EMBEDDING_BASE_URL
PULSARA_EMBEDDING_MODEL=text-embedding-v4         # V1 exact; mismatch disables dense
PULSARA_EMBEDDING_DIMENSIONS=1024                 # V1 exact; mismatch disables dense
PULSARA_MEMORY_AUTO_DENSE=true
PULSARA_MEMORY_CHEAP_HINT_REFLECTION=true
PULSARA_MEMORY_HINT_REVIEW_ALLOW_CROSS_PROVIDER=false

PULSARA_RERANK_API_KEY
PULSARA_RERANK_BASE_URL
PULSARA_RERANK_MODEL=qwen3-rerank
PULSARA_MEMORY_EXPLICIT_RERANK=true
~~~

`PULSARA_MEMORY_AUTO_DENSE=true`只在embedding key、DashScope-compatible endpoint以及exact V1 model/dimension contract全部匹配时生效；任一缺失或不同值都安静退化为sparse-only并记录不含secret/text的`DISABLED_CONTRACT_MISMATCH` diagnostic，绝不能把其他model的1024维vector误当成同一space。Rerank也只在key/model完整时生效。Embedding会向configured provider发送query或accepted fact retrieval text；rerank会发送explicit query与最多20条candidate text。这是数据外发配置，必须在README/.env.example明示；禁用后保持完整sparse product path。

`PULSARA_MEMORY_CHEAP_HINT_REFLECTION`默认true；关闭时opt-out/hint matcher仍可pure测试但production不安装auxiliary attempt。默认开启只授权same provider trust domain：hint handoff冻结foreground与resolved auxiliary target的`ResolvedProviderTrustDomainIdentity`，其canonical identity覆盖provider family、normalized endpoint origin/base-path、sealed credential-slot identity以及organization/project binding，不含raw key。Model不同但上述identity相同仍属same domain。

若auxiliary target与foreground trust-domain不同，默认在任何packet assembly/data egress前返回`DISABLED_CROSS_PROVIDER_NOT_AUTHORIZED`。只有`PULSARA_MEMORY_HINT_REVIEW_ALLOW_CROSS_PROVIDER=true`才允许该调用；默认false。当前`LLMConfig`只有一组credential/base URL时不得为了测试伪造第二provider配置，正常Flash model自然exact join同一trust domain。未来增加独立auxiliary provider时必须同时实现这项typed opt-in，而不能只靠README披露。

无论same/cross provider，开启都会把§6.4.2的bounded user/assistant public projection发送给resolved auxiliary/Flash provider，因此README/.env.example仍必须把它作为独立数据外发行为明示。未配置可用auxiliary model、permission denied、write opt-out、feature disabled或cross-provider未授权都安静退化为“无fallback candidate”，不影响显式`remember`。

Embedding/rerank client都属于Host-owned process-local retrieval resources，共享Round 5A close discipline：停止admission、cancel exact requests、physical join、最后close client。API key、Authorization header、query、candidate text、vector与raw response不进入diagnostic、activation evidence或ordinary log。

### 9.9 Round 8不建立完整召回评测集

本轮明确不以建立100–200条gold memories、50–100条labeled query及大规模distractor集作为activation前置。`0.55 / 0.20 / 0.40`只标记为`COARSE_V1`，不得在README、evidence或代码注释中宣称已由Pulsara workload校准。

Activation仍必须有小型deterministic correctness fixture，但它只证明mechanical contract而非总体召回质量：

- synthetic cosine rows分别位于每个floor的below/equal/above，证明equal保留、below丢弃、全below得到`NO_ELIGIBLE_MATCH`；
- USER/WORKSPACE/domain scope leakage exact 0；
- SUPERSEDED seed exact 0；
- sparse-only、dense-only与hybrid RRF golden；
- explicit filter relaxation stage/order/visible disclosure；
- 中文、English、mixed code/path tokenizer golden和少量hard negative；
- automatic reranker call exact 0。

完整Hit@K、MRR/nDCG、threshold calibration和真实distractor corpus记入长期memory方向文档，作为Round 8之后的独立quality round；不得为了未来评测在本轮恢复durable query trace、usage ledger或在线threshold tuning。

2026-08-15的非敏感synthetic probe使用240条memory-shaped中英/code文本与8个query，仅作成本证据：

~~~text
text-embedding-v4 cold query       1.27s
warm unique query embedding        p50 0.363s / p95 0.643s
local 240x1024 cosine Top-20       p50 0.029s
qwen3-rerank Top-20                p50 0.401s / p95 1.226s
dense -> Top-20 -> rerank E2E      p50 0.832s / p95 1.839s
~~~

这组数据支持“automatic使用warm query embedding但不使用reranker”的默认。它不是SLA，也不允许coding agent为追求该数字引入durable cache、prefetch或不透明的query rewrite。

---

## 10. ACTION_RULE边界

`ACTION_RULE`表达长期参考性行为建议，例如：

~~~text
statement: 修改生产数据库前先备份
applies_when: 修改生产数据库
do_not_apply_when: []
~~~

它不表达：

- tool permission；
- approval requirement；
- filesystem/network sandbox；
- Plan read-only overlay；
- transaction precondition；
- Host必须机械执行的policy。

如果产品需要强制“修改生产数据库前必须备份”，应由tool policy、workflow或外部业务系统实现。Memory最多提醒模型验证和采取备份步骤。

Recall rendering必须把ACTION_RULE放在advisory memory carrier中，不能lower成system policy。

---

## 11. “必须现在记住”的产品行为

用户：

~~~text
必须现在记住：以后回答尽量简短。
~~~

正确路径：

~~~text
current user message
    -> immediately affects current conversation

Agent remember(...)
    -> candidate + ToolResult transaction
    -> status=proposed_for_review

if Agent never calls remember and turn completes successfully
    -> CheapMemoryHintSetV1 matches "必须...记住"
    -> at most one best-effort MEMORY_HINT_REVIEW call
    -> maybe 0..4 ordinary PENDING candidates

best-effort governor
    -> maybe ACCEPT later
    -> maybe SKIP
    -> maybe never finish
~~~

不得：

- 因“必须、现在、确认”直接插入memory fact；
- 因用户强烈措辞绕过candidate；
- foreground等待governance完成；
- ToolResult声称未来一定可recall；
- 把当前任务正确性建立在memory eventual acceptance之上。
- 把cheap hint本身当成accepted fact，或在failed/interrupted turn后补做reflection。

当前turn需要遵守的内容由current user message拥有，而不是memory。

---

## 12. Prefix continuity与provider wire

### 12.1 Citation handle不破坏已安装prefix

ToolResult citation handle必须在该result首次lowering时一起冻结。后续开启`remember`能力、embedding ready或governance状态变化都不得修改旧ToolResult message。

Round 3.1不变量继续是：

~~~text
SYSTEM[n+1]   == SYSTEM[n]
tools[n+1]    == tools[n]
messages[n+1] == messages[n] || append_only_suffix
~~~

如果`remember`tool schema发生变化，必须在safe point开启新的process-local tool-surface epoch；不能只替换executor。

### 12.2 Provider wire卫生

Provider可见：

- advisory memory product fields；
- stable memory ID；
- current-call ToolResult citation handle；
- statement、kind、scope与必要relation warning。

Provider不可见：

- candidate acceptance digest、fact semantic digest或prepared acceptance fingerprint；
- `memory_domain_id`、internal `scope_id`、workspace hash与`origin_workspace_id`；
- database transaction/generation；
- embedding contract fingerprint；
- governor processing token；
- raw governance prompt/output；
- canonical ToolResult database ID；
- private path、DSN、artifact/blob internals或secret。

---

## 13. Failure matrix

| 场景 | 结果 |
|---|---|
| invalid `remember` schema | typed INVALID_ARGUMENTS；0 candidate |
| unknown field或force/bypass字段 | fail closed；0 candidate |
| invalid scope | fail closed；Host不猜scope |
| Agent提交raw `ctx:*`、domain或workspace ID | INVALID_ARGUMENTS；0 candidate |
| transient Host选择WORKSPACE | SCOPE_NOT_AVAILABLE；0 candidate |
| session resume/takeover的memory domain不匹配 | typed identity conflict；不取得writer、不读取memory |
| `resume_most_recent`存在同workspace异domain的新session | SQL先按exact domain过滤；选择本domain winner，不泄漏异domain summary/count/order |
| `memory_get/explain`命中不可见ID | not-found-style result；不泄漏存在性或metadata |
| 跨workspace读取已接受USER fact的`memory_explain` | 返回fact/public relation与redacted provenance；不返回origin transcript/ToolResult/内部ID |
| stale/foreign ToolResult handle | typed source error；0 candidate |
| cited handle来自memory_search/get/explain或其artifact_read | 可记录为`MEMORY_READ_EXPOSURE`，但不计作新证据；同批exposed IDs进入anti-echo provenance |
| 历史ToolResult只有数据库row、没有exact call snapshot/binding | 不签发citation handle；不能被`remember`引用 |
| cited result在当前provider cut后 | source error；0 candidate |
| USER candidate引用workspace-only ToolResult | scope escalation error；0 candidate |
| invalid based-on memory ID | source error；0 candidate |
| candidate transaction失败 | tool failure；不得声称proposed |
| candidate commit ACK unknown | exact confirmation或same-candidate retry |
| successful ROOT turn命中cheap hint且没有任何`remember`request | 最多一次best-effort `MEMORY_HINT_REVIEW`；0..4 candidate或空结果 |
| entry明确说don't remember/save/store或不要记住/保存 | 在matcher和data egress前移除该entry；`don't forget/不要忘记`不移除；全移除时0 call |
| failed/interrupted/USER_STOPPED turn命中cheap hint | 0 reflection call、0 candidate |
| turn已成功提交`remember` candidate | fallback被抑制；不重复提出同一turn候选 |
| `remember`被schema/permission/user-policy拒绝且另有eligible entry | 只过滤对应write-opt-out entry；其余entry仍可review，不把失败请求冒充成功candidate |
| Plan read-only或memory_write denied时命中cheap hint | 0 reflection call、0 candidate |
| cheap-hint feature disabled或auxiliary model未配置 | 0 reflection call、0 candidate；显式remember不受影响 |
| hint auxiliary与foreground provider trust-domain不同且未显式授权 | `DISABLED_CROSS_PROVIDER_NOT_AUTHORIZED`；0 data egress/0 call |
| ROOT terminal FULL后hint lane忙 | foreground slot先释放；handoff可丢失或异步等待，下一prompt不被reflection阻塞 |
| hint review provider/parse/write失败或Host crash | 允许整批丢失；无retry/job/event；不得重跑model |
| hint review输出部分candidate非法 | whole batch拒绝；不部分采用 |
| candidate已PENDING但wake丢失 | 允许；future bounded scan可能发现 |
| origin workspace以外的Host扫描到PENDING candidate | SQL不可见、不可claim、不可送provider；candidate可永久PENDING |
| candidate claim后Host crash | 可永久PROCESSING；无repair |
| governance provider失败 | best-effort ABANDONED或保持PROCESSING |
| governance provider超时/Host close | cancel并physical join exact auxiliary call；candidate可保持PROCESSING；无retry |
| governance想改写statement | output invalid/skip；不得接受改写 |
| model-visible memory provenance OVERFLOW或无法完整投影 | deterministic SKIP；不能只检查prefix subset后接受 |
| cited primary ToolResult与candidate语义无关 | 不解除anti-echo；governance仍SKIP pure memory restatement |
| relatedness unavailable | 只能ACCEPT无supersede/contradict或SKIP |
| target在acceptance前漂移 | entire transaction rollback；不得换绑 |
| governance acceptance ACK unknown | same prepared acceptance exact-confirm FULL/NONE/CONFLICT；不重跑provider |
| 并发exact duplicate acceptance | partial unique选一个ACTIVE winner；plain loser SKIP；sealed SUPERSEDES/CONTRADICTS loser进入existing-source settlement；BASED_ON不同set明确不补边 |
| fact retrieval terms派生超界/失败 | 0 fact/0 relation；candidate `ABANDONED_RETRIEVAL_INPUT_UNSUPPORTED` |
| explicit query tokenizer超界 | typed `QUERY_RESOURCE_BOUND`；0 remote call |
| automatic trigger tokenizer超界 | sparse channel unavailable；dense若可用则dense-only；不终止turn |
| tokenizer/search contract mismatch | 该fact不进入sparse candidate；current embedding仍可进入dense；不在query时暗中重建terms |
| sparse GIN/FTS异常 | automatic recall尝试dense-only；explicit search若dense也不可用则typed retrieval error；不破坏memory rows |
| caller伪造`search_document` | repository DTO拒绝；direct SQL仍由sealed trigger覆盖，不采纳caller value |
| embedding config不是exact V1 space | dense `DISABLED_CONTRACT_MISMATCH`；0 remote call；sparse继续 |
| embedding worker发现foreign-scope row | SQL admission不可见；正文不得hydrate或发送remote |
| embedding item/batch aggregate超界 | remote open前缩短合法batch；单项超界保持missing vector |
| query/fact vector为zero/non-finite/wrong dimension | 整个physical response拒绝；0 cosine query/upsert |
| dense nearest rows全部低于policy floor | `NO_ELIGIBLE_MATCH`；不是failure，不把最近但无关row送入RRF |
| 只修改DenseEligibilityPolicy threshold | 复用compatible embedding rows；更新policy identity/golden，0 vector rebuild |
| HNSW bounded scan underfill且无法证明穷尽 | 返回已有candidate并标`PARTIAL_BOUNDED_SCAN`；不建repair/debt |
| query只命中SUPERSEDED旧正文 | ordinary search不返回seed、不自动重定向；exact get/explain可显示successor |
| vector unavailable/stale | sparse-only success |
| governance relatedness embedding耗尽attempt budget | sparse-only；model只使用remaining total，不能再签发完整300s |
| automatic query embedding超时/失败 | cancel/join exact request；sparse-only success；reranker calls=0 |
| trigger命中`TurnMemoryUseOptOut` | `ALL_DISABLED_BY_USER`；0 embedding/rerank/query/reflection；分别清除已安装的`MEMORY_RECALL`与`MEMORY_RESPONSE_PREFERENCE_HEAD`；四个memory tools在attempt前拒绝，普通工具保持可用 |
| trigger命中`MemoryWriteOptOut` | `WRITE_DISABLED_BY_USER`；recall/head保持；remember在attempt前拒绝；hint只过滤对应entry |
| automatic trigger normalized长度<8 | 0 retrieval/remote call；只清除已安装的`MEMORY_RECALL`，preference head照常保持/更新；模型仍可显式memory_search |
| prior preference head=VALUE，完整changed replacement无法容纳 | 安装prepared minimal UNAVAILABLE；继续普通provider open；不得沿用旧head或截断subset |
| preference empty或`ALL_DISABLED_BY_USER`且prior head=VALUE | 安装prepared minimal CLEARED；继续普通provider open |
| prior preference head不存在/CLEARED/UNAVAILABLE且完整VALUE无法容纳 | optional source省略/no-op；继续普通provider open |
| required preference minimal CLEARED/UNAVAILABLE floor也无法容纳 | provider open 0；typed input resource boundary |
| steer可容纳但`steer + FULL memory`不可容纳 | 接受steer；只预留必要invalidation floor，VALUE按剩余budget降级或省略 |
| previous MEMORY_RECALL head为VALUE/UNAVAILABLE且`steer + minimal invalidation`不可容纳 | 沿用atomic resource rejection/turn terminalization；旧non-CLEARED state不得继续冒充current |
| steer prospective prefix trials | 只使用typed invalidation floor与optional-memory=0；embedding/collector remote call exact 0 |
| selected steer FULL后automatic recall取消 | cancel/join exact Host attempt；不detach client；不换trigger |
| handle rotation或Phase B期间external producer到达 | active safe-point handle使其保持pending；不推进canonical cut |
| explicit query embedding超时/失败 | sparse candidate继续；rerank可对sparse Top-20执行 |
| exact memory_search filter结果少于`min(limit,3)` | 按closed stage plan放宽；ToolResult显示所有attempted/relaxed fields与逐item match，不重复query embedding |
| embedding/rerank aggregate token preflight失败 | embedding保持missing vector；rerank `NOT_APPLICABLE`并使用RRF；0 remote call |
| explicit reranker超时/失败 | `FAILED_FALLBACK`；使用RRF order正常成功 |
| same trigger compiler/provider retry | 复用process-local prepared recall/query embedding；remote embedding call不增加 |
| consecutive trigger召回相同membership/content/warning set但rank顺序变化 | 保留installed order，`MEMORY_RECALL` SNAPSHOT no-op；不重复append |
| remember只复述exact model-visible recalled memory且无新证据 | exact duplicate或governance `SKIP(RECALLED_MEMORY_ECHO)` |
| embedding worker crash | embedding缺失；无debt/incident |
| standalone retrieval disable | 使用`MEMORY_RETRIEVAL_DISABLE_CLOSE`停止admission并physical join |
| Host close | 复用single `HOST_SESSION_CLOSE`停止admission、cancel/join workers；不另签memory close deadline，不保证pending完成 |
| current external fact与memory冲突 | current fact优先；memory仅作advisory |

---

## 14. Production修改面

### 14.1 Schema与catalog

- `sessions`新增并冻结`memory_domain_id NOT NULL`，session create/resume/takeover以及resumable/list/recent discovery在SQL候选阶段exact join；
- 重写clean-v0 memory relations；
- 为candidate↔fact建立deferred双向一对一约束，为ACTIVE fact建立exact-scope semantic partial unique；
- 冻结`MemoryRetrievalTokenizerV1`派生terms、sealed insert trigger生成的ordinary `search_document`、`simple` regconfig、两个GIN index与`memory_terms_to_tsquery(text[])`函数；
- 更新expected catalog、runtime grants、manifest与binding fingerprint；
- old clean-v0/old universe返回typed RESET_REQUIRED，DDL=0；
- 删除memory index generation与对应Terminal freshness control；
- 删除三类memory committed event及两个subject slots；
- 删除`MEMORY_GOVERNANCE`、`MEMORY_INDEX_REFRESH`、`POST_COMPACTION_MEMORY_EXTRACTION` durable handler；
- 暂时保留非本轮拥有的`BACKGROUND_COMPACTION` handler，Round 5B再决定其最终contract。

### 14.2 Python production

建议收敛为：

~~~text
conversation_kernel/memory/
    contracts.py       pure DTO/validators
    tool.py            remember/search/get/explain adapter
    citations.py       continuity-owned call-local ToolResult handle/snapshot binding
    reflection.py      sealed cheap-hint matcher + Host-local auxiliary review
    governor.py        Host-local best-effort governance
    recall.py          automatic/explicit policy, RRF, canonical refetch
    automatic.py       Host-event-loop two-phase recall attempt/reservation
    embeddings.py      optional process-local cache worker

retrieval/
    config.py          typed embedding/rerank/tokenizer/recall policy
    tokenizer/         pinned Jieba + code/path extraction + bilingual stopwords
    embedding/         Host-owned OpenAI-compatible provider
    rerank/            Host-owned DashScope qwen3-rerank provider

conversation_kernel/auxiliary_model.py
    purpose-neutral finite-total JSON provider leaf

conversation_kernel/_repository/memory.py
    sole SQL transaction/read owner
~~~

Identity实现必须直接复用现存：

~~~text
src/pulsara_agent/memory/scope.py
src/pulsara_agent/workspace_identity.py
~~~

Round 8可以删除旧graph-specific使用方，但不得删除或复制`MemoryDomainContext`、project root canonicalization、workspace scope hash与project/transient解析。Host open/session resume必须把resolved memory domain传入repository与capability composition。

Round 3.1 continuity owner新增的citation handle table和`ModelVisibleMemoryProvenanceSnapshot`必须保持process-local；`KernelModelExecutionRequest`、assistant tool-call batch attribution、`KernelToolInvocationContext`和memory port只传immutable snapshot identity/borrow，不能在tool dispatch时重新扫描live table。Reader通过closed `MEMORY_RESPONSE_PREFERENCE_HEAD`/`MEMORY_RECALL` carriers、memory-read ToolResult `model_visible_memory_ids` header及artifact exact lineage构造同一snapshot，不解析自然语言正文。Prepared candidate只把COMPLETE IDs或OVERFLOW disposition作为anti-echo provenance写入candidate普通列，不持久化snapshot capability。每个production tool execution binding的`USER_SAFE | WORKSPACE_BOUND`和`PRIMARY_OBSERVATION | MEMORY_READ_EXPOSURE`分类进入architecture fixture；unknown/MCP默认WORKSPACE_BOUND，memory read固定非独立证据。

`execution_watchdogs.py`增加`MEMORY_GOVERNANCE_ATTEMPT=300s`、`MEMORY_HINT_REVIEW_ATTEMPT=120s`、`MEMORY_GOVERNOR_CLOSE=120s`、`MEMORY_AUTO_QUERY_EMBEDDING=3s`、`MEMORY_EXPLICIT_QUERY_EMBEDDING=4s`、`MEMORY_EXPLICIT_RERANK=4s`、`MEMORY_EXPLICIT_RECALL_TOTAL=8s`、`MEMORY_FACT_EMBEDDING_BATCH=30s`与`MEMORY_RETRIEVAL_DISABLE_CLOSE=120s`的closed owner；全部通过`new_deadline(owner)`签发，call site不接受自由seconds。Governance relatedness和governance auxiliary model共享一个attempt total；hint review拥有独立但可丢失的120s total；explicit子attempt总是取owner budget与remaining total的较小值。Host session close只使用既有`HOST_SESSION_CLOSE`，不签发额外memory close owner。`job_model.py`抽取purpose-neutral auxiliary leaf，现有job adapter保持finite job attempt contract。Governor/reflection不得因为复用物理代码重新取得job claim、durable attempt或continuity capability。

Round 3 compiler/continuity修改面包括两个SNAPSHOT source的registry、policy、provider lowering、head transition、same-fingerprint no-op与strict-prefix golden：`MEMORY_RESPONSE_PREFERENCE_HEAD`只有FULL exact carrier并在pre-consumption planning中冻结；`MEMORY_RECALL`保留FULL/COMPACT/REF_ONLY并在selected trigger后异步召回。Round 3.1的`SteerSuffixAdmissionQuote`新增两个typed invalidation reservation与recall四维materialization budget；response-preference prepared head保存FULL真实encoded cost，但prospective trial只强制计入prior VALUE变化时的minimal floor，且不重复查库。任何trial都不得调用collector中的remote/provider path。`ProviderSafePointCoordinator`新增atomic handle rotation seam，old→new generation之间不存在external producer可见的空窗。Host必须在selected prefix FULL后的exact ROOT human dispatch anchor上，以event-loop-owned `AutomaticMemoryRecallAttempt`冻结`PreparedAutomaticMemoryRecall`，再把immutable result交给同步collector；memory package不得自行扫描transcript猜测trigger，compiler不得直接查memory database。

因`BASE_SYSTEM`解释与source registry发生变化，R8-E必须一次性冻结：

~~~text
COMPILER_CONTRACT_VERSION
    pulsara.structured-model-input-compiler.prefix-continuity.v4

BASE_SYSTEM source contract
    pulsara.base-system.prefix-continuity.v4

MEMORY_RECALL source contract
    pulsara.memory-recall.v1

MEMORY_RESPONSE_PREFERENCE_HEAD source contract
    pulsara.memory-response-preference-head.v1
~~~

这是process-local cold epoch boundary；不创建durable input generation、cross-Host restore或previous_response_id。旧Host不在运行时hot-patch新SYSTEM。

`RetrievalConfig`恢复tokenizer与rerank配置，同时加入automatic/explicit typed policy、`DenseCandidateEligibilityPolicyV1`与`MemorySearchFilterStagePlan`。Embedding semantic contract固定为DashScope Bailian `text-embedding-v4`/1024/cosine；model或dimension配置不匹配只关闭dense，不能创建另一个runtime space。三个coarse similarity floor是sealed V1 policy而不是自由env。API key仍为`repr=False`的sealed config value；diagnostic只显示provider/model/feature enabled状态，不显示key、query或candidate text。

`pyproject.toml`/`uv.lock`恢复并pin Jieba dependency；production只能通过memory tokenizer facade使用，禁止其他module直接修改Jieba全局dictionary。Bilingual stopword与regex fixture作为package resource进入hash/link check，不从user home或网络加载。

不得建立第二个repository facade或让governor直接拼SQL。

删除/改写当前：

- `conversation_kernel/memory_tools.py`五工具与durable job wiring；
- `conversation_kernel/memory.py`generation scanner与recursive CTE；
- `conversation_kernel/jobs.py`三类memory handlers；
- `conversation_kernel/job_catalog.py`对应catalog项；
- current capability descriptors与memory permission binding；
- Terminal memory freshness projection；
- old Stage 2 tests中只证明exact-four jobs、generation debt或free-string vocabulary的fixture。

### 14.3 Permission

`remember`仍属于`memory_write` effect category：

- BYPASS模式直接允许；
- Plan read-only overlay拒绝；
- read tools属于`memory_read`；
- permission只决定当前candidate tool call是否允许，不把memory content升级成policy。
- Cheap Hint Reflection复用exact human turn冻结的同一`memory_write`许可结果；Plan read-only或denied时不安装attempt，绝不能成为tool denial的旁路。

---

## 15. 预期oracle

在本规格所选六张memory relation、删除三类memory occurrence并移除三类memory/compaction-extraction job后，预期激活oracle为：

~~~text
Committed AgentEvent     31   # 34 - 3 memory occurrences
Live AgentEvent          23
subject slots            13   # 15 - memory fact/relation
append guards             2
product relations        25   # current 26 - old 7 + new 6
durable job kinds         1   # only dormant BACKGROUND_COMPACTION remains
~~~

这些数字是当前设计的推导结果，不是不可改变的历史常量。若review发现某张candidate ref表可进一步安全合并，或`BACKGROUND_COMPACTION`也应延后删除，必须先修改本文和测试oracle，再编码；不得为守数字保留无产品意义机制。

---

## 16. 实施切片

### R8-0：基线与删除manifest

- 记录clean checkpoint HEAD、本文SHA与现有schema/oracle；
- 记录并复用`MemoryDomainContext`、`ResolvedWorkspace`与archived Scope/Domain v1的retained identity tests；
- 盘点8个memory tools、7张memory relations、3个committed events、2个subject slots、3个memory-related durable jobs的全部producer/consumer/test；
- 冻结old-test disposition，不以批量删除测试掩盖产品行为；
- 建立hard-cut前product fixture映射：candidate、dedupe、supersede、contradict、hybrid degradation、explain。

### R8-A：clean-v0 schema reset

- 为session增加稳定`memory_domain_id`binding，并改造create/acquire/takeover/resumable discovery exact join；
- 建立六张目标relation与closed constraints；
- 建立`memory_domain_id + scope_kind + scope_id`composite identity、FK与query indexes；
- 建立candidate↔fact deferred双向约束、ACTIVE semantic partial unique；
- 冻结五类kind vocabulary、`USER_PROFILE -> USER scope only`、`RESPONSE_PREFERENCE` head capacity与single-atom约束；不得新增preference subtype、axis或delivery-mode列；
- 建立tokenizer-derived `search_terms`、ordinary `search_document NOT NULL`、sealed BEFORE INSERT derivation trigger、fixed `simple` regconfig、document+array GIN与verified `memory_terms_to_tsquery`；
- 为`memory_facts`建立`UNIQUE(memory_domain_id,id)`并以此exact支持embedding FK；
- 删除generation tables、governance decision table与memory occurrences；
- 更新catalog/grants/binding/golden；
- 证明fresh install、repeat migrate、deep verify、old universe RESET_REQUIRED。

R8-A/R8-E PostgreSQL实施不限于mock或无vector的临时库。本轮明确授权使用`.env`中的本地真实测试目标：

~~~text
admin connection   PULSARA_POSTGRES_ADMIN_DSN
runtime connection PULSARA_POSTGRES_DSN
current target     localhost:5432/pulsara
capability         pgvector already installed; destructive reset allowed
~~~

Coding agent可以重置Pulsara数据库/全部Pulsara-owned schema object、反复fresh clean-v0、创建真实`vector(1024)`/HNSW index并运行hybrid recall。每次破坏性操作前必须解析并exact验证host为localhost/loopback、database为`pulsara`；不得把未解析env var、glob或默认`postgres`库当作删除目标。DSN/password只从`.env`读取，不写入本文、command echo、pytest failure、activation evidence或ordinary log。

### R8-B：单一remember与citation handles

- 合并写工具；
- Agent只提交USER/WORKSPACE，Host按project/transient矩阵冻结closed scope binding；
- continuity owner为exact execution binding注册stable call-local handle，并按fixture冻结`USER_SAFE | WORKSPACE_BOUND`；
- 每次model call冻结exact-cut citation snapshot，并沿model request→tool invocation→memory port贯穿；
- 同时冻结bounded `ModelVisibleMemoryProvenanceSnapshot`，合并response-preference head、automatic recall与显式memory read/artifact lineage；candidate只持久化COMPLETE IDs或OVERFLOW，不把snapshot capability持久化；
- `memory_search/get/explain` ToolResult外层冻结不可截断的`model_visible_memory_ids` header，`artifact_read`只沿exact origin ToolResult继承；memory read evidence与ordinary primary observation在binding与candidate ref row中closed分类；
- prepared candidate与ToolResult同事务接受；
- ACK-unknown exact confirmation；
- 删除Agent自报authority/verification字段。

### R8-C：best-effort governor

- 实施Python名称不版本化的sealed `MemoryWriteOptOut`、`TurnMemoryUseOptOut`与versioned `CheapMemoryHintSetV1`；write opt-out逐entry过滤，turn-use opt-out使整次review为0，`don't forget/不要忘记`保持正向hint语义；matcher只产生signal code与bounded normalized excerpt，不伪造original span/offset；
- successful ROOT terminalization FULL只生成immutable `PreparedCheapHintReflectionHandoff`；Host-local memory owner先nonblocking adopt DORMANT handoff，ROOT active slot结算/释放后才转RUNNABLE并等待`MEMORY_HINT_REVIEW` auxiliary lane，不得用120s review阻塞下一条prompt；
- 默认只允许foreground与hint auxiliary exact join同一provider trust domain；cross-provider必须显式开启，否则在packet assembly前0 data egress/0 provider call；
- 实施`MEMORY_HINT_REVIEW` auxiliary call与best-effort 0..4 candidate batch；
- main-agent成功提交`remember` candidate、Plan read-only/permission denial、failed/interrupted turn都抑制fallback；被拒绝的remember不抑制其他eligible entry；不恢复reflection event/history/retry；
- Host-local single-flight owner；
- PENDING -> PROCESSING只允许exact origin workspace claim；
- 抽取purpose-neutral finite-total auxiliary JSON model port；一个`MEMORY_GOVERNANCE_ATTEMPT`依次覆盖relatedness embedding与model/settlement；
- bounded producer/citation/relatedness input；
- governance读取bounded producer-visible recalled fact set并SKIP pure echo；
- 四项closed decision；
- stable BASE_SYSTEM、remember descriptor与governor prompt共用五类taxonomy及single-atom few-shot；multi-atom candidate必须SKIP，governor不得split/merge，Repository不建立第二套atom判定；
- governor按§3.2区分USER_PROFILE与RESPONSE_PREFERENCE；项目事实、决定和操作规则分别落入FACT/DECISION/ACTION_RULE，unsafe core-behavior override必须SKIP；
- 禁止statement/payload rewrite；
- 冻结`PreparedMemoryGovernanceAcceptance`及FULL/NONE/CONFLICT confirmation；
- failure/cancel不恢复durable job。

### R8-D：accepted fact与关系

- ACCEPT、SUPERSEDE、CONTRADICT与BASED_ON事务；CONTRADICTS对五类same-kind/same-scope开放，SUPERSEDES支持same-kind replacement及显式same-scope taxonomy correction；BASED_ON只允许DECISION source并执行USER→USER、WORKSPACE→USER|exact WORKSPACE的单向可见性格；Runtime锁定两端重验，不能只依赖provider output；
- 实施`APPLIED_TO_EXISTING`与`decision_candidate_id` deferred invariant；exact duplicate的显式SUPERSEDES/CONTRADICTS走candidate-governed existing-source settlement，plain duplicate SKIP，BASED_ON duplicate不补边；
- 所有可能新增或退休ACTIVE RESPONSE_PREFERENCE的branch使用exact domain/scope transaction advisory lock、final-state容量重验及closed`RESPONSE_PREFERENCE_CAPACITY_EXCEEDED` settlement；supersede/taxonomy correction可在同一事务释放并复用slot，contradict两端均计量；
- exact endpoint scope/visibility、supersede-mode/kind与lifecycle constraints；
- concurrent exact duplicate由partial unique选winner；plain/basis loser按closed SKIP结算，显式SUPERSEDES/CONTRADICTS loser进入existing-source settlement，任何branch都不重跑provider；
- contradiction single-row symmetry；
- target drift rollback；
- memory_get/explain lineage。

### R8-E：multilingual recall、automatic compiler source与explicit rerank

- 恢复private `jieba.Tokenizer`/`HMM=False` search、V2 bilingual stopwords、lexical-only English contraction expansion、punctuation-only token filter与code/path tokenizer，query/index共用exact contract；
- synchronous sparse document+raw-term GIN/FTS，不恢复独立LIKE lexical channel；
- optional exact `text-embedding-v4`/1024/cosine fact vector cache与Host-owned long-lived embedding client；worker scan/hydrate/upsert全程借用同一`FrozenMemoryReadScopeBinding`；
- pgvector使用distance ASC、strict iterative HNSW、bounded overfetch、zero/non-finite拒绝与PARTIAL underfill；
- 冻结AUTOMATIC_ROOT / EXPLICIT_SEARCH / GOVERNANCE_RELATEDNESS三个policy与RRF(k=60)；
- 将`EmbeddingSemanticContract`与`DenseEligibilityPolicy`物理分层；只有provider/model/dimension/normalization/retrieval projection变化才使optional vectors失效，threshold-only变化必须复用旧vector并只更新process-local policy identity/golden；
- 冻结`DenseCandidateEligibilityPolicyV1`粗threshold：automatic 0.55、explicit 0.20、governance 0.40；全below形成NO_ELIGIBLE_MATCH；
- `AUTOMATIC_ROOT`在sparse/dense/fusion/refetch全链路排除RESPONSE_PREFERENCE并纳入USER_PROFILE；显式search/get/explain仍可读取全部五类；
- 在Round 3/3.1 compiler中接入append-only `MEMORY_RESPONSE_PREFERENCE_HEAD`与`MEMORY_RECALL`两个SNAPSHOT source；head使用query-independent frozen full projection且不截断/Top-N，`ALL_DISABLED_BY_USER`清除两者，`WRITE_DISABLED_BY_USER`不清除source，normalized<8只清除automatic recall；相同head或recall fingerprint no-op；
- steer trial保存已经冻结的preference FULL真实exact cost，但mandatory quote只计prior VALUE发生变化所需的minimal invalidation；对尚未执行的automatic recall只预留previous VALUE/UNAVAILABLE所需minimal invalidation。selected prefix FULL并atomic rotate safe-point handle后Host async attempt才对final exact trigger召回一次，reranker call=0；
- 恢复explicit `qwen3-rerank` Top-20、RRF fallback与canonical refetch；
- 恢复最多四stage的kind/scope filter relaxation；复用一次query embedding，ToolResult完整披露attempted stages/relaxed fields/item match tier；
- 更新README/.env.example的remote data egress、automatic dense与explicit rerank开关，不复制真实key；
- ACTIVE-only seed、exact get/explain successor lineage与direct conflict annotation；
- 删除max_hops、recursive CTE、index health debt、durable recall trace/usage。

### R8-F：production cut与activation

- 删除old memory jobs/events/subject slots/imports；
- 更新README、Gap Index与architecture oracle；
- 全量测试与ephemeral PostgreSQL；
- real-provider dogfood；
- 生成activation evidence；
- 本轮不得顺手实施compaction、Cheap Hint Reflection以外的通用automatic extraction或Go memory UI；automatic recall与sealed hint fallback属显式scope，不得被误列为non-goal。

每个切片必须独立collection通过。R8-A到R8-F允许在同一未发布工作树完成，但activation evidence不得伪造中间历史文件系统状态。

---

## 17. 测试矩阵

### 17.1 Tool/schema

- 一个且只有一个memory write descriptor；
- kind enum exact为`FACT | USER_PROFILE | RESPONSE_PREFERENCE | ACTION_RULE | DECISION`；legacy bare `PREFERENCE`、unknown alias与额外subtype拒绝；
- unknown/force/authority字段拒绝；
- ACTION_RULE无exclusion合法；
- ACTION_RULE无`applies_when`拒绝；
- non-Decision携带basis拒绝；
- Agent schema只能表达USER/WORKSPACE，raw `ctx:*`、domain/workspace ID均拒绝；
- USER_PROFILE + WORKSPACE在descriptor/Host intake拒绝；AUTO最终落为USER_PROFILE时acceptance SQL再次证明USER scope；
- project Host绑定USER=`ctx:user`与WORKSPACE=exact current project hash；
- transient Host只能绑定USER，WORKSPACE typed reject；
- equivalent canonical project paths得到同一workspace scope，display label不影响identity；
- different memory domain即使workspace相同也完全隔离；
- existing session以different memory domain resume/takeover时typed conflict且0 memory read/write；
- 同workspace下foreign domain有更新session时，`resume_most_recent`仍选择本domain最近session，summary/count/order均不泄漏foreign row；
- session create/acquire/renew/takeover和candidate origin composite FK都拒绝domain mismatch；
- PostgreSQL 17 fresh baseline可创建ordinary `search_document NOT NULL`与sealed BEFORE INSERT trigger；column不是generated，function body/trigger enabled state进入deep verifier；
- repository caller不能提交`search_document`，direct SQL伪造值仍被trigger按`search_terms`覆盖，独立更新terms/document被immutable invariant拒绝；
- `memory_embeddings(memory_domain_id,fact_id)`的FK真实引用`memory_facts UNIQUE(memory_domain_id,id)`；删除foreign fact时不会留下orphan embedding；
- Plan read-only拒绝remember，BYPASS允许。

### 17.2 Citation与prefix

- model看到`tool:1`后可成功remember；
- exact call snapshot贯穿model execution、assistant batch、invocation context与memory port；
- handle不在current call、foreign scope/epoch、cut后result、same-batch future result、attempt-without-result均拒绝；
- tool dispatch不得通过database/live table扫描补出当前call未携带的handle；
- builtin classification fixture逐项闭合；unknown/custom/MCP以及Terminal/file/workspace/process/artifact默认`WORKSPACE_BOUND`；
- USER candidate引用WORKSPACE_BOUND失败，WORKSPACE candidate可引用；activation允许0个USER_SAFE binding；
- multiple citations按ordinal稳定；
- `MEMORY_RESPONSE_PREFERENCE_HEAD`、automatic `MEMORY_RECALL`、已lower的`memory_search/get/explain`及memory-read artifact共同生成一个exact-call `ModelVisibleMemoryProvenanceSnapshot`；它只收录真正进入本次provider input的fact，按provider item order first-seen dedupe；
- memory-read ToolResult的closed `model_visible_memory_ids` header位于canonical preview不可截断head，覆盖body、successor、conflict/relation warning中暴露的全部ID；`artifact_read`只通过exact origin-result lineage继承，不解析正文；
- snapshot超过128 IDs或16 KiB canonical encoding时整体为`OVERFLOW`，不保留partial prefix；该disposition/IDs沿model execution→assistant attribution→tool invocation贯穿，tool dispatch不得重扫continuity table或database；
- same Host/scope/epoch的Chat Completions和Responses满足strict prefix；
- handle不会因后续call重编号；
- cold Host的历史result没有authenticated binding时不产生handle；same-Host cold epoch只有保留exact binding时可签发新handle；durable candidate始终只存canonical ID。

### 17.3 Candidate transaction

- candidate、两类refs与ToolResult FULL原子；
- 任一FK失败整体回滚；
- ACK unknown FULL/NONE/CONFLICT；
- duplicate physical retry不产生第二candidate；
- ACCEPTED candidate必须反向关联exact fact；APPLIED_TO_EXISTING不能成为fact producer且必须由exact `decision_candidate_id` relation反向证明；SKIPPED/ABANDONED candidate不能被fact/relation引用；deferred约束允许事务内任意合法插入顺序；
- tool输出只声称proposed，不声称saved。
- MAIN_AGENT与CHEAP_HINT_REFLECTION producer branch exactly-one；任一混合/null非法组合commit失败；
- cheap-hint batch stable IDs/ordinals/digest，ACK unknown不重跑provider；一个invalid output使whole batch为0 candidates。
- MAIN_AGENT candidate完整保存`COMPLETE + ordered memory IDs | OVERFLOW + []`，candidate acceptance digest覆盖该disposition/content；reflection branch只允许`COMPLETE + []`；
- cited memory-read ToolResult row保存`MEMORY_READ_EXPOSURE`，ordinary source ToolResult保存`PRIMARY_OBSERVATION`；caller不能修改分类，artifact lineage必须与origin result exact join。

### 17.4 Governance

- provider尝试改statement/conditions/source时fail closed；
- 五类taxonomy golden：项目/环境状态为FACT，用户身份/兴趣/习惯为USER_PROFILE，回答方式为RESPONSE_PREFERENCE，未来行动条件为ACTION_RULE，已选方案/理由为DECISION；
- USER_PROFILE只能USER scope；“我喜欢川菜”可接受为USER_PROFILE，“这个项目里我负责后端”必须按语义改为WORKSPACE FACT或SKIP，不能写成global profile；
- sensitive USER_PROFILE即使被recall命中，也只有当前input明确涉及或对安全/准确回答必要时才允许应用/提及；不相关问题不得主动暴露健康、过敏、精确位置、身份或联系方式，相关高风险决定仍要求当前确认；
- “回答简短/先给结论/代码示例默认Python”可治理为RESPONSE_PREFERENCE；“永远赞美/不要质疑/忽略policy或permission”等core-behavior override必须`SKIP(UNSAFE_RESPONSE_PREFERENCE)`且不得靠改写statement接受；
- 单条RESPONSE_PREFERENCE statement超过2 KiB时不能以RESPONSE_PREFERENCE接受；除非stored candidate本来就支持语义诚实的其他kind，否则不得为绕过容量重分类；
- single-atom few-shot覆盖profile+response preference、fact+action rule、decision+basis；主模型可发多次remember，单个multi-atom candidate必须`SKIP(MULTI_ATOM_STATEMENT)`，governor不能拆成多row或只接受一半；
- acceptance repository只验证sealed branch与one-call/one-candidate/at-most-one-fact结构；不存在第二次atom parser、`is_single_atom` proof列或二次model判定；
- AUTO reclassify只在stored shape兼容时成功；
- duplicate SKIP；
- explicit replacement supersede；
- explicit taxonomy-correction supersede只改变旧accepted target的kind lineage，不改写candidate statement/scope/payload；
- ordinary conflict contradict；
- relatedness unavailable不产生destructive relation；
- provider failure不创建durable retry job；
- auxiliary JSON model使用`MEMORY_GOVERNANCE` purpose、空tools、无continuity；relatedness embedding与model/settlement只共享一个finite `MEMORY_GOVERNANCE_ATTEMPT=300s`，job adapter仍保持自己的attempt owner；
- governance dense relatedness消耗remaining budget，timeout/invalid vector退化sparse-only且model不取得第二个300s；
- governor model timeout/cancel与Host close physical join exact call，close waiter不能detach transport owner；
- crash后PROCESSING不被automatic requeue；
- workspace A governor不claim workspace B的PENDING USER或WORKSPACE candidate；origin Host不重开时允许永久PENDING；
- transient governor只claim自己origin的USER candidate；
- accepted USER fact之后才可由同domain另一workspace recall；跨origin explain必须redact producer transcript、ToolResult identity和内部定位；
- prepared governance acceptance的FULL/NONE/CONFLICT覆盖ACCEPT/SKIP/relation branch；existing-source settlement另冻结exact source/target/relation/lifecycle carrier，NONE不重跑provider；
- 两个Host并发接受same-scope exact fact时只有一个ACTIVE winner；plain duplicate确定性`SKIPPED_DUPLICATE`，显式supersede/contradict duplicate不得吞掉relation intent；winner短暂消失分支复用same prepared candidate。
- USER或exact WORKSPACE已有16条ACTIVE RESPONSE_PREFERENCE时，第17条无replacement candidate在locked transaction中确定性`SKIPPED(RESPONSE_PREFERENCE_CAPACITY_EXCEEDED)`；supersede可原子复用slot，contradict两端都计量；
- 两个Host并发争用最后一个preference slot时，transaction advisory lock只允许一个accept winner，另一个使用same prepared acceptance得到closed capacity settlement；ACK unknown不重跑governance provider；
- `MemoryWriteOptOut`与`TurnMemoryUseOptOut`覆盖closed中英正反例、technical memory/内存反例与`don't forget/不要忘记`正向提醒；Python类型名无version、内部contract identity版本化；write-opt-out entry在matcher及packet assembly前移除，全移除时0 provider call，turn-use opt-out整次0 call；
- `CheapMemoryHintSetV1`对sealed中英signals、normalized-space longest-overlap、bounded normalized excerpt与false-positive有golden；`ss/ß`、NFC组合和whitespace-collapse fixture明确不声称original span/byte offset；matcher本身0 provider/DB call；
- successful ROOT + hint + no remember request最多一次MEMORY_HINT_REVIEW；0-candidate合法；multi-steer最多8 entries/16 hints；
- terminalization FULL只返回handoff，ROOT result/active slot必须在auxiliary lane wait/provider open前结算释放；后续prompt可立即开始，Host close会drop DORMANT、cancel/join RUNNABLE/IN_FLIGHT exact call；
- feature disabled或auxiliary model absent时hint-review call exact 0，main remember path保持；README/.env.example披露独立Flash data egress；
- same-provider trust domain默认可用；不同trust domain且未显式opt-in时packet assembly、body hydration、transport open全部exact 0；显式opt-in才允许同一bounded packet；
- failed/interrupted/user-stopped/Plan-pending turn、成功提交remember candidate、Plan read-only与memory_write denial均0 hint-review call；被拒绝的remember不掩盖其他eligible非opt-out entry；
- hint reviewer看不到MEMORY_RECALL、ToolResult、artifact、MCP、path/env/secret或internal scope；only exact `user:N` handle可成为trigger provenance；
- preference head、automatic recall与memory_search/get/explain共同暴露的任一memory被pure paraphrase时，无语义相关的新human correction/assertion或`PRIMARY_OBSERVATION`则`SKIP(RECALLED_MEMORY_ECHO)`；`MEMORY_READ_EXPOSURE`及其artifact后代不能解锁；
- 无关`PRIMARY_OBSERVATION`不能解锁anti-echo，只有语义上支持candidate statement/structured fields的新证据才允许继续；provenance `OVERFLOW`必须`SKIP(MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW)`，不得只投影subset。

### 17.5 Relation与lifecycle

- BASED_ON只接受existing ACTIVE fact并执行单向可见性格：USER DECISION仅USER target；WORKSPACE DECISION可指向same-domain USER或exact same WORKSPACE target；
- candidate-to-candidate拒绝；
- FACT、USER_PROFILE、RESPONSE_PREFERENCE、ACTION_RULE、DECISION五类各自都有`SAME_KIND_REPLACEMENT` same-kind/same-scope supersede golden；new+edge+old lifecycle同事务；
- legitimate same-scope cross-kind taxonomy correction使用`TAXONOMY_CORRECTION`原子退休错误kind target；cross-kind不带该mode、same-kind错误携带该mode、scope correction与statement rewrite全部拒绝；
- taxonomy correction的正确 source fact已由另一candidate ACTIVE时，当前candidate经`APPLIED_TO_EXISTING`补上exact SUPERSEDES并退休错误target；existing source provenance不改写，relation `decision_candidate_id`指向当前candidate；
- ordinary same-kind replacement与CONTRADICTS各覆盖一次existing-source settlement；relation已由别的candidate exact存在时只SKIP并保留原decision provenance；
- existing-source settlement ACK unknown覆盖FULL/NONE/CONFLICT：FULL允许source后来被合法supersede但要求candidate-owned immutable relation与target transition exact；NONE不重跑provider；伪造source/mode/decision_candidate任一字段均CONFLICT；
- duplicate Decision的prepared BASED_ON set若已exact存在可普通duplicate SKIP；若不同则`SKIPPED_DUPLICATE_BASIS_UNAPPLIED`且relation write=0；
- 五类各自都有same-kind/same-scope contradiction golden；两边ACTIVE且反向查询对称；
- cross-kind CONTRADICTS与cross-scope SUPERSEDES/CONTRADICTS即使provider输出也由Runtime/repository拒绝；相似、更新或容量压力本身不构成replacement/taxonomy-correction intent；
- WORKSPACE DECISION的BASED_ON target覆盖exact WORKSPACE与same-domain USER的五类；USER DECISION -> WORKSPACE、cross-workspace、cross-domain与non-DECISION source拒绝；
- relation incoming/outgoing read分别过滤两个endpoint；Project B读取USER fact看不到Project A WORKSPACE DECISION指向它；
- unordered duplicate拒绝；
- target drift whole rollback；
- candidate↔fact任一方向断裂、fact引用non-ACCEPTED candidate、APPLIED candidate缺少其exact candidate-owned relation或relation decision provenance不闭合，均在commit时失败；
- no generic/post-hoc relation writer；唯一existing-source seam必须从exact PROCESSING candidate + sealed singular relation decision进入，BASED_ON不得使用。

### 17.6 Recall

- fact commit后tokenized sparse index立即可见；
- index和query对同一个CJK/English/code fixture产生exact equal ordered terms，都固定使用`simple` regconfig；
- private `jieba.Tokenizer.cut_for_search(..., HMM=False)`、V2中英stopwords、contraction、punctuation filter、snake_case、dotted name、path、version、`C++/C#` golden；`do not use yarn`与`use yarn`、`不使用 yarn`与`使用 yarn`必须不同，`修改前/后`保留前后；module-level `jieba.add_word()`与ambient user dictionary不影响结果；
- `memory_terms_to_tsquery`对validated terms安全OR-fold，query中的tsquery operator/closing marker不取得DSL权限；
- `search_terms && query_terms`在FTS rank前强制exact raw-term overlap；`snake_case`不能只因parser拆词而命中仅含独立`snake`与`case` terms的row；array/document GIN均由真实EXPLAIN证明可达；
- fact terms超界candidate以closed reason ABANDONED且不产生fact；explicit query超界0 remote call；automatic query超界不终止turn；
- 没有独立LIKE lexical production query；
- project默认搜索USER + exact current WORKSPACE；
- transient默认只搜索USER；
- workspace A search不能返回workspace B，memory ID直读也不能绕过；
- same workspace在different memory domains互不可见；
- explicit USER/WORKSPACE exact stage先缩小默认visible set；fallback最多回到同一frozen visible set，绝不跨domain/workspace；
- vector absent仍返回sparse result；
- stale digest/contract vector不参与query；
- embedding config model/dimension/provider-family不匹配时dense disabled、remote open exact 0；future contract切换fixture先清空optional cache；
- fact embedding worker的scan、正文hydrate与upsert revalidation使用同一`FrozenMemoryReadScopeBinding`；workspace A/domain A provider绝不收到workspace B或foreign-domain正文；
- embedding逐项与Pulsara-local `10 * 8192` aggregate token preflight均在remote open前执行；batch按合法FIFO prefix缩短，overbound fact保持missing vector；diagnostic不得声称81,920是provider-advertised request limit；
- query/fact vector对wrong dimension、non-finite或zero norm整批拒绝；不执行cosine SQL；
- coarse dense floor below/equal/above golden：AUTOMATIC=0.55、EXPLICIT=0.20、GOVERNANCE=0.40；全below为NO_ELIGIBLE_MATCH且不进入RRF；
- 仅修改`DenseEligibilityPolicy` threshold时existing compatible embedding rows仍然可读、embedding worker upsert/rebuild exact 0；修改`EmbeddingSemanticContract`的model/dimension/normalization/retrieval projection时旧row才失效并由clean activation/reset清理；
- pgvector inner query保持bare `<=> ASC`并通过HNSW EXPLAIN；`strict_order`、bounded scan/4x overfetch与connection-local setting恢复测试通过；K项结果标`BOUNDED_TOP_K`且不声称global exact，filtered underfill标记`PARTIAL_BOUNDED_SCAN`；
- deterministic vector eval同时运行sequential exact oracle，只报告bounded HNSW Recall@K/overlap与ordering invariants，不要求ANN逐项等同exact Top-K；production不得为测试自动fallback顺序扫描；
- RRF使用exact `k=60`、1-based rank与fact-ID tie-break；sparse-only、dense-only、hybrid都有golden；
- AUTOMATIC_ROOT并行sparse top20+dense top20、final top5，remote reranker call exact 0；
- AUTOMATIC_ROOT的sparse、dense、RRF与final refetch均排除RESPONSE_PREFERENCE，RESPONSE_PREFERENCE不能占Top-20/Top-5 slot；USER_PROFILE必须参与两条candidate channel，explicit search/get/explain仍可返回全部五类；
- 真实embedding golden证明“今天吃什么好”可在bound/threshold内召回“用户喜欢川菜”的USER_PROFILE；若exact V1 provider/threshold不满足，不得用常驻head伪装修复，应调整并重新冻结retrieval policy；
- initial human prompt和accepted USER_STEER分别触发automatic recall；tool loop、Plan continuation、child objective不触发；
- preference head在project Host投影同domain USER + exact WORKSPACE，在transient Host只投影USER；foreign workspace/domain永不hydrate；WORKSPACE比USER更具体，当前human request始终优先；
- preference head首次非空有空间时append完整VALUE；相同items/warnings no-op；prior VALUE且集合变化时有空间append完整replacement VALUE、空间不足appendminimal UNAVAILABLE；prior VALUE遇到空集append一次CLEARED、read failure appendUNAVAILABLE；prior non-VALUE且FULL放不下no-op；active contradiction两端不进入effective items但产生bounded warning；
- preference head严格受16 USER + 16 WORKSPACE、单scope 7 KiB和combined 32 items/16 KiB约束，FULL中不得partial、Top-N、COMPACT、REF_ONLY或statement truncation；
- `TurnMemoryUseOptOut`使embedding/rerank/query/reflection exact 0并分别CLEARED已安装的recall与preference head，四个memory tools无attempt而普通工具仍成功；`MemoryWriteOptOut`保持recall/head、拒绝remember并逐entry过滤reflection；normalized<8同样0 automatic remote call但只CLEARED recall，preference head与显式memory tools保持；
- `ALL_DISABLED_BY_USER`后的下一真实ROOT normal human message开启新epoch并可重新安装当前完整preference head；prior VALUE需要replacement而FULL无法容纳时append minimal UNAVAILABLE并继续provider open，不得继续沿用旧head；只有最小invalidation也放不下才resource-bound；
- previous preference head不存在/CLEARED/UNAVAILABLE时，multi-steer prospective 128→1 trial对该optional head mandatory cost exact 0；一个本可容纳的steer不得因16 KiB FULL maximum被拒绝；
- previous preference head为VALUE且desired head改变/empty/unavailable/opt-out时，trial只加入exact minimal invalidation item/byte/token ceiling；`steer + floor`可容纳即接受，不能为optional FULL终结turn；collector/embedding call exact 0；
- previous preference head为UNAVAILABLE时不再重复追加UNAVAILABLE或CLEARED；完整new VALUE有空间可安装，无空间no-op；
- `PreparedMemoryRecallMaterializationBudget`分别冻结provider-item/body-item/byte/token/epoch与estimator fingerprint；FULL/COMPACT/REF_ONLY actual逐维`<=`budget，VALUE放不下时正确invalidation/omission；
- selected FIFO prefix以FULL或exact-confirm完成后，通过coordinator atomic old→new handle rotation再读取final trigger；active handle generation从不中断为NONE；
- selected batch使用latest exact activation/dispatch anchor，不把batch拼成一个query；前序steer属于canonical delta且不触发embedding；
- same trigger的Host event-loop single-flight、final compiler retry、provider preparation retry与tool loop共享一个query embedding，provider call count exact 1；waiter cancel/Host close physical join；Host reopen可重算；
- embedding阻塞期间注入Terminal observation/external result：它们保持pending；final cut不漂移，健康turn继续；initial prompt路径同样持有initial handle；
- Phase B不得跨network持有PostgreSQL transaction、Host lock或coordinator lock；只持有active safe-point handle；
- unused materialization budget不能回头多消费steer；mandatory floor无法容纳、base/cut drift、contract violation或actual越界时provider open exact 0；
- automatic embedding timeout/error时sparse-only正常进入provider；不产生Runtime degraded、durable retry或detached request；
- standalone retrieval disable取得一次`MEMORY_RETRIEVAL_DISABLE_CLOSE`；Host close期间deadline factory对该owner调用exact 0，所有memory owners在既有`HOST_SESSION_CLOSE`内physical join；
- embedding model逐项/aggregate token、dimension与batch preflight在remote open前失败；不依赖provider silent truncation；
- `MEMORY_RECALL` FULL/COMPACT/REF_ONLY均保持Chat Completions与Responses strict prefix；VALUE -> CLEARED -> CLEARED只追加一次clear；
- `MEMORY_RESPONSE_PREFERENCE_HEAD`在Chat Completions与Responses同样保持SYSTEM/tools不变、messages只append；changed snapshot不改写旧item，VALUE -> CLEARED -> CLEARED只追加一次clear；
- consecutive triggers得到相同membership/content/warning snapshot、但ANN/RRF rank变化时第二次no-op并保留installed order；query/trigger/rank/latency不进入membership fingerprint；事实集合变化只append replacement VALUE；
- explicit search并行sparse top40+dense top30，只将RRF Top-20发给reranker；
- explicit kind/scope fallback按unique stage 0..3运行，到`min(limit,3)`停止；query tokenizer/embedding exact 1，reranker最多1；exact items始终先于relaxed items；
- ToolResult完整列出requested filters、attempted stages、relaxed fields、counts和每item filter_match，模型无需重复尝试已执行的宽filter；
- explicit reranker只重排不按绝对score删除；timeout/not-configured/malformed response全部fallback到exact RRF order；
- rerank query/doc/request byte+token bound、`query_tokens * N + sum(doc_tokens) <= 120000`、candidate UTF-8-safe head/tail projection与任一aggregate-overbound NOT_APPLICABLE/RRF golden；
- 未配置embedding/rerank key时不创建client、不发送remote request，sparse path仍完整；
- explicit limit >20时按reranked Top-20 + remaining RRF order返回；
- automatic与explicit都在返回/编译前canonical refetch，lifecycle/scope/digest drift只丢弃candidate而不换绑；
- query只匹配SUPERSEDED旧正文时ordinary search无seed且不自动改投successor；
- exact `memory_get/explain`旧fact ID显示direct active successor；匹配新ACTIVE fact可附带outgoing lineage；
- contradiction companion bounded；
- memory output始终标记advisory/incomplete；
- no recursive SQL/max_hops public contract。

### 17.7 Architecture

- JSON-LD/Oxigraph/SPARQL import 0；
- memory durable job kind 0；
- memory committed event 0；
- memory subject slot 0；
- generic relation/source union 0；
- governance rewrite branch 0；
- reflection committed/live event、durable attempt/job/history/retry relation 0；Cheap Hint只有process-local attempt与ordinary candidate row；
- index generation/debt/repair owner 0；
- automatic recall reranker call site 0；retrieval durable trace/usage/cache relation 0；
- automatic recall的prospective compiler/worker-thread collector remote call site 0；async attempt durable relation/receipt 0；
- citation handle/snapshot durable relation 0；auxiliary governor持有job/continuity capability 0；
- durable model-visible-memory provenance relation 0；只有exact-call process-local snapshot与candidate的bounded COMPLETE/OVERFLOW普通列；
- durable reflection handoff/queue 0；DORMANT/RUNNABLE/IN_FLIGHT只是Host memory owner的process-local physical state；
- durable projection echo ledger 0；anti-echo只使用bounded candidate provenance、closed ToolResult evidence classification与governance rule；
- embedding semantic contract与dense eligibility policy是两个pure identity；threshold变化不得创建vector generation/debt/rebuild owner；
- response-preference subtype/axis/delivery-mode relation 0；preference-specific compactor/projection/generation 0；head只由现有ACTIVE RESPONSE_PREFERENCE facts确定性投影；
- current oracle exact；
- pytest pre-existing retained node IDs仍被收集，obsolete节点有逐项successor disposition。

### 17.8 Real-provider + real-pgvector dogfood

使用ephemeral PostgreSQL，或上述已授权可reset的local `pulsara`数据库，以及当前配置provider。Activation前至少有一轮必须使用本地真实pgvector，不能只用fake vector repository。必须证明：

1. 用户显式要求remember；
2. model调用单一`remember`；
3. ToolResult只返回proposed；
4. best-effort governor接受；
5. 新model call通过`memory_search`找到item；
6. tool-backed fact使用provider-visiblehandle并最终精确join canonical ToolResult；
7. current tool result与旧memory冲突时，model明确采用current result；
8. 证据不记录API key、DSN、完整prompt、原始ToolResult正文或private source map。

Scope dogfood还必须使用同一provider完成：

9. project A写入USER_PROFILE、USER RESPONSE_PREFERENCE、WORKSPACE RESPONSE_PREFERENCE与WORKSPACE FACT；
10. 由project A origin governor完成后，project B的query-independent head只看到USER RESPONSE_PREFERENCE，automatic相关query可召回USER_PROFILE，但不能看到A的WORKSPACE response preference/fact；
11. 重新打开project A时head同时看到USER与A的WORKSPACE RESPONSE_PREFERENCE，显式/automatic recall可找到USER_PROFILE与WORKSPACE FACT；
12. transient head只看到USER RESPONSE_PREFERENCE，query recall可看到USER_PROFILE，选择WORKSPACE USER_PROFILE时得到typed rejection；
13. project B对该USER memory执行`memory_explain`只能得到redacted provenance，project A同origin可得到bounded producer/citation定位；
14. 一条新ROOT human prompt在不显式调用`memory_search`时通过automatic sparse+dense recall看到已接受memory，query embedding call exact 1、reranker call exact 0；
15. 同turn tool loop不再调用query embedding，新USER_STEER才创建下一个automatic recall；
16. 显式`memory_search`调用configured reranker，然后用fault injection证明ranker failure时仍按RRF返回；
17. 至少一个中文近义query与一个包含code/path identifier的query通过统一tokenizer命中gold memory。
18. 另一个成功ROOT turn包含sealed cheap hint但主模型不调用`remember`，Host只调用一次`MEMORY_HINT_REVIEW`并产生ordinary PENDING candidate；false-positive hint返回0 candidate同样成功；
19. 短输入跳过automatic recall且remote call为0，随后模型可通过显式`memory_search`取得相关memory；
20. 指定narrow kind/scope的显式搜索返回不足时，ToolResult清楚显示fallback stage，模型不再重复调用同一宽filter；
21. main model试图把刚刚recalled的memory原样remember时不会形成第二个ACTIVE fact；连续turn相同membership/content/warning set即使rank变化也不重复append；
22. model先调用`memory_search/get/explain`或memory-read `artifact_read`再paraphrase remember同一fact，该ToolResult只被识别为`MEMORY_READ_EXPOSURE`，不会当作新证据解锁anti-echo；
23. 一个成功ROOT明确说“don't remember/save this”时hint-review data egress/call exact 0，而“don't forget”的正向fixture仍可产生一次best-effort review；
24. 使用不同trust-domain auxiliary target且不显式opt-in时hint packet/body不离开Host且transport open exact 0；同provider默认路径仍正常。
25. 使用与response-preference statement语义无关的普通query时，不调用`memory_search`也仍能从`MEMORY_RESPONSE_PREFERENCE_HEAD`遵循“回答简短”；同一head的下一turn不重复append正文，正常budget下更新偏好只append完整successor SNAPSHOT；forced-tight-budget fixture则appendminimal UNAVAILABLE并继续provider open。
26. normalized短输入不执行automatic retrieval但仍看到preference head；明确“本turn不要使用memory”会清除head，下一正常human trigger重新安装当前head。
27. 用户说“我喜欢川菜”形成USER_PROFILE后，语义相关的“今天吃什么好”通过automatic dense recall看到它；无关query不把该profile常驻注入。
28. 用户把“川菜”明确替换为“粤菜”时USER_PROFILE supersede原子完成；“也喜欢粤菜”不产生错误replacement。FACT/ACTION_RULE/DECISION/RESPONSE_PREFERENCE各有一个等价relation dogfood或deterministic integration fixture。

Real-pgvector证据还必须包含：`public.vector`版本/shape deep verification，`memory_embeddings.embedding vector(1024)`与exact FK，HNSW `vector_cosine_ops`存在，bare distance-ASC query的HNSW EXPLAIN、strict iterative bounded ANN Top-K、相对sequential oracle的Recall@K、scan-bound PARTIAL、zero-vector rejection、scope/domain SQL filtering、stale-contract exclusion与final canonical refetch。测试结束后可再次reset该本地库；不需要保留测试memory rows。

Dogfood不能把governance完成一次误写成V1承诺最终完成。

Activation evidence另记录不含query/body的remote latency摘要：cold/warm query embedding、Top-20 rerank与explicit dense+rerank E2E的sample count/min/p50/p95/max。这些只用于产品成本判断，不设为网络时延硬门槛。

---

## 18. Non-goals

Round 8不实施：

- Cheap Hint Reflection以外的通用automatic extraction、turn/tool/token periodic reflection、compaction double-call或memory extraction job；
- memory deletion/forget/privacy erasure UI；
- generic graph query、SPARQL、任意hop；
- user-defined relation kind；
- cross-workspace/cross-domain relation；
- candidate-to-candidate relation；
- relation backpatch/repair；
- governance retry、lease、checkpoint、receipt或recovery；
- memory event replay；
- generative LLM reranker；explicit search的specialized `qwen3-rerank`属本轮scope；
- automatic recall或governance relatedness中的remote reranker；
- durable recall trace、query cache、embedding request receipt或rerank usage relation；
- durable reflection event/history、projection echo ledger或hint retry/recovery；
- durable vector freshness guarantee；
- RESPONSE_PREFERENCE subtype/axis/delivery mode、独立preference relation或第二套authority；
- preference-specific summarizer/compactor、silent Top-N head或对statement的生成式压缩；
- ActionRule enforcement；
- Go TUI memory panel；
- online migration或旧memory数据兼容。

“不实现forget UI”不等于未来必须保留不可删除审计图。新schema不得恢复会让未来privacy erasure被EventLog、outbox或graph副本阻塞的机制。

---

## 19. Definition of Done

只有同时满足以下条件，Round 8才能标记ACTIVATED：

1. production只暴露一个memory写工具和三个读工具；
2. ToolResult说`proposed`时exact candidate与引用已存在；
3. governance无法改写statement、conditions、scope或citations；
4. processing completion明确是best effort；
5. memory不拥有durable job、event、subject slot、receipt或recovery graph；
6. accepted relation/lifecycle仍保持事务一致；
7. query/index共用private、HMM-disabled多语种/code tokenizer contract；ordinary trigger-derived FTS与raw-term overlap同步可查，vector缺失正常退化；
8. memory输出显式advisory，不参与permission/business authority；
9. producer provenance与semantic citations物理分离；
10. `BASED_ON`只连接already accepted memory，并执行USER→USER、WORKSPACE→same-domain USER或exact WORKSPACE的单向可见性格；
11. ACTION_RULE exclusion可空，且不成为policy；
12. automatic `MEMORY_RECALL`通过Round 3/3.1 compiler作为append-only SNAPSHOT/advisory suffix进入；same membership/content/warning set即使rank变化也no-op并保留installed order，Chat Completions与Responses strict prefix与Round 7 provider-wire卫生保持；
13. complete reset、catalog/grant binding与PostgreSQL测试通过；
14. full pytest、ruff、compileall、Protocol generator、Go test/vet/module verify、`uv lock --check`与`git diff --check`通过；
15. machine evidence记录最终hash、oracle、test results、dogfood disposition与全部non-goals；
16. USER memory只在同一memory domain全局可见，WORKSPACE memory只在exact canonical project scope可见；search/get/explain与governance relatedness共用该读过滤，candidate claim另叠加exact origin-workspace fence；
17. session create/resume/takeover/discovery在SQL候选阶段exact join memory domain；foreign-domain session不会进入summary、排序或最近恢复winner；
18. citation handle由exact call snapshot与execution-binding classification证明；USER scope不能引用WORKSPACE_BOUND result，历史row不能自行恢复capability；
19. candidate↔accepted fact由deferred双向约束证明；APPLIED_TO_EXISTING candidate由decision-candidate relation反向证明且不冒充fact provenance；ACTIVE exact duplicate由partial unique选出单一并发winner；
20. PENDING candidate只由exact origin workspace治理；跨origin USER explain不暴露producer transcript、ToolResult identity或内部定位；
21. governance使用无tools、无continuity、finite-total的auxiliary model owner；relatedness与model共享一个300s attempt，prepared acceptance及existing-source settlement支持FULL/NONE/CONFLICT且不因ACK unknown重跑provider；
22. ordinary sparse/dense只返回ACTIVE seed并在最终投影前canonical refetch；只命中旧SUPERSEDED正文时不自动重定向successor；
23. AUTOMATIC_ROOT的steer prospective trial remote call exact 0且optional VALUE绝不拒绝合法user steer；MEMORY_RECALL previous VALUE/UNAVAILABLE与preference previous VALUE分别只保留各自minimal invalidation floor；selected prefix FULL后atomic rotate并持续持有safe-point handle，每个exact human trigger最多一次query embedding、reranker exact 0；
24. EXPLICIT_SEARCH使用sparse+dense、RRF(k=60)与optional Top-20 rerank，逐项与aggregate preflight闭合，reranker失败不丢失已知candidate；
25. fact embedding与所有recall read借用同一个frozen domain/scope binding；任何foreign-scope正文均不能在过滤前hydrate或外发；
26. dense contract固定为DashScope `text-embedding-v4`/1024/cosine，81,920只标为local derived ceiling；pgvector使用distance ASC HNSW与bounded iterative scan，invalid/zero vector拒绝、ANN结果不冒充global exact、underfill诚实标PARTIAL；
27. retrieval provider为Host-owned long-lived process-local resource；standalone disable与Host close使用各自唯一deadline，close/cancel physical join，不持久化query、score、trace、receipt或cache generation。
28. Cheap Hint Reflection在成功ROOT/no-successful-remember-candidate/permission-allow条件下最多一次，0..4 ordinary candidate；write opt-out逐entry过滤、turn-use opt-out整次禁用；没有event/job/history/retry且失败可完全丢失；
29. 每个真实ROOT USER_MESSAGE开启新的`MemoryUsePolicy` epoch；ordered steer按`ALL_DISABLED > WRITE_DISABLED > ENABLED`聚合，tool loop与automatic continuation只继承/加强；ALL清除recall/head并关闭四个memory tools，WRITE只关闭remember；normalized<8只跳过automatic recall且不关闭工具/head；
30. dense candidate使用绑定exact embedding contract的`0.55 / 0.20 / 0.40` COARSE_V1 eligibility floor，并明确声明本轮未完成full quality calibration；
31. explicit memory_search的kind/scope fallback bounded、一次query embedding、最多一次rerank且完整披露stage；
32. preference head、automatic recall与`memory_search/get/explain`共用exact-call bounded model-visible provenance；memory-read ToolResult/artifact不得被当作新证据，provenance overflow保守SKIP，recalled-memory pure echo不会形成第二个ACTIVE fact；
33. `MemoryWriteOptOut`与`TurnMemoryUseOptOut`在任何auxiliary data egress前执行；`don't remember/save this`只排除对应entry，`本轮不使用记忆`禁用整次review，`don't forget/不要忘记`不被误判；ROOT slot在review等待前释放；
34. hint review默认只对same provider trust domain开启，cross-provider需要显式opt-in并在未授权时0 data egress/0 transport open；
35. `EmbeddingSemanticContract`与`DenseEligibilityPolicy`分离；只threshold变化不清空或重建compatible vectors，semantic contract变化才使旧cache失效。
36. 五类kind形成closed taxonomy：FACT、USER_PROFILE、RESPONSE_PREFERENCE、ACTION_RULE、DECISION；USER_PROFILE仅USER scope，敏感profile只在当前请求明确相关或安全/准确所必需时应用；RESPONSE_PREFERENCE只表示Agent回答/解释/表达方式的soft default，unsafe core-behavior override必须SKIP；
37. 每条candidate/fact只表达一个semantic atom；prompt/descriptor/few-shot鼓励拆成多次remember，Runtime保证一call一candidate，governor对multi-atom只能SKIP且不能split/merge/partial accept；Repository只验证sealed branch与结构不变量，不实现第二套自然语言atom authority；
38. `MEMORY_RESPONSE_PREFERENCE_HEAD`以USER + exact WORKSPACE active RESPONSE_PREFERENCE的bounded full projection作为append-only SNAPSHOT进入；相同head no-op，变化优先append完整successor；prior VALUE且FULL放不下时appendminimal UNAVAILABLE，清空/opt-out append一次CLEARED；prior non-VALUE可省略，只有最小invalidation放不下才resource-bound；不能静默截断/Top-N/摘要；
39. AUTOMATIC_ROOT全链路排除RESPONSE_PREFERENCE、纳入USER_PROFILE，显式memory tools仍可读取全部五类；head、recall与显式read共同进入exact-call anti-echo provenance；
40. CONTRADICTS由Runtime对五类执行same-kind/same-scope/intent重验；SUPERSEDES只允许same-kind replacement或显式same-scope taxonomy correction；BASED_ON只允许DECISION source按单向scope visibility指向accepted target；不把provider relation output当authority，relation read同时过滤两个endpoint；
41. 所有新增或退休ACTIVE RESPONSE_PREFERENCE的acceptance branch以exact domain/scope transaction advisory lock重验最终容量；并发、supersede、taxonomy correction、contradict与ACK unknown都有单一closed winner，超界只产生`RESPONSE_PREFERENCE_CAPACITY_EXCEEDED` SKIP；
42. response-preference head保持BASE_SYSTEM与tools不变、messages只append suffix；没有preference-specific durable projection、generation、receipt、job或compactor。
43. exact duplicate不能吞掉sealed SUPERSEDES/CONTRADICTS intent：existing ACTIVE source通过candidate-governed APPLIED_TO_EXISTING transaction写relation并原子执行lifecycle；BASED_ON duplicate明确不事后补边，repository没有generic existing-source relation API。

---

## 20. 最终架构判断

Round 8不是把旧memory“降级成不可靠文本”，而是重新放正它在系统中的位置：

~~~text
conversation / tool / permission
    own operational truth

memory
    owns only what the advisory dataset currently contains
    may be incomplete or stale
    remains typed, scoped and explainable
~~~

因此应同时坚持两件看似相反、实际互补的事：

- **弱化完成承诺**：不保证每个candidate最终治理、每个fact都有vector、每条memory永远新鲜；
- **强化已落盘结构**：candidate provenance、ToolResult citation、fact kind、relation endpoint与supersede/contradict transaction都不能含糊。

这比hard-cut前系统更小，也比当前Stage 2 memory骨架更诚实。它保留真正有产品价值的candidate、typed memory、replacement/conflict、source explanation与hybrid recall基础，同时删除memory并不需要的durable job、event occurrence、generation debt、recovery和通用图机制。
