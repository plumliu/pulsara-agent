# Inspector / Runtime Projection Contract

_Created: 2026-07-04_

本文档定义 Pulsara Inspector 的长期契约。Inspector 是 deterministic read-only projection，不是 live debugger，不是修复器。

相关代码：

- [src/pulsara_agent/inspector/service.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/inspector/service.py)
- [src/pulsara_agent/inspector/store.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/inspector/store.py)
- [src/pulsara_agent/inspector/diagnostics.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/inspector/diagnostics.py)
- [src/pulsara_agent/runtime/timeline.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/timeline.py)
- [tests/test_inspector.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_inspector.py)
- [tests/test_runtime_timeline.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_runtime_timeline.py)

---

## 1. 核心立场

Inspector 只读 Postgres / Oxigraph / artifact store / event log。

它不得：

- 查询 live `LoopState`；
- 依赖 live `HostSession`；
- 修复数据；
- 触发 worker；
- 执行 tool；
- 调用 LLM。

Inspector 的输出必须能从 durable state 决定性重建。

---

## 2. Inspect kinds

V1 Inspector 支持：

- session；
- run；
- artifact；
- memory；
- health。

不存在的 session/run/artifact/memory 必须返回明确 not found（代码层 `KeyError`，CLI 层转成用户可读错误），不得成功返回空报告。

---

## 3. Session inspection

Session inspection 必须展示：

- session row；
- runs summary；
- event counts；
- current working context summaries；
- capability surface as seen；
- context input manifest/replay status as seen；
- compaction windows；
- event summaries；
- diagnostics。

事件默认限量展示；`include_payload` 才展示完整 payload。

---

## 4. Run inspection

Run inspection 必须展示：

- session/run rows；
- canonical event count / start/end sequence / current user input / run permission snapshot；
- business timeline；
- compaction boundary as seen；
- prior messages as seen；
- projections as seen；
- capability surface as seen；
- assistant replies；
- tool result artifacts；
- recall traces；
- outbox rows；
- diagnostics。

Context compile projection必须区分full `input_audit`与pre-manifest `input_failure`，展示stable `failure_stage`、reason code、
已经形成的component fingerprints、manifest acknowledgement（若已尝试）及outer context/call/index join状态。不得把
“没有ContextCompiledEvent”当成普通compile failure；该情况只允许由ledger untrusted/latch解释并显示为reconciliation blocker。
Render cache hit/miss属于本次process operational fact，不得伪装成historical semantic truth。

Compiled section projection必须保留`metadata.timing`中的compiled time、source timing、freshness、age与sequence范围，并区分
structured timing metadata和实际model-visible `timing_header_text`。Inspector不得从section正文反向搜索时间字符串。
Candidate authority join结果至少展示source、content fingerprint、event/artifact attribution与channel/lowering；join失败属于
input contract failure，不得按普通section omission解释。Process-local cache read/write failure、LRU entry/chars/eviction只放在
runtime diagnostics，不回填历史compile fact。

Candidate source timing必须来自authority本身并能回指source event wrapper；memory/subagent的observed time/sequence分别对应
ProjectionReady/SubagentRunCompleted，而不是current-user observation。Runtime diagnostics可展示cache read/write error与
oversized-skip count，但同一compile的historical candidate-set/manifest不得因cache可用性不同而变化。

`prior_messages_as_seen` 必须用同一条 `rebuild_prior_messages()` 路径重建，不能另写 transcript reducer。

Run permission snapshot 必须来自该 run 的 `RunStartEvent`，展示 `permission_snapshot_id`、`permission_mode`、`permission_policy`、`permission_snapshot_source`；Inspector 不得从 live HostSession default 或 session manifest 反推历史 run 权限。

### 4.1 MCP installation attribution

Session inspection必须从durable`McpCapabilitySnapshotInstalledEvent`投影`mcp_installations`，包含installation chain、
config epoch、event-safe fingerprint、mixed triggers、coalesced count、per-server attempt/status/timing/request/page/cache/
stale summary、total/added/revoked tool counts与bounded diagnostics。完整server-controlled catalog不在event中；V1
`catalog_artifact_id`固定为`null`，Inspector不得补写或查询live catalog。

Run inspection必须从`RunStartEvent.mcp_installation_id`与
`mcp_installation_owner_runtime_session_id`定位audit。Parent owner是当前session；child run可指向parent runtime
session，Inspector通过durable store/EventLog locator跨session join。Canonical empty installation显示
`status="canonical_empty"`；非空引用找不到audit时显示`status="missing"`并产生
`mcp_installation_audit_missing` error diagnostic。

Historical projection不得读取`McpServerSupervisor`、live manager、当前config或当前时间，不得触发network、refresh、
retry、artifact write或任何repair。

### 4.2 Context input replay attribution

每个`ContextCompiledEvent`必须投影`input_audit`或`input_failure`二选一。存在confirmed manifest时，Inspector从artifact、
named event ranges与durable descriptor/tool-result semantics重建snapshot、transcript、tool-result units、prepared candidates，
并报告`exact_replay|fact_replay_only|artifact_missing|contract_mismatch|ledger_untrusted`五种typed status；
不得保留`partial`、`missing_artifact`或`untrusted_slice`兼容别名。

Inspector不得读取live `LoopState`、scratchpad、当前capability exposure、当前capture policy或当前tool-result JSON来补造历史
input。Manifest缺失或fingerprint不一致必须显示diagnostic，不能退回旧message renderer。当前进程的builder build fingerprint只
能作为诊断值显示，不得伪装成historical durable fact。

`input_replay.candidates`必须bounded投影`source_selections`与`collection_decisions`，而不只展示最终entry count。
Inspector必须明确区分：

- `no_eligible_sources`：eligible=0、selected/omitted均为空；
- `policy_limit`：eligible=N、selected可为空、omitted>0；cap=0时即`selected=(), omitted=N`。

两组列表分别带truncated标志；不得以空authority/entry伪造selection，也不得把上述两种状态折叠成同一个`count=0`。

---

## 5. Runtime timeline

`build_run_timeline()` 是 run business timeline 的投影入口。

Timeline item kinds 包括：

- reply；
- model call；
- assistant text；
- assistant thinking；
- tool call；
- tool result；
- permission request；
- plan mode；
- plan question；
- plan exit request；
- error。

Timeline 只总结事件，不替代 canonical event list。

---

## 6. Capability projection

Inspector 必须从 typed runtime events 投影 capability facts：

- `capability_exposure_resolved` -> exposures/latest exposure；
- `CapabilityGateDecisionEvent` -> gate decisions。

`CustomEvent(name="capability_gate_decision")` 不再是新 run 的 canonical fact，也不是 inspector 的兼容读取要求；Capability gate decision 已硬切为 typed event。

不得从当前 `CapabilityRuntime` 重新 resolve 来解释历史，因为 provider/snapshot/skills 可能已经变化。

---

## 7. Compaction projection

Inspector 必须展示每个 completed compaction window：

- phase / safe point；
- current run id；
- max compactable sequence；
- tail message count；
- summary artifact id；
- summary artifact present；
- through / keep-after sequence；
- token estimates；
- included run/artifact ids。

Started without completed/failed 是 warning。Completed boundary 引用 missing summary artifact 是 error。

### 7.1 Typed audit、MCP lifecycle 与 candidate projection

Session/run inspection必须从 typed events投影：

- `mcp_input_required_lifecycle`：suspension chain、resolution attempts、resume failures、
  terminal source、closure reason、RunEnd closure join与 reopen action；
- `context_compaction_requests`；
- `mid_turn_compaction_skips`；
- `tool_result_evidence_projection_failures`；
- `mandatory_runtime_audit_reconciliation`；
- `compaction_candidate_projection_durable_status`。

MCP投影必须复用 production `McpInputRequiredLifecycleStore`，不能另写 reducer。Compaction
candidate historical status只从 exact proposed event、durable outbox row与 candidate-pool
join得到。`preparation_failed`、`owner_installation_failed`、`owner_installed` 和
`candidate_frozen` 等 process-local phase不属于 historical Inspector；无 durable
producer/outbox authority时必须显示 `not_durably_observable`，不得从 event absence猜测。
Host live diagnostics可以显示当前 owner receipt，但不能写回 historical report。

---

## 8. Diagnostics

Inspector diagnostics 是 pure functions。

必须覆盖：

- sequence gap；
- stale run projection；
- orphan tool call；
- late tool result；
- missing artifact ref；
- outbox failed/pending；
- missing required tables；
- missing compaction summary artifact。

Diagnostics 不能修复状态；修复必须由明确 repair API 执行。

---

## 9. Health inspection

Health inspection 必须检查：

- verified PostgreSQL migration head、durable registry prefix、fast executable schema fingerprint、PostgreSQL/pgvector版本与last verification time；
- required Postgres tables；
- recent session sequence gaps；
- compaction diagnostics；
- run projection stale count；
- tool result index missing artifact count；
- outbox status counts；
- Oxigraph configured/connected state。

Oxigraph failure 只影响 health report，不得让 Postgres health 信息丢失。

Inspector必须借用verify-only service签发的connection provider。Schema health不得显示admin DSN、runtime DSN、password、host authority参数或其他credential；它只显示secret-safe database/schema identity。Inspector不得执行migration、grant repair或runtime schema bootstrap。

---

## 10. 禁止事项

- 不允许 Inspector 调用 LLM。
- 不允许 Inspector 执行 tool 或 worker。
- 不允许 Inspector 从 live runtime scratchpad 解释历史。
- 不允许 nonexistent memory 返回成功空报告。
- 不允许 capability history 用当前 provider snapshot 重新推断。
- 不允许 diagnostics 产生写入副作用。

---

## 11. 测试守护

最低测试门槛：

- inspect session reports events/runs/capability/compaction diagnostics。
- inspect run reports timeline/prior messages/tool artifacts/recall/outbox。
- inspect artifact reports payload preview and tool refs。
- inspect memory not found raises not found。
- health reports missing table / stale run / missing artifact / outbox diagnostics。
- timeline serializes/deserializes stable dicts。
- capability projection comes from typed event-log facts。
- compaction missing artifact diagnostic appears。
- diagnostics are deterministic and read-only.
- MCP session projection只来自bounded installed events；child run通过owner runtime session跨ledger join。
- missing MCP installation audit产生稳定diagnostic，canonical empty不产生误报。
- historical MCP inspect不读取live manager、不触发network或补写catalog artifact。
- typed MCP lifecycle与 runtime使用同一 reducer；candidate projection没有 durable
  authority时显示 `not_durably_observable`。

---

## 12. Run boundary / continuation / child entry projection

Run Inspector 必须从 durable facts直接展示：

- Host `run_boundary`：boundary/source high-water、typed current-user id/chars/hash/timing、permission/target/MCP、
  execution surface、catalog/active projection、exposure semantic/fact fingerprint与preflight compaction join；
- `continuation_boundaries[]`：interaction identity、source/effective exposure、reuse/narrow transition、MCP audit IDs与sequence；
- child `child_run_entry`：nullable task id、render policy、current-user artifact、child terminal reference与deterministic handoff；
- host workflow plan exit 单独作为 workflow mutation，不伪造 run。

默认 Inspector 不返回 `current_user_message.text`；只显示 id/chars/hash/timing。历史 exposure 不从 live runtime、当前
provider或当前时间重算。cross-event source/effective exposure、compaction boundary或child terminal reference缺失时显示稳定
`contract_error` diagnostic。

Host live summary 可额外显示 process observation（preparing/committed、active segment generation、handle retirement、
pending compaction terminalization），但这些字段不得冒充 durable historical projection。

---

## 13. Subagent graph checkpoint projection

Session/run Inspector 必须从 typed `SubagentGraphCheckpointCommittedEvent` 投影 bounded checkpoint catalog，至少展示：

- confirmed checkpoint count 与 truncation；
- checkpoint/materialization event/artifact identity；
- through sequence、reducer ID/version/contract fingerprint；
- graph event count 与 graph-state semantic fingerprint。

Context input exact replay projection 必须分开：

- manifest 冻结的 `SubagentGraphSemanticSourceFact`；
- preferred checkpoint ID；
- 本次 replay 实际使用的 checkpoint ID；
- `rebased`、checkpoint through sequence、delta range/count/bytes 与 ledger high-water。

Replay status 仍只有 `exact_replay | fact_replay_only | artifact_missing | contract_mismatch | ledger_untrusted`。原 artifact 缺失但
compatible rebase 恢复出同一 semantic source/payload 时仍是 `exact_replay`，只在 acceleration 中标记 `rebased=true`。Inspector 不得
从 current runtime graph/cache 猜测历史 source。

---

## 14. Same-run context window projection

Session/run Inspector 必须从 durable `ContextWindowOpenedEvent`、`ContextWindowClosedEvent` 与
`ContextWindowCompactionStartedEvent|CompletedEvent|FailedEvent` 投影：

- `context_windows[]`：window/generation/previous/open reason、open/close event identity与sequence、semantic/fact fingerprint、source
  compaction/summary、active或closed状态及next window；
- `context_window_compactions[]`：compaction/attempt、Started/terminal identity与sequence、source/target window generation、plan/call/
  settlement identity、summary artifact existence、actual/target token measurement与failure stage/reason；
- `diagnostics[]`：Started无terminal、同一compaction多个terminal、completed summary artifact缺失。

Inspector只检查durable artifact存在性，不重新运行summarizer、不重新估算summary或post-compaction token，也不从当前window cache推断历史状态。
Completed窗口切换必须表现为旧window closed、新window active；Failed或recovered-interrupted只终结attempt，不能伪造旧window已关闭。

---

## 15. Memory governance evidence projection

Session Inspector新增 bounded `memory_governance` projection：

- `batches[]`：batch input reference、source high-water、resolved call、
  `staged | prepared | terminal`状态与Prepared/terminal event refs；
- `claims[]`：candidate、batch、generation、`preparing | prepared | terminal | released`、
  previous/current fingerprint与carrier refs；
- `evidence_rejections[]`：system-owned rejection reason、candidate/generation、event ID；
- `candidate_projection_outbox[]`：reflection/compaction producer identity、candidate index、
  payload/attribution fingerprint、pending/applied/failed状态；
- counts：batch数、open claims、evidence rejections、pending candidate projections。

Historical Inspector只读取 durable artifact/events/rows，不运行 evidence builder、relatedness、
governance model、candidate dispatcher或recovery owner。它不得从当前 transcript reducer、segment
layout、live Cursor或current model config重建一份看似存在的 governance input。

Batch artifact preview必须 bounded，并分别展示 evidence semantic fingerprint、physical
attribution refs和prompt projection fingerprint；不得把三者折叠成一个 fingerprint。Artifact
missing、hash conflict、open claim无Prepared、terminal batch仍有open claim、projection outbox
永久failed应产生稳定 diagnostic，但 Inspector不得自行修复或终结候选。

---

## 16. Provider input causal projection

ProviderInput Inspector必须从完整session ledger重建generation lifecycle，再按run引用筛选，不能只用当前run的
events猜测generation。每个generation至少展示：scope/epoch、revision、unit-vector root、prefix fingerprint、
transcript frontier、source heads、authority-horizon root、open/closed状态、ModelStart joins与typed rollover reason。

每次model call必须并列展示三种不同视图：

- canonical transcript：stable message/tool-result/pair/compaction replacement事实；
- context frames：非transcript source fragments及preceding/following placement proof；
- linear provider wire view：adapter实际收到的ordered immutable fragments。

Inspector不得把ContextSource frame伪装成user/assistant trajectory，也不得按`current_user | prior_history`重新排
canonical transcript。它应显示causal predecessor、projection-local ordinal、tool result leaf + pair leaf + terminal
projection三方join、pending continuation exact join，以及相邻revision的strict-prefix验证结果。

只有generation start/root、每次append proof、committed frontier、ModelStart reference、manifest nested projection和
hydrated vector全部一致时才报告`exact_replay`。缺少generation start、artifact、causal validation、frame range或
continuation proof时输出稳定diagnostic；不得从live compiler/cache补造缺失事实。Attribution-only drift可显示为
manifest-local audit，但不得表现为durable append、rollover或provider semantic变化。

Provider-input projection还必须展示唯一root privileged identity、每次append中的human/runtime-request/runtime-observation internal owner与user-wire kind、kind/producer/codec contract、
runtime-observation no-op count、replacement semantic head及hydration/placement状态。Source semantic head与event/artifact/horizon attribution必须分栏，不能折成一个fingerprint。

Inspector还必须展示每个historical replacement source的compile-time disposition及reason，区分
`retain/projection_failed`、semantic no-op、explicit empty、terminal与typed allocation rewrite。
Source candidate缺席不得显示为“removed”。One-shot generation必须显示其initial runtime clock和
retry-stable operation owner。

Long-Horizon rollover展示observation rewrite的source active/protected/eligible counts、stable-state/partition/coverage/projection roots、parent authority与resulting effective heads。Provider reported
cached input只作为operation observation展示，并同时显示generation/revision/prefix和rollover reason；不能把cache usage冒充authority或用它修正历史projection。

---

## 17. Terminal monitor projection

Inspector从session ledger exact重建每个monitor的registration、policy、双cursor、observation ordinal、progress limiter、lifecycle state、pending observation、termination和delivery disposition；同时展示notification account余额、process heads、reservation acquire/release与Host run ingress/admission proof。`pending_count`由heads重算，autonomous eligibility按当前selection policy展示为观察结果，不作为durable head字段。

UI `x.pulsara/terminal_monitor_event`是bounded operational stream，不是authority。它提供stream reconnect cursor、retained replay和显式gap；slow/detached subscriber只能丢自己的窗口，不得阻塞journal、monitor writer或模型delivery。Inspector不得从UI stream补造durable observation，也不得在spool range缺失时显示伪造output delta。
