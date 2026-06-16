# Pulsara 记忆生命周期 · Supersede 设计

_Created: 2026-06-16_

> 本文是 supersede（记忆替代）的**主张与设计**层文档,接续 `MEMORY_RECALL_IMPLEMENTATION_GUIDE.zh.md` 的 Phase 2「lifecycle producer」。它回答的不是"怎么写代码",而是 supersede 这个**会销毁用户记忆**的操作应遵循什么教义、按什么触发源切分、v1 做到哪、v2 留什么。
>
> **v1 的落地细节(决策签名、executor 校验门、UoW 序列、降级与审计实现、测试矩阵)见 `MEMORY_LIFECYCLE_SUPERSEDE_V1_IMPLEMENTATION.zh.md`。** 本文只讲主旨要义;两者冲突时,以实施文档的代码签名为准。
>
> 所有"代码现状"主张均经 `file:line` 核实(§1)。代码签名是规范性的;散文是解释性的。

## 0. 总判断

`MemoryLifecycle` 的能力(`supersede` / `mark_stale` / `mark_contradicted`)已经实现并单测通过,但 **governance 从不调用它**——能力存在,无生产路径(§1.5)。把它接通,是激活整个 graph-aware 差异化层的钥匙(它同时让 §2.3 的 lifecycle 过滤器、explainer 的替代/冲突解释、reranker 的 lifecycle 信号从"恒空"变"有数据")。

但"接通 lifecycle"不是"调用一个方法"。supersede 的难点在于它是系统里**第一个会让已治理事实消失**的操作,且涉及两个被反复混淆的正交轴:

```text
轴一 · 检测频率：多久问一次"该不该替代旧记忆？"
轴二 · 应用耦合：决定替代时，是否与新记忆写入同一事务？
```

两个关键判断先行:

1. **"write-then-retire" 是强制顺序,不是选择。** `lifecycle.supersede(old_id, new_id)` 把 `supersedes` 边挂在**新节点**文档上(§1.6),所以 `new_id` 必须先存在。永远是"先落新记忆,再退旧记忆"。唯一的真问题是:**同一事务,还是分开**。

2. **检测频率应按触发源切分,而非一刀切"做得更低频"。** 用户显式要求替代(urgent)与系统推断替代(non-urgent)是两种性质完全不同的事,必须分开对待。这正是本文的核心结论。

## 1. 已核实的代码基线（设计起点，勿再勘探）

每条都经源码核对,是本文所有判断的前提。

### 1.1 现有 governance decision 是 4 类判别联合，全部只引用候选池

`GovernanceDecision` 是判别联合([candidates/pool.py:104](src/pulsara_agent/memory/candidates/pool.py:104)):`SkipDecision` / `SubmitAsIsDecision` / `CorrectAndSubmitDecision` / `MergeAndSubmitDecision`。**全部用 `target_entry_id(s)` 引用候选池 pending 条目**,没有任何一个引用已治理的 canonical `mem:*` 节点。

### 1.2 召回排序无 recency —— coexistence 不会自愈

召回排序是 RRF over lexical+fts,tiebreak 用 `memory_id`([recall/service.py:290](src/pulsara_agent/memory/recall/service.py:290)),**没有 recency 维度**。后果:若"prefer tabs"(旧,ACTIVE)与"prefer spaces"(新,ACTIVE)共存,陈旧节点可能在查询中**排在新节点之前**。这是决定"explicit supersede 不能延迟"的承重事实。

### 1.3 dedupe 与 existing-memory 都是 statement-exact 匹配

`already_exists`([governance/dedupe.py:56](src/pulsara_agent/memory/governance/dedupe.py:56))与 engine 的 `_existing_memory_matches`([governance/engine.py:384](src/pulsara_agent/memory/governance/engine.py:384))都按 **`_normalize(statement)` + `scope` 精确比对**,只识别近重复。**LLM 因此看不见"同主题、不同陈述"的旧节点**——candidate 是"prefer spaces"时,它根本不知道"prefer tabs"存在。这是 v1 的硬前置(§4)。

### 1.4 engine(LLM) 与 executor(deterministic) 分工已定，且 canonical 扫描已在热路径

engine 每个 batch 都调 `_existing_memory_matches` 把匹配喂给 LLM([engine.py:163](src/pulsara_agent/memory/governance/engine.py:163)),prompt 已指示 LLM 用它判重。**canonical 扫描已经在 governance 热路径上,每个 batch 都跑**——这意味着 inline explicit supersede 不新增扫描,只是放宽既有扫描 + 加一个决策分支。

### 1.5 MemoryLifecycle 已建、已单测、但 governance 从不调用

`MemoryLifecycle` 由 UoW 构造并持有([canonical/unit_of_work.py:71](src/pulsara_agent/memory/canonical/unit_of_work.py:71)),`supersede`/`mark_stale`/`mark_contradicted` 均有单测。但 `governance/` 全目录**零调用**。能力 inert,等接线。

### 1.6 lifecycle.supersede 的真实落地形态（强制顺序来源）

`supersede` 把 `supersedes` 边 append 到**新节点**的 JSON-LD,再 `put_jsonld` 触发投影,然后 `set_status(old_id, SUPERSEDED)`([canonical/lifecycle.py:41-48](src/pulsara_agent/memory/canonical/lifecycle.py:41))。因为它 `get_jsonld(new_id)` 读新节点来挂边,所以 `new_id` 必须先写入——**这就是"write-then-retire"强制顺序的代码来源**。

### 1.7 maintenance 只有 event 桩，无决策类型、无 runner

`MemoryMaintenanceProposedEvent` 等已定义,`lifecycle` 也 emit `MemoryMaintenanceAppliedEvent`。但**没有** `MaintenanceProposal` 决策类型、没有 maintenance runner、没有调度器。所以"inferred supersession 走 maintenance pass"是一个**全新子系统**,不是改造既有。

### 1.8 UoW 同事务可见性已验证

`test_postgres_uow_dedupe_sees_uncommitted_same_transaction_node` 证明:同一 UoW 内,未提交的写能被后续读看到。这是 supersede 第 4 步(读新节点挂边)依赖的能力,已就绪。

## 2. 安全教义：coexist 是默认，supersede 是例外

supersede 的特殊性在于:**它是系统第一个会让已治理事实从召回中消失的操作。** 因此它遵循与 explainer 同源的原则——

> **错误的 supersede 比漏掉的 supersede 严重得多。**
> 漏掉 → 两条记忆共存(可恢复,轻微噪声);错误 → 销毁一条仍然有效的记忆(用户"我明明告诉过你 X"的崩溃时刻)。

### 2.1 supersede 的四个必要条件（缺一即降级为 coexist）

只有当下列**全部成立**,才允许 supersede;否则默认 coexist(即正常写新节点,不动旧节点):

```text
1. same scope          —— 跨 scope 永不替代（不同 scope = 不同上下文 = 共存）；executor 可机械校验（G3）
2. same subject        —— 同主题/同所指，而非仅 statement 字面相似；【v1 无结构化 subject 字段，executor 无法校验，仅靠 LLM 判断】
3. explicit intent     —— 用户明确表达替代意图，或（v2）结构化 key 冲突
4. strict update       —— 新记忆是旧记忆的严格更新，而非仅"相关"；同样依赖 LLM 判断
```

> 条件 1 是 executor 硬门(G3);**条件 2、4 在 v1 只能靠 LLM 判断**——没有结构化 subject 字段,executor 无从机械校验"同主题/严格更新"。真正把误伤锁死的不是这四条,而是 §5.2 的 **single-target + Preference-only + explicit-intent** 组合:即便 LLM 在条件 2 上误判,最坏也只销毁恰好 1 个 Preference 节点。v2 的结构化 `subject` 字段才能把条件 2 变成 executor 可校验的硬门。

> **第五条·替代物必须真正可用(ACTIVE viability)**:退休旧节点的前提不是"新候选写成功",而是"**新节点真正落为 ACTIVE**"。gate 可能把新候选判成 NEEDS_REVIEW/REJECTED(REJECTED 仍会落图),此时若退休旧 ACTIVE 节点,等于用一个召不回的节点换掉有效记忆。因此"是否真的 supersede"的最终判定发生在写入**之后**——新节点非 ACTIVE 即默认 coexist,旧节点保持原状。这与§6"实际动作先于记录"一脉相承。

### 2.2 不确定时 → coexist / skip，不是 supersede，也不是 contradict

不确定时的安全出口是 **coexist / skip**:写新节点(或不写),**旧节点保持 ACTIVE、仍可召回**,什么都不销毁。这是真正非破坏性的"我不确定"路径。

> ⚠️ **`mark_contradicted` 不是安全对冲——它比 supersede 更具破坏性。** 现状核实:`mark_contradicted` 把**两个**节点 status 都设为 `CONTRADICTED`([canonical/lifecycle.py](src/pulsara_agent/memory/canonical/lifecycle.py)),而 recall filter 默认只放行 ACTIVE([recall/service.py](src/pulsara_agent/memory/recall/service.py) `_passes_canonical_filter`)。所以 contradiction 的真实效果是**两个节点一起从 cheap recall 消失**(supersede 只移除 1 个)。把它当"都留着、只 warning"是错的。contradiction 整体推迟到 v2(§7),且需先解决一个语义前置(见下)。

```text
有把握「新严格替代旧」          -> supersede（旧 ACTIVE -> SUPERSEDED，仅旧节点退出召回）
不确定谁对 / 是否替代            -> coexist / skip（都留 ACTIVE，都可召回，零销毁）  ← v1 的安全出口
（contradiction 不在 v1）
```

supersede 要求 confidence;一旦没有把握,就 coexist——多一条共存记忆只是噪声,远轻于误杀。

### 2.3 这条教义如何映射到召回不变量

- supersede 成功后,旧节点 `status=SUPERSEDED`,被 §2.3「承重 filter」挡在召回外——此时该 filter 才从"恒空占位"变为真正做事。
- coexist/skip 时,新旧节点都 ACTIVE、都可召回;无 recency 排序意味着二者按 RRF 相关性竞争(§1.2),这是可接受的——没有任何记忆被销毁。
- **已知待解的 contradiction 语义冲突(v2 前置)**:`explain.py` 被设计为发 grounded `contradicted_by` claim——即 contradiction 本意是"可见 + warning";但 recall filter 让 CONTRADICTED **不可见**。**这两个意图在已提交代码里已经互相矛盾**(非本设计引入)。contradiction 落地前必须先裁定:**CONTRADICTED 是被过滤掉,还是带 warning 浮现?二者不能并存。** 这正是 contradiction 推迟到 v2 的真正阻塞点。
- 所有 lifecycle 影响**只在边/状态被真实写入后**才作用于召回——绝不凭语义相似度猜测。


## 3. 按触发源切分：explicit inline，inferred 低频

这是本文对"是否该让替代更低频"的核心回答。supersede 的两种触发源性质截然不同,**不能用同一频率/耦合策略**:

| 维度 | **Explicit**（用户说"把 X 改成 Y"） | **Inferred**（系统怀疑两条记忆冲突，无用户信号） |
|---|---|---|
| 检测频率 | inline，每个 governance batch | **低频 maintenance pass** |
| 应用耦合 | **同一 UoW，原子** write-then-retire | 独立事务，逐条 |
| 紧迫性 | urgent（用户在等，刚说完就可能追问） | non-urgent（无人等待） |
| 为何如此 | §1.2 无 recency → 延迟即暴露陈旧数据 | 昂贵(LLM 相似度推理) + 破坏性(值得深思) + 不急 |
| v1 是否做 | **是** | 否（留 v2） |

### 3.1 为什么 explicit 必须 inline 且原子（而非延迟到 maintenance）

§1.2 是关键:召回无 recency 排序。若用户说"以后用 spaces",系统写了"spaces"并提交、却把退休"tabs"延迟到下一次 maintenance,那么**下一个查询就可能召回陈旧的"tabs"**——对一个刚刚明确改口的用户,这是最糟的 UX。explicit = urgent = 必须原子 + 立即。

"write-then-retire 同一事务"不是"同时发生",而是**有序但原子**:先写新节点(拿到 `memory_id`),再在同一事务内退休旧节点;要么一起成功,要么一起回滚。绝不出现"新节点 ACTIVE + 旧节点仍 ACTIVE + 悬空 supersedes 边"的中间态。

### 3.2 为什么 inferred 应该低频 + 解耦（而非 inline）

inferred supersession(系统主动发现"这两条早先的记忆其实冲突")是:

- **昂贵**:需要对 in-scope 记忆做相似度/主题推理,不该压在每次写入的热路径上。
- **破坏性**:没有用户背书,误判风险更高,值得 maintenance 级的深思与可回溯。
- **不紧迫**:没有用户在等待结果。

所以它属于一个**独立、低频、可调度**的 maintenance pass(§1.7 显示这是全新子系统)。这正是"让替代更低频"这个直觉**正确适用的地方**——但适用对象是 inferred,不是 explicit。

### 3.3 成本现实：canonical 扫描已在热路径，inline explicit 不新增扫描

可能担心"每次 governance 都让 LLM 推理是否替代 = 昂贵"。但 §1.4:`_existing_memory_matches` **本就每个 batch 都跑**。inline explicit supersede 的边际成本只是:(a) 放宽这个已存在的扫描(statement-exact → scope+type),(b) 让 LLM 多一个决策分支。无 in-scope 匹配的写入 → 无 supersede 推理 → 仍然便宜。**不是新增一次昂贵扫描,而是复用一次已付费的扫描。**

> 反直觉但重要:把 explicit supersede 做成独立低频 job,反而会**制造** §1.2 警告的陈旧数据窗口。低频/解耦只属于 inferred。

## 4. v1 硬前置：放宽 `_existing_memory_matches`，让 LLM 看得见替代目标

§1.3 是 v1 的拦路石:`_existing_memory_matches` 当前 statement-exact,意味着 candidate 是"prefer spaces"时,LLM **永远看不到**"prefer tabs"节点——它无从指认 supersede 目标。这与 supersede 的需求(同主题、不同陈述)恰好相反。

所以 v1 的第一项工作不是写 supersede,而是让 LLM"看得见",并**改名**(放宽后它已不是 dedupe 辅助):

> **新增 `related_existing_memories`**(取代 supersede 视角下的 `_existing_memory_matches` 语义),从「同 statement + 同 scope」放宽到「**ACTIVE + 同 scope + 同 type**」,返回项带 `memory_id` / `statement` / `status`,让 LLM 能按 id 指认要替代的旧节点。改名是因为放宽后它返回的是"相关已有记忆上下文",不再是"精确重复匹配",沿用旧名会误导后续读者以为它仍服务判重。

这是放宽一个**已经每 batch 都在跑**的过滤(§1.4),不是新增扫描。三个配套约束:

- **判重逻辑不变**:`already_exists`(statement-exact)仍是判重的唯一权威,**保持不动**;放宽只作用于"喂给 LLM 看的相关上下文",不改判重语义。二者职责分离。
- **必须排序,不能随便取 top-N**:同 scope+type 记忆变多后,真正要 supersede 的旧节点可能被挤出 prompt → LLM 指认不到 → 功能静默失效。排序须至少:`ACTIVE only → 同 scope → 同 type → 与 candidate.statement / user_quote 有词重叠者优先 → 稳定排序`,保留上限(沿用 `limit=10`)。
- **词重叠只是 v1 stopgap**:词重叠是"同主题"的**不完美代理**——"prefer tabs"vs"prefer spaces"仅在"prefer"弱重叠,"dark theme"→"light theme"靠"theme"。Preference-only + 单 scope 内数量小时可接受,但这正是 **v2 需要结构化 `subject` 字段**的理由。本排序是 v1 权宜,不是终态。

## 5. 新决策类型 + executor 校验门

### 5.1 SupersedeAndSubmitDecision —— 第一个跨命名空间的决策

现有 4 类决策只引用候选池 entry(§1.1)。supersede 是**第一个同时引用 pending 候选与已治理 canonical 节点**的决策:

```python
class SupersedeAndSubmitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["supersede_and_submit"] = "supersede_and_submit"
    target_entry_id: str                      # pending 池条目（新记忆来源）
    candidate: MemoryCandidate                # 要创建的新记忆
    superseded_memory_ids: tuple[str, ...]    # 要退休的【已治理 canonical 节点 id】 ← 新命名空间
    reason: str
```

`superseded_memory_ids` 是真正新颖的字段——是 `mem:*` canonical id,不是池 `entry_id`。把它加进 `GovernanceDecision` 判别联合([candidates/pool.py:104](src/pulsara_agent/memory/candidates/pool.py:104))。**v1 只新增这一个决策类型**;`MarkContradictedDecision` 推迟到 v2(§2.2 已说明 contradiction 比 supersede 更具破坏性,且有未裁定的 status-vs-warning 语义冲突)。

### 5.2 executor 校验门：比普通决策更严，因为后果是销毁

沿用既有分工(§1.4):**LLM 提议,deterministic executor 否决/降级**。但 supersede 的 executor 门更硬,因为 blast radius 是销毁。任一门失败即**降级为 coexist / skip**(v1 没有 contradict 这个降级目标):

```text
LLM 提议 supersede（它有对话上下文，能检测"用户说替代"——这是唯一能检测它的地方）
executor 强制以下不可越过的门，任一失败即【降级为 coexist / skip】：
  G0  candidate.kind ∈ _SUPERSEDABLE_TYPES（v1 = {"Preference"}）  且 每个 old 节点 type 也在其中
  G1  superseded_memory_ids 每个都存在               （查 uow.graph）
  G2  且都是 ACTIVE 状态                             （不退休已 REJECTED/SUPERSEDED 的）
  G3  且与新 candidate 同 scope                       （跨 scope 永拒，守教义条件 1）
  G4  新 candidate 通过 dedupe（uow.graph）未判重     （重复则 skip，根本不该新建）
  G5  len(superseded_memory_ids) <= 1（v1 单目标；多目标留 v2，避免 merge-hash 复杂度）
```

这与 `write_gate` 把不合格写入降级为 NEEDS_REVIEW/REJECTED 是同一种"LLM 有灵活性去检测,executor 守安全底线"的模式。新增一个 `_validate_supersede_targets`(against canonical graph,`_validate_decision_targets` 的兄弟,后者只查候选池)。

> **`_SUPERSEDABLE_TYPES` 是 executor 强制的常量,不只是 prompt 指示**——与"LLM 提议、executor 否决"一致。v1 写死 `frozenset({"Preference"})`,v2 加类型时一行拓宽。Claim/Decision/Observation 的"改口"更常是纠错/冲突/上下文变化而非替代,危险度更高,不进 v1。

> **诚实的局限(必须写明)**:G0–G5 全是 executor 可机械校验的,但教义条件 2「same subject」**无法**被 executor 在 v1 校验(没有结构化 subject 字段)。所以"同主题"判断**仍依赖 LLM**。同 scope 内两条合法共存的 Preference("prefer concise summaries" + "prefer dark theme")可能被 LLM 误判为替代。真正限制误伤的不是这些门,而是 **single-target(G5) + high confidence + explicit intent** 的组合——把最坏情况锁死在恰好 1 个节点。

### 5.3 LLM 提示侧的约束（v1）

- 仅当用户在对话中**明确表达替代**("把偏好改成…"、"别再用 X,用 Y")时才提议 supersede;源权威为 `EXPLICIT_USER_INSTRUCTION`(最高)。
- **不做语义主题猜测**:不因"两条看起来像在讲同一件事"就 supersede;那是 v2 的 inferred 路径。
- 拿不准谁替代谁 → 提议 `skip`(或正常 submit 走 coexist),**不要** supersede。(v1 无 `mark_contradicted` 选项。)

## 6. v1 原子落地序列（单个 UoW 内）

executor 处理 `SupersedeAndSubmitDecision` 时,在**一个** `with uow:` 块内,严格按序:

```text
1. dedupe new candidate via uow.graph               # G4：新 candidate 已存在原样副本？→ skip
2. _validate_supersede_targets(superseded_memory_ids, candidate, uow.graph)   # G0+G1+G2+G3+G5
   #   失败 → 标记降级（尚未发生任何 lifecycle mutation）
3. outcome = uow.memory_write_service.submit(cand)   # 写新节点；submit 返回 MemoryWriteOutcome
4. 仅当「预校验通过 且 新节点真正落为 ACTIVE」才执行：
       uow.lifecycle.supersede(old_id=<the one target>, new_id=<new ACTIVE id>, ...)
   #   → 把 supersedes 边 append 到【新节点】文档，re-put 触发投影（§1.6）
   #   → set_status(old_id, SUPERSEDED)
   #   否则（降级 / 写失败 / 新节点非 ACTIVE）→ 不退旧节点（coexist）
5. uow.decisions.append_decision(record)             # record 反映【实际动作】，非 LLM 原始提议
6. uow.outbox.append_decision(record, ...)
   # ---- COMMIT（__exit__）----
7. 提交成功后才 emit 事件：MemoryWriteResult + old_id 的 MemorySuperseded
```

四个关键点:

- **先校验、后写入(决定先于 lifecycle 变更)**:old target 校验只需 `candidate.scope`(在决策 payload 里),不需要新节点已写入,所以放在 submit 之前。校验失败时**尚未发生任何 lifecycle mutation**;随后按 coexist 路径写新节点,但不退休旧节点。若 dedupe 命中、candidate invalid 或写入失败,则按既有 skip/write_failed 路径处理。
- **实际动作先于记录**:"是否真的 supersede"取决于「预校验通过 + 新节点 ACTIVE(§2.1 第五条) + 真的调了 `lifecycle.supersede`」三者同时成立。任一不成立(预校验降级 / 写失败 / 新节点非 ACTIVE)都按 coexist 落地,旧节点保持原状。
- **顺序强制**:create-then-supersede,新节点 id 来自第 3 步写入结果。这是 §1.6 决定的,非选择。第 4 步 `get_jsonld(new_id)` 依赖同事务可见性(§1.8)。
- **事件后置**:事件是提交后的派生通知,不是真相源、不是提交锚点。崩溃在 commit 前 → 整体回滚无 split-brain;commit 后 emit 前 → 状态已一致,事件可由 reconcile 补发。

> **决策记录诚实性 + 审计对称(step 5)**:记录的 decision **当且仅当真的退休了旧节点**才是 supersede;其余一切情况(降级 / 写失败 / 非 ACTIVE)记为 coexist,审计读到的永远是真实发生的事,而非被否决的意图。并且——**supersede-origin 决策一律不在候选池追加 governance audit candidate**(无论成功还是降级)。理由:退休事实已有更丰富的专属审计轨(supersede 事件 + 新节点上的 `supersedes` 边 + 写结果里记录的退休 id),再产一条 governance-origin 候选只会用一份用户偏好副本污染候选池,且会让"真退休"反而比"do-nothing coexist"少一条记录,造成不对称。成功与降级都不追加,二者审计表现一致。

## 7. v1 / v2 边界

### v1 —— explicit-only，inline，原子

范围严格收窄,只做"用户明确要求的 Preference 替代":

```text
- 新增 related_existing_memories（ACTIVE + scope + type，带词重叠排序），让 LLM 看得见替代目标（§4，硬前置）
- 新增 SupersedeAndSubmitDecision（§5.1，仅此一个决策类型）
- executor 校验门 G0–G5 + 降级为 coexist/skip（§5.2），新增 _validate_supersede_targets
- _SUPERSEDABLE_TYPES = frozenset({"Preference"})，executor 强制（§5.2）
- 单目标（G5）：v1 最多退休 1 个 old 节点
- 仅当新节点真正落为 ACTIVE 才退休旧节点（§2.1 第五条）；否则 coexist
- supersede-origin 决策不追加 governance audit candidate（成功/降级一致，§6 审计对称）
- LLM 仅在用户显式替代意图时提议 supersede（§5.3）
- 校验先于 lifecycle 变更、实际动作先于记录的单 UoW 原子序列（§6）
- 接通后：§2.3 承重 filter 真正生效；explainer superseded_by 有真实边可依
```

不做:`MarkContradictedDecision` / contradiction、Preference 以外的类型、多目标、语义主题自动检测、inferred supersession、maintenance runner、subject 结构化字段。

### v2 —— inferred supersession 走低频 maintenance pass

这才是"低频/解耦"直觉正确适用的地方(§3.2):

```text
- 新增 MaintenanceProposal 决策类型 + maintenance runner（§1.7：全新子系统）
- 由 recall-usage 信号驱动（repeated-recall-never-used / frequently-contradicted / stale-warning 频发）
- 逐条独立事务；不在写入热路径
- contradiction：先裁定 CONTRADICTED 是"被过滤"还是"带 warning 浮现"（§2.3 的语义阻塞），再接 MarkContradictedDecision
- 拓宽 _SUPERSEDABLE_TYPES：在各类型的替代语义被想清后逐个加入
- 多目标 supersede（解除 G5），配 outbox merge-hash
- subject 结构化字段：给记忆类型加 subject，supersede 可按 (scope, subject, type) 确定性触发，取代 v1 的词重叠 stopgap
- 边界不变：maintenance 只产 proposal；canonical 变更仍走 lifecycle 写路径，不旁路
```

### 不建议（任何阶段）

```text
- 凭 embedding/lexical 相似度自动 supersede（false-positive 销毁有效记忆）
- 跨 scope supersede
- 把 explicit supersede 做成低频 job（制造 §1.2 陈旧数据窗口）
- 不确定时 supersede（v1 该 coexist/skip）
- 把 contradiction 当"安全对冲"（它让两节点都退出召回，比 supersede 更狠，§2.2）
- 在 related_existing_memories 放宽前就让 LLM 提 supersede（它看不见目标，§4）
- 改动 already_exists 的 statement-exact 判重语义（只放宽喂给 LLM 的上下文，§4）
```

## 8. 测试矩阵（v1）

| 测试 | 断言要点 | 守 |
|---|---|---|
| explicit Preference supersede 原子落地 | 用户改 A→B 后:B=ACTIVE、A=SUPERSEDED、`supersedes` 边存在、**不双 active** | §3.1 / §6 |
| supersede 后旧节点不被召回 | A 进 `filtered_ids`,projection 只含 B | §2.3 承重 filter |
| 非 Preference 类型拒绝降级 | candidate.kind=Claim 提 supersede → executor 降级为 coexist,旧节点不动 | G0 |
| 跨 scope 拒绝降级 | LLM 提 supersede 但新旧 scope 不同 → 降级为 coexist,A 仍 ACTIVE | G3 |
| 目标不存在/非 ACTIVE 降级 | `superseded_memory_ids` 指向缺失或已 SUPERSEDED 节点 → 降级,不报错销毁 | G1+G2 |
| 多目标拒绝（v1 单目标） | `len(superseded_memory_ids) > 1` → 降级 | G5 |
| 新节点非 ACTIVE 不退旧 | gate 把新 Preference 判成 NEEDS_REVIEW/REJECTED → 旧节点保持 ACTIVE、不退休 | §2.1 第五条 |
| 校验先于 lifecycle 变更 | old target 校验失败时,新节点仍按 coexist 写入,旧节点保持 ACTIVE,无 `set_status`/`supersedes` 边;若 dedupe/写失败则按既有 skip/write_failed 路径处理 | §6 |
| 同事务可见性 | 第 4 步 supersede 能读到第 3 步未提交的新节点 | §1.8 / §6 |
| 崩溃原子性 | commit 前抛错 → 新节点未现、旧节点仍 ACTIVE、无悬空边 | §6 |
| 决策记录诚实 | 降级/写失败/非 ACTIVE 时记录写 coexist,非 LLM 原始 supersede 提议 | §6 step 5 |
| 审计对称（不追加 audit candidate） | supersede 成功与降级都不在候选池留 governance-origin 副本 | §6 审计对称 |
| dedupe 语义不变 | 放宽 `related_existing_memories` 不改 `already_exists` 判重 | §4 |
| 无显式意图不 supersede | 用户只是陈述新偏好、未表达替代 → coexist,不退休任何旧节点 | §5.3 |
| explainer 接真实 supersedes 边 | supersede 后 `memory_explain` 旧节点返回 grounded `superseded_by`,confab=0 | §2.3 |
| **【必做】real-LLM：明确改口 → supersede** | 用户明确改口 → LLM 出 supersede,旧 Preference 退休 | §5.3 / 验收 |
| **【必做】real-LLM：语义不强 → coexist** | 仅陈述新偏好、无替代意图 → LLM 出 submit/correct,旧节点保留 | §5.3 / 验收 |

> **real-LLM 验收是必做项,非可选**:supersede 的安全链有一段(explicit intent / same subject)只能由 LLM 判断(§2.1 条件 2/4),仅"类型对、事务对"证明不了模型会按教义走。上面两条 real-LLM 用例(gated,默认 skip)是这条链的最低验收。

## 9. 一句话收束

> supersede 是系统第一个销毁已治理事实的操作,因此 **coexist/skip 是默认与不确定时的安全出口,supersede 是需多条件齐备的例外**(注意:`mark_contradicted` 不是安全对冲——它让两节点一起退出召回,比 supersede 更狠,整体留 v2)。它有两个正交轴:write-then-retire 顺序是强制的(§1.6),真正的设计杠杆是按触发源切分检测频率——**explicit(用户明说)走 inline 原子,因召回无 recency,延迟即暴露陈旧数据;inferred(系统推断)才走低频 maintenance**。v1 收窄到 **explicit-only + Preference-only + 单目标**,硬前置是新增 `related_existing_memories`(ACTIVE+scope+type,带排序)让 LLM 看得见替代目标,且校验先于 lifecycle 变更以保决策记录诚实;contradiction 语义裁定、类型拓宽、多目标、inferred maintenance、subject 结构化全留 v2。canonical 扫描已在 governance 热路径,inline explicit 不新增扫描——把它做成低频 job 反而制造陈旧窗口。


