# Host Resume / Durable Conversation Contract

_Created: 2026-07-04_

本文档冻结 Pulsara durable conversation resume 的长期契约。它与 [WORKSPACE_TERMINAL_LIFECYCLE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/WORKSPACE_TERMINAL_LIFECYCLE_CONTRACT.zh.md) 互补：后者描述 host/session/terminal ownership，本文件描述关闭进程后如何重新打开同一个 runtime conversation。

相关代码：

- [src/pulsara_agent/host/core.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/core.py)
- [src/pulsara_agent/host/resume.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/resume.py)
- [src/pulsara_agent/host/session_manifest.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session_manifest.py)
- [src/pulsara_agent/host/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/transcript.py)
- [src/pulsara_agent/runtime/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/session.py)
- [tests/test_host_resume.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_host_resume.py)
- [tests/test_cli_host.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_cli_host.py)

---

## 1. 核心立场

Resume 不是恢复旧 Python coroutine。

Resume 的语义是：

```text
same runtime_session_id
+ same durable event log
+ same durable manifest/conversation identity
+ fresh HostSession / RuntimeSession process objects
+ replayed prior messages
```

不会恢复：

- in-process `LoopState`；
- Python tasks；
- old ToolExecutor；
- old terminal manager process handles（workspace supervisor 可按其自身契约保留 shared terminal pool）；
- transient scratchpad。

---

## 2. Canonical truth

Resume 的 canonical truth 是：

- Postgres `agent_events`；
- Postgres runtime projection tables；
- artifact store；
- `sessions.metadata` 中的 manifest；
- typed compaction boundary / summary artifact（若存在且有效）。

任何 resume UI 或 inspector 不得以根目录设计文档、JSONL transcript、stdout buffer 或 transient scratchpad 作为事实源。

---

## 3. Session manifest

V1 manifest 存在 `sessions.metadata`，schema version 为 `resume_schema_version = 1`。

Manifest 必须记录：

- runtime session id；
- conversation id；
- workspace kind；
- workspace root；
- display label；
- memory domain id；
- model role；
- permission mode；
- serialized permission policy；
- lifecycle created_by / created_at / last_active_at / closed_at / archived。

`SessionManifest.resumable` 的语义：

```text
not archived and closed_at is null
```

Manifest 是 query/index 层，不是 transcript 真源。缺失或损坏 manifest 时，不得伪造完整对话历史；必须 fail clearly 或要求用户显式选择可修复路径。

---

## 4. Open / detach / close

`HostCore.start_session()` 创建或打开一个 runtime conversation。新open的durable顺序固定为：required MCP initialization
成功 → upsert open manifest → registry publish。required startup失败不得写manifest。manifest写入后、publish前失败时，
当前reservation必须原子转换为manifest-close tombstone并执行幂等`mark_closed()`；finalization失败保留tombstone，
list/resume必须排除或拒绝该runtime，后续explicit close/shutdown重试。

`HostSession` detach 的语义：

- 不关闭 durable conversation；
- 不标记 manifest closed；
- 允许之后通过 `--resume` / `--continue` 重新打开。

Explicit close 的语义：

- 关闭 HostSession；
- 终结 active/suspended run（若有）；
- 标记 manifest `closed_at`；
- 默认不删除 event log / artifacts。

并发 close intent 必须单调合并：`detach < explicit close`。同一物理 close attempt 中，只要任一仍在参与该 attempt 的 caller 请求 explicit close，最终 manifest 就必须写入 `closed_at`；detach/shutdown caller不能把它降级。attempt owner在registry线性化边界内seal合并后的intent，再执行manifest mutation。若更强intent在线性化seal之后才到达，它必须等待物理close并完成幂等manifest close，不能成功返回却遗漏`closed_at`。

Manifest mutation失败不允许退化为“session已移除，所以后续close是no-op”。物理HostSession关闭后，registry必须保留bounded finalization tombstone，仅含`host_session_id/runtime_session_id/conversation_id/manifest_close_pending`及当前retry attempt；再次explicit close或下一次shutdown必须重试`mark_closed()`。成功后原子删除tombstone，失败则保留并把同一异常交给所有retry waiters。tombstone存在期间禁止复用同一host/conversation identity，也禁止resume对应runtime_session_id。

`list_resumable_sessions(limit=N)`不得先按SQL `limit N`截断再过滤process-local tombstone，否则最新tombstone会遮住
后续正常session。V1至少按`N + tombstone_count`有界over-fetch，过滤后再切回N；未来把exclude IDs下推SQL时必须保持
相同排序与limit语义。

`HostCore.shutdown()` 是 host process teardown，不等价于用户关闭 conversation。它必须按照 workspace terminal lifecycle 契约 finalization active/suspended runs，但不得擅自 archive durable conversation。

---

## 5. Resume open flow

`HostCore.resume_session(runtime_session_id, ...)` 必须：

1. 要求 durable wiring；in-memory runtime 不提供生产 resume。
2. 读取 manifest。
3. 若用户没有显式 workspace override，使用 manifest workspace。
4. 若用户有 workspace override，按 HostCore workspace identity 规则重新解析。
5. 在重放 transcript 前调用 dangling run repair。
6. 用相同 `runtime_session_id` 构造新的 `RuntimeSession`。
7. 用新的 `HostSession` 承载该 runtime conversation。

`--continue` / resume most recent 必须从 manifest store 查询最新 resumable session；没有可恢复 session 时应给用户友好错误，不应泄露 KeyError。

---

## 6. Dangling run repair

如果上一个 host 进程崩溃、被杀或机器重启，Postgres projection 里可能存在 `runs.status='running'` 且没有 `RUN_END` 的 run。

Resume 必须先在`HostSessionRegistry`同一临界区原子取得携带`runtime_session_id`的reservation；live/reserved/tombstoned runtime identity会在此处fail closed，且此时不得修改event ledger/projection。只有reservation成功后、session wiring构造前，才执行 `repair_dangling_runs_for_resume()`：

- 读取该 runtime session 中 running 且没有 RUN_END 的 runs。
- 找到每个 run 最新 event 的 turn/reply context。
- append typed `RunEndEvent(status="aborted", abort_kind="host_teardown")`。
- metadata 写入 `recovered_by="resume"` 与 `resume_stop_reason="resume_recovered_interrupted"`。
- 然后修复 projection rows。

禁止只更新 `runs.status` 而不写 `RUN_END` 事件。事件优先是为了让 transcript/recovery/inspector 都能解释中断。

若repair失败，open transaction必须释放刚取得的reservation且不构造session wiring。禁止在reservation前repair，否则一个最终被pending-close tombstone拒绝的resume也会先污染durable ledger。

---

## 7. Transcript replay

Resume 后重建模型上下文必须走 `rebuild_prior_messages()`。

规则：

- 使用 event log，而不是 manifest metadata。
- 尊重最新有效 context compaction completed boundary。
- 过滤/终结未完成工具调用，避免把 dangling assistant tool call 原样喂给模型。
- 注入 recovery guidance，使模型知道上次 run 被中断而不是继续等待旧 tool result。
- 系统提示、runtime context、capability exposure、active skills 不从旧 summary 恢复；每个新 turn fresh resolve。

---

## 8. Permission / workspace restoration

Resume 必须恢复 manifest 中的 permission mode / permission policy，除非用户在本次 resume 命令中显式 override。

Workspace 规则：

- `project` workspace resume 默认回到 manifest workspace root。
- `transient` workspace resume 默认回到 manifest transient root。
- 显式 `--workspace` override 只改变新 HostSession 的 workspace binding；不得改写历史 event truth。

恢复 permission mode 不得绕过当前 `PERMISSION_POLICY_CONTRACT`，也不得恢复旧版本已经删除的 mode / alias。

---

## 9. Interaction boundaries

Resume 不能恢复旧 pending approval / pending plan interaction / pending MCP input-required 的 in-memory continuation，除非该 pending state 有 durable representation 且当前代码显式支持。

当前 V1 语义：

- dangling active run 先被 repair 为 aborted；
- 新 turn 从 repaired transcript 继续；
- process-local MCP pending lease只在同一live HostSession内支持resume，不跨进程重建；
- 用户需要重新发出需要执行的意图；
- 不自动重放未完成工具调用。

这条边界优先保证 correctness，而不是“自动续跑一半的工具批次”。

### 9.1 Live MCP pending lease

同一live HostSession收到`ToolExecutionSuspended(interaction_kind="mcp_input_required")`时，executor持有的exact slot
lease必须先`promote_lease_to_pending(interaction_id)`，durable suspension event提交确认后再confirm reservation；若
pending event在commit前失败则abort reservation并归还lease；若event已commit、仅ordered publisher/observer失败，
canonical pending fact已经成立，必须confirm reservation并保留pending ownership，不得错误归还lease。Resume通过
interaction id borrow原lease，不得按当前tool name重新
acquire可能已变化的slot。

Safe point required失败按原因分支：

- pending所属binding被disable/remove/reconfigure：提交terminal deny/error tool result后释放lease；
- 无关required server不可用：保留pending state与lease，本次host action失败但可重试；
- 同配置TTL/retry暂时失败且leased binding仍有效：保留pending state与lease，不切换generation。

Resume safe point产生的新installation audit必须在state恢复和model continuation前durable commit；失败保留原pending
state/lease，成功后才清pending audit。该路径不伪造第二条RunStart。

用户cancel、session close、round/deadline cap或最终resume result都必须在对应durable terminal fact提交后调用
`complete_pending_lease()`。若fact已commit但observer publication失败，runtime先从committed slice折叠state并完成
lease，再向上传播publication failure。Close drain失败必须保留HostSession/supervisor ownership供同一close retry，不能删除
session后留下孤儿slot。

---

## 10. 禁止事项

- 不允许把 resume 实现成 JSONL transcript append/replay。
- 不允许恢复旧 coroutine / old LoopState。
- 不允许在没有 durable event log 的生产路径声称支持 resume。
- 不允许直接修改 projection 表来“补齐”中断，而不写 typed `RUN_END`。
- 不允许把 manifest 当成 transcript 真源。
- 不允许 resume 时吞掉有效 compaction boundary。
- 不允许 old pending tool call 在未经 capability/permission gate 的情况下自动执行。

---

## 11. 测试守护

最低测试门槛：

- resume reopens same `runtime_session_id` with a new `host_session_id`。
- resume replays prior messages from durable event log。
- resume restores manifest permission mode when not overridden。
- resume with workspace override uses override only for new binding。
- dangling running run is repaired with typed aborted `RUN_END` before replay。
- repair is idempotent when another process repaired first。
- `--continue` chooses most recent resumable session。
- no resumable session returns friendly CLI error。
- closed/archived sessions are excluded by default.
- resumed turn still resolves fresh capability exposure and permission gate.
