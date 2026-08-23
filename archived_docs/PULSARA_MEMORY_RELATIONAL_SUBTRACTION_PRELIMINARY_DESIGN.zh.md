# Pulsara PostgreSQL 记忆模型减法：初步设计

> 状态：**SUPERSEDED — 2026-08-14 / 仅保留为历史讨论输入**
>
> 日期：2026-08-10
>
> 代码读取基线：`9dfc79f2d0b21ea45dd313b4a62d6aa191919154`，并包含读取时尚未提交的 Stage 2 工作树状态
>
> 替代文档：[ROUND_8_ADVISORY_MEMORY_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md](ROUND_8_ADVISORY_MEMORY_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md)
>
> 上位约束：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> Stage 2 输入：[STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md](STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)

本文记录 PostgreSQL-only 记忆系统在 2026-08-10 时的初步收敛方向。其关系减法与历史代码调研仍可作为审计输入，但 reliable durable governance/index/event 假设已被 Round 8 的 advisory、weak-completion 设计替代。本文不再作为编码或review真源。

读取期间，另一个 Codex 线程仍在修改 `src/pulsara_agent/conversation_kernel/`。因此本文的 current-state finding 是 2026-08-10 的点时快照；目标设计不依赖未提交实现细节。

---

## 1. Executive summary

推荐把新记忆架构收敛为：

```text
一个Agent-facing写工具：remember

四种canonical memory fact：
  FACT
  PREFERENCE
  ACTION_RULE
  DECISION

三种fact-to-fact语义关系：
  BASED_ON
  SUPERSEDES
  CONTRADICTS

一条canonical source lineage：
  MemoryFact -> GovernanceDecision -> MemoryCandidate -> TranscriptEntry
  MemoryCandidate -> cited ToolResult refs
```

物理真值全部位于 PostgreSQL。系统不恢复 JSON-LD、RDF、Oxigraph、SPARQL、开放谓词、generic graph DSL 或任意深图遍历。

核心边界是：

- `memory_facts`保存治理接受后的当前语义事实；
- `memory_relations`只保存有独立产品意义的 fact-to-fact 关系；
- candidate的source/citation保存可追溯的canonical来源引用，不复制原文，也不伪装成Host已验证的语义证明；
- ToolResult、TranscriptEntry、Artifact和Blob继续由各自canonical关系拥有；
- Agent只提出候选，不直接发布canonical relation；
- 异步governance在closed contract内决定最终fact类型、生命周期和关系；
- FTS/pgvector只产生seed，direct-edge与two-hop只执行closed motif；
- canonical row是truth，committed event只是同事务接受该transition的occurrence；
- 不新增memory删除/forget能力。

这不是用关系型表重建一个通用知识图谱，而是保留少数能改善治理、解释与召回的产品关系。

---

## 2. 既定约束

以下结论继承自durability subtraction主线，不在本文重新开放：

1. PostgreSQL是唯一canonical memory authority和唯一Agent-facing memory read store。
2. Oxigraph、JSON-LD canonical representation、SPARQL、surface worker、delivery/retry和graph adapter完整退出目标架构。
3. 保留FTS、pgvector、direct relation和现有bounded最多两跳recall；不扩大hop、查询语言或图能力。
4. 用户显式记忆和automatic extraction都必须先可靠写入candidate pool，再异步治理；foreground不等待governance。
5. governance或index worker失败不得否定candidate intake或已完成的canonical fact transaction。
6. memory fact、relation、lifecycle变化和相应index desired generation由同一owner在同一PostgreSQL transaction接受。
7. reopen和recall读取canonical rows，不通过AgentEvent replay恢复memory truth。
8. 本轮不引入forget、delete、tombstone或隐私擦除新feature。
9. complete reset仍是hard-cut activation策略；本文不设计旧数据在线兼容或dual-write迁移。

---

## 3. 当前代码真值

### 3.1 旧系统的七个谓词并不同构

旧的JSON-LD/graph vocabulary混合了memory semantics、evidence provenance和runtime provenance：

| 旧谓词 | 实际方向 | 原producer | 实际语义 |
|---|---|---|---|
| `hasEvidence` | Memory -> Evidence/ToolResult | typed candidate的`evidence_ids` | fact引用准确证据 |
| `supports` | Evidence/ToolResult -> Memory | canonical ledger自动补反向边 | `hasEvidence`的重复反向物化 |
| `basedOn` | Decision -> existing graph node | `remember_decision.based_on_ids` | 决策的明确依据 |
| `supersedes` | new Preference -> old Preference | governance lifecycle | 新偏好替代旧偏好 |
| `contradicts` | Preference A <-> Preference B | governance lifecycle | 两条偏好冲突但都保留active |
| `derivedFrom` | derived memory -> source | 没有production writer | 只有ontology/read-path预留 |
| `rt:provides` | ToolResult -> Artifact | tool-result projection owner | runtime provenance，不是memory relation |

旧`public.memory_relations`没有endpoint FK，正是因为source/target可能是Memory、Evidence、ToolResult或Artifact等异构节点。它不是目标`memory_facts -> memory_facts`模型的可直接迁移输入。

### 3.2 旧关系如何形成

旧的evidence关系被双向物化：

```text
Memory --hasEvidence--> Evidence
Evidence --supports--> Memory
```

旧的Decision可携带：

```text
Decision --basedOn--> existing Memory
```

旧的lifecycle由governance relatedness与closed decision驱动：

```text
new Preference --supersedes--> old Preference
old Preference.status = SUPERSEDED
```

或：

```text
Preference A --contradicts--> Preference B
Preference B --contradicts--> Preference A
两边继续ACTIVE
```

相关代码真值包括：

- [canonical/ledger.py](src/pulsara_agent/memory/canonical/ledger.py)写`hasEvidence`、`supports`与`basedOn`；
- [canonical/lifecycle.py](src/pulsara_agent/memory/canonical/lifecycle.py)写`supersedes`与双向`contradicts`；
- [governance/executor.py](src/pulsara_agent/memory/governance/executor.py)限制production supersede/contradict为Preference、同scope、bounded target；
- [postgres_memory_projection.py](src/pulsara_agent/storage/postgres_memory_projection.py)从JSON-LD document提取relation rows；
- [memory/recall/graph.py](src/pulsara_agent/memory/recall/graph.py)只允许特定一跳/两跳motif。

### 3.3 当前v3骨架尚未冻结最终memory contract

当前 [0013_conversation_kernel_hard_cut.sql](src/pulsara_agent/storage/migrations/sql/0013_conversation_kernel_hard_cut.sql) 已建立：

- `memory_candidates`；
- `memory_governance_decisions`；
- `memory_facts`；
- `memory_relations`；
- PostgreSQL FTS/vector index与desired/applied generation。

但读取时仍有以下缺口：

1. `memory_facts.fact_kind`是自由文本。
2. `memory_relations.relation_kind`是自由文本。
3. production governance在 [conversation_kernel/jobs.py](src/pulsara_agent/conversation_kernel/jobs.py) 调用`accept_memory_governance(..., relations=())`，实际生成0条relation。
4. [conversation_kernel/memory.py](src/pulsara_agent/conversation_kernel/memory.py) 对任意`relation_kind`做只沿source->target的最多两层递归，没有复现旧的双向closed motif。
5. `CORRECT`、`MERGE`、`SUPERSEDE`和`CONTRADICT`当前共享predecessor lifecycle处理；`CONTRADICT`可能把旧fact标成`SUPERSEDED`，与旧产品语义冲突。
6. 五个`remember_*`最终都进入同一个proposal path；Claim、Observation和Decision先被压成`FACT`，ActionBoundary却被映射成`LIFECYCLE`。
7. candidate `proposal_kind`混合了semantic kind（`FACT`、`PREFERENCE`）和workflow action（`RELATION`、`CORRECTION`、`LIFECYCLE`）两条正交轴。

因此，当前v3 schema是可运行骨架，不应被视为最终memory vocabulary已经冻结。

---

## 4. 目标逻辑模型

```text
+--------------+      +-----------------------------+
| memory_facts |----->| memory_governance_decisions |
+--------------+      +-----------------------------+
      |                             |
      |                             v
      |                    +-------------------+
      |                    | memory_candidates |
      |                    +-------------------+
      |                       |              |
      |                       v              v
      |             transcript_entries   cited ToolResult refs
      |                                      |
      |                                      v
      |                                 tool_results
      |
      | BASED_ON / SUPERSEDES / CONTRADICTS
      v
+------------------+
| memory_relations |
+------------------+

ToolResult/content/blob lineage继续由conversation kernel自身的canonical FK拥有。
```

“memory graph”是这些canonical relational rows组成的typed关系视图，不是一个新的通用Node/Edge平台。

---

## 5. Canonical memory fact分类

### 5.1 推荐的四类

| `fact_kind` | 含义 | 例子 | 特殊字段/行为 |
|---|---|---|---|
| `FACT` | 对用户、workspace或外部世界的持久描述 | “仓库使用Python 3.13” | 可追溯canonical source |
| `PREFERENCE` | 用户或workspace的软偏好 | “用户偏好简短回复” | V1可supersede/contradict |
| `ACTION_RULE` | 有明确适用/排除条件的长期行为指令 | “改生产DB前必须备份” | requires applicability fields |
| `DECISION` | 已经作出的选择 | “采用PostgreSQL作为唯一真源” | 可`BASED_ON`其他fact |

### 5.2 为什么合并Claim和Observation

旧Claim与Observation都保存：

- statement；
- scope；
- producer/source provenance；
- confidence；
- cited canonical source references。

两者的主要区别是“谁提供、候选人对来源有多大信心”，而不是canonical statement自身的不同产品行为。这些差异应由typed provenance与confidence表达，不应要求Agent长期区分“这是Claim还是Observation”。因此目标统一为`FACT`。`TOOL_RESULT`来源的存在不自动意味着Host已证明statement为真。

### 5.3 为什么Preference、ActionRule和Decision仍独立

- Preference是软默认，可以与另一偏好冲突或被明确替换。
- ActionRule是条件化行为约束，需要`applies_when`和`do_not_apply_when`等结构，不应按普通偏好处理。
- Decision有明确的“为何作出”语义，是`BASED_ON`的唯一source kind。

`ACTION_RULE`是本文推荐命名；是否保留现有`ActionBoundary`名称仍需在schema冻结前做一次命名审阅。无论命名如何，其产品语义不扩展。

---

## 6. Agent-facing写入口

### 6.1 一个写工具，而不是五个

目标只暴露一个candidate mutation工具：

```text
remember
```

概念契约：

```text
remember(
    statement,
    scope,
    kind_hint = AUTO | FACT | PREFERENCE | ACTION_RULE | DECISION,
    applies_when?,
    do_not_apply_when?,
    based_on_ids?,
    cited_tool_result_ids?
)
```

这只是概念shape，不冻结最终wire字段名或JSON Schema写法。

### 6.2 一个工具不等于无类型payload

Host仍应把输入解析为closed tagged union：

```text
FactProposal
PreferenceProposal
ActionRuleProposal
DecisionProposal
```

至少验证：

- `ACTION_RULE`必须有适用与排除条件；
- 只有`DECISION`可携带`based_on_ids`；
- unknown kind fail closed；
- 非对应kind携带专属字段时不得静默忽略；
- `kind_hint`只是proposal，不是canonical truth；governance可纠正分类。

### 6.3 主模型提出语义引用，Host拥有引用完整性

Agent不应自行声明：

```text
source_authority = USER_CONFIRMED
verification_status = TOOL_VERIFIED
```

Host只根据exact canonical source绑定与校验：

- ID存在且指向committed canonical row；
- source与candidate属于同workspace/session capability边界；
- source在候选接受前已存在；
- `ToolResult`不是contentless状态，且引用数量在hard bound内。

Host不做自然语言entailment判定，不验证候选改写是否逐字等于用户原话，也不复制用户原话或长tool output。主模型（或automatic extractor）负责提出“这个source支持该candidate”的语义主张；Host只保证引用没有伪造、越权或错指。

模型不能把自己的输出升级成“user confirmed”或“tool verified”；`source_authority`/`verification_status`不是`remember`的入参。因主架构不承诺exact context-input audit，durable source ref也不能被解读为“模型必然看过该source的全部字节”。

### 6.4 读工具继续独立

以下工具有独立读语义，应继续保留：

- `memory_search`；
- `memory_get`；
- `memory_explain`。

本次减少的是五个重复写入口，不是把所有memory能力压进一个万能工具。

---

## 7. Candidate与governance边界

### 7.1 Candidate intake

任何合法`remember`调用都先在tool result transaction内：

1. 插入durable `memory_candidate`，并绑定exact source entry与主模型显式提出的bounded cited ToolResult refs；
2. 创建或绑定唯一governance job intent；
3. 返回`status=proposed`；
4. 不等待provider governance call；
5. 不写canonical fact或relation。

automatic extraction遵守同一规则：先candidate，后governance。

### 7.2 Governance输入

governance至少读取：

- exact candidate及source；
- Host已验证的canonical reference binding；
- 主模型或extractor显式提出的cited ToolResult refs；
- 同workspace/scope内bounded active related facts；
- related fact exact ID、kind、lifecycle与row revision；
- candidate显式携带的`based_on_ids`。

FTS/vector relatedness只服务于dedupe、supersede和contradict判断，不证明relation成立，也不得用来为candidate发现或换绑`BASED_ON`/cited source。Governance可接受或拒绝整个candidate，但不得静默重新推断、替换或补造已绑定的source与target。

### 7.3 Governance输出

本文推荐把产品结果收敛为：

```text
SKIP
ACCEPT
ACCEPT_AND_SUPERSEDE(target_fact_id)
ACCEPT_AND_CONTRADICT(target_fact_id)
```

候选内容的correction、normalization或多候选merge属于governance lineage，不应自动等同于canonical predecessor lifecycle变化。

最终是否保留`CORRECT`/`MERGE`作为顶层数据库decision vocabulary，仍需在实施规格前冻结；无论选择如何，它们都不得隐式写`SUPERSEDES`。

---

## 8. Fact-to-fact relation vocabulary

`memory_relations`只允许三种closed kind：

```text
BASED_ON
SUPERSEDES
CONTRADICTS
```

V1不提供`remember_relation`、`link_memory`或任意predicate输入。

### 8.1 BASED_ON

方向：

```text
Decision --BASED_ON--> existing MemoryFact
```

例子：

```text
decision:postgres-canonical
    --BASED_ON-->
fact:atomic-row-and-event-required
```

产生路径：

1. 主模型先通过`memory_search`获得exact memory ID；automatic extractor则只能从Host为当次提取提供的bounded typed source bundle中选择ID；
2. candidate必须显式携带`based_on_ids`，不得只携带一句自然语言“因为/根据/基于”而要求governance猜测target；
3. Host adapter校验source是Decision、target exact ID存在、同kind/workspace/capability且不晚于candidate；
4. governance可因语义不成立而拒绝candidate，但不得自行发现或替换target；
5. target在关系接受时必须有效；未来target lifecycle变化不删除历史依据；
6. Decision fact和relation在同一transaction写入。

禁止仅凭embedding相似度自动建立`BASED_ON`。如果没有确切依据，Decision可以被接受但不写relation。

### 8.2 SUPERSEDES

方向：

```text
new Preference --SUPERSEDES--> old Preference
```

同一transaction还必须：

```text
new.lifecycle = ACTIVE
old.lifecycle = SUPERSEDED
```

V1保持旧产品范围：

- source/target均为Preference；
- target当前为ACTIVE；
- 同scope；
- 每次最多一个predecessor；
- candidate的语义提案含明确replacement intent，并由governance做lifecycle判断；
- “新的冲突陈述”本身不等于replacement。

旧fact不删除。普通recall不注入`SUPERSEDED` fact，`memory_get/explain`仍可读取其successor lineage。

### 8.3 CONTRADICTS

语义是对称关系：

```text
Preference A --CONTRADICTS-- Preference B
```

V1保持旧产品范围：

- 两端均为Preference；
- 同scope；
- 两端继续ACTIVE；
- 每次最多一个related target；
- 只有durable、同subject且不可同时成立、又没有明确replacement intent时建立。

物理上只保存一行，不再双写A->B和B->A。查询把两个endpoint都视为邻接点，并使用unordered pair uniqueness防止反向重复。

`CONTRADICTS`绝不能把target lifecycle改成`SUPERSEDED`。

---

## 9. Cited source lineage，不是Evidence证明图

### 9.1 语义边界

目标中的source/citation只表示“候选生成者指定了这条canonical来源”。它不表示：

- Host已验证statement被source严格逻辑蕴含；
- candidate逐字复述了用户原话；
- provider必然看过长tool result或blob的全部字节；
- ToolResult的存在自动把candidate升级成已验证事实。

因此target vocabulary优先使用`cited_sources`或`source_lineage`，不把`evidence`当成一个Host保证的verification等级。

### 9.2 用户与transcript来源复用现有lineage

当前schema已有一条可约束的链：

```text
MemoryFact
  -> GovernanceDecision
  -> MemoryCandidate
  -> source_entry_id
  -> TranscriptEntry
```

它已能表达“这条fact来自哪个candidate，而candidate在哪个canonical entry中被提出”。不再为TranscriptEntry复制一条通用`memory_fact_sources`，也不把entry原文复制到memory row。

目标Host应绑定候选的exact causal source entry。当前tool path把发出`remember_*`的assistant entry写入`source_entry_id`，这只能证明candidate的producer，不一定就是用户原话所在的causal entry；这是实施规格需要明确的已知差距，不应通过复制quote规避。

### 9.3 ToolResult引用只使用窄typed relation

主模型可从当前capability允许的typed handles中显式提交`cited_tool_result_ids`。Host只做existence、same-scope、causal order、allowed state与hard-bound校验；不读取并复制完整output，不要求模型复述原文，不做内容蕴含证明。

若V1需要一个candidate引用多个ToolResult，最小物理边界是窄child relation，例如：

```text
memory_candidate_tool_result_refs(
    candidate_id,
    origin_session_id,
    tool_result_id,
    ordinal
)
```

它必须使用exact composite FK、bounded ordinal和唯一性，不扩张成`TRANSCRIPT_ENTRY | TOOL_RESULT | BLOB | ARTIFACT | ...`的generic subject union。Fact的ToolResult来源由`Fact -> Decision -> Candidate -> cited ToolResult refs`派生，不在fact接受时复制第二份association。

Artifact、Blob和ToolResult content继续由conversation kernel的canonical FK链拥有。引用长ToolResult时只保存identity，不保存全文副本。

如果承诺共享source的bounded反向查询，ToolResult refs必须使用可约束、可索引的窄typed relation，不能藏入任意JSONB。

### 9.4 Automatic extraction不是governance discovery

Automatic extractor可接收Host构造的bounded source bundle，其中只有opaque typed handles和必要的bounded content projection。Extractor只能从该bundle中选择source ID并提出candidate；Host重新绑定并校验ID。Governance仍只决定candidate是否接受、最终分类及lifecycle，不重新发现、替换或补造source ref。

### 9.5 Cited source binding不需要新event vocabulary

Candidate intake transaction同时接受exact source entry与cited ToolResult refs。治理接受的Fact通过immutable FK lineage达到它们，不要求为每个source ref新增happy-path committed event。

---

## 10. 不进入memory graph的关系

### 10.1 ToolResult provides Artifact

旧`rt:provides`表达：

```text
ToolResult -> Artifact
```

目标由conversation kernel的ToolResult、TranscriptEntry、Blob/content FK表达。它不进入`memory_relations`，也不参与memory two-hop recall。

### 10.2 DERIVED_FROM

旧代码只有ontology与read-path支持，没有production writer。目标删除，不因历史词表存在而恢复producer。

### 10.3 CORRECT与MERGE

它们是候选治理过程，不是事实之间天然存在的图边。除非未来出现独立产品需求和closed producer，否则不增加`CORRECTS`、`MERGED_FROM`或类似relation kind。

---

## 11. PostgreSQL约束草案

本节冻结约束意图，不冻结最终DDL。

### 11.1 memory_facts

至少需要：

```text
fact_kind IN (FACT, PREFERENCE, ACTION_RULE, DECISION)
lifecycle IN (ACTIVE, SUPERSEDED, STALE)
```

`fact_payload`可以承载每类closed versioned payload，但不能成为任意schema逃生舱。每个kind必须有有限版本、validator和fixture。

### 11.2 memory_relations

至少需要：

```text
relation_kind IN (BASED_ON, SUPERSEDES, CONTRADICTS)
source_fact_id <> target_fact_id
same-workspace composite FK
directed uniqueness
CONTRADICTS unordered-pair uniqueness
```

数据库还应能约束endpoint kind：

```text
BASED_ON:
  source_kind = DECISION

SUPERSEDES:
  source_kind = PREFERENCE
  target_kind = PREFERENCE

CONTRADICTS:
  source_kind = PREFERENCE
  target_kind = PREFERENCE
```

一种可审查方案是在relation row携带由composite FK校验的source/target fact kind，再用SQL CHECK冻结组合；最终DDL应在实施规格中选择最小且可验证的表达，不依赖自由字符串或普通插件直接写表。

### 11.3 Relation immutability

V1 relation row一经接受不可UPDATE为另一个kind或endpoint。错误relation是产品数据错误，应由hard-cut前测试阻止；本文不引入通用relation repair owner。

---

## 12. Transaction与authority

canonical governance acceptance transaction应按固定owner完成：

```text
lock/revalidate candidate and exact related targets
  -> insert governance decision
  -> insert canonical fact
  -> insert accepted semantic relations
  -> SUPERSEDES分支更新old lifecycle
  -> advance affected FTS/vector desired generation
  -> append selective committed occurrence
  -> commit
```

不变量：

- 普通Agent、hook或plugin不能直接插入canonical fact/relation；
- relation不能晚于source fact以独立best-effort任务补写；
- relation acceptance失败必须回滚同一canonical governance transaction；
- index refresh失败不能回滚已接受的fact/relation；
- committed event不能证明canonical row真实；row和约束本身是truth；
- durable event consumer失败不能否定canonical commit。

可以继续使用现有selective occurrence：

- `MemoryFactAccepted`；
- `MemoryRelationAccepted`；
- `MemoryFactLifecycleChanged`。

不为候选解析、relatedness搜索、cited source binding、FTS/vector refresh或普通hook新增happy-path事件。

---

## 13. Recall contract

### 13.1 Seed discovery

FTS与pgvector只返回bounded candidate IDs。它们不决定事实真假、lifecycle或relation。

默认只把`ACTIVE` fact作为普通recall seed/result；exact `memory_get`可读取非ACTIVE历史fact。

### 13.2 Direct relation

允许：

- Decision与其`BASED_ON`依据；
- Preference的`CONTRADICTS`伙伴，并附冲突warning；
- `SUPERSEDES` predecessor/successor lineage；
- fact通过`Fact -> Decision -> Candidate`到exact source entry与cited ToolResult refs的解释lineage。

### 13.3 Closed two-hop motif

最多两跳只允许预先冻结的motif：

```text
Decision A -> same basis fact <- Decision B

Preference newest -> SUPERSEDES -> middle -> SUPERSEDES -> oldest
```

是否将superseded intermediate作为可返回result，继续遵守现有visibility规则；默认只用于lineage/explanation，不把inactive fact作为普通context注入。

禁止：

```text
BASED_ON -> CONTRADICTS
CONTRADICTS -> SUPERSEDES
任意relation_kind组合
任意递归深度
raw SQL/SPARQL graph tool
```

关系遍历可从任一endpoint开始，但必须保留存储方向和path explanation。

### 13.4 可选的bounded shared-source lookup

如果V1经golden fixture证明仍需要shared-source recall，它只允许使用可索引的candidate source/citation做一次bounded companion join：

```text
Fact A -> Candidate A -> same cited source <- Candidate B <- Fact B
```

它不是新的`memory_relations` kind，不计作可任意组合的graph hop，也不对任意content/blob做遍历。如果V1不承诺该产品能力，则只保留`memory_explain`的单fact source lineage，不做反向扩展。

---

## 14. 端到端例子

### 14.1 普通Fact

用户：

```text
记住，这个仓库使用Python 3.13。
```

Agent：

```text
remember(kind_hint=FACT, statement="仓库使用Python 3.13", scope=WORKSPACE)
```

结果：先candidate；governance接受后写一个`FACT`，不必创建relation。

### 14.2 Preference supersede

已有：

```text
preference:old = 用户偏好详细回答
```

用户：

```text
以后不要再写得那么详细，改成简短回答。
```

governance在bounded relatedness中获得`preference:old`，确认explicit replacement：

```text
preference:new --SUPERSEDES--> preference:old
preference:old.lifecycle = SUPERSEDED
```

### 14.3 Preference contradiction

已有：

```text
用户偏好tabs
```

新candidate：

```text
用户偏好spaces
```

若没有明确“替换旧偏好”的证据：

```text
tabs --CONTRADICTS-- spaces
两边继续ACTIVE
```

recall必须把它表示为冲突，而不是静默选择或合并。

### 14.4 Decision based on Fact

已有：

```text
fact:atomicity = canonical row与committed occurrence要求同事务接受
```

新Decision：

```text
decision:postgres = 使用PostgreSQL作为唯一canonical truth
```

主模型必须在candidate中提出exact ID；Host验证该ID，governance可接受或拒绝candidate，但不得自行换绑：

```text
decision:postgres --BASED_ON--> fact:atomicity
```

如果找不到确切依据，Decision仍可接受，但不猜测`BASED_ON`。

### 14.5 Tool-backed Fact

工具结果：

```text
tool_result:python-version
```

Fact：

```text
仓库解释器为Python 3.13
```

主模型在发出`remember`时提交exact ToolResult handle。目标只保存：

```text
candidate.source_entry_id = exact causal transcript entry
memory_candidate_tool_result_refs(candidate, exact tool_result identity)
fact -> governance decision -> candidate
```

不再额外写：

```text
fact --hasEvidence--> tool_result
tool_result --supports--> fact
tool_result --provides--> artifact  # 不属于memory graph
```

---

## 15. Failure semantics

| 故障 | 目标行为 |
|---|---|
| `remember`参数无效 | tool typed error；不得写半个candidate |
| source/cited ToolResult ref无效、越权或迟于candidate | candidate intake fail closed；不得静默删除或替换ref |
| candidate commit失败 | tool失败；不得声称proposed |
| governance provider失败 | candidate与finite job保持durable；foreground不回滚 |
| relatedness unavailable | 不允许supersede/contradict；可按policy接受无relation fact或让job有限重试 |
| target revision/lifecycle漂移 | relation branch fail closed；不得换绑另一个target |
| relation constraint冲突 | 整个canonical governance transaction回滚 |
| FTS/vector refresh失败 | canonical fact/relation保持；query报告stale/unavailable |
| committed event consumer失败 | canonical commit保持成功 |
| Host crash | candidate/job/fact按canonical rows恢复；不依赖event replay |

`BASED_ON`缺失不是故障。主模型或extractor没有提供可验证的exact target时，宁可接受一个无basis relation的Decision，也不让Host或governance猜测关系。

---

## 16. 从旧谓词到目标模型

complete reset意味着不执行在线数据迁移，但语义映射用于测试和能力等价审查：

| 旧语义 | 目标处置 |
|---|---|
| `hasEvidence` | 删除通用边；通过Fact -> Decision -> Candidate的source/citation lineage解释 |
| `supports` | 删除；必要时通过candidate source/citation的bounded反向join查询 |
| `basedOn` | closed `BASED_ON`，只连接MemoryFact |
| `supersedes` | closed `SUPERSEDES` + target lifecycle |
| 双向`contradicts` | 单行、对称读取的`CONTRADICTS` |
| `derivedFrom` | 删除 |
| `rt:provides` | conversation/tool/content FK，不进入memory |
| JSON-LD document | closed relational columns/payload |
| Oxigraph/SPARQL | 删除，不提供替代generic DSL |

---

## 17. Architecture guards建议

后续实施规格至少应包含以下静态或数据库gate：

1. production tool catalog只有一个memory candidate写工具；
2. canonical `fact_kind`没有自由字符串；
3. canonical `relation_kind`只有三种；
4. production中不存在generic relation insert port；
5. ordinary hook/plugin不能appendcanonical memory relation；
6. `CONTRADICTS`不会改变两端lifecycle；
7. `SUPERSEDES`只允许Preference->Preference且同scope；
8. `BASED_ON`source只能是Decision；
9. candidate origin使用exact transcript FK；ToolResult citation若保留，只使用窄typed FK，不出现任意`node_kind/node_id`或复制quote；
10. relation query没有任意predicate组合或超过两跳递归；
11. `relations=()`不再是production governance永久占位；
12. Oxigraph/JSON-LD/SPARQL production import、配置、网络与schema计数为0；
13. source authority和verification不由Agent工具参数自行升级；Host不声称完成语义或逐字验证；
14. no delete/forget API、event或lifecycle state；
15. fact/relation/lifecycle/index desired/committed occurrence保持同事务owner；
16. governance不得从embedding/FTS结果为candidate发现、替换或补造`BASED_ON`与cited source refs。

---

## 18. 当前实现与目标的已知差距

按本文读取快照，后续memory implementation spec需要处理：

- 五个`remember_*`合并成一个Agent-facing write tool；
- Claim/Observation折叠成FACT；
- ActionBoundary的最终命名与payload schema；
- `proposal_kind`不再混合semantic type和workflow transition；
- Agent工具移除自报source authority/verification，改为显式、bounded的cited ToolResult handles；
- `fact_kind`与`relation_kind`闭合；
- governance恢复bounded relatedness与exact target validation；
- `based_on_ids`lowering为`BASED_ON`；
- supersede与contradict分离生命周期语义；
- `CONTRADICTS`单行对称约束；
- 将当前assistant producer entry与exact causal source entry区分清楚；
- 复用`Fact -> Decision -> Candidate -> source_entry_id`来源链，并冻结可选窄`memory_candidate_tool_result_refs`的物理表达；
- arbitrary forward recursive CTE改为closed bidirectional motif；
- memory_get/explain输出typed relation与source explanation；
- 删除legacy ontology/entity/graph projection/Oxigraph路径及其tests；
- 重建PostgreSQL-only golden fixtures和0/1/2-hop等价测试。

这些是future implementation scope，不是本文已经执行的代码修改。

---

## 19. 非目标

本文不设计：

- memory deletion、forget、privacy erasure workflow；
- 通用知识图谱或用户自定义predicate；
- 超过两跳的recursive traversal；
- raw SQL/SPARQL graph query tool；
- cross-workspace relation；
- relation post-hoc repair graph；
- Oxigraph compatibility/dual write；
- 在线旧数据迁移；
- memory event replay recovery；
- 让普通hook直接更改canonical memory；
- 在foreground memory tool调用中同步等待governance。

---

## 20. 编码前仍需冻结的问题

以下问题尚未在本轮讨论中完全关闭：

1. `ACTION_RULE`是否作为最终public/schema名称，还是保留`ACTION_BOUNDARY`。
2. V1是否需要多ToolResult citation；若需要，冻结窄`memory_candidate_tool_result_refs`的composite FK、数量与ordinal bound，不引入通用`memory_fact_sources`。
3. Host如何从当前assistant producer entry确定exact causal user/source entry；必须选择可约束的typed binding，不通过复制原话解决。
4. target field最终命名为`cited_sources`还是`cited_tool_result_ids`，以及shared-source反向查询是否进入V1。
5. governance数据库decision是否收敛到`SKIP | ACCEPT | ACCEPT_AND_SUPERSEDE | ACCEPT_AND_CONTRADICT`，还是保留`CORRECT/MERGE`作为显式lineage kind。
6. `BASED_ON`target在接受时允许哪些lifecycle；初步建议必须是当前有效fact，之后的lifecycle变化保留历史edge。
7. shared-source与shared-basis的exact结果、排序和fanout预算，需要以旧golden fixture校准而不是临场发明。
8. 一个`remember`工具的最终JSON Schema采用discriminated union还是flat tagged object；无论选择如何，内部contract必须closed。

这些问题应先通过小型ADR或memory implementation spec冻结，再修改migration和production composition。

---

## 21. 初步结论

推荐目标不是“把旧JSON-LD七个谓词换成七个PostgreSQL字符串”，而是重新识别各关系的真正owner：

- memory fact之间只保留`BASED_ON`、`SUPERSEDES`、`CONTRADICTS`；
- fact的transcript来源复用`Fact -> Decision -> Candidate -> source_entry_id`链，外加最小、可选的candidate-to-ToolResult typed citation，不复制一套fact evidence表；
- ToolResult、TranscriptEntry和Blob关系留在conversation kernel；
- `SUPPORTS`反向副本、`DERIVED_FROM`预留词、`rt:provides`混入memory和通用图协议全部退出；
- Agent只调用一个`remember`提出候选与语义citation；Host拥有reference integrity，不声称语义验证；异步governance拥有canonical分类、lifecycle与关系接受，但不静默发现或换绑source/`BASED_ON`；
- PostgreSQL constraint拥有结构真值，selective AgentEvent只记录接受发生；
- recall只执行closed、bounded、可解释的关系motif。

这套模型比旧系统更小，但没有把记忆降级成无关系的文本向量集合：它保留依据、替代、冲突、可追溯source citation和必要的bounded join，同时删除没有独立产品语义的图 machinery。“可追溯”不等于“Host已证明”。
