# Memory Governance Semantic Relatedness 设计

_Status: implementation-ready design; no code changes in this document_
_Created: 2026-06-30_

## 0. 目的与结论

本文定义 Pulsara 下一阶段的 semantic governance relatedness：为 pending memory candidates 找到可能相关的、已经提交的 canonical memories，并把这些候选作为 advisory evidence 交给 Flash；最终 duplicate、coexist、contradiction、supersede 决策仍由 governance planner 提议、由 executor 强制验证。

冻结以下结论：

1. 正常的 durability / validity / scope 治理与 canonical 落盘是主路径；semantic relatedness 是 best-effort advisory 副路径。**只有 relatedness 找到可信 canonical target 时，才分叉进入 duplicate、coexist、contradiction 或 supersede 判断。**没有可信 target、provider 超时/不可用或证据不足时，普通 `submit_as_is` / `correct_and_submit` / `skip` 仍按现有治理流程继续，不能被 PR E 阻断。
2. embedding / reranker 只负责候选生成、排序和截断，不负责判定关系。
3. relatedness 必须是 batch async service，不能在每个 `_candidate_snapshot()` 内串行调用 provider。
4. 精确浮点相似度不进入 Flash prompt，避免高分锚定导致过度 supersede；精确分数只进入内部 trace。v1 默认只向 Flash 暴露有序候选及匹配渠道，不暴露 score；如 dogfood 证明排序信息不足，最多增加粗粒度 bucket。
5. async vector index 可以加速 candidate generation，但不是 governance target truth。
6. v1 对 `committed-but-unindexed` 做有预算上限的 best-effort 修补，余量由 async vector worker 最终回填；不补齐 `staged-but-uncommitted` canonical lifecycle 窗口。
7. 同 batch candidates 继续整体进入 Flash input，因此 candidate-level duplicate/merge 必须工作；尚未 apply 的 siblings 之间不创建 provisional canonical id，也不创建即时 contradiction/supersede edge。
8. lifecycle target 必须来自该 candidate 的 executor-side advisory allowlist。仅在 prompt 中要求“不要编造 ID”不构成安全边界。

本文不引入 MemScene，不改变 canonical truth 的唯一写入口，也不把 embedding 写进 JSON-LD payload。

## 1. 当前代码基线与缺口

当前 `MemoryGovernanceEngine.run_pending()` 先用同步 list comprehension 为所有 pending candidates 构造 snapshot，再调用一次 Flash，最后依次 apply decisions。

当前 `_candidate_snapshot()` 中的 `related_existing_memories`：

- 从 graph 同步面读取同 scope、同 type、ACTIVE memories；
- 以 statement/user quote token overlap 排序；
- 截断为 top-k；
- 只把 related entries 放进给 Flash 的 snapshot；
- executor 不知道每个 candidate 当时的 relatedness allowlist。

因此现在有四个真实缺口：

1. 跨语言、别名、改写无法可靠发现，例如 `egg tart` / `dan tat`。
2. `_candidate_snapshot()` 是同步、逐 candidate 的落点，直接加入 provider 会形成 N×远程调用。
3. “target 必须来自 related_existing_memories”只是 prompt 规则；一个真实存在、同 scope、ACTIVE、类型合法但未被 surface 的 ID，当前仍可能通过 executor validation。
4. snapshot 到 apply 之间存在 TOCTOU：target 可能已被另一个 batch 改变状态。

## 2. L1：Async batch relatedness service

### 2.1 责任边界

新增独立服务，建议落点：

```text
src/pulsara_agent/memory/governance/relatedness.py
```

概念协议：

```python
class GovernanceRelatednessService(Protocol):
    async def collect_batch(
        self,
        pending: Sequence[PooledMemoryCandidate],
        *,
        graph_id: str | None,
    ) -> RelatednessBatchResult: ...
```

它负责：

- 一次读取整批 candidates；
- 对去重后的 candidate texts 调用一次或有限次 `embed_batch()`；
- 合并 exact / lexical / dense / bounded gap-repair candidates；
- 对整批 top-M pairs 做有限次 rerank；
- 生成 per-candidate allowlist 和内部 diagnostics。

它不负责：

- 判断 duplicate / coexist / contradiction / supersede；
- 创建 canonical memory；
- 改变 lifecycle status；
- 把精确 score 写进 Flash prompt。

### 2.2 必须先 collect，再构造 snapshot

`run_pending()` 的目标时序是：

```text
pending = list_pending()
relatedness = await relatedness_service.collect_batch(pending, ...)
snapshots = [candidate_snapshot(candidate, relatedness.for(candidate.entry_id)) ...]
flash_output = await call_flash(snapshots)
apply decisions with executor-side relatedness context
```

禁止在 `_candidate_snapshot()` 内部调用 `asyncio.run()`，也禁止 per-candidate `await embed()` / `await rerank()`。

batch 的含义不仅是 API 使用 `embed_batch`：candidate text 必须先按 normalized text/hash 去重；待现场补 embed 的 canonical node texts 也必须跨 candidates 合并、去重，并受整个 governance run 的统一预算约束。

### 2.3 Flash-visible 与 internal-only 数据

精确数据只进入内部 trace：

- cosine similarity；
- reranker score；
- channel raw rank；
- threshold；
- embedding fingerprint；
- provider latency/error。

Flash 默认只看到：

```json
{
  "memory_id": "preference:...",
  "memory_type": "Preference",
  "statement": "...",
  "scope": "ctx:user",
  "status": "active",
  "verification_status": "...",
  "source_authority": "...",
  "applies_when": "...",
  "do_not_apply_when": "...",
  "is_exact_duplicate": false,
  "match_channels": ["dense", "lexical"]
}
```

数组顺序可以表达内部排序，但不能暴露 `0.82` / `0.91` 这类精确分数。若未来引入 `high/medium/low`，必须由 eval 证明它改善关系判断且不提高 destructive-action false positive。

### 2.4 Threshold 的归属与校准

dense candidate threshold 是一个高敏感旋钮：过高会重新制造 semantic recall hole，过低会让 Flash 被噪声淹没。它不得散落成 relatedness service 内的 magic number，也不得直接复用 CHEAP_AUTO / EXPLICIT_SEARCH 的 recall threshold。

v1 将它归属于 `MemoryGovernanceRelatednessOptions`，由 deployment settings 显式注入。配置与以下标识一起进入 trace 和 eval manifest：

- embedding fingerprint；
- relatedness policy/version；
- labeled fixture version；
- `dense_candidate_min_score`；
- candidate top-k / rerank top-M；
- `max_inline_gap_embeds`。

threshold 视为随 embedding fingerprint 与 corpus 分布校准的脆弱参数。更换模型、维度、embedded-text builder 或主要语料分布时，必须重新跑 §7 的 recall@k/miss-rate 与 precision gates；不能假设旧 threshold 可迁移。

候选生成优先采用“较低召回 floor + 有界 top-k/rerank”而不是通过抬高 threshold 追求 destructive-action precision。关系 precision 应由 relation decision gate 和 executor validation 守住，不能通过牺牲 candidate recall 偷换得到。

## 3. L2：候选并集、freshness 与成本上限

### 3.1 Candidate generation 并集

每个 candidate 的初始候选来自以下并集：

1. deterministic exact fingerprint / normalized equality；
2. scope/type-aware lexical coarse candidates；
3. 当前 fingerprint 下的 vector-index candidates；
4. bounded committed-vector-gap repair candidates。

合并后必须从同步 `memory_nodes` / `memory_relations` 重新读取并过滤：

- graph 一致；
- scope 一致；
- type compatible；
- target 仍为 ACTIVE；
- canonical id 真实存在。

async search/vector index 只能提供 ID 候选，不能证明 target 合法。

### 3.2 Bounded committed-vector-gap repair

第 4 路只修补已经提交到 `memory_nodes`、但尚无当前 embedding fingerprint vector 的节点。它不能扫描并同步 embed 全部 active memories。

候选必须先经过：

- graph/scope/type 过滤；
- exact/lexical/alias 等 deterministic coarse screen；
- 可选的 recent-unindexed bounded sample；
- run-level `max_inline_gap_embeds` 上限。

现场 embedding 使用跨 candidates 去重后的 `embed_batch()`。超过预算的节点不进入本轮 inline repair，并记录：

```text
relatedness_gap_candidates_truncated
relatedness_inline_embed_count
relatedness_missing_current_fingerprint_count
```

在首次模型切换、全部节点都缺新 fingerprint 时，这一上限尤其重要。后台 vector worker 负责最终回填，governance coordinator 不承担全库迁移。

### 3.3 两类可见性窗口必须分开

本设计 best-effort 修补：

```text
canonical commit 已完成
→ memory_nodes 已可见
→ vector worker 尚未 materialize
```

即 `committed-but-unindexed`。它不保证本轮覆盖所有缺 vector 的节点：超过 coarse-screen / inline-embed 预算的余量仍要等待 async vector worker 回填。

本设计不解决：

```text
candidate 与 sibling 同属当前 governance batch
→ 所有 snapshots 在 apply 前构造
→ sibling 尚未成为 canonical memory
→ memory_nodes 中不存在 sibling canonical id
```

即 `staged-but-uncommitted`。

v1 对后者的冻结语义：

- whole-batch input 让 Flash 看见所有 candidate siblings；
- duplicate/近义 candidates 用 `merge_and_submit(target_entry_ids=[...])` 处理；
- siblings 之间的 canonical contradiction/supersede edge deferred；
- 记录 `same_batch_lifecycle_deferred` 及 candidate refs；
- 不声称下一 batch 会自动补回，因为 terminal decisions 之后 candidates 不再是 pending。

未来只有在产品数据证明该 gap 重要时，才选择 two-phase plan/apply 或独立 maintenance/reconciliation。

## 4. L3：关系语义与现有 decision kind 映射

### 4.1 四种关系

| 关系 | 语义 | 现有 decision kind / outcome |
|---|---|---|
| duplicate | 同一 durable proposition 的重复表达 | exact existing 时 `skip(duplicate_existing_memory)`；同 batch 可 `merge_and_submit` |
| coexist | 主题相关，但两者可以同时成立 | `submit_as_is` / `correct_and_submit`，两条保持 ACTIVE |
| contradiction | 同一主体、条件和有效时间内不能同时成立，但没有明确替换意图 | `contradict_and_submit`，保留双方 ACTIVE，创建非破坏 contradiction edge |
| supersede | 新事实明确替换旧偏好/规则，且具有 replacement intent 或更高权威的新状态 | `supersede_and_submit`，旧节点转为 SUPERSEDED |

### 4.2 判断关系所需上下文

Flash 必须看到 canonical/candidate 已有的关系上下文，而不只看到 statement：

- `applies_when` / `do_not_apply_when`；
- source authority；
- verification status；
- user quote 与相关 source event summaries；
- 可用的时间/有效期信息；
- scope 与 memory type。

示例：

- “喜欢咖啡”与“喜欢拿铁”通常 coexist；
- “不再喝咖啡”可能 contradiction；
- “以后不要用旧偏好，改成喝茶”才满足 supersede replacement intent；
- “今天不想喝咖啡”通常不应覆盖长期偏好。

### 4.3 Durability gate 与 relation gate 正交

“今天不想喝咖啡”首先是 durability 问题：它是否值得成为长期 memory，应由 reflection/governance durability gate 判断。

只有候选通过 durability gate 后，relation layer 才判断它与 existing canonical memories 的关系。relatedness service 不得通过“这像临时状态”自行丢弃 candidate；relation planner 也不得用 contradiction/supersede 修补上游错误地持久化的一次性内容。

## 5. L4：Executor allowlist、数据通路与 TOCTOU

### 5.1 Allowlist 不是 prompt 字段，而是执行上下文

`RelatednessBatchResult` 至少包含：

```text
candidate entry_id
→ surfaced canonical memory ids
→ evidence availability/full-or-partial diagnostics
→ internal channel/score trace
```

同一份结果分成两条通路：

```text
sanitized related entries → candidate snapshot → Flash
authoritative ID allowlist → apply context → MemoryGovernanceExecutor
```

建议让 executor 接收不可变 batch context，例如：

```python
executor.apply_decision(
    decision,
    governance_batch_id=batch_id,
    relatedness_context=relatedness.execution_context,
)
```

对 `supersede_and_submit` / `contradict_and_submit`，executor 必须验证：

1. decision 的 target candidate entry id 存在；
2. lifecycle target id 存在于该 entry id 对应的 advisory allowlist；
3. allowlist 来自当前 governance batch，不能复用旧 batch context；
4. target 在当前 UoW 中仍满足 graph/scope/type/ACTIVE 约束。

以下两种情况必须分别拒绝：

- target id 根本不存在；
- target id 合法存在、同 scope、ACTIVE、类型兼容，但本轮没有 surface 给该 candidate。

这条 allowlist 只约束使用 canonical `memory_id` 的 lifecycle targets，即 `supersede_and_submit.superseded_memory_ids` 与 `contradict_and_submit.contradicted_memory_ids`。

`merge_and_submit.target_entry_ids` 属于 candidate-pool `entry_id` 空间，不受 canonical allowlist 约束。它依赖 whole-batch planner visibility，并继续由 executor 校验 entry 是否属于当前 runtime/current pending batch。不能把 canonical-ID allowlist 检查机械套到 merge，否则会破坏同 batch sibling merge。

### 5.2 Partial degradation 的语义

relatedness batch 需要区分：

- `full`：当前 deployment 已配置、且本次 policy 计划执行的 candidate channels 全部成功；
- `partial`：例如 rerank 失败但 dense 成功，仍产生受验证的 allowlist；
- `unavailable`：没有足够 candidate evidence，无法形成可信 allowlist。

`full` 是 deployment-relative，不是绝对通道集合。没有配置 reranker 的部署不会因此永久成为 `partial`；disabled channel 不算失败。只有已配置并被本轮 policy 选中的通道失败、超时或无法完成时，才降为 `partial`。

单个可选通道失败不能让整批治理失败。v1 冻结为保守行为：`partial` 仍产出 allowlist，供普通 submit/merge、trace、shadow eval 和 planner 理解 related context，但本轮不执行 semantic-evidence-dependent contradiction/supersede。只有 deployment-relative `full` relatedness context 才允许这两类 lifecycle action。这样 partial-channel 测试验证的是“服务没有整体失败、候选信息没有丢失”，而不是在证据链不完整时放宽 destructive action。

`unavailable` 时：

- ordinary `submit_as_is` / `correct_and_submit` / same-batch merge 可继续；
- 不允许 semantic-evidence-dependent contradiction/supersede；
- 记录 structured degradation，不让 Flash 猜 target。

### 5.3 Replacement evidence

supersede 继续限制为单 target。决策必须携带可验证的 replacement evidence reference，例如 source event id 与来自 snapshot 的 quote；executor 验证 reference 确实属于该 candidate 的 source context。

精确相似度、planner reason 或“模型觉得新内容更强”都不能单独构成 replacement evidence。

### 5.4 Transaction-local re-read

snapshot 构造之后，另一个 governance batch 可能已 supersede/reject target。executor 必须在执行 lifecycle mutation 的同一 `MemoryWriteUnitOfWork` 中重新读取 target，并再次验证：

- 存在；
- ACTIVE；
- scope 相同；
- type 允许该 lifecycle action。

失败时不得继续创建 edge；按现有安全语义 downgrade 为 coexist/blocked outcome，并记录稳定 reason code。

该 downgrade 只是安全停点，不保证语义终态正确。例如 B 原本要 supersede A，但 A 已被另一批 supersede，B 可能是新 head 的 duplicate、coexist memory，或仍应 supersede 新 head。v1 可以先非破坏地落为 coexist，但必须记录 `target_drift_requires_regovernance`，不能把它统计为一次正确完成的 lifecycle decision。若真实发生率不可忽略，后续应把该 candidate/decision 送入显式 re-governance 或 maintenance，而不是静默永久接受 coexist。

## 6. 诊断与可观测性

每个 governance batch 记录：

- batch candidate 数、去重后 embed text 数；
- exact / lexical / vector / inline-gap 各通道候选量；
- inline embed 数与 truncation；
- embedding/rerank latency、failure reason；
- per-candidate allowlist IDs；
- exact score/rank（internal only）；
- `full` / `partial` / `unavailable`；
- allowlist rejection；
- target drift rejection；
- `same_batch_lifecycle_deferred`。

provider error 文本不直接进入 Flash。Flash 只需要知道本轮是否提供了 related memories，不需要看到网络异常细节。

## 7. 测试与验收矩阵

以下三项是新增硬门槛：

1. **Allowlist 强制**：Flash 选择一个真实存在、同 scope、ACTIVE、类型合法，但不在该 candidate advisory allowlist 中的 ID；executor 必须拒绝。另有独立测试覆盖完全不存在/编造的 ID。
2. **事务内重读漂移**：snapshot 后 target 被另一个 batch supersede；apply 时 executor 在 UoW 内重读并拒绝 lifecycle action。
3. **部分通道降级**：rerank 失败但 dense 成功，或 dense 失败但另一条已配置 semantic channel 成功；service 仍产生 allowlist 和 `partial` diagnostics，不整体失败。

候选召回本身也有硬门槛，不能只测 destructive-action precision：

- 在 versioned labeled fixture 上计算 `relatedness_recall@k`，其中 k 等于实际进入 Flash 的 canonical candidate 上限；
- fixture 必须分别覆盖 cross-lingual、alias、paraphrase 和 hard-negative slices；
- 初始 gate：overall recall@k ≥ 0.95，各 positive slice recall@k ≥ 0.90，overall candidate miss-rate ≤ 0.05；
- 同时报告 candidate-count/noise 分布，防止靠无界扩大 top-k 达标；
- threshold、fingerprint、builder version 或 top-k 改动必须重跑该 gate。

destructive-action precision 与 candidate recall 是两个独立 gate。不得通过提高 dense threshold、降低 recall 来换取表面的 supersede/contradiction precision。

完整矩阵：

| 层 | 必测内容 |
|---|---|
| batching | N candidates 使用去重后的有限次 `embed_batch` / rerank，不发生 N×串行远程调用 |
| prompt boundary | Flash payload 不包含精确 semantic/rerank score |
| semantic discovery | versioned cross-lingual/alias/paraphrase fixture 达到 recall@k/miss-rate gate，并报告 hard-negative noise |
| threshold calibration | threshold/options 与 fingerprint、fixture version 一起进入 eval manifest；模型或 builder 变化必须重校准 |
| coexist | “喜欢咖啡”/“喜欢拿铁”不产生 contradiction/supersede |
| contradiction | 清晰同主体冲突创建非破坏 edge，双方保持 ACTIVE |
| supersede | 只有明确 replacement evidence 才 supersede，且单 target |
| durability | 临时状态先由 durability gate 拦截，不依赖 relation layer 补救 |
| scope | cross-scope memory 不进入 allowlist |
| committed gap | 刚 commit、尚无 vector 的 bounded node 可通过 inline batch repair 被发现 |
| migration bound | 全库缺新 fingerprint 时 inline embed 不超过 run budget，并记录 truncation |
| allowlist | 合法但未 surface ID 与不存在 ID 分别拒绝 |
| ID-space boundary | canonical allowlist 只约束 lifecycle memory IDs；whole-batch `merge_and_submit.target_entry_ids` 仍可工作 |
| TOCTOU | snapshot 后 status 漂移在同一 UoW 重读时被拒绝 |
| deployment-relative full | 未配置 reranker 的部署在所有已配置通道成功时仍为 full；已配置通道失败才为 partial |
| degradation | partial 继续产出 allowlist但阻止 semantic lifecycle action；unavailable 同样禁止，且保留普通 submit/merge |
| same batch | sibling duplicate 可 merge；canonical contradiction/supersede edge deferred 且有诊断 |
| real LLM | Flash 对 duplicate/coexist/contradiction/supersede fixtures 的 decision precision |

## 8. 推荐实施拆分

### E0：Eval 与数据模型

- 固化 versioned labeled fixture、recall@k/miss-rate/noise 指标，以及独立的 destructive-action false-positive 指标；
- 冻结 threshold calibration manifest 和初始 recall gates；
- 定义 `RelatednessBatchResult`、sanitized prompt view、executor execution context；
- 冻结 score 不进 prompt。

### E1：Batch candidate generation

- 实现 `collect_batch()`；
- exact/lexical/vector/bounded gap-repair union；
- batch embed/rerank、预算和 diagnostics；
- 在固定 labeled corpus、固定 Postgres snapshot、fingerprint 与 options manifest 上 shadow 现有 token-overlap baseline，以 aggregate recall@k/miss-rate/noise 比较，不要求逐条 ANN 排名 diff 完全确定；
- live shadow 只用于观察分布和采集新 fixture，不作为逐条确定性 gate，也不改变 lifecycle writes。

### E2：Executor allowlist 与 TOCTOU

- 把 per-candidate allowlist 显式传入 executor；
- 拒绝 outside-allowlist 与 invented target；
- UoW 内重读 target；
- 加入三项硬门槛测试。

### E3：分级启用关系动作

1. semantic duplicate/merge；
2. non-destructive contradiction；
3. 最后启用 supersede。

supersede 只有在 real-LLM eval 中 replacement precision 达到门槛后才能退出 shadow。

## 9. 非目标

本阶段不做：

- MemScene / subject cluster；
- staged sibling provisional canonical id；
- two-phase governance；
- 自动 maintenance 补回 same-batch lifecycle edge；
- embedding/reranker 直接输出关系类别；
- 用 async vector/search index 充当 target validation truth；
- 首次模型迁移时在 governance run 内同步 embed 全库。

## 10. 契约调整

本设计同步修订 `contracts/MEMORY_SURFACES_CONTRACT.zh.md` 的实际对应条款：

- §1.4：把同步面定义收紧为 committed Postgres projection，并把 whole-batch candidate visibility 与 canonical sync face 分开；
- §9.3：删除 v1 必须读取 transaction-local staged docs 的承诺，冻结 `staged-but-uncommitted` lifecycle gap；
- §11.2：保留 hot-path 不读 Oxigraph、async index 不作为 validation truth，同时声明 same-batch lifecycle deferred；
- §13：加入 allowlist、TOCTOU、partial degradation 和 same-batch deferred 测试守护。

这不是放松 governance validation：executor 仍必须从同步 Postgres canonical face 在事务内复核 target。变化只在于不再把尚未实现、且当前调用时序无法提供的 staged canonical visibility 写成 v1 已有硬契约。
