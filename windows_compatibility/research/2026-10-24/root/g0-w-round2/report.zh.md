# Windows G0-W 第二轮：互操作取消与 PG helper 死亡

日期：2026-10-24启动，跨日至25收尾。受测生产源码：`fb2c57da12d24ddda32da1320db1d7c645b5ebb6`，Windows 与 Linux clone 一致。本轮用户明确授权三位 `gpt-6.1-sol / high` 并行研究和实验；保持已有 Ubuntu/plumwsl、interop、Windows PATH 和自动挂载。**未修改生产代码、未进入 Phase B；完整 G0-W 仍未通过。**

Windows 子进程的回收已经找到有实证的机制：无 breakaway 的 Windows Job，结合每任务独立 WSL interop 入口，可以覆盖普通 Windows 后代；进一步的私有 Unix socket 字节转发原型阻止了所测入口死亡后的跨范围回退。PG helper 异常死亡验证则发现真实反例，当前 CLOEXEC 单 helper 持锁方案不满足维护准入要求。

## Windows：什么已证明

平台沿用 Windows 11 25H2 build26200.9711、WSL2.7.11.0、Ubuntu26.04.1/plumwsl uid1000；源码、资源和 Linux 项目 Python3.12.15 沿用首轮。Windows 研究编排使用项目 uv 管理的 Python3.12.13，另有真实 Node22.23.3 与 Electron44.9.0 main 测试；不是新增产品依赖。

1. 先以 `CREATE_SUSPENDED` 创建准确的 `wsl.exe`，加入只设置 `KILL_ON_JOB_CLOSE`、不允许 breakaway 的 Job，再 Resume。Windows PE 不自行 enroll，仍实际属于该 Job，普通后代也继承。
2. 取消、实际 Job owner 强退、Node/Electron 外层宿主强退、Windows 父先退且子不持 pipe，均有精确 process HANDLE wait 和 Job 观察。取消路径还确认 ActiveProcesses=0；强退路径不偷留 Job handle，观察全部预持有成员 handle signaled 及 Job 对象消失。另有 A/B 隔离正控：取消 A 时 B 保持运行。
3. 不同 Linux 父链可以通过 `WSL_INTEROP` 指向专属 anchor，正常 Windows PE 进入该 anchor 的 Job。原 Linux Popen/PTY/PGID 仍可保留，因此不需要解析任意 shell 命令或改全局 binfmt。
4. **裸 socket 路径不够。** WSL2.7.11 在指定地址不存在时搜索父链的其他 interop socket；实测旧地址失效后 PE 跑到 A/B Job 之外。仅在 spawn 前检查地址或只注入环境变量不可采用。
5. `O_PATH` 可将上游绑定到真实 socket inode，在 anchor 原路径删除后仍连接原对象，连接失败而不回退。但将 `/proc/<owner>/fd/<fd>` 直接交任务，仍有 owner 死亡/FD 复用边界；不是最终候选。普通用户 socket hardlink 实验遇到 EXDEV/EPERM，未改权限或提权绕过。
6. 后续原型在0700 ext4私有目录建立任务专属 Unix socket，只使用标准 asyncio 双向 byte 转发到 O_PATH 固定的上游，不解析/复制 WSL 协议。任务使用这个 socket 路径；转发者死亡后路径保留，连接失败而不触发 ENOENT 回退。真实测试覆盖正常 A/B 路由、A取消后B仍活、anchor死亡后新请求失败、转发进程死亡后新请求失败，且未观察到回退 PE。
7. 最后补测 `normal-join-4a197629`：Windows父及Linux工作runner先退出，普通Windows子stdio均为DEVNULL。仅向anchor送EOF，保持Job handle打开，子仍运行并自然写出finished；随后Job ActiveProcesses=0、TotalTerminatedProcesses=0、全部预持有成员HANDLE wait=0，最后才关闭Job。全程未调用TerminateJob，补上“正常EOF后等待无pipe后代自然完成”的独立机制子项。它没有同时装配私有relay的封准入/退休或生产Linux manager。

上述最后一项是**有实证的 G0 候选**，不是已批准生产实现或整个 C 门通过。需要这个薄适配的具体原因是 WSL 原地址自动 unlink + ENOENT fallback；平台仍拥有进程创建、interop 和 stdio/control 的 vsock。Pulsara 候选只拥有每任务范围、启动准入、字节通道和关闭结算，不复制协议状态机，也不新增持久事件、receipt、任务表或重放。

首轮的普通 Windows 后代泄漏反例仍有效。Node22/libuv 自带的 Job 设置了 breakaway 选项，不能当作这里的范围保证；本轮针对不同 Job 配置取得正证，不是把首轮失败改写成成功。[锁定 libuv 实现](https://raw.githubusercontent.com/nodejs/node/v22.23.3/deps/uv/src/win/process.c)、[Windows Job 官方说明](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

## Windows：还不能宣称解决的范围

- 原型尚未接到真实 terminal/MCP/hook owner，生产没有每任务 anchor 或 Job adapter；Python ctypes 只是实验，不是 Windows 产品运行时。
- 正常完成必须区别于取消：封新准入但保留 socket、关闭 anchor 基础设施、等待普通 Windows 后代自然结束，再结合 Linux root/group/drain/worker join。不能以正常任务退出为由 TerminateJob 并称成功。独立anchor EOF/无pipe后代自然完成已补测；与私有relay封准入、原Linux owner组合的完整次序及反复并发spawn/取消竞态仍待整合验证。
- socket 在旧 Linux scope 真正结算前不可删除/复用，否则重现 fallback；连接已发送后丢回复保留 unknown，不能自动重发。清理路径本身不成为执行权限或恢复日志。
- 私有目录和 Job 是当前范围机制，不是对同 UID 恶意程序、显式改 WSL_INTEROP、WMI/服务/计划任务、任意逃逸或外部编辑器的完整安全沙箱。用户明确系统打开仍遵循原 OS 交接语义，不能扩大清理用户工作。
- 休眠/恢复、隔离故障发行版、真实应用正常 Quit 及原有 Linux PIPE/PTY、8槽、monitor、权限/副作用结算集成仍未补齐。不以本轮正证代替这些验收。

## PG：从待验证补成机制未通过

本轮直接调用真实随包 PG17.9、原生产 `acquire_kernel_lock`，没有第二 `startedHere` owner。故障 hook 仅将真实 pg_ctl/postgres 暂停在可重复阶段；随后杀唯一 helper，观察新有限 helper 自行取得同锁并调用真实 status，再恢复旧物理进程。保留无 hook 正控及注入器错误，未弱化生产断言。

最终在 `/home/plumwsl/.g0d2-*` 的0700 ext4目录完成7项主矩阵和3项补充：

| 边界 | 真实结果 | 判断 |
| --- | --- | --- |
| 正常 start/readback/stop；无hook正控 | 成功，正常退出后锁可取 | 基础正控 |
| pg_ctl fork前、postmaster readiness前、pg_ctl ready后未exit时 helper死亡 | 物理操作仍存在，锁已可得；其中status0也未必ready | 不能当结算 |
| pg_ctl fork子已setsid、尚未exec；另一个真实postgres已exec | 后续helper持同锁status3；恢复原物理进程后status0且真实psql查询成功 | 确定晚启动反例 |
| fd经pass_fds直接继承 | 常驻postmaster及后代保留kernel.lock；helper正常退出仍busy | 阻断kernel/维护，不是可用修复 |

两个 ext4 补充反例：helper1360死后，新helper1383持同锁status3，原fork子1364恢复成为postgres；helper1466死后，新helper1485持同锁status3，原postgres1470恢复。两例均用真实查询确认准确data/socket。具体PID是本次历史观察，不是后续可清理的授权。

因此，`ready/operation + CLOEXEC + helper持锁` 对所测普通 EOF/丢回复场景有价值，但**不能满足 helper 自身死亡时仍排除旧 start inflight 的要求**。一次 status3、wait-owner 成功或 carrier 结束不能授权 reset。增加一个同样可死亡的监督父进程也不能仅凭名称解决它。替代机制仍待设计/验证，本轮没有将另一套未验证方案接入生产。

存储审计还确认当前 `/tmp` 是 tmpfs：首轮及本轮早期 `/tmp/g0*` PG 证据只证明其相应行为，不是 ext4 资格。原日志保留，最终 ext4 轮次重新执行上述全部场景；WSL 仍不能替代原生 Ubuntu x64/arm64 或 GUI 资格。

## 证据与审阅

三路原始工作目录：`output/windows-g0/interop_jobs/round2`、`interop_review/round2`、`pg_death/round2`。归档报告分别见[Windows Job实验](../../terminal/interop-jobs-round2/report.zh.md)、[每任务路由与失败回退实验](../../terminal/interop-route-round2/report.zh.md)、[PG helper死亡实验](../../backend/pg-death-round2/report.zh.md)，并保留相应结果和小型探针；不归档第三方整份源码、二进制、venv、数据库或用户配置。

归档Python探针只做排版、按原顺序拆分import和局部lint注释；逐文件AST比较（仅将合并import规范为独立import）相等，记录见`archive-format-check.json`，原output文件未改。没有为这些归档变化重跑物理实验或声称新执行结果。复现须将各探针恢复到各报告指定的原output布局，Windows和Linux路径分别核对；归档目录不是新的运行入口。`archive-manifest.json`中的bytes为原始文件大小，不是内容身份或校验摘要。完整原始输出仍在output，归档只保留列出的材料。

root核对了现有Linux join、PG owner与kernel.lock代码，独立检查WSL fallback实现与Job/PG原始结果及最后正常join记录；Job代理交叉审查PG，PG代理交叉审查路由候选和主报告/spec状态。各方较早报告中“正常EOF/无pipe后代未测”的时点，已由最后的[独立补测](../../terminal/interop-jobs-round2/normal-join-report.zh.md)补充，不改写原日志。实证计数是各自场景，不能相加宣称完整G0通过。

本轮清理只涉及已记录的实验进程/目录。PG审计25个确切home均已删除且无对应数据argv/私有hook环境进程；Windows Job证据确认本轮成员已退出。Linux清理检查PID/starttime等事实，但不冒充所有Linux操作都用了pidfd原子身份保证。研究Electron留下自己的小型profile；上轮策略拒绝删除的BrowserOwner profile未重试或绕过。

下一步仍在G0：先验证候选的正常join、spawn/取消/关闭竞态与真实Linux owner边界；PG另需收敛能排除旧操作inflight的维护准入机制。原生Ubuntu资格、其他G0门及Phase B准入条件保持。
