# Codex Terminal Capability Survey

本文记录本地 `/Users/plumliu/Desktop/python_workspace/codex` 中 terminal / exec 相关能力，并与 Pulsara 当前实现比较。Codex 的 terminal 设计和 Pulsara 更接近：都把“执行命令”和“继续与长程进程交互”拆成两个工具；不同点在于 Codex 已把这一层深度接入 sandbox、approval、execpolicy、TUI history cell 和 app-server protocol。

## 1. 工具面

Codex 新的主 terminal 工具是 unified exec：

- `exec_command`
- `write_stdin`

`exec_command` 参数：

- `cmd`：shell command 字符串。
- `workdir`：命令工作目录，默认 turn cwd。
- `shell`：可选 shell binary。
- `login`：是否 login shell，受配置开关约束。
- `tty`：是否分配 PTY。
- `yield_time_ms`：等待后 yield，范围 250-30000 ms。
- `max_output_tokens`：输出 token budget，默认 10000。
- `sandbox_permissions` / `additional_permissions` / `justification` / `prefix_rule`：权限与审批相关参数。
- `environment_id`：app-server / attached environment 下可指定目标环境。

`write_stdin` 参数：

- `session_id`：仍在运行的 unified exec session id。
- `chars`：写入 stdin 的字符。空字符串表示 poll。
- `yield_time_ms`：非空写入更短，空 poll 可等待更久。
- `max_output_tokens`。

输出 schema 包含：

- `chunk_id`。
- `wall_time_seconds`。
- `exit_code`。
- `session_id`。
- `original_token_count`。
- `output`。

和 Pulsara 的对齐：

- Pulsara `terminal` ≈ Codex `exec_command`。
- Pulsara `terminal_process.poll/wait/write/submit` ≈ Codex `write_stdin` 的空 poll / 非空 stdin。
- Pulsara action 更显式；Codex 协议更紧凑，也更贴近一个 PTY session。

## 2. 执行模型

Codex unified exec 的执行路径大致是：

1. handler 解析 `cmd` / `workdir` / shell / tty / permission 参数。
2. 按 selected environment 解析 cwd。
3. 通过 `SandboxManager::select_initial()` 决定初始 sandbox。
4. 通过 execpolicy 和 approval runtime 计算是否需要审批。
5. 经 `SandboxManager::transform()` 生成 host-native `ExecRequest`。
6. 由 `UnifiedExecProcessManager` 分配 process id、启动 PTY 或 exec-server process。
7. 初始调用在 `yield_time_ms` 内收集输出；若进程仍活着，返回 `session_id`。
8. 后续用 `write_stdin` 写入或 poll。

Codex 支持两种本地 shell backend：

- `Direct`：直接用 session shell 派生 exec args。
- `ZshFork`：在本地 zsh fork 模式下优化环境和 shell 启动。

远端 environment 会退回 Direct，并使用 environment 报告的默认 shell。

Pulsara 当前更简单：

- 直接 host shell + subprocess。
- workspace supervisor 管理进程。
- 每个 terminal session 有 current cwd。

Codex 更强的点：

- environment-aware：local / remote / app-server attached environment 统一走同一工具形状。
- 命令启动前完整穿过 approval、sandbox、network approval、hooks。
- `exec_command` 能拦截 `apply_patch`，把 shell 里的 apply_patch 统一转成专门 patch handler。

## 3. Process Manager

`UnifiedExecProcessManager` 是 Codex terminal 的核心。

关键行为：

- process id 由 manager 分配，测试可 deterministic。
- 默认最多 64 个 unified exec process。
- 初始 `exec_command` 若发现进程启动后仍活着，会先存入 process store，再等待输出。这避免 turn interrupt 时最后一个 Arc 丢失导致后台进程被提前终止。
- `yield_time_ms` clamp：
  - 普通初始等待 250-30000 ms。
  - Windows 初始等待有 2000 ms floor。
  - 空 `write_stdin` poll 至少 5000 ms，默认最大 300000 ms。
- process store 有 LRU prune：
  - 保护最近 8 个。
  - 优先移除已退出 process。
  - 不够再移除最久未用 live process。
- `terminate_all_processes()` 和 `terminate_process(process_id)` 是 manager 级能力。
- `list_processes()` 返回 `BackgroundTerminalInfo`，包含 item id、process id、command、cwd。

Pulsara 当前已有：

- max live / finished process。
- owner host session 隔离。
- session close / workspace close kill。

Codex 可借鉴处：

- `list_processes()` 作为明确产品/API surface。
- 保护最近 N 个 live process 的 prune 策略。
- initial exec active guard：避免用户在命令刚启动时 terminate 把仍在首个 tool response 里的 process 状态搞乱。

## 4. 输出与流式事件

Codex unified exec 输出采用内存 head/tail buffer：

- `HeadTailBuffer` 默认保留 1 MiB。
- 保留稳定 prefix 和 suffix，中间丢弃。
- `start_streaming_output()` 从 process broadcast receiver 读输出。
- 输出 delta 按有效 UTF-8 prefix 切分。
- 单个 output delta 上限 8192 bytes。
- 每个 call 有 output delta 数量上限。
- exec end event 用 transcript head/tail 聚合输出。
- `original_token_count` 用于告诉模型截断前规模。

TUI 上有 `ExecCell`：

- active command 有 spinner / elapsed。
- read/list/search 类命令会聚合成 “exploring cell”。
- 输出显示 head/tail，并提示 `ctrl + t to view transcript`。
- `write_stdin` 交互会显示为 `Interacted with ... sent ...` 或 `Waited for ...`。
- transcript view 能展示更完整命令和结果。

和 Claude Code 的差别：

- Claude Code 更依赖 output file 作为单一事实源。
- Codex 更依赖 event stream + capped in-memory transcript。

和 Pulsara 的差别：

- Pulsara 已有 head/tail 截断和 full output artifact，但 UI/事件层还没有 Codex 这种 exec cell 聚合。
- Pulsara 的 streaming 是工具结果 JSON chunk；Codex 有独立 `ExecCommandOutputDeltaEvent` / `ExecCommandEndEvent`。

Pulsara 可借鉴：

- 将 terminal output delta 变成 first-class event。
- 给 terminal history 加 `ExecCell` 类模型：active / complete / interaction / exploring group。
- 对 read/list/search terminal command 做 UI 聚合，减少 transcript 噪声。

## 5. Stdin / 交互协议

Codex 的 `write_stdin` 设计简洁：

- `chars=""` 是 poll。
- 非空 `chars` 是真实 stdin。
- 非 TTY process 只接受 interrupt (`\u{3}`)，否则 stdin closed。
- TTY process 可写任意 bytes。
- 非空写入后 sleep 100 ms，让进程有机会产出响应。
- poll/写入后都统一返回 recent output、exit_code、session_id。
- 如果 process 已退出，会从 store 释放。

Pulsara 当前：

- `terminal_process` 明确区分 `poll`、`wait`、`kill`、`write`、`submit`、`close_stdin`。
- pipe 和 PTY 都支持 stdin。

取舍：

- Codex 更小的工具面有利于模型学习。
- Pulsara 更显式的 action 对安全审计和 UI 操作更清楚。

Pulsara 可以保留 action schema，但补一个模型友好的 alias：

- `terminal_process(action="poll")` 仍可用。
- 未来可加 `terminal_wait_or_write(process_id, chars="", yield_time_ms=...)` 作为简化高频路径。

## 6. Sandbox 与 Permission Profile

Codex 的 sandbox 是跨平台主线能力：

- macOS Seatbelt。
- Linux seccomp / bwrap / landlock 相关实现。
- Windows restricted token / Windows sandbox。
- 网络可通过 managed proxy / network approval。
- Permission profile 支持 filesystem / network / workspace roots。
- `danger-full-access` 对应 `PermissionProfile::Disabled`。
- `read-only`、`workspace`、`danger-full-access` 是 built-in approval presets。

内置 approval presets：

- Read Only：可读当前 workspace；编辑或访问互联网需审批。
- Default：可读写 workspace、运行命令；访问互联网或编辑外部文件需审批。
- Full Access：可编辑 workspace 外文件，访问互联网不问。

这和 Pulsara 的理念很接近：full access 是一等公民，而不是隐藏选项。

差异：

- Pulsara 目前没有 OS-level filesystem/network sandbox。
- Pulsara 权限是三轴 `permission_profile / approval_policy / terminal_access`，更轻量。
- Codex 权限 profile 可以更细：具体路径 read/write/deny、network domains、workspace roots。

建议：

- Pulsara 不必马上复制 sandbox；但可以把 permission profile 从 coarse 三档扩展为“coarse preset + optional path/network overrides”。
- `trusted_host` 可继续作为默认 full-power 方向；同时加 inspect 明示 host shell。

## 7. Approval 与 ExecPolicy

Codex 的 approval 体系比 Pulsara 更完整：

- `AskForApproval` 支持 `never`、`on_failure`、`on_request`、`unless_trusted`、`granular`。
- `ExecApprovalRequirement` 有：
  - `Skip`。
  - `NeedsApproval`。
  - `Forbidden`。
- approval 可以缓存为 session 范围。
- approval key 包含 command、cwd、tty、sandbox permissions、additional permissions、environment id。
- network approval 和 exec approval 是相邻但独立的路径。

`execpolicy` 是一个非常值得借鉴的能力：

- 规则是 prefix rule，可决策 `allow` / `prompt` / `forbidden`。
- 支持 network rule。
- policy 可从多个 `.rules` 文件加载。
- approval prompt 可建议追加 allow prefix rule，减少未来重复审批。
- append rule 会同时更新磁盘和内存 policy。
- 对危险/过宽 prefix suggestion 有 banned list：
  - `python -c`、`bash -lc`、`sh -c`、`zsh -lc`。
  - `sudo`、`env`。
  - `node -e`、`perl -e`、`ruby -e`、`php -r`、`osascript` 等。
- 对 shell wrapper 会尝试解析 `bash -lc ...` 里的 plain commands；复杂解析用于评估但不自动生成 amendment。

Pulsara 当前已有 approval resume 和三轴策略，但没有“批准一次并转成持久规则”的能力。

建议 Pulsara 后续做一个轻量版：

- terminal approval 时允许 “approve once / approve for session / approve prefix”。
- prefix 建议必须有 banned list。
- allow prefix 不应覆盖 hardline deny。
- 初期只做 session-scope prefix cache，不急着写磁盘。

## 8. `/shell` User Shell

Codex 还有用户显式 shell 命令路径，不是模型工具：

- 用户直接发 `/shell` 或 shell-mode 命令。
- 作为独立 turn 或 active turn auxiliary 执行。
- 使用 `PermissionProfile::Disabled`。
- `SandboxType::None`。
- 不继承 managed proxy。
- timeout 1 小时。
- 执行结果被封装成 `<user_shell_command>` 上下文片段，role 是 user。

这是一条显式 full-access escape hatch。

Pulsara 当前没有等价的“用户直接 shell command 变成上下文”的 first-class 路径。若目标是 desktop agent，可以考虑：

- Host UI 支持用户直接运行 terminal command。
- 结果作为 user-supplied context 注入模型，而非 assistant tool result。
- 明确区分“用户自己跑的 shell”和“agent 跑的 shell”。

## 9. Hooks

Codex 的 shell-like tools 会发 PreToolUse / PostToolUse hook payload：

- `exec_command` pre hook 以 `Bash` tool name + `{ command }` 形式发。
- `write_stdin` 不发 pre hook：它只是已有 exec session 的 transport。
- `write_stdin` poll 如果观察到最终完成，会为原始 exec command 发对应 post hook。

Pulsara 当前还没有同级别 hook system。短期无需复制，但这里有一个可借鉴边界：

- 初始命令是审计点。
- stdin continuation 通常不应重新跑完整 pre hook。
- 但 stdin 内容如果可能绕过 hardline，要在本地 gate 做额外检查。

这和 Pulsara 已做的 `terminal_process.write/submit` hardline check 是同一方向。

## 10. 对 Pulsara 的补足建议

优先级建议：

1. 建立 terminal task / exec cell 概念。
   - 显示 active / completed / failed / interaction。
   - 支持 process list。
   - 和 `terminal_process` 打通。

2. 增加 output delta / end event。
   - 不只把输出包在 tool result JSON 里。
   - 让 UI、memory、dogfood audit 能稳定订阅 terminal lifecycle。

3. 做 session-scope approval cache 和 prefix approval。
   - 不必马上持久化 `.rules`。
   - 先支持 approve for session。
   - 加 banned prefix suggestions。

4. 把 full access 作为公开 preset。
   - 类似 Codex 的 Full Access。
   - Pulsara 可命名为 `trusted_host`。
   - UI/inspect 明确“host shell, no fs/network sandbox”。

5. 增强 process manager。
   - `list_processes()` API。
   - LRU prune。
   - active initial exec guard。
   - process interaction event。

6. 考虑用户直连 shell。
   - 用户执行 shell，不走 agent approval。
   - 结果作为 user context，而不是 agent 工具输出。
   - 这会非常符合 universal desktop agent 的工作流。

## 11. 不建议直接复制的部分

- 不建议立即复制完整 OS sandbox stack，工程量大且与 Pulsara 当前 full-access 主线不完全一致。
- 不建议马上引入 Starlark-like execpolicy；可以先做 session-scope prefix cache。
- 不建议把 `terminal_process` 压成只有 `write_stdin` 一个工具；Pulsara 的显式 action 对用户 UI 和审计更友好。

更实际的路线是：保留 Pulsara 的轻量 host terminal 体系，把 Codex 的 task/event/cell/approval-cache 四件事逐步吸收进来。
