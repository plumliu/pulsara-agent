# Pulsara 下一代记忆召回设计:从 Lexical Recall 到 Semantic / Agentic Recall

_Created: 2026-06-28_

本文是面向下一阶段实现的设计文档。它不是冻结契约;冻结边界见 [contracts/MEMORY_SURFACES_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MEMORY_SURFACES_CONTRACT.zh.md)。本文回答三个问题:

1. Pulsara 现在到底如何做 recall。
2. EverMemOS 的召回设计哪些值得学,哪些不该照搬。
3. Pulsara 应如何在保留 JSON-LD canonical graph / governance 写闸的前提下,引入 embedding + reranker + scene/subject 聚合,同时支持 naive RAG 与 Agentic RAG。

本文只负责产品/数据/召回架构与长期方向。当前 runtime/tool/worker/lifecycle 接线、PR 0–E 的实施顺序与风险、migration/并发/资源关闭见 [MEMORY_RECALL_RUNTIME_INTEGRATION_AUDIT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/MEMORY_RECALL_RUNTIME_INTEGRATION_AUDIT.zh.md)。两者互补,实施细节以后者为准。

核心结论:

- EverMemOS 的强项是 **semantic retrieval + scene-level aggregation + sufficiency loop**;Pulsara 的强项是 **event-sourced provenance + governance 写闸 + canonical JSON-LD graph + scope/behavior boundary**。
- Pulsara 不应该放弃 JSON-LD;JSON-LD 继续承担 canonical truth。embedding、FTS、reranker candidate、scene index 都是派生投影,不进 JSON-LD payload。
- 下一步应先做「一套 recall core,三路候选生成」:lexical + FTS + vector ANN → RRF → canonical filter / contradiction expansion → optional reranker → projection/tool result。
- EverMemOS 的 MemScene 不应第一步直接变成 canonical memory;应先做 `memory_scene_index` 派生层,验证召回收益后再决定哪些 scene summary 需要 governance 升级。
- Agentic RAG 不应在 memory 内部再养一个独立 LLM controller。Pulsara 的主 agent 已经能多轮调用 `memory_search`;memory core 只需暴露更好的检索原语、trace 和诊断。

---

## 1. 当前 Pulsara recall 的代码事实

### 1.1 truth source 与存储边界

Pulsara 的长期记忆不是单表文本库,而是几层 surface:

- canonical truth:JSON-LD graph documents,由 `graph_documents` 保存原始 JSON-LD payload。
- canonical projections:`memory_nodes` / `memory_relations`,由 Postgres graph store 从 JSON-LD 投影出 typed node 和关系。
- search projection:`memory_search_index`,存 FTS / aliases,由 outbox / reconcile 同步。
- recall trace:`recall_traces` / `recall_usages`,记录 recall query、candidate、included、filtered、warnings 与 usage。

相关 schema 在 [memory_schema.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/memory_schema.py)。

最重要的边界:

- recall 只读 canonical graph / query projection。
- recall 只能写 trace / usage。
- recalled content 不得回写成 new memory。
- canonical write 只走 governance: `MemoryGovernanceExecutor.apply_decision` → `MemoryWriteService.submit` → ledger → graph。

这条边界是 Pulsara 相比 EverMemOS 的核心优势。EverMemOS 的 profile / scene summary 更新更像 LLM 在线合成;Pulsara 则明确区分「投影」和「被治理后的真记忆」。

### 1.2 自动 recall:CHEAP_AUTO

自动注入发生在 [durable.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/durable.py) 的 `DurableMemoryHooks.project()`:

1. 先尝试构建 working_context projection。
2. 若有 recall service,取最近 user quote。
3. 构造 `RecallQuery(trigger=RecallTrigger.CHEAP_AUTO, limit=5, scopes=read_scopes)`。
4. 调 `recall.recall(...)`。
5. 如果 `OK` 且有 items,用 `ProjectionLedger.record()` 记住本轮 surfaced memory id / snippet fingerprint。
6. 用 projector 生成 `<recalled-memory-projection ... do_not_write_back="true">` fenced block。

自动 recall 是 naive RAG:它不要求 agent 主动搜索,而是在每轮模型上下文前置注入少量 canonical memory。

自动 recall 的产品目标:

- 低延迟。
- 小 token footprint。
- 宁可少召回,不要污染上下文。
- 必须防 echo write-back。
- 可失败为结构化 unavailable,不能把模型导回旧的自由文本 fallback。

### 1.3 显式 recall:EXPLICIT_SEARCH

显式搜索由 `memory_search` 工具触发,入口在 [memory_query.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/memory_query.py)。它构造 `RecallQuery(trigger=RecallTrigger.EXPLICIT_SEARCH)` 并调用同一套 recall service。

显式搜索是 Agentic RAG 的基础:模型可根据任务需要主动调用 `memory_search`、`memory_get`、`memory_related`、`memory_explain`。

显式 recall 与自动 recall 的差异应保留在 `trigger` / options 上,而不是做两套 recall 系统:

- `CHEAP_AUTO`:短预算、近期抑制开、projection 注入、可跳过昂贵 reranker。
- `EXPLICIT_SEARCH`:模型主动给 query、结果可以更多、更详细、可启用 reranker、可允许 `needs_review`。

### 1.4 当前检索流水线

当前核心在 [service.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/service.py) 的 `LexicalMemoryRecallService`:

1. backend cooldown:如果 Postgres/query backend 刚失败,短时间返回 `UNAVAILABLE`。
2. query terms: `_query_terms(query.text)`。
3. recent suppression:对 `CHEAP_AUTO` 抑制近期已注入 memory,避免同一轮反复刷屏。
4. lexical candidates: `PostgresMemoryQuery.lexical_candidates(...)` 从 `memory_nodes` 做字面匹配。
5. FTS candidates: `PostgresMemoryQuery.fts_candidates(...)` 从 `memory_search_index` 做 `plainto_tsquery('simple', ...)`。
6. RRF fusion: `_rrf_ranked_ids(("lexical", lexical), ("fts", fts))`。
7. fetch canonical nodes:`fetch_nodes(ranked_ids)`。
8. canonical filter:status / scope / type / needs_review 等。
9. contradiction expansion:如果召回到一侧矛盾记忆,尝试把另一侧 contradiction companion 带出来。
10. graph-aware rerank: `direct_relation_rerank()` 给 support/supersede 等直接关系一点 grounded bonus,并标 contradiction warning。
11. trim / stabilize conflict groups。
12. 写 recall trace / usages。

这条流水线已经有很好的工程骨架:多 channel candidate generation、RRF、canonical filter、relation-aware postprocess、trace。缺的是语义检索能力。

### 1.5 当前 recall 的强项

- **治理边界清楚**:recall 只读,不写 canonical。
- **scope 清楚**:`ctx:user` / workspace / domain 可以隔离。
- **冲突不被揉掉**:contradiction companion 和 warning 会把矛盾作为结构化事实暴露给模型。
- **可审计**:recall trace 与 usage 可持久化。
- **自动/显式共用核心**:naive RAG 和 Agentic RAG 已经不是两套系统。
- **防 echo 污染**:ProjectionLedger + `do_not_write_back`。

### 1.6 当前 recall 的短板

#### 1.6.1 语义召回弱

`lexical_candidates` 是 LIKE / 字面分数;`fts_candidates` 使用 Postgres `'simple'` FTS。它们不能稳定解决:

- 同义词:egg tart / dan tat。
- 改写:concise summaries / brief responses。
- 跨语言:中文需求召回英文 memory。
- 概念层关系:用户说 "no alcohol while on antibiotics",后来问 "movie night drink"。
- 多跳:多个 memory 各自只提供一半信息。

#### 1.6.2 FTS 是滞后投影

`memory_search_index` 由 [index_sync.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/index_sync.py) 的 `MemorySearchIndexSync` 同步。canonical 写入后,FTS 不是在同一个 graph write 中即时可见,而是通过 outbox / rebuild / sync 变成最终一致。

这不是 bug,但它会影响刚写入 memory 的召回。向量索引未来也会有同样问题。

#### 1.6.3 没有 subject / scene 层

当前 canonical node 是单条 Preference / Observation / Decision / ActionBoundary 等。Pulsara 没有一个 MemScene 式的主题层来表达「这些 memory 共同属于同一问题域」。

这导致:

- governance relatedness 仍有 token-overlap stopgap。
- 多跳召回要靠主 agent 自己串。
- recall top-k 可能给出一堆局部片段,而不是完整叙事。

#### 1.6.4 没有 semantic reranker

`direct_relation_rerank` 是图关系 bonus,不是 cross-encoder reranker。它能利用 `SUPPORTS`、`SUPERSEDES`、`CONTRADICTS`,但不能判断 query 与 candidate text 的深层语义匹配。

---

## 2. EverMemOS 值得学习什么

论文: `EverMemOS: A Self-Organizing Memory Operating System for Structured Long-Horizon Reasoning`。

EverMemOS 面向长程对话 QA。它不是 coding-agent runtime,也没有 Pulsara 的 permission / action boundary / tool execution 语义。它的价值主要在 recall 侧。

### 2.1 MemCell:把 raw dialogue 变成结构化回忆单元

MemCell 是四元组:

- `E` Episode:第三人称叙事,消解指代,作为语义锚。
- `F` Atomic Facts:离散、可验证事实,用于高精度匹配。
- `P` Foresight:带有效区间 `[t_start, t_end]` 的前瞻推断。
- `M` Metadata:时间戳、source pointer 等。

Pulsara 对应物:

- Episode:最接近的是 `working_context` / `recalled_memory` 这类投影,而不是 canonical memory node。
- Atomic Facts:对应 Pulsara 里真正会被治理、会写进 canonical graph 的 typed memory node。
- Foresight:当前没有一等公民。`stale_after` / `expires_at` 只是过期/衰减,不是「未来一段时间内应被考虑的前瞻」。
- Metadata:Pulsara 更强,因为 event log / evidence / governance batch / scope / recall trace 都可追。

对 Pulsara 真正值得学的不是 MemCell 这个持久对象本身,而是它的 **输入规整方式**:

- 在 dense / sparse 之前,先把一个 canonical node 的可检索文本整理成稳定的 `embedded_text`。
- 让 recall 的输入单位比“原始 run transcript”更稳定,但又不把它升级成新的 canonical 存储层。
- 如果将来要表达“未来一段时间内有效”的事实,应先扩 ontology / governance,而不是把 MemCell 的 `P` 直接抄成新的记忆类型。

### 2.2 MemScene:主题聚合层

EverMemOS 用 embedding 增量聚类:

1. 新 MemCell 到来。
2. 算 embedding。
3. 找最近 MemScene centroid。
4. 相似度超过阈值 `tau` 就并入;否则开新 scene。
5. scene summary / user profile 在线更新。

它的消融显示 MemScene 对多跳和时序问题帮助很大。这个发现对 Pulsara 很重要,因为 Pulsara 当前最缺的就是 subject / scene 层。

但 Pulsara 不该照搬「在线 LLM 改写 profile」,也不该把 scene 直接做成新 canonical truth:

- 它没有 governance 写闸。
- 它容易 silent drift。
- 它可能把冲突揉成单一 profile。
- 它没有清楚的 echo guard。

Pulsara 应学习 MemScene 的 **检索结构价值**:

- 先从既有 governed memory node 聚类出 `memory_scene_index` / `memory_scene_members` 这类派生投影。
- scene 只服务于 recall / relatedness / multi-hop expansion。
- scene summary 先不进 canonical graph,也不直接变成用户长期事实。
- scene membership 变化应当可重建、可回滚、可 shadow evaluate。

这和前面的路线 B 是同一件事:先让 `DenseCandidateService` 能把“主题相近的一组 governed nodes”找出来,再决定要不要在上层加 scene 视角。

### 2.3 Reconstructive Recollection:必要且充分的召回

EverMemOS 的召回流程:

1. 对 query 与 MemCell Atomic Facts 做 dense embedding + BM25。
2. RRF 融合。
3. 按 MemCell 分数给 MemScene 打分,取 top-N scenes。
4. 在 selected scenes 中 pool Episodes。
5. 用 reranker 选 top-K Episodes。
6. 按当前时间过滤 Foresight。
7. LLM sufficiency checker 判断证据是否足够。
8. 不足时生成 2-3 个 query,再检一轮。

Pulsara 可学习:

- dense + sparse fusion。
- reranker。
- scene-gated retrieval。
- recall trace 中记录 channel / scene / rerank / sufficiency 诊断。

Pulsara 不一定要学习:

- memory 内部 LLM sufficiency loop。Pulsara 的主 agent 本来就是 verifier,它能主动再调 `memory_search`。
- 每次 recall 都跑重 reranker。自动注入路径必须控制延迟。
- 以 dialogue benchmark 为中心的 profile rewrite。

### 2.4 EverMemOS 的局限

客观地看,EverMemOS 的强项和弱项都很明显:

- 它的 benchmark 是对话 QA,不是工具型 agent runtime。
- Foresight 的最有趣行为主要靠 qualitative case,不是主 benchmark 强证明。
- LLM-as-judge 仍有评估 caveat。
- token/cost 很重:LoCoMo 上 Phase I add 和 Phase III search+answer 都是百万级 token。
- clustering 阈值是超参,且不同 dataset 取值不同。
- profile/consolidation 的 provenance、防污染、写治理弱。

---

## 3. 设计原则:Pulsara 如何学 EverMemOS

### 3.1 保留 JSON-LD canonical graph

JSON-LD 的优势不是召回准确率,而是 canonical truth 的治理能力:

- typed memory:Preference / Observation / Decision / ActionBoundary 等。
- structured fields:scope、status、source_authority、verification_status、confidence、applies_when、do_not_apply_when。
- structured relations:SUPPORTS、SUPERSEDES、CONTRADICTS、HAS_EVIDENCE。
- provenance:evidence / run / governance batch 可追。
- ontology 可演进。

因此:

- embedding 不进 JSON-LD payload。
- scene membership 不默认进 JSON-LD payload。
- reranker score 不进 JSON-LD payload。
- sufficiency verdict 不进 JSON-LD payload。

这些都是 projection / trace / diagnostic,不是 canonical fact。

### 3.2 派生索引归派生表

FTS 当前已经在 `memory_search_index`,不是 JSON-LD。embedding 应同样放在投影表。

建议新增兄弟表 `memory_vector_index`,不要直接给 `memory_search_index` 加 vector 列:

```sql
CREATE TABLE memory_vector_index (
    graph_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    embedding_fingerprint TEXT NOT NULL,
    embedded_text_hash TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (graph_id, memory_id, embedding_fingerprint),
    FOREIGN KEY (graph_id, memory_id)
        REFERENCES memory_nodes(graph_id, id) ON DELETE CASCADE
);

CREATE INDEX memory_vector_index_embedding_hnsw_cosine
    ON memory_vector_index
    USING hnsw (embedding vector_cosine_ops);
```

为什么独立表:

- dense 索引生命周期与 FTS 解耦。
- 可单独 rebuild / backfill / shadow evaluate。
- 不把 vector 生命周期绑死到 sparse/FTS 的 schema 变化。
- 避免 canonical graph payload 膨胀。

v1 固定 `vector(1024)`,与默认 `text-embedding-v4` 对齐。`embedding_fingerprint` 应包含 **provider 名 + 模型名 + 向量长度**,例如 `dashscope:text-embedding-v4:1024`。这样一眼就能看出 row 属于哪个向量空间,也能支持同维度的新旧版本并存。

不同维度或不同模型的向量空间不得混用。v1只固定一个active fingerprint;未来模型切换的物理隔离与迁移流程由实施审计在首次model swap/shadow rollout时确定。

ANN 距离语义也必须固定:v1 使用 cosine distance,SQL 查询按 `embedding <=> :query_embedding` 升序取最近邻,并转换成 higher-is-better score:

```text
vector_score = 1.0 - cosine_distance
```

### 3.3 同步走 outbox,不要 inline embed

canonical write path 不应等待 embedding 模型:

```text
governance commit
  -> graph_documents / memory_nodes / memory_relations
  -> memory_write_outbox pending
  -> async derived-index consumers
       -> sync FTS
       -> compute embedding
       -> upsert memory_vector_index
```

理由:

- governance commit 必须可预测、可审计、低外部依赖。
- embedding provider 可能慢、失败、限流。
- embedding 模型可替换,索引可重建。
- 最终一致对 recall 可接受,只要 trace 明确索引版本/coverage。

需要承认的裂缝:

- live graph 与 search projection 会有短暂不一致。
- FTS 已有这个裂缝;vector 只是把它显性化。
- governance 对既有 canonical memory 的 relatedness 是 hot-path,不能只读异步 `memory_vector_index`;必须从同步面(`memory_nodes` / `memory_relations`)取得候选,再做即时 embedding / rerank 评分。异步 vector index只能作为加速或补充,不能成为target validation的唯一来源。同batch pending candidates已经整体进入同一个Flash planner input,可用`merge_and_submit`完成candidate-level去重/合并;v1不为尚未apply的新siblings制造provisional canonical id,它们之间的canonical supersede/contradiction edge明确defer并记录诊断。

### 3.4 一套 recall core,两个使用模式

Pulsara 已有 `RecallTrigger.CHEAP_AUTO` 与 `RecallTrigger.EXPLICIT_SEARCH`。未来仍应保持:

```text
RecallQuery
  -> candidate generation channels
       lexical
       fts
       vector
       scene / subject (later)
  -> RRF / fusion
  -> fetch canonical nodes
  -> canonical filters
       status
       scope
       type
       needs_review policy
       recent suppression for CHEAP_AUTO
  -> contradiction expansion
  -> optional semantic reranker
  -> trace / usage
  -> projection or tool payload
```

差异通过 options 控制:

| trigger | vector | reranker | recent suppression | limit | output |
|---|---|---|---|---|---|
| `CHEAP_AUTO` | 开 | 可默认关或小 top-M | 开 | 小 | projection |
| `EXPLICIT_SEARCH` | 开 | 开 | 关或弱化 | 较大 | tool JSON |
| future `GOVERNANCE_RELATEDNESS` | 开 | 可开 | 关 | 中 | advisory relatedness |

### 3.5 主 agent 负责 agentic loop

EverMemOS 在 memory 内部做 sufficiency checker + query rewrite。Pulsara 不建议第一阶段照搬。

理由:

- Pulsara 的主 agent 已经有工具循环,可以看 `memory_search` 结果后决定是否再搜。
- 内部 LLM controller 会增加一套不可见行为,不利于 event log / trace /权限模型。
- 自动 recall 路径不能承担 LLM verifier 成本。
- `memory_search` 工具可通过 payload 提供 query suggestions / channel diagnostics,但不应自己无限多轮搜索。

推荐做法:

- v1:提升 `memory_search` 结果质量,让 agent 自己多轮调用。
- v2:增加可选 `memory_search` 参数 `search_mode="single" | "expanded"`;expanded 最多做 deterministic query expansion 或 model-assisted rewrite,但必须记录 trace。
- v3:如果真实 dogfood 证明 agent 经常不会主动重搜,再考虑 memory-internal sufficiency loop。

### 3.6 路线 B:拆成四层 recall service,不要把一切塞回单类

下一阶段不建议继续在 `LexicalMemoryRecallService` 里横向长功能开关。更好的实现路径是把 recall core 拆成四层:

1. `SparseCandidateService`
2. `DenseCandidateService`
3. `RecallRerankService`
4. `HybridMemoryRecallService`

这四层的关系是:

```text
RecallQuery
  -> SparseCandidateService
  -> DenseCandidateService
  -> HybridMemoryRecallService 做 fusion / canonical filter / contradiction expansion / trimming / trace
  -> RecallRerankService (optional, policy-gated)
  -> final RecallResult
```

拆分的原因:

- sparse / dense / rerank 的失败模式、延迟、配置与降级策略完全不同。
- `CHEAP_AUTO` 与 `EXPLICIT_SEARCH` 的差异主要是 policy,不是两套 retrieval core。
- 未来 governance relatedness 只需要 dense candidate 能力,不该依赖一整个 recall monolith。
- tokenizer / embedding / rerank provider 已经在 `src/pulsara_agent/retrieval/` 独立;service 层也应顺着这个边界拆。

### 3.7 不走 EverMemOS 式 accessor singleton

EverMemOS 的新版工程更像“组件 accessor + 全局可取 provider”。Pulsara 不建议照搬。

Pulsara 更适合:

- provider 层是显式依赖:
  - `Tokenizer`
  - `EmbeddingProvider`
  - `RerankProvider`
- memory recall / governance relatedness / future search surfaces 通过依赖注入复用这些 provider。

不建议做 `get_embedding_provider()` / `get_reranker()` 这类进程级 singleton accessor:

- deterministic fake 应能通过依赖注入替换真实 provider。
- provider 的模型/version/budget policy 不应藏在模块级缓存里。
- recall 只是 retrieval stack 的第一个消费者,显式依赖更容易复用和审计。

---

## 4. 目标架构

### 4.1 新增组件

#### Tokenizer / EmbeddingProvider / RerankProvider

provider 层只负责“调用外部/本地检索模型能力”,不负责 recall policy:

- `Tokenizer`:给 sparse retrieval 用,把 query / indexed text 变成应用层 token。
- `EmbeddingProvider`:把文本变成向量。
- `RerankProvider`:对 `(query, candidate_text)` 打相关性分。

现有 `src/pulsara_agent/retrieval/` 目录已经基本对应了这一层。下一步不是再造 accessor,而是让上层 service 消费这些 provider。

#### SparseCandidateService

职责:

- 统一 query 归一化 / tokenization。
- 调 `MemoryQuery.lexical_candidates(...)`。
- 调 `MemoryQuery.fts_candidates(...)`。
- 返回结构化 sparse candidates,而不是直接拼成 `RecallItem`。

建议输入/输出心智模型:

```python
class SparseCandidate(NamedTuple):
    memory_id: str
    channel: str          # lexical | fts
    raw_score: float
    rank: int
```

```python
class SparseCandidateService(Protocol):
    async def collect(
        self,
        query: RecallQuery,
        *,
        graph_id: str | None = None,
    ) -> SparseCandidateBatch: ...
```

这里的关键是:它负责 sparse candidate generation,不负责 canonical filter / contradiction expansion / final trimming。

#### DenseCandidateService

职责:

- 调 `EmbeddingProvider` 给 query 做 embedding。
- 查询 `memory_vector_index`。
- 按 scope / type / status 等粗过滤拿回 vector candidates。
- 在未来可扩展 scene/subject candidate generation。

建议输出:

```python
class DenseCandidate(NamedTuple):
    memory_id: str
    channel: str          # vector | scene_member | scene_summary
    raw_score: float
    rank: int
    embedding_fingerprint: str
```

为什么 dense 单独成 service:

- governance relatedness 未来几乎肯定要直接复用它。
- dense channel 有自己的 degraded path:provider down、vector index lag、model mismatch。
- 未来 scene/subject 层也更自然挂在 dense 侧,而不是挂在 recall orchestration 侧。

#### RecallRerankService

职责:

- 接受 fused 后的 top-M candidate texts。
- 调 `RerankProvider` 做 query-time semantic rerank。
- 把 rerank score 作为附加信号回传,而不是绕过 canonical filters。

它不是 today 的 `direct_relation_rerank` 替代品。二者应分工:

- `RecallRerankService`:语义相关性模型。
- `direct_relation_rerank`:图关系 grounded bonus / conflict surfacing。

也就是说,最终排序更像:

```text
sparse/dense candidates
  -> RRF fusion
  -> fetch canonical nodes
  -> canonical filters
  -> contradiction expansion
  -> optional semantic rerank
  -> graph-aware rerank / conflict stabilization
  -> trim
```

#### HybridMemoryRecallService

最外层 orchestration 继续实现 `MemoryRecallService` 协议,供:

- `DurableMemoryHooks.project()` 的自动 recall
- `memory_search` 工具
- 未来 governance relatedness 的上游 orchestration 复用一部分诊断逻辑

它负责:

- 调用 sparse / dense candidate services。
- 做 RRF / fusion policy。
- fetch canonical nodes。
- 应用 status / scope / type / needs_review / recent suppression。
- contradiction expansion。
- 根据 `RecallTrigger` 决定是否启用 `RecallRerankService`。
- 保留并整合 `direct_relation_rerank`。
- 产出 `RecallResult`。
- 写完整 trace / usage。

#### EmbeddingProvider

抽象:

```python
class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int

    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...
```

实现可先支持:

- remote OpenAI-compatible embedding,默认对接阿里云百炼 `text-embedding-v4`。
- future local sentence-transformers / Qwen embedding。
- test deterministic fake。

约束:

- `model_id` 必须进入 `memory_vector_index.embedding_fingerprint` 与 recall trace。
- `dimensions` 必须和 DB vector dim 一致;v1 是 1024。
- `embed_batch` 必须保持输入顺序,即使 provider 返回结果带 index 且顺序被重排。
- embed 失败不影响 canonical write。

#### MemoryVectorIndexSync

职责:

- 作为 unified canonical mutation outbox 的一个 consumer,读取与 `memory_search_index` / Oxigraph mirror 相同的 mutation journal。
- 支持显式 `sync_memory(memory_id)` / `rebuild(...)` 作为 repair/backfill 入口,但不引入第二条 outbox lane。
- 算 `embedded_text_hash`。
- hash 未变则跳过。
- 调 `EmbeddingProvider`。
- upsert `memory_vector_index`。

它应与 `MemorySearchIndexSync` 并列为同一条 outbox 的另一个 consumer,而不是把所有逻辑塞进现有 FTS sync,也不是自造一条 embedding-only queue。

#### vector_candidates

扩展 `MemoryQuery`:

```python
def vector_candidates(
    *,
    query_text: str,
    scopes: Sequence[str] | None,
    types: Sequence[str] | None,
    limit: int,
    graph_id: str | None = None,
    embedding_fingerprint: str | None = None,
) -> list[tuple[str, float]]: ...
```

`PostgresMemoryQuery` 可以持有 `EmbeddingProvider` 或单独注入 `MemoryVectorQuery`。为了保持 read path 清晰,建议把 embedding query 放在新 service 中,再由 recall service 组合。

#### RerankProvider / RecallRerankService

底层 provider 抽象已经落在 `src/pulsara_agent/retrieval/rerank/protocol.py`:

```python
class RerankResult(NamedTuple):
    index: int
    score: float

class RerankProvider(Protocol):
    model_id: str

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        instruction: str | None = None,
        top_n: int | None = None,
    ) -> list[RerankResult]: ...
```

`RecallRerankService` 是 memory 层 wrapper,负责把 canonical node view 转成 documents,调用 `RerankProvider`,再把 `RerankResult.index` 映射回 memory id。reranker 不存储任何东西。它只在 query time 计算 `(query, candidate_text)` 的相关性。

### 4.2 Service 边界与数据流

推荐的数据流边界:

```text
RecallQuery
  -> SparseCandidateService.collect()
       returns sparse candidate ids + per-channel scores
  -> DenseCandidateService.collect()
       returns vector candidate ids + per-channel scores
  -> HybridMemoryRecallService.fuse()
       RRF / dedupe / candidate trace
  -> fetch canonical node views
  -> canonical filter / contradiction expansion
  -> RecallRerankService.rerank()        [policy-gated]
  -> direct_relation_rerank()
  -> RecallResult
```

这里最重要的纪律有三条:

1. candidate services 不直接返回模型可见 snippet。
2. rerank service 不直接读数据库,它只吃 query + candidate text。
3. `HybridMemoryRecallService` 才是唯一把“retrieval signals”翻译成“memory recall result”的地方。

### 4.3 embedded_text 应包含什么

v1 每个 canonical memory node 一条 vector。`embedded_text` 建议由以下字段拼接:

- memory type。
- statement。
- summary。
- applies_when / do_not_apply_when。
- action boundary trigger aliases:`triggerTools`、`triggerActions`、`triggerFileGlobs`、`triggerScopes`、`triggerKeywords`、negative variants。
- relation hints 可选:如 `supersedes`, `contradicts` 只放结构化标签,不要把整条邻居 memory 拼进去。

示例:

```text
Type: Preference
Scope: ctx:user
Statement: The user prefers concise summaries.
Summary: ...
Aliases: concise, brief, summary style
```

不建议 v1 embed:

- evidence full text。
- entire run timeline。
- recalled projection text。
- generated explanation。

这些会把检索空间变吵,也增加 echo 风险。

### 4.4 score 与 reason

RecallItem 未来应能解释命中来源:

```json
{
  "memory_id": "preference:concise",
  "score": 0.82,
  "why": [
    "lexical",
    "fts",
    "vector:qwen3-embedding-4b",
    "reranker:qwen3-reranker-4b",
    "evidence_support"
  ],
  "channel_scores": {
    "lexical": 0.14,
    "fts": 0.06,
    "vector": 0.77,
    "reranker": 0.91
  }
}
```

这不一定全部给模型看,但应进 trace,便于 dogfood/debug。

### 4.5 trace 扩展

`recall_traces` 现在记录 candidate/included/filtered/warnings。未来应扩 metadata,或新增 JSON 字段,记录:

- embedding_fingerprint。
- vector_index_coverage。
- vector_candidate_ids。
- fts_candidate_ids。
- lexical_candidate_ids。
- fusion method / rrf_k。
- reranker_model。
- reranked_ids。
- scene_candidate_ids (v2)。
- degraded channel warnings。

原则:模型可见 payload 简洁;trace 详细。

---

## 5. Scene / subject 层怎么引入

### 5.1 不要第一步 canonical 化 MemScene

EverMemOS 的 MemScene 是核心贡献,但 Pulsara 不应直接把 scene summary 当 canonical memory 写入。原因:

- scene 是检索组织结构,不是用户确认的事实。
- scene summary 容易由 LLM 合成出错。
- scene membership 会随 embedding model / threshold 变化。
- 一旦进 canonical,每次 recluster 都像在改写事实。

所以 v1 scene 应是 projection:

```sql
memory_scene_index (
    graph_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    scene_model TEXT NOT NULL,
    scene_version TEXT NOT NULL,
    scope TEXT NOT NULL,
    scene_label TEXT,
    scene_summary TEXT,
    centroid vector(1024),
    member_memory_ids JSONB NOT NULL,
    member_count INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (graph_id, scene_id, scene_model, scene_version)
);

memory_scene_members (
    graph_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    scene_model TEXT NOT NULL,
    scene_version TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    membership_score DOUBLE PRECISION NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (graph_id, scene_id, scene_model, scene_version, memory_id),
    FOREIGN KEY (graph_id, memory_id)
        REFERENCES memory_nodes(graph_id, id) ON DELETE CASCADE
);
```

### 5.2 Scene 的第一用途:relatedness

最早落地价值不是给模型展示 scene summary,而是替换 governance 的 token-overlap relatedness:

```text
new candidate statement
  -> embed immediately
  -> gather active same-scope same-type candidates from the sync face
     (memory_nodes / memory_relations)
  -> optional ANN / vector scoring over that candidate set or a cached subset
  -> optional rerank
  -> related_existing_memories
```

这个路径不需要 candidate 入库,也不需要 scene summary。它先解决 `egg tart / dan tat` 这种 subject recall hole。`related_existing_memories`在v1只表示已提交canonical memory;同batch pending siblings由Flash从完整candidate batch中直接比较并可merge。若两个新siblings需要canonical contradiction/supersede edge,v1允许暂缓,不承诺下一batch自动补回;通过`same_batch_lifecycle_deferred`诊断评估是否需要未来two-phase governance或maintenance。

### 5.3 Scene 的第二用途:multi-hop recall

当 vector channel 找到某些 memory 后,可上卷到 scene:

```text
query -> vector candidates -> scene candidates
     -> select top scenes
     -> pull top member memories from scenes
     -> rerank memory nodes
```

这类似 EverMemOS:先找场景,再取 episode。但 Pulsara 的单位是 governed memory node,不是 raw dialogue MemCell。

### 5.4 Scene summary 是否进入模型

默认不要。v1 给模型看 canonical memory nodes,不是 scene summary。

可以之后做两种模式:

- compact mode:scene summary + top member ids,用于 CHEAP_AUTO。
- evidence mode:top member memory nodes,用于 EXPLICIT_SEARCH。

如果 scene summary 要长期保存并影响 agent 行为,必须走 governance,变成 typed memory 或 working_context-like projection,不能悄悄进入 canonical。

---

## 6. Foresight 是否要学

EverMemOS 的 Foresight 是时间有界的前瞻推断,例如「用户正在服抗生素,在未来两周推荐饮品时避免酒精」。这是很有价值的,但不能直接搬。

Pulsara 当前有:

- `stale_after`
- `expires_at`
- Decision / ActionBoundary / Observation

缺的是:

- `valid_from` / `valid_until` 的临时状态事实。
- 「推断性建议」与「用户显式事实」的来源区分。
- Foresight 影响 tool/action 的边界。

建议:

1. 不在 embedding PR 里做 Foresight。
2. 先用现有 `expires_at` 表达有期限的 Observation / ActionBoundary。
3. 后续新增 ontology 字段:
   - `valid_from`
   - `valid_until`
   - `inference_kind`
   - `derived_from`
4. Foresight candidate 必须经过 governance,并且 source_authority / verification_status 不能伪装成 explicit user instruction。

---

## 7. 长期能力路线

近期 Integration PR 0 与 PR A–E 的代码落点、实施顺序、测试矩阵和风险统一见 `MEMORY_RECALL_RUNTIME_INTEGRATION_AUDIT.zh.md`;本文不重复维护。

### 7.1 Scene index projection

目标:引入 MemScene-like subject layer,先不 canonical 化。

改动:

- `memory_scene_index` / `memory_scene_members`。
- batch/offline clustering。
- scene query channel。
- recall trace 记录 scene hits。

测试:

- related memories 聚到同 scene。
- scene membership 随 rebuild 可重算。
- scene summary 不写 canonical graph。
- recall 可通过 scene 扩展 member memory。

### 7.2 Agentic search ergonomics

目标:让主 agent 更容易做多轮 memory search,但不在 memory 内部建黑盒 controller。

改动:

- `memory_search` payload 增加 concise diagnostics:
  - channel coverage。
  - `has_more`.
  - optional `suggested_followup_queries`(deterministic or model-generated,需 trace)。
- tool description 明确可多轮调用。

测试:

- real LLM 能在第一次 memory_search 不足时第二次搜索。
- 不把 search suggestions 当 canonical memory。

---

## 8. 评估计划

### 8.1 单元测试

- vector index schema。
- embedding text builder。
- outbox consume。
- RRF channel merge。
- reranker fallback。
- trace metadata。
- governance relatedness。

### 8.2 构造型 recall eval

现有 `evals/recall` 是 lexical/FTS baseline gate:fixture query 与目标 memory 有明显 token overlap,runner 也以 `LexicalMemoryRecallService` 和 lexical test double 为核心。它应继续保留,用于守住 canonical filter、泄漏、投影预算与 lexical regression;但它通过不能被解释为 semantic recall 已生效。

semantic recall 落地时新增独立版本的 semantic fixture/runner,不要静默改写现有 baseline。至少覆盖:

- 无 token overlap 的同义改写。
- 中英文跨语言召回。
- lexical miss、vector-only hit。
- sparse/vector 同时命中时的稳定融合排序。
- vector provider/index unavailable 时的 sparse fallback 与 structured warning。
- reranker 前后 top-k 变化及 failure fallback。

评估应分三层:

1. deterministic fake embedding/reranker:守 pipeline、fusion、fallback、trace 契约,适合 CI blocking gate。
2. frozen vector fixture 或固定模型快照:衡量 recall@k / MRR / nDCG,避免每次 CI 受远端模型漂移影响。
3. live provider shadow/dogfood:发现真实模型、网络与版本漂移问题,默认不作为每次提交的唯一 blocking gate。

semantic fixture 的第一批用例:

| query | canonical memory | 期望 |
|---|---|---|
| "dan tat" | "The user likes egg tarts." | vector 命中 |
| "brief replies" | "The user prefers concise summaries." | vector 命中 |
| "movie night drink" | "User is taking antibiotics until Friday." + "User likes IPA." | scene/agentic search 应 surface both |
| "force push main" | ActionBoundary negative/positive triggers | aliases + vector 命中 |
| "old preference replaced" | superseded + active new | canonical filter 只返回 active/supersede-aware |

报告必须按 channel 拆分命中与降级信息,至少区分 lexical / FTS / vector / reranker。否则即使总 recall@k 上升,也无法判断收益来自 semantic channel,还是 fixture 仍被 lexical overlap 偷偷命中。

### 8.3 Real LLM dogfood

新增 real LLM trajectory:

1. 用户先教多条改写/同义偏好。
2. governance 写入 canonical。
3. vector projection 已同步可用。
4. 后续用户用不同措辞提问。
5. agent 应通过 automatic projection 或 `memory_search` 找到相关 memory。
6. final answer 必须引用正确 memory,且不把 recalled content 再 remember。

### 8.4 指标

- recall@k for hand-labeled fixtures。
- MRR / nDCG for explicit memory_search。
- latency p50/p95 for CHEAP_AUTO。
- reranker invocation count。
- vector degraded warning rate。
- echo guard skip count。
- governance relatedness hit precision。

---

## 9. 风险与取舍

### 9.1 成本

Embedding 与 reranker 引入真实运行成本。EverMemOS 的论文成本表说明这类系统并不轻。Pulsara 应把成本压在:

- write-side async indexing。
- explicit search reranker 优先。
- auto projection 小 top-k。
- trace 可观测。

### 9.2 最终一致

outbox indexing 意味着刚写入的 memory 可能短时间无法被 vector/FTS 搜到。解决:

- `memory_get` by id 始终读 canonical。
- governance relatedness 可对新 candidate 即时 embed 查 existing index。
- 产品 surface 提供 index lag diagnostics。
- recall trace 写 `index_coverage`。

### 9.3 模型版本漂移

embedding model 换代会改变检索结果。必须:

- `embedding_fingerprint` 入主键。
- 支持 rebuild。
- 支持 shadow eval。
- recall trace 记录 model id。

### 9.4 Scene 聚类阈值脆弱

EverMemOS 的 `tau` 是 dataset-sensitive。Pulsara 不应过早把 scene membership 当真值。scene 是 projection,可重建、可评估、可删除。

### 9.5 Reranker 幻觉式自信

reranker 是相关性模型,不是 truth validator。它只能排序候选,不能绕过 canonical status/scope/type/filter,也不能覆盖 contradiction warning。

---

## 10. 设计边界

在 semantic recall 地基稳定前,不进入:

- 完整 MemScene pipeline。
- memory-internal LLM sufficiency controller。
- Foresight ontology。
- profile 在线改写。

这些能力都依赖 semantic recall 地基。应先用真实 dogfood 判断多跳/subject drift是否仍是主要问题,再决定scene与更重的agentic controller。

最终目标不是复制 EverMemOS,而是形成 Pulsara 自己的组合:

```text
JSON-LD governed canonical graph
  + Postgres FTS/vector projections
  + RRF/reranker recall core
  + structured contradiction/scope/provenance filters
  + optional scene projection
  + agent-driven multi-round memory_search
```

这条路能吸收 EverMemOS 的召回强项,同时保留 Pulsara 最有价值的治理与 agent-runtime 优势。
