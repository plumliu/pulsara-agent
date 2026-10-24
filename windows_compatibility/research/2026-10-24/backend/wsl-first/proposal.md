# Windows Electron + WSL2 全内核：backend 提案

2026-10-24；gpt-6.1-sol / high。仅方案/源码与官方材料核对，未改生产、保存配置或数据库，未运行 WSL/G0/provider。本提案替代本专题此前 native-Windows 与强制全量 POSIX 抽象迁移建议；根规格仍由 root 汇总及交叉审阅后冻结。

## 决策建议

采用 Windows Electron + **一个专用 Ubuntu 24.04 WSL2 发行版中的完整 Linux backend**。Python、PostgreSQL 及扩展、terminal/MCP/hooks、Playwright worker、Pulsara home、workspace 与所有 Python Unix sockets 都在同一发行版、同一非 root 用户运行。Windows 仅持桌面外壳、原生浏览器/UI、安装与用户明确发起的文件输入/输出桥。

取消“先把全仓库 fd 改 typed binding、先建 ProcessScope 再做 Windows”的必选前置；本路线根本不需要 Python Windows leaf。现有 Linux `local_source_binding`、fd/dir_fd、flock、renameat2、PTY/process group、selectors、signal、Unix socket 原 owner 可原样复用。只为真实跨 Windows/WSL 边界增添窄接缝；不抽空接口、Windows stub、平台 registry 或通用 RPC。不以此取消实际被改路径的 POSIX 回归。

**PG owner 与 desktop 已直接讨论并收敛：保留 TS `BundledPostgres` 唯一生命周期 owner，使用有限 Linux 执行/文件 mechanism port；不迁出第二个 Python bootstrap/PG lifecycle owner。** Python `prepare_database` 仍唯一拥有 role/db/schema。Linux helper 无自主启动/停止/重置策略，不保存生命周期状态、秘密或设置。

## 官方事实与证据边界

- WSL 官方命令支持显式发行版/用户、导入 rootfs、查询版本/WSL2；导入发行版默认可能是 root，须配置专用非 root，并每次显式指定。`--unregister` 会删除该发行版的数据，不能作为重试或普通卸载清理。[Microsoft basic commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)、[custom distro](https://learn.microsoft.com/en-us/windows/wsl/use-custom-distro)
- `wsl.exe --exec` 不经过默认 shell，`--cd` 是明确工作目录参数；当前官方源码声明了这两个选项。使用绝对 Linux cwd，不能继承 Windows cwd、使用 `--cd .` 或把路径拼进 `bash -lc`。[WSL wsl.h](https://github.com/microsoft/WSL/blob/master/src/windows/inc/wsl.h)、[official release notes](https://github.com/MicrosoftDocs/WSL/blob/main/WSL/release-notes.md)
- `/etc/wsl.conf` 是发行版局部配置，`.wslconfig` 影响全局。关闭 interop 指 Linux 启动 Windows 程序，关闭 Windows PATH 注入是独立设置；automount=false 不等于禁止手动 mount。应用不修改用户全局 WSL 网络/资源配置。[Microsoft configuration](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
- 微软建议 Linux 工具把项目放在 Linux 文件系统；DrvFs 的大小写、Linux 权限观察存在差异。此建议支持首版收窄范围，不是 `/mnt/c` 在技术上绝不可运行的证明。[Microsoft filesystem guide](https://learn.microsoft.com/en-us/windows/wsl/filesystems)
- Windows 通常可通过 localhost 访问 WSL Linux 服务，但 NAT/mirrored、全局 localhostForwarding、VPN/防火墙会影响实际行为。必须用 G0 验证现有 HTTP workbench/preview 路由，不能改绑 `0.0.0.0` 或自动修改全局配置来掩盖失败。[Microsoft networking](https://learn.microsoft.com/en-us/windows/wsl/networking)
- Windows 用户可显式以 root 进入自己发行版，Windows 主机权限优先。因此非 root、禁 interop/automount 是执行环境边界，不得宣传成抵抗同 Windows 用户的沙箱。[Ubuntu security overview](https://ubuntu.com/wsl/docs/latest/explanation/security-overview/)

这些是文档事实，不是本机 WSL 验证结果。Ubuntu 24.04 的选择基于现有 Ubuntu24 Linux资源/CI复用，不跟随 `Ubuntu` 默认别名漂到其他发行版版本。[Ubuntu install guide](https://ubuntu.com/wsl/docs/latest/howto/install-ubuntu-wsl2/)

## 默认拓扑与更窄的首版范围

| 项目 | 建议冻结 |
| --- | --- |
| 发行版 | 每 Windows 用户一个固定产品发行版，例如 `Pulsara-Ubuntu-24.04`；名称冲突或既有未知实例 fail closed，不自动接管/删除。其注册、安装位置及资源部署由桌面安装 owner 管理 |
| 用户 | 固定 `pulsara`，UID 非 0；运行时不 sudo、不要求用户现有 root 权限。初始镜像/一次安装步骤提供用户与 root-owned 资源；每次调用均 `-d fixed -u pulsara` |
| 配置 | 专用发行版 `[interop] enabled=false, appendWindowsPath=false`；`[automount] enabled=false, mountFsTab=false`，按产品镜像管理；不修改用户现有发行版/默认发行版/全局 `.wslconfig` |
| 资源 | Linux Python/rg/PG/extensions/Chromium 放该发行版 Linux ext4 上的应用资源目录；不用 Windows 安装目录下 `/mnt/c/...` 运行 Linux 二进制。镜像或更新载荷通过 Windows→WSL stdin/已定义部署途径送入，不依赖反向 interop |
| home | Linux `require_pulsara_home()` 解析，默认 `/home/pulsara/.pulsara`；Windows Electron userData/缓存/宿主日志在 Windows LocalAppData。二者不是字符串拼接共用一个 home |
| workspace | 首版建议 GUI 新建/选择仅 `/home/pulsara/workspaces` 子树，真实 Linux 本地 ext4、可读写搜索；已有项目需用户放入/clone 到该目录。不是跨发行版项目接入器 |
| 网络 | kernel HTTP 仍只绑定 Linux127.0.0.1；Windows Electron 经已验证 localhost forwarding 加载。PG/diagnostics/内部 Protocol-v3 不因 Windows 外壳改 TCP，全部继续 Linux Unix sockets |
| system services | 不安装/启用自动启动 PG 服务；BundledPostgres 是唯一启动路径。systemd 不能成为新的 PG watchdog。是否启用 systemd仅由既有依赖/进程回收验证决定，不为了启动应用引入服务常驻 |

反例/限制必须明示：现有用户发行版内已有 git/SSH/venv、项目不在产品发行版、企业禁 WSL、虚拟化关闭、VPN阻断localhost、需要 Windows-only MCP/PowerShell工具。专用发行版意味着这批项目/依赖不会自动继承；首版提供产品 Linux workspace 和清楚的导入/clone说明，而非自动改用户发行版。关闭 interop 后 terminal/MCP/hook 是 Linux 平台选择，不能为 Windows 二进制自动回退。

更窄方案：首版只给“在 Linux workspace 根创建项目/打开其中已有项目”，不实现全 Linux 文件管理器或 Windows 文件夹直挂。若产品坚持选择 `/home/pulsara` 任意项目，可把允许根放宽到该 Linux home，仍须排除 `.pulsara`、资源/系统/挂载目录并完整验证；这比固定 workspaces 子树有更大授权与 UI 成本。不要同时支持两套选择逻辑。

`/mnt/c` 不允许直接 workspace 是合理首版声明，但**不能只拒绝此拼写**：DrvFs 可改挂载位置、bind mount 可藏入允许子树。专用 rootfs 配置+Linux 实际 mount/filesystem 检查决定资格；nofollow 检查 symlink 并不检查 mount。验证时确认目标与批准 workspace 根属于预期 Linux ext4/设备，拒绝额外 mount/DrvFs/网络文件系统；不能声称这抵抗已控制 Linux root 的攻击者。工具本身保持既有权限模型，不把 enrollment 根误称所有文件工具的沙箱。

## PG：最小改动及故障闭合

现代码证据：

- `desktop/src/postgres.ts:10` `BundledPostgres` 的 start/status、PG_VERSION、postmaster.pid、socket目录权限、reset 全由 TS 顺序决定；`:20` binary exec 和 Nodefs 混用，Windows 下不能仅给命令加 `wsl.exe`。
- `desktop/src/runtime.ts:22` 在 PostgreSQL物理准备期间创建尚未获 start frame的 Python；PG失败则 EOF 并等待 child；`:119` stop 发 shutdown/EOF后等进程close。
- `desktop/src/main.ts:52` boot 回调先 waitForPreviousOwner，随后 PG.start，再给 Python start frame；`:113` reset 是 kernel.stop→wait-owner→PG.resetConfirmed；`:128` Quit先kernel.stop，后按原ownership条件PG.stop。
- `src/pulsara_agent/desktop_runtime.py:49/:75` 已验证私有 Linux Unix DSN，再由 prepare_database 创建/验证 role/db并迁移schema；`:263` start-frame前EOF直接退出，不触DB；`:225` frame后EOF发stop；finally经原 `application.aclose()`，最后释放kernel flock。

保留 `postgres.ts` 的流程、`initialized/startedHere` 与原 ownership。抽窄的 `postgres-io.ts`（名称候选）并由 `postgres-local.ts` 和 `postgres-wsl.ts` 提供机制；非Windows依旧Node本地实现。不要为了这一个 owner建全局文件service。Linux `desktop_postgres_io.py` 只提供：

1. `prepare_layout`：在精确Linuxhome下建立并验证desktop/socket目录及uid/mode、Unix socket长度；不 init/startPG。
2. `inspect_layout`：返回PG_VERSION、实际目录/socket身份及postmaster.pid所需字段/权限；ENOENT与其他错误分开。只能读owner固定布局，不能任意 read-file。
3. `exec_pg`：有限操作枚举 initdb/status/start/stop，固定随包binary及已验证home/layout，使用argv数组；返回真实 Linux退出码/stdout/stderr与明确命令结果。helper不决定下一步、不重试、不改DSN、不做DDL。
4. `delete_verified_pgdata`：仅TS已确认reset后调用；在Linux中重新绑定检查精确目录/父、uid、PG_VERSION、socket及真实stopped状态后删该data目录；拒绝任意rm路径、禁止删除home/workspace/发行版。查验失败/未明保原件。

可按实现合并前两种调用，未用方法不创建。请求通过固定 `wsl.exe -d ... -u ... --cd / --exec <bundled-python> -m ...` 的私有stdin JSON；严格字段与操作枚举，stdout只有一个有限结果，诊断stderr。不得以shell quoting或任意command RPC代替。请求不是持久事件/receipt；只交接当前调用物理事实。仍可直接 `wsl --exec` PG命令，但必须能区分Linux命令返回与wsl客户端/基础设施中断；固定helper结构化返回使该边界可测，最终只能选一套。

**WindowsChild.close 或 wsl.exe退出不等于 Linux PG已停止。** 正常 stop须取得 `pg_ctl stop -w` 的Linux结果，再确认status=3；只有之后可reset删除。结果丢失、JSON截断、WSL故障是unknown；保留责任/数据，不自动重发start/initdb/reset、不猜PID、不terminate发行版。后续显式重试或重开先wait-owner成功，再以Linux exact-layout/status事实重新观察；active正确实例可复用，未知/错误实例拒绝。PG自己继续拥有锁/recovery，不新增跨重启状态机或recovery journal。

Node崩溃：WSL真实stdin EOF必须抵达Linux child，走现有aclose；PG可能继续运行，允许下一次在kernel锁释放后验证复用。PG启动中失联也可能留下已运行PG；不因桌面错报启动失败清库。WSL被外部terminate等于外部故障，下一次由PG现有恢复处理；应用常规Quit/重试绝不调用 `wsl --shutdown/--terminate`。

进一步必须关闭 **inflight PG command** 的空窗：失联后瞬时 `pg_ctl status=3` 不证明先前仍执行的start稍后不会起库。建议有限PGio调用复用 `desktop_runtime.acquire_kernel_lock(wait=True)`，从Linux验证布局到实际PG子命令结算/确认结果、reset精确删除结束始终持同一kernel flock。其时段本来就在kernel未发start frame/已经退出之后，现有 `--wait-owner` 因而同时等到旧kernel与旧物理PG操作；不另建operation registry/持久receipt或第二生命周期owner。所有PGio状态检查和mutation均串行在此锁下；已运行kernel期间读诊断仍走原diagnostics socket，不反向调用会等待该锁的PGio。helper持锁fd必须CLOEXEC，不让长期PG server继承此fd；helper不因stdin EOF/结果发送失败放弃仍执行的子命令。G0须验证wslclient失联时helper仍能等该子命令、或发行版整体停止；若存在helper先死而pg_ctl继续的支持范围内故障，当前join/锁证明不足，应修订执行机制后再放行，不能只靠瞬时status猜完成。这是物理交接缺口，不是要求新恢复工作流。

EOF/flush/stdio 与PG运行结果可信性在G0实测；不是本文已经证明。任何kernel回收未知，Node保持退出未完成/重试界面，不能停止其可能仍使用的DB或强杀WSL。单实例仍Windows Electron负责，Linuxkernel flock负责跨客户端实际owner；CLI只透传现有诊断或唤起同桌面，不另开第二kernelowner。

## GUI workspace：当前API与最小新增

当前 `frontend/lib/runtime-adapter.ts:2228` 的pickWorkspaceDirectory只调用Electron `chooseDirectory`；`main.ts:218`会返回Windows目录。`session_controller.py:2441` project输入把字符串当Linux绝对Path，经 `workspace_identity.resolve_workspace` 产生canonical root/ID。**不能把Windows目录/UNC字符串直接送当前createSession。** `workspace_identity.py:149` 当前 NEW 会resolve symlink，也不能误称现有workspace resolver已实现严格no-follow enrollment。

建议在Python `desktop_app/workspace_directories.py` 加一个用户级application service，只为该WSL产品模式启用，HTTP加有限的：

- `POST /api/workspace-directories/list`：接允许根内Linux绝对path及cursor，返回已验证canonical Linux目录、子目录名称/分页；仅metadata，不读内容，不依赖session/connection。允许根来自当前运行拓扑，不从任意renderer覆盖。
- `POST /api/workspace-directories/create`：接绑定父目录路径和一个合法叶名，Linux目录relative/no-follow建立，返回实际验证的Linux路径；已存在目录不得静默当本次创建成功，冲突/权限/磁盘满分别反映。创建成功是用户文件副作用，不自动删因取消UI已创建的目录。

创建session仍用原 `POST /api/sessions` (`http_server.py:2294`) 的 `{workspace_kind:'project', workspace_path:<linux>}`，在admission前同一WSL enrollment验证器重新检查真实root/资格，然后调用原resolve_workspace/原canonicalID，不给每个路径加distroID/hash或持久picker token。list与session-create之间被替换须重新验证并拒绝不合格目标；目录清单不是authorization证据。原quick开始继续在Linuxhome管理目录，须同样满足WSL存储资格。

新GUI弹窗可“新建/选择WSL工作目录”，显示Linux路径和固定发行版名称；用户不必手输UNC。frontend workspace picker使用用户级API，不借某active session拿权限。现 `path_completion.py:15` 是会话内相对补全且跟随resolve，不可直接作为新picker安全实现；复用分页UI/模式而不把其语义拷成任意Linux文件浏览。API沿现same-origin/Host/security/write-admission机制，不宣称现有HTTP有不存在的bearer鉴权。

首次scope可只需目录列表/创建+确认已有目录；不做删除、移动、复制、跨发行版映射或导入整个项目的通用管理。project trust/MCP approvals仍在原workspace owner/设置中，选择文件夹不等于新增信任许可。

## 路径、import/export 与预览桥

唯一canonical值是Linux绝对路径：DB workspace root、memory key、tool结果、Skill/MCP/hooks、preview reading_root都如此。发行版固定在桌面拓扑，不能靠字符串同名声明另一发行版同一目录。Windows盘符、UNC、`file://server`不是Python路径输入；应用不启用全局任意wslpath转换。

| 场景 | 首版处理与授权边界 |
| --- | --- |
| Windows文件拖放/选择 | `prompt-draft.ts:190` 当前 `pathForFile` 直接插路径引用；WSL模式必须改为用户所选 File原始bytes→现有 `/api/connections/.../import-file`。现 `LocalImport` no-follow/exclusive publish/连接generation重检全部保留，返回Linux imports路径；不把 `C:\\...` 变 `/mnt/c/...` 引用 |
| Windows目录拖放 | 首版明确不支持直接workspace/目录引用；指引用户Linux目录clone/复制后选择。不要递归读任意Windows目录作为“路径适配”。选目录与上传文件是不同操作 |
| Linux项目内引用 | 原相对路径补全/显式Linux路径，原tools/preview权限不变。Windows Explorer选择同发行版UNC若未来接入，须先验证完整server/distro/分量，再交Linux端重新验证；本版GUI不依赖UNCpicker |
| HTTP preview/download | Python现fd-bound `file_preview.py` / `file_preview_http.py` 与 range/pread、token/connection/revoke、cancel-safejoin保留；Windows浏览器只取当前origin下token URL，不读UNC原文件 |
| HTML/markdown资源 | `file_preview.py:397` 已按held root_fd逐级nofollow，拒dot/control/backslash并仅解码一次；CSP/iframe/资源类型保持。WSL不加file://UNC改写、不允许任意Windows协议、origin-null CORS规则不扩大 |
| 产物/export | artifact/file工具先按原Linux no-clobber合同写Linux目标；“下载到Windows”是另一次用户明确保存副本，通过现HTTP attachment/浏览器下载（若新增Electron保存则由有限下载桥负责）。不把Linux写入成功等同Windows保存成功，不把Windows保存路径回写为canonical artifact来源 |
| Reveal/open externally | 原 `action_path`在Linux重查后给OS路径，不能直接交 `main.ts:231` Windows path.isAbsolute。首版可有限Linux→固定distro UNC仅用于Explorer reveal；路径分量不符合Windows表达时清楚拒绝，仍可显式下载。内容打开建议下载副本后由Windows打开；不要自动以UNC启动active HTML/脚本 |

UNC仅是presentation/用户显式OShandoff，**从不成为source binding/授权/identity**。支持映射时只固定 `\\wsl.localhost\\<product-distro>\\...`，不接 `\\server`、设备路径、其他distro、编码分隔符/路径穿越；保持case原样。Windows不能表达的一些Linux名称/尾部点空格等要真实测试并明确错误，不能normalize为另一文件。Linux action验证后到Explorer打开仍有原OS handoff竞态，不宣称原fd身份跨UNC保留。

WindowsElectron userData、browser cache/profile、Winhostlog仅在Windows；Linux home/logs/settings通过有限port/应用HTTP查看，Nodefs不能对 `/home/...` 做mkdir/read/reset。Linuxsavedsettings不因Windowsenv `PULSARA_HOME=C:\\...`被覆盖；bootstrap显式传Linuxhome或在Linux resolver取默认，并区分WindowsElectronhome配置。必要env逐项传递，WindowsAPIkey/OAuth缓存不做自动secret同步。

网络gate还须确认Windowsrenderer实际命中本次WSLkernel origin，端口碰撞/错误发行版/代理不被当作ready；同源header与token不能证明物理端点归属。复用当前启动通道做窄ready校验方案，由desktop/HTTP owner闭合，不建立通用双向RPC或新持久认证registry。所有local HTTP仅loopback，不因桥失效开LAN/改全局forwarding。

## G0-WSL 门与实际修改顺序

G0是WSL技术路径资格，不再要求nativeWindows PG/ConPTY/NTFS safe primitives。以下是未来试验，无一已执行：

| 门 | 原生Windows11 x64+专用WSL2实际证据 |
| --- | --- |
| WSL topology/resource | WSL2版本与Ubuntu24.04/非root/禁interop&automount真实生效；显式distro/user/cwd；Windows安装路径空格/Unicode；Linux随包Python/rg/Chromium加载；应用迁移/更新不改用户其他distro与全局WSLconfig |
| DB | 既有PG17.9+vector0.8.1+pg_search0.26.1 Linux资源在目标WSL加载、BM25/vector真实查询；私有Unixsocket/localtrust/hostreject；role/db/schema owners原样；现有PG复用、版本冲突、Node/WSLcommand失联、失联start/stop、dirtyrecovery、wait-oldkernel-before-reset、部分删除失败保原件 |
| 文件/path | ext4实际权限/uid/devino/flock/rename-no-replace、fd-held预览与HTML；拒DrvFs含改mount点/bind/symlink逃逸、错发行版UNC、反斜杠/percent双解码、Windows无法表达Linux名称；list/create/session admission竞态；Windows字节import与Linuxexport download区分 |
| 桥/停止 | WindowsElectron→WSL stdin/stdout UTF8/JSONL/EOF、丢失reply unknown；HTTPloopbackforwarding与端口碰撞/防火墙/WSL重启；previewtoken revoke/跨连接拒绝；Node崩溃Linux原aclose与PG复用；不强杀全WSL |

实施顺序：先G0最窄实际探针→资源/发行版bootstrap与唯一WSLcommand机制→Node runtime/PG有限port→Linux用户级workspace picker与admission→Windowsbytes import/reveal/download边界→已有Linux原合同与WindowsElectron+WSL整体验收。同步更新cache输入与安装资源，不夹带全仓库fd/ProcessScope重构。Linux/macOS原路径不改产品语义；旧nativeWindows实施条款应删除而非留自动fallback。

具体落点候选：

- `desktop/src/{main,runtime,postgres}.ts` 与实际WSLcommand/PGio leaf：home分离、固定distro/user绝对cwd、JSONL及启动/失联语义；`desktop_postgres_io.py` 仅finite physical事实。
- `desktop_app/{http_server,session_controller}.py`、新 `workspace_directories.py`、`workspace_identity.py`接WSL enrollment校验；不改其他POSIX用户任意合法workspace规则。
- `frontend/lib/{runtime-adapter,desktop-host,prompt-draft,file-drop}.ts` 与实际workspace选择组件；Windows上传模式/WSL目录选择；`desktop/src/{preload,main}.ts` 的host有限hand-off。
- `desktop_app/{file_import,file_preview,file_preview_http}.py` 主体复用；只在新增桥必要处接UI能力/错误，不改raw fd接口。
- 原Linux资源build配方/目标cache与WindowsNSIS/launcher由desktop拥有；PG扩展无需另开WindowsRust/pgrx链。

测试复用：`test_desktop_runtime.py`、`desktop/src/{runtime,postgres,main}.test.ts`；`test_local_file_import.py`、`test_session_file_preview.py`、`test_html_local_resources.py`、`test_local_skill_removal.py` 及真实export；当前workspace/session/UI对应测试；WSLgate需真实Windows而非Node mocks。LinuxPOSIX有改动者跑聚焦回归，最终跑原native/installed/provider矩阵中本路线实际要求的部分；不能用旧绿测或Linux容器代替WSL+WindowsGUI证据。

## 成本与未知

本专题（不含终端物理回收、browser/OAuth、WSLrootfs/Windows安装构建及全量联验）的净估计 **15–25人日**：NodePG有限port/unknown/reset故障闭合4–6；Linuxworkspace list/create/enrollment与GUI API接入4–6；Windows import/export/preview/reveal边界4–7；上述文件/DB/路径真实WSL聚焦验证3–6。共享WSLcommand机制、安装和最终provider/GUI联验只算一次，不把桌面/terminal同工作重复累加。全typed文件PhaseA原10–16人日应从必选范围移出，不能把新方案成本继续叠在native总预算。

未知：固定WSL最低版本/公司policy可用率；镜像大小、Ubuntu更新/资源部署与应用版本耦合；关闭interop后OAuth/browser与本地外部工具需要的窄桥；localhost在VPN/第三方防火墙的行为；WSLclient失联是否确定传EOF；bind mount/filesystem资格实现能否保持简单；UNC不可表达名称/WindowsSave副作用语义。G0据实回填，必要时修订首版支持范围；不把“不迁Windows fd”夸成“只改launch命令”。
