# Pulsara hard-cut 后产品能力缺失索引

> 状态：WORKING GAP INDEX（产品能力事实索引，不是恢复设计；PHC-02 已通过 Round 1 恢复，PHC-01/03/04/05/06 已通过 Round 2 恢复；PHC-17 的typed compiler与同Host prefix continuity已通过 Round 3 / 3.1完整恢复；PHC-09 的 Python Runtime/Host、canonical/Protocol 后端已于 2026-08-12 通过 Round 4 恢复，Go/TUI 产品闭环明确延期；PHC-07A execution envelope已通过 Round 5A恢复，PHC-07B compaction仍缺失）
>
> 初始调研：2026-08-10；最近复核：2026-08-13（PHC-17 Round 3.1 activation、PHC-09 activation、PHC-07A Round 5A activation）
>
> hard-cut 前代码基线：`5b7ad9f7`
>
> 当前 checkpoint HEAD：`a71aa195f2469701fb078d79f78f4fe234bc0d46`；Round 3 Structured Model-Input Compiler与Round 4 Plan Workflow已经提交；Round 3.1已按[`ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md`](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)激活，并闭合既有Protocol上的busy-steer/queue-next-turn窄Go输入；Round 4 Plan的完整Go/TUI交互产品面仍不计入本次activation
>
> 范围：Python Agent Runtime / Host及其直接产品能力；Go TUI总体仍不在当前恢复主线中，但被确定为后续唯一主要交互与观察产品面。Round 3.1只例外纳入既有Protocol上的busy-steer/queue-next-turn窄输入绑定，因为它直接决定provider-input causal suffix
>
> 明确排除：memory 子系统重设计、Oxigraph/SPARQL、旧 EventLog execution replay、coroutine/provider transport recovery、exact context-input audit、跨Host provider-input generation/prefix accumulator恢复、跨 Host terminal/subagent execution 恢复、Legacy Python REPL兼容恢复、standalone Canonical Inspector产品

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

model-input/context compiler位于canonical truth与一次physical provider request之间。它是process-local、provider-neutral的typed projection：可以选择、排序、预算和lower已经存在的事实，但不拥有conversation、memory、capability、plan、tool或terminal真值，也不把编译结果升级为execution recovery authority。hard-cut删除exact request audit与provider-input replay，不等于可以删除这层产品语义。

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
11. 每次model call的输入必须从exact canonical cut与当前process-local typed facts编译；compiler可以产生bounded operational diagnostics，但不能写durable compiled-input proof、成为reopen前置或通过event replay恢复历史request。

#### `27 / 23 / 13 / 2`的正确地位

当前代码中的27类Committed、23类Live、13个subject slot与2类append guard，是Stage 2 hard-cut及Round 2产品恢复后用于证明旧151类universal grammar仍未回流production composition的**closed activation oracle**。第27类是与canonical `TERMINAL_OBSERVATION`同事务接受的`TerminalObservationAccepted`；这些数字不是Pulsara永久的产品能力上限，也不是评价架构好坏的数字目标。

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
- 当前：`242895dcfef1af1fcdcd1f433b28637c16020720`；
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
- 存在名为`KernelCapabilityComposer`的类，但它只拼接base prompt、skill catalog与active skill prompt，没有多源typed context allocation、channel lowering或预算降级。

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
| PHC-01 | Terminal 三工具完整边界 | **已恢复（Round 2）**：三套strict工具与production executor闭合；monitor只活在当前Host并可在safe point自主唤醒Agent | 长任务可注册future observation，无需模型反复poll；cancel不kill process |
| PHC-02 | 完整 tool output artifact 与 `artifact_read` | **已恢复（Round 1）**：完整 sanitized candidate 先于 preview 保留；大输出可通过 scoped `artifact_read` 分页读取 | 中等输出完整展示并给出 artifact reference；大输出显示 UTF-8-safe head/tail 并可按需读取省略段 |
| PHC-03 | Terminal 真正实时 stdout/stderr streaming | **已恢复（Round 2）**：PIPE/PTY physical reader在process运行期间产生provisional ToolResult Delta，End以canonical preview authoritative replacement | 用户与模型可在命令结束前观察真实sanitized增量 |
| PHC-04 | Terminal retained-output/cursor 语义 | **已恢复（Round 2）**：16 MiB/process、128 MiB/Host UTF-8 retained hard bound，exact cursor/delta与typed GAP | 当前Host内可可靠增量读取；retention淘汰被显式表示而非重复tail |
| PHC-05 | Terminal shell/profile/env 产品语义 | **已恢复（Round 2）**：bounded login-shell snapshot、default-deny inert env、single-flight/TTL/fallback、nearest `.venv/bin`与diagnostic | 用户工具链PATH可用；active capability environment默认拒绝且env value不进入diagnostic |
| PHC-06 | Terminal foreground cwd continuity | **已恢复（Round 2）**：前台命令physical completion后捕获workspace内final cwd；yielded process永不推进session cwd | 后续前台命令从真实final cwd启动，无后台并发竞争 |
| PHC-07 | Long-horizon execution / context window / compaction | **部分恢复**：Round 5A已删除固定model/tool-call次数与turn-wide wall-clock cap，并以closed owner watchdog约束单项operation；Round 5B仍拥有context rebase、summary与snapshot adoption | execution envelope已恢复；当fixed prefix或new suffix命中单次provider-input/resource typed boundary时仍不能主动/自动压缩继续，两类问题不得再由一套budget/recovery graph混合解决 |
| PHC-08 | MCP production capability | **核心已恢复（Round 6）**：stdio/Streamable HTTP、bounded discovery、scope-filtered direct typed tools、resource/prompt读取、MCP_CATALOG、CLI管理与真实执行均已接入；semantic-identical reconnect保持Round 3.1 prefix，schema变化在safe point rebase | Agent可直接使用已配置MCP能力；form/private URL、OAuth、MCP-backed skill activation、server Sampling/Roots、Apps/Tasks与advanced Go UI仍是明确non-goal |
| PHC-09 | Plan workflow | **Python Runtime/Host 与 Protocol 后端已通过 Round 4 恢复；Go/TUI 延期**：三项ROOT-only control tool、canonical question/draft lifecycle、Plan-scoped read-only overlay、send-time immutable permission snapshot、Host-owned automatic continuation及typed Protocol v3边界已进入production；oracle为`34/23/15/2/26/4` | Headless typed caller已可完成Plan流程；面向用户的permission selector、question/draft review与重连展示仍等待Go/TUI闭环 |
| PHC-10 | Hierarchical/batch subagent task graph | **显著退化**：只剩 flat spawn/list/wait/stop | 依赖任务、批量调度、child phase/result reporting 与 task-board 语义消失 |
| PHC-11 | Standalone Canonical Inspector 产品入口 | **并入Go TUI，不单独恢复**：历史Inspector已消失；canonical query/Protocol后端按TUI需要保留和补齐 | 不建设第二套Inspector UI、read model或durable projection；会话观察最终由Go TUI呈现 |
| PHC-12 | Frozen Legacy Python REPL 产品面 | **明确退役，不恢复兼容**：旧命令差异只作hard-cut审计记录 | approval、plan、MCP等仍有价值的产品语义归各自能力族，并最终通过Go TUI交互，不为旧命令表复建Runtime机制 |
| PHC-13 | 跨 turn 失败/中断提示 | **已恢复（Round 7）**：same-scope immediate predecessor在同一canonical cut中形成bounded、脱敏、typed outcome；成功successor遮蔽更早失败，late result只追加修正 | “继续”时模型可区分user stop、Runtime/provider failure、Host lifecycle、resource boundary与unknown interruption，不把完整canonical entry误称为partial message |
| PHC-14 | Model-visible tool observation timing/freshness | **已恢复（Round 7）**：既有`tool_results`冻结observed time、monotonic duration、immutable origin与optional trusted duration；每turn追加freshness frontier而不回写旧result | 模型可判断观测时刻、耗时及CURRENT/PREVIOUS/HISTORICAL关系，tool body不能伪造outer timing |
| PHC-15 | Capability catalog 与真实 executor 一致性 | **仍不闭合，但Plan漂移已由Round 4关闭**：29 个descriptor中，9个direct、4个flat subagent、8个memory及3个Plan control已有production binding；剩余5个均属于旧hierarchical task-graph缺口 | Round 1/2/4已分别闭合`artifact_read`、Terminal与Plan；dead descriptor只剩PHC-10任务图能力族 |
| PHC-16 | Go TUI S1–S3及各恢复轮次UI | **未来主要产品面，整体明确延后；Round 3.1有窄例外**：Round 4只冻结Python Protocol/canonical边界，不把Go selector、question/draft review或客户端exact-join计入activation；Round 3.1仅补既有command kind上的busy Enter steer与Tab queue-next-turn | 最终承接会话观察、交互与控制；composer/copy/paste/notice及Plan/MCP等能力族UI另行实施 |
| PHC-17 | Structured model-input / context compilation | **已恢复（Round 3 + Round 3.1）**：exact canonical reader、provider-neutral compiler、scope-frozen tool surface、target estimator与typed allocation已恢复；Host-scoped ROOT/child epoch保证同scope同epoch的SYSTEM/tools不变、messages只追加，dynamic source与历史tool-result表示不再重写 | busy `Enter` steer exact active ROOT，`Tab`排队future `NEW_TURN`；Host replacement从canonical rows冷启动，不恢复durable generation、provider remote state或prefix replay |

Round 3与Round 3.1已经闭合PHC-17的克制、process-local typed compilation与跨model-call append-only lifecycle。同一Host、同一ROOT/child scope、同一epoch满足`system/tools不变 + messages只追加`；它不新增durable真值，Host replacement仍cold start。PHC-07 compaction及其他model-visible能力应在该边界上生长，避免每个产品族再次各自拼接system/user消息和预算逻辑；它们仍分别保持open。Permission不另立PHC：通用动态mode能力表现为“发送前选择、发送时冻结本次run”，frozen snapshot正是该选择的accepted form；Plan-scoped overlay仍由PHC-09拥有，并在snapshot冻结前强制收窄effective mode。

## 4. Terminal：hard-cut 前后三工具产品真值

Terminal 是本次索引中最需要单独冻结的能力族。hard-cut 前最终公开面不是一个泛化 terminal 工具，也不是两个工具，而是明确的三个工具：

```text
terminal
terminal_process
terminal_monitor
```

Round 2已在当前conversation kernel中恢复这三项产品能力。以下小节同时保留hard-cut后、Round 2前的缺口证据，并在每个“当前”结论处更新为activation后的production真值。机器证据见[`round2_terminal_runtime_activation.json`](benchmarks/suites/core/v1/round2_terminal_runtime_activation.json)。恢复没有新增durable terminal relation、job、guard、subject slot、receipt、checkpoint、projection或跨Host recovery。两轮activation反向审阅又把exact-cursor artifact source、shell leader/process-group completion、physical-completion wait、linearized launching admission、monitor evaluation generation、probe SPAWNING close、PTY EOT、ROOT monitor scope、closed rejection、独立sanitizer-unavailable reason与subagent completion attribution纳入同一产品门控；这些均是现有process-local owner的窄闭合，不是新的durability authority。

[`PULSARA_TERMINAL_PUBLIC_TOOL_API_SPLIT_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_TERMINAL_PUBLIC_TOOL_API_SPLIT_HARD_CUT_IMPLEMENTATION.zh.md) 标记 `TAPI0–TAPI2 已落地`；[`PULSARA_TERMINAL_PROCESS_MONITOR_AND_AGENT_WAKE_DESIGN.zh.md`](archived_docs/PULSARA_TERMINAL_PROCESS_MONITOR_AND_AGENT_WAKE_DESIGN.zh.md) 标记 `TM0–TM5 已完成`。hard-cut 前 `5b7ad9f7:src/pulsara_agent/ports/terminal.py` 也确实同时定义了三套 description、schema、port 与 result union。

### 4.1 三工具职责矩阵

| 工具 | hard-cut 前的正式产品职责 | 当前代码事实 | 判断 |
|---|---|---|---|
| `terminal` | 启动一条 shell command；在 `yield_time_ms` 内等待；完成则返回 terminal result，仍运行则返回 exact `process_id` | strict schema、production binding、PIPE/PTY reader、real live sink、Round 1 artifact与final cwd均闭合 | **已恢复** |
| `terminal_process` | 对 exact process 做一次即时操作：`list/log/poll/wait/write/submit/close_stdin/kill`；不安排未来 wake | 八个action、exact since-cursor、typed GAP、bounded snapshot、stdin/kill/join全部由同一Host process owner提供 | **已恢复** |
| `terminal_monitor` | `register/list/cancel` Host-owned monitor；按 output/quiet/heartbeat/completion/expiry 形成未来观察；可在后续安全点唤醒 Agent | strict schema、production executor、process-local coordinator、safe-point acceptance与autonomous continuation闭合 | **已恢复** |

这三个工具的产品分工不能互相替代：

- `terminal_process.wait` 表示“本次 tool call 内最多等 30 秒”；
- `terminal_process.poll/log` 表示“现在读取一次”；
- `terminal_monitor.register` 表示“结束当前等待，未来有意义的进展或完成时再通知”。

三种语义现已同时存在，但monitor/process/cursor都不会跨Host恢复；只有被safe point接受的observation进入canonical conversation。

### 4.2 `terminal`：保留与恢复后的精确边界

#### 已保留

- 在 workspace 内启动 shell command；
- `yield_time_ms` 到达后返回 `running`；
- 为仍运行的进程返回 Host-scoped exact `process_id`；
- PIPE/PTY 两种模式；
- status、exit code、timed out、process id、bounded output；
- Host close 时终止并 join 当前 Host 所有进程；
- 输出基础 ANSI 清理与常见 secret 文本 redaction；
- 当前 Host 内的进程数量与 finished TTL bound。

#### Round 2已恢复

1. **运行中真实输出增量**：single incremental sanitizer直接消费PIPE/PTY physical reader；process尚未结束时即可发出bounded provisional `ToolResultDelta`，final `ToolResultEnd`用相同block identity安装Round 1 canonical preview。
2. **完整输出与retention边界**：process-local owner保存最多16 MiB sanitized retained body；每次tool result冻结时由Round 1保存可证明的完整retained snapshot，省略内容通过scoped `artifact_read`读取。retention GAP与delivery HEAD_TAIL正交。
3. **真实 final cwd**：只在foreground process physical completion后读取workspace内final cwd并推进session；yielded process不推进。
4. **用户 shell环境近似**：受控login-shell probe具备size/timeout/TTL/single-flight/fallback、default-deny inert allowlist与最近`.venv/bin` overlay；active环境默认拒绝。
5. **长任务下一步提示**：三工具description再次区分即时wait/poll/log、future monitor与artifact continuation。

### 4.3 `terminal_process`：八个即时 action 与cursor/GAP边界

| Action | 当前状态 | Round 2后的精确边界 |
|---|---|---|
| `list` | 保留 | 只能列当前 Host 的 process，不含 monitor inventory；跨 Host 不恢复是既定边界，不算缺口 |
| `poll` | 保留并补全 | 接受exact `since_cursor`，返回delta或typed GAP；单次响应仍有独立展示上限 |
| `log` | 保留并补全 | 读取16 MiB/process retained owner的bounded snapshot/delta；tool result可通过Round 1 artifact continuation读取其冻结正文 |
| `wait` | 保留并补全 | 仍是单次、最长30秒的有限等待；仍running时可显式转入monitor future observation |
| `write` | 保留 | 无换行写入仍可用 |
| `submit` | 保留 | 带换行写入仍可用 |
| `close_stdin` | 保留 | EOF 语义仍可用 |
| `kill` | 保留 | 终止并physical join process；`terminal_monitor.cancel`是独立的只取消通知、不kill语义 |

这里最关键的事实是：`terminal_process`只负责即时操作；长期观察重新由`terminal_monitor`承担，没有把future wake伪装成poll重试。

### 4.4 `terminal_monitor`：Round 2已恢复的完整产品语义

hard-cut 前已经落地、Round 2重新闭合的语义包括：

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

当前实现由[`terminal_process/monitor.py`](src/pulsara_agent/terminal_process/monitor.py)持有process-local registration/draft/cursor/coalescing，由Host scheduler唯一构造prepared target，并通过provider safe point安装canonical observation。Coordinator不访问repository、不创建turn、不启动runner；Host close后所有monitor失效。`TerminalMonitor*` live event现在有真实producer，但live enum本身仍不承担产品authority。

### 4.5 Terminal 输出：retained tail、单次响应与 canonical result 三个边界

Round 2后的 Terminal 大输出受三个正交边界约束：

1. [`terminal_process/output.py`](src/pulsara_agent/terminal_process/output.py) 每process最多保留16 MiB sanitized UTF-8，Host aggregate最多128 MiB；淘汰推进retained start并使旧cursor返回typed GAP；
2. `terminal` / `terminal_process` 单次public response最多32,000 chars，monitor canonical envelope最多32,000 UTF-8 bytes，并用`COMPLETE | HEAD_TAIL`和available/included/omitted counts表达delivery coverage；
3. Round 1 tool-output processor把一次tool result冻结时的exact retained candidate转换为不超过65,536 UTF-8 bytes的inline canonical preview，并按需发布shared blob artifact。

artifact保存的是该次冻结时仍可证明的完整sanitized retained candidate；如果更早bytes已被retention淘汰，source coverage明确为`RETAINED_SNAPSHOT`，不得冒充原始完整process stream。delivery truncation也不得伪装成retention GAP。

下列产品语义已由Round 1与Round 2共同恢复：

- 一次冻结时仍在retention owner中的完整sanitized stdout/stderr被保留；
- bounded inline preview 与完整事实分离；
- huge output 使用稳定 head/tail preview；
- preview 明确声明 original size、omitted middle 与 read-more hint；
- `artifact_read` 按 offset/limit 继续读取完整输出；
- typed result能解释preview为何截断以及完整内容在哪里；
- terminal 与普通 tool output 使用一致的 artifact continuation 语义。

### 4.6 Terminal shell/profile/env 的产品恢复

hard-cut 前 [`TERMINAL_SHELL_ENV_V1_IMPLEMENTATION_PLAN.zh.md`](archived_docs/TERMINAL_SHELL_ENV_V1_IMPLEMENTATION_PLAN.zh.md) 对应的生产代码包含 `runtime/terminal/env.py` 与 `runtime/terminal/shell.py`，并有实际测试。主要用户可观察能力是：

- detect user shell；
- 受控捕获 login/interactive shell 的安全 environment snapshot；
- snapshot timeout、大小上限、TTL cache 与失败 fallback；
- default-deny environment name allowlist；
- secret name/value defense-in-depth；
- sane PATH 与用户工具链 PATH；
- 每条命令按 effective cwd 查找最近 `.venv/bin`；
- shell/env 来源与 fallback diagnostic。

当前[`terminal_process/environment.py`](src/pulsara_agent/terminal_process/environment.py)已经重新实现这组语义：login-shell probe受1 MiB、deadline和process-group physical join约束；成功snapshot按startup-file signature与TTL缓存；失败typed fallback；默认环境只采用inert allowlist并做secret-shaped value防线；最近`.venv/bin`与显式PATH prepend有确定顺序。通用exact-name passthrough是高权限显式配置，默认空；diagnostic只记录来源、错误码和计数，不记录env value。

### 4.7 Terminal cwd continuity 的恢复边界

hard-cut 前的 foreground cwd doctrine 是：

```text
terminal("cd src && pwd")
  -> command 完成并捕获 final cwd
  -> terminal session.current_cwd = <workspace>/src
  -> 下一条 terminal command 默认从 <workspace>/src 启动
```

background/yielded command 不允许回写 session cwd，以避免并发 command 竞争。

当前`TerminalSession.execute()`为每个foreground command创建private cwd probe，并且只在process及reader/watcher已完成、probe结果仍位于workspace且目录存在时推进`current_cwd`。yielded command立即放弃cwd推进权，完成后也不能回写；probe文件在成功、失败、yield与close路径全部清理。

### 4.8 明确不计为 Terminal 缺口的旧 durability

以下内容不属于本索引要求恢复的产品能力：

- yielded terminal process 跨 Host rebind/adopt；
- Host crash 后恢复原 OS process；
- durable monitor receipt graph；
- EventLog replay 恢复 monitor state machine；
- checkpoint/account/head join；
- delivery ACK/reconciliation machinery；
- Host 退出后继续持有 terminal process。

目标产品边界已经实现为：terminal process 与 monitor 都只活在当前 Host；Host close时先停止monitor admission、终止process group并bounded join reader/watcher/process，再释放repository/provider等依赖。没有跨Host rebind或execution replay。

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
- terminal 只对当前仍保留的 sanitized body 承诺 `RETAINED_SNAPSHOT`；rolling retention 丢失更早输出使用`TERMINAL_RETENTION_GAP`，sanitizer处理失败使用独立`TERMINAL_SANITIZER_UNAVAILABLE`，二者都不会被冒充为原始process stream完整。

对应机器证据见 [`round1_tool_output_artifact_activation.json`](benchmarks/suites/core/v1/round1_tool_output_artifact_activation.json)。Go artifact viewer/download UI、binary artifact、多 artifact result、artifact 删除/retention UI 与后台 retention retry job 仍是明确 non-goal；PHC-01、PHC-03 至 PHC-17 的状态不因本轮改变。

## 6. PHC-07：Long-horizon execution、context window 与 compaction

本项不再把“任务能持续工作”与“model-visible context如何换代”视为一个编码切片。当前恢复顺序冻结为：先用PHC-17建立统一的process-local compiled-context测量与source allocation边界；再由Round 5A删除错误层级的step/time保险丝；最后才由Round 5B在provider safe point生成/采用snapshot并重新调用同一个compiler。

### 6.0 两个实施切片

| 切片 | 拥有的产品语义 | 明确不拥有 |
|---|---|---|
| Round 5A：Long-horizon execution envelope | 正常ROOT/child turn无固定model/tool call上限；无turn总deadline；Round 3.1 dispatch planning保留单attempt总上界，foreground canonical transaction、writer renewal、provider、tool、Terminal decision与close按closed owner matrix使用各自watchdog；长循环继续满足append-only prefix | compaction、summary、snapshot adoption、context rebase、rollout account、finalization、128K/16K与256K/1M配置决策 |
| Round 5B：Long-horizon context compaction | active-context测量、safe-point compaction、single-turn continuation、protected tail、explicit continuity epoch rebase、manual/auto/reactive compact | 恢复旧EventLog/reducer/checkpoint/repair、删除canonical transcript、把累计token误当active context |

Round 5A实施规格见[`ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md`](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)，机器证据见[`round5_long_horizon_execution_envelope_activation.json`](benchmarks/suites/core/v1/round5_long_horizon_execution_envelope_activation.json)。PHC-07A已恢复；PHC-07B仍保持open，不能据此宣传自动compaction或跨context-window continuation。

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
- foreground runner在turn admission创建120秒deadline，并在normal canonical prepare/settlement间复用；provider/tool经过的wall time会使后续canonical操作拿到过期deadline；
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

其中固定24次model-call与runner总时限造成的task-progress失败由Round 5A负责；snapshot、thinning与context continuation由Round 5B负责。Round 5A完成后只能把PHC-07标成“execution envelope已恢复、compaction仍缺失”，不能宣传完整long-horizon context window已经恢复。

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

Round 6已把核心MCP能力接入当前conversation kernel：

- user、显式trusted workspace与Host override配置经typed merge进入Host-scoped supervisor；普通Host open默认把repository-owned workspace entry保持disabled，避免checkout即执行stdio command或解封HTTP secret reference；
- stdio与Streamable HTTP均通过唯一official-SDK facade进行bounded initialize、capability-aware discovery和调用；PUBLIC_ONLY HTTP pin验证后的actual address并保留Host/SNI，所有并发response共享slot-owned 32 MiB byte reservation；wire JSON在`json.loads`分配object graph前先经linear structural scanner，Host discovery reservation从`client.open()`前覆盖到normalized candidate安装；
- discovered descriptor、effect policy、slot lease与physical executor绑定同一semantic/physical generation；
- stdio与sessionful HTTP严格串行，只有proved-stateless HTTP可在Host policy上界内并行；
- `listChanged`立即关闭新dispatch，已有admission permit可drain，safe-point reconcile后才恢复；
- scope-filtered direct MCP tools、`MCP_CATALOG`、`list_mcp_servers`、静态或advertised-template resources、prompts读取和Round 1 artifact承接进入production；resource-template matcher线性、拒绝adjacent ambiguity并exact校验query/matrix变量名，remote tool identity用覆盖完整原名与physical generation的bounded domain-separated digest；
- CLI已提供`list/add/remove/enable/disable/doctor/reconnect`，standalone reconnect不会伪装成另一个活跃Host的控制面；
- V1 elicitation固定DISABLED；state-only `input_required`有界续接，human/Sampling/Roots/unknown均typed拒绝；unsupported input出现在external-effect调用后且没有terminal response时保留unknown outcome，绝不伪造known failure；
- supervisor是唯一physical close owner，runtime generation只持有opaque `McpSlotLease`；failure与close exact join slot identity，stdio EOF推进future reconnect，成功retire后删除physical重对象；Host replacement只fresh connect，不恢复旧request。

### 7.3 Round 6恢复范围与剩余缺口

已恢复的用户能力包括：

- enabled MCP server可被Agent发现和直接调用，不再阻止session open；
- server tool schema进入scope-filtered provider surface，permission只在local authorize执行，不按preset改写schema；
- resource/prompt/catalog标准读取、bounded artifact承接及CLI lifecycle诊断可用；
- semantic-identical physical reconnect不破坏provider-input prefix，schema变化通过dirty fence与safe-point rebase显式反映；
- builtin/MCP confirmation共享Host-wide bounded FIFO与单一visible interaction；ALLOW继续复用既有decision+attempt原子事务。

仍明确不属于Round 6 V1的能力是：form/private URL elicitation、OAuth、MCP-backed skill activation、server-initiated Sampling/Roots、Apps/Tasks与advanced Go MCP UI。`input_required`仅恢复bounded keyed state-only continuation；这些缺口不得通过普通字符串fallback或新的durable MCP owner伪装为已支持。

MCP Apps 与 Tasks 在旧 MCP2 文档中本来就是非目标，不列为 hard-cut 回归。

Round 6 V1冻结了一条克制边界：scope-filtered direct MCP tool surface、MCP_CATALOG与`list_mcp_servers`已经恢复，但“MCP-backed skill activation”没有恢复。当前skill resolver会在Host初始allowlist中删除未知tool引用；让late-ready MCP tool重新参与skill dependency resolution需要单独的capability/skill safe-point规格，不能作为MCP supervisor的隐含副作用。该项作为PHC-08的后续capability-integration子项保留，不阻塞direct MCP happy path，也不改变PHC-15当前只剩hierarchical task-graph dead descriptor的统计口径。

同理，hard-cut前确有form/private URL能力，但Round 6 V1因当前Protocol/Go只有ALLOW/DENY而明确不广告elicitation；MCP direct execution不会把secret form伪装成普通字符串。form/private URL仍是PHC-08未恢复的后续产品子项，不能因核心MCP activation而从本索引消失。

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

- 用户可在发送消息前选择`read-only / ask-permissions / accept-edits / bypass-permissions`四种preset；发送动作把选择与本次run一并冻结，之后调节只影响未来submission；
- 用户通过 `:plan` 进入规划；
- Agent 调用 `enter_plan` 主动进入规划；
- 每次run在message submission/admission线性化点冻结permission snapshot，run内所有tool authorization使用同一份不可变policy；
- Plan激活后，在snapshot冻结前对用户所选mode应用scoped read-only overlay，而不是依赖prompt自律；
- Agent 使用 `ask_plan_question` 向用户提出结构化问题；
- 回答回到原 tool call；
- Agent 使用 `exit_plan` 提交 plan draft；
- 用户 approve、revise 或 cancel；
- revise 后 Agent 继续规划并再次提交；
- 用户可 force-exit；
- 退出后只让下一次submission重新采用用户选择的mode，不在当前read-only run内放宽；
- plan interaction 有独立的有限预算。

主要归档证据是：

- [`PLAN_WORKFLOW_EVENT_ARCHITECTURE.zh.md`](archived_docs/PLAN_WORKFLOW_EVENT_ARCHITECTURE.zh.md)；
- [`PULSARA_RUN_BOUND_PERMISSION_MODE_PLAN.zh.md`](archived_docs/PULSARA_RUN_BOUND_PERMISSION_MODE_PLAN.zh.md)；
- hard-cut 前 `runtime/agent.py`、`host/session.py`、`runtime/plan.py` 与相应 tests。

### 8.2 Round 4 activation后的代码事实

- 四种preset仍由typed permission contract拥有；每个turn与queued prompt在admission处冻结requested/effective mode、overlay、Plan identity与contract fingerprint；
- active Plan对ROOT admission应用`PLAN_READ_ONLY`收窄，SUBAGENT_TASK只继承parent effective permission且不成为workflow成员；authorization、attempt与physical invoke exact join同一snapshot；
- `enter_plan`、`ask_plan_question`、`exit_plan`只在ROOT tool surface可达；包含Plan call的batch先执行全批barrier，所有siblings均不得产生physical attempt；
- `plan_workflows`与`plan_interactions`是唯一current Plan truth；question使用process-local dormant waiter，draft review是canonical异步decision；
- ENTER/REVISE/APPROVE由Host-owned continuation attempt完成canonical write、ACK confirmation、ROOT slot settlement与task bind；CANCEL不创建空白turn，只留下下一条真实prompt可认领一次的typed handoff；
- canonical reader在一个repeatable-read cut中同时冻结conversation、permission、workflow、handoff与approved-plan facts；Round 3 pure compiler只消费这些immutable facts；
- Python Protocol v3后端提供requested mode、current Plan control、true question-answer oneof、draft feedback presence及bounded content read；这些是已激活的wire/server authority边界；
- Go生成binding或局部consumer代码不等于Go/TUI产品闭环。permission selector、question/options/free-text交互、draft分页审阅、approve/revise/cancel控制、current-control重建及client-side exact-join均明确延期，不计入Round 4 Python activation；
- Legacy Python REPL的`:plan`/`:mode`命令仍不恢复；当前可验证入口是headless typed API与Python Protocol server，未来主要交互入口才是Go composer/TUI。

### 8.3 恢复后的用户能力

- 普通prompt可携带本次requested preset；提交后UI变化、queue delay或ACK-unknown不会重绑accepted snapshot；
- Agent可主动进入typed Plan，Runtime从下一turn开始强制read-only，而不是依赖prompt自律；
- structured question的answer成为exact canonical ToolResult并继续同一turn；
- draft支持APPROVE、REVISE、CANCEL，Host在前两者FULL后自动启动exact continuation；
- approved plan按content identity只物化/计量一次，implementation turn恢复workflow冻结的resume preset；
- Python Protocol/canonical层允许detach/reattach后重新发现并继续审阅；Host crash不恢复provider/future，但不丢失已提交draft decision truth。Go/TUI尚未被本轮认定为完成这项最终用户旅程；
- descriptor、schema、binding与executor在同一tool-surface snapshot中闭合，SUBAGENT_TASK看不到Plan tools。

### 8.4 send-time permission snapshot与Plan overlay的归属

这里不新增独立的permission PHC，也不能把所有permission能力都误报为丢失。当前四种preset、tool action classifier、ordinary confirmation及pre-dispatch allow/deny仍是有效代码；真正被hard-cut切掉的通用permission产品能力是：

> 用户在发送消息前选择本次run的permission preset；发送动作把消息与effective permission作为同一个稳定candidate冻结。

因此`per-run frozen permission snapshot`不是动态permission旁边的第二项产品能力，而是动态选择在run admission处的不可变体现。未来TUI的自然产品形状是在composer/input旁提供permission selector；selector本身可变，已提交run不可变：

```text
TUI composer selected_permission_mode          # 发送前可变
    + submitted user message

active Plan workflow
    -> PLAN_READ_ONLY narrowing overlay         # PHC-09拥有

submit / command acceptance linearization
    -> FrozenRunPermissionSnapshot              # 本次run的accepted form
    -> stable message + mode candidate
    -> typed pre-dispatch authorization
```

后续恢复必须保持以下归属：

- 动态mode选择不是Legacy Python REPL兼容项；Go TUI应在输入框旁提供selector，headless typed API则把requested mode作为prompt submission字段，而不是先发送一条自由文本或独立`:mode`命令；
- composer selector可以把上次选择作为下一次发送的便捷默认值，但它只是可变input state，不是active run的permission authority；workflow会冻结进入Plan时的`resume_permission_mode`，只用于后续automatic turn admission与Plan退出后的selector恢复；
- Plan read-only overlay属于PHC-09，因为它的安装、持续与撤销都由Plan lifecycle决定，不另立PHC-18；
- overlay只能收窄用户所选mode，不能扩大权限；Plan退出只能影响下一次new-turn admission（包括APPROVE的automatic implementation turn），不得让已经开始的read-only run中途获得写权限；
- permission snapshot必须与prompt submission在同一command candidate中冻结；queue中的每个prompt保留各自mode，后来调节composer selector不得改变已经排队的prompt；
- ACK-unknown只能exact-confirm同一message、requested mode与effective snapshot，不能使用重试时TUI当前选择重新绑定；
- 每次run在tool call/physical attempt之前使用同一份frozen effective policy；UI selection或Plan状态的后续变化不得追溯改写已经接受的run。prompt acceptance与Plan transition的canonical顺序决定overlay是否适用，Plan只约束其激活之后接受的submission；
- detach/attach后若canonical Plan仍active，新Host必须重新得到同一read-only overlay；这应读取canonical Plan state，而不是replay permission event；
- PHC-17只负责把Plan status/guidance编译进model input。是否允许physical effect仍由typed policy port决定，不能以system prompt代替；
- Agent `enter_plan`关闭origin turn并由Runtime原子创建read-only Plan continuation；question answer作为canonical ToolResult恢复同一个turn；`exit_plan`关闭origin turn并把draft review变成canonical异步decision；REVISE/APPROVE分别原子创建新的Plan/implementation turn，CANCEL则等待下一条真实human prompt取得一次性typed handoff；
- automatic continuation使用独立`PLAN_CONTINUATION` input origin，不伪造human `USER_MESSAGE`，也不能激活skill/capability。APPROVE后的第一次compile必须exact携带被批准的plan；CANCEL后的下一条真实prompt仍按用户发送前的selector冻结permission；
- question UI的“其他（以上选项都不合适）”是Go-owned free-text入口，不污染model options。提交前selection/draft可process-local；提交后的exact answer必须进入canonical conversation truth；
- 不恢复旧permission snapshot event、event reducer、RuntimeSession recovery或逐次permission receipt graph。Committed occurrence至多审计用户可观察的Plan/mode transition，不成为effective policy真源。

上述Python Runtime/Host、canonical、compiler与Protocol server ownership边界已按 [`ROUND_4_PLAN_WORKFLOW_AND_RUN_PERMISSION_IMPLEMENTATION_SPEC.zh.md`](ROUND_4_PLAN_WORKFLOW_AND_RUN_PERMISSION_IMPLEMENTATION_SPEC.zh.md) 激活。两张canonical Plan relation、每个turn/queue item的immutable permission snapshot、七类Plan occurrence与两个typed subject slot均已进入production；最终oracle为`34 Committed / 23 Live / 15 subjects / 2 guards / 26 relations / 4 jobs`，证据见 [`round4_plan_workflow_and_run_permission_activation.json`](benchmarks/suites/core/v1/round4_plan_workflow_and_run_permission_activation.json)。该activation不证明Go/TUI产品行为已经完成，也不把Go端未审阅项降格为Python blocker。

### 8.5 明确延期的Go/TUI闭环

Round 4刻意停在“Python authority与typed wire已经可用”，没有把客户端实现量计入PHC-09的Python闭环。后续Go/TUI工作至少包括：

- composer输入框旁的permission preset selector；选择在发送前可变，发送后展示canonical requested/effective snapshot；
- 从canonical snapshot/current-control发现active workflow与open interaction，而不是依赖一次性live通知；
- question的typed options、recommended标记与UI-owned“其他”free-text分支；不得伪造第四个model option；
- 按UTF-8 body identity分页读取并安全渲染plan draft，校验digest、offset连续性与content identity；
- `APPROVE / REVISE / CANCEL` closed review UX，以及idle `ENTER_PLAN / CANCEL_PLAN / FORCE_EXIT_PLAN`控制；
- ACK-unknown、detach/reattach、live/committed GAP后的command winner与current-control重建；
- 所有question、option、feedback和draft正文统一经过terminal-safe public-text transform；
- client-side Plan question fingerprint recomputation与control projection exact join。

这些是PHC-16拥有的客户端产品工作，不得反向推动Python恢复Legacy REPL、第二套Inspector、durable UI receipt或event replay。Go/TUI完成前，不能宣传“普通终端用户已获得完整Plan UX”；但Python Plan workflow、permission authority与headless Protocol能力仍保持已恢复状态。

### 8.6 hard-cut前Plan与permission参考代码

- `5b7ad9f7:src/pulsara_agent/tools/builtins/plan.py`：`EnterPlanTool`、`AskPlanQuestionTool`、`ExitPlanTool`三项模型工具的最小公开面。
- `5b7ad9f7:src/pulsara_agent/runtime/plan.py`：Plan instruction、question options、active/read-only状态、pending question/exit view与approve/revise/cancel语义；旧`reduce_plan_workflow_state(events)`不能作为新state owner。
- `5b7ad9f7:src/pulsara_agent/runtime/permission.py`与`5b7ad9f7:src/pulsara_agent/runtime/permission_snapshot.py`：Plan进入后run-bound read-only policy、退出后恢复default policy，而不在当前run内放宽权限。
- `5b7ad9f7:src/pulsara_agent/host/session.py::set_permission_mode()`、`effective_next_run_permission_mode`与`current_permission_mode`：会话default、Plan overlay和当前/下一run视图的旧产品语义；只参考边界，不恢复Host-held durability graph。
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
- `:mode`命令本身不恢复，但“会话内在run boundary动态切换后续permission preset”的产品语义保留在PHC-09的横切permission边界中，未来由Go TUI或typed API承接；
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

Round 7已在当前Kernel上恢复该语义：canonical reader在同一个
`REPEATABLE READ` cut中选择same-scope immediate predecessor，并冻结bounded、脱敏的
`PREVIOUS_TURN_OUTCOME` fact；compiler只追加typed runtime observation。user stop、
Runtime/provider failure、Host close/takeover、resource boundary与unknown interruption使用closed
分类；成功successor使更早failure不再成为current guidance。no-attempt与attempt-without-result分别表达
“未dispatch”与“effect可能unknown”，raw exception、arguments、private URL和transport detail均不进入
provider正文。late exact result只在首个覆盖它的新cut中追加修正，不修改已安装prefix。

该恢复没有引入coroutine/execution replay、failure-note relation、receipt、checkpoint或repair owner；
tool-call closure继续只负责native provider pairing，previous-turn source只负责turn-level解释。

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

Round 7已把该语义收回existing `tool_results` relation：`observed_at`、单一monotonic
clock产生的observation duration、exact executor binding决定的immutable origin，以及optional trusted
tool-reported duration均在result acceptance时冻结。provider-visible tool result使用独立closed outer
observation envelope，remote/tool body只能作为string data，不能伪造timing。

freshness不再写回历史result。每个新turn只追加一个小型frontier，将result的stable
`source_turn_ref`映射为CURRENT_TURN、PREVIOUS_TURN_TAIL或HISTORICAL；因此同Host、同scope、同epoch的
SYSTEM/tools保持不变，messages严格等于旧prefix或只追加suffix。compaction后的rebase语义仍属于Round
5B，本轮没有建立跨Hostprefix恢复承诺。

### 13.1 hard-cut前tool timing参考代码

- `5b7ad9f7:src/pulsara_agent/primitives/tool_observation.py`：`ToolObservationTimingFact`的UTC、duration和closed freshness vocabulary，是最小typed语义参考。
- `5b7ad9f7:src/pulsara_agent/runtime/tool_executor.py`：`build_tool_observation_timing()`、`synthetic_tool_observation_timing()`与`_tool_observation_freshness()`展示统一构造点。
- `5b7ad9f7:src/pulsara_agent/capability/result_semantics.py`与`5b7ad9f7:src/pulsara_agent/ports/tool_result_semantics.py`：tool-specific结果如何携带统一timing，而不信任任意tool body伪造metadata。
- `5b7ad9f7:src/pulsara_agent/runtime/context_input/render.py`：provider-visible `observed_at/duration/freshness`的bounded render；新实现应从canonical tool result/attempt timestamps生成typed projection，不恢复`ToolResultEndEvent`作为真源。
- Terminal producer参考：`5b7ad9f7:src/pulsara_agent/tools/builtins/terminal.py::terminal_timing_payload`及`5b7ad9f7:src/pulsara_agent/tools/builtins/terminal_process.py`的`background_process_observation`分类。
- 关键回归：`5b7ad9f7:tests/test_event_message_system.py::test_tool_observation_timing_requires_utc_iso_and_non_negative_duration`、`5b7ad9f7:tests/test_context_input_facts.py::test_tool_observation_timing_normalizes_utc_and_rejects_negative`、`5b7ad9f7:tests/test_context_transcript_projection.py::test_tool_body_cannot_forge_observation_timing_inclusion`、`5b7ad9f7:tests/test_tools.py::test_terminal_result_timing_is_shared_by_payload_metadata_and_artifact`。

## 14. PHC-15：Capability catalog 与真实 executor 不一致

当前 builtin catalog 共29个descriptor。Round 4后production model tool surface实际可达24个：

- 9个direct tools：`artifact_read/read_file/search_files/edit_file/write_file/todo/terminal/terminal_process/terminal_monitor`；
- 4 个 flat subagent tools：`spawn_agent/list_agents/wait_agent/stop_agent`；
- 8 个 current memory tools（memory 语义不在本轮复核范围）。
- 3个ROOT-only Plan control tools：`enter_plan/ask_plan_question/exit_plan`。

以下5个descriptor没有当前production executor/binding：

```text
create_agent_tasks
wait_agent_tasks
stop_agent_task
report_agent_phase
report_agent_result
```

Round 1已把`artifact_read`descriptor接到scoped production executor；Round 2已让`terminal_monitor`descriptor、tool-action policy、strict schema与production executor闭合；Round 4又让三项Plan descriptor通过scope-frozen tool surface与Runtime control executor闭合。PHC-15仍未整体恢复，只因为PHC-10的五项hierarchical task-graph descriptor尚不可达。

这不是独立用户功能，但它是重要产品缺失证据：

- 静态 descriptor inventory 不能代表实际可用能力；
- tests 若只检查 descriptor/schema，会漏掉 production binding 缺失；
- dead descriptor 使 hard-cut 后的真实产品面难以判断；
- 剩余task-graph缺失仍可能被残留descriptor掩盖。

### 14.1 hard-cut前catalog/executor闭合参考代码

- `5b7ad9f7:src/pulsara_agent/capability/builtin_catalog.py`：descriptor、binding kind、availability requirement、permission contract及tool family closed sets。
- `5b7ad9f7:src/pulsara_agent/runtime/tool_composition.py`：`build_runtime_tool_composition_input()`、`build_runtime_tool_binding_installation()`、`_builtin_tools()`和`_validate_catalog_binding_kinds()`；它明确区分“descriptor存在”与“真实executor安装”。
- `5b7ad9f7:src/pulsara_agent/capability/exposure.py::build_exposure_plan`：direct/deferred/hidden/unavailable/callable disposition。
- `5b7ad9f7:src/pulsara_agent/tools/registry.py::ToolRegistry`与`5b7ad9f7:src/pulsara_agent/runtime/tool_executor.py::ToolExecutor`：最终name-to-executor lookup及unknown tool fail-closed。
- 关键回归：`5b7ad9f7:tests/test_capability_surface.py::test_exposure_plan_hides_direct_descriptor_without_execution_binding`、`5b7ad9f7:tests/test_capability_surface.py::test_exposure_plan_diagnoses_non_direct_descriptor_without_execution_binding`、`5b7ad9f7:tests/test_capability_surface.py::test_builtin_provider_uses_explicit_descriptor_truth_for_bound_core_tools`及同文件workflow-control missing descriptor测试。
- 不应恢复`5b7ad9f7:src/pulsara_agent/runtime/session_run_capabilities.py`的旧`RuntimeSession`port graph。目标guard应直接证明：任何advertised callable descriptor都有当前Host generation中的exact production binding；不可达descriptor必须被typed标为unavailable或从surface移除。

## 15. PHC-16：Go TUI作为未来主要产品面（当前延后）

用户已明确：当前优先修复Python Agent Runtime内核，Go端TUI S1–S3能力可以先搁置；同时，未来不再恢复Legacy Python REPL或standalone Canonical Inspector，Go TUI将成为主要的会话观察、交互和控制产品面。因此本节当前只登记UI缺口，不把它们混入Runtime恢复主线，但其长期地位不是可选客户端。

根据根目录 TUI 文档、当前 `clients/terminal`以及Round 4的Python-only activation口径，至少以下能力不在已验收的production Go client主路径中，或只有未纳入本轮闭环的局部代码/生成binding：

- bounded composer undo/redo；
- multiline command-history scratch 与对称 Up/Down traversal；
- large bracketed-paste review、head/omission/tail 展示与显式确认；
- selection/copy 与 copy-last/copy current materialization；
- warning/failure sticky notice 与显式 dismiss；
- permission selector、active Plan/current interaction重建、question options/free-text、plan draft分页审阅及approve/revise/cancel/force-exit UI；
- MCP form/private URL/secret interaction UI；
- richer semantic activity cells 与 terminal monitor activity。

当前 Go client 已保留 Protocol v3 snapshot/history/live/content read 的基本能力，Round 4也生成了新的wire类型并存在局部consumer改动，因此本节不能写成“TUI 全部丢失”。但这些代码本轮未接受Go端产品审查，不能据此把selector、Plan交互、renderer safety或reconnect exact-join标记为完成。这里只记录hard-cut前文档已冻结、当前尚未验收的高层UX。

这一方向不要求现在提前实现完整Go UI，但要求Python Kernel恢复产品能力时提供稳定的typed command、canonical observation、live event与bounded content read契约。不得为了方便TUI而让客户端从raw row猜测实时语义；也不得为TUI恢复universal EventLog、execution replay或独立Inspector projection。

### 15.1 hard-cut前Go TUI参考代码

- `5b7ad9f7:clients/terminal/internal/components/composer/model.go`、`5b7ad9f7:clients/terminal/internal/components/composer/update.go`、`5b7ad9f7:clients/terminal/internal/components/composer/history.go`、`5b7ad9f7:clients/terminal/internal/components/composer/paste.go`、`5b7ad9f7:clients/terminal/internal/components/composer/view.go`：grapheme-safe edit、bounded undo/redo、对称history traversal、fresh scratch preservation和large-paste review。
- `5b7ad9f7:clients/terminal/internal/components/notification/model.go`与`5b7ad9f7:clients/terminal/internal/components/notification/view.go`：bounded notification、sticky warning/failure、dismiss和expiry generation。
- `5b7ad9f7:clients/terminal/internal/components/transcript/model.go`、`5b7ad9f7:clients/terminal/internal/components/transcript/wrap_cache.go`与`5b7ad9f7:clients/terminal/internal/components/transcript/view.go`：server order、CJK/emoji display width、scroll anchor、follow-tail和unseen count。
- `5b7ad9f7:clients/terminal/internal/app/input.go`、`5b7ad9f7:clients/terminal/internal/app/keymap.go`、`5b7ad9f7:clients/terminal/internal/app/update.go`：closed keyboard/paste/mouse input、command freeze-before-I/O、clipboard和late receipt不清除新draft。
- `5b7ad9f7:clients/terminal/internal/interaction/state.go`：pending interaction的typed phase/view identity；未来Plan/MCP UI应基于Protocol v3的新typed control，而不是复用v2 carrier。
- 关键回归：`5b7ad9f7:clients/terminal/internal/components/composer/model_test.go`、`5b7ad9f7:clients/terminal/internal/components/notification/model_test.go`、`5b7ad9f7:clients/terminal/internal/components/transcript/view_test.go`、`5b7ad9f7:clients/terminal/internal/app/input_test.go`和`5b7ad9f7:clients/terminal/internal/app/s3_command_test.go`。
- 禁止照搬：旧`terminal_client.proto`/Protocol v2、durable presentation cache、command receipt/reconciliation及三平面旧projection。可复用的是纯Go UX state machine与显示性质；wire identity、snapshot/GAP和command ACK必须重新绑定当前Protocol v3。

## 16. PHC-17：Structured model-input / context compilation

PHC-17不是“把system prompt写得更长”，也不是PHC-07 compaction的别名。它回答的是每次真实model call之前的共同产品边界：

> 基于exact canonical conversation cut与当前已授权的process-local/domain facts，决定哪些事实以什么顺序、channel和有界表示进入本次provider input。

[前后代码确认] hard-cut前这是一条已经接入真实Agent model loop的production路径；hard-cut曾只保留canonical transcript rematerialization、最终provider materialization和一个很薄的skill prompt composer。Round 3已在当前canonical Kernel上重新建立provider-neutral typed compiler，而没有恢复旧durable context-input audit、provider-input replay或execution recovery graph；机器证据见[`round3_structured_model_input_compiler_activation.json`](benchmarks/suites/core/v1/round3_structured_model_input_compiler_activation.json)。Round 3.1随后恢复同一Host、同一exact scope内的append-only source/tool-result lifecycle，机器证据见[`round3_1_provider_input_prefix_continuity_activation.json`](benchmarks/suites/core/v1/round3_1_provider_input_prefix_continuity_activation.json)。PHC-17现为**完整恢复**；跨Host prefix restore与exact historical request replay仍是明确不承诺的减法边界，不计缺口。

### 16.1 必须分开的七个边界

| 边界 | 当前代码事实 | 本索引判断 |
|---|---|---|
| exact canonical conversation cut | [reader.py](src/pulsara_agent/conversation_kernel/reader.py)在repeatable-read transaction中读取binding revision、entry cut、scope、blocks、tool attempt/result与blob content，并冻结provider-neutral immutable facts | **唯一conversation input truth owner** |
| transcript/provider lowering | [lowering.py](src/pulsara_agent/model_input/lowering.py)只消费frozen canonical facts，保持user/assistant/tool顺序、tool pairing、provider-only unknown closure与late outcome；assistant parent manifest不再泄漏正文 | **已恢复并绑定canonical reader** |
| provider materialization与最终估算 | [direct_model.py](src/pulsara_agent/conversation_kernel/direct_model.py)保留transport-bearing `PreparedKernelModelCall`，exact join后才thaw一次性`LLMContext`并执行最终validation | **compiler estimate与pre-send estimate exact equal** |
| first-party context source | [context_sources.py](src/pulsara_agent/conversation_kernel/context_sources.py)当前独立产生BASE_SYSTEM、RUNTIME_ENVIRONMENT、RUNTIME_CLOCK、RUN_PERMISSION、PLAN_HANDOFF、PLAN_WORKFLOW、CAPABILITY_CATALOG与ACTIVE_SKILL；environment/clock来自一次temporal capture，permission/Plan来自同一canonical compile snapshot | **Round 3五类基础source与Round 4三类workflow source均已接入** |
| typed multi-source allocation | [compiler.py](src/pulsara_agent/model_input/compiler.py)拥有channel、placement、degradation priority、budget class、render mode、physical bounds、deterministic degrade/omit与bounded report | **PHC-17核心能力已恢复** |
| process-local prefix lifecycle | Host持有一个ROOT epoch及每个active child各自的epoch；dynamic source形成causal runtime-observation suffix，旧canonical item与旧tool-result render保持冻结；steer planning复用prefix estimate、执行cooperative deadline与累计工作量上限；Plan handoff occurrence使用exact transition/carrier identity；adapter-final执行strict-prefix proof | **Round 3.1已恢复；仍不持久化prefix或provider remote state** |
| exact historical request audit/replay | 不保存每次dispatch的逐byte compiled input，也不靠其reopen | **既定减法，不计缺口** |

Round 3恢复的不是一个更长的prompt builder，而是provider payload构造之前的typed、可预算、可组合且不创造durable真值的编译边界。Tool surface以process-local access/borrow把同一descriptor/schema/executor binding闭合到authorize、attempt acceptance与physical invoke；binding漂移typed fail，不按同名tool切换executor。Round 3.1也不能建立第二个compiler；它只给这条现有路径增加process-local append lifecycle。

### 16.2 hard-cut 前已存在的production能力

[`PULSARA_CONTEXT_COMPILER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_CONTEXT_COMPILER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md)明确标记`C0–C5已实施并通过hard-cut验收`；[`PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md)也标记ContextSource ownership与incremental ProviderInput generation已完成production hard cut。它们不是只有对象草图的未来设计。

hard-cut前`5b7ad9f7:src/pulsara_agent/runtime/agent.py`在真实model-call preparation中调用`compile_context_from_facts()`。生产输入路径已经具备：

- immutable `ContextFactSnapshot`、normalized transcript与typed tool-result render units；
- `ContextSource` closed registry与source-owned candidate；
- `system / leading_user / history / current_user / current_run_tail / tool_context / handoff_hint` channels；
- `must_keep / important / optional / debug` budget classes；
- `full / compact / summary / ref_only / omitted` render modes；
- current user必须保留、assistant tool call/result pairing不得被普通section打断；
- tool schema、system、non-transcript source、message envelope与transcript共同计入最终input budget；
- 在预算压力下按priority和render policy确定性降级或省略，而不是先构造全部字符串、最后整体失败；
- 每个最终section保留source、lifecycle、dependency fingerprint、render decision、token estimate与provenance，供process diagnostics和当时的Inspector使用。

production source builder至少实际处理过：

- base system instruction；
- runtime environment：workspace identity/kind、model-visible workspace root、terminal current cwd、session timezone；
- runtime clock：current date与observed time；
- capability catalog与active skill；
- memory scope instruction与memory projection；
- plan status/guidance；
- rollout status与被选中的subagent result。

这里需要保留的是“不同事实有typed source owner，并由一个compiler统一lower”的产品边界，不是旧source必须一次性全部恢复。Memory仍按用户要求进入后续专项；Plan、MCP、failure note、tool timing和long-horizon也分别由自己的PHC定义产品真值。

### 16.3 当前production路径与恢复结果

当前model-call路径已经hard-cut为：

```text
ProviderSafePoint.freeze exact canonical cut
    -> CanonicalProviderInputReader.read_frozen_snapshot
    -> PreparedKernelToolSurface(scope-filtered descriptor/schema/binding)
    -> KernelContextSourceCollector.freeze one causal source cut
    -> StructuredModelInputCompiler.compile against EMPTY/INSTALLED predecessor
    -> FrozenCompiledModelInput + process-local surface borrow
    -> DirectKernelModelPort.preflight
    -> Host continuity owner compare-and-swap
    -> exact join + ephemeral thaw + open_once
```

该路径现已兑现：

- ROOT与SUBAGENT_TASK共用同一compiler；child surface不advertise ROOT-only `terminal_monitor`；
- 八类first-party fact均为独立typed source；只有稳定`BASE_SYSTEM`保留在SYSTEM root，environment、clock、permission、Plan handoff/workflow、catalog与active skill均按closed lifecycle成为causally appended runtime observation；
- environment与clock只从一次`RuntimeTemporalCapture`派生，Terminal cwd通过窄snapshot port读取；
- 只有最后一个ROOT human prompt可做textual skill activation；SUBAGENT objective、Terminal observation、tool result、clock与runtime source都没有该authority；
- current user、history order、tool pairing、closure与late result由canonical reader保护；compiler只允许对尚未安装的新suffix降级，已安装prefix与旧tool-result表示不再重写；
- tool schema与canonical tool-call arguments使用递归冻结JSON；compiler输出之后的原对象修改不能改变dispatch；
- compiler在provider open前检查source、variant、aggregate、schema、diagnostic、tool-result和64 MiB logical working-set上界；token budget由resolved target estimator决定；
- public operational observation只含closed code、count、mode与opaque fingerprint，不保存full prompt、path、tool argument或secret；
- optional source/hook失败不否定canonical事实；protected input或fixed prefix超限则typed fail且provider open count为0；
- DirectModel按`preflight -> continuity CAS -> open_once`执行；CAS之前完成全部validation，失败不会打开试探性provider stream；
- busy期间`Enter`向exact active ROOT提交steer，`Tab`排队future `NEW_TURN`；同lane FIFO、cross-lane不互锁，accepted steer按三重quote消费最长FIFO前缀并只触发一次后续model call；
- prompt与steer的稳定candidate均使用`FULL | NONE | CONFLICT`确认ACK-unknown；steer consume FULL后只重读canonical exact join并提升同一precompiled input。

Round 3.1的本地adapter-final回归现已证明：同一Host、同一exact scope、同一epoch连续调用的SYSTEM与tools逐项相等，messages要么相等、要么只追加suffix；clock、cwd、permission、Plan、catalog与skill变化不会改写前序。真实provider dogfood也观察到cached input，但remote比例只受provider cohort、TTL与服务端策略影响，继续只作operational observation，不替代本地strict-prefix正确性门控。

### 16.4 已恢复的产品影响与仍开放边界

PHC-17恢复后：

- 模型通过正式contract获得workspace root/kind、Terminal cwd与本地日期/时区；
- capability catalog、active skill与其他source具有统一channel、placement、budget与deterministic degradation；
- Round 1 tool-result artifact可在`COMPACT/REF_ONLY`中继续引导`artifact_read`，不改写canonical result或重跑effect；
- provider adapter不再决定source与预算；Responses和Chat Completions共享同一个frozen compiled truth；
- future PHC-07/08/09/13/14获得共同接入层，不再需要各自拼接隐式prompt。

以上单次compile与prefix continuity现已共同闭合。“模型稳定获得source”同时表示每call得到合法事实，并且同epoch内preceding provider-visible prefix不被后续动态事实或预算决策改写。

这些future source的domain truth仍未恢复，不能由PHC-17代替：

本项与其他PHC有依赖但不重叠：

- PHC-07拥有snapshot生成/采用与长程继续能力；PHC-17只编译当前已被binding revision选中的canonical base和delta；
- PHC-13拥有上一turn失败事实与其产品文案；PHC-17只提供该事实进入model input的typed source位置；
- PHC-14拥有tool timing/freshness的推导语义；PHC-17只负责有界lowering与预算；
- PHC-08/09/10各自拥有MCP、Plan、subagent domain truth；PHC-17不得替它们创造或判断事实。
- PHC-09拥有Plan-scoped read-only overlay；send-time frozen permission snapshot是用户逐run动态选择经过该overlay收窄后的accepted form；PHC-17只呈现Plan guidance，不能授权、拒绝或恢复physical effect权限。

### 16.5 Round 3 / 3.1 activation与后续架构边界

Round 3与Round 3.1已完整激活PHC-17，并保持其他model-visible能力只能按以下依赖方向接入：

```text
canonical/domain facts
    -> process-local typed context sources
    -> bounded provider-neutral compilation
    -> provider adapter materialization

compaction / Plan / MCP / failure note / timing
    -> 各自拥有事实
    -> 通过上述source boundary进入模型
```

后续实施规格必须继续遵守减法架构：

- canonical transcript与exact cut继续由当前reader/repository拥有，不恢复旧EventLog transcript projector；
- compiler工作树、section allocation和最终request builder只在当前process/model call内存在；
- 不新增`ContextCompiled` committed event，不保存完整prompt，不恢复plan/pages/root、append receipt、generation recovery或historical exact replay；
- compile diagnostic默认operational-only、bounded且可redact，丢失不得阻塞canonical commit或reopen；
- source adapter只投影其domain已经接受的事实，不能成为Plan、MCP、memory、tool或terminal的第二authority；
- ordinary hook、TUI或Inspector失败不能否定compile或provider call；若未来需要inspect，只读取bounded operational report或重新从canonical facts计算；
- 恢复typed compiler本身不要求增加Committed/Live event、subject slot、append guard、table或durable job；若后续某个独立产品transition确需新增event，应由对应PHC单独论证。

Round 3的具体DTO、target estimator、source registry、physical bounds与activation gate由[`ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md`](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)冻结。Round 3.1的Host-scoped epoch、source lifecycle、suffix-only degradation、dispatch linearization及cold-reopen边界由[`ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md`](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)冻结。任何future source只能投影其domain owner已经接受的事实，不得把compiler扩成durable input authority、第二个conversation reader或execution recovery graph。

### 16.6 hard-cut前Context Compiler参考代码

- `5b7ad9f7:src/pulsara_agent/runtime/context_input/compiler.py`：`compile_context_from_facts()`、transcript lowering、section allocation、budget/degrade、provider source lowering与final `CompiledContext`；提取纯编译语义，不移植audit/replay依赖。
- `5b7ad9f7:src/pulsara_agent/runtime/context_engine/types.py`：`ContextChannel`、`ContextBudgetClass`、`ContextRenderMode`、`AllocatedContextSection`、`CompiledContextSection`、`ContextBudgetReport`与`CompiledContext`对象边界。
- `5b7ad9f7:src/pulsara_agent/runtime/context_input/sources/builder.py`：system、runtime environment/clock、capability、active skill、memory、plan、rollout与subagent result的source adapter语义。
- `5b7ad9f7:src/pulsara_agent/runtime/context_input/sources/registry.py`与`sources/input.py`：closed source binding、typed input和producer/source policy分离；只参考type/ownership方向，不恢复EventLog authority slice。
- `5b7ad9f7:src/pulsara_agent/runtime/context_input/provider_projection.py`与`transcript.py`：current user、prior history、current-run tail与tool pairing的旧纯语义；当前canonical reader已经覆盖的部分不得重复实现。
- production接线参考：`5b7ad9f7:src/pulsara_agent/runtime/agent.py`中两次`compile_context_from_facts()`调用，以及`runtime/context_input/live.py::prepare_live_context_snapshot()`；只能抽取model-call前的事实准备与编译顺序。
- 关键回归：`5b7ad9f7:tests/test_agent_runtime_loop.py::test_agent_runtime_builds_immutable_context_input_before_compile`验证workspace root与current date真实进入provider context；`5b7ad9f7:tests/test_provider_input_hard_cut.py::test_system_prompt_retains_per_source_fragment_ownership`验证source ownership；同文件`test_compiler_omission_is_final_provider_payload_truth`验证预算省略与最终wire一致；`5b7ad9f7:tests/test_context_candidates.py`覆盖candidate lifecycle、forgery rejection和bounded cache。
- 禁止照搬：`runtime/context_input/audit_*`、`commit.py`、`replay.py`、`event_slice.py`，以及`runtime/provider_input/`中的generation store、continuation/recovery、resident vector和exact audit graph。它们承载的是已删除durability/recovery承诺，不是PHC-17恢复条件。

### 16.7 Round 3.1：Process-local provider-input prefix continuity

Round 3.1恢复的产品不变量是：

```text
same Host + same exact ROOT/SUBAGENT_TASK scope + same epoch

system[n + 1] == system[n]
tools[n + 1]  == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
```

它采用以下最小边界：

- continuity owner由Host持有，跨同Host的ROOT turn/automatic continuation复用；每个subagent task单独持有；
- 只有stable `BASE_SYSTEM`留在SYSTEM root；clock、environment、permission、Plan、catalog和active skill改为causally appended typed user-role runtime observation；
- runtime observation使用closed canonical JSON codec，source VALUE/clear/unavailable/no-op及call/turn/one-shot lifecycle为closed contract；已有stateful head变化时必须安装minimum replacement/invalidation或fail，不能让旧状态悄悄残留；
- configured skill在ROOT/child每个新turn重算，textual activation只来自exact ROOT human prompt，不能泄漏进Plan/Terminal等non-human continuation；
- cold/reset/compatible路径都使用explicit process-local dispatch anchor，不从全history猜最后一个user；同一safe point的多项steer以bounded FIFO accepted-entry batch交付，latest steer是唯一dispatch/textual-skill anchor；
- busy输入采用双入口：`Enter`向exact active ROOT turn提交steer，显式`Tab`排队future `NEW_TURN`；两种既有delivery mode各自在自己的lane内FIFO，**已accepted rows**的current-turn steer不会被future turn互锁，session-wide 128项admission cap仍可typed拒绝新输入；
- prompt ingress必须以完整semantic candidate确认compatible command winner；steer consumption使用stable entry candidate与canonical row/event `FULL | NONE | CONFLICT` confirmation，不能因commit ACK unknown留下RUNNING turn而无physical runner；
- steer quote必须在canonical mutation前冻结base cut、EMPTY/INSTALLED predecessor、target、pinned tool surface与one-cut sources；多项steer保持独立canonical/provider user messages并共享一次后续model call，但每个safe point只消费同时满足item count、canonical UTF-8 bytes与resulting epoch/target quote的FIFO最长前缀，FULL后只exact join并提升同一precompiled input；
- pre-first-call steer以EMPTY predecessor与initial prompt共同形成一次call；single head无法容纳时，queue rejection、turn interruption、`PromptRejected`与`TurnInterrupted`由一个Host-writer transaction原子接受；
- canonical reader的新item只lower一次，old tool-result render decision进入dispatch后冻结；
- DirectModel必须显式`preflight -> continuity CAS -> open_once`；CAS permit由Host continuity owner以exact object identity密封签发，same-shape caller DTO不能打开transport；同一tool-surface borrow覆盖provider response、assistant acceptance和该响应的完整tool/result settlement；
- first-party source registry的每个kind在compiler入口必须exactly one `VALUE | ABSENT`，ABSENT的contract/trust/budget/placement/degradation/lifecycle/disposition由closed binding重验；steer longest-first planning共享immutable base并只对unique base/suffix work计量，不能因重复试探较长prefix而在到达合法短前缀前虚假耗尽；
- append planning input在compile前冻结，完整candidate只在compile后注册；initial/successor的所有pre-install失败分别回到EMPTY/old INSTALLED，不允许stuck PREPARED；
- compiler只对new suffix做budget degradation；fixed prefix超过预算时等待PHC-07显式rewrite或在provider open前typed fail；
- 合法reset只包括cold Host bootstrap、root/tool/model/provider-lowering变化和未来accepted context-binding rewrite；cache miss、turn、clock、permission与Plan变化不是reset理由；
- 同一Host内客户端detach/reattach保持epoch；Host replacement后即使attach同一session也从canonical rows cold start，future session fork默认cold start；
- 不新增表、event、job、guard、subject、blob或Protocol类型，Round 4 oracle保持`34/23/15/2/26/4`。

这不是旧`ProviderInputGeneration`换名。hard-cut前约1.5万行provider-input primitive/package同时承担exact replay、Inspector、restart recovery、persistent vector、prepared abandonment和resident restore；Round 3.1明确排除这些职责。provider cache仍是best effort，hard correctness gate是本地adapter-final strict prefix，cached usage只作redacted dogfood evidence。

## 17. archived_docs 标题覆盖与结论

本轮扫描了 [`archived_docs/`](archived_docs/) 下全部 116 个 Markdown 文件的标题。以下按产品族归类，目的是说明哪些标题触发了进一步代码核对，以及最终是否形成缺口。

### 17.1 Terminal / tool output 标题族

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

### 17.2 Long-horizon / context 标题族

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

结论：Context Compiler Input与ContextSource文档有明确历史产品证据，`5b7ad9f7`也存在真实production owner和tests，因而形成PHC-17；Round 3已在canonical Kernel上恢复单次typed compilation，Round 3.1继续恢复同Host同scope的append-only prefix lifecycle。long-horizon/compaction与tool timing仍分别形成PHC-07/PHC-14。Exact context-input audit及跨Hostprefix recovery已明确不承诺，不计缺口；provider cache accounting仍只是operational observation，不替代typed compiler或本地strict-prefix proof；Context Timing Header只有计划证据，未单列。

### 17.3 MCP 标题族

重点标题：

- `PULSARA_MCP_2026_07_28_AND_SDK_V2_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_CLI_MCP_CAPABILITY_NEXT_IMPLEMENTATION`；
- `PULSARA_MCP_TANZO_GAP_ANALYSIS`；
- `PULSARA_MCP_STARTUP_LATENCY_NOTE`。

结论：MCP2 明确完成且当前整个 execution adapter 不存在，形成 PHC-08。Apps/Tasks 是旧文档明确非目标，不列缺口。

### 17.4 Plan / permission / approval 标题族

重点标题：

- `PLAN_WORKFLOW_EVENT_ARCHITECTURE`；
- `PULSARA_RUN_BOUND_PERMISSION_MODE_PLAN`；
- `STEP4_CONVERSATIONAL_MODE_SWITCH_PLAN`；
- `LIGHTWEIGHT_PERMISSION_SYSTEM_V1_IMPLEMENTATION`；
- `PERMISSION_PR4_ASK_ON_REQUEST_IMPLEMENTATION`；
- `APPROVAL_RESUME_V1_IMPLEMENTATION`；
- `HOST_USER_STOP_V1_IMPLEMENTATION`。

结论：PHC-09已由Round 4恢复，不另立permission PHC。四种preset、逐run选择与immutable snapshot、Plan-scoped read-only overlay、question/draft lifecycle及automatic continuation已在canonical Kernel中闭合。REPL命令兼容仍明确退役；Go TUI仅承接最小selector与review交互，Python继续拥有permission与Plan authority。

### 17.5 Subagent 标题族

重点标题：

- `PULSARA_SUBAGENT_GRAPH_REDUCER_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_SUBAGENT_SYSTEM_NEXT_STEPS`；
- `PULSARA_SUBAGENT_RUNTIME_PRIOR_ART_RESEARCH`；
- `PULSARA_SUBAGENT_DENO_WORKFLOW_RUNTIME_PLAN`。

结论：flat child 保留；batch/dependency/task reporting 有代码证据并形成 PHC-10。Deno WorkflowScript 的更大设计没有完成证据，不计为被删产品。

### 17.6 Capability / skills / filesystem 标题族

重点标题：

- `CAPABILITY_SKILL_RUNTIME_V1_IMPLEMENTATION`；
- `PULSARA_UNIFIED_CAPABILITY_SURFACE_IMPLEMENTATION`；
- `PULSARA_BUNDLED_SKILLS_HERMES_LIKE_IMPLEMENTATION`；
- `CAPABILITY_SKILL_BUNDLE_SURVEY`；
- `READ_ONLY_FILESYSTEM_TOOLS_HOME_SCOPE_IMPLEMENTATION`；
- `PULSARA_DIRECTORY_CONTRACT_CODEX_COMPAT`。

结论：local/bundled skills、active skill选择与薄prompt注入、read-only filesystem home scope和基本directory discovery当前仍存在，不列为整项缺失；这些事实不等于多源Context Compiler仍存在。Catalog/executor的9项漂移形成PHC-15，compiler缺口单独形成PHC-17。

### 17.7 Host / conversation / LLM 标题族

重点标题：

- `CONVERSATION_RESUME_V1_DESIGN`；
- `HOST_USER_STOP_*`；
- `HOST_TRANSCRIPT_FAILURE_NOTE_PLAN`；
- `LLM_RETRY_*`；
- `OPENAI_SDK_STREAMING_V1_IMPLEMENTATION`；
- `PULSARA_RESOLVED_MODEL_CALL_HARD_CUT_IMPLEMENTATION`；
- `PULSARA_AGENT_RUNTIME_AND_HOST_SESSION_OWNERSHIP_HARD_CUT_IMPLEMENTATION`。

结论：detach/reattach、conversation resume、Host stop、provider retry、typed model streaming 与 resolved model config 当前仍存在。跨-turn failure note 已实现后被删，形成 PHC-13。

### 17.8 Memory / graph 标题族

包括全部 `MEMORY_*`、`GRAPH_DATABASE_VISION`、`ONTOLOGY_*`、`OXIGRAPH_*`。

结论：memory 按用户要求由后续专项重新设计，本索引不评价；Oxigraph、SPARQL、JSON-LD ontology 是 hard-cut 明确删除项，不计产品回归。

### 17.9 Durability / recovery / architecture 标题族

包括 `RECOVERY_CONTRACT_DESIGN`、`FAILED_ABORTED_RECOVERY_*`、`EXECUTION_EVIDENCE_LEDGER_MVP`、authority materialization、projection jobs、schema hot path、model segment coalescing、runtime storage/architecture debt 等。

结论：旧 execution recovery、checkpoint、receipt、repair、projection delivery 与 segment persistence 本来就是减法对象，不因历史文档多而列为产品缺口。只有其中承载的独立用户语义——例如跨-turn failure note与Terminal monitor——被拆出单独登记；历史Inspector只作审计，观察面并入Go TUI。

## 18. 已确认仍保留的关键产品能力

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
- local skills、bundled skills sync/status/reset、active skill选择与薄prompt注入；
- flat `spawn_agent/list_agents/wait_agent/stop_agent`；
- Protocol v3 canonical snapshot/history/live/content read 基础；
- PostgreSQL canonical blobs 对已提交 transcript content 与 accepted tool-result artifact edge 的 bounded read。

这里保留的`canonical transcript`是exact cut、scope、entry order、tool pairing与late-result lowering，不等于保留了PHC-17的多源typed compiler；保留的active skill prompt也只证明skill projection仍可拼入system prompt，不证明runtime environment、clock、Plan、MCP、timing或统一预算仍存在。

这些“已保留/已恢复”项不抵消前文仍未恢复的其他能力族。Round 1只保证一次tool-result冻结时可证明的retained snapshot artifact；Round 2恢复了same-Host terminal monitor、真实stdout/stderr streaming与16 MiB/process retention，但仍不承诺恢复在retention前已经丢失的原始字节，也不承诺跨Host process/monitor continuation。

## 19. 尚未作为缺口确认的标题

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

## 20. 本索引的使用边界

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

Round 3与Round 3.1已完整恢复PHC-17的typed compiler和process-local prefix continuity；Round 4已恢复PHC-09的Python Runtime/Host与Protocol后端；Round 5A已恢复PHC-07A execution envelope。上述轮次都不恢复durable compiled-input audit、provider-input replay或旧generation recovery graph。PHC-07B/08/13/14仍需各自规格化其domain truth。它们都只能把已接受事实投影进compiler，不能让compiler替代其authority。

同样，后续恢复不能把`26 / 23 / 13 / 2`当作拒绝真实产品语义的永久配额。任何数量变化都必须经过上述closed contract审查，但“保持旧数字”不优先于“以正确的canonical/live边界完整表达产品能力”。
