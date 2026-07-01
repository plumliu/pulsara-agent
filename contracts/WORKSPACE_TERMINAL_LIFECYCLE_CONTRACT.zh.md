# Workspace Terminal Lifecycle 契约

_Created: 2026-07-01_

这份文档冻结 Pulsara host / session / workspace terminal 的生命周期契约。它是
[HOST_SESSION_OWNERSHIP_RUNTIME_INTEGRATION_AUDIT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/HOST_SESSION_OWNERSHIP_RUNTIME_INTEGRATION_AUDIT.zh.md)
的落地口径，回答四件硬事：

1. 哪一层拥有哪一类状态与资源；
2. 谁是唯一的生命周期协调者；
3. open / close session / close workspace / shutdown 的线性化边界与原子性；
4. 哪些行为在重构后必须保持不变。

它不是实现计划，而是用来冻结边界。涉及的相关代码：

- [`src/pulsara_agent/host/core.py`](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/core.py)
- [`src/pulsara_agent/host/registry.py`](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/registry.py)
- [`src/pulsara_agent/host/supervisor.py`](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/supervisor.py)
- [`src/pulsara_agent/host/session.py`](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)
- [`src/pulsara_agent/runtime/session.py`](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/session.py)
- [`src/pulsara_agent/runtime/wiring.py`](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/wiring.py)
- [`src/pulsara_agent/runtime/terminal/manager.py`](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/manager.py)

---

## 0. 核心立场

- **HostCore 是唯一 lifecycle coordinator。** session / workspace / application 三级关闭都只由 HostCore 编排，其它层不得跨层协调彼此。
- **每一层只拥有自己那一类状态。** `HostCore` 拥有 application 资源与协调权；`WorkspaceTerminalSupervisor` 拥有共享 terminal pool；`HostSession` 拥有 conversation 与 run-control；`RuntimeSession` 拥有 event/tool runtime 与一个明确的 terminal binding。
- **owner 隔离不可回退。** terminal 强制隔离 principal 永远是 `host_session_id`。
- **open 是可回滚事务。** attach lease、build runtime、registry publish 要么整体成功，要么异常路径上 exactly-once 释放已分配资源。
- **同步 teardown 不阻塞 event loop。** 同步 kill / reader-join 必须在锁外、在 `asyncio.to_thread` 上执行。
- **关闭语义可审计。** active 与 suspended run 在 close 时都产生 typed terminal outcome，不靠字段清空或裸 `task.cancel()` 偶然决定语义。

---

## 1. 四种 identity 必须保持分离

| Identity | 语义 | 不承担的语义 |
|---|---|---|
| `workspace_key` | workspace supervisor 缓存键与 workspace close 目标 | 不代表 conversation / run |
| `host_session_id` | host 进程内 session handle，也是 **terminal owner principal** | 不等于 durable conversation id |
| `conversation_id` | 产品层 conversation identity，进入 terminal owner **diagnostics** | 不是 terminal 授权 key |
| `runtime_session_id` | event / artifact / runtime 持久化归属 | 不代表 workspace terminal pool |

- project workspace 的 `workspace_key` 来自 canonical project path 的 scope；同路径多 session 命中同一 supervisor。
- transient workspace 每次 resolve 生成新 `workspace_key`，不共享 supervisor。
- 授权只认 `host_session_id`。`conversation_id` 与 `runtime_session_id` 只进 diagnostics，不做“任一匹配即放行”的授权。

---

## 2. HostCore：唯一 lifecycle coordinator

HostCore 持有显式 lifecycle 状态（`OPEN / CLOSING / CLOSED`），不再用单一 `_shutting_down: bool`。

HostCore 负责：

- reserve / publish / reject host session identity；
- reserve / release workspace terminal lease；
- 编排 HostSession close（唯一入口 `close_session`）；
- close workspace；
- application shutdown；
- idle candidate 的实际关闭；
- 对**所有** public facade 应用 lifecycle gate。

冻结约束：

- **shutdown gate 覆盖所有 facade**，不只 `open_session`。`get` 之外的可改状态/可启动执行入口（new turn、resume approval / plan、stream、stop、mode switch、enter plan）在 `CLOSING/CLOSED` 后都必须被拒绝。最危险的是 resume：shutdown 认为 borrower 已 drain 后，resume 不得再启动 suspended run。
- `close_session` **幂等**：重复关闭同一 id 不抛 `KeyError`，得到 no-op。
- 同一 session 的并发 close 只有一个 coordinator 执行清理，其余调用者等待该 close 完成；清理步骤抛错也必须完成 registry finalize 并唤醒等待者，不得遗留永久 `CLOSING` identity。
- 并发 shutdown 只有一个 owner 执行关闭序列，其余调用者等待 `_shutdown_complete`；资源关闭失败可以向 owner 报错，但 HostCore 最终必须进入 `CLOSED` 并唤醒等待者。
- 同步 terminal teardown（kill / reader-join / session prune）**只在锁外经 `asyncio.to_thread` 执行**；HostCore 的 supervisor async lock 内只做状态转换与取出待清理 lease。
- registry、HostSession、RuntimeSession、supervisor 都不得跨层协调彼此关闭。

---

## 3. Registry：纯索引，不执行 teardown

`HostSessionRegistry` 退化为索引 + idle candidate 发现器，不再是第二个关闭协调者。

冻结的接口语义（命名可调，语义不可调）：

- `reserve(host_session_id, conversation_id) -> reservation`，reservation 携带不可复用 token；identity 相同的新 reservation 也必须获得新 token。
- `publish(reservation, session)`
- `release_reservation(reservation)`
- `get(host_session_id) -> HostSession`
- `begin_close(host_session_id) -> HostSession | None`
- `finish_close(host_session_id)`
- `list_idle_candidates(now) -> list[str]`

硬约束：

- **duplicate `host_session_id` / `conversation_id` fail closed**：不得覆盖已存在的 live owner。覆盖会让两个 borrower 共用一个 terminal owner principal，关闭任一方都可能杀错进程。
- `publish` / `release_reservation` 必须校验当前 identity 所属 token。旧 open transaction 的延迟 rollback 不得释放 identity 相同的新 reservation（ABA）；旧 reservation 也不得 publish 到新一代 slot。
- registry **不调用 `session.aclose()`**；关闭由 HostCore 执行。
- `CLOSING` 中的 session 不再接受普通 get-for-mutation 操作。
- `begin_close` 在 registry 临界区内同步关闭 HostSession 自身的 mutation gate；不得在“registry 已 closing、session 仍 open”的 await 窗口中允许 stop / resume / mode switch 等操作插入。
- idle sweep 只**返回 candidate**，不产生任何外部副作用（不 close、不 detach）。不得再在 registry 内注入一个不可 await 的 close callback。

---

## 4. Supervisor：唯一 shared terminal pool owner

`WorkspaceTerminalSupervisor` 拥有显式状态（`OPEN / CLOSING / CLOSED`）与 **typed lease registry**，不再是 `manager + set[str]`。

```python
@dataclass(frozen=True, slots=True)
class TerminalOwnerContext:
    host_session_id: str          # 授权 principal
    conversation_id: str          # diagnostics only
    runtime_session_id: str | None = None

@dataclass(frozen=True, slots=True)
class WorkspaceTerminalLease:
    workspace_key: str
    owner: TerminalOwnerContext
    generation: int
    manager: TerminalSessionManager
```

核心语义：

- `attach(owner)` 在 `OPEN` 下返回唯一 lease（带单调 `generation`）；**workspace `CLOSING/CLOSED` 后 attach 必须失败**；同一 `host_session_id` 重复 attach 失败（uniqueness 已由 registry 保证，supervisor 是第二道防线）。
- release 幂等且按 `generation` 校验：stale / superseded lease 的 release 是 no-op，不误删新 borrower。
- release 负责**完整 owner cleanup**：kill/drain owner process、删除 owner 的 terminal session 与 cwd state、释放 owner 占用的 manager capacity、清掉强引用 RuntimeSession 的 completion recorder。**不能只 `kill_owned()` 而留下 stale `_sessions` key**（否则 shared 路径容量被已关闭 owner 永久挤占，最终触发 `max_sessions`）。
- shared manager 与 process registry 的容器访问必须有内部线程同步；`release_owner` 先 revoke owner、再 prune/kill，使旧 `TerminalSession` 引用不能在 cleanup snapshot 后重新注册 process。manager shutdown 是永久状态，之后不得再创建 terminal session/process。
- shutdown 负责 all-kill 兜底，并清空 lease registry。
- supervisor 的状态转换是同步、快速的；**真正的同步 kill 由 HostCore 在锁外的 thread 执行**，supervisor 不在持有 asyncio lock 时自行 block，也不偷偷起 background task。
- manager 不作为 HostCore 之外的生命周期 API 暴露。

类名保留 `WorkspaceTerminalSupervisor`；问题从来不在命名。

---

## 5. RuntimeSession：持有 binding，不解释 supervisor policy

`terminal_session_manager + owns_terminal_session_manager + terminal_owner_host_session_id` 三参数组合收敛为一个 typed binding：

```python
TerminalRuntimeBinding = OwnedTerminalRuntime | BorrowedWorkspaceTerminalRuntime
```

- `OwnedTerminalRuntime`：standalone RuntimeSession 拥有 local manager，`close()` 时 `shutdown()` 它。默认 binding 即 owned-local，使裸 `RuntimeSession(workspace_root)` 仍可独立工作。
- `BorrowedWorkspaceTerminalRuntime`：HostCore 路径注入，携带 `TerminalOwnerContext` 与 shared manager。**borrowed close 只释放 runtime-local 资源（publisher/hooks 等），绝不 `kill_owned()` / detach / shutdown shared manager**。lease release 由 HostCore/supervisor 完成且只做一次。
- RuntimeSession 暴露 `terminal_owner_host_session_id` 与 `terminal_owner_conversation_id`（派生自 binding），供 tool registry 与 owner-scoped 视图使用。terminal owner context 同时进入 `TerminalTool` / `TerminalProcessTool`，使 `owner_conversation_id` 数据流不再断裂。
- RuntimeSession 仍保留 `close()`，但职责是 runtime-local，而不是猜测上层 attachment 生命周期。

---

## 6. HostSession：拥有 run state machine

HostSession 有显式状态：

```text
OPEN -> CLOSING -> CLOSED
```

并统一处理 active run、stopping run、suspended approval / plan interaction、plan workflow state、runtime-local close。

### 6.1 统一 execution handle

所有 `run_turn` / `stream_turn` / approval resume / plan resume（streaming 与非 streaming）**必须共用同一个内部 execution task/cancel handle**：

- HostSession 始终拥有内部 `_active_task` + `_active_state`；transport 只消费结果或事件流。
- 是否使用 async generator 只是观察方式差异，**不得改变 task ownership、stop、drain、close 语义**。
- `stop_current_turn` 与 `drain_active_run` 因此能覆盖所有 active 执行入口，不再只覆盖 `run_turn`。
- streaming observer queue 必须有固定上限并通过 `await put` 施加背压，不得让慢消费者无限积压 delta。v1 上限为 128 个 event item。
- transport 关闭/放弃 async generator 只表示 observer detach：清空该 observer 的 bounded queue，后续 event 仍持久化但不再投递给它；**不得取消 HostSession-owned `_active_task`**。只有 typed stop、HostSession close 或 HostCore shutdown 可以取消执行。
- observer detach 后 `_run_lock` 即使因 transport generator 退出而释放，所有 turn/resume/mode/plan 入口仍必须通过 `_active_task` gate 拒绝并发执行，直至 driver 自行 finalization。

### 6.2 close 与 run finalization（决策 1）

- close 是**有界、幂等**的 `aclose(reason=...)`。
- active 与 suspended run 在 close 时都产生 typed、可审计的 terminal outcome：新增 host-teardown 类型的 `AbortKind`，经 `abort_run` 落 `RunEndEvent`。**它不伪装成 `USER_STOP`。**
- 不再用裸 `task.cancel()` + 字段清空来隐式表达“session 被销毁时这个 run 怎么了”。
- close **不直接**承担 shared terminal lease release；lease release 是 HostCore/supervisor 的职责。
- transient root cleanup 永远排在所有 borrower / resource release **之后**。
- summary 从内部字段拼裸 dict 升级为 typed host session snapshot。

---

## 7. 三种 close 的冻结语义

### 7.1 Host session close

1. session 标记 `CLOSING`，拒绝新 turn / resume / mode switch / enter plan；
2. 按 §6.2 终止 active / suspended run，落 host-teardown terminal outcome；
3. 有界等待 host runtime hooks/finally 完成（含 governance notify）；
4. 关闭 runtime-local resources；
5. release workspace terminal lease，kill owner process、prune owner terminal sessions（exactly once，在锁外 thread）；
6. registry `finish_close`；
7. 若 transient root 由 host 创建且允许清理，最后删目录。

上述 finalize 是 fail-safe 的：某个 teardown step 失败不得跳过 registry `finish_close`，也不得让并发 close 永久等待。错误在尽可能完成其余清理后上抛。

### 7.2 Workspace close

1. workspace 标记 `CLOSING`，**拒绝新 attach**；
2. snapshot 并经 HostCore 关闭该 workspace 全部 HostSession；
3. supervisor shutdown 兜底 all-kill；
4. 从 supervisor registry 删除 workspace；
5. 标记 `CLOSED`。

### 7.3 HostCore shutdown

1. HostCore 原子进入 `CLOSING`，所有 facade 拒绝新工作；
2. snapshot sessions / workspaces；
3. 有界关闭所有 HostSession，让 run finally 与 governance notify 完成；
4. 关闭剩余 workspace supervisor；
5. drain/cancel governance / vector worker，再关闭 retrieval provider；
6. 标记 `CLOSED`。

并发 shutdown 调用必须等待同一关闭序列；即使某个 provider / supervisor 的 `aclose` 失败，完成信号也必须在 `finally` 语义下发布。

硬约束：provider 关闭前不能再有可恢复/可启动的 agent borrower；terminal all-kill 前不能再有执行中的 terminal tool。retrieval 与 terminal 的细粒度先后可微调，但上面两条不可破。

---

## 8. 必须保持不变的既有行为

- **owner isolation**：session B 不能 poll / kill session A 的 process；close A 只杀 A-owned process，B 继续运行；workspace close 才 all-kill；terminal cwd 按 owner 隔离。
- **turn 结束 ≠ session 结束**：FINISHED / FAILED / WAITING_USER 后不自动关闭 RuntimeSession 或 terminal manager；yielded process 跨 turn 存活是产品能力。
- **model-facing 与 admin-facing 视图分离**：`terminal_process` 与 HostSession summary 是 owner-scoped；workspace supervisor snapshot 是 admin 视图，可含 owner metadata。统一 schema 不等于放宽模型权限。
- **teardown 不伪造自然 completion**：teardown kill 必须 suppress completion event，不让用户收到“看似自然完成”的后台任务通知。
- **host-process 级软恢复**：transport 断开但 HostCore 存活可按 host session id reconnect；HostCore 崩溃后不承诺重建 registry / adopt OS process。本契约不扩展为 disk crash recovery。

---

## 9. 开工前已拍板的决策

1. **suspended run 持久化语义**：增加 host-teardown typed abort，active 与 suspended run 都落可审计 terminal outcome，不伪装成 `USER_STOP`。
2. **关闭 HostSession 是否总 kill owner process**：v1 是。`关闭 UI tab 但保留 server` 建模为 transport disconnect，不是 close HostSession；process adoption 若需要，另立显式 workspace-admin transfer API。
3. **HostCore 是否保留 `use_workspace_supervisor=False`**：主路径删除该开关。standalone RuntimeSession 仍可自行拥有 local manager，满足低层单测与非 HostCore 嵌入场景。
4. **finished process diagnostics 是否跨最后一个 owner 保留**：v1 不保留；最后一个 owner close 后 workspace pool 可释放。历史任务面板应从 event/artifact projection 构建。
5. **conversation id 是否进入 terminal owner context**：进入 diagnostics metadata，授权 principal 仍是 `host_session_id`。
6. **workspace capacity 是全局还是 per-owner**：v1 workspace 总额度，diagnostics 必须显示 owner 分布；饥饿被证明真实发生后再加 per-owner 子额度。

---

## 10. 禁止事项

- 任何层不得绕过 HostCore 直接 close HostSession（registry 不得 `session.aclose()`）。
- 不得用 duplicate identity 覆盖 live owner。
- 不得让 open 在异常路径上泄漏 supervisor lease 或已建 runtime-local resource。
- borrowed RuntimeSession close 不得 `kill_owned()` / detach / shutdown shared manager。
- 不得只 `kill_owned()` 而不删除 manager 中该 owner 的 terminal session（会永久挤占容量）。
- 不得在持有 asyncio lock 时执行同步 kill / reader-join；也不得在 supervisor 同步方法里偷偷起 background task。
- 不得只对 `open_session` 做 shutdown gate 而放过 resume / stop / mode switch。
- 不得用裸 `task.cancel()` + 字段清空决定 active/suspended run 的可审计语义；host-teardown 必须 typed。
- 不得让 `_active_task` 只覆盖 `run_turn`；streaming 与 resume 必须共用同一 execution handle。
- 不得用 `owner_conversation_id` 与 `host_session_id` 做“任一匹配即放行”的授权。
- 不得让 CLI / UI 把静态 workspace inspect 的空 `workspace_supervisors: []` 当作真实运行时列表消费。
- HostCore 主路径不得再保留 `use_workspace_supervisor` 兼容分叉。

---

## 11. 测试守护

### 11.1 现有、必须继续通过

- owner isolation：close A 只杀 A-owned，B 继续运行；workspace close 才 all-kill；cwd 按 owner 隔离。
- 跨 turn yielded process 存活；teardown completion suppression；mode switch 不重建 runtime/terminal。
- retrieval shutdown 正常路径。

### 11.2 本次必须新增

- **duplicate identity fail closed**：重复 `host_session_id` 被拒绝，旧 session/runtime/process 不受影响；duplicate `conversation_id` 策略钉死（拒绝，不覆盖）。
- **open rollback**：registry capacity / wiring 构造失败时 rollback supervisor lease 并关闭已建 runtime-local resource。
- **reservation ABA**：旧 reservation 释放后，同 identity 重新 reserve；再次 release/publish 旧对象不得消费或覆盖新 reservation。
- **idle sweep 不自行 close**：sweep 只返回 candidate；关闭统一经 HostCore；sweep 后 supervisor lease 不残留。
- **open / shutdown / workspace-close 线性化**：shutdown 开始后 open / new turn / resume / mode switch 被拒绝；workspace closing 后新 attach 被拒绝。
- **lease release exactly once**：borrowed RuntimeSession close 不再重复 kill owner；owner cleanup 只执行一次。
- **owner capacity 恢复**：release 后 manager 中该 owner 的 terminal session 被删除，session capacity 恢复（P0-7 回归）。
- **streaming / resume drain**：streaming turn 与 streaming/非 streaming resume 都能被同一 stop/close primitive drain。
- **bounded stream observer**：慢消费者最多积压 128 个 event；detach 后 run 继续、第二轮被 active-task gate 拒绝，Host close 仍可中止 detached run 并落 typed terminal outcome。
- **active / suspended close 可审计**：两者都落 typed host-teardown terminal outcome，不伪装成 `USER_STOP`。
- **closing mutation gate**：registry `begin_close` 返回后，session 的 turn / resume / stop / mode switch / plan mutation 全部被拒绝；不存在 registry/session 状态错位窗口。
- **concurrent close / shutdown completion**：第二个 close/shutdown 调用等待 owner 完成；清理失败不遗留永久 `CLOSING` registry identity 或 HostCore，也不饿死等待者。
- **event-loop responsiveness**：慢 `release_owner` 不跨 supervisor lock；cleanup 期间其它 workspace 的只读 list/attach 不被数秒阻塞。
- **shared-manager thread safety**：同 workspace 的 owner A release 与 owner B terminal session/process 创建并发时不发生容器迭代竞态；被 release 的旧 TerminalSession 引用不能再执行命令。
- **shared capacity diagnostics**：多 host session 的 named terminal session 共同消耗 workspace 额度；达上限的错误含 workspace/owner 诊断；已关闭 owner 不通过 stale terminal session key 永久挤占容量。

### 11.3 由 real LLM dogfood 守护（本契约不强制其在 CI 通过）

- 两个 HostSession 在同一 workspace 各跑长任务、各自只能操作自己的 process；关闭一个 conversation 后另一个任务仍可被模型继续操作；workspace close 后旧 process id 不可用；pending approval 时 host close 不留可恢复但无 owner 的悬空 run；HostCore shutdown 后 resume 被拒绝。
