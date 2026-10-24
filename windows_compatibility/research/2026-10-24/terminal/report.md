# Windows 终端全链路调研

日期：2026-10-24。范围：只读研究 Pulsara、相邻 Codex 与 deepseek-harness；本报告和探针只放在本目录。没有改生产代码、现行规格或其他仓库。当前机器是 macOS，本报告没有把 macOS 测试当作 Windows 激活证据。交叉审阅后root已决定采用标准PS7 `-Command`退出语义（默认最终失败1、显式`exit N`保留N），不实现自动保留native numeric code的第二套policy；详见`review-round1.md`。

## 结论与决策状态

1. 可以冻结“Windows 11 25H2 x64、原生 Python backend、随包 PowerShell 7.6 LTS、ConPTY + PIPE”作为设计目标，同时把依赖所有权、物理进程范围和 EOF 的原机证明列为 G0 前置阻断条件。冻结目标不等于已证明依赖满足目标，更不等于可发布。G0 失败必须停止并修订产品边界；不能静默改跑 WSL、cmd、PowerShell 5.1 或 Git Bash。
2. Windows 终端不是把 `/bin/sh -c` 换成 `pwsh -Command`。当前的进程组消失、reader/watcher/timer join、启动 rollback、取消副作用结算、输出游标、物理容量、Host/child 隔离都需要平台实现。ConPTY 只提供控制台通信。
3. 推荐随包固定 PowerShell 7.6 LTS 的受支持补丁，私有绝对路径启动；具体补丁在 G0 用官方生命周期与发行包重新锁定。避免维护 PowerShell 5.1 的编码和脚本行为第二套合同。`command` 上限是 **1,048,576 个字符**，不是 1 MiB；Windows CreateProcess 命令行上限不能用来悄悄缩小工具 schema。应统一短 bootstrap + 受保护 UTF-8 command 文件，不占目标 stdin。
4. 不能直接用 `pywinpty.PtyProcess` 或照抄 Codex/deepseek 的生命周期。Pulsara 必须先证明所选维护库支持确切的 spawn/Job/EOF/output-drain 所有权。研究发现了真实 API 缺口，见下文。
5. “Windows Electron + entire Python backend in WSL2”能减少 POSIX 代码移植，但其 shell、路径、插件/MCP/hook 工具链、home、数据库运行域成为 Linux。用户没有接受这个产品语义时，它只是备选，不能作为 native 的实现细节或 fallback。

## 1. 生产事实与不可破坏语义

权威来自当前代码和已实施终端合同。`archived_docs/PULSARA_TERMINAL_MODEL_CALL_COGNITIVE_SUBTRACTION_SPEC.zh.md` 顶部注明已实施且为现行合同；`archived_docs/PULSARA_PERSISTENT_TERMINAL_MODEL_API_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 顶部明确是未实施候选。后者不能用来增加常驻 shell。

### 当前调用与所有权

| 层 | 实际代码 | Windows 任务需要保留的事实 |
|---|---|---|
| 模型工具 schema | `src/pulsara_agent/ports/terminal.py:22` 起 | `terminal(command, workdir?, tty?, yield_time_ms?, max_output_chars?)`；每次独立 shell；默认 workspace；`cd/export` 不跨调用 |
| 跟进 | 同文件 `TerminalProcess*Input` | `list/poll/wait/write/submit/close_stdin/kill`；精确 `process_id`；没有模型 `log` 或伪 session |
| 执行路由 | `src/pulsara_agent/conversation_kernel/tool_runtime.py:483` 起 | 权限、hardline、物理执行与 tool-result settlement；ROOT-only monitor；不可绕开 executor |
| 环境 | `src/pulsara_agent/terminal_process/environment.py` | default-deny env、受限 shell snapshot、single-flight/TTL、venv overlay、关闭正在运行的 probe |
| 执行 owner | `src/pulsara_agent/terminal_process/manager.py:135` / `:1159` | Host-local 注册、独立命令、启动 reservation、精确 rollback、同 Host scope、不会重放 |
| 输出 | `src/pulsara_agent/terminal_process/output.py` | 增量 sanitizer、精确游标、retained GAP、UTF-8、artifact 同范围、每进程/Host 保留边界 |
| monitor | `src/pulsara_agent/terminal_process/monitor.py` | 既有 best-effort 观察，非 durable job；完成来源是物理完成，不是静默或 shell prompt |
| Host 生命周期 | `tests/test_stage2_terminal_host_lifetime.py` | 关闭/取消必须 join 原物理 owner；新 Host 不接管或重启旧 process |
| 私有客户端协议 | `terminal_protocol/v3_gateway.py` / `desktop_app/protocol_client.py` | protobuf 帧和 launch capability 保留；目前 transport 是 Unix socket，不是终端 PTY |

“长期交互终端”在当前产品中的准确含义：命令启动后如果不结束，`process_id` 对应的 PIPE/PTY 可跨多轮读写、等待、监控与终止。初始 shell 每次新建，不是所有命令共用的 shell。必须支持 REPL、会读 stdin 的长程序、TTY 下的行编辑/全屏程序，而不是只证明一条 `Write-Output` 或短 shell wrapper 能跑。

### 物理完成是最关键的合同

`manager.py:640` 的 watcher 先 `process.wait()`，再 `reader.join()`，再等待 `_process_group_exists(pid)` 变假，随后冻结终态、finalize sanitizer 并置 `physical_completion`。因此 shell leader 退出后，仍在受管 group 中运行且已关闭 stdout 的后代也继续占物理容量，仍可被 kill，不能提前唤醒“完成”。

`manager.py:1347` 的终止无论 leader 是否已退出都面向整个 group；`_join_physical(:1372)` 要 join reader、watcher、timer、关闭输入、确认 group 消失。不能把 `Popen.poll()!=None`、node-pty exit callback、ConPTY close 返回、控制台无输出、Job termination 请求已发出等价于物理完成。

Windows范围空的当前查询可以使用 `QueryInformationJobObject(JobObjectBasicAccountingInformation).ActiveProcesses == 0`；IOCP只作唤醒，不能仅凭缺消息或只等单个通知决定完成。普通Job handle wait不是通用“所有进程都结束”的替代。精确root退出码取得后process/thread handle释放、ConPTY附属host和helper计数均要在G0实测，否则范围查询可能永不归零。见[Job状态查询](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject)与[ActiveProcesses定义](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)。

现有 resource 边界是 8 个正在启动或未完成物理结算的执行槽，16 MiB 每进程 retained、128 MiB Host retained；完成记录惰性目标 32、TTL 3600 秒。不得把 Windows 支持变成总进程数、总轮数、总运行时长或 worker lifetime 新上限。monitor 的既有 expiry 不终止原 process。`TerminalRequest.max_lifetime_seconds` 是内部显式可选值，模型 schema 没有命令寿命限制。

### 代码中直接阻塞 Windows 的点

| 当前点 | 为什么 Windows 不能原样用 | 最小职责 |
|---|---|---|
| `manager.py:11 import pty` | Windows import 失败，间接阻断整个应用导入 | 按平台加载后端，通用 owner 可导入 |
| `manager.py:506 openpty`, `Popen(start_new_session=True)` | POSIX PTY/session，不是 ConPTY | 后端提供 spawn/input/output/physical scope |
| `killpg`, `SIGKILL`, group probe | Windows 无 POSIX group；`Popen.terminate()`仅直接进程 | Job Object + 精确 process handles + scope emptiness |
| `manager.py:786 close_stdin` | POSIX PTY EOT 不能直接映射 ConPTY | PIPE 真 half-close；PTY EOF 必须另证 |
| `environment.py:405` shell detect | `SHELL`, `/bin/zsh/bash/sh` | 私有 pwsh 绝对路径 owner |
| `environment.py:52`, `:266` snapshot | `-l -i -c`, `printf ...; env -0`, selectors 读 pipe | PowerShell probe、Windows pipe drain、同一取消/join owner |
| `.venv/bin`, 固定 Unix PATH | Windows venv 是 `Scripts`; PATH `;`; env 名不区分大小写 | 平台路径/大小写 owner；default-deny 不变 |
| `_INERT_NAMES` | 缺 `SystemRoot`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `PATHEXT` 等必要 Windows 名 | 根据真实依赖最小 allowlist；诊断只给名称/数量、不打印实际 secret |
| `capability/terminal_risk.py` | POSIX regexp 看不到 `Remove-Item`, `rd`, `Format-Volume` 等 native 动作 | native dialect 风险判断在执行边界生效，不只改提示 |
| `_render_environment(context_sources.py:2066)` | 只有 workspace/timezone 等，没有真实 shell/platform 信息 | 冷 epoch 源中投影 Windows + PowerShell + 路径规则，运行 epoch 不改 SYSTEM/tools |
| `v3_gateway.py:193` / protocol client `:105` | asyncio Unix socket 在 Windows 不受支持 | 保持帧/auth/owner语义的平台 transport，另由 transport 工作包闭合 |

`_resolve_workdir(manager.py:1596)`目前用 pathlib resolve 和权限 scope；Windows 需与文件系统 owner 一起证明 drive/UNC/reparse/大小写 containment，不能自己用字符串替换另做一个权限来源。`workdir` 相对路径继续基于 workspace，不基于后来的终端 cwd。后端 shell 选择变化不能在旧 provider epoch 重新生成 SYSTEM/tools。

## 2. Codex 原码调研

相邻仓库：`/Users/plumliu/Desktop/python_workspace/codex`。读到的 AGENTS 只在 TUI bottom_pane 和 exec-server sandbox_integrity，未进入受其约束的实现修改。全部只读。

### 可借鉴机制

| 代码 | 实际做法 | 对 Pulsara 的意义 |
|---|---|---|
| `codex-rs/utils/pty/src/pty.rs` | 通用 process handle；Windows 选择自带 ConPtySystem；reader/writer 与 exit 分开；写端 normalizer | 平台后端应位于物理资源层，不复制模型工具/monitor |
| `.../win/conpty.rs` | ConPTY + stdin/stdout pipes；portable-pty traits | 可复用现成 terminal primitives；不是复制 Rust 源变 Python |
| `.../win/psuedocon.rs:170` | `STARTUPINFOEXW` 同时携 `PSEUDOCONSOLE` 和 Job 属性；CreateProcessW 一次完成；Unicode env | 原子 Job attachment 有明确平台 API，可避免 spawn 后加入竞态 |
| 同文件 `:222` | 可用时 ReleasePseudoConsole；Windows 11 24H2+ 输出释放行为不同 | Windows 版本/具体控制台实现必须在原机验证，不能只按 OS 名推断 |
| `.../win/job.rs` | kill-on-close Job；可选择禁止 breakaway；process handle 先捕获再 terminate，避免 PID 重用 | 精确对象所有权优先于数字 PID 与 taskkill 扫树 |
| `.../windows_input.rs` | LF→CR、跨写入CRLF去重、BS→DEL，其他UTF-8/控制字符透传 | ConPTY 输入需要状态机；但 Pulsara 的 write 原样合同不可直接照搬该全局归一化 |
| `.../windows_tests.rs`, `.../win/job_tests.rs` | no-console PIPE、后代终止、正常退出保留、Job 初始化失败、PTY输入行为 | 原机回归应覆盖无父控制台 Electron，而非仅终端启动测试 |

### 不可照搬的语义

`codex-rs/utils/pty/src/pipe.rs:131` 明写 Windows PIPE containment 是 best-effort：Tokio spawn 返回时 root 已运行，之后 AssignProcessToJobObject，期间的后代可逃逸；失败可降为直接 process termination。`pipe.rs:274` 正常 root exit 会 `preserve_descendants()`。`win/job.rs` 也有允许 breakaway 的默认值、assignment失败后无 containment继续resume 的路径。

这些都是 Codex 自己的合同选择，Pulsara 不可援引“Codex也这么做”来放松当前物理结算。Pulsara 的 native PIPE 必须从用户命令执行前就建立受管范围；Job 失败应是未执行的启动失败或保留可结算的精确失败 owner，不能 uncontained fallback。正常 root 退出且后代还活时仍 running，直到范围空且输出 drain，不自动 kill 正常后代，也不直接报完成。

若选 Rust helper，`portable-pty` 是机制复用候选，但它单独不含 Pulsara 所需的 Job emptiness/禁止breakaway/EOF/精确settlement 合同。Codex 私有 crate 存在复制自 WezTerm 的文件且与 workspace 绑定，不能当成现成独立稳定 SDK；需要包/许可/维护责任评估。

## 3. deepseek-harness 原码调研

相邻仓库：`/Users/plumliu/Desktop/python_workspace/deepseek-harness`。已读 `packages/AGENTS.md` 与 `docs/AGENTS.md`；只读生产代码和文档，没有在该仓库写文件。

### 分开两条真正不同的机制

普通进程使用 `packages/subprocess/subprocess-local/src/windows-job.ts` 的私有 runner；`packages/subprocess/win32-process/src/process.ts` 的 `spawnCurrentTokenJobProcess` 用 CreateProcessW + Unicode env + CREATE_SUSPENDED，Assign Job 后 ResumeThread。Job active process count 到0才结算 managed range，direct exit另行投影。这个“direct exit ≠ managed range empty”的边界与 Pulsara相近。

PTY 使用 `subprocess-local/src/index.ts:290` 的 node-pty，并 **没有** 为 Windows PTY走上述 ordinary Job runner；Windows options固定 `useConptyDll:true`。源码解释系统 console host 在实际 Windows 10/Server 2022 上会重绘 prompt tail，使 readiness不能匹配；它使用 node-pty随包的 OpenConsole。不能错误断言 deepseek 所有终端已由 ordinary Job 覆盖。

### PTY层做了什么

`subprocess-local/src/terminal.ts` 的 LocalTerminalHandle负责输出 backpressure、write/resize、前台 signal、终止与quiescence。Windows SIGINT写`\x03`；SIGTERM/SIGKILL走进程 inspector；不支持的POSIX signals明确拒绝。`windows-inspector.ts`用 Toolhelp32 + GetProcessTimes + wait-state liveness，PID重用有创建时间 fence。无POSIX foreground pgid时用shell PID作为pseudo group，不能观察 stdin waiting。

Windows taskkill可能不触发node-pty exit callback，`terminal.ts:369`起的close逻辑通过精确identity/liveness重新确认；确认shell消失后可补settle结果`exitCode:null,signal:null`。这不能直接适配 Pulsara 当前`exit_code:int`，必须有确切结果/未知结果模型与owner，而不能伪造0或-1来冒充自然退出。

`packages/terminal/terminal-bash/src/index.ts` 的persistent pwsh使用prompt function发OSC marker，UTF8 preamble，并借 `@xterm/headless` 回应terminal query；它明确是行导向persistent backend，full-screen不在其合同。Pulsara当前工具允许TTY行编辑/fullscreen，不应引入其prompt/readiness机制，也不应把其line contract当成完整PTY支持证明。

`patches/node-pty@1.2.0-beta.15.patch`对Windows kill时dispose conout worker做本地修补；另有 `apps/desktop/tests/windows-pty-exit.spec.ts`。因此“用node-pty就自动关闭正确”缺乏证据；若采用，必须锁定版本、随包native资源和具体exit-worker修复，并检查上游是否已经提供。

### 依赖复用判断

| 候选 | 可以交给依赖 | Pulsara仍拥有 | 当前未闭合点 |
|---|---|---|---|
| Python `pywinpty` low-level PTY | ConPTY创建/read/write/resize，编码桥 | Job准入、scope emptiness、exact owner、drain/join/settlement | spawn不接受Job/creation属性；没有公开输入half-close和显式dispose；需原机/源码核实 |
| `pywinpty.PtyProcess` high-level | Python file-like façade | 同上 | localhost socket+daemon reader、magic sentinel过滤、terminate/sendeof不足以满足精确输出和关闭；不推荐 |
| node-pty helper | maintained ConPTY/OpenConsole I/O/resize | Python owner、helper生命周期、Job范围、协议、input取消 | Node/ABI/worker/asardll打包；node-pty kill/exit差异；Job原子接纳还需证明 |
| portable-pty/Rust helper | portable PTY traits/ConPTY机制 | Job策略、scope emptiness、helper生命周期、typed结果 | 新Rust打包链、public extension点是否足够；不能只copy Codex私有实现 |
| pywin32 | Windows process/Job/handle操作 | 产品所有权与结算 | 可承担PIPE suspended-create/assign/resume；不能凭它推断pywinpty内部spawn有hook |

G0首先验证维护库原有extension点；只有确实不能满足时，记录确切缺口，考虑很小的 helper/adaptation 或向上游增加受支持参数。不得为Windows支持复制一套ConPTY parser、term emulator、进程树扫描框架或通用调度器。

`pywinpty`低层官方源码的public `spawn(appname,cmdline,cwd,env)`没有Job/creation flags；高层`PtyProcess`额外起loopback socket与daemon reader，空读取用`0011Ignore`作内部哨兵，`sendeof()`仅发Ctrl-D。以上判断来自[PyPTY绑定](https://raw.githubusercontent.com/andfoy/pywinpty/main/src/lib.rs)与[Python façade](https://raw.githubusercontent.com/andfoy/pywinpty/main/winpty/ptyprocess.py)，不能只依据API名称。

## 4. native Windows 设计的冻结项和 G0

### 建议冻结的产品选择

- Windows 11 25H2 x64 原机作为验收基线；不承诺Win10或ARM64；能力检测不能只写OS build字符串判断。
- 私有随包PowerShell7.6 LTS，一个默认dialect；不因发现系统powershell.exe/pwsh.exe/git-bash而悄悄换语言。G0锁定受支持补丁、签名/来源、runtime清单和后续安全更新owner。
- PIPE仍是默认，ConPTY仍由`tty=true`显式请求；输入、输出、初始等待、真实进程寿命、monitor含义不变。
- 8物理槽是并发边界，完成记录/保留边界继续现有值；无新总次数或寿命上限。
- Job范围是新的平台“进程组”实现。没有breakaway、没有adopt外部PID、没有Job失败后继续运行。借助WMI/外部服务创建而未入Job的进程不应无条件声称可管理；与当前POSIX group而非任意任意后代的边界一致，需要明确“受管范围”的定义。
- no-profile执行和profile snapshot需分清：现行POSIX command是`-c`，环境由受限startup probe收集；Windows不能把任意用户profile作为每次执行前置。建议命令NoProfile；probe在隔离env及受管范围中有界执行PowerShellprofile并仅采纳允许的env值，不保留函数/alias，不混同永久shell。

### G0 最小可运行探针（必须 Windows 原机）

| 编号 | 证明目标 | 必须观察的证据 | 失败后动作 |
|---|---|---|---|
| T0 | 包与API闭合 | 选定wheel/native DLL/运行时能在无开发工具的Windows11 x64加载；读安装版本实际源码，不只main分支 | 不开始生产backend替换 |
| T1 | 原子受管准入 | 第一个用户instruction之前已入不可breakaway Job；极快spawn-grandchild、退出root、后台持stdout、关闭stdout等都受控 | 拒绝post-spawn assignment方案 |
| T2 | PIPE关闭与drain | shell exit早于后代、末尾无换行、大输出、子孙关输出但继续工作；完成在range empty+reader joined之后 | 不缩减物理完成定义 |
| T3 | ConPTY退出所有权 | root exit、后代live、附属/无console、输出query/大尾部、终止中write、close race；精确资源最终释放 | 记录依赖extension缺口，停止 |
| T4 | PTY EOF | close_stdin后Python.readline/REPL、.NET Console.ReadLine、常见readline程序行为；目标活着且能继续输出；输入端退休无后续write | 不用ClosePseudoConsole伪装EOF；修订精确平台合同 |
| T5 | 长command | 1,048,576字符（ASCII和非BMP Unicode）通过短bootstrap执行，输入stdin仍归用户程序，错误行/exit显式验证 | 不缩小schema；另找supported script channel |
| T6 | PowerShell语义 | 正常输出/显式exit7/native exit7/terminating error/nonterminating error/parse error、throw/trap/finally、重定向、引号与换行 | 明确单一行为并修订contract，而非自动换-File/-Command路径 |
| T7 | close/cancel/rollback | 每个post-spawn失败点、重复cancel、Host关闭等待取消、reader启动失败、Job分配失败、进程已自然结束时kill | 不报已终止/已释放容量直到已确认 |
| T8 | 无控制台场景 | 从Explorer/Electron启动，中文用户名/空格路径，PTY交互输入、PIPE无弹窗、Host关闭 | 终端内跑通不能替代 |

设计上可考虑“使用维护库的隔离helper，用户目标启动前先完成helper/Job gate，再用公开spawn”，但这目前只是待证假设，不是本报告已选实现：ConPTY host是否计入Job、helper自身对active count的影响、parent crash、bootstrap失败、库析构/reader wake-up都需要T1–T3证明。禁止在spec里把这个猜测写成“库已提供原子Job”。如依赖没有可用extension，G0输出应明确最小上游/adapter变更及独立必要性，再决定后续工作。

### PTY EOF 是真实不确定项

POSIX实现目前发送canonical EOT；`tests/test_round2_terminal_output.py:607` 的实际目标是 `/bin/sh -c cat`，断言close后cat成功退出，并非任意raw-mode应用保证。Windows没有POSIX line discipline；Ctrl-D不能假定为EOF，Ctrl-Z+Enter由CRT/应用解释，不能泛化成底层read handle half-close。ConPTY `ClosePseudoConsole`会向仍连接的client发送关闭事件；不能把它用于“只关闭输入而命令继续”。Windows11较新实现close返回也不等于clients全退出，仍需drain。Microsoft的[ClosePseudoConsole合同](https://learn.microsoft.com/en-us/windows/console/closepseudoconsole)直接支持这些区分。

这不意味着所有PTY程序都不可能退出输入，而是需要冻结准确的产品承诺：当前POSIX EOT本来也不保证raw-mode应用读到EOF，Windows应明确其支持的terminal EOF行为并测真实程序。如果G0不能保留被宣称的合同，必须先修订tool说明/结果/测试；不能仅新增skip、换字节而不解释行为，或把`stdin_closed=true`当作child确实已EOF的证据。

### 输入newline与输出编码

现行`write`说原样UTF-8、`submit`追加换行。Codex把所有LF转换为CR的normalizer不能直接搬进Pulsara，否则会改变write的原样含义。建议分离“原样text write”和“submit产生平台Enter”在物理adapter的映射，并在模型合同中说清；如果需要跨调用CRLF去重，状态只属于确切process输入owner，旧进程关闭即退休。测试write包含LF/CRLF、跨write拆分CRLF、literal Ctrl-D/Ctrl-Z/Ctrl-C、中文/emoji、BS/DEL，不能仅测`print('ok')`。

PowerShell7默认UTF8优于5.1；仍显式设置`[Console]::InputEncoding`、`[Console]::OutputEncoding`与`$OutputEncoding`到UTF8，并测试PIPE及ConPTY。ConPTY输出是UTF8/VT；PIPE下任意第三方CLI仍可能自行选择OEM/legacy codepage，不能由preamble宣称已控制一切。保持输出owner的真实字节/损失信息和增量decode策略，不能通过宽泛redact或丢坏字符隐藏错误。PowerShell编码差异见[官方编码文档](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-7.6)。

### 长command与exit

CreateProcessW的command-line最多32767 UTF16 code units含NUL；Base64 EncodedCommand会扩大而非解决。`-Command -`/`-File -`占stdin，会破坏现有交互。推荐统一使用短、固定bootstrap读取本次受保护UTF8命令文件，目标stdin独立；文件由同一launch owner创建，在physical join后清理，失败rollback也清理。路径要按argv/PowerShell参数域处理，不能用JSON.stringify代替shell quoting。文件属于临时物理资源，不是新durable command记录。

`-File`与`-Command`的默认错误/退出行为不同，不可对短command使用一种、长command悄悄用另一种。G0可检验固定bootstrap + ScriptBlock.Create是否保持选定`-Command`语义；必须测试parse error、explicit exit、native code、ErrorAction、finally及无换行尾输出。PowerShell的行为以[about_pwsh](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh?view=powershell-7.6)为准，代码必须返回真实shell exit，不把Unix numeric mapping臆造到Windows。

### 环境、安全与提示

env名称在Windows大小写不敏感，`PATH/Path`、passthrough/deny、snapshot合并与去重必须归同一owner。加入Windows必需env应逐项验证依赖；不能直接inherit全部parent env。`SystemRoot`等系统位置与私有runtime选取不应由模型command文本控制。nearest venv改为`.venv/Scripts`，限定workspace祖先，private ripgrep路径前置逻辑保留。

profile probe的stdout不是可执行指令/控制权，不信任banner；采用机器可分帧结果、只收env、仅成功且scope已空才缓存；保留5秒/1,000,000bytes既有单操作边界、singleflight、关闭join；selectors不支持Windows pipe，要用维护库/现有thread机制，不能简单换SelectorEventLoop来回避。

native风险语法至少覆盖Remove-Item及常用alias、cmd.exe显式调用、rd/del、Format-Volume/Format-Disk/Clear-Disk、shutdown、系统根与用户home、凭据路径。静态regex仍不是shell sandbox；不声称能证明动态PowerShell、反射、变量/alias和编码命令所有效果。沿用用户授权模式，直接executor强制，必要时复用有维护的PowerShell AST API而不是自行复制parser。

实际OS/shell/path规则加入现有环境context owner和tool指导，随cold epoch/明确adopted compaction successor安装。进程运行时发现shell/权限/插件变化不得中途重写SYSTEM/tools。无需新增event、subject、guard、relation、durable job、fingerprint、receipt或恢复机制。

## 5. Windows Electron + WSL2 entire backend 备选

这是一条独立产品路线：Python、terminal/hook/MCP/plugin执行、文件系统owner、Pulsara home、PostgreSQL/pg_search都在同一个Linux环境。Windows Electron只做受验证的UI/文件选择/导航边界。不能把大部分backend放Windows、只把terminal命令包`wsl.exe`，那会产生两个路径/进程/环境/权限authority，且无法用Linuxkillpg管理Windows一侧wrapper及interoperability进程。

| 方面 | Entire WSL2 backend | Native Windows backend |
|---|---|---|
| shell/commands | Linux bash/zsh及Linux工具链；现有terminal机制可复用 | 固定private pwsh7及Windows工具链，需native语义 |
| Python代码 | pty、killpg、selectors、Unixsocket等大部保持Linux机制 | 平台后端替换并验证 |
| workspace | backend权威为`/home/...`或`/mnt/c/...` | `C:\...`/经支持的UNC |
| user home | distro内Linux home/Pulsara配置；与Windows配置不是自动同一实体 | Windows实际user/Pulsara home |
| capability | 安装并运行LinuxCLI/MCP/hooks；Windows可执行包不是直接等价 | WindowsnativeCLI/MCP/hooks |
| physical lifetime | Linuxscope可用；Windowsinterop进程需明确排除或另管 | JobObject范围 |
| dependencies | Linuxpg_search等可能直接复用发行包（仍需检查架构/版本） | nativepg_search等另有G0 |
| UX/ops | WSL安装/虚拟化/启动错误/distro磁盘/网络转发/更新/注销责任 | 单系统runtime和native资源更新 |
| 证明风险 | 大量现有Linux tests可执行，路径/生命周期边界是新风险 | 更多终端/文件/打包机制原机风险 |

Microsoft建议Linux命令工作负载把工程放WSL文件系统以获得最佳性能；Windows文件通过`/mnt/c`访问有不同元数据/大小写/权限语义。Explorer可见`\\wsl$`路径。不能简单全局`C:\`↔`/mnt/c`替换：UNC、网络盘、subst/volume、中文/空格、reparse、Linuxsymlink、离线文件与边界拒绝都需要真正的路径适配。相关事实见[Microsoft跨文件系统说明](https://learn.microsoft.com/en-us/windows/wsl/interop)。

### WSL明确增加的工作包

- 确定唯一专用distro及版本、安装包/更新/默认user/root bootstrap，不占用或修改用户已有distro/global`.wslconfig`。安装WSL和虚拟化可能需要管理员/重启，必须是显式产品前提。
- Electron启动backend的私有launch/auth通道、stderr诊断、startup handshake、ready条件；不把WSLhost IP固化。Linuxserver与Windows UI通过localhost forwarding等机制连接必须测试VPN/防火墙/WSL重启，保持现有token/Origin/attachment保护。
- selectedWindows路径→Linux可访问路径为边界操作；model/hook/MCP看到同一Linux权威路径；文件导入/export/open/reveal分别转换，不把Linux path传给Windows shell猜测。
- 保存配置来源、home和DBDSN必须明确Linux解释；不要把Windows原生savedsettings悄悄copy成第二credential store或dump secret。
- 正常关闭通过backend既有close协议join任务/terminal后退出；不能以kill `wsl.exe`证明Linux后代已退出，也不能用全局`wsl --shutdown`作为日常close。若是专用distro，有可解释的强制`--terminate <owned distro>`最后手段，仍是异常状态而非已完成所有副作用。
- 用户手动terminate distro/WSL崩溃/Windows休眠恢复后，旧Host/process/cursor失效；不恢复执行或自动重放。数据库/持久canonical rows按既有restart合同恢复，进程本地状态不增加durability。
- Windowsinterop能启动Windows `.exe`，其生命周期不能凭Linuxpgid证明；若要保持单域合同，专用distro可明确关闭interop并不支持WindowsCLI。该语义需要用户接受，不能只把PATH隐藏当执行边界。Microsoft的[WSL配置](https://learn.microsoft.com/windows/wsl/wsl-config)明确`interop.enabled`及`appendWindowsPath`分别控制执行能力与PATH。

Microsoft的[WSL基本命令](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)区分全VMshutdown与单distributionterminate；[WSL网络互操作](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)说明localhost方向与网络模式差异。上述机制提供可用组件，不免除Pulsara自己的单owner关闭、auth、路径与错误呈现。

推荐：当前native目标已经由root明确冻结时，WSL只保留为需用户另行接受的替代产品方案。如果G0证明native依赖无法保留合同，应把完整失败证据提交，再选择native产品边界修订或独立WSL产品，不能自动双路径运行。

## 6. 详细实施工作包

| 包 | 代码范围/owner | 完成定义 | 预估工程日（单熟悉项目工程师） |
|---|---|---|---|
| G0-T | 本目录/后续原机probe，维护库API与版本 | T0–T8原机输出、资源实测、不可满足点明确；不写生产fallback | 3–6 |
| T1 | terminal_process物理backend接缝 | 通用Registry继续唯一semantic owner；POSIX实现隔离但行为不变；无Windows导入失败 | 2–4 |
| T2 | WindowsPIPE+Job owner | command执行前containment、真实direct exit、rangeempty、reader/watcherjoin、no-console、全rollback | 3–6 |
| T3 | WindowsConPTY adapter | 长交互、input/output、EOF合同、full-screen/query、kill/drain/close、无worker/handle泄漏 | 5–10 |
| T4 | PowerShell+environment | privatepwsh、UTF8、短bootstrap、长command、profileprobe、venvScripts、大小写env、secretboundary | 3–6 |
| T5 | model+permission | platform/shell/path冷epoch提示、Windows风险规则/执行校验、无prefixrebase | 2–4 |
| T6 | tests+dogfood+原机包 | 验收矩阵、真实provider交互/长程、Electron无console、资源稳定、失败evidence | 4–7 |

终端专属合计 **22–43工程日**，含G0且有重叠优化空间；若G0需上游或小nativehelper扩展再增加约5–12工程日，第三方响应时间不受控。不是整个Windows端总预算，不含pg_search、no-follow filesystem、私有transport、installer/updater、签名、第三方CLI适配等其他P0。两位工程师合理并行后的日历时间仍受G0与原机验证串行依赖，不能简单除以2。估计基于当前代码耦合和明确场景，非承诺。

WSL备选对“终端代码移植”可能只需3–6工程日，但完成专用distro生命周期、双域path、打包/引导、网络/auth、配置/数据位置和原机验收的额外系统工作粗估10–20工程日；不能用“终端能跑bash”作为整个Windows产品成本。此估计同样不含全部发布/签名工作。

## 7. 验收矩阵（不加skip/xfail，不用OS模拟当原机证据）

| 类别 | 必须场景 | 通过条件 |
|---|---|---|
| 默认调用 | 只填command；relative/absolute/workspace/outsidepermission；两目录交替；命令内cd/export | 每次独立shell、cwd准确、无跨调用伪状态 |
| 长command | 现有字符边界ASCII/中文/emoji；引号、反引号、美元、换行、脚本语法错误 | 无CreateProcess超长；不占stdin；shell语义单一；文件受保护并清理 |
| PIPE交互 | `python -u`多轮readline、无换行小输出、delayedreply、submit/write、输入EOF后尾输出 | 输出增量、精确输入；yield/wait仅观察窗口；不误判完整回答 |
| PTY交互 | Python/NodeREPL、readline行编辑、中文emoji、CR/LF/CRLF拆包、BS/DEL、Ctrl字符、terminalquery/fullscreen | 长期process可用；输入不多发/不遗漏；无query阻塞 |
| EOF | PIPE halfclose；PTY受支持EOF模式；raw-mode明确边界 | 不用kill/consoleclose伪装stdinclose；目标结果真实 |
| 子孙 | root先exit、后代holdstdout、后代关闭stdout但存活、快速grandchild、jobbreakaway请求 | 物理范围未空不完成/不释放容量；禁止未管辖运行fallback |
| 控制竞态 | 自然结束同时kill；read/write/close/kill并发；取消在写入前/后；重复close | 精确原process，不重发已知/未知输入；终态不被迟到kill改写 |
| launch故障 | Job/PTY/PIPE创建失败，assignment/resume失败，reader/watcherstart失败，owner关闭跨spawn | 一次rollback拥有全部资源；没有孤儿；容量不早释 |
| Host隔离 | ROOT/child权限、foreignID、childclose、Hostclose、newHost旧ID、compactionhandoff | 相同owner继续活process；关闭后失效；不接管/重放 |
| 输出 | 大输出、末尾无换行、多字节拆chunk、ANSI/OSC、badUTF8、actualsecret、截断artifact | 原有sanitizer/cursor/GAP/sourcecoverage/补读同范围；只遮实际凭据 |
| monitor | running注册、完成注册竞态、progress/heartbeat/expiry/cancel、compactionsafe-point | 以physicalcompletion唤醒；取消monitor不kill；不增durability |
| 容量 | 8满，第9未开始；leader退出但后代存活；部分joinfailed | 只有真正释放后才准入；无新总命令/寿命cap |
| 环境 | mixedcasePath、secret-shaped值、SystemRoot/USERPROFILE、venvScripts、profiletimeout/oversize/descendant、shutdownprobe | defaultdeny，PIPE/PTY相同有效env；probe缓存只成功且已join |
| GUI运行 | Explorer启动打包Electron、无console、中文用户名/空格路径、受限用户、杀窗口/正常quit、sleep/resume | 无额外console窗口；可信错误；物理owner完成或明确失败，不假成功 |
| 长程/provider | 同一Host多轮交互/监控/compaction、多个排队child、长输出保留压力、上千次短任务周转 | 无总寿命/调用cap；资源回落；SYSTEM/tools前缀稳定；真实工具参数/结果可查 |

现有可直接映射的回归：`tests/test_round2_terminal_output.py:290,334,390,426,452,541,607,745`，`tests/test_round2_terminal_monitor.py:81,321,457,534`，`tests/test_pr03_process_termination.py:46,125,156`，`tests/test_stage2_terminal_host_lifetime.py:220,339,389,455,649`，`tests/test_terminal_cognitive_subtraction.py:111,136,201,252`。平台无关断言继续公用；真实OS机制用各平台等价probe，不以skip把缺能力藏起来。profile、shellcommand测试应使用声明的dialect，不把POSIX脚本文字硬塞PowerShell后只改期望值。

## 8. 本轮验证、未知项与交付限制

本轮已在根`.venv`执行：

```text
.venv/bin/python -m pytest tests/test_terminal_cognitive_subtraction.py tests/test_round2_terminal_environment.py tests/test_pr03_process_termination.py -q
24 passed in 3.72s
```

它确认读取的macOS生产基线及相关语义仍成立；未运行Windows机制、未运行真实providerdogfood、未改/重置DB、未读出savedsecret。本轮不产生Windows可用声明。

尚未知且必须G0闭合：维护库具体release wheel及native依赖；ConPTY lifecycle可公开控制程度；Job创建/嵌套/consolehost范围；PTY输入EOF在所支持应用中的真实表现；长scripttransport的exit/parse/line定位语义；无父console输出/worker关闭；Windows平台prompt与风险policy的充分度；privatepwsh发行包的支持补丁/安全更新责任。

可在这些未知尚未运行时冻结设计目标和可检验失败条件，但不可在spec里同时声称“功能完全保留”和“依赖已满足”。如果原机不能证明一项已保留合同，后续必须修改有权威的产品规格并说明行为改变；不能让工程师在实现中自行决定降级。

## 9. 官方机制参考（均为本轮查看的primary sources）

- [Microsoft：创建Pseudoconsole会话](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)：同步I/O通道，独立输入输出线程、drain与关闭的注意事项。机制支持复用，不含Pulsara Job/settlement。
- [Microsoft：ClosePseudoConsole](https://learn.microsoft.com/en-us/windows/console/closepseudoconsole)：关闭事件、较新Windows异步返回、输出继续drain。
- [Microsoft：JobObjects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)：默认childmembership、breakaway、范围终止/查询；不是任意外部服务树的收集器。
- [Microsoft：UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)：JOB_LIST可在创建时安装；支持属性列表，不必先让用户目标执行。
- [Microsoft：CreateProcessW](https://learn.microsoft.com/en-us/windows/desktop/api/processthreadsapi/nf-processthreadsapi-createprocessw)：命令行边界、Unicodeenvironment、传参与handle职责。
- [Microsoft：ProcessCreationFlags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)：CREATE_NEW_PROCESS_GROUP不等价POSIXsession、CTRL+C行为、CREATE_NO_WINDOW差异。
- [Python：asyncio平台支持](https://docs.python.org/3/library/asyncio-platforms.html)：Windows默认Proactor，Unixsocket与add_signal_handler不能照用，Selector不能监控普通pipe。
- [Pywinpty官方绑定与源码](https://github.com/andfoy/pywinpty)：publicAPI审阅；本轮main源码是调研材料，不是可发布版本锁定。
- [Microsoft：about_pwsh](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh?view=powershell-7.6)与[编码](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-7.6)：单一shelldialect和执行语义需要实测。
