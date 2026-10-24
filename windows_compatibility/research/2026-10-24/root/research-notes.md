# 汇总工作记录

日期：2026-10-24。基线 Git commit：`60e8f21d37ca2c650500ae14438630006d3e62fb`。

本目录是 root 的调研工作区；terminal、backend、desktop 分别由三位 `gpt-6.1-sol` / `high` agent 调研。根实施 spec 由 root 编写，所有 agent 再交叉审阅。不改生产实现、用户设置或数据库。

## 已直接核查

- 根 AGENTS、INSTALL、DEVELOPMENT、pyproject、uv.lock、mise、CI；当前根两篇功能 spec。
- `terminal_process/{manager,environment,models}.py`、`ports/terminal.py`、builtin catalog、terminal_risk、tool_permission。
- `desktop_runtime.py`、`desktop/src/{runtime,postgres,main,cli-install}.ts`、frontend desktop-host/file-preview、file import、local source binding、exclusive publish、workspace identity、memory scope、settings、migration baseline 与 pg_search query。
- 历史桌面迁移合同、终端认知减法已实施合同。特别核实持久 shell 候选顶部明确“未实施”，不能误用为现行能力。
- AST 静态候选见 `posix-candidates.json`：15 个文件，61 处候选，仅用于导航，不是完整缺陷数量，不是 Windows 实测。

## 根汇总决定依据

目标以 native Windows 11 25H2 x64 闭合，条件实施 gate 先证明扩展数据库、进程/ConPTY、安全文件 owner。冻结设计不等于通过 gate 或发布支持。WSL2 可能降低开发成本，但改变整个执行域，不在用户未指定时自动替代 native 目标。

官方资料于本轮检索：

- ParadeDB 官方 extension 安装矩阵无 Windows 预编译包：https://www.paradedb.com/docs/operate/deploy/self-hosted/extension 。这不能推出无法源码构建。
- 固定 pg_search 0.26.1 workspace 明确 pgrx=0.19.3：https://raw.githubusercontent.com/paradedb/paradedb/v0.26.1/Cargo.toml 。其依赖包含 rustix/fs、非 macOS OpenBLAS 等，需要真实 Windows 构建验证。
- pgrx 0.19.3 README 声明 Windows x86_64 tested：https://raw.githubusercontent.com/pgcentralfoundation/pgrx/v0.19.3/README.md 。禁止写成 pgrx 不支持 Windows。
- Python 3.12 asyncio Windows 平台限制：https://docs.python.org/3.12/library/asyncio-platforms.html 。不能将 Python asyncio Unix server 不支持误写成所有 Windows 系统都没有 AF_UNIX。
- ConPTY 的关闭/读写排空边界：https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session 。关闭可能终止 attached clients，必须处理并发排空与死锁。
- Job Object 与创建时属性：https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects 、https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute 。不要用启动后竞态挂 job 或仅根进程 wait 冒充原来的物理结算。
- Reparse points：https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points 。no-follow 要覆盖 junction，不只符号链接。
- WSL 文件系统/网络/命令：https://learn.microsoft.com/en-us/windows/wsl/filesystems 、https://learn.microsoft.com/en-us/windows/wsl/networking 、https://learn.microsoft.com/en-us/windows/wsl/basic-commands 。localhost 已有平台能力，不能默认新建 bridge 或开放0.0.0.0。
- Windows support baseline：https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information 。25H2 当前仍在支持期。
- PowerShell LTS：https://learn.microsoft.com/en-us/powershell/scripting/install/powershell-support-lifecycle?view=powershell-7.6 。选择7.6 LTS，实际资源recipe锁定受支持补丁与官方包；不沿用即将结束支持的7.4。

## 验证限制

本机 macOS。源代码和 SDK/API 调研可以发现硬阻断与定义 gate，不能证明 Windows ConPTY、NTFS/ACL、PG扩展、NSIS 或原生 GUI 可用。最终 spec 必须逐项保留此限制。没有执行 Windows 实现或 Windows dogfood。
