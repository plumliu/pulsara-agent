# Windows 后端兼容调研（非终端范围）

日期：2026-10-24。范围：Python runtime/分发、PostgreSQL、文件安全、后端 IPC/锁、MCP/hooks/Skills、文件与可视化工具、CI。终端/ConPTY 和 Electron 包装的详细方案由其他报告负责。本次只新增本工作目录文件，未读取实际凭据、未修改生产设置或数据库。所有源码结论来自当前检出；根 `AGENTS.md`、`DEVELOPMENT.md`、`INSTALL.md`、`README.md` 为当前约束，`archived_docs` 不作为实施权威。

## 决策建议

可以冻结 **Windows 11 25H2 x64 原生、条件实施规格**：先通过 G0 原生依赖/数据库/文件安全/进程树可行性 gate，再进入全面迁移。G0 尚未通过，不能据本文宣布支持。Windows ARM64、32 位、其他 Windows 版本都没有本轮验证承诺；这不是断言它们永远不可用。

不建议把 WSL2 静默设为 Windows 的第一阶段实现。WSL2 整个 Linux 后端可以保留既有 POSIX 和 pg_search 机制，但工作目录、home、信任、原生应用打开/导入/预览和关闭边界都会改变。若原生 G0 失败，应把具体现象带回规格决策，显式选择继续修上游、独立 WSL 产品模式或其他架构；不得自动换远程 DB、替换检索或关闭能力。

主要阻塞不是 Python 包生态：固定 `uv.lock` 对 CPython 3.12 x64 有 google-re2、Pillow、Playwright、psycopg-binary、pywin32 等 wheel。阻塞是现有程序明确依赖 Unix 安全/IPC/子进程语义，以及 pg_search 固定版本没有已验证的 Windows 产物。

## 证据与限制

- `probe_inventory.py` 只用根 `.venv/bin/python` 读取 AST/uv.lock；`inventory.json` 保留具体位置与 wheel 名称。排除 `terminal_process/` 后仍有 **45 个直接 Unix OS surface 引用**、**130 个 dir_fd 调用点**；这是搜索盘点，不是 Windows 执行或缺陷计数。
- macOS 合同回归：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider --basetemp output/windows-compat-20261024/backend/pytest-tmp tests/test_settings.py tests/test_local_skill_removal.py tests/test_desktop_runtime.py`，**49 passed in 0.98s**，日志 `macos-contract-tests.log`。这些仅证明选定既有行为在本机成立，不能证明 Windows。
- 未在 Windows 运行编译/安装/数据库/浏览器/安全竞争探针；原生平台行为全部需要 G0/G1 验证。没有做实际 Windows ACL 或 Job Object 安全保证。
- 没有生成代码/文档 SHA、fingerprint 注册表、持久化 receipts 或新恢复状态。

## 1. Python 安装与随包资源：P0

| 代码位置 | 当前事实与 Windows 后果 | 实施 owner / hard cut |
|---|---|---|
| `pyproject.toml:6-33`, `.python-version`, `uv.lock` | Python >=3.12，实际固定 3.12.15；uv 有多平台锁。`mcp==2.1.0` 在 Windows 传递依赖 pywin32>=311；本锁为 312。 | uv 继续拥有解析、安装与 Python 选择；Pulsara 只声明目标布局和不可变产物验证。不能因为改平台放弃 locked/offline 边界。 |
| `tools/hatch_build.py:65-70,107-139` | wheel tag 仅 Darwin arm64、Linux x64/arm64；始终找 `_vendor/ripgrep/rg`。Windows 源码安装/构建被主动拒绝。 | 给已验证 Win x64 增加 `win_amd64`、`rg.exe`、PE 机器架构与版本/许可证验证；继续由 Hatch 构建，不写另一套包安装器。 |
| `tools/prepare_ripgrep.py:30-38,51`, `src/pulsara_agent/ripgrep.py:19` | 已有 Windows x64/ARM64 下载校验项、运行时 `.exe` 选择。这仅是准备能力，不代表 wheel 能安装。 | 复用现有显式 prepare 下载；运行时和 build hook 不能隐式下载。 |
| `desktop/scripts/prepare_resources.py:35-75,515-587,639-647` | target/pg_search assets 只有 macOS/Linux；Python 布局写死 `bin/python3`、`lib/python3.12`；验证 Mach-O/ELF、动态库 `.so/.dylib`。 | 资源 builder 保持单一 owner；Win 使用实际 uv-managed CPython 安装布局、PE/DLL 依赖验证、私有浏览器布局。目标 Python 自己 compileall、uv 安装和离线复制照旧。不要用系统 Python/运行时 pip 兜底。 |
| `mise.toml:17-81` | `.venv/bin/python`、产物名和 `.so` 条件都是 Unix。 | mise 同一任务图按物理平台声明 interpreter/output，Windows `.venv/Scripts/python.exe`。保留 freshness/依赖 owner；不造新 cache registry。 |
| `src/pulsara_agent/desktop_launcher.py:12-35` | 安装 manifest 相对路径机制可复用，但 `os.X_OK`、detached 启动和 `.exe` CLI 入口要用真实 Win 安装验证。 | Electron installer/CLI owner 提供实际路径；Python 保留 manifest lookup；无 checkout/PATH/system Python fallback。 |

本锁关键 native wheels（完整文件名见 inventory）：aiohttp3.14.3、google-re2 1.1.20251105、greenlet3.5.6、Pillow12.3.0、Playwright1.63.0、psycopg-binary3.3.4、pydantic-core2.46.4、pywin32 312 对 cp312 Win x64 有候选；cryptography50.0.0 有 cp39/cp311 abi3 x64 候选。**wheel 存在不是锁定环境安装验证**。ARM64 的 psycopg-binary/cryptography 候选缺口进一步支持本轮不承诺 ARM64。

## 2. PostgreSQL 与检索：G0 的独立前置条件

`storage/migrations/sql/0000_conversation_kernel_baseline.sql:1-2` 必须加载 vector 和 **pg_search VERSION '0.26.1'**；`:1569` 和 `:2791` 分别是 memory/history BM25 index；`storage/migrations/runner.py:667-671` 验证恰好版本。替换为 PostgreSQL 内建 FTS、关闭检索、换另一版本或外部服务会改变产品，不属于兼容补丁。

当前资源固定 PostgreSQL17.9、pgvector0.8.1、pg_search0.26.1（`desktop/scripts/prepare_resources.py:26-48`）。官方当前预编译矩阵只有 Linux/macOS，**没有 Windows**；这是产物缺口，不是 Windows 不可能。[ParadeDB self-hosted extension](https://www.paradedb.com/docs/operate/deploy/self-hosted/extension)

已核对精确上游 tag：pg_search v0.26.1 workspace `Cargo.toml:60-62` 固定 pgrx=0.19.3，该 pgrx tag README 明确列 **x86_64 Windows tested**，Windows 要求 MSVC/Clang、libclang，cargo-pgrx 在 Windows 使用 EnterpriseDB 预编译 PostgreSQL。因此“pgrx 不支持 Windows”是错误结论，但 pgrx 的支持也不证明 pg_search 的 Tantivy/rustix/tokenizers/native dependencies 能构建和稳定运行。[pg_search固定依赖](https://raw.githubusercontent.com/paradedb/paradedb/v0.26.1/Cargo.toml)、[pgrx0.19.3系统要求](https://raw.githubusercontent.com/pgcentralfoundation/pgrx/v0.19.3/README.md)

G0 数据库必须在真实 Win11 25H2 x64 以 PostgreSQL17.9 ABI 构建/获取可再分发 pgvector0.8.1+pg_search0.26.1、装载 preload、执行现有 clean-v0 全 baseline+verifier、验证中文/Unicode 检索与原文位置、BM25 排序、向量运算/HNSW、shutdown/reopen、随包目录搬移后再次验证。日志应记录编译器/SDK/库版本与原始错误，不仅“cargo build 成功”。若必须 patch 上游，明确最小差异和维护 owner；需改版本/SQL 合同时先修订规格。

`desktop_runtime.py:49-72` **只允许专有 Unix socket 目录**，并检验 getuid/POSIX mode；`prepare_database` 继续复用 migration owner，不能删掉验证来允许任意本地 PG。原生 Windows 要由 Electron 已有 PG 生命周期 owner 提供单一受管理端点，以 loopback-only PostgreSQL + 双角色最小权限 + 正式认证/凭据存放策略取代目录权限作为 admission 条件。端口本身不是身份；不可默认 `trust` 任意 localhost 客户端。DSN 注入边界继续是启动帧/`LocalSettingsStore(postgres=...)`，不把受管理 DSN 持久化成另一权威。`listen_addresses`、PG socket permission 在 Windows 的语义不同，不能复制 chmod 判定。[PG17连接配置](https://www.postgresql.org/docs/17/runtime-config-connection.html)

## 3. 文件安全与路径：P0，不能字符串替换

### 现有所有者和行为

| Owner / 精确代码 | 必须保留的语义 | Windows 要解决的问题 |
|---|---|---|
| `local_source_binding.py:11-15,46-90` | 词法绝对路径 + 所有组件 no-follow 绑定，当前是从 `/` 开始的 dir_fd walk。 | drive/UNC 根不是 `/`；Python3.12 Windows 不支持 dir_fd，缺 O_NOFOLLOW/O_DIRECTORY，getattr=0 不构成保护。 |
| `settings.py:335-418` | 唯一 settings owner；拒绝宽权限/非regular文件；同目录临时写、fsync、原子替换、结果不确定时 reread。 | ACL/SID 替代 Unix mode；fchmod/dir_fd/directory-fsync 不可直接运行。需要明确 Win publication/flush contract，不能吞 PermissionError。 |
| `exclusive_publish.py:19-72` | 唯一 exclusive no-replace 目录发布，Darwin renameatx_np/Linux renameat2。 | 其他平台显式 unavailable；Windows 必须证明 no-clobber、同卷、被替换 stage 的准确身份、打开句柄与 rename sharing 关系。 |
| `capability/local_skill_removal.py:51`（顶层）、`:95-150,179-200` | inspected physical identity，stale 不删替代目录，完整 unbind 后清理。 | 顶层直接 O_DIRECTORY/O_NOFOLLOW/O_CLOEXEC 使 Windows **导入失败**；后者被 `local_skill_management.py:17` 导入，影响 capability/desktop 主路径。改成平台 owner 后必须同等 stale 安全，不能仅删除常量。 |
| `capability/local_skills.py:132-139,1364`; `skill_import.py:49-55`; `local_skill_publisher.py:747-781,1032-1151` | no-follow source inventory、已冻结内容、独占 staging/publication、可执行位来源。 | Windows reparse/junction、大小写碰撞、没有执行位、发布期间同目录 handle 的锁定/共享冲突。 |
| `plugins/package_core.py:420-607`; `plugins/package_store.py:1521-1750,1817-1866,2043-2068` | 精确 frozen source、读者 lifetime anchor、独占发布/删除、无链接重绑定、不可篡改托管内容（OS权限）。 | 不应靠只读文件属性模拟私有 DACL；需要共享锁/独占锁与 handle identity；正在运行包被删/换的条件不能变宽。 |
| `desktop_app/file_import.py:29-50,62-140` | 流式复制，发布前后验证 held identity，不影响原文件，失败只清自己 staging。 | 需 Windows name policy、DACL/只读语义、发布/清理 sharing；`safe_filename` 目前仍接受 CON、尾点/空格、`*?<>|` 等 Windows 无效/特殊名。 |
| `desktop_app/file_preview.py:417-439,472-493` | 每个资源 no-follow、regular-file 验证、预览授权与读取同一对象。 | 直接 O_NOFOLLOW/O_CLOEXEC 和 dir_fd 不可用；不能用一次 `resolve()` 替代跨 await 的物理对象绑定。 |
| `tools/builtins/filesystem.py:1456-1620` | replace 保留 mode；create/export 写后验证、hardlink no-clobber、明确 published false/true/unknown；不会覆盖既有目标。 | Win path/ACL、hardlink+dir_fd、open文件删除、directory flush 行为不同；保留 typed uncertainty 和权限 owner。 |
| `workspace_identity.py:35-52,148-159`; `capability/pulsara_home.py:93-177` | 用户级 home resolver、当前 cwd 不重绑 home、保存的 workspace canonical identity/availability。 | home 默认为 Windows userhome/.pulsara可保持；需关闭 drive-relative `C:foo`、root-relative、UNC/device path 的产品规则。`workspace_key` 的 path.as_posix 哈希对不同大小写 canonical spelling可能重复，盲目lower又会错并存的case-sensitive目录。 |

Python 官方明确 Windows3.12 `dir_fd` 不可用，chmod只切只读位而不能表达用户/组权限。[Python3.12 os](https://docs.python.org/3.12/library/os.html)。Windows 保留设备名、ADS、UNC/设备命名空间、case sensitivity和长路径必须按官方文件命名语义处理。[Windows命名规则](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file)

### 建议边界（需要 G0 实证后定稿）

保持各产品 owner：settings/store/skill/plugin/import/preview/filesystem 仍决定“可写谁、接受什么、哪个结果”。把**现有 OS 机制**收口成很小的平台实现：bound directory/file handle、ordinary-file/reparse 检验、physical identity、private owner ACL、exclusive publication、flush、共享/独占 lifetime lock。优先用已锁 pywin32 暴露的 Windows API，不自造文件系统/事务层；若核心直接依赖 pywin32，应在 pyproject 声明带平台 marker 的直接依赖，不能依赖 MCP 的偶然传递依赖。

已检查锁定 pywin32 312 对应的官方 b312 tag 源码：`win32file.i` 暴露 CreateFileW/LockFileEx/MoveFileEx/SetFileInformationByHandle，`win32job.i` 暴露 Job API。因此“必须复制一套低层 Windows 框架”没有依据。但 **CreateFileW 单次 OPEN_REPARSE_POINT 只处理最终对象，不能凭名称证明所有祖先未重绑定**；当前包源冻结/删除/导入要求更强，必须用真实 Win 探针证明完整链的 pinning/relative handle 打开方案和 no-clobber 发布。已确认 b312 `win32file.i:5039-5071` 的 `SetFileInformationByHandle(FileRenameInfo)` 接受 `{ReplaceIfExists, RootDirectory: PyHANDLE, FileName}`，RootDirectory 是 destination directory；这是 bound no-clobber rename 的具体候选，须优先验证，不能直接自写rename wrapper。其他info-class仍不能仅因Windows C API存在就假设binding已支持。确有缺口时只加该调用的小适配，或向依赖提供扩展点；禁止将 path.resolve+lstat 先验检查作为等价实现。

参考：[pywin32文件绑定源码](https://raw.githubusercontent.com/mhammond/pywin32/b312/win32/src/win32file.i)、[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)、[SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle)。

G0 文件 gate：至少 NTFS 本地卷、普通用户，强制对抗 source parent/junction/leaf/stage/root 替换；其他用户不能读取 settings secrets；设置 publication fail-before/after点；inspect后替换 target绝不删除替代物；双publisher仅一胜；no-clobber export；打开预览时删除/重命名；长路径/中文/emoji/空格/case-sensitive目录。非NTFS、UNC/网络卷的承诺需规格明确，不可把一类本地成功推广到所有文件系统。若未支持要 typed unavailable，不能默默跟链接或覆盖。

## 4. 非终端进程、MCP、Hooks 与浏览器：P0/P1

`conversation_kernel/mcp/sdk_facade.py:305-500` 的 `_BoundedStdioTransport` 自己用 asyncio spawn、`start_new_session=True` 和 killpg。`:332-339` 默认环境只继承 HOME/LANG/LC_ALL/LOGNAME/TMPDIR/USER，原生 Windows 缺 SYSTEMROOT/TEMP/USERPROFILE/PATHEXT 等；Windows自定义env必须保留必要系统变量，不能借修平台把真实调用者凭据全透传。[Python subprocess](https://docs.python.org/3.12/library/subprocess.html)

`hooks/executor.py:178-201,249-252,516-525` 已有 commandWindows/CREATE_NEW_PROCESS_GROUP/cmd env引用转换，但 terminate/kill 只杀根 process，**CREATE_NEW_PROCESS_GROUP不等于子树清理**；子进程继续持有stdio/副作用，现有 asyncio drain/close可能挂住。`conversation_kernel/visualization_screenshot.py:27-66` 同样 POSIX spawn+killpg；worker `visualization_screenshot_worker.py:98-102` 通过Playwright启动Chromium，取消/timeout/appclose必须清完整树，不只是Python worker。

复用边界：MCP SDK ClientSession继续拥有协议、初始化/响应/订阅；Pulsara保留frame/depth/byte/resource admission、credential boundary、side-effect classification、generation/reconnect与安装prefix冻结。Playwright继续拥有浏览器revision和浏览器控制；Pulsara只提供该次工作的process生命周期。

**已读安装的 mcp2.1.0 实现，不能声称 SDK 无Windows支持：**

- `.venv/lib/python3.12/site-packages/mcp/client/stdio.py:114-221` 的 public `stdio_client` 使用default-env合并、TextReceiveStream按行累积和SDK parse；没有 maximum_frame_bytes/depth 或在进程创建的最后临界区执行Pulsara credential_guard的注入参数。外层收到message后再检查已经太迟，不能为了复用删除现有有界parser/credential守卫。
- 同 SDK `mcp/os/win32/utilities.py:154-199` 的 create_windows_process 已有AnyIO+Job Object；但先运行child再Assign，源码明确记载早期后代可能逸出，建议 CREATE_SUSPENDED→Assign→Resume；job创建/Assign失败日志后允许无Job继续。Pulsara若承诺完整树回收，这个失败路径不能直接继承。
- SDK get_windows_executable_command可解析 `.cmd/.bat/.exe/.ps1`；不能据此保证 npx.cmd/Unicode/metachar参数执行语义或无注入。需测试实际child argv、lookup_path/PATHEXT和引号。

G0先验证能否在SDK支持的transport/process边界注入一个**checked Job scope**而保留Pulsara字节边界。优先推动/采用维护中的公开扩展点；若没有，则最小process适配只负责创建、管道、Job归属、退出回收，不复制MCP状态机。Windows底层job/handle由pywin32/Windows负责，Pulsara owned process scope必须fail-closed：无法建立归属时不得返回“启动成功”。若需要CREATE_SUSPENDED补实际gap，应在spec写清asyncio/SDK现有不支持的exact hook，而不是泛称“Windows特殊”。不能用PID树快照或taskkill成功当作精确物理所有权证明。[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

跨MCP/hooks/preview复用同一窄process mechanism，产品各自deadline/cancellation/output边界不合并。不得新增总工具次数/总活跃时间/总输出caps，保留stderr按chunk读取的长时可用性（sdk_facade.py:439-453）。Hook authored timeout仍是其物理操作边界；MCP长寿命进程不能被一次探针时限变成产品寿命限制。

Playwright官方支持Windows；这不证明随包复制、Chromium DLL、设备丢失或Job强杀已覆盖。[Playwright Python](https://playwright.dev/python/docs/intro)。bundle必须用locked SDK的浏览器revision；验证搬移后离线渲染HTML/SVG、中文字体、timeout/cancel后无chrome/node/python遗留，再通过现有preview HTTP授权链读取结果。

## 5. 后端 IPC、OS锁、signals：P0

- `desktop_runtime.py:31-46` kernel lock 无条件import fcntl、getuid、O_NOFOLLOW；不能获得single owner。`:150-158,297-298` diagnostics是Unix socket；`:211-216` 忽略Windows add_signal_handler不支持，主关闭必须依赖已有stdin EOF/`shutdown`帧，不可宣称SIGTERM可用。
- `terminal_protocol/v3_gateway.py:192-204` + `desktop_app/protocol_client.py:105` 虽叫terminal protocol，却承载整个desktop conversation/session bridge，**属于后端启用阻塞**，不是只有终端tool用到。asyncio Windows不支持start/open_unix_connection；不能因为Windows系统存在AF_UNIX而推断asyncio支持。[Python asyncio Windows](https://docs.python.org/3.12/library/asyncio-platforms.html)
- `plugins/package_store.py:1042-1083,1521-1539` fcntl None时anchor抛错/package永远in_use；shared live-consumer 与 exclusive uninstall/publish需要保持。`hooks/trust.py:396-415` fcntl None时直接yield，反而静默失去跨进程源锁；这条降级必须移除。

建议：kernel/package/hook源锁统一复用OS-backed窄锁机制，采用pywin32 LockFileEx shared/exclusive与本来operation deadline，验证close/crash释放、dup语义、两个进程互斥、持有consumer时不能卸载。不要新增lease、generation、lock数据库表。locked handle本身就是生命周期owner；不能unlink/recreate锁文件躲开现存锁。

内部Protocol-v3可以复用asyncio loopback stream + 既有launch capability/frame/attachment owner，或用Windows named pipe加SID DACL；G0选定一个唯一Windows transport。无论哪种都不要实现第二套协议。诊断CLI目前仅靠private socket访问控制，迁移loopback必须补等价admission（例如复用原有受保护endpoint描述/launch secret owner），不能公开未鉴权DB诊断服务。若选择pipe，先核对asyncio/pywin32支持的stream adapter、取消与EOF，不手写IOCP loop。父子JSON-lines控制继续现有shutdown/EOF→application close顺序，Windows也要证明stdout破裂不能绕过cleanup。

## 6. 工作包与验收

以下为一位熟悉Pulsara的工程师的**人日估算**，不含终端ConPTY和Electron安装UI主体，不是交付保证；原生pg_search失败修上游的时间未知，必须单列。

| 包 | 所有者/工作范围 | 交付/激活条件 | 估算 |
|---|---|---|---|
| G0 可行性 | build资源owner + storage/migration owner + OS filesystem/process owner | Win25H2x64普通用户实机：locked cp312 install、PE/rg、PG17.9+固定扩展+完整baseline、handle nofollow/no-clobber/ACL竞争、Job完整树与取消，保存原始命令版本输出 | 5–10人日；DB修复额外未知 |
| B1 资源/分发 | tools/hatch_build + prepare_resources + mise | 唯一Win资源布局、离线wheel安装、安装路径搬移、中文/空格/长路径、DLL依赖与licenses、目标Python预编译，mac/Linux保持 | 4–6 |
| B2 安全文件/锁 | local_source_binding/exclusive_publish 的窄平台机制，现有settings/package/skill/import/preview/fs owners各自接入 | 所有dir_fd调用点有明确归属；所有上述竞争和uncertain publication保留；无fnctl None无锁降级；case/path规则闭合 | 8–14 |
| B3 进程/MCP/hooks/preview | OS process scope +现有各产品owner | Job失败拒绝admission、spawn cancel race、blocked stdin、孙进程握管道、根早退、父进程退出、preview timeout，所有side-effect分类不变 | 5–8 |
| B4 managed DB/IPC | Electron PG owner + desktop_runtime + protocol gateway | endpoint认证/最小角色、lock、connect、迁移、诊断CLI、ready/failed、shutdown/reopen、parent-loss；用户页保持用户级owner | 4–7 |
| B5 CI/实机激活 | workflow/release owner | 原生Windows wheel+全suite+随包安装搬移+真实provider dogfood+long-horizon；现有Ubuntu/mac回归保持；文档只描述新真相 | 4–6 |

合计约 **30–51人日（含G0，不含上游扩展修复/终端/安装UI）**。工作包可按owner并行但B1–B5不能以“稍后补安全/DB”绕过G0。Windows分支是平台实现，不是old/new兼容机制；同一产品owner只有一条已选择平台路径，删除被替代的Windows placeholder/无锁降级。

G0结果必须有 PASS / FAIL / UNPROVEN 区分，记录精确命令、版本、目标OS/CPU/文件系统、输入、stdout/stderr与可复现失败。PASS后规格才能补入已验证primitive选择和固定native recipes；失败不添加白名单/skip或降低断言。生产support宣称要等B5完成。

### 测试层次

1. **G0原生物理探针**：DB/handle ACL/Job、Win命名、locked dependencies。macOS monkeypatch不能替代。
2. **既有合同跨平台化**：`tests/test_settings.py`（完整写入与unknown outcome）、`test_local_skill_removal.py`（stale绝不删除）、`test_local_skill_management.py`、插件安装/生命周期/源冻结tests、`test_local_file_import.py`、`test_session_file_preview.py`、`test_mcp_transport_lifecycle.py`、`test_mcp_sdk_transport_probe.py`、hooks executor/trust tests、`test_desktop_runtime.py`。依平台提供真实fixture，测试产品结果，不模拟Windows安全常量为零；共享语义不得skip。
3. **Windows DB suite**：不得用CI service-container YAML假装原生Windows PG（现 `.github/workflows/ubuntu-tests.yml` 只有Ubuntu服务容器）。Windows job实际启动新bundle的PG，解析并确认 disposable本机目标后跑baseline/postgres tests；资源测试从安装目录外执行。不能改用户保存设置或连接未确认远程目标。
4. **打包/离线与重定位**：`tests/test_private_ripgrep_packaging.py:65-69` 当前只有Darwin/Linux tag；更新测试期望真实Win PE与rg.exe；wheel/editable/sdist未prepare均拒绝、已prepare均可离线构建。`desktop/scripts/smoke_resources.py` 应在Win实机证明复制后的Python/PG/Chromium一起运行。
5. **实机激活**：干净普通用户安装→首启→真实provider工具调用（file/search/write/hook/MCP stdio+HTTP/预览/检索）→cancel/quit/reopen→user-level capability/memory/schedule页；大量turn/tool/queued subagent/compaction/message下保持prefix append-only与长时可用，不能引入总寿命cap。Win ARM、无GPU、Defender占用、sleep/resume等作为后续扩大矩阵依据。

`.github/workflows/desktop-package.yml:43-57` 当前仅mac arm64/Linuxx64/arm64；`:71-101` 有Unix命令和`.venv/bin/python`。Windows应新增实际runner+native resources/安装smoke，不借WSL运行suite然后贴native通过。现有workflows全部manual/workflow_call语义可保持，是否自动触发是独立产品决定。

## 7. WSL2 备选边界与成本

若用户另行选择WSL，应称为**Windows桌面+指定WSL发行版的完整Linux后端模式**，而不是终端fallback。Python/PG/pg_search/MCP/hooks/Skills/preview/工具工作目录全部运行同一个Linuxowner域，避免Win Python访问LinuxPG与双home/双设置。

- home/settings/plugin store/PG data应放WSL Linux FS，不能直接把Windows `%USERPROFILE%/.pulsara` 映射为同一个private home。DrvFS默认不启用Linux权限metadata，chmod对其他Win账户的真实访问不是POSIX等价。[WSL permissions](https://learn.microsoft.com/en-us/windows/wsl/file-permissions)
- Win目录选择、拖入、file open/reveal、导出destination和CLI分别需要明确host path↔backend path转换；不得让`C:\\...`进入Linux Path后成为相对路径，也不得把`/mnt/c`和`/home`当等价权限根。workspace canonical identity、trust、插件来源路径只能有一个权威，显示path可另有host representation。
- 固定发行版/user/resource版本，已有发行版不可随意重置；安装/启用WSL需要其自身权限/reboot/虚拟化条件；不得接管默认发行版或用户PG。PG lifecycle由该发行版内既有OS进程owner管理；Windows退出wsl.exe不证明后端已死，不可全局`wsl --shutdown`清别的工作。
- Electron→backend loopback HTTP可考虑WSL localhost forwarding，但NAT/mirrored/firewall/localhost重用行为必须实机验收，不开放0.0.0.0来解决连接失败；双端原生文件读取/URL token不能漏到renderer。[WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)
- Linux运行工具时，WSL原生FS比跨`/mnt/c`更适合性能；WindowsFS上的junction/case/links/executable语义仍不能靠Linux路径名假定安全。[WSL filesystem guide](https://learn.microsoft.com/en-us/windows/wsl/filesystems)
- MCP npx/uv、hooks commandWindows选择取决于执行域而非Electron平台；WSL模式应选Linux命令，不能将Windows插件可执行与Linuxpackage contract混合。

初始完整WSL产品模式后端侧约 **15–25人日**（安装配置、WSL supervisor、path/native-file桥、网络、更新/关闭与实机矩阵），还需Electron安装UX与WSL资源发布成本；不是“几个if”。可能比完整原生迁移更快，但牺牲native工具/工作目录的直接语义，应独立规格和用户选择。本次不实现、不默认启用。

## 8. 本地先例与不能照搬的部分

已先读ZCode根和`packages/services/AGENTS.md`，运行根规定的只读freshness检查：main与origin/main同步、ahead0/behind0。grok-build全树未发现AGENTS.md（只读rg扫描）。

- ZCode `packages/services/src/runtime-tools/runtimeCommandEnv.ts:129-153,181-205` 处理Windows PATH大小写、UTF8环境与Node目录；`runtimeToolResolver.ts:30-52` 处理PATHEXT。这些是Pulsara MCP/home/path环境测试用例来源，不能照抄它的system PATH fallback来替代Pulsara私有resource contract。
- ZCode `packages/services/src/process/processTreeTerminator.ts:152-203` 与`windowsTaskkillRunner.ts` 已有异步taskkill、root identity观察、绝对cleanup deadline；可借鉴“原始根身份与exit观察不能混淆”及cleanup时序测试，但Pulsara完整树ownership必须先用OS Job/句柄，不因先例存在就引入PID快照恢复框架。
- grok-build `crates/codegen/xai-grok-file-lock/src/lock.rs:27-109` 复用Rust std File::try_lock、按operation deadline轮询，`locked_file.rs` 明确dup与unlock的所有权后果；支持优先复用OS锁思想。它额外acquire-slot/network-FS机制不是Pulsara当前独立产品需求，不能照搬。
- grok-build `crates/codegen/xai-grok-update/src/windows_payload.rs` 将Windows hook exe和MinGit作为独立payload验证/安装；可借鉴真实Windows `.exe`/DLL目录验证，而非把shell/解释器字符串替换当分发支持。其best-effort update/旧payload保留策略不符合本轮Pulsara完整hard cut，不复制。

## 未关闭项（必须出现在根规格G0清单）

1. pg_search0.26.1+PG17.9完整Win构建/运行/重定位；需要上游patch时范围、license/source再分发和owner。
2. 精确all-components no-follow、anchored rename/no-clobber与Windows打开句柄sharing策略；bindings能否满足，若不能最小native API gap是什么。
3. SDK/AnyIO/pywin32支持的原子Job归属启动、管道与async取消扩展点，SDK job fail-open与早期fork窗口如何关闭。
4. managed PG本地认证与凭据bootstrap、runtime/admin endpoint真实性；不以端口号或任意localhost作为authority。
5. internalProtocol-v3/diagnostic transport唯一选择与等价访问控制；Windows控制stdin/EOF与shutdown结算。
6. Win路径canonical identity规则：NTFS case-sensitive目录、reserved/device/ADS/UNC/long path、home是否允许网络卷，各自typed outcome。
7. hooks cmd/随包PowerShell7.6执行关系（终端报告负责shell选择）；MCP `.cmd/.ps1` 的命令查找与精确argv；不得改变authoring的shell契约。
8. 原生Win测试机/runner与干净安装激活环境尚无本报告证据。G0完成前“可以实施”只代表路线闭合，不代表风险已解。
