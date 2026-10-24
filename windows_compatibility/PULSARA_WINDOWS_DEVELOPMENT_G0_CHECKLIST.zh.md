# Windows 开发准备与 G0-W 操作清单

日期：2026-10-24。面向第一次做平台迁移的开发者。依据：[当前实施 spec](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md) §4、§11、§13、§16。

2026-10-24 按用户授权修订：Ubuntu发行版本号不限，接入已有发行版及默认非root用户，保留interop、Windows PATH和自动挂载，不要求新建`Pulsara`/`pulsara`。本清单命令以已观察到的`Ubuntu`/`plumwsl`及`/home/plumwsl`为示例，其他用户须替换成实际确认目标，不能把示例当固定命名要求。依赖pins、ext4应用数据和完整G0验收仍适用；原四方审阅仅覆盖修订前清单。

**本文件是 Windows 接手操作说明。2026-10-25本机所选矩阵G0-W机制可行性已通过，当前验收看第11节及[最终报告](research/2026-10-25/root/g0-w-final/report.zh.md)。** 前面保留可复用操作模板及阶段记录，未打勾的模板不是当前门状态。Phase A代码与Mac实证冻结范围保持，原生Ubuntu x64/arm64与GUI按用户要求延期，Phase B未进入。生产仍有已记录的跨域缺口，研究候选通过不代表生产已修复。实验只在自有隔离目录和output，不实现生产安装器、WslHostOperations、正式picker或TLS/OAuth桥，不改用户共享发行版配置。

## 1. 先看懂两边分工

Windows 是桌面宿主；WSL2 是里面的一套 Linux 环境。Windows Terminal 是终端窗口，可以打开“命令提示符”或指定 Linux 发行版，它本身不是执行环境。

| 项目 | Windows 本机 | 已选 WSL2 / Ubuntu |
| --- | --- | --- |
| 做什么 | Electron、Windows UI、wsl.exe 载体与网络探针 | Python 内核、Linux 终端、文件、PG、Playwright、Linux 资源构建 |
| 必需工具 | Git for Windows、mise、Windows Node/npm；WSL 系统组件 | Linux Git/curl/编译工具、mise、Linux Node/npm、uv/Python、protoc |
| 仓库建议位置 | `%USERPROFILE%\src\pulsara_agent`，本机普通目录 | `$HOME/workspaces/pulsara_agent`，真实 ext4 |
| 私有依赖 | 这份仓库自己的 `desktop\node_modules` 和 `desktop\dist` | 这份仓库自己的 `.venv/`、node_modules、dist、resources、build |
| 默认身份 | 普通 Windows 用户 | 已有默认非root用户，本机为`plumwsl`；仅明确OS依赖维护使用sudo |
| 编辑器 | 现有编辑器即可；可选 VS Code + WSL 扩展 | 经编辑器的 Windows 侧“连接到指定发行版”功能，或直接 Linux 编辑器 |

两份仓库是**同一项目的两个独立 clone**，用 Git 同步源码；不是维护两套业务代码。不要同时编辑同一个工作目录，也不跨系统复制 `.venv`、node_modules、resources、build、dist 或缓存。不要把 WSL 仓库放在 `/mnt/c`，不要把 Windows Electron 当 Linux Electron 运行。[微软文件系统说明](https://learn.microsoft.com/en-us/windows/wsl/filesystems)、[Electron Windows 开发说明](https://www.electronjs.org/docs/latest/tutorial/tutorial-first-app)

Windows 仓库避开 OneDrive/桌面同步目录；如果用户目录本身是重定向目录，另选自己可写的本机 NTFS 开发目录，后续路径一起替换。WSL 工程和实验数据不直接放到 C 盘映射目录。下面 `cmd` 块在 **Windows Terminal → 命令提示符**执行，`bash` 块在明确标注的 Linux 用户终端执行；不需要另装 PowerShell 7，也不把 PowerShell 用作模型 shell。

## 2. 当前锁定版本与不要提前装的东西

| 工具/资源 | 当前值 | 真相来源 |
| --- | --- | --- |
| 首批目标 | Windows 11 25H2 x64、WSL2、Ubuntu x64；Ubuntu发行版本号不限 | spec §1；记录实际Ubuntu版本并实测，其他Windows版本先记录差异，不自动扩大资格 |
| Node / uv / protoc | 22.23.3 / 0.13.4 / 34.0 | `mise.toml` |
| Python | 3.12.15 | `.python-version`；由 uv 管理，不用 Ubuntu 系统 Python 代替项目环境 |
| mise | Linux脚本固定2026.10.15；Windows WinGet记录实际安装版本，须不低于2026.7.18 | 当前 desktop workflow 与 `mise.toml`；不把WinGet当固定版本下载 |
| Electron / electron-builder / TypeScript | 44.9.0 / 26.15.3 / 5.9.3 | `desktop/package-lock.json`，用 npm ci 安装 |
| PostgreSQL / vector / pg_search | 17.9 / 0.8.1 / 0.26.1 | `desktop/scripts/prepare_resources.py`，不可换成 PG16 或 PG18 的扩展 |
| WSL包、kernel、已有发行版 | 尚无完整G0资格组合；本机版本已可观察 | 记录实际版本/来源，通过完整G0后声明支持组合 |

Windows 暂时不需要 Python、uv、系统 PostgreSQL、Docker Desktop、Rust/pgrx、整套 Visual Studio C++。Linux 构建当前直接使用固定 pg_search 官方 `.deb`，不要求自己编译 pgrx。若真实 npm/native 依赖报出编译缺口，再记录具体包并补它需要的工具；不要先装一套无关工具链。WSLg/Linux 图形应用不是 Windows Electron 的替代验收。

## 3. Windows 本机准备

### 3.1 记录机器，先不改现有 WSL

- [ ] 保存 Windows“设置 → 系统 → 系统信息/关于”中的版本、build、CPU 架构；确认是物理 Windows 普通用户图形会话。
- [ ] 查看任务管理器 CPU 页的“虚拟化”；未启用时按机器厂商说明在 UEFI/BIOS 开启。
- [ ] 在命令提示符观察现状，记录输出；没有安装时的错误也记录，不当作通过：

```cmd
whoami
ver
wsl --version
wsl --status
wsl --list --verbose
```

保留系统默认发行版、所有已有发行版和全局`%USERPROFILE%\.wslconfig`。首次默认选中当前默认Ubuntu，经用户明确确认后记录具体发行版/默认用户名；不新建专用用户、不覆盖同名对象，不unregister或自动改接另一个发行版。

### 3.2 安装 Windows 工具

可用官方安装器图形安装 Git for Windows 与 mise；有 WinGet 时在命令提示符执行：

```cmd
winget install --id Git.Git --exact
winget install --id jdx.mise --exact
```

安装器若要求 UAC，仅是安装维护；日常开发和实验回到普通用户窗口。重新打开命令提示符，检查 `git --version`、`mise --version`、`where git`、`where mise`。避免同一工具多个安装源互相遮盖。[Git 官方下载](https://git-scm.com/download/win)、[mise 官方 Windows 安装](https://mise.jdx.dev/installing-mise.html#windows)

### 3.3 克隆 Windows 仓库并只安装本机 Node

下面目录第一次使用时创建；如果仓库已经存在，先 `git status`，确认未提交工作再更新，不能覆盖 clone 或 reset --hard。

```cmd
mkdir "%USERPROFILE%\src"
cd /d "%USERPROFILE%\src"
git -c core.autocrlf=false clone https://github.com/plumliu/pulsara-agent.git pulsara_agent
cd pulsara_agent
git config core.autocrlf false
git rev-parse HEAD
mise trust
mise install node@22.23.3
mise exec node@22.23.3 -- node --version
mise exec node@22.23.3 -- node -p "JSON.stringify({platform:process.platform,arch:process.arch,version:process.version,execPath:process.execPath})"
mise exec node@22.23.3 -- npm.cmd --version
mise exec node@22.23.3 -- npm.cmd --prefix desktop ci --no-audit --no-fund
mise exec node@22.23.3 -- npm.cmd --prefix desktop run build
```

这里使用 `mise exec` 不依赖 shell activation；`npm.cmd` 也可避免 PowerShell 的 npm.ps1 执行策略问题，不要求放宽系统策略。若私有仓库需要登录，在两套环境分别完成 Git 凭据授权，不复制 Mac 钥匙串或把 token 填进 URL/命令日志。

Electron 44.9.0 的二进制可在首次使用时才下载。显式准备本机锁定二进制并只检查版本：

```cmd
mise exec node@22.23.3 -- node desktop\node_modules\electron\install.js
mise exec node@22.23.3 -- desktop\node_modules\.bin\electron.cmd --version
```

预期 Node 是 Windows x64，Electron 为 44.9.0；不要用 `npx electron@latest` 或设置架构变量伪装平台。下载失败保存具体网络/代理错误，先解决工具下载，不把它当业务失败。[Electron 二进制安装机制](https://www.electronjs.org/docs/latest/tutorial/installation)

可先跑这三个已核源码的平台无关测试文件，Windows 实际结果仍需记录：

```cmd
mise exec node@22.23.3 -- node --test desktop\dist\browser-policy.test.js desktop\dist\oauth-return.test.js desktop\dist\ui-preferences.test.js
```

这不是全部桌面资格。当前完整 Node suite 有 POSIX shebang、chmod、uid、symlink、`/tmp`、pg_ctl 等真实 fixture，不应在 Windows 批量 skip/改断言求绿；完整 suite 在 Linux clone 运行。

**现在不要在 Windows clone 执行 `npm start`、`npm run package` 或 `mise run desktop:verify/package`。** 当前 `main.ts` 仍装配 posixHostOperations/本机 PG；资源配方只接受 Mac/Ubuntu，且用 `.venv/bin/python`，安装配置仍只有 DMG/deb。tsc 成功不等于 Windows 主程序已接通；接通属于 Phase B。

## 4. 接入已有 WSL 环境（不是实现产品安装器）

### 4.1 安装/确认 WSL2

若尚未安装 WSL，在**管理员**命令提示符运行下列系统安装操作，按系统提示重启，然后回普通用户终端。已有 WSL 时先记录版本，只在确有需要时显式更新，不静默改他人的系统环境。

```cmd
wsl --install --no-distribution
```

重开后再运行`wsl --version`、`wsl --status`。现有安装需更新时显式记录更新前后及重启状态；不执行`--set-default`或`--set-default-version`。没有合适Ubuntu时由用户按官方入口安装，再回到目标确认；Pulsara不自动import或转换现有发行版。[微软安装说明](https://learn.microsoft.com/en-us/windows/wsl/install)、[WSL命令定义](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

### 4.2 确认发行版、默认用户和实际home

先从`wsl --status`/`--list --verbose`确认目标。本机已观察到`Ubuntu`为WSL2，默认用户`plumwsl`、UID1000、home为`/home/plumwsl`；其他机器先观察再替换下面示例。

```cmd
wsl -d Ubuntu --exec /usr/bin/id
wsl -d Ubuntu --exec /usr/bin/getent passwd plumwsl
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl --exec /bin/bash -l
```

默认用户须非root，实际home最终沿Linux原resolver解析，不能仅靠拼用户名推断。选择确认后所有应用调用显式给出目标；以后系统默认值改变不自动换目标。此次不下载/导入rootfs、不改默认用户、不新建`pulsara`用户，也不管理用户VHD。

### 4.3 保留互操作与挂载

只读观察`/etc/wsl.conf`及实际行为。允许并保留下面WSL配置值（缺省时沿WSL默认值），不是要求覆盖用户文件：

```ini
[interop]
enabled=true
appendWindowsPath=true

[automount]
enabled=true
mountFsTab=true
```

用户显式关闭时报告实际能力，提供手动开启指引，不自动修改、重启或终止其发行版。保留其他配置节、全局`.wslconfig`和用户工作。`/mnt/c`及Windows PATH可用不代表它们已通过Linux工作区/PG存储接纳；这些仍要求实际ext4。[微软配置定义](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)

本机已有基础观察：四项均开启（含未显式设置时的WSL默认值），Windows PATH存在，`/mnt/c/Windows/System32/cmd.exe /c ver`退出0。这只证明基本互操作可用，不证明Windows后代回收或整个G0通过。

### 4.4 当前用户开发依赖

以下在已选用户的bash执行；只有明确OS依赖安装使用sudo，不改sudoers、不以root运行uv/npm/initdb/模型，也不替换用户已有系统Python/Node/PG。

```bash
cat /etc/os-release
id -u
mkdir -p "$HOME/workspaces"
sudo apt-get update
sudo apt-get install -y ca-certificates curl git build-essential bison flex pkg-config binutils patchelf libopenblas0 xz-utils
```

依赖来自当前workflow/build_environment，所选Ubuntu上的包名/可用性须实际检查。现有sudo权限保留，不自动获取或保存用户密码。用户已有其他任务/服务均不停止；应用payload和各clone的依赖留在各自目录。

## 5. WSL 非 root 开发环境与仓库

以下在 **已选Ubuntu / 默认非root用户的bash**（本机为Ubuntu/plumwsl），先确认身份和真实文件系统：

```bash
whoami
id -u
uname -a
cat /etc/os-release
findmnt -T "$HOME/workspaces" -o TARGET,SOURCE,FSTYPE,OPTIONS
printf '%s\n' "$PATH"
```

应为非0 UID、Ubuntu/x86_64并记录实际发行版本，工程根位于真实ext4，不是DrvFs/9p/CIFS。PATH允许Windows目录；`command -v`仍须确认构建用的是锁定Linux工具。记录实际挂载和interop正测，后代/取消/断连边界另见G0-C。

Linux mise 使用和当前 CI 相同的版本，安装脚本先保存/查看再执行：

```bash
curl -fsSL https://mise.run -o /tmp/pulsara-mise-install.sh
less /tmp/pulsara-mise-install.sh
MISE_VERSION=v2026.10.15 sh /tmp/pulsara-mise-install.sh
export PATH="$HOME/.local/bin:$PATH"
mise --version
cd "$HOME/workspaces"
git -c core.autocrlf=false clone https://github.com/plumliu/pulsara-agent.git pulsara_agent
cd pulsara_agent
git config core.autocrlf false
git rev-parse HEAD
mise trust
mise install
eval "$(mise activate bash)"
uv python install 3.12.15
uv sync --locked --dev --no-install-project --default-index https://pypi.org/simple
```

`mise trust` 前确认 clone 来源；`mise install` 使用仓库 pins。[mise 固定安装版本及 shell 设置](https://mise.jdx.dev/installing-mise.html)、[uv 安装/环境说明](https://docs.astral.sh/uv/getting-started/installation/)

- [ ] 比较 Windows 与 WSL 的 `git rev-parse HEAD`，两边必须是同一提交；仅分支同名或版本号相同不够。缺失最新提交时先经 Git 同步，不能复制另一边的构建输出补齐。
- [ ] 检查 `command -v node uv protoc`、`node --version`、`uv --version`、`protoc --version`、`.venv/bin/python --version`；都应是 Linux 工具及上表版本。
- [ ] 今后每次新 bash 先激活 mise，或明确 `mise exec -- <命令>`；不要碰巧调用系统旧 Node/uv。

Playwright 的 OS 库需一次 root 维护。先退出到 Windows 终端，再明确进入这一个发行版执行：

```cmd
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec sudo /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python -m playwright install-deps chromium
```

这一步只装OS库，sudo需要时由用户在交互终端认证；浏览器资源、venv和构建仍由已选用户拥有。编辑器可从Windows侧连接已选发行版，也可按用户现有interop工具工作；编辑器是否可用与Pulsara应用接入资格分别记录。

## 6. 先验证 Linux 构建，再碰跨域链路

回到 **已选Ubuntu / 非root用户**，仓库根且mise已激活：

生产构建器`desktop/scripts/prepare_resources.py::target_platform()`已按用户授权删除Ubuntu发行版本号检查，原生Linux与WSL共用此路径；Ubuntu身份、架构与依赖校验仍保留。pg_search仍取固定noble制品，所选Ubuntu版本上的实际资源兼容性由下列构建和smoke验证，不能将版本准入取消记作实测通过。

```bash
cd "$HOME/workspaces/pulsara_agent"
mise run --jobs 1 desktop:verify
.venv/bin/python -m pytest -q tests/test_desktop_runtime.py tests/test_desktop_launch_directory.py tests/test_desktop_release.py
.venv/bin/python -m ruff check
```

这会准备真实 Linux Python/PG/扩展/Chromium、编译现有代码并做资源搬移检查；首次下载和编译会花时间，保存具体日志。macOS已通过的46条Node测试不是这里的实际结果；记录 Windows接手提交下 WSL 实际计数和失败，不改skip/断言消除差异。

这些结果只证明 **WSL 中原 Linux 代码/资源的相应场景**；不能消掉 Phase A 尚欠的原生 Ubuntu 双架构/图形资格。`npm run test:startup/browser` 若借WSLg运行，也不等于Windows Electron GUI。

每次PG/内核实验建立当前用户私有短路径home，例如`$HOME/g0-homes/r01`；先记录展开后的realpath/uid/socket和目标，完成后通过原owner停止并确认无进程/锁owner，再清理自己新建的目录。保留其他用户任务/软件/数据库；不复制saved生产配置、不打印credential。G0不要求真实provider；后续真实provider门继续适用spec只读settings注入规则。

## 7. 开始 G0-W：先做小探针，再做完整闸门

### 7.1 第一个探针：Windows Node → wsl.exe → Linux Python 的三条原始pipe

当前仓库**没有** `npm run g0-w`，也没有生产 WSL helper。下例是供 Windows 接手时手工新建的实验文件，不是已经实现/通过的自动化测试。

在 **Windows clone** 用编辑器创建 `output/windows-g0/probes/host-pipes.cjs`，粘贴：

```javascript
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const env = {...process.env};
// Windows环境键名不区分大小写，复制成普通JS对象后要逐键清除。
for (const key of Object.keys(env)) {
  if (key.toUpperCase() === 'WSLENV') delete env[key];
}
// 本机已确认目标示例；其他机器替换为自己已确认的发行版、用户和实际repo。
const repo = '/home/plumwsl/workspaces/pulsara_agent';
const code = 'import sys; data=sys.stdin.buffer.read(); sys.stderr.buffer.write(b"x"*(2*1024*1024)); sys.stderr.buffer.flush(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); sys.exit(7)';
const child = spawn(path.join(process.env.SystemRoot, 'System32', 'wsl.exe'),
  ['-d', 'Ubuntu', '-u', 'plumwsl', '--cd', repo, '--exec',
   repo + '/.venv/bin/python', '-u', '-c', code],
  {shell:false, stdio:['pipe','pipe','pipe'], env});
const payload = Buffer.concat([Buffer.from('中文 空格\r\n'), Buffer.alloc(1024*1024, 0x61)]);
const output = [];
let stderrBytes = 0;
child.stdout.on('data', chunk => output.push(chunk));
child.stderr.on('data', chunk => {stderrBytes += chunk.length;});
child.on('error', error => {console.error(error); process.exitCode = 1;});
child.stdin.on('error', error => {console.error(error); process.exitCode = 1;});
child.on('close', (code, signal) => {
  try {
    assert.equal(code, 7); assert.equal(signal, null);
    assert.deepEqual(Buffer.concat(output), payload);
    assert.equal(stderrBytes, 2*1024*1024);
    console.log('PASS basic pipe bytes / EOF / stderr drain / exit7');
  } catch (error) {console.error(error); process.exitCode = 1;}
});
child.stdin.write(payload.subarray(0, 1)); // 刻意从UTF-8字符中间分段。
child.stdin.end(payload.subarray(1));
```

在 **Windows 命令提示符、Windows clone根**执行：

```cmd
mkdir output\windows-g0\evidence
mise exec node@22.23.3 -- node output\windows-g0\probes\host-pipes.cjs > output\windows-g0\evidence\host-pipes.log 2>&1
echo %ERRORLEVEL%
```

紧接命令执行的`echo %ERRORLEVEL%`应为0，日志中应有PASS；把两者都保存才能勾选这个子项。2MiB/1MiB是本次探针输入规模，不是新产品输出/寿命cap。它只证明基本byte/EOF/stderr drain/退出码；**没有证明 JSONL framing、kernel关闭、后代join、取消或整个G0-C通过**。卡住时从另一个终端观察确切进程/锁/pipe并保留证据，不给probe加自动taskkill/global shutdown。

### 7.2 G0-W-D：已有目标接入与共享环境

- [ ] 保存Windows/WSL/kernel/发行版来源和软件包版本及Git提交；已选目标实际为WSL2 Ubuntu，允许使用默认发行版。
- [ ] 发现默认发行版/用户→明确确认→重开显式使用同一目标；默认值改变、目标缺失、root或未知用户均不静默换环境。
- [ ] 实际home下的workspaces和实验home真实ext4；记录interop/Windows PATH/自动挂载实际状态，Windows程序正测可复现。
- [ ] 准备已知Windows PE及后代探针，基本启动/退出作为本门子项；跨域取消、持pipe/父先退/失联和关闭结算按G0-C单独验证，不用Linux PGID推断。
- [ ] 普通Windows用户可启动，无日常UAC；安装payload不替换用户系统工具/PG、不修改用户配置或终止其他工作；安装失败/离线/磁盘满保留用户数据。

G0-D还须验证未来目标接纳和用户私有payload安装的失败边界；默认Ubuntu能登录或基本interop成功均不代表整门通过。当前不要求rootfs下载/import，也不自动创建用户。

### 7.3 G0-W-C：控制、EOF、取消与物理生命周期

继续在`output/windows-g0/`写有限实验，不把“给python前面加wsl.exe”接入生产。正式载体仍须`shell:false`、显式已选distro/user、明确Linuxcwd/可执行文件及三pipe；不让PowerShell文本管道替代byte relay，不传模型key。保留用户已有Windows PATH/interop能力，应用入口通过绝对Linux路径选定。

- [ ] 扩展7.1到真实随包 `desktop_runtime`：收到原start帧前不接触数据库；只用本次Linuxhome与本次PG DSN启动，不复用生产配置。
- [ ] 覆盖中文分段UTF-8、JSONL分段/畸形/partial frame、大stderr、stdin EOF前后、pipe失联；核实原事件/exit/锁owner。
- [ ] 正常shutdown：真实Linux kernel先结算/退出、锁释放，才允许原main/PG顺序；carrier close不是完成authority。
- [ ] startup等待、start帧发送前后、取消、Windows Node崩溃、仅载体退出、root先退/后代仍持stdout分别观察；不得把结果不明改报未执行/clean success。
- [ ] Windows PE、父先退/后代继续、持stdout、取消、Node/载体失联及Quit有独立Windows事实；无法确认的结果保留unknown，不升级clean success，不自动重发或关闭interop放行。
- [ ] 发行版外部终止实验另备明确可丢弃、无用户工作的故障环境并记录与实际目标的差异；不在用户日常Ubuntu试杀。睡眠/恢复提前留好用户工作，保留unknown，不把强制终止做默认清理。
- [ ] 沿spec §5/§13对Linux PIPE/PTY、输入/end-input、PGID/drain/worker join、8槽、monitor、MCP/hook相关行为挑选真实受影响合同；单个echo探针不能证明整条终端工具链。

当前 `PythonRuntime` 仍只有本机POSIX实现，B中才增加WSL实现与carrier观察。G0需要编写临时探针，不是修改它的生产状态机来求探针绿。相关真实测试导航：`tests/test_desktop_runtime.py`、`desktop/src/runtime.test.ts`及spec §13列出的terminal suites；先核各runner前置，不能假造一条“全部G0已跑”命令。

第四轮已把SCM_RIGHTS绑定、seal/连接结算、Windows Job与Linux group回收接入同一次真实terminal启动预约及rollback研究链路，见[合成报告](research/2026-10-25/root/g0-w-round4/report.zh.md)。额外发现并验证存活/结果冻结也须包含同一scope，仅reader.join不够；冷任务取消及done callback延迟的原失败保留。选定成功案例分实现阶段记录，没有声称最终源码下全矩阵重跑。MCP/hooks/environment、宿主失联/GUI/休眠及更多取消交错仍未覆盖，不勾整个C。

### 7.4 G0-W-P：真实 Linux PG 与资源

- [ ] 第6节资源smoke通过；记录PG17.9/vector0.8.1/pg_search0.26.1/Python3.12.15/rg/Chromium实际来源，不仅是版本文件。
- [ ] 在非root、本次独立home，验证initdb、clean-v0、BM25中文/英文/代码/snippet及vector查询，确认私有Unix socket、角色与原schema合同。
- [ ] 从Windows经有限调用取得真实Linuxversion/uid/mode/realpath/postmaster.pid/data/socket事实及stdout/stderr/exit分类；不要用Windows stat/UNC去证明Linux权限。
- [ ] 启动复用、失败readiness确认、status3与其他错误、原错误保留、正常stop/重开、资源目录搬移均验证。
- [ ] 自有PG错实例/版本/reset拒绝后数据和运行实例保留；精确reset只对自己已验证的data、原kernel关闭/锁释放后执行。绝不`rm -rf ~/.pulsara`、不在Windows删VHDX/data、不为测试给PG开TCP trust。

有限操作helper仍需G0探针验证其锁/EOF/取消/未知结果是否满足spec §3.3；**生产`desktop_guest_ops.py`尚未实现**。Linux本地PG成功只覆盖P的一部分，不能跳过Windows→Linux调用及故障结算证据。

第二轮已将helper自身死亡从待验补成反例：ext4下真实pg_ctl/postgres仍可在helper死后晚启动，而另一helper已能持同锁读到status3。当时CLOEXEC单helper持锁候选不可用于reset准入；直接继承原kernel锁给PG也因常驻持锁被否决。见spec §18及[第二轮报告](research/2026-10-24/root/g0-w-round2/report.zh.md)。今后实验home先用findmnt核真实ext4；本机/tmp实为tmpfs，不能拿旧/tmp结果冒称ext4资格。

第三轮当前候选见spec §3.3/19及[PG报告](research/2026-10-25/backend/pg-admission-round3/report.zh.md)：原kernel锁串行维护，独立PG生命周期EX由start链继承；有限stop/status继承原锁、initdb继承两锁，reset必须重取两锁并持至删除结束。真实晚启动busy拒绝、stop/initdb helper死亡、同guard拒删与实际删除重建有正证。完整JSONL/EOF/取消/失联、TS owner合成和guard内running→stop→delete分支仍待验，不能勾整个P。

上述第三轮缺项已在第四轮补充[真实TS owner集成](research/2026-10-25/backend/pg-owner-round4/report.zh.md)：原resetConfirmed的running→stop→delete真实成功；ready前后EOF/取消、发帧后EOF、丢result/无末尾换行、helper/carrier/Node死亡有证据。未提交后另一个调用起库时，原start catch会误认领其startedHere；研究typed guard对未提交/unknown直接重抛，对已结算PG非零仍保原readback。该修正未进入生产，完整framing/各操作失联/GUI退出仍待验，不勾整个P。

### 7.5 G0-W-F：图形目录、路径与文件

先做不改生产的Windows Electron/有限Linux目录探针；这个GUI探针当前也需要在`output/`编写。

- [ ] Windows资源管理器打开实际选定目标的UNC（本机示例`\\wsl.localhost\Ubuntu\home\plumwsl\workspaces`）；图形新建“中文 工程”，guest观察同一个canonical ext4目标。产品reveal仍由可信Windows UI owner执行；用户日常可继续在WSL使用explorer.exe。
- [ ] 再在无active session的Windows Electron实验picker中图形浏览/新建/选择目标；要真实有限目录服务，不能用终端mkdir替代这个产品可行性验证。
- [ ] 固定发行版UNC↔Linux域转换校验同目标；拒其他发行版/C盘/DrvFs/网络盘/bind mount替代、越界、链接/替换、权限错误、重名、取消及丢回复，路径字符串不作授权。
- [ ] 真实Windows选择/拖入文件按字节导入Linux副本；preview/HTML/附件/另存为保持原owner与隔离，验证失败/部分写入/unknown。

Explorer能新建仅证明Explorer子场景；当前正式Linux picker尚未实现，不能因此勾整个F。无需把图形产品流程变成要求用户输入mkdir。

### 7.6 G0-W-N：localhost、TLS身份与系统浏览器

先在guest用自己的空目录开一个**仅供网络诊断**的loopback server：

```bash
cd "$HOME/workspaces/pulsara_agent"
install -d -m 0700 "$HOME/g0-net-empty"
.venv/bin/python -m http.server 18765 --bind 127.0.0.1 --directory "$HOME/g0-net-empty"
```

Windows命令提示符另开窗口：`curl.exe --fail http://127.0.0.1:18765/`。完成用Linux该终端Ctrl-C关闭确切server；端口已占用就记录并选本次其他空端口，不抢占别的服务。Windows→WSL localhost是官方支持场景，但本机VPN/代理/firewall/网络模式仍需实测。[微软网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)

**HTTP诊断成功只勾“基本可达”子项，不能勾身份/G0-N。** 后续同一Windows Electron实验需完成：

- [ ] 本次endpoint精确TLS身份验证；新连接、SSE、preview、下载全覆盖；普通browser/iframe不可获得工作台凭据或Node/preload。
- [ ] 旧服务退出后同port错误服务/rebind、旧cert/旧Session、并发重连/retire都拒误连；不能单次challenge后永远信该端口。
- [ ] 保留Host/Origin/Fetch-Site检查与browser整control-port隔离；不关闭webSecurity、不忽略证书、不监听0.0.0.0或凭空加代理解决。
- [ ] 用户常用VPN/代理/firewall及睡眠恢复分别观察；不改全局WSL网络配置掩盖不兼容。
- [ ] Linux OAuth listener先安装，Windows系统浏览器经有限OAuth-only请求打开；PKCE/state/issuer及token仍原SDK owner。涉及真实账号授权由用户在Windows现场完成，code/token不入日志/host通道；没有条件就保持该项待验证。

生产TLS/Session、OAuth-only host callback均属B尚未接入；G0应验证锁定Electron/SDK扩展点能满足合同，不能为一次浏览器成功提前宣称N通过。

## 8. 如何记录、同步和停止

两份clone各在 `output/windows-g0/<个人名>/<一次实验名>/`记证据，别互相覆盖；此目录被Git忽略，**不会随本次提交自动带到Windows**。本次已归整的Mac文字研究/原始日志/探针在本目录`research/2026-10-24/`随Git提供，spec §16携带结论；其中应用包/pytest临时数据等生成物仅原机器保留，不会通过clone出现，也不能在Windows复用Mac应用包。后续新G0的output仍不会自动提交。

每个闸门保留：Git commit、Windows/WSL/已有发行版/SDK来源和版本、运行用户、实际repo/home/PG/socket/端口、完整命令/退出码/输入输出、确切进程与锁/文件观察、截图、清理结果。官方制品校验digest可保留；其他代码/文档/证据不另造hash。实际credential精确删除，保留其他有用错误/响应，不提交raw settings、token或dump凭据。

| 状态 | 什么时候用 |
| --- | --- |
| 待执行 | 还没在目标Windows机器运行 |
| 通过某子项 | 只通过明确的probe，例如pipe echo或HTTP可达 |
| 待验证/阻断 | 需要尚未编写的GUI/TLS/helper探针，或有具体失败未闭合 |
| 整门通过 | spec这一门全部实际适用场景均有证据并审阅；不能靠工具安装成功或无报错 |

开始写实验前可建开发分支 `codex/windows-g0`；一次只在选定clone提交一片源码，再经Git推送/另一端fetch同步同一提交。不共用一个node_modules或靠文件复制同步；有dirty时先处理自己的工作，不使用reset --hard/force push抹掉另一端变化。若阶段产出只在output，另将非敏感结论整理进受版本控制的报告；不要为了推送报告`git add -f output`夹带巨大资源或凭据。

每日退出按实际owner关闭自己的probe、kernel和PG，观察确切PID/pipe/锁及受支持Windows后代是否结束；暂时不明就保留并记录，不把杀wsl.exe/终止发行版当正常关闭通过。共享Ubuntu不terminate/shutdown/unregister；清理仅限本次已验证归属的应用目录和进程。

## 9. 接手第一轮完成到哪里

- [x] Windows本机Git/mise/Node/Electron可确认，tsc及选定纯测试5/5有实际结果。
- [x] 已有Ubuntu/plumwsl已明确确认，后续显式使用同一非root身份；ext4/interop/挂载有观察，其他用户工作和配置保留。
- [x] 两份clone受测源码同一提交da397a2b；Linux pins、完整desktop:verify（Node46/46及资源搬移）、聚焦39/terminal-MCP94/hooks45有日志。
- [x] Windows Node→WSL三pipe及Windows Electron→WSL localhost TLS子项有实际证据；取消普通Windows后代的失败事实单列，不记作整C通过。
- [x] G0-D/C/P/F/N缺项及已复现C缺口见[首轮报告](research/2026-10-24/root/g0-w-initial/report.zh.md)，Ubuntu原生资格仍待验收。

**到这里可以停下来总结，再逐门补G0。只有A验收补齐、G0-W全部通过并按spec交叉审阅后，才申请下一轮Phase B实施。当前不能在生产main增加WSL选择器、自动provision/升级/卸载、Windows安装包或宣称Windows支持。**


## 10. 本清单交付检查

最新已归档进展见[spec第21节](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md#21-第五六轮剩余g0机制与gui实证2026-10-25)及[第五轮文件/HTTP报告](research/2026-10-25/root/g0-w-round5/report.zh.md)：MCP/hooks/environment真实跨域研究、PG剩余独立边界、目标发现/离线payload与真实kernel控制链已有记录。前三、四轮保留其当时待验状态，不反写历史。正式生产接入与安装GUI属于B；原生Ubuntu按用户要求延期，不阻塞本轮G0研究，也未被WSL结果替代。

| 本轮新增证据 | 当前验收范围 |
| --- | --- |
| MCP 6场景/21原测试；hooks/environment 12场景/55原测试 | 原提前结算反例、真实scope研究修正、call/session不同生命周期、guard先于spawn |
| PG 29物理case | ext4检索/搬移/reset、有限framing及三类执行结果、initdb/stop/remove剩余边界；不声称全故障排列 |
| kernel 22次运行/18完成观测/4探针失败保留，12原测试 | 正常EOF关闭顺序；原child清空后异常误判与候选；强退宿主无aclose仍须收敛，不能报clean |
| 默认目标与完整payload | 真实显式身份/非root/离线安装、写点ENOSPC与中断保数据；未改系统默认或假造WSL未装 |
| Windows目录与真实文件/HTTP | 图形浏览/创建/选择/取消、用户选择/拖放逐字节、路径竞态/挂载拒绝、原HTML/image/PDF/下载owners；fixture边界见报告 |

整发行版外部终止仅用明确新建可丢弃故障环境；不能终止用户Ubuntu。本节归档时持续网络退休/证书时间、真实OAuth、睡眠恢复及完整门审阅尚待补齐，未提前勾整门；最终已补结果见第11节。interop与Hiddify TUN持续开启。真实Notion授权及睡眠唤醒由用户现场参与。

原root与三位`gpt-6.1-sol high` subagents批准的是修订前的专用发行版清单，历史记录保留在research。原示例JS语法检查及Mac纯测试5/5不证明本次共享WSL/interop方案。当前用户授权修订了活动规范和操作步骤；本机已有基础环境/interop观察，但Windows后代生命周期、正式picker/helper/TLS/OAuth等仍须实测，不沿用历史批准宣称完整G0或B通过。


活动实施目录入口：[README.md](README.md)。本文及冻结spec原位于仓库根，现统一存放`windows_compatibility/`；本文命令仍从对应系统的**仓库根**执行，不能先cd到本文件目录再运行。

## 11. 本机G0最终验收状态（2026-10-25）

以下只对[最终报告](research/2026-10-25/root/g0-w-final/report.zh.md)记录的Windows11 25H2/WSL2.7.11.0/Ubuntu26.04.1/plumwsl/ext4/Electron44.9.0/nat/Hiddify TUN矩阵成立，Ubuntu数字版本不限，interop/挂载不关闭。

- [x] **D**：真实已有目标发现/显式重开、非root/ext4、完整私有payload离线安装及有限失败保数据。默认变化仅输入阴性；未安装/需重启环境未伪造，ENOSPC为真实写点故障。
- [x] **C**：terminal/MCP/hooks/environment实际owner的跨域scope/取消/结算；真实kernel/原main关闭顺序、异常退出误判候选、SIGHUP/EOF收尾、可见窗口关闭、独立发行版故障与实际睡眠。
- [x] **P**：PG两锁/typed结果、剩余29个物理case、ext4 clean-v0/检索/搬移/reset；补[原样PGDATA dirty恢复](research/2026-10-25/terminal/distro-recovery-round7/report.zh.md)，PG自行redo、同canonical记录可读、原Quit收尾。
- [x] **F**：真实图形目录、路径/挂载/替换/unknown、Windows选择/拖放副本与权限、原预览/下载；大小写/NFC/NFD与UNC保持不同identity，真实另存为取消/拒绝覆盖/确认覆盖。
- [x] **N**：精确TLS/旧服务拒绝、自然证书有效期与在途流退休；88.493秒真实睡眠恢复、同kernel/PG/证书，SSE一次只读重连；用户现场Notion授权/握手/list_tools65，补callback陌生服务rebind负控。
- [x] 原失败/修正前探针、秘密边界、清理和三方交叉审阅均保留；root核must-have后才给整门结论。两次临时故障发行版均不用于日常部署，不替换用户Ubuntu。
- [ ] **Phase B与Windows激活**：用户本轮明确不进入。生产接入、正式setup/NSIS/完整GUI、更新卸载、真实provider长程和受影响平台回归仍未实施/验收。
- [ ] **原生Ubuntu资格**：用户延期，不拿WSL代证，不纳入本轮Windows G0阻塞。

Notion仅本地测试grant/home已删除，远端授权/DCR未撤销。闲置Electron profile清理遭自动审批拒绝，保留ignored output、不归档；不绕过删除。R7注销后曾观测共享Ubuntu Stopped，原要求Running的探针失败原样保留，未猜原因或主动终止用户发行版；后续显式Git同步启动与该历史观察分别记。

下一步只在用户明确要求后讨论Phase B实施：按最终报告列出的真实接缝做生产hard cut，并保留后续独立资格门。本轮G0通过不自动启动这些工作，也不宣布已支持Windows发行。
