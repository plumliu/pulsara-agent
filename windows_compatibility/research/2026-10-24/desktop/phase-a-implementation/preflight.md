# Phase A desktop实施前只读盘点

日期：2026-10-24。执行依据：冻结WSL-only根规格§3.2/11.1/13.1；用户只授权本轮Phase A。当前仅建立个人工作区、读取代码/测试/环境；未编辑生产、运行测试、改发布/版本、启动/停止应用或触及保存配置/DB。生产编辑等待root改前baseline与统一契约确认。

## 最小locator与main装配建议

纯`DesktopResourcePaths`（readonly）五值：`root`、`pythonExecutable`、`playwrightBrowsers`、`postgresBin`、`packageCli`。实现只`path.join`当前实际布局：`python/bin/python3`、`playwright`、`postgres/bin`、`bin/pulsara`；无fs/读取配置/探测平台/进程/manifest/registry。main一次解析对象，runtime/PG/CLI消费同一结果，不能各从root重新拼Python位置。

现真实caller：main同步resolveHome；runtime.waitForPreviousOwner/start；cliLauncherScript；postgres.run；main在原linux且root.startsWith('/opt/')时的package CLI target。没有当前消费者的B路径/Win字段不建。主入口保同步home解析调用时点和原错误dialog/exit，boot/reset/quit内部顺序不改。

terminal建议HostOperations：同步resolveHome(pythonExecutable)保execFileSync；stream spawn固定pipe3；finite execFile保promisify的真实rejection/code；prepareRuntimeDirectory(recursive700)；appendRuntimeLog(append600)。PythonRuntime(paths,home,onEvent,host)必传host，无默认旧签名fallback。

backend建议PostgresOperations持唯一directory/socket/logPath及有限操作，由createPosixPostgresOperations(paths.postgresBin,home)创建；BundledPostgres只收必传operations，owner无fs/exec或默认选平台，不传两套可不同home。具体公共类型由root拍板后实施。

CLI脚本将消费同一已解析pythonExecutable，shellquote算法、shebang/marker、`exec ... -m pulsara_agent "$@"`、结尾换行逐字不改。注册的`.local/bin`、`/usr/bin`、package ownership、marker/outdated/conflict与PATH检查不改。全部真实caller/test同片hard cut，无旧root签名兼容。

## 已读取的代码与测试边界

- main/runtime/postgres/cli完整实际caller，`main.test.ts`的cover/oldowner/PG/startframe交接和picker回归，`cli-install.test.ts`真实执行参数/退出码、afterPack无display分发、registration/conflict/symlink/package/PATH。
- `mise.toml`当前native recipe只含prepare_resources.py及固定inputs；本轮不拆recipe，无须新增无消费者resource_build模块。TS源码在现build全量编译；若另引buildleaf必须同时追踪真实cache输入，不靠force。
- `desktop/scripts/test_build_cache.py`验证mise热缓存/应用轮换/重定位；`smoke_resources.py`净化环境+搬移资源/PG扩展/Chromium，不能代替installedGUI。
- `startup-cover-smoke.cjs`实际Electron production preload/cover/IPC；`browser-owner-smoke.cjs`实际BrowserOwner独立profile但输出固定`output/playwright/browser-owner-integration`，如运行须先归档baseline避免覆盖。
- `tests/linux/installed_package_smoke.py`checkout外wheel来源与原HTTP/CLI行为；native Ubuntu CI与真正图形session另有资格，容器不能代替。
- DEVELOPMENT声明手动workflow，当前alpha.6 MacDMG，保留release集合DMG+两个deb；desktop:verify/desktop:package流程不publish。

## 本机可用环境（只读，尚非通过证据）

- OS：Darwin arm64，macOS26.2，build25C56。Node22.23.3、uv0.13.4、mise、npm可用。
- `desktop/release/Pulsara-0.1.0-alpha.6-macos-arm64.dmg`存在；`desktop/release/mac-arm64/pulsara.app`与`/Applications/Pulsara.app`的CFBundleShortVersionString均0.1.0-alpha.6。没有挂载中的DMG（hdiutil info无image条目）。
- `/Applications/Pulsara.app`当前已运行，主进程、其随包Python/PG均可见。它是用户现有实例，不操作/退出，不把本轮测试接到其home/DB。相同version不证明其对应当前Git内容，不能拿旧安装产物冒充candidate。
- repo `desktop/resources`存在：Python3.12执行文件、PG postgres和pg_search.dylib实际均Mach-O arm64；Playwright目录存在。已安装app对应Python/pg_ctl/Playwright资源亦存在。只是文件/架构盘点，没有执行这些程序。
- Docker Desktop daemon可访问，报告linux/aarch64；context名default/desktop-linux。可以作为容器userspace补充，不是native Ubuntu installed/GUI资格。
- command inventory未见podman/lima/limactl/multipass/qemu/Xvfb；常见VM路径UTM/VirtualBox/Parallels及~/.lima/~/.multipass均不存在。未发现已配置native Linux x64/arm64或Linux GUI入口；没有猜测SSH目标/读取SSH私有配置/登录外部机器。
- 所以本机可准备Mac原生资源/包/GUI验证，但Ubuntu native x64、arm64与Ubuntu图形资格需root安排真实目标或最终如实标“验证待补”。

本文件不包含实际凭据或settings正文，不生成文件/文档hash。后续代码及证据按各自实际来源、Git状态、命令/结果记录。
