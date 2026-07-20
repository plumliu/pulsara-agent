# Artifact Store / Tool Result Artifact Index Contract

_Created: 2026-07-04_

本文档定义 Pulsara artifact payload store 与 tool-result artifact index 的长期契约。它与 [TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md) 互补：terminal 契约说明 preview/artifact/completion 三层语义，本文件说明 artifact payload 如何持久化、归属、读取与索引。

相关代码：

- [src/pulsara_agent/memory/foundation/protocols.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/foundation/protocols.py)
- [src/pulsara_agent/memory/artifacts/archive.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/artifacts/archive.py)
- [src/pulsara_agent/memory/artifacts/postgres_archive.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/artifacts/postgres_archive.py)
- [src/pulsara_agent/runtime/tool_artifacts.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/tool_artifacts.py)
- [src/pulsara_agent/tools/builtins/artifact.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/artifact.py)
- [src/pulsara_agent/storage/postgres_schema.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/postgres_schema.py)
- [tests/test_artifact_store_contract.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_artifact_store_contract.py)
- [tests/test_tools.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_tools.py)

---

## 1. 核心立场

Artifact store 是 retained payload authority。

它保存：

- tool result 完整文本/二进制 payload；
- context compaction summary artifact；
- run timeline JSON artifact；
- 未来其它 runtime-retained payload。

它不保存：

- semantic memory truth；
- event log truth；
- permission decision truth；
- capability exposure truth。

Artifact id 是稳定引用键；artifact body 不应默认注入模型上下文，需要通过 preview / artifact ref / `artifact_read` 显式读取。

---

## 2. ArtifactStore protocol

`ArtifactStore` 必须提供：

- `put_text(...)`
- `put_bytes(...)`
- `get_info(...)`
- `read_text(...)`
- `get_text(...)`
- `get_bytes(...)`

`put_text` / `put_bytes` 返回：

- artifact id；
- digest；
- stored_at；
- size_bytes。

`get_info` 返回 metadata，不返回 body。

`read_text` 是 bounded text slice：

- `offset_chars >= 0`；
- `max_chars >= 1`；
- offset/max 按 Python 字符计数；
- 返回 total chars / returned chars / has_more。

---

## 3. Identity / idempotency

同一个 artifact id 重复写入必须满足 identity-stable：

- content 相同；
- digest 相同；
- size 相同；
- media type 相同；
- owner compatible。

若同 id 不同 content / media type / owner，必须 raise，而不是覆盖。

并发写同 id 同 content 必须 idempotent；并发写同 id 不同 content 必须最多一个成功。

Postgres 实现必须使用 advisory transaction lock 或等价机制线性化同一 artifact id 写入。

---

## 4. Ownership

Artifact 可以是全局 unowned，也可以绑定 runtime session / run。

规则：

- `run_id` 非空时必须同时提供 `session_id`。
- Postgres store 写 owned artifact 时，`session_id` 必须存在。
- `run_id` 必须存在，且属于给定 `session_id`。
- 已存在 artifact 的 owner 不得被另一个 session/run 复用。
- `get_info/read_text/get_text/get_bytes(..., session_id=X)` 对不属于 X 的 artifact 必须表现为 not found。

Cross-session hiding 是安全边界：`artifact_read` 不应暴露“存在但不属于当前 session”的区别。

---

## 5. Text / binary boundary

Text artifact：

- 写入 `text_body`；
- `get_text` / `read_text` 可用；
- `get_bytes` 必须拒绝。

Binary artifact：

- 写入 `binary_body`；
- `get_bytes` 可用；
- `get_text` / `read_text` 必须拒绝。

`artifacts` table 必须保证 text/binary 至少有一个 body。media type 是调用方提供的事实，不应由 read path 猜测。

---

## 6. Tool result artifact index

Artifact payload store 只知道 artifact body 与 owner；它不回答“这个 artifact 来自哪个 tool call”。

Tool result artifact provenance 必须写入 `tool_result_artifacts` index。

Index record 必须包含：

- session id；
- run id；
- turn id；
- reply id；
- tool call id；
- tool name；
- artifact id；
- role；
- ordinal；
- media type；
- size bytes；
- stored_complete；
- loss_reason；
- metadata。

唯一性：

```text
(run_id, tool_call_id, role, ordinal)
```

同一个 artifact id 可被按 session 查询；跨 session 不可见。

---

## 7. ToolResultArtifactService

`ToolResultArtifactService` 是 tool-result artifact 的归档/索引入口。

规则：

- 根据 descriptor artifact mode 判断是否归档；
- 按 bytes 判断 archive threshold；
- 对 text payload 构造 adaptive preview；
- 先写 artifact payload；
- 获得 final artifact id 后构造 `ToolResultArtifactRef.preview`；
- 同一份 final preview 写入 ref 与 index metadata；
- primary text artifact 才挂 primary preview；
- `artifact_read` 自身不得被再次归档成 tool-result artifact。

`ToolResultArtifactService` 可以重写模型可见 output 为 preview，但不得丢失完整 body。

---

## 8. `artifact_read` tool

`artifact_read` 是模型显式读取 retained artifact 的工具。

规则：

- 默认按当前 runtime session 限定 owner；
- missing 或 cross-session artifact 都返回 not found；
- text mode 拒绝 binary artifact；
- info mode 可返回 metadata/size/digest，不返回 full body；
- `offset_chars` / `max_chars` 控制 text slice；
- `max_chars` 不应绕过 context budget；返回结果仍受 tool-result preview/artifact pipeline 约束。

---

## 9. Production vs test store

Production wiring 必须使用 `PostgresArtifactStore`。

`InMemoryArchiveStore` 只允许：

- unit tests；
- in-memory compatibility fixtures；
- local support helpers。

它必须遵守 protocol 的 identity/session hiding/text-binary 边界，但不提供 production durability。

---

## 10. 禁止事项

- 不允许同 artifact id 覆盖不同 body。
- 不允许 `run_id` 无 `session_id` 写 artifact。
- 不允许跨 session `artifact_read` 泄漏存在性。
- 不允许 ledger / transcript / completion event 自行写完整 output 侧路。
- 不允许把 artifact body 当成 event log truth。
- 不允许 `artifact_read` 结果再次强制归档成新的 artifact 形成无限链。
- 不允许 preview metadata 只存在于 transient renderer，不进入 durable ref/index metadata。

---

## 11. 测试守护

最低测试门槛：

- PostgresArtifactStore put/get text。
- same id same content idempotent。
- same id different content rejected。
- reload store 后 artifact durable。
- missing session owner rejected。
- run without session rejected。
- run owned by another session rejected。
- same id owner conflict rejected。
- concurrent same content idempotent。
- concurrent different content rejects one writer。
- run timeline persistence can use PostgresArtifactStore and stores session/run owner。
- `artifact_read` hides cross-session artifacts as not found。
- binary artifact rejected in text mode。
- tool result artifact service writes primary preview ref and index metadata consistently。
- old artifact refs without preview still replay/inspect/compact.

## 12. Deterministic semantic idempotency

cross-ledger repair等需要预生成artifact ID的生产路径必须调用
`put_text_if_absent_or_confirm_identical()`。只有同 ID、相同bytes、media type、ownership和完整semantic metadata都一致时
才视为幂等成功；metadata-only差异也是`ArtifactContentConflict`。PostgreSQL并发writer必须在同一事务/锁边界确认，不能
先查后写产生TOCTOU。child inferred result的normal与repair路径必须共享同一policy fingerprint、artifact ID、正文和
semantic metadata builder。

---

## 13. Subagent graph checkpoint artifacts

Checkpoint artifact 使用稳定 ID、canonical bytes、media type
`application/vnd.pulsara.subagent-graph-checkpoint+json` 和完整 semantic metadata identity。同 ID/同 bytes/同 metadata 幂等；
body、media type、owner 或 metadata-only 差异均是 `ArtifactContentConflict`。

Checkpoint artifact 是可丢弃 cache。Historical context manifest 不永久 pin 其原 checkpoint；只要其他 compatible checkpoint + bounded
delta 能恢复相同 semantic source，exact replay 可以 rebase。Physical GC 不删 checkpoint events，且只能在 session
closed/quiescent、持有 checkpoint maintenance exclusive advisory lock 时，使用 artifact ID/digest/media type/semantic metadata fingerprint
条件删除。Exact replay/Inspector 读 checkpoint event + artifact 时必须持有同锁域 shared lease。

---

## 14. Provider-input semantic documents与observation rewrite artifacts

Provider source-head semantic core只保存content digest、canonical wire hash/bytes与semantic document identity。Inline hydration正文或content-addressed artifact locator属于attribution；
hydrator必须验证document contract、SHA、bytes、wire hash与semantic materialization fingerprint后才能形成joined head。Artifact missing、hash/codec conflict或vector placement drift是
`authority_untrusted`，不能当作source absent或cache miss，也不能重新调用当前ContextSource renderer猜测历史正文。

Runtime-observation stable/partition/projection pages使用稳定ID与canonical bytes。所有pages/root在引用它们的rollover batch之前必须FULL confirmed；同ID不同body/metadata冲突。
Successor rewrite FULL前旧stable-state、proof和projection artifacts持续pinned，FULL reducer fold后才切换reachable roots。Event payload只携带bounded roots/counts/accumulators，禁止
内嵌O(history) member tuple。
