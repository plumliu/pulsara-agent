# Pulsara 终端工具的模型调用认知减法

状态：**已实施**（2026-09-26）。本文是现行终端模型调用合同；用户确认后以单次 hard cut 实施。它取代根目录 `PULSARA_PERSISTENT_TERMINAL_MODEL_API_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`，后者仅保留为未实施的历史候选。第 2 节留存切换前事实，第 3—4 节描述当前合同，第 6 节给出验证与默认值冻结证据。

## 1. 问题与目标

当前三个工具已经覆盖常见命令执行与运行中进程的跟进：`terminal` 启动一次 shell 命令，`terminal_process` 对返回的 `process_id` 读输出、等待或送输入，ROOT 可用 `terminal_monitor` 请求稍后的完成／进度提醒。模型能把 `cd`、环境设置、多个步骤和条件判断写进一条命令。现阶段没有证据表明通用持久 shell 能显著改善常见模型任务，因此本方案不引入常驻 shell、持久环境变量、额外的 `terminal_session`／`terminal_command` 工具或新的执行 owner。

“模型高可用性”在本文中特指：模型凭工具描述即可选对入口，最常用调用只填必要参数；收到 `running`、截断、资源满或 monitor 竞态结果后，能够确定下一步；长任务可以跨多轮继续观察。它不表示进程或 monitor 跨 Host 重启持久恢复。

目标调用路径：

```text
普通命令：terminal(command, workdir?)
仍在运行：terminal_process(action="poll"|"wait", process_id, ...)
交互输入：terminal_process(action="write"|"submit", process_id, data, ...)
稍后唤醒：terminal_monitor(action="register", process_id)  # 仅 ROOT，需要时使用
```

先减少确实让模型做无谓选择的概念。`tty`、`process_id`、`poll` 与 `wait` 的区分、`terminal_monitor` 的显式订阅、精确输出游标及 `artifact_read` 仍各有独立用途，不为了减少字段数量而合并不同的物理行为。

## 2. 切换前事实与混淆点（对照记录）

以下依据切换前生产端口 `src/pulsara_agent/ports/terminal.py`、执行路由 `src/pulsara_agent/conversation_kernel/tool_runtime.py`、`src/pulsara_agent/terminal_process/manager.py` 及 `output.py`。以下“当前”均指切换前实现，留作对照；现行行为见第 3 节。

| 模型需要判断的事 | 当前事实 | 调用成本或失败方式 |
| --- | --- | --- |
| `terminal_session_id` 是什么 | 它选一个只记住 cwd 的对象；每次 `terminal` 仍启动新 shell。`process_id` 才是运行中命令的跟进句柄。 | 名字像持久终端，模型可能误以为 `export`、激活环境或函数会跨调用留下。不同目录需要记名字与隐式 cwd，默认 `default` 也可被先前调用改变。 |
| cwd 何时真的被记住 | 无论填写 `workdir` 还是在命令内 `cd`，只有初始调用未 yield、退出时的 cwd probe 有效且目录获准，才写回会话 cwd。已返回 `running` 的命令后来结束，不会通过 `terminal_process.wait` 补写。 | 同一目录设置是否影响下一条命令，取决于初始等待窗口。这也使当前工具描述中“显式 `workdir` 会更新记忆目录”的表述过强。 |
| 送输入后如何读回答 | `submit` 写入 UTF-8 文本并追加换行，随即取一次快照；`wait` 等物理进程结束，`poll` 立即读当前状态。生产路由的 `write` 漏传 `append_newline=False`，当前会在写入前报 `TypeError`，不能作为正常基线。 | 对仍运行的 REPL／交互程序，`submit` 的回答可能在快照后才出现；模型只能再次调用 `poll`，又可能碰到时序竞态。`wait` 并不是“等这一行的回答”。`write` 还需先修复现存故障。 |
| `poll` 与 `log` 选哪个 | 两者都能立即读取当前保留输出并接受 `since_cursor`；`poll` 返回扁平状态与输出，`log` 返回嵌套 `process` 与输出。`log` 已读取失败进程也按工具调用成功结算，`poll` 则把进程失败映射为工具错误。 | 读输出这一高频意图有两个入口、两种结果形状和两种外层结算语义。 |
| 资源满后怎么办 | 当前 Host 最多有 8 个正在启动或尚未完成物理结算的执行槽、分配 4 个命名 terminal session（含 `default`）。进程容量满时 `terminal` 返回 `blocked` 和错误文本；第五个命名 session 会在准入时失败。 | 记 cwd 的名字会占用并非 shell 的配额；模型要从文本推断是否能稍后重试。当前没有 `running` 记录也不能证明物理槽已释放。 |
| 长命令与输出截断 | `running` 携带精确 `process_id`；`terminal_monitor` 显式注册完成／进度提醒。`output_cursor` 指向已观察输出的末端，即使本次正文因长度限制省略部分内容。 | `wait` 只等待本次调用；monitor 注册时进程可能已经结束。直接复制截断结果的末端游标会跳过本次省略的文字，必须按 `artifact_read` 指引补读。 |

上述 `write` 漏参是已单独复现并修复的生产故障，不计入 API 改良的收益。其余主要是模型可见合同的问题，不要求重新实现 PTY、进程表、输出存储或监控调度器。采用前没有真实模型对照；第 5 节规定验证办法，第 6 节记录实际观测及其限制。

## 3. 现行合同：保留三个工具，减少选择

### 3.1 `terminal`：每次独立执行，目录显式

保留 `command` 必填及现有 `workdir`、`tty`、`yield_time_ms`、`max_output_chars` 的单操作边界。删除模型可见的 `terminal_session_id` 和“记住上次 cwd”语义：不填 `workdir` 时，命令总从当前 workspace 根目录启动；填写相对 `workdir` 时从 workspace 根目录解析，填写绝对路径或 `~` 时继续按当前获准范围解析。一次命令内部的 `cd` 正常有效，不改变后续调用的初始目录。每次仍由当前环境 owner 按本次 cwd 构建环境；`tty=true` 继续用于确需交互式终端行为的程序。

普通调用只需 `terminal(command="...")`。要在子目录连续做事，模型每次填相同 `workdir`，或在一次命令中完成多个步骤。`yield_time_ms` 仅控制本次初始观察窗口；超出窗口仍返回 `running` 与同一物理进程的 `process_id`，不终止命令。返回的 `cwd` 应明确表示**本次命令的启动目录**，不再暗示下一次默认目录；如需要最终目录，只从本次命令输出取得，不引入新的持久 cwd 字段。

取消命名伪 session 后，其“最多 4 个分配槽”不再作为模型工具准入限制；保留现有最多 8 个正在启动或尚未完成物理结算的执行槽这一边界。内部可以保留执行所需的 owner 对象，但不能向模型重新投影一个只记 cwd 的会话身份。删除为回写会话 cwd 而插入命令的 EXIT trap／cwd probe；`process_id`、Host 归属、权限门、同组进程关闭及输出保留语义继续由现有 owner 管理。

旧 cwd 记忆曾被其他产品面使用，本次切换统一新来源：模型环境快照移除 `terminal_current_cwd`，保留 `workspace_root`，并把 `relative_workdir_base` 改为 `workspace_root`；所有 PreTool、Permission、PostTool、SessionStart／End、UserPrompt、SubagentStart／Stop、compaction hooks 的公共 `cwd` 与 hook 子进程启动目录统一为 workspace 根目录。针对某个 terminal 命令的 hook 仍可从 `tool_input.workdir` 判断命令自己的目录，不能把 hook 的 `cwd` 冒充命令 cwd。compaction handoff 的运行进程条目删除 `terminal_session_id`，保留确切 `process_id`、启动 cwd 的既有投影与状态；当前投影对 workspace 外目录只给 `<outside-workspace>`，并非总是绝对路径。相应 context、hook 和客户端合同须同步修订，避免留下“相对路径基于上次终端目录”的旧提示。

### 3.2 `terminal_process`：一个立即读入口，一个有限等待入口

保留 `list`、`poll`、`wait`、`write`、`submit`、`close_stdin`、`kill`。删除模型可见的 `log`：`poll` 承担立即读取状态和当前保留输出的用途，继续支持 `since_cursor`、输出保留／GAP 标识、截断标识及 `artifact_read` 指引。原 `log` 的进程状态、退出码及补读信息由 `poll` 保留，command、stdin_closed 和 duration_seconds 由现有 `list` 条目提供，不另增一种结果形状。`wait` 继续表示最多等待当前进程物理结束一个有限时段，绝不暗示等一次交互响应。

`write` 与 `submit` 仍分别代表原样 UTF-8 文本输入和追加换行。等待优化以独立修复 `write` 路由漏传 `append_newline=False` 为前提；两种输入确实到达进程的行为有独立回归测试。为这两个动作增加与 `terminal` 同义的可选 `yield_time_ms`：成功送入并 flush 后，当前调用最多等待该时段并返回当时可见的输出和进程状态；进程结束可提前返回。默认值冻结为 1000 ms，范围复用当前 `terminal` 的 0–30000 ms 单次等待边界；0 明确表示立即返回。冻结依据见第 6 节。观察等待不证明交互程序已完整回答，也不改变进程寿命。PTY 的输入回显可能先于程序回答，故不得把“第一段新输出”判作完整回答或据此提前完成。超过观察窗口仍可用 `poll`、`wait` 或 ROOT 的 `terminal_monitor` 跟进。

实现已核对并复用现有 `ProcessRegistry`、`TerminalOutputOwner` 与物理完成信号：成功写入后等待既有物理完成信号，再读既有输出快照，不另造 PTY、输出队列、quiet detector 或调度器。观察窗口内返回的仍是当前保留输出快照，可能包含送入前的文字；继续返回 `output_cursor` 与补读信息，不伪称“本次输入的完整回答”。作为显式替代，模型也可 `submit(yield_time_ms=0)` 后调用 `wait(timeout_seconds=1)`，代价是额外一次工具调用。写入仍须沿用当前权限、hardline 与物理结算规则；观察等待被取消时不能撤销已经送达的输入，必须按既有副作用结算边界表明已知／未知结果，且不得自动重发。

`list` 用于恢复当前 Host 内**仍保留**的句柄；已完成记录当前的惰性淘汰目标为 32 个、默认 TTL 3600 秒，清理发生在后续准入等现有边界，不能理解成完成瞬间严格只剩 32 个。`list` 查不到并不证明命令从未执行，也不能据此自动重跑。`poll` 用于立即看状态／增量输出；`wait` 用于明确等进程结束；`close_stdin` 提供 EOF；`kill` 终止物理进程。保留这些动作的差别，且仅以返回的确切 `process_id` 操作进程。

删除 `log` 后，`command`、`stdin_closed`、`duration_seconds` 仍可由现有 `list` 的进程条目取得，不为此扩大 `poll`。结果语义是：`poll`／`wait` 成功取得观察时，外层工具调用为成功，内层 `status`／`exit_code` 如实说明进程可能失败、超时或被杀；进程不存在、游标无效或读取失败时外层才报工具错误。成功写入、提交、关闭 stdin 或终止的动作同样不得仅因最终进程状态非 `success` 就被标成调用失败。`terminal` 自身的命令执行结果语义单独保留。实现时同步更新结果渲染、动作分类和测试，不能只删除 schema 中的 `log` 字面量。

### 3.3 `terminal_monitor`：只在需要稍后唤醒时使用

保留 ROOT-only 的 `register`、`list`、`cancel` 和现有可选进度／投递／有效期参数。模型的最短注册调用仍是 `terminal_monitor(action="register", process_id="...")`；省略复杂选项即可只请求完成提醒。`cancel` 只停止后续提醒，不终止进程。若注册返回 `PROCESS_ALREADY_TERMINAL`，模型用同一 `process_id` 调用 `terminal_process.poll` 取得最终状态；若 `PROCESS_NOT_FOUND`，不得推断原命令未执行。结果或工具说明应明确这两条恢复规则。已注册 monitor 的空列表不证明进程结束。

不自动给所有 `running` 命令注册 monitor：模型可以继续独立工作、显式有限等待，或结束当前轮等待提醒。默认 monitor 最长 36000 秒，到期会产生 `EXPIRY` 观察；如果任务仍需等待，模型可根据当时仍有效的 `process_id` 决定再次注册，不能把到期当作进程完成。自动订阅会改变唤醒时机和调用量，需独立产品理由。monitor 仍是 Host 内 best-effort，不升级为 durable job 或跨重启执行恢复。

### 3.4 失败与大输出：让下一步可判断

保留当前单次调用的状态、退出码、`process_id`、输出游标、GAP、截断及产物补读信息。明确的容量准入失败表示命令**未启动**，没有可跟进的 `process_id`；模型可用 `terminal_process.list` 观察仍保留的进程，但暂时没有 `running` 项不保证物理槽已完成结算。待已有执行释放容量后，模型可判断是否重试这次确切未启动的调用，不默认终止有用进程。为容量失败提供稳定、可解析的原因，例如 `PROCESS_CAPACITY_EXHAUSTED`，不要只留英文错误句子。不得把权限拒绝、命令非零退出、执行结果不明或写入结果不明自动重试为另一次有副作用命令。

保留已验证的 8 个执行槽边界：每个在途执行占用本机进程组、PIPE／PTY 描述符、reader／watcher 与输出保留资源；当前单进程保留边界为 16 MiB、Host 为 128 MiB。这是同时占用这些物理资源的准入边界，不是累计命令数或任务寿命限制。达到容量时本次命令未启动，返回 `blocked`／`PROCESS_CAPACITY_EXHAUSTED`／空 `process_id`，已有执行完成物理结算后可再次准入。本文不冻结新的队列或 durable pending 机制；若要把容量准入改成等待队列，必须另行规定接纳、取消、所有权和 Host 关闭的语义，符合根目录 `AGENTS.md` 的物理并发与长程可用性要求。

输出正文因长度限制省略内容时，`output_cursor` 仍是精确的输出末端，不能被模型当成“已读完正文”的证明。工具说明和模型可见结果应明确：任务需要省略部分时先按现有 `artifact_read` 指引补读；只有不需要省略部分时才把该游标用于后续增量读取。不为了简化字段而丢失来源覆盖或 GAP 事实。

## 4. 实施边界

本次采用单次 hard cut：同时替换 provider schema、严格解析器、执行路由、结果投影、能力分类、UI／客户端合同、clean-v0 基线、测试、README 与仍有效的规格。删除旧 `terminal_session_id`／`log` 模型入口、cwd 记忆及其 probe，不保留迁移期别名或双语义；历史文档只作追溯。既有正在运行的 provider epoch 的 `SYSTEM` 与 `tools` 必须保持字节不变；参数形状相同的 `terminal(command=...)` 也不能在旧 epoch 下悄悄改用新的 workspace cwd 语义。实施时复用现有 descriptor／provider binding 闭合检查，在既有 safe point 结算旧工具调用并释放对应工具面借用，然后仅于新 cold epoch 或明确采用的 compaction successor 安装新合同；不兼容旧合同明确拒绝。采用 compaction successor 不关闭 Host 的进程与 monitor owner，仍运行的 `process_id`、输出游标及 monitor 按既有 handoff 合同继续有效；实际 Host 关闭仍沿用原有物理终止语义，不能增加其他 rebase 边界。

不新增 durable event、live event kind、subject slot、fingerprint、总调用数／总任务数上限或跨重启恢复机制。现有 `ProcessRegistry`、输出 owner、monitor、权限门与 Host lifecycle 继续承担物理职责。若实现中发现这些 owner 的扩展点不能满足一项目标行为，应先修订此文档中的产品边界并说明具体缺口。

## 5. 验证与冻结条件

切换前须先以**切换前工具面**记录模型真实调用：普通单次命令、两个目录交替工作、命令内部 `cd`／`export`、长任务返回 `running` 后跟进、需要稍后唤醒的任务、REPL 送入一行后延迟输出、PTY 回显、输出截断后的补读、8 进程容量耗尽、monitor 注册时进程已结束。先用聚焦测试确认并单独记录当前 `write` 漏参故障；有效输入的对照只能在修复该故障后进行，不能把基本能力修复计作等待窗口的收益。比较 `submit` 后 `wait(1)` 与单次 `submit(yield_time_ms=1000)` 的额外调用数和总延迟，再冻结新默认值。记录模型实际参数、结果和下一步；不要只依据理论上的字段数量判定改进。

切换验收对照相同任务检查：模型是否更少填无意义字段、是否把 `process_id` 用对、能否在交互输入后可靠取得后续输出、是否正确区分 `poll`／`wait`／monitor、是否在资源或输出边界继续工作。验证还必须覆盖 ROOT 与 child 可见工具差异、只读权限、进程终止与 Host 关闭、模型环境与全部 hook 的 workspace cwd、compaction handoff 无伪 session、完成记录过期、monitor `EXPIRY`、非零退出进程的只读观察、观察窗口取消后的输入结算、长程多轮跟进、输出游标／产物补读、provider schema 接受度及新旧 epoch 的 provider 输入前缀连续性。按仓库 `AGENTS.md` 使用根目录 `.venv`／`uv`，并在规范要求时使用只读加载的保存生产设置做真实 provider dogfood；不得为通过测试弱化断言或把实际凭据写入证据。

只有证据表明需要常驻 shell 独有的跨调用状态、且单次命令脚本、显式 `workdir` 和既有运行中 PTY 均不足以完成任务时，才重新讨论持久 shell。这一判断应基于具体失败任务，不由终端窗口的拟人化比喻决定。


## 6. 实施与验证记录（2026-09-26）

实现复用 `ProcessRegistry.physical_completion`、现有输出快照／产物 owner、monitor 和 `KernelSessionIO` 结算；没有新建输出就绪检测器、会话、调度器或耐久关系。`TerminalManager` 只组织独立命令和既有环境／进程 owner，所有目录来源改为 workspace。UI 的后台进程读取继续复用内部 `log_process`；它不是模型动作别名。模型 `poll` 与 `list` 已保留必要状态／输出／元数据，模型 schema 严格拒绝旧 session 参数及 `log`。

clean-v0 经审计无命名 terminal session、cwd 记忆或模型 action 枚举列可删除，故不制造空迁移或新增关系。真实 dogfood 每次均由当前 clean-v0 建立验证过的本地临时数据库；原有 30 committed event、24 live kind、11 subject slot、1 append guard、28 product relation 的 oracle 保持不变。客户端协议没有伪 terminal session 字段；更新的是工具结果卡片与进程 DTO 使用方。

真实 provider 使用用户保存的 `openai/gpt-6-luna` 连接，通过 `LocalSettingsStore` 只读加载有效 Pulsara home 后注入临时 home 与本地临时数据库，未修改保存设置。完整参数、结果与回复见 `dogfood_evidence/terminal_cognitive/` 及其中的 `REPORT.zh.md`：

- 修复 `write` 漏参后才运行旧合同基线，8 场景共 43 次工具调用；新合同同一组提示为 39 次。PIPE 交互由 5 次降至 4 次，PTY 由 4 次降至 3 次，均在 `submit` 结果中读到延迟 200 ms 的程序回答。另 2 次差异来自基线在容量场景给 `list`／`kill` 多填字段后收到严格参数拒绝，不足以证明普遍的模型选择改善。
- 明确比较相同 REPL 的 `submit(yield_time_ms=0)` 后 `wait(timeout_seconds=1)` 与 `submit(yield_time_ms=1000)`：包括启动／退出分别 4 次与 3 次调用，总耗时 27.90 秒与 19.10 秒。仅据此冻结 1000 ms 为有界默认观察窗口；单次、单模型测量含模型与网络延迟，不承诺固定提速或回答完整。
- 两种工具面均正确恢复截断中间内容、跟进长命令和处理显式 monitor 注册竞态。原 2 秒 monitor 场景在两次运行中均抢先结束，模型没有完成最终补读；这组记录不能当成自动唤醒证据。补充 30 秒任务成功注册，并产生 canonical `COMPLETION` 后自动执行后续模型轮。
- 旧基线的 8 场景完成后，实验脚本用了已移除的 `close_session` 参数，收尾报 `TypeError`；原始报告保留 `failed`，不改写为整轮通过。修复实验收尾后，新合同与补充对照正常结束。这不计为产品回归或模型收益。

聚焦与集成验证覆盖目录／probe 删除、PIPE／PTY、输入原样与换行、取消后的精确结算和不重发、容量释放、失败进程的成功观察、游标／GAP／产物补读、记录淘汰、monitor 到期及竞态、ROOT／child 可见性、权限、全部 hook 调用来源、Host 关闭、compaction 与 provider 前缀连续性。旧 descriptor 与当前物理执行 owner 不匹配时仍由现有闭合检查拒绝；新的工具面准备与 handoff 不关闭仍运行的进程／monitor。长程测试和既有 clean-v0 oracle 继续生效。具体测试命令、数量与证据限制见报告。
