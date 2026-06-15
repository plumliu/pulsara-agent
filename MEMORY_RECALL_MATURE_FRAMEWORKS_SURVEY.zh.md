# 成熟记忆框架召回系统调研

本文记录对四个新 clone 的本地成熟记忆框架的 memory recall / retrieval 实现阅读结果：

- `/Users/plumliu/Desktop/python_workspace/TencentDB-Agent-Memory`
- `/Users/plumliu/Desktop/python_workspace/OpenViking`
- `/Users/plumliu/Desktop/python_workspace/mem0`
- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes`

本文是独立文档，不与既有 `MEMORY_RECALL_SYSTEM_SURVEY.zh.md` 合并。后者记录的是 MiMo-Code / claude-code / Hermes / openclaw 这一组项目。

调研目标不是比较项目优劣，而是回答一个工程问题：

> Pulsara 的记忆召回，如何吸收成熟框架经验，同时把 graph store / typed memory / evidence / governance 这些自身优势变成真实召回能力，而不是退化成普通向量 RAG。

## 0. 总结

四个系统共同指向一个稳定形状：

1. 每轮自动召回必须小预算、可超时、可为空。
2. 自动召回之外必须提供 agent 可主动调用的深搜工具。
3. raw transcript / conversation search 与 curated canonical memory search 应分开。
4. vector / FTS / BM25 / entity / category / scope 都只是召回信号，不能单独决定最终注入。
5. 召回结果要经过 lifecycle / status / scope / provenance / budget 过滤。
6. recency 不能独立拉入记忆，只能在相关性成立后作为 rerank bonus。
7. 对 Pulsara 来说，Oxigraph 的价值应体现在 graph-aware rerank / expansion / projection，而不是仅作为存储后端。

推荐的 Pulsara recall 方向：

- cheap automatic injection：每轮最多 3-5 条 canonical memory，小 timeout，小 token budget。
- agentic deep recall：提供 `memory_search` / `memory_get` / `memory_related` 等工具。
- candidate generation：Postgres FTS / BM25 / future vector sidecar 负责召回候选。
- graph projection：Oxigraph 负责 scope、typed relation、evidence、supersession、contradiction、action boundary、decision dependencies 等结构过滤与解释。

## 1. TencentDB-Agent-Memory

### 已读核心文件

- `/Users/plumliu/Desktop/python_workspace/TencentDB-Agent-Memory/src/core/hooks/auto-recall.ts`
- `/Users/plumliu/Desktop/python_workspace/TencentDB-Agent-Memory/src/core/tools/memory-search.ts`
- `/Users/plumliu/Desktop/python_workspace/TencentDB-Agent-Memory/src/core/tools/conversation-search.ts`
- `/Users/plumliu/Desktop/python_workspace/TencentDB-Agent-Memory/src/core/store/tcvdb.ts`
- `/Users/plumliu/Desktop/python_workspace/TencentDB-Agent-Memory/hermes-plugin/memory/memory_tencentdb/__init__.py`

### 召回形态

TencentDB-Agent-Memory 是“四层记忆 + 自动召回 + 显式搜索工具”的形态。

它把召回分成：

- L0 raw conversation search；
- L1 structured long-term memory；
- L2 scene / context navigation；
- L3 persona synthesis。

`auto-recall.ts` 开头直接说明召回策略：

- keyword：FTS5 BM25；
- embedding：vector cosine similarity；
- hybrid：keyword + embedding + RRF；
- L3 persona 注入；
- L2 scene navigation 注入。

### 自动召回路径

`performAutoRecall()` 是每轮 prompt 前的自动召回入口。

关键点：

- 默认 timeout 是 5000ms；
- 超时直接 skip recall，不阻塞用户请求；
- recall failure 也 fail-soft；
- L1 相关记忆作为 dynamic context 注入；
- persona / scene navigation / tools guide 作为 stable context 注入。

其中 stable / dynamic 的分层很值得借鉴：

- `prependContext`：L1 relevant memories，跟当前 turn 强相关，放在 user prompt 前。
- `appendSystemContext`：persona、scene navigation、memory tools guide，相对稳定，利于 prompt caching。

这说明它不是把所有记忆都塞进 system prompt，而是显式区分“每轮变化的召回片段”和“稳定工具/人格/场景导航”。

### 搜索工具

Hermes plugin 暴露两个工具：

- `memory_tencentdb_memory_search`：搜索 L1 structured memories。
- `memory_tencentdb_conversation_search`：搜索 L0 raw conversation history。

工具 schema 里明确说：

- memory search 用于用户偏好、历史事件、instructions、previous context；
- conversation search 用于 memory search 没覆盖的信息、具体原文、过去对话细节。

`auto-recall.ts` 中的 memory tools guide 还约束每轮 memory/conversation search 合计最多调用 3 次。

这个约束的意义是：自动召回给小上下文，深搜给工具，但不能让 agent 在搜索循环中空转。

### Hybrid search

`memory-search.ts` 中的工具搜索会：

1. over-retrieve：`candidateK = limit * 3`；
2. FTS5 keyword search；
3. vector embedding search；
4. 两路并行；
5. hybrid 情况下用 RRF merge；
6. 再做 type / scene filter；
7. 最后 trim 到 limit。

TCVDB 后端更进一步：如果有 BM25 sparse encoder，则提供 native hybrid search：

- dense ANN；
- sparse BM25；
- server-side RRF；
- 单次 API 返回混合排序结果。

### 对 Pulsara 的启发

可借鉴：

- 自动召回要有 hard timeout。
- L1 canonical memory 与 L0 transcript search 分开。
- 自动注入和显式 search tool 应同时存在。
- 深搜工具应该有调用预算。
- hybrid search 适合作为候选生成器。

不应照搬：

- 它的 L1/L2/L3 更偏文件/场景组织，不是 typed graph ontology。
- 它的最终召回主要还是文本 search result，不具备 Pulsara 的 evidence / relation / governance projection。

Pulsara 转译：

- FTS/vector/Tencent-style hybrid 只负责 candidate generation。
- GraphStore 决定哪些 candidate 能进入 projection，以及如何附带 evidence/status/scope。

## 2. OpenViking

### 已读核心文件

- `/Users/plumliu/Desktop/python_workspace/OpenViking/openviking/retrieve/hierarchical_retriever.py`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/openviking/retrieve/intent_analyzer.py`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/openviking/service/search_service.py`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/openviking/server/routers/search.py`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/docs/en/concepts/07-retrieval.md`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/docs/en/concepts/03-context-layers.md`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/examples/openclaw-plugin/auto-recall.ts`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/examples/openclaw-plugin/context-engine.ts`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/examples/openclaw-plugin/memory-ranking.ts`
- `/Users/plumliu/Desktop/python_workspace/OpenViking/examples/openclaw-plugin/README.md`

### 重要修正

这个本地 `OpenViking` 不是简单的 agent memory plugin。它是一个 agent-native context database，把 memory/resource/skill/session 组织成 `viking://` 虚拟文件系统，并在这个结构上做层级检索。

这点很重要，因为它是四个项目里最接近 Pulsara “图结构参与召回”的参考。

### find() vs search()

OpenViking 明确区分：

- `find()`：低延迟语义搜索，不需要 session context，不做 LLM intent analysis。
- `search()`：高延迟复杂搜索，需要 session context，先做 LLM intent analysis，生成 0-5 个 typed queries。

`search_service.py` 中：

- `search()` 会从 session 取 `session_info = await session.get_context_for_search(query)`；
- `find()` 直接进入 `viking_fs.find()`。

文档中也明确：

- `find()`：single query；
- `search()`：0-5 TypedQueries；
- chitchat / greeting 可以生成 0 queries。

### Intent analysis

`intent_analyzer.py` 的输入：

- session compression summary；
- recent messages，默认最近 5 条；
- current message；
- optional target abstract；
- optional constrained context type。

输出 `TypedQuery`：

- query；
- context_type：memory / resource / skill；
- intent；
- priority。

这说明 OpenViking 的深召回不是让 agent 自己反复搜，而是先用一个 query planner 把“我要查什么”拆成 typed retrieval plan。

### L0/L1/L2 context layers

OpenViking 的层次模型：

- L0 `.abstract.md`：约 100 tokens，用于 vector search / quick filtering。
- L1 `.overview.md`：约 1-2k tokens，用于 rerank / navigation。
- L2 original/full content：按需读取。

这个模型的关键不是“压缩”，而是“检索层次化”：

- L0 用来快速定位；
- L1 用来判断是否值得进入；
- L2 不默认注入，只在需要时 read。

### Hierarchical retrieval

`hierarchical_retriever.py` 的主流程：

1. embed query 一次；
2. 确定 root directories；
3. global vector search 找 starting points；
4. merge starting points；
5. priority queue 递归搜索 children；
6. 对 child score 与 parent score 做 score propagation；
7. 低于 threshold 的节点不进入；
8. L2 文件是 terminal hit；
9. top-k 收敛或 stagnant 时停止；
10. thinking mode 下可 rerank；
11. 最后可以混入 hotness score。

这不是普通 top-k retrieval，而是“在结构中行走”。

### OpenClaw plugin 的自动召回

`examples/openclaw-plugin` 展示了 OpenViking 如何接入 agent runtime：

- `assemble()` 前自动召回；
- 从 latest user message 提取 query；
- quick availability precheck，避免服务不可用拖慢主请求；
- 按 `recallTargetTypes` 并行搜索 user / agent / resource；
- over-fetch；
- dedup；
- leaf-only；
- threshold filter；
- query-aware boost；
- token/char budget；
- 注入为当前 user message 里的 `## Long-term Memories` section；
- 不添加 standalone synthetic user message。

自动召回还会 gating agent experience：

- write/edit/debug/test/build 等 task-like query 更容易触发；
- casual / question-only / explanation-only 会被压低；
- 已注入过 `<openviking-context>` 的消息不重复注入。

### 对 Pulsara 的启发

这是最重要的结构性参考。

可借鉴：

- `find` / `search` 双入口：cheap recall 与 agentic planned recall 分开。
- L0/L1/L2 分层：短摘要定位，overview 导航，full content 按需读。
- 结构路径参与召回，不只是向量 top-k。
- 检索 trace 记录非常有用。
- 自动召回应该 task-gated，避免闲聊过度注入。

Pulsara 转译：

- OpenViking 的 `viking://` 目录结构，对应 Pulsara 的 typed RDF graph。
- OpenViking 的 hierarchical traversal，对应 Pulsara 的 graph expansion：
  - Memory -> Evidence；
  - Decision -> based_on_ids；
  - ActionBoundary -> applies_when / do_not_apply_when；
  - Claim -> source authority / verification；
  - Node -> supersedes / contradicts / scope。
- OpenViking 的 L0 abstract，可以转成 Pulsara 的 projection snippet / search document。
- OpenViking 的 L1 overview，可以转成 `memory_get` / `memory_related` 的 expanded explanation。

## 3. mem0

### 已读核心文件

- `/Users/plumliu/Desktop/python_workspace/mem0/mem0/memory/main.py`
- `/Users/plumliu/Desktop/python_workspace/mem0/mem0/utils/scoring.py`
- `/Users/plumliu/Desktop/python_workspace/mem0/mem0/utils/entity_extraction.py`
- `/Users/plumliu/Desktop/python_workspace/mem0/integrations/openclaw/recall.ts`
- `/Users/plumliu/Desktop/python_workspace/mem0/integrations/openclaw/tools/memory-search.ts`

### 重要修正

当前本地 mem0 OSS clone 没有看到 `mem0/memory/graph_memory.py` 这样的 graph traversal recall 主路径。

它的召回主要是：

- vector search；
- keyword / BM25；
- entity extraction；
- entity side-index boost；
- optional rerank；
- strict filters。

所以不能把这份本地 mem0 解释成 graph-native recall。它更像生产级多信号 retrieval。

### search() 入口

`Memory.search()` 要求 filters 至少包含：

- `user_id`；
- `agent_id`；
- `run_id`。

否则直接 `ValueError`。

它还支持：

- `top_k`；
- `threshold`；
- advanced metadata filters；
- `rerank`；
- `explain`；
- temporal 参数在 OSS 中不支持。

这一点值得 Pulsara 借鉴：召回必须先锁住 identity/scope，不应该默认跨用户/跨 agent 漫游。

### 核心搜索流程

`_search_vector_store()` 的流程：

1. query lemmatize，用于 BM25；
2. query entity extraction；
3. query embedding；
4. semantic vector search，over-fetch：`max(limit * 4, 60)`；
5. keyword search；
6. BM25 raw score sigmoid normalize；
7. entity boosts；
8. `score_and_rank()`；
9. format result。

`score_and_rank()` 的一个重要设计是：

- semantic score 必须先过 threshold；
- BM25/entity boost 不能把一个 semantic 不相关的候选硬拉进来；
- 最终分数是 additive combined score。

这比“BM25 命中就召回”保守。

### Entity side-index

写入 memory 时，mem0 会：

- batch extract entities；
- dedup entities；
- embed entity；
- 搜 entity store；
- 如果相似度 >= 0.95，更新 existing entity 的 `linked_memory_ids`；
- 否则插入新 entity。

搜索时：

- 对 query entities embed；
- 搜 entity_store；
- 命中的 entity 会 boost linked memories。

这不是图遍历，但它是轻量实体召回增强。

### OpenClaw integration

mem0 的 OpenClaw integration 做了另一个重要工作：token-budgeted category-ranked recall。

默认 category priority：

1. identity；
2. configuration；
3. rule；
4. preference；
5. decision；
6. technical；
7. relationship；
8. project；
9. operational。

它会：

- search long-term memories；
- 如果有 session id，再 search session memories；
- dedup；
- category ranking；
- importance ranking；
- relevance ranking；
- token budget；
- identity/config 可 always include。

### 对 Pulsara 的启发

可借鉴：

- strict scope filters；
- semantic over-fetch；
- BM25/entity/category 多信号排序；
- score explanation；
- identity/rule/preference/decision 优先级；
- token budget。

不应照搬：

- 不能把 entity side-index 当成 Pulsara 的 graph innovation。
- mem0 的 graph 在本地 OSS 召回路径中不是主角。

Pulsara 转译：

- mem0 的 category priority 可以映射到 typed memory priority：
  - Identity / Rule / ActionBoundary / Decision / Preference / ProjectFact / Claim。
- entity boost 可以被 Oxigraph 的 typed relation boost 替代或增强。
- semantic threshold-first 原则可以保留：低相关候选不能只靠 graph/recency boost 进入。

## 4. scope-recall-hermes

### 已读核心文件

- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes/recall.py`
- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes/prompting.py`
- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes/gating.py`
- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes/scoring.py`
- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes/graph.py`
- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes/provider.py`
- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes/tooling.py`
- `/Users/plumliu/Desktop/python_workspace/scope-recall-hermes/storage_views.py`

### 总体形态

scope-recall-hermes 是四个里最接近“保守、可解释、多信号召回”的参考。

它的原则：

- SQLite 是 durable truth；
- vector backend 是 companion / rebuildable；
- journal-first capture；
- current-turn recall 小预算；
- local entity graph 和 trust feedback 可影响召回；
- previous-turn prefetched memory 不注入新 topic。

### prefetch / 自动召回

`provider.prefetch()` 直接调用 `render_current_turn_recall()`。

`render_current_turn_recall()` 的流程：

1. 检查 auto_recall 是否开启；
2. 只在 primary agent context 下召回；
3. normalize query；
4. query 太短则 skip；
5. 调用 `RecallService.search_memories()`；
6. drop recently recalled；
7. 选择 max items / max chars / per item chars；
8. mark recalled；
9. 渲染 `## Scope Recall Relevant Memories`。

默认小预算倾向明显：

- `auto_recall_max_items` 默认约 3；
- `auto_recall_max_chars` 默认约 600；
- `auto_recall_per_item_max_chars` 默认约 180；
- 最近召回过的 memory 在若干 turn 内不重复注入。

### RecallService

`search_memories()` 召回流程：

1. lexical candidates；
2. vector candidates；
3. curated candidates；
4. RRF fusion；
5. dedup；
6. lifecycle filter；
7. general policy；
8. entity graph distance；
9. quality metadata；
10. vector-only higher threshold；
11. temporal decay；
12. freshness bonus；
13. final sort。

它会过滤：

- superseded；
- obsolete；
- rejected；
- archived。

这与 Pulsara 的 `ACTIVE / NEEDS_REVIEW / REJECTED / SUPERSEDED` 这类 node lifecycle 很贴近。

### RRF 与信号融合

scope-recall 的 RRF 支持多路：

- lexical；
- vector；
- BM25；
- curated。

还支持 weights：

- lexical weight；
- vector weight；
- BM25 weight；
- curated weight；
- `rrf_min_signals`。

最终 `final_score()` 支持：

- lexical mode；
- vector mode；
- hybrid mode。

hybrid mode 中，BM25 / lexical / vector 可组合，RRF score 也能作为额外权重混入。

### 图相关能力

`graph.py` 中有本地实体图：

- `memory_entities`；
- `memory_feedback`；
- `memory_relations`。

实体抽取是 deterministic 的，不依赖 LLM：

- proper nouns；
- backtick-delimited names；
- code-ish identifiers；
- compact CJK names；
- target=user 时额外处理一些 user identity token。

`entity_distance_scores()` 是真正参与排序的图距离信号：

- query entity direct overlap：1.0；
- 一跳：0.5；
- 两跳：0.333；
- unrelated：0。

这类似 focal-node reranking，比 mem0 的 entity linked_memory_ids boost 更接近图召回。

### 重要反模式

`storage_views.py` 有一段非常重要的注释：

> 不要在 lexical LIKE/FTS 结果太少时，用 arbitrary recent memories 回填。

原因是：这会让 unrelated fresh conversations 因为新而被召回。

scope-recall 的策略是：

- recency 只能在 relevance established 之后作为 rerank bonus；
- 不能作为 recall entry condition。

这是 Pulsara 必须吸收的不变量。

### 显式工具

`tooling.py` 中至少有：

- search；
- context；
- profile；
- related / explain 等接口。

其中 `_handle_search()` 直接调用 `provider._recall_service.search_memories()`。

这说明 scope-recall 也是双层：

- 自动 current-turn recall；
- 显式工具深搜。

### 对 Pulsara 的启发

可借鉴：

- SQLite truth / vector companion 的边界，可类比 Pulsara graph truth / vector sidecar。
- current-turn recall 小预算。
- recent recall suppression。
- lifecycle/status filter。
- vector-only high threshold。
- recency 只能 rerank，不能 recall。
- entity graph distance / relation distance 作为排序信号。
- `explain` / context payload 很适合 debugging recall。

Pulsara 转译：

- SQLite entity graph -> Oxigraph typed RDF graph。
- `memory_relations` -> RDF relations / ontology predicates。
- `memory_feedback` -> future recall trace / user feedback / usage stats。
- lifecycle filter -> NodeStatus + maintenance relations。
- vector companion -> Postgres/pgvector or external vector sidecar，永远可重建。

## 5. 四者对比

| 项目 | 自动注入 | 显式深搜工具 | 候选生成 | 结构/图参与 | 预算策略 | 最值得借鉴 |
| --- | --- | --- | --- | --- | --- | --- |
| TencentDB-Agent-Memory | 有，5s timeout，L1 dynamic + L3/L2 stable | memory_search + conversation_search | FTS / embedding / hybrid RRF / TCVDB native hybrid | scene navigation，非 typed graph | maxResults / max chars / tool call cap | 小自动召回 + raw/canonical 分层 |
| OpenViking | OpenClaw plugin 中有 assemble auto recall | `memory_recall` / find/search | vector search + hierarchical traversal + rerank | `viking://` virtual FS + L0/L1/L2 hierarchical retrieval | over-fetch + threshold + token budget | 结构化路径参与召回 |
| mem0 | integration 中有 retrieve/recall | memory_search | vector + BM25 + entity boost + optional rerank | entity side-index，不是 graph traversal | category priority + token budget | 多信号排序、scope filters、category priority |
| scope-recall-hermes | 有，小预算 current-turn recall | search/context/profile/related/explain | lexical + vector + BM25 + curated + RRF | local entity graph distance + relations | max items/chars + recent suppression | 保守可解释、多信号、反 recency backfill |

## 6. 对 Pulsara recall v1 的设计约束

### 6.1 只读 canonical graph

Pulsara 已经把 candidate pool 与 canonical memory 分开。召回必须只读 canonical graph：

- 不读 candidate pool；
- 不读 pending governance candidate；
- 默认不把 `NEEDS_REVIEW` 当事实召回；
- 不召回 `REJECTED`；
- 不默认召回 `SUPERSEDED`，除非用户问历史版本或需要解释变更。

### 6.2 分两条召回线

应区分：

- canonical memory recall：治理后可长期使用的 typed memory；
- transcript/session recall：过去具体说过什么、原文、任务时间线。

TencentDB 和 Hermes 都证明这两条线不应混在一个工具里。

### 6.3 Cheap auto recall + Agentic RAG

推荐双层：

1. Cheap auto recall：
   - 每轮自动；
   - timeout 例如 300-800ms，本地可更短；
   - max 3-5 条；
   - 不做 LLM query planning；
   - skip trivial / too short / casual query；
   - recent recalled suppression；
   - 输出 fenced projection，明确 `do_not_write_back`。

2. Agentic deep recall：
   - LLM 主动调用 `memory_search`；
   - 可以指定 kind/scope/time/project/status；
   - 返回 ranked memories + evidence refs + related affordances；
   - 允许进一步 `memory_get(memory_id)` / `memory_related(memory_id)`。

### 6.4 Graph-aware recall pipeline

Pulsara 的关键差异应放在这条 pipeline：

1. Query normalization / trigger detection。
2. ScopeResolver 计算可访问 scopes。
3. CandidateGenerator：
   - Postgres FTS；
   - BM25；
   - future vector sidecar；
   - optional explicit memory kind filters。
4. GraphExpander：
   - 从候选 memory id 进入 Oxigraph；
   - 展开 typed relations；
   - 检查 evidence；
   - 检查 supersession / contradiction；
   - 检查 applies_when / do_not_apply_when；
   - 检查 decision dependencies；
   - 检查 project / artifact / tool / runtime scope。
5. Ranker：
   - lexical / vector score；
   - graph distance；
   - type priority；
   - node status；
   - evidence strength；
   - source authority；
   - freshness only after relevance。
6. ProjectionBuilder：
   - 小预算；
   - fenced；
   - 带 memory id；
   - 带 why recalled；
   - 带 evidence/status；
   - 带 deep recall affordance。

### 6.5 Type priority

可以吸收 mem0 的 category priority，但换成 Pulsara typed memory：

高优先：

- UserIdentity；
- ActionBoundary；
- Rule / Instruction；
- Decision；
- Preference。

中优先：

- ProjectFact；
- Workflow；
- ToolQuirk；
- EnvironmentFact。

低优先或需 query 明确触发：

- raw observation；
- stale evidence；
- old transcript；
- low-confidence claim。

### 6.6 Recency 原则

必须写死：

> Recency never creates relevance.

也就是说：

- 最近不等于相关；
- 只能在 FTS/vector/graph 已经建立相关性后，加 freshness bonus；
- 用户问 “latest/current/recent/today” 时才提高 freshness weight；
- stale memory 可以召回，但必须标注需要验证。

### 6.7 召回结果不能污染写入

projection 应该被标记为 recalled memory，不是本轮用户新说的话。

候选生成 / governance 必须知道：

- 当前候选是否来自用户新输入；
- 是否只是 projection echo；
- 没有新 evidence 时拒绝把 recalled memory 原样写回。

这延续已有 `Projection echo` 拒绝规则。

## 7. 推荐的 Pulsara recall v1 API 草案

### MemoryRecallService

概念接口：

```python
class MemoryRecallService:
    def cheap_recall(
        self,
        query: str,
        context: RecallContext,
        budget: RecallBudget,
    ) -> MemoryProjection: ...

    def search(
        self,
        query: str,
        filters: MemorySearchFilters,
        limit: int,
        mode: Literal["fast", "thinking"] = "fast",
    ) -> MemorySearchResult: ...

    def get(
        self,
        memory_id: str,
        include_evidence: bool = True,
        include_related: bool = False,
    ) -> MemoryDetail: ...

    def related(
        self,
        memory_id: str,
        relation_kinds: list[str] | None = None,
        depth: int = 1,
    ) -> MemoryRelatedResult: ...
```

### Tool surface

v1 可以先给 agent：

- `memory_search`
- `memory_get`

v1.5 再加：

- `memory_related`
- `memory_explain_recall`
- transcript/session search 工具。

### Projection 格式

自动注入应类似：

```text
<recalled-memory-projection do_not_write_back="true">
- [mem:...] statement ...
  type: ActionBoundary
  scope: ctx:...
  why: lexical match + applies_when matched current task
  evidence: ev:...
  status: ACTIVE
</recalled-memory-projection>
```

## 8. 不建议

不建议 v1 做：

- 纯向量 top-k 注入；
- 把 candidate pool 作为 recall source；
- 自动召回 raw transcript；
- 任意 recent memory backfill；
- 把 graph store 只当 passive storage；
- 一开始就做复杂 LLM query planner；
- 把 recalled memory 混进 user message 原文写回 event history。

## 9. 一句话收束

这四个成熟框架的共同经验是：召回要小、稳、可解释、可深搜。

Pulsara 的独特点应该是：

> 全文/向量只负责找到候选；Oxigraph typed graph 负责决定候选能否、为何、以什么证据和边界进入当前 projection。
