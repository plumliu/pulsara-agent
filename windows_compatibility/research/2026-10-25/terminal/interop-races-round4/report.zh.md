# 第四轮：共享 fd relay、确定性取消门与真实 manager 独立审查

2026-10-25，interop_races_r3。受测基线 `adb3e1dec382b5531eface90b76d4968ec21c0b8`，已读当前 AGENTS、spec §5/19及第三轮证据。本分支只写自身 round4；真实 `TerminalManager/ProcessRegistry` 组合由 interop_tasks_r3 分支执行。本分支未改生产、对方目录、spec、依赖或 settings，未提交或进入 Phase B。

**首次 SCM_RIGHTS 绑定与同一真实 manager 的准备/取消/退休已形成可审查的有限组合证据。旧 sealed检查→await upstream 窗口不再由一次布尔检查兜底，而由 owner 取消并实际 gather 所有接纳任务后才 ACK。组合过程中发现两条真实 SDK 调度边界，均保留负例并作最小所有权修正。完整 G0-W-C 仍未通过。**

## 一份共享小模块及边界

`relay_fixture.py` 同时供真实 manager 套件和本分支小型标准 API 自检使用，没有另造独立 WSL 资格矩阵。CLI为 `anchor CONFIG_JSON` / `receiver CONFIG_JSON`；config仅有本次私有directory、record、anchor_record、prefix及可选故障门，额外字段由真实套件持有。文件控制仅为研究fixture RPC，不是产品协议、持久状态或receipt。

Windows Job/HANDLE仍由 WindowsScope 拥有，WSL拥有原PE/vsock/stdio机制，Linux原 manager继续拥有真实shell/PTY/PGID、reader/watcher/原完成发布与容量。模块只复用标准SCM_RIGHTS、SO_PEERCRED、pidfd和asyncio streams/TaskGroup；不解析WSL消息、不接管终端、不实现重放或新调度器。

anchor在 Job 下亲自打开自己的实际 WSL_INTEROP 为 O_PATH，AF_UNIX一次传真实fd；receiver先持peer pidfd，再核SO_PEERCRED的PID/UID与本次host可信anchor记录的starttime，接收fd后才bind独立0700路径。数据面只使用receiver自持fd，不按旧路径重开。inode仅作诊断。`binding-complete`在after-bind门之后；真实scope必须等它、跑诊断PE、由Windows owner用精确creation HANDLE核真实Job归属，才写operation-admit并允许模型命令。

数据门只在连接accept时已经存在operation-admit时触发，因此不误挡绑定诊断。门包括 before/after-upstream、before-forward、half-forwarded、after-forwarded；anchor/bind门保持原四个准备阶段。所有写过上游的请求失联仍unknown；转发计数不证明WSL已经执行，也不授权重发。

## seal 的真实结算边界

- `connected`是同步callback，任何首次await前先把连接task和writer登记到本次owner。
- cancel先设置cancelled/sealed并关闭listener，再取消绑定任务和已接纳连接；TaskGroup拥有两个pump，transport.abort丢弃尚未发送的缓冲。owner等待binder/连接task、writer.wait_closed、Server.wait_closed全部完成，断言task/writer归零，才发布mode=cancel的sealed.json。
- 正常seal关闭listener但不取消合法已接纳连接；等其自然settle及stream关闭后才正常ACK。正常等待可升级取消；mode由真实最终状态提供，consumer重新核对。
- SDK连接的await中发生取消时，stdlib负责关闭其自有socket/transport；owner的task gather等待这个取消返回。bind使用start_serving=False先保存Server，再await start_serving，避免bind让出时Server尚未归owner槽。
- ACK之后再释放旧门或重用旧调用者路径，不能再发上游字节；私有filesystem socket保留到原Linux owner/相关诊断PGID和Windows scope完成join后，只有明确retire才unlink/rmdir。

已直接读取本机Linux Python3.12.15实际asyncio实现：AF_UNIX create_unix_server(start_serving=False)至return无await；create_unix_connection/_create_connection_transport在取消时关闭SDK owned socket/transport；Server.wait_closed等待停止监听及已接纳transport detach。没有复制其实现。SDK说明只支持小接缝设计，实际成功/失败见下述同模块证据。

## 两个 SDK 调度负例，保留断言

1. **Task在协程第一步之前被取消。** 真实watcher.start故障很快触发取消；同步登记了writer，但connection协程尚未进入try/finally，原依赖child finally释放writer的方法漏收。`unstarted_connection_check.py`明确在同步callback登记后立即取消其task，`unstarted-run-1.log`确定性得到task0/writer1与原末尾AssertionError。修正是seal owner对仍登记的精确writer abort→await wait_closed→再移出集合。child正常finally也在wait_closed事实后移除，归零断言未改。`unstarted-run-3.log`通过；真实watcher-start-2d54c333与half-forwarded-888767a0限定复验通过。
2. **Task已经done，但移出集合的done callback仍排队。** 真实normal-pty-588862f5的seal-unsettled快照为tasks=[done:true,cancelled:false]、writers=[]。`done_callback_check.py`用已完成Task和仍排队discard，确定性复制CPython3.12 gather(done)可已完成、await不让出而误assert；`done-callback-run-1.log`保留负例。最终owner保存owned_tasks快照→await gather→逐个assert done→difference_update这些确切已结算task，不等通知callback当结算authority。没有sleep、泛clear或弱化断言。run-2通过；真实最终normal-pty-b1134615通过。

normal-pty-06ea3e18较早也触发同一末尾assert，但没有task/writer详细快照；不能把它逐字认作第二条根因，只能作为相关失败保留。实际详细stderr和真实manager原始记录由tasks分支归档。本分支保留两个更小确定性标准API反例以解释所有权缺口。

## 同一真实 manager 套件的独立只读审核

`audit_integration.py`只读tasks分支结构化记录；`integration-review-results.json`及`integration-review-final.log`最终选择**16个逻辑场景**，没有把重复半请求/旧normal累加充数。

| 组合场景 | 实际证据 |
| --- | --- |
| 原reservation内准备取消四门 | anchor-before-pin-ef604073、anchor-after-pin-bea3d2b7、before-bind-77e26c6b、after-bind-82333dab |
| 原manager下upstream/字节取消门 | before-upstream-d6c33081、after-upstream-bda47393、before-forward-82fd44ff、half-forwarded-888767a0、after-forwarded-3fb9894f |
| 真实Thread.start故障 | reader-start-a97f6810、watcher-start-2d54c333 |
| PIPE/PTY输入及同registry A/B | input-pipe-79a6d726、input-pty-2e57a2bf、shared-ab-100da06a |
| 最新正常完成/Windows尾部取消 | normal-pty-b1134615、tail-cancel-c584858c |

独立断言涵盖：

- scope.prepare在原launch reservation内；binding-complete≤host精确Job/HANDLE诊断核验≤operation-admit≤实际模型PE启动。scope reader.join接缝在原_install/_start_physical_threads之前安装，未误读为watcher已先启动。
- 每个seal ACK为taskCount=writerCount=0；ACK以后该scope日志没有新的bytes-forwarded，operation计数稳定；cancel ACK先于Windows before-job-cancel，normal ACK先于anchor正常EOF。
- Windows完成为ActiveProcesses0、members=[]、已持全部成员HANDLE wait0；原Linux owner返回时root/group/reader/watcher完成、completion为真、launch reservation和decision均空。
- 原owner release/join返回之后才retire，已公布的socket到退休授权时仍存在；诊断PGID另核空。reader从未start的rollback仍先收Windows，再由原_join_physical关闭PIPE/PTY、等待原group并发布原完成。
- 最新 `_process_is_live` 内存接缝是“Windows scope尚未joined OR 原Linux存活”，不是修改输出字典。最新normal/tail-cancel在Linux root返回0、group空而Windows尾部仍活时fresh poll仍running、completion=false且槽位占用；后者真正kill最终killed并双域join，前者自然success且未TerminateJob。

最后这条补上只延迟physical_completion、却可能提前冻结success/忽略尾部取消的状态接缝。它只在最后两项用最新源码实测，不能反向宣称所有旧case都用最终状态接缝跑过。

最终证据分层为：12条修前成功场景，冷writer补收修后watcher/half两条，done-task结算及physical-live接缝修后normal/tail-cancel两条。没有整套最终源码矩阵通过的说法。前后修正触达的是显式owner结算和存活谓词；既有有限结果保留其当时证明的行为，新增故障只做与变更相称的复验。

## 标准 API 自检及清理

`selfcheck.py`不是WSL资格：使用真实AF_UNIX/SCM_RIGHTS但上游只是本次标准Unix stream，不是WSL。11项覆盖before/after-upstream、before-forward、half/after-forwarded、normal、normal→cancel升级、accept/recvmsg/bind前后取消。normal流未EOF时无ACK；取消或自然结束ACK后释放旧gate、重连旧socket都不能继续转发。`selfcheck-results.json`保留冷writer修正后的11项；随后两条确定性调度负例各自修后单测，不再全套重跑。

本分支所有mock sockets位于 `/home/plumwsl/.g0r4check-*` 自有短目录；每次仅精确关闭其socket并删该次已核路径。`verify_cleanup.py`是只读身份/路径审计，`cleanup-final.json`记录9个Linux执行实例、38个已记录私有目录和8个可由记录取得的root，remainingProcesses/remainingPaths均空。未泛杀或结束共享Ubuntu；真实manager/Windows范围的清理由tasks分支单独审计，不能拿本分支mock清理替代。

所有5/15/25秒等仅为有限诊断观察，不是产品寿命或调用总量cap。没有读写saved settings、运行provider或PG；PG仅只读审查，另见pg-cross-review.zh.md。Windows/WSL应用环境、interop、PATH/automount保留。

## 复现与剩余范围

本机标准API小检查从Windows显式调用Ubuntu/plumwsl、Linux `.venv/bin/python`，入口为selfcheck.py、unstarted_connection_check.py、done_callback_check.py；它们只服务共享模块验证。真实manager不由本分支再跑独立矩阵，按tasks报告复现其windows_probe.py/manager_probe.py；离线审核以audit_integration.py传入已选结果文件，不新执行任务。归档清单见archive-files.txt。

仍有接口缺口：输出monkeypatch/文件RPC不是生产scope API；准备中的主机/receiver崩溃、诊断核验中取消、ABORT检查到Popen之间的具体暂停交错、全部Thread/timer/Popen失败以及GUI宿主/Quit、MCP/hooks/environment各自scope/权限与副作用、休眠/发行版外部终止和完整长程矩阵未覆盖。当前通过的四个PREPARING门不能泛称穷举全部启动取消竞态。任何已转发请求的物理回收不恢复丢失结果、不证明未执行、不自动重发。

本轮得到可复用的研究小接缝及有限集成正负证据；完整G0、Windows激活与Phase B仍未获验收。
