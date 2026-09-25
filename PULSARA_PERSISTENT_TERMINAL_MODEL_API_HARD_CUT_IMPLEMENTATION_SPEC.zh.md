# Pulsara 持久终端模型 API：Hard-cut 实施规格

状态：**未实施的历史候选，不作为当前实施依据**（2026-09-25）。当前已采用合同见 `PULSARA_TERMINAL_MODEL_CALL_COGNITIVE_SUBTRACTION_SPEC.zh.md`：减少现有终端工具的模型调用负担，暂不引入持久 shell。本文仅保留此前方案供比较；当前运行代码、工具说明与 README 以认知减法合同为准；以下“现状”留作当时的历史记录。若未来有具体失败任务证明常驻 shell 的必要性，须重新审议并修订有效规格，不能直接按本文实施。根目录 `AGENTS.md` 始终适用。

## 1. 产品目标与现状

模型最常见的调用仍是 `terminal(command="...")`：每次在独立 shell 中运行，初始 cwd 为 workspace；显式 `workdir` 只作用于本次执行。需要跨调用保留 `cd`、`export`、`source`、函数和激活状态时，模型填写 `persistent_session="名字"`，首次隐式创建、后续同名调用复用真实 shell。名字是稳定选择键，不是命令句柄或 UI 窗口 ID。每条被接纳的命令另有 `command_id`，供等待、读输出和输入使用；模型不需要先创建窗口。

现状的 `terminal_session_id` 只记住 cwd；每次 `terminal` 都启动新的 `shell -c`，因此环境变量、函数及激活状态不连续。新合同保留独立执行作为默认能力，但不保留「独立 shell 却隐式记住上次 cwd」这一旧语义；需要 cwd 连续时明确使用 `persistent_session`。现有 `terminal_process` 以物理 `process_id` 跟进单次进程，`terminal_monitor` 监控物理结束。新合同还必须把「命令完成」和「持久 shell 退出」分开。

本文不承诺完整 Terminal.app 仿真、session 内 job control（`fg`／`bg`／Ctrl-Z、前台进程组切换）、跨 Host 或跨重启恢复 shell、任意后台进程的归属证明，也不新增任意总命令数、总会话数或 shell 空闲寿命限制。

## 2. 唯一模型工具面

保留一个执行入口 `terminal`，两个跟进入口 `terminal_command`、`terminal_session`，以及 ROOT-only `terminal_monitor`。工具名、参数和结果须由一个权威 catalog 生成；`terminal_process`、`terminal_session_id`、旧 `process_id` 跟进路径在 hard cut 中删除，不设别名或运行时回退。这里的 isolated 是正式默认能力，不是持久执行失败后的自动 fallback；持久调用要么按所请求 session 执行，要么返回 typed 失败。

### 2.1 `terminal`

`terminal` 的输入按 `persistent_session` 是否存在分支，拒绝多余字段；不允许把模式冲突的字段悄悄忽略。provider 实际工具 schema **及**运行时解析器都必须接受只含 `command` 的对象，并归入 isolated 分支；带 `persistent_session` 的对象进入 session 分支；字段存在但为 null、空串或非法名称时是参数错误，不能被当作省略而落到 isolated。模型不填写 `mode`，运行时不能用默认值误判。这个选择规则是新合同自身的语义，不是旧版兼容分支。

| 模式 | 参数 | 语义 |
| --- | --- | --- |
| 默认 isolated | 必填 `command`；可选 `workdir`、`tty=false`、`yield_time_ms=10000`、`max_output_chars` | 每次独立启动命令，保留现有 PIPE（`tty=false`）和 PTY（`tty=true`）能力。禁止 `persistent_session`。 |
| 显式持久 session | 必填 `command`、`persistent_session`；可选 `workdir`、`yield_time_ms=10000`、`max_output_chars` | 首次使用名字时创建持久 shell，之后在该 shell 中串行执行命令。禁止 `tty`。 |

现有单次命令长度、`workdir` 长度、session 名称字符及长度、等待范围、响应输出长度等已验证**单操作边界**沿用，除非实施中发现具体协议冲突并修订本规格；不能借此加总调用数或总排队时长。默认输出长度和游标规则沿用现有 output owner。`yield_time_ms` 只决定这次调用等待多久，不是执行寿命、取消或终止指令。

`persistent_session` 名由模型选择，仍限当前 1–32 位字母、数字、下划线或连字符；必须显式填写，可用 `default` 作为名字。同名 session 在 ROOT 与不同 child 中各自独立。只有持久分支可隐式创建 session；isolated 无伪 session。isolated 省略 `workdir` 一律从 workspace 开始，不继承任何持久 session 的 cwd 或环境。两种模式都是正式长期能力，走同一 Host 物理执行、输出、权限和关闭 owner；不因 session 创建失败、繁忙、状态丢失、命令非零退出、权限拒绝、等待超时或输出截断自动切到 isolated。模型可在读到 typed 结果后，自行决定发起一条新的 isolated 调用；已执行或执行与否不明的命令不得自动重放。

`terminal` 接纳命令后返回准确 `command_id`、`mode`、`status`、本次输出及 `output_cursor`（若有）；持久分支还返回 `persistent_session`、当前可信 cwd（未知时为 null）。已接纳命令的状态可为 `queued`、`running`、`completed`、`cancelled`、`not_started`、`terminated` 或 `state_lost`；`completed` 可带非零 `exit_code`，不代表 shell 丢失。**准入前**的容量或校验失败返回 `blocked`／`not_started` 及 typed reason，不生成可管理的 `command_id`；**准入后**因权限撤销、工作目录失效等未 dispatch 的命令保留自己的 `command_id` 并以 `not_started` 终结。`queued` 表示已接纳，调用方不得重复提交来“确保运行”；返回占用该 session 的活跃 `command_id`，以便解释排队。

### 2.2 `terminal_command`

以确切 `command_id` 操作，不再要求模型判断物理 shell ID。`list` 列出当前 scope 可见的活跃及仍保留的完成记录；`poll`、`log`、`wait` 使用该命令的输出游标；`wait` 的单次超时沿用现有 1–30 秒边界，只结束本次等待。`write` 原样写入、`submit` 追加换行，只可作用于确切 `running` 命令；对 `queued`、已结束、旧 shell 或别的 scope 的 ID 返回 typed 拒绝，绝不转发给后来的命令。session 每条命令必须有独立输入边界：写给前一命令但未消费的字节在它完成后也不能被下一命令读取；旧输入 owner 退休后不可复用。程序不读 stdin 且管道已满时，写入须遵守现有单次调用等待／取消边界，不得持有 session 状态锁阻塞 `close_stdin`、`kill` 或 `reset`；部分写入须返回已写范围或明确的不确定状态，超时后不能继续向该命令或后继命令偷偷写剩余字节。输入继续经过现有权限及 hardline 校验。

`cancel` 仅用于 `queued` 命令：从队列终结该项，成功响应后保证命令体永不写入 shell。`kill` 用于正在运行的命令：`isolated` 沿用其物理进程组终止与 join；`session` 关闭整个受管理 shell、终结同 session 队列并把 session 标记为 `state_lost`，不能承诺保留环境。`write`、`submit`、`close_stdin`、`kill` 与命令自然完成及下一条 dispatch 必须在同一执行 owner 下线性化：若所给 ID 已完成，返回 `already_finished`，不得把输入或终止信号送给下一条命令；`kill` 不能仅按 session 名决定目标。`close_stdin` 关闭该 `command_id` 的独立输入端，令当前程序读到 EOF，不关闭 shell 控制通道或下一条命令的新输入端；`isolated` 保留现有 PIPE／PTY EOF 语义。不存在首版的“强制杀当前命令但保证 shell 状态原样保留”。若以后引入安全的 session 中断，须另行定义物理和命令边界。

只读 `list`、`poll`、`log`、`wait` 与有副作用的 `write`、`submit`、`cancel`、`kill`、`close_stdin` 分别按真实效果进入现有工具权限分类；不能把所有跟进调用一概标记只读。UI 的用户控制终止仍可使用 Host 内部精确 owner，不向模型泄露其他 scope 的句柄。

### 2.3 `terminal_session`

`list` 不需要 `persistent_session` 名，列出当前 scope 的已分配名字、`ready`／`busy`／`state_lost`／`closing`、可信 cwd、活跃命令及排队数。`close(persistent_session)` 取消队列、终止并 join shell 后释放名字与槽；再次使用同名视为新建。`reset(persistent_session)` 在同一槽中取消队列、终止并 join 旧 shell，再以 workspace 初始 cwd 和当前环境配置新建 shell；绝不重放或迁移旧命令。reset 必须先完成旧根物理关闭，且在新根启动前取得 8 根配额；可复用被释放的配额，但不能短暂超额。若新根容量已满或启动／初始化失败，session 保留 `state_lost` 与原 session 槽，返回 `blocked`／typed 启动失败；已释放的旧根不得残留占用，队列不会恢复。`close`／`reset` 对不存在的名字返回 `not_found`，对未完成物理关闭不得报告完成。两者都是有副作用调用，走权限门；不得加 `force` 等看似可略过物理结算的参数。

显式 reset 后旧 `command_id` 不会绑定到新 shell。首次隐式创建若 shell 检测不支持、物理容量不足或初始化失败，在任何命令被接纳前完成已启动物理根的 join 并回滚新名字／session 槽；返回无 `command_id` 的 typed 失败，不能让连续失败的名字占满 4 槽。异常 shell 退出时 session 保留为 `state_lost`，后续 `terminal` 对此名字返回 `command_not_started`，直到明确 reset 或 close；不得悄悄重建。普通命令非零退出只结束该命令，session 仍可使用。

## 3. Scope、归属与准入顺序

Host 持有唯一 `TerminalSessionManager`、物理 `ProcessRegistry` 和最终关闭责任。内部 session 键包含精确模型 scope（ROOT 或某个 child）及名字；模型不填写 owner。`command_id` 绑定精确执行与 scope；只有持久命令还绑定原 session 实例，isolated 不创建伪 session owner。进程结束、reset、close、同名重建均不得重绑。ROOT 与 child 不得通过猜 ID 操作彼此命令。child scope 结束时停止新准入、取消本 scope 队列、关闭并 join 本 scope shell **及 isolated 执行**；Host 关闭时兜底终止所有剩余物理执行。跨 Host 和重启没有 shell 恢复承诺。

同一 session 一次只 dispatch 一条命令，其余按唯一 admission owner 确定的顺序 FIFO 排队；同一模型批次多个 tool call 以已知 tool-call 顺序接纳，不能由线程抢锁先后决定语义。进入队列之前完成形状、scope、权限及资源准入。真正写入 shell 前再次确认该 scope 仍存活、当前策略仍允许原调用且授权未撤销；若条件不成立，终结为 `not_started`／`cancelled`，不得后台弹审批或凭旧授权运行。reset 之后不沿用队列和先前审批。排队、取消、dispatch、结果接受须挂在现有 foreground decision 与 side-effect settlement owner 上；不能以一个裸异步队列绕过取消后不得发生新物理副作用的既有保证。

命令入队不等于 shell 已执行。工作目录在**实际 dispatch 前**解析和检查：session 相对路径基于前一条完成后该 shell 的可信 cwd；先做受管理的 `cd`，成功才送命令体。`workdir` 无效时命令为 `not_started`，不改变 session 的先前 cwd。未给 `workdir` 就沿用 shell 真实 cwd；命令本身的 `cd` 可改变后续命令 cwd。可访问目录范围沿用当前获准 terminal 的 Host-local 路径合同，workspace 是初始目录而不是沙箱。`isolated` 的相对 `workdir` 只基于 workspace，执行结果从不改变任何 session cwd。

## 4. shell、环境与命令完成

首版持久 session 使用受管理的 **PIPE 上的 POSIX shell，禁用 job control**，先支持现有 Host 检测路径中的 zsh／bash；stdout/stderr 沿现有 `tty=false` 路径合流，逐命令控制和完成走独立控制通道，每条命令的输入另有不会遗留给下一条命令的 owner。session 不提供 `tty` 开关。若检测到其他 shell 而适配器未通过同等验收，返回明确 `unsupported_shell`，不能回退到每命令 `-c` 并声称持久。Windows PowerShell／pwsh 目前不是 Pulsara 当前终端后端的已实现能力；为它另行冻结平台适配和验收，不在本次规格中假称跨平台支持。`isolated` 继续使用当前 shell 检测、环境构建与启动路径。

session 在创建或 reset 时调用现有 `TerminalEnvironmentOwner` 构建一次默认拒绝敏感变量的环境及初始虚拟环境 PATH。此后 `export`、`source`、shell 函数、激活环境与 cwd 由该 shell 自己维持；后续 `cd` **不**自动重新注入附近的 `.venv`。需要切换环境时，模型显式执行激活或 PATH 命令。`isolated` 每次仍由现有 environment owner 按自身 cwd 构建环境；不继承任何 session 的临时设置。

命令完成协议必须与用户 stdout／stderr 分开：受管理 shell 的最小适配层以独立控制通道传递命令开始、完成、退出码与 cwd；输出裁剪、ANSI 清理、用户打印的 marker、prompt 或一段静默都不能作为完成证据。每条命令有独立的合流输出通道／等价来源隔离，不能把前条的迟到字节归入后条。**控制通道上的完成帧可能先于输出 reader 读到最后的 stdout／stderr 字节**；必须在该命令的输出边界结算后，才冻结本次工具结果并 dispatch 下一条命令。可使用由物理 owner 控制、经测试不会被用户输出冒充的输出 barrier；它只界定已完成前台写入的观察范围，独立控制通道仍是唯一完成证据。不能等所有 `&` 后代退出才认为前台命令完成。具体 shell 启动/控制实现需先检查标准库与已维护依赖的实际 API、明确依赖与 Pulsara 各自所有权；只实现缺失的窄适配，不自写 shell parser 或另造 PTY/process manager。必须验证 `exec`、输出重定向、`set -e`、trap、函数覆盖、异常控制通道关闭等情形。协议失效时将活跃命令标为 `state_lost`，`exit_code=null`，阻止后续排队命令进入 shell；可观测物理 shell 退出码和信号要与命令退出码分列，不能拿 shell 的退出码冒充上一个命令的状态。

持久 shell 不启用 job control；不承诺 session 内 `fg`／`bg`／Ctrl-Z 或完整前台组 SIGINT。现有 `pty.openpty()+Popen(start_new_session=True)` 没有证明拥有 controlling TTY 与所有 job group；不能用它把 session 升格为完整交互 PTY，也不能声称 `killpg(shell.pid)` 能清理开启 job control 后的所有子组。session 的物理关闭必须验证它所承诺管理的同组执行根已 join，失败时给 typed 边界结果并由 Host 继续清理。需要行编辑、全屏程序或现有 PTY 行为时，模型在默认分支直接填 `tty=true`；需要独立 PIPE 输出则只填 `command`。session 中 shell 的 `&` 后台任务保留 shell 本身的行为，但某条命令返回不代表其后台后代结束；Pulsara 不把它冒称独立 `command_id` 或完整纳管工作。长时任务优先以前台命令运行，交由 `terminal_monitor` 提醒。

## 5. 命令、会话与物理资源状态

命令状态流为 `queued → running → completed`，或在允许边界进入 `cancelled`、`not_started`、`terminated`、`state_lost`；每条命令仅有一个终态。session 的 `completed` 由独立控制协议确认逐命令返回；isolated 的 `completed` 继续由现有物理进程及进程组完成边界确认；两者的退出码都可非零。`terminated` 表示物理 owner 确认主动终止，携带终止原因／信号而非伪造正常退出码；`state_lost` 表示无法确认命令结束或 shell 已失联，此时退出码为 null。`running` 的 `wait` 超时不改变状态。`cancelled` 的 queued 命令不会产生该命令的 shell 输出；已经 dispatch 的命令不能谎报为 queued cancellation。session 中活跃命令被 `kill` 时，如果 shell/命令控制协议未能给出正常完成，命令为 `state_lost`；isolated 被 `kill` 时为 `terminated`。

会话状态为 `ready`（shell 可接命令）、`busy`（有活跃命令）、`state_lost`（需 reset/close）、`closing`。会话 shell 的异常退出取消其队列；在完成协议尚未确认的命令为 `state_lost`，已经有可信完成的命令保持原结果。`reset` 和 `close` 与新命令 admission 线性化；结束响应之后不得再出现来自旧实例的写入。若物理关闭尚在进行，状态保持 `closing`、继续占槽，不能释放后让同名新 shell 与旧 shell 并存。

每 Host 现有资源边界明确为**同时最多 4 个已分配 terminal session 槽、8 个纳管物理执行根**，跨 ROOT/child 合计。一个存活的持久 shell 占一个 session 槽及一个物理根；`ready`、`busy`、`state_lost`、`closing` 都占 session 槽，直到显式 close 或 scope 结束完成物理关闭；异常退出且已 join 的 `state_lost` 会话不再占物理根，未 join 的旧根仍占物理配额；reset 复用 session 槽。`isolated` 占一个物理根，不占 session 槽。8 不是「同时可跑 8 条 session 前台命令」；四个 session 各自至多一条前台命令，剩余物理槽可给 isolated。该配额统计现有 registry 纳管根，不等于 OS 后代进程总数。

新增 session 或 isolated 物理准入遇既有 4／8 容量边界，按**当前代码的容量拒绝合同**返回 typed `blocked`；不暗中超额或加入无限待启动物理队列。已存在 session 内的命令则按本规格 FIFO 排队，物理满不应把其已接纳的逻辑命令误判为新物理根。排队不限制会话生命期内累计接纳的命令数，但待 dispatch 的冻结参数会占用内存：现有 Host `128 MiB` retained-output 总预算窄扩展为**输出保留 + pending 冻结输入及调度元数据**的瞬时预算，由原 terminal/process owner 统一原子计费。每项只持有一份冻结参数；按实际 UTF-8 字节再为每项预留 `8 KiB` 元数据费用，以覆盖大量短命令的调度对象，不能用单命令字符上限冒充队列有界；实施测试须测量该收费不低于实际 pending 对象的驻留开销。入队前预留，不足时返回无 `command_id` 的 `not_started(resource_unavailable)`；不能接纳后丢队列。dispatch／取消／scope 关闭立即释放 pending 费用；输出仍按现有机制驱逐并报告 GAP，已接纳而待执行的参数不可驱逐。此 128 MiB 是指定 payload 的同时占用预算，不是整个 Host RSS 限制；资源释放后新调用可再接纳，不是累计命令数或队列寿命上限。保留现有已验证的单次输入、每命令输出保留及完成记录回收边界；不增加 shell idle TTL、总执行时长或重试次数等新硬上限。若未来决定物理容量也改为等待，先另行规定公平性、取消和资源边界，不能借实现顺手改变用户已知的拒绝行为。

## 6. 输出、监控与 UI

沿用现有输出 sanitizer、每命令有界保留、`output_cursor`、`GAP`、截断提示与 `artifact_read`；游标精确绑定 `command_id` 及 stream，不能用共享 shell 的物理 ID 混读两条命令。`poll`／`log`／`wait` 返回当前保留的输出和可信状态；输出截断不改变完成判断。**命令终态与该命令输出流 EOF 是两个边界**：完成时仅结算前台输出观察，不能对仍可能有后台写入的 decoder/sanitizer 调用最终 finalize；原 sanitizer owner 需提供受控的命令观察边界，让无换行尾部中已证明安全的字节可见，仍未决的 token／escape 留待后续输出或真正 EOF，不能为显示尾部提前公开可能含凭据的字节；真正 EOF 时才最终关闭该流的 sanitizer。由该命令启动的 `&` 后台任务可在前台命令完成后继续向其原输出通道写入；完成记录仍按现有 TTL／数量保留，保留期间迟到字节可读，淘汰后按既有 GAP／不可用语义丢弃。淘汰不应关闭尚被后台后代持有的读端而给它额外制造 SIGPIPE；由 session 的单一物理 IO owner 继续有界 drain 并丢弃，直到 EOF 或 session 物理关闭，不为每条历史命令留下永久线程、无限 spool 或 durable 日志。仍存活的流／FD 是真实物理资源；若首次命令在准入前分配失败，返回无 ID 的 typed `resource_unavailable`；若已排队命令在 dispatch 前分配失败，保留其 `command_id` 并终结为 `not_started(resource_unavailable)`，不得丢失该项或冒称已执行。Host 关闭时这些流与 shell 一起 join。UI 对 session、isolated、queued、active、完成、state-lost、close/reset 给一致且可辨的呈现；用户可通过现有 Host 控制结束真实执行，不能把关闭工具卡片当作终止进程。

`terminal_monitor` 保持 ROOT-only、Host 内 best-effort 与既有注册／取消／进度节流语义，但注册对象改为 `command_id`，可监控 queued→running→终态；命令完成即发送完成观察，不等持久 shell 退出。取消 monitor 只停止未来提醒，不取消命令；空列表不证明命令结束。monitor 的有效期、进度更新边界沿用现有合同，不把它升级为 durable job、delivery ACK 或恢复机制。必要的现有 typed live event／observation 仅调整字段语义与投影，不增加事件类别来“证明”另一机制工作。

正式采用的 compaction successor 可收到由现有运行时 owner 生成的轻量交接：当前 scope 的 session 名、可信 cwd／状态、活跃 `command_id`、排队数及正在监控的命令。无需快照完整环境，也不能在压缩后重建 shell 或重放队列；未变更 Host 内的活 shell 继续由原 owner 管理。冷 epoch 与明确采用的 compaction successor 是安装新工具 schema／SYSTEM 提示的唯一边界。session 新建、关闭、权限变化和 UI 需要都只通过工具结果及既有 typed runtime 路径呈现，不改正在使用 epoch 的 `SYSTEM`／provider `tools` 前缀。

## 7. 权限与安全边界

`terminal` 两个模式都走现有 terminal access、READ_ONLY／ASK／ON_REQUEST／RISKY_ONLY 和 hardline 判断；`terminal_command` 输入和 `terminal_session` 修改操作依真实副作用分类。模型提交的命令字符串可以改变 shell 状态，持久变量、alias 或函数也可影响后来命令；静态 hardline 检查不是 shell 沙箱，工具描述不能承诺它能解释所有间接效果。跨 scope 访问、伪造 ID、权限撤销、Host 或 child 已结束必须在物理写入前拒绝。用户主动终止后，不自动重试、重启或重放先前命令。

本轮 dogfood／报告证据按 `AGENTS.md` 只遮蔽实际加载的凭据值，不广泛抹掉 prompt、响应或错误证据；生产命令输出继续由现有 sanitizer owner 处理，本规格不隐含修改其现行通用遮蔽策略。已保存生产配置只读，用现有 `LocalSettingsStore`／`require_pulsara_home()` 读取；真实提供商 dogfood 不从环境变量或旧 `.env` 另取凭据。本功能不新增 credential store、持久命令日志、恢复表、receipt、lease 或指纹。终端可访问 Host-local 路径是现有授权后的能力，不因称为 session 而扩大或缩小文件系统权限。

## 8. 所有权与依赖边界

现有 `TerminalEnvironmentOwner` 负责默认环境和 shell 检测；现有 `ProcessRegistry`、输出 owner、monitor、Host lifecycle、权限分类和工具结果 settlement 继续各自负责物理执行、观察及结算。新增的 session adapter 只负责：一个活 shell 的串行命令投递、独立控制通道上的逐命令完成、可信 cwd，以及与现有物理 owner 对接。实现前检查现有依赖或平台能力能否承担控制通道和 PTY 关闭；可复用则用其受支持的接口，不复制一套 parser、调度器或进程树回收器。若实际依赖无法满足第 4 节的完成与清理保证，应先修订本规格的确切产品边界，而不是以 stdout marker、静默推断或自动 isolated fallback 冒充成功。

## 9. 失败结果与不变量

| 情况 | 模型可见结果 | 后续允许的动作 |
| --- | --- | --- |
| 新 session／isolated 超出 4／8 容量 | `blocked`、具体资源边界、无 `command_id` | 结束其他资源后重试；不声称已排队 |
| 已存在 session 忙 | `queued` 与自己的 `command_id`、活跃 ID | 等待、监控或取消该 ID；不得重交命令 |
| 排队后 scope／权限失效 | `not_started`／`cancelled`，命令体未写入 | 新授权流程由以后明确调用触发 |
| 工作目录在 dispatch 时无效 | `not_started`，原 session cwd 不变 | 修正 `workdir` 后新调用 |
| 命令正常返回非零 | `completed` 与真实退出码 | 同 session 可继续 |
| shell 退出或控制协议失联 | 活跃命令 `state_lost`、退出码 null；队列终结，session `state_lost` | 显式 `reset` 或 `close` |
| `kill` 活跃 session 命令 | shell 与队列结束、session `state_lost` | 显式 `reset` 或 `close` |
| `wait` 超时或输出 GAP | 当前状态／GAP，如实报告 | 再查同一 ID 或读 artifact；不得重跑以恢复日志 |
| Host／child 关闭 | 停准入、取消队列、终止并 join 其物理根 | 旧 ID 不再可操作 |

所有成功结果必须来自对应 owner 已确认的状态；不以尚未完成的 shell 物理退出或未经确认的控制标记推测 `exit_code`。每个已接纳 `command_id` 至多 dispatch 一次；取消或 reset 后无迟到写入；容量只在物理 join 后释放；输出游标不能跨命令；同名 shell 重建不能恢复旧 ID。这些是不随 UI、provider 或具体 shell adapter 实现变化的合同。

## 10. 模型可读的最短用法

工具说明应直接教模型：普通独立命令只填 `command`，需要交互 PTY 时另填 `tty=true`；需要连续 cwd／环境变量／函数时，在每次相关调用中填同一个 `persistent_session` 名。长命令拿 `command_id` 用 `terminal_command(wait|log|write|submit)` 或 ROOT 的 `terminal_monitor`；只有管理持久 shell 时才调用 `terminal_session(list|reset|close)`。明确写出 `yield_time_ms`／`wait` 不是命令时限、`queued` 已接纳不必重试、异常 `state_lost` 不能静默恢复，也不能自动改用独立执行。这套默认调用不要求模型先调用 create，也不要求它填物理进程 ID 或 owner ID。

## 11. 一次性实施与验收

1. 在现有端口、catalog、权限分类及 Kernel binding 中一次替换工具 schema、描述和调用路由；删旧 `terminal_session_id` 的“只记 cwd”实现、`terminal_process` 的模型入口及相关旧结果投影。默认 isolated 作为新规格的正式独立执行能力继续走现有每命令 `-c`／ProcessRegistry 物理路径，省略 `workdir` 时从 workspace 开始；它不是兼容别名，也不是持久失败后的回退。对运行中的旧 provider epoch，先按现有生命周期有序关闭／排空后再安装新工具面，或在明确采用的 compaction successor 切换；不得中途改 provider 前缀，也不得长期双 schema。
2. 在现有 Host 物理 owner 内实现精确 scope、session 槽、命令 FIFO、dispatch 前校验、session adapter 与控制通道；保留现有 side-effect settlement、取消、输出裁剪、进程组及 join 责任。新增状态只放进 typed 进程本地状态；不引入 durable job/event/subject/guard/relation。若确需增加某类别，先修订本文，写明独立产品理由、owner、事务边界和失败路径。
3. 同步修订仍有效的 process-local execution、capability/policy、terminal client protocol 合同，clean-v0、生成的客户端绑定、tool card／Host 用户控制、compaction handoff、README 及英文／中文工具说明；旧 `process_id` 与 `terminal_session_id` 不留双读双写。检查所有前端与 CLI 消费者实际显示的 command/session 状态。
4. 聚焦测试覆盖：实际 provider schema 和 parser 均把仅 `command` 归为 isolated、带有效 `persistent_session` 归为持久分支，拒绝 null／空串／非法名称及与 `tty` 混用；同 shell 的 `cd/export/source/function` 跨调用、named scope 隔离、默认 isolated 每次 workspace／PIPE／PTY、同批次 FIFO、queued 取消与权限撤销、输入残留不能跨命令、stdin 不读且写满管道时 write／close_stdin／kill／reset 不死锁并报告部分写入、旧 ID 的 write/kill 与下一条 dispatch 竞态、reset 容量／启动失败与同名重建、持久请求的各种失败均不自动改跑 isolated、非零退出、`exec`／`set -e`／trap／重定向／控制通道损坏、连续两条无换行 stdout/stderr、完成帧先于输出 reader、大输出末尾不丢、后台持有输出 FD 时前台无换行尾部可见且迟到输出仍可读、逐命令完成与 monitor 唤醒、游标／GAP／artifact、8／4 容量及物理 join、child/Host 关闭、取消与 tool-result settlement。不要以降级断言、skip 或旧兼容分支取得绿灯。
5. 用仓库根 `.venv`／`uv` 执行聚焦和必要集成测试；按修订后的 active spec 做真实提供商和长程 dogfood：让模型仅凭新描述完成普通独立命令、显式持久连续 cwd/环境、双持久 session、默认分支交互 PTY、排队、监控与 reset，检查真实 provider 输入前缀、工具参数和结果。通过现有保存的生产配置只读获取模型连接；若需重置数据库，先核验其确为本地 disposable 目标。记录可复现的输入输出与故障证据，遮蔽实际凭据值。

冻结验收的核心判据是：模型普通独立命令保持单参数，持久状态必须显式请求；每条命令的执行、输入、输出、取消和完成都有准确身份；持久状态真实存在；PIPE／PTY 的独立能力未丢；旧 epoch 的 provider 前缀不被重写；不增加无产品必要的耐久机制或长程总上限。若其中任何一项无法在选定 shell／平台上成立，先修订本文，不以“最好努力”替代已经声明的硬合同。
