# Pulsara 记忆召回方案可行性分析

_Created: 2026-06-15_

## 评估对象与依据

本文是对 `MEMORY_RECALL_FINAL_STORAGE_ARCHITECTURE.zh.md`（下称"最终方案"）的批判性可行性分析。结论建立在三类证据之上：

1. 最终方案及其两份参考调研（`MEMORY_RECALL_SYSTEM_SURVEY.zh.md`、`MEMORY_RECALL_MATURE_FRAMEWORKS_SURVEY.zh.md`）。
2. 对 `pulsara_agent` 当前代码的直接核对（所有承重结论均落到 `file:line`）。
3. 一次覆盖五个维度的并行 critique（存储一致性、召回延迟、本体充分性、Oxigraph 选型、不变量与分期）。

本文所有"代码现状"主张都经过独立 grep / 读源码验证，未经验证的推断会显式标注。

## 一、总体判断

**最终方案的边界哲学是对的，工程排序是反的。** 它正确吸收了 8 个成熟框架的关键约束——候选池不是召回源、recency 不创造相关性、transcript 召回与 memory 召回分离、召回失败必须结构化而非让模型幻觉——这些是宝贵且站得住的设计直觉。

但作为一份标题写着"最终整合方案 / 最终不变量 / 最终架构图"的**落地蓝图**，它存在一个根本性的认知错位：

> 它把一个**尚未动工**的召回系统，描述成了一个"写入侧已基本收敛、召回侧只差接线"的系统。

文档语气（§3"写入侧已经基本收敛"、§5.3"v1 的接口应预留"、§12 Phase 1"不追求 fancy，先保证边界正确"）暗示地基已浇好。而代码现状是：**召回是 0% —— 不是"接线"，是地基的钢筋都还没绑。** 更严重的是，方案把全系统**最贵、最未实现、最违背自身引用经验**的一步（跨 HTTP 的逐候选图扩展）放进了 v1 关键路径，并称之为"核心创新点"；而真正应该先做的 lexical 基线、评测护栏、原子写入、代码级护栏，要么被当成已完成，要么被后置。

可行性分级如下：

| 主张 | 可行性 | 关键约束 |
|---|---|---|
| 召回边界哲学（只读 canonical、候选池非召回源、recency 不创造相关性、双工具分离） | **可行且应保留** | 与两份 survey 结论一致 |
| v1 = 进程内 lexical 召回 + canonical 裁定 + projection | **可行** | 需先消除 `query()` 的 `NotImplementedError` |
| §5.1 cheap auto recall 的 hard timeout / fail-soft | **当前不可实现** | 同步 urllib + inline await，见 §三.1 |
| §6.4 OxigraphGraphExpansion 作为"核心创新"且在 v1 关键路径 | **不可行（需降级）** | N+1 over HTTP；遍历的边从未被写入；延迟预算崩溃 |
| §6.5 Ranker 的 8 档类型优先级 | **不可建（键于不存在的类型）** | 本体只有 5 类，3 档映射到空 |
| "三个不可替代的存储" + 治理双写 | **结构性脆弱** | 非原子双写 = split-brain；运营成熟度最低的存储承载黄金真相 |
| "graph 召回优于 lexical"这一核心赌注 | **不可证伪（缺评测）** | 全仓无 eval harness、无 golden set |

一句话总判断：**方案的愿景值得保留，但它现在描述的不是一个可在 v1 落地的系统，而是一个把最高风险项前置、且与自身引用的成熟经验相悖的系统。最小可行修正是把图扩展退化为"需用评测数据赢得位置"的可选 reranker，先交付便宜、可测、原子的 lexical 基线。**

## 二、现行状态：方案隐含的存在性 vs. 代码实际

最终方案默认了一批基础设施"已存在、待接线"。逐项核对的结果是大面积落空。这不是"细节没写到"，而是**工作量估计严重失真**——它直接导致 Phase 1 被低估为"最小召回"，实则是一个完整子系统。

### 2.1 召回侧：0% 实现

| 方案隐含/声称 | 代码实际 | 证据 |
|---|---|---|
| projection 基础设施已就绪，"只需接真实 recall service" | `project()` **永远返回 `None`**；`DurableMemoryHooks`/`ReflectiveMemoryHooks` 从未 override 它，只继承 `NoopMemoryHooks` 的空实现 | 唯一实现在 [hooks.py:218](src/pulsara_agent/runtime/hooks.py:218)（返回 None）；[durable_hooks.py:34,85](src/pulsara_agent/memory/hooks/durable.py:34) 的方法列表里无 `project` |
| `MemoryRecallService` 协议待定义（Phase 1 任务） | 全仓**不存在** `MemoryRecallService` / `RecallService` | grep 零命中 |
| `memory_search` / `memory_get` 只读工具 | 全仓**不存在**这两个工具 | grep 零命中 |
| §7 SearchIndex、§10 RecallTrace 表 | Postgres schema 只有 sessions/runs/turns/agent_events/artifacts/tool_execution_records；**无** search_index / recall_traces | grep `search_index`/`recall_trace` 零命中；[postgres_schema.py](src/pulsara_agent/storage/postgres_schema.py) |

`_project_memory` 的调用链是真实存在的（[agent.py:402-446](src/pulsara_agent/runtime/agent.py:402)：emit `ProjectionRequested` → `await memory_hooks.project()` → emit `ProjectionReady/Failed`），projection 事件与 fence 注入也都在（[context.py:165](src/pulsara_agent/runtime/context.py:165)）。但因为 `project()` 恒返回 `None`，这条链**每轮都空转**：emit 一个 requested、拿到 None、emit 一个 included 为空的 ready。**外壳齐全，内核为零。**

### 2.2 本体侧：Ranker 与图扩展键于不存在的类型/关系

| 方案引用 | 代码实际 | 证据 |
|---|---|---|
| §6.5 Ranker 按 ~8 档类型排序（含 UserIdentity / ExplicitRule / ToolQuirk / Workflow / ProjectFact） | 本体**只有 5 种**：Claim / Decision / Preference / ActionBoundary / Observation。其余类型 grep **零命中** | [memory.py:13-17](src/pulsara_agent/ontology/memory.py:13)；`grep -rE 'UserIdentity\|ExplicitRule\|ToolQuirk\|Workflow\|ProjectFact' src/` = 0 |
| §2.3 节点类型 Project / ArtifactRef / ProjectionPolicy / MemoryPolicyVersion | **均不存在**（无 entity、无 term） | grep 零命中 |
| §2.3 关系 aboutEntity / touchesArtifact / usesTool / createdFrom / storedAs | **均无 term** | grep 零命中 |
| §6.4 遍历 supersedes / supersededBy / contradicts / contradictedBy 决定"替代/矛盾" | 这些关系**从未被任何 producer 写入**。ledger 的 `_add_relation` 只发 `PROVIDES` 与 `SUPPORTS` | [ledger.py:179,217,262,306,344,386](src/pulsara_agent/memory/canonical/ledger.py:217)；supersedes/contradicts 仅出现在 ontology 定义与 codec force-list，无生产调用 |

也就是说：§6.5 的 8 档优先级里有 **3 档**（含第二高的 UserIdentity/ExplicitRule）映射到系统永远存不出、查不到的类型；§6.4 这个"核心创新"遍历的 supersedes/contradicts 边**今天跑出来是空集**——它的招牌决策（"是否应被更近/更权威节点替代"）**根本无法触发**。

### 2.3 实际被写入的关系面

为避免以偏概全，明确**真正被生产**的关系只有：`PROVIDES`（tool_result → evidence）、`SUPPORTS`（evidence → memory node），以及节点自带的 `scope` / `status` / `hasEvidence` / `appliesWhen` / `doNotApplyWhen` / `basedOn` / `createdFrom`（取决于 entity）。§6.4 列举的 ~13 个 facet 里，**关系类 facet 大部分在实践中是空的**。在一张边大面积未填充的图上运营 triple store 去"遍历关系"，是在为未被使用的能力付运营代价。

### 2.4 一句话现状

写入侧并非方案所说的"基本收敛"——它能把 5 类节点连同 SUPPORTS 边写进 Oxigraph，但（a）双写非原子（§三.3），（b）supersedes/contradicts 等关键关系无人生产，（c）时间戳无类型（§三.6）。召回侧则是从 `project()` 往下**整体为零**。方案的分期必须以此为真实起点重写。

## 三、核心质疑

按"威胁程度 × 是否被方案忽视"排序。每条给出论点、证据、为何致命。

### 3.1【BLOCKER】同步 urllib + inline await，使 §5.1 的 "hard timeout / fail-soft" 字面意义上无法实现

这是最强的单点反驳，也是方案完全没有意识到的。

- `project()` 在 [agent.py:416](src/pulsara_agent/runtime/agent.py:416) 被 **inline `await`**，位于每轮循环顶部（[agent.py:168](src/pulsara_agent/runtime/agent.py:168)），且**没有任何 `asyncio.wait_for` 或超时包裹**（`agent.py:615` 处的 `timeout=0.05` 是无关的 pending-task 等待）。
- Oxigraph 客户端是 `urllib.request.urlopen` **同步阻塞**调用（[oxigraph.py:118,140](src/pulsara_agent/graph/oxigraph.py:118)），`timeout_seconds` 默认 10.0，无 async、无连接池。

致命点在于：**给一个内部卡在同步 `urlopen`（C 层 socket read，非 yield point）的协程套 `asyncio.wait_for` 并不能取消它。** 事件循环会被冻结，cancellation 无法触发，整个 turn（包括流式输出、并发工具）会一直挂起，直到 urllib 自己的 10s 超时到期。

于是 §5.1 反复强调的两条最基本的延迟保障——"hard timeout（300–800ms）"与"fail-soft：失败时不阻塞主请求"——**在当前底座上是写不出来的**。方案把召回宣传为"便宜、有界、非阻塞"，而底层实现使它"昂贵、无界、完全阻塞"。

> 这条必须作为任何召回上线的**前置项**，排在 N+1 问题之前。

### 3.2【BLOCKER】§6.4 的逐候选图扩展是 N+1-over-HTTP，且无批量、无参数化查询

§4 规定每个候选 id **必须**回 Oxigraph 做 status/relation/evidence/scope 扩展，§6.4 列举每候选 ~13 个 facet。但：

- 唯一读原语 `get_jsonld` 只取单 subject 自身三元组 + 一层 blank-node OPTIONAL，**不跟随 named-node 关系目标**（[oxigraph.py:55-65](src/pulsara_agent/graph/oxigraph.py:55)）。于是每个相关节点（每条 evidence、每个 basedOn decision、每个 supersedes 目标）都要**各自再来一次 `get_jsonld`**。
- 逆向边（supersededBy/contradictedBy）需要各发一条 `SELECT ?s WHERE { ?s mem:supersedes <cand> }` 全模式查询。
- `query()` 在传 bindings 时直接 `raise NotImplementedError`（[oxigraph.py:101-103](src/pulsara_agent/graph/oxigraph.py:101)），所以**没有参数化或批量扩展的可能**。
- `find_by_type` 本身已是 N+1（先 SELECT ?s，再对每个 subject `get_jsonld`，[oxigraph.py:83-99](src/pulsara_agent/graph/oxigraph.py:83)）。

K=3–5 个候选、典型扇出下，这是每轮 **15–40 次阻塞 HTTP POST**，且 urllib 无连接复用、每次新开 TCP。"便宜的 projection"实为一扇数十次串行网络调用。被方案引为先例的 scope-recall-hermes 恰恰是**进程内** Python over in-memory graph（`graph.py:259-303` 在内存里算 hop 权重），它**不能**证明"跨 HTTP 逐候选扩展"在 800ms 内可行。

### 3.3【BLOCKER】治理双写非原子 = split-brain，而召回正建立在"canonical graph 是唯一真相"之上

`governance.apply_decision` 顺序执行**三个独立、无事务**的写：`memory_write_service.submit`（经 ledger 写 GraphStore，HTTP）→ `event_log.extend`（另一存储）→ `candidate_pool.append_decision`（独立 `psycopg.connect` 事务，[candidate_pool.py:299](src/pulsara_agent/memory/candidates/pool.py:299)）。无 2PC、无 outbox、无幂等键、无补偿回滚。

任一步之间崩溃，会留下 split-brain：**Oxigraph 里有 canonical `mem:*` 节点，却没有对应的 GovernanceDecisionLog 行**（或反之）。这直接证伪了 §2.2"GovernanceDecisionLog 是 append-only workflow truth"——如果它能在任意部分失败时与 canonical 状态分叉，它就不是可靠的审计真相。

更深一层：**即使在单个存储内部，节点也非原子。** ledger 先 `put_jsonld` 写节点（[ledger.py:200-215](src/pulsara_agent/memory/canonical/ledger.py:200)），再逐条 evidence 发 `_add_relation`（[ledger.py:216-217](src/pulsara_agent/memory/canonical/ledger.py:216)）作为独立 graph mutation；`OxigraphGraphStore` 是无事务句柄的 HTTP 客户端。`put_jsonld` 成功、SUPPORTS 边未写完时崩溃，会产生一个 **evidence 链被截断的 canonical 节点**——而 §6.4 正是靠遍历这些 supports/hasEvidence 边来裁定可召回性。**于是一个半写入节点会被当作事实静默召回，其证据链却是残缺的。** 方案把召回正确性完全建立在图扩展上，而写路径无法保证它要扩展的图是内部自洽的。

### 3.4【BLOCKER】"normal recall 只读 Oxigraph"这条不变量自相矛盾

§4 明文写"normal memory recall 只读 Oxigraph canonical graph"。但同一文档的 §6.3 CandidateGenerator 把 **PostgreSQL SearchIndex 的 lexical/FTS/BM25** 作为候选来源，§5.1 cheap recall 也要走完整 pipeline（含 CandidateGenerator）。所以正常召回**必然读 Postgres**。

这不是文字游戏，它暴露了事实模型没想清：**Postgres SearchIndex 到底是不是召回路径的一部分？** 如果是（实际如此），那"只读 Oxigraph"是假的；如果不是，CandidateGenerator 就只能靠 Oxigraph SPARQL 全图扫描产候选——又回到 §3.2 的延迟死局。这条不变量必须重述为可执行的版本（见 §四.G）。

### 3.5【BLOCKER】default `InMemoryGraphStore` 不能跑 SPARQL，Phase-1 图管线在默认后端没有入口

§12 Phase 1 把"InMemory / Oxigraph-backed canonical graph search"并列，仿佛两个后端都支持 CandidateGenerator → OxigraphGraphExpansion → Ranker 管线。**实际不支持**：`InMemoryGraphStore.query` 与 `update` 都 `raise NotImplementedError`（[in_memory.py:69-73](src/pulsara_agent/graph/in_memory.py:69)），只有 `find_by_type` 可用。

后果：

- §6.3 的 coarse SPARQL 与 §6.4 的逐候选扩展在默认后端**不可能跑**。
- 整个 graph-aware 管线只在"外部 Oxigraph server 可达 localhost:7878"时才工作。
- 跑该管线的测试在无 server 时**静默跳过**（`oxigraph_available()` gating），于是召回回归能在 CI 蒙混过关。

而 Oxigraph **不是嵌入式**——`pyoxigraph` 不是依赖，`OxigraphGraphStore` 是 HTTP 客户端（[oxigraph.py:26-31](src/pulsara_agent/graph/oxigraph.py:26)）。召回的全部性能特征因此被一个进程外 HTTP server 的可用性与延迟锁死，与"嵌入式/便宜"的隐含框架矛盾。

### 3.6【HIGH】appliesWhen/doNotApplyWhen 是自由文本，"便宜的 ActionBoundary 触发匹配"无机器可匹配底座

自动召回的最高优先级是"current task 适用的 ActionBoundary"（§5.1），§6.4 也把 appliesWhen/doNotApplyWhen 列为"是否适用"的判定输入。但这两个字段在 entity 上是裸 `str`（[action_boundary.py:21-22](src/pulsara_agent/entities/memory/action_boundary.py:21)），按普通字面量序列化（codec 只 typed boolean/integer）。

没有结构化条件、谓词、tag、scope-glob 或任何可匹配字段。要判断"这条 boundary 是否适用于当前任务"，只能对自由文本做 **NL 解释（LLM 调用）或模糊子串匹配**——前者绝不"便宜"且本身就是数百毫秒级的热路径延迟项，后者低精度（正是 scope-recall 警告的反模式）。**最高价值的记忆类型，其触发匹配恰恰是 §5.1"便宜、hard-timeout、有界、lexical"前提所禁止的那条昂贵路径。**

### 3.7【HIGH】codec 不给时间戳打类型，破坏 §6.5 的 recency 不变量

§6.5 的 recency 规则（"Recency never creates relevance"）与 §6.4 的 stale/时效替代判定，都依赖比较 `staleAfter/expiresAt/createdAt/updatedAt`（[memory.py:35-38](src/pulsara_agent/ontology/memory.py:35) 已定义）。但手写 codec **只给 `xsd:boolean` 和 `xsd:integer` 赋 RDF 数据类型**（[jsonld_codec.py:15-16,314-317](src/pulsara_agent/graph/jsonld_codec.py:15)），其余一切字面量——包括所有时间戳——round-trip 成**无类型字符串**。

在 SPARQL 里按无类型字符串做排序/范围过滤是脆弱的字典序比较，无法用 `xsd:dateTime` 比较。于是"freshness only after relevance"的加分与 stale 检测，**建立在存储层无法可靠比较的字段上**。这悄悄削弱了方案从 scope-recall-hermes 借来的、唯一一条 recency 不变量。

### 3.8【HIGH】"graph 召回优于 lexical"是全文核心赌注，却不可证伪（无评测）

方案的中心价值主张——§1"Oxigraph typed graph 决定候选能否、为何、以什么证据和边界进入 projection"、§6.4"这是 Pulsara 的核心创新点"、§14"Oxigraph graph projection 才是 Pulsara 的核心差异"——是一个可证伪的经验命题：图感知裁定比朴素 lexical top-k 召回得更好。但：

- 全仓**无 eval harness、无 ground-truth/golden set、无 precision/recall 度量、无 baseline 对比**：`grep -rEi 'precision|recall_eval|ground.?truth|golden|relevance_judg' src/ tests/` = 0。
- §10 的 recall_traces 表（捕获 included/filtered/later_confirmed）既不存在，也只是 telemetry 而非带标注的评测。
- §12 的行为断言（"active preference 可召回""unrelated 不召回"）是手挑用例的二值冒烟测试，**永远无法让 graph-gating 输给 lexical**。

两份 survey 自己的结论恰恰相反——都建议 v1 先做便宜稳定的 lexical recall，不要一上来上图扩展。方案在 §1 列了这些教训，却在架构上把最重的图扩展放进 v1 关键路径，且其优越性要到 Phase 3 才"测试"。**核心差异被断言、而非被测量，且按当前写法结构性地不可证伪。**

### 3.9【MEDIUM】"两个逻辑 PostgreSQL 角色"是文件组织，不是架构

§开篇把 Postgres 分成 Runtime Truth 与 Durable Sidecars 两个逻辑角色，并 hedge"不一定必须是两个物理实例"。实际是：**一个 DSN、一个 schema**，表只是拆在两个 SQL 常量/文件里（`RUNTIME_TRUTH_SCHEMA_SQL` vs `CANDIDATE_POOL_SCHEMA_SQL`）。sidecar 表**硬 FK 进** runtime 表（[candidate_pool.py:414-416](src/pulsara_agent/memory/candidates/pool.py:414)），无法迁到第二个物理实例而不动 schema——hedge 描述的是一个代码已经堵死的选项。文档还自相矛盾：ArtifactStore 被放进 sidecar 层（§2.2），但 `artifacts` 表定义在 runtime-truth schema 文件里。一个不可拆、FK 耦合、且自身对"哪张表属于哪层"都不一致的"逻辑角色"，是命名约定，不是存储边界。

### 3.10【MEDIUM】`do_not_write_back` 只是 prose，无任何强制

[context.py:165](src/pulsara_agent/runtime/context.py:165) 注入的 fence 是纯文本提示。§3 把"防止模型把召回内容当事实写回"列为关键边界，但**全链路无任何代码层校验**：reflection/governance 生成候选时，不会检查内容是否来自本轮 projection echo。模型只要复述了召回内容，reflection 就可能把它当新证据再次入池 → 记忆自我增殖。一个被反复强调的"关键不变量"，实际只靠模型自觉。

### 3.11【根因】Oxigraph 作为独立 triple store 是 accidental complexity，而非差异化

把上面多条串起来，会指向一个结构性判断：**单独引入 triple store 制造了一个本可避免的问题，却没带来对应规模下的收益。**

- Postgres 已是候选池、治理日志、artifact、event log 的 system of record。把 canonical memory 放进物理隔离的 HTTP 存储，**正是双写原子性缺口的直接成因**（§3.3）。若 memory 节点是同库 Postgres 行，整个 `apply_decision` 可以是一个 `BEGIN/COMMIT`。
- §6.4 的 1–2 跳 typed 扩展（数百到数千节点/用户）是**递归 CTE 的教科书甜区**，relational 模型在此规模无劣势。
- 真正的差异化（typed 节点 + 8 态 status lifecycle + relation-aware ranking）**是本体与 ranker 的属性，store-agnostic**；RDF/SPARQL 是实现细节，不是差异点。方案引为先例的图重排（scope-recall-hermes）本身就在**进程内**跑。
- 当前代码把 Oxigraph 当**一跳 document KV** 用（不跟随 named-node 关系、N+1 HTTP、bindings 未实现）——图能力**付了钱却没用**。
- 把不可替代的黄金真相，放在栈里**运营成熟度最低**的存储：HTTP-only、无事务句柄、无 migration 工具、无 in-repo 备份纪律、lossy codec（时间戳成字符串）。Postgres 的 MVCC / advisory lock（`PostgresEventLog` 已用）/ SQL migration / `pg_dump`·PITR / `timestamptz` 全部开箱即用。

### 3.12【HIGH】SearchIndex 只列了重建来源，没有同步触发、没有时效边界、与 canonical 无引用完整性

§7 说 SearchIndex"可从 Oxigraph canonical + ArtifactStore + EventLog 重建"，§4 要求 SearchIndex hit 只返回 id、再回 canonical 扩展。但文档**没说谁在每次治理提交时写索引、何时 reconcile、最大分叉窗口多大**。因为 SearchIndex 在 Postgres、canonical 节点在 Oxigraph（跨库无 FK），二者**无引用完整性**：索引里的 `memory_id` 可能指向已被 superseded/rejected/deleted 的节点；或者——更危险的方向——**一个刚治理为 ACTIVE 的节点尚未进索引**。

由于 §6.3 CandidateGenerator 依赖 SearchIndex 做内容候选生成，索引**欠填充**会产生**静默召回缺失**（active 事实从不被 surface），而下游图扩展**无法挽救**——扩展只在索引已返回的候选上跑。方案把 staleness 当良性（"SearchIndex 是 candidate accelerator，不是事实源"），但**候选生成器里的 staleness 是召回完整性 bug，不只是新鲜度小瑕疵**。这恰是用户最期待"刚告诉过你"能被记住的场景。

此外，§6/§12 称"删除 SearchIndex 仍能用 SPARQL + lexical scan 生成低性能但正确的 projection"——但**全仓没有任何对图的 lexical scan**：`InMemoryGraphStore.query` 抛 `NotImplementedError`，`OxigraphGraphStore.query` 拒绝 bindings，唯一结构原语是 type-only 的 `find_by_type`。所以"删了不丢事实"为真（Oxigraph 仍 canonical），但"仍能降级召回"为假（**没有任何代码路径能按 statement/snippet 相关性找候选**）。"不丢事实"被用来暗示"优雅降级"，而该暗示对当前代码不成立。

## 四、修改方向

按"先让 v1 真能跑且边界正确，再谈图创新"的原则排序。每条标注它消解的质疑。

### A. 把图扩展从 v1 关键路径上拿下来，降级为可插拔 reranker（消解 3.2 / 3.8 / 3.11）

- v1 召回**只做 lexical**（正是两份 survey 的结论）：候选生成 + 排序 + projection，全部**进程内**完成，数据源是一张 Postgres 投影表或直接扫 in-memory canonical 记录。延迟可控、可测、fail-soft 自然成立。
- 把 `OxigraphGraphExpansion` 定义为 `MemoryRecallService` 之后的**可选 reranker 接口，默认关闭**。等 lexical 基线 + eval harness（方向 F）就位，再用评测数据证明"开启图扩展确实涨点"，才把它接进路径。
- 原则转变：**让"核心创新"靠数据赢得位置，而不是靠架构图预定位置。**

### B. canonical memory 的默认后端改为 Postgres，Oxigraph 作可插拔实现（消解 3.3 / 3.5 / 3.11）

`GraphStore` 已经是 Protocol（seam 用对了），但默认走独立 HTTP 的 Oxigraph 把延迟与写入原子性两个问题一起引入。建议：

- 用 Postgres 表建模 canonical 节点与关系：
  ```sql
  memory_nodes(id, type, scope, status, statement, summary,
               confidence, verification, source_authority,
               applies_when, do_not_apply_when,
               created_at timestamptz, updated_at timestamptz,
               payload jsonb)
  memory_relations(src_id, predicate, dst_id)   -- 双向边在此物化
  ```
- 用 `WITH RECURSIVE` CTE 表达 supersedes/contradicts/basedOn 的 1–2 跳（乃至链式）扩展：**一条查询、一次往返、planner 选索引、与节点写入同事务**——顺手消解 §3.3 的 split-brain。
- Oxigraph 保留为 `GraphStore` 的一个实现，留给未来真正需要多跳跨实体推理的场景；但它不该是 v1 唯一真相源。
- 附带收益：`timestamptz` 列让 recency 比较正确（消解 3.7 的一半），FK 让 SearchIndex 悬空 id 不可能（消解 3.12 的一部分）。

### C. 先消除 `query()` 的 `NotImplementedError`，否则 §6.x 全是空中楼阁（消解 3.5）

无论最终选 Postgres 还是 Oxigraph，`InMemoryGraphStore.query` 与 `OxigraphGraphStore.query(bindings)` 的 `NotImplementedError` 必须先消除（[in_memory.py:69](src/pulsara_agent/graph/in_memory.py:69)、[oxigraph.py:103](src/pulsara_agent/graph/oxigraph.py:103)）。把"参数化/可过滤的 canonical 查询"列为 **Phase 0 硬前置**写进依赖清单——现在它被默认成"已有能力"，正是 Phase 1 估算失真的根源之一。

### D. 召回上线前，先修异步底座与超时（消解 3.1）

- 把 `urllib` 换成异步带连接池的客户端（`httpx.AsyncClient` / `aiohttp` keep-alive），使 SPARQL/HTTP 往返成为真正的 `await` 点；或退一步用 `asyncio.to_thread` / `run_in_executor` 把同步客户端挪出事件循环。
- 在 [agent.py:416](src/pulsara_agent/runtime/agent.py:416) 的 `project()` 调用处包 `asyncio.wait_for(timeout=budget_ms)`，超时即 fail-soft 路径（`state.memory_projection = None`）。
- **没有可 await、可取消的客户端，§5.1 的"hard timeout"就是空文。** 若采纳方向 B（Postgres 默认），异步驱动（asyncpg/psycopg async）天然满足，本条与 B 合流。

### E. 把 ranker 类型体系对齐到真实的 5 类（消解 2.2 / 3.6 部分）

§6.5 引用的 UserIdentity / ExplicitRule / ToolQuirk / Workflow / ProjectFact 都不存在。二选一，但必须选：

1. **映射**（推荐）：把这些语义并入现有 5 类的 `(type, source_authority, scope)` 元组——例如 `source_authority=EXPLICIT_USER_INSTRUCTION` 的 Preference 即"ExplicitRule"档；project-scope 的 Claim 即"ProjectFact"；ToolQuirk/Workflow 归 ActionBoundary。ranker 改为键于元组而非不存在的类型名。
2. **扩展本体**：正式新增类型——但要意识到这会牵动 codec / gate / ledger / CONTEXT 全链路（对应既有教训"Shared schema evolution"：hard-cut、边界处用判别联合、不留 compat shim）。

同时给 appliesWhen/doNotApplyWhen 加**结构化触发底座**（tag / 工具名 / scope glob / 条件 token，写入时索引），让 RecallTriggerDetector 用索引查找匹配，而非 NL 扫描；若暂不做，则把 ActionBoundary 自动触发**移出 cheap recall**，只在显式/agentic 路径用 LLM 匹配（消解 3.6）。

### F. 先建 recall-quality eval harness，再谈 Phase 2/3（消解 3.8）

一个最小评测即可让核心赌注可证伪：

- 固定一组 `(历史记忆集, query, 期望 included/excluded memory_ids)` golden cases，覆盖真实 5 类。
- 离线 harness 同时跑 **lexical-only baseline** 与 **graph-gated pipeline**，报告 `precision@k` / `recall@k` / **superseded-leak rate**。
- 把它列为 **Phase 1 交付物**，并给图路径承诺一个"必须击败 lexical baseline"的阈值。
- 在 harness 落地前，把方案语言从"核心创新"降级为"待验证的假设"。

### G. 重述自相矛盾/过度承诺的不变量（消解 3.4 / 3.12）

- 把"normal recall 只读 Oxigraph"改成可执行版本：**"normal recall 的最终事实裁判只认 canonical store；SearchIndex/lexical 只产候选 id，候选必须回 canonical 做 status/scope/relation 裁定。"** 与 §6.3 一致，且不谎称不读 Postgres。
- 给 SearchIndex 补**增量同步契约**：governance 写 canonical 成功后，经 outbox / 同事务写一条 index-dirty 记录，由后台 worker 消费更新 SearchIndex；并声明 freshness SLO（如"index 落后 canonical 不超过一个治理批次 / N 秒"）。定义悬空 id 契约：候选 id 解析到缺失或非 ACTIVE 节点时静默丢弃并记 trace，**绝不注入**。补一条**欠填充方向**的测试（"刚治理为 ACTIVE 但尚未索引的节点最终仍可召回"），否则"刚记住的东西召不回"会是 v1 最尴尬的 bug。

### H. 把 `do_not_write_back` 变成代码级护栏（消解 3.10）

projection 注入时给每条记忆带来源标记（`memory_id` + `from_projection` 标志）；reflection/governance 生成候选时校验：若候选内容可追溯到本轮 projection 的 `memory_id`，直接拒绝入池或标记为 `projection_echo` 不予治理。让 §3 这条"关键边界"有执行点，而非只在 prompt 里喊话。

### I. 写路径原子化与逆向边物化（消解 3.3 余下部分 / 2.2）

- 选定单一 canonical commit point：以 GraphStore（或方向 B 的 Postgres 节点表）写入为唯一真相，治理事件写进 EventLog 同一逻辑步；GovernanceDecisionLog 与 SearchIndex 作为该事件流的**可重建投影**（transactional outbox / event-sourced projection）。加幂等键（`governance_batch_id + target_entry_id`，二者已 threaded）使崩溃重放安全；加 reconciliation pass 检测"有 canonical 节点但无 decision 行"。
- 把节点与其全部出边写在**一个** INSERT/update 里，使节点在存储层原子；在该能力落地前，§6.4 扩展必须把"节点存在但预期 evidence 边缺失"当作**损坏信号（NEEDS_REVIEW / 不召回）**，而非干净的 active 节点。
- 在写方真正发出 supersedes/contradicts **之前**，把替代/矛盾分支移出 cheap-recall 热路径（推迟到 v2 的显式 `memory_related`），不为 inert 逻辑付查询延迟。

## 五、重排后的实现路线

方案 §12 的四阶段方向不错，但起点假设是错的（把"写入侧基本收敛、projection 已就绪"当真）。以真实的 0% 起点重排：

### Phase 0 — 底座前置（方案未列，但全部后续阶段的硬依赖）

- 消除 `query()` 的 `NotImplementedError`，提供可过滤的 canonical 查询（方向 C）。
- 决定召回 substrate：默认 Postgres 节点/关系表 + 递归 CTE（方向 B）。
- 异步、可取消、带超时的存储客户端（方向 D）。
- 写路径原子化 + 幂等键 + reconciliation（方向 I 第一项）。

退出标准：能在一个事务内完成"治理写节点 + 写 decision 行"，崩溃重放安全；canonical 查询支持 status/scope/type 过滤。

### Phase 1 — 正确的最小 lexical 召回（对应方案 Phase 1，但收窄）

- `MemoryRecallService` 协议 + 进程内 lexical 实现（token OR + 相对 score floor + 保留 top-1 + zero-result guidance）。
- 只读 active canonical（exclude REJECTED/STALE/SUPERSEDED/CONTRADICTED/ARCHIVED/DELETED；NEEDS_REVIEW 默认仅诊断）。
- 接 `_project_memory` hook，生成小预算 fenced projection；**真正 override `project()`**。
- 只读工具 `memory_search` / `memory_get`。
- `do_not_write_back` 代码级护栏（方向 H）。
- **eval harness + golden set**（方向 F）——本阶段交付，不后置。

退出标准（即方案 §12 的测试，补一条欠填充召回）：active preference 可召回；unrelated 不召回；rejected/superseded 不召回；候选池不参与；projection echo 不被写回；user says ignore memory 时 suppress；zero result 有 guidance；**刚治理为 ACTIVE 的节点最终可召回**。

### Phase 2 — Durable SearchIndex + Trace + 增量同步

- Postgres FTS（tsvector/GIN）或 BM25 候选生成。
- recall_trace 表 + recent-recalled suppression + structured unavailable。
- SearchIndex 作为治理事件流的投影，写后增量更新 + 周期全量 rebuild；声明 freshness SLO（方向 G）。

退出标准：删 index 可重建；index hit 必回 canonical 裁定；trace 不改 canonical；欠填充与过期两个方向都有测试。

### Phase 3 — 图重排（仅当评测证明其价值）

- 先在**进程内** graph（Postgres 节点/边 + Python hop 权重，仿 scope-recall-hermes）做 reranker，默认关闭，用 Phase 1 的 harness 证明涨点。
- 写方先物化 supersedes/contradicts（含逆向边），ranker 才启用替代/矛盾分支。
- 只有当测得的多跳大 N 工作负载让递归 CTE 退化，才考虑引入 Oxigraph triple store。
- `memory_related` / `memory_explain`；可选 pgvector / hybrid RRF / MMR。

退出标准：图路径在 harness 上**实测击败** lexical baseline 才合入路径；vector-only 中置信候选不注入；stale/contradiction warning 正确出现。

### Phase 4 — 从召回使用反哺 maintenance

- repeated-recall-never-used → 低效用信号；frequently-contradicted → maintenance candidate；stale warning 频发 → verification task。
- 边界：recall usage 只产 maintenance candidate；canonical 变更仍走 governance/maintenance 写路径。

## 六、最终收口

最终方案的**愿景**——小自动注入 + agentic 深搜 + 可重建索引 + 治理后的 typed 语义裁定——是对的，应保留。它**真正的差异化**也确实存在：治理化、带状态生命周期、关系感知排序的记忆。但这份差异化**是本体与 ranker 的属性，与存储引擎无关**。

方案的问题不在愿景，在排序与对自身状态的误判：

- 它把全系统**最贵、最未实现、且违背自身引用经验**的跨 HTTP 图扩展放进 v1 关键路径，称之为核心创新；
- 同时把真正该先做的 lexical 基线、eval harness、原子写入、异步底座、代码护栏，当成已完成或后置；
- 并把不可替代的黄金真相，交给栈里运营成熟度最低、且制造了双写 split-brain 的独立 triple store。

**最小可行修正：**

> v1 = 进程内 lexical 召回 + Postgres canonical 裁定（GraphStore 作可插拔 seam）+ 评测护栏 + 异步可超时底座 + 代码级 `do_not_write_back`；图扩展退化为需用评测数据赢得位置的可选 reranker；Oxigraph 推迟到实测多跳大 N 工作负载证明递归 CTE 不足时再引入。

先证明便宜的版本不够好，再为图创新买单——这既是两份 survey 的原结论，也是让这份"最终方案"真正可落地的唯一路径。

---

## 附录：质疑 → 修改方向 速查

| # | 质疑 | 级别 | 主要修改方向 |
|---|---|---|---|
| 3.1 | 同步 urllib + inline await，hard timeout 不可实现 | BLOCKER | D（+B） |
| 3.2 | 图扩展 N+1-over-HTTP，无批量/参数化 | BLOCKER | A、B、C |
| 3.3 | 治理双写非原子 = split-brain；节点内部也非原子 | BLOCKER | B、I |
| 3.4 | "只读 Oxigraph"自相矛盾 | BLOCKER | G |
| 3.5 | InMemory 不能 SPARQL，Phase-1 默认后端无入口 | BLOCKER | C、B |
| 3.6 | appliesWhen 自由文本，便宜触发无底座 | HIGH | E |
| 3.7 | codec 不 typed 时间戳，破坏 recency | HIGH | B、I |
| 3.8 | "graph 优于 lexical"不可证伪（无评测） | HIGH | F、A |
| 3.9 | "两个逻辑 PG 角色"是文件组织非架构 | MEDIUM | B（重述） |
| 3.10 | `do_not_write_back` 无强制 | MEDIUM | H |
| 3.11 | Oxigraph 独立 store = accidental complexity | 根因 | A、B、I |
| 3.12 | SearchIndex 无同步触发/时效边界/引用完整性 | HIGH | G、B |
| 2.2 | Ranker 键于不存在的类型/关系 | 现状 | E |
