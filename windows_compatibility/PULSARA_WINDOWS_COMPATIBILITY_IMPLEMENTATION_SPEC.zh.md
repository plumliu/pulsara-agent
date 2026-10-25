# Pulsara Windows 兼容实施规格：Windows Electron + WSL2 内核

日期：2026-10-24；G0验收与Phase B授权更新：2026-10-25。状态：**Windows Electron + WSL2接入已有发行版/默认非root用户，保留interop与自动挂载。本机所选矩阵G0-W-D/C/P/F/N机制可行性验收通过（§22）。共享owner独立修正见§23；用户现已授权并开始Phase B正式实施（§24）。旧Phase A与Mac证据保留为历史基线，Ubuntu原生/图形资格按用户要求延期，Windows支持尚未激活。原四方冻结审阅属于修订前合同。**

2026-10-24 用户授权修订：删除Ubuntu发行版本号及`Pulsara`/`pulsara`固定命名要求；默认接入用户已有WSL2 Ubuntu及其默认非root用户，允许interop、Windows PATH注入和自动挂载。Ubuntu 26.04.1 LTS可作为候选，实际兼容性由资源与G0-W实测决定。无需另立解冻结流程；历史研究/批准记录保留原貌，本次修订及跨域生命周期仍须实际验证。

2026-10-25 G0阶段用户明确聚焦Windows WSL2，原生Ubuntu x64/arm64与GUI资格延期，不能以WSL代证，也不作为继续G0-W的阻塞。当时继续复用三位研究agent，保持interop及用户Hiddify TUN，不进入Phase B；后续正式实施授权见§24。G0验收对象是隔离探针与真实现有owner的机制可行性；§13.2中的正式NSIS/产品setup与生产界面闭环属B及激活验收，不能要求先实现B再给G0结论。研究接缝与fixture范围须逐项注明，不以此减免G0必须实测的控制、文件、网络与失联合同。

调研基线：Git `60e8f21d37ca2c650500ae14438630006d3e62fb`。路线冻结时仅修规格；后续按用户授权实施收窄Phase A，实际记录见第16节。不部署WSL发行版、不修改保存配置、不调用真实provider；真实PG实验仅操作已确认的独立临时实例。冻结规格、完成A、通过G0-W、Windows激活是四个不同状态。

## 1. 路线决策与支持范围

用户明确选择 **Windows原生Electron界面 + WSL2中的完整Linux执行环境**。Python kernel、terminal、MCP、hooks、Skills/plugins、私有PostgreSQL及扩展、rg、截图worker/Playwright Chromium均在同一受支持Linux发行版/非root用户中运行；Windows拥有窗口、tray、系统对话框、资源管理器、系统浏览器和安装入口。模型执行Linux命令，日常启动不经过PowerShell求值。

本次完整取代此前原生Win32内核路线与其宽Phase A：**ConPTY/Job、PowerShell载荷、Win32 HANDLE/NTFS全消费者binding、Windows PG DLL、内核named-pipe/Proactor迁移全部退出当前计划。** 不保留两条Windows内核路径，不把旧G0-D/T/F/R或88–154人日总数当当前验收/预算，也不以WSL失败自动回退原生Windows、WSL1、Git Bash、远程PG或Docker Desktop。

上段排除的是原生Windows内核的整套迁移。保留interop后的G0研究已实测用于Windows普通后代回收的窄Job候选（§18）；这不恢复ConPTY/原生kernel路线。生产Job/relay适配现按§24的Phase B授权实施。

首批资格矩阵为Windows 11 25H2 x64普通用户、受支持且固定验证版本的WSL2、用户选定的已有Ubuntu x64发行版；Ubuntu不预设发行版本号门槛，记录实际OS build/WSL包及Linux kernel/发行版来源与版本，按实测结果声明支持组合。26H2、ARM64、Server、WSL1、其他Linux发行版、DrvFs/网络盘工程需另行目标验收后才扩声明。选择25H2作为首批测试矩阵不代表它是唯一当前Windows版本。[Windows官方版本信息](https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information)

**首版接入一个用户明确选择的已有WSL2 Ubuntu，首次默认选中用户默认发行版并读取其默认非root用户。** 用户确认的具体发行版/用户名由现有Windows宿主设置owner保存；后续启动显式指定`-d/-u`，不因系统默认值变化静默切换数据。名称可为`Ubuntu`、用户可为`plumwsl`，home由Linux实际解析，不按用户名拼接。目标缺失、用户不存在或为root时显示具体错误，不自动创建/接管其他环境。不修改发行版默认用户、全局`.wslconfig`或系统默认发行版/WSL版本，不把共享发行版视作Pulsara独占资源或安全沙箱。

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
| `desktop/src/postgres.ts` | 调研时Node同时拥有PG状态/顺序与物理操作；A已将后者分到postgres-posix，状态/顺序仍唯一 | 保留唯一TS lifecycle owner；文件/PG操作经窄Linux执行件，不能仅给binary加wsl.exe前缀 |
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

### 3.2 文件落点（A已实现；B仍为计划）

Python生产仍在`src/pulsara_agent/`，Electron在`desktop/`，frontend在`frontend/`。`output/`仅个人研究/探针/证据，不被安装包import。

| 位置 | 计划变化 | 阶段 |
| --- | --- | --- |
| `desktop/src/resource-paths.ts` | A的纯只读本机资源locator：root、pythonExecutable、playwrightBrowsers、postgresBin、packageCli；只包含当前consumer。B再按真实需要引入guest路径 | A已实现；guest部分属B |
| `desktop/src/host-operations.ts`、`host-posix.ts` | 当前stream child、有限exec、同步home解析、runtime目录与日志；POSIX实现直接复用Node，runtime仍拥有策略与生命周期 | A已实现 |
| `desktop/src/postgres-operations.ts`、`postgres-posix.ts` | 专用PG物理端口及本机实现；与host执行端口分别直接复用Node，无跨域泛化或重复PG状态 | A已实现 |
| `desktop/src/{main,runtime,postgres,cli-install}.ts` | 注入上述操作，沿用唯一状态/错误/退出顺序；去除被替代的内联操作 | A；B只加WSL接入 |
| `desktop/src/host-wsl.ts`、`wsl-setup.ts` | 显式使用已选-d/-u/--exec、有限调用结果分类、目标接纳/资源安装状态 | B |
| `desktop/src/backend-paths.ts`、`backend-session.ts` | 固定发行版路径映射、系统open/reveal、每次launch专属TLS Session与retire | B |
| `src/pulsara_agent/desktop_guest_ops.py` | Linux短命有限PG/文件操作；不含模型/任意shell/通用文件RPC，不拥有lifecycle/DDL | B |
| `desktop_runtime.py`、`desktop_app/{application,http_server}.py` | WSL启动信息/可选TLS装配、原关闭边界；保持同一Linux应用 | B |
| `desktop_app/application.py`、`conversation_kernel/host.py`、`capability/mcp_management.py`、`desktop_runtime.py`、`desktop/src/runtime.ts` | OAuth-only `open_browser`回调注入贯穿现owner；可信JSONL有限URL请求/结果接Windows系统浏览器，原SDK/回调监听不替换 | B |
| `desktop_app/workspace_directories.py`及`http_server.py`用户级路由 | 仅实际需要的目录分页/检查/单叶名创建；复用原workspace与文件机制 | B |
| `frontend/lib/{desktop-host,runtime-adapter,prompt-draft,file-drop,file-preview}.ts`及实际picker组件 | host/执行域分离、Linux图形工作区、导入副本、preview/open/reveal接入 | B |
| `desktop/scripts/prepare_resources.py`、必要`resource_build/wsl.py`、mise、package/release/workflow | Linux guest应用资源与Windows壳的组合构建/安装/重定位；输入变更及时追踪 | B |

名称可合并无独立职责的薄文件，契约仅含真实调用，不建平台容器/插件注册表/动态backend发现。Linux资源路径用明确POSIX操作；Windows路径用host API，不能以Windows`path.resolve`处理`/home/...`。同一owner装配时选一次实现，模型参数不能切发行版/用户/backend。

### 3.3 PG接缝必须覆盖文件，不只覆盖spawn

保留TS `BundledPostgres`，抽出其当前用到的`prepare_layout`、`inspect_version`、`verify_instance`、有限`exec_pg(initdb/status/start/stop)`、`remove_verified_pgdata`事实操作；返回完整typed结果/实际退出码/输出，不用hash或通用receipt。POSIX实现继续现有Node fs/exec，WSL实现调用固定包内Linux helper。helper限制在有效Linux home专用PG根与固定随包PG binary，不接受renderer任意程序/任意路径执行。

helper在已选发行版同一已解析非root用户内检查uid/mode/realpath/PG_VERSION/postmaster.pid/data-dir/socket并执行现有必要文件操作。Node不能经UNC stat证明Linux0700/uid，也不能按可复用PID猜实例。helper一操作一退出，无daemon、队列、后台重试、自主start/stop/reset或第二`startedHere`；不读/写另一份settings。

**失联时不能只probe一次status=3：旧start可能仍inflight，稍后才起库。** 当前G0候选采用两种独立的OS生命周期锁：原`kernel.lock`继续串行kernel与维护；PGDATA外的`<home>/desktop/pg-lifetime.lock`以EX flock保护启动链/常驻PG及数据维护。新增锁的独立必要性是：helper独占原锁会在自身死亡时失去旧start保护，直接把原锁传给常驻PG又会阻断正常kernel（§18）。它只是空文件上的内核锁，不是持久状态、操作receipt、第二份startedHere或实例身份。两锁按kernel→PG顺序非阻塞取得；busy不排队未来操作。已运行kernel期间的诊断仍走原Unix socket。

新start先取得两锁，再ready/接纳本次操作；真实pg_ctl→shell→postgres启动链只继承PG锁，原kernel锁保持CLOEXEC。常驻PG持独立锁时，正常kernel仍可取得原锁。PG锁busy且status3必须保留inflight/unknown，拒新start及删除；已运行实例的复用/停止仍须当前准确data/socket/readiness事实，不以busy证明实例正确。stop/status等有限子命令继承原kernel锁，initdb及其有限后代继承两锁，防止helper死亡后旧有限命令晚执行。helper不因结果pipe坏掉放弃仍执行的child。

reset先取得原kernel锁并核对准确实例，必要时执行有限stop；随后必须实际取得PG生命周期EX、重新核实当前已停，并持两锁至删除完成。普通reset仅删已验证PGDATA，不能unlink/替换PG锁文件。锁文件沿真实私有home/desktop，O_NOFOLLOW打开并检查regular/uid/0600/nlink1；继承的是同一open file description，各owner只close自己的fd，不能用LOCK_UN连带解锁后代。`--wait-owner`仅是原kernel串行屏障，后续helper仍须重新取实际需要的锁，不把旧等待/stop结果当删除授权。此协议只约束遵守同一准入路径的操作，不以锁约束任意外部手动PG命令。

为排除“旧helper尚未取得锁、父已死、稍后进入锁空窗才执行”这一实际边界，有限调用沿现kernel start帧模式：helper先取锁并发本次ready，Node收到后才发送一个有限operation帧；helper取锁前不接纳/缓存操作。Node在发帧的真实不可逆点再次检查原取消/close准入，取消不能留下晚到帧；operation帧写出前的任何取消（含已ready但尚未发帧）必须结束本次stdin并等helper实际退出，不只reject一个Promise。ready前父EOF则无操作退出；operation已发时物理锁已持有，失联仍待真实子命令结算/readback才释放，不能把发帧后的取消改报未发生。整个过程仅当前私有pipe/锁生命周期，无durable receipt、generation或新的操作registry。G0-W-C/P必须验证父在各边界退出与helper延迟调度；若所支持故障使helper先死后仍可晚执行的PG副作用失去所需的最后准入锁，允许后续新start或删除进入，则该机制未通过，不能用瞬时status放行reset。

完整有效回复与carrier正常结束才可采用操作结果；wsl启动失败、截断、EOF、丢回复与pg_ctl真实非零不同。可能已经生效但未收全结果时返回本次结果不明，保留PG/data，不自动重发start/reset/delete或继续初始化。重新进入由TS先等原kernel锁，再通过当前Linux事实验证；不做跨重启操作重放。

第五轮有限helper候选将请求收窄为：持锁ready之后，恰一条完整LF结尾operation JSONL及stdin EOF才接纳执行；拒绝提前、重复、尾随残片。该EOF是一次有限调用的输入结束，不用于长连接desktop_runtime。回复须完整fatal UTF-8 decode、唯一ready/result、次序/方法/必要值类型正确，且carrier正常结束才采用；已发操作后的坏帧/截断仍为unknown。仅复用既有有限exec每流4MiB资源边界，不增加任务寿命限制。部分删除后失联如实保留unknown及部分目录，不自动恢复、重删或初始化；unknown不承诺撤销已生效副作用。

物理错误须向原TS owner区分“本次未提交”“可能执行但结果不明”“子命令已结算非零”，不能都由同一catch读当前status再更新startedHere。第四轮真实反例证明：本次未提交后，其他调用可在合法间隙启动同库，原owner随后status/readInstance会误记startedHere=true。§23已在共享owner和POSIX实现落实`PostgresCommandError.postgresExecution`的`not-submitted`/`unknown`/`failed`分类：未提交/unknown原样抛错，不据此次catch认领实例；明确已结算PG非零仍保持原readback和原错误。已有startedHere不因新错误被清空，不增第二份状态；status3也必须是已结算失败才可准入。未来WSL helper仍须真实实现该合同及双锁，不能用数值code3、当前同路径实例或本次POSIX修正代替跨域执行/准入事实。

**第三至六轮候选已有真实PG机制、TS owner与实际bundled kernel集成证据（§19–21），尚无生产WSL helper。** 已覆盖晚start拒绝、有限stop/initdb后代持锁、双锁内删除/重建、真实TS reset、有限framing、remove前中后失联、方法间竞争、真实PG非零readback及正常kernel退出后PGstop。明确未提交须关闭准入、拒晚ready写帧并等helper退出；发帧后不因EOF撤回副作用。宿主异常退出与整发行版/睡眠等组合仍须按当前证据分别裁定，不能把这些子项当整门资格。被否定的单helper CLOEXEC方案只作§18历史反例保留。

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

**跨域生命周期的G0-W-C机制验收已通过，当前正式接入及验证见§24。** 原Linux PGID/root/drain/join只证明其Linux范围，不证明interop启动的Windows后代已退出。必须实测Windows命令退出码、子进程持pipe、父先退/后代继续、取消、载体失联和正常Quit；不能确认的结果按unknown保留，不自动重发，也不把它升级为完整结算。正式接入须在生产owner重新验证Windows模型任务的取消/关闭范围，不能仅沿用G0探针结果；不能用关闭interop、杀整套WSL或削弱原物理结算断言放行。用户明确系统打开动作继续其已有OS交接语义，不承诺关闭外部编辑器/浏览器。

第五轮真实MCP/hooks/environment研究证明跨域scope必须接在原spawn/关闭边界，不能套用terminal槽或另建调度器。MCP stdio服务进程属于连接/会话生命周期；一次tool cancel可释放原call permit但不能擅自结束仍READY的服务会话，session close才结算其Linux/Windows范围。原timeout的remote UNKNOWN与现transport failure/fence处理保持，不重放工具。hooks成功/失败/取消、environment probe缓存写入均须等待其同一物理scope结算；credential guard拒绝应发生在scope prepare/spawn之前。研究证据见§21，当前生产接入见§24。

§18–20的无breakaway Job、专属anchor及私有socket字节转发只通过明确的机制/集成子场景。首次绑定候选由anchor自己打开上游O_PATH fd，经标准SCM_RIGHTS转交，以SO_PEERCRED/pidfd及本次可信anchor身份核对，避免另一进程重开旧PID路径。第四轮已把它放进真实terminal的启动预约内，与选定取消门合成；其余交错仍待验。裸WSL_INTEROP失效会回退，私有socket须保留到旧调用者结算，不能由Job空就提前unlink。

第三轮真实terminal baseline已证实Windows后代存活时会提前PHYSICALLY_JOINED并释放槽位；仅透传正确路由不足。未来最小接缝须把每次执行的跨域scope纳入原取消/物理join，原watcher继续唯一发布完成及finalize，Linux group/reader与Windows Job/精确HANDLE均结算后才能释放原槽位。内存实验通过PIPE/PTY正常、取消、输入及同registry A/B子项；没有生产接口或完整MCP/hook集成。正常完成等待自然退出，不用TerminateJob冒充success。取消封新准入并保留路径，还须结算Linux调用者；请求已转发后只有Windows Job空不足以证明其退出。半请求/失联保持unknown，不重发。不得把实验monkeypatch、文件RPC或探针直接import到生产。

第四轮进一步证明，仅延迟join仍不足：原`_status`及终止intent接纳也依赖物理存活谓词，须包含同一跨域scope，否则Linux root/group已退时会提前冻结success，误拒Windows尾部的取消。scope准备、线程启动rollback、存活判断和物理join共同纳入原owner，不能仅钩reader.join。relay seal须停止新准入并真实结算已接纳的connect/forward任务，再发ACK；同步创建而尚未首次执行的任务可能没有进入finally，创建方仍负责其已拥有资源。完成回调尚未调度也不能替代直接join事实。这些是研究接缝要求，生产manager未修改，具体实证范围及原失败见§20。

Windows launcher仍用系统实际`wsl.exe`绝对位置、`shell:false`、明确已选-d/-u/--cd/--exec和三pipe，不经PowerShell求值。应用入口使用明确Linux可执行文件，不因PATH注入调用同名WindowsPython/Node；模型使用的是已选用户的实际环境，允许Windows PATH项存在。清理本次宿主启动环境的WSLENV自动传递清单，不抹除Linux会话的interop能力；credential仍沿原guard和Linux settings读取，不进launcher argv/宿主日志。Linux PTY仍由guest原manager创建。

## 6. 图形工作区：无需命令行的产品流程

### 6.1 唯一主流程与数据范围

首版选择 **Linux-backed工作区picker** 作为唯一接纳流程；picker底部的“浏览…”可调用Windows原生目录窗口作为候选路径入口，不形成第二个接纳owner。2026-10-26用户修订：入口“新建工作区”“打开工作区”在Windows窗口内展示同一Linux目录服务，直接从所选Linux用户的实际home（`~`）开始，允许选择该home及其中符合原权限/canonical/native-ext4检查的目录，不再要求工程位于`~/workspaces`。GET不创建目录，删除专用workspaces根初始化接口；home缺失或不可访问按实际错误展示，不由应用重建Linux用户home。quick继续由原owner在有效PULSARA_HOME下的`workspaces/quick-*`创建并保留，不强制改成project，也不改变既有清理语义。

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

G0发现`runtime.ts`的`child`在close后清空时，原`stop()`会直接return而漏查异常退出；§23已在共享owner修复：stop仍检查已保存的本次exit结果，ready事实随同一个结果保存，不能被新attempt重置。该修正保持原判据“曾ready且实际exit非0则失败”，不以此宣称已完成WSL carrier/控制帧/信号的完整unknown合同。WSL接入仍须保证不能因carrier先消失便升级为clean success并让main停PG；复用process-local事实，不建durable退出receipt，刚创建尚未start-frame的无工作child沿原可证明not-started路径收回。

第六轮在真实宿主强退后观察到kernel收到SIGHUP（未记录发送者siginfo），原默认动作会先于stdin EOF收尾终止kernel。研究启动接缝仅忽略该载体挂断信号后，Node/Electron两项均实际进入原EOF/application.aclose，关闭门持有时kernel活且原锁忙，放门后完成原收尾并由独立pidfd确认退出。B必须在Linux启动边界保留该挂断处理与原EOF所有权，不能靠存活carrier或原默认signal行为推定已清洁关闭；不新增daemon/第二kernel owner，不忽略任意致命信号。宿主已死仍不能获得新的clean回复，本次未确认输入保持unknown。

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

2026-10-25用户进一步授权睡眠后的自动恢复与友好失败界面。优先复用现有连接观察、重连、会话关闭及冷开owner：普通传输中断重新连接原live实例；writer已失效则封旧准入，先完成原会话物理关闭，再打开新的冷会话实例。不得复活旧lease、在原epoch内重建前缀、自动重发用户消息或模型工具，也不得为恢复启动第二kernel。前端保持历史与本页内存草稿，显示“正在恢复连接”；自动恢复不能完成时显示明确的恢复失败状态、重试入口和可展开技术详情。真实物理关闭失败继续阻断冷开/维护，不以友好文案遮蔽异常；用户级页面继续独立于活动会话。复用现有单次恢复尝试与用户重试，不另造恢复调度器、持久任务或寿命上限。

正常Quit、reset、升级和卸载均不终止用户共享发行版，不执行`wsl --terminate`、`--shutdown`或`--unregister`；只处理已确认归本应用所有的进程/资源。外部强制终止仅为故障输入，其破坏性实验在另备可丢弃环境执行，不拿用户日常Ubuntu试杀后宣称cleanup成功。[WSL命令范围](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

WSL官方启动实现区分pipe/terminal/file relay，并描述root退出后的IO与exit-code转发；carrier异常结束不能由此推出Linux后代全回收。G0-W-C必须验证真实所锁版本、EOF和原kernel锁生命周期，而非只mock Node child close。[WSL启动与relay](https://wsl.dev/technical-documentation/boot-process/)

## 10. 网络、认证边界与Windows界面

### 10.1 保持三条链路分开

内部Protocol-v3及diagnostics都留Linux私有Unix socket，CLI诊断也在Linux执行，不建立Windowsnamed pipe或额外端口。Windows→Linux宿主控制是原JSONL经wsl pipe；工作台/preview/流式观察才跨WSL localhost网络。PG仅Unix socket，不给Windows暴露数据库端口。

首先验证WSL原生localhost转发，Linux仅bind127.0.0.1，不默认改0.0.0.0、netsh portproxy或要求改全局mirrored网络。NAT/用户已有mirrored、代理/VPN/firewall实际行为各记录；不支持的配置给可操作错误而不是新通用proxy。[微软WSL网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)

### 10.2 端点身份与TLS

localhost可达不证明服务是本次Linux kernel；一次明文challenge后任意新连接仍可能误连重绑定端口。因此冻结连接级身份要求，并选用**标准TLS的最小WSL适配**作为G0-W-N验证方案：Linux本次进程生成短命server key/cert，通过可信继承控制pipe把完整public certificate与精确origin交Electron；私钥不出Linux执行owner。aiohttp现TCPSite接受SSLContext，无需复制HTTP服务器。[aiohttp接口](https://docs.aiohttp.org/en/stable/web_reference.html#aiohttp.web.TCPSite)

Electron在任何导航/HTTP/WS/SSE/preview/下载前创建本次launch专属workbench Session，使用已锁Electron的证书验证扩展点比对完整certificate和准确目标；原Host/Origin/Fetch-Site与IPC sender验证继续存在。证书callback不提供port且网络服务缓存验证结果，所以**还必须单独限制精确scheme/host/port，并为新launch使用全新Session/连接上下文**，不能在旧session热换pin/沿用旧缓存。当前WebContents.session构造时固定，main须在同一UI owner中为新launch更换实际workbench Window/WebContents，完整退休旧browser/views、StartupCover、IPC sender、SSE/上传/下载与连接，同时保存用户窗口/UI偏好；不能只换一个session变量或未经证明用clearAuthCache当证书验证缓存清除。G0-W-N验证重开/启动失败/慢退这一真实构造闭包。退休时先封旧请求、再关闭连接/清数据，旧origin、旧cert和端口rebind均不得被采用。[Electron Session官方API](https://www.electronjs.org/docs/latest/api/session#sessetcertificateverifyprocproc)

第七轮真实在途实验中，仅关闭连接及销毁窗口/取消下载后，三条尚未收headers的请求未在本次观察截止前结算；没有据此推断SDK在所有场景的行为。退休须复用各原owner已持有的请求/下载对象：封准入，abort在途ClientRequest、在native回调外cancel本次DownloadItem，销毁对应WebContents并等destroyed，再关闭Session连接并等原请求/下载终态与Linux流清理。真实LocalImport的部分stage被清、没有发布或重放；这个组合不新增通用任务registry。生产renderer fetch/SSE、IPC与connection的具体关闭仍由各现owner装配并验收，不能把研究壳的几个对象列表当产品通用实现。

不使用全局ignore-certificate-errors、不将self-signed任意放行、不改系统trust store、不引入自制TLS/proxy/持久证书registry。完整public cert已携带，直接typed值比较，不另加DTO指纹；临时key/cert仅此次网络物理生命周期，必要私有临时文件0600/加载后删除，不作为durable身份。“短命”指随本次进程退出废弃，不能给长期运行任务增加证书到期的隐藏wall-clock cap；本地精确pin校验/时间处理必须由G0封闭，不能到期强制重启kernel、换epoch或中断任务。G0须验证fetch/SSE、sandbox HTML/null-Origin、PDF/image、下载、时间变化/长期连接及retire后的新连接，不仅首个healthz。

TLS证明server身份，不把本机同用户进程当远程攻击沙箱；现有客户端授权规则仍归原owner。任何新增client会话要求必须展示独立必要性并修规格，不偷偷把Protocol-v3 launch capability传给renderer或为TLS新增通用票据/认证数据库。若依赖扩展点不能满足以上每连接保证，停止B网络接入并修规格，不以单次challenge+后续明文fallback过门。

### 10.3 Windows原生界面与OAuth

win32加入真实host类型，frontend同时知道host平台与Linux执行域，不能`platform!=='darwin'`就把路径当Windows或把所有非macOS当Linux控件。验证Windows窗口控制/拖动/snap/DPI、tray失败可恢复入口、Ctrl/AltGr/中文IME、文件选择/拖放、PDF/image/HTML与正常后台退出。图形用户不会因选择Linux执行域被要求开terminal窗口。

2026-10-25 用户界面修订：Windows使用系统原生完整标题栏（`titleBarStyle: 'default'`），由系统提供最小化、最大化/还原、关闭与标题栏拖动；首次设置/启动遮罩和工作台不再绘制Windows窗口控制按钮，也不为它们保留页面内占位。关闭按钮仍沿原宿主close事件处理托盘隐藏/退出，不改变生命周期合同。macOS原生traffic lights和Linux现有自绘控件保持原方式。浏览器HTML预览只展示内容区，不模拟系统标题栏。

2026-10-26 目录选择器界面修订：列表单击选中文件夹，双击或Enter进入，支持后退/前进/上一级；底部明确显示待选路径，最终确认仍由原选择API重新验证。通过“浏览…”显式选到无效目录时，以Pulsara标识、品牌字样和简短状态的浅色遮罩覆盖路径工具栏和整个目录列表区域，导航、新建及列表项均不再可点击或聚焦；标题栏刷新/关闭与底部浏览/取消仍可用。底部保留无效路径，具体原因靠右与取消/确认按钮组成一组并垂直居中，确认按钮置灰；重新定位有效目录后撤下遮罩。工程根目录、允许的Linux文件系统范围、分页、显式创建及取消的原owner保持；新建文件夹从工具栏打开编辑行。导航记录与高亮仅是本次弹窗的内存状态，不增加持久状态。

OAuth授权仍原Linux loopback owner和已锁SDK拥有PKCE/state/issuer及token。实际`LocalMcpManagementService`已有async `open_browser`注入点；即使interop开启，guest默认`webbrowser.open`也不作为Windows系统浏览器成功打开的证明。仍经`DesktopApplication`→`KernelHostCore.production`/构造→management贯穿注入唯一OAuth-only host callback，明确由可信Electron打开并回实际结果。

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
| B0 闸门封闭 | G0-W-D/C/P/F/N各项原生证据，记录WSL/已有发行版/SDK版本及共享环境和interop窄缺口 | A验收完成，G0-W全过；新语义先修规格，不用探针作生产依赖 |
| B1 接入/资源 | 已有发行版/默认用户发现与明确选择、用户私有Linux资源/有效home、win host数据分离；setup图形进度、保数据维护 | 非root/ext4实际符合；保留interop/自动挂载和其他用户工作；目标变动/失败/离线/磁盘满不误删或改环境 |
| B2 载体/PG/网络 | WslHostOperations + Linux finite helper，接原runtime/PG顺序；TLS/session/HTTP origin同片接齐 | stream EOF/取消/真实PG实例/unknown不回归；同一kernel endpoint每连接身份、旧sessionretire成立 |
| B3 工作区/路径/传输 | 用户级Linux picker/API、新建/选择/最近目录、固定域路径转换/reveal、Windows文件导入/下载 | 全程图形创建到同一Linux工作区；不混C盘/他发行版；preview/HTML隔离、最终文件与失败结算实测 |
| B4 Windows壳/安装 | frontend host类型/窗口/tray/IME/OAuth、per-user NSIS/无强杀、CLI状态、组合资源cache/release矩阵 | 图标启动→Linux服务ready→工作→Quit/重开；卸载保data；实际资源版本/许可证/架构一致 |
| B5 激活 | Windows普通用户实装、故障/长程/真实provider、受影响Mac/Linux回归、文档/支持声明 | 第13节全满足才宣称Windows支持 |

资源recipe/图标/静态UI可提前研究，生产B仍受A/G0-W约束。B1/B2与B3可在明确接口上分工，最终B3依赖真实Linux环境与受认证网络，B4安装资格依赖真实闭环。每片保留当前已支持POSIX单一路径，不引入新旧SDK/API兼容层；CI尽早接，最后只收口完整矩阵。

## 12. 成本、取舍与维护预算

原专用rootfs路线的50–95人日及含余量63–129人日仅为历史估算，详见research的wsl-first记录；本次改为已有发行版接入且保留互操作后，不继续把该数字作为当前预算。发行版import/rootfs再分发与整套OS维护退出产品范围，目标接纳、共享环境不干扰和interop跨域生命周期的实际成本待G0证据后重新估计，不直接扣减旧数字或拼接各专题报价。

当前工作包为：G0-W、既有A资格补证、已有目标接入/私有资源安装、runtime/PG/TLS、Linux图形工作区/文件传输、Windows壳/安装及真实provider/长程/受影响POSIX回归。G0-W尤其先验证共享环境中的Windows后代观测/取消缺口；若现依赖不能满足原结算要求，明确最小机制与成本后修订，不把未知上游工作藏入reserve。相同验证只计一次，硬件/签名/下载/重启与测试机排队另计日历成本。

仍不扩大为原生Win32 Python内核、任意Linux发行版、多目标同时运行或C盘直接workspace。interop允许日常Windows程序互操作，模型任务的支持范围与回收机制须通过§5.2/§4的G0门；不因Windows程序能启动便宣称所有跨域任务已受控。

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

复用terminal/subagent/artifact等dogfood前先读runner当前API/连接/DB前置。artifact runner的`--phase baseline`会删tool，不能作为本次前后基线；两份代码均使用完整生产tool面的candidate，仅输出目录分before/after。部分DB helper只接受loopback根dbname=pulsara，不能直接套WSL managed Unix socket；修测试harness到已授权本地隔离方式或报告缺项，不为runner开PG TCP/改settings。硬编码输出须原样归档避免覆盖。

**A完成条件**为11.1及13.1实际适用验收/review全部通过。**Windows激活条件**为A完成、G0-W全过（含共享环境不干扰及interop生命周期闭合）、B1–B5代码/旧路径hard cut完成、实际安装/图形工作区/路径/部署/生命周期/TLS/真实provider长程与受影响POSIX回归通过、无权限/数据/副作用未解P0/P1、原oracle与prefix不变、安装界面/文档如实声明已有WSL接入、实际任务回收范围与数据保留合同。任何缺项均不可宣称Windows支持完成；文档修订不冒充执行证据。

## 14. 参考依据、采用与撤出

本轮继续参考当前生产、AGENTS与用户指定本地仓库；上一轮源码版本为deepseek-harness `687f282e8625`、codex `89461ea68338`、ZCode `3c5b22764937`、grok-build `2bdd1d6a6369`、opencode `37e3912d8ea5`。它们的Windows Job/ConPTY/PowerShell实现仅保留为已放弃原生路线的研究背景，不成为WSL生产依赖。ZCode/opencode的NSIS安装资源/更新退出、grok文件锁生命周期观察仍可作review反例，不复制其强杀/fallback/新manifest或恢复机制。

主要修改导航为`desktop/src/{main,runtime,postgres,preload,cli-install,browser-owner,browser-policy}.ts`、`frontend/lib/{desktop-host,runtime-adapter,prompt-draft,file-drop,file-preview}.ts`、`desktop_runtime.py`、`desktop_app/{http_server,application,file_import,file_preview,file_preview_http,path_completion}.py`、`workspace_identity.py`及现构建/安装/release owners。此导航最初写于B实施前；A的Node资源/宿主/PG接缝见第16节，后续Windows/WSL正式实现及候选验收以第24节起的记录和当前源码为准，不能继续将这些已实现模块称为仅有计划。

官方依据在相应章节近旁链接；文档证明平台能力/依赖扩展点，不证明Pulsara集成已通过。下载/布局/API须检查实际锁定版本实现，禁止拿别的agent产品已发Windows包推断本产品PG、物理join、文件授权或prefix已成立。

## 15. 历史工作区、交叉审阅与冻结记录（用户本次修订前）

本轮使用原三位`gpt-6.1-sol / high` subagents，在`windows_compatibility/research/2026-10-24/{root,terminal,backend,desktop}/wsl-first/`分别研究；root唯一汇总根spec，agent只在个人目录写提案/审阅。此前native/代码组织/宽POSIX-A审阅属于已取代版本，不能批准本次新路线。

前版695行规格的研究快照仅保存`root/wsl-first/previous-native-posix-spec.zh.md`供追溯，不再是当前实施authority；根文件完整替换旧原生目标、G0和强制A，避免追加WSL章节却保留相反前置。研究过程已整理到本目录research/2026-10-24；文本材料随Git保存，生成物/临时目录仍本地忽略。当前spec独立携带实施合同，不依赖探针/报告运行。

已完成流程：root汇总387行唯一全文候选→三位各自通读全文并交叉核对其余提案→desktop指出OAuth系统浏览器实际调用接缝D1→root核源码、补真实注入链并闭合其他精确化→三位对394行最终语义候选全文复核，均明确“可冻结，无剩余阻断”→root逐份读审阅并独立认可→仅更新文首/本节冻结元数据。没有用沉默、agent结束或历史版本批准推断本次通过。

| 审阅者 | 个人提案/全文交叉审阅（相对工作区根） | 最终结论 |
| --- | --- | --- |
| terminal / gpt-6.1-sol high | `terminal/wsl-first/proposal.md`、`review-round1.md`、`review-round2.md` | 可冻结；保留Linux执行、PG握手/取消、stopped/exit/锁与carrier区别、长程边界闭合 |
| backend / gpt-6.1-sol high | `backend/wsl-first/proposal.md`、`review-round1.md`、`review-round2.md` | 可冻结；TS PG唯一owner、用户级picker/admission、文件源/路径/preview、OAuth与取消边界闭合 |
| desktop / gpt-6.1-sol high | `desktop/wsl-first/proposal.md`、`review-round1.md`、`review-round2.md` | 可冻结；D1关闭；部署保数据、File来源、TLS实际Session/WebContents退役、安装/构建/预算闭合 |
| root | `root/wsl-first/research-notes.md`、`validation.json`、`freeze-record.md` | 独立同意冻结；完整取代旧native/宽A前置，当前文档/位置检查通过 |

**路线冻结时事实：WSL2主路线及收窄Phase A实施规格已冻结；当时A/B实现与回归未执行。后续A实施事实仅以第16节为准，前轮文档批准不替代本轮代码/测试审阅；G0-W及B/Windows激活仍未完成。**


## 16. Phase A实施与验收记录（2026-10-24）

本轮由root与原三位`gpt-6.1-sol / high` subagents在各自`windows_compatibility/research/2026-10-24/{root,terminal,backend,desktop}/phase-a-implementation/`工作；仅root汇总本规格。用户明确授权将当前无法获得的Ubuntu原生/图形资格保留为**待验收**，不删除矩阵、不计作通过，也不进入B。

### 16.1 实际hard cut与所有权

- `DesktopResourcePaths`只返回当前随包路径；main一次装配，runtime wait/start、CLI及PG消费相同值。不读取fs/settings/manifest，不预建WSL字段。
- `HostOperations`仅同步`resolveHome`、三pipe `spawn`、有限`execFile`、`prepareRuntimeDirectory`、`appendRuntimeLog`。`posixHostOperations`复用Node；`PythonRuntime`保留唯一child/preparing/start帧/ready/inspect/stop/EOF状态、原环境及错误通道。
- `PostgresOperations`仅现有目录/socket/log路径事实、布局/version/实例/权限/realpath/uid观察、限定`initdb`/`pg_ctl`执行与删除。`postgres-posix.ts`直接复用Node fs/execFile，保留原`LC_ALL=C`和4MiB有限exec输出边界；`BundledPostgres`唯一拥有initialized/startedHere、104字节socket协议边界、DSN、version拒绝、Numeric status 3、失败start确认、reset及stop顺序。
- `readInstance('resolved')`仅供startup确认，`readInstance('raw')`供reset精确比较。审阅发现提取时对reset额外执行`path.resolve`会在调用者cwd已删除时抢先抛ENOENT；现仅resolved分支规范化，并以独立真实Node child覆盖原中文拒绝及数据保留。Mac测试临时根先realpath，不能因`/tmp`自身链接提前命中原symlink保护。
- `main.ts`显式注入两组POSIX操作；原boot/reset/requestQuit与同步home错误时点保留。`cliLauncherScript`改为消费已解析Python路径，原marker、shell quoting、argv、exit、注册冲突/PATH与Linux packageTarget规则保留。所有调用者和测试同片更新，无旧构造器/双路径/平台fallback。
- Python、terminal/filesystem、provider prefix、schema/oracle、frontend、构建recipe、WSL helper/部署/TLS/OAuth桥均未修改；不存在新增持久状态、事件或恢复机制。新TS文件已被现tsconfig与`dist/**`打包规则覆盖，无新的native资源输入需要改mise失效规则。

### 16.2 基线与当前验证

改前Git为`60e8f21d37ca2c650500ae14438630006d3e62fb`，仅根规格未跟踪，无生产代码dirty；在同一checkout先完成改前构建/测试再修改，未冒用另一editable版本。macOS 26.2/Darwin25.2 arm64，Node22.23.3、uv0.13.4、Python3.12.15（仓库`.venv`）。

| 验证 | 改前 | 当前结果/边界 |
| --- | --- | --- |
| `npm --prefix desktop test` / tsc | 31/31 | 46/46，0skip；覆盖main/runtime/PG/CLI与真实Node POSIX边界 |
| 三份Python桌面契约 `test_desktop_runtime` / `launch_directory` / `release` | 23通过 | 23通过；未修改Python业务 |
| `mise run --jobs 1 desktop:verify` | 改前独立资源smoke通过 | 通过；fresh frontend/application wheel、Node测试、relocation、资源/版本检查。未变SDK按原cache复用 |
| checkout外已安装wheel smoke | — | 随包site-packages来源，真实launcher/assets/rg/Skill/Plugin/HTTP startup/shutdown通过；Mac证据不算Ubuntu资格 |
| `test:startup` / `test:browser` | startup通过 | startup通过；browser 11/11通过 |
| 真实POSIX lifecycle | 资源smoke通过 | 当前dist配随包PG/Python；init/reuse、pg_search/vector、reset、kernel ready/inspect/normal quit/锁释放/重开通过；仅自建canonical临时home |
| `ruff check` / `git diff --check` | — | 通过 |
| native Ubuntu x64/arm64、真实Ubuntu GUI/安装 | 未具备 | **待验收**；用户已明确同意。未以Mac/mock/container冒充 |

首轮新增main fixture漏mock原有`setAsDefaultProtocolClient`，修正fixture后45/45通过；之后新增删除cwd回归的Mac `/tmp`前置已纠正，最终46/46通过，没有削断言、skip/xfail或生产兼容分支。一次并发Python测试遇到mise bootstrap暂时移除editable项目，待mise project安装完毕后重跑23通过；属于验证调度，不修改import规则。

macOS独立包/GUI及三位并行真实实验已完成，实际结果见16.3；最终全文审阅记录见16.4。主要原始证据为root工作区`before/`、`verify-final.log`、`python-final.log`、`wheel-outside-checkout.log`、`startup-final.log`、`browser-final.log`、`real-posix-lifecycle.{cjs,log}`及各agent个人记录；这些证据已从`output/`迁入本目录`research/2026-10-24/`，文字材料随Git保存，仍不被生产import或作为运行时依赖。


### 16.3 独立包/GUI与并行真实实验

`npm --prefix desktop run package:dir -- --publish never --config.directories.output=.../root/phase-a-implementation/package`成功构建当前Mac arm64应用包，未覆盖`/Applications/Pulsara.app`、未发布。其APFS clone在独立目录通过LaunchServices/CUA实际启动；仅测试bundle ID、PULSARA_HOME/HOME及URL注册元数据作隔离、重签测试包，应用ASAR/业务不变。desktop独立以现asar SDK对两个包中九个生产模块逐完整Buffer比对当前dist，18/18一致，不生成hash；两份包实际Python各复制到中文/空格/单引号资源路径，六次version/help/源码来源检查exit0。

Mac GUI实测：新home工作台ready/数据正常→正常Quit（核实本次Electron/Python/PG均退出）→重开；暂停**已验证确切测试PID**的kernel制造慢退，原inspect unknown确认后保持stopping遮罩且PG仍运行，恢复该child后按原顺序退出；停止的自有cluster临时PG_VERSION=99时显示原完整版本错误/重试不会清库，数据保留、不起PG、pre-start child按EOF退出；退出后恢复自己的17版本，重开ready，再正常Quit和原`--wait-owner`通过。只有自建临时home被清理，用户原应用/kernel持续运行，保存settings/用户DB未修改。不是旧release、mock窗口或容器GUI证据。

测试隔离修正也如实保留：最初改测试包显示名导致Electron找不到helper、CUA报超时；恢复原命名后解决，没有要求用户授权或改生产。另发现Mac Electron `os.homedir()`并未被临时HOME隔离，原best-effort CLI注册短暂把用户launcher更新为测试包路径；root恢复到`/Applications/Pulsara.app`的常规脚本，余下验收用临时symlink冲突保护，最终已移除保护并验证为可执行普通文件。没有改前完整launcher快照，不能声称逐字恢复未知旧内容；已恢复现有生产应用的正确入口。下次GUI harness必须显式保护CLI。详见root `macos-gui-review.md`与`gui-cleanup.log`；测试副作用不混写成产品代码变更。

三位同时补做独立实际资源实验，不争用dist、不重建、不共享PG/home：

| 实验者 | 实际边界 | 结果 |
| --- | --- | --- |
| terminal | 当前dist+真实随包Python，preparePostgres受控失败，不发start帧 | 原Error对象保留；EOF后exit0/close、无running/PID，原wait-owner通过，临时home清理 |
| backend | 当前dist+真实PG17.9，错socket/PID目录拼写/版本 | 拒绝复用/reset时同一PG仍可查询、未误停；`/.`仅startup resolved接受/reset raw拒绝；停库后版本16拒绝且control/sentinel保留；恢复后确切status3再清理 |
| desktop | 两包真实ASAR与私有Python/CLI | 完整字节来源一致；中文/空格/引号路径及真实版本/help/import来源通过；未调用registerCli |

三项均不调用provider、不读取保存凭据，不把mock/容器/本机结果记作Windows或native Ubuntu资格。原始证据在各个人目录`runtime-eof-real.*`、`real-pg-failures.*`、`package-cli-evidence.json`及各final review。

### 16.4 最终状态与交叉审阅

当前源码、本机证据及本节记录已由root和三位交叉终审，四方均明确同意冻结，无剩余代码/本机证据阻断；本次仅冻结实际通过的Phase A实现/本机证据，不取消第13.1节的Ubuntu待验收项。root的最终收口记录保存于`root/phase-a-implementation/final-review.md`；三位个人记录为terminal `evidence-review-final.md`、backend `final-review.md`、desktop `final-evidence-review.md`及后续本候选全文批准。

**当前事实：Phase A代码实施完成，本机Mac arm64回归、真实资源/PG/内核与独立应用包GUI验证通过；Ubuntu x64/arm64原生与真实Ubuntu GUI/安装相关资格按用户要求待验收，因此不宣称A全平台验收完成。Phase B、G0-W、WSL部署、Windows支持/激活/发布均未实施。**


| 最终审阅者 | 实际批准记录 | 本次结论 |
| --- | --- | --- |
| terminal / gpt-6.1-sol high | `terminal/phase-a-implementation/freeze-review.md` | 同意冻结A实现与Mac证据；EOF/控制与PG/GUI关闭边界无剩余阻断 |
| backend / gpt-6.1-sol high | `backend/phase-a-implementation/freeze-review.md` | 同意冻结；真实PG失败保数据/实例及最终清理证据闭合 |
| desktop / gpt-6.1-sol high | `desktop/phase-a-implementation/freeze-review.md` | 同意冻结；包来源/CLI/GUI和实际清理核对无阻断 |
| root | `root/phase-a-implementation/final-review.md` | 独立同意；Ubuntu待验收，B未实施 |

root逐份读取三位最终批准，之后只更新本次冻结/批准元数据；未因agent沉默或前轮45条结果推断通过。A冻结时尚未提交/推送或发布；现有用户应用持续运行，测试CLI临时保护已撤回。

Windows接手的环境安装、双仓库落点及G0-W逐项操作见[PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md](PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md)。该清单仅提供准备与实验步骤，不代表Windows实测，也不授权进入Phase B；此前未提交/推送是本轮A冻结时事实，后续用户已另行授权提交和推送。


2026-10-24 目录整理：两篇Windows活动文档及四方研究/审阅/验证材料统一移入`windows_compatibility/`，以[本目录索引](README.md)导航。原始过程文件保留当时命令与绝对/相对路径以供追溯，旧根目录/`output/windows-compat-20261024`路径不再是当前落点；生成应用包及pytest临时目录仅本地保留。此整理不改变冻结语义或验收状态。

## 17. Windows现有Ubuntu接入的首轮G0证据（2026-10-24）

用户授权在已有Ubuntu26.04.1/plumwsl中准备开发环境，并用三位gpt-6.1-sol/high并行实验，明确不进入B。受测源码da397a2b，两份独立clone一致；Windows工具/纯测5/5、Linux固定工具/完整资源构建、Node46/46、资源搬移PG/Python/Chromium、聚焦Python与缓存39、terminal/MCP94、hooks45通过。真实PG/检索及有限helper部分子项、隔离TLS/Session12项、BrowserOwner11项行为和Explorer图形目录子项有实际证据。

**完整G0-W未通过，B未进入。** 普通非detached Windows后代在Linux进程组取消、Linux root及carrier结束后仍运行的反例已重复，现Linux PGID不能作为跨域结算证明；G0-W-C该项不通过。PG helper自身死亡在真实pg_ctl各阶段的结算仍未验证，其他接纳/GUI/网络/OAuth/休眠等矩阵仍待补；不以Linux源码测试或fixture代替生产集成，不用这些WSL结果替代原生Ubuntu资格。

范围、精确失败输入、原始证据、测试profile清理拒绝与保留产物见[首轮报告](research/2026-10-24/root/g0-w-initial/report.zh.md)。本节为实际观察记录，不重新宣称修订前冻结批准覆盖新路线，也不授权提前实现生产WSL/安装器。

## 18. 第二轮取消与helper死亡证据（2026-10-24启动，跨日至25收尾）

用户再次授权三位gpt-6.1-sol/high并行研究，受测源码fb2c57da，两份clone一致，interop保持开启，未改生产代码或进入B。详细报告及各方交叉审阅见[第二轮报告](research/2026-10-24/root/g0-w-round2/report.zh.md)。

- Windows无breakaway Job在wsl.exe开始运行前绑定，实际覆盖interop普通Windows PE及后代。取消、实际Job owner/Node/Electron宿主崩溃、父先退且后代不持pipe、A/B隔离均有Windows句柄/Job事实；不是只看LinuxPGID或carrier结束。
- 专属anchor可给不同Linux父链提供每任务interop路由，但裸WSL_INTEROP路径失效会回退到其他入口，已有真实反例。私有0700 ext4 socket+标准双向byte转发+上游O_PATH固定inode候选，在所测anchor/转发进程死亡后拒绝新PE且未观察回退；不复制WSL协议，不关闭互操作，不把Python ctypes研究探针当生产依赖。
- 最后独立补测中，anchor正常EOF后无pipe普通Windows子仍运行并自然finished；保持Job handle至ActiveProcesses0、TotalTerminatedProcesses0且全成员handle signaled后才关闭，未调用TerminateJob。此项补上正常回收机制正控，但尚未组合私有relay封准入/退休和原Linux owner。
- 此候选尚未完成首次绑定/半请求/失联/反复spawn取消竞态、完整正常join与socket退休及真实terminal/MCP/hook owner集成。因此仅有机制子项正证，整个G0-C不通过；正常完成不得借TerminateJob冒充success。
- PG在实际ext4上完成7项主矩阵与3项补充。真实pg_ctl fork子已setsid或postgres已exec时，helper死亡释放锁；后续helper持同锁status3之后，旧启动恢复仍会起同一库。当前§3.3的CLOEXEC单helper持锁候选明确未通过；直接继承fd又让常驻PG占住kernel.lock，阻断正常kernel/维护。替代准入方案未批准，禁止由瞬时status继续reset。
- 存储审计确认本机/tmp为tmpfs。首轮及本轮早期/tmp PG记录只保留其行为含义；本轮最终矩阵已在/home/plumwsl的ext4重跑，不改写旧日志，也不代替原生Ubuntu资格。

完整G0、原生Ubuntu/GUI资格、Windows激活仍待验收。活动规范记录以上新证据和不合格候选，不恢复旧原生Windows内核方案、不自动放行Phase B。

## 19. 第三轮PG替代准入与跨域组合证据（2026-10-25）

受测源码671f8fd2，Windows/Linux clone一致；三位gpt-6.1-sol/high并行实验，interop保持，生产未改，B未进入。[第三轮总报告](research/2026-10-25/root/g0-w-round3/report.zh.md)给出原始证据索引和交叉审阅。

- PG采用§3.3的单一两锁候选。真实start链继承独立PG EX，helper死亡后PG锁仍busy，status3不放新start/reset；正常PG运行不挡原kernel锁。EX7项主矩阵及最终3项复验支持此边界。有限stop2项/initdb2项证明其后代继续占维护锁；同guard真实拒删、准确stop后双锁内删除/保留inode及外部sentinel/socket、重建查询完成。完整TS/JSONL故障矩阵及guard内running→stop→delete分支仍待验，原只读readback未继承kernel的探针限制明确保留。
- SCM_RIGHTS首次绑定四门有实证：pin前、pin后send前、send后bind前取消和live正控。请求前、部分转发、全转发后的组合取消三门通过；Windows Job空但Linux调用者未退的原反例保留，后续必须合并Linux PGID取消/join。半/全请求失联仍unknown，物理回收不证明操作没发生。
- 真实terminal baseline复现Windows尾部仍活时提前完成/释放槽位。内存候选14项通过，覆盖PIPE/PTY正常/取消/输入、原8槽及同registry同owner A/B；原测试47项通过。候选等待自然结束，正常路径未用TerminateJob。它不是生产接口，首次绑定/封准入竞态尚未与此owner全部合成，MCP/hooks/环境probe/启动故障/GUI宿主全链等仍待验。

新增PG锁仅承担无法由原kernel锁同时承担的独立OS物理寿命，不增加durable事件/registry/重放/第二startedHere。探针和文件RPC不得作为生产依赖。以上仅关闭所列研究子项，完整G0-C/P及其他门、原生Ubuntu资格与Windows激活仍未获验收。

## 20. 第四轮真实owner合成与结果分类（2026-10-25）

本轮复用原三位gpt-6.1-sol/high agent，两clone受测基线adb3e1de，保持interop、未改生产、未进入B。完整记录见[第四轮总报告](research/2026-10-25/root/g0-w-round4/report.zh.md)。

- terminal在原foreground decision/launch reservation内完成SCM_RIGHTS绑定和Windows诊断PE精确Job核验，再准入真实任务。选定pin/bind/upstream/forward取消、reader/watcher Thread.start rollback、PIPE/PTY输入、8槽及同owner A/B有同链证据。正常路径等待自然结束，socket在原owner物理join后退休。
- 真实watcher故障暴露创建后尚未首次运行的连接任务不会执行finally；后续normal故障又确认done callback仍排队时不能据集合非空推定任务未结束。研究seal owner直接结算自己已拥有的任务/streams后才ACK，保留零集断言及全部原失败，不靠sleep或盲清集合。
- 真实wait/poll反例说明仅延迟physical_completion仍会提前冻结success。研究变体将同state跨域scope纳入原live谓词，最后正常尾部poll为running，尾部取消真实变killed并join/释放槽位。矩阵按原成功、冷任务修后复验及最后done/status修后验证分阶段，不宣称最终源码完整矩阵重跑。
- PG真实BundledPostgres通过PostgresOperations调用Windows Node→WSL双锁helper，正常start/reuse/reset running→stop→delete/重建查询通过。选定ready前后EOF/取消、发帧后EOF、丢result/未换行结果、helper/carrier/Node死亡有实际锁/进程/owner字段事实。
- 原PG owner真实负例：本次零operation提交，另一有限调用已起同库后，catch按当前status/readInstance仍设startedHere=true；这不能代表本次启动归属。output机械生成两行guard，对not-submitted/unknown保原错误而不由此次catch更新归属，对已结算PG非零保原readback。两项真实对照及原/研究变体各8测试通过，未修改生产，也未把main Quit条件推导当GUI实测。

本轮没有增加durable receipt、第二startedHere、产品总寿命cap或关闭interop。剩余MCP/hooks/environment、更多启动取消/relay失联、真实Electron退出/休眠、PG全操作framing/失联及其他G0门继续待验；原生Ubuntu资格仍独立。生产尚有已记录缺口，不能激活Windows或放行Phase B。

## 21. 第五、六轮剩余G0机制与GUI实证（2026-10-25）

两clone受测基线b741dd92，复用三位gpt-6.1-sol/high；用户确认既有Ubuntu/plumwsl、interop和Hiddify TUN保持。原生Ubuntu延期，生产代码不变，B未实施。以下是已经归档的阶段证据，不以其数量代替整门审阅。

- [MCP](research/2026-10-25/terminal/mcp-round5/report.zh.md)：6个真实SDK/transport/executor场景及21项原测试；原close会在无pipe Windows后代仍活时返回，研究scope等自然结束/精确join。tool cancel与session close保持不同边界；timeout/服务进程死亡的原UNKNOWN和fence语义保留；guard拒绝不spawn。
- [hooks/environment](research/2026-10-25/terminal/hooks-environment-round5/report.zh.md)：12个选定场景，含3个原跨域提前完成反例；真实取消/timeout/close等待同scope，environment不能在Windows尾部仍活时缓存成功；55项原测试通过，精确清理有记录。
- [PG剩余边界](research/2026-10-25/backend/pg-boundaries-round5/report.zh.md)：29个物理case；ext4中文路径clean-v0/deep验证、真实BM25中英代码/snippet及vector、搬移重开、精确reset；有限framing与UTF8、initdb/stop/remove前中后失联、方法间新start、真实PG非零/readback均有证据。未声称全部故障排列或最终源码整套重跑。
- [真实kernel/main控制链](research/2026-10-25/terminal/kernel-control-round6/report.zh.md)：Windows Node/Electron壳、原Runtime/BundledPostgres、AST抽取原requestQuit、实际bundled DesktopApplication及PG。22次运行中18项完成观测、4项探针失败保留；12项原runtime测试通过。正常EOF关闭门证明kernel/锁结算后才PGstop；原child已清空后kernel/carrier异常仍停PG并请求exit0为真实反例，两行output候选在三个已测窗口拒绝。app.exit/UI用观察stub，不冒称正式GUI/安装包。宿主强退下无aclose的unclean事实仍需与§9.3合同收敛，不能以外层研究清理代产品成功。
- [目标/离线payload](research/2026-10-25/desktop/discovery-payload-round6/report.zh.md)：真实默认发现、显式固定目标、缺发行版/用户及root拒绝；完整资源经字节pipe传入非root私有无网络namespace。缺制品、坏摘要、真实写点ENOSPC、解包中断/部分stage重开拒绝均保模拟数据；发布后Python/CLI/PG/rg在新位置实际执行。默认变化仅输入阴性，ENOSPC不是整块ext4真满，未伪造WSL未安装/需重启环境。
- [图形目录/文件与HTTP](research/2026-10-25/root/g0-w-round5/report.zh.md)：无active session的Windows图形浏览/新建/选择/取消；用户真实选择及拖放285字节文件，三个Linux副本逐字节相同、0400/0500。17项HTTP目录/导入、4项真实替换竞态、2项private mount namespace拒绝及最终9项路径检查；两个原目录竞态分别收敛为已写unknown与接纳前同inode重核。生产HTTP/import/preview owners的13项集成与17项原测试通过，session依赖为明确fixture；HTML/null-Origin、图片解码、可见PDF及真实保存/失败取消均有证据。

PDF正证需要Electron内置固定extension及其自身frame发起的chrome://resources本地资源，不能因此放宽backend精确origin/完整cert、任意extension/file/外网。下载失败的updated interrupted仍可恢复；明确取消须离开当前回调再执行并等待done cancelled，不能以取消冒充下载成功。研究启动器stdout管道提前关闭引发EPIPE、中文filename header初错、PDF白屏和native同步取消日志均原样保留；最终host exit0/stderr空，未改生产以求绿。

本节归档时剩余独立发行版故障、宿主loss收尾、在途网络退休/证书有效期、OAuth和真实睡眠恢复另行补证，当时未宣布完整G0通过；后续最终结论见§22。每轮已经记录并精确清理自己的进程/home/大资源，未向共享Ubuntu执行终止、未改用户网络配置或运行真实provider。

## 22. Windows WSL2本机G0最终验收（2026-10-25）

**本机所选矩阵的D/C/P/F/N机制可行性验收通过。** [最终报告](research/2026-10-25/root/g0-w-final/report.zh.md)给出逐门索引、原失败和证据限度；复用三位gpt-6.1-sol/high完成各自实验及交叉审阅。受测生产基线b741dd92，仍包含main/3ac7fc2b；本轮只有研究记录与规格变化，没有生产WSL接入，也没有进入B。

资格范围是Windows11 25H2 x64普通用户、WSL2.7.11.0/kernel6.18.33.2、已有Ubuntu26.04.1/plumwsl/ext4、Electron44.9.0及固定资源、nat网络与用户Hiddify TUN开启。Ubuntu版本数字不是新准入门槛；此实测不泛化为所有Windows/WSL/网络组合。用户延期的原生Ubuntu x64/arm64/GUI未记通过。

- D/F：目标发现/显式重开、非root完整离线payload及失败保数据；图形目录、Windows选择/拖放、路径竞态与挂载拒绝、大小写/NFC/NFD独立identity、HTML/image/可见PDF与真实native SaveDialog取消/拒绝覆盖/确认覆盖。默认变化仅输入阴性，ENOSPC为真实写点注入，不伪造WSL未安装/需重启或整盘真满。
- C/P：R4/R5真实各scope owner与原上层await闭包，加R6实际bundled kernel/main关闭门、异常退出检查、SIGHUP默认与最小忽略策略、可见窗口close及睡眠，支撑机制可组合性；不是声称同一个run同时运行全部工作类型。原信号日志不证明sender PID，忽略策略须在guest可处理start前安装。
- [真实dirty恢复](research/2026-10-25/terminal/distro-recovery-round7/report.zh.md)补上终审具体缺项：原HTTP提交canonical会话→仅终止明确可丢弃distro→保持PGDATA/pidfile/socket原样→新显式attempt原wait-owner/TS start/initialize=False→PG真实redo/ready，同system_identifier/PGDATA inode/原canonical SQL与HTTP记录可读→正常Quit。旧attempt仍unknown，不重发工具或原写入，不以手工删pidfile/reset/initdb冒充恢复。
- [网络与睡眠](research/2026-10-25/desktop/network-round7/report.zh.md)：自然cert跨validTo时同PID/SSE及新TLS Session可用；在途上传stage、下载、SSE与pending用原SDK对象结算，旧Session拒新请求。实际睡眠事件间隔88.493秒；同kernel/PG/锁/SQL继续，SSE一次只读自动重连，未重启guest或重放工具。
- [OAuth](research/2026-10-25/backend/oauth-round6/report.zh.md)：原SDK拥有PKCE/state/issuer/token，Windows仅OAuth-only系统打开；8项mock与用户现场真实Notion授权/token200、协商2026-07-28/list_tools65成功，无业务tools调用。本地grant/listener/home清理不代表撤销Notion远端授权/DCR。额外[旧callback端口rebind](research/2026-10-25/backend/oauth-round6/rebind/report.zh.md)证明错服务成功页不能改变原cancelled/token0/no grant；最初整N结论因漏此must-have被撤回，补验和复审后才恢复。

原反例、修正前源码与有限探针失败保留，不弱化原测试、不把最终源码未整套重跑写成已重跑。官方制品摘要/签名只用于真实下载边界；无代码/证据hash、durable receipt、恢复队列、第二lifecycle owner或新产品寿命cap。各研究进程/PG/home/大资源及临时发行版精确清理；可选Electron缓存自动审批拒删，保留ignored output，不绕过、不归档。R7注销后共享Ubuntu实际曾观测Stopped，保留原要求Running的探针断言失败，不猜原因；命令审计及注册/default核对证明未主动终止它，后续Git同步显式重开与该历史观察分别记录。

**G0通过只表示所选机制可用于后续实施决策。** 生产跨域scope、PG typed helper、Runtime退出/信号接缝、目录与TLS/OAuth/Session装配仍未实施；正式setup/NSIS、tray/IME/DPI、更新卸载、真实provider长程和受影响平台回归仍按§11–13验收。用户本轮明确不进入B，故不启动这些生产改动或宣称Windows支持已完成。

## 23. G0之后的独立问题修正（2026-10-25）

用户在§22验收之后授权澄清README并单独修复三类问题，同时明确暂不进入Phase B。§16和§17–22的源码状态、失败及通过记录保留为各自当时事实，不随新修正改写为通过。新的共享代码修改不能沿用旧Mac应用包GUI证据作为自身回归资格。

- Runtime：`child`清空后跳过退出检查是macOS/Linux共用owner中的逻辑缺陷；本轮在该owner保留并检查现有attempt结果，不增加WSL adapter或新的持久状态。G0复现来自Windows研究宿主，不能据此宣称已在macOS原生复现。
- PG：本轮在现有`PostgresOperations`及POSIX实现明确未提交、结果未知和已结算失败的错误分类，原`BundledPostgres`只对已结算的启动失败进行原实例读回；未提交/unknown不得凭当前同路径实例认领本次启动，亦不得凭携带的数值code3准入启动或删除。保留原错误细节、已有归属及真实已结算非零后的读回，不另建PG状态owner。WSL有限helper、双锁和跨域装配仍属B，不由本轮接口修正宣称已实现。
- terminal及其他执行owner：Linux PGID不能证明Windows后代已退出，这是Windows接入缺口；完整修复需要§5的Job/relay范围及各原owner的真实集成。仅改变Linux存活判断、加入未使用scope接口或导入output探针均不算修复；是否单独提前实施这一部分须与用户的“不进入B”约束明确协调。

本轮已正式修正Runtime与PG共享owner，WSL隔离源码编译及全部desktop Node测试60/60通过，Windows定向真实子进程测试通过，交叉审阅无阻断。结果、原失败与适用范围见[独立修正报告](research/2026-10-25/root/shared-owner-fixes/report.zh.md)。完整Windows跨域回收仍未实施，等待上述范围取舍；不改变interop、Hiddify TUN、共享Ubuntu配置或Windows未激活状态。本次没有原生macOS/应用包GUI重验，不能把WSL回归记作原生Ubuntu资格。

## 24. Phase B正式实施授权与接缝（2026-10-25，进行中）

用户在§23之后明确授权进入Phase B，先修复两个POSIX启动清理缺陷，再按本规范正式接入；实施期间发现的相关生产缺陷直接修复。继续复用三位gpt-6.1-sol/high并行实施，保持已有Ubuntu/plumwsl、interop及Hiddify TUN。§16–23记录各历史时点，不能将其“未进入B”解释为当前禁令，也不能将当时G0通过视作生产接入通过。

- POSIX原owner先收敛：foreground timer创建/启动失败必须释放本次预约并结算原decision；Popen成功后的stream包装、线程或state构造失败必须关闭本次fd并终止、join实际进程组后才能释放预约。新反例在原实现7/7失败，修正后相关62项通过；测试在WSL隔离ext4源码运行，不记作macOS原生重验。
- Windows宿主接入沿用原PythonRuntime/BundledPostgres/Application与前端API。WSL有限helper承担已明确的双锁、一次命令及物理结果；TS owner继续承担启动归属与Quit顺序。安装器调用已安装主exe的有限`--pulsara-maintenance-check`，使用同一helper的kernel EX→PG EX及真实status观察；busy/unknown/unavailable均拒绝维护，不启动、终止或删除数据库。空布局只允许建立原协调锁，不添加维护receipt或新状态owner。
- Windowsinterop辅助程序使用C# P/Invoke调用原生Job/HANDLE API，构建机编译成独立PE，运行时不执行PowerShell/Add-Type，也不要求Windows Python或pywin32。目标Windows11 25H2自带.NET Framework4.8.1，参见[Microsoft平台说明](https://learn.microsoft.com/en-us/dotnet/framework/install/on-windows-and-server)。依赖只拥有平台机制；Pulsara的任务范围、首次绑定、取消和最终结算仍由§5的原执行owner承担。若helper死亡后须独立保有准确Job/member HANDLE以完成物理join，只允许同scope、有限、私有pipe的monitor；不引入后台daemon、PID推断或产品生命周期注册表。
- 每次WSL内核启动经私有控制pipe交付完整临时TLS证书；Windows创建独立内存Session并绑定精确origin/证书。退出或重启先封住旧请求，再等待旧窗口、BrowserOwner、启动cover、下载与连接实际退休。OAuth仅将原SDK的浏览器打开请求交给Windows系统浏览器并返回打开结果；PKCE/state/token仍由原SDK拥有。
- 正式首启实测中，Linux ready后Windows localhost转发仍有约半秒不可达窗口。导航前由同一BackendSession使用Electron SDK、同一完整证书pin执行只读`HEAD /`；只有明确`ERR_CONNECTION_REFUSED`继续等待，其他传输/证书/HTTP错误原样失败。250ms仅为转发可达性的采样间隔，不是总等待时限；不设重试总次数，不重发业务操作。Quit/Session退休取消原请求与等待并join后再退出，Linux ready不冒充Windows已可达。
- Windows首次安装暴露迁移资源的checkout换行差异：CRLF进入PL/pgSQL函数正文，使新库不符合原LF catalog。仓库迁移SQL/JSON统一为LF输入，现有registry在校验、身份计算和执行前使用同一规范文本；原baseline身份保持，observed catalog仍严格检查。不得修改失败库函数/ledger或放宽verifier来伪装首启成功；本轮未配置的空测试库经精确身份核验后按开发重置合同重建。
- 此实施阶段的工程目录服务属于用户级API，初始根当时为Linux系统用户home下`workspaces`，不是PULSARA_HOME下的目录。该范围及根初始化流程已由2026-10-26的§6.1修订取代：现从用户home开始，GET不隐式创建；单层新建/选择及createSession继续分别重验Linux对象，前端选择器不依赖活动会话。Windows本地File走原上传副本协议，同发行版UNC只做固定目标路径转换；不增加Windows读文件IPC。
- Runtime除§23退出结果修正外，以本attempt是否实际发送start帧划分未使用child与已提交启动；已提交后即使尚未ready，failed/异常退出也不能当作空进程回收。控制消息必须完整LF及合法UTF8，拒绝ready后failed/无效帧却exit0，以及WSL未收到完整stopped的“成功退出”；数字0不能替代原应用收尾事实。PG的`initialized`只表示本次start内已确认成功的initdb，宿主在PG准备完成后读取该事实；既有cluster缺库/角色明确报错，不借后续启动自动补建。相应反例纳入正式回归。

- 真实provider长程验收发现共享ROOT准入会无限重发head已过期的prospective输入。仅同workspace下的head过期使用明确typed冲突；原confirm先裁定同一immutable command的FULL/ABSENT，只有确定尚无ROOT提交时才关闭旧prospective并重新prepare。已有epoch、SYSTEM/tools与已安装provider前缀保持；workspace变化及其他冲突不改为这个重准备路径，ACK unknown仍走原确认，取消后不得重新准入。不新增持久状态、命令身份、重放或重试总次数限制。此修复须由新候选重跑原真实长程场景，不能仅据源码定向测试宣称已安装产品通过。

- macOS原生真实provider回归另暴露ROOT已经accept、尚未安装prospective provider输入时，子任务推进session全局head的窗口。只有actual head严格前进、model binding相同，且将actual四层既有DTO的through-sequence归回prepared值后，经原constructor校验和完整值相等证明其余canonical内容、closure、permission、context与replay完全相同，才退休本次handle、dry borrow、hook reservation和planning，回到同一已accepted ROOT的普通prepare；不再次accept、创建command/turn、复用过期wire测量或重建installed epoch/SYSTEM/tools/messages。其他漂移仍按原prefix conflict失败；任一资源退役失败原样传播，不能冒充已恢复。

本节描述获授权的实现边界与已经执行的定向验证，不宣称B1–B5或发布矩阵全部通过。正式候选的构建、集成、GUI、provider长程及受影响原生平台资格按§11–13逐项记录；用户延期的原生Ubuntu资格不由WSL代替。

用户先批准本地候选由`0.1.0-alpha.6`升级到`0.1.0-alpha.7`，用于安装共享ROOT修复并验证正常更新保留模型配置与会话；随后明确批准`alpha.8`及本轮后续必要本地候选递增，并要求同步修改规范。当前版本权限以§8.3为准：本地版本递增、构建和安装无需逐次确认，Git tag及对外发布仍需另行授权。


- macOS 原生真实 fork-tail 回归发现，仅含尚未 reap 的僵尸进程组会令 Darwin killpg 返回 EPERM；新增 hooks/MCP 物理回收不能据此丢弃成功输出或把组当作空。两条现有 leaf 直接对齐 terminal/environment 已有合同：EPERM 保留可能存活事实，signal 拒绝不替代原执行/close 结果，只有后续 ESRCH 证明原进程组消失；不新增超时、进程快照权威或恢复状态。真实未 reap 子进程的 EPERM→reap→ESRCH、自然 fork-tail 输出、MCP 重复取消与物理 join 负例必须通过。

## 25. Windows共享修改后的原生macOS回归（2026-10-25）

用户授权root与三位gpt-6.1-sol/high并行确认共享修改保持原生Mac行为，并明确真实OS睡眠本机无法协助。基线a57ce3b1加§24本轮窄修，所选macOS26.2 arm64矩阵通过；[四方报告](research/2026-10-25/macos-regression/root/report.zh.md)保留完整改前失败、修后证据、构建来源、清理与适用边界。

- 完整frontend83文件1372测试、backend最终244、desktop105、terminal原167及私有PG4、窄修后定向68、Electron browser13分别通过且无skip；suite有重叠，不累加为独立总数。真实Electron cover的fixture在可见路径先确认原PageVisibility，并独立验hidden直接结算；不增加原2秒或修改生产动画。最终统一verify及应用打包通过。
- 最终wheel从随包site-packages运行真实provider：context8、capacity15、terminal四场景19次调用；MCP、并发/pending、压缩、PIPE/PTY输入和39.29秒自主唤醒通过。逐次SYSTEM/tools精确相同，messages按suffix追加。随机长程没触发post-accept新分支，另用同wheel+真实PG确定性head6→7反例证明仅退休未安装资源并保原accepted turn/installed view。writer expiry与并发controller恢复证明单次旧Core物理close、同session generation1→2；不称硬件睡眠。
- 最终Mac独立app包中三条生产修复文件与当前源码完整bytes相等；实际图形原生新建中文/空格/单引号目录与会话、原生文件reference、⌘N/⌘K、关闭窗口恢复草稿、正常Quit、再次cold reopen保留会话/工作区均通过。完整Quit后未发送草稿按既有内存owner清空，不添加跨重启持久化。独立bundle重签名触发的DesktopFolder原生提示由用户确认，不能记作产品启动超时。
- 测试使用私有PULSARA_HOME、独立PG及UUID DB；saved production settings只读并仅内存注入真实provider。未更新日用/Applications应用。所有测试进程/库/home已结算，原用户CLI恢复相同inode/bytes/mode，原生产main/kernel/PG仍运行。构建生成static散列文件只还原本轮噪声，不提交数百无前端产品变化的生成差异。

本轮只关闭已执行Mac arm64矩阵。真实Mac睡眠/硬件IME、Mac x64及签名分发矩阵仍待验收；原生Ubuntu延期不变。本轮共享窄修后的Windows候选须在Windows重验受影响用例，不继承旧alpha.9安装通过事实；不自动tag、发布或激活B5。root与三位gpt-6.1-sol/high已完成最终交叉审阅，均APPROVE；所选Mac arm64矩阵和本轮窄修合同冻结，见[四方记录](research/2026-10-25/macos-regression/root/freeze-review.zh.md)。

## 26. 共享修复后的 Windows alpha.10 最小重验（2026-10-25）

Windows/WSL 两份仓库同步 `0ea98e63d90043773f42bd9bb9bbb56951fca8b4`，复用三位 gpt-6.1-sol/high，按最小重验清单 R0–R5 全部通过。新基线及各原始证据见[汇总](research/2026-10-25/windows-shared-recheck/root/report.zh.md)。新候选为本地 alpha.10；七个原版本文件同步，另修复 mise Windows 配方的相对 exe 路径，不改变三条共享修复产品逻辑。

新 payload、真实 Windows PE、安装目录 Python -I 与三条文件完整 bytes 已核验；源码 R1 27 项/R2 71 项、真实 PE R3 10 项、Windows R4 54 项加 10 项 main 及 startup 通过。最终随包 context 8 次真实调用与原场景/prefix 断言通过；新 post-accept 窗口由 R1 确定性实证，不能由随机 context 成功推断命中。

alpha.9 正常更新保留原模型、两个会话/5 turns/32 entries、工作区文件与 PG 身份；alpha.10 实际 GUI 中文目录/新会话/终端往返、Ctrl+N/K、隐藏恢复中文草稿、正常 Quit、冷重开保留会话及最终 Quit 通过。没有人工强停正式 PG；冷重开后草稿按原内存合同清空。interop、automount、Hiddify TUN 保持。

此基线只关闭本轮共享差分最小回归。后续完整 Phase B 故障资格继续按原矩阵执行，不激活 B5。用户没有备用 Windows/VM，明确不为测试删除当前 Ubuntu，因此真实“WSL 未安装／需重启”暂时延期；mock/有限注入不替代真实资格。不得为了补验终止、注销或重建用户共享 Ubuntu。原生 Ubuntu 延期、其他硬件及签名分发边界不变。

## 27. Phase B 后半段本机验收收口（2026-10-25）

在第26节新Windows基线上，继续复用三位gpt-6.1-sol/high补齐原矩阵具体缺证，结果见[后半段报告](research/2026-10-25/phase-b-final/root/report.zh.md)。所选本机验收通过，本批没有新增产品逻辑修改，不额外递增版本或重跑不受影响的整套G0。

- 原installed TS owner/client/helper的5项真实PG失联：未提交不追认归属、start结果丢失的PG lifetime锁、stop尾部kernel锁、initdb双FD、已结算readiness非零读回；原错误分类/无自动重放/保数据及精确清理均有证据。
- 5项原scope边界补齐pin/send/bind取消、双观察者丢失unjoined、MCP/hook失联；隔离installed main正常活动Quit先物理join再kernel/carrier0、PGstop、exe0。宿主强退则原EOF/aclose保unknown、runtime非零，故障前精确HANDLE/pidfd证实任务与kernel退出，PG保留/维护拒入；此为§9.3负向门通过，不冒称clean或自动恢复旧任务。
- 生产setup8项、真实BackendSession/HTTP/import组合3项及有限ENOSPC分配点注入通过；原cache fixture7项通过。只证明对应生产owner错误保数据与结算，不称真实整盘填满，也不新增同一main run全部流量同时在途的额外验收门。
- 正式GUI启动一条真实PIPE/Windows等待输入任务，用户实际睡眠最后132.121秒；同main/kernel/PG、writer1→2、原精确HANDLE退出、草稿保留。新消息唯一消费且不重放工具，用户确认正常Quit后PG停/维护0。Explorer真实新建中文目录后，产品picker刷新/选择同Linuxcanonical目录亦通过。

B1–B4核心实现及本轮选定本机证据据此收口；完整B5/Windows支持声明仍不激活。用户无备用Windows/VM，未装WSL/需重启真实环境延期；用户出差仅内屏，完整混合DPI往返延期；原生Ubuntu继续延期，其他跨平台/完整发布制品资格按§13独立列示。interop、automount、Hiddify TUN及日常Ubuntu保持；没有合并main、tag或对外发布。
