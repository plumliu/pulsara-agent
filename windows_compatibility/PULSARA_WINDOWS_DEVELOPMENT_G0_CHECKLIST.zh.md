# Windows 开发准备与 G0-W 操作清单

日期：2026-10-24。面向第一次做平台迁移的开发者。依据：[冻结实施 spec](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md) §4、§11、§13、§16。

**本文件是 Windows 接手操作说明，不是已执行的 Windows 验收记录。** Phase A 代码与 Mac 实证已冻结，原生 Ubuntu x64/arm64 与 Ubuntu GUI 仍待验收；G0-W 全部尚待 Windows 实测，Phase B 未进入。这里允许手工准备一次性实验发行版和 `output/` 内探针，不实现产品安装器、WslHostOperations、正式目录 picker、TLS/OAuth 桥或任何生产 fallback。

## 1. 先看懂两边分工

Windows 是桌面宿主；WSL2 是里面的一套 Linux 环境。Windows Terminal 是终端窗口，可以打开“命令提示符”或指定 Linux 发行版，它本身不是执行环境。

| 项目 | Windows 本机 | 专用 WSL2 / Ubuntu 24.04 |
| --- | --- | --- |
| 做什么 | Electron、Windows UI、wsl.exe 载体与网络探针 | Python 内核、Linux 终端、文件、PG、Playwright、Linux 资源构建 |
| 必需工具 | Git for Windows、mise、Windows Node/npm；WSL 系统组件 | Linux Git/curl/编译工具、mise、Linux Node/npm、uv/Python、protoc |
| 仓库建议位置 | `%USERPROFILE%\src\pulsara_agent`，本机普通目录 | `/home/pulsara/workspaces/pulsara_agent`，真实 ext4 |
| 私有依赖 | 这份仓库自己的 `desktop\node_modules` 和 `desktop\dist` | 这份仓库自己的 `.venv/`、node_modules、dist、resources、build |
| 默认身份 | 普通 Windows 用户 | `pulsara`，非 root；只有明确安装维护进入 root |
| 编辑器 | 现有编辑器即可；可选 VS Code + WSL 扩展 | 经编辑器的 Windows 侧“连接到指定发行版”功能，或直接 Linux 编辑器 |

两份仓库是**同一项目的两个独立 clone**，用 Git 同步源码；不是维护两套业务代码。不要同时编辑同一个工作目录，也不跨系统复制 `.venv`、node_modules、resources、build、dist 或缓存。不要把 WSL 仓库放在 `/mnt/c`，不要把 Windows Electron 当 Linux Electron 运行。[微软文件系统说明](https://learn.microsoft.com/en-us/windows/wsl/filesystems)、[Electron Windows 开发说明](https://www.electronjs.org/docs/latest/tutorial/tutorial-first-app)

Windows 仓库避开 OneDrive/桌面同步目录；如果用户目录本身是重定向目录，另选自己可写的本机 NTFS 开发目录，后续路径一起替换。WSL 工程和实验数据不直接放到 C 盘映射目录。下面 `cmd` 块在 **Windows Terminal → 命令提示符**执行，`bash` 块在明确标注的 Linux 用户终端执行；不需要另装 PowerShell 7，也不把 PowerShell 用作模型 shell。

## 2. 当前锁定版本与不要提前装的东西

| 工具/资源 | 当前值 | 真相来源 |
| --- | --- | --- |
| 首批目标 | Windows 11 25H2 x64、WSL2、Ubuntu 24.04 x64 | spec §1；其他 Windows 版本先记录差异，不自动扩大资格 |
| Node / uv / protoc | 22.23.3 / 0.13.4 / 34.0 | `mise.toml` |
| Python | 3.12.15 | `.python-version`；由 uv 管理，不用 Ubuntu 系统 Python 代替项目环境 |
| mise | Linux脚本固定2026.10.15；Windows WinGet记录实际安装版本，须不低于2026.7.18 | 当前 desktop workflow 与 `mise.toml`；不把WinGet当固定版本下载 |
| Electron / electron-builder / TypeScript | 44.9.0 / 26.15.3 / 5.9.3 | `desktop/package-lock.json`，用 npm ci 安装 |
| PostgreSQL / vector / pg_search | 17.9 / 0.8.1 / 0.26.1 | `desktop/scripts/prepare_resources.py`，不可换成 PG16 或 PG18 的扩展 |
| WSL 包、kernel、rootfs | **还没有本项目验证版本** | 第一次 G0 记录实际版本/来源；通过后再固定支持组合 |

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

原有默认发行版、其他发行版、全局 `%USERPROFILE%\.wslconfig` 都保持原样。若已存在名为 `Pulsara` 的发行版，先停止本清单的 import 步骤并查明归属；不 unregister、不覆盖、不把它默认为本次空实验环境。

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

## 4. 手工准备 G0 专用发行版（不是实现产品安装器）

### 4.1 安装/确认 WSL2

若尚未安装 WSL，在**管理员**命令提示符运行下列系统安装操作，按系统提示重启，然后回普通用户终端。已有 WSL 时先记录版本，只在确有需要时显式更新，不静默改他人的系统环境。

```cmd
wsl --install --no-distribution
```

重开后再运行 `wsl --version`、`wsl --status`。现有安装需更新时可用 `wsl --update`，记录更新前后及重启状态；不执行 `wsl --set-default` 或 `--set-default-version`。首轮只使用明确带 `--version 2` 的本次 import。[微软安装说明](https://learn.microsoft.com/en-us/windows/wsl/install)、[WSL 命令定义](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

### 4.2 下载并核验 Ubuntu 24.04 x64 rootfs

本次采用 Canonical 官方 WSL rootfs。已核查的入口为 [Ubuntu 24.04 WSL 镜像目录](https://cloud-images.ubuntu.com/wsl/releases/24.04/current/)，其中有 `ubuntu-noble-wsl-amd64-24.04lts.rootfs.tar.gz`、SHA256SUMS 和签名。`current` 是浮动目录，**不把这个 URL 当已冻结的镜像版本**：第一次实验必须保存实际制品、下载日期、摘要文件/签名和实际 rootfs/包版本，后续重现使用同一已验证制品，不重新漂移下载“最新版”。

Windows 命令提示符示例，下载到自己新建的实验目录：

```cmd
mkdir "%USERPROFILE%\Downloads\pulsara-g0-rootfs"
cd /d "%USERPROFILE%\Downloads\pulsara-g0-rootfs"
curl.exe -fLO https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-24.04lts.rootfs.tar.gz
curl.exe -fLO https://cloud-images.ubuntu.com/wsl/releases/24.04/current/SHA256SUMS
curl.exe -fLO https://cloud-images.ubuntu.com/wsl/releases/24.04/current/SHA256SUMS.gpg
certutil -hashfile ubuntu-noble-wsl-amd64-24.04lts.rootfs.tar.gz SHA256
findstr /C:"ubuntu-noble-wsl-amd64-24.04lts.rootfs.tar.gz" SHA256SUMS
```

- [ ] 人工比对两处摘要一致；不一致或下载中断就停止 import。
- [ ] 按 [Ubuntu Cloud Images 官方校验说明](https://ubuntu.com/docs/public-images/public-images-how-to/verify-image-checksum/)用可信来源的完整 signing-key fingerprint 验证 SHA256SUMS.gpg；注意这是WSL/cloud image，不要照通用ISO教程抄CD-image key ID，也不要把签名里的任意key自动视为可信。官方提供的 [cloud image keyring说明](https://ubuntu.com/docs/public-images/public-images-reference/artifacts/)可用于已有可信Ubuntu环境验证；Windows也可使用Git安装自带且已确认可用的gpg。保存公钥完整fingerprint及 `gpg --verify` 实际结果；来源/签名尚未确认就先停在下载步骤，不执行下面import，不把未验证写成通过。
- [ ] 摘要允许用于此处真实下载内容边界；不要给代码、spec 或实验输出新建 SHA 证据系统。

若入口迁移/文件不可用，先到 Canonical 官方目录重新确定24.04 x64制品及校验材料，更新本次记录；不换26.04、不用第三方 rootfs 或自己的默认 Ubuntu 兜底。

### 4.3 普通 Windows 用户 import

再次确认 `wsl --list --verbose` 没有同名 `Pulsara`，并且 `%LOCALAPPDATA%\Pulsara-G0\WSL` 不包含已有数据。本地目录用于 WSL 自己的虚拟磁盘，**不等于把 Linux 工作区变成 C 盘目录**。

```cmd
mkdir "%LOCALAPPDATA%\Pulsara-G0"
wsl --import Pulsara "%LOCALAPPDATA%\Pulsara-G0\WSL" "%USERPROFILE%\Downloads\pulsara-g0-rootfs\ubuntu-noble-wsl-amd64-24.04lts.rootfs.tar.gz" --version 2
wsl --list --verbose
wsl -d Pulsara -u root --cd / --exec /bin/bash
```

应看到本次 `Pulsara` 为 VERSION 2。最后一条只进入这套新实验发行版的 root 维护 shell；如果 import 失败，保留错误及目录现状，不自动 unregister 重试。`--unregister` 会永久删除发行版，本清单没有自动清库/删发行版命令。[微软 import 与注销语义](https://learn.microsoft.com/en-us/windows/wsl/basic-commands#import-a-distribution)

### 4.4 一次 root 维护：用户、基础工具及执行域

以下只在刚创建的 **Pulsara root shell** 运行。若发现已有同名用户或不明数据，先核来源，不重置密码/改归属覆盖。

```bash
cat /etc/os-release
id -u
useradd --create-home --shell /bin/bash pulsara
install -d -m 0750 -o pulsara -g pulsara /home/pulsara/workspaces
apt-get update
apt-get install -y ca-certificates curl git build-essential bison flex pkg-config binutils patchelf libopenblas0 xz-utils
```

预期 Ubuntu24.04；不要给 `pulsara` 免密 sudo，不以 root 运行 uv/npm、initdb 或模型。上述 Linux 构建依赖来自当前 `.github/workflows/desktop-package.yml` / `build_environment.py`，不用旧 Linux 手册里的 PG16/Docker/web_app 路线。

先查看 `/etc/wsl.conf` 和 `/etc/fstab`，已有文件先复制为本次实验的before备份，勿覆盖已有备份。在这个新发行版的root shell可执行 `apt-get install -y nano`、`nano /etc/wsl.conf`；编辑后按Ctrl-O、Enter保存，Ctrl-X退出。nano仅是可选开发编辑器，不是产品运行依赖。相关节各保留一份：

```ini
[user]
default=pulsara

[interop]
enabled=false
appendWindowsPath=false

[automount]
enabled=false
mountFsTab=false
```

只改本次发行版，不改 Windows 全局 `.wslconfig`。保留并记录原文件中其他有依据的设置，不盲目追加重复 section。配置含义与重启生效规则见 [微软 wsl.conf 文档](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)。

输入 `exit` 退出维护 shell。确认本次尚未启动测试任务/PG，且所有本次 shell 都已退出，才在 **Windows 普通终端**执行：

```cmd
wsl --terminate Pulsara
wsl -d Pulsara --cd /home/pulsara --exec /usr/bin/id
wsl -d Pulsara -u pulsara --cd /home/pulsara --exec /bin/bash -l
```

这里 terminate 仅用于尚无工作的本次配置生效/后续显式故障实验，**不是产品 Quit 或测试卡住时的日常清理手段**；不使用全局 `wsl --shutdown`。不带 `-u` 的 id 应已经是 pulsara，带 `-u pulsara` 的后续命令仍明确目标。

## 5. WSL 非 root 开发环境与仓库

以下在 **Pulsara / pulsara 的 bash**，先确认身份和真实文件系统：

```bash
whoami
id -u
uname -a
cat /etc/os-release
findmnt -T /home/pulsara/workspaces -o TARGET,SOURCE,FSTYPE,OPTIONS
printf '%s\n' "$PATH"
```

应为非0 UID、Ubuntu24.04/x86_64，工程根位于真实 ext4，不是 DrvFs/9p/CIFS。PATH 不应含 Windows 驱动器目录。`/mnt/c` 不存在本身**不能证明** interop 禁止；真正 PE 负测见 G0-D。

Linux mise 使用和当前 CI 相同的版本，安装脚本先保存/查看再执行：

```bash
curl -fsSL https://mise.run -o /tmp/pulsara-mise-install.sh
less /tmp/pulsara-mise-install.sh
MISE_VERSION=v2026.10.15 sh /tmp/pulsara-mise-install.sh
export PATH="$HOME/.local/bin:$PATH"
mise --version
cd /home/pulsara/workspaces
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
wsl -d Pulsara -u root --cd /home/pulsara/workspaces/pulsara_agent --exec /home/pulsara/workspaces/pulsara_agent/.venv/bin/python -m playwright install-deps chromium
```

这一步只装 OS 库；浏览器资源、venv 和构建仍由 pulsara 用户拥有。编辑器连接禁 interop 的发行版时从 Windows 侧选择具体 distro，不依赖 Linux 的 `code .` 或 `explorer.exe .`，失败也不要为编辑方便悄悄打开 interop。

## 6. 先验证 Linux 构建，再碰跨域链路

回到 **Pulsara / pulsara**，仓库根且 mise 已激活：

```bash
cd /home/pulsara/workspaces/pulsara_agent
mise run --jobs 1 desktop:verify
.venv/bin/python -m pytest -q tests/test_desktop_runtime.py tests/test_desktop_launch_directory.py tests/test_desktop_release.py
.venv/bin/python -m ruff check
```

这会准备真实 Linux Python/PG/扩展/Chromium、编译现有代码并做资源搬移检查；首次下载和编译会花时间，保存具体日志。macOS已通过的46条Node测试不是这里的实际结果；记录 Windows接手提交下 WSL 实际计数和失败，不改skip/断言消除差异。

这些结果只证明 **WSL 中原 Linux 代码/资源的相应场景**；不能消掉 Phase A 尚欠的原生 Ubuntu 双架构/图形资格。`npm run test:startup/browser` 若借WSLg运行，也不等于Windows Electron GUI。

每次PG/内核实验建立独立短路径home，例如 `/home/pulsara/g0-homes/r01`，目录仅当前用户可访问；运行前记录 realpath/uid/socket与目标，完成后通过原owner停止并确认无进程/锁owner，再清理自己刚创建的目录。不要把saved生产配置复制到实验目录，不连接用户现有数据库，不打印credential。G0不要求真实provider；后续真实provider门继续适用spec的只读settings注入规则。

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
const repo = '/home/pulsara/workspaces/pulsara_agent';
const code = 'import sys; data=sys.stdin.buffer.read(); sys.stderr.buffer.write(b"x"*(2*1024*1024)); sys.stderr.buffer.flush(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); sys.exit(7)';
const child = spawn(path.join(process.env.SystemRoot, 'System32', 'wsl.exe'),
  ['-d', 'Pulsara', '-u', 'pulsara', '--cd', repo, '--exec',
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

### 7.2 G0-W-D：部署与 Linux-only 执行域

- [ ] 保存本次 Windows/WSL/kernel/rootfs/软件包版本及Git提交，确认不是默认Ubuntu、WSL1或其他用户发行版。
- [ ] import前后发行版清单、同名冲突/失败import残留都可解释；重启后默认身份与显式 `-u pulsara` 都非root。
- [ ] `/home/pulsara/workspaces`及实验home真实ext4；automount/Windows PATH配置实际有效，非只看配置文本。
- [ ] **准备已知真实 Windows PE 的正/负对照探针**：Windows确实可运行的测试PE，经受控字节导入放入自己的ext4探针目录，确认同一文件存在/可读，再观察guest执行被拒。绝不能把禁automount后 `/mnt/c/...exe` 的ENOENT，或清WSL_INTEROP变量，记作“禁止interop通过”。若缺正对照能力，本项保持待验证，不临时开放用户磁盘或全局interop凑结果。
- [ ] 普通Windows用户可启动上述实验，无日常UAC；没有修改其他发行版/默认值/全局配置。

G0-D既包含手工环境事实，也要验证未来安装路线的可重现性/失败边界；一次手工import成功并不代表产品provision完成。

### 7.3 G0-W-C：控制、EOF、取消与物理生命周期

继续在 `output/windows-g0/`写有限实验，不把“给python前面加wsl.exe”接入生产。正式载体仍须 `shell:false`、固定distro/user、明确Linuxcwd/可执行文件、三pipe；不让PowerShell文本管道替代byte relay，不混入模型key/WindowsPATH。

- [ ] 扩展7.1到真实随包 `desktop_runtime`：收到原start帧前不接触数据库；只用本次Linuxhome与本次PG DSN启动，不复用生产配置。
- [ ] 覆盖中文分段UTF-8、JSONL分段/畸形/partial frame、大stderr、stdin EOF前后、pipe失联；核实原事件/exit/锁owner。
- [ ] 正常shutdown：真实Linux kernel先结算/退出、锁释放，才允许原main/PG顺序；carrier close不是完成authority。
- [ ] startup等待、start帧发送前后、取消、Windows Node崩溃、仅载体退出、root先退/后代仍持stdout分别观察；不得把结果不明改报未执行/clean success。
- [ ] 在**确认为可丢弃且无用户工作**的本次发行版，明确标注地做发行版外部终止、Windows睡眠/恢复实验；保留unknown事实，不把强制终止做默认清理策略。
- [ ] 沿spec §5/§13对Linux PIPE/PTY、输入/end-input、PGID/drain/worker join、8槽、monitor、MCP/hook相关行为挑选真实受影响合同；单个echo探针不能证明整条终端工具链。

当前 `PythonRuntime` 仍只有本机POSIX实现，B中才增加WSL实现与carrier观察。G0需要编写临时探针，不是修改它的生产状态机来求探针绿。相关真实测试导航：`tests/test_desktop_runtime.py`、`desktop/src/runtime.test.ts`及spec §13列出的terminal suites；先核各runner前置，不能假造一条“全部G0已跑”命令。

### 7.4 G0-W-P：真实 Linux PG 与资源

- [ ] 第6节资源smoke通过；记录PG17.9/vector0.8.1/pg_search0.26.1/Python3.12.15/rg/Chromium实际来源，不仅是版本文件。
- [ ] 在非root、本次独立home，验证initdb、clean-v0、BM25中文/英文/代码/snippet及vector查询，确认私有Unix socket、角色与原schema合同。
- [ ] 从Windows经有限调用取得真实Linuxversion/uid/mode/realpath/postmaster.pid/data/socket事实及stdout/stderr/exit分类；不要用Windows stat/UNC去证明Linux权限。
- [ ] 启动复用、失败readiness确认、status3与其他错误、原错误保留、正常stop/重开、资源目录搬移均验证。
- [ ] 自有PG错实例/版本/reset拒绝后数据和运行实例保留；精确reset只对自己已验证的data、原kernel关闭/锁释放后执行。绝不`rm -rf ~/.pulsara`、不在Windows删VHDX/data、不为测试给PG开TCP trust。

有限操作helper仍需G0探针验证其锁/EOF/取消/未知结果是否满足spec §3.3；**生产`desktop_guest_ops.py`尚未实现**。Linux本地PG成功只覆盖P的一部分，不能跳过Windows→Linux调用及故障结算证据。

### 7.5 G0-W-F：图形目录、路径与文件

先做不改生产的Windows Electron/有限Linux目录探针；这个GUI探针当前也需要在`output/`编写。

- [ ] Windows资源管理器地址栏打开 `\\wsl.localhost\Pulsara\home\pulsara\workspaces`；图形新建“中文 工程”，在guest观察同一个canonical ext4目标。Windows UI启动资源管理器，不从Linux执行explorer.exe。
- [ ] 再在无active session的Windows Electron实验picker中图形浏览/新建/选择目标；要真实有限目录服务，不能用终端mkdir替代这个产品可行性验证。
- [ ] 固定发行版UNC↔Linux域转换校验同目标；拒其他发行版/C盘/DrvFs/网络盘/bind mount替代、越界、链接/替换、权限错误、重名、取消及丢回复，路径字符串不作授权。
- [ ] 真实Windows选择/拖入文件按字节导入Linux副本；preview/HTML/附件/另存为保持原owner与隔离，验证失败/部分写入/unknown。

Explorer能新建仅证明Explorer子场景；当前正式Linux picker尚未实现，不能因此勾整个F。无需把图形产品流程变成要求用户输入mkdir。

### 7.6 G0-W-N：localhost、TLS身份与系统浏览器

先在guest用自己的空目录开一个**仅供网络诊断**的loopback server：

```bash
cd /home/pulsara/workspaces/pulsara_agent
install -d -m 0700 /home/pulsara/g0-net-empty
.venv/bin/python -m http.server 18765 --bind 127.0.0.1 --directory /home/pulsara/g0-net-empty
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

每个闸门保留：Git commit、Windows/WSL/rootfs/SDK来源和版本、运行用户、实际repo/home/PG/socket/端口、完整命令/退出码/输入输出、确切进程与锁/文件观察、截图、清理结果。官方制品校验digest可保留；其他代码/文档/证据不另造hash。实际credential精确删除，保留其他有用错误/响应，不提交raw settings、token或dump凭据。

| 状态 | 什么时候用 |
| --- | --- |
| 待执行 | 还没在目标Windows机器运行 |
| 通过某子项 | 只通过明确的probe，例如pipe echo或HTTP可达 |
| 待验证/阻断 | 需要尚未编写的GUI/TLS/helper探针，或有具体失败未闭合 |
| 整门通过 | spec这一门全部实际适用场景均有证据并审阅；不能靠工具安装成功或无报错 |

开始写实验前可建开发分支 `codex/windows-g0`；一次只在选定clone提交一片源码，再经Git推送/另一端fetch同步同一提交。不共用一个node_modules或靠文件复制同步；有dirty时先处理自己的工作，不使用reset --hard/force push抹掉另一端变化。若阶段产出只在output，另将非敏感结论整理进受版本控制的报告；不要为了推送报告`git add -f output`夹带巨大资源或凭据。

每日退出按实际owner关闭自己的probe、kernel和PG，观察确切PID/pipe/锁是否结束；暂时不明就保留并记录，不把杀wsl.exe/terminate发行版当“正常关闭通过”。不自动unregister；需要删这套实验发行版时另核全量数据归属和明确删除决定。

## 9. 接手第一轮完成到哪里

- [ ] Windows本机Git/mise/Node/Electron版本可确认，tsc及选定纯测试有实际结果。
- [ ] 只有本次`Pulsara`被import/配置，默认及显式身份非root，ext4/interop/挂载事实开始验证。
- [ ] 两份clone同一Git提交；Linux pins/资源构建与聚焦回归有日志。
- [ ] 至少完成7.1三pipe基础probe及7.6基本可达诊断，结果如实限定；有失败先保留具体错误。
- [ ] G0-D/C/P/F/N逐项建立待验证清单，明确哪些需要后续临时probe，Ubuntu原生资格继续待验收。

**到这里可以停下来总结，再逐门补G0。只有A验收补齐、G0-W全部通过并按spec交叉审阅后，才申请下一轮Phase B实施。当前不能在生产main增加WSL选择器、自动provision/升级/卸载、Windows安装包或宣称Windows支持。**


## 10. 本清单交付检查

root与三位`gpt-6.1-sol high` subagents已对照冻结spec、当前代码/锁文件、官方平台文档交叉审阅本清单，均同意交付。已采纳WSLENV大小写清理、Windows Node实际平台/架构/路径观测、明确退出码、mise版本来源和cloud-image签名/编辑步骤修正。示例JS做过`node --check`，列出的三个纯测试文件在Mac本机5/5通过；**这些不是Windows运行证据，G0-W状态仍为待实测**。本轮没有部署WSL或进入Phase B。


活动实施目录入口：[README.md](README.md)。本文及冻结spec原位于仓库根，现统一存放`windows_compatibility/`；本文命令仍从对应系统的**仓库根**执行，不能先cd到本文件目录再运行。
