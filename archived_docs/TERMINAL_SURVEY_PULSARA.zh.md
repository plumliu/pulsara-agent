# Pulsara Terminal Capability Survey

本文记录 Pulsara 当前 terminal 工具实现，作为后续对比 Claude Code、Codex、Hermes、OpenClaw、Anybox 的基线。

## 1. 工具面

Pulsara 当前有两个模型可见 terminal 工具：

- `terminal`：执行 shell command。
- `terminal_process`：对 yielded process 做 `poll` / `wait` / `kill` / `write` / `submit` / `close_stdin`。

`terminal` 的关键参数：

- `command`：shell command 字符串，必填。
- `workdir`：workspace 内工作目录；相对路径从 workspace root 解析。
- `terminal_session_id`：默认 `default`，用于维护每个 session 的 current cwd。
- `yield_time_ms`：等待窗口；超时后不杀进程，而是返回 `process_id`。
- `tty`：是否分配 POSIX PTY。
- `max_output_chars`：输出上限。

`terminal_process` 的关键能力：

- `poll` / `wait`：读取 yielded process 当前/最终输出。
- `kill`：终止进程组。
- `write` / `submit`：向 pipe 或 PTY 写 stdin；`submit` 追加换行。
- `close_stdin`：发送 EOF。

## 2. 执行模型

Pulsara 通过 `TerminalSessionManager` 管理 workspace 级 terminal 状态：

- 每个 workspace supervisor 持有一个 `TerminalSessionManager`。
- 每个 host session 通过 `owner_host_session_id` 绑定 terminal session 和 yielded process。
- 同一 workspace 可以有多个 host session，但 process ownership 会限制跨 session 操作。
- session 关闭时会 `kill_owned(owner_host_session_id)`。
- workspace 关闭时会 shutdown 整个 supervisor。

进程执行在本地主机 shell 中：

- 默认 backend 是 `local`。
- command 经 shell config 包装后由 `subprocess.Popen` 执行。
- pipe 模式合并 stdout/stderr。
- PTY 模式通过 `pty.openpty()` 支持交互式程序。
- 使用 `os.setsid` 创建进程组，`kill`/timeout watchdog 可终止进程组。

`yield_time_ms` 是核心长程机制：

- 若命令在窗口内结束，返回最终结果。
- 若命令仍在运行，返回 `TerminalStatus.RUNNING` 和 `process_id`。
- yielded process 被登记进 `ProcessRegistry`，后续由 `terminal_process` 接管。
- 默认最多 8 个 live process、32 个 finished process，finished TTL 为 1 小时。

## 3. CWD 与 workspace guard

Pulsara terminal 是 host shell，不是文件系统沙箱，但有 workspace 级 cwd guard：

- `workdir` 必须解析在 workspace root 内。
- 默认工作目录使用 terminal session 的 `current_cwd`。
- 命令结束后通过 wrapped command 捕获最终 cwd。
- 若命令最终 cwd 仍在 workspace 内，则更新 session current cwd。
- 若命令结束在 workspace 外，结果标记为 `blocked`，且不更新 current cwd。
- 若 current cwd 被删除，会回退到存在的 workspace ancestor。

这不是安全边界：命令仍可通过 shell 访问 host 文件系统。它更像“默认落点和状态一致性” guard。

## 4. 环境构造

Pulsara 的 terminal env 不是直接继承父进程全量环境，而是做了较细的构造：

- 基础 allowlist：`HOME`、`USER`、`SHELL`、`PATH`、locale、display、proxy 等。
- toolchain allowlist：`NVM_DIR`、`VOLTA_HOME`、`CARGO_HOME`、`PYENV_ROOT`、`HOMEBREW_*`、`GOPATH` 等。
- 默认移除 provider/API secret，例如 `PULSARA_API_KEY`、`OPENAI_API_KEY` 不会自动继承。
- 可通过 `PULSARA_TERMINAL_ENV_INHERIT_ALLOWLIST` 和 `PULSARA_TERMINAL_ENV_PASSTHROUGH_NAMES` 放开。
- 支持 login shell env snapshot，默认开启，TTL 300 秒。
- PATH 合并顺序包含：
  - nearest `.venv/bin` overlay；
  - `PULSARA_TERMINAL_EXTRA_PATH_PREPENDS`；
  - shell snapshot PATH；
  - sanitized parent PATH；
  - sane fallback PATH。
- `PULSARA_TERMINAL_VENV_OVERLAY=0/1` 控制 venv overlay。

这是 Pulsara 当前 terminal 的明显强项：既能贴近用户 shell，又尽量避免把 LLM provider secrets 暴露给命令。

## 5. 输出处理

输出通过 `OutputAccumulator` 处理：

- UTF-8 incremental decode，错误替换。
- strip ANSI escape。
- 简单 secret redaction：
  - `KEY/TOKEN/SECRET/PASSWORD=...`
  - bearer token。
- head/tail 截断。
- 超过阈值后写入 `.pulsara/terminal-output/<process_id>.txt`。
- tool result 返回 `full_output_ref`。
- streaming 模式会逐步输出 JSON `"output"` 字段片段。

风险和限制：

- redaction 是 pattern-based，不是完整 DLP。
- full output artifact 当前落在 workspace `.pulsara/terminal-output`，它本身也应被视为敏感输出。
- 目前没有按 stdout/stderr 分流，pipe 模式合并 stderr 到 stdout。

## 6. 权限与审批

Pulsara terminal 权限由三轴 policy 控制：

- `permission_profile`：`trusted_host` / `workspace_guarded` / `read_only`。
- `approval_policy`：`never` / `risky_only` / `on_request`。
- `terminal_access`：`off` / `allow` / `ask`。

默认：

- project run：`trusted_host + risky_only + allow`。
- inspect：`read_only + on_request + off`。
- transient/unknown：`read_only + on_request + off`。

工具注册层：

- `read_only` 会隐藏 `terminal`、`terminal_process`、写文件工具。
- `terminal=off` 会隐藏 `terminal` 和 `terminal_process`。

gate 层：

- hardline terminal command 永远 `DENY`，即使 `approval_policy=never`。
- `terminal_access=ask` 对 `terminal` 和 `terminal_process` 都要求用户确认。
- `approval_policy=on_request` 对 terminal 和文件写入要求确认。
- `approval_policy=risky_only` 对 risky/sensitive terminal command 要求确认。
- `terminal_process.write/submit` 的 stdin 内容也会走 hardline 检查，避免交互 shell 绕过灾难命令底线。

hardline 覆盖：

- `rm -rf /`、`/*`、`~`、`$HOME`、`/home`、`/etc`、`/usr`、`/var`、`/bin` 等根级删除。
- `dd of=/dev/disk|sd|nvme|vd|mmcblk|hd...`。
- `mkfs`、`shutdown`、`reboot`。

risky 覆盖：

- `rm -rf`、`sudo`、`chmod -R`、`chown -R`、`dd of=...`、`ssh-keygen`。
- sensitive path，如 `.env`、`.ssh`、shell rc、`.npmrc` 等。

## 7. Stop / approval / lifecycle

Pulsara 已支持：

- pending approval：`RequireUserConfirmEvent`。
- approval resume：`resolve_approval()` 后继续同一 run。
- deny：向模型返回 denied tool result，同一 run 继续。
- suspended approval stop：停止 pending approval，不等价于 deny，pending tool 不执行。
- active run soft stop：设置 run `aborted`，注入 interrupted note 到下一轮 prior context。
- failed run note：failed 后下一轮注入 failure note。

terminal 与 stop 的关系：

- active run stop 是 soft cancel，不承诺强杀已经 yielded 的后台进程。
- host session close 会 kill 当前 host session owned process。
- workspace close/shutdown 会 kill workspace supervisor 管理的 process。

## 8. 当前强项

Pulsara 的 terminal 能力已经不只是“执行一条 shell 命令”：

- 有长程命令 yield + process handle。
- 有后续 process 操作，包括 stdin write/submit。
- 支持 pipe 和 PTY。
- 有 workspace cwd 状态追踪。
- 有 owner session 隔离和 workspace supervisor。
- 有 env sanitization、shell snapshot、venv overlay。
- 有输出 redaction、截断、full output artifact。
- 有三轴权限、hardline deny、risky/on-request approval、approval resume、stop。
- inspect 能诚实展示 host shell / non-isolated 语义。

这些已经覆盖了许多成熟 agent terminal 的核心能力。

## 9. 当前短板 / 后续对比重点

这些不是一定要立刻实现，但应在外部项目调研时重点对比。

### 9.1 Terminal 输出与进程状态 UI 还偏底层

当前结果主要是 JSON payload。缺少更产品化的：

- 命令时间轴。
- stdout/stderr 分流。
- exit summary。
- process list / live process dashboard。
- terminal transcript viewer。
- 一键 resume / kill / follow。

### 9.2 `terminal_process` 审批粒度偏粗

`terminal_access=ask` 会 gate 所有 `terminal_process` action，包括只读 `poll` / `wait`。这对安全保守，但真实交互会很烦。

后续可以考虑：

- `poll` / `wait` 默认 allow。
- `write` / `submit` / `kill` 才 ask。
- 或按 process origin 继承 approval：已批准命令产生的 process，其 read-only action 自动 allow。

### 9.3 没有 durable terminal process registry

Process registry 是内存态：

- app 重启后 process handle 丢失。
- full output artifact 保留，但 process ownership 和状态不可恢复。
- session timeline 可回放 event log，但不能重新 attach live process。

如果目标是 universal desktop agent，长程任务可能需要：

- durable process metadata。
- app 重启后的 orphan detection。
- workspace supervisor 启动时扫描/提示历史 process。

### 9.4 Shell command 是字符串，不是结构化 argv

当前模型传入 shell command string。优点是万能；缺点是：

- quote/escape 风险高。
- policy 只能正则匹配。
- 难以做精确命令 diff/approval preview。

可考虑保留 shell string 作为 full-access 主路径，同时提供可选结构化 command schema：

- `argv: list[str]`；
- `env_overrides`；
- `stdin`；
- `timeout_policy`；
- `expected_output`。

### 9.5 `max_lifetime_seconds` 存在但不是模型可用参数

`TerminalRequest` 和 process watchdog 支持 `max_lifetime_seconds`，但 `terminal` 工具显式拒绝模型传入该参数，提示它是 runtime-only。

后续若要让 agent 安全跑 dev server / watcher，可考虑暴露受限版本：

- `max_lifetime_seconds` 上限；
- `auto_kill_on_session_close`；
- `keep_alive` / `daemon` 显式语义。

### 9.6 缺少命令级资源限制

当前主要限制：

- live process 数量。
- output chars。
- wait/yield 时间。
- optional lifetime watchdog。

尚未看到：

- CPU / memory / file descriptor 限制。
- network mode。
- per-command max output bytes at OS pipe 层。
- process tree resource accounting。

这与“full access 是一等公民”不冲突，但需要在 inspect 中诚实展示。

### 9.7 Secret redaction 仍是轻量级

当前 output redaction 和 env sanitization 已有基础，但仍可补：

- 更广泛 secret detector。
- full output artifact 同步 redaction / encryption / access warning。
- command string 中 secret 的 redaction。
- approval preview 中 secret masking。

### 9.8 Terminal 工作区隔离不是安全沙箱

Pulsara 文档和 inspect 已经比较诚实：terminal 是 host shell。后续产品 UX 需要持续避免用户误解为安全沙箱。

## 10. 对外部项目调研时的比较维度

后续每个项目文档都按这些维度对比：

- 工具 API：单工具还是多工具；是否有 process handle。
- I/O：pipe、PTY、stdin、streaming、stdout/stderr 分流。
- 长程任务：timeout、background/yield、poll/wait/kill、持久化。
- CWD/session：是否有 session cwd、workspace root、跨 turn 状态。
- 环境：继承策略、shell init、venv/toolchain 处理、secret 防泄漏。
- 权限：approval、risk classification、hardline deny、bypass mode。
- Stop/cancel：是否取消模型、是否杀进程、是否清理 orphan。
- 输出：截断、artifact、redaction、UI timeline。
- 产品体验：是否鼓励 terminal 为主路径，是否有 ergonomic affordances。
