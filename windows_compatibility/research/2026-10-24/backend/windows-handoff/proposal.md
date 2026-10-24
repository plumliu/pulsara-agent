# Windows准备与G0-W：backend交接建议

2026-10-24；gpt-6.1-sol/high。只读核冻结spec及实际脚本/依赖，未改root/production，未运行WSL、DB或provider。本文件供root汇总新手操作清单，不是已执行结果。提交/推送由root按用户授权处理。

## 先让新手知道当前能做到哪里

当前只有Phase A本机POSIX宿主接缝。**尚无Windows安装器、WSL setup/host adapter、Linux finite PG helper、TLS workbench、Linux-backed workspace picker或OAuth host动作。** 不能写“安装WSL后npm start就能运行Windows Pulsara”；也不能把Linux Electron或WSLg窗口当Windows原生界面。本轮不实现B，G0应是独立实验探针与证据，不在production加临时分支。

建议顺序：Windows只读盘点 → WSL engine前提 → 新专用测试发行版/固定rootfs → 一次root provision → nonroot/配置重开核实 → Linux构建/资源smoke → 独立D/P/F/N临时probe → 汇总pass/fail/pending → 另行决定B。不能把清单完成、G0通过和Windows产品支持混成一个勾。

每个命令块标注“Windows PowerShell”“Pulsara Linux bash”“一次性root provision”三者之一。PowerShell只负责系统/发行版/宿主probe管理，产品模型执行仍Linux；Windows Terminal中的Ubuntu标签不是PowerShell。正常应用与initdb绝不root。

## 1. Windows先只读盘点

**Windows PowerShell普通用户**逐条执行，保留实际输出与`$LASTEXITCODE`：

```powershell
Get-ComputerInfo -Property WindowsProductName, WindowsVersion, OsBuildNumber, OsArchitecture, CsSystemType
wsl.exe --version
wsl.exe --status
wsl.exe --list --verbose
```

确认首批矩阵为Windows11 25H2 x64、当前可用的WSL2；Windows发行版本以`winver`或系统设置显示的版本/build核对，不单凭可能保留旧兼容值的WindowsVersion字段；x64与ARM64不能只看“64-bit”。`--version`是WSL组件版本，发行版列表末列的2才是该发行版WSL2资格，两者不要混淆。检查是否已有名为Pulsara的发行版；**有同名未知实例就停下记录冲突，不import覆盖、不unregister、不拿默认Ubuntu兜底**。不自动改默认发行版、默认WSL版本或全局`.wslconfig`。

若缺WSL/虚拟化/企业许可，走微软官方安装说明。系统功能安装可能需管理员和重启；与后续普通用户运行分开。选择不安装默认发行版的引擎路径时，可由用户明确在所需管理员窗口运行`wsl.exe --install --no-distribution`，按系统提示完成/重启，再回到普通用户盘点；不能要求使用空白`wsl --install`却没说明它默认装Ubuntu。[Microsoft install](https://learn.microsoft.com/en-us/windows/wsl/install)、[commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

Pass：实际版本/架构/WSL2可用，普通用户可运行wsl命令，未碰其他distro。Fail：企业策略禁用/虚拟化不可用等实际报错。Pending：须重启或尚未确定WSL受测版本。不要“重试直到绿”或自动升级用户全局配置。

## 2. rootfs与用户：准备好固定材料后才执行

开发者先给出**确切Ubuntu24.04 amd64 rootfs的build、官方URL、摘要/签名及已验证的import格式**。当前repo并没有锁定rootfs recipe，不能拿浮动`latest`/`Ubuntu`别名填空；不能从当前Mac资源目录复制Mach-O payload到Linux。[Ubuntu安装来源](https://ubuntu.com/wsl/docs/latest/howto/install-ubuntu-wsl2/)、[Microsoft import](https://learn.microsoft.com/en-us/windows/wsl/use-custom-distro)

Windows资料准备示意（变量必须由已核对制品填写，不是可直接照抄的真实下载值）：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath $RootfsFile
wsl.exe --import Pulsara $RetainedDistroDirectory $RootfsFile --version 2
wsl.exe --list --verbose
```

这里只对下载制品验证官方摘要，禁止给代码/文档/G0证据再存hash。安装目录是每用户**保留数据目录**，不放版本化应用目录或Electron cache；失败保原件，禁止为重试unregister。

导入默认可能是root，开发者提供的有限一次provision应明确：创建`pulsara`用户/同名组与home、工作根`/home/pulsara/workspaces`，设置default user；不加入免密sudo组、不启systemd PG服务、不给renderer任意root执行。没有该固定provision脚本时标Pending，不让新手拼长串`bash -c`、复制任意网络安装脚本或误把root shell当正常运行。

专用`/etc/wsl.conf`应为：

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

写完配置后须让该专用发行版真正停止并重开才验证，不能只cat文本；若需`wsl --terminate Pulsara`让初次无应用状态配置生效，明确它是部署维护，不是日常Quit/cleanup。不要`wsl --shutdown`波及其他发行版。[Microsoft configuration](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)

**Windows PowerShell**在provision/重开完成后可做现成只读检查：

```powershell
wsl.exe -d Pulsara -u pulsara --cd / --exec /usr/bin/id
wsl.exe -d Pulsara -u pulsara --cd / --exec /usr/bin/uname -m
wsl.exe -d Pulsara -u pulsara --cd / --exec /usr/bin/cat /etc/os-release
wsl.exe -d Pulsara -u pulsara --cd / --exec /usr/bin/cat /etc/wsl.conf
wsl.exe -d Pulsara -u pulsara --cd / --exec /usr/bin/findmnt -T /home/pulsara/workspaces -o TARGET,SOURCE,FSTYPE,OPTIONS
```

Pass：Ubuntu24.04/x86_64、uid非0、默认用户与显式用户一致，实际工作根在批准ext4。只读挂载表不等于no-follow/race通过。还须实际PATH不是Windows工具链，config重开生效。

**interop负测陷阱：** automount关闭后`/mnt/c/Windows/...exe`不存在（ENOENT）只说明没挂载，不能证明interop disabled。需要开发者提供已知可工作的Windows PE正对照，再在同一无业务测试环境禁用interop验证拒绝/未产生Windowsmarker；不运行用户exe、不改其他distro。未有该probe就Pending，不能把清WSL_INTEROP/PATH或不存在路径计Pass。该配置不是抵抗Windows账户/root的安全沙箱。

## 3. 当前Linux构建：确切能复用的API与依赖

`desktop/scripts/prepare_resources.py:65`明确只接macOS ARM64或Ubuntu24.04 x86_64/ARM64；`main():610`所有component先target_platform。**Windows原生Python运行它会拒绝，替换`.venv/bin/python`成Scripts路径不会解决。** `mise desktop:resources/verify/package`当前均POSIX配方；`package.json`只有Mac/Linux目标、release脚本只有DMG+两个deb；本轮不能据这些命令声称有Windows NSIS。

实际pins来自mise/.python-version/package-lock/prepare_resources：Node22.23.3、uv0.13.4、protoc34.0、Python3.12.15、Electron44.9.0、PG17.9、vector0.8.1、pg_search0.26.1、rg15.2.0。mise最低2026.7.18，当前CI实际用2026.10.15；不要把Mac生成的`desktop/build/environment.json`当Linux环境事实，它由本机`build_environment.py`重建。

Ubuntu构建依赖复用`.github/workflows/desktop-package.yml:63`：build-essential、bison、flex、pkg-config、binutils、patchelf、libopenblas0、xz-utils；干净rootfs另需Git、CA证书、下载/解压工具。先核命令`cc make bison flex pkg-config readelf patchelf ldd dpkg-deb dpkg-query xz ar`，缺项按Ubuntu维护包一次安装。Chromium OS依赖用当前已安装Playwright的维护命令`python -m playwright install-deps chromium`；OS安装是明确一次root维护，不让正常agent依赖sudo。无需为这条Linux预编译扩展路径装VisualStudio/pywin32/Windows PostgreSQL/Rust/pwsh。

repo放`/home/pulsara/workspaces/...` ext4，Windows与Linux不要共享`.venv/node_modules/desktop/resources`；不要在`/mnt/c`checkout上构建后假称通过目标FS。首次clone URL由root提供当前远端，不在报告中猜私有repo地址。工具安装依维护文档且按上述pin，不能下载“最新”代替锁。

在**Pulsara Linux bash、普通pulsara用户、repo根**，以下是现有脚本路径：

```sh
id -u
uname -m
cat /etc/os-release
mise install
uv python install 3.12.15
mise run desktop:bootstrap
mise run --jobs 1 desktop:resources
npm --prefix desktop test
.venv/bin/python desktop/scripts/test_linux_dependencies.py
.venv/bin/python desktop/scripts/smoke_resources.py
.venv/bin/python desktop/scripts/release_artifacts.py check --resources desktop/resources
```

`id -u`为0先退出root shell切回用户，再继续；PG initdb拒root。在resources前须完成上述原生/Chromium OS依赖，否则记录缺项而不是先把失败当Pulsara逻辑缺陷。`mise run --jobs 1 desktop:verify`可替代后面已包含的Node/resource/check组合，避免为了凑证据重复全套。无需同一时刻并行mise bootstrap与pytest：bootstrap用`--no-install-project`暂时移除editable，原位项目重新安装完成后再测。

`prepare_resources.py`现有components只有requirements/postgres/python-dependencies/browsers/application/validate；不带component会调mise图。它会写/替换**构建资源目录**，不是用户数据库；自定义extension-lib/share不是新手默认选项，固定Linuxpg_search noble_amd64.deb已有官方digest，现路线不要求自行编译pgrx。

Pass输出至少：真实LinuxPython3.12.15/ELF架构正确；`pg_search=0.26.1`、`vector=0.8.1`、`search=1`、`vector=0`与最终relocation通过。smoke会复制资源到自己`/tmp/pulsara-payload-*`、净化PG/PULSARA/Python动态库环境、启动自己PG并finally停，再验证Chromium。**它没有覆盖TS lifecycle、finite WSL helper、Windows GUI或TLS，所以仅是G0-P资源子项**；WSL执行也不等于native Ubuntu GUI资格。

不要复制ubuntu-tests.yml的CI loopback DSN/测试账户到产品settings。产品PG保持私有Unix socket/local auth/host reject，Pythonrole/db/schema原owner；G0各probe创建并记录独立短canonical临时home/data/socket，完成先确切pg_ctl stop/status3再删自有目录。失败未知保数据/责任，绝不Windowsrm/猜PID/taskkill全distro。

## 4. G0-P/F/N需要开发者probe的最小清单

| 先后/对象 | 操作与可复用入口 | Pass证据 | Fail/Pending边界 |
| --- | --- | --- | --- |
| P1 Linux资源 | 上述现有build/smoke；随包binary实际版本与BM25/vector | 实际命令/exit/stdout、ELF/Ubuntu事实、PGsocket目录与清理 | root/缺依赖/版本错先修前提，不替换PG/删搜索/打开host trust |
| P2 TS PGowner/真实Linux实例 | 临时Node probe直接require最终dist PostgresOperations/BundledPostgres；自建canonicalhome | init/reuse、原state、失败start原error、错实例/版本拒绝保data、stop/reset顺序、stat/权限/DSN | 当前Mac probe只能参考设计，不能复制`/Users/...`路径或结果当Windows/WSL通过 |
| P3 跨WSL有限helper | 需新增output临时probe，模拟固定-d/-u、NB kernel flock→ready→Node单operation→child/readback→完整结果/close | ready前取消及ready后未发帧均EOF+join；发帧后unknown不重放；lostreply/延迟调度时无锁外late start；PGserver不继承kernel锁 | 该helper生产未实现，不能把`wsl pg_ctl status`一次3当完整过门；helper先死child存活释放锁则gate失败修机制 |
| F1 Linux物理文件 | 现no-follow/rename-no-replace/flock/preview tests +临时真实mount/symlink竞态probe | 允许根/实际ext4，拒错distro/DrvFs含改挂载点及bind替代；原数据/borrow/identity不变 | 字符串拒`/mnt/c`、只cat config、Windows stat看uid均不足 |
| F2 用户级图形picker | 需output临时renderer+现owner窄probe；无session分页/新建/选择后再次admission检查 | 图形新建/冲突/取消/结果不明/最近missing、canonical身份、中文/空格/大小写、替换/挂载失败 | 当前生产未有Linux-backedpicker；手敲mkdir不计图形流程Pass，不能暗中换UNC主picker |
| F3 Windows File与preview | 显式Windowsfile input/drop→原bytes LocalImport→Linux引用；token/content/HTML资源流/Windows下载保存 | 实际File字节/最终文件内容、generation/revoke、no-clobber/partial/unknown；CSP/iframe/null-Origin与普通Browser控制port隔离 | 字符串path不是File；目录不隐递归；file://UNC不替授权；Windows另存不是模型Linuxexport原子提交 |
| N1 仅网络可达 | 空临时目录起Linux127.0.0.1临时HTTP，Windows浏览器/curl.exe访问，保存端口/模式/代理/VPN/firewall事实 | 正确自己空目录可达、无LAN绑定；中断如实 | 只是可达预检查，不是TLS/source identity gate；不可0.0.0.0/portproxy改全局配置救绿 |
| N2 完整连接身份 | output probe aiohttp SSLContext+Electron44.9.0独立Session，可信stdio送本次publiccert/精确origin | 所有首导航/fetch/SSE/preview/download验证exactcert+scheme/host/port；旧cert/假listener/rebind拒绝；retire旧WebContents/连接 | 一次明文nonce、忽略证书、热换pin不清cache都不算；失败需修规格机制，不能fallback |
| N3 OAuth真实系统浏览器 | 注入现management async open_browser接有限可信host动作，SDK仍PKCE/state/iss/token owner | 原listener先安装；Windows打开结果/取消未知；端口抢占/rebind/错state/issuer/PKCE/复用拒绝 | Linux默认webbrowser.open在禁interop/无WSLg不可假称会开Windows；pulsara URI只focus，code/token不跨host |

可先跑现有Linux纯文件资格（不当F全门）：

```sh
PLAYWRIGHT_BROWSERS_PATH="$PWD/desktop/resources/playwright" .venv/bin/python -m pytest -q tests/test_local_file_import.py tests/test_session_file_preview.py tests/test_html_local_resources.py
```

已读这些tests与conftest：使用tmp_path/测试server/连接替身，相关三个文件不请求共享Postgres fixtures；HTML真实截图需要上述Chromium资源/OS依赖。命令仍应在当前uv env/有效repo运行，保存真实失败，不加skip/xfail。它们不验证WindowsFile API、TLS或最终GUI。

## 5. 新手交证据的最小格式

每项保存：在哪个shell/OS/arch/WSL2版本、Git revision/dirty、完整命令/exit、实际stdout/stderr、自己的temp home/data/socket及开始/结束状态、Pass/Fail/Pending和具体缺项。记录真实错误，不只截图绿色按钮；PG日志/普通provider以外内容不广泛遮盖，实际secret不进报告。rootfs/payload下载验证可用官方digest，普通代码/证据不生成hash。

发生失败：先保原件/日志；只按已知精确owner正常关闭自己的probe；无法确认停止则保目录并报告unknown，不“清理一下所有WSL/PG”。不要照抄`unregister`、`shutdown`、远程DSN、安装系统PG服务或重置原用户数据库。

最终汇总应分别列A Ubuntu待验收、G0-D/C/P/F/N、B未实施。环境准备好但F/N probe尚未提供的状态是Pending，不是路线已通过；Windows支持与发布仍保持未激活。
