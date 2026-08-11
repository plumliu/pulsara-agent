# Pulsara hard-cut 后产品能力缺失索引

> 状态：WORKING GAP INDEX（产品能力事实索引，不是恢复设计；PHC-02 已于 2026-08-11 通过 Round 1 恢复）
>
> 调研日期：2026-08-10
>
> hard-cut 前代码基线：`5b7ad9f7`
>
> 当前代码基线：`12636e34`
>
> 范围：Python Agent Runtime / Host及其直接产品能力；Go TUI暂不在当前恢复主线中，但被确定为后续唯一主要交互与观察产品面
>
> 明确排除：memory 子系统重设计、Oxigraph/SPARQL、旧 EventLog execution replay、coroutine/provider transport recovery、exact context-input audit、跨 Host terminal/subagent execution 恢复、Legacy Python REPL兼容恢复、standalone Canonical Inspector产品

## 1. 文档目的

这份文档只回答一个问题：

> durability subtraction hard-cut 之后，哪些 hard-cut 前已经存在的关键产品能力，在当前减法内核中已经消失、不可达或明显退化？

本文不回答：

- 应新增哪些 `CommittedAgentEvent` 或 `LiveAgentEvent`；
- 应新增哪些表、port、job、hook 或 state machine；
- 每项能力应如何分阶段恢复；
- 是否应原样搬回旧实现；
- 哪些旧 durability machinery 应复活。

“旧代码很多”不等于“旧产品能力都应恢复”。本索引刻意把以下三件事分开：

1. **产品语义**：用户或模型实际能做什么、看到什么；
2. **历史实现方式**：该能力当时是否依赖 EventLog、projection、receipt 或 checkpoint；
3. **当前事实**：新 Kernel 是否仍有一条真正可达的 production path。

只要产品语义已经消失，就记为缺口；旧实现中的 durability/recovery machinery 不因此自动成为保留对象。

### 1.1 产品能力恢复所依赖的架构核心

本索引登记产品缺口，不表示应回退到 `5b7ad9f7` 的 universal EventLog 架构。当前 hard-cut 的真正成果也不是把 151 类 event 压缩成更小的固定数字，而是改变了 Runtime 中的**真值归属、effect 顺序与恢复承诺**。后续恢复本文任何 PHC 能力，都必须继续建立在以下正式架构上：

> **Canonical relational conversation kernel with selective domain, effect, and work journals**

其物理边界分为六个单向平面：

| 平面 | 拥有的真值 | 不得承担的职责 |
|---|---|---|
| canonical relational rows | conversation、ordered semantic blocks、tool、job、coordination等“现在是什么”的semantic truth与数据库约束 | 不靠event replay证明自身存在 |
| selective committed `agent_events` | 某项用户可观察transition在`event_sequence=N`被接受的occurrence/audit truth | 不恢复coroutine、provider transport、foreground execution或canonical row |
| tool/job physical attempt journals | physical dispatch/claim、remote identity、result缺失时的ambiguity与immutable attempt lineage | 不承诺通用exactly-once，不以lease过期自动重做非幂等effect |
| shared content-addressed blobs | 被canonical subject引用的完整immutable大内容与integrity metadata | 不为每个产品族再造artifact receipt/hold/repair graph |
| process-local typed live stream | 当前Host generation中的provider/tool-result增量、interaction、terminal与subagent进度 | 不进入durable serializer，不承诺跨Host replay或continuation |
| disposable derived planes | presentation、index、telemetry与可重建projection | 失败不得反向否定canonical commit或阻塞foreground correctness |

这里的核心依赖方向不可反转：canonical row回答“现在是什么”，committed occurrence回答“何时接受了什么”，live plane回答“当前进程正在发生什么”。Go TUI、eval、hook及未来diagnostic consumer可以观察这些真值，但不能因自身投递、projection或callback失败而成为它们的成立条件。

#### 永久保留的Runtime不变量

1. canonical row及其对应committed occurrence由同一domain owner在同一PostgreSQL transaction内写入；event不能用来证明row已经真实。
2. completed assistant message及全部ordered semantic blocks在provider completion后原子commit；包含多个tool call的message不能按call拆开提交。
3. complete assistant tool-request message commit成功前，任何physical tool call都不可达；每次真实invoke前先commit exact attempt。`call无attempt`表示未dispatch，`attempt无result`表示outcome unknown，不能静默自动重试。
4. provider input使用exact canonical cut；late result不能倒插成旧assistant实际看过的context，只能在未来cut中作为真实late observation出现。
5. Host crash/takeover使未完成turn、subagent及Host-owned terminal work按其canonical规则interrupted；reopen只rehydrate canonical rows，不恢复旧coroutine、provider cursor或历史Start/End。
6. `LiveAgentEvent`保持typed、bounded和process-local；slow observer只GAP/detach，不能阻塞provider、tool owner或canonical commit。
7. ordinary hook只有registration cut后的best-effort delivery，无restart catch-up或generic receipt graph；真正要求跨进程必达的产品工作必须升级为具名durable job。
8. pre-dispatch授权不是普通观察hook，而是显式typed policy port；普通extension、TUI、diagnostic consumer与plugin都没有`CommittedAgentEvent` append authority。
9. 当前正式append authority封闭为`HostWriterGuard | JobAttemptClaimGuard`。新增产品事件通常必须归入其中之一；若产品确实需要第三个writer domain，那是需要独立ADR、fencing与SQL lock-order证明的架构变更，而不是随event一起顺手增加。
10. 产品语义可以增长，execution recovery machinery不能借产品恢复换名回归。

#### `26 / 23 / 13 / 2`的正确地位

当前代码中的26类Committed、23类Live、13个subject slot与2类append guard，是Stage 2 hard-cut时用于证明旧151类universal grammar已经退出production composition的**closed activation oracle**。它们不是Pulsara永久的产品能力上限，也不是评价架构好坏的数字目标。

- 两类append guard表达当前真正存在的writer authority domain，具有长期架构意义；
- Committed/Live event数量会随着新的独立产品语义受控增长；
- canonical subject种类增长时，可以增加带数据库FK/CHECK约束的typed subject slot；
- 每次增量都必须同步更新closed vocabulary、producer、transaction、subject/guard、schema/version、sensitivity/redaction、observation projection与fixtures；
- 不能用`CustomAgentEvent(kind, payload)`、自由字符串subject或把多个无关transition塞进巨型JSON来绕过审查；
- 也不能为了维持26/23/13的旧数字，把artifact、plan、MCP、terminal monitor等真实用户语义重新压成untyped callback、raw row推断或静态字符串。

一个新增event只有在它表示独立、用户可观察且已经被canonical owner接受的transition，或表示当前进程中值得extension/UI消费的typed lifecycle时才成立。reservation、candidate、receipt、projection-ready、checkpoint、reducer repair、delivery ACK等仅用于证明另一份状态的machinery/proof event，仍然禁止回归。

#### 与成熟Agent产品对照后冻结的判断

Codex、Claude Code和Grok Build都同时拥有typed lifecycle、completed conversation history与process-local streaming/hook机制；它们证明了“typed event很多”不等于“必须用event replay恢复execution”。当前Pulsara在此基础上进一步冻结same-transaction canonical occurrence、Host/job fencing以及attempt-before-effect，因此其核心优势是**更清楚的authority与side-effect ambiguity边界**，而不是event枚举更少。

成熟产品也说明另一面：event vocabulary必须能够随真实产品能力演进。本文后续登记的artifact、terminal monitor、plan、MCP与subagent能力若需要新的canonical subject或typed event，这本身不是架构回退；只有当新类型重新承担execution recovery、consumer proof或derived delivery authority时，才违反hard-cut核心。用户观察面最终由Go TUI承接，不为standalone Inspector另造一套durable truth或projection体系。

## 2. 证据口径与范围

### 2.1 证据等级

| 标记 | 含义 |
|---|---|
| `[前后代码确认]` | hard-cut 前生产代码/测试与当前生产代码直接对照确认 |
| `[归档验收确认]` | 归档实施文档明确标记完成，并保存测试或阶段验收记录 |
| `[当前不可达]` | 类型、descriptor、schema 或 helper 仍在，但 production composition 没有调用/绑定 |
| `[显著退化]` | 能力名称仍在，但用户可观察语义较 hard-cut 前收缩 |
| `[已知延后]` | 已确认缺失，但不进入当前 Python Runtime 修复主线 |
| `[并入Go TUI]` | 不恢复原Python产品面；其仍有价值的用户语义由未来Go TUI承接 |
| `[明确退役]` | 历史产品面不再构成兼容或恢复义务 |
| `[不计缺口]` | hard-cut 有意删除，或归档中只有调研/计划而没有已实现产品证据 |

### 2.2 代码对照点

- hard-cut 前：`5b7ad9f7`，即 Stage 2/3–5 开始删除生产 owner 之前的代码真值；
- 当前：`12636e34`；
- 归档标题扫描：[`archived_docs/`](archived_docs/) 下共 **116** 份 Markdown 文档；
- 根目录仍活跃的产品/架构材料也用于确认原有产品承诺，尤其是：
  - [`PULSARA_LEGACY_REPL_RETENTION_CONTRACT.zh.md`](PULSARA_LEGACY_REPL_RETENTION_CONTRACT.zh.md)；
  - [`PULSARA_TERMINAL_UI_UX_RESEARCH_AND_DESIGN.zh.md`](PULSARA_TERMINAL_UI_UX_RESEARCH_AND_DESIGN.zh.md)；
  - [`PULSARA_BUBBLE_TEA_CLIENT_IMPLEMENTATION.zh.md`](PULSARA_BUBBLE_TEA_CLIENT_IMPLEMENTATION.zh.md)；
  - [`PULSARA_HIERARCHICAL_AGENT_RUNTIME_ORCHESTRATION_DESIGN.zh.md`](PULSARA_HIERARCHICAL_AGENT_RUNTIME_ORCHESTRATION_DESIGN.zh.md)；
  - [`PULSARA_MCP_CATALOG_AND_LIST_FALLBACK_DESIGN.zh.md`](PULSARA_MCP_CATALOG_AND_LIST_FALLBACK_DESIGN.zh.md)。

### 2.3 “存在文件”不等于“能力被保留”

当前仓库仍存在若干旧 descriptor、enum、DTO 或 helper。只有同时满足以下条件，本文才把能力视为保留：

- production composition 会实例化它；
- 模型、Host、CLI 或正式协议有入口能调用它；
- 调用能到达真实 owner，而不是只返回静态说明或 fail closed；
- 结果能回到当前 canonical conversation path。

因此，以下情况仍属于缺口：

- `artifact_read` 类和 descriptor 还在，但模型工具面没有绑定；
- `enter_plan` descriptor 还在，但当前 runner 不会暴露或执行；
- schema 中有 `context_snapshots`，但没有生产触发者采用 snapshot；
- Live vocabulary 中有 `TerminalMonitorOpened/Observation/Closed`，但没有真正的 monitor registration 与未来通知 owner。

### 2.4 hard-cut前代码的读取约定

本文所有“hard-cut前参考代码”都固定指向同一个Git tree：

```text
5b7ad9f7ffc8565bc572180b2bde0c81ab64473a
2026-08-08T18:36:16+08:00
docs: finalize durability subtraction implementation plan
```

这些旧文件大多已被物理删除，因此下文故意使用`<commit>:<path>`文字引用，而不伪装成当前工作树中的可点击链接。后续coding agent应使用只读命令查看：

```bash
PRE_HARD_CUT=5b7ad9f7ffc8565bc572180b2bde0c81ab64473a
git show "$PRE_HARD_CUT:src/pulsara_agent/ports/terminal.py"
git grep -n 'TerminalMonitorTool' "$PRE_HARD_CUT" -- src tests
```

每组参考面遵守以下规则：

- 旧production owner用于找回已经验证过的产品语义、输入约束、输出形状和happy-path顺序；
- 旧tests用于提炼新的canonical Kernel回归，不要求旧fixture原样复活；
- 旧EventLog、RuntimeSession、reducer、checkpoint、receipt、repair、projection delivery和execution replay代码即使与产品代码同文件，也只能作为“如何分离”的证据，不能直接移植；
- 新实现必须重新落到canonical rows、selective committed occurrence、physical attempt journal、shared blob及process-local LiveAgentEvent边界；
- 本文不提供Standalone Canonical Inspector或Legacy Python REPL的旧代码入口，它们已分别并入Go TUI或明确退役。

## 3. Executive gap index

| ID | 产品能力族 | 当前判断 | 主要用户影响 |
|---|---|---|---|
| PHC-01 | Terminal 三工具完整边界 | **缺失**：`terminal_monitor` 整个工具与 future-notification owner 消失 | 长任务只能轮询/等待，无法注册完成通知或自动唤醒 Agent |
| PHC-02 | 完整 tool output artifact 与 `artifact_read` | **已恢复（Round 1）**：完整 sanitized candidate 先于 preview 保留；大输出可通过 scoped `artifact_read` 分页读取 | 中等输出完整展示并给出 artifact reference；大输出显示 UTF-8-safe head/tail 并可按需读取省略段 |
| PHC-03 | Terminal 真正实时 stdout/stderr streaming | **缺失**：当前 ToolResult Delta 在物理调用返回后一次性产生 | 运行中的命令没有真实增量反馈，交互体验与可观察性退化 |
| PHC-04 | Terminal retained-output/cursor 语义 | **显著退化**：只保留进程内 8 MiB rolling tail 与重复 tail snapshot | 早期输出被丢弃，无法可靠取得“自上次以来的新输出” |
| PHC-05 | Terminal shell/profile/env 产品语义 | **显著退化**：登录 shell snapshot、default-deny env、fallback/diagnostic 消失 | Agent shell 与用户 shell 不一致，环境安全与工具可发现性下降 |
| PHC-06 | Terminal foreground cwd continuity | **缺失**：命令结束后的真实 cwd 不再回写 session | `cd` 类前台命令不能改变下一条 terminal 命令的工作目录 |
| PHC-07 | Long-horizon context window / compaction | **缺失**：新 schema 有 dormant snapshot primitives，但无产品触发路径 | 长任务碰到固定输入、call 数或总时限后失败，不能主动/自动压缩继续 |
| PHC-08 | MCP production capability | **缺失**：仅保留配置检测，启用任何 MCP server 会阻止 Kernel open | MCP server discovery、tool call、interaction 与 CLI 管理均不可用 |
| PHC-09 | Plan workflow | **缺失**：三个 workflow descriptor 残留，但无执行/交互入口 | Agent 无法进入只读规划、提问、提交计划并等待批准/修订 |
| PHC-10 | Hierarchical/batch subagent task graph | **显著退化**：只剩 flat spawn/list/wait/stop | 依赖任务、批量调度、child phase/result reporting 与 task-board 语义消失 |
| PHC-11 | Standalone Canonical Inspector 产品入口 | **并入Go TUI，不单独恢复**：历史Inspector已消失；canonical query/Protocol后端按TUI需要保留和补齐 | 不建设第二套Inspector UI、read model或durable projection；会话观察最终由Go TUI呈现 |
| PHC-12 | Frozen Legacy Python REPL 产品面 | **明确退役，不恢复兼容**：旧命令差异只作hard-cut审计记录 | approval、plan、MCP等仍有价值的产品语义归各自能力族，并最终通过Go TUI交互，不为旧命令表复建Runtime机制 |
| PHC-13 | 跨 turn 失败/中断提示 | **缺失**：turn 可标 interrupted，但下一轮 provider context 没有明确失败旁注 | “继续”时模型无法区分上一轮完整回答与空/半截失败输出 |
| PHC-14 | Model-visible tool observation timing/freshness | **缺失**：数据库时间仍可能存在，但不再进入 provider-visible typed observation | 模型无法判断旧工具结果何时观测、耗时多久、是否可能过期 |
| PHC-15 | Capability catalog 与真实 executor 一致性 | **不闭合**：28 个 descriptor 中 9 个没有 production executor/binding | 静态 catalog 会误报能力；代码残留掩盖真实产品缺失 |
| PHC-16 | Go TUI S1–S3 后续能力 | **未来主要产品面，当前已知延后** | 最终承接会话观察、交互与控制；composer/copy/paste/notice及各能力族UI另行实施 |

## 4. Terminal：hard-cut 前后三工具产品真值

Terminal 是本次索引中最需要单独冻结的能力族。hard-cut 前最终公开面不是一个泛化 terminal 工具，也不是两个工具，而是明确的三个工具：

```text
terminal
terminal_process
terminal_monitor
```

[`PULSARA_TERMINAL_PUBLIC_TOOL_API_SPLIT_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_TERMINAL_PUBLIC_TOOL_API_SPLIT_HARD_CUT_IMPLEMENTATION.zh.md) 标记 `TAPI0–TAPI2 已落地`；[`PULSARA_TERMINAL_PROCESS_MONITOR_AND_AGENT_WAKE_DESIGN.zh.md`](archived_docs/PULSARA_TERMINAL_PROCESS_MONITOR_AND_AGENT_WAKE_DESIGN.zh.md) 标记 `TM0–TM5 已完成`。hard-cut 前 `5b7ad9f7:src/pulsara_agent/ports/terminal.py` 也确实同时定义了三套 description、schema、port 与 result union。

### 4.1 三工具职责矩阵

| 工具 | hard-cut 前的正式产品职责 | 当前代码事实 | 判断 |
|---|---|---|---|
| `terminal` | 启动一条 shell command；在 `yield_time_ms` 内等待；完成则返回 terminal result，仍运行则返回 exact `process_id` | 仍正式暴露；基础启动、yield、Host-scoped process id、PIPE/PTY 均存在 | 基础能力保留，输出/cwd/env 子语义退化 |
| `terminal_process` | 对 exact process 做一次即时操作：`list/log/poll/wait/write/submit/close_stdin/kill`；不安排未来 wake | 八个 action 当前仍在 [`ports/terminal.py`](src/pulsara_agent/ports/terminal.py) 和 [`terminal_process/manager.py`](src/pulsara_agent/terminal_process/manager.py) | 基础 action 保留，完整输出与 cursor 语义退化 |
| `terminal_monitor` | `register/list/cancel` Host-owned monitor；按 output/quiet/heartbeat/completion/expiry 形成未来观察；可在后续安全点唤醒 Agent | 当前 direct tools 只有 `terminal` 与 `terminal_process`；无 public schema、port、manager、registration 或 production binding | **整个产品工具缺失** |

这三个工具的产品分工不能互相替代：

- `terminal_process.wait` 表示“本次 tool call 内最多等 30 秒”；
- `terminal_process.poll/log` 表示“现在读取一次”；
- `terminal_monitor.register` 表示“结束当前等待，未来有意义的进展或完成时再通知”。

当前只有前两种语义，因此长任务只能通过模型反复 poll/wait，或在本轮结束后无人继续跟进。

### 4.2 `terminal`：保留与缺失的精确边界

#### 已保留

- 在 workspace 内启动 shell command；
- `yield_time_ms` 到达后返回 `running`；
- 为仍运行的进程返回 Host-scoped exact `process_id`；
- PIPE/PTY 两种模式；
- status、exit code、timed out、process id、bounded output；
- Host close 时终止并 join 当前 Host 所有进程；
- 输出基础 ANSI 清理与常见 secret 文本 redaction；
- 当前 Host 内的进程数量与 finished TTL bound。

#### 已缺失或退化

1. **运行中真实输出增量**：hard-cut 前 `_StreamingTerminalJsonBuilder` 会在 command 尚未完成时产生 `ToolResultTextDeltaEvent`。当前 [`conversation_kernel/runner.py`](src/pulsara_agent/conversation_kernel/runner.py) 先等待 `tools.invoke()` 完成，随后才把整段结果作为一个 `ToolResultDelta` 发出。即使 event 名仍叫 Delta，它也不是物理 terminal stream。
2. **完整输出权威**：hard-cut 前 terminal result 同时提供 bounded preview 与完整 redacted output artifact。当前输出先在 manager 的 rolling buffer 中淘汰，再在 tool runtime 中截断，省略部分没有可读取 owner。
3. **真实 final cwd**：hard-cut 前前台 command 完成后会捕获 shell 最终 cwd，并更新 terminal session；当前只把 command 启动时的 `cwd` 重新赋给 `current_cwd`。
4. **用户 shell 环境近似**：hard-cut 前有 shell detection、受控 login-shell env snapshot、TTL/cache/timeout/fallback 与最近 `.venv/bin` overlay；当前只使用 `$SHELL -c`、父进程 env 的 suffix blocklist 和最近 `.venv/bin`。
5. **长任务下一步提示**：hard-cut 前工具 description 明确区分 wait/poll/log/monitor，并在 output artifact 存在时提示 `artifact_read`。当前 description 只能指向 `terminal_process`。

### 4.3 `terminal_process`：八个 action 没有丢，但产品完整性已下降

| Action | 当前状态 | 缺失/退化点 |
|---|---|---|
| `list` | 保留 | 只能列当前 Host 的 process，不含 monitor inventory；跨 Host 不恢复是既定边界，不算缺口 |
| `poll` | 保留 | 返回当前 bounded tail；没有 exact since-cursor |
| `log` | 保留 | 只读 8 MiB rolling buffer 的末尾，早期输出可能已丢失；没有完整 artifact continuation |
| `wait` | 保留 | 仍是单次、最长 30 秒的有限等待；等待后仍 running 时没有 monitor 后续路径 |
| `write` | 保留 | 无换行写入仍可用 |
| `submit` | 保留 | 带换行写入仍可用 |
| `close_stdin` | 保留 | EOF 语义仍可用 |
| `kill` | 保留 | 终止 process 仍可用；但“只取消通知而不杀进程”的 `terminal_monitor.cancel` 已不存在 |

这里最关键的事实是：`terminal_process` 没有整体消失，但它已经无法完成 hard-cut 前“三工具协作”中的长期观察分工。

### 4.4 `terminal_monitor`：当前缺失的完整产品语义

hard-cut 前已经落地、当前完全不可调用的语义包括：

- 对 exact `process_id` 执行 `register`；
- 返回并维护 exact `monitor_id`；
- `list` 当前 Host-owned monitors；
- `cancel` future notifications，但不杀 process；
- completion 始终被监控；
- 可选 output growth threshold；
- output growth 后的 quiet period；
- 可选 heartbeat interval；
- minimum progress delivery interval；
- bounded per-observation output；
- bounded monitor lifetime；
- progress、heartbeat、completion、expiry 四类未来观察；
- 对已经消费的 output cursor 去重，避免每次通知重复同一 tail；
- 在当前 run 结束后仍由同一 Host 持有 monitor；
- notification 到达时进入 Host ingress；
- 在 human input、stop、active run 等竞争条件下选择安全点；
- 条件满足后触发 same-Host autonomous continuation；
- process completion 与 monitor cancellation 是两种不同的用户动作。

当前 [`conversation_kernel/tool_runtime.py`](src/pulsara_agent/conversation_kernel/tool_runtime.py) 的 `_offer_terminal_live()` 会在一次 `terminal`/`terminal_process` 返回时同步发出名字类似 `TerminalMonitorOpened/Observation/Closed` 的 Live events，但它不等于上述产品能力：

- 没有模型可调用的 `terminal_monitor`；
- 没有 registration；
- 没有持有 future observation 的后台 owner；
- running response 之后不会自动观察新增输出或完成；
- 下一次 poll 使用新的 tool attempt/monitor identity，不是原 monitor 的后续 observation；
- 没有 autonomous continuation。

因此，“Live vocabulary 中仍有 TerminalMonitor 名称”不能作为产品保留证据。

### 4.5 Terminal 输出：retained tail、单次响应与 canonical result 三个边界

当前 Terminal 大输出依次受三个不同边界约束：

1. [`terminal_process/manager.py`](src/pulsara_agent/terminal_process/manager.py) 的 `_BoundedOutput` 只保留最近 8 MiB bytes，超出后从 head 删除；
2. `terminal` / `terminal_process` 的 public request 把单次返回限制为最多 32,000 chars，因此普通响应只携带 rolling buffer 的一个 bounded tail；
3. [`conversation_kernel/tool_runtime.py`](src/pulsara_agent/conversation_kernel/tool_runtime.py) 对所有 tool result 另有 4 MiB 总 hard cap；Terminal 通常先被 32,000-char response bound 限制，但这个 generic cap 仍是最终 carrier 上界。

随后 canonical inline/blob content 保存的是**该次 bounded tool response**，不是 manager 中曾经出现过的完整 output。虽然 canonical blob relation 支持更大的 entry content，它无法恢复在形成 tool response 之前已经淘汰或省略的 bytes。

hard-cut 前存在的下列产品语义因此一起消失：

- 完整 redacted stdout/stderr 被保留；
- bounded inline preview 与完整事实分离；
- huge output 使用稳定 head/tail preview；
- preview 明确声明 original size、omitted middle 与 read-more hint；
- `artifact_read` 按 offset/limit 继续读取完整输出；
- Inspector 能解释 preview 为什么被截断以及完整内容在哪里；
- terminal 与普通 tool output 使用一致的 artifact continuation 语义。

### 4.6 Terminal shell/profile/env 的产品退化

hard-cut 前 [`TERMINAL_SHELL_ENV_V1_IMPLEMENTATION_PLAN.zh.md`](archived_docs/TERMINAL_SHELL_ENV_V1_IMPLEMENTATION_PLAN.zh.md) 对应的生产代码包含 `runtime/terminal/env.py` 与 `runtime/terminal/shell.py`，并有实际测试。主要用户可观察能力是：

- detect user shell；
- 受控捕获 login/interactive shell 的安全 environment snapshot；
- snapshot timeout、大小上限、TTL cache 与失败 fallback；
- default-deny environment name allowlist；
- secret name/value defense-in-depth；
- sane PATH 与用户工具链 PATH；
- 每条命令按 effective cwd 查找最近 `.venv/bin`；
- shell/env 来源与 fallback diagnostic。

当前实现仍做“最近 `.venv/bin`”和部分 secret suffix stripping，但已经不具备完整等价语义：

- 不捕获用户 shell profile 的 PATH；
- 没有 snapshot/cache/timeout/fallback；
- env 从 default-deny allowlist 退回到“继承绝大多数父环境，只排除以 KEY/TOKEN/SECRET/PASSWORD 结尾的变量名”；
- 没有稳定的 env provenance/diagnostic；
- 非典型 secret 变量名可能继续传入 subprocess；
- 用户配置在 shell profile 中的 `nvm`、`pyenv`、`mise`、Homebrew、proxy helper 等可能不可见。

这既是可用性退化，也是 terminal 安全边界退化。

### 4.7 Terminal cwd continuity 的具体丢失

hard-cut 前的 foreground cwd doctrine 是：

```text
terminal("cd src && pwd")
  -> command 完成并捕获 final cwd
  -> terminal session.current_cwd = <workspace>/src
  -> 下一条 terminal command 默认从 <workspace>/src 启动
```

background/yielded command 不允许回写 session cwd，以避免并发 command 竞争。

当前 `TerminalSession.execute()` 在 foreground command 完成后执行的是 `current_cwd = cwd`，其中 `cwd` 是**启动前解析出的目录**，并非 shell command 的 final cwd。结果是：

- `cd subdir` 的输出可以显示正确目录；
- tool result 的 `cwd` 仍报告启动目录；
- 下一条 command 仍从旧目录启动；
- hard-cut 前的 terminal session cwd 连续性不再成立。

### 4.8 明确不计为 Terminal 缺口的旧 durability

以下内容不属于本索引要求恢复的产品能力：

- yielded terminal process 跨 Host rebind/adopt；
- Host crash 后恢复原 OS process；
- durable monitor receipt graph；
- EventLog replay 恢复 monitor state machine；
- checkpoint/account/head join；
- delivery ACK/reconciliation machinery；
- Host 退出后继续持有 terminal process。

目标产品边界仍可保持为：terminal process 与 monitor 都只活在当前 Host；Host close 时 process 关闭。缺失的是**同一 Host 生命周期内**已经存在过的完整 terminal 产品体验。

### 4.9 hard-cut前Terminal参考代码

以下路径覆盖PHC-01、PHC-03、PHC-04、PHC-05与PHC-06的决定性旧production path和回归。它们都以§2.4的commit为前缀。

#### PHC-01：三工具与same-Host monitor

- `5b7ad9f7:src/pulsara_agent/ports/terminal.py`：`TERMINAL_*_TOOL_DESCRIPTION`、三套strict input、`TerminalCommandPort`、`TerminalProcessPort`、monitor registration/cancellation DTO；这是三工具公开边界的首要参考。
- `5b7ad9f7:src/pulsara_agent/tools/builtins/terminal.py`、`5b7ad9f7:src/pulsara_agent/tools/builtins/terminal_process.py`、`5b7ad9f7:src/pulsara_agent/tools/builtins/terminal_monitor.py`：模型工具的参数lowering、typed rejection和结果payload。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/tool_port.py`：`RuntimeTerminalCommandPort`、`RuntimeTerminalProcessPort`、`RuntimeTerminalMonitorPort`如何把公开tool contract接到同一process owner。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/monitor.py`：重点参考`default_monitor_conditions()`、delivery/lifetime bounds、output/quiet/heartbeat/completion/expiry判定；`TerminalMonitorCoordinator`中的store/reducer/receipt部分不可照搬。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/notification.py`与`5b7ad9f7:src/pulsara_agent/runtime/terminal/ui_stream.py`：只参考safe-point notification选择、bounded observer/GAP和public observation内容；不得恢复account/head/checkpoint/reconciliation graph。
- 关键回归：`5b7ad9f7:tests/test_terminal_public_api_hard_cut.py`、`5b7ad9f7:tests/test_terminal_tool_ports.py`、`5b7ad9f7:tests/test_terminal_monitor_tm0.py`、`5b7ad9f7:tests/test_terminal_monitor_tm1_tm5.py`。新测试应保留三工具分工、cancel不kill、same-Host future wake、slow observer不阻塞等语义，并删除restart replay/receipt proof断言。

#### PHC-03：真实stdout/stderr streaming

- `5b7ad9f7:src/pulsara_agent/runtime/terminal/process.py`：`_reader_loop()`与`_emit_output_delta()`是物理pipe/PTY reader在process完成前产生增量的源头。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/tool_port.py`：`RuntimeTerminalCommandPort.execute(..., output_sink=...)`展示process owner到tool live sink的直接边界。
- `5b7ad9f7:src/pulsara_agent/tools/builtins/terminal.py`：`execute_streaming*()`、`_CallableOutputSink`与`_StreamingTerminalJsonBuilder`展示bounded live preview与最终结果如何保持一致。
- 关键回归：`5b7ad9f7:tests/test_tools.py::test_terminal_streams_tool_result_delta_before_command_finishes`、`5b7ad9f7:tests/test_tools.py::test_terminal_streamed_json_deltas_match_final_result`、`5b7ad9f7:tests/test_tools.py::test_terminal_streaming_large_output_uses_conservative_live_head_then_tail`，以及`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_yield_keeps_partial_output_and_does_not_kill`。
- 新实现只应把这些增量lower为process-local `ToolResultStart/Delta/End`；不得恢复`5b7ad9f7:src/pulsara_agent/llm/terminal_projection.py`的durable model-stream projection。

#### PHC-04：retained output与cursor

- `5b7ad9f7:src/pulsara_agent/ports/terminal.py`：`TerminalProcessLog`、output cursor及typed unavailable/gap形状。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/process.py`：`process_log()`、`snapshot_process_for_monitor_registration()`及reader与retained buffer的并发边界。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/output.py`：`ProcessedOutput`、`TerminalOutputJournalSegment`、streaming ANSI/secret sanitizer、`SanitizedOutputJournal`与`recover_terminal_output_delta()`可用于理解chunk boundary、cursor和gap语义；其中bounded spool writer、durable page authority和restart recovery不是目标。
- 关键回归：`5b7ad9f7:tests/test_terminal_monitor_tm0.py::test_tm0_chunk_ansi_and_secret_boundaries_match_one_shot`、`5b7ad9f7:tests/test_terminal_monitor_tm0.py::test_tm0_partial_line_is_observable_after_quiet_bound`、`5b7ad9f7:tests/test_terminal_monitor_tm0.py::test_tm0_retention_gap_and_typed_unavailable_recovery`、`5b7ad9f7:tests/test_terminal_monitor_tm0.py::test_tm0_one_hundred_thousand_segments_keep_memory_bound`，以及`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_yielded_large_output_log_keeps_full_output_text`。

#### PHC-05：shell/profile/env

- `5b7ad9f7:src/pulsara_agent/runtime/terminal/shell.py`：`TerminalShellConfig`与`detect_terminal_shell()`。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/env.py`：`TerminalEnvConfig`、`TerminalEnvSnapshot`、`TerminalEnvBuilder`、`capture_shell_env_snapshot()`、default-deny allowlist、TTL/cache/fallback和nearest-venv overlay。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/session.py`与`5b7ad9f7:src/pulsara_agent/runtime/terminal/manager.py`：effective cwd、shell和env builder在每次command上的组合点。
- 关键回归：`5b7ad9f7:tests/test_terminal_env.py`全文件，以及`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_sanitizes_pipe_child_environment`、`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_sanitizes_pty_child_environment`、`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_shell_snapshot_path_is_used_without_login_shell_default`、同文件nearest-venv系列测试。

#### PHC-06：foreground cwd continuity

- `5b7ad9f7:src/pulsara_agent/runtime/terminal/process.py`：`_wrap_command()`、`read_captured_cwd()`、per-process cwd file生命周期。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/session.py`：只在foreground completion后、且final cwd仍位于workspace时推进`current_cwd`的owner。
- `5b7ad9f7:src/pulsara_agent/runtime/terminal/manager.py`：session-scoped cwd的入口与process access边界。
- 关键回归：`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_persists_current_cwd_after_cd`、`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_does_not_update_cwd_when_command_ends_outside_workspace`、`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_yielded_process_does_not_update_session_cwd`、`5b7ad9f7:tests/test_terminal_runtime.py::test_terminal_runtime_cleans_per_process_cwd_file_after_readback`。

## 5. PHC-02：完整 tool output artifact 与 `artifact_read`（Round 1 已恢复）

这不是只属于 Terminal 的缺口。hard-cut 前，generic tool executor 也支持把完整 tool output 归档为 artifact，并让模型看到 bounded preview 与 read-more reference。

### 5.1 hard-cut 前已存在的产品语义

[`TOOL_RESULT_ARTIFACT_PR1_IMPLEMENTATION_PLAN.zh.md`](archived_docs/TOOL_RESULT_ARTIFACT_PR1_IMPLEMENTATION_PLAN.zh.md) 与 [`TOOL_RESULT_ADAPTIVE_PREVIEW_IMPLEMENTATION.zh.md`](archived_docs/TOOL_RESULT_ADAPTIVE_PREVIEW_IMPLEMENTATION.zh.md) 对应的代码/测试确认了：

- 完整输出是 artifact authority；
- inline/model context 只承载有限 preview；
- 中等输出尽量完整展示；
- 大输出使用 head/tail preview；
- preview 保留 tool-specific 小字段，例如 status、exit code、cwd、process id；
- artifact ref 包含 size、media type、role 与 continuation hint；
- `artifact_read` 支持 info/text slice 等 bounded read；
- 模型可以从 suggested offset 继续读取，而不是总从 0 开始；
- terminal streaming 与 non-streaming tool result 最终指向同一完整事实。

### 5.2 Round 1 开始前代码事实（`12636e34`）

- [`tools/builtins/artifact.py`](src/pulsara_agent/tools/builtins/artifact.py) 仍有 `ArtifactReadTool` 实现；
- [`capability/builtin_catalog.py`](src/pulsara_agent/capability/builtin_catalog.py) 仍有 `artifact_read` descriptor；
- [`message/blocks.py`](src/pulsara_agent/message/blocks.py) 仍残留 `read_more.tool = artifact_read` 形状；
- 但当前 `DirectKernelToolPort` 没有 artifact read port，也没有把 `artifact_read` 放进 production tool set；
- 当前 tool result 在 canonical content publication 前已经被 4 MiB hard cap 截断；
- 当前没有 tool-result-to-artifact publication path；
- 当前 canonical content blob 只保存截断后的 transcript block，不是完整原始 tool output。

因此在 Round 1 开始前，该能力是 `[当前不可达]`，不是“已有实现只缺 UI”。这一段作为 hard-cut 缺口的历史证据保留，不再描述当前 production 状态。

### 5.3 hard-cut 期间具体丢失的用户能力

- 用户或模型不能请求被 preview 省略的中间段；
- 大型测试日志、编译日志、网页内容、数据库输出和 CLI 输出不能完整保留；
- 失败发生在输出尾部之外时，后续诊断可能没有证据；
- 同一 tool result 的完整内容不再可由 Inspector 查询；
- preview 无法证明自己对应哪份完整内容；
- resume 后只能看到截断后的 canonical tool result；
- `artifact_read` 名称仍在 catalog，会造成“看似存在、实际不能调用”的误判。

### 5.4 hard-cut前artifact参考代码

- `5b7ad9f7:src/pulsara_agent/ports/artifact.py`：`ToolResultArtifactOptions`、`build_adaptive_preview()`、`ToolArtifactReadPort`、`ToolResultArtifactProcessingPort`及info/text-slice DTO；这是完整内容与bounded preview分离的主要产品契约。
- `5b7ad9f7:src/pulsara_agent/ports/tool_execution.py`：`ToolResultArtifactCandidate`与tool execution result携带artifact candidate的provider-neutral边界。
- `5b7ad9f7:src/pulsara_agent/runtime/tool_artifacts.py`：`ToolResultArtifactService`、`RuntimeToolArtifactReadPort`、stable artifact id、preview rewrite与source-ref reuse；新实现应把这里的独立artifact index/row lowering到当前shared content-addressed blob和canonical subject reference。
- `5b7ad9f7:src/pulsara_agent/tools/builtins/artifact.py`：`ArtifactReadTool`的`info`与bounded text slice行为、cross-session not-found语义和read bounds。
- `5b7ad9f7:src/pulsara_agent/runtime/tool_composition.py`与`5b7ad9f7:src/pulsara_agent/runtime/tool_executor.py`：artifact reader、processing policy与真实executor的production binding顺序。
- Terminal producer参考：`5b7ad9f7:src/pulsara_agent/tools/builtins/terminal.py::terminal_artifact_candidates`和`5b7ad9f7:src/pulsara_agent/tools/builtins/terminal_process.py`，用于确保streaming/non-streaming最终引用同一完整内容。
- 关键回归：`5b7ad9f7:tests/test_tools.py::test_tool_executor_archives_generic_large_output`、`5b7ad9f7:tests/test_tools.py::test_terminal_large_output_returns_preview_and_readable_artifact`及同文件`artifact_read` slice/cross-session测试；`5b7ad9f7:tests/test_tool_artifact_processing_policy.py`；`5b7ad9f7:tests/test_artifact_store_contract.py`。
- 禁止照搬：独立artifact receipt/hold/finalization owner、EventLog artifact relation proof或因artifact publication失败而否定已经成立的canonical tool result。新事务契约必须重新冻结“完整结果何时成为canonical accepted content”。

### 5.5 Round 1 当前 production 真值

Round 1 已在新 conversation kernel 内恢复产品能力，没有恢复旧 durability machinery：

- physical tool 返回后先冻结完整 process-local sanitized candidate，然后才生成 lossy preview；
- 小输出以 inline `COMPLETE / NOT_REQUIRED` 直接保留；超过 8,000 UTF-8 bytes 且不超过 16 MiB 的正文使用 shared content-addressed blob；
- 中等输出保持完整展示并附 artifact reference；大输出使用 UTF-8-safe head/omission/tail，最终 inline preview 含 envelope、marker 和 warning 仍不超过 65,536 bytes；
- `tool_results` 直接拥有 nullable artifact edge、source coverage、artifact disposition、display kind 与两类独立 reason；没有新增 `tool_result_artifacts` relation；
- tool-result entry、artifact edge/state 与现有 `ToolResultAccepted` occurrence 在同一 Host-writer transaction 接受；ACK unknown 只 exact-confirm/reissue 同一 prepared candidate，不重跑 physical tool；
- `artifact_read` 已进入 production tool specs/executor，支持 `info | text`、character offset、bounded page、session/workspace scope 与 integrity failure；它的结果不再递归归档；
- terminal 只对当前仍保留的 sanitized body 承诺 `RETAINED_SNAPSHOT`；一旦 rolling retention 丢失更早输出，`TERMINAL_RETENTION_GAP` 不会被冒充为原始 process stream 完整。

对应机器证据见 [`round1_tool_output_artifact_activation.json`](benchmarks/suites/core/v1/round1_tool_output_artifact_activation.json)。Go artifact viewer/download UI、binary artifact、多 artifact result、artifact 删除/retention UI 与后台 retention retry job 仍是明确 non-goal；PHC-01、PHC-03 至 PHC-16 的状态不因本轮改变。

## 6. PHC-07：Long-horizon context window 与 compaction

### 6.1 hard-cut 前已存在的产品能力

[`PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md) 标记 `L0A–L5 已完成`。与下列归档标题一起，它描述并验收过一套真实长程运行能力：

- [`PULSARA_CONTEXT_COMPACTION_TIMING_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_CONTEXT_COMPACTION_TIMING_IMPLEMENTATION.zh.md)；
- [`PULSARA_CONTEXT_COMPACTION_CONTINUITY_DESIGN.zh.md`](archived_docs/PULSARA_CONTEXT_COMPACTION_CONTINUITY_DESIGN.zh.md)；
- [`PULSARA_CONTEXT_COMPACTION_MID_TURN_INLINE_DESIGN.zh.md`](archived_docs/PULSARA_CONTEXT_COMPACTION_MID_TURN_INLINE_DESIGN.zh.md)；
- [`PULSARA_TOOL_RESULT_CONTEXT_BUDGET_INVESTIGATION.zh.md`](archived_docs/PULSARA_TOOL_RESULT_CONTEXT_BUDGET_INVESTIGATION.zh.md)；
- [`PULSARA_LONG_HORIZON_REAL_REPL_TRAJECTORY_ANALYSIS.zh.md`](archived_docs/PULSARA_LONG_HORIZON_REAL_REPL_TRAJECTORY_ANALYSIS.zh.md)。

其中产品层能力包括：

- `:compact` 手动压缩；
- provider call 前根据预算自动预检并压缩；
- current-run deterministic micro-compaction；
- pairing-safe current-run LLM compaction；
- tool-result rollup 与 artifact-aware thinning；
- compaction 后继续保留受保护的当前 tail；
- cumulative rollout/model-call budget；
- finalization reserve，避免预算全耗尽后无法总结；
- recurrence/loop 检测与中性 status hint；
- Inspector 解释 window、projection generation、预算状态与 call count。

### 6.2 当前代码事实

当前 canonical schema 仍包含：

- `context_snapshots`；
- `turn_context_binding_revisions`；
- `CompactionAdopted` committed event；
- `ConversationKernelRepository.adopt_context_snapshot()`；
- `enqueue_background_compaction()` 与 background compaction job handler。

但这些只是 dormant primitives：production Host/runner 没有调用 adoption 或 enqueue path，CLI 也没有 `:compact`。

当前前台路径主要依赖固定 hard bounds：

- 每 turn 最多 24 次 model call；
- 每 call 最大估算输入 128,000 tokens；
- foreground runner 使用 120 秒 operation deadline；
- 输入估算超过 cap 时直接抛出 `ValueError`；
- model-call 次数耗尽时直接失败。

### 6.3 具体丢失的用户能力

- 长对话不能主动压缩后继续；
- 运行中增长的 tool output 不能在 safe point 被折叠；
- 触及 provider input cap 时没有自动 continuation path；
- 24 次 model-call 限制前没有“为最终回答保留预算”的产品行为；
- 长程 Agent 不能把稳定历史与当前未完成 tail 分层；
- 当前有 snapshot 表并不意味着用户实际获得 compaction；
- transcript 完整保留这一正确 hard-cut 决策仍成立，但“完整保存历史”目前没有配套的“有界选择历史进入下一 call”产品能力。

以下不计为缺口：不保存 exact context-input audit、不通过 event replay 恢复 execution，以及不删除 canonical transcript。这些是既定减法边界。

### 6.4 hard-cut前compaction参考代码

- `5b7ad9f7:src/pulsara_agent/runtime/compaction/planner.py`：`CompactionBoundary`、summary message构造、private analysis stripping和previous-summary continuation，适合作为最小纯语义起点。
- `5b7ad9f7:src/pulsara_agent/runtime/compaction/service.py`：`ContextCompactionPolicy`、`should_auto_compact()`、`compact_if_needed()`、`compact()`、input builder和token estimate；重点提取threshold、target budget、summary reserve、model-visible source选择与failure circuit，不移植EventLog publication owner。
- `5b7ad9f7:src/pulsara_agent/runtime/compaction/inline.py`：`RuntimeContextCompactor.maybe_compact_before_followup()`及“保护current run tail”的mid-turn边界。
- `5b7ad9f7:src/pulsara_agent/runtime/long_horizon/window_compaction.py`：`build_window_compaction_plan()`与`build_compacted_context_window()`的deterministic window transition。
- `5b7ad9f7:src/pulsara_agent/runtime/long_horizon/window_compaction_service.py`：`ContextWindowCompactionService.compact()`与source-stale/safe-point处理；只参考业务顺序，不恢复window event replay、pending recovery或repair owner。
- production接线参考：`5b7ad9f7:src/pulsara_agent/host/session.py::compact_now`、`5b7ad9f7:src/pulsara_agent/host/session.py::_compact_if_needed_and_notify`，以及`5b7ad9f7:src/pulsara_agent/runtime/agent.py::_maybe_compact_mid_turn_before_followup`和provider-call前window compaction safe point。
- 关键回归集中在`5b7ad9f7:tests/test_context_compaction.py`：manual compaction、repeated summary、current-tail protection、threshold-driven auto compaction、huge completed run、provider preflight、mid-turn、target-derived budget、summarizer hard cap与source-stale测试。`5b7ad9f7:tests/test_long_horizon_window_rollout.py::test_compacted_window_binds_exact_plan_and_summary`补充window transition语义。
- 禁止照搬：`5b7ad9f7:src/pulsara_agent/runtime/compaction/commit.py`、long-horizon reducer/checkpoint/store及大量Started/Completed/repair event链。新实现应以canonical snapshot/revision和显式safe-point CAS表达采用结果；失败不能恢复成foreground execution recovery state machine。

## 7. PHC-08：MCP production capability

### 7.1 hard-cut 前已存在的产品能力

[`PULSARA_MCP_2026_07_28_AND_SDK_V2_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_MCP_2026_07_28_AND_SDK_V2_HARD_CUT_IMPLEMENTATION.zh.md) 标记 `MCP2 CLOSED`，保存了完整阶段验收。相关标题还包括：

- [`PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION.zh.md)；
- [`PULSARA_CLI_MCP_CAPABILITY_NEXT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_CLI_MCP_CAPABILITY_NEXT_IMPLEMENTATION.zh.md)；
- [`PULSARA_MCP_TANZO_GAP_ANALYSIS.zh.md`](archived_docs/PULSARA_MCP_TANZO_GAP_ANALYSIS.zh.md)。

已实现的产品面至少包含：

- 从配置启动/连接 MCP server；
- initialize 与协议协商；
- tool discovery、schema normalization 与动态 capability advertisement；
- MCP tool invocation；
- server catalog refresh/subscription；
- 启动延迟隔离与 readiness；
- form elicitation；
- private URL 显示/consent；
- input-required 后的用户交互；
- CLI `list/add/remove/enable/disable/doctor/reconnect` 等管理入口；
- Inspector/diagnostic 可见 server 状态与失败原因。

### 7.2 当前代码事实

当前只保留 MCP config parser/detection：

- `host inspect` 能列出已配置且 enabled 的 server id；
- production Kernel open 发现任何 enabled MCP server 时直接抛出 `KernelCompositionUnavailable`；
- README 明确说明 MCP execution adapter 未安装；
- 当前没有 MCP server supervisor、discovery owner、tool executor、interaction bridge 或 CLI 管理命令。

### 7.3 具体丢失的用户能力

- 任何已配置 MCP server 都不能被 Agent 使用；
- 配置启用 MCP 不是“能力降级”，而是阻止 session 打开；
- MCP tools 不进入模型 tool set；
- server-side schema/update 不能刷新；
- input-required、form 与 private URL 交互不可用；
- CLI 无法管理或诊断 server lifecycle；
- MCP 与 skills/capability catalog 的统一展示能力消失。

MCP Apps 与 Tasks 在旧 MCP2 文档中本来就是非目标，不列为 hard-cut 回归。

### 7.4 hard-cut前MCP参考代码

- `5b7ad9f7:src/pulsara_agent/runtime/mcp/types.py`：stdio/streamable-http config、discovered tool/resource/prompt、server snapshot、tool result、name mangling和secret-safe redaction等核心产品DTO。
- `5b7ad9f7:src/pulsara_agent/runtime/mcp/schema.py`：bounded schema normalization、dialect选择、external `$ref`拒绝和structured output validation。
- `5b7ad9f7:src/pulsara_agent/runtime/mcp/sdk.py`：`SdkMcpClientManager`、`discover_mcp_server()`、pagination bounds、transport client、result/resource/prompt lowering及safe child env；优先抽取SDK I/O与typed conversion，不搬运freshness receipt或continuation recovery graph。
- `5b7ad9f7:src/pulsara_agent/runtime/mcp/supervisor.py`与`5b7ad9f7:src/pulsara_agent/runtime/mcp/installation.py`：server启动/关闭、optional/required readiness和discovered surface安装的旧production owner；新Host只需要重新设计bounded process-local supervisor，不恢复跨Host execution continuation。
- `5b7ad9f7:src/pulsara_agent/capability/providers/mcp.py`、`5b7ad9f7:src/pulsara_agent/tools/adapters/mcp.py`、`5b7ad9f7:src/pulsara_agent/runtime/wiring.py`：discovered descriptor如何进入capability exposure并绑定真实MCP executor。
- `5b7ad9f7:src/pulsara_agent/ports/mcp_elicitation.py`、`5b7ad9f7:src/pulsara_agent/ports/mcp_secret.py`与`5b7ad9f7:src/pulsara_agent/host/mcp_elicitation.py`：form、private URL consent、sealed secret carrier和capability scope；普通hook不得看到这些raw secret。
- tool invocation产品形状可参考`5b7ad9f7:src/pulsara_agent/ports/mcp.py::McpToolExecutionRequest/McpToolCompletedOutcome`及`5b7ad9f7:src/pulsara_agent/runtime/mcp/tool_execution_port.py`，但其中pending handle、suspension companion、resume receipt与stateless recovery不得原样恢复。
- 关键回归：`5b7ad9f7:tests/test_mcp_sdk_discovery.py`、`5b7ad9f7:tests/test_mcp_v2_sdk.py`、`5b7ad9f7:tests/test_mcp_v2_contracts.py`、`5b7ad9f7:tests/test_mcp_tool_execution_port.py`、`5b7ad9f7:tests/test_mcp_host_lifecycle.py`、`5b7ad9f7:tests/test_mcp_elicitation_batch.py`、`5b7ad9f7:tests/test_mcp_subscriptions.py`。新suite应优先保留真实discovery/invoke/schema/secret/close-drain happy path，并重写依赖旧recovery owner的断言。

## 8. PHC-09：Plan workflow

### 8.1 hard-cut 前已存在的产品能力

hard-cut 前生产代码和测试确认以下完整 workflow：

- 用户通过 `:plan` 进入规划；
- Agent 调用 `enter_plan` 主动进入规划；
- 下一 run 使用 read-only permission contract；
- Agent 使用 `ask_plan_question` 向用户提出结构化问题；
- 回答回到原 tool call；
- Agent 使用 `exit_plan` 提交 plan draft；
- 用户 approve、revise 或 cancel；
- revise 后 Agent 继续规划并再次提交；
- 用户可 force-exit；
- 退出后只恢复下一 run 的默认执行权限，不在当前 read-only run 内放宽；
- plan interaction 有独立的有限预算。

主要归档证据是：

- [`PLAN_WORKFLOW_EVENT_ARCHITECTURE.zh.md`](archived_docs/PLAN_WORKFLOW_EVENT_ARCHITECTURE.zh.md)；
- [`PULSARA_RUN_BOUND_PERMISSION_MODE_PLAN.zh.md`](archived_docs/PULSARA_RUN_BOUND_PERMISSION_MODE_PLAN.zh.md)；
- hard-cut 前 `runtime/agent.py`、`host/session.py`、`runtime/plan.py` 与相应 tests。

### 8.2 当前代码事实

- `enter_plan`、`ask_plan_question`、`exit_plan` descriptor 仍在 builtin catalog；
- 当前 `DirectKernelToolPort` 不绑定它们；
- 当前 runner 没有 plan workflow dispatch；
- current canonical schema 没有 plan workflow current state；
- current REPL 没有 `:plan`、approve/revise/cancel/force-exit 命令；
- Protocol v3 interaction 只承载普通 tool confirmation 的 allow/deny。

### 8.3 具体丢失的用户能力

- 用户不能要求一个由 Runtime 强制只读的正式规划阶段；
- Agent 无法把“规划”作为 typed workflow，而只能用普通文本模拟；
- Agent 无法在规划中结构化提问并恢复原 tool call；
- plan draft 没有 approve/revise/cancel lifecycle；
- “规划完成后再执行”的权限边界消失；
- descriptor 残留会让静态 capability inspection 高估实际能力。

### 8.4 hard-cut前Plan参考代码

- `5b7ad9f7:src/pulsara_agent/tools/builtins/plan.py`：`EnterPlanTool`、`AskPlanQuestionTool`、`ExitPlanTool`三项模型工具的最小公开面。
- `5b7ad9f7:src/pulsara_agent/runtime/plan.py`：Plan instruction、question options、active/read-only状态、pending question/exit view与approve/revise/cancel语义；旧`reduce_plan_workflow_state(events)`不能作为新state owner。
- `5b7ad9f7:src/pulsara_agent/runtime/permission.py`与`5b7ad9f7:src/pulsara_agent/runtime/permission_snapshot.py`：Plan进入后run-bound read-only policy、退出后恢复default policy，而不在当前run内放宽权限。
- `5b7ad9f7:src/pulsara_agent/runtime/agent.py`：`_execute_enter_plan()`、`_execute_exit_plan()`、structured question suspension及workflow tool dispatch的旧happy path。
- `5b7ad9f7:src/pulsara_agent/host/session.py`：`enter_plan()`、`exit_plan_workflow()`及approve/revise/cancel/force-exit入口；新实现应由canonical plan row和typed interaction owner承载，而不是恢复Host-held replay state。
- `5b7ad9f7:src/pulsara_agent/runtime/run_execution/interaction.py`：只参考approval/plan/MCP三类pending interaction的closed public view；`5b7ad9f7:src/pulsara_agent/runtime/run_execution/interaction_transition.py`的receipt/reconciliation流程不是恢复目标。
- 关键回归：`5b7ad9f7:tests/test_host_core.py`中`test_user_enter_plan_immediately_switches_read_only_and_emits_durable_entry`、plan question、approve、revise、cancel、force-exit和interaction budget系列；`5b7ad9f7:tests/test_agent_runtime_loop.py`的approval resume系列；`5b7ad9f7:tests/test_plan_workflow.py`只可作为状态语义种子，不能继续以event reducer作为最终断言。

## 9. PHC-10：Hierarchical / batch subagent task graph

### 9.1 当前仍保留的 flat subagent 能力

当前新 Kernel 正式暴露四个工具：

```text
spawn_agent
list_agents
wait_agent
stop_agent
```

它们支持：启动一个 bounded child、列出 child、等待单个 child、停止单个 child；accepted task/result 进入 canonical subagent relations。这个基础面不是缺口。

### 9.2 hard-cut 前额外存在、当前不可达的能力

hard-cut 前还有一套 task-board / graph surface：

```text
create_agent_tasks
wait_agent_tasks
stop_agent_task
report_agent_phase
report_agent_result
```

对应产品语义包括：

- 一次创建一批具 stable task key 的逻辑任务；
- task 间显式 dependency；
- dependency 满足后自动启动 downstream；
- upstream failure 使 downstream 得到明确 blocked/failed cause；
- 一次等待多个 task 并返回部分 settled 结果；
- 以 logical task id 停止 task 与 active child；
- child 主动报告 phase/progress；
- child 主动提交结构化 result；
- parent 读取统一 task board，而不是手工拼多个 flat child。

归档和代码证据包括：

- [`PULSARA_SUBAGENT_GRAPH_REDUCER_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_SUBAGENT_GRAPH_REDUCER_HARD_CUT_IMPLEMENTATION.zh.md)；
- [`PULSARA_SUBAGENT_SYSTEM_NEXT_STEPS.zh.md`](archived_docs/PULSARA_SUBAGENT_SYSTEM_NEXT_STEPS.zh.md)；
- hard-cut 前 `runtime/subagent/` 与相应测试。

### 9.3 当前代码事实与缺失

- 五个旧 descriptor 仍在 builtin catalog；
- current `KernelSubagentManager.tool_names` 只有四个 flat tools；
- 五个 task-graph/report 工具没有 production executor；
- 当前每个 spawn 是独立 live asyncio task；
- 没有 dependency scheduler、batch wait、child phase reporting 或 graph task board。

[`PULSARA_SUBAGENT_DENO_WORKFLOW_RUNTIME_PLAN.zh.md`](archived_docs/PULSARA_SUBAGENT_DENO_WORKFLOW_RUNTIME_PLAN.zh.md) 主要是下一步计划，不能整体算作“被删能力”；本节只记录 hard-cut 前代码实际存在的 task graph surface。

### 9.4 hard-cut前task-graph参考代码

- `5b7ad9f7:src/pulsara_agent/ports/subagent.py`：`CreateAgentTaskSpec`、batch create/wait/stop、phase/result report command与typed outcome；这是恢复五项tool vocabulary时的首要产品契约参考。
- `5b7ad9f7:src/pulsara_agent/tools/builtins/subagent.py`：`CreateAgentTasksTool`、`WaitAgentTasksTool`、`StopAgentTaskTool`、`ReportAgentPhaseTool`、`ReportAgentResultTool`的模型工具边界。
- `5b7ad9f7:src/pulsara_agent/runtime/subagent/tool_port.py`：`_resolve_dependency_map()`、`_reject_dependency_cycles()`、`_initial_task_statuses()`和blocked dependency lowering；这些纯DAG语义可以映射到当前canonical task rows。
- `5b7ad9f7:src/pulsara_agent/primitives/subagent.py`：budget/profile、graph node/projection、explicit result handoff、stable result identity和parent result rendering policy。
- `5b7ad9f7:src/pulsara_agent/runtime/subagent/runtime.py`：batch启动、dependency completion/failure cascade、partial wait、phase/result acceptance和parent delivery的旧production orchestration；只提取产品状态转移，不恢复child `RuntimeSession`、parent EventLog graph或restart hydration。
- 关键回归：`5b7ad9f7:tests/test_subagent_runtime.py`中batch create、dependency wait、failure propagation/cycle rejection、partial multi-wait、task stop、phase report、explicit result及parent context delivery系列；`5b7ad9f7:tests/test_subagent_tool_port.py`验证closed command boundary；`5b7ad9f7:tests/test_subagent_commands.py`中的same-batch/cross-task invariant应改写成canonical SQL约束与transaction测试。
- 禁止照搬：`5b7ad9f7:src/pulsara_agent/runtime/subagent/reducer.py`、`5b7ad9f7:src/pulsara_agent/runtime/subagent/projection.py`、`5b7ad9f7:src/pulsara_agent/runtime/subagent/hydration.py`、graph checkpoint/repair和child execution recovery。Host退出后child仍按既定规则interrupted。

## 10. PHC-11：Standalone Canonical Inspector（不进入恢复主线）

### 10.1 hard-cut 前已存在的产品面

hard-cut 前 CLI 支持：

```text
pulsara inspect run ...
pulsara inspect session ...
pulsara inspect artifact ...
pulsara inspect memory ...
pulsara inspect health ...
```

Inspector 能显示或解释：

- run/session 生命周期；
- transcript 与 tool call/result；
- failure/aborted 状态；
- tool result artifact；
- context/compaction/window budget；
- terminal process/monitor；
- MCP state；
- subagent graph；
- memory/health；
- capability advertisement/gate decision 的原因。

### 10.2 当前代码事实

[`conversation_kernel/query.py`](src/pulsara_agent/conversation_kernel/query.py) 已经有部分合理的 canonical query backend：

- session/entry page；
- canonical conversation inspect；
- tool attempts/results；
- prompt queue；
- subagent tasks；
- jobs；
- selective event suffix；
- memory/health query。

但当前 CLI 的 `host inspect` 只输出静态 workspace/capability/config 信息：workspace identity、permission、skills、available tool names、MCP configured status。它不要求 session id，也不调用上述 canonical query。

### 10.3 历史产品面消失的事实

- 没有按 session/run 查询真实 canonical transcript/control state 的 CLI；
- 没有 artifact inspector；
- 没有 tool attempt/result join 的可用产品入口；
- 没有 terminal process/monitor observation；
- 没有 compaction/window 状态解释；
- 没有 MCP lifecycle inspect；
- 没有 subagent dependency/task-board inspect；
- `host inspect` 的名字容易让用户误以为它是完整 canonical Inspector，实际只是 static composition preview。

这些事实用于解释hard-cut前后发生了什么，不再构成恢复standalone Inspector CLI的需求清单。

### 10.4 冻结处置：观察面并入Go TUI

- 不恢复`pulsara inspect run/session/artifact/memory/health`兼容面，也不新建独立Python Inspector应用；
- 不为Inspector建立第二套durable projection、read model、event vocabulary或consumer receipt；
- Go TUI最终负责session history、canonical state、tool attempt/result、artifact content、terminal/subagent状态及必要diagnostic的用户呈现；
- `conversation_kernel`仍须提供TUI所需的typed、bounded、capability-scoped canonical query、observation projection与content read边界；这些是Kernel/Protocol契约，不是“恢复Inspector”；
- 当前Go TUI尚未消费的查询能力按具体产品能力和PHC ID推进，不能以“不做Inspector”为由删除其canonical真值或封闭读取路径。

因此，PHC-11的最终状态是**产品入口退役、读取能力按Go TUI需要保留**，而不是等待恢复的独立P1产品缺口。

## 11. PHC-12：Frozen Legacy Python REPL（正式退役）

根目录 [`PULSARA_LEGACY_REPL_RETENTION_CONTRACT.zh.md`](PULSARA_LEGACY_REPL_RETENTION_CONTRACT.zh.md) 明确标记：

> FROZEN LEGACY SURFACE；maintenance-only，无计划删除日期

它当时不是要求继续扩张Python REPL，而是要求hard-cut期间保留已经存在的non-secret产品行为。当前产品方向已经改变：该文档继续作为hard-cut历史证据，但不再构成未来兼容承诺。

### 11.1 当前仍保留的 REPL 命令

```text
:sessions
:resume <id>
:continue
:stop
:close
:help
```

ordinary prompt、detach/quit 也仍可用。

### 11.2 hard-cut 前存在、当前已删除的 REPL 控制面

| 命令/能力 | hard-cut 前语义 | 当前状态 |
|---|---|---|
| `:status` | 显示 stored default 与 effective next-run permission/workflow state | 缺失 |
| `:mode <preset>` | 在 run boundary 修改后续 run permission mode | 缺失 |
| `:plan [reason]` | 进入正式 Plan workflow | 缺失 |
| `:approval` | 查看 pending approval | 缺失 |
| `:interaction` | 查看 pending interaction | 缺失 |
| approval allow/deny commands | 解决 pending tool confirmation | 缺失 |
| `:compact` | idle 时手动 compaction | 缺失 |
| `:answer` / `:choose` | 回答结构化 workflow interaction | 缺失 |
| `:approve-plan` | 接受提交的 plan | 缺失 |
| `:revise-plan` | 要求修改 plan | 缺失 |
| `:cancel-plan` | 取消 plan workflow | 缺失 |
| `:force-exit-plan` | 强制退出 plan | 缺失 |
| `:mcp-cancel` | 取消 non-secret MCP pending interaction | 缺失 |

旧 `:mcp-input <json>` 可能包含 secret，原 retention contract 已要求在 secret 场景 fail closed；本索引不把“不安全地恢复该命令”当作产品目标。但 MCP 的 non-secret cancel/status 与正式 typed interaction 产品面确实整体消失。

### 11.3 历史产品影响

- Python REPL 目前不是 retention contract 描述的 maintenance-equivalent surface；
- permission 设为 ask/confirm 时，REPL 没有与 TUI 等价的交互解决入口；
- manual compaction、Plan、MCP control 全部消失；
- hard-cut 不只是删除 durability owner，也改变了被冻结的用户命令 vocabulary。

### 11.4 冻结处置：不恢复REPL兼容面

- 不以满足旧retention contract为目标恢复上述命令、命令拼写或REPL状态机；
- `:plan`、approval、structured interaction、MCP control、manual compact等背后的产品语义，分别由PHC-07/08/09及相应policy/interaction契约判断是否恢复；
- 需要用户参与的能力最终通过Go TUI的typed command/interaction协议提供，不经自由文本REPL命令推断；
- Python侧若未来需要headless automation，应建立明确的typed API/CLI command，而不是复活交互式Legacy REPL兼容层；
- 本节保留的命令表只用于证明hard-cut曾改变产品面，不能作为实现清单或架构约束。

因此，PHC-12不再是待修复缺口。真正仍需恢复的Plan、MCP、compaction等能力继续由各自PHC项负责，避免“删除REPL”被误解为“删除这些产品语义”。

## 12. PHC-13：跨 turn 失败/中断提示

[`HOST_TRANSCRIPT_FAILURE_NOTE_PLAN.zh.md`](archived_docs/HOST_TRANSCRIPT_FAILURE_NOTE_PLAN.zh.md) 虽名为 plan，但 hard-cut 前代码已经实现并有测试：

- 上一 turn failed/aborted 时，下一 turn provider context 会得到轻量、脱敏的 runtime note；
- note 说明用户输入已保存；
- note 说明上一轮 assistant text 可能为空或不完整；
- 用户说“继续”时，模型知道应从保留的输入继续，而不是把半截 reply 当完整结论；
- 新er successful turn 会使旧 note 不再反复注入。

当前新 Kernel 会把异常 turn 标记为 `INTERRUPTED`，并为未闭合 tool call 生成 provider-only closure；这是正确的 canonical/effect continuity。但 provider input item vocabulary 没有“previous turn failed/interrupted note”，普通 model/provider failure 也未被转成 model-visible explanation。

具体缺失是：

- 下一轮模型看见旧 user/assistant entries，却未必知道上一轮 terminal outcome；
- 空 assistant 或部分 assistant text 可能被误解为正常完成；
- “继续”需要模型自行猜测上一次为什么停止；
- tool-call closure 只修复 provider wire pairing，不能代替对整个 turn 失败的用户/模型语义说明。

本项不要求恢复 coroutine 或 execution replay；它记录的是 canonical history lowering 时丢失的产品语义。

### 12.1 hard-cut前failure-note参考代码

- `5b7ad9f7:src/pulsara_agent/runtime/recovery.py`：`FAILURE_NOTE_TEXT`、`INTERRUPTED_NOTE_TEXT`、`classify_unfinished_tool_calls()`、`render_unfinished_summary()`和secret-safe wording；新实现应从canonical turn/tool call/attempt/result读取，而不是调用`project_recovery_from_events()`。
- `5b7ad9f7:src/pulsara_agent/runtime/transcript.py`：`_last_terminal_run_note_target()`、`_should_emit_terminal_note()`、`_strip_unfinished_tool_calls()`与`_note_message()`展示note插入位置、只注入最新terminal failure及成功后停止重复的语义；`rebuild_prior_messages()`本身的EventLog replay不可复活。
- 关键回归：`5b7ad9f7:tests/test_host_core.py::test_rebuild_prior_messages_injects_system_note_for_failed_last_run_with_reply_end`、`::test_rebuild_prior_messages_drops_audit_only_partial_reply_before_failure_note`、`::test_rebuild_prior_messages_note_mentions_failed_proposed_only_tools`、`::test_rebuild_prior_messages_injects_note_for_failed_last_run_without_reply_end`，以及aborted/successor-turn覆盖。
- 新回归必须继续区分`call无attempt`与`attempt无result`，并确保note不泄漏tool arguments、provider URL、API key或raw exception。

## 13. PHC-14：Model-visible tool observation timing 与 freshness

[`PULSARA_UNIVERSAL_TOOL_OBSERVATION_TIMING_PLAN.zh.md`](archived_docs/PULSARA_UNIVERSAL_TOOL_OBSERVATION_TIMING_PLAN.zh.md) 对应的 hard-cut 前代码已经包含 `ToolObservationTimingFact`，并贯穿 tool result、terminal projection、context renderer 与 Inspector。

模型曾能看到的语义包括：

- `observed_at`；
- observation duration；
- tool-reported duration；
- freshness，例如 current turn/current run tail/historical；
- compaction 后旧 observation 的时间归属。

当前 canonical rows 仍保存若干 `accepted_at`/`started_at` 时间，但 provider input lowering主要输出 tool result text，不再形成上述统一的 model-visible typed observation。

具体产品退化：

- 模型难以判断“这个状态是刚查的还是很早以前查的”；
- 长运行 terminal、网络查询、文件状态、MCP/resource observation 的过期风险不可见；
- duration 不再帮助判断 timeout、卡住或完成速度；
- resume/compaction 后，旧 tool result 与当前环境的时间关系不明确。

### 13.1 hard-cut前tool timing参考代码

- `5b7ad9f7:src/pulsara_agent/primitives/tool_observation.py`：`ToolObservationTimingFact`的UTC、duration和closed freshness vocabulary，是最小typed语义参考。
- `5b7ad9f7:src/pulsara_agent/runtime/tool_executor.py`：`build_tool_observation_timing()`、`synthetic_tool_observation_timing()`与`_tool_observation_freshness()`展示统一构造点。
- `5b7ad9f7:src/pulsara_agent/capability/result_semantics.py`与`5b7ad9f7:src/pulsara_agent/ports/tool_result_semantics.py`：tool-specific结果如何携带统一timing，而不信任任意tool body伪造metadata。
- `5b7ad9f7:src/pulsara_agent/runtime/context_input/render.py`：provider-visible `observed_at/duration/freshness`的bounded render；新实现应从canonical tool result/attempt timestamps生成typed projection，不恢复`ToolResultEndEvent`作为真源。
- Terminal producer参考：`5b7ad9f7:src/pulsara_agent/tools/builtins/terminal.py::terminal_timing_payload`及`5b7ad9f7:src/pulsara_agent/tools/builtins/terminal_process.py`的`background_process_observation`分类。
- 关键回归：`5b7ad9f7:tests/test_event_message_system.py::test_tool_observation_timing_requires_utc_iso_and_non_negative_duration`、`5b7ad9f7:tests/test_context_input_facts.py::test_tool_observation_timing_normalizes_utc_and_rejects_negative`、`5b7ad9f7:tests/test_context_transcript_projection.py::test_tool_body_cannot_forge_observation_timing_inclusion`、`5b7ad9f7:tests/test_tools.py::test_terminal_result_timing_is_shared_by_payload_metadata_and_artifact`。

## 14. PHC-15：Capability catalog 与真实 executor 不一致

当前 builtin catalog 共 28 个 descriptor。production model tool surface实际可达 19 个：

- 7 个 direct tools：`read_file/search_files/edit_file/write_file/todo/terminal/terminal_process`；
- 4 个 flat subagent tools：`spawn_agent/list_agents/wait_agent/stop_agent`；
- 8 个 current memory tools（memory 语义不在本轮复核范围）。

以下 9 个 descriptor 没有当前 production executor/binding：

```text
artifact_read
enter_plan
ask_plan_question
exit_plan
create_agent_tasks
wait_agent_tasks
stop_agent_task
report_agent_phase
report_agent_result
```

此外，`capability/tool_action.py` 仍有 `terminal_monitor` policy helper，会查询一个当前 builtin catalog 中已不存在的 `terminal_monitor` entry。

这不是独立用户功能，但它是重要产品缺失证据：

- 静态 descriptor inventory 不能代表实际可用能力；
- tests 若只检查 descriptor/schema，会漏掉 production binding 缺失；
- dead descriptor 使 hard-cut 后的真实产品面难以判断；
- Terminal、artifact、Plan 与 task-graph 的缺失被残留类型掩盖。

### 14.1 hard-cut前catalog/executor闭合参考代码

- `5b7ad9f7:src/pulsara_agent/capability/builtin_catalog.py`：descriptor、binding kind、availability requirement、permission contract及tool family closed sets。
- `5b7ad9f7:src/pulsara_agent/runtime/tool_composition.py`：`build_runtime_tool_composition_input()`、`build_runtime_tool_binding_installation()`、`_builtin_tools()`和`_validate_catalog_binding_kinds()`；它明确区分“descriptor存在”与“真实executor安装”。
- `5b7ad9f7:src/pulsara_agent/capability/exposure.py::build_exposure_plan`：direct/deferred/hidden/unavailable/callable disposition。
- `5b7ad9f7:src/pulsara_agent/tools/registry.py::ToolRegistry`与`5b7ad9f7:src/pulsara_agent/runtime/tool_executor.py::ToolExecutor`：最终name-to-executor lookup及unknown tool fail-closed。
- 关键回归：`5b7ad9f7:tests/test_capability_surface.py::test_exposure_plan_hides_direct_descriptor_without_execution_binding`、`5b7ad9f7:tests/test_capability_surface.py::test_exposure_plan_diagnoses_non_direct_descriptor_without_execution_binding`、`5b7ad9f7:tests/test_capability_surface.py::test_builtin_provider_uses_explicit_descriptor_truth_for_bound_core_tools`及同文件workflow-control missing descriptor测试。
- 不应恢复`5b7ad9f7:src/pulsara_agent/runtime/session_run_capabilities.py`的旧`RuntimeSession`port graph。目标guard应直接证明：任何advertised callable descriptor都有当前Host generation中的exact production binding；不可达descriptor必须被typed标为unavailable或从surface移除。

## 15. PHC-16：Go TUI作为未来主要产品面（当前延后）

用户已明确：当前优先修复Python Agent Runtime内核，Go端TUI S1–S3能力可以先搁置；同时，未来不再恢复Legacy Python REPL或standalone Canonical Inspector，Go TUI将成为主要的会话观察、交互和控制产品面。因此本节当前只登记UI缺口，不把它们混入Runtime恢复主线，但其长期地位不是可选客户端。

根据根目录 TUI 文档与当前 `clients/terminal` 对照，至少以下能力不在当前 production Go client 主路径中，或只剩 S0 spike 证据：

- bounded composer undo/redo；
- multiline command-history scratch 与对称 Up/Down traversal；
- large bracketed-paste review、head/omission/tail 展示与显式确认；
- selection/copy 与 copy-last/copy current materialization；
- warning/failure sticky notice 与显式 dismiss；
- plan draft/approval UI；
- MCP form/private URL/secret interaction UI；
- richer semantic activity cells 与 terminal monitor activity。

当前 Go client 已保留 Protocol v3 snapshot/history/live/content read 的基本能力，因此本节不能写成“TUI 全部丢失”。这里只记录 hard-cut 前文档已冻结、当前 production implementation 未兑现的高层 UX。

这一方向不要求现在提前实现完整Go UI，但要求Python Kernel恢复产品能力时提供稳定的typed command、canonical observation、live event与bounded content read契约。不得为了方便TUI而让客户端从raw row猜测实时语义；也不得为TUI恢复universal EventLog、execution replay或独立Inspector projection。

### 15.1 hard-cut前Go TUI参考代码

- `5b7ad9f7:clients/terminal/internal/components/composer/model.go`、`5b7ad9f7:clients/terminal/internal/components/composer/update.go`、`5b7ad9f7:clients/terminal/internal/components/composer/history.go`、`5b7ad9f7:clients/terminal/internal/components/composer/paste.go`、`5b7ad9f7:clients/terminal/internal/components/composer/view.go`：grapheme-safe edit、bounded undo/redo、对称history traversal、fresh scratch preservation和large-paste review。
- `5b7ad9f7:clients/terminal/internal/components/notification/model.go`与`5b7ad9f7:clients/terminal/internal/components/notification/view.go`：bounded notification、sticky warning/failure、dismiss和expiry generation。
- `5b7ad9f7:clients/terminal/internal/components/transcript/model.go`、`5b7ad9f7:clients/terminal/internal/components/transcript/wrap_cache.go`与`5b7ad9f7:clients/terminal/internal/components/transcript/view.go`：server order、CJK/emoji display width、scroll anchor、follow-tail和unseen count。
- `5b7ad9f7:clients/terminal/internal/app/input.go`、`5b7ad9f7:clients/terminal/internal/app/keymap.go`、`5b7ad9f7:clients/terminal/internal/app/update.go`：closed keyboard/paste/mouse input、command freeze-before-I/O、clipboard和late receipt不清除新draft。
- `5b7ad9f7:clients/terminal/internal/interaction/state.go`：pending interaction的typed phase/view identity；未来Plan/MCP UI应基于Protocol v3的新typed control，而不是复用v2 carrier。
- 关键回归：`5b7ad9f7:clients/terminal/internal/components/composer/model_test.go`、`5b7ad9f7:clients/terminal/internal/components/notification/model_test.go`、`5b7ad9f7:clients/terminal/internal/components/transcript/view_test.go`、`5b7ad9f7:clients/terminal/internal/app/input_test.go`和`5b7ad9f7:clients/terminal/internal/app/s3_command_test.go`。
- 禁止照搬：旧`terminal_client.proto`/Protocol v2、durable presentation cache、command receipt/reconciliation及三平面旧projection。可复用的是纯Go UX state machine与显示性质；wire identity、snapshot/GAP和command ACK必须重新绑定当前Protocol v3。

## 16. archived_docs 标题覆盖与结论

本轮扫描了 [`archived_docs/`](archived_docs/) 下全部 116 个 Markdown 文件的标题。以下按产品族归类，目的是说明哪些标题触发了进一步代码核对，以及最终是否形成缺口。

### 16.1 Terminal / tool output 标题族

重点标题：

- `TERMINAL_SURVEY_*`（Pulsara、Codex、Claude Code、Hermes、OpenClaw、Anybox）；
- `TERMINAL_RUNTIME_V1_IMPLEMENTATION_PLAN`；
- `TERMINAL_YIELD_MODEL_V2_DESIGN/IMPLEMENTATION`；
- `TERMINAL_SHELL_ENV_V1_IMPLEMENTATION_PLAN`；
- `TERMINAL_P0/P1_IMPLEMENTATION_PLAN`；
- `PULSARA_TERMINAL_PUBLIC_TOOL_API_SPLIT_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_TERMINAL_PROCESS_MONITOR_AND_AGENT_WAKE_DESIGN`；
- `TOOL_RESULT_ARTIFACT_PR1_IMPLEMENTATION_PLAN`；
- `TOOL_RESULT_ADAPTIVE_PREVIEW_IMPLEMENTATION`；
- `PULSARA_TOOL_RESULT_CONTEXT_BUDGET_INVESTIGATION`；
- `PULSARA_TOOL_RESULT_ENVELOPE_BUDGET_INVESTIGATION`。

结论：形成 PHC-01 至 PHC-06 和 PHC-02。三工具、monitor、streaming、artifact、env、cwd 都有 hard-cut 前代码/测试，不只是概念稿。

### 16.2 Long-horizon / context 标题族

重点标题：

- `PULSARA_CONTEXT_COMPACTION_*`；
- `PULSARA_LONG_HORIZON_*`；
- `PULSARA_CONTEXT_ENGINEERING_COMPILER_DESIGN`；
- `PULSARA_CONTEXT_COMPILER_INPUT_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_PROVIDER_INPUT_CAUSAL_ORDER_AND_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_RUNTIME_OBSERVATION_AND_AUXILIARY_CONTEXT_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_UNIVERSAL_TOOL_OBSERVATION_TIMING_PLAN`；
- `PULSARA_PROMPT_CACHE_CONTRACT`；
- `PULSARA_CONTEXT_TIMING_HEADER_PLAN`。

结论：long-horizon/compaction 与 tool timing 有已实现证据，形成 PHC-07/PHC-14。Exact context-input audit 已明确不承诺，不计缺口。Prompt cache accounting/adapter support仍有部分当前代码，未列为关键产品丢失。Context Timing Header 只有计划证据，未单列。

### 16.3 MCP 标题族

重点标题：

- `PULSARA_MCP_2026_07_28_AND_SDK_V2_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_CLI_MCP_CAPABILITY_NEXT_IMPLEMENTATION`；
- `PULSARA_MCP_TANZO_GAP_ANALYSIS`；
- `PULSARA_MCP_STARTUP_LATENCY_NOTE`。

结论：MCP2 明确完成且当前整个 execution adapter 不存在，形成 PHC-08。Apps/Tasks 是旧文档明确非目标，不列缺口。

### 16.4 Plan / permission / approval 标题族

重点标题：

- `PLAN_WORKFLOW_EVENT_ARCHITECTURE`；
- `PULSARA_RUN_BOUND_PERMISSION_MODE_PLAN`；
- `STEP4_CONVERSATIONAL_MODE_SWITCH_PLAN`；
- `LIGHTWEIGHT_PERMISSION_SYSTEM_V1_IMPLEMENTATION`；
- `PERMISSION_PR4_ASK_ON_REQUEST_IMPLEMENTATION`；
- `APPROVAL_RESUME_V1_IMPLEMENTATION`；
- `HOST_USER_STOP_V1_IMPLEMENTATION`。

结论：Plan workflow缺失形成PHC-09。REPL mode及旧命令确实在hard-cut中消失，但PHC-12现已明确退役，不作为兼容恢复项；仍有价值的permission/approval交互语义应进入Go TUI和typed policy/interaction协议。普通tool confirmation、allow/deny、Host stop当前仍存在，不应误报为全部permission/approval丢失。`STEP4`本身标记计划草案，但hard-cut前`:mode`已有代码，因此仍保留为历史审计事实。

### 16.5 Subagent 标题族

重点标题：

- `PULSARA_SUBAGENT_GRAPH_REDUCER_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_SUBAGENT_SYSTEM_NEXT_STEPS`；
- `PULSARA_SUBAGENT_RUNTIME_PRIOR_ART_RESEARCH`；
- `PULSARA_SUBAGENT_DENO_WORKFLOW_RUNTIME_PLAN`。

结论：flat child 保留；batch/dependency/task reporting 有代码证据并形成 PHC-10。Deno WorkflowScript 的更大设计没有完成证据，不计为被删产品。

### 16.6 Capability / skills / filesystem 标题族

重点标题：

- `CAPABILITY_SKILL_RUNTIME_V1_IMPLEMENTATION`；
- `PULSARA_UNIFIED_CAPABILITY_SURFACE_IMPLEMENTATION`；
- `PULSARA_BUNDLED_SKILLS_HERMES_LIKE_IMPLEMENTATION`；
- `CAPABILITY_SKILL_BUNDLE_SURVEY`；
- `READ_ONLY_FILESYSTEM_TOOLS_HOME_SCOPE_IMPLEMENTATION`；
- `PULSARA_DIRECTORY_CONTRACT_CODEX_COMPAT`。

结论：local/bundled skills、active skill prompt、read-only filesystem home scope 和基本 directory discovery 当前仍存在，不列为整项缺失。Catalog/executor 的 9 项漂移形成 PHC-15。

### 16.7 Host / conversation / LLM 标题族

重点标题：

- `CONVERSATION_RESUME_V1_DESIGN`；
- `HOST_USER_STOP_*`；
- `HOST_TRANSCRIPT_FAILURE_NOTE_PLAN`；
- `LLM_RETRY_*`；
- `OPENAI_SDK_STREAMING_V1_IMPLEMENTATION`；
- `PULSARA_RESOLVED_MODEL_CALL_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_AGENT_RUNTIME_AND_HOST_SESSION_OWNERSHIP_HARD_CUT_IMPLEMENTATION`。

结论：detach/reattach、conversation resume、Host stop、provider retry、typed model streaming 与 resolved model config 当前仍存在。跨-turn failure note 已实现后被删，形成 PHC-13。

### 16.8 Memory / graph 标题族

包括全部 `MEMORY_*`、`GRAPH_DATABASE_VISION`、`ONTOLOGY_*`、`OXIGRAPH_*`。

结论：memory 按用户要求由后续专项重新设计，本索引不评价；Oxigraph、SPARQL、JSON-LD ontology 是 hard-cut 明确删除项，不计产品回归。

### 16.9 Durability / recovery / architecture 标题族

包括 `RECOVERY_CONTRACT_DESIGN`、`FAILED_ABORTED_RECOVERY_*`、`EXECUTION_EVIDENCE_LEDGER_MVP`、authority materialization、projection jobs、schema hot path、model segment coalescing、runtime storage/architecture debt 等。

结论：旧 execution recovery、checkpoint、receipt、repair、projection delivery 与 segment persistence 本来就是减法对象，不因历史文档多而列为产品缺口。只有其中承载的独立用户语义——例如跨-turn failure note与Terminal monitor——被拆出单独登记；历史Inspector只作审计，观察面并入Go TUI。

## 17. 已确认仍保留的关键产品能力

为避免索引变成“hard-cut 后什么都没有”，以下能力经当前代码确认仍有 production path，不列为缺口：

- canonical user/assistant/tool transcript；
- 多 tool call 的 message-before-dispatch 与 result closure；
- detach、reattach、resume most recent session；
- prompt queue 与 steer active turn；
- stop active turn、close conversation；
- tool confirmation allow/deny；
- read/search/edit/write filesystem；
- read-only filesystem 对 home/外部文本 scope 的有限访问；
- `todo` local state；
- `terminal` 基础 command/yield；
- `terminal_process` 八个即时 action；
- Host close kill/join terminal process；
- 完整 sanitized tool-output artifact、adaptive inline preview 与 scoped `artifact_read` info/text 分页；
- Text/Thinking/Data/ToolCall/ToolResult typed Live events；
- provider retry；
- local skills、bundled skills sync/status/reset 与 active skill prompt；
- flat `spawn_agent/list_agents/wait_agent/stop_agent`；
- Protocol v3 canonical snapshot/history/live/content read 基础；
- PostgreSQL canonical blobs 对已提交 transcript content 与 accepted tool-result artifact edge 的 bounded read。

这些“已保留/已恢复”项不抵消前文的子语义缺失。例如，Round 1 恢复了已保留 terminal snapshot 的 artifact，但没有恢复 terminal monitor、真正实时 stdout/stderr streaming 或被 rolling retention 淘汰的原始字节；“TerminalMonitor Live enum 保留”也不代表“terminal_monitor 工具保留”。

## 18. 尚未作为缺口确认的标题

以下标题触发了检查，但目前不应写成 hard-cut 回归：

- `PULSARA_SUBAGENT_DENO_WORKFLOW_RUNTIME_PLAN`：主要是未来设计；
- `PULSARA_CONTEXT_TIMING_HEADER_PLAN`：未确认形成稳定 production product；
- MCP Apps/Tasks：MCP2 明确排除；
- local Agent sandbox：主要是 survey，Pulsara 的 terminal 本来仍是 trusted host shell；
- graph database vision/Oxigraph：明确有意删除；
- exact context-input audit：已经冻结“不承诺”；
- yielded terminal/subagent 跨 Host continuation：已经冻结“不承诺”；
- old event replay/reducer/checkpoint/receipt：durability machinery，不是独立产品能力；
- memory governance/recall/lifecycle：进入后续 memory 专项，不在本索引下结论。

## 19. 本索引的使用边界

后续任何产品恢复规格都应从本索引选择一个或多个 PHC ID，并重新做当时的代码真值确认。本文本身不授权：

- 恢复旧 EventLog vocabulary；
- 恢复 execution recovery state machine；
- 把所有缺失能力塞回同一个 Runtime owner；
- 因旧 descriptor 存在就直接接线；
- 因旧测试被删除就原样复制旧测试；
- 绕过 canonical relational conversation kernel。
- 恢复Legacy Python REPL兼容命令或其交互状态机；
- 建设standalone Canonical Inspector、第二套Inspector read model或durable projection。

本索引当前冻结的唯一结论是：hard-cut 成功删除了大量 durability machinery，但同时确实删除或降级了上述独立产品能力；这些能力不能再被“旧机制已删除”或“类型还在”掩盖。

PHC-11与PHC-12是例外处置：它们保留在索引中用于审计hard-cut事实，但不进入恢复backlog。前者所需的canonical观察能力、后者所涉及的Plan/MCP/approval等独立产品语义，最终由Go TUI及各自Kernel契约承接；不以恢复旧Python产品面为目标。

同样，后续恢复不能把`26 / 23 / 13 / 2`当作拒绝真实产品语义的永久配额。任何数量变化都必须经过上述closed contract审查，但“保持旧数字”不优先于“以正确的canonical/live边界完整表达产品能力”。
