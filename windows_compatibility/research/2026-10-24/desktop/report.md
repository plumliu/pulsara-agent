# Windows 桌面、前端与发行兼容成本调研

日期：2026-10-24。研究基线：Pulsara Git `60e8f21d`；本地 OpenCode `37e3912d8e`；ZCode `3c5b2276`。这是只读代码调研和后续实施建议，不声称 Windows 已可运行。仅本目录写入笔记，没有读取实际生产凭据，没有修改生产代码、保存设置、数据库或根规格。

## 1. 结论与可冻结范围

可以冻结为 **Windows 11 25H2 x64 验收基线的原生 Electron + 原生 Python/工具/数据库的单一实施目标**，但必须先执行 native pg_search、ConPTY、Windows 安全文件 owner 三个可行性 Gate，并把桌面私有数据、数据库连接、内核 lifetime lock 的证据纳入对应 Gate。冻结的是未来实施合同，不是这些依赖现已通过。任一 Gate 失败，停止依赖它的实施，修订产品路径与根规格；不能自动改用 WSL、外置数据库或替代检索引擎。

这不是把 electron-builder 加上 `--win` 的工作。跨平台 UI 的基础很好，但整个发行包的底层资源与生命周期目前是 macOS/Ubuntu 产品。建议第一版保留现有手动更新合同，使用 electron-builder NSIS、每用户安装、非提权运行；自动更新单列增量，不作为 Windows 的隐性前提。Windows 11 25H2 x64 是首个验收基线，不作为拒绝所有其他Windows版本的版本硬gate；其他 Windows/架构未获资格，不能静默模拟通过，也不应在资源无关的产品逻辑中添加无理由的拒绝。

## 2. 当前代码的确定事实

| 边界 | 精确代码位置 | 已确认现状及影响 |
| --- | --- | --- |
| 壳资源根 | `desktop/src/main.ts:14-19` | `resourcesPath` 选择合理，但 `resolveHome()` 执行 `python/bin/python3`，会在创建主窗前失败。 |
| Python launcher | `desktop/src/runtime.ts:19-33` | wait-owner 与主 child 都用 Unix 布局；env/cwd、stdio JSON 控制协议本身可复用。必须统一 Windows interpreter layout，不能在各调用处手拼不同路径。 |
| 控制管道与编码 | `src/pulsara_agent/desktop_runtime.py:337-346`；`desktop/src/runtime.ts:29-32,57-61` | backend stdout使用ensure_ascii=False且通过connect_read_pipe读取stdin；Windows pipe/event-loop支持和UTF-8必须Gate实测，不能默认系统代码页正确或以shell包裹规避。 |
| 后端 lifetime lock | `src/pulsara_agent/desktop_runtime.py:31-46` | `fcntl.flock`、`O_NOFOLLOW`、`os.getuid()`、0700 检查；Electron single-instance 不能替代旧 Python owner 收尾锁。 |
| 私有 PostgreSQL | `desktop/src/postgres.ts:19-72` | binary 无 exe、Unix socket、0700、PID 文件 socket 行、trust-local/reject-host、空 listen_addresses。Windows 不能仅把 socket 字符串换 127.0.0.1，必须明确认证与实例身份。 |
| DB reset | `desktop/src/postgres.ts:78-94` | `getuid`、realpath/uid、PID/socket 验证后删除；Windows 需要 ACL/reparse-point 与已核实实例身份，不能删除 uid 检查就当通过。 |
| DB 入口校验 | `src/pulsara_agent/desktop_runtime.py:49-72` | backend 仅允许私有 Unix socket，DSN 仅 host/port/dbname/user；增加 TCP/SCRAM 要改完整 owner 合同，不是壳单边修改。 |
| CLI 诊断 | `src/pulsara_agent/desktop_runtime.py:148-161,291-298` | client AF_UNIX，server asyncio.start_unix_server + chmod；原生 Windows 需要受当前用户隔离的 named-pipe 等受支持 IPC 适配，不能直接开放无认证诊断 TCP。 |
| 关闭结算 | `desktop/src/main.ts:128-148`；`runtime.ts:119-125` | requestQuit→inspect→确认→stdin shutdown→child close→PG stop；无总任务时长上限、无强杀。应完整保留。 |
| Windows 系统结束 | `desktop/src/main.ts:151-189` | 仅 before-quit；无 query-session-end/session-end。Windows 注销/关机会绕过普通退出序列，需要明确 OS 强制中止的语义。 |
| 窗口控制 | `desktop/src/main.ts:163-167`；`frontend/components/desktop-titlebar.tsx:11-14` | non-mac hidden titlebar，无 overlay；renderer controls 仅 Linux，Windows 将无按钮。 |
| 平台契约 | `frontend/lib/desktop-host.ts:13-30` | platform 类型只有 darwin/linux；preload 实际暴露 process.platform。需一次 hard cut 补 win32 与平台显示策略。 |
| 文件选择/剪贴板 | `desktop/src/main.ts:213-237`；`desktop/src/preload.ts:28-38` | Electron dialog、webUtils.getPathForFile、clipboard、shell 已是合适 owner；native path 不需要自写浏览器提权。 |
| 原始文件引用 | `frontend/lib/prompt-draft.ts:181-206,214`；`frontend/lib/file-reference.ts:41-56` | 原始 path 直接写 canonical prompt；parser 已识别盘符和 UNC，也按两种 separator 取 basename；不能据此认定预览全链路已支持。 |
| 文件预览 frontend | `frontend/components/file-reference-chip.tsx:13,29`；`frontend/lib/file-preview.ts:45-65,90-92`；`frontend/components/file-browser.tsx:73-74` | chip 只预览 `/` 开头；URL classifier 拒绝所有反斜杠和非file scheme，`C:`会判 scheme；fileUrl 按 `/` split 会把 C:\\... 编错为 authority；必须区别 native path 与 URL。 |
| 文件预览 backend | `src/pulsara_agent/desktop_app/file_preview.py:101-125,229-243,303,401-425,467-507` | URL 解码拒绝反斜杠，预览使用 dir_fd/no-follow/pread；Windows 需安全文件 owner支撑。改 frontend regex 不构成功能完成。HTML resource 用相对 path 编码也要输出 URL separator。 |
| 用户级目录打开 | `frontend/lib/runtime-adapter.ts:2170-2174` | 后端返回 path 给 host.openPath；WSL 路线尤其受双路径域影响；不得依赖当前 active session。 |
| 新会话工作目录 | `frontend/lib/runtime-adapter.ts:2230-2244` | chooseDirectory 结果原样进 createSession workspace_path；native 可保留，WSL 需要翻译边界。 |
| 拖放 | `frontend/lib/file-drop.ts:5-21`；`workbench-file-drop.ts:73-95` | 已通过 Electron 取得路径，目录 metadata 同步捕获并防会话切换；Win Explorer/OneDrive/UNC 内容真实性与权限仍须实机验收。 |
| 快捷键 | `frontend/app/pulsara-app.tsx:1735-1739`；`session-search-dialog.tsx:173`；`session-sidebar.tsx:277,283` | 行为同时支持 meta/ctrl，但提示写死 ⌘；需 Windows Ctrl legend、IME、不抢浏览器/编辑器输入验收。 |
| 内嵌 browser owner | `desktop/src/browser-owner.ts:41-79,101-132` | WebContentsView/session 分区、禁止权限/下载、保护控制服务端口、快捷键使用 non-mac Control，已有可复用边界；不改成 webview、不暴露 generic IPC。 |
| OAuth return | `desktop/src/oauth-return.ts:1-5`；`main.ts:151-162` | custom URL 仅 focus，不接受 code/grant；second-instance 已无条件 focus，因此已有 warm focus 能力，不能误报为全断。Windows/Linux 参数与冷启动需补资格测试，不能开始从 argv 接受授权代码。 |
| CLI 安装 | `desktop/src/cli-install.ts:3-7,27,33-58,63-82`；`main.ts:98-104` | shell 脚本、X_OK、POSIX executable bit、`.local/bin/pulsara`；PATH delimiter 已用 Node API但缺 PATHEXT/.cmd/.exe 搜索。Windows 推荐复用打包 Python console entry/native shim，不重写命令解析器。 |
| CLI 到 GUI | `src/pulsara_agent/desktop_launcher.py:11-35`；`prepare_resources.py:654-663` | launch manifest 是合适唯一资源定位输入，但当前只输出 mac/linux executable；Popen detached/platform flags 要验证 PowerShell/CMD、关闭终端后的存活和无多余console。 |
| 资源构建 | `desktop/scripts/prepare_resources.py:35-76,106-145,516-527,638-675` | 只允许 Mac arm64/Ubuntu x64/arm64；只识别 ELF/Mach-O，不识别 PE；Python 固定 POSIX layout；必须做 Windows 原生架构、依赖DLL、重定位 smoke。 |
| 包与图标 | `desktop/package.json:12-18,47-114` | 无 win/nsis，只有 icns/png；extraResources 的 bin/pulsara 是 sh；需 ico、EXE元数据、稳定 appId/AUMID、Windows CLI ownership。 |
| release inventory | `desktop/scripts/release_artifacts.py:52-76,123-129` | 强制一DMG二deb并写固定平台标签；加exe必须同步精确产物集合与版本校验，否则CI上传后draft仍拒绝。 |
| CI | `.github/workflows/desktop-package.yml` | 当前 Native Desktop Packages workflow matrix 只mac/Linux，脚本与测试命令写 `.venv/bin/python`，shell步骤为Unix。 |
| 更新当前产品 | `INSTALL.md:43-59`；`DEVELOPMENT.md:9-37` | 手动覆盖、先显式退出、数据默认`.pulsara`、无跨版本DB迁移承诺、CLI不自动改PATH；不应因Windows顺手增加自动更新器/跨版本恢复。 |

根目录 AGENTS 和当前生产是判断依据。已归档的前端/本地web生命周期/附件/导出历史规格仅用于理解保留语义，不因其存在增加新产品承诺。

## 3. 原生目标：具体 owners、依赖复用和 Gate 证据

### G-D1：资源可搬移与私有 DB owner（与 pg_search Gate 合并）

- 依赖负责：uv/CPython 可搬移发行布局与依赖安装；PostgreSQL 启停、锁、WAL恢复、认证；electron-builder 安装/卸载与协议注册；Windows ACL提供用户隔离。
- Pulsara 负责：唯一 native executable locator、实际安装目录对应资源、版本/架构闭合、PULSARA_HOME解析、专用cluster识别、admin/runtime角色边界、启动并行顺序与失败保留数据。
- 适配规模：资源脚本及mise graph补Windows变体；Python路径集中为一个owner；PS 7.6 LTS作为private shell随包（依赖供应链/许可证/PE架构与离线启动同验），工具调用由后端terminal owner使用明确exe而非依赖PATH或用户profile；backend校验与Node PG owner同时hard cut支持已规定Windows端点。若用 loopback TCP，明确 SCRAM、随机凭据来源/保存权限与非日志泄漏、端口获得与冲突的物理分配规则，不得 trust-host、不把`localhost`当owner证明。凭据不是新的语义数据库或execution恢复机制。
- Gate证据：从Windows安装目录移动整包到空格/中文路径后运行；无系统Python/uv/PG/rg/浏览器；当前用户可启动、另一普通用户不能连接专用DB/读取凭据；模拟旧实例仍退出、端口被他人占用、PID重用/未知cluster拒绝接管；DB reset只删验证过的专用data-dir，任意junction/reparse跳转拒绝。
- 如果pinned pg_search没有可维护Windows payload或编译路径，整个native desktop资源Gate失败。不要以SQLite/远程PG/WSL替换并仍声称通过。

### G-D2：内核 lifetime 与 shutdown（与 ConPTY/进程 Gate 合并）

- Electron负责UI single-instance与原生窗口、系统结束事件；Python内核负责accepted work、工具/terminal children与settlement；Node PythonRuntime仅有child/stdin owner并等待close；BundledPostgres负责经过核实的专用instance。
- Windows kernel lifetime lock应复用成熟库/OS locking能力；先检查项目现有依赖extension API，不自建锁目录lease/PID时间戳恢复协议。锁必须覆盖当前Python完整close路径，错误启动第二实例不得先进入DB写入。Electron lock与Python lock语义不同。
- 正常退出沿现有顺序：停接新工作→现有application owners停止并结算→Python close成功→PG stop→app exit。启动失败不能按猜测PID杀旧owner；显式用户quit与installer覆盖安装要走同一个settlement owner。
- Windows系统结束有独立物理边界：query-session-end时尽力发起正确收尾；session-end后不能承诺阻止注销。崩溃或OS终止不添加 durable execution恢复；重启恢复既有canonical truth，进程本地advisory状态按现有合同丢失。UI不能把“已收到shutdown”显示成“工作已结算”。[Electron官方事件合同](https://www.electronjs.org/docs/latest/api/browser-window#event-query-session-end-windows)。
- Gate证据：Windows真机的stdin EOF、cancel、MCP/terminal child仍在输出、普通退出、重复退出、内核pre-ready异常、Electron异常退出、注销/重启；确认无孤儿占用安装资源、无第二writer、正常成功退出不早停PG。ConPTY库是否能满足进程树和副作用边界由terminal Gate实测决定。
- 明确否决：移植ZCode“固定9秒后taskkill/继续安装”的策略；它与Pulsara不按总时长终结工作、不伪造settlement的合同冲突。

### G-D3：安全文件 owner（backend Gate 的 desktop验收切面）

- 依赖负责：Windows handle、reparse、ACL与原子文件系统原语，Node URL转换；Pulsara保留权限与读取根、已打开对象身份、no-clobber写入、取消后实际副作用结算。
- native path与file URL是两种输入。Node `pathToFileURL`/`fileURLToPath`可承担Windows编码/解码；renderer不能引入Node primitive，可由窄IPC或后端接口返回URL，并继续只暴露现有受限动作。任何窄适配必须收齐盘符、UNC、extended/device命名的产品资格，而不是“替换所有反斜杠”。[Node URL API](https://nodejs.org/api/url.html#urlpathtofileurlpath-options)。
- 默认建议只先资格本地NTFS常规路径；UNC/网络share与device namespace的允许/拒绝必须在根spec明确，不能调用不同处各自猜测。未支持类型返回明确错误。文件preview action仍由后端token/scope校验后给Electron，不将shell.openPath放宽为任意exe。
- Gate证据：drag/drop、choose files/dir、@补全、markdown/native path、tool presentation、附件preview/复制、HTML和Markdown相对资源、reveal/open office/PDF/image、artifact export完整贯通；夹具含`C:\\中文 空格\\a#?%.md`（按Windows合法字符调整，`?`应拒绝，不是可创建叶名）、百分号编码、大小写别名、盘符相对路径、junction交换、临时目录被替换、磁盘满与取消。

## 4. UI、安装与更新的具体落地建议

1. **Windows chrome**：优先 `titleBarStyle:hidden + titleBarOverlay`，由Electron保留Windows原生按钮/系统菜单/snap；CSS给overlay预留空间并验证多display DPI。官方明确hidden且无overlay不会显示Win controls，所以现状是确定缺口。若坚持自绘buttons，额外承担snap/键盘系统菜单/高对比度与accessible hit area，成本高且无当前产品收益。[Electron window options](https://www.electronjs.org/docs/latest/api/structures/base-window-options)。
2. **关闭/后台**：可先沿现有Windows分支的“最小化/退出”对话，不冒充已有托盘；若要close-to-tray，显式Windows tray owner、恢复与退出入口、tray失败时退回可操作窗口。Linux tray实现可复用逻辑，窗口产品语义由Pulsara定。
3. **权限**：继续`asInvoker`，不让模型工具因安装器提升而随应用提权。壳、前端、guest的sandbox/contextIsolation/nodeIntegration现有配置保留。不要通过关闭Chromium sandbox来解决安装权限。
4. **CLI**：保留版本/帮助/启动/诊断命令；每用户CLI目录、入口文件形式、PATH不自动改、同名冲突不自动覆盖应在spec决定。建议安装器管理稳定`pulsara.cmd`或既有Python console entry，脚本仅做正确引用和`%*`转交；若cmd quoting/Unicode不能满足现有argv合同，复用成熟native shim。`Get-Command pulsara`、`where.exe pulsara`替换POSIX说明，必须覆盖PATHEXT与PowerShell/CMD分歧。不要仅复制无扩展sh脚本。
5. **NSIS**：复用electron-builder默认install/uninstall，选择per-user；固定appId、GUID/AUMID与exe/product名称、ico、artifact名。仅在实测真实缺口时加include宏，官方也建议include而非复制整套NSIS。[NSIS官方](https://www.electron.build/v26/docs/nsis/)。
6. **手动更新**：先显式退出并完成工作结算，再交NSIS覆盖。安装器发现运行中或资源被占用时要求关闭或明确失败，不能先删除一部分程序树。卸载保留PULSARA_HOME及用户workspace。应用安装目录不是用户工作目录；若承诺保留用户意外放置的无关文件，需要另评估最小owner清单，而不是默认移植ZCode manifest机制。
7. **签名**：区分开发可评估unsigned包与对外正常分发资格。由electron-builder签EXE/installer，并按资源执行链核实随包native DLL/EXE策略，CI用现成签名provider；不创建自制信任系统。证书/Artifact Signing账号资格、采购和信誉形成时间是外部日历风险，不能承诺签名后无任何SmartScreen提示。Microsoft说明Smart App Control认可trusted-provider证书，并推荐Artifact Signing。[Microsoft官方](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control)。
8. **自动更新可选**：新增electron-updater时由它负责下载/校验/安装器启动；Pulsara仅负责复用quit结算后交接与UI错误。不要从Windows资格任务扩张到更新server、durable job/receipt、跨版本DB迁移。当前README明确手动更新，首版没有必要先增此范围。

## 5. 参考仓库：可复用思路与不应搬入的机制

阅读了各仓库AGENTS；ZCode按其要求只运行freshness检查，结果main与origin/main同步。未写参考仓库、未运行其产品、未把其历史spec当Pulsara权限。

| 参考 | 实际位置 | 可吸收的边界 | 不直接移植 |
| --- | --- | --- | --- |
| OpenCode Electron NSIS | `/Users/plumliu/Desktop/python_workspace/opencode/packages/desktop/electron-builder.config.ts:119-160,183-197` | 架构匹配、bundled `.exe`验收、NSIS per-user，titlebar复用Electron | `verifyUpdateCodeSignature:false`不能照抄；Bun standalone CLI不是Python供应链方案 |
| OpenCode sandbox安装ACL | `.../resources/windows/installer.nsh:1-6` | 真正运行Electron sandbox需要可执行资源ACL；只改应用data ACL不够 | 该宏授予安装程序树read/execute，不能给包含凭据的PULSARA_HOME相同广泛权限；先复现才适配 |
| OpenCode系统结束 | `.../src/main/windows/index.ts:217-221` | 明确session-end不会等价before-quit，独立事件测试 | 仅标记registry quit不证明Pulsara任务已settle |
| ZCode签名/包 | `/Users/plumliu/Desktop/python_workspace/ZCode/packages/desktop/electron-builder.config.js:854-868,923-929` | 继续用electron-builder hook签node/dll/installer，不自写打包器；ico与安装scope显式配置 | 其定制sign provider与大规模afterPack补依赖不应变成Pulsara默认架构 |
| ZCode更新资源占用 | `.../docs/desktop/windows-update-install-handoff.md:10-55,98-123` | 安装资源被子进程持有是真实风险，正常退出+实际安装/重装测试比源码grep有价值 | 固定退出预算强杀、辅助scan fail-open继续安装不符合Pulsara现有正确性合同 |
| ZCode更新清理 | `.../docs/desktop/windows-installer-preserve-files.md:3-25` | 精确区分install资源与user data，不能无条件递归删用户放进目录的文件 | 无当前Pulsara产品理由，不增durable manifest/legacy compatibility；Pulsara未发布Win版，没历史Windows兼容债 |

## 6. Windows Electron + WSL2 整后端：真实备选成本

本节是对比，不授权实现第二产品路线。其好处是保留Linux终端、no-follow与ParadeDB payload；代价是引入Windows/Linux两个文件/进程/用户home域。不能把Windows壳搭Linux后端称为同一原生资源布局。

| 项目 | 必须闭合的产品与owner | 复用/风险 |
| --- | --- | --- |
| WSL前置 | 用户已有选定Ubuntu24.04 WSL2，还是应用managed distro；若不存在，安装/重启/virtualization-disabled/企业policy/离线如何处理 | 系统`wsl.exe`安装/import/export/lifecycle可复用。官方安装步骤需管理员并可能重启；不能作为透明安装无感前提。[安装官方](https://learn.microsoft.com/en-us/windows/wsl/install) |
| distro所有权 | 明确name、arch、Linux user、rootfs版本、升级/卸载保留；绝不能接管用户默认distro或全局`wsl --shutdown` | 若只是existing-distro developer preview可省bootstrap但不是相同桌面安装承诺；managed distro还需体积、磁盘空间、备份说明与发行源许可 |
| home/config | PG/socket/kernel lock和配置唯一owner放Linux文件系统；Win壳electron preferences用Windows路径，二者必须显式命名 | 不能将当前`home`一个变量同时当两端cwd/env/Node日志路径；也不能把PG data/socket放`/mnt/c/...`并假设uid/0700等价 |
| 文件picker/drop | Windows picker返回C:\\...，backend需要`/mnt/c/...`；Linux `/home/...`返回Win openPath需`\\wsl.localhost\\DISTRO\\...` | wslpath/WSL自身interop做转换，Pulsara拥有路径域/选定distro与映射失败语义；不能前端replace斜杠。网络UNC、其他distro、不可见volume、OneDrive未hydrated需确定拒绝/支持 |
| canonical prompt | `prompt-draft.ts:190`当前立即把原生path写入普通文本；翻译必须在生成canonical reference前完成，模型所见路径须是执行后端能访问的路径 | 保留原始消息prefix不重写；显示用Win路径与模型路径若同时存在，须有明确用途而不是重复fingerprint/authority |
| HTTP origin | 先验证Windows访问WSL `127.0.0.1:<port>`，当前shell校验可继续 | 默认NAT官方支持Windows访问Linux服务localhost；不必须自写HTTP bridge，也不应把服务改绑0.0.0.0。若企业策略阻断localhost，明确unsupported outcome或修订路径，不能默默开LAN。[网络官方](https://learn.microsoft.com/en-us/windows/wsl/networking) |
| browser owner | Electron WebContentsView在Windows；模型Playwright Chromium在Linux | localhost URL的可达对象可能不同，用户启动Windows dev server时Linux工具在NAT不能照抄localhost；guest保护工作台端口规则需测映射前后，不把same-port当全部跨域身份 |
| OAuth | 实际loopback authorization callback owner在Linux、外部浏览器在Windows | custom pulsara URI仍只focus；授权code/state继续归既有OAuth owner。验证外部browser到Linux callback，不从deep-link绕过grant校验 |
| 进程退出 | Windows Node child是wsl.exe wrapper；Linux Python/PG/terminal真正owner在distro | stdin协作控制仍可复用，但wsl.exe close不自动证明Linux已退出，需Linux owner确认流程与既有lock对齐；不能taskkill wrapper后停DB、不能停止其他distro |
| CLI | Win终端入口要将argv、cwd、PULSARA_HOME正确进入指定Linux用户/distro | PowerShell/CMD、重定向/UTF8、空格/引号、从Linux侧`pulsara app`回到Win壳须资格验证；不允许自动跟随用户default distro变化 |
| resources/update | Win安装器携带Win Electron与Linux payload，或distro内安装；资源更新需先退出Linux owners | Windows文件锁问题和WSL distro残留并存；不增加自制远端复制/sync/lease框架。

Microsoft建议Linux命令工作负载尽可能在Linux文件系统保存项目；跨域文件存取有性能和大小写语义成本。Windows工作目录仍可显式挂载，但“本地项目”等价语义必须真机长任务/大仓库验证，不能仅跑hello-world。[文件系统官方](https://learn.microsoft.com/en-us/windows/wsl/filesystems)。

推荐根spec保留本路线为需要产品改版才能启动的备选，现阶段不锁定未定义的distro管理/配置落点。native Gate失败后必须重新决定，不自动选择此路线。

## 7. 工作包与成本区间

以下为**有Windows开发机与CI、单名熟悉仓库工程师的净工程人日**，代码+测试+文档；区间是估计，不是跑过Windows后的承诺。日历时间另受pg_search可得性、签名审批、真机环境制约。backend terminal/安全文件完整迁移由其他调研包估算，这里只计桌面接口与UI验证，合并总预算时勿重复计算。

| 包 | 工作内容 | 原生估计 | 依赖/退出条件 |
| --- | --- | --- | --- |
| D0 Gate集成切片 | 在Win11 25H2 x64真实运行最小Python+专用PG+pg_search+terminal+安全文件+Electron链，明确owner合同 | 5–10 | 与backend/DB Gate共享，不应再累加完整Gate估计；失败停止全面实施 |
| D1 资源构建/重定位 | uv布局、PE/DLL/arch校验、mise Windows命令、资源native smoke、离线运行 | 6–10 | 必须有可维护pg_search payload；不包含移植pg_search本身 |
| D2 宿主生命周期 | resolver、kernel lock/IPC接缝、DB owner对接、启动/失败/正常exit/system-end、logs | 5–9 | 底层Windows进程/文件/DB primitives已通过Gate |
| D3 前端/文件/UX | platform、native titlebar、Ctrl、拖放/链接/preview、目录选择、用户级open、DPI/IME | 4–7 | backend native文件owner已完成，路径域合同冻结 |
| D4 CLI/安装签名/CI | per-user NSIS、ico/AUMID、CLI及冲突、手动更新/uninstall保数据、release inventory、Win runner/sign hook | 6–10 | 签名服务资格/机器可用；人工证书审批不计工程日 |
| D5 端到端资格 | packaged install+model/MCP/browser/files/long-horizon、shutdown/update、故障、mac/Linux回归 | 5–8 | 与整体验收共享，不再重复计同一场景 |
| 合计（桌面相关净工作量） | D1–D5，Gate另列且与全项目共享 | **26–44 人日 + D0共享Gate** | 不包含backend完整Windows工具/存储重构、pg_search移植、自动更新、ARM64 |
| 自动更新可选增量 | electron-updater集成、签名验证、下载/网络失败、quit交接、发布元数据与真installer测试 | +5–9 | 需新产品合同；不包含新更新服务/跨版本DB迁移 |

**WSL路线对比**：在用户已经配置好指定Ubuntu24.04 WSL2、只承诺技术预览的条件下，Windows壳+路径双域+stdio/runtime控制+手动安装/CLI/资格约 **24–40人日**；该区间保留Linux backend而省原生底层移植，不能再加native D1/D2全额。若要与现有“用户无需准备运行时”桌面安装相近的managed-distro产品，额外bootstrap/distro生命周期、离线与错误引导、双资源升级、完整跨域文件覆盖约 **15–25人日**，总 **39–65人日**，并有系统前置条件与磁盘/企业政策支持风险。自动更新另加。WSL不是普遍比原生便宜；native pg_search若不可得，原生成本上界也不能合理封顶。

## 8. 验收矩阵（不能用Mac单测替Windows资格）

| 维度 | 必验样本 | 通过准则 |
| --- | --- | --- |
| clean machine | Win11 25H2 x64普通用户，未装uv/Python/PG/rg/Chromium，安装路径含空格/中文 | 安装后正常启动，所有资源来自包，runtime/CLI/renderer版本一致，无系统依赖fallback |
| security boundary | 当前与第二Windows普通用户、安装dir与data ACL、guest网页尝试desktop IPC/control port | 只允许正确owner，未开放凭据/诊断/DB旁路，不关闭sandbox |
| startup | cold、warm、旧owner慢退、UI重复启动、PG失败、Python导入失败、资源缺失 | 可观察错误/日志，retry不清数据，无第二内核writer，不错误stop他人PG |
| home | default、显式absolute PULSARA_HOME、relative拒绝、不可写、junction/目录换位 | resolver合同一致，reset仅专用cluster，无越界写删 |
| keyboard/window | Ctrl K/N、中文IME、AltF4、Win snap/系统菜单、双击标题、125/150/200%DPI、屏幕切换 | 无控件遮挡、焦点串入guest、误发送输入；显式关闭/退出行为准确 |
| file roundtrip | picker/@/drop/paste、盘符/Unicode/#/%/大小写、UNC/device政策、relative markdown、PDF/Office/image/HTML | 模型引用路径可读、preview/shell reveal正确、native path/URL互转不丢字符；不支持类型明确失败 |
| side effects | import/export/write no-clobber、被替换父目录/reparse、取消、I/O失败 | 只有获准对象被读写，实际发布/可能发布正确结算，不虚称无副作用 |
| browser/OAuth | native browserguest+模型browser、popup、permission/download、control port保护；OAuth warm/cold focus+loopback | 保留各owner权限，deep-link不作code/state输入，选定HTTP origin不扩大LAN暴露 |
| quit/update | active model/MCP/terminal/subagent/scheduled任务，退出竞态、内核close失败，手动覆盖/重装/uninstall | 正常exit等待结算再停PG；安装资源未释放时清晰失败；配置/DB/workspace保留，未引入强杀寿命上限 |
| crash/system end | Electron/Python crash、注销、重启、断电后的PG recovery | 既有canonical truth可重新打开，进程本地advisory损失如实表现，无假execution replay恢复 |
| CLI | PowerShell/CMD、PATH/PATHEXT同名、Unicode argv、--help/--version/app/diagnostic、关闭原终端 | 入口准确，冲突不覆盖，GUI仅一实例；终端不同环境的PATH差异不被误诊成安装失败 |
| release | Win installer/签名/完整resource清单、draft精确asset集合、保留Mac/Linux包 | Windows artifact真正进入版本/上传/安装校验，不只在CI生成文件；未执行自动发布 |
| long horizon | 真实provider多轮、工具/终端持续输出、queued subagents、compaction、用户steer | 无新turn/model/tool/总时长上限；installed SYSTEM/tools与messages前缀连续；现有advisory/持久语义不扩张 |
| WSL备选若重新授权 | existing与managed distro、NAT/mirrored、Win/Linux项目、wslpath失败、WSL关闭 | 限定distro/user，双域路径不混用，不停止其他distro；wrapper退出不冒充Linux结算 |

## 9. 未决事项与报告局限

- Windows native pg_search供应链是否存在可维护构建路径、许可/分发与版本资格，需数据库Gate实证；没有Windows执行环境的本次调研不能对此下成功结论。
- Windows lifetime锁/named-pipe、DB认证凭据落点、native安全文件库/ConPTY的准确选择应由对应owner基于实际dependency实现决定，不能先指定无法满足合同的库再补fallback。
- Windows首次规格应明确UNC/device/长路径资格、CLI安装scope与用户PATH策略、close-to-tray是否首版需求、对外发布是否签名阻断；这不妨碍原生目标+Gate冻结，但会影响实施细节与成本。
- 参考仓库只能提供风险和成熟扩展点证据，未证明同样方案在Pulsara可直接运行。ZCode较复杂的兼容/更新/manifest/fail-open路径不符合本仓库hard-cut与durability subtraction偏好。
- 本次仅静态阅读与官方primary-source核实，没有Windows真机/VM、没有构建安装包、没有执行Pulsara测试或真实provider dogfood，因此上表是待执行验收，不是已通过清单。

## 10. 本地已锁定依赖API复核

实际读取当前`desktop/node_modules`，而非仅据最新网页推断：Electron 44.9.0的`electron.d.ts:2691-2804,4400-4412`提供query-session-end/session-end与titleBarOverlay；electron-builder 26.15.3的`app-builder-lib/out/options/winOptions.d.ts:4-77`提供NSIS target、asInvoker、默认开启update signature验证、signtool/Azure signing和signExts；`out/targets/nsis/nsisOptions.d.ts:76-103,163-185`提供perMachine/include/deleteAppDataOnUninstall等扩展点。现成机制足够承担窗口/安装/签名边界，本调研没有找到需要复制安装器、取消sandbox或关闭update签名校验的理由。真实系统行为仍按验收矩阵执行。

## 11. 补充参考：CC Switch（只读）

本地Git `0b5da510`；`rg --files -g AGENTS.md`未发现该仓库附加AGENTS。`src-tauri/tauri.windows.conf.json:4-12`选择Windows原生visible标题栏；`src-tauri/tauri.conf.json:36-50`单列ico与Windows WiX模板；`src-tauri/wix/per-user-main.wxs:23-30`明确perUser/limited。可借鉴“明确平台chrome、普通用户scope”，但它是Tauri/WebView2/Rust产品，不构成Pulsara Electron/Python/PG可直接替换框架的理由。

`.github/workflows/ci.yml:155-214`在Windows-2025上另做WSL2-backed home文件系统合同，明确采用`\\wsl.localhost\\Ubuntu-24.04\\tmp...`；注释指出link.exe/mt.exe不能在WSL UNC TEMP中创建manifest，因此先用native temp编译再切到UNC测试。这个案例支持WSL不能把Windows build/runtime的所有路径变量无差别改成Linux/UNC home；它不是“整后端在WSL运行”的证据。其0777临时测试目录也不能当作Pulsara生产凭据ACL合同。

成本交叉审阅说明：已读`../backend/report.md`；D0与backend G0共用，D1资源构建、D2的Python lock/IPC/PG适配、D5 CI/E2E均与backend工作包部分重叠。根预算应按唯一owner重排，不能直接把两份报告总数相加。WSL区间是该整条替代路线估计，不再叠加backend WSL估计。

追加依赖源码核查：builder26.15.3默认NSIS `allowOnlyOneInstallerInstance.nsh:94,127,140-141`会按安装路径Stop-Process并在约1秒后force kill；`:37-41`有`customCheckAppRunning`扩展。Pulsara保留正常settlement要求，必须用此现成扩展点关闭默认强杀策略并做真实installer资格测试；这是一处具体机制冲突，不是自写安装框架的理由。
