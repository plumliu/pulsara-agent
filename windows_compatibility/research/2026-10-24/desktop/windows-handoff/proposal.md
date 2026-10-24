# Windows开发准备/G0-W清单：桌面专题建议

2026-10-24。只读复核生产/锁文件与官方文档；只在本目录写建议，不改根文档/生产/git，不在本机冒充Windows验证，不发布。供root写`PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md`。

## 1. 新手首先需要知道的事实

现在实现的是Phase A本机POSIX接缝，Windows运行适配尚未实现。Windows能安装Node、取得依赖、通过tsc，不意味着现`npm start`能启动产品。先完成“准备环境”，再把真实Windows/WSL信息交给实施者执行G0-W；不要让新手自行绕过错误、修改路径、增加fallback使产品勉强打开。

现`desktop/src/main.ts:12–23`仍选择`posixHostOperations`，在模块加载时同步执行`resources/python/bin/python3`来resolve home；runtime/PG也都是本机POSIX操作。Windows没有这份可运行的本机LinuxPython，更没有WslHostOperations/Linux helper。原PG Node fs/uid/Unix socket机制也不能在Windows解释Linuxhome。失败时保留错误，不把它诊断成“再装个WindowsPython就好”。

`desktop/package.json`只有mac/deb配置、POSIXCLI资源与原afterPack；没有本规格要求的Windows NSIS无强杀、WSL部署/rootfs、TLS/路径picker等B闭环。mise的desktop图仍用`.venv/bin/python`，`prepare_resources.target_platform()`只允许Macarm64或Ubuntu24.04 x64/arm64；wheel hook没有win_amd64目标。因此不要列以下为当前Windows可成功启动/打包步骤：

- `npm.cmd --prefix desktop start`
- `npm.cmd --prefix desktop run package`/`package:dir`
- `mise run desktop:resources`/`desktop:verify`/`desktop:package`
- 在Windows根运行完整`uv sync`/Python产品测试，然后把Windowswheel失败归为环境误配。

这些路径要么仍POSIX，要么属于尚未实现B。Git Bash/MSYS/WSL的bash能运行某段脚本，也不能把WindowsNodefs变成Linuxowner。

## 2. Windows与WSL使用两个独立clone

| 用途 | 建议位置 | 实际执行工具 |
| --- | --- | --- |
| Windows Electron host源码/G0原生宿主probe | `C:\src\pulsara-agent-host`，本地NTFS | Windows Git/Node/npm/Electron，PowerShell或cmd |
| Linux guest源码/资源/内核测试 | 专用Pulsara发行版中`/home/pulsara/workspaces/pulsara-agent-guest`，真实ext4 | Linux Git/mise/uv/Python/Node/PG资源构建 |

两个clone采用同一最终交付Git revision，各自记录`git rev-parse HEAD`及dirty；不要自动同步未提交改动或把一边旧checkout误当当前candidate。初次安装时尚无专用distro，则先做Windows准备，Linuxclone等G0部署步骤明确导入/配置后再做，不能擅自借用户默认Ubuntu代替。

`node_modules`、`.venv`、`desktop/resources`、`desktop/build`、`desktop/dist/release`和cache各平台独立。不要从Mac拷这些目录，不把Win native Node/npm放到LinuxPATH，不从Windows通过`\\wsl$`运行Linuxnode_modules，也不让Linux资源构建在`/mnt/c`工程上跑。Microsoft建议工具及其频繁读写仓库位于对应文件系统；本方案同时避免架构/可执行格式和符号链接/权限混域。[微软WSL环境](https://learn.microsoft.com/en-us/windows/wsl/setup/environment)、[路径/文件系统](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)

Git换行配置如有需要仅针对新clone使用repository-local `core.autocrlf=false`，避免改用户全局设置；不能靠批量改CRLF/执行位修到测试绿。不要把含token的remote URL粘贴证据，用正常Git凭据管理登录。

## 3. 锁版本与工具边界

| 工具 | 当前依据 | 安装范围 |
| --- | --- | --- |
| Node22.23.3 x64 | `mise.toml` | Windows宿主必需，Linux构建另装Linux版同pin |
| npm | Node官方22.23.3附npm10.9.9；记录实际`npm.cmd --version` | 不额外`npm install -g npm@latest` |
| Electron44.9.0 / builder26.15.3 / TS5.9.3 | desktop package+lock | 项目本地`npm ci`，不全局装Electron、不创建新Forge工程 |
| Python3.12.15 / uv0.13.4 / protoc34.0 | `.python-version`、`mise.toml` | Linux backend/资源构建；WindowsPython不是应用内核前提，未来构建脚本需要时才使用repo uv环境 |
| mise最低2026.7.18 | `mise.toml min_version` | 可在两侧各装原生版本；Windowsmise可用不等于现POSIX任务图可用 |
| Git/编辑器/WindowsTerminal | 实际Git版本记录 | Windows宿主；不为当前纯JS开发预先安装完整VisualStudio/MSVC/WindowsPG/pwsh私有runtime |

Node22.23.3官方发布页提供Windowsx64安装器/归档，bundled npm版本有明确记录；这不是“最新LTS”，要跟当前repo pin。[Node官方22.23.3](https://nodejs.org/en/blog/release/v22.23.3)

新手建议直接安装官方Node22.23.3 x64预构建包，不同时混装多个nvm/mise/systemNode。mise是可选管理方式：Windows官方支持winget/PowerShell，而WSL需要Linuxmise；若使用`mise exec`须核实际Node来源，不使用`mise use ...@latest`改repo pin。mise官方也提醒部分shell插件不支持nativeWindows。[mise安装](https://mise.jdx.dev/installing-mise.html)、[miseFAQ](https://mise.jdx.dev/faq.html)

WindowsPowerShell用`npm.cmd`，避免同名`npm.ps1`被ExecutionPolicy挡住；不指导`Set-ExecutionPolicy Unrestricted`或全局绕过策略。Windows管理终端不要求PowerShell7.6——旧原生路线的随包pwsh已撤出；Linux模型执行仍bash/sh。

## 4. 可以现在给新手的Windows宿主命令

以下均在已checkout最终交付revision的Windows本机host仓库根执行。它们是准备/少量平台无关测试，不是G0通过。本文未在Windows实跑，预计结果须由目标机器记录。

```powershell
Get-Command node.exe, npm.cmd, git.exe
node.exe -p "JSON.stringify({platform:process.platform,arch:process.arch,version:process.version,execPath:process.execPath})"
npm.cmd --version
git.exe rev-parse HEAD
git.exe status --short
npm.cmd --prefix desktop ci --no-audit --no-fund
npm.cmd --prefix desktop run build
```

期望nativeNode为`win32/x64/v22.23.3`，execPath为Windowsexe；看到`linux`或`/usr/...`说明跑错终端。锁文件用`npm ci`，不升级lock或先运行npm update来修错误；npm官方规定ci不写package/lock，依赖不符应停下检查revision与环境。[npm ci官方](https://docs.npmjs.com/cli/v11/commands/npm-ci/)

**Electron二进制下载是独立一步**。已读实际Electron44.9.0 package：没有postinstall，包含`install-electron`bin；index.js首次require会执行install.js。所以npm ci成功不能证明electron.exe已就绪。可显式执行当前锁包提供的脚本，再观测：

```powershell
node.exe .\desktop\node_modules\electron\install.js
& .\desktop\node_modules\.bin\electron.cmd --version
```

应报告v44.9.0，不手设ELECTRON_INSTALL_PLATFORM/ARCH伪装其他target，不用未限定的npx下载latest。下载失败按代理/离线/cache实际错误记录，不关闭TLS校验或从随机源拿exe。[Electron安装能力](https://www.electronjs.org/docs/latest/tutorial/installation)

Windows主机Node用于开发工具；Electron自身带另一个Node runtime，不能因为`node.exe --version`正确就声称Electron内部版本相同。未来G0应记录Electron `process.versions`与实际win32/x64。[Electron开发前提](https://www.electronjs.org/docs/latest/tutorial/tutorial-prerequisites)

若需做frontend编辑再在同一个Windowsclone执行`npm.cmd --prefix frontend ci --no-audit --no-fund`与原lint/test/build:desktop；这些仍只证明renderer层，不是工作台WSL连通或安装资格。不要在WSL与Windows共用frontend/node_modules。

## 5. 测试分类：避免把全npm test写成Windows第一步

当前`npm test`会编译并全跑`dist/*.test.js`。它包含大量实质POSIX测试，不是当前Windows suite。

| 测试/命令 | 当前Windowshost能否作为准备项 | 理由/证据边界 |
| --- | --- | --- |
| `npm.cmd --prefix desktop run build` | 可做编译检查 | 不执行main/内核，不证明资源/WSL |
| browser-policy.test / oauth-return.test / ui-preferences.test | 可显式挑这3文件，现共5个case | URL/端口隔离/几何/focus-only与临时JSON prefs，未含POSIX子进程；目标机实测后才报通过 |
| main.test.ts | 不列为Windows当前资格 | fixture刻意mock darwin/linux，POSIX `/tmp`/`/opt`与宿主path.join依赖；不能把mock平台当Windowsmain支持 |
| cli-install.test.ts | Linux/Mac执行 | 真实shebang `/bin/sh`、exec/chmod/X_OK/symlink/原LinuxafterPack/PATH语义 |
| runtime.test.ts | Linux/Mac执行 | fixture写python3 shell脚本后真实spawn，非Windowsexe |
| host-posix.test.ts | Linux/Mac执行 | 真实POSIX shell/权限mode700/600断言等；其中个别Node机制可移植也不应整文件冒充Windowshostport |
| postgres.test.ts | Linux/Mac执行 | `/tmp`socket边界、POSIXshell pg_ctl fixture、uid/symlink/删除cwd与UnixDSN |
| 原Python全suite / wheel/PG/资源smoke | Linuxguest执行 | POSIX imports/fd/PTY/socket/私有Linuxbinary；WindowsPython/pytest并非本产品执行域 |
| test:startup / test:browser | 后续有隔离措施的原生WindowsElectron probe可复用 | 真实GUI/session条件、版本及fixture资源先确认；不是完整产品npm start/WSL应用验收 |

显式小集合命令：

```powershell
node.exe --test .\desktop\dist\browser-policy.test.js .\desktop\dist\oauth-return.test.js .\desktop\dist\ui-preferences.test.js
```

如果其中失败保留原错误，不改assert/skip/xfail让它变绿。完整46项POSIX Node tests仍在Linux/Mac跑；共享合同不因Win不适用而从套件删除。Windows这5条通过，也不能填“G0-W-C/P/F/N通过”。

## 6. G0-W新手交接的硬边界

- 清单必须把“用户可立即执行的机器/工具准备”和“需实施者编写并审查的G0探针”分栏。当前repo没有已完成的WslHostOperations/TLS/helper，不能写一个不存在的probe命令让用户以为已提供完整自动验证。
- 开发WSL环境和产品专用Pulsara执行域不得互相冒充：专用distro来源/版本/用户/nonroot/interop/automount/import必须按根规格G0-W-D另做。其他Ubuntu上全pytest通过只支持Linux行为，不证明本次WSL部署、网络与控制。
- G0脚本/证据放明确output个人目录；源码通过Git revision定位，WSL/Windows各记录OS/工具/argv/实际结果。不导出全部process.env、不粘贴保存settings/实际凭据；不生成源码/文档证据hash。官方下载完整性摘要属于真实制品边界，可保留。
- 首轮只需要工具/clone/锁文件/编译与5条小集合证据；碰到WSL未装/企业策略/虚拟化/错误distro/未提供rootfs或probe等缺项就明确停在准备完成/待补。不要安装nativePG/ConPTY/pwsh或把Linux资源改后缀作为绕过。
- 无单独实施授权与G0硬证据不进入B；Ubuntu原生/GUI仍保待验收，不因有一台Windows+WSL就自动填通过。

## 推荐给root的清单结构

1. 当前能做/不能做及4状态；Windows普通用户打开PowerShell/区分管理员一次性WSL动作。
2. Windows工具与同revision独立clone；明确先验nodeplatform/arch/version。
3. WSL前提与专用发行版部署由backend/terminal给出安全单路径；无未知distro覆盖/全局shutdown。
4. Linuxext4独立clone/工具与资源构建；为什么不共享目录及产物。
5. 当前可运行的Windows安装/编译/5条tests；当前不可运行的产品start/package/mise图。
6. G0-D/C/P/F/N逐项输入、实际命令/探针提供责任、预期证据、失败停止、可清理范围。
7. 输出模板/下一次交接内容；明确全程未实际执行Windows，不预写通过。
