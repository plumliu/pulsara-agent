# OpenClaw Terminal Capability Survey

本文记录本地 `/Users/plumliu/Desktop/python_workspace/openclaw` 中 terminal / exec / process 相关能力，并与 Pulsara 当前 terminal 实现比较。OpenClaw 的设计和 Pulsara、Codex 很接近：都把“启动命令”和“继续管理长程进程”拆成两个工具；但 OpenClaw 已经把后台进程、sandbox/elevated、host/node/gateway、多端审批和系统事件唤醒做成了完整产品层。

## 1. 工具面

OpenClaw 的主要模型工具是：

- `exec`：执行 shell command。
- `process`：管理 `exec` background/yield 后的 session。

`exec` 参数：

- `command`：shell command 字符串。
- `workdir`：工作目录。
- `env`：环境变量 overrides。
- `yieldMs`：等待指定毫秒后自动 background。
- `background`：立即 background。
- `timeout`：秒级进程 timeout，超时会 kill。
- `pty`：需要真实 TTY 的 CLI 可开启。
- `elevated`：在允许时绕过 sandbox，在 host 上执行。
- `host`：`auto` / `sandbox` / `gateway` / `node`。
- `security` / `ask`：普通调用中基本由配置和 host approvals 决定。
- `node`：指定 node host。

`process` 参数：

- `action`：`list` / `poll` / `log` / `write` / `send-keys` / `submit` / `paste` / `kill` / `clear` / `remove`。
- `sessionId`：background exec session id。
- `data`：`write` stdin。
- `keys` / `hex` / `literal`：`send-keys` 的输入形式。
- `text` / `bracketed`：`paste` 的文本和 bracketed paste mode。
- `eof`：write 后关闭 stdin。
- `offset` / `limit`：log 分页。
- `timeout`：`poll` 等待窗口，最大 30000ms。

与 Pulsara 对齐：

- OpenClaw `exec` ≈ Pulsara `terminal`。
- OpenClaw `process` ≈ Pulsara `terminal_process`。
- OpenClaw 的 process action 比 Pulsara 更偏交互终端产品：`send-keys`、`paste`、`clear`、`remove`、`waitingForInput` 都是模型和 UI 友好的高频能力。

## 2. 执行目标：sandbox / gateway / node

OpenClaw 的 `exec` 不只是本地 shell，它会先解析 execution target：

- `auto`：有 sandbox 时走 sandbox，否则 gateway。
- `sandbox`：强制 sandbox。
- `gateway`：Gateway host。
- `node`：远端 node host。

`elevated=true` 是 exec-only escape hatch：

- sandboxed 时可请求跑到 gateway host。
- 如果当前 target 是 node，则 elevated 可保留 node。
- elevated 不授予工具访问权，只影响 exec 的运行边界。
- elevated 仍受 `tools.elevated.enabled` 和 `allowFrom` gate 约束。

OpenClaw 文档明确把三层控制拆开：

- sandbox：决定工具在哪里运行。
- tool policy：决定哪些工具可用。
- elevated：exec-only 的 host escape hatch。

这点值得 Pulsara 借鉴。Pulsara 当前三轴 policy 解决的是 permission/approval/terminal access；OpenClaw 的拆法更进一步，把“运行边界”和“工具可见性/审批”分开得很清楚。

## 3. Sandbox 能力

OpenClaw sandbox 是可选的，但做得很系统：

- mode：`off` / `non-main` / `all`。
- scope：`agent` / `session` / `shared`。
- backend：`docker` / `ssh` / `openshell`。
- workspace access：只读/读写等。
- Docker backend 支持 bind mounts、network、browser sandbox/noVNC 等。
- SSH backend 使用 remote-canonical workspace，首次 seed 后远端成为真实状态。
- OpenShell backend 支持 `mirror` / `remote` 两种 workspace 模式。
- `openclaw sandbox explain/list/recreate` 提供 inspect 和 lifecycle 管理。

Pulsara 当前明确是 host shell，inspect 诚实显示非隔离。这是合理 V1。但如果后续要支持 universal desktop agent 的“低风险模式”，OpenClaw 的 sandbox 分层是一个更成熟的参考：先保持 host/full access 是一等公民，再增加可选 sandbox backend，而不是把两者混在一个权限枚举里。

## 4. Exec 主流程

`src/agents/bash-tools.exec.ts` 的主流程很完整：

1. 解析 background/yield 行为。
2. 解析 elevated mode 和 target host。
3. 合并 config policy 和 host approvals file。
4. 解析 sandbox workdir / gateway workdir / node workdir。
5. 检查 control-shell command。
6. 构造 host/sandbox env，并拒绝危险 env override，特别是 host `PATH`。
7. gateway host 路径进入 allowlist/approval pipeline。
8. 可选 script preflight，检查常见“shell syntax 泄漏进 Python/JS 文件”的模型错误。
9. 调用 `runExecProcess()` 启动 supervisor process。
10. 在 `yieldMs` 后或 `background=true` 时 `markBackgrounded()`，返回 `status: "running"` 和 session id。
11. 如果 foreground 正常结束，返回聚合输出和 exit details。

Pulsara 当前也有 retry/approval/terminal runtime，但 OpenClaw 的 exec pipeline 对“命令启动前的环境、target、approval、script preflight、background handoff”拆分更细。

## 5. Process Supervisor 与 Registry

OpenClaw 用 `bash-process-registry.ts` 管理 background exec session：

- running sessions。
- finished sessions。
- `ProcessSession` 包含 command、scopeKey、sessionKey、mainKey、routing policy、notify context、pid、cwd、stdin、startedAt、output buffer、exit state。
- `FinishedSession` 保留聚合输出、tail、exit code/signal、truncated、total chars。
- 默认 finished TTL 30 分钟，env 可配置，限制在 1 分钟到 3 小时。
- running session 只有 backgrounded 后才进入 process 工具可见列表。
- 输出有 aggregate cap 和 pending stdout/stderr cap。
- session id 使用短 slug，并避免冲突。

Pulsara 当前也有 live/finished registry 和 owner isolation，但 OpenClaw 领先处包括：

- pending stdout/stderr 分开。
- `process poll` 可 drain pending output。
- finished session retention 用于 `poll/log/clear/remove`。
- output 与 UI/system-event routing 有更强绑定。
- process list 包含 derived `name`，便于扫描。

OpenClaw 没有 durable process registry；docs 明确说进程在进程重启后丢失。这一点 Hermes 比 OpenClaw 更强，Pulsara 也还没有。

## 6. Background Completion Wake

OpenClaw 对后台进程完成有系统事件：

- `notifyOnExit` 默认开启。
- background session 退出后 `maybeNotifyOnExit()` 会构造短摘要。
- 摘要进入 `enqueueSystemEvent()`。
- 同时 `requestHeartbeat()`，让相关 session 被唤醒。
- 成功但无输出默认不唤醒，可通过 `notifyOnExitEmptySuccess` 开启。
- subagent session 不走 heartbeat，以免错误唤醒 main session。

这是 Pulsara 当前最值得补的能力之一。Pulsara 的 yielded process 目前需要模型主动 `terminal_process.poll/wait`；OpenClaw 让长任务退出变成系统事件，模型/用户不用靠手动轮询发现。

## 7. Output 与交互

OpenClaw 对输出做：

- `DEFAULT_MAX_OUTPUT` 默认 200KB。
- pending output 默认 30KB。
- stdout/stderr 都写入 session pending buffer 和 aggregated output。
- `sanitizeBinaryOutput()` 处理二进制/控制字符。
- PTY output 检测 cursor key mode：normal/application。
- foreground output 用 `(no output)` placeholder，避免空输出不明确。
- `process log` 支持 line offset/limit，默认最后 200 行并给 paging hint。
- `process poll` 支持短等待窗口；若无新输出且还在运行，会给 “Process still running”。
- `waitingForInput` 基于 stdin writable + idle time 推导。
- process tool 会提示 `write/send-keys/submit/paste` 可恢复交互 session。

Pulsara 当前有 head/tail 截断和 output artifact，但 OpenClaw 的优势在于“交互式长程进程”的状态提示更自然：不是只给 output，而是告诉模型这个 session 可能在等输入。

## 8. PTY 与输入

OpenClaw 的 process 输入能力比 Pulsara 更细：

- `write`：写原始 stdin，可选 EOF。
- `submit`：发送 carriage return。
- `send-keys`：发送 key tokens、hex bytes 或 literal。
- `paste`：发送文本，默认 bracketed paste。
- PTY cursor key mode 会影响 key encoding。

Pulsara 已支持 PTY、write、submit、close_stdin，但还没有：

- key token 层。
- bracketed paste。
- waiting-for-input hint。
- cursor key mode。

这对 universal desktop agent 很重要，因为 coding agent/REPL/TUI/installer 交互经常不是简单写一行文本。

## 9. Approval / Exec Policy

OpenClaw 的 exec approval policy 很成熟：

- `ExecSecurity`：`deny` / `allowlist` / `full`。
- `ExecAsk`：`off` / `on-miss` / `always`。
- `ExecMode`：`deny` / `allowlist` / `ask` / `auto` / `full`。
- approvals 文件位于 `~/.openclaw/exec-approvals.json`。
- host approvals file 是 enforceable source of truth。
- CLI 有 `openclaw approvals` 和 `openclaw exec-policy`。
- allowlist 支持 agent 维度。
- ask fallback 支持 `full` / `deny` / `allowlist`。
- `allow-always` 可写入 allowlist。
- approval request 包含 command、argv、env keys、cwd、host、security、ask、command analysis、command spans、turn source 等。
- macOS native helper 可弹出 native approval prompt。

OpenClaw 还做了不少安全细节：

- host env 继承会移除危险变量。
- caller-provided host env 拒绝危险 keys 和 `PATH` override。
- safeBins 要求 profile；对 interpreter/runtime safeBins 很保守。
- shell wrapper / env invocation 有专门解析。
- node host 的 system-run approval 绑定 argv/cwd/env hash，防 prompt 和实际执行错配。

Pulsara 当前已经有 on_request / ask、approval resume、deny result、stop approval。下一步最接近 OpenClaw 的补足是：

- session/always approval cache。
- allowlist/prefix policy。
- approval prompt 中包含 command analysis 和可读 argv/cwd/env key preview。
- host env override 更严格地拒绝 PATH/危险 keys。

## 10. Tool Policy 与 Runtime Tool Groups

OpenClaw tool policy 和 sandbox tool policy 分离：

- deny 永远赢。
- allow 非空时只允许名单内工具。
- tool groups 如 `group:runtime` 包含 `exec` / `process` / `code_execution`。
- read-only agent 如果只 deny write/edit 但保留 exec，是不完整的；docs 明确提醒需要同时 deny runtime 或依赖 sandbox filesystem。
- sandboxed MCP/plugin tools 也需要进入 sandbox tool allowlist。

Pulsara 当前 read_only 会隐藏 `terminal`、`terminal_process` 和写文件工具。这比较清楚，但 OpenClaw 的 docs 提醒很有价值：只要 shell 在，文件写能力就还在。Pulsara inspect/文档也应继续坚持这个诚实口径。

## 11. Stop / Abort / Kill

OpenClaw 区分：

- tool-call abort：如果进程尚未 backgrounded，会 kill。
- 已 yielded/backgrounded 的 session：abort 不杀，让 process 工具接管。
- process `kill`：优先 supervisor cancel，失败再 fallback kill process tree。
- `remove`：running 时终止并删除 registry；finished 时清除。
- timeout：进程超时 kill，并提示如果预期更长应使用更高 timeout 或 background/yield。

Pulsara 当前 active run stop 是 soft cancel，不承诺杀 yielded process；session close 会 kill owned process。OpenClaw 的策略更细：foreground tool abort 会 kill，backgrounded process 则留给 process 管理。这可能是 Pulsara 后续可以采用的更直观语义：

- stop run 默认不杀已经 backgrounded 的 task。
- stop foreground terminal call 时可 kill 正在首轮等待的 process。
- 对仍存活 task 明确提示用户。

## 12. UI / Native Approval

OpenClaw 有多端 UI：

- macOS native approval prompt。
- iOS push approval bridge。
- UI exec approval views/controllers。
- gateway approval manager。
- operator approvals client。

它还区分 local prompt suppression 和 native route active 状态，避免同一 approval 通过多个 UI 同时打扰用户。

Pulsara 当前 CLI/host 能力还在本地 runtime 层，尚未形成桌面 UI 的 terminal task / approval card / notification 体系。对 universal desktop agent 来说，这正是后续产品层要补的地方。

## 13. 与 Pulsara 对比

Pulsara 已有：

- host shell terminal。
- PTY。
- yielded process 和 `terminal_process`。
- stdin write/submit/EOF。
- output artifact。
- env sanitization 和 `.venv` overlay。
- workspace cwd guard。
- permission 三轴、approval resume、stop、failure/interrupted note。

OpenClaw 领先处：

- `exec/process` 已经是系统任务层，不只是工具调用。
- background completion wake。
- `process` 的 `send-keys` / `paste` / `waitingForInput`。
- sandbox/gateway/node/elevated target 分层。
- robust exec approval policy 和 allowlist。
- native/mobile approval UX。
- host env override security 更细。
- safeBins / shell wrapper / env invocation 解析。
- process output pending/aggregated 分层。

OpenClaw 不如 Hermes 的地方：

- background process registry 不是 durable。
- 没有 Hermes 那样的 watch pattern 机制。
- 没有 Hermes 的多 cloud backend terminal abstraction；它的 sandbox backend 强，但更围绕 OpenClaw gateway 架构。

## 14. Pulsara 可借鉴的优先级

### P0：background completion wake

为 yielded terminal process 增加完成事件和下一轮唤醒机制：

- process completed event。
- session/system event。
- prior context 或 host UI 告知“某 terminal task 完成”。
- 成功无输出是否通知可配置。

### P0：process list / log / poll 产品化

把 `terminal_process` 的返回从底层 JSON 提升到：

- `list`：显示 session id、status、duration、cwd、name、tail。
- `log`：line offset/limit，默认 tail 200 行。
- `poll`：drain new output，返回 retry hint。
- `waitingForInput`：idle + writable stdin 推导。

### P1：交互输入能力

补：

- `send_keys`。
- bracketed paste。
- key token 编码。
- cursor key mode。

这些对 REPL、TUI、认证命令、其他 agent CLI 都很实用。

### P1：approval cache / allowlist

在 approval resume 稳定后，增加：

- approve once/session/always。
- command/prefix allowlist。
- prompt 中显示 command、cwd、host、env keys。
- `allow-always` 写 policy。

### P2：运行边界分层

保留 Pulsara 的 `trusted_host` full access 默认，但把未来 sandbox 设计成独立 axis：

- terminal target：host / sandbox / remote。
- tool policy：工具可见性。
- approval policy：问不问。
- elevated：sandbox 下的 host escape hatch。

## 15. 结论

OpenClaw 对 Pulsara 最有价值的启发是：terminal 能力的成熟路径应该从“命令执行 API”进化到“可观察、可恢复、可唤醒、可审批的任务系统”。

Pulsara 的底层已经不错，尤其是 approval resume、stop note、env overlay、host session ownership。下一步若要提升 terminal 体验，优先级应放在 background completion wake、process UI/log/poll 产品化、交互输入、approval allowlist，而不是急着复制完整 sandbox runtime。
