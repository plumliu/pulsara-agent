# Pulsara Host Core 与 InMemory Supervisor 实施计划

_Created: 2026-06-20_

本文规划 Pulsara 的长生命周期 host core，以及在它之上的 InMemory 级「软恢复 / 长会话 registry」。目标不是把 CLI 做成产品主轴，而是补上 web / desktop / CLI 都能复用的宿主脊柱：由 host 显式拥有 workspace、conversation、`RuntimeSession`、terminal yielded process、event replay 与 shutdown 边界。

这份计划承接：

- `TERMINAL_YIELD_MODEL_V2_IMPLEMENTATION.zh.md`：terminal 已硬切到 Codex-like yield 模型，长命令超出 yield window 后成为 managed process。
- `TERMINAL_SHELL_ENV_V1_IMPLEMENTATION_PLAN.zh.md`：terminal subprocess env 已收敛到 sanitizer + shell snapshot + per-command `.venv/bin` overlay。
- memory scope 既有契约：host/前端识别 project vs transient，后端 memory 规范化 stable project key、生成 workspace scope、强制 read/write scope gate。

## 0. 总结

最终决策：

1. **先做 Host Core，再做 Supervisor**：没有长生命周期 host，就没有生产可达的「后台进程跨 turn 存活」与「断线重连软恢复」。先把 `RuntimeSession.close()` 的所有权上移到 host/session 真正结束；再做 workspace-scoped in-memory registry。
2. **CLI 只做薄 smoke driver**：CLI 可以提供 `run` / `repl` / `serve` 的最薄入口用于本地开发、测试、CI smoke，但产品主轴应是 web/desktop 复用的 host core。不要把业务状态绑死在 argparse loop 里。
3. **workspace_kind 保持现状 Literal**：后端 canonical 值继续使用 `Literal["project", "transient"]`，不引入 enum。`ephemeral` 可以作为 UI/CLI 入口别名，但进入后端前必须规范化为 `transient`。
4. **InMemory 软恢复只承诺 host 进程内恢复**：支持 transport 断开后重连、同一 workspace/conversation 内继续 poll/kill yielded process、event log replay；不承诺 host 进程崩溃后的磁盘恢复。
5. **不做磁盘 crash recovery**：当前输出三层结构只在超过阈值后写 artifact。若要做 Hermes-like crash recovery，需要 tee-from-spawn、PID/PGID identity 校验、recovered/detached 语义等前置条件；本文明确不做。
6. **workspace-scoped supervisor 是第二阶段**：先保证一个 host session 能跨多轮持有同一 `RuntimeSession`。再把 terminal manager/registry 从 per-session 提升为 per-workspace shared supervisor，并加 owner metadata、owner-scoped kill 与 detach/shutdown 生命周期策略。

推荐 PR 顺序：

```text
PR1: Host workspace identity resolver + memory domain coupling tests
PR2: HostSession / HostCore 长生命周期包装 + event-log canonical transcript seeding
PR3: 薄 CLI smoke 入口，复用 HostCore，不承载产品逻辑
PR4: InMemory HostSessionRegistry + reconnect event replay + idle lifecycle
PR5: Workspace-scoped TerminalSupervisor 注入 RuntimeSession
PR6: Supervisor diagnostics / process listing / cleanup policy / real LLM smoke
```

## 1. 当前代码事实

### 1.1 CLI 只是骨架，不是宿主

`src/pulsara_agent/cli.py` 当前只有：

- `--version`
- `demo-ledger`
- `config-check`

它没有：

- 打开 workspace / conversation 的入口。
- 长生命周期 loop。
- `RuntimeSession` registry。
- transport reconnect。
- session shutdown ownership。

所以 CLI 目前不能作为 terminal yielded process 生命周期的生产宿主。

### 1.2 wiring 有组合根，但没有 driver

`src/pulsara_agent/runtime/wiring.py` 已经能构建：

- `RuntimeSession`
- `EventLog`
- graph / archive / candidate pool
- memory hooks
- `AgentRuntime`

但 `build_agent_runtime_wiring(...)` 只返回对象，不驱动一个长生命周期 conversation。它也不会把同一 workspace 下的多个 conversation 组织起来。

### 1.3 AgentRuntime.run_task 不再 close RuntimeSession

当前契约已经正确改成：

- `AgentRuntime.run_task(...)` / `stream_task(...)` 只完成一轮 agent loop。
- `AgentRuntime.close()` 调 `RuntimeSession.close()`。
- `RuntimeSession.close()` 调 `terminal_sessions.shutdown()`。
- yielded process 在 run `FINISHED` 后继续存活，直到宿主显式 close。

测试 `tests/test_agent_runtime_loop.py::test_agent_runtime_finished_run_keeps_background_process_until_session_close` 已经钉住：

1. 一轮 run 启动 `sleep 10` 并 yield。
2. run `FINISHED` 后进程仍是 `RUNNING`。
3. `runtime_session.close()` 后进程变成 `KILLED`。

这正是 host core 要承接的边界：**run 结束不是 session 结束**。

注意：这条 close 语义只在当前 per-`RuntimeSession` terminal manager 下成立。等 PR5 引入 workspace-scoped shared supervisor 后，`RuntimeSession.close()` 不能再无条件 `terminal_sessions.shutdown()`，否则关闭 HostSession A 会误杀同 workspace HostSession B 的 yielded process。PR5 必须把 close contract 迁移成 owner-scoped cleanup / detach，见 §4.2 和 §8.6。

### 1.4 RuntimeSession 仍是 per-instance terminal owner

`RuntimeSession.__post_init__()` 内部直接创建：

```python
self.terminal_sessions = TerminalSessionManager(self.workspace_root)
```

因此当前 terminal registry 的所有权是：

```text
RuntimeSession -> TerminalSessionManager -> ProcessRegistry
```

这对单个长会话已经够用，但还不是 workspace-scoped supervisor。若一个 web/desktop host 重新创建了新的 `RuntimeSession`，旧 yielded process 不会自动出现在新 session 里。

### 1.5 AgentRuntime.stream_task 当前是单轮消息种子

`AgentRuntime._stream_task(...)` 每次都：

```python
state.messages.append(UserMsg(name="user", content=user_input))
```

它没有从 host conversation transcript 里 seed 历史消息。也就是说，即使 host 复用同一个 `AgentRuntime` 和 `RuntimeSession`，模型上下文仍然是单轮，除非 memory projection / working context 提供了足够背景。

所以长生命周期 host 不能只复用对象；还需要明确「conversation transcript 如何进入下一轮」。

当前 `RunStartEvent` 只记录 `user_input_chars`，不保存完整 user input。若未来要从 event log 完整重建跨 turn transcript，需要新增 user-turn 事件或在 `RunStartEvent.metadata` 中保存可重放的 user message。Host 层不得长期维护一个与 event log 无法互证的第二份 transcript。

### 1.6 MemoryDomainContext 的 canonical workspace_kind 是 project/transient

`src/pulsara_agent/memory/scope.py` 当前定义：

```python
workspace_kind: Literal["project", "transient"]
```

并在 `__post_init__` 中校验：

- `project` 必须有 `stable_project_key`。
- `transient` 必须没有 `stable_project_key`。

后端不使用 `ephemeral`。若 UI 或 CLI 使用「ephemeral」作为用户可读概念，必须在 host adapter 层转换成 `transient`。

## 2. 本地项目参照

### 2.1 Codex：长生命周期 thread/session 是 terminal yield 的前提

Codex 的 terminal yield 模型把「命令是否后台」从模型显式判断中移走，由 `yield_time_ms` 决定：

- 窗口内结束：返回 final result。
- 超过窗口：返回 process id，后续 poll/wait/kill。

关键不是 CLI 形态，而是所有权形态：thread/session 的 shutdown 边界早于进程 cleanup 边界。Codex 的 reducer 中也明确把 terminal yielded process 看成 session 资源，而不是单轮 turn 资源。

Pulsara 已经完成 terminal tool 形态上的 hard cut，但还缺 host/thread 层。没有 host/thread，yielded process 的跨 turn 能力只在测试里可达。

### 2.2 OpenClaw：InMemory supervisor 性价比高

OpenClaw 的方向更接近「应用进程内的 runtime registry」：

- 一个长运行应用管理 workspace/task/conversation runtime。
- transport/channel 断开不等于 runtime 立刻消失。
- registry 可以在内存里维持 session、task、subprocess、event 状态。

这类设计的性价比高于 Hermes-like disk crash recovery：它能解决 web/desktop 最常见的问题，即 UI 重连、下一轮继续、显式 shutdown；但不需要处理 PID reuse、checkpoint 损坏、跨重启日志恢复等重成本问题。

### 2.3 Hermes：磁盘恢复更重，本阶段不做

Hermes 的 gateway/session/orphan/recovery 体系更重，适合已经存在长运行 gateway 的产品形态。它也提示了磁盘恢复的正确成本：

- 需要明确 shutdown/reap 语义。
- 若要 crash 后恢复进程日志，必须从 spawn 起 tee 到磁盘。
- 若要 crash 后恢复 kill 能力，必须存 PID/PGID identity disambiguator，避免 PID reuse 后误杀无关进程。

Pulsara 当前输出 artifact 只在超过阈值后写入。强行做磁盘 crash recovery 会破坏既定输出存储结构。因此本阶段只做 InMemory soft recovery。

## 3. 目标与非目标

### 3.1 目标

Host Core v1 需要提供：

1. **可复用 host core**：web / desktop / CLI 都调用同一个 Python API，不复制生命周期逻辑。
2. **workspace identity resolver**：统一处理 project/transient、canonical path、display label、memory domain。
3. **长生命周期 HostSession**：一个 conversation/session 持有同一 `RuntimeSession`，多轮 user turn 不自动 close。
4. **conversation transcript seeding**：下一轮模型能看到必要历史，而不只是当前 user input。
5. **event replay / reconnect**：transport 断开后，client 可以用 session id + sequence cursor 重连并 replay missed events。
6. **terminal yielded process continuity**：run 结束后 yielded process 仍可 poll/kill；下一轮可继续使用。
7. **显式 shutdown**：host session/workspace 结束时统一 close，清理 terminal process。
8. **InMemory registry**：host 进程内按 session/workspace 管理 active sessions、recent events、terminal supervisor。

### 3.2 非目标

本阶段不做：

- 磁盘级 crash recovery。
- host 进程重启后恢复 live terminal process。
- PID/PGID adoption。
- artifact tee-from-spawn。
- 把 CLI 做成长期产品入口。
- 将 `workspace_kind` 从 Literal 改成 enum。
- 跨设备同步 runtime state。
- 多 host 进程同时管理同一 workspace 的分布式锁。

## 4. 术语与契约

### 4.1 Workspace identity 契约

宿主/前端负责识别用户意图：

```text
project:
  - 用户选择或当前打开的是一个真实项目目录
  - host 必须提供 canonical absolute path
  - host 可提供 display label

transient:
  - 一次性、scratch、未绑定项目的对话
  - host 不提供 stable_project_key
  - host 可提供 display label，例如 "Scratch"
```

后端 memory 负责：

```text
project:
  stable_project_key = canonical_project_key(abs_path)
  workspace_scope = ctx:workspace/<hash(stable_project_key)>
  read_scopes = {ctx:user, workspace_scope}
  write_scopes = {ctx:user, workspace_scope}

transient:
  stable_project_key = None
  read_scopes = {ctx:user}
  write_scopes = {ctx:user}
```

注意：

- 后端 canonical 值是 `project` / `transient`。
- `ephemeral` 只允许作为 UI/CLI adapter 的 alias，必须在创建 `MemoryDomainContext` 之前规范化为 `transient`。
- 继续使用 `Literal["project", "transient"]`，不新增 enum。

### 4.2 Runtime lifecycle 契约

```text
HostCore
  owns HostSessionRegistry

HostSession
  owns AgentRuntimeWiring
  owns RuntimeSession
  owns event-log-derived conversation transcript cache
  does not close after each run

AgentRuntime.run_task / stream_task
  executes one agent run / turn
  never closes RuntimeSession

RuntimeSession.close
  PR2-PR4: with a per-session terminal manager, shuts down that manager and kills the session's yielded processes
  PR5+: with an injected shared workspace supervisor, detaches from the supervisor and performs owner-scoped cleanup only

HostSession.close
  calls AgentRuntime.close / RuntimeSession.close exactly once, but cleanup scope depends on terminal manager ownership
```

这条契约必须在测试名和断言中写成 owner-scoped，不要写成「close kills all processes」。在 per-session manager 阶段，「session-owned process」等价于「manager 内全部 process」；但 PR5 共享 manager 后，测试必须迁移为「关闭 session A 只清理 A-owned process，B-owned process 存活」。

### 4.3 Soft recovery 契约

InMemory soft recovery 意味着：

- 同一个 host 进程仍然活着。
- `HostSessionRegistry` 里仍保留对应 session。
- transport 断开、UI 刷新、websocket 重连、CLI REPL 下一轮，不会丢失 session。
- 可以 replay missed events。
- 可以继续 poll/wait/kill yielded process。

它不意味着：

- Python host crash 后仍能恢复 live process。
- 机器重启后仍能恢复 process。
- 对已经 orphan 的 OS process 做 adoption。
- 对 sub-20k 输出做磁盘日志读取。

### 4.4 Concurrency 契约

v1 选择 asyncio 作为 host 并发模型。每个 `HostSession` 同时只允许一个 active run：

- `run_turn` / `stream_turn` 获取 per-session async lock。
- 若已有 run 正在执行，新请求返回 `busy` / `already_running`。
- transport reconnect 可以订阅/replay，但不能并发驱动同一 session 另一个 run。

workspace 级可以有多个 sessions，但 terminal process ownership 需要明确。

Registry 可使用普通同步 lock 保护纯内存 dict 的短临界区，但绝不能在持有 `threading.RLock` 时 `await`。如果 registry 方法需要执行 async close / stream / event publishing，必须改用 `asyncio.Lock` 或把临界区缩成「查表/换状态」后释放，再执行 await。

## 5. Host Core 设计

### 5.1 新模块结构

建议新增：

```text
src/pulsara_agent/host/__init__.py
src/pulsara_agent/host/identity.py
src/pulsara_agent/host/session.py
src/pulsara_agent/host/core.py
src/pulsara_agent/host/registry.py
src/pulsara_agent/host/supervisor.py
```

职责：

- `identity.py`：workspace input 规范化、project/transient 判定、memory domain 构造。
- `session.py`：`HostSession`，封装 `AgentRuntimeWiring`、event-log-derived transcript cache、run lock、event replay helpers、close。
- `core.py`：`HostCore` facade，供 web/desktop/CLI 调用。
- `registry.py`：in-memory `HostSessionRegistry`，管理 session lifecycle 与 idle sweep。
- `supervisor.py`：第二阶段 workspace-scoped terminal supervisor。

### 5.2 Workspace identity 数据结构

建议先用 dataclass，不引入 enum：

```python
@dataclass(frozen=True, slots=True)
class HostWorkspaceInput:
    workspace_kind: Literal["project", "transient"]
    workspace_root: Path | None = None
    display_label: str | None = None
    memory_domain_id: str = "u_local"
    cleanup_workspace_root_on_close: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedWorkspace:
    workspace_kind: Literal["project", "transient"]
    workspace_root: Path
    display_label: str
    memory_domain: MemoryDomainContext
    workspace_scope: str | None
    workspace_key: str
    cleanup_workspace_root_on_close: bool = False
```

Adapter 层可提供：

```python
def normalize_workspace_kind(raw: str) -> Literal["project", "transient"]:
    if raw == "ephemeral":
        return "transient"
    ...
```

但 `MemoryDomainContext` 只接收 canonical `project` / `transient`。

### 5.3 Project resolution

`workspace_kind="project"`：

- `workspace_root` 必须存在且是目录。
- resolve 成 canonical absolute path。
- `stable_project_key = resolved_workspace_root.as_posix()`。
- `display_label` 默认用目录名，允许 host 覆盖。
- `MemoryDomainContext(memory_domain_id=..., workspace_kind="project", stable_project_key=...)`。

测试断言：

- 相对路径会被 resolve 成绝对路径。
- `stable_project_key` canonical。
- `workspace_scope(stable_project_key)` 进入 read/write scopes。
- `graph_id == graph:user/<memory_domain_id>`。

### 5.4 Transient resolution

`workspace_kind="transient"`：

- 不设置 stable_project_key。
- workspace root 可以由 host 提供；若不提供，则 HostCore 创建一个 session 专属目录，或使用 configured scratch root 下的 session dir。
- transient 只表示「不绑定 project workspace scope」，不表示 workspace root 是 disposable。自动创建的 transient root 默认与 host-supplied transient/project root 平权保留，便于用户后续重连同一对话继续访问文件。
- 只有 host 显式设置 `cleanup_workspace_root_on_close=True`，且该 root 是 HostCore 自动创建而非 host-supplied path 时，close 才会删除 workspace root。project root 和 host-supplied transient root 永远不由 Pulsara 自动删除。
- `display_label` 默认 `"Scratch"` 或 `"Transient"`。
- `MemoryDomainContext(memory_domain_id=..., workspace_kind="transient")`。

测试断言：

- 传入 stable project key 会失败。
- read/write scopes 只有 `ctx:user`。
- transient session 不产生 workspace scope。
- 自动 transient root 默认 close 后保留。
- 自动 transient root 只有 opt-in cleanup 时 close 后删除。
- host-supplied transient root close 后保留。

### 5.5 HostSession 数据结构

建议：

```python
@dataclass(slots=True)
class HostSession:
    host_session_id: str
    conversation_id: str
    workspace: ResolvedWorkspace
    wiring: AgentRuntimeWiring
    created_at: float
    last_active_at: float
    closed: bool = False
    _run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _prior_messages_cache: list[Msg] = field(default_factory=list)
```

核心方法：

```python
async def stream_turn(self, user_input: str) -> AsyncIterator[AgentEvent]
async def run_turn(self, user_input: str) -> AgentRunResult
def replay_events(self, *, after_sequence: int | None = None) -> list[AgentEvent]
def close(self) -> None
```

### 5.6 Conversation transcript seeding

当前 `AgentRuntime.stream_task(user_input)` 每次只 seed 当前 `UserMsg`。HostCore 要成为真正多轮 conversation，必须补一个 transcript 入口。

推荐最小改动：

```python
async def AgentRuntime.stream_task(
    self,
    user_input: str,
    *,
    prior_messages: list[Msg] | None = None,
) -> AsyncIterator[AgentEvent]:
    ...
```

内部：

```python
state.messages.extend(prior_messages or [])
state.messages.append(UserMsg(name="user", content=user_input))
```

Canonical source of truth 必须是 `RuntimeSession.event_log`，不是 host 内另一份不可重建 list。PR2 有两种可接受做法：

1. **推荐**：新增可重放的 user-turn 事件，或在 `RunStartEvent.metadata` 中记录完整 user message；然后用 event log reducer 跨 run 重建 `prior_messages`。
2. **过渡**：`HostSession` 维护 `_prior_messages_cache`，但它必须声明为 event-log-derived cache。PR2 同时补充 user input 的 canonical event，否则 cache 不可在 reconnect / durable event replay 中重建。

不要把 `_prior_messages_cache` 作为第二个权威 transcript store。`event_log.replay(reply_id)` 已经是 assistant reply 的权威重建路径，跨 turn user/assistant/tool transcript 应沿用同一思路。

测试断言：

- turn 1 assistant 说出 sentinel。
- turn 2 模型上下文里能看到 turn 1 assistant/user history。
- tool result message 也随 transcript 进入下一轮，直到 compaction 策略出现。
- 清空 `_prior_messages_cache` 后，可以从 event log 重建同等 prior messages，或测试明确标注 cache 只是 PR2 过渡且已存在 canonical user-turn event。

风险：

- transcript 无界增长。v1 可接受，但必须在 docstring 里标注。
- 后续需要和 memory projection / compaction 统一。
- UI reconnect replay 与 model transcript seeding 不能分叉成两套历史；event log 是共同真相源。

### 5.7 Event replay

当前 `RuntimeSession.event_log` 已经能 append canonical sequence；`RuntimeEventPublisher` 支持 subscribe。HostCore 应提供：

```python
def replay_events(session_id: str, after_sequence: int | None) -> list[AgentEvent]
```

语义：

- `after_sequence=None`：返回当前 session event log 全量或最近窗口。
- `after_sequence=N`：返回 sequence > N 的 events。
- reconnect 后先 replay，再 subscribe live events。

v1 可以只做 in-memory event replay；durable event log 以后自然复用。

注意：当前 `EventLog.iter(...)` 只支持 `run_id` / `turn_id` / `reply_id` 过滤，不支持 sequence cursor。`after_sequence` 是新工作，需要在 protocol 与 in-memory/postgres 实现中补一个 sequence filter，或 HostCore 在 `iter()` 结果上临时过滤。

测试断言：

- run 中途 unsubscribe 后继续执行。
- reconnect 使用 sequence cursor replay missed events。
- replay 不重复 sequence <= cursor 的事件。

## 6. Thin CLI 设计

CLI 是 smoke driver，不是产品脊柱。它必须只调用 HostCore API，不直接拼 `RuntimeSession` lifecycle。

建议新增子命令：

```text
pulsara host run "prompt"
pulsara host repl
pulsara host inspect
```

通用参数：

```text
--workspace PATH
--workspace-kind project|transient
--display-label LABEL
--memory-domain-id u_local
--durable / --in-memory
--model-role pro|flash
--env-file .env
```

别名：

- CLI 可以接受 `--workspace-kind ephemeral`。
- CLI parser 立刻规范化为 `transient`。
- 后端 HostWorkspaceInput 不接收 `ephemeral`。

### 6.1 `host run`

用途：CI / 本地 smoke。

生命周期：

```python
core = HostCore(...)
session = core.open_session(...)
try:
    result = await session.run_turn(prompt)
finally:
    session.close()
```

它是 one-shot driver，因此 close 是正确的。

### 6.2 `host repl`

用途：验证长生命周期和 terminal yielded process。

生命周期：

```python
session = core.open_session(...)
try:
    while True:
        prompt = input("> ")
        await session.stream_turn(prompt)
finally:
    session.close()
```

测试：

- 第一轮让模型启动 `python -m http.server` 或 `sleep 10`。
- 第二轮要求 poll/kill 上一轮 process。
- 退出 REPL 时 close 清理 process。

### 6.3 `host inspect`

可选，用于调试：

- list host sessions。
- list workspace supervisors。
- list yielded processes。

初版可以不暴露给模型，只做 host/admin diagnostics。

## 7. InMemory HostSessionRegistry

### 7.1 Registry 数据结构

```python
@dataclass(slots=True)
class HostSessionRegistry:
    _sessions: dict[str, HostSession]
    _by_conversation: dict[str, str]
    _lock: asyncio.Lock
```

方法：

```python
def create_session(...) -> HostSession
def get_session(host_session_id: str) -> HostSession
def find_by_conversation(conversation_id: str) -> HostSession | None
def close_session(host_session_id: str) -> None
def list_sessions() -> list[HostSessionSummary]
async def sweep_idle(now: float | None = None) -> list[str]
```

如果实际实现选择同步 `threading.RLock`，只能用于不含 await 的短临界区，并且文档/注释必须写明「never held across await」。默认建议用 `asyncio.Lock`，因为 web/desktop transport 与 agent streaming 都是 async 工作流。

### 7.2 Session id 与 conversation id

- `host_session_id`：host 进程内 runtime session handle，适合 reconnect。
- `conversation_id`：产品层 conversation/thread id，由 web/desktop 管理，也可 CLI 自动生成。
- `runtime_session_id`：现有 Pulsara event/runtime id，继续由 `RuntimeSession` 使用。

不要混用这三个 id。

### 7.3 Idle lifecycle

建议配置：

```python
idle_ttl_seconds: float | None = 6 * 3600
max_sessions: int = 64
max_sessions_per_workspace: int = 8
```

策略：

- session active run 中不 sweep。
- idle 超 TTL 后是否 close session 取决于 live yielded process policy。
- transient session TTL 可短于 project session。
- web/desktop 用户显式关闭 conversation 时立即 close。

live yielded process policy 必须显式选择。推荐 v1：

- 若 session 有 live yielded process，默认不因普通 idle TTL 自动 close，改为标记 `idle_with_live_processes` 并暴露 diagnostics。
- 可配置 `live_process_idle_ttl_seconds`，默认 `None`，表示除显式 close / workspace close / host shutdown 外不杀。
- transient session 可以选择更短的 live TTL，但必须在 CLI/UI 告知。

原因：`npm run dev`、watcher、server 正是用户有意留下的长任务。「6 小时无新 turn」不等于「可以杀 server」。如果产品想要省资源，应该把这作为显式 policy，而不是藏在 idle sweep 中。

测试：

- idle session sweep 调用 `RuntimeSession.close()`。
- active run 不被 sweep。
- live yielded process 存在时，默认 idle sweep 不 close，或测试钉住所选配置。
- close idempotent。

## 8. Workspace-scoped InMemory TerminalSupervisor

### 8.1 为什么不是第一步

当前 per-`RuntimeSession` terminal manager 已能满足「同一 HostSession 多轮」。workspace-scoped supervisor 解决的是更进一步的问题：

- 同一 workspace 下多个 conversation 是否共享 terminal registry。
- web/desktop 重新创建 HostSession 时是否还能看到该 workspace 的 yielded process。
- workspace 关闭时统一清理所有 process。

这些都依赖 HostSessionRegistry 已存在。因此 supervisor 是第二阶段。

### 8.2 Supervisor 所有权

建议新增：

```python
@dataclass(slots=True)
class WorkspaceTerminalSupervisor:
    workspace_key: str
    workspace_root: Path
    terminal_sessions: TerminalSessionManager
    owner_sessions: set[str]
    created_at: float
    last_active_at: float
```

`workspace_key`：

- project：`workspace_scope_key(stable_project_key)` 或 canonical path hash。
- transient：`host_session_id` 或 generated transient workspace id。不要把所有 transient 混到一个 supervisor。

### 8.3 RuntimeSession 注入点

需要让 `RuntimeSession` 支持注入 terminal manager：

```python
@dataclass(slots=True)
class RuntimeSession:
    ...
    terminal_sessions: TerminalSessionManager | None = None

    def __post_init__(self):
        ...
        if self.terminal_sessions is None:
            self.terminal_sessions = TerminalSessionManager(self.workspace_root)
```

或者更干净地把字段拆成 init 参数：

```python
terminal_session_manager: TerminalSessionManager | None = None
```

HostCore 创建 project HostSession 时：

```python
supervisor = registry.get_or_create_workspace_supervisor(resolved_workspace)
runtime_session = RuntimeSession(..., terminal_session_manager=supervisor.terminal_sessions)
```

一旦 terminal manager 是注入的 shared supervisor 资源，`RuntimeSession.close()` 不能调用 `terminal_sessions.shutdown()`。需要区分 owned vs borrowed：

```python
RuntimeSession(
    ...,
    terminal_session_manager=supervisor.terminal_sessions,
    owns_terminal_session_manager=False,
)
```

语义：

- `owns_terminal_session_manager=True`：per-session manager，close 时 shutdown manager。
- `owns_terminal_session_manager=False`：shared supervisor manager，close 时只 detach session owner；不 all-kill。

这个 owner flag 是 PR5 的硬要求，不是 polish。

### 8.4 Process ownership metadata

workspace-scoped registry 会让多个 sessions 看到同一 ProcessRegistry。因此必须给 yielded process 加 owner metadata。

建议在 `TerminalProcessState` 增加：

```python
owner_host_session_id: str | None
owner_conversation_id: str | None
```

由 `TerminalSession.execute()` 或 Host-owned metadata 注入。初版可以通过 `TerminalRequest.metadata` 传入 host owner，后续再专门结构化。

ProcessRegistry 还必须新增消费 owner metadata 的方法；只加字段不够：

```python
def kill_owned(self, owner_host_session_id: str) -> list[TerminalResult]: ...
def close_owned(self, owner_host_session_id: str) -> None: ...
def list_owned(self, owner_host_session_id: str) -> list[TerminalProcessSummary]: ...
```

PR5 之前 `shutdown()` 仍然是 all-kill；PR5 之后 session close 必须走 `kill_owned` / detach，而 workspace close / host shutdown 才允许 all-kill。

策略选项：

1. **默认 owner-only**：普通 `terminal_process` 只能 poll/kill 本 session 创建的 process。
2. **workspace-admin diagnostics**：host inspect 可以看 workspace 全部 process。
3. **显式 attach**：未来如果要跨 conversation 接管 process，需要 host/admin API，不通过模型猜测。

推荐 v1 选择 owner-only。理由：

- process id 虽然是随机 uuid，但 workspace 内共享 registry 后，权限边界不能只靠不可猜。
- 模型不应随意 kill 另一个 conversation 的 server。

### 8.5 Soft recovery 行为

在同一 host 进程内：

```text
turn 1:
  terminal(command="npm run dev")
  yield -> process_id=proc_x
  run FINISHED

transport disconnect:
  HostSession remains open
  RuntimeSession remains open
  ProcessRegistry keeps proc_x

reconnect:
  client sends host_session_id + last_sequence
  HostCore replay events
  user asks "看一下服务还在吗"
  model can terminal_process(poll proc_x)
```

如果 host process crash：

```text
HostSessionRegistry lost
ProcessRegistry lost
in-memory output lost
OS child may become orphan depending process group/session
Pulsara does not promise adoption/recovery
```

### 8.6 Shutdown 行为

建议：

- `HostSession.close()`：per-session manager 阶段 kill its own manager；shared supervisor 阶段 kill session-owned yielded processes 或按 live-process policy detach，绝不 all-kill workspace manager。
- `HostCore.close_workspace(workspace_key)`：kill all processes in workspace supervisor，close all sessions。
- `HostCore.shutdown()`：close all sessions and supervisors。
- `atexit`：best-effort `HostCore.shutdown()`，但不作为唯一生命周期保障。

当前 `ProcessRegistry.shutdown()` 会 kill `_processes` 中 running process。workspace-scoped supervisor 不能复用这个方法作为 session close；PR5 必须先补 owner-aware kill/detach，再共享 manager。否则 HostSession A close 会杀掉 HostSession B 的 process。

## 9. Event 与 run active 状态

### 9.1 Active run tracking

`HostSession` 应维护：

```python
active_run_id: str | None
active_task: asyncio.Task | None
last_sequence_seen: int
```

v1 简化：

- `stream_turn` 在调用期间持有 lock。
- 不启动 detached background agent run。
- disconnect 是否 cancel 由 transport 决定；host core 不因 subscriber 消失自动 cancel。

web/desktop 后续可选择：

- 用户关闭 tab：只断开 transport，不 close session。
- 用户点击 Stop：cancel active run。
- 用户关闭 conversation：close session。

### 9.2 Cancellation

当前 `AgentRuntime` 没有显式 cancellation API。v1 可以依赖 task cancellation：

```python
task.cancel()
```

但要补测试：

- active model stream 被 cancel 后 run end / error 是否落 event。
- terminal tool 正在执行时 cancel 是否会留下可管理进程或被清理。

如果风险太大，PR1-PR4 先不暴露 Stop，只支持 close session。

### 9.3 Memory hook naming

当前 `MemoryHooks.on_session_start(state, user_input)` 在 `_stream_task()` 开头调用。单轮架构下它近似 session start；HostCore 多轮后，它事实上是 per-turn hook。

PR2 必须拍板：

- per-turn memory recall / cheap hint setup 是意图内行为，应保留。
- hook 名称应迁移为 `before_turn` / `on_turn_start`，或至少新增别名并逐步弃用 `on_session_start`。
- `on_session_end` 当前也在每个 run finalize 时触发；多轮 host 下它更像 `after_turn` / `on_turn_end`。若反射仍希望每 turn safe point，语义正确，但名字要改。

建议 PR2 同步做命名迁移，避免 host 引入后「session」一词同时指 run 和 host session。

## 10. Memory Scope 与 Host Core 的耦合

### 10.1 Project session

Host input：

```json
{
  "workspace_kind": "project",
  "workspace_root": "/abs/path/to/repo",
  "display_label": "repo",
  "memory_domain_id": "u_local"
}
```

Host resolver：

```python
stable_project_key = canonical_project_key("/abs/path/to/repo")
memory_domain = MemoryDomainContext(
    memory_domain_id="u_local",
    workspace_kind="project",
    stable_project_key=stable_project_key,
    workspace_label="repo",
)
```

Wiring：

```python
build_agent_runtime_wiring(..., memory_domain=memory_domain)
```

Expected scopes:

```text
read_scopes  = {ctx:user, ctx:workspace/<hash>}
write_scopes = {ctx:user, ctx:workspace/<hash>}
graph_id     = graph:user/u_local
```

### 10.2 Transient session

Host input：

```json
{
  "workspace_kind": "transient",
  "workspace_root": "/tmp/pulsara-scratch/session_x",
  "display_label": "Scratch",
  "memory_domain_id": "u_local"
}
```

Host resolver：

```python
memory_domain = MemoryDomainContext(
    memory_domain_id="u_local",
    workspace_kind="transient",
    workspace_label="Scratch",
)
```

Expected scopes:

```text
read_scopes  = {ctx:user}
write_scopes = {ctx:user}
graph_id     = graph:user/u_local
```

### 10.3 Ephemeral alias

Only adapter layer:

```text
UI label: ephemeral / scratch / temporary
CLI accepted value: ephemeral
Backend canonical: transient
```

Add tests:

- CLI `--workspace-kind ephemeral` creates `workspace_kind="transient"`.
- `MemoryDomainContext(..., workspace_kind="ephemeral")` still raises.

## 11. Implementation PR Plan

### PR1: Host identity resolver

Files:

- Add `src/pulsara_agent/host/identity.py`
- Add `tests/test_host_identity.py`

Implement:

- `HostWorkspaceInput`
- `ResolvedWorkspace`
- `normalize_workspace_kind`
- `resolve_workspace`
- project/transient memory domain creation

Tests:

- project path canonicalization。
- project scopes include workspace scope。
- transient has no workspace scope。
- `ephemeral` alias only accepted by normalizer。
- invalid workspace kind rejected。
- project without path rejected。

No LLM required.

### PR2: HostSession and transcript-aware AgentRuntime

Files:

- Add `src/pulsara_agent/host/session.py`
- Modify `src/pulsara_agent/runtime/agent.py`
- Add `tests/test_host_session.py`

Implement:

- `HostSession.run_turn/stream_turn/close`
- per-session run lock
- event-log canonical transcript seeding；如使用 cache，cache 必须可由 event log 重建
- canonical user-turn event / metadata so prior user messages are replayable
- `AgentRuntime.stream_task(..., prior_messages=None)`
- `AgentRuntime.run_task(..., prior_messages=None)`
- memory hook naming migration: `on_session_start/end` -> per-turn names or explicit aliases

Tests:

- two turns share transcript。
- `run_turn` does not close runtime session。
- close kills the session-owned yielded process in per-session manager mode; do not assert all-kill semantics that PR5 must later invert。
- concurrent run returns busy / raises deterministic error。
- close idempotent。
- per-turn memory hook fires once per user turn and naming reflects that contract。

注意：不要把 `RuntimeSession.close()` 放回每轮 finally。

### PR3: Thin CLI smoke driver

Files:

- Modify `src/pulsara_agent/cli.py`
- Add `tests/test_cli_host.py`

Implement:

- `pulsara host run`
- `pulsara host repl` if easy; otherwise PR3 只做 run，PR4 做 repl。
- args normalize workspace identity。

Tests:

- parser accepts project/transient。
- parser accepts ephemeral alias and maps to transient。
- one-shot run closes session。

CLI one-shot 是唯一允许 `finally: session.close()` 的地方，因为它代表 host lifetime 结束。

### PR4: HostCore and HostSessionRegistry

Files:

- Add `src/pulsara_agent/host/core.py`
- Add `src/pulsara_agent/host/registry.py`
- Add `tests/test_host_core.py`

Implement:

- `HostCore.open_session`
- `HostCore.get_session`
- `HostCore.close_session`
- `HostCore.replay_events`
- `HostSessionRegistry`
- idle sweep

Tests:

- reconnect by `host_session_id` returns same session。
- replay after sequence returns missed events。
- idle sweep closes sessions without live yielded process。
- idle sweep marks/skips sessions with live yielded process by default。
- active session not swept。

### PR5: WorkspaceTerminalSupervisor

Files:

- Add `src/pulsara_agent/host/supervisor.py`
- Modify `src/pulsara_agent/runtime/session.py`
- Possibly modify `src/pulsara_agent/runtime/wiring.py`
- Add `tests/test_workspace_terminal_supervisor.py`

Implement:

- workspace supervisor registry。
- inject shared `TerminalSessionManager` into `RuntimeSession`。
- owner metadata plumbing。
- `ProcessRegistry.kill_owned` / owner-scoped list/close。
- `RuntimeSession` owned-vs-borrowed terminal manager flag。
- close session kills owner processes。
- close workspace kills all workspace processes。

Tests:

- two HostSessions in same project get same workspace supervisor。
- process from session A survives session A run end。
- session B cannot model-poll/kill session A process unless attach/admin path exists。
- closing session A kills A-owned process but not B-owned process。
- closing workspace kills all。
- closing RuntimeSession with borrowed manager does not call shared manager shutdown。

### PR6: Diagnostics and real LLM smoke

Files:

- Add host inspect summaries if useful。
- Add gated real LLM tests。

Real LLM tests:

1. **Long session terminal continuity**：
   - turn 1: ask model to start a long process using terminal yield。
   - turn 2: ask model to poll/kill that process。
   - assert same host session and process id path works。
2. **Workspace memory scope**：
   - project session can write/read workspace-scoped memory。
   - transient session does not memorize project-specific scratch task detail。
3. **Reconnect replay**：
   - simulate subscriber disconnect; replay missed event sequence。

## 12. Test Matrix

### Deterministic unit tests

- Host workspace identity canonicalization。
- `project` vs `transient` memory scope。
- `ephemeral` alias normalization。
- HostSession close idempotency。
- `run_turn` keeps RuntimeSession open。
- close cleans up session-owned yielded processes in per-session mode。
- transcript is passed to next turn。
- transcript/prior messages are reconstructable from event log or backed by a declared canonical user-turn event。
- per-session run lock。
- event replay after sequence。
- idle sweep with and without live yielded process。
- workspace supervisor owner isolation。

### Integration tests

- HostCore + in-memory wiring。
- HostCore + durable wiring if Postgres available。
- CLI `host run`。
- CLI `host repl` smoke with scripted input if practical。

### Real LLM gated tests

Use `PULSARA_RUN_REAL_LLM=1` only:

- long-lived terminal across turns。
- terminal process poll/kill after run finished。
- model does not need to decide background; uses Codex-like terminal naturally。
- memory scope discipline in project vs transient。

## 13. Observability

HostSession summary should include:

```json
{
  "host_session_id": "...",
  "conversation_id": "...",
  "runtime_session_id": "...",
  "workspace_kind": "project",
  "workspace_root": "/abs/path",
  "display_label": "repo",
  "created_at": 123.0,
  "last_active_at": 456.0,
  "closed": false,
  "active_run_id": null
}
```

Supervisor diagnostics should include:

```json
{
  "workspace_key": "...",
  "workspace_root": "/abs/path",
  "owner_session_count": 2,
  "live_process_count": 1,
  "finished_process_count": 3
}
```

Do not expose provider secrets or raw env diagnostics in host summaries.

## 14. Failure Modes and Guardrails

### 14.1 Session leak

Risk：web/desktop disconnects without close。

Mitigation：

- idle TTL。
- live yielded process disables ordinary idle close by default, or uses an explicit `live_process_idle_ttl_seconds` policy。
- explicit close endpoint。
- host shutdown closes all sessions。
- metrics / diagnostics for active sessions。

### 14.2 Cross-conversation process kill

Risk：workspace-scoped registry lets one conversation kill another conversation's process。

Mitigation：

- process owner metadata。
- `terminal_process` checks owner by default。
- admin/inspect path separate from model-facing tool。

### 14.3 Transcript growth

Risk：HostSession transcript grows unbounded。

Mitigation：

- v1 mark as accepted limitation。
- later add compaction / summary / memory projection integration。
- set max transcript token/char guard before product surface。
- event log remains canonical; transcript cache, if any, is derived and invalidatable。

### 14.4 Active run cancellation

Risk：transport disconnect cancels task accidentally, leaving partial state。

Mitigation：

- host core does not tie subscriber lifetime to run lifetime。
- explicit cancel/stop API later。
- close session remains the hard cleanup boundary。

### 14.5 Crash recovery overpromise

Risk：用户误以为 InMemory soft recovery 能处理 host crash。

Mitigation：

- docs/API names avoid `crash_recovery`。
- use `soft_recovery` / `reconnect_recovery` / `in_memory_supervisor`。
- diagnostics mention `recovery_scope: "host_process"`。

### 14.6 Shared supervisor all-kill hazard

Risk：PR5 共享 terminal manager 后，旧的 `RuntimeSession.close() -> terminal_sessions.shutdown()` 会 all-kill 同 workspace 其它 sessions 的 process。

Mitigation：

- `RuntimeSession` tracks whether terminal manager is owned or borrowed。
- borrowed manager close means detach / owner cleanup, not shutdown。
- `ProcessRegistry` provides owner-aware kill/list/close。
- PR2 tests phrase cleanup as owner-scoped from the start。

## 15. Acceptance Criteria

Host core is ready when:

- A project HostSession can run at least two turns using the same `RuntimeSession`。
- A terminal yielded process started in turn 1 can be polled/killed in turn 2。
- `RuntimeSession.close()` is called only when HostSession/HostCore closes。
- project/transient memory scopes match existing `MemoryDomainContext` contract。
- CLI smoke path uses HostCore rather than direct wiring。
- reconnect replay can recover missed events within same host process。
- idle sweep and explicit shutdown clean yielded processes。
- ordinary idle sweep does not silently kill intentionally yielded live processes unless configured。

Workspace supervisor is ready when:

- project workspace has one shared in-memory terminal supervisor per host process。
- transient workspace is isolated per transient session。
- process owner metadata prevents accidental cross-conversation kill。
- RuntimeSession close on a borrowed/shared manager does not all-kill sibling sessions。
- workspace close kills all processes in that workspace。
- no disk crash recovery promise is made。

## 16. Open Questions

1. Product-level `conversation_id` source：web/desktop should probably own it; CLI can generate one。
2. User identity / `memory_domain_id`：local dev can default to `u_local`，product host should pass authenticated user id normalized to flat id。
3. Transcript compaction：v1 can be full transcript; product needs compaction before large conversations。
4. Stop/cancel API：do we want explicit stop in first host PR, or defer until active run UX exists？
5. Process list tool：should model ever get a list of yielded processes, or only host/admin diagnostics？建议先只做 diagnostics。

## 17. Bottom Line

Pulsara 现在已经有了正确的 terminal primitive，但还缺一个真正拥有它的长生命周期 host。先补 HostCore，让 `RuntimeSession` 从「测试里被复用」变成「产品架构里被拥有」；再做 OpenClaw-like workspace-scoped InMemory supervisor，让 web/desktop 的重连、下一轮继续、显式 workspace shutdown 都有稳定落点。

这条路线刻意避开 Hermes-like 磁盘 crash recovery，因为它会牵动输出存储模型、PID identity 与跨重启 adoption。当前最值得做的是 host 进程内的软恢复：轻、可测、产品路径马上用得到。
