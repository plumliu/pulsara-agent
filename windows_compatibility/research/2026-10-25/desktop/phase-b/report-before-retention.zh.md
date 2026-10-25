# Windows Phase B：WSL 宿主、资源与 Session 接缝

本目录记录正式源码实现的定向验证；不是 G0 fixture 的产品激活声明，也不代替父任务的完整 main、用户配置、provider、安装更新验收。生产文件没有导入研究探针。未提交。

## 所有权与实现

- `WslSetup` 从真实 WSL 列表发现已有 WSL2 Ubuntu，显式确认非 root 用户、实际 passwd home、UID、ext4；只保存 distro/user。损坏保存记录返回固定 savedError 和真实列表，必须重新明确选择，读取本身不改旧记录。不会新建 distro、改默认目标或 wsl.conf。
- `createWslHostOperations` 固定目标副本，用绝对系统 wsl.exe、shell:false、明确 -d/-u/--cd/--exec。Windows WSLENV 不转发；guest env 仅三个已有运行字段。runtime 在原 Python 入口忽略 SIGHUP 后执行原模块，EOF 和生命周期仍归原 owner。日志写入 Windows 宿主目录，不为每块 stderr 启动 WSL 子进程。
- setup 使用现有资源 recipe 与 GNU tar；native Ubuntu 构建 ELF 资源和当前 Windows 源码 wheel，再包为随 Windows 应用交付的固定 tar。SHA-256 仅用于此不可变制品传输边界。用户私有 ext4 staging、外置 flock、验证后原子发布；同版本不同材料拒绝覆盖。取消关闭本次输入并等有限 installer 真实 close，部分 tar 不会发布。创建安装目录前检查最近已存在祖先的 canonical/owner 事实。
- `backend-paths` 只作固定 distro 的纯内存 UNC/POSIX 映射。保留大小写和 Unicode；不可表示的 Linux 名称仍能留在 Linux 预览，Windows reveal 映射拒绝 ADS、设备名、尾点空格等歧义。文件权限与最终 fd 验证由 guest 原 owner 负责。
- `BackendSession` 为每次 ready 建独立非持久 Electron Session；全 PEM 与 exact HTTPS 127.0.0.1 origin 共同验证。PDF 只准精确内置扩展以及该 frame 的 chrome://resources。退休先封准入，等待调用方原浏览器/请求 owner，再关连接、等 DownloadItem done、清存储；保留原 owner 失败。
- 共享 `BrowserOwner` 为关闭后尚未 destroyed 的 WebContents 保留原 partition 所有权；并发 forget/retire 等同一真实 destroyed 与 clearData，不能仅从 pages map 消失就宣布退休。
- tar 中 WSL 专用 launcher manifest 只使 CLI 给出“请从 Windows Pulsara 图标启动/聚焦桌面应用”的明确提示，不反向 RPC，不尝试 Linux 桌面 exe。原 Mac/Linux launcher 分支保留。

## 实际结果

1. 四个新增 TS 模块 12/12 Node 测试通过，Windows tsc 通过。父任务另完成隔离 ext4 全 desktop Node 97/97；其初次 Electron 顶层 import 引发隐式 Linux binary 下载的失败已保留，改为 type-only import 与仅默认参数 require 后重跑通过。
2. `host-smoke-results.json`：真实 Windows→Ubuntu/plumwsl 的 UID、cwd、literal argv、binary stdin、固定 target 副本、环境准入、wslpath 和本地 host log 均通过，自有 guest 目录已删除。
3. `setup-failure-results.json`：缺制品、错误摘要、非 tar、提交前取消、实际部分 archive bytes 后取消五项通过；原 ENOENT/取消 error 保留，无 version 发布、staging 空、flock 可重新取得。没有用实验观察等待替代产品 lifetime cap。
4. `saved-selection-results.json`：三种损坏记录保持原文件，返回实际 distro 且无 saved；随后显式 select 成功覆盖、重开一致、临时文件删除。
5. `session-smoke-results.json`：真实 production BackendSession + Windows Electron，Linux TLS guest 持 SSE、两个 pending fetch、慢 download；retire 后 download cancelled、全部 Linux handler 归零、旧 Session 新请求拒绝，carrier0/host0。lazy Electron import 修正后同一真实默认接口再次通过。
6. BrowserOwner 严格原版负控 10/13，失败包括提前 close 后 forget、提前 close 后全 host retire、加强的 host destroyed 结算；当前正式修复 13/13、exit0。最初较弱观察门的原 11/13 记录与源码也保留，最终结论依据加强门。没有弱化断言或 sleep 掩盖 join。
7. Python launcher 原/新两项在真实 Linux 通过；Windows release inventory 五项、Windows payload manifest 四项通过。没有声称原生 macOS 实测。

## 当前制品与范围

`build-stage.json` 记录私有 ext4 构建目录、Windows 源码/static 来源和快照时间。最终 freeze 源码快照为 2026-10-25 08:02:23–30 UTC，包含 08:01:43 的 hooks/SDK 部分 task 创建失败修正；资源 wheel 安装在 08:02:32 UTC。最终 tar 08:03:28 UTC 完成，版本 0.1.0-alpha.6，材料摘要以 manifest/build-stage 为准。早期三个候选均在安装前淘汰，历史时间与材料值保留，不是已激活产品版本。

helper 当前源经真实 .NET Framework csc 编译、--version 正控；成员 HANDLE 修正的真实循环正负证据由 interop owner 归档：原六样本 191→271，修后六样本恒189，随后正常 join/retire。helper 协议/Job/SCM 首次绑定与消费者资格属于该 owner 的正式测试范围。

NSIS 使用维护 SDK 的 per-user 安装和自定义只读维护门：不终止 app/helper/WSL/PG；发现进程、锁忙、helper unknown 或 maintenance 非零时阻止替换。真实 `electron-builder --win --x64 --publish never` 退出0，包含自定义 NSIS 宏实际编译。最终 `desktop/release/Pulsara-0.1.0-alpha.6-windows-x64.exe` 537003273 bytes，unpacked 与包内 PE/version 正控通过；tar 1595770880 bytes，SHA-256 `77332ad0f6f0e11d2f5e990f1811a45ee29992fe745a1393b3ef15aab24dcac9`。具体文件和有限结果见 windows-package-results.json。没有运行 installer；实际安装/更新拒绝、用户配置与完整 UI 状态仍归父任务后续验收。本次没有发布 release 或提交。

## 清理与限制

真实定向 smoke 的本次 Linux workload/临时目录及 carrier 已退出；损坏选择临时文件、取消用大文件已删除。构建私有 ext4 目录与输出 tar/PE 保留供当前候选构建和父任务安装复核，不属于用户 settings/workspace。Electron idle 测试 profile 保留在 ignored output；未在进程持有文件时递归删除。原失败、stderr 与候选脚本均保留。未改共享 Ubuntu/global interop/TUN/firewall；未读取或复制真实 provider secrets。
