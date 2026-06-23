# Anybox Terminal Capability Survey

本文记录本地 `/Users/plumliu/Desktop/python_workspace/anybox` 中 terminal / shell / PTY 相关能力，并与 Pulsara 当前实现比较。Anybox 是个人项目，成熟度和 Claude Code、Codex、Hermes、OpenClaw 不在同一个量级；但它有一个很值得看重的方向：把“会话里的真实终端”作为桌面产品的一等界面，而不只是 agent 的一次性命令工具。

## 1. 工具面

Anybox 有两套 terminal 路径。

第一套是绑定到 main session 的 persistent PTY：

- `terminal_run_command`：在该 session 的持久终端中执行命令。
- `terminal_read`：读取该终端最近 buffer。
- `terminal_write_input`：向该终端写原始输入。

`terminal_run_command` 的参数：

- `command`：命令字符串。
- `timeoutMs`：默认 60 秒，最大 10 分钟。
- `maxOutputChars`：默认 12000，最大 200000。

第二套是 cross-platform shell command：

- `git_bash_command`。
- `macos_shell_command`。
- `powershell_command`。
- `cmd_command`。
- `wsl_bash_command`。

这些工具有统一参数：

- `command`。
- `workdir`。
- `timeoutMs`。
- `maxOutputChars`。
- `allowUnsafe`。
- `description`。
- `runInBackground` / `run_in_background`，只在 Git Bash 和 macOS shell 上支持。
- `distro`，仅 WSL tool 支持。

后台任务另有：

- `read_background_task`：读状态和 buffered output。
- `stop_background_task`：终止后台任务。

与 Pulsara 对齐：

- Anybox persistent PTY 更接近“桌面终端面板”。
- Anybox shell command 更接近 Pulsara `terminal` 的非交互命令路径。
- Anybox background task 更接近 Pulsara yielded `terminal_process`，但 action 面更窄。

## 2. Persistent PTY 模型

Anybox 的 persistent PTY 由 `PtyRegistry` 和 `ManagedPtySession` 管理：

- 每个 main session 最多复用一个 PTY。
- side chat session 明确不支持 terminal。
- PTY 通过 `node-pty` 启动。
- 默认 rows/cols 为 32/120。
- buffer 默认保留 200000 字符。
- exited session 保留 5 分钟，deleted session 保留 15 秒。
- `replay(cursor)` 支持 delta/reset 读取。
- `write(data)` 直接写入 PTY。

`terminal_run_command` 在持久 shell 中执行命令的方式很直接：

1. 生成一个唯一 marker。
2. 把用户命令写入 PTY。
3. 命令后追加 `printf` / `Write-Output` / `echo` marker。
4. 订阅 PTY output，直到看到 marker 或 timeout。
5. 返回 marker 前的 output。

这和 Pulsara 的模型不同：

- Pulsara 每次 `terminal` 调用启动一个新的 subprocess，靠 terminal session cwd 状态模拟 shell continuity。
- Anybox 真的有一个长期 shell，因此 shell state、环境变量、alias、后台 job 等会自然留在同一个 PTY 里。

Anybox 的优点：

- 桌面终端体验自然，用户和 agent 看到的是同一个 live terminal。
- 适合 REPL / TUI / 交互命令。
- cursor replay 与 websocket 推送直接服务 UI。

Anybox 的风险：

- marker detection 可能被命令输出伪造或干扰。
- 持久 shell state 强大但更难审计。
- 命令完成检测依赖 shell payload 拼接，对复杂 shell 状态更脆弱。

Pulsara 当前的 per-command subprocess 更可审计、更容易做 output artifact 和 owner isolation；Anybox 的 persistent PTY 更适合桌面产品的“真实终端”体验。

## 3. Session 队列与并发

Anybox 对 `terminal_run_command` 做了 session 级队列：

- `commandQueues` 保证同一 session 的 run command 串行。
- `activeRunCommands` 记录正在跑的 run command。
- 当 `terminal_run_command` 活跃时，`terminal_write_input` 会被 validate 拒绝。

这个设计避免了两个常见问题：

- 两条命令同时写进同一个 persistent shell，output 混成一团。
- 模型一边用 run command 等 marker，一边又 raw input 干扰 marker 检测。

Pulsara 当前是不同的并发模型：

- 每次 `terminal` 调用有自己的 subprocess。
- yielded process 后续通过 `process_id` 交互。
- 同一个 host session 的 process ownership 清楚，但没有“单一真实 PTY shell”的队列问题。

Anybox 给 Pulsara 的启发不是必须改成 persistent PTY，而是如果 Pulsara 未来做桌面 terminal panel，应明确区分：

- agent 启动的一次性 terminal task。
- 用户/agent 共用的 persistent terminal。
- 两者的并发和输入规则不能混在一起。

## 4. Cross-platform Shell Command

Anybox 的 shell-command 工具覆盖多平台：

- Git Bash/MSYS Bash。
- macOS zsh/POSIX shell。
- Windows PowerShell。
- Windows Command Prompt。
- WSL Bash。

它会解析可执行路径：

- Git Bash 从配置、`git.exe` 位置、常见 Windows 路径查找。
- macOS shell 从 `ANYBOX_MACOS_SHELL`、`SHELL`、系统 shell、PATH 查找。
- PowerShell、CMD、WSL 都有各自 fallback。

foreground 执行：

- stdout/stderr 分开收集。
- 每路默认最多 12000 字符。
- timeout 时 kill process tree。
- abort 时 kill process tree。
- 返回 text 与 JSON model output，包含 command、workdir、shell、exitCode、signal、timedOut、aborted、stdout/stderr truncation。

Pulsara 目前主要面向 POSIX/macOS host shell，跨 Windows shell 的工具面没有 Anybox 细。若 Pulsara 要做 universal desktop agent，多平台 shell surface 是一条迟早要补的路，但不一定应在当前 macOS 优先阶段做。

## 5. Background Task

Anybox 的 `runInBackground` 只在 Git Bash 和 macOS shell command 上开启。

后台任务由 `ShellTaskRegistry` 管理：

- task id 使用 descending id。
- 每个 task 记录 title、command、cwd、shell、status、exitCode、signal、createdAt、updatedAt、cursor。
- output 用 `PtyBuffer` 存，默认 200000 字符。
- stdout/stderr 合并进同一个 output stream。
- exited task 默认 5 分钟后 prune。
- deleted task 默认 15 秒后 prune。
- `read_background_task` 支持 cursor delta/reset。
- `stop_background_task` kill process tree 并标记 deleted。

与 Pulsara 对比：

- Pulsara yielded process 是 terminal 主工具自然产生的结果，`terminal_process` 有 `poll/wait/kill/write/submit/EOF`。
- Anybox background task 更像“非交互 shell task”，只有 read/stop，没有 stdin 交互。
- Anybox background task text 明确提示模型使用 `read_background_task` 和 `stop_background_task`，这比 Pulsara 当前 JSON-ish `process_id` 更产品化一点。

Anybox 缺少：

- background completion notification / wake。
- durable process registry。
- watch patterns。
- rich task list。
- process stdin。

所以 Anybox 的 background task 比 Pulsara 的底层能力更窄，但其 user-facing 文案和 cursor read 机制值得借鉴。

## 6. Desktop PTY Bridge

Anybox 有比较清楚的 desktop PTY bridge：

- agent server 提供 `/api/pty/:id` 查询。
- `/api/pty/:id/connect` websocket 连接 live PTY。
- websocket open 时发送 `ready`，包含 session info 和 replay。
- 后续发送 `output`、`state`、`exited`、`deleted`。
- client 可发送 `input` 写入 PTY。
- Electron main 的 `PtyProxyManager` 把 renderer IPC 转发到 agent websocket。
- websocket 连接中断/重连期间，可缓存最多 100000 字符 pending input。
- renderer destroyed 时自动 detach。

这是 Anybox 最值得 Pulsara 看的一点。Pulsara 目前 terminal 能力主要是 host/runtime/tool 层；如果目标是 universal desktop agent，最终也需要一条类似的 UI transport：

- terminal task / PTY session 可 attach。
- output 是 live stream，不只是 tool result。
- reconnect 能 replay cursor。
- UI detach 不等价于 kill。
- 输入在 connecting 期间要么 queue，要么明确拒绝。

## 7. CWD 与边界

Anybox PTY 的 cwd 通过 `resolveAllowedCwd()` 限制：

- 解析 cwd。
- 通过 project + sandbox 找到 allowed roots。
- cwd 必须位于 workspace roots 或 sandbox 内。

shell-command 工具的 `workdir` 也通过 `resolveToolPath()` 解析，并验证目录存在。

这和 Pulsara 的 workspace cwd guard 类似：它主要保证默认落点和 UI 状态一致，不是完整 sandbox。只要 shell command 能运行，进程仍有 host 能力。Anybox 的 `shell-command` 描述里写“inside current project boundary”，但从实现看，它更像 workdir/path 约束，不应被理解成强 OS sandbox。

Pulsara 这点更诚实：inspect 明确显示 host shell / no fs sandbox / network not isolated。

## 8. 环境

Anybox PTY env 走 `buildPtyEnvironment()`：

- allowlist 常见终端相关变量。
- macOS shell command 会补系统默认 PATH。
- shell resolution 覆盖 macOS、Windows、WSL、Git Bash。

Pulsara 的 env sanitization 更深入：

- 明确移除 provider/API secret。
- 支持 login shell snapshot。
- 支持 `.venv` overlay。
- 支持可配置 allowlist/passthrough。
- 对 Python/Node/Rust/Homebrew/pyenv 等 toolchain 路径处理更细。

Anybox 的多平台 shell discovery 值得借鉴；Pulsara 的 env 安全和本地开发体验目前更强。

## 9. 权限模型

Anybox 每个 tool 可以提供 `assessPermission()`：

- persistent `terminal_run_command` 总是 `ask`，critical command 风险为 critical，其他为 medium。
- `terminal_write_input` 总是 `ask`，risk medium。
- shell-command 工具调用 `assessShellPermission()`。

`assessShellPermission()` 的规则：

- critical shell command 或 `curl/wget/iwr | sh/bash/iex` 网络执行：`deny`，risk critical。
- read-only command：`allow`，risk low。
- write-like command：`allow`，risk low。
- 其他 unknown：`ask`，risk medium。

schema 层：

- permission request 有 `pending/approved/denied/expired`。
- risk 有 `low/medium/high/critical`。
- action 有 `allow/deny/ask`。
- request/audit 写 SQLite。
- decision 支持把 `allow-once/session/project/forever` 预处理成 `allow`。

和 Pulsara 相比：

- Pulsara 三轴 policy 更明确：profile / approval / terminal access。
- Pulsara 的 `ask/on_request` 已经有 resume/deny/stop 语义。
- Pulsara hardline 是不可覆盖的 deny；Anybox shell-command 的 critical 权限与 `allowUnsafe` / 全局 permission mode 的交互更分散，需要读更大系统才能完全确认。
- Anybox 的 persisted permission request/audit 比 Pulsara 当前 host in-memory approval 更偏产品化。

Anybox 给 Pulsara 的启发：

- approval/prompt/audit 可以持久化，便于桌面 UI 展示历史。
- “read-only command allow、unknown ask、critical deny”这类 heuristic 对 shell-command UX 很实用。
- 但 full-access/bypass 作为一等公民时，Pulsara 应保持 hardline 极少数不可覆盖，其他风险交给用户配置。

## 10. Stop / Abort / Kill

Anybox foreground shell command：

- timeout 会 `terminateProcessTree(proc)`。
- abort signal 会 `terminateProcessTree(proc)`。

Anybox persistent PTY `terminal_run_command`：

- timeout/abort 时向 PTY 写 Ctrl-C，然后返回 timedOut=true。
- 不删除 PTY session。

Anybox background task：

- `stop_background_task` 调用 registry stop。
- stop 会把 task 标成 deleted，kill process tree，短期保留后 prune。

Pulsara 当前：

- active run stop 是 soft cancel，不承诺杀 yielded process。
- `terminal_process.kill` 可显式杀。
- session close / workspace close 会清理 owned process。

Anybox 的 foreground/PTY stop 语义更接近用户直觉：当前命令 timeout/abort 会直接 Ctrl-C 或 kill。Pulsara 可以后续考虑把“stop run”和“kill/interrupt active terminal call”拆成两个可选按钮，而不是只保留 soft cancel。

## 11. Anybox 强项

Anybox 对 Pulsara 有价值的点：

- persistent PTY session 是桌面产品一等对象。
- websocket bridge 支持 ready/replay/output/state/exited/deleted。
- cursor replay 机制简单实用。
- raw terminal input 可以经 desktop proxy 直达 PTY。
- shell-command 工具覆盖 macOS、Git Bash、PowerShell、CMD、WSL。
- foreground stdout/stderr 分流。
- background task 有 read/stop 和 cursor。
- permission request/audit 有 SQLite 持久化。

这些强项更偏产品交互层，而不是 agent runtime 深层安全/恢复。

## 12. Anybox 短板

结合“个人项目”的先验，这些短板不意外：

- background task 没有 completion wake / system event。
- 没有 Hermes 式 durable process checkpoint。
- 没有 OpenClaw 式 sandbox/elevated/target 分层。
- persistent PTY marker completion 可能被输出扰动。
- shell-command background 和 persistent PTY 是两套并行体系，任务模型不统一。
- background task 没有 stdin/write/paste/send-keys。
- permission/sandbox 边界不像成熟产品那样完整。
- 没有 watch pattern、stall detection、native approval 等高级能力。

## 13. Pulsara 可借鉴点

### P0：为桌面端设计 terminal transport

Anybox 的 websocket PTY bridge 很适合作为 Pulsara 未来 UI 参考：

- task/session attach。
- cursor replay。
- live output。
- UI detach。
- input forwarding。
- reconnect 后恢复 replay。

这和 Pulsara 当前 `terminal_process.poll` 不冲突；一个是模型工具，一个是产品 UI transport。

### P1：明确区分 persistent terminal 与 task terminal

Pulsara 当前 `terminal_session_id` 管 cwd，不是真 persistent shell。未来如果加桌面 terminal panel，应把它作为另一种资源：

- persistent PTY：面向用户+agent共用。
- terminal task：面向 agent 命令执行和审计。
- 两者可以互相引用，但不要混用生命周期。

### P1：后台任务读写协议更友好

Anybox 的 `read_background_task` 返回 cursor、mode、outputTruncated、status。Pulsara 可以让 `terminal_process.poll/wait` 更接近这个结构：

- cursor / start cursor。
- delta vs reset。
- status / exit / signal。
- output truncated reason。
- display cwd。

### P2：多平台 shell tool surface

Pulsara 做 universal desktop agent 时，迟早需要 Windows：

- PowerShell。
- CMD。
- WSL。
- Git Bash。

Anybox 的 resolver 不是企业级完整方案，但足够说明这些应该被显式建模，而不是都塞进一个 POSIX shell 字符串。

### P2：持久化 approval request/audit

Pulsara 已有 approval resume，但 approval 本身还不是 durable UI object。Anybox 的 SQLite request/audit schema 可以启发 Pulsara：

- pending approval 可持久展示。
- approved/denied/expired 可审计。
- prompt view 和 runtime input 分开。
- risk/resource/paths/command 都进入记录。

## 14. 结论

Anybox 对 Pulsara 最有价值的不是安全模型，也不是后台任务成熟度，而是“桌面真实终端”的产品形态：persistent PTY、websocket replay、renderer proxy、raw input。

Pulsara 当前底层 agent terminal 更强：yielded process、stdin、权限三轴、approval resume、stop note、env sanitization 都更完整。下一步若面向 universal desktop agent，Anybox 提醒我们：除了 agent 工具 API，还需要一个用户能看、能接管、能恢复的 live terminal transport。
