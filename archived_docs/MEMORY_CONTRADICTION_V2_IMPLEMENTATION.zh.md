# Pulsara Memory Contradiction v2 实施设计

_Created: 2026-06-18_

> 本文描述“非显式冲突”的安全落地方案。目标不是自动删除旧记忆，也不是把冲突记忆从召回里隐藏，而是把冲突结构化为图关系：**两条记忆保持 ACTIVE，额外 materialize `CONTRADICTS` 边；召回时平权展示，并明确告诉 LLM 这些记忆互相冲突。**
>
> 核心原则：**contradiction 是关系，不是生命周期终局 status。**
>
> 本文基于当前代码事实写成实施计划。代码签名/门控/测试矩阵是规范性的；散文是解释性的。

## 0. 已核实代码基线

### 0.1 现有 lifecycle contradiction 语义不适合 v2

`MemoryLifecycle.mark_contradicted()` 当前做两件事：

- 给左右两边 JSON-LD 都追加 `CONTRADICTS` 边。
- 同时把左右两边 `status` 都改成 `NodeStatus.CONTRADICTED`。

代码位置：`src/pulsara_agent/memory/canonical/lifecycle.py`。

这和 v2 目标冲突。因为 recall 当前只召回 `ACTIVE`：

- Postgres search index 候选过滤 `_candidate_filters()` 固定 `status = ACTIVE`。
- `LexicalMemoryRecallService._passes_canonical_filter()` 对非 `ACTIVE` 默认过滤。

所以如果沿用 `mark_contradicted()`，冲突双方会从 cheap recall 消失。这不是“平权提示冲突”，而是“双方隐藏”。

结论：**v2 不得复用当前 `mark_contradicted()` 的 status 语义。**

### 0.2 explainer / reranker 已经能理解 `CONTRADICTS` 边

`src/pulsara_agent/memory/recall/explain.py` 已有：

- `ClaimKind.CONTRADICTED_BY`
- incoming/outgoing `memory.CONTRADICTS.name` → grounded contradiction claim

`src/pulsara_agent/memory/recall/rerank.py` 已有：

- 检测 incoming/outgoing `CONTRADICTS`
- 往 `why` 加 `contradiction_warning`
- 不加分也不降分

这正好符合“平权但标记”的方向。缺的是：

- production governance 决策不会 materialize `CONTRADICTS`。
- recall 不保证把冲突 counterpart 一起带出来。
- projection 对冲突组没有明确结构化说明。

### 0.3 governance 当前只有 supersede，没有 contradiction decision

`GovernanceDecision` 目前包含：

- `SkipDecision`
- `SubmitAsIsDecision`
- `CorrectAndSubmitDecision`
- `MergeAndSubmitDecision`
- `SupersedeAndSubmitDecision`

`SupersedeAndSubmitDecision` 处理的是**显式替换**，并会退休旧 memory。它不适合“非显式冲突”：

- 显式改口：`supersede_and_submit`
- 非显式冲突：应新增 `contradict_and_submit`

### 0.4 related_existing_memories 可作为 v2 前置上下文

`MemoryGovernanceEngine._related_existing_memories()` 当前会给候选注入：

- 同 type
- 同 scope
- `ACTIVE`
- token overlap 排序
- `is_exact_duplicate`

这本来为 supersede v1 服务，也可复用给 contradiction v2。对于“我喜欢蛋挞” vs “我讨厌蛋挞”，两者 type/scope/subject token 通常能进入 related list。

局限仍然存在：subject matching 仍是 LLM 判断，token overlap 只是 v1/v2 的 stopgap。v2 必须用 deterministic gates 限制 blast radius。

### 0.5 contradiction relation 不需要 search-index outbox 重刷旧节点

当前有两套投影，职责不同：

- discovery 前端：`lexical_candidates` / `fts_candidates` 读 `memory_search_index`。该索引只含 `memory_nodes` 的文本字段、scope/type/status/aliases，不含关系边。
- view 水合：`fetch_nodes()` 读 `memory_relations` 的 incoming/outgoing。`PostgresGraphStore.put_jsonld()` 在同一次写入里调用 `_sync_relations_from_document()`，内联刷新 `memory_relations`。

contradiction v2 只改 `CONTRADICTS` relation，不改旧节点的 statement/scope/type/status/aliases。因此：

- 旧节点 search-index 行仍然正确，不需要重刷。
- companion expansion / rerank / explain 都依赖 `fetch_nodes()` 的 relation view，`put_jsonld()` 已经同步 `memory_relations`。
- `WriteSucceededOutcome.contradicted_memory_ids` 仍可保留作审计记录，但**不要**把它接进 `_outbox_memory_ids()`，否则是在解决一个不存在的 index 问题。

## 1. 产品语义

### 1.1 三种相近情况必须分开

| 情况 | 例子 | v2 行为 |
|---|---|---|
| exact duplicate | “我喜欢蛋挞” 又说 “我喜欢蛋挞” | skip duplicate |
| explicit replacement | “我改了，以后不要记我喜欢蛋挞，我讨厌蛋挞” | supersede old |
| non-explicit conflict | A 对话：“我喜欢蛋挞”；B 对话：“我最讨厌蛋挞” | 两条 ACTIVE + `CONTRADICTS` 边 |

非显式冲突不等于替换。系统不能擅自决定哪条更真实，也不能静默删除旧记忆。

安全性上的关键非对称：错误 supersede 会退休一条仍然有效的记忆，用户会遇到“我明明告诉过你”的失败；错误 contradiction 只是多一个冲突 warning，两条记忆仍保持 `ACTIVE`、仍可召回、仍可由用户澄清。因此 contradiction 可以比 supersede 稍微更可尝试，但仍必须限制 blast radius，并在不确定时回落 coexist。

### 1.2 doctrine：ACTIVE + relation

v2 的最终形态：

```text
preference:like-egg-tart
  status = ACTIVE
  contradicts -> preference:hate-egg-tart

preference:hate-egg-tart
  status = ACTIVE
  contradicts -> preference:like-egg-tart
```

召回时两条平权进入 projection，并明确提示：

```text
These recalled memories conflict:
- preference:like-egg-tart: The user likes egg tarts.
- preference:hate-egg-tart: The user hates egg tarts.
Do not silently choose one. Ask the user to clarify or rely on explicit current-turn instruction.
```

### 1.3 `NodeStatus.CONTRADICTED` 的 v2 地位

v2 不使用 `CONTRADICTED status` 表示普通冲突。

建议语义：

- `ACTIVE + CONTRADICTS edge`：普通可召回冲突，v2 主路径。
- `CONTRADICTED status`：保留为未来“被治理层判定不可直接使用的失效节点”或删除/弃用，v2 不生产。

如果实现时保留 enum，不影响 v2；但 production executor 不应调用会把 status 改为 `CONTRADICTED` 的旧方法。

## 2. v2 范围

### 2.1 v2 做什么

- 新增 governance decision：`contradict_and_submit`
- 新增/替换 lifecycle 方法：只 materialize symmetric `CONTRADICTS` edges，不改 status。
- executor 在 UoW 内原子执行：
  - 写新 ACTIVE memory
  - 给新旧双方加 `CONTRADICTS`
  - 记录 decision + outcome + outbox（只需新 memory id 走现有 outbox；旧节点 relation 由 `put_jsonld()` 内联刷新）
- recall 支持 contradiction companion expansion：
  - 召回一条冲突 memory 时，把 ACTIVE 且同 scope 可见的 counterpart 一起带出。
- projection 明确展示 conflict group。
- explainer / memory_related 继续用已 materialized edge 给 grounded claim。

### 2.2 v2 不做什么

- 不做自动 supersede。
- 不把冲突双方 status 改为 `CONTRADICTED`。
- 不跨 scope 标冲突。
- 不做多目标冲突，v2 单 target。
- 不对所有 memory type 开放。v2 只做 `Preference -> Preference`。
- 不解决 structured subject。subject matching 仍是 LLM 判断 + deterministic gates 限制。
- 不用 contradiction 处理临时任务细节；非 durable 仍 skip。

## 3. 新 decision 与 outcome

### 3.1 `ContradictAndSubmitDecision`

目标文件：`src/pulsara_agent/memory/candidates/pool.py`

新增：

```python
class ContradictAndSubmitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["contradict_and_submit"] = "contradict_and_submit"
    target_entry_id: str
    candidate: MemoryCandidate
    contradicted_memory_ids: tuple[str, ...]
    reason: str
```

加入：

```python
GovernanceDecision = Annotated[
    SkipDecision
    | SubmitAsIsDecision
    | CorrectAndSubmitDecision
    | MergeAndSubmitDecision
    | SupersedeAndSubmitDecision
    | ContradictAndSubmitDecision,
    Field(discriminator="kind"),
]
```

说明：

- `target_entry_id` 仍是候选池 entry id。
- `contradicted_memory_ids` 是 canonical memory ids。
- 字段为复数，但 v2 executor 限制最多 1 个。
- 加入 `pulsara_agent.memory.__init__` facade import 和 `__all__`。

### 3.2 `WriteSucceededOutcome.contradicted_memory_ids`

目标文件：`src/pulsara_agent/memory/candidates/pool.py`

在 `WriteSucceededOutcome` 加：

```python
contradicted_memory_ids: tuple[str, ...] = ()
```

原因：

- 审计记录要表达“实际 materialize 了哪些 contradiction edges”。
- executor downgrade 时可以诚实记录“没有实际连边”（空 tuple）。
- 默认空 tuple 保持现有路径兼容。
- **不要**把该字段接到 `MemorySearchIndexSync._outbox_memory_ids()`；它不是 index dirty list。

### 3.3 新增 `MemoryContradictionLinkedEvent`

不建议把 contradiction ids 塞进 `MemoryWriteResultEvent`。它表示新 memory 写入结果；contradiction 是 governance/lifecycle relation mutation，不是 write gate 结果。

也不要复用 `MemoryMaintenanceAppliedEvent`。该 event 的 schema 是 `proposal_id + target_memory_id + action`，适合 maintenance pass；inline governance contradiction 没有真实 maintenance proposal，复用会迫使我们合成一个假的 `proposal_id`，语义不诚实。

参考 `MemorySupersededEvent(memory_id, superseded_by)`，新增专属事件：

```python
class MemoryContradictionLinkedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_CONTRADICTION_LINKED] = EventType.MEMORY_CONTRADICTION_LINKED
    memory_id: str
    contradicts: str
```

实现要求：

- `EventType` 增加 `MEMORY_CONTRADICTION_LINKED`。
- `event/__init__.py` re-export。
- `AgentEvent` union 加该事件。
- `link_contradiction(left, right)` 两边各返回一个 event：
  - `memory_id=left_id, contradicts=right_id`
  - `memory_id=right_id, contradicts=left_id`

## 4. Lifecycle 改造

### 4.1 新方法：`link_contradiction`

目标文件：`src/pulsara_agent/memory/canonical/lifecycle.py`

新增方法，或将旧 `mark_contradicted` 替换为此语义。建议不保留兼容 wrapper，避免名字继续暗示 status mutation。

```python
def link_contradiction(
    self,
    *,
    left_id: str,
    right_id: str,
    governance_batch_id: str,
    graph_id: str | None = None,
) -> list[AgentEvent]:
    """Materialize a symmetric contradiction edge without changing node status."""

    left_doc = self.graph.get_jsonld(left_id, graph_id=graph_id)
    right_doc = self.graph.get_jsonld(right_id, graph_id=graph_id)
    _append_node_ref(left_doc, memory.CONTRADICTS.name, right_id)
    _append_node_ref(right_doc, memory.CONTRADICTS.name, left_id)
    self.graph.put_jsonld(left_doc, graph_id=graph_id)
    self.graph.put_jsonld(right_doc, graph_id=graph_id)
    return [
        MemoryContradictionLinkedEvent(..., memory_id=left_id, contradicts=right_id),
        MemoryContradictionLinkedEvent(..., memory_id=right_id, contradicts=left_id),
    ]
```

必须保持：

- symmetric edge
- idempotent append
- 不调用 `set_status`
- 不更新时间语义可以讨论；如果 `put_jsonld` 会更新 projection updated_at，则接受；否则可显式更新 docs，但不改 status。

### 4.2 删除或改写旧 `mark_contradicted`

当前测试 `tests/test_memory_lifecycle.py::test_in_memory_lifecycle_contradiction_updates_both_nodes_edges_and_events` 明确断言 status 变为 `CONTRADICTED`。

v2 应改成：

- 测试名：`test_lifecycle_link_contradiction_keeps_both_nodes_active_and_adds_edges`
- 断言：
  - left status remains `ACTIVE`
  - right status remains `ACTIVE`
  - symmetric `CONTRADICTS` edges exist
  - `MemoryContradictionLinkedEvent` emitted for both directions

这一步是语义切换的 load-bearing test。

## 5. Governance engine

### 5.1 prompt 增加 decision kind

目标文件：`src/pulsara_agent/memory/governance/engine.py`

在 return shape 和 allowed kinds 中加入：

```json
{
  "kind": "contradict_and_submit",
  "target_entry_id": "pool:...",
  "candidate": {...},
  "contradicted_memory_ids": ["preference:..."],
  "reason": "The new Preference conflicts with an existing same-scope Preference, but the user did not explicitly request replacement."
}
```

### 5.2 prompt 规则

新增规则：

- Use `supersede_and_submit` only for explicit replacement intent.
- Use `contradict_and_submit` only when all are true:
  - new candidate is a durable `Preference`
  - existing memory id comes from `related_existing_memories`
  - existing memory is `Preference`, `ACTIVE`, same scope
  - new candidate and existing memory are about the same subject
  - statements cannot both be true as user preferences
  - user did not explicitly ask to replace the old one
- If unsure whether it is same subject, use coexist (`submit_as_is` / `correct_and_submit`), not contradiction.
- Never contradict an exact duplicate.
- Never invent canonical memory ids.
- Prefer skip for weak/non-durable candidate.

### 5.3 related existing memories

Current `_related_existing_memories()` already filters same type/scope/ACTIVE and ranks by overlap. v2 can reuse it.

Recommended small additions:

- include `overlap_score`
- include outgoing/incoming `CONTRADICTS` ids if cheap to fetch; otherwise not required for v2
- keep `is_exact_duplicate`

Do not widen to cross-type in v2.

### 5.4 probe 结果不能直接替代 production prompt 验收

`evals/contradiction_probe.py` 使用独立的 4-label prompt（`skip/coexist/supersede/contradict`）测“给定 doctrine + related list 时，模型能否做判断”。这证明的是承重判断假设，不等价于 production `MemoryGovernanceEngine` prompt 已经通过。

PR3 必须用生产 prompt 再跑等价 gate：

- decision space 是真实 `GovernanceDecision` union，而不是 4 个抽象 label。
- 同时存在 `correct_and_submit / merge_and_submit / scope rules / dedupe / supersede` 等约束。
- related list 和 target ids 必须用 production snapshot 形状。

结论：probe 的 18/18 只能说明“值得继续”，不能作为 PR3 验收替代品。

## 6. Executor gates

目标文件：`src/pulsara_agent/memory/governance/executor.py`

新增 constants：

```python
_CONTRADICTABLE_TYPES: frozenset[str] = frozenset({"Preference"})
_MAX_CONTRADICTED_PER_DECISION = 1
_CONTRADICTION_DOWNGRADE_SENTINEL = "contradiction_downgraded_to_coexist"
```

### 6.1 deterministic gates C0-C6

`_validate_contradiction_targets(decision, uow) -> tuple[tuple[str, ...], str | None]`

Gates：

- C0：candidate.kind in `_CONTRADICTABLE_TYPES`
- C1：`contradicted_memory_ids` 非空
- C2：len <= 1
- C3：每个 old id exists in same graph
- C4：old status == `ACTIVE`
- C5：old scope == candidate.scope
- C6：old type intersects `_CONTRADICTABLE_TYPES`

不做：

- 不做 semantic subject 判断；这是 LLM proposal 的职责。
- 不做 cross-scope conflict。
- 不接受 non-ACTIVE target。

### 6.2 validate-before-submit

顺序：

```text
1. candidate = _candidate_for_decision(decision)
2. allowed_write_scopes gate
3. exact duplicate gate already_exists(...)
4. if ContradictAndSubmitDecision:
     validate old targets C0-C6
     if fail: remember downgrade reason
5. submit new candidate
6. require new_active_id
7. if valid contradiction and new ACTIVE:
     uow.lifecycle.link_contradiction(left_id=old_id, right_id=new_active_id, ...)
8. record effective decision + write outcome
9. append existing decision outbox; existing `memory_id` indexing is enough
10. commit
11. emit outcome.events + `MemoryContradictionLinkedEvent`s after commit
```

### 6.3 downgrade semantics

If decision is `ContradictAndSubmitDecision` but any gate fails, or new write is not ACTIVE:

- do not materialize edge
- write new candidate if possible
- record effective decision as `CorrectAndSubmitDecision`
- reason starts with `_CONTRADICTION_DOWNGRADE_SENTINEL`
- `WriteSucceededOutcome.contradicted_memory_ids == ()`

This mirrors supersede v1 audit honesty:

- record what actually happened
- keep original proposal distinguishable via sentinel
- no false audit trail saying contradiction was linked

### 6.4 legacy path

If `memory_write_uow_factory is None`:

- `ContradictAndSubmitDecision` must downgrade to coexist.
- It must not try to link edges.
- It must not append governance audit candidate for contradiction-origin decisions.

Reason：multi-node mutation requires UoW atomicity.

## 7. Index/outbox

目标文件：无生产改动。

不要修改 `src/pulsara_agent/memory/canonical/index_sync.py`。

原因：

- `memory_search_index` discovery 只依赖文本字段、scope/type/status/aliases；contradiction 不改这些字段。
- `memory_relations` 由 `PostgresGraphStore.put_jsonld()` 同事务内联刷新；`link_contradiction()` 对左右两边都 `put_jsonld()` 后，incoming/outgoing relation view 已经是新的。
- companion expansion / rerank / explain 都走 `fetch_nodes()` 的 relation view，不读 search index 的关系衍生字段。

`WriteSucceededOutcome.contradicted_memory_ids` 是审计字段，不是 index dirty list。PR2 的测试应断言不需要 contradicted old id 进入 `_outbox_memory_ids()`。

## 8. Recall / projection

### 8.1 Keep status filter unchanged

Because v2 keeps both nodes `ACTIVE`, no change is needed to:

- `_candidate_filters(status=ACTIVE)`
- `_passes_canonical_filter()`

Do not loosen recall to include `NodeStatus.CONTRADICTED` in v2. That would revive the old status ambiguity.

### 8.2 Add contradiction companion expansion

Problem：if query matches only one side of a contradiction, LLM may see one memory with a warning but not the conflicting counterpart.

Add relation expansion inside `LexicalMemoryRecallService._recall_sync()` after initial candidate filtering:

```text
for each selected ACTIVE item:
  inspect view.incoming/outgoing CONTRADICTS edges
  collect counterpart ids that are not already in views
  second fetch_nodes(counterpart_ids) to hydrate counterpart views
  merge counterpart views back into views before direct_relation_rerank(...)
  keep counterparts only if:
    counterpart exists
    counterpart passes _passes_canonical_filter(view, query)
    counterpart not recently suppressed
    counterpart not already selected
  append with why += ("contradiction_companion", "contradiction_warning")
```

Ordering rule：

- Do not boost either side.
- Keep original matched item order.
- Insert companion immediately after the matched item if space allows.
- If limit is full, reserve one slot for companion by dropping the lowest-ranked non-companion item.

This is the key to “平权展示冲突”。

实现注意：当前 `views = fetch_nodes(ranked_ids)` 只包含 query 命中的候选。companion 的意义正是“没有命中 query 的另一侧”，所以必须有一个有界的 second fetch。这个 round-trip 只在已选 item 带 `CONTRADICTS` edge 时发生，通常很少。

排序/limit 是本节最容易出 bug 的部分。实现时优先保持简单：

- 先完成“有空间时插入 companion”的版本。
- 满 limit 时若要 reserve slot，必须处理 companion-of-evicted、多个 matched items 共享 companion、companion 已被 suppressed/selected 等边界。
- 对这些边界没有测试前，不要写聪明的替换逻辑。

### 8.3 Extend `RecallItem`

Add:

```python
conflicts_with: tuple[str, ...] = ()
```

For normal rerank warning, fill from direct relation ids. For companion item, also fill back to source id.

### 8.4 Projection must name conflict pairs

目标文件：`src/pulsara_agent/memory/recall/projection.py`

Current render includes `why=...`, but v2 should make conflict explicit.

Recommended rendering:

```text
<recalled-memory-projection do_not_write_back="true">
- [preference:like] The user likes egg tarts. (...; why=lexical,contradiction_warning; conflicts_with=preference:hate)
- [preference:hate] The user hates egg tarts. (...; why=contradiction_companion,contradiction_warning; conflicts_with=preference:like)

Conflict warnings:
- preference:like conflicts with preference:hate. Do not silently choose one; ask for clarification or follow explicit current-turn instruction.
</recalled-memory-projection>
```

Also add dict metadata:

```python
"conflict_groups": [
  {"memory_ids": ["preference:like", "preference:hate"], "kind": "contradiction"}
]
```

如果 working context 和 recalled-memory projection 被 `_merge_projections()` 合并，`conflict_groups` 必须像 `projection_kind/projection_kinds` 一样被 union 保留下来。否则混合 projection 会丢掉 dict-level conflict metadata，只剩 summary fence 里的文本 warning。这是 v2 必测项。

### 8.5 Rerank remains non-scoring

`direct_relation_rerank()` currently adds `contradiction_warning` but no bonus. Keep that.

Contradiction should not imply “more true” or “less true”。它只是一个 warning。

## 9. Governance execution record

### 9.1 Successful contradiction

If edge is actually linked：

```json
{
  "decision": {
    "kind": "contradict_and_submit",
    "target_entry_id": "pool:new-hate",
    "candidate": {...},
    "contradicted_memory_ids": ["preference:like"],
    "reason": "..."
  },
  "write_outcome": {
    "kind": "write_succeeded",
    "memory_id": "preference:hate",
    "node_status": "active",
    "contradicted_memory_ids": ["preference:like"]
  }
}
```

### 9.2 Downgraded contradiction

If edge is not linked：

```json
{
  "decision": {
    "kind": "correct_and_submit",
    "reason": "contradiction_downgraded_to_coexist: target_scope_mismatch; original: ..."
  },
  "write_outcome": {
    "kind": "write_succeeded",
    "memory_id": "preference:hate",
    "contradicted_memory_ids": []
  }
}
```

No separate governance audit candidate should be appended for contradiction-origin decisions, matching supersede-origin audit policy.

## 10. Tests

### 10.1 Lifecycle

Replace current destructive contradiction test:

- `test_lifecycle_link_contradiction_keeps_both_nodes_active_and_adds_edges`
  - left/right start ACTIVE
  - call `link_contradiction`
  - both remain ACTIVE
  - symmetric `CONTRADICTS`
  - events mention `link_contradiction_with`

Postgres test:

- after `MemorySearchIndexSync.rebuild/sync`
- both memories are recallable
- fetched views include incoming/outgoing `CONTRADICTS`

### 10.2 Executor

Add `tests/test_memory_contradiction.py` or extend governance tests.

Cases：

1. happy path:
   - old ACTIVE Preference exists
   - new Preference candidate conflicts
   - `ContradictAndSubmitDecision`
   - new ACTIVE written
   - old remains ACTIVE
   - new remains ACTIVE
   - symmetric edges exist
   - outcome.contradicted_memory_ids == (old_id,)
   - emitted events include two `MemoryContradictionLinkedEvent`s
   - existing outbox indexes the new memory id only; old target is not treated as search-index dirty

2. non-Preference candidate:
   - downgrade to coexist
   - no edge

3. missing target:
   - downgrade
   - no edge

4. old target non-ACTIVE:
   - downgrade
   - no edge

5. scope mismatch:
   - downgrade
   - no edge

6. multi-target:
   - downgrade
   - no edge

7. exact duplicate:
   - dedupe skip before contradiction
   - old remains ACTIVE
   - no edge

8. write returns NEEDS_REVIEW/REJECTED:
   - no edge
   - old remains ACTIVE
   - outcome.contradicted_memory_ids empty

9. lifecycle raises after first put:
   - UoW rollback leaves no partial edge

10. legacy no-UoW path:
   - contradiction decision downgraded
   - no edge
   - no governance audit candidate appended

11. index/outbox non-requirement:
   - `_outbox_memory_ids()` does not include `contradicted_memory_ids`
   - `fetch_nodes()` still sees fresh relation after `put_jsonld()` without search-index sync

### 10.3 Recall/projection

1. both ACTIVE contradiction nodes recall normally.
2. query matching only one side expands counterpart via second `fetch_nodes(counterpart_ids)`.
3. companion respects scope filter.
4. companion respects status filter.
5. projection includes:
   - both memory ids
   - `contradiction_warning`
   - `contradiction_companion` on companion
   - `conflicts_with`
   - `conflict_groups`
6. `_merge_projections()` preserves/merges `conflict_groups` when working context and recalled memory coexist.
7. rerank does not boost or suppress contradiction items.

### 10.4 Explain / related tools

Existing explainer should mostly pass once edge exists.

Add:

- `memory_explain(preference:like)` includes `contradicted_by`
- `memory_related(preference:like)` shows outgoing/incoming contradiction relation
- validator rejects ungrounded contradiction claim

### 10.5 Governance engine parsing

1. `_parse_governance_output()` parses `contradict_and_submit`.
2. prompt few-shot:
   - explicit replacement → `supersede_and_submit`
   - non-explicit conflict → `contradict_and_submit`
   - uncertain relation → coexist
   - exact duplicate → skip
3. production prompt gate repeats the probe categories using real `MemoryGovernanceInput` / candidate snapshot shape:
   - explicit replacement
   - non-explicit conflict
   - temporary/story/one-off skip
   - ambiguous subtype/context coexist
   - buried target id selection

### 10.6 Real LLM smoke

Optional but recommended after deterministic tests:

1. Seed canonical:
   - `Preference`: “The user likes egg tarts.” `ctx:user`
2. New dialogue:
   - user says “Please remember this: I absolutely hate egg tarts.”
3. Run governance:
   - expect `contradict_and_submit` or, if model is conservative, coexist is acceptable for first smoke?

Better real LLM gate should be two cases:

- explicit “I changed my mind...” → supersede
- non-explicit “I hate egg tarts” → contradiction

If real model frequently chooses coexist for non-explicit conflict, that is acceptable product-safety data, but then contradiction v2 should remain deterministic/manual until prompt/evals improve.

Note：standalone `evals/contradiction_probe.py` 的 18/18 结果是前置证据，不是本节的生产验收。本节必须跑 production governance prompt，因为真实 decision space 和 prompt 约束更复杂。

## 11. Implementation sequence

### PR1：types + lifecycle semantic switch

Files:

- `event/events.py`
- `event/__init__.py`
- `memory/candidates/pool.py`
- `memory/__init__.py`
- `memory/canonical/lifecycle.py`
- `tests/test_memory_lifecycle.py`

Exit:

- `from pulsara_agent.memory import ContradictAndSubmitDecision` works
- `MemoryContradictionLinkedEvent` is in `EventType`, `AgentEvent`, and facade exports
- lifecycle contradiction test asserts ACTIVE + symmetric edge
- old destructive status assertion removed

### PR2：executor + outcome

Files:

- `memory/governance/executor.py`
- tests for executor/UoW/audit

Exit:

- all C0-C6 gates tested
- happy path atomic
- downgrade records are honest
- no index_sync change is required; old target relation freshness is covered by `put_jsonld()`/`fetch_nodes()` tests

### PR3：engine prompt + related context

Files:

- `memory/governance/engine.py`
- governance parser/prompt tests

Exit:

- LLM can output new decision shape
- prompt clearly distinguishes supersede vs contradiction vs coexist

### PR4：recall companion expansion + projection warning

Files:

- `memory/recall/service.py`
- `memory/recall/projection.py`
- `memory/hooks/durable.py` if `_merge_projections()` needs to carry `conflict_groups`
- `memory/recall/rerank.py` if needed
- recall/projection tests

Exit:

- querying one side can bring the counterpart
- projection explicitly tells LLM not to silently pick a side
- mixed working-context + recall projection preserves `conflict_groups`

### PR5：real LLM smoke

Files:

- `tests/test_real_llm_integration.py`

Exit:

- explicit replacement still supersedes
- non-explicit conflict either contradicts or, if model is conservative, produces documented coexist data

## 12. Open design choices

### 12.1 Should v2 require explicit “remember”?

For safety, yes at first. Let main agent / reflector produce candidates only when the utterance is intended as durable memory. Contradiction v2 should not turn every casual negative sentence into a durable conflicting memory.

### 12.2 Should contradiction target count stay 1?

Yes for v2. Multiple contradictory old memories requires conflict group semantics and more complex projection. Single target keeps audit and UX understandable.

Known blind spots of single-target v2:

- 非对称可见性：如果新 memory 实际上与两个旧 ACTIVE memories 都冲突，但 v2 只连其中一个，query 命中未连边的旧 memory 时不会看到 contradiction warning。
- multi-target downgrade：如果 LLM 提出两个 `contradicted_memory_ids`，C2 会把整个 contradiction 降级为 coexist，因此真实存在的多方冲突可能没有 warning。
- standalone probe 已加入 multi-target cases，并在当前 prompt 下选择 coexist；但这仍需用 production governance prompt 重跑，不能直接当生产验收。

这些不是 v2 blocker，因为 contradiction 是非破坏性的；但文档和产品表述不能承诺“所有冲突都会被标出来”。v2 承诺的是：**当单 target contradiction 被 materialize 后，它会平权、可召回、可解释地展示。**

### 12.3 Should conflict warnings affect answer policy?

Projection should instruct:

- do not silently choose one
- prefer current-turn explicit instruction
- ask user to clarify if the answer depends on the conflict

But this is prompt guidance, not a hard runtime rule.

### 12.4 What about existing `CONTRADICTED` status rows?

Likely none in production. If any exist:

- do not silently re-activate them in this PR
- write a one-off migration/test fixture only if real data appears

v2 production path should simply stop producing new `CONTRADICTED` statuses.

### 12.5 Should probe success be treated as production readiness?

No. The standalone probe is useful because it isolates the LLM semantic judgment, but production readiness requires two more checks:

- production governance prompt gate：真实 `MemoryGovernanceEngine` prompt、真实 decision union、真实 candidate snapshot。
- end-to-end producer/retrieval gate：candidate producer 是否会提出 durable candidate，`related_existing_memories` 是否把 correct counterpart 捞到 LLM 面前。

没有这两项，probe 只能降低判断层风险，不能证明完整自动 contradiction path 已安全。

## 13. Summary

The safe v2 model is:

```text
supersede = explicit replacement + destructive lifecycle transition
contradiction = non-explicit conflict + non-destructive relation edge
coexist = uncertainty fallback
```

Do not make `CONTRADICTED status` recallable. That keeps the old ambiguity alive.

Instead:

- keep both memories ACTIVE
- materialize symmetric `CONTRADICTS`
- expand recall to include the conflicting counterpart
- render an explicit conflict warning to the LLM
- never auto-resolve without user clarification or explicit replacement intent
