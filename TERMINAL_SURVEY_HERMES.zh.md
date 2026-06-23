# Hermes Terminal Capability Survey

本文记录本地 `/Users/plumliu/Desktop/python_workspace/hermes-agent` 中 terminal / process 相关实现，并和 Pulsara 当前 terminal 能力做对比。Hermes 是成熟度很高的本地/远程 agent 产品，它的 terminal 不只是 shell 执行器，更像一个跨 backend 的任务运行时。

## 1. 工具面

Hermes 暴露两个主要工具：

- `terminal`：执行 foreground 或 background command。
- `process`：管理 `terminal(background=true)` 启动的后台进程。

`terminal` 的模型参数：

- `command`：shell command 字符串。
- `background`：是否后台运行。
- `timeout`：foreground 等待秒数。
- `workdir`：命令工作目录。
- `pty`：本地/SSH backend 可用的伪终端模式。
- `notify_on_complete`：后台进程退出时自动通知。
- `watch_patterns`：后台输出匹配特定字符串时通知，带强限流。

`process` 的模型参数：

- `action`：`list` / `poll` / `log` / `wait` / `kill` / `write` / `submit` / `close`。
- `session_id`：后台进程 id。
- `data`：写入 stdin 的文本。
- `timeout`：`wait` 最长等待时间。
- `offset` / `limit`：`log` 分页。

和 Pulsara 对齐：

- Hermes `terminal` 约等于 Pulsara `terminal`。
- Hermes `process` 约等于 Pulsara `terminal_process`。
- Hermes 的 action 面更贴近“后台任务管理”，Pulsara 更贴近“yielded process handle”。

## 2. 多 Backend 执行模型

Hermes terminal 的最大差异是 backend 抽象。`tools/terminal_tool.py` 声明并创建多种 execution environment：

- `local`：本地主机 shell。
- `docker`：Docker container。
- `singularity` / Apptainer。
- `ssh`：远端 SSH host。
- `modal`：Modal cloud sandbox。
- `daytona`：Daytona sandbox。

所有 backend 共享 `BaseEnvironment.execute()` 形状：

1. backend 初始化时捕获一次 shell snapshot。
2. 每次命令 spawn 一个新的 bash process。
3. 执行前恢复 snapshot。
4. command wrapper 在输出里嵌入 CWD marker 或写 CWD temp file。
5. `_wait_for_process()` 负责 drain stdout、检查 timeout、检查 interrupt、更新活动心跳。
6. 执行结束后从 marker / temp file 更新 session cwd。

Pulsara 当前是单一 host-shell backend。它有 workspace supervisor、PTY、process ownership 和 env sanitization，但没有把 terminal 抽象成 local / container / remote / cloud sandbox 多 backend。

## 3. Foreground 行为

Hermes foreground command 有几条产品化 guard：

- foreground timeout 有硬上限 `TERMINAL_MAX_FOREGROUND_TIMEOUT`，超过会要求改用 `background=true`。
- 对疑似 server / watcher / `nohup` / `disown` / trailing `&` 的 foreground 命令给出错误和指导，要求使用托管 background process。
- 对 `A && B &` 这类 bash 解析会产生 subshell wait 的形状做 rewrite，避免 foreground 被后台 grandchild stdout pipe 卡死。
- `_wait_for_process()` 使用 `select()` drain stdout，避免 `for line in proc.stdout` 因 orphaned pipe 永久卡住。
- interrupt 时杀掉 process group 并返回 `returncode=130`。
- timeout 时杀掉 process group 并返回 `returncode=124`。
- 有 activity heartbeat，避免 gateway 误判长命令无响应。

Pulsara 当前有 `yield_time_ms` 和 watchdog/kill group，但缺少 Hermes 这种“引导模型用 background 而非 shell hack”的工具说明和 runtime guard。

## 4. Background Process Registry

Hermes 的 `ProcessRegistry` 是 terminal 成熟度的核心。它维护：

- running / finished process 字典。
- 每个 process 的 `session_id`、command、task_id、session_key、pid、cwd、started_at。
- rolling output buffer，默认 200KB。
- finished TTL，默认 30 分钟。
- max process 数，默认 64。
- completion queue。
- watch-pattern queue。
- checkpoint file：`~/.hermes/processes.json`。

本地 backend 的后台进程：

- 使用用户 login shell。
- 可用 PTY。
- stdout reader thread 持续读 output。
- `PYTHONUNBUFFERED=1` 让 Python progress 更容易被捕获。
- POSIX 下用 `setsid`，kill 时尽量杀 process tree。

非本地 backend 的后台进程：

- 通过 `env.execute()` 在 sandbox 内启动 `nohup bash -lc ...`。
- 输出写 sandbox 内 log 文件。
- PID / exit code 写 sandbox 内 sidecar 文件。
- registry poller thread 周期性读 log 和 exit marker。
- 不能完整支持 stdin；`process.write/submit/close` 对非本地 backend 会报 stdin 不可用。

Pulsara 当前也有 registry，但更轻：

- live / finished process、owner host session、TTL、full output artifact。
- 没有 crash checkpoint。
- 没有后台 completion queue。
- 没有 watch pattern。
- 没有非本地 backend。

## 5. Notification 与 Watch Patterns

Hermes 强烈建议：

- 有明确结束的长任务使用 `background=true, notify_on_complete=true`。
- 长驻 server / watcher 可以不通知，或者用少量 `watch_patterns` 监听 readiness。
- 不建议后台 bounded task 没有 notification，因为模型很容易忘记 poll。

后台通知分三类：

- completion：进程退出时通知一次。
- watch_match：输出匹配 pattern 时通知。
- watch_disabled：pattern 触发太频繁时自动关闭，降级为 completion notification。

Hermes 对 watch pattern 有双层限流：

- 单 process：至少 15 秒一次；连续 3 个 strike window 后关闭该 process 的 watch。
- 全局：10 秒窗口最多 15 个 watch notification；超过后全局冷却 30 秒。

通知格式会转为 `[IMPORTANT: ...]` 消息，进入后续 agent turn。Gateway 模式还能把 watcher metadata 绑定到 platform/chat/thread/message，完成后触发新一轮 agent。

这是 Pulsara 目前最大的缺口之一：Pulsara 的 yielded process 需要模型主动 poll/wait；没有“进程完成后主动唤醒模型/用户”的产品语义。

## 6. Output 与持久化

Hermes 对 terminal foreground 输出做：

- head/tail 截断，默认来自 `tool_output.max_bytes`，内置默认 50KB。
- strip ANSI。
- secret redaction。
- exit code interpretation，例如 `grep=1` / `diff=1` 不一定代表错误。
- plugin hook `transform_terminal_output`，允许插件规范化输出。

更重要的是，Hermes 有通用 tool result persistence：

- 大 tool result 可写入 sandbox temp dir `/tmp/hermes-results/<tool_use_id>.txt`。
- 上下文里替换成 `<persisted-output>` block。
- block 告诉模型完整路径、原始大小、preview，并要求用 `read_file` 分段读取。
- persistence 通过 `env.execute()` 写入，因此 local / Docker / SSH / Modal 等 backend 都能访问同一个“环境内路径”。
- 还有 per-turn aggregate budget，把多个中等大小结果也 spill 到磁盘。

Pulsara 当前有 `.pulsara/terminal-output/<process_id>.txt` 和 `full_output_ref`，但它还只是 terminal-specific artifact，不是整个 tool pipeline 的统一协议。

## 7. CWD、环境和 Secret

Hermes backend 持有 `cwd`，每次命令后更新。`workdir` 显式覆盖；否则使用 live environment cwd；再 fallback 到配置 cwd。

环境变量方面，Hermes 的本地 backend 会过滤 Hermes/provider/messaging/tool secrets：

- provider API key 和 base URL。
- Hermes dashboard/session token。
- messaging token。
- Modal / Daytona key 等。

同时它故意不硬阻断通用 AWS credential chain，理由是 terminal 是用户可信 operator shell，用户可能需要 `aws` / `terraform` / `cdk` / `boto3`。这点和 Pulsara 的“full access 当一等公民”方向很接近：不是假装安全，而是明确哪些 secret 不应泄漏，哪些 host capability 是设计目标。

Hermes 还支持：

- `SUDO_PASSWORD` 或交互式 sudo password prompt。
- sudo password cache 按 session/callback/thread 隔离。
- `sudo -n true` 探测 passwordless sudo，避免无意义 prompt。
- 对真实 `sudo` 命令做 token-level rewrite，避免 grep/printf 中的字面 `sudo` 被误判。

Pulsara 当前 env sanitization 和 `.venv` overlay 很强，但没有 sudo prompt/cache 这类 operator-shell 体验。

## 8. Approval 与 Hardline

Hermes 的 dangerous-command approval 在 `tools/approval.py`。关键形状：

- hardline block 在 yolo / approval off 之前执行。
- containerized backend 默认跳过 dangerous-command check，因为不能触达 host。
- yolo mode 在 import 时冻结，避免运行中被 skill 设置 env 绕过。
- approval mode 支持 manual / smart / off。
- cron session 有独立 `cron_mode`。
- gateway approval 会提交 pending request，并阻塞 agent thread 等用户响应。
- approval waiting 会发 activity heartbeat，避免 gateway idle watchdog 误杀。
- approval 有 pre/post plugin hooks。
- session approval / permanent allowlist 可缓存 pattern。
- smart approval 可调用辅助 LLM 判断 false positive。

Hermes hardline 包括灾难级命令、sudo stdin 猜密码等不可绕过底线。危险命令还覆盖 sensitive write target，例如：

- `~/.ssh`。
- `~/.hermes/.env`。
- `~/.hermes/config.yaml`。
- shell rc。
- credential files。
- `/etc` / macOS `/private/etc` 等系统配置路径。

Pulsara 当前已经有 hardline/risky/on-request/ask 和 approval resume。差距主要在：

- Hermes approval 是跨 gateway/CLI/cron/contextvars 的完整系统。
- Hermes 有 smart approval。
- Hermes 有 session/permanent approval cache。
- Hermes 对敏感路径的 shell 写入覆盖更广。

## 9. Stop / Interrupt / Kill

Hermes 的 interrupt 是 thread-aware：

- `tools.interrupt` 记录被 interrupt 的 thread id。
- 工具调用轮询 `is_interrupted()`。
- foreground `_wait_for_process()` 看到 interrupt 后 kill process group 并返回 `[Command interrupted]`。
- `process.wait()` 看到 interrupt 后返回 `status="interrupted"`，不杀后台进程。
- kill 后台 process 通过 `process(action="kill")`。
- session reset / gateway cleanup 可检查 session scoped active processes。

Pulsara 当前已经实现 active run soft stop、approval stop、session close kill owned process。两者差异：

- Pulsara stop 更偏 run 级 canonical event，强调 transcript note。
- Hermes interrupt 更偏 tool/process 级，foreground command 会被强杀。
- Hermes 的后台 process 是可通知、可恢复的任务对象，因此 stop/kill 的产品含义更丰富。

## 10. Crash Recovery 与 Orphan 处理

Hermes 对后台进程做了比 Pulsara 更强的恢复：

- checkpoint running process metadata 到 `~/.hermes/processes.json`。
- gateway 重启后可恢复仍存活的 host PID 为 detached session。
- detached session 无法读取历史 output，但可以展示状态、kill。
- sandbox PID 不在 host 重启后恢复。
- Docker backend 有 orphan reaper，按 profile 清理 stale containers。
- `_reconcile_local_exit()` 用 `Popen.poll()` 修复 reader thread 被 orphaned pipe 卡住导致 process 永远 running 的问题。

Pulsara 当前没有 durable process checkpoint，也没有 restart 后 attach/kill orphan 的能力。

## 11. 与 Pulsara 对比

Pulsara 已经具备的能力：

- host shell terminal。
- PTY。
- yield 后 process handle。
- process stdin / poll / wait / kill / EOF。
- workspace cwd guard。
- owner host session isolation。
- env sanitization、shell snapshot、venv overlay。
- output redaction、truncation、artifact。
- permission 三轴、approval resume、user stop、failure/interrupted note。

Hermes 明显领先的能力：

- 多 backend execution environment。
- background process 是 first-class task。
- notify_on_complete。
- watch_patterns + 限流 + 自动降级。
- gateway completion notification 触发新 turn。
- crash checkpoint + detached process recovery。
- foreground/background 使用指导和 runtime guard。
- foreground interrupt 直接 kill process group。
- universal persisted-output protocol。
- smart approval / session approval / permanent allowlist。
- sudo prompt/cache。
- 对 shell hack 和 orphaned pipe 的大量实战补丁。

Pulsara 不必复制的部分：

- Modal / Daytona / Docker 全 backend 可以后置；Pulsara 的近期目标仍可以是优秀的本地 desktop host agent。
- Hermes 的 gateway/messaging 多平台 routing 很重，不是 V1 terminal 必需。
- smart approval 需要额外模型成本和安全评估，最好等基础 approval cache 稳定后再做。

## 12. Pulsara 可借鉴的优先级

### P0：让后台进程变成可通知任务

新增 `notify_on_complete` 或等价机制，让 yielded process 完成时能进入 host/session event，而不是只能靠模型 poll。

最小形状：

- `terminal(..., yield_time_ms=..., notify_on_complete=True)` 或新增 `background=True`。
- process 完成后发 `TerminalProcessCompletedEvent`。
- 下一轮 prior context 或 active session UI 收到 `[terminal task completed]`。
- inspect 显示 live / completed process list。

### P1：输出 artifact 协议化

把 `.pulsara/terminal-output` 从内部 ref 升级成模型可理解的结构：

- 原始大小。
- head/tail preview。
- full output path。
- 推荐读取方式。
- 是否 redacted/truncated。

最好做成所有 tool result 都能用的通用 `<persisted-output>` 机制，而不是 terminal-only。

### P1：后台 process registry 持久化

为 host desktop 产品补：

- process metadata checkpoint。
- app 重启后扫描 still-alive host PIDs。
- detached process 可 list/kill。
- output history 不可恢复时明确标记。

### P2：foreground/background 使用指导

Pulsara 可以给 terminal 工具 description 和 runtime 加 Hermes 式 nudges：

- 发现 `nohup` / `disown` / trailing `&` 时建议使用 managed background。
- foreground timeout 超大时建议 background。
- 疑似 server/watch command 建议 background + readiness check。

### P2：审批缓存和 prefix/rule 体验

Pulsara 已有 approval resume，下一步可以补：

- approve once/session/always。
- session-scoped command/pattern approval cache。
- risky command false-positive 降噪。

### P3：watch pattern

watch pattern 很有用，但容易通知风暴。若做，必须像 Hermes 一样带：

- per-process rate limit。
- global circuit breaker。
- 与 notify_on_complete 互斥或明确优先级。
- 自动降级为 completion notification。

## 13. 结论

Hermes 给 Pulsara 的核心启发是：terminal 工具的下一层成熟度，不是再多几个 shell 参数，而是把“命令运行中和运行后发生的事”产品化。

Pulsara 当前已经有很好的底层：PTY、yielded process、权限、approval resume、stop、env sanitization。下一步最值得补的是后台任务生命周期：completion event、process list、durable registry、输出 artifact 协议、以及模型不用反复 poll 的通知通道。
