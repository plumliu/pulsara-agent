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

- required Postgres tables；
- recent session sequence gaps；
- compaction diagnostics；
- run projection stale count；
- tool result index missing artifact count；
- outbox status counts；
- Oxigraph configured/connected state。

Oxigraph failure 只影响 health report，不得让 Postgres health 信息丢失。

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
- capability projection comes from event log custom events。
- compaction missing artifact diagnostic appears。
- diagnostics are deterministic and read-only.
- MCP session projection只来自bounded installed events；child run通过owner runtime session跨ledger join。
- missing MCP installation audit产生稳定diagnostic，canonical empty不产生误报。
- historical MCP inspect不读取live manager、不触发network或补写catalog artifact。

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
