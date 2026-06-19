# Pulsara Terminal Runtime v1 增强实施计划

_Created: 2026-06-19_

本文规划 Pulsara 下一步 terminal tool 的重构/增强。目标不是一次性实现 Docker/SSH/云沙箱，而是把当前「前台命令执行器」升级成可支撑真实 agent 工作流的 **managed execution runtime**。

相关调研见：`TERMINAL_TOOL_OPEN_SOURCE_SURVEY.zh.md`。

## 0. 当前基线

当前 Pulsara terminal 的事实：

- `TerminalTool` 入口：`src/pulsara_agent/tools/builtins/terminal.py`
- runtime model：`src/pulsara_agent/runtime/terminal/models.py`
- session manager：`src/pulsara_agent/runtime/terminal/manager.py`
- local backend：`src/pulsara_agent/runtime/terminal/backends/local.py`
- output helper：`src/pulsara_agent/runtime/terminal/output.py`
- tool executor：`src/pulsara_agent/tools/executor.py`
- permission skeleton：`src/pulsara_agent/runtime/permission.py`

当前能力：

1. 前台执行 shell command。
2. workspace-root 限制。
3. cwd 持久化。
4. `workdir` 参数。
5. timeout + 进程组终止。
6. partial output 保留。
7. ANSI strip、secret redaction、head/tail truncation。
8. session 数量限制。
9. tool result 事件目前是工具结束后一次性发出。

当前缺口：

1. 没有 PTY。
2. 没有 live process registry。
3. 没有 background process。
4. 没有 stdin/write/submit/EOF。
5. 没有 poll/wait/kill。
6. 没有真实 streaming output。
7. 没有 full output artifact。
8. 没有 exec policy / approval / elevated。
9. 没有 shell detection/snapshot。

## 1. v1 总目标

把 terminal 从：

```text
TerminalTool(command) -> TerminalResult(output)
```

升级为：

```text
TerminalTool(command, mode) -> managed process/run
Process actions -> poll/wait/write/kill
Output -> streaming events + bounded projected result + optional full artifact
Policy -> command approval/sandbox/elevated 的预留边界
```

v1 的目标是 local backend 可用、接口稳定、测试闭环；不要求一次性接 Docker/SSH/云沙箱。

## 2. 非目标

v1 不做：

1. 不接 Docker/SSH/remote sandbox。
2. 不实现完整企业审批 UI。
3. 不实现跨 OS 的 PowerShell/Cmd 完整兼容。
4. 不把所有 terminal delta 做 coalescing；先保留完整 delta，后续根据真实压力再投影优化。
5. 不删除现有 `terminal` 工具语义；尽量兼容短命令 foreground 使用习惯。
6. 不把 file read/write/edit 全部改走 terminal。
7. PR1 不实现 UI 可见的 live streaming events；但 PR1 **必须**持续 drain 后台进程输出，否则后台高吞吐命令会死锁。

## 3. 核心设计原则

### 3.1 Runtime first

新增能力应放在 `src/pulsara_agent/runtime/terminal/`，`TerminalTool` 只做参数解析和 tool result 包装。

例外：真正的 live streaming events 会触碰 `ToolExecutor` / tool execution protocol，因为事件需要在工具调用尚未完成时进入 event log。也就是说，process lifecycle、PTY、output accumulation 属于 terminal runtime；streaming delivery 是唯一需要合法改动工具执行边界的能力。

### 3.2 进程是 runtime 资源

`process_id` / `terminal_session_id` 不是普通 JSON 字段，而是 runtime 管理的资源：

- 有 owner session。
- 有 cwd。
- 有 backend。
- 有 started/ended 时间。
- 有 running/exited/killed/timed_out 状态。
- 有 output accumulator。
- 有 cleanup 策略。

进程生命周期归 `RuntimeSession` / conversation / workspace session 所有，不归单次 `AgentRuntime.run_task()` 所有。单轮 run 进入 `FINISHED` 后，background process 应继续存活，允许后续用户消息通过 `terminal_process(poll|wait|kill)` 观察或控制它。只有宿主/driver 明确结束整个 session 时，才调用 `RuntimeSession.close()` 做 best-effort cleanup。

因此 one-shot CLI / test driver 必须在自己的外层显式包 cleanup：

```python
try:
    await agent.run_task(user_input)
finally:
    runtime_session.close()
```

Codex-like 多轮对话则不能在每轮后 close；它应在用户关闭 thread/workspace、driver teardown、app 退出等真正 session 生命周期终点 close。

### 3.3 Foreground/background 统一

foreground 命令也应通过同一套 process runner 执行，只是工具调用会 wait 到完成或 timeout。

background 命令返回 live process id，后续由 process actions 操作。

但「同一套 process runner」不等于 foreground 也成为 registry 里的可轮询资源。PR1 的边界是：

1. foreground 是同步 tool call 拥有的 run：可以复用 `ManagedTerminalProcess` / reader / accumulator，但不返回可 poll 的 `process_id`。
2. foreground 不计入 max live background process limit。
3. foreground 完成后不进入 finished-process retention 环；若实现上临时注册用于 cleanup，返回 tool result 前必须自动 evict。
4. finished retention 只服务 background process，因为只有 background 的 `process_id` 会在后续 turn 被 `terminal_process` 查询。

cwd doctrine 必须在 PR1 钉死：

1. foreground 命令可以在命令完成后更新 `TerminalSessionState.current_cwd`。
2. background 命令只在启动时捕获 cwd，**不得**在退出时回写 session cwd。
3. background 命令不参与 cwd readback；直接把启动时的 `effective_cwd` 记录进 `TerminalProcessState.cwd`。
4. 前台命令只有在进程完全退出、输出 drain 完成、cwd 捕获完成后，才能更新 session cwd。
5. foreground 若继续使用 `pwd -P` readback 机制，该 readback 必须 scoped to run/process，不能复用同一 backend 上的共享 `_cwd_file`。

### 3.4 Pipe mode 与 PTY mode 并存

默认 pipe mode；显式 `tty=true` 或 `pty=true` 启用 PTY。

PTY 不应吞掉 pipe stdin 场景。v1 至少保留一个反例 guard：

```text
gh auth login --with-token -> pipe mode
```

### 3.5 完整输出与上下文投影分离

内部可保存完整输出；给模型 replay 的 tool result 必须是 bounded preview。

redaction 必须在 accumulator 内按边界处理。v1 采用行边界策略：reader 保留未完成行 tail，只对完整行做 ANSI/binary sanitation 和 secret redaction 后再进入 live buffer / artifact / streaming event。否则 secret 被拆成两个 chunk（如 `API_KEY=ab` + `cd123`）时会漏出。

这带来一个有意识的 v1 取舍：未换行的 pending line 不进入 `poll/wait` snapshot。`Password:`、`>>>`、`Continue? [y/N]` 这类无换行 prompt 可能暂时不可见，直到进程输出换行或退出。v1 优先保证 split secret 不泄露；若未来要展示 pending prompt，必须先设计对半截 tail 的安全 redaction，而不能直接把 pending buffer 暴露给 LLM/UI。

### 3.6 Policy 不写死在 backend

backend 只执行命令。命令是否允许、是否要审批、是否要后台、是否要 elevated，属于 policy/runtime 层。

## 4. 建议文件结构

```text
src/pulsara_agent/runtime/terminal/
  models.py              # request/result/process/action models
  manager.py             # TerminalSessionManager + process registry
  session.py             # session state
  guard.py               # v1 deterministic command guard
  output.py              # output processing + accumulator
  policy.py              # exec policy skeleton
  process.py             # ManagedTerminalProcess / ProcessRegistry
  pty.py                 # local PTY adapter helpers
  shell.py               # shell detection/init/snapshot helpers
  backends/
    local.py             # foreground/background local backend

src/pulsara_agent/tools/builtins/
  terminal.py            # start command
  terminal_process.py    # optional: poll/wait/kill/write/submit/close
```

两种工具表面可选：

1. **单工具多 action**：`terminal(action="exec|poll|wait|kill|write|submit|close")`
2. **双工具**：`terminal` 启动命令，`terminal_process` 管理已有进程

推荐 v1 使用双工具：模型心智更清楚，也更接近 Codex 的 `exec_command + write_stdin` 与 Hermes 的 `terminal + process`。

## 5. Tool schema v1

### 5.1 `terminal`

```json
{
  "command": "string",
  "workdir": "string?",
  "terminal_session_id": "string?",
  "timeout_seconds": "integer?",
  "max_output_chars": "integer?",
  "background": "boolean?",
  "tty": "boolean?"
}
```

语义：

- `background=false`：等待命令完成或 timeout，返回 final result。这里的 `timeout_seconds` 是 **进程寿命上限**；超时必须杀进程组并返回 `timeout`。
- `background=true`：启动后尽快返回 `process_id` 和 best-effort initial output。这里的 `timeout_seconds` **不得**被解释为后台进程寿命上限；PR1 建议对 `background=true + timeout_seconds` 返回结构化 `unsupported_argument`，直到未来显式引入 `max_lifetime_seconds` / watchdog 语义。
- `tty=true`：使用 PTY backend，适合交互 CLI。
- `terminal_session_id`：保留 cwd/env/shell snapshot 语义。
- `notify_on_complete` 不进入 v1 schema。完成通知属于 PR4 streaming/event delivery 之后的能力；如果兼容解析层提前收到该参数，应 fail closed 为 `not_supported_yet`，不得静默承诺。

`background=true` 的 initial output 采集窗口在 PR1 固定为 best-effort：

1. spawn 成功且 reader 已启动后，等待到首个安全 snapshot 非空、进程立刻退出，或最多约 200ms。
2. initial output 只来自已经过行边界 redaction 的 accumulator snapshot；未完成行保留在私有 pending buffer，不进入返回值。
3. initial output 可以为空；调用方应依赖 `poll/wait` 获取后续输出，而不是把启动返回当作完整日志。

返回 JSON：

```json
{
  "status": "success|error|running|timeout|blocked|killed",
  "output": "...bounded preview...",
  "exit_code": 0,
  "cwd": "...",
  "timed_out": false,
  "truncated": false,
  "process_id": "proc_... or null",
  "terminal_session_id": "default",
  "backend_type": "local",
  "full_output_ref": null
}
```

`process_id` 只在 `background=true` 时是可被 `terminal_process` 使用的 runtime id；foreground 返回 `null`。

### 5.2 `terminal_process`

```json
{
  "action": "poll|wait|kill|write|submit|close_stdin",
  "process_id": "string",
  "data": "string?",
  "timeout_seconds": "integer?",
  "max_output_chars": "integer?"
}
```

语义：

- `poll`：返回当前状态和新输出/尾部输出。
- `wait`：阻塞直到完成或 timeout。这里的 `timeout_seconds` 只是 **等待上限**；超时表示本次 wait 停止等待并返回当前 `running` 状态，绝不能杀进程。未传 `timeout_seconds` 时必须使用有限默认值（v1：30 秒），不存在无限等待。
- `kill`：终止进程树或 PTY process。
- `write`：写 raw stdin，不追加换行。
- `submit`：写 data + `\n`。
- `close_stdin`：发送 EOF / close pipe。

Schema 可以先一次性声明完整动作，但实现必须按 PR 增量显式 fail closed：

- PR1：只支持 `poll|wait|kill`。
- PR2：支持 pipe mode 的 `write|submit|close_stdin`。
- PR3：支持 PTY mode 的 `write|submit|close_stdin`。
- 未支持 action 必须返回结构化 `not_supported_yet`，不得静默 no-op。

## 6. Runtime model

新增或扩展 model：

```python
class TerminalRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    KILLED = "killed"

class TerminalIOMode(StrEnum):
    PIPE = "pipe"
    PTY = "pty"

@dataclass
class TerminalProcessState:
    process_id: str
    terminal_session_id: str
    command: str
    cwd: Path
    mode: TerminalIOMode
    status: TerminalRunStatus
    pid: int | None
    started_at: float
    ended_at: float | None
    exit_code: int | None
    timed_out: bool
    output: OutputAccumulator
    reader_thread: Thread | None
    lock: Lock
```

`TerminalSessionState` 继续保存：

- `session_id`
- `workspace_root`
- `current_cwd`
- `backend_type`
- shell snapshot metadata

线程安全要求：

1. `TerminalProcessState` 会被 reader 线程和 tool handler 同时访问，状态更新必须受锁保护。
2. `OutputAccumulator` 必须自带锁，或只允许在 `TerminalProcessState.lock` 下访问。
3. `TerminalSessionManager` / process registry 必须是两个工具共享的同一实例；`terminal` 与 `terminal_process` 不能各自 default-construct manager。
4. `TerminalProcessState` 是纯 runtime 内存资源，**不得**序列化进 event log、tool result 或持久化表。事件与 tool result 只能携带显式构造的 snapshot：`process_id`、status、exit_code、cwd、preview、truncation、full_output_ref 等。禁止 `dataclasses.asdict(process_state)` 这类整对象导出，因为 `Thread` / `Lock` 不可安全 deepcopy/序列化。

Process registry 边界：

1. PR1 必须定义 max live process limit，独立于现有 `TerminalSessionManager.max_sessions`。
2. PR1 必须定义 finished process retention：至少包括 finished TTL 或最大 finished records，避免旧 `process_id` 和输出无限驻留内存。
3. max live process limit 与 finished retention 只作用于 background process；foreground 短命令不消耗 background 配额，也不挤占 finished retention。
4. finished retention 清理在 PR1 采用 **惰性驱逐**：只在 spawn/poll/wait/kill 等 registry 访问路径中执行，不启动单独 cleanup timer thread。这样 reader thread 只更新自己的 `TerminalProcessState`，不直接修改 registry map。
5. 若未来改成定时清理或支持真正并行 tool execution，必须给 registry map 增加集合级锁；PR1 不把这个锁作为隐含需求。

## 7. OutputAccumulator

替换「结束后一次性 finalize」为增量 accumulator：

职责：

1. append bytes/chunks。
2. streaming decode UTF-8。
3. ANSI strip / binary sanitation。
4. 行边界 secret redaction。
5. rolling tail。
6. 记录 total chars/bytes/lines。
7. 超限时持久化 full output 到 artifact/temp file。
8. snapshot 生成 bounded preview。
9. 线程安全地支持 reader thread 与 poll/wait 并发读取。

v1 可先简单实现：

- 内存保留完整输出直到 `max_full_output_chars`。
- 超过阈值落到 workspace `.pulsara/terminal-output/` 或 runtime artifact store。
- replay 给模型只返回 `max_output_chars` 预览。
- PR1 即使不做 live streaming，也必须在后台进程启动后立刻启动 reader drain，否则高吞吐后台进程会在 OS pipe buffer 满后阻塞。

后续再接正式 artifact store。

## 8. Streaming events

当前 `ToolExecutor.execute()` 在线程里同步执行工具，结束后只发一个 `ToolResultTextDeltaEvent`。

v1 需要让 terminal 能向 event log 增量发 output。可选实现路径：

### 路线 A：工具内回调

`ToolCall` 或 `ToolExecutionContext` 增加 `emit_event` callback。

问题：会影响所有工具接口。

### 路线 B：Terminal runtime publisher

`TerminalSessionManager` 接收 runtime publisher / event recorder，terminal backend 在读到 output chunk 时 emit `ToolResultTextDeltaEvent`。

问题：terminal runtime 会知道 tool_call_id/event_context。

### 路线 C：先不改 Tool 协议，只实现 process registry

PR1 先做 background/poll/wait/kill 和持续 drain，不做 UI 可见 live streaming；后续再改 `ToolExecutor` 支持 streaming tool。

推荐：**C -> B**。

理由：

- 先把 process lifecycle 跑通。
- 再把 terminal 接入 event streaming，避免一次 PR 同时改工具协议和进程管理。

## 9. Exec policy v1

新增 `runtime/terminal/policy.py`，先做 deterministic floor：

```python
class ExecPolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_CONFIRMATION = "require_confirmation"
    SUGGEST_BACKGROUND = "suggest_background"

@dataclass
class ExecPolicyDecision:
    kind: ExecPolicyDecisionKind
    reason: str | None
    suggested_args: dict[str, Any]
```

v1 policy：

1. 空命令 block。
2. 非正 timeout/output limit block。
3. workspace escape block。
4. 已知长跑命令 foreground 时不直接 block，而是返回 suggestion：`background=true`。
5. shell-level background wrappers foreground 时提示改用 managed background。
6. 高风险 destructive 命令先只做 `REQUIRE_CONFIRMATION` skeleton，接现有 `PermissionGate`。

PR1 必须先把现有 long-running hard block 改成：

- `background=false`：保持 blocked/suggest_background。
- `background=true`：放行到 managed background process。

否则会出现 `background=true` 已实现但 `npm run dev` / `vite` / `uvicorn` 仍被 `CommandGuard` 拦在 backend 前的能力不可达状态。

高风险命令示例：

- `rm -rf`
- `sudo`
- `chmod -R`
- `chown -R`
- `dd`
- `mkfs`
- 写 `$HOME`、`~/.ssh`、credential files

v1 不追求全覆盖，但 policy 结构必须可扩展。

## 10. Shell environment v1

目标：减少「用户 shell 能跑，agent 不能跑」。

v1 最小能力：

1. 检测用户 shell：`SHELL`，fallback `/bin/bash` 或 `/bin/sh`。
2. 支持 `login=True` / `interactive_init=False` 参数，内部可配置；v1 默认 `login=False`，避免用户 profile 输出或阻塞逻辑污染每条工具结果。
3. 对 macOS/zsh，需要显式 login/init 能力时再考虑 `zsh -lc` 或 shell snapshot，不能把 login shell 作为所有命令的默认路径。
4. 保留 repo `.venv`/uv 偏好不在 terminal 内硬编码，由 AGENTS.md/系统提示指导。
5. 支持用户 shell helper，例如 `proxy_on`，前提是 login shell/init 文件可见。

Shell snapshot 可在 v1.1 做，不必塞进 PR1。

但 shell environment 不是锦上添花。仓库已有「优先使用 repo `.venv/` + `uv`」的执行偏好，若 terminal 长期固定 `bash -c` 且不加载用户 shell/init，macOS/zsh 用户很容易遇到 `uv`、`nvm`、`pyenv` 或 `proxy_on` 不可见。若 PR1/PR2 的真实 smoke 暴露这类问题，应把 PR7 提前。

## 11. 实施阶段

### PR1：Process registry + background lifecycle

改动：

1. 新增 `runtime/terminal/process.py`。
2. `TerminalSessionManager` 持有唯一 process registry，并由 runtime/wiring 注入 `terminal` 与 `terminal_process` 两个工具。
3. 禁止两个工具各自 default-construct manager；进程管理启用时，如果没有共享 manager，应 fail closed。
4. `LocalTerminalBackend` 支持启动 managed process。
5. 每个 live process 启动独立 reader thread / drain task，持续读取 stdout/stderr。
6. 引入线程安全 `OutputAccumulator`。
7. `terminal(background=true)` 返回 `running + process_id`。
8. 新增 `terminal_process` 工具：PR1 只支持 `poll|wait|kill`。
9. foreground 改用同一 managed process path，但 foreground run 不返回可 poll 的 `process_id`，不计入 live background limit，不进入 finished retention；若临时注册，返回前自动 evict。
10. long-running guard 在 `background=true` 时放行。
11. cwd doctrine：foreground 可更新 session cwd；background 只记录启动 cwd、不做 readback、不回写 session cwd；foreground readback 机制不得复用 backend-wide `_cwd_file`。
12. registry shutdown：宿主/driver 调用 `RuntimeSession.close()` 时 best-effort SIGTERM tracked process groups，必要时 SIGKILL；`AgentRuntime.run_task()` 正常 FINISHED 不自动 close。
13. accumulator redaction 单位定为行边界，reader 保留未完成行 tail。
14. process registry 定义并执行 max live process limit。
15. process registry 定义 finished process retention 策略：finished TTL 或最大 finished background records，且 poll/wait 对过期 process 返回结构化 not_found/expired；retention 采用惰性驱逐。
16. `background=true` initial output 采集窗口固定为「首个安全 snapshot / 进程退出 / 约 200ms」三者先到；返回值允许 output 为空。
17. `background=true + timeout_seconds` 在 PR1 fail closed 为 `unsupported_argument`，避免把 foreground 寿命 timeout 与 background watchdog 混用。

退出条件：

1. foreground 短命令行为与现有测试兼容。
2. background `sleep` 返回 running，poll 后仍 running，wait 后 success。
3. 高吞吐后台进程不会死锁：例如持续输出超过 pipe buffer 的命令可正常退出并被 wait 到。
4. `npm run dev` / `vite` / `uvicorn` 这类长跑命令在 `background=true` 下不再被 guard 拦截。
5. background server 可被 kill。
6. timeout 仍杀进程组，不泄漏 child。
7. foreground cwd 仍按 workspace 约束更新。
8. background 进程退出不改变 session cwd。
9. 同一 `terminal` 启动的 process 可被 `terminal_process` poll/kill，证明共享 registry 接线正确。
10. 显式 `RuntimeSession.close()` 会终止 tracked running processes；普通 run FINISHED 后 background 仍可被 poll。
11. chunk 边界拆开的 secret 不会以明文进入 accumulator snapshot。
12. 超过 max live process limit 时，新 background 命令 fail closed，并返回结构化错误。
13. finished process 超过 retention 后被清理；后续 poll 返回结构化 expired/not_found，而不是抛异常或泄漏旧输出。
14. 连续执行大量 foreground 短命令不会消耗 background live 配额，也不会把 background finished records 挤出 retention。
15. background 启动返回遵守 initial output 窗口：reader 已启动，最多短暂等待，output 可以为空但不得包含未 redacted 的半行 secret。
16. `terminal_process(wait, timeout_seconds=N)` 超时只停止等待并保持进程 running，不杀进程。

### PR2：stdin + pipe mode interaction

改动：

1. pipe mode process 保留 stdin pipe。
2. `terminal_process(write|submit|close_stdin)`。
3. `python -c "print(input())"` 这类命令可被 submit。
4. EOF 可结束 stdin-driven command。

退出条件：

1. write 不追加换行。
2. submit 追加换行。
3. close_stdin 发送 EOF。
4. finished process 拒绝 write。
5. stdin 操作会产生可追踪事件/metadata。

### PR3：PTY mode

改动：

1. 增加 PTY dependency 或基于 stdlib `pty` 实现 POSIX v1。
2. `terminal(tty=true, background=true)` 启动 PTY process。
3. PTY 支持 write/submit/close/kill。
4. 对 pipe-stdin 必需命令禁用 PTY 或提示。

退出条件：

1. Python REPL 可启动、submit 表达式、返回输出。
2. 普通 foreground 命令不受 PTY 影响。
3. `gh auth login --with-token` 这类命令不会被强行 PTY。
4. PTY process kill 不残留。

### PR4：Streaming output events

改动：

1. terminal process reader 读到 chunk 时 emit `ToolResultTextDeltaEvent`。
2. `ToolExecutor` 或 terminal runtime 支持 streaming tool event。
3. event log 能 replay 最终 `ToolResultBlock`。
4. UI/timeline 能看到 running updates。

退出条件：

1. 长命令每 0.1-0.5s 输出一段，事件在进程结束前已出现。
2. final tool result 等于 accumulator projection。
3. 并发 readonly tools 不被 terminal streaming 破坏。

### PR5：Output artifact / full output ref

改动：

1. `OutputAccumulator` 超限落盘。
2. tool result JSON 返回 `full_output_ref`。
3. full output 可通过 read_file/artifact API 读取。
4. context replay 只使用 preview。

退出条件：

1. 生成 1MB 输出不会把 tool result 直接塞入上下文。
2. preview 含 head/tail 或 tail。
3. full output 文件存在且可读。
4. secret redaction 策略明确：artifact 是否保存 redacted 版本需文档钉死。

### PR6：Exec policy skeleton

改动：

1. 新增 `runtime/terminal/policy.py`。
2. `CommandGuard` 迁移/收敛到 policy。
3. 长跑 foreground 改为 suggestion 或 controlled error。
4. dangerous command 接 `PermissionGate` 的 WAIT_FOR_USER。

退出条件：

1. `npm run dev` foreground 不再只是 MVP block，而提示 managed background。
2. shell-level `&`/`nohup` 有明确 guidance。
3. 高风险命令能触发 confirm event。
4. denial 会变成 structured tool result。

### PR7：Shell environment baseline

改动：

1. shell detection helper。
2. login shell option。
3. shell init file/source 策略。
4. PATH 修复。
5. 记录 shell metadata。

退出条件：

1. `echo $SHELL` / `which uv` / 用户 PATH case 有测试或本地 smoke。
2. cwd 被删除时能恢复到最近存在 ancestor 或 workspace root。
3. proxy helper 的支持边界写入文档。

## 12. 测试矩阵

### 12.1 兼容性

1. 现有 `tests/test_terminal_runtime.py` 全绿。
2. 现有 `tests/test_tools.py::test_terminal_*` 全绿。
3. foreground `pwd && printf hi` 行为不变。
4. 连续大量 foreground 短命令不消耗 background live process 配额，也不挤占 background finished retention。
5. foreground result 不暴露可被 `terminal_process` poll 的 `process_id`。

### 12.2 Background lifecycle

1. `sleep 5` background -> running。
2. poll running process -> running。
3. wait process -> success。
4. kill process -> killed。
5. kill 后 poll -> killed。
6. max process limit 生效。
7. finished process TTL 或 cleanup 行为明确。
8. 高吞吐后台输出不会死锁：命令输出远超 OS pipe buffer 后仍能退出，`wait` 能看到完整/截断后的输出。
9. `terminal(background=true)` 启动的 process 能被 `terminal_process` 找到，证明两个工具共享同一 registry。
10. 显式 `RuntimeSession.close()` 会终止 tracked running process，不留下 orphan；普通 run FINISHED 不触发 shutdown。
11. background initial output 最多等待固定短窗口；返回可以为空，但 reader 必须已经启动。
12. background initial output 不包含未完成行里的 secret 片段；后续完整行进入 snapshot 后才 redacted 输出。
13. retention 驱逐是惰性的：在 spawn/poll/wait/kill 路径触发，没有独立 cleanup timer thread。

### 12.2.1 cwd 与后台并发

1. foreground `cd src && pwd` 仍更新 session cwd。
2. background `cd src && sleep ...` 不更新 session cwd。
3. background 进程退出时不覆盖前台命令刚更新的 cwd。
4. background 不触碰 cwd readback 文件/机制；foreground readback scoped to run/process，不竞争 backend-wide `_cwd_file`。

### 12.3 Timeout / process tree

1. foreground `timeout_seconds` 是进程寿命上限：超时保留 partial output。
2. foreground `timeout_seconds` 超时杀 shell child/grandchild。
3. foreground `timeout_seconds` 超时不更新 stale cwd。
4. `background=true + timeout_seconds` 在 PR1 返回结构化 `unsupported_argument`，不启动进程，避免把 timeout 误解为后台寿命看门狗。
5. `terminal_process(wait, timeout_seconds=N)` 的 timeout 只是等待上限：wait 返回 running/partial snapshot，进程继续存活，可被后续 wait/kill。
6. 未来若加入后台寿命上限，必须使用显式字段如 `max_lifetime_seconds`，不能复用 wait timeout。

### 12.4 stdin

1. pipe stdin write。
2. submit newline。
3. close stdin / EOF。
4. finished process write 报错。
5. stdin 操作不会破坏 output accumulator。

### 12.5 PTY

1. Python REPL。
2. command requiring TTY。
3. PTY output decoding。
4. PTY kill。
5. known pipe-stdin command disables PTY。

### 12.6 Output

1. ANSI stripping。
2. secret redaction。
3. binary output sanitation。
4. large output truncation。
5. full output artifact。
6. streaming chunks before process completion。
7. secret 跨 chunk 边界仍会被 redacted：例如 reader 收到 `API_KEY=ab` 和 `cd123` 两段，snapshot/artifact/event 都不得出现明文。
8. accumulator 在 reader thread 与 poll/wait 并发访问时不会数据竞争或抛异常。

### 12.7 Policy

1. workspace escape blocked。
2. empty command blocked。
3. invalid timeout blocked。
4. long-running foreground suggests background。
5. long-running background allowed：`background=true` 时 `npm run dev` / `vite` / `uvicorn` 类命令不被旧 guard 拦截。
6. shell-level background wrapper warning。
7. dangerous command confirmation path。
8. v1 收到 `notify_on_complete=true` 返回结构化 `not_supported_yet`，不得静默忽略或假装支持。

## 13. 迁移策略

对现有 `terminal` 工具保持兼容：

- 不传 `background` 时仍按 foreground 工作。
- 不传 `tty` 时仍 pipe mode。
- `session_id` 可保留别名，但内部统一为 `terminal_session_id`。
- 返回 JSON 增加字段不破坏旧调用。
- 旧 `TerminalStatus.SUCCESS/ERROR/TIMEOUT/BLOCKED` 可映射到新 `TerminalRunStatus`。

## 14. 风险与取舍

### 14.1 最大风险：一次 PR 改太多

terminal runtime、streaming tool protocol、PTY、policy 都是承重件。必须分 PR。

但 PR1 不能被低估。PR1 不是「加一个 background 参数」，而是必须同时立起：

1. process registry。
2. per-process reader drain。
3. 线程安全 accumulator。
4. cwd doctrine。
5. long-running background 放行。
6. 共享 registry 接线。
7. shutdown cleanup。
8. line-buffered redaction 边界。
9. process limit 与 finished retention 边界。
10. foreground run 与 background registry 的归属边界。
11. timeout 三种语义的分流。
12. background initial output 短窗口契约。

少任何一项都会出现能力不可达、死锁或安全漏口。

### 14.1.1 后台输出死锁

没有持续 drain 时，高吞吐后台进程会填满 stdout/stderr pipe buffer，然后阻塞在写输出上。`sleep` 测试无法覆盖这个风险。PR1 必须用高吞吐输出测试验证 reader thread / drain task。

### 14.2 PTY 依赖风险

Python stdlib `pty` 只适合 POSIX；Windows 需要 winpty/pywinpty。v1 可明确只支持 POSIX PTY，Windows 后续。

### 14.3 输出保存的隐私边界

完整输出 artifact 是否 redacted 必须钉死。建议 v1 保存 redacted full output；如果未来需要 raw output，必须带更强权限和安全说明。

redaction 不能只在 final string 上做；一旦 reader 按 chunk 工作，secret 可能跨 chunk 边界。v1 采用 line-buffered redaction：完整行才能进入 accumulator/artifact/event，未完成行只留在私有 pending buffer。

### 14.4 Shell init 的副作用

source 用户 rc 文件可能有输出、阻塞、prompt 逻辑。v1 支持显式 login shell，但默认不启用；未来如果启用 shell init，也必须 best-effort，失败不阻塞 terminal。

### 14.5 Policy 误判

命令危险性识别永远不完美。v1 policy 应偏保守：可提示、可请求确认，但不要把复杂语义安全完全交给 regex。

### 14.6 Registry 所有权错误

如果 `terminal` 和 `terminal_process` 各自构造 `TerminalSessionManager`，`poll/kill` 会找不到 `terminal` 刚启动的进程。process registry 的所有权必须上移到 runtime/session/wiring，并注入两个工具。进程管理启用后，缺少共享 manager 应 fail closed。

### 14.7 后台 orphan

managed background process 不能只在内存里登记。`RuntimeSession.close()` / driver teardown 时必须 best-effort 终止 tracked process groups。完整 crash recovery 可以后续做，但 PR1 至少要有 `registry.shutdown()` 并接到 session close，避免真正 session 退出后留下 orphan 服务器。注意：close 不能绑在每轮 `AgentRuntime.run_task()` 的 FINISHED 上，否则 background 进程无法跨用户消息存活，finished retention 也会失去意义。

### 14.8 Foreground 污染 registry

foreground 虽然复用 managed process runner，但它是同步 tool call 的内部资源，不是可跨 turn 管理的 background process。若 foreground 进入 live limit 或 finished retention，短命令会误触发配额，或把真正的 background 记录挤掉。PR1 必须让 foreground 不返回 pollable `process_id`，不计入 live background limit，不进入 finished retention。

### 14.9 Timeout 语义混用

同名 `timeout_seconds` 在不同入口上容易被误解。PR1 的硬边界是：foreground terminal timeout 杀进程；`terminal_process(wait)` timeout 只停止等待，且缺省使用有限默认值而不是无限等待；background 启动不接受 `timeout_seconds` 作为寿命上限。未来如需后台 watchdog，应新增显式字段，不复用 wait timeout。

### 14.10 Retention 并发边界

PR1 的 finished retention 采用惰性驱逐，只在 registry 访问路径里清理。reader thread 不修改 registry map，只更新自己的 `TerminalProcessState`。如果未来增加定时清理线程或并行 tool execution，再引入 registry 级集合锁。

## 15. 推荐下一步

从 **PR1：Process registry + background lifecycle** 开始，但按本文修订后的真实范围理解 PR1。

原因：

1. 它是 terminal 从 command runner 到 execution runtime 的最小跨越。
2. 它不依赖 PTY。
3. 它能立刻支持 dev server、watcher、长测试。
4. 它为 streaming、stdin、PTY、policy 都打下同一个 process 模型。

PR1 完成后，再做 stdin 和 PTY。不要先做 Docker/SSH backend；那些都依赖 process lifecycle 的边界先稳定。Shell environment baseline 默认仍排在后面，但如果 PR1/PR2 本地 smoke 证明 `uv`、用户 PATH 或 `proxy_on` 不可见影响真实可用性，应提前处理。
