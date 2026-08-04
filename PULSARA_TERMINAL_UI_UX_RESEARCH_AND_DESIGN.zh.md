# Pulsara Terminal UI/UX 调研与目标设计

> 文档性质：竞品代码真值调研 + Pulsara Terminal 产品设计基线 + 规范索引
> 状态：ACTIVE PRODUCT BASELINE；Bubble Tea v2已通过S0 feasibility gate，冻结为长期一等Terminal客户端
>
> 当前实施边界（2026-08-03）：renderer-neutral Python infrastructure baseline、Go-ready Protocol 2.0原子切换、attachment-attempt handshake recovery、atomic five-section control baseline、three-plane observation wire prerequisite与typed active-queue genesis extension已完成；隔离S0 feasibility spike为`PASS`，Bubble Tea S1只读production纵切为`IMPLEMENTED`。S2–S6行为、正式四平台Go packaging与默认TTY activation仍为`NOT STARTED`。S0证据不得计入Python Foundation或S1完成证据。
> 规范口径：本文拥有`TUI-UX-*`产品行为与跨规格总边界；其中保留的DTO、算法和伪代码用于解释设计来源，不再充当唯一implementation authority
> 调研对象：
>
> - MiMo-Code：本地提交 `d056619`
> - Claude Code：本地提交 `5a774a2`
> - Codex：本地提交 `6138909d6e`
> - Pulsara：本文编写时当前工作树

### 规范索引

| Requirement namespace | 唯一owner文档 | 内容 |
|---|---|---|
| `TUI-UX-*` | 本文 | 产品目标、UX行为、跨层authority与验收口径 |
| `TUI-FND-*` | `PULSARA_TERMINAL_PRESENTATION_FOUNDATION_IMPLEMENTATION.zh.md` | Python receipt、tap、projection、viewport、queue与interaction application services |
| `TUI-PROTO-*` | `PULSARA_TERMINAL_CLIENT_PROTOCOL_CONTRACT.zh.md` | 本地transport、Protobuf、attachment、cursor、command、receipt与secret channel |
| `TUI-BT-*` | `PULSARA_BUBBLE_TEA_CLIENT_IMPLEMENTATION.zh.md` | Go/Bubble Tea Model、Update、View、TTY、layout、测试与发布 |
| `TUI-COMPAT-*` | `PULSARA_LEGACY_REPL_RETENTION_CONTRACT.zh.md` | 冻结的prompt_toolkit历史入口及其禁止扩张边界 |

每项normative requirement只能在上表的一份owner文档中完整定义。本文中的implementation-shaped说明若与owner子规格冲突，以子规格为准；子规格不得反向改变本文的`TUI-UX-*`产品行为，除非同一变更先更新本文并说明产品取舍。

### Traceability ledger

| Requirement family | Owning document | Planned code owner | Planned test owner |
|---|---|---|---|
| `TUI-FND-EVT/OBS-*` | Foundation | `primitives.stored_event`、`event_log`、`runtime.terminal_presentation.observation` | Python unit + EventLog PostgreSQL integration |
| `TUI-FND-PROJ/VIEW-*` | Foundation | `runtime.terminal_presentation.projection/viewport` | pure reducer + bounded page tests |
| `TUI-FND-CMD/QUEUE/INT-*` | Foundation | Terminal application services、prompt queue、Host interaction adapters | command/queue/interaction integration |
| `TUI-PROTO-*` | Protocol | `.proto`、Python Gateway、generated Python/Go adapters | schema golden + cross-language + socket fault tests |
| `TUI-BT-S0-*` | Bubble Tea | disposable Go/Python feasibility probe | PTY、CJK/IME、tmux/SSH、packaging evidence |
| `TUI-BT-APP/VIEW/COMP/KEY-*` | Bubble Tea | `clients/terminal/internal/*` | Go model/view golden + PTY |
| `TUI-BT-INT/SECRET/QUEUE-*` | Bubble Tea | Go interaction、secret和queue components | Go + cross-language integration |
| `TUI-COMPAT-*` | Legacy retention | `host.legacy_repl`、`repl.py` | legacy smoke + AST no-growth |

实施PR必须把具体requirement ID、最终文件symbol和test node补入对应子规格；不能只引用family或本文行号。

## 0. 执行摘要

Pulsara 不应完整照搬任何一个现有 coding agent TUI。

三个调研对象各自最值得借鉴的部分不同：

| 项目 | 最值得借鉴的能力 | 不宜直接照搬的部分 |
|---|---|---|
| MiMo-Code | 产品化入口、命令面板、响应式侧栏、丰富 composer、插件化 slot | 一次引入 voice、主题、侧栏、插件和大量工具组件；前端镜像过多后端状态 |
| Claude Code | 默认折叠、语义化工具聚合、长任务真实进度、重要结果优先、typed approval | 巨型 PromptInput、feature flag 堆叠、进程内 command queue、隐藏快捷键过多 |
| Codex | transcript/active cell/bottom pane 分层、运行中输入、steer/follow-up 区分、显式状态机 | 大量专用 cell 和 Rust TUI 的实现复杂度 |

对 Pulsara 的推荐组合是：

1. 采用 Codex 式的交互 ownership 与状态机骨架。
2. 采用 Claude Code 式的渐进披露与语义聚合。
3. 在基础交互稳定后，再引入 MiMo-Code 式的命令面板、响应式侧栏和插件化扩展点。

Pulsara 当前最影响体验的并不是颜色、主题或动画，而是 REPL 控制流：

- 主循环等待整轮 `run_turn()` 完成后才重新读取输入；
- 运行期间没有 composer；
- 同一个 REPL 无法自然接收 stop、steer 或 follow-up；
- 工具、subagent、MCP、compaction 和后台 terminal process 没有统一的可见状态；
- approval、plan interaction 和 MCP input-required 仍大量依赖命令或 JSON 输出；
- 最终文本之外的 durable runtime 事实尚未投影成稳定的人类界面。

好消息是，Pulsara 已经拥有比普通 CLI 更强的底层基础：

- `HostSession.stream_turn()`；
- typed `AgentEvent`；
- durable EventLog；
- stable run/session/tool identities；
- approval、plan、MCP、terminal monitor、subagent 和 compaction 的 typed facts；
- session-owned worker、close/drain 和 recovery contract。

因此本阶段应新增的是 UI projection 与交互 controller，而不是把 UI 逻辑重新塞进 AgentRuntime。

本文同时冻结九条实施前提：

1. `HostSession.stream_turn()` 只保留为一次 activation 的兼容观察入口，不充当长期 UI event bus。
2. EventLog normal commit直接返回完整physical `StoredEventBatchCommitReceipt`；只有持有exact prepared candidate identity的FULL confirmation可重建同形receipt。Generic restart/doctor/catch-up只能构造连续`JoinedRawStoredEventRangeProof`，不得伪造transaction batch。
3. EventLog queue transition chain是prompt queue唯一semantic authority；versioned checkpoint拥有canonical genesis与hard admission bound，production reopen只fold bounded typed delta。Terminal snapshot只呈现Foundation定义的bounded active client projection，不携带全部历史queue rows，也不允许client截断。
4. Enter获得`Queued` acknowledgement前，prompt queue acceptance必须已经durable FULL并通过companion固定保守physical charge；large-paste artifact由`PREPARED -> CONSUMED -> RELEASED` hold覆盖完整生命周期。
5. Full-screen transcript从S1起使用bounded viewport与paged history，不把完整session transcript常驻内存。
6. status/render热路径只能读取O(1) process-local projection，不得同步扫描PostgreSQL EventLog。
7. MCP encrypted continuation是durable secret authority，Host owner是唯一decrypt/hydration authority；shared revocation cell的generation是owner epoch而非borrow ordinal，只保证旧borrow无法再次reveal，不承诺撤销已返回/显示的plaintext。
8. physical commit、exact confirmation、publication、UI observation和queue domain disposition使用不同closed vocabulary。
9. Durable history只按transcript-owned stable placement coordinate与presentation placement key寻址；checkpoint冻结prefix cut并在FULL时保留并发suffix，continuous history ordinal和FULL-implies-empty-tail均不是合法实现。

## 1. 调研范围与口径

### 1.1 调研对象

本文只基于本地仓库代码真值，不依赖产品宣传材料。

Codex 部分指本地开源仓库中的 Rust/Ratatui TUI，不代表 Codex Desktop 的闭源界面实现。

Claude Code 仓库中存在大量构建变体和 feature flag。本文只将代码中已经形成清晰 ownership 或 UI pattern 的部分视为可借鉴设计，不假设所有路径均在同一产品版本中启用。

### 1.2 比较维度

统一比较以下问题：

1. 启动和首屏如何反馈。
2. composer 如何处理历史、粘贴、附件、命令与自动补全。
3. agent 运行期间是否仍可输入。
4. steer、follow-up、interrupt 如何区分。
5. 流式文本和工具活动如何呈现。
6. 长任务如何避免“看起来卡死”。
7. approval、question 和 plan 如何接管交互。
8. background task、subagent、MCP 和 terminal process 如何显示。
9. transcript 如何兼顾紧凑与可审计。
10. UI 状态是否会形成 runtime 之外的第二真源。

### 1.3 本文不覆盖

本文暂不冻结：

- GUI/Web/Desktop 技术栈；
- CSS、像素级布局或品牌视觉；
- 每个模块的最终 Python 文件路径；
- 完整 durable event schema；
- 移动端或浏览器端交互；
- 无障碍规范的最终认证等级；
- 每一种工具的最终文案。

## 2. Pulsara 当前代码真值

### 2.1 当前 REPL 是顺序式输入循环

当前交互输入由 `prompt_toolkit.PromptSession` 提供：

- 历史文件；
- history search；
- history auto-suggest；
- suspend；
- redirected stdin fallback。

代码入口见：

- [repl.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/repl.py:18)
- [repl.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/repl.py:52)

它目前仍是“读取一行 → 执行 → 打印结果 → 再读取一行”的 REPL：

- [cli.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py:1661)
- [cli.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py:2056)

最终输出主要依赖 `AgentRunResult.final_text`：

- [cli.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py:1276)

这意味着当前 `:stop` 虽然存在，但主 REPL 在 `await session.run_turn()` 期间不会继续读取键盘命令。同一终端用户不能在普通交互流中输入 `:stop`。

### 2.2 当前已有可复用的 run streaming seam，但不是 UI event bus

`HostSession.stream_turn()` 已经：

- 同步捕获 ingress；
- 创建 session-owned boundary task；
- 绑定 observer；
- 返回 `AsyncIterator[AgentEvent]`；
- 将 boundary task 与 session lifecycle 对齐。

见：

- [session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py:3341)

因此 UI 不需要直接接管 AgentRuntime，也不应自己持久化 model/tool lifecycle。

但该接口不能直接承担 full-screen UI 的长期 observation contract：

- 它只覆盖一次 initial/resume activation，activation terminal 后 iterator结束；
- 它不提供 session bootstrap high-water、reconnect cursor 或 gap repair；
- Host 外层 `_StreamObserver.emit()` 在 queue 满时会等待，慢消费者可能阻塞 Host boundary/ingress 的收尾；
- background terminal、MCP、subagent和 session-level状态并不都属于同一 activation stream。

底层 `RunExecutionRegistry` 已有更正确的语义：slow observer queue 满时 detach，不反向取消或阻塞 run driver。新的 UI feed 应复用这一 ownership 原则，但不能让 UI 直接借用 run registry 的内部对象。

### 2.3 当前状态事实已经足够丰富

现有 Host/runtime 能够提供：

- session open/close/recovery；
- active run 和 run boundary；
- model stream；
- tool call/result；
- approval；
- plan question/exit；
- MCP input-required；
- MCP installation；
- subagent task/run；
- terminal monitor；
- context compaction；
- rollout/context status；
- Inspector projection。

真正缺少的是把这些事实折叠成“用户现在应该看到什么”的 UI reducer。

### 2.4 当前主要 UX 缺口

| 缺口 | 用户感受 | 结构原因 |
|---|---|---|
| 运行中无法继续输入 | 像同步脚本，不像长期 agent | REPL 主循环等待 `run_turn()` |
| 没有 live activity | 不知道是在思考、读文件、跑命令还是等待 | 只打印最终结果 |
| `:stop` 不自然 | 需要另一个控制面或等当前调用返回 | input owner 与 run owner 未分离 |
| approval 依赖命令/JSON | 用户需记忆 `:approve`、`:deny` | 缺 typed interaction view |
| MCP/plan 状态散落 | 用户不知道当前 mode 和 capability | 缺固定 status surface |
| 工具事件不分层 | 若直接全量打印会产生海量噪音 | 缺 semantic grouping policy |
| 后台 process/subagent 不稳定可见 | 长程任务缺持续反馈 | 缺 persistent activity region |

### 2.5 当前 Host summary 不是 renderer-safe snapshot

`HostSession.summary()` 当前会同步构造 long-horizon 和 compaction projection，其中包含有界但仍可能很大的 EventLog 查询与 decode。它适合作为显式 `:status`/诊断入口，不适合作为每秒刷新或每帧 render 的数据源。

full-screen UI 必须新增 O(1) 的 process-local `TerminalUiSessionSnapshot`。数据库 hydration 只能发生在 session-owned async bootstrap/recovery operation 中；renderer、status line和animation callback不得触发 SQL、artifact读取或 graph查询。

### 2.6 当前 ingress queue 不是 prompt queue authority

`HostIngressCoordinator` 已经是 human/resume/runtime boundary 的线性化 owner，但其 queue：

- 归单个 live `HostSession` 所有；
- 只存在于进程内；
- `run_turn()` 默认 busy 时拒绝；
- waiting-user期间拒绝非 matching resume；
- 没有 user prompt的 durable acceptance、cancel、replace或safe-point consumption事实。

它可以继续作为边界执行仲裁器，但不能被重命名或直接暴露为 UI prompt queue。durable prompt queue必须是 runtime-session scoped 的独立 owner，并在 dispatch 时借用 Host ingress。

## 3. MiMo-Code 调研

### 3.1 架构：client/server projection TUI

MiMo-Code 使用 OpenTUI + Solid。

TUI 建立集中式 reactive store，保存：

- provider；
- session；
- session status；
- goal、diff、todo、task；
- message 和 part；
- permission、question；
- LSP、MCP；
- actor；
- workflow run、transcript 和 structure。

见：

- [sync.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/context/sync.tsx:158)

store 通过 server event subscription 增量更新：

- [sync.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/context/sync.tsx:263)

优点是 UI 与 server 生命周期解耦，多个组件共享同一 projection。

风险是 UI store 手工镜像大量 backend shape。若没有明确 projection contract，很容易形成第二套半权威状态。

### 3.2 响应式布局

MiMo-Code 在终端宽度大于 120 列时自动启用 sidebar，并预留固定宽度；窄终端将 sidebar 作为 overlay：

- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/routes/session/index.tsx:223)
- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/routes/session/index.tsx:1343)

这是值得 Pulsara 后期借鉴的响应式策略：

- 80–119 列：单列 transcript + bottom pane；
- ≥120 列：增加可选 context/status sidebar；
- sidebar 永远不是完成基础交互的前置条件。

### 3.3 命令入口统一

MiMo-Code 的 command registry 同时拥有：

- 稳定 command ID；
- 可本地化标题；
- slash alias；
- keybinding；
- searchable keywords；
- suggested 状态；
- visible/enabled 状态。

见：

- [dialog-command.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/component/dialog-command.tsx:32)
- [dialog-command.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/component/dialog-command.tsx:61)

这避免了三套漂移：

- `:help` 有一个列表；
- slash autocomplete 有另一个列表；
- 快捷键又硬编码第三套列表。

Pulsara 应采用同类统一注册表，但 command handler 只能调用 Host/API，不得直接修改 runtime state。

### 3.4 Composer

MiMo-Code composer 支持：

- 多行输入；
- history；
- stash/list/pop；
- 外部编辑器；
- clipboard paste；
- image/file references；
- `@`/slash autocomplete；
- agent/model selection；
- shell mode；
- voice input/control；
- inline `/btw` side question。

相关入口：

- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx:90)
- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx:944)
- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx:1131)

值得借鉴的是 draft preservation、stash 和统一 autocomplete。

Voice 不应进入 Pulsara UI 第一阶段。它增加：

- provider/config dependency；
- microphone permissions；
- recording lifecycle；
- partial transcript；
- voice command ambiguity；
- 新的 error and privacy surface。

### 3.5 运行中输入与中断

MiMo-Code 使用异步 `promptAsync()`，运行中的后续 user message 会在 transcript 中标记为 queued：

- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/routes/session/index.tsx:1415)
- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx:1180)

中断采用重复按键保护，并给出 `again to interrupt` 提示：

- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx:570)

这种保护适合“中断会丢掉当前流”的客户端；Pulsara 已有 stable termination 和 durable terminalization，更适合使用显式状态：

- 第一次 Esc：request stop；
- UI 立即显示 `Stopping…`；
- 再次 Esc 不创建第二个 stop owner；
- 超过 deadline 后显示 `Stop pending; close is blocked`；
- 不用重复按键次数推断 durable outcome。

### 3.6 工具展示

MiMo-Code 将工具分为：

- `InlineTool`：一行完成的低噪音操作；
- `BlockTool`：需要正文、diff 或复杂状态的操作。

见：

- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/routes/session/index.tsx:2482)
- [index.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/routes/session/index.tsx:2583)

它还区分：

- permission pending；
- completed；
- denied；
- recoverable error；
- real error。

特别值得借鉴的是：agent 可自我纠正的 recoverable error 不使用醒目的红色大块，避免用户误以为任务整体失败。

### 3.7 Permission 和 question

Permission view 根据 edit、read、glob、grep、bash、task、web 等类型生成不同正文；edit permission 使用真实 diff，并根据宽度选择 split/unified：

- [permission.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/routes/session/permission.tsx:62)
- [permission.tsx](/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/cli/cmd/tui/routes/session/permission.tsx:208)

Question view 支持：

- 多问题；
- tabs；
- 单选/多选；
- 数字快捷键；
- custom answer。

Pulsara 的 plan question 和 MCP input-required 可以复用同一 interaction framework，但不能把它们压成同一个无类型 JSON form。

### 3.8 对 Pulsara 的结论

优先吸收：

- command registry；
- responsive sidebar；
- composer draft/stash；
- inline/block tool taxonomy；
- typed permission/question view。

延后：

- voice；
- 大量主题；
- mouse hover；
- plugin-defined任意 UI mutation；
- workflow full-screen route；
- UI 内部手工镜像全部 backend DTO。

## 4. Claude Code 调研

### 4.1 架构特点

Claude Code 使用 React/Ink，并拥有高度组件化的 message、tool、permission、prompt 和 status line。

它最成熟的部分不是布局，而是信息密度控制：

- 默认只显示用户需要知道的变化；
- 重复操作聚合；
- 详细事实仍可展开；
- 快速变化经过 minimum display time 和 monotonic counters 降低抖动；
- 真正业务结果优先于过程日志。

### 4.2 语义化工具聚合

`CollapsedReadSearchContent` 将多次：

- read；
- search；
- list；
- REPL；
- MCP；
- bash；
- memory；

合并成一条 activity summary：

- [CollapsedReadSearchContent.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/messages/CollapsedReadSearchContent.tsx:142)

关键细节：

1. 最新 hint 最少显示 700ms，避免一帧闪过。
2. 计数只增长，不因 streaming 中间态短暂回落而抖动。
3. active 使用现在时，terminal 使用过去时。
4. verbose mode 仍可查看每个 tool call。
5. commit、push、PR 等 load-bearing outcome 优先显示。

见：

- [CollapsedReadSearchContent.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/messages/CollapsedReadSearchContent.tsx:26)
- [CollapsedReadSearchContent.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/messages/CollapsedReadSearchContent.tsx:168)
- [CollapsedReadSearchContent.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/messages/CollapsedReadSearchContent.tsx:298)

这是 Pulsara 最应直接吸收的显示原则。

### 4.3 长任务不只显示 spinner

Claude Code 对运行超过 2 秒的 shell command 显示：

- elapsed time；
- output line count。

见：

- [CollapsedReadSearchContent.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/messages/CollapsedReadSearchContent.tsx:269)

这解决了 coding agent 最常见的 UX 问题之一：

> 用户无法区分“命令仍在运行”“event loop 死锁”“publisher 卡住”“模型正在思考”。

Pulsara 的 terminal monitor 已有：

- process identity；
- live output；
- elapsed/lifecycle；
- bounded UI stream；
- terminal completion。

因此可以提供比简单 spinner 更准确的状态。

### 4.4 Command queue

Claude Code 的 command queue 是 module-level、React-independent 状态：

- user input；
- task notifications；
- orphaned permissions；
- priority `now > next > later`；
- 同优先级 FIFO。

见：

- [messageQueueManager.ts](/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/messageQueueManager.ts:41)

UI 通过 `useSyncExternalStore` 订阅 immutable snapshot：

- [useCommandQueue.ts](/Users/plumliu/Desktop/python_workspace/claude-code/src/hooks/useCommandQueue.ts:8)

通知会：

- 过滤 silent idle notification；
- 最多展示三项 task notification；
- 其余合并为 `+N more tasks completed`。

见：

- [PromptInputQueuedCommands.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/PromptInput/PromptInputQueuedCommands.tsx:29)

这是优秀的 UI 降噪，但不是 Pulsara 所需的 queue authority：

- 它是 process-local；
- 不能天然支持 HostSession detach/resume；
- 不能证明 safe-point delivery；
- 不能作为跨 client 的唯一真源。

Pulsara 应借鉴它的 priority 和 bounded notification projection，不复制其 ownership。

### 4.5 Typed permission

Claude Code 根据工具类型选择独立 permission component：

- file edit；
- file write；
- bash；
- PowerShell；
- web fetch；
- notebook；
- plan enter/exit；
- skill；
- ask-user；
- workflow；
- monitor。

见：

- [PermissionRequest.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/permissions/PermissionRequest.tsx:47)

这说明 approval UI 的核心不是“有一个确认框”，而是：

- 用户明确知道动作；
- 用户明确知道风险；
- 用户看得到关键参数；
- 用户知道一次允许、session 允许和拒绝的差别；
- 编辑类动作优先展示 diff；
- 网络类动作优先展示 host/target。

### 4.6 Status line

Claude Code 支持用户命令生成 status line，输入包括：

- model；
- cwd/project；
- version；
- cost/context usage；
- rate limits；
- Vim mode；
- agent；
- worktree。

它使用 debounce、cancel in-flight 和 stable height，避免每个 stream delta 都触发昂贵刷新。

见：

- [StatusLine.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/StatusLine.tsx:195)
- [StatusLine.tsx](/Users/plumliu/Desktop/python_workspace/claude-code/src/components/StatusLine.tsx:309)

Pulsara V1 不应执行用户自定义 shell status command。更稳妥的是：

- 先提供 typed internal segments；
- segment 有明确成本和更新频率；
- 后续若提供扩展，只允许 bounded、secret-safe、可取消的 provider。

### 4.7 对 Pulsara 的结论

优先吸收：

- semantic grouping；
- compact/verbose 双层视图；
- minimum display time；
- monotonic progress；
- elapsed + output count；
- background notification cap；
- typed permission renderer；
- stable status height。

谨慎吸收：

- module-level queue；
- user shell status hook；
- 大量隐式快捷键；
- 大型 feature flag matrix；
- 将所有功能塞入单个 PromptInput。

## 5. Codex 调研

### 5.1 架构：显式交互状态机

Codex 的 TUI 最值得借鉴的是 ownership，而不是 Rust/Ratatui。

它明确区分：

- finalized transcript；
- active cell；
- composer；
- bottom-pane view stack；
- status widget；
- pending input preview；
- approval；
- queued interruptive UI events。

`TranscriptState` 单独持有 active cell revision、copy history、plan streaming 和 turn flags：

- [transcript.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/chatwidget/transcript.rs:13)

`BottomPane` 持有 composer，同时保留临时 view stack。approval/question 出现时 view 替代 composer，但 composer 对象本身不销毁：

- [mod.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/bottom_pane/mod.rs:203)

这个分层非常适合 Pulsara：

- durable transcript 和 live activity 不混；
- interaction view 和 composer draft 不混；
- terminal notification 和 model reply 不混；
- UI repaint 不改变 runtime lifecycle。

### 5.2 输入 queue 的语义分层

Codex 将输入分为：

1. `queued_user_messages`：等待下一 turn；
2. `pending_steers`：已提交 core、尚未进入历史；
3. `rejected_steers_queue`：当前非 regular turn 无法接纳，稍后重试；
4. `user_turn_pending_start`：已交 core，但 `TurnStarted` 尚未到达。

见：

- [input_queue.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/chatwidget/input_queue.rs:21)

运行中的普通输入会形成 pending steer：

- [input_submission.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/chatwidget/input_submission.rs:322)

UI 明确告诉用户：

- 将在下一个 tool call boundary 提交；
- 按 Esc 可立即 interrupt 并发送；
- queued follow-up 可以取回编辑。

见：

- [pending_input_preview.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/bottom_pane/pending_input_preview.rs:13)

这比只有一个模糊“queued”状态更清楚。

Pulsara 还可以进一步加强：

- client draft 只归 client；
- Enter 后必须先得到 Host authoritative acknowledgement；
- UI 只有在 acknowledgement 后显示 queued；
- queue item 有 stable identity；
- delivery safe point 可审计；
- detach/reconnect 后可恢复 authoritative queue；
- client 不用 message ID 大小关系推断 queue 状态。

### 5.3 Composer

Codex composer 支持：

- slash command；
- file/skill/plugin/app mentions；
- image attachment；
- local shell escape；
- history；
- queued item edit；
- Vim mode；
- external editor；
- large-paste placeholder；
- terminal paste burst detection。

大粘贴超过 1000 chars 后只在 UI 中显示 placeholder：

- [chat_composer.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/bottom_pane/chat_composer.rs:279)

该设计对 Pulsara 很有价值：

- transcript 不被粘贴内容撑爆；
- cursor movement 仍然稳定；
- submit 时仍保留完整内容；
- placeholder 删除能同时删除 backing payload；
- 可显示准确字符数。

### 5.4 Live status

Codex status widget 将：

- spinner；
- header；
- elapsed；
- interrupt hint；
- bounded details；
- optional inline context；

放在稳定区域：

- [status_indicator_widget.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/status_indicator_widget.rs:45)

details 默认最多三行，超限截断：

- [status_indicator_widget.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/status_indicator_widget.rs:202)

这比在 transcript 中反复插入“Still working…”更好，因为：

- 不污染历史；
- 不造成 transcript 抖动；
- 当前状态始终在固定位置；
- 完成后状态自然消失或折叠成 terminal summary。

### 5.5 Approval

Codex 将 approval 作为 bottom-pane overlay，能够显示：

- command；
- cwd；
- reason；
- network target；
- additional permissions；
- patch；
- one-time/session scope；
- cross-thread ownership。

见：

- [approval_overlay.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/bottom_pane/approval_overlay.rs:70)

它还强调 input routing：

- active view 先消费 Ctrl-C；
- composer history search 可以消费取消；
- 未处理的 Ctrl-C 才上升为 turn interrupt 或 quit。

见：

- [mod.rs](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/bottom_pane/mod.rs:1)

Pulsara 也需要按层路由 Esc/Ctrl-C，不能让所有按键直接调用 `stop_current_turn()`。

### 5.6 对 Pulsara 的结论

Codex 提供了最适合作为基础的结构：

- transcript 与 live cell 分离；
- bottom pane 保留 composer；
- typed modal；
- queue category；
- safe-point steer；
- stable status；
- key-routing hierarchy。

需要避免：

- 为每个 event type 创建专用 UI cell；
- UI 组件持有 runtime manager；
- 在 UI 中复制 HostSession state machine；
- 将 observer cancellation 传播给 run owner；
- 让 UI queue 取代 Host ingress authority。

## 6. 三者共同证明的 UX 原则

### 6.1 `TUI-UX-OWN-001` 输入owner与执行owner必须分离

一个长期 agent 运行时，用户仍然需要：

- 输入 follow-up；
- steer；
- stop；
- 回答 question；
- approve/deny；
- 查看状态；
- 切换详情；
- 复制历史。

因此 composer 不能与 `run_turn()` 生命周期绑定。

### 6.2 `TUI-UX-STATE-001` UI必须投影typed state，而不是消费stdout

stdout 不足以回答：

- 这是 active 还是 terminal？
- 该 tool call 是否已 durable commit？
- approval 属于哪个 run？
- background process 是否仍归 session 所有？
- queued message 是否已被 Host 接纳？
- model output 是否被 control disposition 接纳？

UI 应从 typed event/snapshot 构造 process-local projection。

### 6.3 `TUI-UX-DISC-001` 默认compact，必要时可无损展开

默认界面只回答：

- 现在在做什么；
- 做了多少；
- 是否仍有进展；
- 用户是否需要行动；
- 最终产生了什么。

完整 durable 事实通过：

- expand；
- transcript mode；
- Inspector；
- artifact viewer；

按需查看。

### 6.4 `TUI-UX-PROG-001` Progress必须有实际语义

无穷 spinner 只能说明 event loop 仍在刷新，不能说明任务有进展。

更可靠的 progress 来源包括：

- model stream received chars/tokens；
- current activity category；
- tool started/terminal；
- command elapsed/output lines；
- subagent completed/total；
- plan completed/total；
- MCP starting/ready/failed；
- compaction stage；
- queue depth；
- stop/drain stage。

### 6.5 `TUI-UX-INT-001` Human interaction是状态，不是普通消息

Approval、plan question、MCP input-required 不应伪装成 assistant text。

它们应：

- 临时占用 bottom pane；
- 保留 composer draft；
- 显示 owner run/session；
- 提供合法选项；
- 阻止不合法输入；
- terminal resolution 后回到原交互面。

### 6.6 `TUI-UX-MOTION-001` 动画不能替代状态

动画只用于让静态终端知道 UI 未冻结。

必须支持：

- reduced motion；
- animations disabled；
- 非 TTY；
- redirected stdout；
- snapshot tests。

## 7. Pulsara 目标产品形态

### 7.1 目标

V1 UI 应做到：

1. agent 运行时 composer 始终可用。
2. 用户清楚知道输入会 steer 当前 run 还是成为下一 run。
3. 用户能编辑或取消尚未 dispatch 的输入。
4. 用户能看到当前阶段与真实进度。
5. approval/question/MCP interaction 使用 typed UI。
6. 工具轨迹默认紧凑，完整事实仍可展开。
7. background process、subagent 和 MCP 状态持续可见。
8. detach/reconnect 后重建同形状 projection。
9. UI observer 失败不影响 durable runtime。
10. UI 不成为新的 runtime authority。

### 7.2 非目标

V1 不追求：

- IDE 级代码编辑器；
- mouse-first 操作；
- voice；
- 任意插件绘制任意区域；
- 用户 shell status hooks；
- dashboard 式全量 Inspector；
- GUI；
- 全部 tool type 定制组件；
- 在 UI 中复刻 EventLog。

### 7.3 `TUI-UX-ARCH-001` 推荐总体结构

```text
RuntimeSession / EventLog       HostSession / RunExecutionRegistry
            │                                │
            │ committed sequence             │ bounded operational frames
            └──────────────┬─────────────────┘
                           ▼
              TerminalPresentationFoundation
              ├── bootstrap snapshot as-of H
              ├── subscribe committed H+1
              ├── operational generation/cursor
              └── gap/backpressure disposition
                           │
                           ▼
                 TerminalClientGateway
              attachment / commands / receipts
                           │
                           ▼
              PulsaraTerminalClientProtocol v1
                           │
                           ▼
                  Bubble Tea v2 client
          ┌────────────────┼──────────────────┐
          ▼                ▼                  ▼
 PresentationHistoryState LiveActivityState InteractionRequestView
          │                │                  │
          └────────────────┼──────────────────┘
                           ▼
                Model / Update / View

RuntimePromptQueueService <── typed queue commands ── composer
Host interaction ports    <── sealed resolution ───── interaction view

Frozen Legacy REPL ── limited shared application services ── Runtime/Host
```

`TerminalPresentationFoundation`、`TerminalClientGateway`、`RuntimePromptQueueService`和Host interaction ports是不同能力：

- observation只读，不得拥有 mutation；
- gateway只拥有client attachment、controller lease、wire cursor和typed command dispatch，不解释`AgentEvent`；
- queue service只拥有尚未进入 run 的 user intent；
- interaction port只解析/提交 exact pending interaction的合法 resolution；
- Bubble Tea只消费projection并拥有display mechanics，不取得上述service的内部owner；
- Legacy REPL不是fallback或production-equivalent client，只能借用冻结的application-service子集。

### 7.4 `TUI-UX-AUTH-001` Authority边界

| 数据 | Authority | UI 是否可修改 |
|---|---|---|
| composer draft | Go client attachment | 是 |
| selection、scroll、wrap cache与展开偏好 | Go client attachment | 是 |
| paste backing payload | Go client attachment，直到冻结inline content或确认artifact receipt | 是 |
| MCP event-safe request view | durable request projection | 否 |
| MCP private URL/request secret | encrypted continuation store是storage-only durable authority；Python Host MCP interaction owner是唯一decrypt/hydration authority | 当前controller attachment只能借用ephemeral secret lease；已显示plaintext不可撤销 |
| sealed MCP response draft | Go controller attachment的ephemeral secret state | 只能经secret channel提交/释放，不进入普通snapshot或replay |
| accepted queue item | EventLog queue transition chain；queue service是mutation owner；row/account是CAS projection | 只能发typed CAS command |
| active run state | Run owner + durable facts | 否 |
| approval/question | Host pending owner | 只能 resolve |
| canonical transcript acceptance、suppression、tool pairing与terminal-document join | `TranscriptProjectionStateStore` + EventLog/projection facts | 否 |
| canonical leaf的display cell/tool subtype与registered durable-audit classification | Python presentation kernel；同一event可双purpose，但不得重判canonical acceptance/pairing | 否 |
| transcript leaf与durable audit cell在终端history的全局placement/order | `PresentationHistoryProjectionOwner` + exact root/checkpoint；只引用两类语义authority | 否 |
| tool progress | runtime operational stream | 否 |
| tool terminal result | durable event | 否 |
| semantic grouping identity | Python presentation kernel | 否 |
| group展开/collapse preference | Go client attachment | 是 |
| sidebar visible | client preference | 是 |
| status bar segment order | client preference | 是 |
| client attachment/controller lease | Python TerminalClientGateway | 只能通过typed attach/takeover/release command改变 |
| command outcome | Python application service + exact durable confirmation | 否 |

### 7.5 Projection bootstrap 与 reconnect

> Informative design history：本节保留此前review收紧的算法依据，`StoredEventBatchCommitReceipt`、confirmation evidence、tap和bootstrap的唯一normative定义已迁入Foundation规格的`TUI-FND-EVT/OBS-*`。

UI committed observation不得注册为`RuntimeEventPublisher` subscriber。Publisher会逐个await subscriber；UI进入该链会把renderer backpressure重新变成durable publication failure。

RuntimeSession新增process-local、非等待式`UiCommittedEventTap`。唯一安装点位于canonical commit FULL且committed reducers已经fold之后：

```text
critical writer
  -> EventLog assigns canonical sequence
  -> EventLog constructs RawStoredEventEnvelope + EncoderBuiltStoredEventPair exactly once
  -> persistence + transcript-prefix accounting consume that envelope
  -> commit result returns complete StoredEventBatchCommitReceipt
  -> RuntimeSession classifies business/accounting projections
  -> TranscriptProjectionStateStore.apply_live_committed(receipt) returns live fold result
  -> other committed reducers fold owned stored events
  -> build CommittedPresentationTapEntry(receipt, fold result)
  -> UiCommittedEventTap.offer_nowait(entry)
  -> existing RuntimeEventPublisher enqueue/delivery
```

`AgentEvent`不是递归不可变类型，不能进入UI tap/ring。EventLog write result必须扩展为双carrier：

```text
StoredEventBatchCommitReceipt            # ports.stored_event process-local receipt
  owned_stored_events: tuple[AgentEvent, ...]                 # 完整physical batch
  raw_stored_envelopes: tuple[RawStoredEventEnvelope, ...]    # 与上一字段1:1
  ordered_join_fingerprint

CommittedPresentationTapEntry
  source_first_sequence / source_last_sequence
  raw_stored_envelopes
  stored_batch_ordered_join_fingerprint
  canonical_fold_result
  tap_entry_fingerprint

JoinedRawStoredEventRangeProof
  from_sequence_exclusive / through_sequence
  owned_stored_events / raw_stored_envelopes
  historical decoder ID/version/fingerprint
  ordered_range_envelope_accumulator
  range_proof_fingerprint

EventWriteResult
  committed_events: tuple[AgentEvent, ...]                    # caller-facing business subset
  accounting_events: tuple[AgentEvent, ...]                   # materialization bookkeeping subset
  stored_batch_receipt: StoredEventBatchCommitReceipt         # 完整physical authority
  business_accounting_classification_contract_fingerprint
  business_accounting_partition_fingerprint
  ... existing commit/reducer/publication fields
```

所有single/batch/conditional append生产API都必须从EventLog返回`StoredEventBatchCommitReceipt`，不能只返回`AgentEvent`后让上层补建raw envelope。RuntimeSession原样安装该receipt，不复制或重建。

构造storage receipt时必须验证`owned_stored_events`与`raw_stored_envelopes`的数量、ordered event ID、runtime session ID、canonical sequence、event type、schema version/fingerprint和domain contract fingerprint逐项一致，并重算ordered join fingerprint。

Normal write与exact candidate FULL confirmation使用不同的pair proof，但收敛为同一receipt shape：normal encoder在首次canonical encode时生成sealed `EncoderBuiltStoredEventPair`，receipt只重验scalar/fingerprint，historical decoder调用次数必须为零；exact confirmation从canonical row hydrateraw envelope，再由historical decoder生成`DecoderHydratedStoredEventPair`，并用prepared candidate证明batch order。两种proof都是module-private process-local carrier，不进入receipt或wire。

`agent_events`不持久化batch ID/ordinal/size。因此generic reopen、doctor、repair与bounded catch-up只能把canonical raw rows hydrate成`JoinedRawStoredEventRangeProof`；SQL page或单行不能称为physical receipt。Range proof只证明同一session内的连续sequence与owned/raw exact join，不进入tap，也不用于candidate confirmation。

构造`EventWriteResult`时再验证：

- `committed_events`与`accounting_events`是`owned_stored_events`的disjoint projection；
- 两个projection各自保持physical batch中的相对顺序；
- 两者event ID并集精确等于receipt完整event ID集合，不多、不少、不重复；
- classification contract fingerprint与当前materialization vocabulary一致；
- partition fingerprint从完整physical ordered event IDs及每项`business | accounting`label中央重算，caller不得自报。

Runtime committed reducer registration拆成`apply_live_committed(receipt)`与`fold_restored_range(range_proof)`。Transcript reducer让两者调用同一个grouping-independent pure fold core：live result绑定真实batch fingerprint并可形成tap entry；restore result只绑定range proof。相同ordered events无论如何分页，最终canonical state/leaf set/accumulator必须相同。UI tap只接收将完整live raw tuple与live fold result exact join后的`CommittedPresentationTapEntry`，所以accounting sequence不会形成gap，raw evidence和canonical fold也不会因ring eviction或bootstrap分离。Presentation policy可将accounting event的audit purpose设为noop，但不得用raw envelopes重新判断transcript acceptance、suppression或tool pairing。

UNKNOWN/idempotent confirmation不能直接返回batch receipt。它先返回逐candidate evidence：

```text
StoredEventCandidateMatch
  candidate_index: int
  candidate_event_id: str
  candidate_payload_fingerprint: Fingerprint
  owned_stored_event: AgentEvent
  raw_stored_envelope: RawStoredEventEnvelope
  join_fingerprint: Fingerprint

EventBatchConfirmationEvidence
  exact_ordered_candidate_batch_fingerprint: Fingerprint
  matched_candidates: tuple[StoredEventCandidateMatch, ...]
  missing_event_ids: tuple[str, ...]
  actual_last_sequence: int
  evidence_fingerprint: Fingerprint

EventBatchConfirmationDisposition =
    FULL
  | NONE
  | PARTIAL
  | CONFLICT
  | UNAVAILABLE

ConfirmedFullStoredBatch
  receipt: StoredEventBatchCommitReceipt
  confirmation_evidence_fingerprint: Fingerprint
  classifier_contract_fingerprint: Fingerprint
```

`StoredEventCandidateMatch`必须由数据库raw row与exact prepared candidate共同构造；candidate index唯一、严格递增，event ID、runtime session、schema binding及把stored sequence归一化为`None`后的candidate payload fingerprint必须逐项exact join。Evidence允许只覆盖部分candidate，也允许matched rows并不连续；因此它不是physical batch receipt，绝不能进入tap。

中央classifier冻结以下唯一矩阵：

- 所有candidate均matched，ordered identity/payload正确，canonical sequence严格连续且顺序与prepared batch一致：`FULL`，由这些match构造唯一`ConfirmedFullStoredBatch`；
- 所有candidate均missing且没有同ID payload conflict：`NONE`；
- exact matched subset非空且missing subset非空：`PARTIAL`；
- 任一同ID不同payload、duplicate candidate index、runtime/schema mismatch，或全部matched但sequence不连续/顺序错误：`CONFLICT`；
- physical read未完成：`UNAVAILABLE`，不伪造evidence或domain winner。

只有`FULL`的`ConfirmedFullStoredBatch.receipt`与normal commit receipt同形。`PARTIAL | CONFLICT`必须进入ledger reconciliation，`NONE`才允许重试same stable candidate；任何非FULL evidence都不得送入UI tap。数据库exact-candidate confirmation、restart中的stable-candidate repair与idempotent append winner必须直接hydrate stored raw rows和owned decoded events，不得从decoded `AgentEvent`重新构造raw envelope；没有prepared candidate batch identity的generic restart只能走range proof。

`RawStoredEventEnvelope`是本次stored row的唯一canonical payload carrier，而不是UI专用副本：

- EventLog在分配sequence后只执行一次schema resolve、canonical encode、payload/envelope fingerprint validation；
- PostgreSQL insert参数、ledger/transcript prefix accounting、normal commit receipt复用encoder-built pair与stored raw envelope；normal receipt construction禁止再次decode；
- exact candidate confirmation从stored row hydrateraw envelope并经historical decoder构造decoder-hydrated pair，最终FULL receipt与normal receipt同形；generic restore用相同pair组成range proof而不是receipt；
- 禁止RuntimeSession在commit后调用旧`RawStoredEventEnvelope.from_stored_event(...)`或任何等价factory重新序列化、JSON parse或验证；
- tap只移动frozen dataclass reference/tuple，不decode canonical bytes；
- UI observation service在critical writer之外、受page/bytes/deadline约束地按historical schema binding decode成owned projection input；
- `RawStoredEventEnvelope`的最终类型owner下沉为`primitives.stored_event`；它只含frozen scalar/bytes字段和纯validation，不import `AgentEvent`、schema registry或EventLog；
- `StoredEventBatchCommitReceipt`与`JoinedRawStoredEventRangeProof`的最终owner是`ports.stored_event`；它们可以组合owned `AgentEvent`与primitives raw envelope，但不得import concrete EventLog adapter；
- `event_log.serialization`拥有唯一`build_raw_stored_event_envelope(...)`factory；PostgreSQL row hydration也走event_log-owned factory；
- 所有production、test与tooling调用方在同一hard-cut阶段直接改为`from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope`；`event_log.protocol`删除该symbol，不提供re-export、alias、`__getattr__`或兼容shim；
- `ports.event_write.EventWriteResult`从`ports.stored_event`导入receipt；UI projection只import primitives raw owner；`event_log.protocol`拥有candidate match/evidence，通过qualified module dependency引用`primitives.stored_event.RawStoredEventEnvelope`，只有其FULL classifier output引用ports receipt。`event_log.protocol`不得把raw envelope绑定为自身public symbol或re-export旧路径。由此避免`ports -> event_log.protocol -> ports`循环；禁止复制第二份raw envelope/receipt结构或保留shadow class identity。

`offer_nowait()`规则：

- 在RuntimeSession write coordinator的no-await临界区内按sequence调用；
- 只能执行有界内存操作，禁止await、SQL、decode、serialize、schema resolve和renderer callback；
- tap failure只记录bounded diagnostic并要求UI从ledger重建，不改变event commit/publication outcome；
- 每个runtime session拥有按event count和canonical bytes双重有界的ring；
- ring记录`first_sequence/last_sequence/ring_generation`；
- 每个subscriber queue有独立容量，满时立即detach；
- ring eviction或subscriber overflow不得阻塞publisher、run、Host close或RuntimeSession close。

`CommittedPresentationTapEntry`进入tap时执行原子entry-ingestion矩阵：

- `source_first_sequence == subscriber.next_sequence`：append完整entry；
- 整个sequence范围及`tap_entry_fingerprint`已存在：duplicate no-op；
- 任意partial overlap，即使重叠envelope完全相同：不得拆分raw/fold复合项，立即detach并转bounded ledger catch-up/rebootstrap；
- sequence gap、重叠位置出现不同envelope fingerprint、same batch identity对应不同fold result、receipt内部不连续或runtime session不匹配：立即detach该subscriber generation并转bounded ledger catch-up/rebootstrap。

Tap不得用event ID相同代替sequence/envelope exact join，也不得跳过accounting envelope来“修复”gap。

#### 7.5.1 Durable bootstrap 算法

每次 attach/reconnect 使用以下线性化流程：

```text
1. 从checkpoint/root恢复DurableUiBootstrap exactly through H
2. acquire tap lock，安装CATCHING_UP subscriber并冻结tap head R
3. 后续commit只进入whole-entry catch-up buffer
4. 选择第一个source_first_sequence > H的完整live entry E
5. 对(H, E.first-1]分页读取canonical rows并构造JoinedRawStoredEventRangeProof
6. transcript与durable-audit分别消费同一range；任何跨H的live entry都不拆分、不直接应用
7. state through == E.first-1后才按完整CommittedPresentationTapEntry切回live fold
8. 若没有完整E，则range-fold through R，等待未来从R+1开始的entry
9. acquire tap lock，drain whole buffered entries并验证连续后切换LIVE
10. 返回DurableUiBootstrap + committed subscription handle
```

若ring未覆盖`(H, R]`：

- 允许在absolute bootstrap deadline内执行有界EventLog page catch-up；
- catch-up受page event/bytes和总resident bytes约束；
- 若delta超过总预算，不继续扫描任意长历史，而是构造更新的snapshot `H2`并重新开始；
- EventLog page边界不参与canonical semantic identity，不得构造fake batch receipt；
- 已被range fold覆盖的ring/buffer entry必须exact验证后丢弃，不得再次apply；
- catch-up buffer溢出时detach本次attempt并重新bootstrap；
- 任一返回的live handle都必须已经证明从snapshot high-water到current tap head连续。

`DurableUiBootstrap`最小identity包含：

- runtime session ID；
- source ledger ID/schema registry fingerprint；
- authoritative `through_sequence=H`；
- unified presentation-history root/checkpoint、bounded resident tail与latest-root cursor pair；
- pending interaction exact view；
- prompt queue domain checkpoint/head receipt、bounded delta disposition及Foundation-validated active client projection；
- terminal/reconciliation state；
- durable bootstrap fingerprint。

Committed event以 canonical sequence去重。以下任一情况返回 typed `projection_rebuild_required`：

- sequence gap；
- historical schema无法decode；
- cursor早于可读取retention horizon；
- ledger进入reconciliation/untrusted；
- tap/ledger无法在deadline内证明连续prefix。

UI 不得猜测缺失状态。重建期间保留已经确认的 transcript，live activity显示 `Reconnecting…` 或 `State unavailable`。

#### 7.5.2 Operational inventory 独立冻结

Durable snapshot与live operational owners不能宣称共享一个原子bootstrap或fingerprint。EventLog和多个process-local owner没有共同transaction boundary。

Observation service在durable committed handle安装后，另行读取`OperationalUiInventorySnapshot`：

- process/service generation；
- inventory cursor；
- resident owner identities；
- 每个owner的latest bounded frame；
- captured-at monotonic time；
- operational snapshot fingerprint。

controller分别安装durable与operational snapshot。Operational frame只有在其source durable identity已经fold、owner generation仍匹配时才可显示；否则buffer、丢弃或标记stale。

Operational history不承诺durable replay。reconnect只提供“当前仍resident的live owners”快照；已经terminal的事实从EventLog重建，既不resident又无durable terminal authority的项目显示unknown/recovery-required。

#### 7.5.3 Unified presentation history root

Canonical transcript与durable audit保持各自语义owner，但full-screen history只有一个全局placement owner。Normative DTO与算法只存在Foundation `TUI-FND-PROJ-004/005`；本文只冻结产品级结论：

- `PresentationHistoryProjectionRootFact`同时绑定canonical transcript reducer、event-domain registry、presentation policy registry与audit extractor registry contract fingerprints；
- root entry只引用canonical transcript leaf或durable audit cell，root只决定placement/order，不重判语义；
- canonical transcript reducer冻结不可重排的stable placement spine；append只在尾部签发新coordinate，single/interval replacement继承原位置，retirement保留不可渲染anchor tombstone，replacement sequence永不成为canonical排序键；
- durable audit只通过typed `before_leaf | after_leaf | ledger_sequence` anchor合并；ledger-sequence audit必须先解析成proved transcript gap，不能交换两个canonical anchors；
- root复用现有transcript projection的bounded persistent tree：immutable leaf/internal nodes、stable placement-key ranges、subtree counts、content-addressed root与path-copy update；连续history ordinal不进入entry/tree/cursor identity，display rank只在读取时派生；禁止随session线性增长的flat page manifest；
- `PresentationHistoryMaterializationPolicyFact`完整冻结node bytes、leaf/internal fanout、tree height、tail soft/hard bounds、ordinary growth quote、session-rotation threshold、terminalization maintenance reserve、root generations/TTL与page-read limits；
- live committed projection可以通过process-local bounded active head tail低延迟显示，但tail不是第二个root、不可page，cursor仍只绑定confirmed immutable root；
- active head按每个EventLog sequence保存一个bounded fold segment；noop sequence也拥有空mutation segment，aggregate hash不是可切分authority。Checkpoint candidate只在segment边界冻结exact prefix；typed FULL通过durable source-prefix transition proof消费installed root覆盖的segment prefix，并保留checkpoint I/O期间新增的append/noop segment suffix，resulting active head允许non-empty tail。Post-cut rewrite/retirement废弃candidate或typed rebuild，不能当append suffix；root-advanced frame原子交付new latest cursor pair、consumed segment prefix、retained segment suffix与完整resident transition；old-root cursor可作为retained pinned browser cursor继续读取旧snapshot，但不能承担follow-tail；
- resident transition是完整`unchanged | bounded ordered changes | rebase required` union：分别携带equal-vector proof、ordered upsert/remove count+bytes+accumulator，或exact target root/head与bounded token；Protocol/Go不得只传标签；
- ordinary capacity只按`confirmed + current tail + active reservation remaining + requested quote`计算；terminalization maintenance reserve只隔离soft threshold与hard maximum。Tree达到soft rotation threshold后拒绝对应prompt/run/queue growth并提供“新建会话”，已准入run使用隔离reserve收口；unexpected hard exhaustion typed fail closed，不静默截断/evict。V1不实现history epoch/super-root；
- viewport/page/cursor只有一套，server与Go都不分别分页或merge transcript/audit feed；
- operational activity不进入root、checkpoint或page，只使用独立generation/cursor。

RunStart/RunEnd lifecycle不创建新的wire cell branch；它们统一为`AuditCell(audit_kind="run_lifecycle")`。Foundation与Protocol物理分离`DurableHistoryCell`与`OperationalActivityCell`，旧`TerminalSemanticCell`与`RunLifecycleCell`均不保留。

### 7.6 Durable 与 operational feed 分层

Observation feed包含两种carrier：

```text
CommittedPresentationTapEntry
  complete raw stored envelope tuple
  complete stored-batch fingerprint
  exact-joined canonical transcript fold result
  atomic first/last sequence range

OperationalUiFrame
  runtime_session_id
  owner identity
  operational generation
  monotonic cursor
  frame kind
  bounded typed payload
```

规则：

- committed tap entry不可修改、拆分或只丢raw/fold其中一半；overflow时detach并要求ledger catch-up；
- UI reducer永远不接收与publisher共享的live `AgentEvent`对象；decode后得到的owned projection input也不得回写raw envelope；
- operational frame允许按 owner/kind coalesce，只保留最新进度；
- terminal结果不得只存在于 operational frame；
- slow renderer不得成为 `RuntimeEventPublisher` 的 awaited subscriber；
- observer detach只取消 observation borrow，不取消 run、terminal process、subagent或MCP operation；
- renderer crash后，service释放observer queue；Host close不等待UI drain。

## 8. UI 状态模型

### 8.1 Session-level state

UI 至少区分：

| 状态 | 主界面行为 |
|---|---|
| `opening` | 立即显示 shell；可编辑client-local draft，但Host ready前不得显示authoritative queued |
| `idle` | composer 可提交新 run |
| `preparing` | 显示 boundary/preflight 阶段；composer 可排 follow-up |
| `running` | 显示 live activity；composer 可 steer/follow-up |
| `waiting_approval` | bottom pane 显示 approval；只允许排follow-up，不允许steer |
| `waiting_interaction` | bottom pane 显示 plan/MCP question；只允许排follow-up，不允许steer |
| `stopping` | 停止 admission；显示 drain progress |
| `suspended` | 显示 resume/cancel action |
| `reconciliation_required` | 禁止普通运行，仅允许 inspect/close |
| `closing` | 显示 bounded close/drain progress |
| `closed` | composer disabled，保留 transcript |

这些 UI 状态应由现有 authoritative facts/snapshot 组合派生，不新增平行 runtime enum 后再试图双向同步。

### 8.2 Turn-level state

```text
queued
  -> preparing
  -> running
  -> waiting_user
  -> running
  -> stopping
  -> finished | failed | aborted | reconciliation_required
```

UI 可以有更细的 display phase，例如：

- resolving target；
- preflight compaction；
- compiling context；
- waiting provider；
- streaming response；
- executing tool；
- waiting subagent；
- finalizing。

但 display phase 只是 projection，不得影响 runtime admission。

## 9. Layout 设计

### 9.1 `TUI-UX-LAYOUT-001` 基础四区

推荐单列基础布局：

```text
┌─────────────────────────────────────────────────────────┐
│ Transcript                                               │
│                                                         │
│ • Read 8 files · searched 3 patterns                    │
│ • Running pytest… (18s · 426 lines)                     │
│                                                         │
│ Assistant final/streaming text                           │
├─────────────────────────────────────────────────────────┤
│ Pending steer / follow-up preview                        │
├─────────────────────────────────────────────────────────┤
│ Composer or typed interaction view                       │
├─────────────────────────────────────────────────────────┤
│ pro · trusted_host · 31% ctx · MCP 2/3 · 1 agent · 18s │
└─────────────────────────────────────────────────────────┘
```

### 9.2 Transcript 区

Transcript 只保存 terminal 或已稳定的 visual cell。

运行中的高频变化留在 live activity/status region，完成后再折叠成 terminal summary。

这样避免：

- 每个 delta 使历史整体重排；
- spinner 行进入复制文本；
- progress heartbeat 污染 durable transcript；
- scroll position持续跳动。

Full-screen模式从S1开始就必须拥有有界viewport，而不是把完整transcript常驻内存。Foundation规格冻结DTO：

```text
PresentationHistoryViewportSnapshot
  runtime_session_id
  projection_revision
  active_head_identity                # confirmed root identity + bounded uncheckpointed tail
  ordered_resident_ranked_entries     # stable placement-key order + basis-local display rank
  latest_root_cursor_pair             # 原子before/after pair，只绑定current confirmed root
  resident_cell_count
  resident_bytes
  oldest_history_entry_id / placement_key
  newest_history_entry_id / placement_key

GoPresentationHistoryViewportState
  follow_tail
  unseen_terminal_cell_count
  selected_cell_ids
  expanded_cell_ids
  page_hydration_state
```

前一个branch是Python Foundation提供的renderer-neutral bounded view；后一个branch完全属于Go attachment，不能回写或进入server snapshot。

`PresentationHistoryPageCursorFact`只绑定unified root/generation、anchor placement key与entry ID，不保存feed kind、direction或display rank。`PresentationHistoryPagePort`只提供`read_page(cursor, direction, limits, absolute_deadline)`；direction由本次request唯一拥有。调用同时受最大entry数、canonical bytes、rendered bytes、tree-node reads和absolute deadline约束。它按placement-key range从confirmed persistent tree执行`O(tree height + page size)`有界读取，并通过subtree counts派生root-local display rank；当前仍resident的operational activity由独立feed维护，两者不得伪装成同一原子snapshot。

Go同时维护一个latest-root cursor pair与有界的old-root pinned page state。`PresentationHistoryRootAdvancedFrame`必须原子携带new active head、new latest cursor pair、old/new root relation、consumed segment prefix、retained concurrent segment suffix与resident transition，并推进projection revision；noop-only suffix虽然没有history mutation，仍必须携带positive segment count与advanced source lineage。丢帧走GAP/snapshot rebuild。旧root的empty-after-page只表示该旧snapshot结束，不能遮蔽已经checkpoint到new root的tail。

Foundation page disposition与Terminal protocol wire branch必须一一对应：`PAGE | CURSOR_STALE | REBASE_REQUIRED | RECONCILIATION_REQUIRED`。只有empty PAGE且对应`has_more=false`才表示history end；stale/rebase必须返回latest checkpoint/projection generation、root与authority high-water hints，并由Go重试proved replacement cursor或执行bounded rebase，绝不能显示成“没有更多历史”。Reconciliation branch只返回被证明可信的hint。

Viewport规则：

- bootstrap只hydrate有界recent tail和可继续分页的cursor，不从RunStart扫描到ledger head；
- resident window同时按cell count和rendered bytes设硬上限，远离viewport的历史page可以evict；
- active cell、action-required cell和当前selection不得被无提示evict；
- 用户向上滚动后立即令`follow_tail=false`，新terminal cell只增加unseen count，不移动viewport；
- `End`/显式follow命令重新定位durable tail并清零已经纳入viewport的unseen count；
- live activity更新不得改变用户正在阅读的历史anchor；
- selection/copy以stable cell ID和当前projection revision为界，页面evict时保留最小selection identity而非整页文本；
- 完整 transcript export是显式、分页、可取消的export/Inspector operation，不走renderer snapshot；
- S6可以增强semantic grouping，但不得到S6才补scroll、cursor、resident bound或history hydration。

Alternate-screen退出必须在`finally`中恢复终端模式。正常退出只向主屏打印有界`ReplExitSummary`：最后一个已确认final result摘要、runtime session ID/resume hint，以及pending/reconciliation警告；不得把完整alternate-screen transcript回灌到主屏。异常、SIGINT、startup失败和renderer crash使用同一恢复路径。

### 9.3 Bottom pane

Bottom pane 持有：

- composer；
- slash/file popup；
- approval；
- plan question；
- plan exit；
- MCP input-required；
- generic fallback；
- fatal/reconciliation notice。

切换 view 时 composer draft 不销毁。

### 9.4 Responsive sidebar

宽度建议：

| 终端宽度 | 布局 |
|---:|---|
| `<80` | compact 单列，进一步缩短 status |
| `80–119` | 标准单列 |
| `≥120` | 可选 36–42 列 sidebar |

Sidebar 内容优先级：

1. 当前 plan/todo；
2. subagent；
3. background terminal process；
4. MCP；
5. context/rollout；
6. workspace/git；
7. diagnostics。

Sidebar 不得成为批准、停止或恢复的唯一入口。

## 10. Composer 设计

### 10.1 `TUI-UX-COMP-001` V1必需能力

- 多行输入；
- history；
- `Ctrl-R` search；
- external editor；
- large-paste placeholder；
- slash autocomplete；
- file/skill mention；
- queue preview；
- restore last queued item；
- clear draft；
- local draft stash；
- image/file attachment若当前模型支持；
- model/mode capability validation。

Composer owner始终独立于 Host/run owner：

- opening、reconnecting或interaction overlay期间，draft仍可本地编辑；
- draft尚未提交时不得进入session snapshot或durable transcript；
- Enter 后进入 `submitting`，只有durable queue FULL receipt后才能显示 `Queued`；
- submit失败不清空draft；
- observer detach不清空draft；
- session close成功后，draft只能显式stash到client-local storage，不能自动转移到另一个runtime session。

### 10.2 Large paste

超过阈值的 paste：

- composer 显示 `[Pasted Content · 12,431 chars]`；
- backing payload 保存在 draft owner；
- cursor 将 placeholder 视为单一 logical element；
- 删除 placeholder 同时删除 backing payload；
- submit 时展开；
- queue persistence 不得只保存 placeholder。

阈值不必沿用Codex的1000 chars，应基于S0 Bubble Tea paste测试、protocol frame上限和artifact preparation成本共同确定。

#### 10.2.1 Queue content preparation

> Informative design history：artifact preparation/hold的唯一normative定义在Foundation规格`TUI-FND-QUEUE-003/004`。本节只说明UX为什么不能引用未确认artifact。

submit边界只能接受已经冻结的closed union：

```text
PreparedPromptQueueContent =
    InlineQueueContent
  | ConfirmedArtifactQueueContent

InlineQueueContent
  canonical_text
  utf8_bytes
  byte_count
  media_type
  codec
  content_semantic_reference
  content_semantic_fingerprint
  content_attribution_fingerprint
  content_fact_fingerprint

ConfirmedArtifactQueueContent
  preparation_id
  preparation_fingerprint
  preparation_hold_revision
  stable_content_addressed_artifact_id
  artifact_identity_fingerprint
  canonical_payload_sha256
  canonical_byte_count
  media_type
  codec
  artifact_semantic_reference
  confirmed_write_receipt_identity
  confirmed_write_receipt_fingerprint
  content_semantic_fingerprint
  content_attribution_fingerprint
  content_fact_fingerprint
```

Identity分层不得把storage occurrence混入queue semantic identity：

```text
content_semantic_fingerprint = H(
  canonical payload hash/bytes,
  normalized media type,
  codec,
  semantic reference,
)

content_attribution_fingerprint = H(
  preparation/hold identity and revision,
  artifact identity,
  confirmed write receipt,
  storage attribution,
)

content_fact_fingerprint = H(
  content_semantic_fingerprint,
  content_attribution_fingerprint,
)
```

Inline branch使用registered canonical UTF-8 text semantic reference；artifact branch使用`artifact_semantic_reference`。Preparation、hold、artifact ID/location、write receipt、storage generation与时间戳不得进入semantic fingerprint。相同内容重新确认或storage relocation保持相同queue content semantic identity；queue item ID只依赖semantic fingerprint，physical occurrence只由attribution/fact fingerprint区分。

Artifact确认与queue acceptance之间由durable storage-only hold承接：

```text
PromptQueueArtifactPreparationHoldFact
  schema_version
  preparation_id
  runtime_session_id
  owner_client_submission_identity
  artifact_id
  artifact_identity_fingerprint
  content_fingerprint
  state: PREPARED | CONSUMED | RELEASED
  consuming_queue_item_id: str | None
  hold_revision
  created_at_utc
  expires_at_utc
  confirmed_write_receipt_identity
  confirmed_write_receipt_fingerprint
  preparation_fingerprint
  hold_row_fingerprint
```

`preparation_id`由runtime session、client submission identity、artifact identity和content fingerprint稳定派生。same ID + same complete fact是exact reuse；same ID + different fact是conflict。caller不得自报state、revision或fingerprint，均由central factory/repository重算。

Hold fact使用registered `FrozenStorageFactBase`，不进入EventLog payload；queue event只保存preparation ID/fingerprint和artifact identity reference。Secret deny规则不适用，但EventLog/type gate仍禁止把完整storage row误当event-safe authority。

该hold只是artifact retention/preparation authority，不是queue intent的semantic authority。queue是否accepted、reserved或consumed仍只由EventLog queue transition chain证明；hold的`CONSUMED`必须exact join matching accepted event和queue-content reference。

Hold状态机完整冻结为：

```text
PREPARED
  -> CONSUMED       # queue acceptance FULL
  -> RELEASED       # pre-accept abandonment/expiry

CONSUMED
  -> RELEASED       # terminal queue-item retention retirement

RELEASED
  -> physical row deletion by GC
```

Artifact-backed content使用以下preparation协议：

1. composer submission owner冻结canonical bytes、stable artifact ID和content fingerprint；
2. 在queue acceptance之前，由有absolute deadline且可drain的physical owner幂等写入/确认artifact；
3. 同一artifact repository transaction原子创建或exact-confirm`PREPARED` hold；PostgreSQL使用同一connection/transaction，in-memory实现使用同一lock；
4. 返回的`ConfirmedArtifactQueueContent`必须同时携带artifact write receipt与PREPARED hold identity/revision/fingerprint；
5. 只有未过期、owner/submission匹配且状态为`PREPARED`的carrier可以进入acceptance candidate；
6. acceptance transaction通过RuntimeSession transaction companion对hold执行`SELECT ... FOR UPDATE`/exact CAS，验证artifact identity和confirmed receipt；
7. 同一transaction写queue event/account/head和queue-content reference，并把hold原子改为`CONSUMED`、绑定exact queue item；
8. physical NONE保持hold为`PREPARED`并复用同一acceptance candidate；physical UNKNOWN必须把queue event、row/account、queue-content reference与hold state一起exact-confirm；
9. 用户在acceptance candidate形成前显式放弃时可CAS为`RELEASED`；waiter cancellation本身只detach，不得擅自release in-flight hold；
10. queue FULL绝不能引用尚未确认、hold缺失/过期、仍在写入或无法hydrate的artifact；
11. reconnect/bootstrap只读取bounded content summary、queue reference与hold disposition，不自动hydrate完整large paste；dispatch按item预算和deadline hydrate。

Acceptance admission还必须证明`hold.expires_at_utc`覆盖本次absolute write deadline和confirmation tail。Hold不得在stable acceptance candidate形成后原地续期，因为revision/fingerprint变化会让同一event candidate漂移。若physical NONE后hold已经过期，旧candidate终止为typed `queue_content_preparation_expired`；用户重新submit时使用新的client submission/preparation identity。physical UNKNOWN即使越过expiry也必须先在同一lock order下确认是否已经`CONSUMED`，expiry sweeper不能抢先删除证明载体。

物理retention规则：

- preparation hold与queue-content reference只对`artifacts.id`建立`ON DELETE RESTRICT`外键；当前artifact schema没有复合identity key，不为此扩张artifact表；
- artifact digest、media type、size与semantic metadata fingerprint必须在`SELECT ... FOR UPDATE`锁定exact artifact row后逐项验证，不能由外键或caller自报替代；
- `PREPARED` hold在process crash、client detach和acceptance UNKNOWN期间继续阻止artifact删除；
- `CONSUMED`后queue item/reference成为主要retention owner，hold只保留exact join直到queue retention结束；
- expiry sweeper锁定hold后先确认不存在matching committed queue reference，才能`PREPARED -> RELEASED`；
- GC准入必须同时证明不存在active `PREPARED` hold且不存在queue-content reference；
- production artifact delete只能经typed retention guard在同一transaction/lock内完成上述证明；普通caller不得直接取得`delete_if_identity()` capability；
- 删除artifact前，maintenance transaction先删除已`RELEASED`且满足retention的hold row，再执行identity-guarded artifact delete；
- content-addressed artifact在hold释放后可能成为orphan，这是允许的maintenance状态，但不能在active hold窗口被删除。

Queue item到达吸收态且retention deadline到期后，session-owned retirement owner执行单一RuntimeSession transaction：

```text
validate exact absorbing queue head + retention cutoff
lock queue item/account, queue-content reference, CONSUMED hold, artifact row
revalidate artifact identity and matching accepted/consumed event references
append PromptQueueContentRetiredEvent
retire/delete exact queue-content reference
CAS hold CONSUMED -> RELEASED
advance queue account/checkpoint tail and materialization charge
COMMIT
```

`PromptQueueContentRetiredEvent`只保存queue item ID、preparation/hold identity、artifact identity fingerprint、retention policy fingerprint和retirement reason；不复制large content。它是queue transition accumulator的一部分，因此同样受checkpoint watermark和companion charge约束。

任一步失败整批rollback。physical UNKNOWN使用event、row/account、reference和hold的完整matrix exact-confirm；不得只看到reference消失就推断retirement FULL。只有该transaction FULL后，后续GC才可删除`RELEASED` hold row及无其他引用的artifact。

Artifact preparation的waiter cancellation只detach；physical owner必须在Host/UI close释放artifact/DB dependency之前真实退出或使close返回typed blocked。UI可以显示`Preparing paste...`，但只能在queue acceptance FULL后显示`Queued`。

### 10.3 Slash command registry

每个 command definition 统一声明：

- stable command ID；
- display title；
- description；
- aliases；
- keybindings；
- argument shape；
- availability predicate；
- whether allowed while running；
- whether client-only；
- whether it creates Host command；
- whether it can be queued；
- help category。

同一 registry 驱动：

- command palette；
- slash autocomplete；
- `:help`；
- key hints；
- availability；
- tests。

### 10.4 Key routing

Esc/Ctrl-C 的处理顺序：

1. autocomplete/popup；
2. current interaction view；
3. history search；
4. selection；
5. active run stop；
6. idle draft clear；
7. process quit。

任何一层消费后不得继续冒泡。

Quit 与 stop 必须是不同 command。

## 11. Server-authoritative prompt queue

> Informative product rationale：queue transition、companion charge、checkpoint、repair和safe-point的唯一implementation authority在Foundation规格`TUI-FND-QUEUE-*`；本文只拥有用户可见行为与产品取舍。

### 11.1 为什么不能只放在 UI

Pulsara 支持：

- durable session；
- detach/resume；
- future desktop/web client；
- HostSession safe point；
- pending approval/interaction；
- run recovery。

因此 Enter 之后的 user intent 不能只留在 terminal 进程数组中。

本文冻结：V1 queue acceptance立即durable。Host process-local接受后异步落盘、随后再向UI显示queued的方案不允许进入production，因为它无法满足client crash/reconnect和多client exact ordering。

queue authority属于runtime session，而非某一个可detach的HostSession。HostSession只在dispatch时借用queue item并进入现有ingress/boundary owner。

EventLog中的typed queue transition chain是唯一semantic/audit authority。`prompt_queue`表不是第二真源，而是支持多client查询和CAS的durable head projection；`prompt_queue_account`保存runtime-session级ordering/head。每个row至少保存：

- exact runtime session、queue item ID与accepted ordinal；
- current domain state与row revision；
- exact head transition event ID、canonical sequence和candidate payload fingerprint；
- requested/resolved delivery mode；
- 可空reservation identity、generation和ordered reservation-set fingerprint；
- `PreparedPromptQueueContent` reference及fingerprints；
- cancellation/rejection/reconciliation disposition；
- schema/registry binding。

account row至少保存reducer/registry contract fingerprints、`next_accepted_ordinal`、queue chain head event ID/sequence/fingerprint、account revision、checkpoint generation、checkpoint through sequence、queue-transition count/accumulator、bounded-tail start/count/accumulator和bounded capacity counters。Queue head event identity只在canonical empty genesis中允许全组为null。Client projection的empty/committed head由`checkpoint transition count + bounded tail count`唯一决定；首条transition到首checkpoint前必须以`generation-0 checkpoint + non-empty tail receipt`表示committed head，不能按checkpoint generation选branch。所有domain transition必须在单一RuntimeSession transaction中同时：

1. append exact typed queue transition event；
2. transaction companion对item row/account row执行expected-revision CAS；
3. 用canonical stored event ID、sequence和normalized candidate fingerprint安装resulting head；
4. 在commit返回前重读并验证event chain head与row/account projection一致。

所有queue transaction companion必须携带central factory生成的physical charge：

```text
PromptQueueCompanionKind =
    ACCEPT
  | RESERVE
  | RELEASE_RESERVATION
  | COMMIT_TO_ACTIVE_RUN
  | COMMIT_TO_NEW_RUN
  | CANCEL
  | DELIVERY_REJECT
  | RECONCILIATION_LATCH
  | CONTENT_RETIRE

PromptQueueCompanionChargeFact
  schema_version
  companion_kind
  runtime_session_id
  exact_ordered_event_batch_fingerprint
  item_row_mutation_count
  account_row_mutation_count
  content_reference_mutation_count
  artifact_hold_mutation_count
  total_auxiliary_row_mutations
  normalized_auxiliary_payload_base_bytes
  sequence_wrapper_max_bytes
  revision_wrapper_max_bytes
  conservative_charged_payload_bytes
  charge_contract_fingerprint
  storage_mutation_plan_fingerprint
  charge_fingerprint
```

`PromptQueueCompanionChargeFact`是registered event-safe `FrozenFactBase`，schema version、domain separator和own fingerprint全部冻结且覆盖所有字段。Charge contract为每个companion kind冻结允许的table/op matrix、最大row count、单row bytes、sequence/revision wrapper上限和total payload bytes。Handle绑定exact runtime session与ordered event candidate batch。

Canonical sequence与resulting row revision只在PostgreSQL transaction内分配，因此stable candidate形成前不得声称知道最终actual row bytes。中央factory必须从完整normalized mutation plan计算固定保守charge：

```text
conservative_charged_payload_bytes
  = normalized_auxiliary_payload_base_bytes
  + sequence_wrapper_max_bytes
  + revision_wrapper_max_bytes
```

其中wrapper上限由registered storage contract按mutation kind、最大canonical sequence位数、revision位数和JSON/row framing唯一派生，caller不能传入估值。该固定保守值在candidate形成、NONE retry和UNKNOWN confirmation期间不变，并与ledger physical-charge reservation exact join。

EventLog分配canonical sequence并产生stored rebind receipt后，transaction adapter从最终item/account/reference/hold rows重算`actual_auxiliary_payload_bytes`，并在任何auxiliary SQL mutation前验证：

```text
actual_auxiliary_payload_bytes <= conservative_charged_payload_bytes
actual row counts == frozen mutation counts
actual table/op set == companion-kind matrix
```

任一超界、自报fingerprint、batch/plan/charge mismatch或affected-row drift均整批rollback。Materialization account始终按固定`conservative_charged_payload_bytes`结算，不在sequence分配后回填actual值、不退款，也不新增terminal charge event/state transition。

`conservative_charged_payload_bytes`只覆盖item/account/reference/hold等auxiliary row canonical payload上界，不重复计算提前存储的large artifact bytes；artifact preparation使用ArtifactStore自己的storage budget。Queue companion不得绕过RuntimeSession materialization account，也不得在commit后补写或调整charge。Architecture guard只允许唯一`PostgresPromptQueueTransactionCompanion`修改这些queue auxiliary relations。

发现event与row不一致时必须fail closed；不得让caller或adapter在两者之间任选“看起来更新”的一侧继续。

#### 11.1.1 Bounded queue checkpoint 与 repair

Queue projection拥有独立的domain checkpoint，不依赖从session sequence 1完整fold：

```text
PromptQueueReducerContractFact
  schema_version
  reducer_id
  reducer_version
  reducer_contract_fingerprint

PromptQueueEventDomainRegistryBinding
  schema_version
  registry_id
  registry_version
  ordered_event_type_schema_accumulator
  registry_fingerprint

PromptQueueDomainCheckpointFact
  schema_version
  runtime_session_id
  reducer_id
  reducer_version
  reducer_contract_fingerprint
  event_registry_id
  event_registry_version
  event_registry_fingerprint
  checkpoint_generation
  through_sequence
  transition_count
  transition_accumulator
  account_revision
  next_accepted_ordinal
  pending_item_head_set_accumulator
  checkpoint_fingerprint

PromptQueueHeadReceipt
  schema_version
  reducer_contract_fingerprint
  event_registry_fingerprint
  checkpoint_generation
  checkpoint_fingerprint
  bounded_tail_first_sequence
  bounded_tail_last_sequence
  bounded_tail_count
  bounded_tail_accumulator
  resulting_queue_head_event_id: str | None
  resulting_queue_head_payload_fingerprint: Fingerprint | None
  resulting_account_revision
  resulting_row_set_accumulator
  receipt_fingerprint

PromptQueueCheckpointCommitGuard          # process-local, generation-scoped
  runtime_session_id
  expected_previous_through_sequence
  expected_previous_payload_fingerprint
  expected_account_revision
  expected_queue_head_event_id
  expected_queue_head_payload_fingerprint
  expected_row_set_accumulator
  guard_generation
```

Checkpoint/receipt是event chain的可重算proof，不是新semantic authority。V1明确复用现有mutable、validated `runtime_projection_checkpoints` relation，不新增immutable checkpoint table、artifact carrier、content-addressed checkpoint ID、retention或GC协议。Physical lowering唯一为：

```text
RawRuntimeProjectionCheckpoint
  projection_kind = "prompt_queue.v1"
  through_sequence = PromptQueueDomainCheckpointFact.through_sequence
  projection_schema_version = registered queue checkpoint schema version
  ledger_prefix = exact committed prefix at through_sequence
  validation_base_through_sequence = previous trusted checkpoint through_sequence
  validation_base_state_payload = previous trusted queue checkpoint state payload
  state_payload = canonical PromptQueueDomainCheckpointFact
  payload_fingerprint = central raw-checkpoint fingerprint
```

`PromptQueueDomainCheckpointFact`是该mutable row的typed state payload，不是第二个storage row。Queue account中的`checkpoint_generation/checkpoint_through_sequence/checkpoint_fingerprint`是唯一pointer projection。每个queue transition transaction必须增量推进account中的transition accumulator；checkpoint maintenance通过唯一`PromptQueueCheckpointCommitPort`在一个PostgreSQL transaction中：

1. 锁定runtime session、当前`runtime_projection_checkpoints` row和queue account/head；
2. 验证`PromptQueueCheckpointCommitGuard`的previous checkpoint、account revision、queue head和row-set accumulator；
3. 覆盖写入validated checkpoint row；
4. CAS推进queue account pointer与bounded-tail base；
5. 重读checkpoint/account/head并返回`PromptQueueHeadReceipt`。

该port以current pending/reserved row-set accumulator证明projection覆盖，不能接受caller自报的“drain complete”。Checkpoint row与account pointer不得由两个transaction或普通EventLog checkpoint API分别写入。

上述contract/binding/checkpoint state/head receipt均使用registered、schema-versioned `FrozenFactBase`和domain-separated central fingerprint factory；commit guard是不可序列化的process-local carrier。Caller不得直接实例化durable facts或提交自报fingerprint。

Reducer与registry规则：

- reducer ID/version/fingerprint覆盖完整transition matrix、row/account lowering、ordering、capacity、reservation和retirement semantics；
- registry binding覆盖所有queue transition event type的schema version/fingerprint与domain contract fingerprint；
- checkpoint factory、reopen reducer、offline doctor和transaction companion只能从中央registered contract读取这些identity；
- checkpoint中的任一contract与当前binary不匹配时fail closed，禁止用当前reducer解释旧payload；
- contract升级必须提供显式checkpoint migration/rebuild或reset，不允许只改常量后继续推进旧accumulator。

每个runtime session必须拥有唯一canonical generation-0 empty checkpoint：

```text
checkpoint_generation = 0
through_sequence = 0
transition_count = 0
transition_accumulator = H("prompt-queue-transition-genesis:v1", runtime_session_id, contracts)
account_revision = 0
next_accepted_ordinal = 1
pending_item_head_set_accumulator = canonical_empty_accumulator
resulting_queue_head_event_id = None
resulting_queue_head_payload_fingerprint = None
```

Head event fields只允许在generation-0/transition-count-0 genesis中同时为`None`；第一条queue transition FULL后必须同时变为non-null并永久由accumulator recurrence推进。

新session由唯一session bootstrap transaction同时创建queue account和genesis checkpoint。Hard-cut activation前，existing session由offline maintenance安装同一canonical genesis并证明尚无queue transition；production queue admission在genesis缺失时fail closed，绝不临时自建或猜测。这样第一次reopen不会因为“尚无checkpoint”进入reconciliation。

#### 11.1.2 Checkpoint maintenance 上界

冻结六个registry-owned界限：

```text
SOFT_TAIL_MAX_TRANSITIONS = 192
HARD_REOPEN_MAX_TRANSITIONS = 256
SOFT_TAIL_MAX_BYTES = 4 MiB
HARD_REOPEN_MAX_BYTES = 8 MiB
MAX_ADMITTED_TRANSITION_BURST = 1
MAX_ADMITTED_TRANSITION_BURST_BYTES = 64 KiB
```

必须满足：

```text
SOFT_TAIL_MAX_TRANSITIONS + MAX_ADMITTED_TRANSITION_BURST
  <= HARD_REOPEN_MAX_TRANSITIONS

SOFT_TAIL_MAX_BYTES + MAX_ADMITTED_TRANSITION_BURST_BYTES
  <= HARD_REOPEN_MAX_BYTES
```

每次queue transition admission按以下lock choreography执行，禁止在持有writer lock时等待checkpoint：

```text
acquire session writer lock
  -> 读取checkpoint/account head
  -> 按planned event + conservative companion charge计算最大burst
  -> 未越过hard bound：继续普通admission
  -> 需要checkpoint：安装或取得shared checkpoint attempt identity
release session writer lock
  -> wake/await shared checkpoint attempt，或返回queue_checkpoint_advance_required
reacquire session writer lock
  -> 从读取authority、capacity、safe point和candidate preparation之前完整重跑admission
```

达到soft watermark后必须wake session-owned checkpoint maintenance owner；若本次burst会越过hard bound，普通queue admission不得提交transition。等待checkpoint的caller cancellation只detach，不取消shared owner。

Checkpoint owner必须取得`PhysicalOperationKind.CHECKPOINT_COMMIT` reservation，并使用已有`PostgresConnectionLane.CHECKPOINT_MAINTENANCE`保留容量；不得占满普通queue/EventLog admission容量后再等待自身。它使用bounded page/bytes、absolute deadline和上述专用CAS transaction推进到observed account head；concurrent winner只允许exact confirm或基于新head重建candidate。Host close在释放EventLog/DB dependency前bounded drain该owner。Owner failure只latch queue admission，不反向取消已经运行的run；由于admission永远不能越过hard bound，下一次reopen仍可在上界内重建并重新推进checkpoint。

每个成功checkpoint必须满足`generation = previous + 1`、`through_sequence`不回退且精确等于其observed queue head、transition accumulator recurrence连续。达到soft watermark是强制创建时机；idle/close可以提前checkpoint，但不得产生空generation churn。Checkpoint FULL后mutable checkpoint row、account pointer与head receipt同事务安装；NONE复用same candidate。UNKNOWN confirmation必须同一snapshot读取checkpoint row与account pointer：exact candidate match为FULL；previous pair完全未变为NONE；严格更新且能从candidate recurrence证明兼容的concurrent checkpoint为`SUPERSEDED_BY_COMPATIBLE_WINNER`；其余进入queue reconciliation。不能只看到generation增加就接纳winner。

Production reopen/bootstrap只能：

1. 在一个database snapshot中读取latest trusted queue checkpoint、account/head receipt和current pending/reserved row set；
2. 通过queue-event-type indexed read port分页读取`(checkpoint.through_sequence, head]`的bounded typed delta；
3. 重算transition count/accumulator、head identity、account revision和row-set accumulator；
4. exact match后安装queue projection；
5. delta超过event/bytes/deadline bound、checkpoint缺失/不可信或任一identity不匹配时返回typed `queue_projection_reconciliation_required`，不得退化为完整session scan。

Privileged offline doctor可以在exclusive maintenance barrier下分页fold完整queue transition chain，重建checkpoint/row/account并写typed repair receipt。它只读取queue-domain typed events，不扫描/解码无关session events，也不进入Host open、UI attach或普通reconnect路径。

### 11.2 `TUI-UX-QUEUE-001` 两类用户意图

#### Steer current run

适合：

- “先别继续搜索，直接总结”；
- “刚才路径写错了，用另一个目录”；
- “顺便也检查测试”；
- “不要修改文件，只做 review”。

只有 runtime 声明的 safe point 才能接纳 steer。

#### Follow-up next run

适合：

- 当前 turn 类型不支持 steer；
- 用户明确选择 next；
- 当前已进入 finalization；
- 当前等待 user interaction；
- 当前 run 已开始 terminalization。

### 11.3 Queue item 最小语义

durable queue item至少需要：

- schema version；
- stable queue item ID；
- runtime session ID；
- source client ID；
- canonical submitted time；
- intent kind；
- bounded inline或artifact-backed user content reference；
- content semantic fingerprint与attribution fingerprint；
- requested delivery mode；
- current authoritative status；
- row revision；
- exact head transition event reference/fingerprint；
- accepted/reserved delivery boundary；
- reservation identity/generation与ordered set fingerprint；
- cancellation/rejection reason；
- ordering key。

queue item ID由runtime session、source client、client submission ID和content semantic identity稳定派生。重复submit：

- same ID + same semantic/attribution = exact confirmation；
- same ID + different payload = conflict；
- caller不得依赖当前时间或数据库sequence生成retry ID。

### 11.4 建议状态

```text
client_draft
  -> submitting
  -> accepted_pending
       -> steer_reserved
            -> committed_to_active_run
            -> released_to_pending -> accepted_pending
            -> delivery_rejected
            -> reconciliation_required
       -> follow_up_reserved
            -> committed_to_new_run
            -> released_to_pending -> accepted_pending
            -> delivery_rejected
            -> reconciliation_required

accepted_pending
  -> cancelled
  -> delivery_rejected
  -> reconciliation_required
```

`client_draft`和`submitting`只属于client。其余状态由event chain定义，repository row只投影current head。`committed_to_active_run`、`committed_to_new_run`、`cancelled`和`delivery_rejected`都是吸收态，不再额外写一个无语义的generic `terminal`状态。

Reservation规则：

- reservation owner绑定exact queue item set、ordered fingerprint、run/safe-point target、generation和absolute deadline；
- preflight、follow-up target、RunStart preparation、provider-input preparation或safe-point validation在stable commit candidate形成前失败，且item仍可合法重试时，写`PromptQueueReservationReleasedEvent`并原子返回`accepted_pending`；
- stable candidate已经形成后，physical `NONE`必须由同一reservation owner重试同一candidate，不能先release再生成不同payload；
- policy/capacity/content发生确定性不合法变化时写typed `delivery_rejected`；
- explicit steer错过其合法boundary必须`delivery_rejected`，不得静默变成next；
- `auto`在run先terminal时可以先通过typed release回到pending，再由一个新的follow-up reservation generation接管；这不是原reservation的隐式改写；
- caller cancellation只detach waiter，不得直接释放已经in-flight的reservation；
- confirmation无法证明唯一结果时进入`reconciliation_required`，禁止后续reservation。

#### 11.4.1 Outcome vocabulary

队列实现必须区分四层状态，禁止跨层复用同名enum：

```text
EventBatchCommitOutcome.status
  full | none | unknown

QueueAtomicCandidateConfirmation
  FULL | NONE | CONFLICT | UNAVAILABLE

PublicationDeliverySummary
  completed | enqueued | unavailable | failed_after_commit

UiObservationDisposition
  live_delivered | tap_detached | catch_up_required
```

语义如下：

- 本节正文中的physical `FULL | NONE | UNKNOWN`分别是现有`EventBatchCommitOutcome.status`三个小写值的规范化写法，不新增平行Python enum；
- physical `FULL`表示EventLog transaction已经提交完整ordered batch；
- physical `NONE`表示已证明本次transaction没有提交；
- physical `UNKNOWN`只表示连接/取消窗口无法直接知道commit结果，随后必须使用新authority执行exact confirmation；
- confirmation `FULL`要求event batch、item row和account head全部exact match；
- confirmation `NONE`要求candidate event不存在且row/account未发生该transition；
- confirmation `CONFLICT`只在完整读取后证明event/row/account发生矛盾、partial split或不同winner时使用；
- confirmation `UNAVAILABLE`表示本次无法完成证明，可重试且不得建议用户重置；它不是physical commit outcome，也不复用D3 job的`UNRESOLVED`；
- `failed_after_commit`是由非空publication errors投影出的summary，不是physical EventLog status；
- publication与UI observation发生在durable commit之后，失败只触发catch-up/latch，不得回滚或改写queue domain disposition。

UI 只有在 acceptance transaction FULL 后才显示 `Queued`：

```text
single RuntimeSession transaction
  PromptQueueAcceptedEvent
  prompt_queue row INSERT/CAS
  prompt_queue_account ordering/capacity CAS
  exact queue-content reference INSERT
  artifact hold PREPARED -> CONSUMED CAS    # artifact-backed branch only
  exact PromptQueueCompanionChargeFact
  physical materialization-account reservation/settlement
```

- physical FULL：返回authoritative acknowledgement；
- physical NONE：复用same immutable candidate重试；
- physical UNKNOWN：不得显示queued；执行`QueueAtomicCandidateConfirmation`；
- confirmation FULL/NONE：分别接受exact committed winner或重试same immutable candidate；
- confirmation CONFLICT：进入queue/ledger reconciliation并fail closed；
- confirmation UNAVAILABLE：保持stable candidate和owner，向UI显示`Confirming submission...`，不得声称已提交或未提交；
- publication unavailable：durable acceptance仍可能FULL，UI通过exact receipt或reconnect bootstrap恢复，不得告诉用户“没有提交”。

### 11.5 Safe-point 行为

运行中 Enter 的默认行为可配置：

- 默认 `auto`：Host 判断 steer/follow-up；
- 显式 `steer`；
- 显式 `next`。

`requested_delivery_mode`不等于最终placement。`auto`在durable acceptance时只表示请求策略；最终 steer/follow-up disposition由reservation CAS在当时的exact run state上决定。

V1只允许一个steer safe point：

```text
after_tool_results_before_followup_model_input_freeze
```

即：

- 当前physical model call/stream期间不修改已经冻结的provider input；
- tool batch尚未terminal时只保留accepted item；
- tool results全部FULL且下一次provider input尚未freeze时，可以reserve一个或多个有界steer item；
- reserve后必须在同一safe-point owner中commit，不能回到普通accepted状态后被另一个owner再次选择。

V1 placement matrix：

| Host/run状态 | `auto` | explicit `steer` | explicit `next` |
|---|---|---|---|
| idle | next run | reject：无active run | next run |
| preparing/first model前 | next run | reject：initial input已冻结 | next run |
| model streaming | 等待下一合法tool boundary；若run先terminal则next | 保持pending直到boundary；run terminal则typed reject | next run |
| tool batch active | 等待batch terminal后的safe point | 等待batch terminal后的safe point | next run |
| waiting approval/plan/MCP | next run | reject | next run |
| suspended | next run | reject | next run |
| stopping/finalizing | next run | reject | next run |
| reconciliation/closing/closed | reject | reject | reject |

UI 必须显示authoritative最终判定，而不是只显示用户请求：

```text
Queued for current run · after active tool call
Queued for next run · current run is finalizing
```

### 11.6 编辑与取消

只有尚未 dispatch 的 queue item 可编辑。

V1编辑唯一建模为：

- cancel old item；
- submit replacement；

不得让 client 原地修改已被 safe point 读取的对象。

V1采用cancel + replacement：

- 只有`accepted_pending`可cancel；
- `steer_reserved`/`follow_up_reserved`已由dispatch owner持有，UI只能等待terminal disposition；
- cancel和replacement是两个独立stable candidate，不伪装成原地mutation；
- cancel FULL后旧item才从pending projection消失；
- replacement acceptance失败时旧item保持cancelled，不自动复活。

### 11.7 Dispatch 与 run commit 原子边界

Follow-up消费：

```text
single RuntimeSession transaction
  exact queue row CAS: accepted/reserved -> committed_to_new_run
  queue disposition event
  matching RunStart batch
  Host ingress attribution exact-join queue item
  exact PromptQueueCompanionChargeFact + physical account settlement
```

Steer消费：

```text
single RuntimeSession transaction
  validate active segment/safe-point authority
  exact queue row CAS: steer_reserved -> committed_to_active_run
  typed UserSteerCommittedEvent
  provider-input/current-input generation append
  exact PromptQueueCompanionChargeFact + physical account settlement
```

AgentRuntime不得接受普通字符串callback来“插入”当前run。下一次model input只能从已经FULL的provider-input generation读取steer内容。

Commit后断连先得到physical `UNKNOWN`，再进入`QueueAtomicCandidateConfirmation`。confirmation只有`FULL | NONE | CONFLICT | UNAVAILABLE`；stable queue candidate、reservation identity和ordered item set在所有confirmation/retry generation中不得变化。publication/observation failure只决定catch-up和UI提示，不参与该confirmation。

Queue acceptance/reservation/disposition event均为non-transcript audit。follow-up只在matching RunStart FULL后成为新的user transcript cell；steer只在`UserSteerCommittedEvent` FULL后成为当前run的user-intent cell。UI不得同时把queue preview和durable user cell渲染成两条消息。

### 11.8 Ordering、容量与恢复

- runtime session内使用durable accepted ordinal形成FIFO；
- explicit steer和auto不通过client timestamp抢占已经accepted的item；
- 单次safe point有最大item数、总bytes和token预算；
- queue整体有item/bytes/artifact retention上限；
- 超限返回typed capacity rejection，不能静默丢弃最旧输入；
- reconnect bootstrap读取durable pending/reserved items和queue revision；
- reserved item recovery必须先exact-confirm对应RunStart/steer event，再决定完成、释放或reconciliation；
- queue内容不进入UI notification channel作为唯一载体。

## 12. Transcript 与渐进披露

### 12.1 `TUI-UX-TRANSCRIPT-001` Visual cell分类

推荐设计层分类：

| Cell | 默认呈现 |
|---|---|
| user message | 完整正文，large paste 可折叠 |
| assistant text | streaming 后形成稳定 markdown |
| thinking/reasoning | 默认折叠或仅显示状态 |
| grouped read/search | 单行计数 + latest hint |
| terminal command | command + live progress + terminal summary |
| file edit | path + diff summary，可展开 diff |
| tool error | 可见 stable code + actionable summary |
| recoverable tool error | muted，默认不抢占注意力 |
| approval decision | compact audit row |
| plan update | checklist/progress |
| subagent activity | compact tree/status |
| MCP activity | server/tool/status |
| compaction | one-line boundary summary |
| queue disposition | bottom pane/ephemeral，必要时 terminal audit |
| run terminal | final/failed/aborted summary |

### 12.2 工具默认分组

可聚合：

- read；
- glob；
- grep/search；
- memory lookup；
- artifact lookup；
- repeated MCP read/query；
- short diagnostic commands；
- repeated status/list calls。

不应与其他操作无条件聚合：

- write/edit/delete；
- permission；
- external network mutation；
- git commit/push；
- background process creation；
- subagent creation；
- plan state transition；
- any error；
- user-visible artifact creation。

### 12.3 Group identity

UI grouping 是 process-local projection，但必须由稳定事实决定：

- run ID；
- contiguous activity region；
- tool family；
- actor/subagent；
- terminal state；
- side-effect class。

禁止只按 tool name 全局合并，否则会跨越 assistant reasoning、user steer 或 approval boundary。

每个terminal visual cell必须保留可重建的source identity：

- runtime session ID；
- projection kind/schema version；
- ordered source event references或contiguous sequence range；
- source payload fingerprint accumulator；
- actor/run/tool/subagent identity；
- cell projection fingerprint。

active cell可以使用process-local owner/generation identity，但terminal后必须由durable source重新物化，不能把active object原地标记为“durable”。Inspector跳转消费exact source references，不接受renderer生成的行号或列表index。

### 12.4 Compact 与 verbose

至少提供：

- session default；
- 当前 turn toggle；
- 当前 cell expand；
- full transcript/Inspector。

建议快捷键：

- `Ctrl+O`：当前视图 compact/verbose；
- Enter/click：展开选中 cell；
- `/inspect`：进入 durable fact 视图；
- `/copy-last`：复制最后 assistant reply。

实际 keybinding 可在实施阶段确定。

### 12.5 绝不隐藏的内容

以下内容即使在 compact mode 也必须可见：

- user action required；
- non-recoverable error；
- permission denial；
- session reconciliation latch；
- destructive operation；
- model/provider terminal failure；
- failed close/drain；
- context/rollout exhausted；
- incomplete or interrupted output；
- queue rejection。

## 13. Long-running task UX

### 13.1 `TUI-UX-PROGRESS-001` 状态必须回答四个问题

1. 正在做什么？
2. 已运行多久？
3. 最近是否有进展？
4. 用户能做什么？

示例：

```text
● Running tests (18s · 426 lines)
  └ tests/test_runtime_session.py::test_close_drain
  Esc stop · Ctrl+O details
```

### 13.2 Terminal process

Live terminal card 建议显示：

- normalized command；
- cwd；
- process ID；
- elapsed；
- line/byte count；
- latest meaningful line；
- running/waiting/exited/killed；
- exit code；
- output artifact/read-more。

UI-only monitor stream只用于 live preview。

Terminal completion 和 future replay 必须来自 durable terminal facts，不能从 UI stream补造。

这与现有长期契约一致：

- [TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md:328)
- [INSPECTOR_PROJECTION_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md:401)

### 13.3 Subagent

默认显示：

```text
Agents 2 running · 1 completed
  ├─ review_api        running  31s
  ├─ inspect_tests     completed
  └─ benchmark_db      waiting dependency
```

只在以下情况主动插入 transcript：

- spawn；
- terminal；
- user action required；
- failure；
- parent handoff。

高频 phase/progress 留在 sidebar/live activity。

### 13.4 MCP

MCP 状态优先放在 status/sidebar：

```text
MCP 2 ready · 1 connecting
```

状态变化可以使用 bounded transient notice，但不要每次 refresh 都写 transcript。

模型是否可见某个 MCP 由 run-frozen capability exposure 决定；UI 的 `ready` 不等于当前 active run 已经获得该工具。

UI 应区分：

- process connection ready；
- installation ready；
- current run exposed；
- unavailable/failed。

## 14. Typed interaction 设计

### 14.1 `TUI-UX-INTERACTION-001` Approval

Approval view 应根据 action 类型显示：

#### Terminal

- command；
- cwd；
- environment/sandbox；
- network；
- reason；
- risk hints；
- once/session/deny。

#### Edit/write

- path；
- unified diff；
- added/removed lines；
- outside-workspace warning；
- once/session/deny。

#### MCP/network

- server；
- tool；
- target host；
- submitted fields的 secret-safe summary；
- external side effect；
- once/session/deny。

### 14.2 Plan question

支持：

- 单选；
- 多选；
- free text；
- option shortcut；
- question progress；
- revise；
- approve/cancel。

Plan mode 的 permission snapshot 和 read-only contract必须明确显示，不能只改变 prompt prefix。

### 14.3 MCP input-required

> Cross-spec rationale：event-safe interaction view与Python hydration owner由Foundation拥有；attachment/secret frame由Protocol拥有；Go ephemeral input/display state由Bubble Tea规格拥有。本节不定义可序列化carrier。

不要求用户手写 JSON 作为主路径。

应根据 typed request schema生成：

- field label；
- description；
- input kind；
- optional/default；
- validation；
- submit/cancel。

MCP interaction必须拆成三层，且跨进程后不再把Python sealed object冒充UI carrier：

```text
McpInteractionRequestView
  Python event-safe presentation
  可进入普通snapshot/delta
  URL mode只暴露private-url-present与bounded safety summary

HostMcpInteractionSecretService
  Python唯一decrypt/hydration/validation/expiry owner
  从durable encrypted continuation签发attachment-bound ephemeral lease

BubbleTeaSecretInteractionState
  Go current-controller临时输入/显示状态
  只经non-replay secret frame收发
  detach/takeover/expiry后不可恢复
```

UI client从来不是private URL/request secret的durable authority。Detach、renderer crash或client replacement只revoke attachment lease并清理Go local state；不得删除或修改底层encrypted continuation。只有exact resolution、cancel、expiry或typed closure transaction可以通过MCP continuation companion删除/terminalize storage row。

Reconnect必须从exact durable pending interaction与continuation control重新hydrate Host owner，并为新controller attachment签发新lease。旧attachment ID、lease generation、URL reveal或response draft不得恢复、转移或重新激活。解密失败、expiry、binding mismatch和missing carrier均走现有typed MCP terminalization/reconciliation，不得从event-safe request view猜出secret。

产品约束：

- request-side secret、response draft、private URL和exact form value不进入普通projection、protocol replay、transcript、copy-all、status、diagnostic或snapshot golden；
- secret frame只接受current controller attachment并绑定exact interaction/round/request key/owner epoch；
- stale interaction、request set、controller generation或lease必须拒绝；
- URL mode显示完整URL并要求显式同意，但禁止prefetch、自动open和普通logging；
- Go secret component禁用history、autosuggest、completion、ordinary undo persistence和generic copy handler；
- mutable buffer退出时best-effort覆盖并清理，但Python、Go和terminal emulator均不能承诺已经复制/显示的plaintext物理零化；
- Legacy REPL不新增secret reader；遇到secret-bearing MCP form/private URL时typed reject并要求使用Bubble Tea controller。

完整lease、frame、revoke与memory承诺由Protocol `TUI-PROTO-SECRET-*`和Bubble Tea `TUI-BT-SECRET-*`唯一冻结。

### 14.4 Generic fallback

未知 interaction 必须 fail closed，但 UI 可展示：

- interaction kind；
- owner；
- bounded sanitized payload；
- stable error code；
- Inspector command；
- cancel/close availability。

## 15. Status line

### 15.1 `TUI-UX-STATUS-001` 建议segment

按优先级：

1. session state/action required；
2. elapsed + stop hint；
3. model/profile；
4. permission mode；
5. context usage；
6. queue depth；
7. MCP；
8. subagent；
9. terminal process；
10. workspace/git。

示例：

```text
Working 18s · pro · trusted_host · ctx 31% · queue 1 · MCP 2/3 · agents 1
```

### 15.2 窄屏退化

窄屏只保留：

```text
Working 18s · Esc stop · queue 1
```

其余内容进入 `/status` 或 sidebar。

### 15.3 更新策略

- event-driven；
- elapsed 最多每秒更新；
- animation frame不重算昂贵状态；
- git/MCP/status aggregation 有独立缓存；
- status component 使用 stable height；
- unknown 不显示伪造的 0。

renderer唯一允许读取的是immutable `TerminalUiStatusSnapshot`。该snapshot由projection controller在event/operational frame到达时增量替换：

- status render为O(1)，不得调用`HostSession.summary()`；
- 不得同步读取EventLog、PostgreSQL、ArtifactStore、GraphStore或git；
- elapsed由本地monotonic clock和已冻结start time派生；
- git/workspace等较慢segment由bounded auxiliary owner刷新，stale时保留last-known并显示stale/unknown；
- 高频frame按owner/kind coalesce，建议最多10Hz invalidate；
- terminal/action-required更新不受普通progress debounce影响。

## 16. Startup 与首屏

### 16.1 `TUI-UX-STARTUP-001` Banner应立即出现

Optional MCP、LSP 或其他远端 discovery 不阻塞基础 shell。

推荐：

```text
Pulsara · workspace little_snake
MCP docs-langchain connecting

pulsara>
```

Python launcher先建立startup projection与TerminalClientGateway，再启动Bubble Tea child。startup controller拥有：

```text
ui_started
  -> opening_host
  -> host_ready
  -> open_failed_retryable | open_failed_terminal
```

opening期间：

- composer可编辑client-local draft；
- submit disabled，并明确显示`Waiting for runtime…`；
- 不创建prompt queue item；
- retry沿用同一UI application和draft；
- quit只关闭startup owner和已经取得的partial Host resources；
- Host ready后才安装observation bootstrap和queue/interaction capabilities。

不得为了“首屏更快”在后台创建一个UI无法drain的Host open task。

### 16.2 Required dependency

Required dependency 可以阻止 run admission，但不必阻止 UI 启动。

UI 应显示：

- waiting；
- deadline；
- retry；
- failure action；
- close availability。

### 16.3 首次 run 前完成的连接

如果 MCP 在用户提交首条消息前 ready：

- 直接刷新 status/header；
- 首条 run 使用正常 frozen exposure；
- 无需额外 transcript notice。

若 active session 中途 ready：

- UI transient notice；
- capability 是否进入 next run 由 Host safe point决定；
- 不暗示当前 run 已经能调用。

## 17. Error、cancel 与 recovery UX

### 17.1 `TUI-UX-ERROR-001` Error三层

每个错误展示：

1. 用户可理解的 summary；
2. stable error code；
3. 查看 Inspector/diagnostics 的入口。

示例：

```text
Run failed while compiling context.
code=context_input_manifest_write_unknown
Inspect: /inspect run run:...
```

### 17.2 Publication failure

UI 必须区分：

- durable commit失败；
- durable commit成功但 publication/observer失败；
- commit outcome unknown；
- ledger untrusted。

不能把 observer failure显示成“操作没有发生”。

### 17.3 Stop

Stop UX：

```text
Esc
  -> Stopping current run…
  -> Draining terminal command and model stream…
  -> Run aborted
```

若 drain 超时：

```text
Stop is still pending.
Session close remains blocked to preserve durable ownership.
```

### 17.4 Reopen/recovery

Reopen 时把 repair 事实投影为一条 bounded system row：

```text
Recovered an interrupted run · run:abc · no model output accepted
```

详细 repair chain留在 Inspector。

## 18. 技术栈判断

### 18.1 Bubble Tea v2 frozen selection

Pulsara选择Bubble Tea v2的理由是架构，而不是star数量或流行度：

- `Model -> Update -> View`适合消费typed snapshot、closed delta和closed command outcome；
- Go client可以独占TTY，Python runtime不再与renderer争夺stdin/stdout；
- renderer process与run physical owner分离，client crash/detach不等于run cancellation；
- 跨语言client会迫使Runtime/UI boundary成为真实、版本化协议；
- Terminal UI被确认为长期一等入口后，不再值得先建设一套Python full-screen UI再迁移。

该选择已在S0 gate后冻结。S0已经验证宽字符光标、多行textarea、大型bracketed paste、streaming期间持续输入、resize、自动化tmux、真实SSH、terminal crash restore、四目标cross-build以及Python parent/Go child signal ownership。真实IME、attached tmux视觉检查与非本机clean-runner启动作为后续兼容性/release regression保留，不再阻塞S1；依赖升级仍须重跑对应矩阵。

### 18.2 不建设Textual或prompt_toolkit full-screen

本轮明确不进入以下修改面：

- `prompt_toolkit.Application` full-screen renderer；
- Textual lifecycle、message bus、CSS或widget tree；
- Python进程内第二套layout/state/render abstraction；
- Bubble Tea失败后静默降级到能力不完整的Legacy REPL。

现有`prompt_toolkit.PromptSession`保留为显式、冻结、maintenance-only的历史入口。它不叫fallback，不承诺与Bubble Tea功能对等，不获得queue、steer、semantic transcript或secret interaction等新能力。

### 18.3 Renderer-independent presentation kernel

Bubble Tea以及未来desktop/web client都应消费相同的Python presentation semantics：

- unified durable history entries/cells；
- independent operational activity；
- pending input snapshot；
- interaction view model；
- status segments；
- notifications。

这样未来换 UI 不需要重新解释 durable events；canonical transcript的acceptance、suppression、tool pairing与terminal-document join仍只由`TranscriptProjectionStateStore`决定，presentation kernel不复制这套reducer。

projection package只包含：

- immutable UI DTO；
- canonical transcript leaf/delta到cell的pure projection；
- registered projection-purpose policy与durable-audit typed extractors；
- canonical leaf/audit cell到unified history placement/root的pure projection；
- bounded persistent history tree、path-copy node/root与typed checkpoint confirmation/restore；
- registered fixed placement-key contract，以及逐EventLog sequence的tail segment/source-prefix lineage；
- stable cell/group/status identity factories；
- bootstrap/gap validation；
- bounded redaction、security truncation和physical-cap policy。

它不得import：

- `prompt_toolkit`、Textual或任何Go/Bubble Tea类型；
- concrete HostSession；
- concrete EventLog/PostgreSQL adapter；只允许依赖`primitives.stored_event.RawStoredEventEnvelope`最终owner；
- terminal/MCP/subagent manager；
- secret response carrier。

Python kernel拥有display cell kind、group identity、severity、source references和bounded public content；它只能消费canonical transcript fold result、registered durable audit input和operational frames。Event policy按`canonical_transcript_handling`与`durable_audit_handling`两个独立axis决策，而不是把event type互斥分类：`RunStartEvent`/`RunEndEvent`可同时由transcript reducer贡献canonical leaf，并由受限extractor贡献run lifecycle/status。每个typed extractor binding必须携带ID、version与contract fingerprint；process-local registry按完整三元组exact resolve，同一ID/version不同fingerprint是composition conflict，retained historical schema所需binding不得被current callable替代。Audit extractor只能读取registry allowlist中的非正文字段，且绝不能产生`UserPromptCell`、`AssistantMessageCell`或`ToolTerminalCell`。Go client拥有line wrap、颜色、layout、selection、scroll、collapse preference、key routing和render invalidation。Observation service负责I/O与authority high-water；presentation projection和Bubble Tea `Update/View`都不得执行阻塞I/O。

### 18.4 Local client protocol与process topology

Python继续持有Runtime、presentation kernel和TerminalClientGateway；Bubble Tea作为独立Go process连接本地versioned protocol。协议必须分离control、observation、command和secret四个plane，并至少冻结：

- `authority_high_water`与`projection_revision`两种cursor；
- mutation command的`client_instance_id/attachment_id/command_id/expected target/generation`；
- 多read-only observer、单interactive controller；
- bounded client buffer、`GAP`和snapshot rebuild；
- attachment-bound secret lease、无普通replay、detach/takeover revoke；
- runtime directory `0700`、socket `0600`和peer UID validation。

完整wire contract只由`PULSARA_TERMINAL_CLIENT_PROTOCOL_CONTRACT.zh.md`拥有。

## 19. 推荐阶段

F/B namespace只表达contract ownership，不表达PR合入顺序。生产落地必须按S0-S6端到端vertical slice推进；每个slice同时贯通Python authority、presentation、wire、Go client和integration test，禁止先累计多层无消费者抽象。

### S0：Bubble Tea feasibility spike

目标：

- 使用Bubble Tea v2/Bubbles v2建立一次性、非production spike；
- 验证CJK/IME、wide rune cursor、多行textarea和大型bracketed paste；
- 验证持续streaming时composer仍可输入；
- 验证resize、tmux、SSH与alternate-screen crash restore；
- 验证Python parent/Go child的SIGINT、SIGTERM、SIGHUP和unexpected exit ownership；
- 产出darwin arm64/amd64与linux amd64/arm64测试artifact；
- 记录CPU、resident memory、input latency和render jitter基线。

2026-08-01的disposable S0 fixture已经完成20Hz/100Hz各20次的本机darwin/arm64基线：1秒warm-up、3秒active window、10Hz process sampling与每轮20个交错keypress probe全部通过。CPU/RSS通过`proc_pid_rusage`采样，renderer cadence来自Bubble Tea最终PTY output writer的physical write seam；完整阈值、per-run结果与binary identity由`clients/terminal/spikes/s0/evidence/darwin-arm64-performance.json`持有。

同日真实远程host fixture也已通过：macOS OpenSSH → Windows OpenSSH/ConPTY → WSL2 Linux x86_64，运行现有`linux/amd64` artifact并验证SHA-256、UTF-8、TERM、CJK、alternate-screen、keypress latency、abrupt disconnect、remote process exit、parent emergency restore、reconnect及删除后的remote staging absence。证据由`clients/terminal/spikes/s0/evidence/real-ssh-plumliuwin-wsl2-amd64.json`持有。该结果关闭resource/render与真实SSH自动化项，不替代真实IME、attached tmux、terminal emulator视觉检查或non-native clean-runner证据。

验收：

- 所有阻断项有可复现fixture、terminal matrix和明确PASS/FAIL；
- client crash后terminal mode可恢复，Python probe owner不被误取消；
- S0只验证framework与process可行性，不接生产Runtime、EventLog或secret；
- S0通过后冻结Bubble Tea路线，后续不建设Python full-screen TUI。

### S1：只读observation vertical slice

目标：

- 建立session-scoped observation port；
- 实现非等待式`UiCommittedEventTap`与`bootstrap as-of H -> subscribe H+1`线性化；
- 让EventLog一次生成`RawStoredEventEnvelope`并随`EventWriteResult`上传；
- 将single/batch/conditional append统一切为`StoredEventBatchCommitReceipt`；
- 将exact candidate confirmation/idempotent winner切为逐candidate raw match evidence，并只在FULL classifier后产出`ConfirmedFullStoredBatch`；
- 为generic reopen/doctor/catch-up建立`JoinedRawStoredEventRangeProof`，升级committed reducer registration为live receipt/restored range双入口；
- 分离committed envelope与operational frame；
- 建立pure `canonical fold result + durable-audit input -> unified history placement/root`与独立`operational frame -> activity state` reducer；
- 建立bounded persistent history tree、完整materialization policy、validated generation-0 checkpoint、FULL/NONE/UNKNOWN/CONFLICT confirmation、bounded production restore与offline doctor rebuild；
- 建立逐sequence tail segment、noop segment carrier、durable source-prefix transition proof与growth quote/reservation capacity owner；
- 建立O(1) `TerminalUiSessionSnapshot`；
- 建立bounded `PresentationHistoryViewportSnapshot`、单一latest-root cursor pair、bounded pinned-root cursor states与`PresentationHistoryPagePort`；
- 定义只读protocol bootstrap snapshot；
- Bubble Tea显示真实session transcript viewport；
- Legacy REPL继续作为冻结的旧入口，不参与该slice。

验收：

- bootstrap与并发commit之间不丢event；
- `stored_batch_receipt.owned_stored_events`与`raw_stored_envelopes`逐项exact join；business/accounting projection构成完整disjoint partition；
- 所有production EventLog append路径都返回storage receipt，不存在上层补建raw envelope的兼容分支；
- UNKNOWN confirmation、idempotent winner和持有stable candidate的restart repair返回raw-row-backed candidate evidence；generic restart只返回range proof。Partial/non-contiguous candidate evidence不能构造receipt或进入tap；
- FULL confirmation构造与normal commit相同的physical receipt；normal receipt不重复decode；tap对next、whole duplicate、partial overlap、gap/fingerprint conflict执行closed原子entry矩阵；
- live receipt fold、single restored range与多种bounded range partition得到相同canonical state/leaf set/accumulator；只有live fold可以形成tap entry；
- live/restored输入得到相同unified history placement/root，root exact绑定policy/extractor registries，Run lifecycle只产生`AuditCell(run_lifecycle)`；
- canonical replacement继承transcript reducer anchor而不移动到history尾部；audit只通过proved before/after/gap anchor合并；
- append/replacement/retirement使用transcript reducer签发的stable spine transition proof；retired anchor保留不可渲染tombstone，已有audit不被任意迁移；
- placement key由registered ID/version/fingerprint和固定75-byte framing唯一编码；Python/Go/historical root使用同一exact binding；
- checkpoint work按stable placement key只path-copy affected tree paths，不随total history线性增长；插入/删除不重写未受影响suffix；page read通过subtree counts派生rank并满足tree-height/node-read hard bounds；
- checkpoint FULL后root-advanced frame原子交付new latest cursor pair、consumed segment prefix、retained concurrent segment suffix与closed resident transition；old pinned cursor继续读取old root但不承担follow-tail；NONE retry复用stable candidate，UNKNOWN进入reconciliation；
- checkpoint candidate冻结后并发append/noop不会在FULL swap时丢失或重复；noop-only suffix仍推进source/segment lineage；post-cut rewrite/retirement只能废弃candidate或rebuild；
- ordinary capacity只按`confirmed + tail + active reservation remaining + requested quote`计算；terminalization maintenance reserve只隔离soft threshold与hard maximum。Soft history capacity触发typed session rotation且已准入run仍能terminalize；hard exhausted不silent truncate；
- protocol/client只消费一种directionless history cursor和一个page RPC；每个root一对、attachment一个latest pair及bounded pinned old-root states，不包含feed kind或cross-feed merge；operational activity不进入root/page/revision；
- tap critical path不执行event serialization/schema resolve/decode；
- ring覆盖、ring eviction、catch-up buffer与ledger page merge均能证明连续prefix；
- duplicate/gap/retention failure均有typed结果；
- slow observer被detach，run/Host close不被阻塞；
- bootstrap不扫描完整session history，viewport resident cells/bytes有硬上限；
- snapshot/status read path不执行数据库或artifact I/O；
- projection package不importrenderer或concrete storage。

### S2：增量、gap与reconnect vertical slice

目标：

- projection delta与独立`projection_revision`；
- durable `authority_high_water`、operational generation/cursor和client revision分层；
- bounded gateway/client buffer；
- `GAP -> bounded catch-up | snapshot rebuild`；
- client detach/reconnect与duplicate/overlap ingestion；
- Bubble Tea viewport在reconnect后保持合法follow-tail/unseen语义。

验收：

- durable writer、publisher和run owner均不等待Go client；
- overflow只detach受影响attachment或发送`GAP`；
- event产生零个或多个projection change时两种cursor仍自洽；
- reconnect不重复或遗漏可见semantic cell；
- Go client不接收或解释`AgentEvent`、`RawStoredEventEnvelope`或storage receipt。

### S3：主动控制vertical slice

目标：

- Bubble Tea `Model/Update/View` startup owner；
- 多行 composer；
- large paste；
- command registry；
- live assistant/activity projection；
- viewport scroll/follow-tail/unseen count、selection/copy和lazy history navigation；
- hierarchical Esc/Ctrl-C routing；
- stop state；
- alternate-screen恢复与bounded exit summary。
- typed prompt/stop command、stable command ID和server receipt；
- disconnect后按同一command ID查询winner，不重放physical mutation。

验收：

- shell在Host open前可见，open失败可retry/quit；
- active run期间composer仍可编辑，但Enter只保留draft并提示queue尚未启用；
- stop不依赖新一轮输入循环；
- observer/UI crash不取消run；
- 用户向上滚动时live event不移动viewport，恢复follow-tail后unseen count正确清零；
- 所有退出路径恢复terminal mode且不回灌完整transcript；
- Legacy REPL现有行为保持冻结，但不承诺新command parity。

### S4：Typed interaction与secret vertical slice

目标：

- tool-specific approval；
- plan question/exit；
- MCP form/URL input-required；
- interaction view stack；
- draft preservation；
- encrypted continuation authority、Host hydration owner与UI sealed borrow分层；
- event-safe request view、sealed request-side secret与sealed response draft；
- Bubble Tea attachment-local secret input state与history/snapshot hard guard；
- current controller lease、takeover/revoke和secret frame非replay contract。

验收：

- 主路径不要求手写JSON；
- interaction owner/identity明确；
- stale interaction不能误提交；
- MCP value/private URL不会进入history、snapshot、logs或copy-all；
- detach后durable continuation仍可由新client经Host exact hydration接管，旧UI borrow不可复活；
- 同一owner epoch签发多个borrow不会互相撤销；owner epoch在detach/replacement/terminal/close后推进，旧private-URL borrow handle的后续`reveal()` fail closed；不宣称撤销已经返回/显示的plaintext；
- approval/resolution与durable fact一致。

### S5：Durable queue vertical slice

目标：

- runtime-session scoped durable queue；
- EventLog transition authority + CAS head projection；
- acceptance/replace/cancel confirmation；
- inline/confirmed-artifact content preparation与durable PREPARED hold；
- queue companion physical charge与materialization account join；
- versioned reducer/registry-bound queue checkpoint、watermark fence、bounded reopen与offline doctor repair；
- terminal queue-content retirement与hold `CONSUMED -> RELEASED`；
- queue preview与reconnect；
- follow-up到RunStart原子消费；
- tool-boundary steer到provider-input generation原子消费；
- typed capacity/rejection/reconciliation。

验收：

- UI只在FULL后显示queued；
- client crash后accepted item可恢复；
- event chain与queue row/account mismatch fail closed且可由typed maintenance rebuild；
- generation-0 genesis存在且reducer/registry contract mismatch fail closed；
- soft watermark会触发shared checkpoint owner，普通admission无法越过hard reopen bound；
- checkpoint复用validated `runtime_projection_checkpoints` row，并与queue account pointer在专用CAS transaction中原子推进；writer lock内不等待checkpoint owner；
- production reopen只foldcheckpoint后的bounded queue delta，不扫描完整session ledger；
- queue FULL不可能引用未确认/未持有artifact，PREPARED hold与queue reference阻止并发GC；
- queue companion实际row count/table-op必须与plan一致，stored actual bytes必须不超过冻结的保守charge；超界整批rollback且account不回填actual值；
- queue retention结束后content reference与CONSUMED hold原子退休，不产生永久retention；
- edit与safe-point dispatch竞争只有一个CAS winner；
- explicit steer不在非法boundary静默降级；
- reservation precommit failure走typed release/reject，UNKNOWN不被误报为domain conflict；
- consumption后不存在queue row与run/provider input分叉。

### S6：Semantic transcript与production activation

目标：

- transcript/live cell分层；
- inline/block tool taxonomy；
- read/search聚合；
- terminal elapsed/output progress；
- compact/verbose；
- stable status line；
- MCP/subagent/process；
- responsive sidebar；
- bounded notifications；
- context/rollout display。
- macOS/Linux binary distribution与version compatibility；
- TTY默认入口切换至Bubble Tea；
- `pulsara host repl`保留为显式Frozen Legacy REPL，绝不自动fallback。

验收：

- 长trajectory默认行数显著少于raw event数；
- expand可定位exact source event/tool IDs；
- errors永不因折叠丢失；
- streaming不造成明显布局抖动；
- 80/120/160列均可用；
- optional MCP不阻塞；
- background notification不淹没输入；
- render/status callback不执行I/O；
- sidebar关闭后所有关键动作仍可完成。
- production launcher、gateway和client任一版本不兼容时typed fail closed；
- default切换后Legacy REPL仍不获得queue、secret或semantic UI新能力。

### Post-activation polish候选

候选：

- themes；
- mouse；
- localization；
- accessible color palette；
- custom status segment provider；
- artifact viewer；
- transcript export；
- desktop/web projection adapter。

## 20. 测试策略

### 20.1 Pure projection tests

使用complete stored receipt、canonical fold result与operational-frame fixtures验证：

- bootstrap high-water与H+1 fold；
- ring copy、ledger catch-up与catch-up buffer的sequence merge；
- accounted physical batch中的business/accounting events在tap entry中保持全局sequence连续，accounting event的audit purpose按policy为noop；
- duplicate idempotence、sequence gap和rebuild；
- viewport cursor、follow-tail、unseen count和bounded page eviction；
- unified root中canonical-spine/audit-anchor placement、persistent-tree/checkpoint restore与registry-bound cursor stale/rebase；
- stable placement key在audit插入、entry删除与interval replacement后保持未受影响suffix byte-identical；display rank只随root-local view变化；
- placement-key六kind字段矩阵、sentinel、integer bounds、fixed framing、historical registry binding与unsigned byte ordering；
- canonical placement transition proof覆盖append/single replace/interval replace/retire tombstone；
- checkpoint cut与并发tail race覆盖exact segment prefix、compatible longer source-prefix lineage、retained append/noop suffix和rewrite invalidation；
- resident transition三branch逐项验证before/after vector、ordered changes bounds/accumulator和rebase target/token；
- history growth quote/reservation settlement、无重复计数的soft rotation、terminalization maintenance reserve、quote/policy reconciliation与hard capacity exhausted；
- durable history与operational activity分离，RunStart/RunEnd lifecycle只产生`AuditCell(run_lifecycle)`；
- event sequence到 transcript cells；
- active/terminal转换；
- semantic grouping；
- error不可隐藏；
- accepted/suppressed model output；
- compaction boundary；
- subagent/MCP/terminal状态。

Architecture tests验证：

- projection package不importrenderer/concrete storage；
- renderer不调用`HostSession.summary()`或EventLog；
- UI observer不是`RuntimeEventPublisher`的awaited subscriber；
- `UiCommittedEventTap.offer_nowait()`路径不await、不执行I/O或renderer callback；
- tap/ring的committed element type只能是原子`CommittedPresentationTapEntry`；entry只持有完整raw envelope tuple、stored-batch identity与exact-joined canonical fold result，不得保存第二份`AgentEvent`或拆成独立lane；
- `RawStoredEventEnvelope.__module__`必须是`pulsara_agent.primitives.stored_event`；`event_log.protocol`只允许qualified module dependency，不得定义、alias、绑定为public module symbol或导出该类型；
- AST/import gate禁止任何production、test或tooling调用方从`pulsara_agent.event_log.protocol`导入`RawStoredEventEnvelope`；protocol实现自身必须使用`import pulsara_agent.primitives.stored_event as stored_event_primitives`一类qualified引用，不能创建旧路径symbol binding；
- `StoredEventBatchCommitReceipt.__module__`必须是`pulsara_agent.ports.stored_event`；EventLog、RuntimeSession和tests直接import最终owner，不设置旧路径alias；
- `JoinedRawStoredEventRangeProof.__module__`同样必须是`pulsara_agent.ports.stored_event`；EventLog、RuntimeSession、doctor、restore与repair直接import最终owner，不复制第二份proof DTO；
- `ports.event_write`和UI projection不得import `event_log.protocol`，EventLog以外禁止调用raw-envelope construction factory；
- RuntimeSession/tap禁止调用旧`RawStoredEventEnvelope.from_stored_event()`或等价serialization seam；canonical envelope只能由EventLog storage write path生成；
- production EventLog single/batch/conditional append返回类型必须是`StoredEventBatchCommitReceipt`，禁止仅返回`AgentEvent`的双入口；
- EventLog exact candidate confirmation、idempotent winner与stable-candidate restart repair必须返回`EventBatchConfirmationEvidence`；generic reopen/doctor/catch-up必须返回`JoinedRawStoredEventRangeProof`；禁止decoded-event-only evidence，也禁止range/page或partial candidate evidence伪造`StoredEventBatchCommitReceipt`；
- 只有中央FULL classifier可以构造`ConfirmedFullStoredBatch`；tap API类型只接受normal/FULL physical receipt，不接受candidate match evidence；
- `StoredEventBatchCommitReceipt`必须验证完整physical owned/raw 1:1；`EventWriteResult`必须验证business/accounting是receipt的完整disjoint partition；
- normal write必须从sealed encoder-built pair构造receipt，historical decoder调用次数为零；只有exact candidate FULL confirmation可从stored row构造同形receipt；generic restore构造range proof；
- `TranscriptProjectionStateStore`只暴露`apply_live_committed(receipt)`与`fold_restored_range(range_proof)`，二者调用同一grouping-independent core；旧tuple API及fake receipt factory为零；
- `register_committed_reducer`、initial catch-up、reconcile、doctor、restore与repair均按live/range入口路由，不能把任意event tuple交给ambiguous callback；
- presentation event policy必须分别冻结transcript与audit purpose；`RunStartEvent`/`RunEndEvent`双purpose回归通过，audit extractor无法构造三种canonical transcript cell；
- audit extractor policy携带ID/version/fingerprint，registry exact resolve历史binding并拒绝same ID/version fingerprint conflict；
- unified history root同时覆盖transcript reducer、event-domain registry、presentation policy registry与audit extractor registry fingerprints；不存在双feed root/page/client merge；
- canonical placement只来自transcript reducer anchor；replacement sequence不能重排leaf，audit sequence只能经typed anchor/gap proof插入；
- transcript reducer是placement transition proof与anchor tombstone唯一owner；presentation tree使用stable placement key，entry/node/root/cursor中没有continuous history ordinal；
- history checkpoint使用bounded path-copy tree而非flat manifest；materialization policy与FULL/NONE/UNKNOWN/CONFLICT confirmation matrix是单一真源；
- active tail保存逐sequence segment tuple；noop range不能只存在于不可逆aggregate；root/checkpoint保存可重放presentation-source prefix lineage；
- placement-key contract ID/version/fingerprint、fixed framing与historical registry binding是tree/root/cursor/Protocol的共同gate；
- root advanced frame、latest cursor pair、old pinned-root relation、consumed segment prefix、retained segment suffix与closed resident transition原子交付；`AuthorityAdvanceFrame`不能改变confirmed root；
- session history growth quote/reservation、soft fence、terminalization maintenance reserve与hard exhaustion只能由Foundation service决定；Gateway/Legacy/Go不能绕过或重算；
- `DurableHistoryCell`与`OperationalActivityCell`是不相交closed union，`TerminalSemanticCell`和`RunLifecycleCell`物理不存在；
- history cursor/request不含feed kind，cursor不含direction，protocol request是唯一direction owner并调用单一`read_page()`；
- ordinary UI DTO不能持有sealed MCP secret carrier；
- queue mutation只能由typed event + transaction companion更新row/account，禁止repository-only production transition；
- production queue-artifact delete只能经retention guard，caller不得直接取得identity delete capability；
- queue companion必须携带`PromptQueueCompanionChargeFact`；sequence分配前冻结保守charge，stored rebind后、任何auxiliary SQL mutation前验证actual rows/table-op及`actual_bytes <= conservative_charge`；
- queue checkpoint只能作为typed state payload写入现有`runtime_projection_checkpoints`，并由专用transaction port与queue account pointer原子CAS；禁止新增shadow checkpoint store或在writer lock内await checkpoint attempt；
- checkpoint owner必须消费`CHECKPOINT_COMMIT` physical reservation与`CHECKPOINT_MAINTENANCE` lane，不得与普通queue admission竞争到自锁；
- queue result companion禁止更新background publication状态来伪造commit outcome。

### 20.2 Snapshot tests

固定：

- 80列；
- 120列；
- 160列；
- reduced motion；
- no color；
- CJK；
- long path；
- long command；
- large paste；
- transcript scrolled-up/unseen/follow-tail；
- bounded alternate-screen exit summary；
- multiple queued items；
- approval diff；
- plan question；
- MCP form的redacted request view；
- MCP URL view不落真实URL golden；
- opening/retry/failure。

### 20.3 Concurrency tests

覆盖：

- submit while model streaming；
- steer 与 tool terminal竞争；
- stop 与 approval竞争；
- queue edit 与 safe-point dispatch竞争；
- observer detach；
- slow renderer；
- observer queue overflow后run与Host close继续；
- tap ring在snapshot/read与并发commit间evict；
- catch-up buffer在ledger分页期间overflow并触发重新bootstrap；
- viewport scrolled-up时terminal/live event burst；
- renderer尝试修改decoded projection input不会改变publisher持有的`AgentEvent`或ring raw bytes；
- terminal progress flood；
- MCP ready during active run；
- child completion burst；
- close while interaction visible；
- startup open task与quit竞争；
- MCP submit与interaction replacement竞争；
- queue reservation与preflight/safe-point失效竞争；
- artifact preparation FULL与acceptance NONE/UNKNOWN竞争；
- PREPARED hold与GC/doctor identity delete竞争；
- queue acceptance consume hold与expiry sweeper竞争。
- checkpoint maintenance CAS与queue admission burst竞争；hard watermark只能由checkpoint winner解除；
- queue content retirement与GC/queue reconnect竞争；
- presentation checkpoint artifact/CAS进行时live append/noop继续进入active tail，FULL swap只消费proved segment prefix并保留segment suffix；noop-only suffix必须保留source lineage；
- presentation checkpoint cut后出现replacement/retirement时candidate typed invalidation/rebuild，不按append suffix安装；
- history soft rotation fence与已准入RunEnd terminalization竞争；ordinary growth reservation与terminalization maintenance reserve分别结算，reserve只允许terminal收口；
- 同一owner epoch连续签发多个MCP borrow时先前borrow保持有效；owner epoch推进后shared cell同时撤销该epoch全部borrow的后续reveal。

### 20.4 Durable/reconnect tests

- queue acknowledgement后client crash；
- reconnect恢复queue；
- snapshot H与subscription安装之间并发commit；
- ring完整覆盖、部分覆盖、完全不覆盖`(H, R]`三种bootstrap；
- committed notification loss后的ledger catch-up；
- operational generation丢失后的live-state unknown/rebuild；
- steer reservation与tool terminal/RunEnd竞争；
- follow-up consumption与RunStart commit UNKNOWN；
- pending approval恢复；
- active run terminal后UI漏收notification；
- publication failure后从ledger重建；
- normal commit直接返回physical receipt；UNKNOWN/idempotent/stable-candidate repair先返回逐candidate raw evidence，只有FULL classifier产出同形receipt；generic restart/doctor按连续range proof分页fold；partial/non-contiguous candidate evidence与range proof绝不进入tap；
- checkpoint H后按不同range page partition恢复均得到相同canonical state，并在下一条完整live entry边界无重复切回tap；
- presentation checkpoint candidate H冻结后，crash/reopen从FULL receipt、candidate segment cut、durable source-prefix transition proof与current tail tuple恢复同一个installed root + retained suffix active head；
- tap entry ingestion覆盖exact next、完整duplicate、partial overlap、gap与same-sequence/same-batch fingerprint conflict；除exact next与完整duplicate外均不得拆分entry，必须detach并进入bounded catch-up；
- event chain与queue row/account head不一致时fail closed；
- generation-0 queue checkpoint在新session与hard-cut activation session上均唯一且可重算；
- reducer/registry contract fingerprint drift拒绝加载旧checkpoint；
- soft watermark触发checkpoint owner，admission burst永不把tail推进超过hard reopen bound；
- hard fence释放writer lock后等待shared checkpoint attempt，随后重新取得writer lock并从头重验admission；
- mutable checkpoint row与queue account pointer在同一transaction中CAS；UNKNOWN覆盖FULL、NONE、compatible supersession与reconciliation；
- checkpoint owner暂时失败时queue admission被typed阻塞，但已有run和下次bounded reopen仍可继续；
- queue checkpoint + bounded typed delta重建production projection；
- delta超界时production reopen fail closed且不回退完整session scan；
- offline doctor只分页foldqueue transition chain并重建checkpoint/projection；
- confirmed artifact receipt缺失/冲突时queue acceptance拒绝；
- process crash发生在artifact+PREPARED hold FULL、queue acceptance前时，hold在reopen后仍阻止删除；
- acceptance FULL时hold、queue reference与event/account/head同时证明CONSUMED；
- acceptance NONE保持PREPARED；UNKNOWN exact-confirm覆盖FULL/NONE/CONFLICT/UNAVAILABLE；
- hold在stable candidate后过期时不得原地续期；NONE typed-expire，UNKNOWN仍可确认既有CONSUMED winner；
- queue retention retirement原子完成content reference移除与hold `CONSUMED -> RELEASED`；失败/UNKNOWN不会留下无法证明的半退休状态；
- `ON DELETE RESTRICT`只绑定artifact ID，digest/media type/semantic identity在artifact row lock内exact校验；
- acceptance/reservation/commit/release/retirement companion的actual row/table-op drift或stored bytes超过固定保守charge时整批rollback；materialization account始终按保守值结算；
- release/expiry后只有在无PREPARED hold且无queue reference时GC才能删除artifact；
- physical UNKNOWN分别确认成FULL、NONE、CONFLICT、UNAVAILABLE；
- publication unavailable与tap detach不改写已经FULL的queue disposition。

Secret tests额外验证：

- MCP response不进入PromptSession history；
- exact private URL不进入event-safe request view或普通widget state；
- UI detach释放borrow但不删除encrypted continuation；新client只能由Host从exact carrier签发新owner-epoch borrow；
- 旧client borrow handle在reconnect后不可重新激活或再次reveal；已返回/显示的plaintext不作可撤销承诺；
- 同一active owner epoch签发两个不同purpose borrow时二者均可继续reveal；只release其中一个不会撤销另一个，推进owner epoch才会同时撤销旧epoch全部borrow；
- response draft不能普通serialize/pickle/asdict；
- snapshot、copy-all、diagnostic与exception均为constant redacted；
- dedicated secret buffer没有history/autosuggest/completion/ordinary undo residue；
- release后mutable buffer执行best-effort overwrite，应用主动清除document/undo/render cache；不测试Python/terminal物理零化；
- owner close/replacement后旧request-secret、URL display与response borrow handle的后续access全部fail closed；测试不得声称已经复制出的Python `str`被撤销。

### 20.5 Real dogfood

最后运行：

- 长 terminal command；
- 多轮 read/search/edit；
- plan revise/approve；
- MCP startup中途ready；
- subagent并发；
- queue steer；
- stop/close；
- compaction；
- reconnect。

Real LLM只作为最终行为验证，不替代 deterministic projection tests。

## 21. UX 指标

建议跟踪：

| 指标 | 目的 |
|---|---|
| time-to-shell | 启动后多久可输入 |
| time-to-first-visible-activity | Enter 后多久看到有意义状态 |
| queue-ack latency | 用户输入多久获得 authoritative queue状态 |
| stop-request latency | 按 Esc 到显示 Stopping |
| stop-terminal latency | stop 到 durable terminal |
| no-progress-visible duration | 最长多久只有 spinner而无语义变化 |
| compact/raw line ratio | 信息压缩效果 |
| approval completion time | approval可理解性 |
| accidental scope expansion | UI噪音是否诱导无关修改 |
| dropped UI notifications | observer质量，不能影响runtime |
| layout shifts per minute | streaming稳定性 |

不把动画 FPS 当作核心产品指标。

## 22. 已冻结决策与仍待标定项

### 22.1 已冻结

1. Queue acceptance立即durable；FULL acknowledgement之前UI不显示queued。
2. EventLog queue transition chain是唯一semantic/audit authority；queue row/account只是exact-head CAS projection，HostSession只借用item执行boundary。
3. V1 steer只允许`after_tool_results_before_followup_model_input_freeze`。
4. approval/plan/MCP waiting与suspended期间只允许排follow-up。
5. explicit steer在非法或已经错过的boundary typed reject，不静默改成next。
6. S0-S6通过后，TTY一等入口切换为Bubble Tea；`pulsara host repl`只作为显式Frozen Legacy REPL保留，Bubble Tea失败时禁止自动fallback。
7. EventLog一次生成完整physical `StoredEventBatchCommitReceipt`；normal commit由encoder-built pair直接构造且不重复decode，只有exact candidate FULL confirmation可构造同形receipt；generic restart/doctor/catch-up只使用连续range proof。UI tap只消费将live receipt完整raw tuple与live fold result exact join后的原子entry。
8. transcript terminal cell保存exact durable source references/fingerprint accumulator。
9. raw reasoning/private chain-of-thought不显示；只有明确event-safe的public reasoning summary可以作为默认折叠cell。
10. MCP encrypted continuation store是durable secret authority，Python Host owner是唯一decrypt/hydration authority；Go controller只能取得attachment-bound ephemeral secret lease，revoke后旧attachment不得再次reveal；不承诺撤销已跨IPC或显示的plaintext。
11. status/render path只读O(1) immutable snapshot，不执行I/O。
12. V1 status/sidebar不开放任意插件provider；扩展推迟到post-activation并要求typed、bounded、secret-safe、可取消。
13. non-TTY本轮不新增机器协议；已有输出保持兼容，未来JSONL contract单独设计。
14. Full-screen transcript从S1起使用bounded resident viewport与paged durable history；向上滚动时不自动跳尾，退出只打印bounded summary。
15. Large-paste artifact必须先幂等持久化并与durable PREPARED hold原子确认；queue acceptance同事务消费hold，retirement同事务执行`CONSUMED -> RELEASED`。
16. 物理commit、exact confirmation、publication delivery、UI observation和queue domain disposition使用不同closed vocabulary；physical commit只有`FULL | NONE | UNKNOWN`。
17. Reservation失败必须通过typed release、delivery rejection或reconciliation收口；explicit steer错过boundary绝不静默降级为next。
18. Queue checkpoint绑定registered reducer/event-registry contract并拥有canonical generation-0 genesis；V1复用mutable validated `runtime_projection_checkpoints`并与account pointer同事务CAS，soft watermark触发保留容量maintenance，普通admission不得越过hard reopen bound或持锁等待owner。
19. reconnect使用非等待式`UiCommittedEventTap`线性化`bootstrap as-of H -> subscribe H+1`；ring以不可拆分的raw+fold复合entry为最小单元；`authority_high_water`表示Python解释到的durable sequence，`projection_revision`表示client-visible projection stream版本，operational inventory另用generation/cursor。
20. Production queue reopen只使用trusted domain checkpoint + bounded typed delta；完整queue-chain fold只允许privileged offline doctor执行，普通Host/UI路径绝不扫描完整session ledger。
21. Artifact hold和queue reference只对`artifacts.id`建立`ON DELETE RESTRICT`外键；其余artifact identity在row lock内exact验证。
22. 每个queue transaction companion都携带pre-commit normalized-plan-derived固定保守`PromptQueueCompanionChargeFact`并加入RuntimeSession materialization accounting；stored actual bytes超过保守值或row/table-op drift时整批rollback，account不在sequence分配后回填actual charge。
23. 所有mutation command携带stable command identity与expected target/generation；断线后查询同一command ID的既有winner，不重复physical mutation。
24. V1允许多个read-only observer，但同一runtime session同时只有一个interactive controller；takeover显式且可审计，secret frame只接受当前controller attachment。
25. client buffer溢出产生`GAP`并走snapshot rebuild，不能让Runtime writer、publisher或run owner等待UI。
26. `prompt_toolkit`不进入full-screen修改面；Legacy REPL不新增secret reader、durable queue、steer或semantic transcript。
27. Durable terminal history只有一种`PresentationHistoryProjectionRootFact`和一个page port；每个root一对directionless cursor，每个attachment一个latest pair及bounded pinned old-root cursors；root只拥有transcript leaf/durable audit cell的全局placement/order。
28. Root显式绑定transcript reducer、event-domain registry、presentation policy registry与audit extractor registry contract fingerprints；history cursor/request不含feed kind，client不做cross-feed merge。
29. `DurableHistoryCell`与`OperationalActivityCell`不相交；run lifecycle只使用`AuditCell(run_lifecycle)`，operational activity不进入history root/checkpoint/page或projection revision。
30. Canonical transcript是unified history的不可重排stable-coordinate spine；replacement继承原anchor/区间，retirement保留不可渲染tombstone，audit只能使用proved `before_leaf | after_leaf | ledger_sequence` anchor。
31. Presentation history使用registered fixed-framing stable placement-key indexed bounded immutable persistent tree和path-copy update；flat all-page manifest与durable continuous history ordinal被禁止，page read为`O(tree height + page size)`，display rank只按subtree count临时派生。
32. Checkpoint stable candidate与physical attempt guard分离；FULL/NONE/UNKNOWN/CONFLICT、compatible winner与reopen reconciliation均有唯一出口。
33. New root只通过推进projection revision的root-advanced frame安装latest cursor pair；frame exact证明consumed segment prefix并保留checkpoint I/O期间新增segment suffix，noop-only suffix仍推进source lineage，resulting head可有non-empty tail；old-root cursor仅作为retained pinned history继续有效。
34. Root resident transition是`unchanged | bounded ordered changes | rebase required`完整closed DTO，不允许标签-only或自由payload映射。
35. V1以typed growth quote/reservation和session rotation处理tree soft capacity；ordinary projected count不重复包含terminalization maintenance reserve，已准入run使用隔离reserve收口；hard exhaustion要求新建session或privileged repair，不做epoch/super-root、silent truncation或本地eviction。

### 22.2 实施前通过benchmark/UX test标定

- large-paste threshold；
- terminal live output默认行/byte cap；
- operational frame coalesce频率；
- presentation history tree fanout/height、ordinary growth quote、session rotation threshold与terminalization maintenance reserve entries；
- sidebar启用宽度和宽度范围；
- compact group minimum display time；
- animation cadence和reduced-motion细节；
- 具体keybinding，但Esc/Ctrl-C路由层级不可改变；
- queue item/byte/token容量与retention数值。

## 23. 最终推荐

Pulsara 的 UI 优化不应从“做一个更漂亮的 prompt”开始，而应从下列 hard boundary 开始：

```text
single canonical raw envelope + non-awaiting committed tap + cursor recovery
  -> renderer-independent typed projection + unified durable history root/checkpoint
  -> bounded viewport + independent operational activity
  -> versioned local protocol + controller/command/secret boundaries
  -> Bubble Tea full-screen shell/composer/stop/scrollback
  -> typed interactions + sealed input
  -> event-authoritative durable follow-up/steer queue + artifact holds
  -> semantic transcript
  -> status/sidebar
  -> visual polish
```

最终产品气质应是：

- 默认安静；
- 状态明确；
- 长任务不神秘；
- 用户随时可以继续表达意图；
- approval不需要猜；
- 详细事实随时可查；
- UI断开不影响run；
- durable runtime始终是唯一真源。

用竞品作一句话归纳：

> 用 Codex 的状态机保证“不会乱”，用 Claude Code 的信息层级保证“不会吵”，再用 MiMo-Code 的产品化入口保证“找得到”。
