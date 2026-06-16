# Pulsara Supersede v1 实现说明书

_Created: 2026-06-16_

> 本文是 `MEMORY_LIFECYCLE_SUPERSEDE_DESIGN.zh.md` 的**可落地化重写**。设计文档讲教义与边界(为什么);本文讲**具体改哪个文件、加什么签名、executor 怎么校验降级、UoW 内按什么顺序落地、跑什么测试**(怎么做)。
>
> 范围严格 = 设计文档的 **v1**:explicit-only + Preference-only + 单目标。contradiction、类型拓宽、多目标、inferred maintenance、subject 结构化全部留 v2,本文不涉及。
>
> 所有"代码现状"主张均经 `file:line` 核实(§1)。代码签名/SQL 是规范性的;散文是解释性的。冲突时以签名为准。

## 0. 如何使用本文

- 按 §6 的实现顺序切 PR,每步 tests 全绿再下一步。
- `【现状】`=今天代码已如此(§1 已核实,勿再勘探);`【新增】`=本文要求新建;`【改造】`=改既有;`【守】`=对应设计文档的不变量/门。
- v1 的安全底线:**任何校验失败都降级为 coexist(写新、不退旧)或 skip,绝不报错销毁**;**supersede 必须走 UoW 原子路径**,无 UoW 时不允许 supersede。

## 1. 已核实的代码基线（实现起点，勿再勘探）

每条都经源码核对,是本文所有改动的前提。

### 1.1 decision 判别联合 + outcome 形状

`GovernanceDecision` = `SkipDecision | SubmitAsIsDecision | CorrectAndSubmitDecision | MergeAndSubmitDecision`,`Field(discriminator="kind")`([candidates/pool.py:104](src/pulsara_agent/memory/candidates/pool.py:104))。每个 decision 的目标都是**候选池 entry_id**(`target_entry_id` 或 `target_entry_ids`),无一引用 canonical `mem:*` 节点。

`WriteSucceededOutcome`([candidates/pool.py:116](src/pulsara_agent/memory/candidates/pool.py:116))字段:`memory_id / memory_type / node_status / confidence_level / verification_status / gate_reason / write_event_ids`。这是记录"写成功了什么"的审计载体——supersede 要把"退休了哪些旧节点"也记在这里。

`decision_target_entry_ids`([candidates/pool.py:349](src/pulsara_agent/memory/candidates/pool.py:349))与 executor 私有 `_target_entry_ids`([governance/executor.py:305](src/pulsara_agent/memory/governance/executor.py:305))都是:`Skip|Merge → target_entry_ids`,其余 `→ (target_entry_id,)`。新决策若用单数 `target_entry_id` 字段,这两个 helper 的 fallback 分支**自动覆盖**,无需改。

### 1.2 executor 双路径，supersede 只能走 UoW 路径

`MemoryGovernanceExecutor.apply_decision`([governance/executor.py:49](src/pulsara_agent/memory/governance/executor.py:49))有两条路径:有 `memory_write_uow_factory` 时走 `_apply_decision_with_uow`([:104](src/pulsara_agent/memory/governance/executor.py:104)),否则走 legacy 内联路径。**supersede 涉及多节点原子变更,只能在 UoW 路径实现**;legacy 路径遇 supersede 决策必须降级为 coexist(见 §4.3)。

UoW 路径已有的关键调用形态(supersede 要复用):
- `already_exists(candidate, uow.graph, graph_id=uow.resolved_graph_id)` 判重([:132](src/pulsara_agent/memory/governance/executor.py:132))
- `outcome = uow.memory_write_service.submit(candidate, event_context=context)`([:146](src/pulsara_agent/memory/governance/executor.py:146))——`outcome` 是 `MemoryWriteOutcome`
- `uow.ensure_event_context_rows(context)`([:147](src/pulsara_agent/memory/governance/executor.py:147))
- `uow.decisions.append_decision(record)` + `uow.outbox.append_decision(record, graph_id=uow.resolved_graph_id)`([:159-160](src/pulsara_agent/memory/governance/executor.py:159))
- 块退出即 commit;**事件在块外** `self.event_log.extend(outcome.events)`([:162](src/pulsara_agent/memory/governance/executor.py:162))

`_write_outcome(outcome, events)`([:261](src/pulsara_agent/memory/governance/executor.py:261))把 `outcome.events` 里的 `MemoryWriteResultEvent` 转成 `WriteSucceededOutcome`。supersede 要在这里补 `superseded_memory_ids`。

### 1.3 UoW 暴露的能力（全部 connection-bound，同事务）

`MemoryWriteUnitOfWork`([canonical/unit_of_work.py:41](src/pulsara_agent/memory/canonical/unit_of_work.py:41))在 `__enter__` 构造并暴露:`graph: PostgresGraphStore`、`memory_write_service`(其 ledger.graph == uow.graph)、`decisions`、`outbox`、`lifecycle: MemoryLifecycle`([:71](src/pulsara_agent/memory/canonical/unit_of_work.py:71))、`resolved_graph_id`([:98](src/pulsara_agent/memory/canonical/unit_of_work.py:98))。**`uow.lifecycle` 已经存在且 connection-bound——supersede 直接调它即可,不必新建。**

### 1.4 lifecycle.supersede 的确切签名与行为

```python
# canonical/lifecycle.py:28
def supersede(self, *, old_id: str, new_id: str, governance_batch_id: str,
              graph_id: str | None = None) -> list[AgentEvent]:
    # get_jsonld(new_id) → append SUPERSEDES edge onto NEW doc → put_jsonld(new_doc)
    # → set_status(old_id, SUPERSEDED) → return [MemorySupersededEvent(...)]
```

它 `get_jsonld(new_id)`(读新节点挂边),所以 **`new_id` 必须先写入**——这是 write-then-retire 强制顺序的代码来源([lifecycle.py:40-48](src/pulsara_agent/memory/canonical/lifecycle.py:40))。返回 `MemorySupersededEvent`,**不 emit**(调用方在 commit 后 emit)。

### 1.5 candidate 都带 scope + kind（executor 门可机械校验 G0/G3）

`MemoryCandidateBase` 有 `scope: str`([event/candidates.py:26](src/pulsara_agent/event/candidates.py:26));每个变体有 `kind: Literal[...]`。`PreferenceCandidate.kind == "Preference"`([:36](src/pulsara_agent/event/candidates.py:36))。所以 executor 能机械校验"新候选是 Preference""新旧同 scope"。

### 1.6 canonical 节点状态/scope 可在 UoW 内读到（G1/G2/G3 校验源）

`uow.graph.get_jsonld(node_id, graph_id=...)` 返回完整 JSON-LD([graph/postgres.py](src/pulsara_agent/graph/postgres.py))。`memory.STATUS.name` / `memory.SCOPE.name` / `@type` 字段可读出 status/scope/type。`set_status` 同时回写 `graph_documents` 与 `memory_nodes` 投影,所以同事务内 `get_jsonld` 看到的 status 是最新的(§1.3 的 same-transaction 已被 `test_postgres_uow_dedupe_sees_uncommitted_same_transaction_node` 证明)。

> 校验 old target 用 `uow.graph.get_jsonld`(同 connection),**不要**新开 `PostgresMemoryQuery`(那是独立连接,看不到未提交写,且 v1 校验在 submit 之前、还没有未提交写,但统一用 uow.graph 保持一致与正确)。

### 1.7 engine 的 existing-memory 注入是 statement-exact（v1 拦路石）

`_existing_memory_matches`([governance/engine.py:367](src/pulsara_agent/memory/governance/engine.py:367))按 `_normalize(statement)` + `scope` 精确比对,只识别近重复。LLM 因此**看不见"同 scope 同 type、不同 statement"的旧节点**——是 §4 要放宽并改名的对象。它在 `_candidate_snapshot` 里每 batch 都被调用([engine.py:163](src/pulsara_agent/memory/governance/engine.py:163)),挂到 `snapshot["existing_memory_matches"]`。

### 1.8 engine 输出自动解析判别联合

`MemoryGovernanceOutput.decisions: list[GovernanceDecision]`([engine.py:42](src/pulsara_agent/memory/governance/engine.py:42)),`_parse_governance_output` 走 `model_validate`([engine.py:175](src/pulsara_agent/memory/governance/engine.py:175))。**只要把新决策加进判别联合,LLM 输出的 `{"kind":"supersede_and_submit",...}` 会被自动解析**,无需改解析器。系统提示([engine.py:190](src/pulsara_agent/memory/governance/engine.py:190))需新增该 kind 的说明与规则。

## 2. 任务总览

| # | 任务 | 目标文件 | 类型 | 守 |
|---|---|---|---|---|
| 2.1 | `SupersedeAndSubmitDecision` 加进判别联合 | `candidates/pool.py`【改造】 | 决策 | 设计 §5.1 |
| 2.2 | `WriteSucceededOutcome` 加 `superseded_memory_ids` | `candidates/pool.py`【改造】 | 审计 | 设计 §6 step5 |
| 2.3 | `related_existing_memories`（重命名 + 放宽 + 排序） | `governance/engine.py`【改造】 | 前置 | 设计 §4 |
| 2.4 | 系统提示新增 supersede kind + 规则 | `governance/engine.py`【改造】 | 前置 | 设计 §5.3 |
| 2.5 | executor 校验门 G0–G5 + 降级 | `governance/executor.py`【改造】 | 核心 | 设计 §5.2 |
| 2.6 | executor UoW supersede 落地序列 | `governance/executor.py`【改造】 | 核心 | 设计 §6 |
| 2.7 | `_write_outcome` 透传 superseded ids | `governance/executor.py`【改造】 | 审计 | 设计 §6 step5 |
| 2.8 | 测试矩阵 | `tests/test_memory_supersede.py`【新增】 | 验收 | 设计 §8 |

依赖顺序见 §6。核心约束:**2.5 的校验先于 2.6 的写入**(设计 §6——decide-before-mutate)。

## 3. 决策类型与 outcome 改动

### 3.1 `SupersedeAndSubmitDecision`（任务 2.1）

【改造】`candidates/pool.py`,在 4 个既有决策类之后新增,并加进判别联合:

```python
class SupersedeAndSubmitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["supersede_and_submit"] = "supersede_and_submit"
    target_entry_id: str                      # pending 池条目（新记忆来源）——单数，复用 _target_entry_ids fallback
    candidate: MemoryCandidate                # 要创建的新记忆
    superseded_memory_ids: tuple[str, ...]    # 要退休的【已治理 canonical 节点 id】（v1 校验后最多 1 个）
    reason: str

GovernanceDecision = Annotated[
    SkipDecision | SubmitAsIsDecision | CorrectAndSubmitDecision
    | MergeAndSubmitDecision | SupersedeAndSubmitDecision,   # ← 新增
    Field(discriminator="kind"),
]
```

要点:
- **用单数 `target_entry_id`**(像 `CorrectAndSubmitDecision`),这样 `_target_entry_ids` / `decision_target_entry_ids` 的 fallback 分支(§1.1)自动覆盖,不必改这两个 helper。
- `superseded_memory_ids` 声明为 `tuple[str, ...]`(可能多个),但 executor G5 把 v1 实际退休数限制在 ≤1(设计 §5.2)。声明保留复数是为 v2 兼容,**不**意味着 v1 会退休多个。
- 加进判别联合后,§1.8 的 LLM 输出解析自动支持,无需改 `_parse_governance_output`。
- **必须 re-export 到 facade**:既有 4 类决策都从 `pulsara_agent.memory` re-export(`memory/__init__.py` 的 import 与 `__all__` 各列 `SkipDecision/SubmitAsIsDecision/CorrectAndSubmitDecision/MergeAndSubmitDecision`,已核实)。`pulsara_agent.memory` 是稳定入口,**`SupersedeAndSubmitDecision` 必须同样补进 `memory/__init__.py` 的 import 块和 `__all__`**,否则上层 facade 缺口、实现者只改 `candidates/pool.py` 会导致 `from pulsara_agent.memory import SupersedeAndSubmitDecision` 失败。这是 PR1 的一部分,不是后补。

### 3.2 `WriteSucceededOutcome` 加 `superseded_memory_ids`（任务 2.2，守审计诚实）

supersede 退休了哪些旧节点,必须进审计记录。最小改动是给 `WriteSucceededOutcome` 加一个**默认空**的字段(向后兼容既有 4 类决策):

```python
class WriteSucceededOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["write_succeeded"] = "write_succeeded"
    memory_id: str
    memory_type: str
    node_status: memory.NodeStatus
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    gate_reason: str
    write_event_ids: tuple[str, ...]
    superseded_memory_ids: tuple[str, ...] = ()   # ← 新增，默认空；非 supersede 决策保持 ()
```

默认 `()` 让所有既有路径无需改造即合法。supersede 成功时填入实际退休的 id(§4.4)。

## 4. executor：校验门 + 降级 + 落地序列

这是 v1 的核心。全部发生在 `governance/executor.py`。

### 4.1 supersedable 类型常量（executor 强制，守 G0）

【新增】模块级常量,executor 强制(非仅 prompt):

```python
# governance/executor.py 顶部
_SUPERSEDABLE_TYPES: frozenset[str] = frozenset({"Preference"})  # v1；v2 一行拓宽
_MAX_SUPERSEDED_PER_DECISION = 1                                 # v1 单目标（G5）
```

### 4.2 `_validate_supersede_targets`（任务 2.5，守 G0–G5）

【新增】executor 方法,**在 submit 之前**调用(decide-before-mutate)。它不抛错——返回"通过的目标列表 + 可选降级原因",由调用方据此决定 supersede 还是降级:

```python
def _validate_supersede_targets(
    self,
    decision: "SupersedeAndSubmitDecision",
    uow: MemoryWriteUnitOfWork,
) -> tuple[tuple[str, ...], str | None]:
    """返回 (valid_old_ids, downgrade_reason)。downgrade_reason 非 None ⇒ 降级为 coexist。"""
    candidate = decision.candidate
    # G0：新候选必须是 Preference
    if candidate.kind not in _SUPERSEDABLE_TYPES:
        return (), f"type_not_supersedable:{candidate.kind}"
    # G5：v1 单目标
    if len(decision.superseded_memory_ids) > _MAX_SUPERSEDED_PER_DECISION:
        return (), "too_many_supersede_targets"
    valid: list[str] = []
    for old_id in decision.superseded_memory_ids:
        try:
            old_doc = uow.graph.get_jsonld(old_id, graph_id=uow.resolved_graph_id)
        except KeyError:
            return (), f"supersede_target_missing:{old_id}"          # G1
        old_status = str(old_doc.get(memory.STATUS.name, ""))
        old_scope = str(old_doc.get(memory.SCOPE.name, ""))
        old_types = _jsonld_type_names(old_doc)                       # 归一化 @type（见下）
        if old_status != memory.NodeStatus.ACTIVE.value:
            return (), f"supersede_target_not_active:{old_id}:{old_status}"   # G2
        if old_scope != candidate.scope:
            return (), f"supersede_target_scope_mismatch:{old_id}"    # G3
        if not (old_types & _SUPERSEDABLE_TYPES):
            return (), f"supersede_target_type_not_supersedable:{old_id}:{old_types}"  # G0(旧侧)
        valid.append(old_id)
    return tuple(valid), None
```

**`_jsonld_type_names`（任务 2.5 配套，守 G0 旧侧）**：`@type` 在文档里可能是 compact term(`"Preference"` / `["Preference"]`)或 IRI(`"https://pulsara.dev/memory#Preference"`)。必须归一化成 short name 集合,否则 G0 旧侧校验对 IRI 形态会误判:

```python
def _jsonld_type_names(document: Mapping[str, Any]) -> set[str]:
    raw = document.get("@type", ())
    values = raw if isinstance(raw, (list, tuple)) else (raw,)
    names: set[str] = set()
    for value in values:
        if not value:
            continue
        text = str(value)
        # IRI（含 # 或 /）取末段 short name；compact term 原样
        if "#" in text:
            text = text.rsplit("#", 1)[-1]
        elif "/" in text:
            text = text.rsplit("/", 1)[-1]
        names.add(text)
    return names
```

这与 `postgres.py:_canonical_memory_type`([graph/postgres.py:334](src/pulsara_agent/graph/postgres.py:334))判 `@type` 的思路一致;若愿意,可把该逻辑提取为共用 helper,避免两处分别维护。

任一门失败 ⇒ 返回 `downgrade_reason`,调用方降级。注意 G4(dedupe)不在这里——它在序列第 1 步独立做(与既有 UoW 路径一致)。

### 4.3 降级语义：supersede → coexist，记录诚实（守设计 §6 step5）

降级 = **写新节点、不退旧节点**(coexist)。关键:**降级后的决策记录不能再是 `SupersedeAndSubmitDecision`**——否则 `superseded_memory_ids` 仍挂在记录里,审计会误以为退休发生过。降级时把记录改写成等价的非 supersede 决策:

```python
_SUPERSEDE_DOWNGRADE_SENTINEL = "supersede_downgraded_to_coexist"

def _downgrade_to_coexist(decision: "SupersedeAndSubmitDecision", reason: str) -> CorrectAndSubmitDecision:
    """supersede 未实际执行 → 退化为'只写新节点'(coexist)。记录为 CorrectAndSubmitDecision，
    但用稳定 sentinel 前缀标记，便于分析区分'真 correction'与'supersede 借壳降级'。"""
    return CorrectAndSubmitDecision(
        target_entry_id=decision.target_entry_id,
        candidate=decision.candidate,
        reason=f"{_SUPERSEDE_DOWNGRADE_SENTINEL}: {reason}; original: {decision.reason}",
    )
```

#### 审计不变量（钉死，勿再漂）

**规则:supersede-origin 决策一律不追加 governance audit candidate——无论成功还是降级。**

- **为何不追加**:governance audit candidate 的语义是\"governance 合成了一个与池中 pending 不同的 corrected/merged 候选\"。supersede 写的是用户自述偏好(近乎原样,语义接近 `submit_as_is`,后者本就不产 audit candidate);而退休事实已有**更丰富的专属审计轨**——`MemorySupersededEvent` + 新节点上的 `supersedes` 边 + `WriteSucceededOutcome.superseded_memory_ids`。再产一条 governance-origin 候选,只会用一份用户偏好副本污染候选池,且与成功 supersede 不追加形成不对称。
- **成功 supersede**:`_governance_candidate_for_decision(SupersedeAndSubmitDecision)` 本就返回 `None`([executor.py:204](src/pulsara_agent/memory/governance/executor.py:204)),天然不追加。**保持不变,不要给它加分支。**
- **降级 coexist**:记录虽是 `CorrectAndSubmitDecision`,但**必须跳过** audit candidate 追加——否则\"do-nothing 的 coexist\"反而比\"真退休\"多一条审计候选,正是要消除的漂移。实现见 §4.4 步骤 6 的 `is_supersede_origin` 守卫。
- **`CorrectAndSubmitDecision` 是 v1 临时审计载体**:它在这里是\"借壳\"——语义上 supersede-downgrade ≠ 真 correction。`_SUPERSEDE_DOWNGRADE_SENTINEL` 前缀让下游统计/分析能把二者分开,不至于把 supersede 降级误算进 correction 率。**v2 应给 downgrade 一个一等记录形态**(如 `CoexistDecision` 或带 `downgraded_from` 字段),取代借壳。

> **legacy 路径(无 `memory_write_uow_factory`)遇 supersede 决策一律降级为 coexist**:supersede 需原子多节点变更,没有 UoW 无法保证(§1.2)。这是 legacy-only fallback;**生产环境不应触发**——durable wiring 始终带 UoW factory,只有纯 InMemory 轻量运行/单测才走 legacy。legacy 降级同样遵守上面的审计不变量:**不追加 audit candidate**。

legacy path 必须同时做到两件事:**record 降级** 与 **跳过 audit candidate**。不能只让 `_candidate_for_decision` 支持 `SupersedeAndSubmitDecision`,然后继续按原始 decision 写 record;那会把一个没有退休旧节点的动作记成 supersede。实现形态应明确使用 `effective_decision`:

```python
# legacy apply_decision 内,在 candidate 提取前
is_supersede_origin = isinstance(decision, SupersedeAndSubmitDecision)
effective_decision = (
    _downgrade_to_coexist(decision, "legacy_no_uow")
    if is_supersede_origin
    else decision
)
candidate = self._candidate_for_decision(effective_decision)
...
if already_exists(candidate, self.graph, graph_id=self.graph_id):
    ...  # 既有 duplicate skip 分支，不追加 audit candidate
...
outcome = self.memory_write_service.submit(candidate, event_context=governance_batch_context(batch_id))
stored_events = self.event_log.extend(outcome.events)
if not is_supersede_origin:
    governance_candidate = self._governance_candidate_for_decision(
        effective_decision, governance_batch_id=batch_id)
    if governance_candidate is not None:
        self.candidate_pool.append_candidate(governance_candidate)
record = self._append_decision(
    decision=effective_decision,
    governance_batch_id=batch_id,
    write_outcome=_write_outcome(outcome, stored_events),
)
```

要点是:legacy fallback 也必须记录**实际动作**。无 UoW 时实际动作只能是 coexist,所以 record 是带 sentinel 的 `CorrectAndSubmitDecision`,不是原始 `SupersedeAndSubmitDecision`。

### 4.4 UoW supersede 落地序列（任务 2.6，守设计 §6）

【改造】`_apply_decision_with_uow`,在 candidate 取出后、既有 dedupe 分支处,增加 supersede 分支。完整序列(校验先于写入,**实际动作先于记录**):

```python
# 在 with self.memory_write_uow_factory() as uow: 块内
# 1. dedupe（G4，复用既有逻辑）——命中则走既有 duplicate skip，根本不进 supersede
if already_exists(candidate, uow.graph, graph_id=uow.resolved_graph_id):
    ... # 既有 skip 分支，不变

# 2. supersede 预校验（仅当 decision 是 SupersedeAndSubmitDecision）——校验先于写入
valid_old_ids: tuple[str, ...] = ()
supersede_blocked_reason: str | None = None
if isinstance(decision, SupersedeAndSubmitDecision):
    valid_old_ids, supersede_blocked_reason = self._validate_supersede_targets(decision, uow)

# 3. submit 新节点
context = governance_batch_context(governance_batch_id)
outcome = uow.memory_write_service.submit(candidate, event_context=context)  # -> MemoryWriteOutcome
uow.ensure_event_context_rows(context)

# 4. 决定是否【实际】supersede：必须 (a) 是 supersede 决策 (b) 预校验通过
#    (c) 新节点写成功且为 ACTIVE。任一不满足 => 不退休旧节点（coexist）。
new_active_id = _active_memory_id(outcome)          # ACTIVE 的 memory_id，否则 None（见下）
supersede_events: list[AgentEvent] = []
did_supersede = (
    isinstance(decision, SupersedeAndSubmitDecision)
    and supersede_blocked_reason is None
    and valid_old_ids
    and new_active_id is not None
)
if did_supersede:
    for old_id in valid_old_ids:                    # v1 实际最多 1 个
        supersede_events += uow.lifecycle.supersede(
            old_id=old_id, new_id=new_active_id,
            governance_batch_id=governance_batch_id,
            graph_id=uow.resolved_graph_id,
        )

# 5. 计算【实际动作】对应的 record decision —— 只有真正 supersede 了才记 supersede
if isinstance(decision, SupersedeAndSubmitDecision) and not did_supersede:
    effective_decision = _downgrade_to_coexist(decision, supersede_blocked_reason or "write_not_active")
else:
    effective_decision = decision
recorded_superseded_ids = valid_old_ids if did_supersede else ()

# 6. governance audit candidate + decision record（都用 effective_decision / 实际 ids）
#    审计不变量（§4.3）：supersede-origin 决策一律不追加 audit candidate——
#    无论成功(记 SupersedeAndSubmitDecision，helper 本就返 None)还是降级(记 CorrectAndSubmitDecision，
#    必须用 is_supersede_origin 守卫显式跳过，否则借壳的 correct 会误产一条)。
is_supersede_origin = isinstance(decision, SupersedeAndSubmitDecision)
if not is_supersede_origin:
    governance_candidate = self._governance_candidate_for_decision(
        effective_decision, governance_batch_id=governance_batch_id)
    if governance_candidate is not None:
        uow.decisions.append_candidate(governance_candidate)
record = _decision_record(
    decision=effective_decision,
    governance_batch_id=governance_batch_id,
    write_outcome=_write_outcome(outcome, outcome.events, superseded_memory_ids=recorded_superseded_ids),
)
uow.decisions.append_decision(record)
uow.outbox.append_decision(record, graph_id=uow.resolved_graph_id)
# ---- COMMIT (块退出) ----

# 7. 块外 emit：write events + supersede events
stored_events = self.event_log.extend(outcome.events + supersede_events)
```

**`_active_memory_id`（模块 helper）**：`submit` 返回的是 `MemoryWriteOutcome`(`record` + `events`),**不是** `WriteSucceededOutcome`(后者是 `_write_outcome` 之后才从事件转出的 decision-record outcome)。新节点 id 与状态在 `outcome.events` 的 `MemoryWriteResultEvent` 上。且 supersede 的前提不是"写成功",而是"**新节点为 ACTIVE**"——gate 可能把新节点判成 NEEDS_REVIEW/REJECTED(REJECTED 仍落图),那种情况下退休旧 ACTIVE 节点 = 用一个召不回的节点换掉有效记忆,绝不允许:

```python
def _active_memory_id(outcome: MemoryWriteOutcome) -> str | None:
    """新节点写成功且 status==ACTIVE 时返回其 memory_id，否则 None。"""
    result = next(
        (e for e in outcome.events if isinstance(e, MemoryWriteResultEvent)),
        None,
    )
    if result is None:                                   # 写失败（只有 MemoryWriteFailedEvent）
        return None
    if result.status != memory.NodeStatus.ACTIVE:        # NEEDS_REVIEW / REJECTED → 不可作 supersede 锚点
        return None
    return result.memory_id
```

要点:
- **`_active_memory_id` 比"写成功"更严**:它额外要求 `status==ACTIVE`。新节点非 ACTIVE → `did_supersede=False` → 旧节点保持 ACTIVE(coexist),record 降级。这把"写失败不应记成 supersede"和"新节点 REJECTED/NEEDS_REVIEW 仍退休旧节点"两种危险一并堵死。
- **record decision 反映实际动作**:`record.decision` 是 `SupersedeAndSubmitDecision` **当且仅当** `did_supersede==True`(预校验过 + 写成功 + ACTIVE + 真的调了 `lifecycle.supersede`)。其余一切情况(预校验降级 / 写失败 / 非 ACTIVE / 多目标 / 非 Preference)都记为 `CorrectAndSubmitDecision`,`recorded_superseded_ids=()`。审计永远只看到真实发生的事。
- **审计不变量:supersede-origin 不追加 audit candidate(§4.3)**:步骤 6 用 `is_supersede_origin = isinstance(decision, SupersedeAndSubmitDecision)` 守卫——成功(helper 本就返 None)与降级(借壳的 `CorrectAndSubmitDecision` 会误产一条)都跳过。这保证"真退休"与"do-nothing coexist"在候选池里都不留 governance-origin 副本,二者审计表现对称。
- **校验(步骤 2)在 submit(步骤 3)之前**;但"是否真 supersede"的最终判定在 submit 之后(步骤 4),因为它还依赖新节点是否 ACTIVE。两者不矛盾:预校验先挡掉目标侧问题,ACTIVE 判定再挡掉新节点侧问题。
- **`new_id` 来自 `_active_memory_id(outcome)`**;步骤 4 的 `lifecycle.supersede` 内部 `get_jsonld(new_id)` 依赖它已写入(§1.4,同事务可见性 §1.6)。
- **emit 在 commit 后**(块外),含 supersede 事件。崩溃在 commit 前 → 全回滚无 split-brain;commit 后 emit 前 → 状态已一致,事件可补发。

### 4.5 `_write_outcome` 透传（任务 2.7）

【改造】`_write_outcome` 加一个默认空参数,把 superseded ids 填进 `WriteSucceededOutcome`:

```python
def _write_outcome(outcome, events, *, superseded_memory_ids: tuple[str, ...] = ()):
    ...
    if result is not None:
        return WriteSucceededOutcome(
            memory_id=result.memory_id, memory_type=result.memory_type,
            node_status=result.status, confidence_level=result.confidence_level,
            verification_status=result.verification_status, gate_reason=result.gate_reason,
            write_event_ids=event_ids,
            superseded_memory_ids=superseded_memory_ids,   # ← 新增透传
        )
    ...  # WriteFailedOutcome 分支不变
```

默认 `()` 保证既有 4 类决策调用点无需改。

## 5. engine：让 LLM 看得见替代目标

### 5.1 `related_existing_memories`（任务 2.3，守设计 §4 + 硬前置）

【改造】`governance/engine.py`,把 `_existing_memory_matches` **改名为 `_related_existing_memories`**,从「同 statement + 同 scope」放宽到「**ACTIVE + 同 scope + 同 type**」,并按词重叠排序:

```python
def _related_existing_memories(
    candidate: PooledMemoryCandidate,
    graph,
    *,
    graph_id: str | None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not isinstance(candidate.payload, ValidCandidatePayload):
        return []
    memory_candidate = candidate.payload.candidate
    term = _KIND_TO_TERM.get(memory_candidate.kind)
    if term is None:
        return []
    cand_tokens = _overlap_tokens(memory_candidate.statement, candidate.user_quote)
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for record in graph.find_by_type(term, graph_id=graph_id):
        scope = str(record.get(memory.SCOPE.name, ""))
        status = str(record.get(memory.STATUS.name, ""))
        # 放宽：同 scope + 同 type + ACTIVE（不再要求 statement 精确相等）
        if scope != memory_candidate.scope:
            continue
        if status != memory.NodeStatus.ACTIVE.value:
            continue
        statement = str(record.get(memory.STATEMENT.name, ""))
        overlap = len(cand_tokens & _overlap_tokens(statement))
        is_exact_duplicate = _normalize(statement) == _normalize(memory_candidate.statement)
        scored.append((
            overlap,
            str(record.get("@id", "")),       # 稳定 tiebreak
            {
                "memory_id": record.get("@id"),
                "memory_type": memory_candidate.kind,
                "statement": statement,
                "scope": scope,
                "status": status,
                "verification_status": record.get(memory.VERIFICATION_STATUS.name),
                "is_exact_duplicate": is_exact_duplicate,   # ← 标给 LLM：exact dup 只能 skip，绝不 supersede
            },
        ))
    # 词重叠降序 → memory_id 升序（稳定），截断 limit
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[:limit]]
```

配套:
- 在 `_candidate_snapshot` 把 `snapshot["existing_memory_matches"] = _existing_memory_matches(...)`([engine.py:163](src/pulsara_agent/memory/governance/engine.py:163))改为 `snapshot["related_existing_memories"] = _related_existing_memories(...)`。系统提示里的字段名也同步改(§5.2)。
- `_overlap_tokens(*texts)` 是新 helper:Unicode 词拆分 + casefold + 去停用词(可复用 recall 的 tokenizer 思路),返回 `set[str]`。
- **判重不变**:`already_exists`(statement-exact,[governance/dedupe.py:56](src/pulsara_agent/memory/governance/dedupe.py:56))**保持不动**,仍是判重唯一权威。放宽只作用于"喂给 LLM 看的上下文"。
- **保留 exact duplicate 在结果里,但显式标注 `is_exact_duplicate`**:同 scope+type+ACTIVE 的 statement-exact 节点仍会出现在 `related_existing_memories`(它确实"相关"),但必须带 `is_exact_duplicate=True`,让 LLM 知道**这是 dedup-skip 目标,不是 supersede 目标**。executor 的 G4(dedupe,§4.4 步骤 1)本就会先拦掉 exact-duplicate 候选,所以即便 LLM 误把它当 supersede target,也走不到退休——但 prompt 必须明说,避免 LLM 错误学习(§5.2)。
- **关于"排除自身 id"**:v1 这个碰撞**不可能发生**——`related_existing_memories` 在 submit **之前**调用,此时新 candidate **还没有 canonical id**(id 在 ledger submit 时才生成,§1.4)。所以无需做 id-exclusion;真正的风险是 statement-exact-duplicate 被误当替代目标,已由上面的 `is_exact_duplicate` 标注 + G4 dedupe 双重覆盖。v2 引入 governance-origin / derived-id 路径时若出现同 id 可能,再加 id-exclusion 不迟。
- **stopgap 标注**(写进代码注释):词重叠是"同主题"的不完美代理,v2 用结构化 `subject` 字段取代。

### 5.2 系统提示新增 supersede（任务 2.4，守设计 §5.3）

【改造】`_GOVERNANCE_SYSTEM_PROMPT`([engine.py:190](src/pulsara_agent/memory/governance/engine.py:190))。三处改动:

1. **输出 shape 示例**加一行 supersede 例子。
2. **Allowed decision kinds** 加:
   ```text
   - supersede_and_submit: ONLY when the user EXPLICITLY asked to replace/change an
     existing preference (e.g. "change my preference to X", "stop using Y, use Z").
     Provide the new candidate AND superseded_memory_ids (canonical mem ids from
     related_existing_memories). v1: Preference only, single target, same scope.
   ```
3. **Rules** 加:
   ```text
   - supersede_and_submit requires EXPLICIT user replacement intent. Do NOT supersede
     on mere topical similarity — if unsure whether the new memory replaces an old one,
     use submit_as_is/correct (coexist), NOT supersede.
   - superseded_memory_ids MUST come from related_existing_memories (same scope, same
     type). Never invent a memory id.
   - NEVER supersede a related_existing_memories entry whose is_exact_duplicate is true.
     A statement-exact duplicate means the memory already exists — use skip with
     skip_reason duplicate_existing_memory, never supersede.
   - If no related_existing_memories entry is a clear replacement target, do not supersede.
   ```
4. 把提示里旧的字段名 `existing_memory_matches` 改为 `related_existing_memories`(与 §5.1 一致)。

> 注意:LLM 提议 supersede 后,executor 的 G0–G5(§4.2)仍会硬校验并在不合规时降级,且 G4 dedupe 会先拦 exact-duplicate。prompt 约束是第一道("尽量只在该 supersede 时提、绝不 supersede exact-duplicate"),executor 是不可越过的第二道。

## 6. 实现顺序（每步 tests 全绿再下一步）

```text
PR1  decision + outcome 形状（纯类型，零行为）
       3.1 SupersedeAndSubmitDecision 进判别联合
       3.1 facade re-export：memory/__init__.py 的 import + __all__ 补 SupersedeAndSubmitDecision
       3.2 WriteSucceededOutcome += superseded_memory_ids=()
     退出：既有全量测试仍绿（默认空字段不破坏任何路径）；新决策可被 model_validate 解析；
           `from pulsara_agent.memory import SupersedeAndSubmitDecision` 成功

PR2  engine 前置（让 LLM 看得见目标，但还不会真 supersede）
       5.1 _existing_memory_matches → _related_existing_memories（放宽+排序+改名+is_exact_duplicate 标注）
       5.2 系统提示新增 supersede kind/规则（含'绝不 supersede exact-duplicate'）+ 字段改名
     退出：related_existing_memories 返回同 scope+type+ACTIVE 并按词重叠排序、exact-dup 带标注；
           判重(`already_exists`)语义不变；engine 既有测试绿

PR3  executor 校验 + 降级（核心）
       4.1 _SUPERSEDABLE_TYPES / _MAX_SUPERSEDED_PER_DECISION
       4.2 _jsonld_type_names + _validate_supersede_targets（G0–G5，不抛错，返回降级原因）
       4.3 _downgrade_to_coexist（带 _SUPERSEDE_DOWNGRADE_SENTINEL）
       4.5 _write_outcome 透传 superseded_memory_ids
       4.4 _apply_decision_with_uow 加 supersede 分支（_active_memory_id、did_supersede、is_supersede_origin 守卫）
            + legacy 路径遇 supersede 决策一律降级（同样不追加 audit candidate）
     退出：§7 测试矩阵全绿（含审计不变量、ACTIVE 门、IRI type 用例）

PR4  【必做，非可选】engine 真实 LLM 冒烟（gated by PULSARA_RUN_REAL_LLM=1，落 tests/test_real_llm_integration.py）
     supersede 的安全链依赖 LLM 真按教义走（explicit intent / same subject 是模型判断的），
     仅"类型对、事务对"证明不了模型行为。最低必做两类：
       (a) 用户明确改口（"以后用 spaces 不要 tabs"）→ LLM 出 supersede_and_submit，旧 Preference 被退休
       (b) 语义不够强 / 仅陈述新偏好（无替代意图）→ LLM 出 submit_as_is/correct（coexist），旧节点保留
     退出：(a)(b) 在真实 LLM 下稳定通过；未设 PULSARA_RUN_REAL_LLM 时 skip（与既有 real-llm 测试一致）
```

硬依赖:PR1 → PR2/PR3(都依赖新决策类型);PR3 的 4.2 校验必须在 4.4 序列里**先于** submit 调用。PR4 依赖 PR1–PR3 全部落地。

## 7. 测试矩阵（任务 2.8，`tests/test_memory_supersede.py`【新增】）

需要 Postgres 的用例用既有 `_connect_or_skip(dsn)` 模式;纯逻辑用例(降级判定)可用 InMemory。

| 测试 | 断言要点 | 守 |
|---|---|---|
| explicit Preference supersede 原子落地 | UoW 应用 supersede 后:新节点 ACTIVE、旧节点 SUPERSEDED、新节点文档含 `supersedes` 边、**不双 active** | §4.4 / 设计 §3.1 |
| supersede 后旧节点不被召回 | 召回旧节点的 query 中,旧 id 进 `filtered_ids`,projection 只含新节点 | 设计 §2.3 承重 filter |
| outcome 记录退休 id | `record.write_outcome` 是 `WriteSucceededOutcome` 且 `superseded_memory_ids == (old_id,)` | §3.2 |
| 非 Preference 降级 coexist | candidate.kind=Claim 的 supersede 决策 → executor 降级,旧节点不动,record.decision.kind=="correct_and_submit" | G0 / §4.3 |
| 跨 scope 降级 coexist | 新旧 scope 不同 → 降级,旧节点仍 ACTIVE,无 `supersedes` 边 | G3 |
| 目标缺失/非 ACTIVE 降级 | `superseded_memory_ids` 指向不存在或已 SUPERSEDED 节点 → 降级,不抛错 | G1+G2 |
| 多目标降级（v1 单目标） | `len(superseded_memory_ids) > 1` → 降级 | G5 |
| **新节点非 ACTIVE 不退旧（强化项）** | gate 把新 Preference 判成 NEEDS_REVIEW/REJECTED → `_active_memory_id` 返 None → 旧节点保持 ACTIVE、不退休、record 记为 correct_and_submit | §4.4 `_active_memory_id` |
| **写失败不退旧不记 supersede** | submit 只产 `MemoryWriteFailedEvent`(无 result event) → `did_supersede=False`、`write_outcome=write_failed`、旧节点不动 | §4.4 |
| 旧侧 @type 为 IRI 仍正确校验 | 旧节点 `@type` 是 `https://pulsara.dev/memory#Preference` → `_jsonld_type_names` 归一化为 `{"Preference"}`,G0 旧侧通过 | §4.2 `_jsonld_type_names` |
| exact-duplicate 标注 + 永不 supersede | `related_existing_memories` 中 statement-exact 项带 `is_exact_duplicate=True`;即便误传为 supersede target,G4 dedupe 先拦,旧节点不退 | §5.1 |
| 校验先于写入 | 降级路径下,新节点**仍被写入**(coexist),但**无任何** `set_status`/`supersedes` 边被执行 | §4.4 |
| 降级记录诚实 | 降级时 `record.decision` 是 `CorrectAndSubmitDecision`、`write_outcome.superseded_memory_ids == ()` | §4.3 |
| 同事务可见性 | supersede 步骤能 `get_jsonld(new_id)` 读到同 UoW 刚 submit 的新节点 | §1.6 |
| 崩溃原子性 | 在 supersede 后、commit 前注入异常 → 回滚:新节点未现、旧节点仍 ACTIVE、无悬空边 | §4.4 |
| legacy 路径降级 | 无 `memory_write_uow_factory` 时 supersede 决策 → record 降级为带 sentinel 的 `CorrectAndSubmitDecision`,写新节点、不退旧节点,且不追加 governance audit candidate | §4.3 |
| dedupe 命中优先于 supersede | 新候选与已有 ACTIVE 节点 statement-exact 重复 → 走既有 duplicate skip,不进 supersede | G4 / §4.4 |
| related_existing_memories 放宽 | 同 scope+type+ACTIVE、不同 statement 的节点出现在结果里,按词重叠排序 | §5.1 |
| already_exists 不变 | 放宽 related 查询后,判重仍 statement-exact | §5.1 |
| **supersede-origin 不追加 audit candidate（审计不变量）** | 成功 supersede 与降级 coexist 都**不**在候选池留 governance-origin 副本;断言 `list_candidates()` 中 GOVERNANCE-origin 计数不因 supersede 增加 | §4.3 / §4.4 step6 |
| **降级载体可区分** | 降级记录 `reason` 带 `_SUPERSEDE_DOWNGRADE_SENTINEL` 前缀,可与真 correction 区分 | §4.3 |
| **facade 露出（PR1）** | `from pulsara_agent.memory import SupersedeAndSubmitDecision` 成功 | §3.1 |
| **【必做】real-LLM：明确改口 → supersede** | 用户明确改口 → LLM 出 `supersede_and_submit`,旧 Preference 退休（gated PULSARA_RUN_REAL_LLM） | §6 PR4 |
| **【必做】real-LLM：语义不强 → coexist** | 仅陈述新偏好、无替代意图 → LLM 出 submit_as_is/correct,旧节点保留（gated） | §6 PR4 |
| explainer 接真实边 | supersede 后 `memory_explain(old_id)` 返回 grounded `superseded_by`,confab=0 | 设计 §2.3 |

## 8. 文件级改动地图

### 8.1 改造既有文件

```text
src/pulsara_agent/memory/candidates/pool.py
    + class SupersedeAndSubmitDecision；加进 GovernanceDecision 判别联合（3.1）
    + WriteSucceededOutcome.superseded_memory_ids: tuple[str,...] = ()（3.2）
src/pulsara_agent/memory/__init__.py
    + import SupersedeAndSubmitDecision + 加进 __all__（3.1 facade re-export）
src/pulsara_agent/memory/governance/executor.py
    + _SUPERSEDABLE_TYPES / _MAX_SUPERSEDED_PER_DECISION（4.1）
    + _SUPERSEDE_DOWNGRADE_SENTINEL（4.3）
    + _jsonld_type_names（4.2，归一化 @type compact/IRI → short name set）
    + _validate_supersede_targets（4.2）
    + _downgrade_to_coexist（4.3，模块函数，带 sentinel 前缀）
    + _active_memory_id（4.4，从 outcome.events 取 MemoryWriteResultEvent；仅 status==ACTIVE 返 id，否则 None）
    ~ _apply_decision_with_uow 加 supersede 分支（4.4；did_supersede 在 submit 后判定；
        step6 用 is_supersede_origin 守卫跳过 audit candidate——成功与降级都不追加，§4.3 审计不变量）
    ~ legacy apply_decision 遇 supersede 决策降级（4.3；record 降级为带 sentinel 的 CorrectAndSubmitDecision，
        同样不追加 audit candidate）
    ~ _write_outcome += superseded_memory_ids 参数（4.5）
    ~ _candidate_for_decision 加 SupersedeAndSubmitDecision 分支（return decision.candidate）
    （_target_entry_ids 无需改：单数 target_entry_id 命中 fallback，§1.1）
src/pulsara_agent/memory/governance/engine.py
    ~ _existing_memory_matches → _related_existing_memories（放宽+排序+改名+is_exact_duplicate 标注，5.1）
    + _overlap_tokens helper（5.1）
    ~ _candidate_snapshot：字段名 related_existing_memories（5.1）
    ~ _GOVERNANCE_SYSTEM_PROMPT：supersede kind/规则（含'绝不 supersede exact-duplicate'）+ 字段改名（5.2）
tests/test_real_llm_integration.py
    + PR4 两条必做 real-LLM 冒烟：explicit 改口→supersede；语义不强→coexist（gated）
```

### 8.2 新增文件

```text
tests/test_memory_supersede.py    §7 测试矩阵（除两条 real-LLM 冒烟，后者落 test_real_llm_integration.py）
```

### 8.3 明确不动（v1 边界）

```text
canonical/lifecycle.py            supersede 已就绪，直接调用（§1.4），不改
canonical/unit_of_work.py         uow.lifecycle 已暴露（§1.3），不改
governance/dedupe.py              already_exists 保持 statement-exact（§5.1），不改
recall/*                          召回侧不改；supersede 后旧节点经既有 ACTIVE-filter 自然退出
MarkContradictedDecision / contradiction / 多目标 / subject 字段 / maintenance runner   —— 全留 v2
```

## 9. 一句话收束

> v1 supersede = **在既有 UoW governance 路径上加一个决策分支**:新增 `SupersedeAndSubmitDecision`(进判别联合,LLM 输出自动解析)、放宽并改名 `related_existing_memories` 让 LLM 看得见替代目标、executor 用 G0–G5 硬校验(Preference-only + 同 scope + 单目标 + 目标 ACTIVE)、**校验先于 submit**,合规则在同一 UoW 内 write-then-retire(复用已就绪的 `uow.lifecycle.supersede`)、不合规则降级为 coexist 并把记录诚实改写成 `CorrectAndSubmitDecision`。`lifecycle`/`unit_of_work`/`dedupe`/`recall` 全不改;contradiction、类型拓宽、多目标、subject、maintenance 全留 v2。




