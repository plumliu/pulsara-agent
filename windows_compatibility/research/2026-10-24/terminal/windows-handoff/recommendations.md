# Windows 开发准备 / G0-W-C 交接建议

2026-10-24；terminal专题。只读检查冻结规格、mise/资源构建/CI/当前runtime；未改根文档/源码/tests，未运行Windows/WSL实验。本文件供root写新手清单，不是已通过的Windows资格。

## 1. 两份代码与当前能做什么

建议用同一Git提交的两个独立clone，示例位置：Windows `C:\src\pulsara_agent`，Pulsara发行版 `/home/pulsara/src/pulsara_agent`。Windows是Electron/Node宿主代码，WSL是Ubuntu24.04 x86_64 Linux资源与Python内核代码。`/home/pulsara/workspaces`是未来产品工程根，不等于开发仓库一定要放工程根中。

必须独立生成 `.venv`、`node_modules`、`desktop/build`、`desktop/resources` 和dist等平台输出，绝不从Mac/Windows复制到Linux，也不把WSL clone放 `/mnt/c`。两个clone比对Git commit即可，不生成工作树/文件hash注册表。Windows Node架构名x64、Linux `uname -m`为x86_64，不能混淆字符串。

当前Phase A没有WSL host实现。Windows只可先安装依赖、编译TypeScript、做明确适用的host探针；**不能运行现有 `mise desktop:resources/verify/package`或 `npm start`后声称Windows桌面已经接WSL**。`prepare_resources.target_platform()`只接受Mac ARM64和Ubuntu24.04 x86_64/aarch64。现`npm --prefix desktop test`也含真实POSIX shell/PG/uid fixture，不能列为已支持的Windows整套测试。

WSL内可准备当前Linux资源并跑Linux测试；这是目标WSL中的Linux代码/资源证据，不能记作native Ubuntu图形验收或WindowsUI支持。此次清单本身不进入B，不修改生产adapter、不运行provider。

## 2. Linux依赖：照当前CI，不照历史handoff

实际authority是 `.github/workflows/desktop-package.yml`、`mise.toml`、`desktop/scripts/build_environment.py` 和 `prepare_resources.py`。当前CI的Ubuntu工具包为：

```sh
apt-get update
apt-get install -y build-essential bison flex pkg-config binutils patchelf libopenblas0 xz-utils
```

首次Ubuntu环境另需 `ca-certificates curl git` 才能取得项目和工具。APT步骤是专用发行版显式维护root操作；日常开发/构建/运行必须回到pulsara nonroot。不要为此给模型免密sudo，也不要root运行uv/npm/build导致项目文件归root。

对应关系：cc/make（build-essential）、bison/flex/pkg-config用于PG原生构建；readelf/ar（binutils）、patchelf/ldd/dpkg-deb/dpkg-query/xz用于Linux依赖收集、解包、ELF修补与许可证来源。Ubuntu24.04基本镜像提供dpkg与glibc loader；缺哪个工具以build_environment明确错误定位，不绕过检查。

版本从已提交文件读：mise min2026.7.18（当前CI用2026.10.15），Node22.23.3、uv0.13.4、protoc34.0；`.python-version`为3.12.15。不以历史Node22.13/uv0.10.7或系统Python3.12.x代替实际pins。按官方方式装mise、trust当前repo、安装所声明工具后，可显式 `uv python install 3.12.15`，再跑原 `mise run desktop:bootstrap`。不要删锁文件/放宽版本来让build_environment通过。

Playwright OS库需要bootstrap后使用这个checkout的uv Python安装：现CI为 `uv run --no-sync python -m playwright install-deps chromium`；若pulsara无sudo，应由显式root维护入口调用该项目实际 `.venv/bin/python -m playwright install-deps chromium`，不把后续开发整体切root。浏览器payload下载与复制则继续普通用户的 `mise run --jobs 1 desktop:resources` graph完成，无需手动全局安装Chromium。

当前PG17.9由源码configure/make构建（明确without-icu/readline/zlib），vector0.8.1由PGXS构建，pg_search0.26.1取固定官方Noble/PG17 `.deb`并校验/提取；**不需要Rust/pgrx或系统PostgreSQL服务作为本路线前置**。不要装PG16/Docker PG取代bundled PG，也不要手工换pg_search PG18包。

`tests/linux/HANDOFF.zh-CN.md`是2026-10-03历史文档，含旧web_app/native_desktop/Zenity、PG16/Docker及旧Node要求；不能直接照它写新WSL主路线。`tests/linux/Dockerfile`只是backend/package validation，不是目标桌面/WSL安装recipe。

## 3. guest最早检查和资源验证顺序

新手每组命令须标明Windows PowerShell或WSL nonroot/root。Linux先观察：

```sh
id
uname -m
cat /etc/os-release
pwd -P
findmnt -T . -o TARGET,SOURCE,FSTYPE,OPTIONS
git status --short
git log -1 --oneline
```

确认普通pulsara、Ubuntu24.04、x86_64，clone在实际ext4；不要用路径长得像 `/home` 就推断filesystem，额外bind mount要检查。GNU `stat -f`的名称不宜当作ext4字面字符串资格，`findmnt`更直接展示FSTYPE。

工具与APT就绪后，在guest仓库根普通用户执行原graph：

```sh
mise run desktop:bootstrap
uv run --no-sync python -m playwright install-deps chromium
mise run --jobs 1 desktop:resources
npm --prefix desktop test
.venv/bin/python desktop/scripts/test_linux_dependencies.py
.venv/bin/python desktop/scripts/smoke_resources.py
```

install-deps需要上述明确root维护安排；其余不能sudo。`desktop:resources`会构建frontend和installed wheel，并准备私有rg/PG/Python/Chromium；不能只`uv sync`后跳过私有rg和静态资源。`smoke_resources.py`只拥有自建临时PG，不读保存settings/用户DB，但会实际启动自己的PG，清单应明确这一点。WSL无GUI时不运行Linux Electron GUI/不要求WSLg；WindowsUI和跨域控制是独立G0探针。

准备阶段正常会联网取得固定上游包；首次资源构建时间/磁盘占用显著，原脚本`make -j cpu_count`是CPU物理并行，不加任务总时长cap。下载/包/ELF缺依赖应保留具体错误，勿关TLS验证、强改pin、force cache或从其他OS复制二进制。

## 4. G0-W-C：现有路径与尚需一次性探针

现 `PythonRuntime + posixHostOperations`只会本机spawn Linux/POSIX路径，没有Windows→wsl实现。G0探针必须放output个人目录，Windows Node真实 `spawn` 系统wsl.exe绝对路径，`shell:false`、明确 `-d Pulsara -u pulsara --cd ... --exec`和pipe3；不得用cmd/PowerShell字符串包装或PS文本pipeline证明byte行为。Linux内terminal需要PTY由原manager创建，与外层JSONL pipe分开。

最早可做的零PG用例：专用短canonical Linux临时home、包内Python `-m pulsara_agent.desktop_runtime`，无start帧、结束stdin，观察真实EOF→stopping/stopped→Linux正常exit0，收齐Windowsclose。`PULSARA_HOME`只在guest明确设置，不继承Windows同名C盘值；路径不是secret，可以通过受控 `/usr/bin/env` argv设置，但模型key/DSN/凭据不入launcher argv/环境。

后续G0-C矩阵应逐项留命令/实际stdout/stderr/退出/owner事实，不能先写passed：

| 用例 | 合格观察 |
| --- | --- |
| literal argv/UTF8 | 中文/空格/引号/反斜线/空参数准确；分chunk UTF8与大量stderr不污染JSONL；真实Linuxcode0/1/7分类清楚 |
| start前EOF/准备失败 | 未发start即不触DB；child真退出、start原错误不被别的ready错误覆盖；原wait-owner可完成，无活kernel/持有锁 |
| frame前后取消 | 区分未提交/可能已发生，不重发operation/write；helper ready-before-operation属于尚待资格的未来B接缝，不假称已有生产模块 |
| pipe/carrier丢失 | Windowsclose只表示carrier/handles；还须观察Linux root/原PGID/worker或原kernel锁，不凭taskkill宣布joined |
| 正常关闭 | 原aclose后无收尾failed+Linuxexit0+完整本次帧/pipe；stopped或锁释放单独不能证明成功 |
| sleep/reopen/外部terminate | 暂断不等于取消；恢复只认同一live身份；强制distro停是crash/unknown，不当正常cleanup；重开先原wait-owner，不新起并发kernel |
| terminal实机 | PIPE/PTY cat、submit/EOT、root先退同group child延迟stdout、SIGTERM忽略/kill、原drain/reader/watcher/Timer/槽语义，不能只测shell echo |

所有故障注入只针对本次可验证的probe PID/distro/home，标签明确为外部fault input。**正常Quit不用taskkill/wsl --terminate/--shutdown**。全局shutdown会影响其他用户WSL工作；carrier kill只是单项实验，不是恢复脚本。当前helper握手/TLS/picker代码尚未实现，G0可做独立技术spike，不能为填清单偷实现B。

## 5. Linux-only / interop负测不可误判

冻结配置为专用Pulsara/nonroot，`[interop] enabled=false; appendWindowsPath=false`，`[automount] enabled=false; mountFsTab=false`；配置更改只在专用环境正常关闭后的明确维护边界重启生效，不动用户全局.wslconfig/default distro。

- 只清PATH或WSL_INTEROP无效证明：interop还会按父PID找server。必须测试平台实际拒绝Windows PE执行。
- automount关闭后，直接执行 `/mnt/c/Windows/System32/whoami.exe`得到ENOENT，只证明文件不可达，不能记作interop通过。先用明确stdin制品传输把无害已知PE放到自己的ext4 probe目录，确认它存在/可读，再验证执行被拒；不为负测开放全局interop或挂用户盘。
- 其他用户distro可保持原interop配置；专用配置不能冒充恶意Windows用户/root沙箱，也不把原POSIX process group说成整个Linux发行版全部进程scope。

官方依据：[WSL boot/relay](https://wsl.dev/technical-documentation/boot-process/)、[interop](https://wsl.dev/technical-documentation/interop/)、[wsl.conf](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)。它们描述平台机制，尚无本轮Windows实测。

## 6. 清单建议的退出状态

环境准备完成、Linux资源smoke通过、G0-D/C/P/F/N各自通过、Windows产品激活必须分开。当前明确可达的是依赖准备/资源构建与后续独立G0 spike；没有host-wsl/guest helper/TLS/picker/Windows包装，所以不能给新手一条`npm start`假装产品已上线。

生产长程任务/provider不在环境准备中自动启动。保存settings仍Linux有效home的LocalSettingsStore唯一owner，缺连接如实提示；不要复制Windows/macOSsecret，不要求把APIkey export到shell。Linux CLI `pulsara app`不能通过interop打开WindowsGUI，首版沿规格提示Windows图标；这里也不自动改Windows PATH或用户现有CLI。
