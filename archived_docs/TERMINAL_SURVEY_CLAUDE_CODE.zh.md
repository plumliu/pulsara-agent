# Claude Code Terminal Capability Survey

本文记录本地 `/Users/plumliu/Desktop/python_workspace/claude-code` 中 terminal / shell 相关能力，并和 Pulsara 当前实现对齐比较。Claude Code 是成熟产品，本文件重点提炼可借鉴的工程形状，而不是逐项追求复制。

## 1. 工具面

Claude Code 的主 terminal 工具是 `BashTool`，Windows 侧另有 `PowerShellTool`。

`BashTool` 的模型可见输入比 Pulsara 更偏产品化：

- `command`：shell command 字符串。
- `timeout`：命令超时。
- `description`：给 UI / 后台任务展示的人类描述。
- `run_in_background`：明确让命令后台运行。
- `dangerouslyDisableSandbox`：在允许 unsandboxed fallback 时绕过 sandbox。

输出也比 Pulsara 更结构化：

- `stdout` / `stderr`。
- `backgroundTaskId`。
- `backgroundedByUser`。
- `assistantAutoBackgrounded`。
- `interrupted`。
- `isImage`。
- `returnCodeInterpretation`。
- `noOutputExpected`。
- `persistedOutputPath` / `persistedOutputSize`。
- `rawOutputPath`、`structuredContent` 等扩展字段。

Pulsara 当前的 `terminal` / `terminal_process` 更接近通用底层 API，Claude Code 则在工具返回里直接带上 UI、后台任务、语义解释、大输出持久化等产品语义。

## 2. 执行模型

Claude Code 每次 Bash 调用都会创建新的 shell process，并通过 shell provider 构造命令包装：

- 默认 timeout 为 30 分钟。
- 支持 bash / zsh shell detection，`CLAUDE_CODE_SHELL` 可覆盖。
- 通过 provider 注入 cwd tracking 文件，命令结束后同步读取 `pwd -P` 输出并更新 app cwd。
- foreground 命令结束后才更新 cwd；后台命令不更新 cwd。
- spawn env 由 `subprocessEnv()` 构造，并额外设置 `GIT_EDITOR=true`、`CLAUDECODE=1`。
- 使用 `tree-kill` 终止 process tree。

Pulsara 与它相似的地方：

- 也是每次 tool call spawn host shell。
- 也维护 terminal cwd 状态。
- 也使用 process group / tree 级别 kill。

关键差异：

- Pulsara 有显式 `terminal_session_id`，每个 session 有 current cwd；Claude Code 更像一个 app-level current cwd。
- Pulsara 支持 PTY 与 `terminal_process.write/submit`，Claude Code Bash 主路径更偏非交互式命令；交互卡住时由 stall watchdog 提醒重跑非交互式命令。
- Claude Code foreground/background 是产品任务模型；Pulsara 是 `yield_time_ms -> process_id -> terminal_process` 的工具协议模型。

## 3. 后台任务与长程命令

Claude Code 的后台任务系统非常成熟：

- 模型可主动传 `run_in_background`。
- 前台命令超过进度阈值后 UI 显示 progress，并允许用户用快捷键把 foreground task background。
- assistant mode 下长阻塞命令可自动 background。
- timeout 到期时，在允许 auto-background 的命令上不是直接 kill，而是转为后台任务。
- 后台任务完成、失败、被杀都会通过 `<task_notification>` 注入给模型。
- 后台任务有 `taskId`、description、output file、status、toolUseId、agentId。
- agent 退出时会 kill 该 agent 启动的后台 bash 任务，避免 orphan。
- 有 stall watchdog：后台输出长时间不增长，且 tail 看起来像 `(y/n)` / `Press Enter` / `Overwrite?` 等交互 prompt 时，通知模型 kill 并用 piped input 或非交互 flag 重跑。

Pulsara 当前已有：

- `yield_time_ms` 后返回 `process_id`。
- `terminal_process.poll/wait/kill/write/submit`。
- session close / workspace close 清理 owned process。

Claude Code 领先处：

- 后台任务是 app 级 first-class 状态，而不是只作为 tool handle。
- 有 completion notification，不需要模型轮询。
- 有 foreground-to-background 的用户操作。
- 有 interactive prompt stall detection。
- 有 agent-scoped cleanup。

Pulsara 可以借鉴但不必照搬的方向：

- 给 yielded process 增加更产品化的 task notification。
- 对无输出但疑似等待输入的 process 做 prompt-tail 检测。
- 将 `process_id` 的状态在 inspect / UI 中变成可浏览的 task list。

## 4. 输出处理

Claude Code 对 Bash 输出采用“文件是单一事实源”的设计：

- Bash stdout/stderr 在 file mode 下直接写入 output file fd，不经过 JS 常驻内存。
- stdout/stderr 合并写入同一文件，按时间交错。
- React progress 轮询文件 tail，提取最近 5 行 / 100 行、total lines、total bytes。
- 小输出可完整读回内联。
- 大输出会复制到 tool-results 目录，通过 `<persisted-output>` 协议告诉模型路径、原始大小和 preview。
- 若输出文件超过 5GB，后台 size watchdog 会 kill 进程。
- 输出文件打开使用 `O_NOFOLLOW` 防 symlink attack。
- 还有 image output 识别、resize/re-encode 后以 image block 返回。

Pulsara 当前已有：

- `OutputAccumulator`。
- head/tail truncate。
- `.pulsara/terminal-output/<process_id>.txt` full output artifact。
- 轻量 secret redaction。
- streaming JSON output chunk。

Claude Code 领先处：

- 大输出不是“附带 ref”，而是模型可读协议的一部分。
- 进度 tail 和后台通知围绕同一 output file。
- 有磁盘上限和 symlink 防护。
- 有图像输出特殊处理。

Pulsara 建议补足：

- 把 full output artifact 升级为模型明确可读的 `<terminal-output>` 或结构化 result block。
- 为 `terminal_process.poll/wait` 返回 output preview + persisted path + size。
- 给后台 process 增加 output size watchdog。
- 考虑文件模式直接写 fd，减少 Python 进程持有大输出的压力。

## 5. 权限与规则匹配

Claude Code 的 Bash 权限比简单正则更复杂，核心入口是 `bashToolHasPermission()`。

主要机制：

- 先用 tree-sitter bash 做 AST security parse；太复杂或语义危险则 ask。
- fallback 到 legacy shell-quote / regex 安全检查。
- exact deny 优先。
- prompt-level deny / ask 可通过 classifier 判定。
- compound command 会拆分成子命令逐段检查。
- pipe / redirect 会额外检查原始命令，防 redirection 绕过。
- `checkPathConstraints` 处理 path command、redirect target、settings 文件等限制。
- sed in-place edit 有专门 preview/apply 权限路径。

规则匹配有几个很值得 Pulsara 借鉴的细节：

- allow rule 比 deny/ask rule 更保守。
- allow 匹配只剥安全 env var 和安全 wrapper。
- deny/ask 匹配会剥所有安全可剥 leading env var，防 `FOO=bar denied_command` 绕过。
- 不建议生成 `bash:*`、`sudo:*`、`env:*`、`xargs:*`、`nice:*` 等过宽规则。
- prefix rule 不匹配 compound command，防 `cd:*` 放行 `cd x && evil`。
- wrapper stripping 只允许安全 flag value，避免 `timeout -k$(id) 10 ls` 被错误归一化成 `ls`。
- subcommand 安全检查有数量上限，避免复杂 compound command 卡死 permission path。

Pulsara 当前的 hardline/risky 正则是好的 V1，但还偏轻量。下一步若继续增强 terminal 权限，最值得借鉴的是：

- allow/deny/risky 采用非对称归一化。
- hardline/risky 检测最好基于 shell AST 或 argv 粗解析，而不是只靠正则。
- 对 compound / pipe / redirect 的原始命令再检查一遍，避免拆分后丢掉 redirect 风险。

## 6. Sandbox

Claude Code 已接入 `@anthropic-ai/sandbox-runtime`，这是 Pulsara 当前没有的能力。

Sandbox adapter 会把 Claude Code settings 转换成 runtime config：

- network allow / deny domains。
- filesystem allowRead / denyRead / allowWrite / denyWrite。
- current directory 和 Claude temp directory 默认 writable。
- settings 文件、managed settings drop-in、`.claude/skills` 默认 deny write。
- bare git repo escape 文件有特殊 deny / post-command scrub。
- git worktree 主 repo `.git` 目录可被 allowWrite。
- 额外工作目录会进 allowWrite。
- sandbox 不可用时可报告原因，而不是静默失效。
- `dangerouslyDisableSandbox` 只有在 policy 允许 unsandboxed commands 时才生效。
- `excludedCommands` 明确只是用户 convenience，不是安全边界。

Pulsara 当前明确走“host shell / full access 是一等公民”，所以不需要立即复制 sandbox-runtime。但有两个可借鉴点：

- inspect 中应继续诚实显示 `execution_boundary=host`、`network_isolated=false`、`filesystem_sandbox=false`。
- 即使 full access，也可以加入“写 settings/skills/agent config 的额外 hardline 或 confirm”防自我提权。

## 7. Stop / kill / cancel

Claude Code 区分几类终止：

- foreground command 的 abort signal 通常会 kill process tree。
- reason 为 `interrupt` 时不 kill，而让 caller background process，使模型能看到 partial output。
- `TaskStopTool` / SDK `stop_task` 可以停后台 task。
- shell task stop 会 suppress 低价值 “exit 137” 通知，但直接发 SDK terminated event。
- agent 退出时 kill 该 agent 的后台 shell task。

Pulsara 当前已有：

- active run soft stop，产生 `aborted` run。
- pending approval stop。
- `terminal_process.kill`。
- session close kill owned processes。

差异：

- Pulsara 的 active run stop 不承诺强杀已经 yielded 的后台进程，这是诚实但偏底层。
- Claude Code 的后台 task stop 是产品能力，有 notification、SDK event、UI task 状态。

Pulsara 后续可以补：

- `stop_current_turn(kill_processes=True/False)` 或 UI 层单独 expose “stop run” 与 “kill terminal task”。
- stop run 时列出仍存活 process，避免 soft-cancel 后用户误以为命令也停了。

## 8. UI / 产品体验

Claude Code 在 terminal UI 上明显更成熟：

- Bash tool use summary 可用 `description`，否则 command truncation。
- long-running progress 不是单纯 dump 输出，而有 elapsed、line count、tail。
- 背景任务有可管理状态。
- 空输出会显示 Done / No output / return code interpretation。
- silent command detection 避免模型误判“没输出就是失败”。
- semantic command result interpretation 让非零或特殊返回码更可读。
- 大输出、后台输出、图片输出都有不同的 model-facing / UI-facing 表达。

Pulsara 当前 terminal 结果更像 JSON API。若要做 universal desktop agent，terminal 的产品层很值得优先补：

- 命令卡片 / 任务列表 / output viewer。
- exit summary。
- background notification。
- “没有输出但成功”的明确提示。
- “命令可能正在等交互输入”的提示。

## 9. 对 Pulsara 的补足建议

优先级建议：

1. 把 yielded process 升级为 first-class terminal task。
   - 保留 `terminal_process` 作为底层工具。
   - inspect / UI 展示 live / finished tasks。
   - background completion 注入模型上下文，减少 poll。

2. 增强输出 artifact 协议。
   - 返回 preview、full path、size、truncated reason。
   - 大输出允许模型用 read_file 打开。
   - 加 output file size watchdog。

3. 改进 permission matching。
   - deny/hardline/risky 使用更强归一化。
   - allow 使用更保守归一化。
   - 对 compound / pipe / redirect 加二次检查。

4. 增加 interactive prompt stall detection。
   - tail 不增长且最后一行像 prompt 时，提醒模型 kill/retry with stdin。
   - 这比无限 `poll` 更适合真实使用。

5. terminal stop 语义产品化。
   - stop run 不等于 kill process，要在 UI/result 里显式列出残留 process。
   - 给用户“一键 kill 本轮产生的所有 terminal tasks”。

6. 保持 full access 一等公民。
   - 不急着把 sandbox 作为默认主线。
   - 但可以加入更诚实的 inspect、更好的 hardline、更清晰的 bypass/full-access UI。

## 10. 不建议直接复制的部分

- 不建议立刻引入 Claude Code 同等级的 shell AST/classifier 全套系统，成本高且会牵动 permission UX。
- 不建议在 V1 把 sandbox-runtime 当主路径；Pulsara 的产品理念更接近 trusted host agent。
- 不建议把所有 background 机制隐藏成 UI 状态；Pulsara 的 `terminal_process` 显式 handle 对 agent 可控性很有价值。

更合理的路线是：保留 Pulsara 的底层通用 terminal/process 模型，在它上面补 Claude Code 式的任务状态、输出协议、stall detection 和更精细的权限归一化。
