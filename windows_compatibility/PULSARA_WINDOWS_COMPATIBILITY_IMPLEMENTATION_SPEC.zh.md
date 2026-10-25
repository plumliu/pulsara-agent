# Pulsara Windows 实现合同：Windows Electron + WSL2

当前版本：`0.1.1-alpha.1`（Python `0.1.1a1`）。B1–B4 核心实现已接入；本机所选矩阵已完成机制及安装版验证，B5／Windows 发布资格仍未激活。原生 Ubuntu x64/arm64 与 GUI 验收继续延期，不由 WSL 结果替代。本文描述当前生产边界；实验过程保留在 `codex/windows-g0` 分支。

## 1. 路线决策与支持范围

用户明确选择 **Windows原生Electron界面 + WSL2中的完整Linux执行环境**。Python kernel、terminal、MCP、hooks、Skills/plugins、私有PostgreSQL及扩展、rg、截图worker/Playwright Chromium均在同一受支持Linux发行版/非root用户中运行；Windows拥有窗口、tray、系统对话框、资源管理器、系统浏览器和安装入口。模型执行Linux命令，日常启动不经过PowerShell求值。

本次完整取代此前原生Win32内核路线与其宽Phase A：**ConPTY/Job、PowerShell载荷、Win32 HANDLE/NTFS全消费者binding、Windows PG DLL、内核named-pipe/Proactor迁移全部退出当前计划。** 不保留两条Windows内核路径，不把旧G0-D/T/F/R或88–154人日总数当当前验收/预算，也不以WSL失败自动回退原生Windows、WSL1、Git Bash、远程PG或Docker Desktop。

首批资格矩阵为Windows 11 25H2 x64普通用户、受支持且固定验证版本的WSL2、用户选定的已有Ubuntu x64发行版；Ubuntu不预设发行版本号门槛，记录实际OS build/WSL包及Linux kernel/发行版来源与版本，按实测结果声明支持组合。26H2、ARM64、Server、WSL1、其他Linux发行版、DrvFs/网络盘工程需另行目标验收后才扩声明。选择25H2作为首批测试矩阵不代表它是唯一当前Windows版本。[Windows官方版本信息](https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information)

**首版接入一个用户明确选择的已有WSL2 Ubuntu，首次默认选中用户默认发行版并读取其默认非root用户。** 用户确认的具体发行版/用户名由现有Windows宿主设置owner保存；后续启动显式指定`-d/-u`，不因系统默认值变化静默切换数据。名称可为`Ubuntu`、用户可为`plumwsl`，home由Linux实际解析，不按用户名拼接。目标缺失、用户不存在或为root时显示具体错误，不自动创建/接管其他环境。不修改发行版默认用户、全局`.wslconfig`或系统默认发行版/WSL版本，不把共享发行版视作Pulsara独占资源或安全沙箱。

实施次序为：**收窄后的Phase A先完成现有macOS/Linux桌面宿主接缝及回归，可独立交付；G0-W可与A研究并行；A验收和G0-W全部通过后进入Phase B的WSL部署/跨域闭环；最终原生Windows安装验收才激活。** 不必先重构全部POSIX业务代码。

## 2. 共享代码不变量

### 2.1 全程保留

1. AGENTS及当前有效产品合同继续适用。现有epoch的SYSTEM/provider tools字节不变，messages仅追加；Linux/WSL说明只在cold epoch或采用的compaction successor安装，不新增rebase例外。
2. terminal每次独立命令，显式workdir；运行中process跨轮跟进。保留list/poll/wait/write/submit/close_stdin/kill和monitor，不引入常驻shell、session/command新身份或模型log别名。
3. 权限、foreground decision、副作用已写/未写/unknown、Host/child授权、输出游标/GAP/artifact与物理join保持；丢回复不自动重发命令/输入。root退出不等于范围空，更不等于输出drain/worker join。
4. 保留8个尚未物理结算terminal槽、单process 16MiB/Host 128MiB等既有物理边界及理由。不新增总任务/历史/轮次/调用/重试或运行寿命cap；单操作协议/资源边界必须沿现有有效规格。
5. 不新增durable event、live kind、subject、append guard、relation、job、receipt、lease、恢复日志或fingerprint注册表。部署状态从OS注册、安装资源和现有settings读取，不造持久编排引擎。
6. 保存配置由Linux有效home的`LocalSettingsStore`/`require_pulsara_home()`拥有；dogfood只读注入、精确隐藏实际credential。没有模型/PG设置就报告缺项，不新增环境key fallback；不自动复制Windows/macOS凭据。
7. 当前关系/事件oracle、schema/clean-v0、canonical workspace/Skill/plugin ID不因部署路线增加字段/空migration；workspace仍以Linux canonical路径走既有owner。硬切每个改变的接口及全部受影响caller，删除被替代路径，不留双读写/兼容别名。
8. 用户级工作区浏览、部署状态、设置/Skills/MCP/hooks页面独立于active session。明确对某会话操作仍使用该目标，不借active session作全局授权。

## 3. 架构、唯一所有权与生产代码位置

```text
Windows Electron（main唯一boot/reset/quit）
  ├─ 原窗口/tray/preload/浏览器隔离、资源管理器、安装入口
  ├─ PythonRuntime + BundledPostgres（唯一生命周期/状态owner）
  │    └─ WslHostOperations → wsl.exe -d <selected-distro> -u <selected-user> --exec …
  │         ├─ 长连接：Linux desktop_runtime，原JSONL start/ready/shutdown
  │         └─ 短调用：有限Linux PG/文件事实操作，一请求一退出
  └─ 专属workbench Session → 被精确认证的WSL HTTPS origin
       选定WSL2 Ubuntu / 默认非root用户 / Linux ext4应用数据
         ├─ 原DesktopApplication、HTTP路由、kernel/model/权限/存储
         ├─ 原Linux terminal/MCP/hooks/Skills/plugins/截图worker
         ├─ 原Protocol-v3 Unix socket、diagnostics、kernel flock
         └─ 私有PostgreSQL17 + vector + pg_search，Unix socket
```

### 3.1 责任归属

| 对象 | 唯一owner | 平台/依赖与窄适配 |
| --- | --- | --- |
| boot/reset/requestQuit | `main.ts` | Windows只选择宿主实现，不复制WslMain/WslApplication |
| kernel进程/控制帧 | `PythonRuntime` + 原`desktop_runtime.py`各自原边界 | WSL relay拥有Windows↔Linux字节搬运；Pulsara校验帧、保留EOF/取消/关闭结算 |
| PG initialized/startedHere、启停reset顺序 | `BundledPostgres` | HostPgOperations在本机POSIX或固定WSL中执行有限操作，不拥有另一份状态或自动重试 |
| role/database与schema | Python `prepare_database`及原migration owners | PostgreSQL拥有WAL/恢复/锁，现有Unix DSN/角色隔离继续使用 |
| terminal/MCP/hooks/preview文件 | 现有Linux产品owner | 标准库/SDK/OS机制继续复用；不复制PTY/filesystem/protocol框架 |
| 工作区图形服务 | 原workspace identity/availability owner + 小用户级目录服务 | Linux检查canonical/ext4/权限；前端只分页浏览/提交，不发shell命令 |
| Linux↔Windows显示路径 | 单一host路径适配 + Linux目标验证 | 固定发行版映射，路径字符串不作授权，也不重建workspace身份 |
| 目标接入/资源安装 | Windows setup owner、现有资源构建owner | 读取WSL注册与默认用户，安装用户私有Linux应用资源；OS依赖由明确维护动作安装，不接管发行版生命周期 |

### 3.2 生产文件落点

| 位置 | 当前责任 |
| --- | --- |
| `desktop/src/main.ts`、`runtime.ts`、`postgres.ts` | 唯一启动、退出、重置及状态裁决 |
| `desktop/src/host-wsl.ts`、`wsl-setup.ts`、`postgres-wsl.ts` | WSL 目标、资源安装、载体及有限 PG 操作适配 |
| `desktop/src/backend-session.ts`、`browser-owner.ts`、`preload.ts` | 每次 launch 的 TLS Session、浏览器隔离与受限宿主动作 |
| `desktop/native/`、`desktop/scripts/` | Windows helper、NSIS 维护准入、Linux payload 与资源构建 |
| `src/pulsara_agent/windows_interop/` | 每次执行的 anchor、relay、Windows 后代范围结算 |
| `desktop_guest_ops.py`、`desktop_runtime.py` | 固定 guest 操作、锁、控制链与 EOF 收尾 |
| `desktop_app/`、`frontend/` | 原业务 owner、用户级目录服务、跨域文件及真实工作台界面 |

### 3.3 PG接缝必须覆盖文件，不只覆盖spawn

保留TS `BundledPostgres`，抽出其当前用到的`prepare_layout`、`inspect_version`、`verify_instance`、有限`exec_pg(initdb/status/start/stop)`、`remove_verified_pgdata`事实操作；返回完整typed结果/实际退出码/输出，不用hash或通用receipt。POSIX实现继续现有Node fs/exec，WSL实现调用固定包内Linux helper。helper限制在有效Linux home专用PG根与固定随包PG binary，不接受renderer任意程序/任意路径执行。

helper在已选发行版同一已解析非root用户内检查uid/mode/realpath/PG_VERSION/postmaster.pid/data-dir/socket并执行现有必要文件操作。Node不能经UNC stat证明Linux0700/uid，也不能按可复用PID猜实例。helper一操作一退出，无daemon、队列、后台重试、自主start/stop/reset或第二`startedHere`；不读/写另一份settings。

**失联时不能只probe一次status=3：旧start可能仍inflight，稍后才起库。** WSL实现采用两种独立的OS生命周期锁：原`kernel.lock`继续串行kernel与维护；PGDATA外的`<home>/desktop/pg-lifetime.lock`以EX flock保护启动链/常驻PG及数据维护。新增锁的独立必要性是：helper独占原锁会在自身死亡时失去旧start保护，直接把原锁传给常驻PG又会阻断正常kernel。它只是空文件上的内核锁，不是持久状态、操作receipt、第二份startedHere或实例身份。两锁按kernel→PG顺序非阻塞取得；busy不排队未来操作。已运行kernel期间的诊断仍走原Unix socket。

新start先取得两锁，再ready/接纳本次操作；真实pg_ctl→shell→postgres启动链只继承PG锁，原kernel锁保持CLOEXEC。常驻PG持独立锁时，正常kernel仍可取得原锁。PG锁busy且status3必须保留inflight/unknown，拒新start及删除；已运行实例的复用/停止仍须当前准确data/socket/readiness事实，不以busy证明实例正确。stop/status等有限子命令继承原kernel锁，initdb及其有限后代继承两锁，防止helper死亡后旧有限命令晚执行。helper不因结果pipe坏掉放弃仍执行的child。

reset先取得原kernel锁并核对准确实例，必要时执行有限stop；随后必须实际取得PG生命周期EX、重新核实当前已停，并持两锁至删除完成。普通reset仅删已验证PGDATA，不能unlink/替换PG锁文件。锁文件沿真实私有home/desktop，O_NOFOLLOW打开并检查regular/uid/0600/nlink1；继承的是同一open file description，各owner只close自己的fd，不能用LOCK_UN连带解锁后代。`--wait-owner`仅是原kernel串行屏障，后续helper仍须重新取实际需要的锁，不把旧等待/stop结果当删除授权。此协议只约束遵守同一准入路径的操作，不以锁约束任意外部手动PG命令。

为排除“旧helper尚未取得锁、父已死、稍后进入锁空窗才执行”这一实际边界，有限调用沿现kernel start帧模式：helper先取锁并发本次ready，Node收到后才发送一个有限operation帧；helper取锁前不接纳/缓存操作。Node在发帧的真实不可逆点再次检查原取消/close准入，取消不能留下晚到帧；operation帧写出前的任何取消（含已ready但尚未发帧）必须结束本次stdin并等helper实际退出，不只reject一个Promise。ready前父EOF则无操作退出；operation已发时物理锁已持有，失联仍待真实子命令结算/readback才释放，不能把发帧后的取消改报未发生。整个过程仅当前私有pipe/锁生命周期，无durable receipt、generation或新的操作registry。G0-W-C/P必须验证父在各边界退出与helper延迟调度；若所支持故障使helper先死后仍可晚执行的PG副作用失去所需的最后准入锁，允许后续新start或删除进入，则该机制未通过，不能用瞬时status放行reset。

完整有效回复与carrier正常结束才可采用操作结果；wsl启动失败、截断、EOF、丢回复与pg_ctl真实非零不同。可能已经生效但未收全结果时返回本次结果不明，保留PG/data，不自动重发start/reset/delete或继续初始化。重新进入由TS先等原kernel锁，再通过当前Linux事实验证；不做跨重启操作重放。

有限helper请求为：持锁ready之后，恰一条完整LF结尾operation JSONL及stdin EOF才接纳执行；拒绝提前、重复、尾随残片。该EOF是一次有限调用的输入结束，不用于长连接desktop_runtime。回复须完整fatal UTF-8 decode、唯一ready/result、次序/方法/必要值类型正确，且carrier正常结束才采用；已发操作后的坏帧/截断仍为unknown。仅复用既有有限exec每流4MiB资源边界，不增加任务寿命限制。部分删除后失联如实保留unknown及部分目录，不自动恢复、重删或初始化；unknown不承诺撤销已生效副作用。

物理错误须向原TS owner区分“本次未提交”“可能执行但结果不明”“子命令已结算非零”。`PostgresCommandError.postgresExecution` 的 `not-submitted` / `unknown` / `failed` 由真实执行边界提供：未提交或 unknown 原样抛错，不根据随后出现的同路径实例认领 startedHere；明确已结算的 PG 非零保留原 readback 和原错误。已有 startedHere 不因新错误被清空，status3 只有在已结算失败时才可参与准入；WSL helper 必须维持相同分类及双锁合同。

## 4. G0-W：WSL路线可行性闸门

旧原生G0退出；新G0-W在真实Windows 11首批矩阵、普通用户、实际选定WSL发行版执行。Mac单测、native Ubuntu、mock win32、静态扫描不能代替。版本/G0输出放个人`output/`，不成为生产authority。

| 闸门 | 必须证明 | 失败时 |
| --- | --- | --- |
| G0-W-D 接入/环境 | 默认目标发现→明确选择→重开显式使用同一发行版/非root用户；实际home/WSL2/ext4和私有资源有效；interop/Windows PATH/自动挂载保持开启，原用户工作不受损；资源安装失败保数据；无管理员日常启动 | 不改系统默认值、不关闭用户互操作、不创建替代发行版兜底；报告实际缺失条件 |
| G0-W-C 控制/生命周期 | Node直接argv+3条pipe→Linux原JSONL；中文/分段UTF-8/大量stderr/EOF；正常退出码、root/后代范围、启动中取消、Windows进程崩溃、carrier被终止、隔离故障环境的WSL终止/休眠恢复；实际Windows PE及其后代退出/取消/持pipe/脱离行为有证据；原锁/端点能区分未结束/结果不明 | 不以wsl.exe close、Linux PGID消失或taskkill证明Windows后代joined；不通过终止共享发行版回收，不加总寿命cap |
| G0-W-P PG/资源 | 固定PG17.9/vector0.8.1/pg_search0.26.1及当前Python3.12.15、rg、Playwright在guest实际运行；clean-v0、BM25中英文/代码/snippet/vector、Unix socket、helper结果分类、退出重开/搬移与精确reset | 不换数据库/删搜索/开TCP trust，不通过Windowsfs删除data |
| G0-W-F GUI/路径/文件 | 无session时图形浏览/新建/选择Linux目录；UNC Explorer操作；同目标路径转换；拒C盘/他发行版/DrvFs/bind-mount替代；中文空格、大小写、Unicode、权限/替换/取消/丢回复；文件导入和preview/另存为 | 不要求用户mkdir补产品功能，不盲replace盘符或把C盘当Linuxworkspace |
| G0-W-N 网络/身份 | Windows Electron经localhost访问本次Linux服务；精确TLS身份覆盖新连接/SSE/preview/下载；端口占用/rebind/错误服务/旧证书不能误连或泄露内容；原Host/Origin/Fetch-Site、browser隔离、代理/VPN/firewall/OAuth实测 | 不用单次HTTP challenge冒充持续身份，不放宽0.0.0.0、忽略证书或默认另造proxy |

G0-W记录实际可用WSL最低/测试版本、已有发行版来源与版本、SDK API和应用payload recipe；不以“最新版”漂移运行依赖。标准库/SDK扩展点能满足就复用；确有缺口记录具体要求和最小适配，改变产品/安全/执行合同先修规格再审。官方制品摘要/签名用于真实内容验证可以保留，不生成代码/文档/测试证据SHA。

## 5. Linux执行域与终端链路

### 5.1 保留当前Linux实现

manager/environment、原MCP/hooks/screenshot进程机制继续在Linux执行，不创建`_physical_windows.py`、`_shell_windows.py`、pywin32依赖或Windows能力占位。原始byte输出、PTY canonical EOT、PIPE关闭、root/group/drain/worker组合join、取消/输入结果/容量隔离按现行有效合同回归；不把PGID提升为“整个发行版所有进程/任意逃逸后代”保证。

probe继续原default-deny、single-flight/TTL、shell候选、`.venv/bin`与实际用户PATH。MCP/hook保留各自credential guard真实spawn、frame/环境/超时/流任务与完成policy；不共享terminal8槽。应用Linux依赖使用Linux版本，Windows工具调用属于interop，Windowsvenv不被当作Linuxvenv。model看到Linux主执行域及实际可用的Windows互操作能力，desktop-host.platform仍win32；这些说明只在已有合法prefix安装边界进入模型输入。

### 5.2 共用发行版与Windows互操作

保留并允许`/etc/wsl.conf`的`[interop] enabled=true; appendWindowsPath=true`和`[automount] enabled=true; mountFsTab=true`，未显式设置时沿WSL默认行为；不要求用户写入这些条目，也不自动覆盖其配置。用户显式关闭某项时显示实际能力与手动开启指引，不静默修改或声称已开启。允许用户继续从WSL启动Windows程序、使用Windows PATH和`/mnt/c`；不修改全局网络/内存配置或默认用户。[微软配置定义](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)

kernel、私有PG、Protocol-v3和应用资源仍使用Linux机制与ext4。自动挂载可用不扩大workspace/PG的存储接纳范围，首版C盘工程仍按第6/7节显式导入。宿主UI、OAuth、下载/reveal仍经原可信Electron owner，不改为拼接shell调用Windows程序。普通任务可使用互操作能力，受现有权限和副作用规则约束；不以PATH隐藏或通用命令黑名单冒充禁止Windows执行。

原Linux PGID/root/drain/join只证明其Linux范围，不证明interop启动的Windows后代已退出。必须实测Windows命令退出码、子进程持pipe、父先退/后代继续、取消、载体失联和正常Quit；不能确认的结果按unknown保留，不自动重发，也不把它升级为完整结算。正式接入须在生产owner重新验证Windows模型任务的取消/关闭范围，不能仅沿用G0探针结果；不能用关闭interop、杀整套WSL或削弱原物理结算断言放行。用户明确系统打开动作继续其已有OS交接语义，不承诺关闭外部编辑器/浏览器。

MCP/hooks/environment的跨域scope必须接在原spawn/关闭边界，不能套用terminal槽或另建调度器。MCP stdio服务进程属于连接/会话生命周期；一次tool cancel可释放原call permit但不能擅自结束仍READY的服务会话，session close才结算其Linux/Windows范围。原timeout的remote UNKNOWN与现transport failure/fence处理保持，不重放工具。hooks成功/失败/取消、environment probe缓存写入均须等待其同一物理scope结算；credential guard拒绝应发生在scope prepare/spawn之前。

Windows 普通后代使用无 breakaway Job、每次执行的独立 anchor 及私有 interop socket。anchor 自己打开上游 O_PATH fd，以标准 SCM_RIGHTS 传交，并以 SO_PEERCRED/pidfd 和当前可信 anchor 身份核对，避免重开可复用 PID 路径。裸 WSL_INTEROP 失效可能回退，私有 socket 失联必须关闭本次准入，不能退回无范围约束的执行。

每次执行的跨域 scope 纳入原启动预约、线程启动 rollback、取消及物理 join；原 watcher 继续唯一发布完成和 finalize。Linux group、reader、Windows Job 与精确 HANDLE 都结算后才可释放原槽位；仅透传正确路由或等待 Linux root 退出均不足以证明整个范围结束。

物理存活谓词和终止 intent 接纳也必须包含同一跨域 scope，不能在 Linux root/group 已退而 Windows 后代仍存活时冻结 success 或拒绝取消。relay seal 停止新准入，真实结算已接纳的 connect/forward 任务后再发 ACK；不能将 ACK、载体退出或 Promise reject 当作范围物理回收。

Windows launcher仍用系统实际`wsl.exe`绝对位置、`shell:false`、明确已选-d/-u/--cd/--exec和三pipe，不经PowerShell求值。应用入口使用明确Linux可执行文件，不因PATH注入调用同名WindowsPython/Node；模型使用的是已选用户的实际环境，允许Windows PATH项存在。清理本次宿主启动环境的WSLENV自动传递清单，不抹除Linux会话的interop能力；credential仍沿原guard和Linux settings读取，不进launcher argv/宿主日志。Linux PTY仍由guest原manager创建。

## 6. 图形工作区：无需命令行的产品流程

### 6.1 唯一主流程与数据范围

首版选择 **Linux-backed工作区picker** 作为唯一接纳流程；picker底部的“浏览…”可调用Windows原生目录窗口作为候选路径入口，不形成第二个接纳owner。入口“新建工作区”“打开工作区”在Windows窗口内展示同一Linux目录服务，直接从所选Linux用户的实际home（`~`）开始，允许选择该home及其中符合原权限/canonical/native-ext4检查的目录，不再要求工程位于`~/workspaces`。GET不创建目录，删除专用workspaces根初始化接口；home缺失或不可访问按实际错误展示，不由应用重建Linux用户home。quick继续由原owner在有效PULSARA_HOME下的`workspaces/quick-*`创建并保留，不强制改成project，也不改变既有清理语义。

原生窗口由Electron `dialog.showOpenDialog(openDirectory)`拥有，默认位置动态映射所选发行版与真实home的UNC地址。可信host仅将同发行版UNC转换为Linux候选路径；Windows磁盘/其他发行版/歧义路径保留原显示值但不交给Linux读取。同发行版候选继续由原用户级目录服务执行home范围、canonical、权限、native-ext4验证：有效时在picker定位该目录；无效时用遮罩覆盖路径工具栏及目录列表，底部显示候选路径与简短原因，禁用“选择文件夹”。标题栏刷新可返回上次有效位置，再次浏览并选择有效目录也可恢复；原生取消保留原选择，弹窗关闭/owner取消后的迟到结果不得接纳。最终确认及create-session仍重新验证实际Linux对象，不因原生窗口选择而信任路径或增加持久状态。

picker及服务是用户级，未创建/选择session也能使用。现有session `path-candidates`继续服务会话中的@补全，不借某个active session运行全局浏览/建目录。existing `workspace_identity.py`仍唯一canonical/availability/稳定key owner；WSL profile检查作为目录接纳边界，不给数据库新增Windows路径副本或发行版字段。当前NEW resolver会resolve链接，不能假称现已实现严格接纳；list/选择后在原create-session入场前再次执行同一WSL验证，不能仅前端检查，也不改其他平台resolver语义。首版固定一个执行域，未来多发行版必须另修身份合同。

| 图形操作 | 交互与后端行为 | 失败/取消结果 |
| --- | --- | --- |
| 打开工作区 | 分页列出目录、面包屑、返回上级；选择已有目录→Linux验证实际canonical/ext4/可读写执行→交给原create-session入口 | 不存在/不可访问/换目标显示具体状态，保持picker；取消不创建会话/改绑定 |
| 新建工作区 | 浏览父目录→“新建文件夹”→输入单个名称→Linux绑定父目录、exclusive mkdir/重新绑定并核对身份→返回实际Linux路径→用户选中后建会话 | 同名不覆盖/不自动加后缀；不能原子消除的namespace竞态按真实检查结算；会话创建失败不偷偷删用户新目录 |
| 新建子目录 | picker内同一按钮完成，不要求用户打开terminal；一次创建只接受单叶名，不接受shell/递归脚本 | 名称/权限/磁盘满等直报；取消未提交无写入；写请求结果不明时刷新当前目录核对，不盲重复mkdir |
| 最近工程/重开 | 原存储返回Linuxcanonical路径，重新观察MISSING/UNAVAILABLE；不按同名新目录重绑 | 不因WSL失联自动创建/移动workspace；恢复沿既有明确restore合同 |
| 在资源管理器中显示 | Linux先核对当前目标→host映射同一固定发行版UNC→Windows Electron reveal/open | 原发行版不可用/目标已变/名称不可表示时具体失败；不打开另一个同名目录 |

新增最小用户级HTTP操作：目录分页、目录接纳、在绑定父目录创建单个子目录；如采用`/api/workspaces/directories`，方法/DTO同一切片连前后端一起定，不新增通用文件管理RPC。现有权限/安全middleware覆盖这些写操作。复用已有100项分页边界的理由与cursor机制，不加目录总量/深度/任务寿命cap；目录读取只传所需名称/类型/路径/availability，不读文件内容。

Linux检查实际挂载/filesystem与canonical包含关系，防止不是`/mnt/c`字面路径却通过symlink/bind mount落到DrvFs；不靠字符串前缀判断存储语义。目录服务以所选Linux用户home为浏览和接纳边界，阻止`..`、NUL、分隔符、越界根/特殊项；沿原POSIX no-follow/身份检查，不为此全面重写文件binding。没有新增针对应用自身目录的隐藏或禁选规则；目录接纳继续依赖原对象身份、所有权、权限与文件系统检查。UI新建名称选择能被Windows资源管理器表示的明确规则，冲突/不合法不静默改名。Linux工具生成的其他合法Linux文件名不整体禁用；对应文件仍可经Pulsara preview使用，host reveal有具体不支持结果。

### 6.2 资源管理器仍可直接管理

用户可在Windows资源管理器打开已选发行版及实际home对应的UNC，例如`\\wsl.localhost\Ubuntu\home\plumwsl\workspaces`，新建/复制/重命名目录；默认Linux用户有相应权限即可。Pulsara刷新picker后可选择同一目录，不把“Linux文件系统”解释为只能mkdir。Windows通过WSL共享访问Linux文件采用该发行版默认用户权限；接入时记录默认用户，后续变化时重新观察权限并报告，不自动修改默认用户或换执行用户。[WSL文件访问与权限](https://learn.microsoft.com/en-us/windows/wsl/file-permissions)

工程代码/依赖与应用数据保持Linux磁盘内。C盘工程首版不作为直接workspace：界面明确引导用户经资源管理器复制到上面目录后再选择；不隐式搬家、同步或重写Windowsvenv。用户在Explorer改名/删目录可能让既有会话MISSING，沿当前availability处理，不造目录重定位/同步恢复系统。

## 7. 路径转换、附件、预览与跨域传输

### 7.1 路径类型与转换

| 类型 | 示例 | 使用规则 |
| --- | --- | --- |
| Linux canonical | `/home/plumwsl/workspaces/demo`（示例；使用实际home） | kernel/model/terminal、session/workspace identity、数据库、Skill/plugin来源使用；POSIX区分大小写 |
| 同发行版Windows访问路径 | `\\wsl.localhost\Ubuntu\home\plumwsl\workspaces\demo`（示例） | 仅host reveal/open或真实选择输入；另一合法别名`\\wsl$\Ubuntu\...`经同一解析器归一，不写入canonical字段 |
| Windows本机路径 | `C:\Users\alice\Documents\a.txt` | 只属Windows host选择/导入源或另存为目的地，不直接传成Linux路径/模型reference |
| 资源URL | `https://127.0.0.1:<port>/api/file-previews/<token>/...` | 由现preview生成、由reading-root/token与TLS origin限定，不能反解成任意读文件授权 |

唯一`backend-paths`解析准确的共享主机/固定发行版、组件、Unicode与编码；拒其他发行版、远端UNC、设备路径、盘符相对路径、控制字符/歧义dot segments。Linux最终重验路径/挂载/权限；映射不是存在性或授权证明。反向转换只将已验证Linux目标映射到固定UNC，不用Windows`path.resolve`改写Linux路径、不统一casefold、不盲replace `C:`。可复用官方`wslpath`的真实映射能力，但首版产品仍只接固定发行版/ext4，不能因能转成`/mnt/c`就接纳为workspace。[微软路径互操作说明](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)

### 7.2 文件选择、拖放与导出

1. 从同一已选发行版UNC选择/拖入的文件或目录：可信host取得原路径→固定域转换→Linux原文件/preview owner验证→插入Linux reference。renderer粘贴路径同样必须重验，不当作OS来源证明。
2. Windows本机普通文件：用明确“从Windows导入文件”的renderer标准`<input type=file multiple>`取得浏览器File，显示“导入副本”，沿现File原始字节流→`LocalImport` staging/no-clobber→Linux位置→草稿确认后引用。保留现连接generation/controller、请求/资源边界和取消/清理，图片仍走现有accepted media规则。当前`chooseFiles():Promise<string[]>`跨IPC不能返回File，不允许把path字符串标成upload就声称已有字节，也不新增任意Windows路径读IPC/选择tokenregistry。原生UNC选择器仅为Linux文件/目录引用入口，工作区主picker仍为6.1唯一Linux-backed流程；已有drop/clipboard File按来源执行上述转换或副本导入。
3. Windows目录或其他发行版目录：首版不做隐式递归导入/同步；给出复制到Pulsara工程根的图形指引。不能伪装普通file上传或把C盘目录挂载为Linuxworkspace。
4. Linux内的模型artifact_export保持现目标/权限/no-clobber/published-unknown语义。用户“下载/另存到Windows”是独立明确host动作，复用token-bound download和Electron标准下载/保存机制，目标由用户图形选择，结果显示Windows路径；不把它伪装为Linux模型export已原子提交。
5. Windows另存为的取消/现有文件覆盖确认、断网/磁盘满/部分写入/结果不明均由原下载/host owner如实报告；不自动重复下载/覆盖或删除未核实目标。不增加跨域双写、同步目录或永久文件副本索引。G0-W-F必须验真实最终文件内容与错误，不只assert按钮可点。

### 7.3 preview与系统打开

Windows Chromium不能把`file:///home/<user>/...`当作Linux文件。继续使用`FilePreviewHttp`的read_token、reading_root、冻结资源、受限HTML/CSP与sandbox，在工作台专用view中展示text/image/PDF/HTML；截图仍由Linuxworker完成。`frontend/lib/file-preview.ts`的本地链接保持Linux语义，不能把Linux file URL交Windows loadURL。

普通BrowserOwner继续禁止控制服务整port及file://，不为WSL放宽白名单；Preview与普通网页隔离保持。可信host执行open/reveal前经原preview action/明确workspace服务确认当前目标和允许文件类型，再映射UNC。外部OS打开依旧是用户明确动作，不承诺外部应用继续受preview冻结控制。web页面、HTML、附件不能调用preload、任意文件RPC或launcher。

## 8. 部署、资源、设置与维护

### 8.1 图形部署流程与权限

首次界面显示检查/接入进度及具体失败，顺序：检测WSL2/虚拟化→列出已有Ubuntu并默认选中系统默认目标→读取该发行版默认用户、uid和真实home→用户明确确认→检查ext4、应用依赖与资源→在该用户私有应用目录安装固定Linux payload→启动应用。目标选择由现有Windows宿主设置owner保存具体发行版/用户名，不存第二份provider设置。WSL缺失、没有合适发行版或需重启时，提供官方安装指导并等待用户完成；不自动import专用rootfs、不接管现有发行版。[微软WSL安装](https://learn.microsoft.com/en-us/windows/wsl/install)、[目标发行版/用户参数](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

日常调用固定本次已选非root用户。只在明确系统依赖维护时由用户授权sudo/root安装缺失包，不更改sudoers、系统Python/Node/PG或用户默认shell；用户本已有的sudo权限保持原样，不作为自动提权授权。应用资源仍私有，PG仍由TS owner启动，不接管已有系统PG。保留发行版现有systemd/interop/automount配置，不增boot service托管kernel/PG；不暴露root launcher给renderer。缺依赖或部分安装失败时保数据、显示具体错误，不unregister、重建或改接其他目标。无需Docker Desktop/WSLg。

### 8.2 两个home与版本布局

| 位置 | 拥有的数据 | 约束 |
| --- | --- | --- |
| Windows标准每用户应用数据根 | Electron UI prefs、已选发行版/用户名、Windows安装/宿主日志 | 原Electron owner管理，不存provider配置/第二数据库或canonical workspace，不管理用户WSL磁盘 |
| Linux实际home下的 `.pulsara` | 唯一LocalSettingsStore、PG data/socket、历史/记忆、原应用持久数据 | 原Linux resolver/权限；不放DrvFs，不从Windows raw settings复制secret |
| 用户选择的Linux home及其子目录（如`~/Projects`、`~/workspaces`） | 用户工程 | 由原目录接纳检查，应用更新不覆盖 |
| Linux用户私有版本资源根（如`~/.local/share/pulsara/runtime/<app-version>`） | 私有Python/PG/extensions/rg/Chromium与应用wheel | 已选用户安装/验证，不覆盖系统或其他应用资源；准确绝对路径由recipe/locator共同定义，不运行时下载latest |

Windows host home与Linux有效`PULSARA_HOME`是两个不同typed值；Windows用户环境里的`C:\...`不能直接当Linux home。现有Linux resolver仍唯一，实际home不写死为`/home/<username>`；显式Linux home override须验证ext4/私有权限/Unix socket长度并全链一致，不默默取Windows环境同名变量。首版一次只接一个已确认目标，不提供运行中跨发行版/用户切换；修改选择前须原owner正常结算，并明确新目标有独立数据，不自动搬迁或合并历史。用户默认发行版/用户改变不静默改变已保存目标；用户WSL磁盘始终由用户/WSL管理。

model/service连接在设置页写Linux现有owner；Windows壳不读取并缓存一套credentials。PG仍private Unix socket、原local auth/host reject；不需要原生Windows方案的postgres-auth.json/TCP SCRAM。首次缺模型连接展示现有设置页，不能因为系统已安装就宣称provider可用。Linux工具链与Windows互操作能力分别如实展示；Windows PATH可保留，应用私有入口仍用Linux绝对路径。

### 8.3 构建、更新和卸载

首版安装包携带固定、已验证的Linux应用payload，接入用户已有WSL2 Ubuntu，不携带/定制/再分发rootfs，也不拥有发行版升级。记录实际发行版来源、版本与系统包，以及payload制品版本/摘要/许可；摘要仅用于真实制品内容边界。已有合格WSL环境上的payload支持离线安装；WSL engine、发行版和缺失OS依赖是单独系统前提，缺项须如实显示。Ubuntu发行版本号不预先限定，实际ABI/依赖兼容性由构建和G0验证。

按用户后续授权，`desktop/scripts/prepare_resources.py`的`target_platform()`已删除Ubuntu发行版本号检查，原生Linux与WSL共用此路径；Ubuntu发行版身份、目标架构、依赖pins与资源验证继续保留。pg_search仍使用已固定的noble制品，CI/容器仍保留各自构建基线；版本准入取消不等于这些资源在其他Ubuntu版本上已通过实测，也不等于G0通过。

Linux x64 guest包使用现有Linux private resources和当前pins；Windows仅Electron/必要host资源，不构建win_amd64 Python wheel、Windowspg_search DLL或捆pwsh。应用部署仍用明确stdin制品流/固定Linux解包入口传输到用户私有ext4资源根；即使automount开启，也不把`/mnt/c`当Linux资源运行位置。权限、symlink和架构由Linuxrecipe验证。拆recipe须在当片更新mise sources/outputs/cache fixture；内容校验沿既有真实制品边界，不增hash registry。

沿现build owner分开目标：native Ubuntu x64构建/验证guest wheel和Linux payload，Windows原生runner构建Electron/NSIS并装入同版本已验证制品；不能在Windows误跑Mac/Linux native recipe，不能把Windows壳平台写成guest wheel平台。跨job传递保留既有不可变制品验证/版本完整性，mise分别声明payload/host输入与输出，新模块变更立即失效。开发构建脚本如需Python仍用该平台仓库uv环境（Windows为`.venv/Scripts/python.exe`），这是构建依赖而非Windows内核。release集合在原`release_artifacts.py`加入精确`Pulsara-${version}-windows-x64.exe`，同步package/mise/workflow/文档和许可，保留DMG/两个deb及手动触发/发布权限。

按用户明确授权，本轮Windows迁移与验收可自主递增必要的本地alpha候选版本号，并构建、安装和验证，不再为每次版本递增单独询问。版本变更须同步Python/npm及对应锁文件中的本包版本，通过既有release owner的一致性检查，并按正常维护路径保留用户配置、数据库与工程；不得以同版本覆盖或修改payload摘要标记替代。验证构建继续使用`--publish never`；Git tag、上传制品和对外发布仍须另行明确授权，本地候选通过不等于Windows支持激活。

更新为手动发布/安装：先正常Quit、确认原kernel/PG及已支持interop任务按合同结算且无未解决状态，再安装新版本Linux应用资源并验证，最后使用新版本。数据/工程保持；不覆盖运行中的Python/PG/Chromium文件。失败保留原数据与错误，不自动清库、升级/重建发行版或引入双版本运行fallback。旧应用资源只在确无live owner使用且新安装成功后清理；不增加持久更新journal/恢复job。PG major升级须另定数据维护合同；发行版和系统包升级由用户管理，Pulsara检测兼容性变化并报告，不替用户执行dist-upgrade。

普通卸载移除Windows应用入口，**保留用户发行版、Linux settings/DB/workspaces与备份**，界面说明应用资源/数据位置和重新安装复用方式。清理Linux应用资源或应用数据须单独明确范围并确认原owner已正常结算；不得删除整套发行版、整个home或用户其他软件/工程。Pulsara不提供注销共享发行版的卸载动作，不执行`wsl --unregister`。用户自行管理WSL备份，不引入自动checkpoint或恢复authority。

NSIS沿electron-builder维护机制使用per-user安装及tracked customCheckAppRunning扩展，替换默认强杀行为。运行/收尾/unknown时等待用户正常Quit、取消或明确失败，不taskkill main/wsl/PG后继续覆盖/卸载。Windows全局控制台CLI不在首版：Linux `pulsara`管理/诊断入口在已选发行版可用，设置页明确展示“WSL内CLI”，不自动改Windows PATH。首版Linux `pulsara app`仍提示从Windows图标启动/聚焦；interop开启本身不实现这条产品接入，不新增反向常驻RPC。现有Linux/macOS平台app入口保持原行为；扩展WSL CLI聚焦能力需另定有限宿主动作合同。

## 9. 启动、关闭、失联与恢复的具体合同

### 9.1 正常启动

1. main取得Windows应用单实例/当前setup状态，准备startup cover；验证固定发行版、用户、执行域与安装资源。发现维护未完成或环境不合格则显示错误，不起另一kernel/PG。
2. `PythonRuntime`经HostOperations创建Linux `desktop_runtime`，pipe3仅为JSONL/stdout与日志stderr；Linux在收到start帧前不触DB。所有home/资源值来自同一装配。
3. 在同一Linux目标运行原`desktop_runtime --wait-owner`或包内独立wait-owner模式，等待旧kernel/物理PG操作持有的同一flock释放；不能先让PG操作helper持锁再嵌套取锁自锁。等待可显示/取消UI，不能据一个等待超时强杀或猜锁过期。**没有拿到真实锁完成就不进入PG/reset。**
4. TS `BundledPostgres`在Linux精确检查/准备/启动原专用PG；只有本次已证实启动/既有精确实例事实才更新原状态。先前PG身份不明或helper结果不明则停止本次启动，不删data。
5. PG可用后发原start帧，Linux沿原prepare_database/应用start、Protocol-v3/diagnostics初始化；WSL ready携带本次HTTPS endpoint及完整public certificate等启动值，stdout仍无普通日志/secret。
6. Node在任何工作台导航/请求前安装本次TLS/session策略，确认正确endpoint/ready；之后加载同一工作台并开放交互。ready前异常走原关闭/PG事实核验，不以healthz或端口通代替应用ready。

### 9.2 正常退出/reset

关闭窗口继续沿现tray/后台工作提示；用户明确Quit才进入原requestQuit。inspect失败/超时表示状态未知，只影响提示，不授权强杀。封新工作→发送原shutdown或关闭stdin→Linux原application.aclose结算terminal/MCP/hooks/worker与持有资源→停止Unix diagnostics/HTTP、释放kernel flock→实际退出。当前finally即使failed也可能发stopped/释放锁；**正常完成须本次没有收尾failed、实际Linux正常exit0、控制帧/pipe完整收回，锁只是并发准入事实，单独不是工具成功/物理回收证明。**

`PythonRuntime.stop()` 即使在 child 已清空后也检查本次已保存的 exit 结果；ready 事实随同一结果保留，不被新 attempt 重置。曾 ready 且真实 exit 非零须报告失败；载体先消失不证明 Linux kernel 或其执行范围已结算。

Linux relay 启动边界忽略载体挂断引起的 SIGHUP，由原 stdin EOF 路径封准入并进入 application.aclose。收尾期间保留物理锁，直到真实 kernel 和其执行范围结算；不能以存活 carrier 或默认信号退出代替 kernel 收尾。

随后只有TS owner确认本实例可停止时才执行Linux`pg_ctl stop -w -m fast`并核验status为已停；最后退休TLS/控制资源并退出Windows壳。无法确认结算就保留实际错误/数据/清理责任，阻止本次维护或reset继续，不发第二stop/reset或把超时升级为总寿命cap。reset沿原明确用户确认→关闭kernel→等待旧锁→当前helper取得原kernel锁→精确PG身份/停止→取得独立PG生命周期EX并重新确认已停→持两锁删除专用data→clean-v0重建（§3.3）；不使用Windowsrm/UNC权限替代Linux验证，不触workspace/settings。

### 9.3 失败矩阵

| 事件 | 必须行为 | 禁止推断/动作 |
| --- | --- | --- |
| Windows Electron崩溃/控制pipe EOF | Linux原EOF路径封准入并aclose；新壳重开等待同home锁、检查精确PG事实；无正常回复的本次输入/操作保留原unknown | 不自动恢复/重发模型工具，不把Windows进程结束当Linux范围joined |
| wsl.exe carrier被终止、管道截断、invalid JSON | 退休本次UI连接/停止新副作用；Linux是否已收EOF由G0与实际事实确认；等待原锁/诊断，不启动第二kernel | 不taskkill后宣称join，不按wslPID杀Linux，不让launch reservation因丢handle而消失 |
| 整个WSL/发行版被外部终止、OS关机 | 如实断开/unknown；下次启动由PostgreSQL恢复与现canonical数据决定可读状态，process-local观察丢失按原合同接受 | 不添加durable执行恢复日志，不假造所有工具成功/未执行 |
| 睡眠/恢复、VPN/代理/转发暂断 | UI显示失联并封相关新动作；只可重新验证同一live启动身份/连接，保留原任务；无法确认则按unknown+原锁重开路径 | 不把网络断连当用户取消/任务完成，不在后台再spawn第二个kernel |
| PG/helper可能执行但lost reply | 不采用未闭合回复；保PG/data，报告unknown；重新进入须锁与当前精确实例验证 | 不重放start/delete、复制startedHere、用旧postmaster.pid或Windowsstat作authority |
| 用户改WSL配置/挂载/root身份 | 重新观察实际目标/能力/ext4/权限；不合格项给具体错误，root执行拒绝，默认用户改变不自动换已保存身份 | 不覆盖用户配置，不把interop/自动挂载开启判作不合格，不静默切其他目标 |

睡眠恢复验收不能止于进程存活、健康检查和历史界面可见：还须验证原会话在writer资格仍有效或已失效两种情况下的后续操作，以及正常Quit的物理收尾。writer失效不得继续提交，也不得为恢复而静默重建已有epoch的provider输入；恢复沿原会话关闭/冷开边界。资格失效不能阻止旧owner完成自己已经持有的物理资源回收；真实scope、hook、MCP或I/O关闭失败仍须保留原失败屏障，不因捕获`StaleHostWriter`而统一报告成功。

睡眠恢复沿用现有自动恢复与失败界面。优先复用现有连接观察、重连、会话关闭及冷开owner：普通传输中断重新连接原live实例；writer已失效则封旧准入，先完成原会话物理关闭，再打开新的冷会话实例。不得复活旧lease、在原epoch内重建前缀、自动重发用户消息或模型工具，也不得为恢复启动第二kernel。前端保持历史与本页内存草稿，显示“正在恢复连接”；自动恢复不能完成时显示明确的恢复失败状态、重试入口和可展开技术详情。真实物理关闭失败继续阻断冷开/维护，不以友好文案遮蔽异常；用户级页面继续独立于活动会话。复用现有单次恢复尝试与用户重试，不另造恢复调度器、持久任务或寿命上限。

正常Quit、reset、升级和卸载均不终止用户共享发行版，不执行`wsl --terminate`、`--shutdown`或`--unregister`；只处理已确认归本应用所有的进程/资源。外部强制终止仅为故障输入，其破坏性实验在另备可丢弃环境执行，不拿用户日常Ubuntu试杀后宣称cleanup成功。[WSL命令范围](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

WSL官方启动实现区分pipe/terminal/file relay，并描述root退出后的IO与exit-code转发；carrier异常结束不能由此推出Linux后代全回收。G0-W-C必须验证真实所锁版本、EOF和原kernel锁生命周期，而非只mock Node child close。[WSL启动与relay](https://wsl.dev/technical-documentation/boot-process/)

## 10. 网络、认证边界与Windows界面

### 10.1 保持三条链路分开

内部Protocol-v3及diagnostics都留Linux私有Unix socket，CLI诊断也在Linux执行，不建立Windowsnamed pipe或额外端口。Windows→Linux宿主控制是原JSONL经wsl pipe；工作台/preview/流式观察才跨WSL localhost网络。PG仅Unix socket，不给Windows暴露数据库端口。

首先验证WSL原生localhost转发，Linux仅bind127.0.0.1，不默认改0.0.0.0、netsh portproxy或要求改全局mirrored网络。NAT/用户已有mirrored、代理/VPN/firewall实际行为各记录；不支持的配置给可操作错误而不是新通用proxy。[微软WSL网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)

### 10.2 端点身份与TLS

localhost可达不证明服务是本次Linux kernel；一次明文challenge后任意新连接仍可能误连重绑定端口。连接级身份由**标准TLS的最小WSL适配**提供：Linux本次进程生成短命server key/cert，通过可信继承控制pipe把完整public certificate与精确origin交Electron；私钥不出Linux执行owner。aiohttp现TCPSite接受SSLContext，无需复制HTTP服务器。[aiohttp接口](https://docs.aiohttp.org/en/stable/web_reference.html#aiohttp.web.TCPSite)

Electron在任何导航/HTTP/WS/SSE/preview/下载前创建本次launch专属workbench Session，使用已锁Electron的证书验证扩展点比对完整certificate和准确目标；原Host/Origin/Fetch-Site与IPC sender验证继续存在。证书callback不提供port且网络服务缓存验证结果，所以**还必须单独限制精确scheme/host/port，并为新launch使用全新Session/连接上下文**，不能在旧session热换pin/沿用旧缓存。当前WebContents.session构造时固定，main须在同一UI owner中为新launch更换实际workbench Window/WebContents，完整退休旧browser/views、StartupCover、IPC sender、SSE/上传/下载与连接，同时保存用户窗口/UI偏好；不能只换一个session变量或未经证明用clearAuthCache当证书验证缓存清除。G0-W-N验证重开/启动失败/慢退这一真实构造闭包。退休时先封旧请求、再关闭连接/清数据，旧origin、旧cert和端口rebind均不得被采用。[Electron Session官方API](https://www.electronjs.org/docs/latest/api/session#sessetcertificateverifyprocproc)

Session 退休复用各 owner 已持有的请求和下载对象：先封准入，abort 在途 ClientRequest，在 native 回调外 cancel 当前 DownloadItem，销毁对应 WebContents 并等待 destroyed，再关闭 Session 连接，等待原请求、下载和 Linux 流的终态。LocalImport 未发布的 stage 由原 owner 清理；不发布或重放不明结果，不增加通用任务 registry。

不使用全局ignore-certificate-errors、不将self-signed任意放行、不改系统trust store、不引入自制TLS/proxy/持久证书registry。完整public cert已携带，直接typed值比较，不另加DTO指纹；临时key/cert仅此次网络物理生命周期，必要私有临时文件0600/加载后删除，不作为durable身份。“短命”指随本次进程退出废弃，不能给长期运行任务增加证书到期的隐藏wall-clock cap；本地精确pin校验/时间处理必须由G0封闭，不能到期强制重启kernel、换epoch或中断任务。G0须验证fetch/SSE、sandbox HTML/null-Origin、PDF/image、下载、时间变化/长期连接及retire后的新连接，不仅首个healthz。

TLS证明server身份，不把本机同用户进程当远程攻击沙箱；现有客户端授权规则仍归原owner。任何新增client会话要求必须展示独立必要性并修规格，不偷偷把Protocol-v3 launch capability传给renderer或为TLS新增通用票据/认证数据库。若依赖扩展点不能满足以上每连接保证，停止B网络接入并修规格，不以单次challenge+后续明文fallback过门。

### 10.3 Windows原生界面与OAuth

win32加入真实host类型，frontend同时知道host平台与Linux执行域，不能`platform!=='darwin'`就把路径当Windows或把所有非macOS当Linux控件。验证Windows窗口控制/拖动/snap/DPI、tray失败可恢复入口、Ctrl/AltGr/中文IME、文件选择/拖放、PDF/image/HTML与正常后台退出。图形用户不会因选择Linux执行域被要求开terminal窗口。

Windows使用 `titleBarStyle: 'hidden'` 与Electron原生 `titleBarOverlay`，在现有44px顶行右侧提供系统最小化、最大化/还原和关闭按钮，不额外增加完整系统标题栏；菜单栏默认隐藏，保留Alt访问与原菜单快捷键。前端依据Window Controls Overlay的真实安全区域为顶行操作留位，四个工作区分类与Linux一样放在下一行；全屏、窗口缩放及DPI变化由原生布局更新。覆盖层跟随已有应用主题。首次设置/启动遮罩和工作台均不重画Windows按钮。关闭按钮仍沿原宿主close事件处理托盘隐藏/退出，不改变生命周期合同。macOS原生traffic lights和Linux现有自绘控件保持原方式。浏览器HTML预览只展示内容区，不模拟原生控件。

Windows 宿主在 Electron ready 之前调用 `app.commandLine.appendSwitch('disable-gpu-compositing')`，由 Chromium 使用软件合成，以处理本机遮挡/截图触发的黑边；WebGL 与视频仍可加速。macOS/Linux 不设置此开关，不新增焦点时修改尺寸、强制重绘或整体禁用 GPU 的路径。其他显卡及显示矩阵仍按实际环境验证。

列表单击选中文件夹，双击或Enter进入，支持后退/前进/上一级；底部明确显示待选路径，最终确认仍由原选择API重新验证。通过“浏览…”显式选到无效目录时，以Pulsara标识、品牌字样和简短状态的浅色遮罩覆盖路径工具栏和整个目录列表区域，导航、新建及列表项均不再可点击或聚焦；标题栏刷新/关闭与底部浏览/取消仍可用。底部保留无效路径，具体原因靠右与取消/确认按钮组成一组并垂直居中，确认按钮置灰；重新定位有效目录后撤下遮罩。工程根目录、允许的Linux文件系统范围、分页、显式创建及取消的原owner保持；新建文件夹从工具栏打开编辑行。导航记录与高亮仅是本次弹窗的内存状态，不增加持久状态。

OAuth授权仍原Linux loopback owner和已锁SDK拥有PKCE/state/issuer及token。实际`LocalMcpManagementService`已有async `open_browser`注入点；即使interop开启，guest默认`webbrowser.open`也不作为Windows系统浏览器成功打开的证明。仍经`DesktopApplication`→`KernelHostCore.production`/构造→management贯穿注入唯一OAuth-only host callback，明确由可信Electron打开并回实际结果。

本路线固定采用现可信JSONL的有限`open-oauth-browser`请求/结果（本次requestId、已由原OAuth owner验证的HTTP(S)授权URL、实际打开成功/失败）；Node `PythonRuntime`只为当前live launch校验请求、无userinfo/非HTTP(S)拒绝，并用`main`受限动作调用Windows `shell.openExternal`回实际结果。Linux先安装原callback listener再请求打开，Node在真实打开sink检查本地已知close/retire，拒绝其late请求。Linux发出请求后取消只关闭原flow/pending await并拒绝晚回调/结果，不能保证另一eventloop尚未获知的取消会撤销已排队或已打开的系统浏览器；打开结果不明如实保留，不自动再开，不为此增加跨域cancel协议/registry。回调/state/token裁决仍原OAuth owner；请求完成/取消/管道失联后原pending future与listener按现owner清理，无durable任务或通用反向执行RPC，code/token/client secret不经这个host通道。

callback实际验证Windows→WSL转发/代理、端口冲突/rebind、取消、PKCE S256、错误state/issuer与一次性复用拒绝，不能错服务误标authorized，不复制SDK grant/PKCE实现或另建OAuth协议/回调代理。自定义pulsara URI只沿现focus/单实例合同，不从argv接code/grant或交shell求值；TLS工作台origin与独立OAuth回调端口各有明确owner，不一并改协议。系统浏览器/普通Browser窗口没有Node/preload/任意本地文件或控制服务访问权。

## 11. 实现范围与发布边界

Phase A 的 POSIX 资源、宿主执行及 PG 接缝与 Windows 适配共用原生命周期 owner。B1–B4 已接入已有 WSL2 目标、离线 payload、控制链、PG、TLS、工作目录、文件、OAuth、恢复界面和 Windows 安装器；B5 发布资格仍独立验收。原生 Ubuntu 与 GUI 的延期不等于通过，也不阻止维护当前 Windows 实现。

## 12. 构建与维护约束

Linux 资源在所选 Ubuntu 的原生 Linux 文件系统中构建，Windows 仅校验并携带匹配版本的 payload。Python/npm/锁文件保持一致，字体及全部许可证随安装资源分发。沿用现有 build、release 与生命周期 owner，不增加并行编排、持久恢复或另一份运行配置。

## 13. 验证矩阵与完成条件

### 13.1 A的独立验证

A0记录真实before/after Git/dirty、OS/arch、工具版本、实际命令/结果/物理观察。不同checkout各用对应uv环境/独立wheel，记录`pulsara_agent.__file__`/CLI/资源来源，不能baseline实为candidate editable代码。测试不镜像薄转发，复用现有产品/真实OS断言，只补注入/partialfailure/文件操作接缝缺口。

必跑受改`desktop/src/{main,runtime,postgres,cli-install}*.test.ts`、`npm --prefix desktop test`、`tests/test_desktop_runtime.py`及真实PG/关闭/安装合同相关tests；代码检查/类型测试按现行workflow执行。cache/recipe若改，跑`desktop/scripts/test_build_cache.py`与实际freshness；应用wheel保持新鲜，未变nativeSDK可按原cache复用。

Mac arm64和native Ubuntu x64/arm64在受影响层各自验证；改随包定位/PG路径时做checkout外wheel、native资源relocation（`smoke_resources.py`、`tests/linux/installed_package_smoke.py`）及Mac与至少一个真实Ubuntu图形session的已安装启动/正常Quit/慢退/失败/重开/CLI检查。另一Linux架构触及native/安装差异或有未解问题也补GUI；证据分层明确，不把Mac/mock/container当native Linux/GUI。缺证据只能“实现完成、验证待补”。

A不因薄宿主抽取强制重跑未触及的全部terminal/filesystem/provider长程矩阵；若改变真实kernel/权限/控制/副作用边界，则受影响的现有聚焦与真实provider合同仍必需，不能因名叫抽象就免测。整个WSL Phase B必需的provider/长程门保持完整，不因A范围收窄删除。

### 13.2 G0-W/B的真实Windows资格

| 层级 | 必需场景/现有基础 | 证据边界 |
| --- | --- | --- |
| 接入/部署 | WSL未装/不可用/需重启、默认目标发现/明确选择、默认用户变动、目标缺失/root拒绝、用户私有payload离线安装/磁盘满/失败重开、更新保data与卸载保留 | 真实普通用户及已有WSL目标；保留其他工作/配置，不import或注销用户环境，mock不能证明 |
| 控制与PG | 当前`test_desktop_runtime.py`和TS runtime/PG tests；真实3pipe/UTF8、invalid/partialframe、start前后EOF、pg_ctl失联/stop/status/reset、同home旧锁 | Windowscarrier状态、Linuxroot退出、kernel flock、精确PG状态分别观察；不taskkill/全局shutdown收尾 |
| Linux工具/interop | `test_round2_terminal_*`、`test_pr03_process_termination.py`、`test_stage2_terminal_host_lifetime.py`、`test_terminal_cognitive_subtraction.py`；MCP/hooks/preview suites及Windows PE探针 | 真实WSL PIPE/PTY、root先退/后代持stdout、8槽、输入/取消、drain/join、credential guard；Windows程序正测、后代退出/取消/失联及unknown边界另验，不用Linux PGID证明Windows树结算 |
| 图形工作区 | 无session浏览/新建/选中/建会话；Explorer新建后刷新；取消/冲突/中文空格/权限/失联/替换/挂载变化/重开missing | 不使用mkdir补UI；同一Linuxcanonical目标与旧identity/availability合同 |
| 文件与网络 | 当前settings/Skill/plugin/no-follow/no-clobber、`test_local_file_import.py`、`test_session_file_preview.py`、`test_artifact_export.py`、HTML/image测试；真实WindowsFile/drop/保存目标 | Linuxext4与host文件域分开；read-at/held锁/published-unknown不改；端口重绑定/TLS旧cert/iframe/SSE/下载/普通Browser隔离实测 |
| 安装GUI | 图标冷暖启动→Linux PG/kernel ready→图形workspace→模型工具→附件/HTML→后台/tray→正常Quit/重开；IME/DPI/快捷键/OAuth | Windows11实际NSIS安装，ServerCI不等于桌面；不只npm start/wheel smoke |
| 回归/发布 | 现有Mac/Linux受改矩阵、构建cache、许可证/版本、DMG+两个deb+Windows组合安装器集合 | 当前资源构建/版本/release owner唯一，workflow手动触发保持，不自行publish或改PR发布权限 |

新增真实WSL机制/端到端测试可在`tests/windows_wsl/`及对应desktop测试，现有共享合同继续原位置。fake HostOperations只测Pulsara业务边界，不能证明WSL网络/锁/UNC/权限。不得批量skip/xfail/削断言让WSL变绿；不能加隐藏任务寿命cap。

### 13.3 真实provider、长程与激活

在实际Windows Electron+已选WSL2环境通过Linux LocalSettingsStore只读加载保存连接，在已核实本地disposable DB/隔离home中用原注入；不改保存设置、不导出key或source.env。保留实际prompt、SYSTEM/tools完整值、provider响应、工具结果、进程/线程/FD与故障观察，只精确删除实际credential。不生成代码/文档/激活证据hash。

至少覆盖：中文空格工程、Linuxvenv/Python/Git/Node工具、独立命令与running后继续工作、PIPE/PTY交互/end-input、monitor唤醒/输出截断补读、文件导入/HTML/image/导出、MCP stdio/HTTP与hook、subagent scope和compaction期间process跟进、正常/异常关闭后历史/memory检索。按既有长程合同验证，不限制总轮数/任务寿命；进程本地观察丢失不变成durable恢复产品。

**A完成条件**为11.1及13.1实际适用验收/review全部通过。**Windows激活条件**为A完成、G0-W全过（含共享环境不干扰及interop生命周期闭合）、B1–B5代码/旧路径hard cut完成、实际安装/图形工作区/路径/部署/生命周期/TLS/真实provider长程与受影响POSIX回归通过、无权限/数据/副作用未解P0/P1、原oracle与prefix不变、安装界面/文档如实声明已有WSL接入、实际任务回收范围与数据保留合同。任何缺项均不可宣称Windows支持完成；文档修订不冒充执行证据。
