# Pulsara Terminal Capability Gap Analysis

本文综合以下本地项目 terminal 调研文档：

- `TERMINAL_SURVEY_PULSARA.zh.md`
- `TERMINAL_SURVEY_CLAUDE_CODE.zh.md`
- `TERMINAL_SURVEY_CODEX.zh.md`
- `TERMINAL_SURVEY_HERMES.zh.md`
- `TERMINAL_SURVEY_OPENCLAW.zh.md`
- `TERMINAL_SURVEY_ANYBOX.zh.md`

目标不是复制某一个产品，而是判断 Pulsara 下一步 terminal 能力最该补什么。Pulsara 的愿景是 universal desktop agent，同时又把 trusted host / full access 当一等公民；因此改进方向应优先服务“更好用、更可观察、更可恢复、更诚实”，而不是假装把 host shell 变成安全沙箱。

## 1. 当前基线

Pulsara 已经具备一个不算弱的 terminal 底座：

- `terminal` + `terminal_process` 双工具。
- shell command 执行、PTY、stdin write/submit/EOF。
- `yield_time_ms` 后返回 `process_id`，长程任务可继续 poll/wait/kill。
- workspace cwd guard、terminal session current cwd。
- owner host session isolation。
- env sanitization、login shell snapshot、`.venv` overlay。
- output truncation、redaction、full output artifact。
- 三轴权限：permission profile / approval policy / terminal access。
- hardline deny、risky/on-request approval、approval resume、deny result、stop approval。
- active run soft stop、aborted/failure transcript note。
- inspect 诚实显示 host shell / non-isolated。

这些能力已经覆盖成熟 agent terminal 的底层核心。但调研显示，Pulsara 的短板主要不在“能不能跑命令”，而在命令运行之后的产品生命周期。

## 2. 成熟产品的共同形状

Claude Code、Codex、Hermes、OpenClaw 虽然实现差异很大，但 terminal 成熟度有几条共同线索。

### 2.1 Terminal task 是 first-class object

成熟实现不会只把长程命令当成一个裸 `process_id`：

- Claude Code 有 background task、task notification、output file、progress UI。
- Codex 有 unified exec process manager、process list、exec cells、output delta/end event。
- Hermes 有 `ProcessRegistry`、completion queue、watch patterns、checkpoint。
- OpenClaw 有 process registry、system event、heartbeat wake、process log/poll/list。
- Anybox 虽轻，但也有 background task id 和 read/stop。

Pulsara 当前 `terminal_process` 是好的底层，但还不像产品任务。

### 2.2 后台完成需要主动唤醒

这是最稳定、最强的结论：

- Claude Code 的 `<task_notification>`。
- Hermes 的 process completion queue / notification。
- OpenClaw 的 `notifyOnExit` + system event + heartbeat。

Pulsara 当前需要模型主动 `poll` 或 `wait`。这在真实 dogfood 中会变成反复轮询、忘记检查、长任务完成无人知道。

### 2.3 输出需要 artifact 协议，而不是附带路径

成熟实现普遍把大输出做成可被模型和 UI 都理解的对象：

- Claude Code 的 `<persisted-output>`。
- Hermes 的 persisted-output 风格。
- OpenClaw 的 pending/aggregate/log 分层。
- Codex 的 output delta/end events 和 exec history cells。

Pulsara 已有 `.pulsara/terminal-output/<process_id>.txt`，但它还只是 terminal 内部 ref。下一步应把它升级成明确协议：preview、path、size、truncated reason、how to read more。

### 2.4 交互式进程需要比 write/submit 更丰富的输入

OpenClaw 和 Codex 都说明了这一点：

- `send-keys`。
- bracketed paste。
- key token / hex / literal。
- cursor key mode。
- waiting-for-input hint。

Pulsara 已支持 PTY + write/submit/EOF，这是基础。下一步的差距是让模型更容易处理 REPL、TUI、installer、认证 prompt。

### 2.5 Approval 要有缓存和可解释规则

Pulsara 已经把 approval resume 做出来，这是关键前置。成熟产品更进一步：

- Codex 有 prefix/session approval。
- Hermes 有 session/permanent allowlist。
- OpenClaw 有 approvals file、allowlist、ask/on-miss/full、native approval。
- Anybox 有 persisted request/audit schema。

Pulsara 现在可恢复，但每次都问会很烦。下一步是 approve once/session/always、prefix rule、approval audit。

### 2.6 Full access 不等于没有 UX

Claude Code/Codex/OpenClaw 都有 bypass/full/full-access 形态，但它们仍做：

- task list。
- output management。
- stop/kill。
- approval cache。
- shell policy explanations。
- sandbox/host boundary inspect。

这正好符合 Pulsara 的方向：full access 是一等公民，但 terminal 仍需要生命周期和透明度。

## 3. Pulsara 比它们做得好的地方

### 3.1 Env sanitization 与开发环境贴合

Pulsara 的 terminal env 设计很强：

- provider/API secret 默认不继承。
- toolchain env allowlist。
- login shell snapshot。
- `.venv` overlay。
- proxy/toolchain/PATH 合并。

在被调研项目里，这个组合很少同时出现。Anybox 有 shell resolver，Claude/Hermes/OpenClaw 有各自 env 清理，但 Pulsara 对本地 Python/uv 开发工作流的贴合更细。

### 3.2 Approval resume 与 stop note 已经打通

Pulsara 近期补上的 approval resume、deny result、pending approval stop、active run abort note，是很多轻量项目没有的。

这意味着 Pulsara 已经可以放心打开 `terminal_access=ask` 和 `approval_policy=on_request` 这类高频审批路径，并且可以 dogfood 它们。

### 3.3 Host/full-access 语义诚实

Pulsara 的 inspect 和文档明确说：

- terminal 是 host shell。
- filesystem sandbox 不存在。
- network 不隔离。
- workspace cwd guard 不是安全边界。

这点比一些“inside project boundary”但仍执行 host shell 的文案更诚实。对 universal desktop agent 来说，诚实的安全边界本身就是产品能力。

### 3.4 `terminal_process` action 面比部分项目更完整

Pulsara 已有：

- `poll`。
- `wait`。
- `kill`。
- `write`。
- `submit`。
- `close_stdin`。

Anybox background task 没有 stdin；Claude Code 主 Bash 路径偏非交互。Pulsara 的底层交互能力是不错的，只是还需要产品化。

## 4. Pulsara 的核心差距

### P0：后台任务完成事件与唤醒

问题：

- 长程 terminal process 完成后，模型/用户必须主动 poll 才知道。
- dogfood 中很容易出现“测试已经跑完，但 agent 还在等/忘了查”的体验。

建议：

- 新增 `TerminalProcessCompletedEvent` 或等价 host event。
- yielded process 退出时发 event。
- host session 能把 completion note 注入下一轮 prior context。
- UI/inspect 显示 recent completed terminal tasks。
- 成功且无输出是否通知可配置。

这是最值得先做的 terminal 增量。

### P0：process list / log / poll 产品化

问题：

- 当前 `terminal_process` 更像底层 RPC。
- 用户和模型缺少“当前有哪些 terminal task”的共同视图。

建议：

- 增加 `terminal_process(action="list")`。
- 增加 `log`，支持 line offset/limit，默认 tail。
- `poll` 返回 delta cursor、status、duration、cwd、command summary、truncated reason。
- inspect 输出 live/finished process count 和简要列表。
- stop run 后明确列出仍存活 process。

这不需要引入新 sandbox，是对已有 registry 的产品化。

### P1：统一输出 artifact 协议

问题：

- `full_output_ref` 目前只是 terminal result 字段。
- 大输出和后续读取缺少统一模型说明。

建议：

- 设计 `<terminal-output>` 或通用 `<persisted-output>` block。
- 字段包含 path、size、preview、truncated reason、read instructions。
- `terminal_process.poll/wait/log` 都使用同一协议。
- 加 output file size watchdog。
- 考虑 stdout/stderr 分流，至少在 metadata 中保留来源。

### P1：交互输入增强

问题：

- `write` / `submit` 对简单 stdin 足够，但对 TUI/REPL/installer 不够自然。

建议：

- `send_keys`：Enter、Ctrl-C、Arrow、Tab 等 token。
- `paste`：默认 bracketed paste。
- `waiting_for_input`：基于 idle + stdin writable + tail prompt。
- PTY cursor key mode。
- 对 poll/wait 返回“可能在等输入”的 hint。

这会显著提升 terminal 作为 universal desktop agent 的适用面。

### P1：approval cache / allowlist

问题：

- on_request/ask 可恢复后，常态审批会暴露重复确认成本。

建议：

- approve once/session/project/always。
- prefix allow rule。
- approval prompt 显示 command、cwd、env keys、risk reason。
- approval audit 可查询。
- hardline deny 仍保持极少数不可覆盖。

这和“full access 当一等公民”不冲突：默认可以 full，但用户选择问时，体验不能笨重。

### P2：foreground/background 语义与 stop 产品化

问题：

- Pulsara 的 `yield_time_ms` 很工程化，但用户不一定理解 foreground/yield/background 的状态。
- stop run 现在是 soft cancel，不等价 kill process。

建议：

- terminal result 明确“command is still running as task X”。
- 支持模型主动 `background=true` 或更清楚的 `yield_after_ms` alias。
- stop active terminal call 可选 interrupt/kill foreground process。
- stop run 后列出 live process，并给 kill/follow 操作。
- 对疑似 server/watch 命令给出 background nudge。

### P2：durable process metadata

问题：

- app 重启后 process registry 丢失。
- full output artifact 还在，但 live handle 无法恢复。

建议：

- checkpoint process metadata。
- 重启后 detect orphaned process。
- 能 list/kill known detached process。
- 先做 metadata + honest stale state，不必马上实现完整 attach。

Hermes 在这里最强，但实现成本也更高，适合放在 process list/completion event 之后。

### P2：shell risk 解析增强

问题：

- 当前 hardline/risky 主要靠正则。
- shell wrapper、compound command、redirect、pipe 都可能让规则漏判或误判。

建议：

- 保留简单 hardline V1。
- 对 deny/hardline/risky 做更强归一化。
- allow 规则更保守，deny/risky 规则更积极。
- 检查 compound / pipe / redirect 的原始命令。
- 可借鉴 Claude Code 的 tree-sitter bash，但不要急着完整复制。

### P3：可选 sandbox / remote backend axis

问题：

- Pulsara 未来如果做 universal desktop agent，低风险模式和远端执行迟早会出现。

建议：

- 不要把 sandbox 混进 permission profile。
- 单独建 runtime boundary axis：host / sandbox / remote。
- host/full access 保持默认一等路径。
- sandbox 作为可选 backend，inspect 明确显示实际边界。
- elevated/escape hatch 只影响 terminal target，不等价工具权限。

这个方向重要，但不是下一步最高优先级。当前更该补 terminal task 生命周期。

### P3：watch patterns 与 stall detection

问题：

- 长程命令可能等输入、卡住、输出特定错误。

建议：

- `notify_on_complete` 先做。
- 再做 `watch_patterns`，必须带 rate limit。
- 对 tail 像 `(y/n)`、`Press Enter`、`Overwrite?` 的 process 发 waiting-input hint。
- 避免无限通知打扰模型。

Hermes 和 Claude Code 都证明这有价值，但它依赖前面的 completion/task event 基础。

## 5. 不建议现在做的事

### 5.1 不建议先重写 terminal 为 persistent shell

Anybox 的 persistent PTY 对桌面 UI 很好，但 Pulsara 当前 per-command subprocess 更适合：

- 审计。
- output artifact。
- owner isolation。
- approval/retry/stop。

未来可以新增 persistent terminal panel，但不应替换现有 `terminal`。

### 5.2 不建议把 sandbox 作为 terminal 改进第一步

Codex/Claude/OpenClaw 的 sandbox 很复杂。Pulsara 现在的产品原则是 trusted host/full access 一等公民。更务实的路线是：

1. 先让 host terminal 更好用。
2. inspect 持续诚实。
3. 再加可选 sandbox/remote axis。

### 5.3 不建议把 `terminal_process` 压缩成单个 `write_stdin`

Codex 的 `write_stdin` 协议很紧凑，但 Pulsara 显式 action 对 UI、审计、权限更友好。可以增加 convenience action，但不必删除现有 action 面。

### 5.4 不建议立即复制完整 shell AST classifier

Claude Code 的权限解析很成熟，但成本高、牵动大。Pulsara 可以先做：

- hardline 正则继续收紧。
- terminal_process stdin hardline 已有。
- risky path 和 command 规则补缺。
- 后续再引入 AST/argv 解析。

## 6. 推荐实施顺序

### PR1：Terminal Task Inventory

目标：让已有 process registry 可见。

内容：

- `terminal_process list`。
- `terminal_process log`。
- `poll/wait` 返回 cursor/status/duration/cwd/command summary。
- host inspect 显示 live/finished terminal tasks。
- stop run 后提示 live tasks。

收益：低风险，高 UX 增益，为后续 completion event 铺路。

### PR2：Terminal Completion Event

目标：后台任务完成不再靠模型轮询。

内容：

- yielded process exit 时发 event。
- session recent terminal task note。
- prior context 注入轻量 completion note。
- UI/CLI 可展示 completion notification。

收益：解决真实 dogfood 最痛的长程命令观察问题。

### PR3：Persisted Output Protocol

目标：把 `.pulsara/terminal-output` 升级成模型可用协议。

内容：

- 结构化 output artifact block。
- preview/path/size/truncation/read-more。
- size watchdog。
- `log/poll/wait` 一致使用。

收益：降低大输出丢失和 transcript 噪声。

### PR4：Interactive Input UX

目标：让 agent 能处理真实 CLI/REPL/TUI。

内容：

- `send_keys`。
- `paste` / bracketed paste。
- `waiting_for_input` hint。
- poll tail prompt detection。

收益：terminal 变成更万能的桌面工具。

### PR5：Approval Cache / Audit

目标：让 ask/on_request 能长期使用。

内容：

- approve session/always/prefix。
- approval audit store。
- host inspect pending/recent approvals。

收益：把高频审批从“能恢复”变成“好用”。

### PR6：Durable Process Metadata

目标：app 重启后不把 terminal 任务变成黑箱。

内容：

- checkpoint metadata。
- stale/orphan detection。
- restart inspect。
- best-effort kill orphan。

收益：为 desktop long-running tasks 打底。

## 7. 结论

Pulsara 的 terminal 底座已经不弱。它当前真正缺的不是“再加一个能跑命令的工具”，而是把 terminal 从工具调用升级成桌面 agent 的任务系统：

- 可列出。
- 可跟随。
- 可唤醒。
- 可停止。
- 可审计。
- 可恢复。

下一步最值得做的是 `process list/log` 和 completion event。它们最贴近 Claude Code、Codex、Hermes、OpenClaw 的共同成熟方向，也最符合 Pulsara “full access 是一等公民，但必须诚实、可控、好用”的产品判断。
