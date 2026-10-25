# Windows Phase B：WSL 宿主、资源与 Session 接缝

本目录记录正式源码实现的定向验证；不是 G0 fixture 的产品激活声明，也不代替父任务的完整 main、用户配置、provider、安装更新验收。生产文件没有导入研究探针。未提交。

**安装验收状态：** 首个真实 NSIS 安装及首次私有资源部署成功，kernel 新库验证报 schema_catalog_drift；当前新候选已包含 canonical LF、MCP Host close 与 Windows 只读 HEAD 就绪屏障修正，正在交父任务按维护门作明确开发替换。Windows 尚未激活。下面构建/定向通过结果保留其原范围，不等于完整启动通过；首次只读诊断期间已装 alpha.6、tar、数据库保持故障现场，后续替换由父任务明确执行并另行记录，未静默同版本覆盖。根因见末节。

## 所有权与实现

- `WslSetup` 从真实 WSL 列表发现已有 WSL2 Ubuntu，显式确认非 root 用户、实际 passwd home、UID、ext4；只保存 distro/user。损坏保存记录返回固定 savedError 和真实列表，必须重新明确选择，读取本身不改旧记录。不会新建 distro、改默认目标或 wsl.conf。
- `createWslHostOperations` 固定目标副本，用绝对系统 wsl.exe、shell:false、明确 -d/-u/--cd/--exec。Windows WSLENV 不转发；guest env 仅三个已有运行字段。runtime 在原 Python 入口忽略 SIGHUP 后执行原模块，EOF 和生命周期仍归原 owner。日志写入 Windows 宿主目录，不为每块 stderr 启动 WSL 子进程。
- setup 使用现有资源 recipe 与 GNU tar；native Ubuntu 构建 ELF 资源和当前 Windows 源码 wheel，再包为随 Windows 应用交付的固定 tar。SHA-256 仅用于此不可变制品传输边界。用户私有 ext4 staging、外置 flock、验证后原子发布；同版本不同材料拒绝覆盖。取消关闭本次输入并等有限 installer 真实 close，部分 tar 不会发布。创建安装目录前检查最近已存在祖先的 canonical/owner 事实。
- `backend-paths` 只作固定 distro 的纯内存 UNC/POSIX 映射。保留大小写和 Unicode；不可表示的 Linux 名称仍能留在 Linux 预览，Windows reveal 映射拒绝 ADS、设备名、尾点空格等歧义。文件权限与最终 fd 验证由 guest 原 owner 负责。
- `BackendSession` 为每次 ready 建独立非持久 Electron Session；全 PEM 与 exact HTTPS 127.0.0.1 origin 共同验证。PDF 只准精确内置扩展以及该 frame 的 chrome://resources。退休先封准入，等待调用方原浏览器/请求 owner，再关连接、等 DownloadItem done、清存储；保留原 owner 失败。
- 共享 `BrowserOwner` 为关闭后尚未 destroyed 的 WebContents 保留原 partition 所有权；并发 forget/retire 等同一真实 destroyed 与 clearData，不能仅从 pages map 消失就宣布退休。
- tar 中 WSL 专用 launcher manifest 只使 CLI 给出“请从 Windows Pulsara 图标启动/聚焦桌面应用”的明确提示，不反向 RPC，不尝试 Linux 桌面 exe。原 Mac/Linux launcher 分支保留。

## 实际结果

1. 四个新增 TS 模块 12/12 Node 测试通过，Windows tsc 通过。父任务另完成隔离 ext4 全 desktop Node 最终105/105；其初次 Electron 顶层 import 引发隐式 Linux binary 下载的失败已保留，改为 type-only import 与仅默认参数 require 后重跑通过。
2. `host-smoke-results.json`：真实 Windows→Ubuntu/plumwsl 的 UID、cwd、literal argv、binary stdin、固定 target 副本、环境准入、wslpath 和本地 host log 均通过，自有 guest 目录已删除。
3. `setup-failure-results.json`：缺制品、错误摘要、非 tar、提交前取消、实际部分 archive bytes 后取消五项通过；原 ENOENT/取消 error 保留，无 version 发布、staging 空、flock 可重新取得。没有用实验观察等待替代产品 lifetime cap。
4. `saved-selection-results.json`：三种损坏记录保持原文件，返回实际 distro 且无 saved；随后显式 select 成功覆盖、重开一致、临时文件删除。
5. `session-smoke-results.json`：真实 production BackendSession + Windows Electron，Linux TLS guest 持 SSE、两个 pending fetch、慢 download；retire 后 download cancelled、全部 Linux handler 归零、旧 Session 新请求拒绝，carrier0/host0。lazy Electron import 修正后同一真实默认接口再次通过。
6. BrowserOwner 严格原版负控 10/13，失败包括提前 close 后 forget、提前 close 后全 host retire、加强的 host destroyed 结算；当前正式修复 13/13、exit0。最初较弱观察门的原 11/13 记录与源码也保留，最终结论依据加强门。没有弱化断言或 sleep 掩盖 join。
7. Python launcher 原/新两项在真实 Linux 通过；Windows release inventory 五项、Windows payload manifest 四项通过。没有声称原生 macOS 实测。

## 当前制品与范围

`build-stage.json` 记录私有 ext4 构建目录、Windows 源码/static 来源和快照时间。当前最终源码快照为 2026-10-25 08:36:55–08:37:02 UTC，包含 canonical migration resource bytes 和 08:21:35 MCP Supervisor close 修正；wheel 安装08:37:04，tar完成08:37:56。版本仍为0.1.0-alpha.6。此前23781d9893174f015b4e37899ecff52a8ee9f7bd6f608d30ad8dfb2a62e52238候选实际安装后暴露CRLF问题；其他更早候选仅构建未安装。当前不静默覆盖已装Linux版本，由父任务明确记录旧材料/空测试库开发清理和替换。

helper 当前源经真实 .NET Framework csc 编译、--version 正控；成员 HANDLE 修正的真实循环正负证据由 interop owner 归档：原六样本 191→271，修后六样本恒189，随后正常 join/retire。helper 协议/Job/SCM 首次绑定与消费者资格属于该 owner 的正式测试范围。

NSIS 使用维护 SDK 的 per-user 安装及只读维护门，不终止 app/helper/WSL/PG；busy/unknown/非零拒绝替换。当前最终 electron-builder --win --x64 --publish never 真实退出0，自定义NSIS宏实际编译；安装器537003394 bytes，包内tar1595781120 bytes，实际全内容校验SHA-256 be6afac1663c37c7101f6bdeae68d5fd6863a7da828ba9b50a2dd988730d73b9与manifest一致，包内PE17920 bytes、--version0。见 windows-package-results.json。父任务走原旧installed maintenance门再安装，Finish不自动启动，之后按明确开发授权精确处理本轮空测试PG/旧runtime；这不是同版本产品自动升级成功。本分支未执行installer、发布release或提交。

## 清理与限制

真实定向 smoke 的本次 Linux workload/临时目录及 carrier 已退出；损坏选择临时文件、取消用大文件已删除。构建私有 ext4 目录与输出 tar/PE 保留供当前候选构建和父任务安装复核，不属于用户 settings/workspace。Electron idle 测试 profile 保留在 ignored output；未在进程持有文件时递归删除。原失败、stderr 与候选脚本均保留。未改共享 Ubuntu/global interop/TUN/firewall；未读取或复制真实 provider secrets。

## 本次追加冻结

retention 原确定性失败、最终共享129/129与真实跨域27/27证据归 `windows_compatibility/research/2026-10-25/terminal/phase-b/`（retention-deterministic-baseline.json、evidence/shared-final.json、final-matrix.json）。本分支不复制该 raw 矩阵。Windows CI 已添加原 main.test 的 `^Windows ` 筛选，真实本机最终10/10、skipped0；Linux/Mac jobs 仍执行原完整 npm test，没有修改断言或添加 skip。root TS 冻结后最新 NSIS 构建退出0；包内 tar 全内容摘要与最终 manifest 复核一致、包内 helper --version0。上一构建日志/results/report 均另存 before-retention 历史文件，不混为本次最终结果。父任务接手真实 NSIS 安装；本分支未执行 installer。

小证据归档位置为 `windows_compatibility/research/2026-10-25/desktop/phase-b/`，archive-files.txt 列逐文件清单。归档的 smoke 源码保留原 output 执行上下文，供审阅/复现，不作为生产 import 或新协议入口。cleanup-audit.json 只读复核本次两个 guest home 不存在、原 Session PID/starttime 不存活、各 carrier/host 已退出；构建私有目录保留且归 uid1000。默认 per-user NSIS 路径来自维护 SDK 的 UserProgramFiles/Pulsara；本机常规值为 `C:/Users/plumlocal/AppData/Local/Programs/Pulsara`。实际安装前本用户 HKCU 卸载项/默认目录未见 Pulsara，没有将此观察扩称其他机器或系统级安装情况。

## 安装后 CRLF 缺陷只读诊断

父任务实际安装路径为 `C:/Users/plumlocal/AppData/Local/Programs/pulsara/pulsara.exe`（实际向导结果优先于上文 SDK 推荐路径显示大小写），首次 setup 已完成资源发布，新库随后被原 verifier 拒绝为 schema_catalog_drift。

`schema-resource-audit.py/json` 未打开数据库，只读比对 Windows checkout、私有源码快照、构建资源、实际已装资源和 Linux 主 clone：SQL 在 Windows/快照/wheel/已装处一致为 150654 bytes、2793 个 CRLF；Linux 原文件 147861 bytes、0 CRLF，前者仅 CRLF→LF 后逐字节等同后者。两个 JSON 也只存在换行字节差异，解析语义一致，expected catalog 保持原 LF 基线的既有值。最初只读 git ls-files --eol 观察为 i/lf、w/crlf 且无属性覆盖；随后 PG 分支开始修复并写入 .gitattributes，归档 git-eol-audit.json 于 08:20:19 UTC 捕获的是 core.autocrlf=true、i/lf、w/crlf 且 attr/text eol=lf，早于工作树重检出；不能把该稍后 JSON 当作属性尚未存在的快照。

原 registry 用 read_bytes().decode('utf-8') 发送完整 SQL，不做 newline 转换；原 observed verifier 包含 pg_get_functiondef 全文。PG 分支独立只读数据库证明五个 PL/pgSQL 函数体 CRLF 分别 735/79/18/21/4，原观察失败，仅内存 CRLF→LF 后恰匹配 expected；证据归其 `output/windows-g0/pg_admission_r3/phase-b-pg/diagnose_installed.py` 及对应原输出。本分支没有重复连接数据库或改变校验器。

因此这不是丢资源或新 schema 语义，而是本次 Windows 工作树到 native Linux wheel 的构建输入未保持 Git LF 内容。修复需由父任务确认 canonical LF 输入/构建策略及新候选版本；只在执行时替换 SQL 文本会留下 raw SQL/JSON 参与既有 migration universe 的身份差异，不能代替完整修复。不得修改 observed verifier 来接受已知不同 catalog，也不得手工迁移当前现场后声称原包通过。

## Windows localhost 就绪屏障修正

PG分支对同一真实DesktopApplication记录Linux ready后Windows首次HEAD/GET拒绝，约531ms后原PID/证书端点自然200；没有重启或重发写操作。BackendSession新增waitUntilReachable(signal?)，用同原生Session/fullcert/exactorigin的SDK fetch，只读HEAD根路径、redirect:error。真实SDK原错误为Error.message精确net::ERR_CONNECTION_REFUSED（code/cause均null），仅该值按250ms调度采样，无总寿命cap；其他错误/HTTP非成功立即失败。seal/retire及main取消会abort在途请求/等待，并await原readiness task；正常abort不新增退休失败，原owner与真实Session清理错误仍缓存拒绝。main在navigate前await此门，Quit取消不阻断原PG安全结算。

readiness-unit.log为8/8；Windows main最终筛选10/10、skipped0（Linux/Mac仍原全量），父任务最终desktop105/105。readiness-smoke-results.json真实Electron/WSL6项：同pin delayed-listen两拒绝后200、采样中retire、外部取消、在途HEAD retire、错误pin原ERR_FAILED仅1次、401仅1次；全部host/carrier0及私有home清空。小型JS观测facade仅调用同一个nativeSession.fetch，不复制SDK或协议。其最终观察门也覆盖prepared前等待，15s仅是研究fixture失败观测限度，不进入生产。

保留两个原harness失败：事件字符串误与完整event对象比较，prepared从未放行，尚未发HEAD。第一次关于native.fetch不可包装的推测已撤回，未得到证据；实际根因为事件比较错误。原源码/进程记录/cleanup及第二次progress保留，用精确Windows HANDLE及creation事实仅结束本次隐藏fixture主进程，stdin随之关闭让Linux finally删除自有home；没有泛杀共享WSL。第一次/proc fd0写入被权限拒绝后未绕过。另首次Node命令将筛选参数放文件后，意外跑原POSIX main case在Windows路径断言失败，原断言保留，正确命令重跑通过，摘要见readiness-first-unit-command.json。

CI native Mac/Linux build增加test_migration_resource_portability.py，真实WSL2/2通过；原stage5仍由带完整PG服务的既有backend suite覆盖。PG分支本机14pass/1缺pgcrypto前置资源失败如实保留，未为测试扩大正式依赖或加skip。

当前修复定义由registry统一把SQL和两个JSON资源转为唯一LF字节用于原身份、校验和执行；wheel中物理文本即使来自CRLF checkout也不再改变clean-v0含义，.gitattributes使未来Git检出直接LF。原observed verifier未放宽，当前新候选的同版开发替换仍需父任务实际安装/首启/重开验收。
