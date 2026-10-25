# Phase B 后半段：本机正式候选与剩余资格

2026-10-25，`codex/windows-g0`，共享源码基线 `0ea98e63d90043773f42bd9bb9bbb56951fca8b4`，本地安装候选 alpha.10。

本轮先完成 [R0–R5 Windows 共享修复最小重验](../../windows-shared-recheck/root/report.zh.md)，再补原 Phase B 验收清单中的故障与实机边界。**B1–B4 核心生产接入已落地；本报告是后半段验收收口，不是新一轮 G0，也不是 B5 激活声明。** 这批没有新增产品逻辑修改；版本同步与 Windows mise 命令路径修正来自前一批新基线，不需要仅为新增证据构建 alpha.11。

## 已完成的正式补验

| 项目 | 本轮新事实 | 原证据 |
| --- | --- | --- |
| 共享差分/最终安装来源 | R0–R5 全过，两个 clone 同 0ea98e63；alpha.10 实际包三条共享文件与 WSL 源码 bytes 一致；98 项源码、10 项真实 PE、Windows宿主/startup、随包 context 8 次真实调用 | windows-shared-recheck 四方报告 |
| 更新与正式 GUI | 原模型、两会话/5 turns/32 entries、工作区文件和 PG 身份保持；新中文目录/会话/terminal、Ctrl+N/K、隐藏草稿、正常 Quit/冷重开通过 | windows-shared-recheck/root、backend |
| PG helper 失联 | 5 项：提交前取消不认领别人的启动；start 丢 result 但子进程持锁；PG 已停而有限 stop 尾部仍持 kernel 锁；initdb 双 FD 继承；真实 readiness 非零与 unknown 严格区分。全部原 owner/client/installed helper，私有 PG 精确清理 | [backend](../backend/report.zh.md) |
| interop/原任务 owner | anchor pin/send 前和 received-FD/bind 前取消；两个 Windows 观察者同时丢失保持 unjoined/原槽位；MCP pending call 与 hook helper 失联沿原 owner 报错并结算，5 项通过 | [terminal](../terminal/report.zh.md) |
| installed main 活动退出 | 正常 Quit：原 Application 物理结算任务/线程/scope 后 kernel 正常退出、原 PG stop、exe0；宿主强退：原 EOF/aclose 进入后保持 unknown，runtime 非零，故障前持有的3个 Windows member HANDLE及kernel pidfd均确认退出，PG保留，维护拒入。后者是负向合同通过，绝非 clean | [terminal](../terminal/report.zh.md) |
| setup/网络/错误保数据 | setup8、生产 BackendSession/HTTP/import 组合3、ENOSPC 分配点有限注入1、原 recipe fixture7 通过；stage/锁/原配置与数据边界保留，原失败不覆盖 | [desktop](../desktop/report.zh.md) |
| 真实活动进程睡眠 | 原 GUI→保存模型→一次 PIPE/Windows等待输入任务；最后132.121秒睡眠后同main/kernel/PG，writer1→2，原精确HANDLE退出、草稿保留；第二真实消息唯一消费，无工具重放，用户确认正常Quit后PG停止/维护0 | [active-sleep](active-sleep/report.zh.md) |
| Explorer 新建后刷新 | 原 Explorer Ctrl+Shift+N 创建中文空格目录；产品 picker 的原旧列表刷新后出现新目录，选定结果为同一 Linux canonical 路径；inode265523/uid1000/ext4 独立核对，取消新会话后正常 Quit，app0/PGstatus3/kernel锁空闲 | [desktop](../desktop/report.zh.md) |

**本轮选定的 Phase B 后半段本机验收已完成。** 所有原失败、故障注入层次和清理边界保留；没有待修复的本轮已识别生产缺陷。它不替代下面明确列出的环境/发布资格，不能写成完整 B5 激活。

installed main 实验使用原 exe/asar/main/Runtime/PG，隔离 userData/home，并仅通过测试入口在原 Application/Core/TerminalManager 启动活动任务；不冒称它经过公开 GUI/provider 工具入口。真实公开入口由本轮 GUI terminal 与活动睡眠另行验证。宿主强退后的外部精确 reset 是清理本次私有 PG，不是产品自动恢复或旧工具重放。

## 验收边界

新网络证据使用实际 production BackendSession、LocalHttpServer、RendererBridge、LocalImport 与 native Electron 对象，sessions/Core 和受控 streams 是测试依赖。它证明这些 owner 的组合；不称一条正式 main run 中全部流量类型同时在途。原 §11–13 没有要求那个额外笛卡尔矩阵，不将它新增为硬门。

ENOSPC 是仅限本次有限 installer 的 payload open 分配点返回 errno28，原 shell 返回2、错误原样传播，stage清理/无发布/保数据已核；它证明实际生产路径的该错误处理，不称整块物理磁盘填满。没有为测试填满或重建用户日常文件系统。

活动睡眠仅覆盖一条真实 PIPE/Windows任务；原进程由 writer失效收尾，不是将普通网络断连当用户取消。原unknown、关闭失败屏障及冷开边界保留，不要求旧 process-local 任务复活或重放。原先 alpha.9 闲置睡眠证据继续独立保留。

## 明确延期与未激活项

- **WSL未安装／需重启真实环境**：用户没有备用 Windows/VM，并明确不要为此删除当前 Ubuntu。保留延期，不以 mock 记通过。
- **同一原窗口混合 DPI 往返**：用户正在出差，只有笔记本内屏；既有 100%→150% 证据保留，完整往返待可用外接屏。物理右 Alt 也不等于实际 AltGr 布局资格。
- **原生 Ubuntu x64/arm64 与 GUI**：按用户要求延期，WSL不替代。Mac arm64 本轮选定源码/实际包/provider/GUI矩阵已通过，不再误写未执行；其他硬件/分发资格仍按原矩阵。
- **统一发布制品集合及支持激活**：本轮只构建和本机安装 Windows alpha.10，没有新的四制品同版本完整发布矩阵、tag、制品上传或公开发布。Windows支持/B5尚未激活；文档和局部通过不能代替缺失真实资格。

用户已要求尽快推进后半段。本轮收口只处理原矩阵必需项和发现的具体问题，不继续扩展额外研究矩阵。当前代码、已执行资格、用户延期与公开发布是分别报告的状态。

## 收尾

正式应用最后于23:04:34核对进程0，正式PG status3、kernel锁空闲、diagnostics socket不存在。私有PG、finite mutator、interop scopes、测试Windows壳、网络服务均由各分工按准确身份结算；unknown门的外部清理单列，不追认clean。保存的用户模型与原会话/工作区保留；本轮GUI验收会话及Explorer空目录保留供复核，没有删除日常Ubuntu、改变interop或Hiddify TUN。

四方交叉核对见 [review.zh.md](review.zh.md)。Windows/WSL 两份仓库记录同一 Git 基线和本轮改动；原 Git 提交身份由 Git 管理，不生成源码/证据 SHA。未合并 main、打 tag、上传制品或公开发布。
