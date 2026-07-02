# Conversation Resume V1 设计与调研

本文讨论 Pulsara 的“延续对话 / resume”能力：用户关闭 CLI、GUI 或整个宿主进程后，之后仍能重新打开同一条对话并继续说话，类似 Claude Code 的 `--resume` / `/resume`，以及 Codex、网页端 Chat 产品中的 thread continuation。

核心判断：

> Pulsara 已经有比 Claude Code JSONL 更强的事件事实层，所以不应该复制 JSONL transcript 模式。Pulsara 应该走 Codex-like “thread id + event log replay + live writer reopen”的路，但吸收 Claude Code 的恢复卫生：过滤/终结未完成工具调用，恢复工作状态，明确 detach / close。

换言之，V1 不是“恢复 REPL 历史”，也不是“把上一轮 prompt 拼回去”。V1 应该把 `runtime_session_id` 作为 durable thread id，重新打开同一个 durable runtime session，在新的进程内创建新的 HostSession / terminal lease，并继续向同一条 Postgres event log 追加新的 run。

---

## 1. 产品语义

### 1.1 目标体验

用户期望的是：

- 关闭 REPL、GUI 或整个 Pulsara 进程后，可以重新打开并继续某条对话；
- 当前 workspace 下可以选择“继续最近一条对话”；
- 可以按 session id 精确恢复；
- 历史上下文来自真实运行记录，而不是临时内存；
- 如果上次进程在运行中断，下一次恢复时系统要诚实说明“上次 run 未完成”，而不是假装完成或把非法 tool call 序列发给模型；
- 关闭 UI 不应该默认销毁对话。

### 1.2 V1 推荐语义

建议定义四种不同动作：

| 动作 | 语义 | V1 是否做 |
| --- | --- | --- |
| `resume <runtime_session_id>` | 继续同一个 durable runtime session，新进程、新 host session、新 terminal owner | 做 |
| `continue` | 在当前 workspace / memory domain 选择最近可恢复 session | 做 |
| `detach` | UI/CLI 离开，不终结对话 | 做 |
| `close` | 明确关闭对话，不再默认恢复 | 做 |
| `fork` | 复制历史到新 runtime session，形成分支 | 暂缓 |
| `resume-at` | 从某个 message/run 截断恢复 | 暂缓 |
| 恢复 pending approval / pending plan coroutine | 恢复同一个 suspended LoopState | 暂缓 |

V1 的最重要边界：恢复的是 conversation / runtime history，不恢复上个进程内的 coroutine、terminal process、worker task。

---

## 2. 成熟产品调研

### 2.1 Codex：thread id + local thread store + live writer reopen

Codex 的公开 SDK 语义很直接：

- `startThread()` 创建新 thread；
- `resumeThread(id)` 根据 thread id 恢复已有 thread；
- thread 会跨进程持久化在 `~/.codex/sessions`。

关键代码：

- `/Users/plumliu/Desktop/python_workspace/codex/sdk/typescript/src/codex.ts`
  - `Codex.startThread()`
  - `Codex.resumeThread(id)`
- `/Users/plumliu/Desktop/python_workspace/codex/sdk/typescript/src/thread.ts`
  - `Thread` 持有 `_id`；
  - 首轮事件 `thread.started` 返回 thread id；
  - 后续 `run()` / `runStreamed()` 继续传同一个 thread id。
- `/Users/plumliu/Desktop/python_workspace/codex/sdk/typescript/src/exec.ts`
  - 如果存在 `threadId`，CLI 参数变成 `resume <threadId>`。
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/thread-store/src/local/mod.rs`
  - `LocalThreadStore` 是 filesystem / SQLite backed store；
  - live appends 写 canonical JSONL history；
  - SQLite 是 queryable metadata index。
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/thread-store/src/local/live_writer.rs`
  - `resume_thread()` 先读已有 thread rollout path；
  - 用 `RolloutRecorderParams::resume(rollout_path)` 重开 recorder；
  - 之后 append 会继续写同一条 rollout history。

Codex 的重点不是“恢复一个 UI 对象”，而是把 thread id 映射到 durable rollout，然后为当前进程重新打开一个 live writer。这个模型非常适合 Pulsara，因为 Pulsara 已经有 Postgres event log，天然比 JSONL rollout 更结构化。

### 2.2 Claude Code：JSONL transcript + 恢复卫生 + 状态水合

Claude Code 的 resume 语义比 Codex 更复杂，因为它是 TUI，本地 transcript 以 JSONL 为主。

主要入口：

- `/Users/plumliu/Desktop/python_workspace/claude-code/src/main.tsx`
  - `-c, --continue`：继续当前目录最近会话；
  - `-r, --resume [value]`：按 session id 或搜索项恢复；
  - `--fork-session`：恢复时创建新 session id；
  - `--resume-session-at <message id>`：print 模式下截断恢复。
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/commands/resume/index.ts`
  - 注册 `/resume`，alias `/continue`。

存储与选择：

- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/sessionStorage.ts`
  - transcript 路径大致是 `~/.claude/projects/<project>/<sessionId>.jsonl`；
  - 通过 `uuid / parentUuid` 链重建主 conversation；
  - 需要处理 sidechain、并行 tool_use、旧格式迁移和 metadata。
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/conversationRecovery.ts`
  - `loadConversationForResume()` 统一处理最近会话、指定 id、已加载 log、任意 `.jsonl` 路径；
  - 会做反序列化、过滤未完成 tool use、恢复 skill state、追加 session start hooks。
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/sessionRestore.ts`
  - `processResumedConversation()` 恢复 session id、worktree、agent setting、mode、cost、todo、file history、attribution、context collapse 等状态。

Claude Code 值得 Pulsara 吸收的不是 JSONL 存储，而是恢复卫生：

- 未配对的 assistant `tool_use` 不能直接重放给模型；
- 孤立 thinking / 空 assistant / 损坏 permission mode 要过滤；
- 如果上次中断在 user prompt 或 tool result 后，需要识别 interrupted turn；
- 恢复时不仅要有 messages，还要恢复相关工作状态；
- 关闭 UI 与关闭 session 是两个概念。

### 2.3 对 Pulsara 的启发

Codex 给 Pulsara 的启发：

- thread id 是 durable identity；
- resume 是重开 writer，不是复制历史；
- 持久事实层与活跃进程对象分离。

Claude Code 给 Pulsara 的启发：

- resume 需要显式处理不完整 turn；
- transcript / message replay 需要保证 API 合法；
- session metadata、workspace、permission、plan、todo、worktree 等状态不能只存在内存里；
- `/resume` 需要有选择器、搜索和“最近一条”逻辑。

Pulsara 的路线应该是二者结合：事件事实层采用 Codex-like 模型，恢复卫生采用 Claude Code-like 模型。

---

## 3. Pulsara 当前已有基础

### 3.1 Durable runtime truth 已经存在

Pulsara 当前 Postgres runtime truth 已经包含：

- `sessions`
- `runs`
- `turns`
- `agent_events`
- `artifacts`
- `tool_result_artifacts`
- `working_context_summaries`

关键代码：

- `src/pulsara_agent/storage/postgres_schema.py`
- `src/pulsara_agent/event_log/postgres.py`

`PostgresEventLog` 已经支持：

- 为同一个 `runtime_session_id` 追加事件；
- canonical sequence；
- session-level replay；
- `repair_run_projection()` 从 canonical event 重建 `runs` summary。

这意味着 Pulsara 不需要另造 JSONL transcript。`agent_events` 就是更强的 transcript。

### 3.2 Wiring 已经允许指定 runtime_session_id

`build_durable_runtime_wiring()` 已经支持传入 `runtime_session_id`：

- `src/pulsara_agent/runtime/wiring.py`

当前逻辑：

- 不传则生成新 `runtime:<uuid>`；
- 传入则构造同一个 `PostgresEventLog(runtime_session_id=...)`；
- `RuntimeSession` 使用该 id；
- graph id 默认会从 runtime session 派生，或由 memory domain 提供。

这就是 resume 最关键的低层能力：重新构造 RuntimeSession 并继续写同一个 durable event log。

### 3.3 Host transcript replay 已经存在

`HostSession.run_turn()` 会在新 turn 前调用 `_prior_messages()`，而 `_prior_messages()` 基于 `rebuild_prior_messages(event_log)`：

- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/host/transcript.py`

`rebuild_prior_messages()` 已经能：

- 从 `RunStartEvent` 还原 user message；
- 从 `ReplyEndEvent` replay assistant message；
- 对 failed / aborted terminal runs 插入 recovery note；
- 对 terminal failed/aborted run strip unfinished tool calls，避免非法 tool ordering。

这对 resume 很有利：只要新的 HostSession 绑定到旧 `runtime_session_id`，下一轮自然会看到旧 conversation。

### 3.4 Plan state 已经有 event replay

`HostSession.__post_init__()` 会调用 `reduce_plan_workflow_state(event_log.iter())`：

- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/runtime/plan.py`

这说明 plan mode 的“是否 active / 最近 accepted plan”已经部分具备跨进程恢复基础。

### 3.5 Inspector 已经提供 durable 读模型

当前新增的 inspector API 已经能只读查看：

- session；
- run；
- artifact；
- memory；
- health。

关键代码：

- `src/pulsara_agent/inspector/store.py`
- `src/pulsara_agent/inspector/service.py`
- `src/pulsara_agent/cli.py`

这套 store 很适合作为 resume V1 的“查询最近 session / 查 session metadata / 诊断恢复状态”的基础。

### 3.6 Recovery note 基础已存在

失败/中断恢复相关逻辑已经在：

- `src/pulsara_agent/runtime/recovery.py`
- `src/pulsara_agent/host/transcript.py`

当前能力可用于把上次失败/中断 run 变成对模型可见的 system note。

---

## 4. 当前缺口

### 4.1 缺少 resume composition root

当前只有：

- `HostCore.open_session(...)`：创建新 host session 和新 runtime session；
- `build_durable_runtime_wiring(..., runtime_session_id=...)`：底层支持旧 runtime id；
- 但没有 `HostCore.resume_session(...)` 把两者接起来。

需要新增一个显式 composition root：

```python
async def resume_session(
    self,
    runtime_session_id: str,
    *,
    workspace_input: HostWorkspaceInput | None = None,
    conversation_id: str | None = None,
    host_session_id: str | None = None,
    model_role: ModelRole = ModelRole.PRO,
    options: LLMOptions | None = None,
    system_prompt: str | None = None,
    permission_policy: EffectivePermissionPolicy | None = None,
) -> HostSession:
    ...
```

### 4.2 关闭 REPL 现在等于关闭 HostCore

当前 `pulsara host repl` 在 `finally` 中调用：

```python
await core.shutdown()
```

路径：

- `src/pulsara_agent/cli.py`

`core.shutdown()` 会关闭所有 host sessions；`HostSession.aclose()` 会终结 active / suspended run；terminal lease 也会释放。

这意味着现在 CLI 生命周期语义是：

> 一个 REPL 进程 = 一个 HostCore 生命周期 = 一个 HostSession 生命周期。

而 resume 需要的新语义是：

> 一个 REPL / GUI 进程只是观察和驱动某个 durable conversation；退出 UI 默认 detach，不默认 close conversation。

这不是小改文案，而是产品语义调整。

### 4.3 session metadata 不够完整

Postgres `sessions` 目前有：

- `id`
- `workspace_root`
- `created_at`
- `metadata`

但 `metadata` 还没有形成稳定契约。Resume 需要至少保存：

```json
{
  "conversation_id": "conversation:...",
  "workspace_kind": "project",
  "workspace_root": "...",
  "display_label": "...",
  "memory_domain_id": "u_local",
  "model_role": "pro",
  "permission_mode": "bypass-permissions",
  "created_by": "host_repl",
  "last_active_at": "...",
  "closed_at": null,
  "archived": false,
  "resume_schema_version": 1
}
```

否则 `--continue` 很难知道“当前 workspace 最近哪条 session 才是可恢复对话”，`--resume` 也无法完整还原 workspace / permission / model role。

### 4.4 pending interaction 仍是内存态

当前 pending approval / pending plan interaction 依赖 `LoopState` 和 `HostSession._suspended_state`：

- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/runtime/approval.py`
- `src/pulsara_agent/runtime/plan.py`

进程退出后 coroutine 和 suspended state 不存在。V1 不应该承诺恢复同一个 pending coroutine。

V1 应该将 dangling / suspended run 视为 interrupted，并在恢复时终结或投影为 recovery note。

### 4.5 terminal process 不能自然恢复

当前 terminal session manager 是进程内资源，HostCore 通过 workspace supervisor 管理：

- `src/pulsara_agent/host/supervisor.py`
- `src/pulsara_agent/runtime/terminal/*`

进程退出后，terminal manager 不存在。即使 OS process 还活着，也没有可靠 registry 和 owner binding。

因此 V1 只能恢复 conversation，不恢复 live terminal process。跨进程 terminal supervisor 是后续独立能力。

### 4.6 dangling run 需要显式处理

如果进程崩溃在 run 中间，Postgres 可能存在：

- `runs.status = 'running'`；
- 有 `RUN_START`；
- 没有 `RUN_END`；
- 可能有 `ToolCallStartEvent` 但没有 `ToolResultEndEvent`；
- 可能有部分 assistant deltas 但没有 `ReplyEndEvent`。

如果不处理，下一轮 `rebuild_prior_messages()` 可能看不到完整 recovery note，也可能漏掉“上次未完成”的事实。

V1 需要在 resume 时做 repair / projection。

---

## 5. V1 设计

### 5.1 Durable identity

使用 `runtime_session_id` 作为 Pulsara 的 durable conversation id。

原因：

- 它已经是 Postgres `sessions.id`；
- 它是 `agent_events.session_id` 的外键；
- 它是 recall trace、artifact、timeline 等运行事实的共同 owner；
- 它跨进程稳定。

`host_session_id` 不应作为 resume id。它是某个 HostCore 进程内的 owner / terminal principal，重启后必须新建。

`conversation_id` 可以作为用户可见 alias / GUI thread id，但 V1 仍以 `runtime_session_id` 为 canonical。

### 5.2 Session metadata contract

V1 建议将 `sessions.metadata` 规范化为 lightweight manifest：

```json
{
  "resume_schema_version": 1,
  "conversation_id": "conversation:...",
  "workspace": {
    "workspace_kind": "project",
    "workspace_root": "/abs/path",
    "display_label": "pulsara_agent",
    "memory_domain_id": "u_local"
  },
  "runtime": {
    "model_role": "pro",
    "permission_mode": "bypass-permissions"
  },
  "lifecycle": {
    "closed_at": null,
    "archived": false,
    "last_active_at": "..."
  }
}
```

写入时机：

- `open_session()` 创建新 runtime session 后写 manifest；
- 每次 `run_turn()` / resume 后更新 `last_active_at`；
- `:close` / `close_session(close_conversation=True)` 写 `closed_at`；
- `archive` 后续再做。

V1 可以不新增表，但应把 metadata 格式作为代码内数据模型固定下来。

### 5.3 HostCore.resume_session()

新增 `HostCore.resume_session()`：

1. 读取 `sessions` row；
2. 校验存在；
3. 校验未 closed / archived；
4. 根据 metadata 或调用参数解析 workspace；
5. 新建 `host_session_id`；
6. 新建 terminal lease；
7. 调用 `build_agent_runtime_wiring(..., runtime_session_id=existing_id)`；
8. 构造 HostSession；
9. 发布到 registry。

它应该和 `open_session()` 共用 reservation / attach supervisor / rollback 流程，避免重复引入 Host ownership bug。

建议在 `HostCore.open_session()` 内抽出：

- `_open_session_with_runtime_id(...)`
- 或 `_build_host_session(...)`

但不要为了复用牺牲线性化检查。

### 5.4 Detach vs close

需要修改 REPL 语义：

- `Ctrl-D` / `:q` / `quit` / `exit`：detach；
- `:close`：关闭当前 conversation；
- `core.shutdown()`：关闭当前进程资源，但不应默认把 durable conversation 标为 closed；
- 只有显式 close 才写 `closed_at`。

注意：即使 detach 不关闭 conversation，当前进程退出时仍然需要释放 terminal lease、关闭 retrieval provider、停止 worker。区别是：不要把 durable session 标为 closed，也不要把可恢复 suspended run 当成用户显式 close。

这里需要设计一个 `HostSession.aclose(detach=True/False)` 或 HostCore 级参数：

```python
await core.shutdown(detach_sessions=True)
await core.close_session(host_session_id, close_conversation=True)
```

V1 中，进程退出时若有 active run，仍需写 interrupted/aborted RunEnd，避免 dangling；但这不等价于 close conversation。

### 5.5 Dangling run repair

推荐 V1 在 `resume_session()` 开始时做一次 repair：

- 找该 `runtime_session_id` 下 `runs.status = 'running'`；
- 若没有 live owner，则对每个 run append `RunEndEvent(status='aborted', stop_reason='resume_recovered_interrupted')`；
- 然后调用 `PostgresEventLog.repair_run_projection()`。

这样：

- Inspector 能看到 canonical 事件；
- `rebuild_prior_messages()` 可以走已有 failed/aborted recovery note；
- 未完成 tool call 会被 strip；
- 模型会得到“上一轮未完成”的诚实上下文。

需要谨慎：不要在另一个 live HostCore 正在运行该 session 时误杀 active run。V1 CLI 可以保守假设单进程，但代码应至少预留 lease/lock 检查。

### 5.6 Pending interactions 的 V1 边界

V1 不恢复 pending approval / pending plan coroutine。

恢复时：

- 如果上次 run 是 waiting_user，但进程已退出，则终结为 interrupted；
- 下轮 transcript 里插入 recovery note；
- 用户可以继续说“继续刚才的计划”；
- agent 根据历史重新进入 plan 或重新请求 approval。

后续 V2 若要精准恢复 pending，需要 durable table：

```text
pending_interactions(
  interaction_id,
  runtime_session_id,
  run_id,
  turn_id,
  reply_id,
  kind,
  payload,
  created_at,
  resolved_at
)
```

并且还要持久化足够的 LoopState。这个不建议放进 V1。

### 5.7 CLI/API

建议 CLI：

```bash
pulsara host repl --resume runtime:...
pulsara host repl --continue
pulsara host repl --list-sessions
```

REPL 命令：

```text
:sessions
:resume <runtime_session_id>
:continue
:close
:q
```

推荐初版：

- 先做启动参数 `--resume` / `--continue`；
- 再做 REPL 内 `:sessions` / `:resume`。

因为 REPL 内切换 session 需要处理旧 session detach、terminal lease release、new HostSession attach，状态更复杂。

### 5.8 GUI / Web API 预留

即使 V1 先做 CLI，也应把 HostCore API 设计成 GUI 可用：

```python
await core.list_resumable_sessions(...)
await core.resume_session(runtime_session_id)
await core.detach_session(host_session_id)
await core.close_session(host_session_id, close_conversation=True)
```

GUI 的 thread list 可以直接读 `sessions` + latest run summary + first/last user input。

---

## 6. 代码落脚点

### 6.1 新增模块

建议新增：

```text
src/pulsara_agent/host/resume.py
src/pulsara_agent/host/session_manifest.py
tests/test_host_resume.py
```

`session_manifest.py` 负责：

- 读写 `sessions.metadata`；
- 解析 workspace；
- 判断 resumable / closed / archived；
- 列最近 sessions。

`resume.py` 负责：

- dangling run repair；
- resume diagnostics；
- 与 HostCore composition root 使用的数据结构。

### 6.2 修改 HostCore

文件：

- `src/pulsara_agent/host/core.py`

需要新增：

- `resume_session()`
- `list_resumable_sessions()`
- `detach_session()` 或 close/shutdown 参数化

同时应尽量复用当前 open transaction：

- registry reservation；
- supervisor attach；
- lifecycle lock publish；
- rollback cleanup；
- retrieval resources owner。

不要绕过 `HostSessionRegistry.reserve/publish`，否则会重新引入 Host/session ownership 债务。

### 6.3 修改 runtime wiring

文件：

- `src/pulsara_agent/runtime/wiring.py`

现有 `runtime_session_id` 参数已经够用，但需要确认：

- resume 时 graph id 是否来自 memory domain；
- 若无 memory domain 时默认 `graph:runtime/{runtime_session_id}` 是否符合预期；
- timeline hook / outbox hook 使用同一个 runtime id 后是否继续 append 而非覆盖。

### 6.4 修改 CLI

文件：

- `src/pulsara_agent/cli.py`

新增参数：

```text
pulsara host repl --resume <runtime_session_id>
pulsara host repl --continue
pulsara host repl --list-sessions
```

REPL 命令：

```text
:sessions
:close
```

`_host_repl()` 需要根据参数选择：

- `core.open_session(...)`
- `core.resume_session(...)`
- `core.resume_most_recent_session(...)`

退出时：

- 默认 detach；
- `:close` 才 close durable conversation。

### 6.5 修改 inspector

文件：

- `src/pulsara_agent/inspector/store.py`
- `src/pulsara_agent/inspector/service.py`

可新增：

- `list_sessions(workspace_root=None, memory_domain_id=None, include_closed=False, limit=20)`
- `session_summary(runtime_session_id)`

这能同时服务 CLI `--continue` 和 GUI thread list。

### 6.6 修改 transcript / recovery

文件：

- `src/pulsara_agent/host/transcript.py`
- `src/pulsara_agent/runtime/recovery.py`
- `src/pulsara_agent/event_log/postgres.py`

建议：

- 为 `resume_recovered_interrupted` 增加 typed stop reason / recovery kind；
- 确保 dangling repaired run 会得到可读 system note；
- 对未完成 tool call 的 strip 行为增加测试。

---

## 7. 编码中可能遇到的坑

### 7.1 把 detach 误做成 close

这是最大产品坑。

如果 `Ctrl-D` 仍然调用会话 close 并写 `closed_at`，用户会觉得“我只是关了窗口，为什么不能继续？”

V1 必须明确：

- detach 是 UI lifecycle；
- close 是 conversation lifecycle。

### 7.2 复用旧 host_session_id

不能复用旧 `host_session_id`。旧 host id 是上一进程的 terminal owner principal。Resume 必须创建新的 host session，只复用 durable `runtime_session_id`。

### 7.3 pending approval 恢复幻觉

不要在 V1 声称可以恢复 pending approval。没有 durable LoopState 时，恢复同一个 approval 是假象。

正确做法：

- repair interrupted run；
- 给模型 recovery note；
- 用户重新表达下一步。

### 7.4 dangling run 与 live run 的竞争

如果未来 GUI/daemon 支持多个进程同时 attach，同一个 `runtime_session_id` 可能正由另一个进程运行。Resume repair 不能简单把所有 `running` run 终结。

V1 可以先限制单进程，但应预留：

- session lease 表；
- active owner heartbeat；
- fencing token。

### 7.5 terminal process 跨进程恢复

不要把 terminal process 当作可恢复事实。当前 terminal manager 是进程内资源。Resume 后应给用户 note，而不是尝试读旧 process handle。

真正跨进程 terminal 需要独立 daemon / workspace-scoped terminal supervisor，这不属于 resume V1。

### 7.6 graph id / memory domain 漂移

如果 resume 时 workspace 参数和原 session metadata 不一致，可能导致：

- 旧 event log 与新 memory domain 不一致；
- recall scope 变化；
- timeline outbox 写到不同 graph。

V1 应默认使用 session manifest 中的 workspace / memory domain；只有显式 override 才允许改变，并且要在 summary 中报告。

### 7.7 permission mode 漂移

恢复时应区分：

- session 原 permission mode；
- 当前 CLI 显式传入 `--permission-mode`；
- env 默认值。

推荐优先级：

1. CLI 显式参数；
2. session manifest；
3. 当前默认值。

否则用户恢复旧会话时可能意外变成 bypass 或 read-only。

### 7.8 runs projection 与 canonical events 不一致

最近已经出现过 run projection stale。Resume 不应信任 `runs` summary 作为唯一真值；应以 `agent_events` 为 canonical，必要时调用 `repair_run_projection()`。

### 7.9 recovery note 重复

如果每次 resume 都对同一个 dangling run 重复写 RunEnd 或重复插 note，会污染 transcript。

Repair 必须 idempotent：

- 如果已有 terminal RunEnd，不再写；
- stop reason 固定；
- 可通过 run status / event existence 判断。

### 7.10 session list 误选

`--continue` 如果只按 `sessions.created_at` 排序会错，因为旧 session 的最新 run 可能更新在最近。应该按：

- latest event sequence / latest event created_at；
- 或 latest run started/completed；
- fallback 到 session.created_at。

### 7.11 archived / closed session

闭合语义要固定：

- closed 默认不出现在 `--continue`；
- `--resume` closed session 默认拒绝；
- 后续可加 `--include-closed` 或 `--fork-closed`。

---

## 8. 测试矩阵与验收

### 8.1 Session identity

| 测试 | 验收 |
| --- | --- |
| 新开 REPL 创建 session | Postgres `sessions` 有 manifest |
| resume 指定 `runtime_session_id` | 新 HostSession 的 `runtime_session_id` 相同 |
| resume 后 host_session_id | 与旧 host_session_id 不同 |
| resume 后继续 run | `agent_events` 继续追加到同一 session，sequence 连续 |

### 8.2 Transcript continuity

| 测试 | 验收 |
| --- | --- |
| session A 说“我讨厌蛋挞”，detach，resume 后问“我讨厌什么？” | 模型能基于 prior messages 或 working context 回答“蛋挞” |
| 多轮历史恢复 | `rebuild_prior_messages()` 包含旧 user/assistant turns |
| resume 后再运行一轮 | 新 run 的 prior_messages 不包含未来事件 |

### 8.3 Detach vs close

| 测试 | 验收 |
| --- | --- |
| `:q` / Ctrl-D 退出 | session 未写 `closed_at`，可 resume |
| `:close` 退出 | session 写 `closed_at`，默认不可 resume |
| `--continue` | 不选择 closed session |

### 8.4 Continue selection

| 测试 | 验收 |
| --- | --- |
| 同 workspace 多 session | `--continue` 选择 latest activity 的 session |
| 不同 workspace session | 当前 workspace 的 `--continue` 不误选别的 workspace |
| memory_domain 不同 | 默认不跨 domain 继续 |

### 8.5 Dangling run recovery

| 测试 | 验收 |
| --- | --- |
| 人工制造 `RUN_START` 无 `RUN_END` | resume 写入 interrupted / aborted terminal event |
| dangling run 有 unfinished tool call | 下一轮 prior messages 不包含非法未完成 tool call |
| repair 幂等 | 多次 resume 不重复写 terminal RunEnd |
| inspector | `inspect run` 能看到 recovery diagnostic / terminal status |

### 8.6 Pending interaction V1 boundary

| 测试 | 验收 |
| --- | --- |
| 上次停在 pending approval | resume 不恢复 pending object，改为 interrupted note |
| 上次停在 pending plan exit | resume 不暴露旧 interaction_id，下一轮可让模型继续计划 |
| REPL prompt | resume 后不显示 `approval>` 或 `plan>`，除非新 run 再次进入 pending |

### 8.7 Terminal boundary

| 测试 | 验收 |
| --- | --- |
| 上个 session 有 terminal process | resume 创建新 terminal owner |
| 旧 terminal session id | 不出现在新 HostSession terminal manager 中 |
| transcript note | 如有未完成 terminal work，给出非恢复说明 |

### 8.8 Permission and plan state

| 测试 | 验收 |
| --- | --- |
| session manifest 有 permission_mode | resume 默认恢复该 mode |
| CLI 显式 `--permission-mode` | 覆盖 manifest |
| plan mode active events | HostSession.__post_init__ 恢复 plan_state |
| accepted plan summary | resume 后仍在 plan_state 中可见 |

### 8.9 Inspector / CLI

| 测试 | 验收 |
| --- | --- |
| `pulsara inspect session <id>` | 能显示 resume manifest |
| `pulsara host repl --resume <id>` | 进入同一 conversation |
| `pulsara host repl --continue` | 自动选择最近可恢复 conversation |
| `pulsara host repl --resume missing` | 清晰 not found |

### 8.10 Real LLM dogfood

至少需要一个真实模型 dogfood：

1. 启动 REPL/session；
2. 用户给出偏好或任务上下文；
3. agent 执行一个带工具调用的小任务；
4. detach；
5. 新进程 `--resume`；
6. 用户问“你还记得我们刚才在做什么吗？”；
7. agent 应能结合 prior_messages / working_context 正确回答；
8. 再要求继续原任务，agent 不应重新从零开始。

验收重点不是模型逐字回答，而是：

- 没有重新创建孤立 runtime session；
- event log 追加在同一个 `runtime_session_id`；
- 工具历史合法；
- 模型可见之前的 conversation state；
- 若上次中断，模型明确知道中断而不是虚构完成。

---

## 9. V1 实施顺序

推荐 PR 拆分：

### PR R0：session manifest 与查询

- 固定 `sessions.metadata` resume manifest；
- open_session 写 manifest；
- 更新 last_active_at；
- inspector/list sessions 支持 latest activity；
- 测试 manifest 和 list。

### PR R1：HostCore.resume_session

- 新增 resume composition root；
- 复用旧 `runtime_session_id`；
- 新建 host_session_id / terminal lease；
- 测试 identity、event sequence、prior_messages。

### PR R2：detach / close 语义

- REPL `:q` 默认 detach；
- 新增 `:close`；
- HostCore shutdown 不默认关闭 durable conversation；
- 测试 detach 可恢复、close 不可恢复。

### PR R3：dangling run repair

- resume 前 repair running runs；
- 写 recovery RunEnd；
- 幂等；
- 未完成 tool call 不重放；
- 测试 interrupted recovery。

### PR R4：CLI resume / continue

- `--resume`;
- `--continue`;
- `:sessions`;
- 错误消息和 not found；
- 测试 CLI parser 和真实 Postgres integration。

### PR R5：real LLM dogfood

- 跨进程 resume；
- 记忆/工作上下文；
- 工具使用；
- dangling run recovery。

---

## 10. 非目标

V1 不做：

- 复制 JSONL transcript；
- 恢复旧 terminal process；
- 恢复同一个 pending approval coroutine；
- 多进程同时 attach 同一 runtime session 的强一致 lease；
- fork session；
- resume-at message；
- GUI picker。

这些都可以后续做，但不应挡住 V1。

---

## 11. 最终判断

Pulsara 目前已经具备 resume 的最难基础：durable event log、runtime session id、transcript replay、run projection repair、inspector、working context、memory recall。

真正缺的是产品级 session lifecycle：

- durable conversation manifest；
- resume composition root；
- detach / close 区分；
- dangling run recovery；
- CLI/API 入口；
- 明确不恢复进程内 terminal/coroutine 的边界。

只要按这个边界做，Pulsara 的 resume 会比 Claude Code 的 JSONL transcript 更稳，因为 replay truth 是结构化事件；同时又能吸收 Claude Code 在恢复卫生上的成熟经验，避免“看似恢复、实际上下文非法或工具状态幻觉”的坑。
