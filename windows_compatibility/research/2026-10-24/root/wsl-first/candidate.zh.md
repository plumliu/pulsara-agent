# Pulsara Windows 兼容实施规格：Windows Electron + WSL2 内核

日期：2026-10-24。状态：**Windows Electron + WSL2全内核实施规格已冻结。收窄后的Phase A/B未实施，G0-W未执行，Windows支持未激活。**

调研基线：Git `60e8f21d37ca2c650500ae14438630006d3e62fb`。本轮只修规格，不实施代码、不部署发行版、不修改保存配置、不执行数据库重置或真实provider。冻结规格、完成A、通过G0-W、Windows激活是四个不同状态。

## 1. 路线决策与支持范围

用户明确选择 **Windows原生Electron界面 + WSL2中的完整Linux执行环境**。Python kernel、terminal、MCP、hooks、Skills/plugins、私有PostgreSQL及扩展、rg、截图worker/Playwright Chromium均在同一受支持Linux发行版/非root用户中运行；Windows拥有窗口、tray、系统对话框、资源管理器、系统浏览器和安装入口。模型执行Linux命令，日常启动不经过PowerShell求值。

本次完整取代此前原生Win32内核路线与其宽Phase A：**ConPTY/Job、PowerShell载荷、Win32 HANDLE/NTFS全消费者binding、Windows PG DLL、内核named-pipe/Proactor迁移全部退出当前计划。** 不保留两条Windows内核路径，不把旧G0-D/T/F/R或88–154人日总数当当前验收/预算，也不以WSL失败自动回退原生Windows、WSL1、Git Bash、远程PG或Docker Desktop。

首批资格矩阵为Windows 11 25H2 x64普通用户、受支持且固定验证版本的WSL2、Pulsara专用Ubuntu 24.04 x64发行版；记录实际OS build/WSL包及Linux kernel/rootfs版本。26H2、ARM64、Server、WSL1、任意用户发行版、DrvFs/网络盘工程需另行目标验收后才扩声明。选择25H2作为首批测试矩阵不代表它是唯一当前Windows版本。[Windows官方版本信息](https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information)

**首版只接应用专用发行版`Pulsara`和非root用户`pulsara`。** 不选择用户默认Ubuntu、不修改其他发行版、不修改全局`.wslconfig`/默认发行版/默认WSL版本。专用环境的部署维护成本明确进入预算；此前“接已有WSL约35–65人日”的下界不能直接套用。专用发行版是部署/执行域边界，不宣称能抵御Windows账户拥有者、Linux root或任意恶意本机程序。

实施次序为：**收窄后的Phase A先完成现有macOS/Linux桌面宿主接缝及回归，可独立交付；G0-W可与A研究并行；A验收和G0-W全部通过后进入Phase B的WSL部署/跨域闭环；最终原生Windows安装验收才激活。** 不必先重构全部POSIX业务代码。

## 2. 当前代码事实、保留项与不变量

### 2.1 已核实的生产边界

| 当前位置 | 当前事实 | WSL路线的决定 |
| --- | --- | --- |
| `terminal_process/manager.py`、`environment.py` | Linux PTY/PIPE、独立shell、PGID、reader/watcher/Timer、环境probe、8槽 | 在WSL继续运行同一Linux代码；不为Windows新增physical/shell/backend抽象 |
| `local_source_binding.py`、`exclusive_publish.py`、settings/Skill/plugin/preview owners | fd/dir_fd、no-follow、flock、POSIX身份/权限、原子发布 | ext4中继续复用，撤销全面typed binding hard cut前置 |
| `terminal_protocol/v3_gateway.py`、`desktop_app/protocol_client.py` | 会话内部protobuf/auth/attachment经Unix socket通信 | gateway/client都在Linux，Unix协议原样保留，不搬loopback |
| `desktop_runtime.py` | stdin/stdout UTF-8 JSONL、start帧门、kernel lifetime flock、Unix diagnostics、application.aclose | Linux入口继续负责原kernel关闭；WSL carrier是外层传输，不成为kernel完成authority |
| `desktop/src/main.ts`、`runtime.ts` | main唯一boot/reset/requestQuit；runtime唯一子进程/start/ready/EOF/inspect owner | 注入宿主操作和明确host/guest路径，保留一套状态机 |
| `desktop/src/postgres.ts` | Node同时拥有PG状态/顺序与execFile、fs/path物理操作 | 保留唯一TS lifecycle owner；文件/PG操作经窄Linux执行件，不能仅给binary加wsl.exe前缀 |
| `desktop_app/http_server.py` | 当前HTTP 127.0.0.1、Host/Origin/Fetch-Site保护；origin硬编码http | WSL跨宿主链新增经G0验证的TLS连接身份；原aiohttp/路由/业务owner继续共用 |
| `desktop/src/main.ts`、preload、`frontend/lib/desktop-host.ts` | chooseDirectory返回本机路径；open-path用本机stat；platform只darwin/linux | 区分Windows宿主和Linux执行域；新增Linux-backed图形工作区选择，不把C盘路径塞入kernel |
| `frontend/lib/prompt-draft.ts`、`file-drop.ts`、`desktop_app/file_import.py` | 原生path直接变reference；无path的File已有原始字节导入 | 同发行版合法路径可转Linux引用；Windows文件显式导入副本，复用现有流式import |
| `file_preview_http.py`、`desktop/src/browser-policy.ts` | preview由token/reading-root控制；普通browser禁止控制服务整port和file:// | Linux文件沿专用preview HTTP(S)展示，不放开browser隔离或直接加载Linuxfile URL |

以上是源码观察，不是WSL实测结果。源码/测试实际路径以实施时引用闭包为准；root此前POSIX候选数量不作为当前迁移工作量或缺陷数量。

### 2.2 全程保留

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
  │    └─ WslHostOperations → wsl.exe -d Pulsara -u pulsara --exec …
  │         ├─ 长连接：Linux desktop_runtime，原JSONL start/ready/shutdown
  │         └─ 短调用：有限Linux PG/文件事实操作，一请求一退出
  └─ 专属workbench Session → 被精确认证的WSL HTTPS origin
       Pulsara WSL2 / Ubuntu24.04 / user pulsara / ext4
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
| 发行版/资源安装 | Windows setup owner、现有资源构建owner | 复用WSL注册/import与官方rootfs；安装维护使用有限显式root操作，日常模型不用root |

### 3.2 文件落点（计划，尚未创建）

Python生产仍在`src/pulsara_agent/`，Electron在`desktop/`，frontend在`frontend/`。`output/`仅个人研究/探针/证据，不被安装包import。

| 位置 | 计划变化 | 阶段 |
| --- | --- | --- |
| `desktop/src/resource-paths.ts` | 只读资源布局：分别返回host资源与guest POSIX资源；集中main/runtime/CLI真实重复 | A |
| `desktop/src/host-operations.ts`、`host-posix.ts` | 现有实际使用的stream child、有限exec、home解析及PG物理操作窄契约；本机实现直接复用Node/原代码 | A |
| `desktop/src/{main,runtime,postgres,cli-install}.ts` | 注入上述操作，沿用唯一状态/错误/退出顺序；去除被替代的内联操作 | A；B只加WSL接入 |
| `desktop/src/host-wsl.ts`、`wsl-setup.ts` | 固定-d/-u/--exec、有限调用结果分类、环境检查/import/provision/安装状态 | B |
| `desktop/src/backend-paths.ts`、`backend-session.ts` | 固定发行版路径映射、系统open/reveal、每次launch专属TLS Session与retire | B |
| `src/pulsara_agent/desktop_guest_ops.py` | Linux短命有限PG/文件操作；不含模型/任意shell/通用文件RPC，不拥有lifecycle/DDL | B |
| `desktop_runtime.py`、`desktop_app/{application,http_server}.py` | WSL启动信息/可选TLS装配、原关闭边界；保持同一Linux应用 | B |
| `desktop_app/application.py`、`conversation_kernel/host.py`、`capability/mcp_management.py`、`desktop_runtime.py`、`desktop/src/runtime.ts` | OAuth-only `open_browser`回调注入贯穿现owner；可信JSONL有限URL请求/结果接Windows系统浏览器，原SDK/回调监听不替换 | B |
| `desktop_app/workspace_directories.py`及`http_server.py`用户级路由 | 仅实际需要的目录分页/检查/单叶名创建；复用原workspace与文件机制 | B |
| `frontend/lib/{desktop-host,runtime-adapter,prompt-draft,file-drop,file-preview}.ts`及实际picker组件 | host/执行域分离、Linux图形工作区、导入副本、preview/open/reveal接入 | B |
| `desktop/scripts/prepare_resources.py`、必要`resource_build/wsl.py`、rootfs recipe、mise、package/release/workflow | Linux guest资源与Windows壳的组合构建/安装/重定位；输入变更及时追踪 | B |

名称可合并无独立职责的薄文件，契约仅含真实调用，不建平台容器/插件注册表/动态backend发现。Linux资源路径用明确POSIX操作；Windows路径用host API，不能以Windows`path.resolve`处理`/home/...`。同一owner装配时选一次实现，模型参数不能切发行版/用户/backend。

### 3.3 PG接缝必须覆盖文件，不只覆盖spawn

保留TS `BundledPostgres`，抽出其当前用到的`prepare_layout`、`inspect_version`、`verify_instance`、有限`exec_pg(initdb/status/start/stop)`、`remove_verified_pgdata`事实操作；返回完整typed结果/实际退出码/输出，不用hash或通用receipt。POSIX实现继续现有Node fs/exec，WSL实现调用固定包内Linux helper。helper限制在有效Linux home专用PG根与固定随包PG binary，不接受renderer任意程序/任意路径执行。

helper在同一发行版同一`pulsara`用户内检查uid/mode/realpath/PG_VERSION/postmaster.pid/data-dir/socket并执行现有必要文件操作。Node不能经UNC stat证明Linux0700/uid，也不能按可复用PID猜实例。helper一操作一退出，无daemon、队列、后台重试、自主start/stop/reset或第二`startedHere`；不读/写另一份settings。

**失联时不能只probe一次status=3：旧start可能仍inflight，稍后才起库。** PG物理helper复用现有Linux`kernel.lock`的同一flock，非阻塞取得失败即busy，不排队一个未来start；从布局验证到真实PG子命令settle/readback和本次文件操作结束始终持锁，已运行kernel期间的诊断仍走原Unix socket。锁fd为CLOEXEC，不让常驻PG继承；helper不因结果pipe坏掉放弃仍执行的child。`--wait-owner`是串行屏障，后续helper仍须自己取得锁，不把旧等待结果当操作授权。

为排除“旧helper尚未取得锁、父已死、稍后进入锁空窗才执行”这一实际边界，有限调用沿现kernel start帧模式：helper先取锁并发本次ready，Node收到后才发送一个有限operation帧；helper取锁前不接纳/缓存操作。Node在发帧的真实不可逆点再次检查原取消/close准入，取消不能留下晚到帧；operation帧写出前的任何取消（含已ready但尚未发帧）必须结束本次stdin并等helper实际退出，不只reject一个Promise。ready前父EOF则无操作退出；operation已发时物理锁已持有，失联仍待真实子命令结算/readback才释放，不能把发帧后的取消改报未发生。整个过程仅当前私有pipe/锁生命周期，无durable receipt、generation或新的操作registry。G0-W-C/P必须验证父在各边界退出与helper延迟调度；若所支持故障会让helper先死、pg_ctl仍活并释放锁，则该机制未通过，不能用瞬时status放行reset。

完整有效回复与carrier正常结束才可采用操作结果；wsl启动失败、截断、EOF、丢回复与pg_ctl真实非零不同。可能已经生效但未收全结果时返回本次结果不明，保留PG/data，不自动重发start/reset/delete或继续初始化。重新进入由TS先等原kernel锁，再通过当前Linux事实验证；不做跨重启操作重放。

## 4. G0-W：WSL路线可行性闸门

旧原生G0退出；新G0-W在真实Windows 11首批矩阵、普通用户、实际Pulsara发行版执行。Mac单测、native Ubuntu、mock win32、静态扫描不能代替。版本/G0输出放个人`output/`，不成为生产authority。

| 闸门 | 必须证明 | 失败时 |
| --- | --- | --- |
| G0-W-D 部署/执行域 | 官方rootfs可重现取得并import，固定发行版/用户/资源、WSL2与ext4真实有效；重开仍nonroot；interop与Windows PATH、automount配置实际生效；同名冲突不碰他人发行版；无管理员日常启动 | 不改用户默认发行版、不接现有Ubuntu兜底；明确缺虚拟化/企业策略/WSL版本/rootfs依赖 |
| G0-W-C 控制/生命周期 | Node直接argv+3条pipe→Linux原JSONL；中文/分段UTF-8/大量stderr/EOF；正常退出码、root/后代范围、启动中取消、Windows进程崩溃、carrier被终止、WSL终止/休眠恢复；原锁/端点能区分未结束/结果不明 | 不以wsl.exe close/taskkill证明Linuxjoin，不加全局shutdown/硬结束预算 |
| G0-W-P PG/资源 | 固定PG17.9/vector0.8.1/pg_search0.26.1及当前Python3.12.15、rg、Playwright在guest实际运行；clean-v0、BM25中英文/代码/snippet/vector、Unix socket、helper结果分类、退出重开/搬移与精确reset | 不换数据库/删搜索/开TCP trust，不通过Windowsfs删除data |
| G0-W-F GUI/路径/文件 | 无session时图形浏览/新建/选择Linux目录；UNC Explorer操作；同目标路径转换；拒C盘/他发行版/DrvFs/bind-mount替代；中文空格、大小写、Unicode、权限/替换/取消/丢回复；文件导入和preview/另存为 | 不要求用户mkdir补产品功能，不盲replace盘符或把C盘当Linuxworkspace |
| G0-W-N 网络/身份 | Windows Electron经localhost访问本次Linux服务；精确TLS身份覆盖新连接/SSE/preview/下载；端口占用/rebind/错误服务/旧证书不能误连或泄露内容；原Host/Origin/Fetch-Site、browser隔离、代理/VPN/firewall/OAuth实测 | 不用单次HTTP challenge冒充持续身份，不放宽0.0.0.0、忽略证书或默认另造proxy |

G0-W固定实际可用WSL最低/测试版本、rootfs来源与版本、SDK API和recipe；不以“最新版”漂移运行依赖。标准库/SDK扩展点能满足就复用；确有缺口记录具体要求和最小适配，改变产品/安全/执行合同先修规格再审。官方制品摘要/签名用于真实内容验证可以保留，不生成代码/文档/测试证据SHA。

## 5. Linux执行域与终端链路

### 5.1 保留当前Linux实现

manager/environment、原MCP/hooks/screenshot进程机制继续在Linux执行，不创建`_physical_windows.py`、`_shell_windows.py`、pywin32依赖或Windows能力占位。原始byte输出、PTY canonical EOT、PIPE关闭、root/group/drain/worker组合join、取消/输入结果/容量隔离按现行有效合同回归；不把PGID提升为“整个发行版所有进程/任意逃逸后代”保证。

probe继续原default-deny、single-flight/TTL、shell候选、`.venv/bin`与Linux PATH。MCP/hook保留各自credential guard真实spawn、frame/环境/超时/流任务与完成policy；不共享terminal8槽。Linux用户工具由用户在Pulsara环境安装，Windows已有Python/Node/Git/venv不被假称可直接复用。model看到的platform是Linux执行域，desktop-host.platform仍win32以正确显示窗口/快捷键。

### 5.2 Linux-only范围与interop

专用`/etc/wsl.conf`使用`[user] default=pulsara`、`[interop] enabled=false; appendWindowsPath=false`，以及关闭驱动器自动挂载的明确配置。两个interop开关作用不同，清`WSL_INTEROP`或隐藏PATH不等于禁止Windows进程。G0用绝对Windows exe和真实父子链负测实际生效，Windows UI动作由可信Electron完成，不从Linux执行`explorer.exe`。[微软配置定义](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)

不修改全局网络/内存配置；用户手动改专用发行版使执行域不再符合条件时，启动给具体错误并提供恢复指导，不静默转为混合Windows工具链。不把这些配置当恶意用户沙箱，不新增通用命令字符串黑名单。

Windows launcher用系统实际`wsl.exe`绝对位置、`shell:false`、明确argv、-d/-u/--cd/--exec，stream运行使用pipe而非外层PTY/交互shell。Windows环境不直接作为Linuxenv：清理自动传递的WSLENV/Windows PATH影响，只按原policy安装明确Linux HOME/PATH/locale/资源值；模型key仍Linux settings读取，不进launcher argv/环境。Linux terminal需要PTY时由原manager在guest创建，与外层控制pipe分开。

## 6. 图形工作区：无需命令行的产品流程

### 6.1 唯一主流程与数据范围

首版选择 **Linux-backed工作区picker**，不是同时维护Windows原生目录dialog和Linux目录picker两套默认流程。入口“新建工作区”“打开工作区”在Windows窗口内展示同一Linux目录服务；默认根`/home/pulsara/workspaces`由provision创建，所有首版project workspace位于该根内的实际Linux ext4目录。quick/transient workspace继续原Linux临时目录owner，不强制改成project，也不改变既有清理语义。

picker及服务是用户级，未创建/选择session也能使用。现有session `path-candidates`继续服务会话中的@补全，不借某个active session运行全局浏览/建目录。existing `workspace_identity.py`仍唯一canonical/availability/稳定key owner；WSL profile检查作为目录接纳边界，不给数据库新增Windows路径副本或发行版字段。当前NEW resolver会resolve链接，不能假称现已实现严格接纳；list/选择后在原create-session入场前再次执行同一WSL验证，不能仅前端检查，也不改其他平台resolver语义。首版固定一个执行域，未来多发行版必须另修身份合同。

| 图形操作 | 交互与后端行为 | 失败/取消结果 |
| --- | --- | --- |
| 打开工作区 | 分页列出目录、面包屑、返回上级；选择已有目录→Linux验证实际canonical/ext4/可读写执行→交给原create-session入口 | 不存在/不可访问/换目标显示具体状态，保持picker；取消不创建会话/改绑定 |
| 新建工作区 | 浏览父目录→“新建文件夹”→输入单个名称→Linux绑定父目录、exclusive mkdir/重新绑定并核对身份→返回实际Linux路径→用户选中后建会话 | 同名不覆盖/不自动加后缀；不能原子消除的namespace竞态按真实检查结算；会话创建失败不偷偷删用户新目录 |
| 新建子目录 | picker内同一按钮完成，不要求用户打开terminal；一次创建只接受单叶名，不接受shell/递归脚本 | 名称/权限/磁盘满等直报；取消未提交无写入；写请求结果不明时刷新当前目录核对，不盲重复mkdir |
| 最近工程/重开 | 原存储返回Linuxcanonical路径，重新观察MISSING/UNAVAILABLE；不按同名新目录重绑 | 不因WSL失联自动创建/移动workspace；恢复沿既有明确restore合同 |
| 在资源管理器中显示 | Linux先核对当前目标→host映射同一固定发行版UNC→Windows Electron reveal/open | 原发行版不可用/目标已变/名称不可表示时具体失败；不打开另一个同名目录 |

新增最小用户级HTTP操作：目录分页、目录接纳、在绑定父目录创建单个子目录；如采用`/api/workspaces/directories`，方法/DTO同一切片连前后端一起定，不新增通用文件管理RPC。现有权限/安全middleware覆盖这些写操作。复用已有100项分页边界的理由与cursor机制，不加目录总量/深度/任务寿命cap；目录读取只传所需名称/类型/路径/availability，不读文件内容。

Linux检查实际挂载/filesystem与canonical包含关系，防止不是`/mnt/c`字面路径却通过symlink/bind mount落到DrvFs；不靠字符串前缀判断存储语义。目录服务只在workspace根内操作，阻止`..`、NUL、分隔符、越界根/特殊项；沿原POSIX no-follow/身份检查，不为此全面重写文件binding。UI新建名称选择能被Windows资源管理器表示的明确规则，冲突/不合法不静默改名。Linux工具生成的其他合法Linux文件名不整体禁用；对应文件仍可经Pulsara preview使用，host reveal有具体不支持结果。

### 6.2 资源管理器仍可直接管理

用户可在Windows资源管理器打开`\\wsl.localhost\Pulsara\home\pulsara\workspaces`，新建/复制/重命名目录；默认Linux用户有相应权限即可。Pulsara刷新picker后可选择同一目录，不把“Linux文件系统”解释为只能mkdir。Windows通过WSL共享访问Linux文件采用该发行版默认用户权限，因此provision必须确保default user与执行用户一致。[WSL文件访问与权限](https://learn.microsoft.com/en-us/windows/wsl/file-permissions)

工程代码/依赖与应用数据保持Linux磁盘内。C盘工程首版不作为直接workspace：界面明确引导用户经资源管理器复制到上面目录后再选择；不隐式搬家、同步或重写Windowsvenv。用户在Explorer改名/删目录可能让既有会话MISSING，沿当前availability处理，不造目录重定位/同步恢复系统。

## 7. 路径转换、附件、预览与跨域传输

### 7.1 路径类型与转换

| 类型 | 示例 | 使用规则 |
| --- | --- | --- |
| Linux canonical | `/home/pulsara/workspaces/demo` | kernel/model/terminal、session/workspace identity、数据库、Skill/plugin来源使用；POSIX区分大小写 |
| 同发行版Windows访问路径 | `\\wsl.localhost\Pulsara\home\pulsara\workspaces\demo` | 仅host reveal/open或真实选择输入；另一合法别名`\\wsl$\Pulsara\...`经同一解析器归一，不写入canonical字段 |
| Windows本机路径 | `C:\Users\alice\Documents\a.txt` | 只属Windows host选择/导入源或另存为目的地，不直接传成Linux路径/模型reference |
| 资源URL | `https://127.0.0.1:<port>/api/file-previews/<token>/...` | 由现preview生成、由reading-root/token与TLS origin限定，不能反解成任意读文件授权 |

唯一`backend-paths`解析准确的共享主机/固定发行版、组件、Unicode与编码；拒其他发行版、远端UNC、设备路径、盘符相对路径、控制字符/歧义dot segments。Linux最终重验路径/挂载/权限；映射不是存在性或授权证明。反向转换只将已验证Linux目标映射到固定UNC，不用Windows`path.resolve`改写Linux路径、不统一casefold、不盲replace `C:`。可复用官方`wslpath`的真实映射能力，但首版产品仍只接固定发行版/ext4，不能因能转成`/mnt/c`就接纳为workspace。[微软路径互操作说明](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)

### 7.2 文件选择、拖放与导出

1. 从同一Pulsara UNC选择/拖入的文件或目录：可信host取得原路径→固定域转换→Linux原文件/preview owner验证→插入Linux reference。renderer粘贴路径同样必须重验，不当作OS来源证明。
2. Windows本机普通文件：用明确“从Windows导入文件”的renderer标准`<input type=file multiple>`取得浏览器File，显示“导入副本”，沿现File原始字节流→`LocalImport` staging/no-clobber→Linux位置→草稿确认后引用。保留现连接generation/controller、请求/资源边界和取消/清理，图片仍走现有accepted media规则。当前`chooseFiles():Promise<string[]>`跨IPC不能返回File，不允许把path字符串标成upload就声称已有字节，也不新增任意Windows路径读IPC/选择tokenregistry。原生UNC选择器仅为Linux文件/目录引用入口，工作区主picker仍为6.1唯一Linux-backed流程；已有drop/clipboard File按来源执行上述转换或副本导入。
3. Windows目录或其他发行版目录：首版不做隐式递归导入/同步；给出复制到Pulsara工程根的图形指引。不能伪装普通file上传或把C盘目录挂载为Linuxworkspace。
4. Linux内的模型artifact_export保持现目标/权限/no-clobber/published-unknown语义。用户“下载/另存到Windows”是独立明确host动作，复用token-bound download和Electron标准下载/保存机制，目标由用户图形选择，结果显示Windows路径；不把它伪装为Linux模型export已原子提交。
5. Windows另存为的取消/现有文件覆盖确认、断网/磁盘满/部分写入/结果不明均由原下载/host owner如实报告；不自动重复下载/覆盖或删除未核实目标。不增加跨域双写、同步目录或永久文件副本索引。G0-W-F必须验真实最终文件内容与错误，不只assert按钮可点。

### 7.3 preview与系统打开

Windows Chromium不能把`file:///home/pulsara/...`当作Linux文件。继续使用`FilePreviewHttp`的read_token、reading_root、冻结资源、受限HTML/CSP与sandbox，在工作台专用view中展示text/image/PDF/HTML；截图仍由Linuxworker完成。`frontend/lib/file-preview.ts`的本地链接保持Linux语义，不能把Linux file URL交Windows loadURL。

普通BrowserOwner继续禁止控制服务整port及file://，不为WSL放宽白名单；Preview与普通网页隔离保持。可信host执行open/reveal前经原preview action/明确workspace服务确认当前目标和允许文件类型，再映射UNC。外部OS打开依旧是用户明确动作，不承诺外部应用继续受preview冻结控制。web页面、HTML、附件不能调用preload、任意文件RPC或launcher。

## 8. 部署、资源、设置与维护

### 8.1 图形部署流程与权限

首次界面显示检查/初始化进度及具体失败，顺序：检测系统WSL可用→确认WSL2/虚拟化与版本→检查`Pulsara`注册名→校验固定官方rootfs/应用资源→以明确位置import专用WSL2→受控一次性root provision→设置nonroot/default user/config→重开验证→启动应用。WSL缺失/需OS功能启用或重启时，界面提供明确系统安装动作/指导并等待用户完成，不能自动提权或假报ready。普通用户后续启动/工作均不需要管理员、PowerShell窗口或手敲mkdir。[微软WSL安装](https://learn.microsoft.com/en-us/windows/wsl/install)、[rootfs import与默认root说明](https://learn.microsoft.com/en-us/windows/wsl/use-custom-distro)

只有专用发行版的初次provision/明确维护使用`-u root`，代码与参数来自随包固定recipe；模型/工具调用入口固定`-u pulsara`，不暴露root launcher给renderer。无需Docker Desktop/WSLg；不默认给agent免密sudo，不启systemd/boot command来另托管PG或kernel。发现同名非Pulsara环境/版本不匹配/部分import残留，显示冲突/状态，保留数据并走明确修复动作，不unregister重建来掩盖失败。

### 8.2 两个home与版本布局

| 位置 | 拥有的数据 | 约束 |
| --- | --- | --- |
| Windows标准每用户应用数据根 | Electron UI prefs、Windows安装/宿主日志、专用WSL磁盘文件的安装位置 | 原Electron owner管理，不存provider配置/第二数据库或canonical workspace；卸载不能误删保留中的WSL磁盘 |
| Linux `/home/pulsara/.pulsara` | 唯一LocalSettingsStore、PG data/socket、历史/记忆、原应用持久数据 | 原Linux owner/权限；不放DrvFs，不从Windows raw settings复制secret |
| Linux `/home/pulsara/workspaces` | 用户工程 | 与runtime安装根、PG数据根分离，应用更新不覆盖 |
| Linux固定版本资源根（如`/opt/pulsara/runtime/<app-version>`） | 私有Python/PG/extensions/rg/Chromium与应用wheel | root provision安装/验证，日常pulsara只读使用；准确布局由recipe/locator共同定义，不运行时下载latest |

Windows host home与Linux有效`PULSARA_HOME`是两个不同typed值；Windows用户环境里的`C:\...`不能直接当Linux home。首版Linux home固定默认，现有Linux resolver仍唯一，若显式允许Linux home override则必须先验证ext4/私有权限/Unix socket长度并让全链一致，不默默取Windows环境同名变量。为了少建配置，首版安装UI不提供自由发行版/用户/跨盘home配置。Windows资源安装根与保留数据根分开：专用VHD放每用户保留目录（例如LocalAppData下Pulsara/Data/wsl），不放版本化安装目录或会随Electron缓存清理删除的位置。

model/service连接在设置页写Linux现有owner；Windows壳不读取并缓存一套credentials。PG仍private Unix socket、原local auth/host reject；不需要原生Windows方案的postgres-auth.json/TCP SCRAM。首次缺模型连接展示现有设置页，不能因为系统已安装就宣称provider可用。Linux工具链安装状态如实展示，Windows用户工具不加入Linux PATH。

### 8.3 构建、更新和卸载

固定Canonical官方Ubuntu24.04 WSL rootfs的具体build、来源、摘要/签名和许可，可重现recipe不以浮动latest/current目录、用户已有Ubuntu或任意容器镜像替代；G0-D记录确切受支持制品。安装包携带已验证rootfs及对应Linuxpayload，支持离线import；WSL engine缺失是单独系统前提，界面明确在线/离线官方安装路径。基镜像定制/再分发与品牌义务在release前按官方政策核实，不能把自改镜像继续冒称未修改官方发行物。[Canonical定制说明](https://ubuntu.com/wsl/docs/latest/howto/custom-ubuntu-distro/)、[再分发政策](https://canonical.com/legal/intellectual-property-policy)

Linux x64 guest包使用现有Linux private resources和当前pins；Windows仅Electron/必要host资源，不构建win_amd64 Python wheel、Windowspg_search DLL或捆pwsh。interop/automount关闭后，部署通过明确stdin制品流/固定解包命令等支持途径传输资源，不能依赖`/mnt/c`或Linux调用Windows程序；权限、symlink和架构由Linuxrecipe验证，不在Windowszip解压后假称rootfs等价。拆recipe须在当片更新mise sources/outputs/cache fixture；内容校验沿既有真实制品边界，不增hash registry。

沿现build owner分开目标：native Ubuntu x64构建/验证guest wheel和Linuxpayload/rootfs制品，Windows原生runner构建Electron/NSIS并装入同版本已验证制品；不能在Windows误跑Mac/Linux native recipe，不能把Windows壳平台写成guest wheel平台。跨job传递保留既有不可变制品验证/版本完整性，mise分别声明rootfs/payload/host输入与输出，新模块变更立即失效。开发构建脚本如需Python仍用该平台仓库uv环境（Windows为`.venv/Scripts/python.exe`），这是构建依赖而非Windows内核。release集合在原`release_artifacts.py`加入精确`Pulsara-${version}-windows-x64.exe`，同步package/mise/workflow/文档和许可，保留DMG/两个deb及手动触发/发布权限；验证构建publish never，不自行bump/tag/publish。

更新为手动发布/安装：先正常Quit、确认原kernel/PG已按合同结算且无未解决状态，再安装新版本Linux资源并验证，最后使用新版本。数据/工程保持；不覆盖运行中的Python/PG/Chromium文件。失败保留原数据与错误，不能自动清库、重建发行版或引入双版本运行fallback。旧资源只在确无live owner使用且新安装成功后清理；不增加持久更新journal/恢复job。PG major/rootfs跨版本升级不冒充资源替换，须另定数据维护合同；安全更新使用明确固定发布recipe并负担Linux基础包持续维护。

普通卸载移除Windows应用入口，**保留Pulsara发行版、Linux settings/DB/workspaces与备份**，界面明确空间占用和重新安装可复用。彻底删除专用Linux环境是另一个破坏性用户操作：显示固定发行版/数据/工程范围、备份指引，完成精确正常关闭后再明确确认；不借卸载默认unregister，不对其他发行版执行命令。WSL export可作为用户选择的离线备份机制，不当自动checkpoint或唯一恢复authority。

NSIS沿electron-builder维护机制使用per-user安装及tracked customCheckAppRunning扩展，替换默认强杀行为。运行/收尾/unknown时等待用户正常Quit、取消或明确失败，不taskkill main/wsl/PG后继续覆盖/卸载。Windows全局控制台CLI不在首版：Linux `pulsara`管理/诊断入口在专用发行版可用；设置页明确展示“WSL内CLI”，不自动改Windows PATH。Linux `pulsara app`因interop关闭不能沿原manifest启动Windowsexe，首版明确提示从Windows图标启动/聚焦，不开interop、不造反向常驻RPC；Linux现有平台的app入口保持原行为，WSL实现沿明确执行域装配。需LinuxCLI直接聚焦WindowsGUI或Windows全局CLI时另修产品合同，而不是静默fallback。

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

`runtime.ts`当前`child`在close后清空，`stop()`若child已无会直接return；WSL接入必须保留本次已结束attempt的退出/失败/unknown并在stop/quit检查，不能因为carrier先消失便升级为clean success并让main停PG。复用现owner的process-local字段，不建durable退出receipt；刚创建尚未start-frame的无工作child可按原可证明not-started路径收回。

随后只有TS owner确认本实例可停止时才执行Linux`pg_ctl stop -w -m fast`并核验status为已停；最后退休TLS/控制资源并退出Windows壳。无法确认结算就保留实际错误/数据/清理责任，阻止本次维护或reset继续，不发第二stop/reset或把超时升级为总寿命cap。reset沿原明确用户确认→关闭kernel→等待旧锁→精确PG身份/停止→确认已停→有限helper删除专用data→clean-v0重建；不使用Windowsrm/UNC权限替代Linux验证，不触workspace/settings。

### 9.3 失败矩阵

| 事件 | 必须行为 | 禁止推断/动作 |
| --- | --- | --- |
| Windows Electron崩溃/控制pipe EOF | Linux原EOF路径封准入并aclose；新壳重开等待同home锁、检查精确PG事实；无正常回复的本次输入/操作保留原unknown | 不自动恢复/重发模型工具，不把Windows进程结束当Linux范围joined |
| wsl.exe carrier被终止、管道截断、invalid JSON | 退休本次UI连接/停止新副作用；Linux是否已收EOF由G0与实际事实确认；等待原锁/诊断，不启动第二kernel | 不taskkill后宣称join，不按wslPID杀Linux，不让launch reservation因丢handle而消失 |
| 整个WSL/发行版被外部终止、OS关机 | 如实断开/unknown；下次启动由PostgreSQL恢复与现canonical数据决定可读状态，process-local观察丢失按原合同接受 | 不添加durable执行恢复日志，不假造所有工具成功/未执行 |
| 睡眠/恢复、VPN/代理/转发暂断 | UI显示失联并封相关新动作；只可重新验证同一live启动身份/连接，保留原任务；无法确认则按unknown+原锁重开路径 | 不把网络断连当用户取消/任务完成，不在后台再spawn第二个kernel |
| PG/helper可能执行但lost reply | 不采用未闭合回复；保PG/data，报告unknown；重新进入须锁与当前精确实例验证 | 不重放start/delete、复制startedHere、用旧postmaster.pid或Windowsstat作authority |
| 用户改WSL配置/挂载/root身份 | 明确环境不合格，封本次启动/维护；只有专用环境的明确修复流程可修改 | 不改其他发行版/默认设置，不隐藏降级Windows进程混域 |

正常Quit不关掉整个发行版，更不执行`wsl --shutdown`。仅专用环境安装配置生效/明确彻底删除且所有应用owner已正常退出时，才可在显示范围后针对`Pulsara`执行必要发行版维护；不得波及用户其他WSL工作。外部强制终止是故障输入，不作为Pulsara正常cleanup实现。[WSL命令范围](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

WSL官方启动实现区分pipe/terminal/file relay，并描述root退出后的IO与exit-code转发；carrier异常结束不能由此推出Linux后代全回收。G0-W-C必须验证真实所锁版本、EOF和原kernel锁生命周期，而非只mock Node child close。[WSL启动与relay](https://wsl.dev/technical-documentation/boot-process/)

## 10. 网络、认证边界与Windows界面

### 10.1 保持三条链路分开

内部Protocol-v3及diagnostics都留Linux私有Unix socket，CLI诊断也在Linux执行，不建立Windowsnamed pipe或额外端口。Windows→Linux宿主控制是原JSONL经wsl pipe；工作台/preview/流式观察才跨WSL localhost网络。PG仅Unix socket，不给Windows暴露数据库端口。

首先验证WSL原生localhost转发，Linux仅bind127.0.0.1，不默认改0.0.0.0、netsh portproxy或要求改全局mirrored网络。NAT/用户已有mirrored、代理/VPN/firewall实际行为各记录；不支持的配置给可操作错误而不是新通用proxy。[微软WSL网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)

### 10.2 端点身份与TLS

localhost可达不证明服务是本次Linux kernel；一次明文challenge后任意新连接仍可能误连重绑定端口。因此冻结连接级身份要求，并选用**标准TLS的最小WSL适配**作为G0-W-N验证方案：Linux本次进程生成短命server key/cert，通过可信继承控制pipe把完整public certificate与精确origin交Electron；私钥不出Linux执行owner。aiohttp现TCPSite接受SSLContext，无需复制HTTP服务器。[aiohttp接口](https://docs.aiohttp.org/en/stable/web_reference.html#aiohttp.web.TCPSite)

Electron在任何导航/HTTP/WS/SSE/preview/下载前创建本次launch专属workbench Session，使用已锁Electron的证书验证扩展点比对完整certificate和准确目标；原Host/Origin/Fetch-Site与IPC sender验证继续存在。证书callback不提供port且网络服务缓存验证结果，所以**还必须单独限制精确scheme/host/port，并为新launch使用全新Session/连接上下文**，不能在旧session热换pin/沿用旧缓存。当前WebContents.session构造时固定，main须在同一UI owner中为新launch更换实际workbench Window/WebContents，完整退休旧browser/views、StartupCover、IPC sender、SSE/上传/下载与连接，同时保存用户窗口/UI偏好；不能只换一个session变量或未经证明用clearAuthCache当证书验证缓存清除。G0-W-N验证重开/启动失败/慢退这一真实构造闭包。退休时先封旧请求、再关闭连接/清数据，旧origin、旧cert和端口rebind均不得被采用。[Electron Session官方API](https://www.electronjs.org/docs/latest/api/session#sessetcertificateverifyprocproc)

不使用全局ignore-certificate-errors、不将self-signed任意放行、不改系统trust store、不引入自制TLS/proxy/持久证书registry。完整public cert已携带，直接typed值比较，不另加DTO指纹；临时key/cert仅此次网络物理生命周期，必要私有临时文件0600/加载后删除，不作为durable身份。“短命”指随本次进程退出废弃，不能给长期运行任务增加证书到期的隐藏wall-clock cap；本地精确pin校验/时间处理必须由G0封闭，不能到期强制重启kernel、换epoch或中断任务。G0须验证fetch/SSE、sandbox HTML/null-Origin、PDF/image、下载、时间变化/长期连接及retire后的新连接，不仅首个healthz。

TLS证明server身份，不把本机同用户进程当远程攻击沙箱；现有客户端授权规则仍归原owner。任何新增client会话要求必须展示独立必要性并修规格，不偷偷把Protocol-v3 launch capability传给renderer或为TLS新增通用票据/认证数据库。若依赖扩展点不能满足以上每连接保证，停止B网络接入并修规格，不以单次challenge+后续明文fallback过门。

### 10.3 Windows原生界面与OAuth

win32加入真实host类型，frontend同时知道host平台与Linux执行域，不能`platform!=='darwin'`就把路径当Windows或把所有非macOS当Linux控件。验证Windows窗口控制/拖动/snap/DPI、tray失败可恢复入口、Ctrl/AltGr/中文IME、文件选择/拖放、PDF/image/HTML与正常后台退出。图形用户不会因选择Linux执行域被要求开terminal窗口。

OAuth授权仍原Linux loopback owner和已锁SDK拥有PKCE/state/issuer及token。实际`LocalMcpManagementService`已有async `open_browser`注入点，但当前默认`webbrowser.open`在guest执行，禁interop/无WSLg后不会自动打开Windows浏览器；必须经`DesktopApplication`→`KernelHostCore.production`/构造→management贯穿注入唯一OAuth-only host callback，不能只写“由host打开”而保留原guest调用。

本路线固定采用现可信JSONL的有限`open-oauth-browser`请求/结果（本次requestId、已由原OAuth owner验证的HTTP(S)授权URL、实际打开成功/失败）；Node `PythonRuntime`只为当前live launch校验请求、无userinfo/非HTTP(S)拒绝，并用`main`受限动作调用Windows `shell.openExternal`回实际结果。Linux先安装原callback listener再请求打开，Node在真实打开sink检查本地已知close/retire，拒绝其late请求。Linux发出请求后取消只关闭原flow/pending await并拒绝晚回调/结果，不能保证另一eventloop尚未获知的取消会撤销已排队或已打开的系统浏览器；打开结果不明如实保留，不自动再开，不为此增加跨域cancel协议/registry。回调/state/token裁决仍原OAuth owner；请求完成/取消/管道失联后原pending future与listener按现owner清理，无durable任务或通用反向执行RPC，code/token/client secret不经这个host通道。

callback实际验证Windows→WSL转发/代理、端口冲突/rebind、取消、PKCE S256、错误state/issuer与一次性复用拒绝，不能错服务误标authorized，不复制SDK grant/PKCE实现或另建OAuth协议/回调代理。自定义pulsara URI只沿现focus/单实例合同，不从argv接code/grant或交shell求值；TLS工作台origin与独立OAuth回调端口各有明确owner，不一并改协议。系统浏览器/普通Browser窗口没有Node/preload/任意本地文件或控制服务访问权。

## 11. 收窄后的Phase A与Phase B实施顺序

### 11.1 Phase A：仅现有POSIX桌面宿主接缝

**A目标：不改变Linux/macOS产品行为，抽出Windows壳将实际调用的最小执行/资源/PG物理接缝并验证。A可独立合并交付，不以G0-W通过为前置。** 不建WSL stub、空Windowsadapter或外部稳定平台SDK。

| 顺序 | 具体位置/动作 | 退出条件 |
| --- | --- | --- |
| A0 基线/接口闭包 | 核现main/runtime/postgres/CLI直接/间接资源与close调用；记录改前Git/dirty、环境/安装来源；冻结实际被消费的方法 | 不把历史24/49条测试或前轮文档批准当当前基线，不预造没consumer的接口 |
| A1 Node纯locator | main resolveHome、runtime wait/start、CLI与实际PG/Playwright路径集中；明确host path与backend POSIX path | 当前Mac/Linux路径逐值相同，原home resolver唯一，无manifest/settings/进程副本 |
| A2 Host/PG操作 | 从当前owner提取stream spawn、有限exec、PG文件/实例校验与删除；同片全部caller注入POSIX实现 | 原boot/start帧/initialized/startedHere/重置/退出/CLI marker/PATH行为相同，旧执行路径删掉；无通用fs/RPC或第二PG状态机 |
| A3 回归/独立交付 | 聚焦Node/runtime/PG与原Python desktop契约、cache/安装来源、受影响native包/GUI生命周期；三专题交叉审阅 | Mac/Linux受影响场景真实通过才A完成；缺native/GUI证据标待验收，不以Windows未实现否决已验证POSIX接缝 |

**A明确撤出的工作：** 全库int fd→typed Directory/FileBinding、所有held DTO/锁/preview迁移、ProcessScope全caller合并、terminal physical/shell/probe大拆分、内部Unix socket transport重构、frontend Windows工作区/TLS/rootfs部署。它们不再是进入WSL路线的前置；如以后有独立产品收益，另立有依据的小任务，不在A借机做。

A的Python production通常无需结构变化，最多因真实测试注入调整现desktop入口；保留`pty/fcntl/dir_fd/killpg`在原Linux代码不是架构缺陷，不能用全库静态禁止扫描推动无关重构。每片hard cut的是实际被改接口/所有调用者，不是整个仓库。新资源leaf若确有必要，立即接mise sources/outputs并验证失效，不靠force补漏。

### 11.2 Phase B：A与G0-W通过后的接入

| 顺序 | 具体动作 | 完成条件 |
| --- | --- | --- |
| B0 闸门封闭 | G0-W-D/C/P/F/N各项原生证据，固定WSL/rootfs/SDK版本与窄缺口 | A验收完成，G0-W全过；新语义先修规格，不用探针作生产依赖 |
| B1 部署/资源 | 专用发行版import/provision、Linux资源/有效home、win host数据分离；setup图形进度、保数据维护 | 非root/interop/ext4实际符合；同名/失败残留/离线/磁盘满均不误删/改用户环境 |
| B2 载体/PG/网络 | WslHostOperations + Linux finite helper，接原runtime/PG顺序；TLS/session/HTTP origin同片接齐 | stream EOF/取消/真实PG实例/unknown不回归；同一kernel endpoint每连接身份、旧sessionretire成立 |
| B3 工作区/路径/传输 | 用户级Linux picker/API、新建/选择/最近目录、固定域路径转换/reveal、Windows文件导入/下载 | 全程图形创建到同一Linux工作区；不混C盘/他发行版；preview/HTML隔离、最终文件与失败结算实测 |
| B4 Windows壳/安装 | frontend host类型/窗口/tray/IME/OAuth、per-user NSIS/无强杀、CLI状态、组合资源cache/release矩阵 | 图标启动→Linux服务ready→工作→Quit/重开；卸载保data；实际资源版本/许可证/架构一致 |
| B5 激活 | Windows普通用户实装、故障/长程/真实provider、受影响Mac/Linux回归、文档/支持声明 | 第13节全满足才宣称Windows支持 |

资源recipe/图标/静态UI可提前研究，生产B仍受A/G0-W约束。B1/B2与B3可在明确接口上分工，最终B3依赖真实Linux环境与受认证网络，B4安装资格依赖真实闭环。每片保留当前已支持POSIX单一路径，不引入新旧SDK/API兼容层；CI尽早接，最后只收口完整矩阵。

## 12. 成本、取舍与维护预算

以下为熟悉仓库的资深工程师人日规划，不是模型运行时长或承诺；G0-W尚未执行，置信度低到中。新估算已包含专用发行版生命周期，而非只在用户已有Ubuntu里跑一条命令。

| 工作包 | 初步人日 | 归属/条件 |
| --- | ---: | --- |
| G0-W原生spike | 5–9 | 部署/载体/PG/GUI/网络依赖API与失败证据 |
| 收窄A | 4–8 | Node locator/宿主与PG物理接缝、POSIX对应回归；不计全库fd/terminal重构 |
| 专用WSL部署与资源维护 | 9–16 | rootfs许可/recipe、provision、版本化资源、保数据更新/卸载 |
| runtime/PG/身份网络 | 9–17 | 真实pipe/EOF/unknown、Linux有限操作、TLS/session/origin与生命周期 |
| GUI工作区/路径/文件 | 8–15 | 用户级picker/创建/验证、UNC/reveal、导入/另存/preview闭环 |
| Windows壳/安装发布 | 6–12 | 原生UI/IME/tray/OAuth、NSIS/cache/组合产物/文档 |
| 整体资格与收口 | 9–18 | 已安装、故障、真实provider/长程、Mac/Linux受影响回归与review |
| **合计** | **50–95** | 不需长期上游fork/新增执行域产品的前提 |

另留25%–35%不确定性，规划约63–129人日；硬件/签名采购、下载/OS重启、企业策略与测试机排队另计日历成本。原35–65+专用环境15–30仅为前轮路线粗估，本表是当前统一分配，不再相加；原生88–154及旧宽A退出。新成本降低来自复用Linux机制，不能因用户不喜欢pwsh就忽略rootfs和桥维护。

A与B的同一验证不重复计费；新增独立阶段安装/review劳动据实际差异单列。首批环境不可得/依赖缺扩展/需要proxy或rootfs迁移产品时重估，不把无界上游fork塞入reserve。持续成本包括Windows/WSL更新、Linux基础包与Python/PG扩展/Chromium安全更新、磁盘空间与备份指导、跨域路径/网络/休眠回归，仍低于维护两套内核/文件/PTY语义的范围。

原生Win32内核、任意已有WSL发行版、混合Windows命令、C盘直接workspace不是同期开关。未来需要时另立范围/身份/资源回收合同并重新估算，不在本次留可自动选择的fallback。

## 13. 验证矩阵与完成条件

### 13.1 A的独立验证

A0记录真实before/after Git/dirty、OS/arch、工具版本、实际命令/结果/物理观察。不同checkout各用对应uv环境/独立wheel，记录`pulsara_agent.__file__`/CLI/资源来源，不能baseline实为candidate editable代码。测试不镜像薄转发，复用现有产品/真实OS断言，只补注入/partialfailure/文件操作接缝缺口。

必跑受改`desktop/src/{main,runtime,postgres,cli-install}*.test.ts`、`npm --prefix desktop test`、`tests/test_desktop_runtime.py`及真实PG/关闭/安装合同相关tests；代码检查/类型测试按现行workflow执行。cache/recipe若改，跑`desktop/scripts/test_build_cache.py`与实际freshness；应用wheel保持新鲜，未变nativeSDK可按原cache复用。

Mac arm64和native Ubuntu x64/arm64在受影响层各自验证；改随包定位/PG路径时做checkout外wheel、native资源relocation（`smoke_resources.py`、`tests/linux/installed_package_smoke.py`）及Mac与至少一个真实Ubuntu图形session的已安装启动/正常Quit/慢退/失败/重开/CLI检查。另一Linux架构触及native/安装差异或有未解问题也补GUI；证据分层明确，不把Mac/mock/container当native Linux/GUI。缺证据只能“实现完成、验证待补”。

A不因薄宿主抽取强制重跑未触及的全部terminal/filesystem/provider长程矩阵；若改变真实kernel/权限/控制/副作用边界，则受影响的现有聚焦与真实provider合同仍必需，不能因名叫抽象就免测。整个WSL Phase B必需的provider/长程门保持完整，不因A范围收窄删除。

### 13.2 G0-W/B的真实Windows资格

| 层级 | 必需场景/现有基础 | 证据边界 |
| --- | --- | --- |
| 部署 | WSL未装/不可用/需重启、同名冲突、root import后nonroot、离线/磁盘满/失败重开、更新保data与卸载保留 | 真实普通用户/WSL包与专用发行版；假rootfs/mock不能证明 |
| 控制与PG | 当前`test_desktop_runtime.py`和TS runtime/PG tests；真实3pipe/UTF8、invalid/partialframe、start前后EOF、pg_ctl失联/stop/status/reset、同home旧锁 | Windowscarrier状态、Linuxroot退出、kernel flock、精确PG状态分别观察；不taskkill/全局shutdown收尾 |
| Linux工具 | `test_round2_terminal_*`、`test_pr03_process_termination.py`、`test_stage2_terminal_host_lifetime.py`、`test_terminal_cognitive_subtraction.py`；MCP/hooks/preview suites | 真实WSL PIPE/PTY、root先退/后代持stdout、8槽、输入/取消、drain/join、credential guard；Windowsexe负测符合Linux-only，不假称任意树全部受控 |
| 图形工作区 | 无session浏览/新建/选中/建会话；Explorer新建后刷新；取消/冲突/中文空格/权限/失联/替换/挂载变化/重开missing | 不使用mkdir补UI；同一Linuxcanonical目标与旧identity/availability合同 |
| 文件与网络 | 当前settings/Skill/plugin/no-follow/no-clobber、`test_local_file_import.py`、`test_session_file_preview.py`、`test_artifact_export.py`、HTML/image测试；真实WindowsFile/drop/保存目标 | Linuxext4与host文件域分开；read-at/held锁/published-unknown不改；端口重绑定/TLS旧cert/iframe/SSE/下载/普通Browser隔离实测 |
| 安装GUI | 图标冷暖启动→Linux PG/kernel ready→图形workspace→模型工具→附件/HTML→后台/tray→正常Quit/重开；IME/DPI/快捷键/OAuth | Windows11实际NSIS安装，ServerCI不等于桌面；不只npm start/wheel smoke |
| 回归/发布 | 现有Mac/Linux受改矩阵、构建cache、许可证/版本、DMG+两个deb+Windows组合安装器集合 | 当前资源构建/版本/release owner唯一，workflow手动触发保持，不自行publish或改PR发布权限 |

新增真实WSL机制/端到端测试可在`tests/windows_wsl/`及对应desktop测试，现有共享合同继续原位置。fake HostOperations只测Pulsara业务边界，不能证明WSL网络/锁/UNC/权限。不得批量skip/xfail/削断言让WSL变绿；不能加隐藏任务寿命cap。

### 13.3 真实provider、长程与激活

在实际Windows Electron+Pulsara WSL2环境通过Linux LocalSettingsStore只读加载保存连接，在已核实本地disposable DB/隔离home中用原注入；不改保存设置、不导出key或source.env。保留实际prompt、SYSTEM/tools完整值、provider响应、工具结果、进程/线程/FD与故障观察，只精确删除实际credential。不生成代码/文档/激活证据hash。

至少覆盖：中文空格工程、Linuxvenv/Python/Git/Node工具、独立命令与running后继续工作、PIPE/PTY交互/end-input、monitor唤醒/输出截断补读、文件导入/HTML/image/导出、MCP stdio/HTTP与hook、subagent scope和compaction期间process跟进、正常/异常关闭后历史/memory检索。按既有长程合同验证，不限制总轮数/任务寿命；进程本地观察丢失不变成durable恢复产品。

复用terminal/subagent/artifact等dogfood前先读runner当前API/连接/DB前置。artifact runner的`--phase baseline`会删tool，不能作为本次前后基线；两份代码均使用完整生产tool面的candidate，仅输出目录分before/after。部分DB helper只接受loopback根dbname=pulsara，不能直接套WSL managed Unix socket；修测试harness到已授权本地隔离方式或报告缺项，不为runner开PG TCP/改settings。硬编码输出须原样归档避免覆盖。

**A完成条件**为11.1及13.1实际适用验收/review全部通过。**Windows激活条件**为A完成、G0-W全过、B1–B5代码/旧路径hard cut完成、实际安装/图形工作区/路径/部署/生命周期/TLS/真实provider长程与受影响POSIX回归通过、无权限/数据/副作用未解P0/P1、原oracle与prefix不变、安装界面/文档如实声明专用WSL/Linux工具链/数据保留合同。任何缺项均不可宣称Windows支持完成；规格可先冻结，但不冒充执行证据。

## 14. 参考依据、采用与撤出

本轮继续参考当前生产、AGENTS与用户指定本地仓库；上一轮源码版本为deepseek-harness `687f282e8625`、codex `89461ea68338`、ZCode `3c5b22764937`、grok-build `2bdd1d6a6369`、opencode `37e3912d8ea5`。它们的Windows Job/ConPTY/PowerShell实现仅保留为已放弃原生路线的研究背景，不成为WSL生产依赖。ZCode/opencode的NSIS安装资源/更新退出、grok文件锁生命周期观察仍可作review反例，不复制其强杀/fallback/新manifest或恢复机制。

主要实际修改导航为`desktop/src/{main,runtime,postgres,preload,cli-install,browser-owner,browser-policy}.ts`、`frontend/lib/{desktop-host,runtime-adapter,prompt-draft,file-drop,file-preview}.ts`、`desktop_runtime.py`、`desktop_app/{http_server,application,file_import,file_preview,file_preview_http,path_completion}.py`、`workspace_identity.py`及现构建/安装/release owners。新增模块均是上述窄边界计划，未创建生产代码。

官方依据在相应章节近旁链接；文档证明平台能力/依赖扩展点，不证明Pulsara集成已通过。下载/布局/API须检查实际锁定版本实现，禁止拿别的agent产品已发Windows包推断本产品PG、物理join、文件授权或prefix已成立。

## 15. 工作区、交叉审阅与冻结记录

本轮使用原三位`gpt-6.1-sol / high` subagents，在`output/windows-compat-20261024/{root,terminal,backend,desktop}/wsl-first/`分别研究；root唯一汇总根spec，agent只在个人目录写提案/审阅。此前native/代码组织/宽POSIX-A审阅属于已取代版本，不能批准本次新路线。

前版695行规格的研究快照仅保存`root/wsl-first/previous-native-posix-spec.zh.md`供追溯，不再是当前实施authority；根文件完整替换旧原生目标、G0和强制A，避免追加WSL章节却保留相反前置。`output/`为本地忽略研究区，当前根spec独立携带实施合同，不依赖探针/报告运行。

已完成流程：root汇总387行唯一全文候选→三位各自通读全文并交叉核对其余提案→desktop指出OAuth系统浏览器实际调用接缝D1→root核源码、补真实注入链并闭合其他精确化→三位对394行最终语义候选全文复核，均明确“可冻结，无剩余阻断”→root逐份读审阅并独立认可→仅更新文首/本节冻结元数据。没有用沉默、agent结束或历史版本批准推断本次通过。

| 审阅者 | 个人提案/全文交叉审阅（相对工作区根） | 最终结论 |
| --- | --- | --- |
| terminal / gpt-6.1-sol high | `terminal/wsl-first/proposal.md`、`review-round1.md`、`review-round2.md` | 可冻结；保留Linux执行、PG握手/取消、stopped/exit/锁与carrier区别、长程边界闭合 |
| backend / gpt-6.1-sol high | `backend/wsl-first/proposal.md`、`review-round1.md`、`review-round2.md` | 可冻结；TS PG唯一owner、用户级picker/admission、文件源/路径/preview、OAuth与取消边界闭合 |
| desktop / gpt-6.1-sol high | `desktop/wsl-first/proposal.md`、`review-round1.md`、`review-round2.md` | 可冻结；D1关闭；部署保数据、File来源、TLS实际Session/WebContents退役、安装/构建/预算闭合 |
| root | `root/wsl-first/research-notes.md`、`validation.json`、`freeze-record.md` | 独立同意冻结；完整取代旧native/宽A前置，当前文档/位置检查通过 |

**最终事实：WSL2主路线及收窄Phase A实施规格已冻结。A/B实现与回归未执行，G0-W未执行，WSL部署/Windows原生验收/激活/发布未完成；本轮仅文档和只读研究，没有生产代码、保存配置或数据库变更，也没有新一轮产品测试/provider调用。**
