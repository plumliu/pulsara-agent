# Memory Surfaces 写回边界契约

_Created: 2026-06-27_

这份文档定义 Pulsara memory surfaces 的长期契约。它不是 implementation plan(实现指导见 `ARCHITECTURE_DEBT_AUDIT.zh.md` 第六大章),而是当前和后续实现必须遵守的硬协议:memory 不是一个单点系统,而是几个**职责不同、并存**的 surface;它们绝不能被压成一层,但每个 surface 的回写边界必须被写死。

这份契约要回答的不是「memory 有哪些功能」,而是对每个 surface 更硬的三件事:

1. **真值来源(truth source)** —— 这个 surface 的数据最终从哪来。
2. **允许写到哪里(allowed write targets)** —— 它能写进哪些存储。
3. **绝对不能回写到哪里(forbidden write-back)** —— 它绝不能写进哪些存储。

核心立场:

- **canonical graph 是唯一语义真源,唯一写入口是 governance。** canonical 写只走 `MemoryWriteService.submit` ← `MemoryGovernanceExecutor.apply_decision`。任何其它 surface 都不得直接写 canonical graph。
- **投影类 surface 永不进 governed graph。** working_context / recall projection 以 fenced block 注入 prompt,带 `do_not_write_back="true"` 标记;它们是投影,不是记忆。
- **只有 reflection 能把本轮内容升级成「新 memory candidate」。** 而 candidate ≠ canonical;candidate→canonical 仍只归 governance。
- **run timeline 是唯一的 run 业务视图。** 不得再长第二套独立演化的 summary 语义。
- **recall 的 backend-unavailable degraded-mode 是结构化事实,不是自由文本退路。** 不再用自然语言把模型导回旧检索路径。
- **Postgres 是唯一的检索 substrate;in-memory 仅作纯逻辑单测的显式 test double,永不作运行时模式、永不背 recall / governance relatedness。** 见 §9。

相关代码:

- [src/pulsara_agent/memory/working_context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/working_context.py)
- [src/pulsara_agent/memory/recall/service.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/service.py)
- [src/pulsara_agent/memory/recall/projection.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/projection.py)
- [src/pulsara_agent/memory/recall/projection_ledger.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/projection_ledger.py)
- [src/pulsara_agent/memory/recall/trace.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/trace.py)
- [src/pulsara_agent/memory/reflection/engine.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/reflection/engine.py)
- [src/pulsara_agent/memory/hooks/durable.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/durable.py)
- [src/pulsara_agent/memory/hooks/run_timeline_persistence.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/run_timeline_persistence.py)
- [src/pulsara_agent/memory/foundation/run_timeline_query.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/foundation/run_timeline_query.py)
- [src/pulsara_agent/runtime/timeline.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/timeline.py)
- [src/pulsara_agent/memory/canonical/write_service.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/write_service.py)
- [src/pulsara_agent/memory/governance/executor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/executor.py)
- [src/pulsara_agent/tools/builtins/memory_query.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/memory_query.py)
- [tests/test_recall_v1.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_recall_v1.py)

---

## 0. surface 全景:四个真 surface + 一个 degraded 通路

memory 由四个职责不同的 surface 组成,外加一个 recall 在 backend 不可用时的 degraded 通路:

| surface | 真值来源 | 允许写 | 绝对不能回写 |
|---|---|---|---|
| working_context | run timeline summary | `working_context_summaries` | candidate pool / canonical graph / event log |
| recall | canonical graph / query | `recall_traces` / `recall_usages` | new memory(canonical) |
| reflection | 本轮 trace + safe-point | candidate pool(origin=REFLECTION)+ reflection 事件 | canonical graph |
| run timeline | event log 投影 | artifact archive + graph `RunTimelineRecord` | event log / candidate pool / working_context |
| recall degraded(§5) | —(backend 不可用) | —(只读结构化结果) | 不得用自由文本把模型导回旧检索路径 |

唯一写 canonical graph 的是 **governance**(见 §6)。上表里没有任何一个 surface 直接写 canonical graph。

---

## 1. working_context(operational cache)

**定位**:最近活动摘要,是 operational cache,**不是** canonical semantic memory。

- **真值来源**:run timeline summary。由 `build_run_timeline(event_store.iter(run_id=...))` + `summarize_run_timeline(...)` 推出,见 [durable.py:164-197](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/durable.py:164)。它消费的是 run timeline(§4)的派生摘要,不是 canonical memory。
- **允许写**:只能写 `working_context_summaries` 表,唯一写入口是 `PostgresWorkingContextStore.upsert`,见 [working_context.py:79](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/working_context.py:79)。一个 memory domain 一行(`ON CONFLICT (memory_domain_id) DO UPDATE`)。
- **绝对不能回写**:candidate pool、canonical graph、event log。
- **守护**:注入 prompt 的投影块固定带 `do_not_write_back="true"` + `projection_kind="working_context"`,见 [working_context.py:170-183](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/working_context.py:170)。模块 docstring 明确「it never enters the governed memory graph」。

working_context 是 cache:它可以被重算、可以过期(TTL),丢了不影响 canonical 真源。这是它和 canonical memory 的根本区别。

---

## 2. recall(canonical 检索注入)

**定位**:从 canonical graph 检索已有记忆并注入当前轮,只读。

- **真值来源**:canonical graph / query。`LexicalMemoryRecallService` 通过 `MemoryQuery` 的 `lexical_candidates` / `fts_candidates` / `fetch_nodes` 读 `memory_nodes` / `memory_relations` / `memory_search_index`,全部 `SELECT`,见 [service.py:124-219](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/service.py:124)。recall 自身不产生新事实,只重排和投影既有 canonical 节点。
- **允许写**:只能写 usage/trace —— `recall_traces`(整次 recall 的 query/candidate/included/filtered/warnings/latency)与 `recall_usages`(哪些 memory 被 injected / selected),唯一写入口是 `PostgresRecallTraceStore.record`,见 [trace.py:80-136](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/trace.py:80)。`recall_usages` 仅在 `included_ids` 非空时写。
- **绝对不能回写**:recalled content **不得**被回写成 new memory。已被召回的内容若再被当作「新发现」写回 candidate pool,会形成记忆自我增殖的回声(echo)。
- **守护(双重)**:
  1. `ProjectionLedger.record` 在 recall 投影成功后,把本轮 surfaced 的 `memory_id` + snippet 指纹记进 `state.scratchpad`,见 [projection_ledger.py:17-21](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/projection_ledger.py:17);`is_echo` 用精确/子串匹配判回声,见 [projection_ledger.py:23-37](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/projection_ledger.py:23)。`DurableMemoryHooks._is_projection_echo` 在 `_append_to_pool` 处把命中回声的候选直接 skip。
  2. recall 投影块带 `do_not_write_back="true"`,见 [projection.py:15,35](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/projection.py:15)。

---

## 3. reflection(候选提案)

**定位**:在 safe-point 上,把本轮内容评估为可能值得长期记住的「新 memory candidate」。

- **真值来源**:当前 run 的 user/assistant/tool trace,加上 safe-point 触发与 cheap hints。safe-point 是 `on_session_end`,触发判定在 `ReflectiveMemoryHooks._trigger_reasons`,见 [durable.py:234-285](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/durable.py:234);reflection 输入由本轮事件 trace 组装,见 [engine.py:186-219](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/reflection/engine.py:186)。
- **允许写**:
  - candidate pool —— `memory_candidates` 表,origin 固定为 `CandidateOrigin.REFLECTION`,写入口 `candidate_pool.append_candidate`,见 [engine.py:223-237](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/reflection/engine.py:223)。
  - reflection 事件 —— `MemoryReflectionCompletedEvent` / `MemoryReflectionFailedEvent`(只作为事件返回,见 [engine.py:240-268](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/reflection/engine.py:240))。
- **绝对不能回写**:canonical graph。**只有 reflection 能把本轮内容升级成「新 memory candidate」;但 candidate ≠ canonical**,candidate → canonical 仍只归 governance(§6)。
- **守护**:`reflection/engine.py` 不 import、不持有 `MemoryWriteService`,结构上无法直接写 canonical graph。它只能 append 到 candidate pool。

candidate pool 是「待治理的提案箱」,不是记忆本身。reflection 把内容放进箱子,放进箱子不等于记住。

---

## 4. run timeline(唯一 run 业务视图)

**定位**:从 event log 装配出的**唯一** run 业务视图(「这一轮发生了什么」:reply / model call / tool call / tool result / permission / error)。当前唯一消费者是 memory(working_context);未来若有 UI / 诊断要展示 run 业务视图,必须从这里取,不得另起炉灶。

> 边界澄清:`HostSession.summary()`([session.py:375](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py:375))**不是** run 业务视图,而是 **session 生命周期元数据**(`active_run_id` / `pending_approval` / `plan` / `terminal` 等),与 run timeline 正交。它不读 run timeline,也不该被要求读 —— 两者是不同的问题域,不要混为「host inspect 从 timeline 读摘要」。

- **真值来源**:event log 的业务投影。`build_run_timeline` 从 `AgentEvent` 序列重建出层级结构,见 [timeline.py:125](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/timeline.py:125)。event log 是权威事实,timeline 是它的业务投影。
- **允许写**:
  - artifact archive —— timeline 序列化后的 blob(`artifact_kind="run_timeline"`)。
  - graph `RunTimelineRecord` node —— 指向该 blob 的元数据节点。
  - 两者唯一写入口 `RunTimelinePersistenceHook`,见 [run_timeline_persistence.py:37-59](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/run_timeline_persistence.py:37),在 `REPLY_END` / `RUN_ERROR` / `EXCEED_MAX_ITERS` / `RunEnd` 上触发。
- **绝对不能回写**:event log(timeline 只读派生,绝不反向产生事件)、candidate pool、working_context(单向依赖:working_context 读 timeline,timeline 不读 working_context)。

**冻结(本契约把 audit §6.8 收为硬约束)**:

1. **唯一装配函数**:run 业务视图只能由 `build_run_timeline` 装配、`summarize_run_timeline` 摘要(见 [run_timeline_query.py:77-120](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/foundation/run_timeline_query.py:77))。**不得**再写第二个从 `AgentEvent` 导出 run-level status / item_count / tool trace / assistant summary 的并行实现。
2. **working_context 是 canonical 消费者**:它经 `build_run_timeline → summarize_run_timeline → propose_working_context_update` 取摘要(见 [durable.py:164-197](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/durable.py:164)),**不自起一套事件扫描总结逻辑**。
3. **读侧与持久化天然收敛**:持久化(hook)与读侧摘要(working_context)都调同一个 `build_run_timeline`,因此事件分类口径变化时两侧同步演进,不会漂移成两套语义。working_context 当前从 event log 重算 timeline(自包含,无 read-after-write 依赖),而非依赖 persisted artifact 先落库 —— 这是有意选择,**不要**为了「省一次重算」把它改成依赖持久化时序。
4. **与 Chapter 5(Host/session ownership)解耦**:`RunTimelinePersistenceHook` 在 runtime 组合根注册([wiring.py:386-401](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/wiring.py:386)),纯事件驱动,不经 HostSession / RuntimeSession 所有权或 terminal manager。关闭 host session 不影响 timeline 持久化。故 timeline 收口与 session 生命周期债务互不纠缠,可独立演进。

**carve-out(以下不算「第二套 summary 语义」,允许存在)**:从 event log 直接迭代但**不产出 run 业务视图**的路径不受本冻结约束 —— reflection 抽 memory 候选(`reflection/engine.py`)、governance 校验候选来源(`governance/engine.py`)、transcript 重放对话消息供 LLM 上下文(`host/transcript.py` 的 `rebuild_prior_messages`)。它们各自是不同投影,不是 run 摘要的竞品。

---

## 5. recall degraded mode(backend unavailable)

当 recall backend 不可用(cooldown 或 backend 抛异常)时,`memory_search` **只**返回结构化事实:

```json
{
  "status": "unavailable",
  "reason": "recall_backend_unavailable",
  "warnings": ["recall_backend_cooldown"],
  "can_retry": false
}
```

硬约束:

- **不得**带 `fallback: "history_search_or_current_files"` 之类的自由文本退路字段。
- **不得**带把模型导回旧检索路径的自然语言 guidance(例如 "Use current tools or history search if the answer needs verification.")。即使 payload 不含 `fallback` 字段,自由文本 guidance 仍会在语用上把模型导向旧路径,因此一并删除。
- `warnings` 保留结构化诊断码:cooldown 期 `("recall_backend_cooldown",)`;backend 异常 `(f"recall_backend_unavailable:{type(exc).__name__}: {exc}",)`。来源见 [service.py:82-114](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/service.py:82)。
- `can_retry` 固定 `false`:cooldown / backend 故障不是简单重试能立刻解决的,告诉上层别空转重试。

**empty ≠ unavailable**:backend 正常但无命中时返回 `status="empty"` + `results=[]` + `guidance`(来自 `_empty_guidance()`,见 [service.py:403-408](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/service.py:403))。empty 的 guidance **保留**,因为它不是 fallback,而是「没有 canonical memory 结果」的结果解释(换词重查 / 用 history search 查逐字 / 用当前工具查现状)。这与 unavailable 是两种完全不同的 payload 形状。

**口径对齐**:`memory_get` / `memory_related` / `memory_explain` 的 `_unavailable_payload` 已经是 `{status, reason, error, can_retry}` 形状、无 fallback(见 [memory_query.py:351-357](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/memory_query.py:351))。`memory_search` 收敛到同一 `{status, reason, can_retry}` 骨架。唯一差异在诊断字段,且是**有意区分**:

- `memory_search` 走 recall service,产出结构化 `warnings`(可能含 cooldown 态)。
- id 类工具走直连 query,产出原始 `error` 串。

骨架一致,诊断字段随来源不同 —— 这是契约允许的,不是漂移。

---

## 6. canonical write 的唯一归属:governance

canonical graph 是唯一语义真源。它的写入口**只有一个**:

- `MemoryGovernanceExecutor.apply_decision` 是治理决策的唯一入口,见 [executor.py:63-136](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/executor.py:63)。
- 它通过 `MemoryWriteService.submit` 派发到 ledger,由 ledger 的 `submit_*` 调 `graph.put_jsonld` 落 canonical 节点,见 [write_service.py:56-99](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/write_service.py:56)。
- governance 自己产生的修正候选(supersede / contradict)以 origin=`GOVERNANCE` 回 candidate pool,并被 `list_pending` 过滤掉,不会形成治理回环。

三层写边界因此是:**reflection / main-agent 提候选 → governance 决策并执行 → canonical graph 落库**。前两层都不能跳过 governance 直达 canonical graph。

### 6.1 related_existing_memories:advisory-only,非 subject 真值(audit §6.6)

governance 做 `supersede_and_submit` / `contradict_and_submit` 时,会拿到一个 `related_existing_memories` 列表作为参考。本契约把它的语义和边界**冻结**为:

- **它是什么**:从 **live graph** 取的 advisory 候选集 —— `_related_existing_memories` 用 `graph.find_by_type` 读同 scope、`status=ACTIVE`、同 type 的现有 memory,按 **token overlap** 排序,截断 ≤10 条,并标 `is_exact_duplicate`,见 [engine.py:419-463](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/engine.py:419)。
- **它不是什么**:它**不是结构化 subject 真值**。token overlap 只说明「字面像」,不说明「语义同 subject」("concise summaries" vs "concise commit messages" overlap 高但非同 subject;"hates egg tarts" vs "likes dan tat" 是同 subject 却 overlap 低)。
- **为什么 advisory 安全**:precision 有**两道硬背板**,recall 才是这个输入要负责的。①Flash 必须显式选 target(`superseded_memory_ids` / `contradicted_memory_ids`),prompt 硬规则「必须来自 related_existing_memories,不得臆造 id」,且 supersede 要求显式用户替换意图;②executor 对每个 target 复核 scope / ACTIVE / 可 supersede 类型,见 [executor.py:383-443](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/executor.py:383)。因此 token overlap 的 false positive 会被吸收;它真正的局限是 **recall hole**(同义/改述召不回)。
- **冻结约束**:`related_existing_memories` **只能是 advisory 输入,绝不能机械决定 target**。target 选择权归 Flash,合法性归 executor。

**为什么不用 recall 栈的 FTS/lexical/RRF 复用(留档,防再提)**:
- `memory_search_index`(FTS 读的表)**不在 canonical 写路径上同步** —— 它是 reconcile/离线投影(`MemorySearchIndexSync.rebuild`),相对同 batch / 同 session 的 governance 写是**陈旧**的;而现行 token overlap 读 live graph,反而更新。
- FTS 用 `'simple'` 配置(无 stemming / 无同义),见 [query.py:192](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/query.py:192),相对 token overlap **几乎无召回增益**。
- `lexical_candidates` / `fts_candidates` 仅 `PostgresMemoryQuery` 有;governance 核心单测跑 `InMemoryGraphStore`(`memory_query=None`),复用它会让 in-memory governance 直接失去 relatedness 并破测。

**真正的结构化解法(deferred,非本轮)**:结构化 subject key(typed candidate/entity 上加 `subject` 字段 + ontology 谓词,remember 时由模型产出)或语义 embedding(pgvector / subject-alias graph)。两者才能真正补 recall hole,但都有 schema / 依赖成本,留到 subject 漂移被实测证明会咬人时再做。`engine.py:445` 的 "v1 stopgap" 注释保留为指针。

---

## 7. 禁止事项

- 任何非-governance surface 不得直接写 canonical graph。
- working_context 不得写 candidate pool / canonical graph / event log;它只能写 `working_context_summaries`。
- recalled content 不得被回写成 new memory;`ProjectionLedger` echo guard 必须在。
- reflection 不得直接写 canonical graph;它只能 append candidate pool(origin=REFLECTION)+ emit reflection 事件。
- 不得在 run timeline 之外再写第二个从 `AgentEvent` 导出 run 业务视图(status / item_count / tool trace / assistant summary)的并行实现;需要 run 业务视图的消费方一律走 `build_run_timeline` / `summarize_run_timeline`。`HostSession.summary()` 是 session 生命周期元数据,不在此列。
- run timeline 不得反向写 event log。
- `memory_search` 在 backend unavailable 时不得用自由文本把模型导回 history search / current files;payload 里不得有 `fallback` 自由文本路径,也不得有把模型导回旧路径的 guidance。
- 不得把 working_context 当成 durable memory 来引用或回写。
- `related_existing_memories` 只能是 advisory 输入,不得机械决定 supersede / contradict 的 target;target 选择归 Flash、合法性复核归 executor。不得把 token overlap 当作 subject 真值,也不得用陈旧的 `memory_search_index` FTS 替代 live-graph relatedness。
- in-memory store 不得作为运行时模式存在,不得背 recall / governance relatedness;它只能作纯逻辑单测的 test double(见 §9)。

---

## 8. 测试守护

这份契约由以下测试守住(`tests/test_recall_v1.py` 为主):

- `test_recall_backend_unavailable_enters_short_cooldown` —— UNAVAILABLE 的 status / warnings / cooldown 自愈语义(只断言 status/warnings,不依赖 guidance)。
- `test_projection_echo_valid_candidate_is_not_written_back_to_pool` —— recalled content 命中 echo guard 后不进 candidate pool。
- 新增:`memory_search` 在 backend unavailable 时,payload key 集合恰为 `{"status", "reason", "warnings", "can_retry"}`;`reason == "recall_backend_unavailable"`;`can_retry is False`;`"fallback"` 与 `"guidance"` 均不在 payload 中。
- `test_related_existing_memories_returns_active_same_scope_type_ranked_and_marks_duplicates`(`tests/test_memory_governance_engine.py`)—— relatedness 取 active / 同 scope / 同 type、按 overlap 排序、标 `is_exact_duplicate`。守住 §6.1 的 advisory 形状(本轮未改其行为,仅冻结语义)。

回归口径:`memory_search` 的 OK / EMPTY 分支不变(EMPTY 仍带 `_empty_guidance()`),id 类工具的 `_unavailable_payload` 形状不变。

---

## 9. substrate 立场:Postgres 唯一,in-memory 仅 test double

### 9.1 立场

> **Postgres 是唯一的检索 substrate。in-memory store 只作纯逻辑单测的显式 test double —— 永不作运行时模式,永不背 recall / governance relatedness。**

理由是被语义召回这个方向**反向逼出来的**:一旦 memory 要做语义召回(embedding + reranker,FTS / 向量都只在 Postgres),一个**做不了语义召回**的 in-memory 运行时就不再是「同一个产品少了持久化」,而是「一个做不了记忆本职的退化产品」。让两个不等价的 substrate 并存,只会让"这条 surface 到底能不能召回"变成一个跟着 substrate 走的隐藏分支 —— 这正是 §6.6 / §6.7 反复咬到的那类漂移。

### 9.2 这条线买到的清晰度

- `MemoryQuery` 变必选(去掉 `| None`),消灭一整类 None 分支。
- 召回只有一套语义(lexical + FTS + 向量 + rerank),不再有"in-memory 的 lexical-only 降级模式"。
- §6.1 的 token-overlap stopgap 可以**真删**,不只是冻结 —— governance 直接用统一检索原语(乃至向量)算 relatedness。
- 本契约里所有"Postgres-only / 降级"的条件分支从规格中消失。**规格里条件分支越少,漂移落点越少。**

### 9.3 三条不能混的边界(立场的精确范围)

1. **砍 co-equal substrate ≠ 砍 test double。** 纯逻辑单测(timeline 投影、recovery 分类、candidate schema 校验、plan reducer、host FSM、governance 推理)不碰检索语义,用快速、无依赖的 in-memory store 是对的。强行把它们全拖上 Postgres 是**测试基建税,不是语义清晰度收益**。
2. **砍 in-memory 实现 ≠ 砍 protocol 抽象。** `GraphStore` / `EventLog` / `MemoryQuery` 作为 protocol 必须保留(未来换向量库 / SQL 引擎靠它)。**留缝,杀掉撒谎的双胞胎** —— 收益来自删掉那个有损的第二实现,不是删接口。
3. **它修不了 live-vs-projection 分裂。** 即便只剩 Postgres,仍有 live graph(`memory_nodes`)vs reconcile-滞后索引(`memory_search_index` / 向量)这条裂缝。**单一 substrate ≠ 单一一致性模型**;那条得靠 outbox 同步 + governance 即时 embed 候选去管(见关于 embedding 的设计讨论),不在本立场覆盖内。

### 9.4 当前状态 vs 目标态(诚实标注)

本节是**契约目标态**,不是当前代码现状。今天 `HostCore.durable=False` 仍是产品默认、`build_in_memory_runtime_wiring` 仍是一个产品入口、in-memory 仍背着 governance relatedness(`memory_query=None` 时走 token overlap)。

代码侧收敛(CLI/Host 默认 Postgres、`build_in_memory_runtime_wiring` 退出产品入口、in-memory 降为 test-only)留作**一个独立、可在 Postgres 下验证的改动**,最自然的落地时机是真做 embedding + reranker 那一步 —— 那一步才真正让 in-memory 运行时不自洽。本契约先把方向钉死,避免在那之前再长出依赖 in-memory 作产品 substrate 的新代码。

