# G0-C 最终只读审阅：一个明确的恢复缺项

2026-10-25。只读核当前spec §4/§9、实际runtime/main/desktop_runtime/application/KernelHost/tool_runtime关闭链，以及R1–R6原报告与选定原始矩阵；未新开实验、下载/import、改生产或spec。

**结论：本机选定环境的控制/跨域回收机制已有足够的G0可行性正证；我不建议此刻无条件勾完整C/整体G0，因为§9.3“整发行版外部终止后，下次启动由PG恢复且canonical数据可读”还缺一条真实重开正证。** 这是一个具体未运行的恢复路径，不是因为所有交错没穷举，也不是要求先做PhaseB/正式installer。若该单项通过，现有证据没有显示还须再扩大C矩阵才能授予本机选定矩阵的机制可行性结论；其他D/P/F/N门由root分别裁定，Windows产品支持/激活仍另论。

## 已足够的机制证据

| §4/§9要求 | 可采用的实际证据及限度 |
| --- | --- |
| 真实argv/三pipe/JSONL、UTF8/stderr/EOF/exit | R1锁定Node→WSL literal argv、逐字节中文、1MiB输入/2MiB stderr、exit0/1/7；R6实际bundled kernel的中文DSN内拆包、畸形/partial输出、真实中文启动/收尾故障、start前后EOF。不是仅echo。 |
| 普通Windows PE及无pipe后代的范围/失联 | R2 suspended→Job→resume、普通PE归属、实际Node/Electron/owner死亡、A/B；R2明确否定裸路径fallback。R3–R4收敛SCM_RIGHTS自开FD、私有socket保留、seal真实task/stream join和Windows exact HANDLE/Job事实。未把LinuxPGID或wsl close当Windows join。 |
| 真实启动预约/取消/rollback/完成与容量 | R4真实TerminalManager reservation内四绑定门、upstream前后/首块/首1字节取消、reader/watcher真实Thread.start故障、PIPE/PTY/输入/EOT、8槽和同registry A/B；原watcher唯一完成，live谓词与尾部取消实际验证。最终源码未整套重跑的阶段范围已明确。 |
| MCP/hooks/environment实际受影响owner | R5真实stdio SDK/supervisor/transport+credential guard、call cancel仍保session、session close才跨域join；hooks原executor/parser/16+8槽、timeout/cancel/hostclose与guard；environment原snapshot/close政策。均复用实际owner和同一物理scope候选，没有借terminal槽或复制parser。 |
| 原主链关闭与PG顺序 | R6真实bundle Application、原Runtime/BundledPostgres、AST原requestQuit；真实close门阻止PGstop，放门后原aclose/carrier0/锁free才PGstop。child已清空的原失败及两行candidate的3窗口均保留。实际可见BrowserWindow原close callback进入原requestQuit；不是正式installer资格。 |
| host死亡后的Linux原EOF/aclose | R6真实SIGHUP1对照；最小启动策略忽略HUP后，Node/Electron两项均原EOF→原aclose，门内kernel活/锁busy，放门后独立pidfd确认退出。信号sender PID未知，不能写成已证明WSL内部sender；整distro terminate仍unknown。 |
| 实际休眠/恢复 | root收到用户实际睡眠1–2分钟/已唤醒后才开inspect；同held Windows host身份、同kernel/PG PID+starttime/namespace、两锁及真实SQL/postmaster时间一致，随后原Quit干净结束。独立电源/网络传感器由races R7报告补充。 |

原产品缺口仍是生产要实现的接缝，不因研究通过而消失。G0评估的是候选机制在所选环境可行，不要求PhaseB代码已经shipping；不能用正式GUI/安装包尚未实现循环阻塞G0，也不能把output候选称作生产修复。

## SIGHUP安装前：安全边界足够，不追加交错穷举

当前真实desktop_runtime只在读到并校验start后取得kernel锁、准备DB与创建Application；研究HUP策略在运行`-m`入口前的启动接缝安装。若HUP先于该安装而使Python退出，guest尚不能处理start或接受工具工作。R1真实start前EOF、R6 start前host/carrier死亡和准备拒绝均没有Application.start/专用PGDATA；这支持无工作child的既有not-started回收边界。宿主已经写出但尚未获确认的start不能改报not-submitted，仍保unknown。

PG是独立前置owner，可能在kernel尚导入时已接受有限操作；“guest无工作”不等于“PG未启动”。该域由R3–R5实际ready→单operation+EOF、物理子命令持锁、父/helper死亡与not-submitted/unknown分类负责；不能让HUP回收或子close绕开PG锁去reset。R6保存attempt exit的candidate负责ready后异常不得误放PGstop。

因此，**不把“还没有专门在每条Python启动指令前打HUP门”列为本机G0阻塞**。条件是B的最小启动适配必须在任何start可处理前安装指定HUP策略，保留原SIGTERM/SIGINT/EOF所有权；不推广为忽略任意致命信号。如果后来选择另一个更晚的安装点，该新方案须单独证明，不能借用本轮结论。

## 活scope与原主链：可组合，不冒称同一次全链

已直接核当前原链：`DesktopApplication._close_owner` await sessions.aclose/core.shutdown；真实KernelHost.aclose关闭准入后await MCP close、hooks.aclose、tools.aclose；tools关闭最终await原terminal owner release/join。R4/R5内存接缝改变这些相同owner的物理settlement返回边界，没有另造完成发布者。R6真实application.close门及实际main顺序证明上层不会越过尚未返回的关闭owner去停PG。

R6窗口/hostloss case没有在同一kernel里运行带Windows尾部的活terminal/MCP/hook；不能写成已经实测“同时挂三类工作的一次整链”。但§4没有要求所有owner在同一run重演：下层真实owner集成、原上层await闭包和主链门足以支撑**机制可行性的组合结论**。没有发现需要再为同一scope全部重跑或添加新调度框架的机制缺口。PhaseB装配后仍应以真实受影响工作做比例合适的集成回归，这不是此次G0新增前置条件。

## 必须补的单项：未修改PGDATA的真实crash recovery/reopen

R6 `distro-loss-ae39b6d0` 只有：故障前PG live/双锁busy、exact carrier退出、显式重启后namespace不同/双锁free/无本次PG/旧PGDATA和pidfile保留、旧owner保持unknown。随后`distro_recovery.py remove-stale-pid-for-cleanup`手工unlink旧pidfile再移除home；这仅为避免在新namespace对复用PID误发stop的安全cleanup，**绝不是生产恢复算法或PG恢复成功**。R5正常stop/搬移重开和数据查询也不能代替dirty crash恢复。

最小补验方案（先交root决定，当前未重新下载/import）：

1. 再建一次唯一可丢弃distro，复用上次同一已校验官方镜像版本/最小bundle配方及name/BasePath/default guard；上次镜像/实例已清理，若无可复用官方本地镜像，需重新下载同一418495746bytes镜像并验官方签名/摘要。只做这一个恢复链，不重跑原矩阵。
2. 同一专用home由原BundledPostgres和实际bundle kernel ready；沿已有应用测试入口提交一条可读canonical记录，保存已提交返回/查询、PG system_identifier、PGDATA inode和实际控制状态。可复用既有生产prepare/verify与测试数据入口，不建产品receipt/恢复表。
3. 只terminate经guard核对的该临时distro，记录旧exact Windows carrier退出/UNKNOWN。保留故障前PGDATA、旧postmaster.pid、socket和锁文件，**恢复前不手工unlink、不reset、不initdb、不删重建**。
4. 新Windows壳显式重开同一distro/user/home，先原wait-owner，再由原BundledPostgres正常start现有目录，随后原kernel `prepare_database(initialize=False)`/Application.start。这是用户显式重开一个新attempt，不重发上一attempt接受的工具/写入；新attempt不能继承旧startedHere或结果。
5. 让PG自己处理脏数据与旧pidfile，保存真实PG日志/pg_controldata或恢复状态、SQL查询已提交canonical记录/当前schema、实际kernel ready。不能以锁free或pg_ctl status3替代成功。若原PG因为旧pidfile/新namespace PID冲突或其他原因拒绝，原错误/数据保持，记录具体失败；不手工修文件后把本项算通过。
6. 成功后原Quit完成kernel clean exit→PGstop/双锁free，再精确清理home与唯一注册项；失败时另标研究cleanup，并精确注销自己的distro。均不终止共享Ubuntu或改变默认/TUN/interop。

验收仅需：真实dirty状态经PG自己恢复、同PGDATA/system_identifier和已提交canonical记录仍可读、实际新kernel ready、原正常关闭顺序。无需重新跑BM25全套、所有owner/取消矩阵或正式installer，也不通过创建新data来“恢复”。

在这条补验之前，可以接受C的控制/回收机制子项；§9.3恢复跨C/P子项仍记具体待验，不无条件宣布整个Windows G0通过。
