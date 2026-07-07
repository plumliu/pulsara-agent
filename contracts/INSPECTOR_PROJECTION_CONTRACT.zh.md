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
- canonical event count / start/end sequence / current user input；
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
