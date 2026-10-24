# 独立可丢弃 WSL2 故障环境计划（尚未执行 import）

2026-10-25。本计划只准备本次 G0 的 distro terminate 故障输入。当前只有共享 `Ubuntu`（Running、WSL2）；它保持运行，interop/Hiddify TUN 保持原状。没有下载镜像、创建发行版、改默认发行版或执行 terminate/shutdown。

## 来源、体积与空间

现有共享 Ubuntu 的 `/etc/os-release` 为 Ubuntu 26.04.1 LTS。拟使用同版本的 Canonical 官方 [26.04.1 WSL root filesystem](https://releases.ubuntu.com/26.04.1/ubuntu-26.04.1-wsl-amd64.wsl)，[官方目录](https://releases.ubuntu.com/26.04.1/) 列 399M、2026-08-27；不是 ISO、cloud VM 镜像或旧 cloud-images WSL tarball。Ubuntu [官方安装说明](https://documentation.ubuntu.com/wsl/latest/guides/install-ubuntu-wsl2/) 指定 releases.ubuntu.com 的 `.wsl` 格式；[定制说明](https://documentation.ubuntu.com/wsl/latest/howto/custom-ubuntu-distro/) 明确它是可按 tar 处理的 rootfs。

[官方 SHA256SUMS](https://releases.ubuntu.com/26.04.1/SHA256SUMS) 对该镜像的值为 `48d56724b5c8e60f24893e83e73bbb58c60b3ca22fba3da977075420acd54104`。执行阶段下载后核对此不可变内容摘要；下载时重新核官方文件/版本，不给普通代码加 hash。必要时以官方 SHA256SUMS.gpg 和既有 Ubuntu archive keyring 验签。

本机只读观测：C: Free **435146969088 bytes**（约405 GiB）；共享 Ubuntu ext4 Available **1017839951872 bytes**（逻辑空间，不把它另加到物理 C: 可用空间）。两份随包资源实际 `du -sb` 为 Python **477121651 bytes**、PostgreSQL **356507014 bytes**，合计 **833628665 bytes**（约795MiB）。不复制 Playwright browsers、node_modules、整个开发 venv、PGDATA 或日常 home。镜像压缩约399M，解包空间以镜像实测为准；初始预计数GiB，执行前后记录真实C:余量与VHD大小，不把逻辑最大VHD容量当实际占用，也不新增产品资源上限。

## 明确创建范围

候选唯一名称：`PulsaraG0-kernel6-20261025-<随机8位>`，先验证该名称不在 WSL 列表。唯一安装目录为本分支 output 下 `round6/disposable-distro/<同一名称>/instance`；镜像/清单留同父目录。创建前解析绝对路径并确认严格位于本次 workspace output。注册后读取对应 Lxss 的 DistributionName/BasePath/Version，只在 name、BasePath、WSL2 三者匹配时许可任何后续 terminate/unregister。

使用 [微软指定名称与安装位置的 import](https://learn.microsoft.com/en-us/windows/wsl/basic-commands#import-a-distribution)：`wsl --import <唯一名称> <精确安装目录> <已校验.wsl路径> --version 2`。不使用 install 默认发行版路径，不调用 set-default/set-default-version，不编辑 `.wslconfig` 或共享 Ubuntu 的 `/etc/wsl.conf`。每条后续指令必须显式 `-d <唯一名称> -u <本次用户>`。

首次只在新发行版内创建无密码的专用非root用户/0700研究home；若rootfs已含普通用户，选择独立新UID，不照搬用户settings。保留镜像 interop 设置。无需设置登录默认用户、启动systemd、安装GUI或添加provider配置。

## 最小资源与实际控制链

通过精确列出的 tar 项把共享 Ubuntu 的 `desktop/resources/python`、`desktop/resources/postgres` 只读打包，经 Windows 编排子进程 pipe 送入新发行版的 ext4 目录；只转移这两棵已随包资源，不 export 发行版、不复制 `/home/plumwsl`、`.pulsara`、settings、凭据、现有 PGDATA 或生产配置。源码探针可以从 `/mnt/c/.../round6` 只读加载，真正 runtime/home/PGDATA/socket 全在新发行版 ext4。固定 Windows Node22/Electron44 仍在宿主，不在新发行版装Node。

共享 Ubuntu 上实际 `ldd`：bundled Python 只列 glibc族 pthread/dl/util/m/rt/c，postgres 主程序只列 m/c/ld-linux。新rootfs内重新检查 `ldd`（包括实际需用的Python扩展和pg_search），只按实际 missing library 安装最小发行版依赖；不能仅凭主程序 ldd 宣称所有扩展可运行。先实际 bundled Python import/PGinit/start/kernelready+clean退出小探测，失败保留原样，不能用系统Python替换 bundled runtime。

现有R6研究owner与PG P5 helper的固定 Ubuntu/home路径应仅在新 output 适配中显式参数化（不改共享P5模块或生产），保留其实际协议与原 BundledPostgres/PythonRuntime/main Quit。若无法直接注入target，先给最小接缝差异，不手抄PG或kernel状态机。

确认目标独立后最小故障矩阵：start前 carrier/owner loss（不启PG），实际ready后仅 `wsl --terminate <唯一名称>`；宿主持有本次carrier/host exact HANDLE，记录 exit与协议是否干净结束，不能把distro中已失去的pidfd当可继续查询；重新启动同一临时发行版，只观察本次 kernel/PG锁和专用PGDATA恢复状态，按not-submitted/unknown/unclean分类，不重发已接受请求。需要补的不是普通pipe重测，而是整个发行版失联时原 owner 不得以缺child/载体结束推断 clean 或授权PGstop/删除。

## 清理范围

先正常释放本次门、join本次host/carrier；仅在仍可访问临时发行版时用本次pidfd/精确PG路径清理，保持失败原始记录。故障注入只允许 `wsl --terminate <已核对的唯一名称>`，禁止共享 Ubuntu 和 global shutdown。结束后再次核对 Lxss name/BasePath，确认唯一注册项匹配本次安装目录，再 [unregister该唯一发行版](https://learn.microsoft.com/en-us/windows/wsl/basic-commands#unregister-or-uninstall-a-linux-distribution)，并核它已从列表消失、共享Ubuntu仍运行/仍默认。unregister删除的全部数据只属于本次新实例。

最后只对已解析且严格位于本次 output 的安装/下载目录用原生 PowerShell `Remove-Item -LiteralPath` 清理；保留报告与必要raw事实。执行前后保存WSL列表/默认项、精确安装注册项、C:空间和清理结果。无全盘枚举删除、无跨shell字符串删除、无导出用户Ubuntu。

本文件提交root审阅；当前未import，不能把distro terminate或恢复记作已实测。
