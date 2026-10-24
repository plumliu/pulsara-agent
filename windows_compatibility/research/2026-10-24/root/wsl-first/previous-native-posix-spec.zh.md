# Pulsara Windows 兼容实施规格

日期：2026-10-24。状态：**实施规格（含代码组织与POSIX先行分阶段修订）已冻结。Phase A未实施，G0未执行，Windows支持尚未实现或激活**。

调研基线：Git `60e8f21d37ca2c650500ae14438630006d3e62fb`。本轮交付是实施规格与成本研究，没有实施 Windows 支持。规格冻结、前置可行性验证通过、产品激活是三个不同状态；后两者必须有原生 Windows 证据。

## 1. 结论与实施决策

Windows 兼容是一次平台移植，不能按“Electron 增加一个 exe target”估算。成本主要集中在三条相互独立的链路：终端物理执行与副作用结算、带安全语义的文件操作、随包 PostgreSQL 与扩展。前端业务和模型调用大部分可复用，但它们暴露的路径、快捷键、宿主能力与启动失败处理仍须逐项闭合。

本规格选择 **原生 Windows 桌面 + 原生 Python 内核 + 原生 PostgreSQL + 随包 PowerShell 7.6 LTS + ConPTY/PIPE**。第一批验收环境为 **Windows 11 25H2 x64、普通用户、本地 NTFS 磁盘**；记录实际系统 build。选择这个矩阵是为了覆盖当前受支持的 Windows 桌面和现有依赖可得的架构，不是添加任务或运行寿命上限。Windows 10、Windows ARM64、Windows Server、网络盘/UNC workspace、WSL workspace、非 NTFS home 不在首批支持声明中，不凭未测试就断言它们永远不可支持。[Windows 官方支持信息](https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information)

实施分为两段：**Phase A先抽取现有Linux/macOS机制接口、接齐POSIX消费者并完成回归，可独立交付；Phase B在第4节G0全部通过后接入Windows实现并做原生验收。** G0研究可与A并行，不是A的前置条件。这样把“内部重构是否回归”与“Windows机制是否满足合同”分开验证；A的详细边界、切片与独立退出条件见11.2。**G0未过，不能进入全面Windows移植或激活原生Windows版本；不得悄悄改为WSL、外置数据库、删除搜索、PIPE冒充PTY、放宽安全检查。** G0失败不否定已验收的POSIX改进；如实记录失败点，修订Windows路线。

WSL2 整体内核是有价值的备选：它可能明显降低 POSIX/数据库迁移成本，也会把 shell、工具、文件路径、Skills、MCP 和 hooks 改成 Linux 语义。第 12 节完整估算这一条路径；本规格没有同时实施两套 Windows 产品，也不把 WSL 作为原生失败后的自动 fallback。

## 2. 现状、证据范围与不变量

### 2.1 当前生产事实

| 范围 | 已检查的生产 owner | 当前事实与 Windows 成本 |
| --- | --- | --- |
| 终端调用 | `ports/terminal.py`、`capability/builtin_catalog.py`、`conversation_kernel/tool_runtime.py` | 每次独立命令，显式 workdir；运行中 process 可跨多轮跟进；工具面仍为 terminal / terminal_process / terminal_monitor |
| 物理执行 | `terminal_process/manager.py` | 顶层 import pty；openpty、start_new_session、killpg、POSIX reader 和 group-liveness；不能在 Windows 导入后直接运行 |
| 环境 | `terminal_process/environment.py` | SHELL、zsh/bash/sh、login probe、selectors 管道、`.venv/bin`、POSIX 默认 PATH 与进程组清理；PATH虽已用os.pathsep，来源与shell语义仍需适配 |
| 宿主内核 | `desktop_runtime.py` | fcntl kernel lifetime lock、uid/0700、Unix diagnostics socket、私有 Unix PG socket、stdin JSONL 控制与 asyncio 管道 |
| 会话内部协议 | `terminal_protocol/v3_gateway.py`、`desktop_app/protocol_client.py` | 名称含terminal，但承载桌面conversation/session桥；当前start/open_unix_connection同样阻断全部会话，不只是terminal工具 |
| 安全文件操作 | `local_source_binding.py`、`exclusive_publish.py`、`settings.py`、skill publisher/removal、plugin package store、desktop file import/preview | dir_fd、no-follow、目录描述符、fchmod、原子发布、文件身份/替换检测；部分模块顶层访问 POSIX flags，另一些 getattr(...,0) 也不代表 Windows 已保留语义 |
| 数据库 | `desktop/src/postgres.ts`、`storage/migrations/*`、`retrieval/pg_search.py`、`conversation_kernel/history_search.py` | PG17、vector、pg_search 0.26.1 以及 BM25、score/snippet API 是实际必需项，不是可略过的优化 |
| 桌面资源 | `desktop/scripts/prepare_resources.py`、`tools/hatch_build.py`、`mise.toml` | 资源、wheel target、Python 路径、ELF/Mach-O 检查与 native dependency 收集以 macOS/Linux 为基础 |
| 前端与原生动作 | `frontend/lib/desktop-host.ts`、`file-preview.ts`、路径附件/预览组件、`desktop/src/main.ts`、preload/browser owners | platform 类型只列 darwin/linux；本地链接部分只接受 POSIX；文件 URL 编码、窗口控制与 CLI 路径需要适配 |
| 发布 | `.github/workflows/desktop-package.yml`、`desktop/scripts/release_artifacts.py` | 当前矩阵与必需产物只有一个 DMG、两个 deb；不能生成 exe 后绕过原有版本与资源校验 |

代码定位以文件和具体 owner 为导航，不把行号当永久接口。root 的静态 AST 候选扫描得到 15 个文件、61 处 POSIX 候选，见工作区 `root/posix-candidates.json`；它不是完整缺陷数量，更不是 Windows 运行结果。

### 2.2 有效语义与历史文档

根 `AGENTS.md`、当前生产代码和本规格共同约束实施。历史桌面迁移合同描述了当前数据库/启动所有权，可用于追溯；本规格在 Windows 范围取代其中“不做 Windows”的范围条款。不要借此复活已删除的 Web 产品入口。

`archived_docs/PULSARA_TERMINAL_MODEL_CALL_COGNITIVE_SUBTRACTION_SPEC.zh.md` 对照当前代码仍描述已实施的终端合同。`PULSARA_PERSISTENT_TERMINAL_MODEL_API_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 顶部明确为“未实施的历史候选”；**不引入常驻 shell，不增加 session/command 身份层，不恢复 terminal_session_id 或模型 log 动作。** UI 内部 `log_process` 的存在不等于模型动作别名。

### 2.3 全程保留

1. 当前 epoch 的 SYSTEM、provider tools 字节不变，messages 仅追加后缀。Windows 环境说明、shell 说明或工具描述的变动只在 cold epoch 或明确采用的 compaction successor 安装；平台检测不能新增 rebase 边界。
2. 保留现有 foreground decision、权限、取消、输入已写/未写/未知结算和物理 join；不因 UI 收到取消就假装副作用未发生，不自动重发结果不明的命令或输入。
3. 不新增 durable event、live event kind、subject、append guard、product relation、durable job、receipt、lease 或执行恢复日志。平台 handle、Job、锁、诊断连接均是 process-local；数据库继续是现有 canonical truth。
4. 保留现有物理资源边界及其原因，例如 8 个尚未物理结算的终端执行槽、单进程 16 MiB/Host 128 MiB 输出保留；不把它们变成累计任务/调用/轮次/运行时长上限。容量满仍按现行 typed not-started/blocked 合同结算，不在本规格发明排队产品。
5. 继续使用保存的有效 Pulsara home 与 LocalSettingsStore；生产配置只读用于 dogfood，实际 credentials 精确脱敏，保留其余有效诊断。没有保存配置时报告缺少项，不引入环境 API key fallback。
6. 平台实现是同一产品 owner 的适配，不是保留旧/新两套业务路径。POSIX 实现仍服务 macOS/Linux；Windows 不保留不安全 fallback、旧别名或影子状态。无 schema 语义变化时不制造空 migration。

## 3. 目标结构与所有权

```text
Windows Electron 主进程
  ├─ BrowserWindow / tray / preload / dialogs / browser owner
  ├─ 专用 PostgreSQL owner → pg_ctl / postgres / extensions
  └─ 原生 Python 子进程（结构化控制管道）
       ├─ DesktopApplication / 原 HTTP、流式观察与 kernel
       ├─ 原权限、终端、输出、monitor、subagent owners
       │    └─ Windows physical adapter → Win32 process + PIPE/ConPTY + Job
       └─ 原 settings / Skills / plugins / 文件 owners
            └─ Windows handle/ACL/no-follow/atomic-publish adapter
```

| 事项 | 依赖/平台拥有 | Pulsara 拥有的最小适配 |
| --- | --- | --- |
| ConPTY / PIPE / OS handles | Windows 控制台、文件与进程 API；优先采用经过核实的 maintained binding | 向 ProcessRegistry 提供创建、读写、退出/纳管范围存活、关闭和 join；输出、权限、工具投影仍只在现有 owner |
| 进程集合 | Windows Job Object、创建属性或 suspended-create/assign/resume | 创建前纳管、无 breakaway 的受管理直接子孙、资源回收；Job 不是权限沙箱或任意系统进程清理证明 |
| 文件系统安全 | NTFS、Windows handle、reparse/ACL/rename API；优先复用 pywin32/标准库已有支持 | 等价安全操作，不复制通用文件系统；保持原调用者事务边界和失败语义 |
| Python 异步 | CPython/asyncio、AnyIO、现有 SDK | 保持 Windows 默认 Proactor；仅替换不受支持的 Unix/pipe seams，不全局改 Selector 规避问题 |
| 数据库 | PostgreSQL 官方进程、WAL、认证与扩展 | 应用专用实例、地址/凭据注入、角色隔离、生命周期和失败展示 |
| 打包 | uv、mise、Playwright、Hatch、electron-builder、PowerShell 官方 runtime | 平台资源 recipe、校验和安装配置；不增加缓存权威或 fingerprint 注册表 |
| GUI、链接、文件系统打开 | Electron 原生 API、Node URL/path API | 来源验证、实际文件目标与工作台展示，不手写通用浏览器或 file URL parser |

G0 必须读取实际采用版本的 API 与实现。已有库不能实现创建前纳管、半关闭或安全路径语义时，记录具体缺口和扩展点，再实现一个窄 adapter；不能“复制一份协议/PTY manager，再包 SDK 名字”。本轮发现的缺口见第 5、6、8 节。

### 3.1 生产代码落点与目录组织

Phase A与后续Phase B的Python正式实现均放在 **`src/pulsara_agent/`**，沿用同一个Python包、CLI入口与依赖锁。Electron代码继续在`desktop/`，React代码继续在`frontend/`。`output/`只保存调研、G0探针和测试证据；生产代码、wheel、安装包不得import它、查找探针脚本或把开发者checkout当运行依赖。

采用“共用产品owner + 领域内平台实现 + 少量被多个owner共用的OS原语”。新增文件的建议落点如下；这些是计划文件，当前尚未创建生产实现。实现可合并没有独立职责的薄文件，但不得改变下述依赖方向/所有权，或另建一套Windows业务应用。

```text
src/pulsara_agent/
  process_scope.py                 # 共用窄进程范围契约/创建入口
  _process_scope_posix.py          # POSIX进程组与精确资源回收
  _process_scope_windows.py        # 经G0验证的Win32 Job/handle机制
  local_source_binding.py         # DirectoryBinding/FileBinding共用操作契约
  _local_source_posix.py           # fd/dir_fd/no-follow/锁/发布所需原语
  _local_source_windows.py         # HANDLE/reparse/ACL/锁/发布所需原语
  exclusive_publish.py            # 沿用现有独占发布入口，接收typed binding
  runtime_resources.py            # Python侧只读随包资源定位，供私有pwsh等消费
  desktop_runtime.py              # 共用settings/DB准备/应用/关闭编排
  desktop_runtime_io.py           # 宿主控制/诊断/锁适配的窄契约与平台选择
  _desktop_runtime_posix.py       # 原Unix诊断、控制I/O等平台操作
  _desktop_runtime_windows.py     # Win named pipe/匿名pipe适配，复用文件锁原语
  desktop_postgres_files.py       # 仅G0确认需要Python接缝时新增的专用DB文件helper
  terminal_process/
    manager.py                    # 唯一Registry/decision/完成结算
    output.py                     # 共用输出owner
    monitor.py                    # 共用monitor owner
    environment.py                # 共用环境policy/cache/probe接纳
    physical.py                   # terminal物理I/O窄契约与平台选择
    _physical_posix.py            # openpty/PIPE字节端点
    _physical_windows.py          # ConPTY/PIPE字节端点
    shell.py                      # 命令载荷生命周期/平台shell计划契约
    _shell_posix.py               # 原shell候选、argv、环境probe脚本
    _shell_windows.py             # 私有PS7、UTF-8载荷/短bootstrap/probe脚本
  terminal_protocol/
    v3_gateway.py                 # 共用protobuf/auth/attachment/dispatch
    transport.py                  # 共用连接生命周期契约与平台选择
    _transport_posix.py           # Unix socket
    _transport_windows.py         # 独占loopback listener/stream与确切lifetime
  desktop_app/
    protocol_client.py            # 共用帧与attachment，调用transport连接

desktop/src/
  resource-paths.ts               # Node侧只读资源定位，集中所有exe/目录布局
  main.ts                        # 唯一boot/reset/requestQuit与原生UI编排
  runtime.ts                     # 唯一Python子进程/JSONL/ready/close owner
  postgres.ts                    # 唯一BundledPostgres状态与认证/lifecycle owner
  postgres-posix.ts               # Unix端点、启动参数、实例检查的叶子操作
  postgres-windows.ts             # TCP/SCRAM、Win参数/实例检查、安全文件helper接缝
  cli-install.ts                 # 共用注册/冲突/状态policy，按平台安装实际入口
```

上树是A+B的最终候选落点，不是A必须预建的文件清单。A建立当前确有消费者的共用契约、POSIX实现和Node资源locator；所有`*_windows.py`/Windows TS叶子、PS资源、DB文件helper延后B，Python `runtime_resources.py`无当前POSIX消费者也延后。Unix IPC/PG/构建脚本的可选抽取条件见11.2；不为了补齐树创建空模块、Windows stub或未使用API。

这里的`process_scope`只封装进程范围与OS资源，不是平台服务容器；`local_source_binding`只提供现有消费者需要的安全操作，不是通用VFS。没有新增平台插件注册表、动态backend发现、通用RPC、执行队列或跨平台能力协商。`desktop_postgres_files.py`的跨语言边界见3.4。

### 3.2 共用逻辑与平台拆分清单

| 领域 | 保持共用的逻辑/owner | 拆分到平台实现的操作 | 原有接入位置 |
| --- | --- | --- | --- |
| Kernel与模型 | turn/subagent/scheduling、权限、foreground decision、副作用settlement、history/memory、provider prefix/compaction | 仅注入实际环境/路径观察，不复制kernel | `conversation_kernel/host.py`、`tool_runtime.py`、`context_sources.py`及现有provider owners |
| Terminal | `ProcessRegistry`、`_ProcessState`、准入reservation、process_id/scope、状态机、物理完成组合条件、容量、输出和monitor发布 | spawn所需端点/OS范围、read/write/half-close、root exit、range liveness、OS终止与handle释放 | `terminal_process/manager.py`接`physical.py`与`process_scope.py`；`output.py`、`monitor.py`继续共用 |
| Shell/环境 | 每次独立命令、workspace基准、default-deny、single-flight/TTL、snapshot解析/接纳与失败policy、凭据检查 | shell选择、argv/命令载荷、PS bootstrap或POSIX probe脚本、平台env-key与PATH/venv布局操作 | `terminal_process/environment.py`、`shell.py`；MCP/hooks保留各自env policy |
| 非终端进程 | MCP帧/SDK会话/credential guard；hook事件/命令选择/deadline/输出；截图请求/Playwright控制 | 共用同一窄`ProcessScope`的创建前纳管/终止/范围回收，按调用者提供同步或异步I/O端点 | `conversation_kernel/mcp/sdk_facade.py`、`hooks/executor.py`、`visualization_screenshot.py`及terminal probe |
| 文件/包/设置 | settings格式/事务结果、包/Skill来源冻结与借用、安装/删除授权、preview token、import/export发布结果 | 有主目录/文件绑定、逐级no-follow、身份比较、读写/seek/flush、ACL/mode、锁、rename/no-replace、精确清理 | `local_source_binding.py`与`exclusive_publish.py`供全部消费者使用，见3.4 |
| 路径 | workspace/home解析与availability的产品规则、唯一canonical来源、包内POSIX路径、URL/授权区分 | 本机盘符/最终路径/大小写/文件身份观察，POSIX系统alias，file URL编码所需宿主操作 | `workspace_identity.py`、`capability/pulsara_home.py`、`memory/scope.py`、preview/path-completion；前端只展示/调用受限API |
| 会话内部协议 | protobuf/framing/auth/controller attachment、detach/revoke与业务dispatch | Unix socket或Windows独占loopback连接、server listener生命周期 | `terminal_protocol/v3_gateway.py`、`desktop_app/protocol_client.py`共同接`transport.py` |
| 宿主控制 | JSONL消息语义、settings注入、database prepare、应用ready与shutdown/EOF结算 | stdin字节reader、diagnostics端点、OS lifetime锁的实际机制 | `desktop_runtime.py`接`desktop_runtime_io.py`；锁复用安全文件原语 |
| 数据库 | `BundledPostgres`启动状态/secret authority/reset次序；`prepare_database`的role/database；migration/schema/检索SQL | 随包exe布局、Unix socket或Win TCP参数、受管理实例物理检查与私有文件操作 | `desktop/src/postgres*.ts`、`desktop_runtime.py`、`storage/postgres_endpoint.py`及原migration owners |
| Electron/前端 | `boot/reset/requestQuit`状态机、IPC来源验证、HTTP API、组件/数据流/preview授权、用户级页面owner | 窗口chrome/tray、快捷键显示、系统打开、文件URL和平台安装动作 | `desktop/src/main.ts`、preload/browser/CLI owners；`frontend/lib/desktop-host.ts`及文件/标题栏组件 |
| 构建/发布 | uv/mise/Hatch/Playwright/electron-builder原有owner、版本/资源完整性、release集合校验 | 平台binary/layout/native依赖检查、Windows NSIS/签名/CLI资源 | `prepare_resources.py`、`build_environment.py`、`smoke_resources.py`、`tools/hatch_build.py`、`mise.toml`、release与CI |

平台实现不直接导入`conversation_kernel`、模型tool catalog、repository或UI，也不接收模型决定权限。产品owner可以调用窄OS接口；OS接口不能反向决定工具成功、释放语义容量或发用户可见completion。共用层不保留一份Windows专用状态机/输出缓存/认证或配置副本。

### 3.3 平台选择、导入与资源注入

- 在上述领域factory/现有composition处按实际`sys.platform`或`process.platform`选择一次实现，owner整个生命周期固定使用它。Python路径由`TerminalManager`创建处（当前`conversation_kernel/tool_runtime.py`）及`DesktopApplication`/`desktop_runtime`装配；Node由`main.ts`装配；不能在每个tool动作里重新检测/切换backend。
- factory只选择当前已实现且通过对应验收的平台。A只有现有POSIX实现，在实际需要该机制的入口对Windows明确unsupported，不创建占位adapter，也不把未修改领域全改为无条件导入失败。B的Windows依赖失败不能尝试POSIX、系统shell或WSL。支持矩阵与真实资源检查共同决定能否启动，不新建可由模型/用户任意字符串选择的backend配置。
- 已抽取领域的`pty`、`fcntl`、POSIX专属flags放入POSIX实现；B的Win32 binding/ConPTY依赖放入Windows实现。共享模块及`__init__.py`不同时import两个leaf，不在导入期求值另一平台常量。Windows完整CLI/settings/kernel/desktop入口import闭合是B条件，A不借此宣称已支持Windows；POSIX安装无需Windowsnative依赖。
- 单个value操作如Node`path`/URL标准库或平台快捷键文案可保留局部明确分支；不为每个if拆文件。涉及OS资源所有权、创建/关闭、认证或完整业务流程的分支，收口到对应leaf，避免在manager/MCP/hooks各复制一套Job/kill逻辑。
- `runtime_resources.py`和`desktop/src/resource-paths.ts`都是只读locator：输入随包root/目标平台，返回确切Python、PG、Playwright、PS等路径；不读取用户settings、不拥有进程或凭据、不建资源registry。Node的同一已解析Python路径供`resolveHome`、`PythonRuntime.waitForPreviousOwner/start`及安全文件helper使用。Python私有pwsh定位与打包recipe声明的同一布局一致，重定位smoke同时核对两端；`ripgrep.py`继续拥有包内rg位置，`desktop_launcher.py`继续使用既有`desktop-launch.json`定位GUI，不增第二份可编辑launch manifest。
- 测试可在原owner构造入口注入满足同一契约的实现/失败点，生产平台选择不可由provider工具参数控制。所有实际缺失的binding API与窄扩展原因记录在相应leaf/recipe旁；G0报告不能成为运行时authority。

### 3.4 必须成组修改的接口与调用者

**终端物理状态：** `_ProcessState.process:Popen`、`stdin`、`master_fd`等OS细节切成被同一state持有的physical对象，不能用一个通用`.pid`替代真实性。`physical.py`提供root退出/范围空/字节端点等观察和受控操作；reader/watcher/Timer可以继续由Registry组织，依赖自带reader时由同一physical对象保证join。`manager.py`仍是组合终态、`physical_completion`和completion subscriber的唯一发布者，`output.py`仍唯一接收/记账输出字节。冻结的是root退出、范围空、输出排空与workers join共同满足后才完成，不要求Windows机械复制POSIX内部等待次序；ConPTY需要的source release/close必须与持续并行drain配合，不能先reader.join阻塞到永远。移除manager内被替代的`openpty/Popen/killpg/OS group probe`执行路径，不保留新旧并跑。

**共享进程范围：** `process_scope.py`只共享创建前纳管、root/managed-range观察、终止/等待/释放；I/O需兼容terminal同步reader与MCP/hooks异步流，不强制把所有caller套成同一`Popen`或切换event loop。终端的Job和非终端Job归各自已接纳的物理执行持有，不设全局进程registry。MCP/hooks最后`credential_guard`必须覆盖真正的spawn、取消竞态收敛和安全发布；factory返回lazy plan后在guard外启动属于违规。各caller的env、frame界限、deadline、重连与副作用结果继续由原owner决定，不能迁入process scope。

**文件绑定：** 当前`open_absolute_directory_nofollow()`等返回`int fd`，消费者随后直接调用`os.open(dir_fd=...)`、`fstat/fchmod/dup/listdir/pread/fdopen/close`。只给Windows返回一个HANDLE整数不能完成迁移。统一hard cut为owned `DirectoryBinding` / `FileBinding`（或同等封闭typed对象），提供已有调用所需的子项打开/创建、普通文件与身份观察、字节读取/写入/有界read-at、枚举、flush/seal、共享/独占锁、绑定rename/replace/no-clobber、精确删除及显式close/受控借用；不公开通用`fileno()->int`逃逸口。

POSIX fd只留POSIX原语leaf内部，Windows HANDLE只留Windows原语leaf内部；共享入口不再导出`DIRECTORY_NOFOLLOW_FLAGS`供消费者拼flags。权限调用保留原有私有/冻结/只读意图；A可显式表达真实POSIX mode，B按G0 hard cut必要的权限契约，Windows不以POSIX mode位冒充ACL证明。read-at保留并发读取位置独立性，不在共享stream上无锁seek+read模拟pread；anchor duplicate/borrow继续持有原对象和锁，一个借用关闭不能释放其他借用，不按路径重新acquire制造空窗。

`exclusive_publish.publish`改收同一directory binding，在原publisher唯一入口执行；不让消费者自己做exists+rename。迁移同批覆盖：`settings.py`、`capability/{local_skills,bundled_skills,skill_import,local_skill_management,local_skill_publisher,local_skill_removal}.py`、`plugins/{package_core,package_store,skill_producer,source_import}.py`、`hooks/{source,trust}.py`、`desktop_app/{file_import,file_preview,file_preview_http,path_completion}.py`、`tools/builtins/filesystem.py`与使用同一held对象的清理/锁逻辑。尤其`plugins/source_import.py`通过package_core借fd，`file_preview_http.py`通过Preview做dup/fstat/pread，都是间接消费者，也必须接入；Preview/held Skill/held Plugin/PhysicalLifetimeAnchor等保存的fd槽与借用close责任一起修改。沿用现有冻结值/身份比较，不新增hash映射；POSIX稳定canonical ID不变。

**Electron与Python安全文件接缝：** `BundledPostgres`在Node侧拥有auth材料/PG生命周期，不能直接调用Python对象。G0先检查维护中的Node/native binding是否能满足第6节全部所需物理语义；能够满足则`postgres-windows.ts`使用它。仅在G0证明需要复用Python原语且验证接缝可行时新增`desktop_postgres_files.py`：随包Python的短命令经私有UTF-8管道执行专用DB目录/ACL、auth文件及initdb临时pwfile的有限安全文件操作，目标由`require_pulsara_home`限定；不提供任意路径文件服务，secret不进argv/环境/普通日志。专用data-dir删除如需经该接缝执行，必须由BundledPostgres先完成第7节确认/精确实例停止，helper只进行绑定对象的物理删除及真实结果返回，不拥有reset授权。

helper只持有本次文件handle，不拥有PG启动/重试/密码轮换/角色DDL，不常驻、不增加durable状态；一请求完成/EOF后完整退出，父进程取消/破管不能绕过文件cleanup或给出假成功。Node拿到实际secret后先纳入scrub再运行PG操作；错误/结果不明保留第7.2节结果，不能降为Node `fs.readFile/chmod/rm`的较弱路径。G0需闭合打开/校验/物理使用之间的对象绑定，不能helper返回路径后就假定外部initdb或后续删除不受替换影响。G0-R/F与资源smoke验证真实跨语言传输、secret不泄露、helper完整退出和所需binding生命周期；最终选定的唯一机制/窄API写入recipe/leaf旁，不运行时在native binding与helper之间fallback。这是原有安全/所有权合同内的机制选择，不授权新服务或第二secret authority。

**IPC与数据库：** `v3_gateway`/`protocol_client`共同替换socket路径假设，shared frame/auth不变；`desktop_runtime_io`只适配三条通道中的宿主JSONL/诊断和lock，不把Protocol-v3业务放进去。`postgres.ts`保留单一实例状态，平台leaf返回启动/端点参数并执行精确物理验证；Node/Python的managed DSN解析、role/bootstrap、SCRAM、auth文件和资源recipe按第7节同一切片更新，不能只改host字符串。`main.ts`不复制Windows boot/quit，`runtime.ts`不增加第二套ready/close状态机。

### 3.5 代码组织的验收与替代路径删除

Phase A的每个接缝须证明现有POSIX行为等价并完成全部相应消费者hard cut，可独立交付；Phase B加入Windows后，再证明两个实现遵守同一产品契约。生产引用检查需覆盖直接/间接消费者、持有DTO与cleanup路径：已抽取接缝的共享层不残留直接`pty/fcntl/killpg/dir_fd`操作，不用`getattr(flag,0)`跳过Windows保护；B的Windows叶子也不能保留仅杀根或Job失败后继续的路径。未纳入A的其他领域继续由原owner拥有，不为静态扫描全绿做无关重写。检查真实raw操作归属，不机械禁止用于展示/分类的合法`platform`判断。

平台无关测试继续放现有`tests/test_*.py`、`tests/frontend/`、`desktop/src/*.test.ts`；shell文字、路径与文件fixture通过声明的dialect/真实filesystem准备。新增`tests/windows/`用于真实Win Job/ConPTY/NTFS/pipe/native资源机制；POSIX机制仍由已有macOS/`tests/linux/`验证。不整体搬动测试树，不以Windows-only目录替代共用产品断言，也不让fake Win32实现证明真实ACL/Job成立。打包仍以`desktop/scripts`与现有workflow为唯一生产路径。

这次修订只规定后续实施；没有创建上述生产文件、修改provider schema或执行G0。实施者不能把“目录已建立/接口已抽出”计为能力通过，第11.2节定义A的独立退出条件，第11.1节定义B的接入条件。

## 4. G0：投入全面移植前的可行性闸门

G0是Phase B全面Windows移植的工程spike与证据闸门，不是Phase A的前置，也不是生产中新增的持久工作流。以下各项在真实Windows 11 x64、普通用户下执行；输出放个人`output/`工作区，不能用macOS monkeypatch os.name、WSL、容器、A的POSIX绿测或静态扫描替代。

| 闸门 | 必须产出的证据 | 不通过时 |
| --- | --- | --- |
| G0-D 数据库 | 固定 PG17.9 + vector0.8.1 + pg_search0.26.1 原生 x64 DLL/SQL/control 与全部依赖可重现构建/取得；私有目录 relocation 后 preload、CREATE EXTENSION、clean-v0、BM25中文/英文/代码搜索、snippet、vector query、退出/重启成功 | 不得删 pg_search 或悄悄换版本/数据库；说明编译/运行错误、上游缺口与维护负担，停止全面移植并修订路线 |
| G0-T 终端 | 所选 binding/最小扩展在 PIPE 和 ConPTY 都能创建前纳管；leader 先退出、孙进程继承 stdout、取消时启动、reader drain、写入/kill 竞态、物理 join 和无残留；1,048,576 字符 command 的脚本通道不占 stdin；PTY close_stdin 语义按5.4封闭 | 缺失扩展点不是默许 race/fallback；未能证明则不可激活，明确需要窄扩展或修订产品合同 |
| G0-F 文件安全 | NTFS 上真实 junction/reparse/目录替换/打开期间 rename/并发 publish、无覆盖发布、settings 原子替换、ACL与锁跨进程争用；包/Skill的Windows源元数据和exe/com/cmd/bat/显式解释器启动规则；证明不会跟随替换目标、覆盖他人路径或错误清理 | 不得将 O_NOFOLLOW 等 flags 置0、跳过 owner/权限验证或整功能降为只读假装支持；源/launch语义未证实则仍未通过 |
| G0-R 基础 runtime | uv locked安装与私有Python搬移；代表模块 import；原生 Playwright Chromium screenshot；Node→Python真实匿名控制管道及UTF-8、Protocol-v3 loopback/auth、named-pipe诊断、MCP/hooks必要env；用户目录、中文/空格路径、包内PS7.6 LTS与rg运行 | 记录准确 wheel/DLL/runtime 缺项，不自动用系统 Python、PATH 中 rg/pwsh 或管理员权限兜底 |

通过记录应给出实际 SDK/binding/PowerShell patch、OS build、工具版本、输入、输出、失败注入与限制；使用完整 typed 值与 Git 内容身份。下载包的官方摘要/签名属于真实不可变内容验证，可保留；不得生成逐文件源码 SHA、文档 SHA 或激活证据 hash。

本规格只冻结应满足的外部语义；G0 在这些语义内选定具体 binding 和系统调用组合，并把版本及窄缺口写入受影响 recipe/代码旁。若必须改变语义，先修订本规格，不能把 G0 当作自由改产品的口子。

## 5. 终端全链路

### 5.1 工具合同与物理 backend

保留 `terminal(command, workdir?, tty?, yield_time_ms?, max_output_chars?)`；每次新 shell，默认 workspace，相对 workdir 从 workspace 解析，内部 cd/export 不影响下一次。保留原 process_id 的 `list/poll/wait/write/submit/close_stdin/kill`，ROOT-only monitor 与输出 artifact 补读。poll/wait 的外层成功表示观察成功，不能因为命令非零退出就伪造工具调用失败。wait/yield 只限单次观察窗口，不限进程寿命。

将 `manager.py` 内直接依赖 `subprocess.Popen`、master_fd、killpg、group probe 的物理部分抽到同一 owner 的平台实现。最小接口围绕 spawn、read bytes、write bytes、close input、request termination、root exit、managed-set liveness、join/close；不新建调度器、输出缓存、session registry 或第二套终态。PosixPhysicalProcess 与 WindowsPhysicalProcess 由确定的平台选择，运行时不得互相 fallback。

Windows PIPE 合并 stderr/stdout 的可观测合同沿用当前实现。TTY 使用 ConPTY；Windows 控制台的 UTF-8/VT 输出进入原 `TerminalOutputOwner`，保留增量解码、ANSI清理、游标、GAP、截断、输出订阅和产物来源。不能先把 SDK 字符串解码/换行转换再按原始字节游标记账而不说明覆盖差异。用 split UTF-8、CR/CRLF、ANSI、二进制片段、超量输出验证单一 output owner。

### 5.2 创建、取消、终止与物理结算

1. 每次执行持有自己的 Job/管道/ConPTY 物理所有权，使用精确 handle，不按可复用 PID 推断身份。
2. 进程在运行用户代码之前纳入 Job：优先使用所选依赖支持的创建时 JOB_LIST 属性；或使用可证明的 suspended create → assign → resume。任一步失败，子进程不能继续执行命令，负责关闭/回收后返回 not-started 或真实 unknown。禁止 `spawn()` 已运行后再 Assign 来掩盖孙进程逸出窗口。
3. 不开启 breakaway；只声明 Windows 正常 CreateProcess 继承进入该 Job 的受管理集合，不承诺 WMI、服务、提权程序或外部系统启动的一切后代均受控。Job 是生命周期机制，不是安全隔离。原 POSIX 也没有任意逃逸后代全覆盖承诺。[Job Object 官方语义](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
4. `root exited`、`managed-set empty`、`output drained`、`reader/watcher joined` 均须分别观察；仅 shell wait 完成不能释放 8 槽、发布 completion 或唤醒 monitor。Job的IOCP通知只用于唤醒，不作为唯一事实：用`QueryInformationJobObject`的`ActiveProcesses==0`查询等实际证据，结合确切root退出/输出/线程join；不能单凭通知或Job handle wait断言范围为空。G0同时排除helper/ConPTY附属host计数令范围永久不空的设计。保留 TERMINALIZING / PHYSICALLY_JOINED / PRUNABLE 与已有物理未结算结果。
5. 显式 kill 与 Host close 由现有 owner 执行，Job kill/close后的集合与 reader 都要 join。无法完成时如实返回现有 `PHYSICAL_SETTLEMENT_INCOMPLETE`，保留资源占用及清理责任；不谎报 success 后允许下一轮无限泄漏 handles。
6. foreground cancel、dispatch、已写部分输入、scope结束与自然完成继续由现有 decision/side-effect owner 线性化。不在取消后启动新物理副作用，不重放，不能把观察等待取消当作输入回滚。

Node/Electron UI不接管终端业务；不为使用 node-pty 把 Python executor 整体迁入 Node。若经过 G0 证明某个小型 native helper 才能连接已有 maintained backend 的缺失创建/半关闭扩展点，helper 只拥有上述 OS handles 与字节操作，不解析模型命令或发明执行恢复协议。

### 5.3 Shell、命令通道、编码与退出码

Windows 固定使用应用随包的 **PowerShell 7.6 LTS**，由资源 recipe 锁定实际受支持补丁、x64官方资源及下载验证。私有绝对路径启动，明确 fail closed；不按用户 PATH/SHELL 自动切换 PowerShell5.1、cmd、Git Bash 或 WSL。随包引入包体、许可证和安全更新维护成本；它避免同时维护5.1的UTF16/codepage/BOM与7.x两套合同。LTS及补丁策略以[PowerShell 官方生命周期](https://learn.microsoft.com/en-us/powershell/scripting/install/powershell-support-lifecycle?view=powershell-7.6)为准。

保留现有 `command.maxLength=1048576` 字符这一单次输入合同。Windows CreateProcess 的命令行 UTF-16边界更小，不能把整个command拼进 `-Command` 或 `-EncodedCommand`，也不能悄悄缩小 provider schema。使用短固定bootstrap与 ACL保护的每调用UTF-8脚本载荷；命令载荷不走子进程stdin，stdin留给用户程序。脚本载荷仍是一次独立执行，不形成持久 shell；其文件仅作为不可复用的临时执行输入，由物理 owner按已读/执行结束/失败路径清理，不新增 durable receipt。[CreateProcessW 边界](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)

启动不载入执行 profile，先设置UTF-8输入/输出编码，再按PowerShell自身解析器执行原命令，不手写shell parser、不按POSIX shlex重写。**返回标准PowerShell7 `-Command`语义下的shell退出码**：显式`exit N`保留N；正常完成按最终成功/失败为0/1，最后原生命令非零默认可能被PowerShell映射为1。确实需要原生子程序的精确数值时，命令作者显式`exit $LASTEXITCODE`。不自行追加这个表达式，也不承诺额外的native numeric-preserving政策。[PowerShell启动与退出码](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh?view=powershell-7.6)

固定bootstrap无论command长短都须保持同一语义，不能短命令用-Command、长命令用-File后假0。G0覆盖`throw/trap/finally`、`return`、显式exit、pipeline、脚本/函数调用、最后cmdlet失败、失败后成功，不沿用旧`$LASTEXITCODE`误判本次状态。不全局设置ErrorActionPreference=Stop，不为所有PIPE/TTY调用加`-NonInteractive`而破坏Read-Host/长期输入，不修改机器ExecutionPolicy绕过企业策略；受策略阻断时给真实错误。

G0锁定满足这些语义的具体bootstrap/API。临时文件不能泄露实际配置凭据，日志不能打印有secret的环境；命令正文/输出仍按仓库诊断规则保留，不能广泛遮盖调试证据。

### 5.4 stdin、EOF 与 TTY 的平台差异

`write` 原样UTF-8，`submit`追加逻辑换行；ConPTY中逻辑Enter的实际CR/VT适配由physical adapter拥有，PIPE输入不擅自把任意文本改写成shell语法。原有1000ms输入后观察窗口与0–30000ms单次范围保留；回显或首字节不代表程序回答完毕。

PIPE 的 close_stdin 关闭写端，保留读端排空。POSIX PTY 当前发送EOT并关闭写副本，本来就不是任意 raw-mode程序的可靠EOF。**ConPTY close销毁会终止attached clients，不能用它冒充EOF，也不能把Ctrl-Z、Ctrl-D字节声称通用EOF。** 目标为独立关闭输入、保留输出/进程；G0必须验证所选API和代表程序确实支持。[ConPTY 关闭与排空要求](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)

若该半关闭在当前后端做不到，必须在全面实施前修订这一项产品合同：允许明确的TTY close_stdin unsupported typed outcome、指引需要EOF的程序采用PIPE等方案只能经规格修订采用；不可在实现中直接静默降级。未封闭前G0-T不通过。read/write满缓冲、写入取消、关闭与kill并发必须保持可取消与明确结算，不能持有ProcessRegistry全局锁等待同步OS I/O。

### 5.5 环境、开发工具与模型说明

`TerminalEnvironmentOwner`继续默认拒绝敏感/活跃变量，仅扩展Windows所需的SystemRoot、WINDIR、USERPROFILE、APPDATA、LOCALAPPDATA、TEMP/TMP、ProgramFiles、PATHEXT等经过分类的惰性项；不能直接继承完整桌面env。环境键按Windows不区分大小写规则去重，阻止Path/PATH双份绕过deny。PATH使用分号；工作目录附近虚拟环境查找 `.venv/Scripts`，不要求source激活，不污染bundled Python自身依赖。用户开发工具如Git、Node/npm、Python/uv、Rust仍取自用户显式安装，不把应用运行时当通用开发环境。

环境snapshot沿用一次有界、可取消、有精确失败原因的探测：Windows实现用受管理PS7 probe产生独立结构化结果，probe通道不混入用户profile噪声；只探测/显式加载与既有snapshot合同对应的环境配置，过滤secret与active项，不把函数/alias偷渡到后续独立shell。用Windows可用的pipe reader替换selectors；probe失败可按当前“无snapshot的净化基线”继续，不能换shell或无限挂起。TTL、缓存和probe单操作超时沿用现有owner，不加总调用/寿命边界。

provider环境只在获准epoch边界说明 Windows、PowerShell7、Windows路径、每次独立shell、`.venv/Scripts` 与可用私有工具；避免模型继续生成 `source`、`export`、`/tmp` 命令。正在运行的epoch不因用户安装Git/pwsh或PATH变化被重写。完成真实provider任务才能证明模型可用，单纯echo不能覆盖迁移。

权限分类仍由 `terminal_risk.py` / `tool_permission.py` 和executor门拥有；补充Windows敏感路径及PowerShell常见危险命令/别名与直接输入的同等检查，例如Remove-Item/rd/del针对卷根/用户根、磁盘格式化、重启、注册表破坏路径。复用PowerShell官方parser/AST的可用接口解决引号与语句结构；现有heuristic不是完整沙箱，不能承诺可静态判定任意动态PowerShell。保留read_only关闭terminal与各权限模式，不以启用ConPTY扩大授权。

## 6. 文件系统、路径、用户数据与本地安全

### 6.1 Home与身份

保留默认 `%USERPROFILE%/.pulsara` 和 `PULSARA_HOME` 的绝对路径解析；不擅自迁为AppData或创建第二套settings。Electron继续通过随包Python的同一resolver得到有效home。Windows registry/AppUserModelID/安装位置不成为用户业务数据authority。相对PULSARA_HOME明确拒绝；配置缺失、目录不可写、权限过宽分别报错，不覆盖原文档修复。

首批保证本地NTFS的home/workspace；home不能落网络share、junction或未知reparse链。用户明确选择含junction的项目时保留当前workspace resolve/availability合同：解析一次合法目标，记录真实canonical root，后续路径身份改变按UNAVAILABLE处理，不重绑定旧会话。不能为了支持Windows删除当前symlink/目录替换防护。

用真实handle返回的最终路径/卷语义建立Windows canonical path，保留显示路径与文件操作目标的区分。盘符大小写、`\\?\`前缀、分隔符等仅在平台path owner规范化；不要到处replace或对全路径lower/casefold。考虑NTFS目录可启用大小写敏感，不用字符串前缀判断containment；用handle/操作系统比较验证实际目标。现有workspace、memory domain、MCP/hook trust继续使用唯一canonical来源；POSIX稳定ID输出不变，不制造fingerprint->object注册表。handle文件身份用于当前安全比较，不另存新持久身份authority。

### 6.2 安全操作的最小平台接口

在 `local_source_binding` / `exclusive_publish` 原语边界提供Windows实现，调用者仍拥有settings、Skill、plugin、import、preview等产品语义。所需操作为：

| 操作 | 必须保留的语义 |
| --- | --- |
| 打开/建立绝对目录 | 逐级确认真实目录、拒绝未经允许的reparse/junction、不因检查后替换跟随到别处；保持已打开祖先的身份 |
| 打开已存在文件 | 普通文件检查、无意外重解析、实际handle与预期身份相符；读取长度与内容边界保留 |
| 创建临时文件/目录 | exclusive create、当前用户私有ACL、不可被其他用户提前植入；失败只清理本请求仍拥有的对象 |
| 发布 | 同卷、原子、无覆盖rename；并发发布最多一个胜者，不能先exists后rename；失败/commit unknown按原owner结算 |
| settings替换 | 允许替换精确的既有目标，写入/flush后原子发布；保留结果不明分支；不得把“无覆盖发布”和“替换设置”混为同一操作 |
| 锁/读取与删除 | Win32跨进程共享/独占锁和真实handle lifetime；Skill/plugin使用期间不能被后台删除，不能在fcntl不存在时直接当成功 |
| sealing与清理 | ACL替代仅POSIX权限位的假证明；文件共享/删除语义下只删除仍属本次输入的对象；打开文件导致sharing violation时保留具体失败 |

优先复用pywin32或标准库支持的Win32 API。实际已核查锁定pywin32 312的b312源码：`SetFileInformationByHandle(FileRenameInfo)`支持`ReplaceIfExists/RootDirectory/FileName`，`LockFileEx`支持锁操作；这是已有可验证的扩展点，不能先自写另一套rename/锁。若核心直接依赖pywin32，增加带win32 marker的直接依赖，不依赖MCP的偶然传递依赖。[pywin32 b312绑定](https://raw.githubusercontent.com/mhammond/pywin32/b312/win32/src/win32file.i)

G0要检查CreateFile/reparse flags、handle相对操作或等价的不可替换祖先策略、文件身份、ACL、rename/no-replace、FlushFileBuffers与目录发布的实际保证；单次OPEN_REPARSE_POINT不能证明所有祖先未被替换。如果API不能给某个POSIX fsync等价保证，写明平台持久性边界并经规格修订确认，不能直接忽略异常。[Windows reparse point 定义](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points)

全量迁移调用者：LocalSettingsStore、skill_import/local_skill_publisher/local_skill_removal、plugin package_store/local producer、hooks trust/source、file import/preview/HTML资源冻结、`tools/builtins/filesystem.py`的读写/replace/export/no-clobber及相关清理与锁。某一页能显示不证明背后写入安全链路可用。没有证据支持把这些模块全部搬到一套新storage framework。

### 6.3 路径与文件名

区分本机文件路径、HTTP URL、file URL、插件包内POSIX相对路径、ZIP/OOXML内部路径。包内路径继续POSIX并拒绝zip-slip；不能全局将posixpath改ntpath。原生路径支持盘符绝对路径；`C:relative`、单反斜杠的根相对`\x`、设备命名空间、ADS、NUL/CON等保留名、尾随点/空格及路径别名必须按用途校验，不能让字符串清理后悄悄指向另一个目标。超出OS/工具单次路径边界时报告具体resource/path outcome，不加任意总文件数/目录层数上限。

首批网络UNC明确typed unavailable，不把`file://server/share`交给系统引发意外网络认证。Windows文件URL由Node `pathToFileURL` / `fileURLToPath` 及Python标准库构造/解析，不沿用手工`file://`拼接。空格、中文、emoji、合法叶名字符`# % & ' ( )`、混合斜杠、盘符大小写、长路径必须贯通附件/markdown/preview/open/reveal/export；非法叶名字符`? * < > |`明确拒绝，URL query/fragment另按URL语法解析，不要求创建非法NTFS文件名。行尾读取/编辑保留既有CRLF/编码语义，不能因Windows强制整个文件换行或把UTF8字节游标改成字符偏移。

## 7. 随包PostgreSQL与本机服务

### 7.1 pg_search是发布前置条件

当前clean-v0创建`vector`和`pg_search VERSION '0.26.1'`，memory/history使用BM25索引、`paradedb.score`与snippet，向量仍为现有维度/距离合同。SQLite、Windows PostgreSQL裸安装、去掉搜索或只让聊天页打开都不满足目标。

官方当前提供的pg_search预编译平台未列Windows；固定v0.26.1使用pgrx0.19.3，而这一pgrx版本明确列Windows x86_64 tested。因此正确结论是**pg_search原生构建/分发尚未证实**，不能写“pgrx不支持Windows”或“换一个文件后缀即可”。其rustix/fs、OpenBLAS、Tantivy及PostgreSQL ABI链必须在G0实际编译/运行。[官方分发矩阵](https://www.paradedb.com/docs/operate/deploy/self-hosted/extension)、[固定依赖](https://raw.githubusercontent.com/paradedb/paradedb/v0.26.1/Cargo.toml)、[pgrx固定版本支持范围](https://raw.githubusercontent.com/pgcentralfoundation/pgrx/v0.19.3/README.md)

保留PG17.9、vector0.8.1、pg_search0.26.1目标组合，使用官方Windows构建工具/maintained packaging能力，不复制PostgreSQL构建系统。recipe记录MSVC/SDK/LLVM/Rust/OpenBLAS/CRT及实际DLL依赖，校验PE架构、许可证和搬移后extension目录。只在G0证据表明必须升级组件时单独修订本规格和clean-v0验证；不默默升级pgrx/扩展掩盖成本。若需要长期维护pg_search Windows fork，成本另行估算，本规格的人日估算不把它当普通移植吸收。

### 7.2 Windows数据库端点与认证

Windows桌面专用PG使用 **127.0.0.1 TCP + SCRAM**，不使用当前Unix socket目录，也不把`--auth-local=trust`照搬成TCP trust。Node PG owner、`desktop_runtime.validate_managed_postgres`与`storage/postgres_endpoint.py`的typed解析/允许字段一起hard cut：Windows managed DSN固定`require_auth=scram-sha-256`，客户端也拒绝trust、MD5或请求明文密码的伪服务，不仅修改server hba。G0核实随包libpq支持并测试错误认证挑战；缺能力不能删除此要求。数据目录/home持有当前用户受限ACL；仅精确loopback地址、专用数据库和固定admin/runtime角色可接受。runtime仍无superuser/createdb/createrole/replication/bypassrls权限。[libpq认证约束](https://www.postgresql.org/docs/17/libpq-connect.html#LIBPQ-CONNECT-REQUIRE-AUTH)

数据库管理凭据是此新TCP认证边界独立必需的secret：在现有`BundledPostgres` owner内使用私有`desktop/postgres-auth.json`，仅含格式版本、canonical专用data-dir和admin/runtime两份随机密码。它不是用户可编辑DSN来源、execution checkpoint或第二settings truth；以第6节安全文件原语和当前用户受限ACL创建/读取。密码不进命令行、普通日志或终端环境；实际值加入现有secret scrub。UI可展示DSN地址/角色，精确隐藏密码。

凭据文件与PostgreSQL DDL不跨系统假装原子，冻结以下顺序与失败规则：

1. 真正首次初始化，先exclusive创建、flush并原子发布auth文件，随后用同一admin密码经`initdb --pwfile`的私有临时文件初始化。临时pwfile只归此次owner，所有成功/失败路径清理，不把密码文本拼进argv。既有auth文件不覆盖；已有非空未知data-dir拒绝初始化。
2. 有`PG_VERSION`时只读取并使用已绑定auth文件；文件缺失/损坏/ACL不合格、data-dir不匹配或认证失败均明确失败。不得生成新密码、自动ALTER PASSWORD、尝试trust启动或自动删库。
3. auth已发布但initdb失败时保留auth及失败目录，报告真实错误；只有确认为本owner的空目标可按同一材料重新调用initdb，非空未完成目录仍拒绝，需用户明确reset。admin已初始化但runtime角色创建中断时，重开不构造修复图；正常role校验失败并显示明确reset路径。schema级重试只走已有migration owner的既有合同。
4. 本轮不提供自动密码轮换。明确确认的物理reset先关闭精确owner、停止已验证专用PG、删除其data-dir，确认删除成功后再删除绑定auth；失败或结果不明保留尚存auth与具体状态，不先删凭据令旧cluster无法打开，不自动启动新cluster。auth残留而data已空的后续显式初始化复用原材料即可；不新增receipt/recovery job。
5. G0-D和最终测试逐个注入上述文件发布、initdb、角色创建、迁移、重开、reset删除失败点，证明没有自动清库/换密码/连错误实例。新secret文件是认证必要输入，不为证明初始化成功另存持久状态。

随机可用loopback端口在每次成功start的控制帧携带，供Python单一settings composition注入。`BundledPostgres`负责物理实例/auth材料/确认后的data-dir reset；`desktop_runtime.prepare_database`负责认证后的角色与database首次创建/校验；`PostgresMigrationRunner`继续负责schema、grants与clean-v0，不能把它们揉成新的DB manager。端口本身不证明身份：复用现有实例必须确认精确data-dir、实际server版本/角色/数据库、由该owner控制的认证材料及启动目标；端口抢占或实例不符失败，不尝试连任意本机PG。残存postmaster.pid和PID均不能独立证明存活/归属。

只启用所需loopback监听，hba拒绝其他地址/角色；不监听0.0.0.0、不要求Windows防火墙开放入站、不安装系统数据库服务或计划任务。PG负责崩溃恢复；明确用户确认前不清库、不删未知锁、不从启动失败自动reset。

### 7.3 启动、退出与锁

继续 Electron单实例 → Python旧owner释放 → PG就绪/role/schema → Python application ready → 工作台的顺序。应用单实例只约束GUI，Python仍需Windows OS lifetime kernel lock（精确home、当前用户ACL、跨进程互斥）；不写租约、generation或持久进程登记。CLI数据诊断改为当前用户SID DACL且local-only的named pipe客户端/服务端，使用PIPE_REJECT_REMOTE_CLIENTS等平台能力拒绝远程client；endpoint由同一canonical home确定并验证服务器owner，避免多home串连或管道抢占。复用现有有限诊断消息，不给任意进程提供reset/执行接口，不建持久endpoint注册表。

内部Protocol-v3在Windows统一改用 **127.0.0.1上OS分配的临时端口 + 既有launch_id/launch_capability握手**，POSIX继续Unix socket。`v3_gateway`与`protocol_client`共同切换transport接缝，不改protobuf帧、大小边界、角色、attachment generation、detach/revoke和settlement。端点仅在原Python owner之间传递，launch capability不进入renderer、URL、日志或磁盘，不新增token注册表。

这个client/server同在原Python进程边界，必须绑定精确listener lifetime：Windows socket先用SO_EXCLUSIVEADDRUSE建立独占绑定，再交asyncio现有stream server；新connect/hello与server.close的admission/撤销线性化，close先禁止新connect并取消在途连接，已接纳连接/hello结束或取消前不释放listener。close后的endpoint不可重新连接或重用；不能向已释放端口的未知新listener发送launch capability。G0验证抢占/重绑定与关闭竞态，不以端口号充当server authenticity。未知/旧capability、非loopback、未握手业务帧和连接取消均须验证拒绝。[Windows独占socket能力](https://learn.microsoft.com/en-us/windows/win32/winsock/using-so-reuseaddr-and-so-exclusiveaddruse)

诊断IPC没有该既有握手，所以仍按本节独立采用SID DACL named pipe，不增未鉴权TCP诊断服务；检查管道server实际用户与绑定home，first-instance创建/占用冲突明确失败，不连接抢占管道的其他用户进程。它与Protocol-v3、Electron宿主JSONL是三条用途不同的通道，不能合并成新的RPC框架。

Python的stdin/stdout JSONL仍是宿主控制/状态通道，wire固定为**UTF-8无BOM**，按字节管道接收并增量解码，不受Windows text换行/系统代码页转换；普通日志独立stderr并明确UTF-8编码。Node spawn产生的继承匿名stdin不因为Proactor支持subprocess就自动成为可overlapped读取的handle。G0-R必须测原Node→bundled Python链的中文/emoji路径和错误、跨chunk拆分UTF-8、shutdown、EOF、broken pipe、pre-ready失败及崩溃收尾；实现先复用维护库可用的Windows pipe seam，必要时采用受控reader线程，线程/管道仍须在关闭时join。不改系统codepage、不缩为ASCII、不吞乱码、不使用命令shell包裹。不能因为Unix asyncio不支持就把整个应用loop切Selector；CPython明确Windows Proactor支持subprocess，而若干Unix server/signal和Selector pipe API不支持。[Python平台文档](https://docs.python.org/3.12/library/asyncio-platforms.html)

正常退出：停止接纳 → Python原owner取消/结算并收尾terminal/MCP/hooks/children → 等child close → PG fast stop → Electron退出。UI显示未知/收尾失败，不设置一个新的任意9秒/30秒总超时自动强杀。宿主管道EOF让Python进入原关闭路径；主进程崩溃时的Job kill-on-close可用于受管理内核树的物理兜底，但它不是canonical成功结算或跨重启执行恢复。

Windows注销、关机、任务管理器强杀、断电可能不给足关闭时间；明确属于OS强制中断。重开只走现有canonical状态和PG恢复，不自动重放工具、不恢复进程/monitor、不用“退出成功”掩盖settlement unknown。不得通过拦截用户注销无限等待任务自然成功。

## 8. MCP、hooks、Skills、插件与外部工具

MCP继续由当前SDK和 `sdk_facade.py` / supervisor拥有，不因为Windows另写MCP协议栈。核查锁定mcp2.1.0 / AnyIO实际Windows进程helpers：可复用的CreateProcess/Job/termination能力只复用到它实际保证的层级。启动后Assign或Job失败后退为普通Popen不能满足需要创建前纳管的地方；必须消除该失败降级或在owner明确返回失败，不能扩散到终端。

现有有界stdio还负责单帧大小/嵌套、反序列化、secret mutation临界区和取消收尾；SDK stdio_client如果没有这些注入点，不能为了“用SDK”删除这些产品保护。保留Pulsara所需窄transport适配，并在代码旁记录实际缺失接口，不复制SDK完整parser/lifecycle。HTTP/SSE/streamable HTTP、OAuth继续使用SDK网络/认证路径。

MCP stdio自己的env owner补齐经分类的Windows必要惰性baseline（SystemRoot、TEMP、USERPROFILE、PATHEXT等以G0真实依赖记录为准），保留声明的lookup_path、secret_env精确resolver与最后spawn临界区的credential_guard；不以TerminalEnvironmentOwner的继承策略代替它。hooks保留原overlay/实际secret scrub策略，核实COMSPEC、编码和key唯一化。可共享Windows大小写key规范化小函数，不合并业务继承policy；Path/PATH双份不得覆盖已审查值，不能直接全量继承process.env。G0-R覆盖依赖SystemRoot的真实子程序、cmd/Unicode argv及动态credential mutation。

Windows executable解析覆盖 `.exe/.com/.cmd/.bat`、PATHEXT与带空格绝对路径；`.cmd`不是可直接当exe的POSIXargv。在明确需要cmd wrapper的MCP/hook配置上使用唯一的Windows launcher/escaping规则，校验用户配置的确切参数，不能把任意数组join成shell字符串；不偷偷改成终端默认pwsh。拒绝不支持的配置时保留原配置并给具体指导。

hooks已有`commandWindows`选择与Windows分支，必须验证在生产执行owner上真正闭合：子树取消、stdin/JSON输入、超时、输出边界、动态credential scrub、source trust、cwd=workspace、用户可见结果；`pragma:no cover`不算支持证明。没有commandWindows的POSIX-only脚本不自动翻译，给配置/依赖错误，不假成功。

Skill/plugin安装、使用借用、删除、发布与trust必须使用第6节同一安全原语；不可因Windows没有fcntl跳过锁。源执行属性与实际launch admission分开：POSIX精确保留/比较原执行位；Windows的process-local frozen executable/executable_bits采用明确不适用值，不用st_mode&0111、X_OK、shebang或伪造全True/全False代替权限。按同一平台owner冻结影响已接受源的真实属性/ACL观察并校验copy后的一致性，不增加包格式/持久身份/fingerprint，POSIX既有canonical输出不变。

Windows包内`./x.exe/.com`须指向已冻结普通文件，交唯一native launcher按OS真实执行；`./x.cmd/.bat`只用本节明确cmd wrapper；ps1/py/sh等须配置显式解释器与args，不按后缀/shebang自动选择pwsh或翻译脚本。hook commandWindows仍拥有其命令文本。G0-F必须证明每类允许/拒绝、真实argv、无安全路径绕过与源属性变化后拒绝；未闭合仍为G0未通过。ACL sealing仅保护安装内容，不能代替可执行性/配置授权。

`.venv/Scripts`、python/uv调用示例、bundle路径逐个校验。包内相对路径继续标准POSIX；不为Windows增第二套bundle身份。bundled data/docs/pdf/sheets脚本的Python、LibreOffice/Poppler/字体等可选工具，分别说明随包与用户依赖；缺失时给可操作提示，不扩大“安装后零依赖”承诺。

私有rg已有rg.exe路径和下载目标，仍须贯通Hatch target/PE验证、wheel inclusion、mise输出与安装后运行；不能改为在PATH找rg。Playwright负责Chromium版本/可执行目录，验证Windows搬移、截图/HTML播放、字体和浏览器子进程关闭；不能让打包机器的绝对cache路径进入产物。

## 9. 前端、Electron与用户流程

业务HTTP、流式观察、分页、markdown/HTML播放、任务和设置owners保留；适配宿主与路径，不另造Windows renderer。

| 用户流程 | 必须修改与验证 |
| --- | --- |
| 窗口 | desktop-host/platform扩为win32；使用Electron hidden titlebar + titleBarOverlay提供Windows原生控制，验证拖动/非拖动区域、系统菜单/snap、DPI缩放、多屏、最大化还原；不把Linux专属判断当“所有非macOS” |
| 后台/退出 | 扩展现有tray owner到Windows，关闭时驻留可从tray恢复；tray创建失败则显示可操作窗口/最小化或退出选择，不hide成无入口。保留后台工作提示、明确退出和OS session-end；不关闭窗口即强杀任务 |
| 输入 | Ctrl/Cmd平台快捷键、AltGr、中文IME组合输入、Enter/Shift-Enter、搜索/复制粘贴、焦点；不让Windows Ctrl操作触发macOS-only逻辑 |
| 选择/拖放 | Electron dialog、webUtils.getPathForFile等真实来源产生本机路径；prompt草稿、chip、提交、后端读文件同一目标；拖入文件不能路径字符串被冒充或改成copy而不说明 |
| 附件/本地链接 | 盘符、反斜杠、file URL、markdown相对路径、目录、export/save-as、open/reveal完整闭环；删掉各处startsWith('/')作为绝对路径的判定 |
| 工作区 | 创建/选择、missing/unavailable、recent sessions、路径补全、CLI cwd、transient workspace；大小写/删除/目录改名不悄悄改绑定 |
| Browser/HTML | 原browser owner、导航/窗口创建策略、原生文件URL与本地资源freeze、来源隔离、截图；网页与HTML不得获得preload/Node/本地执行权限 |
| 设置 | 无活动session时仍可读/改用户级设置、Skills/MCP/hooks/模型/CLI状态；不得依赖active session给Windows页授权或刷新 |
| OAuth | HTTP loopback、系统浏览器打开、pulsara自定义协议/第二实例冷暖启动focus、URL编码与失败/取消；custom URI继续只focus，不从argv接受code/grant。OAuth state/授权仍归原loopback owner；不把协议URL交给shell求值 |
| 用户体验 | Windows字体回退/emoji、滚动条、PDF/image展示、系统文件打开失败、保留具体诊断；图标启动不闪控制台，不要求管理员/终端窗口保持开启 |

本地path识别移到明确的宿主/后端path owner，frontend接收可信平台路径信息和受限操作；不为前端方便开放任意filesystem IPC。所有preload IPC继续校验sender，browser窗口/嵌入文档不能调用宿主能力。路径显示字符串不是读取授权，原read_token和preview owner仍负责授权与生命周期。

## 10. 打包、开发环境、CLI和发布

沿用uv拥有repo `.venv` 与locked依赖；开发命令用 `uv run --no-sync python ...` 或平台选出的repo `.venv/Scripts/python.exe`。将mise、npm scripts、测试fixtures/命令与CI里的`.venv/bin/python`、node_modules/.bin的POSIX路径改为平台可执行形式；不要给开发者另发system Python路径当兼容方案。前端构建通常平台无关，资源与安装验证必须在目标Windows上完成。

| 资源/交付 | 实施要求 |
| --- | --- |
| 私有Python | CPython3.12.15 x64（与当前pin一致）、uv锁定依赖、可搬移stdlib/site-packages/pyc；实际root布局集中在resource resolver，原Windows模块import通过；不能猜/bin换/Scripts后所有路径自动可用 |
| native wheels | 核查uv.lock与实际Windows安装，RE2、Pillow、psycopg-binary、cryptography、Playwright等DLL都要搬移验证；x64 wheel存在不等于ARM64可用 |
| wheel/rg | `_WHEEL_TARGETS`增加win_amd64、PE架构/资源校验、rg.exe与许可证；不打一个含Windows exe的“纯py-any”wheel掩盖平台属性 |
| PG与PS | 见G0及第7节；构建recipe分别管理PG/扩展/PowerShell资源、官方内容校验与许可证；没有运行时静默下载最新依赖 |
| electron-builder | Windows x64 NSIS per-user installer、ico资源、protocol注册、Start菜单入口、windowsHide；默认不提权，不增加系统服务，不把shell脚本放到Windows当CLI |
| CLI | 安装目录中的小型原生console launcher转发既有Python CLI，正确保留argv、Unicode、stdio、exit code与Ctrl-C；`pulsara app`交付到同一GUI单实例后返回。可选每用户入口放用户私有目录；不自动改PATH、不覆盖同名外部命令；设置页如实显示安装/冲突/PATH状态 |
| 生命周期 | update/卸载前先由用户正常Quit并完成同home/相关资源owner收尾；发现仍运行/结算或安装资源占用，只等待用户正常退出、取消或明确失败，不能强杀已知或未知进程后继续覆盖、删数据库或把手动更新升级为自动更新产品 |
| 签名 | 内测可显式标注unsigned；面向普通用户分发前完成Windows代码签名/时间戳和原生安装验证。签名不保证SmartScreen立即无提示；CI secret不进资源包/日志 |
| CI/发布 | 新增windows原生backend/资源/安装任务，产物名固定为`Pulsara-${version}-windows-x64.exe`；同步release_artifacts必需集合、版本检查、上传模式、README/INSTALL/DEVELOPMENT，不破坏现有DMG/deb |

CLI不能仅用`.cmd`的`%*`拼接声称所有引号/元字符无损，不能让GUI exe启动后的默认返回0冒充管理命令退出码。先读electron-builder/Windows console launcher已有维护能力，只有其无法表达的stdio/argv缺口才实现小型转发器。CLI是可选管理入口，不是普通用户启动GUI前置条件。

已核查electron-builder26.15.3自带NSIS的`allowOnlyOneInstallerInstance.nsh`：默认CHECK_APP_RUNNING可Stop-Process并强杀，安装/卸载都会调用。**必须采用其现有customCheckAppRunning扩展点改为上述不强杀的退出/失败门**，不能保留默认kill行为，也不复制整套NSIS。真实安装器覆盖“已知main退出但Python/PG/Chromium仍占用资源”、正在收尾、用户取消、完全退出后重装/卸载；不以“只杀已知进程”当作满足结算。

保持mise任务图/outputs/cache新鲜度；Windows `.exe/.dll/.cmd`输出由目标声明，下载、SDK、native recipe变更在原资源缓存边界失效。新平台资源单独按平台/架构隔离，不能把macOS缓存误判成功；不新增指纹注册表、artifact恢复系统或按逐文件SHA证明工作树状态。

## 11. 实施工作包、成本与顺序

以下是资深工程师熟悉仓库后的人日估算，包含实现、聚焦测试与review；不是计费承诺，也不是agent运行时长。G0尚无Windows证据，置信度低到中。工期不能从“命中多少Windows字符串”推算。

| 包 | 内容与完成条件 | 初步人日 |
| --- | --- | ---: |
| W0 | G0-D/T/F/R原生spike、依赖API检查、证据与方案封闭 | 8–15 |
| W1 | physical adapter、PIPE/ConPTY、创建前Job、取消/join、输入/输出/monitor；共享窄机制接入MCP/hooks/截图worker | 18–30 |
| W2 | PS7命令通道/exit/编码、environment snapshot、venv/PATH、风险门与provider说明 | 8–14 |
| W3 | NTFS handle/ACL/锁/发布原语；settings/Skills/plugins/import/preview等消费者整体迁移 | 15–25 |
| W4 | 原生PG/扩展recipe、TCP认证、精确实例验证、Protocol-v3/diagnostics、lock/host control | 12–22 |
| W5 | Python/Chromium/rg/PS资源、Hatch/mise、NSIS/CLI/签名与release矩阵 | 10–18 |
| W6 | 前端/Electron路径、窗口/快捷键/tray、附件/预览/browser/OAuth闭环 | 7–12 |
| W7 | 原生整体验收、故障/长程/真实provider dogfood、三平台回归、文档 | 10–18 |
| **合计** | G0通过且不需长期维护上游fork的前提 | **88–154人日** |

另留约25%–35%不确定性预算，规划量级约 **110–208人日**。G0失败、pg_search/pgrx/ConPTY库需长期fork或新增平台产品合同，不在这个区间内硬吸收，应重估。2–3位有平台经验的工程师可并行部分工作，预计约12–22周量级；单人按总人日和依赖顺序排期，不能简单除以agent数。签名采购/CI硬件/Windows测试机、用户工具安装排障、安装包体积/下载时间与发布排队属于额外资源和日历成本。

工程顺序改为11.2的Phase A独立交付→W0全部通过→11.1的Phase B接入；W0研究可与A并行。A从W1–W5/W7中提前完成共用/POSIX抽取与相应回归，不另加一整套重构预算。B的W1/W2、W3、W4仍是关键路径，可分工但共享接口先闭合；W5部分准备与W6纯展示可并行，真实路径闭环依赖W3；W7不能被局部绿测替代。每个切片完整hard cut并保留当时已支持平台的有效路径；不得为短期演示留不安全compatibility branch。持续维护预算包括每次OS/Electron/CPython/PowerShell/PG/扩展升级的Windows回归、漏洞更新、符号/崩溃诊断与签名，不只首次开发。

### 11.1 Phase B：G0通过后的Windows接入顺序

下表是A验收之后W1–W7剩余Windows工作的依赖顺序和review边界，不新增运行时阶段状态、持久job或一套重复收费预算；本轮仍未实施A或执行G0。A可独立合并/交付，不再要求POSIX抽取与Windows实现留在同一未发布分支。B以A唯一有效路径为基线；每个新切片同样接齐消费者并删除被替代路径，不保留旧fd/新binding双签名或兼容别名。

| 顺序/工作包 | 具体改动位置与动作 | 进入后续步骤前的完成条件 |
| --- | --- | --- |
| S0 / W0收尾 | 将已通过G0的实际SDK/binding、资源布局、半关闭/flush/borrow/取消语义写入受影响recipe/adapter；确认3.1契约只含真实需要的操作 | 原生证据符合第4节且无未封闭产品语义；不把output探针加入生产依赖，不先搭空平台框架 |
| S1 / W1–W5接口复核 | 依据G0逐项核对A的真实契约；只为Windows的已证实缺口修改私有签名/补窄操作，同批更新全部POSIX调用者与测试；建立实际被消费的Windowsleaf及必要Python资源locator | A接口不是外部冻结SDK，也不等于Windows证明；无重复POSIX抽取或空框架，唯一可用POSIX路径保持；Windows入口完整import闭合须等S2–S5 |
| S2 / W3安全文件全链路 | 在A的typed binding/所有caller上接WinNTFS实现；按G0落实Windows metadata/执行语义，不再重做一轮fd迁移；如契约确需改动则同批hard cut | int-fd/HANDLE混用、共享flags、无锁降级均消失；settings、Skill/plugin、工具文件读写、preview HTTP read-at都在真实平台验证；不以“核心原语可跑”算完成 |
| S3 / W1–W2进程与shell | 在A的scope、physical、shell及同步/异步caller上接Win Job/PIPE/ConPTY/PS载荷与环境实现；保持原Registry/guard/业务policy | Registry/output/monitor/业务state共用；创建前Job、guard跨真实spawn、取消rollback、root/range/drain/join成立；无直接root-only终止或未纳管fallback。同步/异步caller不互占业务slots |
| S4 / W4内部服务与DB | `DesktopApplication`、`v3_gateway/protocol_client`一起接typed listener；diagnostics server/CLI与kernel lock一起接平台通道；Node JSONL和Python字节reader一起改；`postgres*.ts`、安全文件接缝、`prepare_database`、`postgres_endpoint`与资源recipe一起完成SCRAM/auth/实例验证 | 三条IPC各有原owner；真实Node pipe/UTF8/EOF、未授权client、精确home锁、client/server SCRAM、auth失败/reset顺序与既有schema verifier通过；不能TCP trust先跑再后补认证 |
| S5 / W4–W6同一应用闭环 | `main.ts`用locator装配原`PythonRuntime`/`BundledPostgres`；原boot/reset/requestQuit调用新leaf，不复制流程。接preload与frontend desktop-host/path/附件/preview/titlebar/快捷键/tray/OAuth | 原生图标启动→DB/kernel ready→会话/文件/工具→交付→正常quit/重开成立，用户级页无session也正常；前端不造可执行路径或在后端不支持时显示成功。W6纯样式可提前并行 |
| S6 / W5构建与安装 | `prepare_resources.py`保留唯一编排入口；确有不同native recipe时拆`desktop/scripts/resource_build/{common,macos,linux,windows}.py`叶子。接Hatch/rg、mise输出、版本/许可证/PE-DLL、Chromium/PS/Python；原smoke执行重定位；再接CLI/NSIS/签名 | 静态NSIS扩展放tracked `desktop/scripts/windows-installer.nsh`并由package.json引用，不能放ignored `desktop/build/`；依赖/应用wheel/随包资源搬移后通过，默认强杀宏已替换；真实已安装CLI/GUI与更新/卸载保数据验证通过 |
| S7 / W7激活 | 在既有workflow新增Windows native job/测试，接相同build与release owner；`release_artifacts.py`增加exe精确集合，更新INSTALL/DEVELOPMENT/README；完成第13节真实provider/长程与三平台回归 | 全部激活条件成立，证据区分G0/单元/资源/installed GUI；没有批量skip、mock OS充证据或新的总寿命caps；仅到此可声明已支持Windows |

S2与S3可按owner并行；S4依赖它们实际使用的安全文件/进程/资源接缝，S5的真实文件工作闭环依赖S2。S6的recipe/图标/CLI转发器准备可提前，最终安装资格依赖S4/S5。CI可从第一条可运行切片接入并持续检查，S7表示全矩阵收口，不要求等最后才开始CI。不存在“先让界面启动，后补权限/物理结算”的发布阶段。

若S6拆出`resource_build/**/*.py`、新增Windows NSIS include或CLI native source，必须同步把它们加入相应既有mise任务的sources/outputs或对应构建输入，并扩充`desktop/scripts/test_build_cache.py`验证真实leaf变更使受影响产物失效；不能只追踪旧入口文件而让热缓存假命中。继续由mise/uv/electron-builder各自拥有新鲜度与产物，不新增缓存authority或源码hash注册表。

每个工作包review同时检查：共同状态机是否仍唯一、选择点是否集中、平台专属imports是否隔离、全部持有对象/异常/取消close是否接齐、原生失败是否如实返回、旧实现是否已删除。模块拆分本身已包含在88–154人日预算；若G0证明需要额外native helper/上游fork或产品语义改变，按原规则修订和重估，不把大型平台重构夹进此次移植。

### 11.2 Phase A：先交付Linux/macOS接口抽取与回归

#### 11.2.1 目标、交付边界与不做项

A的目标是把已经存在的POSIX机制从产品owner中移出，让Linux/macOS使用一份明确的资源接口。**可独立review、合并和继续交付当前POSIX产品，无须等待Windows G0。** 这是实施顺序，不新增数据库phase字段、产品状态机或持久任务。A完成的表述为“POSIX接口抽取及回归通过”，不表述为“跨平台已完成”。本轮只写此计划，尚未开始A实现。

A必选范围是安全文件binding全消费者、受管进程范围及terminal字节端点、POSIX shell/probe接缝、现有异步进程消费者，以及Node纯资源定位。调用这些接口的业务owner仍共用，行为与当前基线保持一致。没有共同的“操作系统服务对象”，不把文件、终端、数据库、IPC塞进一个PlatformService。

A不创建Windows实现/stub、Job参数占位、ConPTY假半关闭、可选能力列表、PS载荷文件、NTFS权限策略、Windows认证文件、TCP/SCRAM切换、NSIS/Windows CLI或win32前端。既有POSIX PG/IPC、工具名/描述/SYSTEM、独立shell行为、权限、输出、取消、副作用结算、canonical ID、schema、事件/关系oracle与现有物理边界不变。不为了接口迁移添加hash、receipt、全局孤儿进程registry、恢复工作流或总运行寿命cap。

**接口完成一个领域就同批接齐该领域全部消费者和资源持有者，删除原raw执行路径；不能接口先合、旧调用者长期保留。** 不同领域可分开交付，非终端caller也可逐个成片迁移；未触及的领域继续由原owner拥有，不等于新旧双轨。A的接口表达已经成立的行为/所有权，不是对外发布的稳定SDK；G0后可为证实的Windows缺口一次性hard cut私有签名和全部caller，不留兼容别名。若改变产品语义仍先修订本规格。

#### 11.2.2 最小文件binding与现有业务的边界

`local_source_binding.py`定义封闭typed入口与owned `DirectoryBinding` / `FileBinding`；`_local_source_posix.py`持有fd并实现当前stdlib/flock/平台rename原语。对象只能由真实打开/创建或受控借用产生；诊断路径不具有再次打开、删除或确认对象身份的authority。类型名与方法名可按实现简化，但下列现有能力和所有权必须闭合，不照抄整个`os` API。

| 接口操作族 | A必须提供的现有能力 | 继续归业务owner的内容 |
| --- | --- | --- |
| 目录取得/遍历 | 绝对目录逐级no-follow打开/按现有规则创建；从绑定目录打开相对子目录、普通文件、枚举和观察子项；名称/相对路径规则明确，不允许路径逃逸 | home/workspace/Skill/package根选择、信任/授权、deadline、允许读写的范围和包内POSIX路径规则 |
| 文件读取与观察 | bounded read、`read_at(offset, count)`、typed metadata、受控独立读取/文本流；实际普通文件/目录分类、原权限检查所需观察 | 每项现有大小/编码/内容限制、MIME/preview分类、冻结值与变化拒绝规则 |
| 创建/写入/持久化 | 原exclusive create、读写/flush/file fsync、私有/冻结权限操作，以及目录fsync；短写与异常原样可结算 | YAML/包内容、何时发布、提交前后结果、重试与清理决定；不在leaf中返回万能commit receipt |
| 锁与借用 | 原共享/独占锁、nonblocking尝试、受控duplicate/borrow、显式close；一次借用关闭不释放其他存续借用/原锁，借用不能按路径重新取得制造空窗 | lock重试的原deadline/取消、package in-use、安装/删除/信任与物理借用结束时机 |
| 三种不同发布 | 目录rename-no-replace仍经`exclusive_publish.publish(binding, staging, final)`唯一入口；settings的原同父目录原子replace；artifact/export已有hard-link no-clobber | 三种操作不互换、不统一成exists+rename，不改变冲突/失败/unknown及用户可见成功条件 |
| 清理/释放 | 依绑定对象完成原相对删除与关闭；真实失败上报，重复close幂等；构造中途失败也释放已取得资源 | 是否允许删除、删除哪些项、替换检测、清理错误如何影响该产品结果；不能擅自按旧路径递归删新目标 |

具体操作候选：目录`metadata/names/inspect/open_directory/open_regular/create_regular/mkdir/replace_entry/link_file_noreplace/unlink/rmdir/sync/duplicate/close`；文件`metadata/read/read_at/write/reader/sync/duplicate/close`；锁`try_lock`或原所需blocking acquire、`duplicate/close`。权限操作A可为明确的`set_posix_mode(mode)`，metadata保留真实mode及owner_uid；不预造通用ACL策略。访问模式仅含已有READ/WRITE/READ_WRITE，不透传任意OS flags。最终名称可简化，未被真实caller使用的方法不创建。

相对操作首先使用绑定父目录+单个叶名；现有多级访问逐级绑定，不把`..`或分隔符交给叶名操作，不额外收紧POSIX合法文件名/隐藏项规则。枚举不替owner筛隐藏项/排序，不加总成员数cap。regular打开保持现有非阻塞+no-follow后分类，防止FIFO等特殊项令原有限读取卡住。`mkdir`不谎称已原子绑定新目录，`unlink/rmdir`也不谎称具备系统未提供的atomic compare-identity-delete；原owner前后检查/错误仲裁保留，不借接口声称消除了所有namespace竞态。

read-at保留`pread`的独立位置语义，不用无锁共享seek/read模拟。现有`fdopen`/`dup`文本迭代改为受控stream借用，保留UTF-8-sig、换行、seek与预算行为；不能为接第三方库重新公开通用`fileno()`、`descriptor:int`或在共享层偷偷unwrap。确有库必须用fd时，把最小适配留在POSIX实现内，外部仍持封闭资源。`PhysicalLifetimeAnchor`继续持有实际锁；锁/文件借用不是新增产品lease，也不新增全局引用注册表。

duplicate保留同一已打开对象，POSIX dup的顺序offset可共享，不宣称它天然产生独立cursor；独立位置由read-at及各受控reader的现有合同保证。parent close后已取得的借用可按原合同结束；inline preview revoke与显式download可继续完成的区别仍归token/preview owner。已开始的`to_thread`/异步读取须cancel-safe join后才释放其borrow。wrapper关闭先撤销自己持有，再做一次实际close；close不明时不能拿同一可能复用的整数fd反复重试。保留原最终close失败的published事实并尝试释放其余持有。sync只执行真实物理同步；各owner原来对ENOTSUP/EINVAL等的可容忍政策不互相推广，settings严格父fsync不能被通用helper弱化。

锁轮询始终作用于同一已打开锁对象，保持原绝对deadline/取消及成功后再次检查abort的cut。anchor duplicate沿用同一open-file-description的锁持有，某借用close不能主动LOCK_UN导致其他借用失锁；最后持有结束才释放相应lifetime锁。独占临界区仍按原owner时机结束，不将全部锁一律改成anchor策略。`desktop_runtime.acquire_kernel_lock()`及run/wait-owner/清理同批改为owned锁，等待旧owner不启动settings/PG/app；已迁移hook信任锁不再保留fcntl缺失就yield的无锁分支。

**metadata观察不等于统一业务相等性。** A保留POSIX原dev/ino/size/mtime_ns/ctime_ns/mode/执行位等完整typed观察，但每个owner继续使用自己的原身份谓词：preview主要比较dev/ino/size/mtime；plugin冻结含ctime和executable；部分Skill根/删除只比较dev/ino；settings还检查private mode、regular与size；import保留其原samestat规则。不能全改成metadata dataclass的全字段`==`，不能用新hash代替，也不能为未来Windows填0/None而改变现有canonical bytes。G0-F决定Windows观察/执行语义后，B再封闭其必要差异。

settings写入仍由`settings.py`拥有“临时文件写完/flush/fsync→同父replace→父目录fsync”的提交边界；replace之前失败与replace之后父目录fsync失败继续区分，后者保留现行commit-unknown，不降为普通未写、不自动重发。Darwin `renameatx_np(RENAME_EXCL)`与Linux `renameat2(RENAME_NOREPLACE)`继续为独占目录发布真实实现；缺原语仍失败，不添加普通rename fallback。原有POSIX系统alias、路径规范化、权限模式、锁超时/取消策略不在A顺便改写。

| 成组切片 | 必须接齐的生产位置（相对`src/pulsara_agent/`） | 特别检查的持有/间接消费者 |
| --- | --- | --- |
| 设置/Skill | `settings.py`；`capability/{local_skills,bundled_skills,skill_import,local_skill_management,local_skill_publisher,local_skill_removal}.py` | held Skill/root binding、publisher的独占发布、删除前后身份、失败清理和close；同一typed接口变化不能留下int消费者 |
| plugin/hooks来源 | `plugins/{package_core,package_store,skill_producer,source_import}.py`；`hooks/{source,trust}.py` | `HeldPluginPackageObservation`、`PhysicalLifetimeAnchor`、`source_import`经package_core借用、flock共享/独占及abort后cleanup |
| 文件/预览/工具 | `desktop_app/{file_import,file_preview,file_preview_http,path_completion}.py`；`tools/builtins/filesystem.py`及相关HTML/导出owner | Preview保存槽、HTTP的dup/fstat/pread、文本流和bounded read；仅实际依赖binding的路径接入，不重写全部普通路径/文件产品 |

实施前必须从这些owner追踪真实直接/间接调用闭包，补齐同对象的所有持有DTO/cleanup。为了保持每片可用，若两个消费者共享同一被改签名/held类型，就合为一个切片；不得用暂时暴露fd维持小提交。清单是已定位的导航，不能替代实际引用检查。

#### 11.2.3 进程范围、terminal物理端点与交接

`process_scope.py` / `_process_scope_posix.py`只封装现有受管创建和OS范围操作，不拥有工具结果。同步terminal/probe与异步MCP/hooks/screenshot保留不同的流/任务API，只共用最小范围语义；不强迫一个对象同时伪装`Popen`、`asyncio.Process`和未来Win32 handle，也不在event loop里blocking wait。

| 接缝 | A的最小操作与含义 | 不能移入的policy |
| --- | --- | --- |
| 受管创建 | 实际需要的同步exec、异步exec/shell入口；只收现有argv/command、cwd、审查后的env与stdio/stream边界。POSIX内部仍用现有`start_new_session`，返回同次创建的root/scope和端点 | 不镜像全部subprocess kwargs，不给业务暴露`adopt(pid)`重建authority；不因通用创建改env、shell选择或生命周期预算 |
| root与范围 | root poll/同步或异步wait、当前managed-range是否仍存活/未能确认空；保留POSIX退出码/负signal值与错误 | root退出不等于范围空；范围空不等于输出排空或工具完成；不宣称任意逃逸后代受控 |
| stop与资源释放 | 原范围TERM/KILL请求、已不存在/失败观察、自有OS资源close；已消失可幂等，请求成功不等于物理join | 不固定所有caller统一宽限期、不自动重复spawn/发送、不在close里悄悄杀其他owner或释放业务容量 |
| terminal字节端点 | `physical.py` / `_physical_posix.py`提供当前PIPE/PTY的增量byte read、write/flush、end-input、端点close；复用scope，OS资源归同一physical对象 | 不接管输出解码/游标、monitor、权限、8槽或`PHYSICALLY_JOINED`；read不能等待凑满才交付现有实时输出 |

`manager.py`继续唯一拥有Registry/_ProcessState、reservation、process_id与Host/child授权、decision/adoption、状态机、输出订阅、reader/watcher/Timer安装、组合join、physical_completion与completion subscribers。`output.py`与`monitor.py`仍各有唯一owner。A保持当前POSIX实际等待/输出完成顺序；冻结的是root退出、范围空、输出drain与worker join的组合条件，不把某个POSIX等待次序强加给B的ConPTY。

terminal输入仍保持原已写/未写/unknown结算；PIPE end-input关闭写端，POSIX PTY按现行canonical EOT及写副本退休，读端继续drain。接口不把EOT包装成所有程序/平台都成立的真正半关闭；原模型close_stdin合同不新增结果枚举。B必须另过G0-T，不因A有方法就算Windows能力成立。

**launch交接必须覆盖五段：**

1. OS创建前：原owner持reservation/admission/credential guard与argv/env；失败按原not-started结算，不产生执行。
2. OS已创建但完整handle未返回：leaf持有root/scope/端点；后续dup/fdopen/stream包装等失败须终止并join、释放资源。若物理结算未完成，必须把实际仍活的physical对象交回原caller的既有隔离/收尾路径，不能普通raise后让reservation释放并丢掉进程。
3. handle已返回但state未公开：原Registry/caller接管；reader/watcher/Timer/subscription安装或owner-close竞态失败走原精确rollback，不重新spawn。
4. 公开后：输入、kill、自然退出、Host close沿用原线性化；一条执行的scope/端点不转移给新任务；资源未结算仍保留责任和占用。
5. 组合物理事实满足后：原owner完成输出/终态及completion，然后才能回收容量/prune；幂等close不能靠GC掩盖尚活范围。

现行`_spawn`在Popen后仍有包装/构造步骤；新边界必须补足这段资源交接故障覆盖。优先在leaf构造完成前完整回收；只有无法回收才走原Registry的未结算隔离，不能新建orphan registry/receipt。若由此需要修正现存泄漏，也仅修此交接边界并用真实失败注入验证，不借抽取重定义工具成功/终态。

#### 11.2.4 shell、probe与非终端进程

`terminal_process/shell.py` / `_shell_posix.py`只抽现有shell候选/可执行验证、独立命令`(shell, '-c', command)`和login/interactive probe argv/脚本。A无需为POSIX每条命令创建临时脚本或空载荷context manager。`environment.py`继续拥有default-deny/passthrough、workspace/venv/PATH策略、snapshot key/TTL/single-flight、attempt状态与cache接纳。

probe的selectors/原始读端可进入POSIX probe执行件，但不能成为不可取消的`run_probe()->bytes`黑盒。spawn和inflight process发布保持原environment锁下线性化：close先封准入，或能看到并join确切attempt；不允许锁外晚spawn。只有EOF、root成功、范围空和原所需资源收回后接纳cache；timeout、oversize、失败、close仍按原typed错误，不加重试/总寿命限制。

| 非终端caller | 接入范围 | 保持原owner的合同 |
| --- | --- | --- |
| `conversation_kernel/mcp/sdk_facade.py` | 实际异步stdio spawn/范围终止与关闭；复用原SDK/framing/stream tasks | credential guard覆盖真实不可逆spawn、取消后取回同一spawn与安全publication；帧大小/深度、secret、重连、现有close边界不改 |
| `hooks/executor.py` | 实际异步exec/shell、范围控制与cleanup | event选择、platform command、env overlay、deadline、输出解析、权限/credential guard与副作用结果不改 |
| `visualization_screenshot.py` | worker创建/范围终止与流收尾 | Playwright/Chromium仍由原worker/依赖控制；render/communicate deadline、输出和错误保持，不复制浏览器进程框架 |

同步/异步创建可有少量独立入口，异步取消仍shield/retrieve同一个实际spawn再收尾，不能丢进程或第二次spawn。各caller保留自己的完成条件、宽限/等待边界和业务资源槽，**不因共享scope就一律套terminal的group-empty完成policy或计入8槽**；不把frame、credential/env、hook/截图结果迁进scope。取消后进程/端点的真实回收按各自现有合同验证。

#### 11.2.5 桌面、IPC与资源的最小范围

| 领域 | A的决定及具体位置 | A保留的现状 / B才改变的内容 |
| --- | --- | --- |
| Node资源定位（必选） | `desktop/src/resource-paths.ts`纯locator；同一已解析Python路径注入`main.ts`的resolveHome、`runtime.ts`的wait-owner/start、`cli-install.ts`的现有POSIX入口；集中实际重复的PG/Playwright路径 | 当前`python/bin/python3`与资源目录/CLI marker/冲突/PATH行为不变，不增加manifest、系统Pythonfallback或新的home resolver |
| boot/quit/PG | 仅为真实接口/locator接入修改原`main.ts`/`runtime.ts`；`postgres.ts`只有实际需要时机械拆POSIX叶子 | 保留唯一boot/reset/requestQuit/BundledPostgres；私有Unix PG socket、原local认证/拒绝host、版本、DDL/schema、reset与关闭顺序不变；TCP/SCRAM/auth文件/helper均B |
| 宿主锁/diagnostics（条件片） | 如typed文件锁触及kernel lifetime锁，同批接`desktop_runtime.py`与其wait-owner/diagnostics client；如抽`desktop_runtime_io.py`则迁完整server/client/cleanup | 相同Unix diagnostics路径/权限/消息、原锁等待、Node JSONL编码/消息/EOF结算不改；named pipe、真实Windows匿名pipe适配延后B |
| Protocol-v3（可选片） | 当前有gateway/client/application实际需求才在A抽`terminal_protocol/transport.py`与POSIX leaf，并一次接齐`v3_gateway.py`、`desktop_app/protocol_client.py`及application端点持有者 | 相同Unix路径、launch capability、frame/auth/attachment/revoke/close；不在A提前切loopback、搬secret或复制业务bridge |
| Python locator/构建拆分（可选） | `runtime_resources.py`须有当前真实消费者；`resource_build`拆分须有真实Mac/Linux recipe分离理由，否则延后B | 不改固定版本/布局/许可证/目标；有新leaf立即接入mise sources/outputs并测cache失效，不等S6，也不靠force掩盖漏追踪 |
| renderer/安装/发布 | 默认不改frontend/preload/browser API和发布权限；只跑受影响回归 | Windows标题栏/盘符/PS说明、NSIS/Win launcher/Windows release集合全B；A不自行bump/tag/publish，当前DMG+两个deb集合及workflow_dispatch保持 |

可选片是否纳入在A0依据真实调用闭包确定并记录；不拆一个没有当前消费者的接口，也不以“可选”绕过必选binding/进程迁移触及的真实锁、端点或清理责任。

#### 11.2.6 实施切片与顺序

| 顺序 | 具体动作 | 退出条件 |
| --- | --- | --- |
| A0 基线/契约 | 读当前代码/有效规格；记录Git revision、dirty状态、支持矩阵；建立raw文件/进程直接间接caller与持有/close清单；选择条件片并跑改前基线 | 不是把历史24/49条绿测当本轮基线；明确当前结果/既有失败与native环境缺项；实际接口只含已用操作 |
| A1 安全文件闭包 | typed binding+POSIX实现+exclusive publish；按11.2.2共享签名/held类型闭包分片迁移设置/Skill/plugin/hooks/文件/preview | 每片所有caller同次接齐、旧fd/flags/unwrap路径删掉；borrow锁、read-at、metadata投影、三类发布与commit-unknown真实成立 |
| A2 同步进程/terminal | scope真实POSIX创建→physical字节端点→Registry/_ProcessState；保留业务owner和安装/join；补构造中途失败交接 | PIPE/PTY、root先退/后代、输入、满槽、rollback、Host close和monitor不回归；没有活进程漏出reservation/清理责任 |
| A3 shell/probe | POSIX shell计划/probe执行件提取；environment保留policy/attempt/cache | 独立命令/cwd、env/venv、single-flight、取消/close与probe接纳条件保持 |
| A4 异步消费者 | MCP/hooks/screenshot逐片接scope并删除该caller被替代的OS创建/kill分支 | 真spawn guard、异步取消取回、原deadline/流任务/结果及回收合同保持；不修改全局event loop或共用业务槽 |
| A5 桌面接入 | Node locator全部caller；必需锁适配与A0选定的Unix IPC/recipe条件片；输入追踪随新leaf一起改 | 单一boot/PG/IPC authority，原endpoint/wire/layout不变；没有unused模块或Windowsstub |
| A6 独立验收/交付 | 11.2.7多层验证与全文review，记录实测/未测，清理旧路径并更新只描述新单一路径的文档 | 必需证据全部通过才称A完成；源码完成但缺native Linux/GUI/provider证据须标待验收。无需G0通过，无需Windows依赖 |

A1与A2/A3可按owner并行；A4依赖scope真实创建/取消合同；A5中锁/endpoint依赖A1相应binding，最终A6只验一次共用矩阵，不按专题重复跑同一长程suite。每片先聚焦测试，A6再做最终整套资格；没有新改动/失败无需无故重复全部测试。A成功后继续W0/B，G0研究先行也不能跳过A退出条件。

#### 11.2.7 回归矩阵、真实证据与A完成条件

改前/改后使用相同锁版本、场景和生产tool surface；记录各自Git revision/dirty状态、OS/arch、工具版本、命令/退出码、实际输出/失败和cleanup。若用两个checkout/worktree，不能共用指向candidate源码的editable环境：各用对应uv环境或独立安装对应wheel，记录`pulsara_agent.__file__`及CLI/资源实际来源，避免baseline目录实际导入新代码。结果比较产品行为与副作用，模型自然语言不要求逐字相同。测试继续在现有目录，不为接口每个转发方法写镜像单测；只为真实新边界缺口补断言。以下均为**未来实施验收要求，本轮未执行**。

| 验证层 | 当前可复用的测试/路径 | 资格与关键断言 |
| --- | --- | --- |
| 文件/设置/包 | `tests/test_settings.py`、`test_local_skill_management.py`、`test_local_skill_removal.py`、`test_skill_import.py`、`test_round9_3_plugin_physical_semantics.py`、`test_round9_3_agent_plugin_product.py`、`test_plugin_source_import.py` | 实际symlink/父目录替换、并发发布、held对象借用、锁跨进程争用；临时写/replace前后/父fsync失败；原canonical metadata/ID不变；不能只mock binding调用 |
| preview/工具文件 | `tests/test_local_file_import.py`、`test_session_file_preview.py`、`test_content_revision_line_edit.py`、`test_html_local_resources.py`、`test_local_image_tool_executor.py`与对应export测试 | HTTP并发read-at不串位置、文本编码/预算、替换检测、no-clobber、精确清理、权限/结果unknown不回归；补borrow close/短写真实边界 |
| terminal物理 | `tests/test_round2_terminal_{output,environment,host,architecture,monitor,safe_point,postgres}.py`、`test_terminal_cognitive_subtraction.py`、`test_pr03_process_termination.py`、`test_stage2_terminal_host_lifetime.py` | 真实PIPE/PTY增量字节/EOF；leader退出但后代存活/持stdout；8槽准入、写入/kill/close竞态；reader/watcher/Timer部分安装、dup/fdopen/构造故障回收或原隔离；root退出不能提前完成/释放槽 |
| 非终端与前缀/长程 | `tests/test_mcp_transport_lifecycle.py`、`test_mcp_sdk_transport_probe.py`、`test_round9_2_hook_subsystem.py`、`test_round9_2_hook_integration_postgres.py`、`test_visualization_preview.py`；`test_round5_long_horizon_execution_envelope.py`、`test_round3_1_provider_input_prefix_continuity.py`、`test_round5b_long_horizon_context_compaction.py` | 真实异步spawn取消、credential变更拒绝、blocked stdin/TERM忽略/输出/cleanup；Host/child反复周转与compaction保留process跟进；现有单epochSYSTEM/tools字节严格不变，messages后缀追加，无新总caps |
| 当前POSIX完整suite | 仓库uv `.venv`、现有ruff/pytest；`.github/workflows/ubuntu-tests.yml`的native Ubuntu24.04 x64/arm64与既有PG17扩展fixture | macOS arm64与两套native Ubuntu目标按现行有效合同跑最终Python回归；真实依赖缺失待补，不能skip/xfail改绿。容器须有init reaper，其结果只作userspace补充，不能替native矩阵/GUI |
| Node/前端/IPC | `tests/test_desktop_runtime.py`；`desktop/src/{runtime,postgres,main,cli-install}*.test.ts`；`npm --prefix desktop test`与当前frontend checks | 原ready/EOF/错误/关闭顺序、home/CLI定位、sender/用户级page不变；若动preload/renderer/browser，跑对应类型/组件及`test:browser`，不能以默认不改免掉实际影响 |
| wheel/cache/随包资源 | `desktop/scripts/test_build_cache.py`、`smoke_resources.py`、`tests/linux/installed_package_smoke.py`；原`desktop:verify`与build/package任务 | fresh应用wheel与checkout外安装，保留热复用未变依赖；真实输入变化触发受影响native资源重建。三个现有native目标做架构/版本、带空格路径搬移、净化环境下Python/rg/PG BM25/vector/Chromium；旧缓存/开发cwd不得掩盖漏文件 |
| 已安装GUI/CLI | `npm --prefix desktop run test:startup`、现有native DMG/deb构建安装路径（publish never） | macOS与至少一套真实Ubuntu图形session：图标冷暖启动→bundle DB/kernel ready→workspace/附件/preview/用户设置→正常Quit与慢退/失败→同home重开；CLI help/version/app、冲突、后台/tray恢复。Ubuntu另一架构至少native安装/CLI/资源实测；如触及架构专属native/安装行为或该目标有待解问题，也须该架构GUI实测，明确记录分层证据，不能声称未测GUI通过 |

抽取可能影响已安装路径，Node mock、wheel smoke、资源relocation、真实GUI各有不同证据边界，不能互相替代。recipe/layout未变时无需无理由全量重建native SDK；但必须更新应用wheel并验证最终引用。新recipe leaf须验证真实mise freshness与cache失效，不能只用`--force`成功盖掉依赖漏追踪。真实Linux图形/所需runner不可用时报告具体待补项，macOS本机通过不能算A整体通过。

**真实provider与长程属于本次核心A的必需退出证据。** 在macOS与Linux分别覆盖受改OS边界：独立命令、running后继续任务、PIPE/PTY交互与关闭、输出截断补读/导出、文件预览/变更、MCP/hook、subagent/compaction和Host关闭回收。利用既有长程场景/物理边界，不加总轮数/总时长cap。保存实际prompt/provider request-response/tool trace与物理观察，只精确删除真实credential值，不广泛遮盖内容、不生成文档/代码/证据SHA。原prefix测试验证字节与连续性，不靠随机模型回答相同证明等价。

执行前先只读preflight runner与当前API、保存连接和精确本机测试DB：`LocalSettingsStore` + `require_pulsara_home()`读取配置后再隔离home/已验证disposable DB，不导出key或改保存配置。部分现有dogfood DB helper只接受localhost/loopback且根dbname=pulsara，不能把其存在等同可直接跑managed Unix-socket配置；如不适用，修测试harness以复用已授权的本地隔离机制或报告缺项，不为runner偷偷开TCP/重写settings，也不触及未核实/远程DB。

可复用`tests/dogfood/run_terminal_cognitive_dogfood.py`、`run_round10_subagent_dogfood.py`、`run_artifact_export_dogfood.py`，按实际保存连接/模型选择场景，输出按platform/before-A/after-A隔离。**artifact runner的`--phase baseline`会删artifact_export工具，禁止拿它当本次改前基线；两份代码均用完整生产工具面的`--phase candidate`，仅用输出目录区分前后。** terminal runner的phase可作证据label；其他旧runner需检查硬编码连接/输出和API，不把默认模型当作必然存在。测试harness过时就修到当前合同，不给production加兼容wrapper；硬编码输出的GUI runner须运行后归档原始baseline，避免candidate覆盖。

A退出须同时满足：选定切片与所有真实消费者/held/异常cleanup已闭合、旧路径删除；上述必需聚焦/完整/native/installed/provider证据通过；无未解决的权限/副作用/文件或物理结算回归；prefix/schema/oracle/容量与当前产品合同不变；root及三专题全文交叉审阅无阻断。外部环境缺项允许如实记录“实现完成、验证待补”，不允许按时间到了推断通过。A验收不涉及Windows激活，第13节仍完整保留。

#### 11.2.8 成本归属与进入B

A承担原W1/W2的共用/POSIX进程、shell及消费者抽取，W3的typed binding/POSIX全caller，W4/W5的实际锁/IPC/locator接缝与W7中对应POSIX验证。它们均已包含在88–154人日整体预算中，不能再把三份专题估计累加到总数上。A0按实际caller闭包与native环境拆出A/B剩余劳动，未知的Windows Job/ConPTY/NTFS/扩展成本仍留B，不因A完成就下调到几个if的工作量。

分两次交付可能多一次整体验收、安装和review，确有新增劳动时单列跨阶段验证成本并说明覆盖差异；同一证据不重复计费。B只复用未受后续改动影响的A基线，Windows接入后的POSIX受影响回归仍须重跑。A降低定位风险，不承诺缩短总人日，也不预先保证Windows技术路线成立。

进入B的条件是A独立验收完成与G0-D/T/F/R全部通过。G0如发现接口缺口，按11.1 S0/S1修订唯一接口/所有caller；如发现产品语义无法满足，先修订路线/规格并重审，不能以“A接口已经抽出”为理由放宽Windows合同。Windows路线失败时，已经验收的A仍可继续服务Linux/macOS。

## 12. WSL2及其他路线的真实成本

| 路线 | 能省下什么 | 仍需付出的成本/语义变化 | 决策 |
| --- | --- | --- | --- |
| 原生Windows全链路 | Windows文件/PowerShell/现有工具链同一执行域 | 终端、NTFS安全、PG扩展三项底层移植，见上文 | 本规格实施目标；POSIX Phase A→G0全部通过→Windows Phase B |
| Windows Electron + 整体WSL2 Linux内核与PG | 大量复用PTY/killpg/dir_fd/Unix socket/pg_search/Linux wheels | Linux home/工具链、发行版所有权与部署、Win↔Linux路径、网络/OAuth、生命周期、跨FS权限/性能和GUI整合 | 有价值备选，需另修订规格方实施 |
| 只把terminal包wsl.exe | 表面避开ConPTY | Python本体仍有POSIX导入、安全文件/PG阻断；cwd/env/文件/权限跨域，wsl.exe退出不证明Linux命令join | 拒绝作为兼容方案 |
| Git Bash/MSYS2/Cygwin套壳 | 某些shell命令可运行 | 无法补齐Python dir_fd/ACL/PG扩展和进程结算，路径/工具混域 | 不采用 |
| 外置/远程Postgres或Docker Desktop | 避开原生pg_search构建 | 改变随包专用DB、零手动服务的桌面合同；网络、认证、端口、资源与用户运维成本 | 不自动采用 |
| SQLite/替换检索引擎 | 降低native扩展依赖 | 事务、schema、权限、memory/history检索语义重写，属于另一项产品/架构任务 | 不在Windows兼容中夹带 |

WSL方案若另行采用，至少需要以下明确合同，不能估成“几天打包”：

- **单一执行域**：Python、PG、terminal、hooks、MCP、Skills都在确定发行版/用户中；Windows端只拥有Electron与宿主动作。不把Windows的Node/Python/npm工具假称Linux可用。
- **Windows interoperability**：WSL能启动Windows exe，Linux killpg不能证明其Windows侧物理join。若采用专用发行版，可明确`interop.enabled=false`与`appendWindowsPath=false`（不同开关）保持Linux-only工具合同；既有用户发行版不能被静默改配置。若仍允许跨域Windows命令，必须另定受管范围/Windows执行owner并重估，不能只从PATH隐藏exe就声称执行权限封闭。[WSL配置](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
- **数据与配置**：Linux ext4内使用同一require_pulsara_home/LocalSettingsStore；不将PG/socket/settings放`/mnt/c`后假装0700、uid、Unix socket保证不变。Windows壳仅存壳自身偏好，避免双settings truth。选择既有发行版会受用户环境影响；导入专用发行版则承担rootfs来源/许可、更新/磁盘/备份/卸载/data保留所有权。两种不能隐式互换。
- **路径**：模型与canonical数据使用Linux路径；Windowsdialog/drop的`C:\...`必须在提交前由明确owner转换/校验成该发行版可达路径，Linux`/home/...`的系统open/reveal经正确`\\wsl.localhost\发行版\...`转换；不能盲replace盘符。Windows `.venv`不能当Linuxvenv，NTFS/Git大小写/执行位/性能差异必须告知。[WSL官方文件系统建议](https://learn.microsoft.com/en-us/windows/wsl/filesystems)
- **网络**：先验证Windows→WSL loopback服务的官方localhost转发，保留127.0.0.1和origin校验，不默认改0.0.0.0或另建HTTP bridge。NAT/mirrored、VPN、防火墙、OAuth回调落在哪侧均需真实测试；如确需proxy，以可证明的缺口修订，不提前造网络framework。[WSL网络](https://learn.microsoft.com/en-us/windows/wsl/networking)
- **生命周期**：控制管道EOF应到达Linux的既有关闭owner；等Linux确切owner释放后才停其PG，不能taskkill wsl.exe即声称join。禁止`wsl --shutdown`波及其他发行版，禁止自动unregister用户发行版；显式删除专用发行版也不能等同普通卸载。休眠/恢复/WSL更新/发行版失联必须如实标记执行unknown，不重放。[WSL命令范围](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)
- **用户前提**：虚拟化/WSL2可用性、企业策略、首次安装提权/重启/离线部署/磁盘和内存成本；没有WSL时给明确安装入口或失败，而不是虚假“原生Windows开箱即用”。

以Windows壳接已有受支持WSL2发行版、明确Linux工具链前提估计，完整产品化约 **35–65人日**；管理专用发行版/rootfs与升级/卸载可再增加 **15–30人日**，仍需不确定性预算和Windows实机验收。这是备选估算，不与原生预算相加，也不授权实现。若用户接受Linux执行域，这可能是更合理的首版路线；若目标是操作原生Windows工程/PowerShell工具，它不等价。

## 13. 验证矩阵与激活条件

本节13.1–13.3描述Phase B的Windows资格。Phase A独立按11.2.7验收；A通过只能声明Linux/macOS接口重构完成，不构成下表任何Windows原生证据。B接入后的POSIX受影响回归仍必需，A的绿测不能免掉后续回归。

### 13.1 分层验证

| 层级 | 必须覆盖的场景 | 证据边界 |
| --- | --- | --- |
| Import/资源 | 全部生产入口与代表owners、uv locked安装、PE/DLL依赖、中文空格路径、资源搬移、非管理员 | 不能由Linux wheel解析或mock win32替代 |
| 终端基础 | 独立cwd/export；PS脚本/cmdlet/native退出码；PIPE/ConPTY；交互REPL；write/submit/close_stdin；非ASCII/CRLF/ANSI；command接近既有上限 | 任一有副作用输入都验证真实到达与不重发 |
| 终端物理 | leader先退出/孙进程持stdout、root/child隔离、创建/取消/kill竞态、满stdin、Job赋值失败、readers drain/join、8槽耗尽再释放、handle泄漏 | 观察进程集合与handle，不能只assert UI状态 |
| 输出/长程 | 原游标/GAP/截断/artifact_read、monitors竞态/expiry/完成唤醒、多轮长任务、compaction期间process句柄有效 | 不添加总工具次数/任务寿命上限；monitors仍非durable |
| 文件与安全 | junction/reparse/目录替换/并发publish/锁竞争、settings写入失败/unknown、安装/使用/删除Skill/plugin、preview reading root与HTML资源替换 | 真实NTFS/ACL，不能跳过安全断言换绿 |
| 数据库 | relocation、新建/已有目录、版本不符、错误实例/端口占用、SCRAM拒绝、runtime grants、BM25/vector/历史搜索、normal/forced退出重开 | 原生PG+扩展；容器DB只做既有查询回归 |
| 桌面流程 | 点击图标→ready→选workspace/附件→模型工作→交付物→退出/重开；tray、IME、DPI、OAuth/browser、CLI、安装/更新/卸载 | 测试GUI宿主与已安装NSIS产物，不只源码dev shell |
| 权限/前缀 | read_only、ask、hardline/input、workspace信任与用户级页面；cold epoch / compaction successor；连续请求前缀严格不变 | 工具/OS说明变化只在允许边界采用 |
| 三平台回归 | macOS arm64、Ubuntu x64/arm64现有资源与终端/路径/安装检查 | Windows平台抽象不能回归现有可用产品 |

优先复用现有tests：`test_round2_terminal_*`、`test_terminal_cognitive_subtraction.py`、`test_stage2_terminal_host_lifetime.py`、`test_round5_long_horizon_execution_envelope.py`、`test_desktop_runtime.py`、settings/skills/plugins/MCP/hooks/path/preview测试与desktop/frontend测试。把真正平台专属的API测试放对应target；平台无关合同全部执行。不能批量`skipif(win32)`、xfail、放宽断言或把所有Windows失败归为“环境问题”。

CI可用Windows runner执行单元/资源检查，桌面11验收仍要真实普通用户交互环境；Windows Server runner不等于Windows11桌面证据。Workflow保留当前手动触发语义，是否增加PR触发另行决定，不借本任务改发布权限或自动publish。

### 13.2 真实provider与用户验收

按AGENTS通过LocalSettingsStore只读加载保存配置，在隔离home和已验证可重置的本地临时数据库中做原生Windows dogfood；没有配置先报告缺项，不要求导出key到环境。至少包含：中文含空格工程、`.venv/Scripts` Python任务、用户Git/Node工具、PIPE和TTY交互、长任务返回running后继续工作、subagent命令scope、monitor唤醒、输出截断补读、图片/HTML交付物、MCP stdio与HTTP、hook commandWindows、重开历史/memory搜索、模型知道当前PowerShell语法及prefix continuity。

证据保留实际提示、模型参数/响应、工具结果、进程物理观察与失败原因，仅隐藏实际secret；不把少数echo任务推广为整个Windows平台通过。保存配置、用户workspace与非测试数据库保持只读/不重置；只能重置已经核实是本地disposable的目标。

### 13.3 产品激活清单

Phase A独立验收通过；G0全部通过；W1–W7剩余Windows实现与review完成；原生目标安装包完成关键矩阵；没有未解决的权限/副作用/数据安全P0/P1；真实provider和长程证据通过；三平台有效回归；干净基线与当前关系/事件oracle没有无产品理由的增加；文档与安装界面如实声明支持矩阵、手动更新和开发期数据合同。缺少任何一项都不宣称“Windows支持完成”。

## 14. 本地参考仓库的采用原则

本轮只读调研用户指定的 `deepseek-harness`、`ZCode`、`codex`、`grok-build`、`opencode` 本地checkout。它们的实际实现优于README平台口号；本地版本与审阅路径记录在各agent报告中。借鉴边界为维护中的依赖、平台适配与已验证行为，不复制产品的权限、恢复、总超时或自动降级政策。

| 仓库与本地Git版本 | 具体实现路径（相对各仓库根） | 可借鉴与不能照搬的结论 |
| --- | --- | --- |
| deepseek-harness `687f282e8625` | `packages/subprocess/win32-process/src/process.ts`、`subprocess-local/src/windows-job.ts`、`terminal.ts`、`windows-inspector.ts`、`packages/terminal/terminal-bash/src/index.ts`、`patches/node-pty@1.2.0-beta.15.patch` | ordinary process用suspended→assign Job→resume并区分root exit/range empty；PTY却另走node-pty，不可推断同一Job覆盖。prompt marker的persistent line-oriented shell与Pulsara当前独立命令/fullscreen用途不同；其worker补丁说明依赖并非零维护成本 |
| codex `89461ea68338` | `codex-rs/utils/pty/src/{pty.rs,pipe.rs,windows_input.rs}`、`win/{conpty.rs,psuedocon.rs,job.rs}` | ConPTY创建属性/Unicode环境与精确handle可参考；PIPE源码明确post-spawn Job是best-effort且正常root退出可preserve descendants。Pulsara不能继承这些fallback、breakaway和提前完成策略；不能复制workspace私有crate当稳定SDK |
| ZCode `3c5b22764937` | `packages/services/src/runtime-tools/{runtimeCommandEnv.ts,runtimeToolResolver.ts}`、`process/{processTreeTerminator.ts,windowsTaskkillRunner.ts}`、`packages/desktop/electron-builder.config.js`、`docs/desktop/windows-update-install-handoff.md` | PATH大小写/PATHEXT、资源锁与签名/安装handoff测试有价值；固定退出预算后taskkill、清理失败继续安装及额外manifest机制不符合Pulsara本次合同 |
| grok-build `2bdd1d6a6369` | `crates/codegen/xai-grok-file-lock/src/{lock.rs,locked_file.rs}`、`xai-grok-update/src/windows_payload.rs` | 复用OS文件锁、dup/unlock所有权和真正Windows payload的思路；不移植无产品必要的acquire-slot、network-FS、best-effort更新与旧payload兼容机制 |
| opencode `37e3912d8ea5` | `packages/desktop/electron-builder.config.ts`、`resources/windows/installer.nsh`、`src/main/windows/index.ts` | per-user NSIS、系统结束事件、安装资源execute ACL可参考；安装目录可执行ACL不能复制给含secret的home，不照搬`verifyUpdateCodeSignature:false`，Bun CLI不替代Python供应链 |

所有表中分组路径均相对该行仓库的对应父目录；完整定位与源码观察见专题报告。任何仓库已实现ConPTY、PowerShell或Windows安装包，都不能证明它已满足Pulsara的物理join、prefix continuity、安全发布、PG扩展与长期运行合同。Pulsara不同的数据库、安全与进程所有权必须单独验证。

## 15. 调研工作区、审阅与冻结

本轮使用三位 `gpt-6.1-sol` / `high` subagents，分别研究terminal、backend、desktop；root汇总与独立核查。个人workspace均在`output/windows-compat-20261024/`，不修改参考仓库、不改生产配置与实现。

| 工作区 | 内容 |
| --- | --- |
| `root/` | 独立代码扫描、官方来源核查、汇总取舍与冻结记录 |
| `terminal/` | 终端生产合同、codex/deepseek-harness、ConPTY/Job/PowerShell研究与交叉审阅 |
| `backend/` | 非终端后端、SDK/文件安全/依赖、ZCode/grok-build研究与交叉审阅 |
| `desktop/` | Electron/frontend/资源/发布、opencode/ZCode与WSL边界研究和交叉审阅 |

专题报告：[终端](output/windows-compat-20261024/terminal/report.md)、[后端](output/windows-compat-20261024/backend/report.md)、[桌面](output/windows-compat-20261024/desktop/report.md)。`output/`按仓库规则为本地忽略目录，根规格正文已独立保留决策、验收、成本和关键依据，不依赖这些忽略文件才能实施；需要共享完整研究时应随任务交付这些报告，不新增durable证据系统。

首次调研两组macOS现行合同聚焦回归分别24 passed与49 passed（具体命令/结果见专题报告），用于校验读取的生产基线，**不能计为Windows通过**。桌面报告只读复核了已锁Electron44.9.0/electron-builder26.15.3的titleBarOverlay/session-end、NSIS/per-user/asInvoker/签名扩展点，没有执行Windows构建/GUI测试。没有运行真实provider或重置数据库；本次代码组织修订仅做文档/源码位置检查，没有重跑这些测试或G0。

首次平台规格的已完成流程：root汇总全文 → 三位agent第一轮通读及跨专题审阅 → root逐项修订 → 三位agent第二轮重新通读同一候选 → 三位均明确“可冻结，无剩余阻断” → root独立认可。没有用“无人反对”或agent结束状态推断批准。

| 审阅者 | 第一轮发现并已关闭的主要问题 | 最终结论与记录 |
| --- | --- | --- |
| terminal / gpt-6.1-sol high | 内部Protocol-v3遗漏、PowerShell退出码混用、Job空集判据、WSL interop、NonInteractive风险 | 可冻结，无剩余阻断；`terminal/review-round1.md`、`terminal/review-round2.md` |
| backend / gpt-6.1-sol high | PG auth文件/DDL失败顺序与owner、Windows源执行属性、非终端必要env与credential边界 | 可冻结，无剩余阻断；`backend/review-round1.md`、`backend/review-round2.md` |
| desktop / gpt-6.1-sol high | NSIS默认强杀已知应用、OAuth URI权限歧义、宿主管道UTF-8与匿名pipe差异 | 可冻结，无剩余阻断；`desktop/review-round1.md`、`desktop/review-round2.md` |
| root | 汇总取舍、三份报告/两轮审阅闭合、源码定位与官方依据、成本去重和证据限制 | 同意冻结；`root/research-notes.md`及`root/freeze-record.md` |

表中审阅记录相对本节工作区根`output/windows-compat-20261024/`。冻结后若G0要求修改执行、安全、认证、更新或其他产品语义，必须修订规格并重新审阅受影响合同；不能用本次批准跳过。

本次按用户要求增加3.1–3.5与11.1，明确代码组织、平台适配和实施顺序。仍由上述三位gpt-6.1-sol/high agent交叉审阅修订全文，本轮个人工作区为各自`code-organization/`子目录；首次批准没有自动覆盖新条款。523行组织候选经全文审阅后补充唯一构建输入/缓存失效段，三位均复核525行最终语义候选并明确同意；root独立认可后仅更新本节与文首状态。

| 本次审阅者 | 全文交叉审阅及最终复核 | 结论 |
| --- | --- | --- |
| terminal / gpt-6.1-sol high | `terminal/code-organization/review-round1.md`、`review-round2.md` | 可冻结，无剩余阻断 |
| backend / gpt-6.1-sol high | `backend/code-organization/review-round1.md`（含525行最终确认） | 可冻结，无剩余阻断 |
| desktop / gpt-6.1-sol high | `desktop/code-organization/review-round1.md`（525行最终候选） | 可冻结，无剩余阻断 |
| root | `root/code-organization/research-notes.md`、`validation.json`、`freeze-record.md` | 同意冻结 |

新条款冻结内容包括：共用业务owner与平台leaf边界、生产目录/具体文件、typed file binding全消费者hard cut、同步/异步进程范围和真实spawn凭据临界区、Node/Python有限安全文件接缝、三条IPC、资源定位与构建输入、S0–S7依赖顺序/退出条件。没有新增生产模块、G0通过声明或第二套平台框架。

上述为此前平台/代码组织版本的冻结记录。本轮按用户要求新增11.2.1–11.2.8，并同步修订1/3/4/11/11.1/12/13的阶段前置，明确A可独立交付、G0不阻断A、B须A验收及G0全过；没有沿用此前批准替代本次审阅。

本轮个人工作区为各自`posix-first/`。root先汇总684行全文候选，三位agent分别完整交叉核对全文和另外两份专题提案，均明确“可冻结，无剩余阻断”；root逐份读取审阅记录并独立认可。三份审阅未提出必须修改的阻断，最终仅更新文首与本节冻结元数据，不改已批准的实施语义。

| POSIX先行修订审阅者 | 提案与全文交叉审阅（相对本节工作区根） | 结论 |
| --- | --- | --- |
| terminal / gpt-6.1-sol high | `terminal/posix-first/proposal.md`、`review-round1.md` | 可冻结，无剩余阻断；Registry/handoff、同步异步创建与shell/probe及跨专题边界闭合 |
| backend / gpt-6.1-sol high | `backend/posix-first/proposal.md`、`review-round1.md` | 可冻结，无剩余阻断；typed全caller、metadata谓词、锁/borrow/read-at、发布与unknown闭合 |
| desktop / gpt-6.1-sol high | `desktop/posix-first/proposal.md`、`review-round1.md` | 可冻结，无剩余阻断；资源/条件IPC、native/installed/provider分层验收、基线真实性与预算去重闭合 |
| root | `root/posix-first/research-notes.md`、`validation.json`、`freeze-record.md` | 独立同意冻结；章节/文件定位与旧前置冲突检查完成 |

**最终结论（2026-10-24）：平台实施规格、代码组织方案及本次POSIX先行Phase A修订均可冻结。A实现与Linux/macOS验收未执行，G0未执行，Windows实现/原生验收/激活/发布未完成。本轮未改生产代码、保存配置或数据库，未执行新一轮产品测试或真实provider调用。**
