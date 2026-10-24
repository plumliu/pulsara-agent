# 第四轮：首次 FD 绑定、取消与真实 TerminalManager 合成

2026-10-25，interop_tasks_r3。Windows/Linux clone HEAD 均为 `adb3e1dec382b5531eface90b76d4968ec21c0b8`。已读 AGENTS、当前 Windows spec §5/19、第三轮 root/terminal/竞态报告和真实 manager/environment/测试入口。仅写本 output 目录；未改生产、spec、依赖、settings，未提交，interop 保持，未进入 Phase B。采用 ponytail 控制最小接缝，标准 relay fixture 由竞态分支拥有，双方没有互改目录。

**首次 SCM_RIGHTS 绑定已接进真实 foreground reservation，确定性绑定/转发取消、启动回滚、PIPE/PTY、8 槽和同 owner A/B 获得有限合成正证。实验同时发现并修复两处 relay 结算缺陷，并发现真实 manager 除取消/join 外还需要扩展 live 谓词，避免过早冻结 success。完整 G0-C 仍未通过。**

## 单一组合与边界

Win32 继续拥有不可继承、无 breakaway、KILL_ON_JOB_CLOSE Job，以及 CREATE_SUSPENDED→Assign→Resume 的 anchor 创建和精确 HANDLE 等待。WSL 继续拥有原生 PE 创建、stdio/control。真实 Linux TerminalManager 继续拥有 shell、PIPE/PTY、root/PGID、reader/watcher、输入、输出、结果与原 8 槽；实验没有复制这些 owner。

竞态分支的 `output/windows-g0/interop_races_r3/round4/relay_fixture.py` 使用标准 socket/asyncio/TaskGroup。anchor 在自身生命周期打开实际 WSL_INTEROP 的 O_PATH FD，经 SCM_RIGHTS 传给 receiver；receiver 先持 peer pidfd，再核 SO_PEERCRED/PID/starttime 与本次 anchor 记录，完成 bind。此后先经过一条仅诊断的 PE 连接，Windows owner 持 HANDLE 核精确 creation、Job membership 和仍 live；确认后才允许 operation-admit 和真实模型命令。没有再由 receiver 重开上游字符串路径。

socket 在 `/home/plumwsl/.g0t4-*`，ext4、本次目录 0700；应用 cwd 始终 Linux clone。小型研究源码/JSONL 经 `/mnt/c` 读取和记录。Windows Python 为项目 `.venv` 3.12.13，Linux `.venv` 为 3.12.15；环境来源见 environment.json。没有使用 Node/Electron、provider、PG 或 saved settings。

本目录内存适配的必要接缝为：

1. 真实 registry 已取得 launch reservation、decision=PREPARING 后，在实例 `_spawn` 内准备跨域 scope。局部 decision→scope 让真实 abort/release_owner 可取消尚未产生 task Popen 的准备；scope 先 join，再由原 exec 异常路径释放 reservation。没有在 registry lock 内等待。
2. `_spawn` 返回的真实 state 在原 `_install_spawned_process` 启动 reader/watcher 之前关联 scope。reader.join 先等原 reader 和 Linux group，再 normal seal、anchor EOF、Windows Job/所有观察 HANDLE join；原 watcher 保持唯一正常 finalize/完成发布者。
3. 原终止调用前先 cancel seal：封准入、abort/gather 所有已登记连接与 binder、确认 taskCount=writerCount=0，然后 Windows Job cancel 与原 Linux PGID 取消；组合 join 覆盖两域。`_join_physical` 的研究入口另合并 scope，覆盖 reader 未 start 的 rollback，未依赖根本不会调用的 reader.join。
4. 新发现要求扩展 `_process_is_live(state)`：本次 scope 未 Windows join 时仍 live。原 `_status`、`_accept_termination_intent`、freeze、watcher 和容量公式保持；没有另造结果 reducer。
5. normal seal 关闭 listener、保留 pathname，等已接受连接自然结算；正常不调用 TerminateJob。路径退休必须发生在原 release_owner_and_join 返回、Linux group/reader/watcher 和 reservation 已收回之后。诊断和 ACK 后 late caller 各自的 Linux PGID 也在 scope 初始化 owner 内确认空。

上述 command 匹配、局部 state/decision map、文件 RPC、内存 monkeypatch、ctypes 编排都是研究装配，不是生产接口或依赖。没有增加 durable registry/receipt/event、产品总量或寿命 cap；6 秒 child、25/60/70 秒边界均为有限研究负载/诊断观察。

## 选定证据：16 个逻辑场景，分版本

唯一归档矩阵 `final-matrix.json` 保留每例 sourceResult 和 implementationStage，`verify_results.py` 执行记录断言。**不是最终源码重新跑完整矩阵。**

| 实际阶段 | 选定场景 | 结果 |
| --- | --- | --- |
| relay 两次结算修复前，12 条成功记录 | pin 前、pin 后、bind 前、bind 后 4 门；upstream 打开前后 2 门；首次转发前/首块已转发 2 门；reader.start 故障；PIPE/PTY 输入；同 registry 同 owner A/B | 准备门确在真实 PREPARING/reservation=1 内。pin 后门使用真实 release_owner_and_join，其余准备门用真实 abort；均在 scope join 后释放 reservation。转发取消经过真实 kill；原 reader 未启动 rollback 已合并 scope。输入 ACK/EOF、PTY EOT 和 A 取消/B 保持槽位与精确 Windows live HANDLE 有证据。 |
| 冷任务 writer 修复后，2 条限定复验 | watcher-start-2d54c333、half-forwarded-888767a0 | watcher.start 真抛错，reader 已 start/watcher 未 start，Windows scope 在原 `_join_physical` 前结算，原 rollback 收回 state/reservation。首 1 字节门 operationForwardedBytes=1；ACK 后放门、旧 socket 新尝试均无新 PE、字节计数不变。 |
| done-task bookkeeping 和 live 谓词修复后，2 条限定验证 | normal-pty-b1134615、tail-cancel-c584858c | normal 的 Linux root/reader/group 已退但 Windows child 仍活时，fresh poll=running、completion=false、liveCount=1；7 个新任务 running，第 8 个 blocked；Windows child 自然 finished、Job 空及全部所持 HANDLE signaled 后 success，normal 无 TerminateJob。tail case 同样先确认 root/PGID 已退、Windows child live，此时 poll 仍 running；随后真实 kill 最终 killed、完成且 liveCount=0。 |

每条选定记录均核：binding-complete ≤ host exact Job 诊断核验 ≤ operation-admit ≤ 模型 PE；seal ACK 的 task/writer 为零且之后无 bytes-forwarded；seal ≤ Windows EOF/terminate ≤ 全观察 HANDLE signaled/ActiveProcesses0；原 Linux owner 收回全部线程/组/launch reservation ≤ socket 退休。准备取消未发布模型路由时没有 task PE。原 watcher 的正常 subscriber 每个 process ID 至多一次；无 watcher 的启动回滚只走原 join 的完成支路。

首块/部分请求已转发后的取消保留执行 unknown；本轮 after-forwarded 没看到模型 PE，也不能据此宣称未执行。该门是 opaque stream 的首块转发门，没有解析/证明完整 WSL 消息或虚构 reply gate。normal 的真实 root 退出/stdio 结果与取消请求 unknown 分开判断，relay ACK 本身不提供操作成功语义。

竞态分支只读审查了同一 16 例的次序及最新状态 seam，未另跑脱离 manager 的 WSL 矩阵。其标准 Unix fixture 自检与两个确定性负例见该分支报告，不能代替本表的真实任务结果。

## 三个实际缺陷与保留的失败

**live 谓词缺口。** `_status` 和 `_accept_termination_intent` 只看原 `_process_is_live` 的 root/PGID。仅延迟 watcher 的 physical_completion 虽能留住槽位，却不能防止 `_status` 冻结 success，进而拒绝后续取消 intent。normal-pty-06ea3e18 在 wait 诊断窗口后真实返回 success，同时 completion=false/liveCount=1；原记录保留。最新 normal/tail 两条验证新增 live seam；不能把此前未 poll 的成功子项扩大成它们已测过该性质。

**冷 task writer。** watcher-start-8a547091 的 receiver 未给 seal ACK、owner 超时。竞态分支确定性重现：connected 同步登记 writer 后，connection coroutine 尚未首次运行就被 cancel，子 finally 不会执行。scope seal owner 必须直接 abort/wait_closed 这些仍归自己拥有的 writer，不能依赖 child finally。此例没留 receiver stderr，不能单凭日志把它确认为同一 assert 根因；修前事实是 receiver 消失、ACK 缺失和 owner 联动失败。其 Windows/manager/receiver及私有路径已精确清理，failed-watcher-cleanup.json 保留。

**已 done task 的 bookkeeping。** 冷 writer 修后，normal-pty-06ea3e18 再遇 seal assert，receiver stderr 已落文件。normal-pty-588862f5 的新增失败诊断明确 `tasks=[done:true,cancelled:false], writers=[]`：CPython gather(已 done task) 可能不让出执行，排队的 discard callback 尚未运行。竞态分支用确定性自检复现后，改为 owner 捕获任务、gather、逐一 assert done、只移除这批已结算 task；末尾零 task/writer 断言保留，没有加 sleep 或清掉活任务。随后单 normal 验证通过。两个失败 scope 最后按 exact pidfd 终止本次故障 fixture、Job 精确 join、再 unlink 自有目录；这是失败清理，不是正常成功。

另外两项研究工具错误保留：normal-pty-0dac9d8d 的日志 pid 字段冲突，中断 host 编排；修正后由原确切 Windows HANDLE 已死证据恢复本次 join 通知，让该次 manager 收尾，first-probe-recovery-final.json 证明收回。normal-pty-0f801224 的并发 manager append 在 DrvFs 损坏一行，raw 未修写；统一进程内 LOG_LOCK 后后续记录可逐行解析。两例都不入选定成功矩阵。

## 清理的准确结论

本目录所有成功及失败记录共审计 **254 个 Windows 历史 PID+creation 身份、73 个 Linux PID+starttime 身份、25 个精确私有目录**。原失败、Hung receiver 和 manager 的清理只针对已核身份的 pidfd/Windows HANDLE 或自有 Job；无发行版停止、无泛杀、无配置/凭据操作。Linux 当前本次活身份、精确私有目录为空；成功记录退休前断言 task/诊断 PGID 及 reader/watcher 收回，失败正常尾部也已观察其 Linux group 先为空。

最终 fresh Windows 查询有一个明确观测限制：PID33468 当前 OpenProcess 返回 ERROR_ACCESS_DENIED=5，不能写成当前 PID 不存在或已复用，也未操作它。原严格回查失败 JSON 保留为 cleanup-current-query-failure.json。该 PID 对应本次 diagnostic creation=134373340069445234；当时 host 核对后持续持有的同一 HANDLE 在 windows-scope-joined 明确 wait=0。故**本次原精确身份已结算，remainingExactOwned=[]；currentPidUnobservable=[33468/error5] 仍保留**。cleanup-final.json、settle_cleanup_evidence.py 把两类证据对象明确分开，没有用查询失败假定死亡。

## 未测边界

本轮没有穷举 prepare 的最后取消检查与原 Popen 之间的每种交错、所有 native API/reader 故障、取消 normal ACK 升级的全部时序、宿主崩溃/GUI Quit/睡眠恢复、真实 MCP/hooks/环境 snapshot probe、provider 长程或任意主动逃逸/外部 broker。最终代码对 normal ACK 被 cancel 升级的分流已静态收敛，没有因此再宣称全交错矩阵完成。fixture 的两个缺陷和 late status 冻结证明这些边界必须分别验，不能由标准 Unix 单测或一组绿色场景代替。

本轮没有重跑第三轮 47 个原 terminal tests；生产代码未改，验证集中在上述真实 owner 内存组合与记录断言。Ruff/格式化通过。完整 G0-W-C、生产采用与 Phase B 仍未获得放行。
