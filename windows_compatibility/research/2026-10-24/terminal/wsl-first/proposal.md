# WSL2 全内核：终端、控制管道与生命周期建议

日期：2026-10-24。作者专题：terminal。状态：给根规格汇总的只读研究建议，尚未执行 Windows G0；不代表产品已验证可激活。本轮未改生产、未运行测试、数据库或 provider。

## 1. 推荐冻结的唯一执行拓扑

Windows 原生 Electron/Node 负责窗口、托盘、原生对话框、浏览器宿主、安装与既有 BundledPostgres 生命周期协调；专用 `Pulsara` WSL2 发行版负责全部 Python 内核、terminal、MCP stdio server、hooks、截图 worker/Chromium、PostgreSQL/pg_search 及其 Linux 数据。不要同时保留 native Windows Python/ConPTY/PowerShell、用户默认发行版或任意现有发行版作为自动替代路径。

发行版镜像包含 `pulsara` nonroot 用户、固定受控的 Linux 资源根和 shell；每个正常执行入口都显式给出发行版与 `--user pulsara`，不依赖 Windows 的默认发行版、默认用户、当前工作目录或 shell。`/etc/wsl.conf` 由镜像部署：

```ini
[user]
default=pulsara
[interop]
enabled=false
appendWindowsPath=false
```

不为本产品开启 systemd 或 boot command 来托管内核/数据库；沿现有 Electron 的显式启动/关闭协调。专用用户不配置 passwordless sudo。镜像配置更新必须在没有 active owner 的部署阶段生效，不能启动期间覆盖文件后假定运行中的发行版已切换配置。

`enabled` 与 `appendWindowsPath` 是两个不同配置；前者控制 Windows 进程启动，后者只控制 PATH。Microsoft 的 interop 实现还会在没有 `WSL_INTEROP` 时沿父 PID 寻找 interop server，因此清理环境变量也不足以禁用该机制。首版专用发行版比检查/改写现有用户发行版的权限、shell、apt 状态、interop 和长期服务简单且可验收；保留现有发行版支持会重新打开进程跨边界 owner 问题。[配置说明](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)、[interop 架构](https://wsl.dev/technical-documentation/interop/)。

这不是针对恶意 Windows 用户、Linux root 或用户自行修改发行版的安全沙箱。WSL 与 nonroot 配置保证的是支持环境中的执行域，不能声称拦住所有主动绕过行为。G0 必须证实所锁 WSL 版本下这些配置实际生效，而不只检查文本。

## 2. 当前生产语义及保留范围

实际源码根均为本仓库 `src/pulsara_agent/`，以下引用生产路径，历史常驻 shell 文档不作为 authority。

| owner / 实际路径 | 当前机制 | WSL 路线要求 |
| --- | --- | --- |
| `terminal_process/manager.py:477` `_spawn` | 每 command 独立 `shell -c`；PIPE 或 `pty.openpty`；`start_new_session=True`；reader/watcher 线程 | 完整保留，绝非改成常驻 bash 并按 prompt 猜结果 |
| `terminal_process/manager.py:640` `_watch_process` | root wait → reader join → 原 PGID 消失 → 输出 finalize → physical completion | Windows carrier/root exit 不替代该顺序；PTY 与输出都留在 Linux |
| `terminal_process/manager.py:1347/1372` | root 已退出仍 terminate 原 process group；join root/reader/watcher/timer，检查原 group | 不改为只杀 PID；合同仍是 original group，并非任意逃逸 `setsid` 后代或整个发行版 |
| `terminal_process/manager.py:758/786` | write 的 observe 不承诺答案完整；submit 加 newline；PIPE close；PTY EOT 与独立读端 | 逐项保留；uncertain write 不重送 |
| `terminal_process/environment.py:44/261/481` | bash/zsh/sh 选择，登录环境 snapshot，`env -0`，selectors；默认拒绝敏感/主动环境；探针 close/join | 保留 Linux 实现；镜像至少提供既有支持的 bash/sh、Linux私有 rg 与 venv 布局 |
| `hooks/executor.py:260` 等 | 最后一刻 async credential guard 围住真正 spawn，取消时取回同一个 spawn；独立 deadline/env/frame/result | Linux 执行，保留原 hook policy，不借本轮统一为 terminal completion |
| `conversation_kernel/mcp/sdk_facade.py:299/346/470` | SDK 上层协议；Pulsara bounded stdio、secret env、spawn shield、close stdio/process/tasks | Linux stdio server 与 Linux executable/path；不把 Windows .exe 命令静默转换为可执行配置 |
| `conversation_kernel/visualization_screenshot.py:21/60` | 当前 Python 启动 Linux worker，PIPE3；取消/close 时 killpg 与 root wait | Linux Chrome/字体/系统依赖随资源安装，Windows Electron 只展示结果 |
| `desktop_runtime.py:179` `run_desktop_child` | 专用 JSONL stdin/stdout；EOF、shutdown、SIGTERM/SIGINT、status BrokenPipe 都进入原 `application.aclose()` | 复用，而非加第二套调度/settlement daemon |

原 live capacity、retention、snapshot TTL、命令 schema 字符数、deadline 与 provider 工具合同都不因 WSL 增加总时长/任务数限制。SYSTEM/tools/prompt 仅在既有 cold epoch/明确采用 compaction successor 边界表达 Linux/WSL 能力；重连、睡醒或发行版版本查询不改写已安装前缀。

## 3. Windows Node → wsl.exe 的实际边界

建议由一个窄 HostExec 构造直接 spawn/execFile，不经过 cmd.exe、PowerShell、`sh -c` 或字符串命令拼接。形状为 `wsl.exe --distribution Pulsara --user pulsara --exec <绝对 Linux 可执行文件> <argv...>`；若 cwd/env 需要受控 bootstrap，bootstrap 必须有限、保留 stdin，不让 shell profile 或启动噪声侵占 JSONL。选择明确 Linux cwd，不从 Windows Node cwd 自动映射。

保留两种既有需求：stream child 供 `PythonRuntime` 的长期 control pipe；finite execution 供 `pg_ctl`、`initdb`、`--wait-owner` 与少量有限文件 helper。不要形成一个通用远程执行 RPC 服务。有限 helper 接收结构化操作和 Linux 路径，返回类型化结果；不接受任意 Python/shell 代码，不启动第二 lifecycle owner，不进行 DDL。

WSL 官方说明 `wsl.exe` 取得 stdin/stdout/stderr、control 和 interop handles；按 pipe/file/terminal 选择不同 relay；Linux root 退出后 flush 剩余 IO 并返回其 exit code。但提前杀死 wsl.exe 时 wslhost.exe 会接管 interop channel。因此正常命令 root 退出码可以透传，Windows carrier 被结束则不证明 Linux root/后代被结束。[WSL boot/relay](https://wsl.dev/technical-documentation/boot-process/)。源码 `src/windows/common/relay.cpp:68–211` 将 broken pipe/EOF/取消读转为 relay 结束，并用 byte span 读写；这不构成应用的物理 join 协议。[Microsoft relay.cpp](https://github.com/microsoft/WSL/blob/master/src/windows/common/relay.cpp)。

Node 的 `close` 指它观察的 child 和 stdio handles 已关闭；`exit` 早于 stdio 完成。这里 child 是 wsl.exe。保持使用 close 收齐 Windows handles，同时禁止将该事件单独提升为 Linux cleanup success。[Node child_process](https://nodejs.org/api/child_process.html#event-close)。

控制管道必须保留 UTF-8 JSONL、stdout 协议专用/stderr 日志、startup frame 前不能使用 DB、ready 仍要求数据库 READY。真实 Windows Node→WSL G0 验证 argv 中空格、中文、引号、反斜线、空参数、长参数，stdin分片、反压、末尾无换行、二进制/Unicode stderr，完整 exit0/1/7 和信号退出。不依据主机终端手测 shell 能运行就证明 Electron pipe 场景成立。WSL 自身启动失败/本地化诊断属于 launcher failure，不当作合法内核 JSONL；不要假定所有 wsl.exe 管理输出均为 UTF-8。

## 4. PG owner 可保留，物理执行与文件语义必须归 Linux

当前 `desktop/src/postgres.ts` 只有 TS owner：`initialized/startedHere`、start/status/verify/stop/reset、角色 DSN；`desktop_runtime.py` 负责原 DDL/bootstrap。该分工可保留，无证据要求迁移成 Linux daemon 或重写 macOS/Linux lifecycle。

需要替换的是 `execFile(resourceRoot/postgres/bin/*)` 物理执行与 Node 本地 fs：Windows不能直接运行 Linux binary，Win `path.resolve/stat/uid` 不能校验 Linux postmaster.pid/private socket。HostExec 在同一专用 distro、同一 nonroot 用户执行 PostgreSQL。有限 Linux helper 仅实现本 owner 必要的 mkdir/mode/read/stat(real UID)/realpath/no-follow/remove 叶操作。Linux路径使用明确 `path.posix` 或 owner 已提供的绝对路径；Windows host path 只用于 Electron 配置/安装与日志，不能混入 PG DSN/socket。

保留原 `waitForPreviousOwner → postgres.start → start frame → kernel ready`，以及 `runtime.stop 成功 → postgres.stop → Electron exit` 和显式 reset 确认/旧 owner wait 的顺序。finite helper/wsl.exe spawn失败、信号退出或无效结果不得变成 `pg_ctl status` 的合法 code3，也不得被 `startedHere=true` 掩盖。不通过读猜测 PID 后跨域 kill。`pg_ctl stop` 必须等待真实对应 Linux 命令正常完成。

## 5. 最少接缝与收窄 Phase A

建议撤下此前为 native Windows 必选的 ProcessScope/TerminalBackend/ShellAdapter/全面安全文件 binding 抽取。全部 Python 调用仍处在 Linux，当前 POSIX 实现不必为 Windows 可导入而重构；macOS/Linux继续原行为。A 仅准备 WSL 实际需要、可在现有 Linux/macOS 独立交付的窄 seam：

1. `desktop/src/runtime.ts` 与 `postgres.ts` 用注入的 stream/finite HostExec；local 实现直接复用现有 spawn/execFile。不加平台 process registry。
2. `desktop/src/main.ts` 区分 host UI home 与 backend Linux home；把 Linux资源根、backend home、Linux日志/PG目录作为明确 typed values 传给 owner，不复用一个受 Win path 默认行为影响的 `home/resources` 字符串。local实现取值不变。
3. `postgres.ts` 仅抽出已列叶文件操作；Unix local owner 与有限 Linux helper 承担相同校验。没有需求的 terminal/file API 暂缓。
4. 保持现有 `desktop_runtime` control protocol；仅在证明 WSL 所需处补 launcher失联的类型化失败与 startup诊断，不加新 backend transport/长连 RPC/daemon。

后续 B 增加专用 distro provision/validate、WSL HostExec、Linux helper、Linux资源部署、Windows GUI路径选择接缝，G0-WSL 前不能声称Windows激活。每切片 hard cut 删除对应旧调用点；不同时保留旧路径自动fallback。A 不依赖尚未跑过的 G0；B 的受支持 WSL/镜像版本与行为必须在真实 Windows 上锁定。

## 6. 关闭、断管、休眠和重开

| 情形 | 产品/owner行为 | 必须避免 |
| --- | --- | --- |
| 普通关窗/最小化 | 沿现有 window close policy 留在后台；不结束 backend | 为关窗自动 terminate distro |
| 显式退出 | 原 inspectQuit/确认后 shutdown+stdin EOF；Linux 原 aclose；观察正常 stopped/无收尾失败与正常 exit0/pipe close 后 PG stop | timeout 后 taskkill wsl.exe 并报告已结算 |
| startup前 EOF/PG准备失败 | 原 stdin EOF 让 child 不取得 writer、不碰未ready DB；等待其退出 | 为取消准备而启动第二 kernel |
| Electron死/控制stdin断 | 真正的 Linux EOF 应触发原 close；G0观察 terminal/MCP/hooks/screenshot/DB顺序 | 仅测 mock StreamReader.feed_eof 就通过 Windows验收 |
| stdout/status断或 wsl.exe 被杀 | UI失联；已有收尾继续；未知结果保留未知；不凭 carrier close 继续停PG/成功退出 | `stop()` 因 child已清空就直接返回成功 |
| Windows sleep/resume | OS暂停不是用户取消；resume检查当前 origin/control，沿原 deadline策略与现 owner状态继续；不得因探针暂时失败新启backend | 每次睡醒重放命令/重建providerprefix/设任务总时长上限 |
| 外部 `wsl --terminate Pulsara`、`--shutdown` 或系统崩溃 | 强制全域终止是crash；UI明确失败，后续按既有cold启动/PG crash recovery；不伪造 prior tool success | 宣称强杀完成了 application settlement；新增durable recovery ledger |
| 重新打开 | 同一distro/user/home执行原 `--wait-owner` 锁排他后才启动新kernel/修改PG；不重新发旧process_id写入 | 把旧锁释放当成所有旧工具已成功结算 |

`desktop_runtime.py` 目前 finally 无论成功与否均释放 kernel.lock 并发 stopped；failed 时退出1。故 stopped 单帧不能代表成功，锁释放仅表明 old kernel owner 不再持有该锁。原锁可复用来防并发写主；不能用它填补崩溃丢失的 process-local outcome。原 in-memory完成/side-effect settlement仍由现有 owners负责；丢失后不回放、不增新持久事件或工作恢复机制。

应用日常退出不调用 `wsl --terminate` 或 `--shutdown`；后者还终止其他所有发行版。部署/卸载需要停止本专用发行版时，先原 graceful close，确认没有 owner，再明确操作目标。不要为回收少量空闲 VM 内存给任务增加隐性寿命。

## 7. GUI 工作区与执行环境建议

推荐默认工作区、backend home、PG data、socket、MCP/hook cwd 全位于该发行版 ext4，例如 `/home/pulsara/...`，Linux项目用 Linux venv/node_modules。原生GUI通过后端 workspace API选择/创建 Linux目录，向用户显示“Pulsara Linux”与真实 `/home/...`，不把Windows文件夹选择结果直接交给Linux tool。Microsoft也建议Linux工作负载文件放LinuxFS。[文件系统说明](https://learn.microsoft.com/en-us/windows/wsl/filesystems)。

首版如需 Windows源文件入口，必须冻结明确copy/import/export语义（产物由谁写、重名/覆盖确认、目录边界），或者冻结有限的 `/mnt/c` 工作区支持并接受其权限/性能差异；两者不能在实现时随手选择。本专题推荐首版 Linux ext4工作区+显式导入导出，避免把DrvFS当普通POSIX安全文件系统。不得从 Windows `C:\...` 猜转同名Linux路径，也不接受其他发行版 UNC路径冒充本发行版workspace。Explorer展示可由Windows host打开受校验的专用发行版UNC路径；Linux内不需要 explorer.exe interop。

Windows环境不整体通过WSLENV注入：Linux bootstrap给定明确 HOME/USER/LOGNAME/SHELL/PATH/PULSARA_HOME 与私有浏览器/rg资源，实际 credentials仍由Linux LocalSettingsStore原owner加载，经既有精确secret边界分发。MCP explicit secret env与hooks overlay语义保留，但它们的 executable、cwd、PATH属于Linux。Windows `.exe` MCP/hook配置给出可操作的Linux安装/配置错误，禁止偷偷开interop。

GUI访问仍采用 ready 中的 `http://127.0.0.1:<port>/`，保留Host/Origin/认证与原Electron可信页面校验。官方支持Windows→WSL localhost访问；必须在所支持网络模式实测端口冲突、睡醒/VPN/防火墙。[WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)。失败应报告受支持环境不可用，不自动bind0.0.0.0、不加全局portproxy、不改用户全局.wslconfig。

## 8. G0-WSL-T / G0-WSL-L 验收建议

验收代码与结果只在实现阶段由真实 Windows 11 x64/受锁 WSL版本执行；本提案没有跑过，下列均是计划。

| gate | 真实操作/故障 | 必须证据 |
| --- | --- | --- |
| T1 Linux执行域 | exact distro/user、Linux exe、shell/env、无 Windows PATH；absolute Windows .exe、清空WSL_INTEROP后interop尝试；并存用户发行版interop开启 | 正常入口nonroot且仍拒绝Windows进程；不修改用户其他distro/全局WSL配置 |
| T2 relay/argv | Electron所用Node真实spawn pipe3；argv边界；分片JSONL、反压、stderr与退出0/1/7/信号、启动失败 | stdout不污染，退出/flush/EOF符合协议；wsl.exe故障不能伪装Linux业务code |
| T3 terminal | PIPE/PTY cat、输入submit/close_stdin、root先退而同group child延迟写、SIGTERM忽略、已退root取消、输出压力、close取消竞态 | 原PGID消失+root/reader/watcher/timer join后才settled；各既有容量/保留边界不变 |
| T4 环境/secret | bash snapshot超时/输出界限/cancel、Linux venv与rg、实际secret值在argv/cwd/env/stdin检查 | 原最后guard与probe join；主机secret不经WSLENV/PATH意外传入 |
| T5 非terminal | MCP blocked stdin/stderr、hook超时取消、Chrome渲染取消/关闭，与PTY任务同时退出 | 各自现owner policy与资源释放成立，不靠统一root-exit状态 |
| L1 正常退出 | 活动terminal/tool、MCP、hook、screenshot与PG同期开启，显式Quit | application原close完整；正常stopped+exit；之后pg_ctl stop；再Electron退出 |
| L2 parent/pipe损失 | end stdin、destroy stdout、kill carrier、kill Electron，分别在start前/ready后/close中 | Linux端是否真EOF/收尾可观察；异常不报成功、不提前停PG；重新打开先wait旧owner |
| L3 suspend/reopen | Windows睡眠恢复、窗口隐藏/重开、网络变化、重开时原owner仍收尾 | 不创建并发kernel、不重发command/write、不改providerprefix；旧runtime待close仍被join |
| L4 distro外部终止 | `--terminate Pulsara`及系统/VM强终止；PG写入期间fault后重开 | 明确crash/unknown；Linux锁不存在后既有PG恢复，无自动清库/跨distrofallback |
| L5 部署升级/卸载 | 名称冲突、镜像版本不符、rootfs导入中断、更新时有owner、卸载后重装 | 不接管不明同名distro、不覆盖个人发行版、不丢用户Linux数据；配置切换只在停机部署边界 |

已有回归入口：`tests/test_desktop_runtime.py`（shutdown/EOF、lost status pipe、kernel lock）、`tests/test_terminal_cognitive_subtraction.py`、`tests/test_round2_terminal_environment.py`、`tests/test_round2_terminal_output.py`、`tests/test_stage2_terminal_host_lifetime.py`、`tests/test_pr03_process_termination.py`、`tests/test_mcp_transport_lifecycle.py`、`tests/test_mcp_sdk_transport_probe.py`、`tests/test_round9_2_hook_subsystem.py` 等。它们支持Linux行为回归，但不能代替T2/L2真实跨OS pipe测试；原provider dogfood在后续授权激活阶段沿原配置路径执行。

## 9. 部署未知项与冻结边界

还需G0锁定：WSL版本/安装来源和离线要求、rootfs发行版与Linux ABI、实际wsl.exe参数pipe行为、interop在并存发行版下的隔离、localhost有效性、sleep与外部terminate故障结果、LinuxChrome依赖与字体、镜像/应用更新停机边界。它们是Windows激活前gate，不妨碍先冻结执行域与owner设计。

部署首装通过明确 `--import ... --version 2` 创建专用发行版；不改变默认发行版。WSL官方明确imported发行版的defaultuser应通过wsl.conf设置，unregister会永久删除全部发行版数据；因此常规卸载不能顺手unregister用户数据，更不能在启动不兼容/升级失败时unregister后重装。[WSL commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)。镜像/发行版名冲突要明确报错，不能借一个字符串名称推断安装owner authenticity；采用现有安装资源签名与确切受控路径/配置校验，不发明per-file SHA注册表。

本路线省去native ConPTY/Job、PowerShell、WindowsPython依赖重建、WindowsPG/pg_search与Windows no-follow实现；不能沿用原88–154人日native计划简单扣一项后称为WSL预算。需要重估的核心是部署/更新、桌面路径、HostExec与跨域生命周期实证；终端本体以原Linux回归+真实WSL故障矩阵计，不再收取全面跨平台抽取费用。
