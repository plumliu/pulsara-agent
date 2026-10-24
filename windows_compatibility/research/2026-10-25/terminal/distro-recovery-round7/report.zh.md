# 第七轮：整发行版终止后的原样 PG crash recovery

2026-10-25。root审阅R6后确认唯一必补项：§9.3外部终止后，不能以新namespace/锁free/手工清理pidfile代替PG实际恢复。本轮只补 **一条完整恢复链 `recovery-dee640cd`，通过**；此前R6证据和失败保持原样。生产/spec/共享PG helper未改，未进入B、未激活Windows支持。

这条正证合并R1–R6控制/跨域回收、真实owner、主链顺序、HUP/窗口/人工睡眠后，**支持本机选定Windows11/WSL2.7.11/Ubuntu26.04.1普通用户矩阵的C/P机制可行性通过**。它不是全平台/所有交错或生产shipping资格；正式主壳/安装器/真实provider长程仍按独立阶段验收，不以未做B循环阻塞G0。

## 一次实际链，两个显式attempt

新唯一发行版 `PulsaraG0-kernel7-20261025-e4e95020`，注册ID `{c6987db1-b068-4097-a76c-58e0a29b6de4}`。create/terminate/unregister前均核唯一name、output内精确BasePath、Version2，原default/Ubuntu注册项不变。重新下载同一Canonical官方26.04.1 WSL镜像418495746bytes，SHA256匹配官方清单且Ubuntu CD签名gpgv通过；只读传入833628665bytes随包Python/PG资源。未复制日常home/settings/凭据或整发行版，未安装provider。

Windows固定Node22.23.3装配原Runtime/BundledPostgres及AST原requestQuit，Linux实际运行随包Python3.12.15的desktop_runtime/Application。P5有限JSONL helper/client只在本目录机械复制、固定新的distro与bundle解释器；共享版本不变。`recovery_build.py`只加测试门、原wait-owner调用和初始化布尔值，没有重写PG恢复、kernel parser或生命周期owner。

1. 第一attempt原wait-owner→原TS PG.start→实际kernel ready（首次initialize=True）。通过真实HTTP `POST /api/sessions {workspace_kind:quick}` 返回201，调用原LocalSessionController/Core/事务owner；不传model binding、不调provider。原GET成功，精确SQL读到 `pulsara_v3.sessions` JOIN `workspaces` 同一canonical行。PG agent只读核实原事务commit在201之前。fsync/synchronous_commit均on，pg_controldata为in production，双锁busy。
2. 仅对核验后的该唯一distro terminate；故障前持有同一creation的Windows carrier HANDLE随后wait0。旧attempt仅记录UNKNOWN、PGstop0，不重发POST/start或把research host退出0称应用clean。
3. 显式新namespace观察：**旧postmaster.pid完整字节相同，PGDATA inode相同，control仍in production，双锁free**。没有unlink/改pidfile、删除socket、reset、initdb、换新data或人工恢复文件。
4. 新Windows显式attempt重新使用同distro/user/home，先原`runtime.waitForPreviousOwner`，然后原TS `BundledPostgres.start`已有目录；实际helper方法无initdb/removeDataDirectory。原start帧明确 **initialize=False**，PG自己处理原脏目录和旧文件，实际kernel再次ready。
5. 新增PG日志含 `database system was interrupted`、`automatic recovery in progress`、redo与`ready to accept connections`。system_identifier、PGDATA inode与故障前一致；相同session ID原GET200，原canonical SQL行逐值一致。没有自动恢复旧live session/模型工具或重放上一attempt已提交写入；这里只冷读canonical事实。
6. 原requestQuit→实际Application.close返回→carrier正常0/本次kernel已退和锁free→PGstop1→原main appExit0请求。双锁free/无本次PG后精确移除home；无forced清理升级。

完整原始数据、HTTP结果、SQL行、control/PG日志、两attempt的真实控制写入/owner/进程记录在 `final-result.json` 一份，`verify_final.py`只读重验，无需重复归档每条stdout。

## 为什么这次关闭了R6明确缺口

R6只恢复namespace观察、随后手工unlink旧pidfile以安全cleanup；其报告已准确否定这是生产恢复。本轮直到PG自行恢复并查询通过之前保持旧文件原样，原PG资源/TS owner/实际kernel直接工作。因此它关闭的是§9.3真实dirty恢复/可读状态子项；不是用另一个空库或正常stop重开代替，也不是由OS锁free推断数据库正确。

不同namespace的旧PID不会拿到共享Ubuntu里比较/发信号。新namespace查询不是旧pidfd的连续证据；历史载体完成由故障前持续持有的Windows HANDLE证明，数据恢复由PG日志/身份/真实canonical查询证明。单个成功case不承诺所有stale-PID碰撞都可无条件恢复；原PG拒绝时仍须保错误/数据，不添加猜PID清理或新恢复日志。

## 精确清理与原清理探针失败

两Windows host的原Popen wait均exit0，最终PID+creation回查不存在；本次home在unregister前已确认删除、PG/kernel双锁free/无本次进程。注销前再次核name/BasePath/version，只有新唯一注册项被unregister；镜像、VHD和本次空目录已删除，官方摘要/签名保留为小证据。

复用R6 `distro_finish.py` 的末尾“共享Ubuntu必须仍Running”断言失败，原错误保留于 `cleanup-listing-failure.json`。实际该时点WSL列表为 **Ubuntu Stopped**。不能猜自动停原因，不能倒填后来root为Git同步而启动后的Running，也没有为求绿启动Ubuntu。

该断言在本次唯一注册项注销、VHD/镜像删除完成之后才失败，不改变恢复正证。随后只读 `audit-retired` 核原注册/default逐值不变、本次名称/目录消失；记录全部management命令没有共享Ubuntu terminate/shutdown/unregister，明确破坏性命令仅本次唯一名称。`verification-cleanup.json`记录两host/home/注册项已收回、current unknown空，并保留实际Stopped列表。它证明没有主动终止共享Ubuntu，**不声称其运行状态始终不变**。未改interop/TUN/default/settings。

PG agent已只读审本条probe/build/facts和实际结果，确认原canonical commit、原样dirty恢复、新attempt无initdb/reset、原Quit顺序，无新增实质缺口；未运行第二套实验。R6只读C门审阅中的唯一待验项由本轮关闭，其他范围和原生产缺口不改写。
