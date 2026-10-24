# G0-W 第四轮：真实任务合成与PG owner失联边界

本轮复用上一轮三位 `gpt-6.1-sol high` subagents，未新建agent。受测 Windows/Linux clone 基线均为 `adb3e1dec382b5531eface90b76d4968ec21c0b8`。继续 Ubuntu/plumwsl、WSL2、ext4应用数据与开启的interop；生产源码、用户配置与依赖未改，Phase B未开始。

**首次fd绑定、取消与真实terminal已在同一研究链路取得正证；PG双锁也已接入真实TS owner验证。实验识别了三类必要接缝：relay创建方收齐未开始或已完成但回调未跑的任务；terminal存活/结果冻结纳入跨域scope；PG owner区分未提交/未知结果与已结算PG错误。** 修正均仅在output原型中，不能宣称生产问题已修复或完整G0已通过。

## Terminal：本轮实际合成的链路

详见[真实任务报告](../../terminal/interop-tasks-round4/report.zh.md)及[机制与独立审阅](../../terminal/interop-races-round4/report.zh.md)。原TerminalManager先持有foreground decision和launch reservation，再在研究_spawn接缝内准备scope。anchor在Windows无breakaway Job内启动，自开上游O_PATH并用SCM_RIGHTS传fd；receiver核peer/pidfd并完成绑定。Windows宿主对实际诊断PE的精确HANDLE核Job归属后，才准入真实任务。

原Popen、PIPE/PTY、reader、watcher、8槽计算及完成发布继续由真实manager执行。scope装配发生在原线程start之前；正常完成须收齐Linux group/reader、封闭relay并结算已接纳连接、anchor EOF及Windows精确Job/HANDLE。取消先封新准入并结算连接任务，再组合Windows Job与Linux group回收；旧调用者未join前保留socket。路径退休在原owner release/join完成之后。

本轮通过的场景包括：

- 真实PREPARING期间的pin前、pin后、bind前、bind后取消；其中包含原release_owner关闭路径，确实持有原launch reservation，非manager调用前独立scope自测。
- upstream连接前后、首次转发前、部分转发后、首块已转发后取消；已有字节转发的请求失联仍为unknown，不由物理回收改报未发生。after-forwarded仅是opaque字节门，本轮没有用parser或PE启动事实证明该门已转发完整创建请求。
- 真实reader.start和watcher.start失败的回滚；原_join_physical在reader未start时也必须等待额外scope，不能只钩reader.join。
- PTY正常无pipe尾部、原8槽、PIPE/PTY Unicode输入及EOF/EOT、同一个registry/owner内A取消而B继续自然完成。

最终矩阵选择16份case证据，分为12份先前成功、冷任务修后2份针对性复验、最后done回调/存活谓词修后2份验证；不称整套在最终源码下全部重跑。独立断言核对绑定完成→宿主Job核验→operation admission；seal ACK的task/writer为0且之后无继续forward；Windows ActiveProcesses0、成员空及全部所持HANDLE signaled；原owner完成时Linux group/reader/watcher收齐、launch reservation为0；最后才退休路径。正常路径没有用TerminateJob冒充成功。

### 冷连接任务取消的真实失败与修正

`watcher-start-8a547091`的真实回滚实验超时：receiver在seal时退出，未返回ACK。其stderr当时未完整留存，因此不能把后续推断当成该次崩溃堆栈。独立确定性自检随后复现了相同候选缺陷：同步connected回调登记writer并create_task，任务在首次执行前被cancel，其try/finally根本未进入，最终task0但writer1使原assert失败。

修正将同步接纳的writer清理归回创建方：seal owner abort并等待剩余writer关闭后再移出集合，保留task/writer0断言；正常child finally同样等待真实关闭。未把清空集合当物理回收。修后冷任务门通过，并针对性重跑真实`watcher-start-2d54c333`和`half-forwarded-888767a0`通过。此前成功矩阵没有在最终源码下全部重跑，报告明确这一覆盖边界。

随后normal补测`06ea3e18`和`588862f5`又触发归零assert；后者实际记录task done=true、writers=[]，证明问题是完成回调仍排队，不能推定集合里的task还活着。标准asyncio门确定性复现：gather已完成task不保证让出执行done callback。最终改为创建方记录本次owned tasks、gather后逐个核done，再只移除这批已结算task；不加sleep掩盖。`06ea3e18`缺少同等细粒度快照，只保留关联推断。最终`normal-pty-b1134615`正常完成、seal后late caller拒绝/字节计数稳定且自然退出后退休。

### 存活判断与结果冻结不能漏掉Windows尾部

`06ea3e18`还实际暴露：physical_completion=false/liveCount1时wait结果已经success。原`_status`及`_accept_termination_intent`使用`_process_is_live`，它只观察Linux root/PGID；因此仅延迟reader.join和完成发布，仍可能提前冻结success并拒绝后来的取消intent。

最终仅在研究解释器内扩展同一个state的物理存活谓词：scope尚未Windows join也算live，仍沿原_status/freeze/intent及watcher。`normal-pty-b1134615`尾部poll为running、slot1、再接受7项而第8项blocked；`tail-cancel-c584858c`在Linux root/PGID已退、Windows child仍活时poll为running，之后真实kill最终为killed、物理join且slot0。这证明必要接缝不止spawn与join，还包括既有owner的存活/结果冻结判断；没有改写原结果使测试变绿。

另保留首轮诊断日志pid键冲突、manager多线程写DrvFs导致JSONL交错，以及修前watcher超时。原日志不补写成成功；修后manager日志串行记录，成功case逐行可解析。标准Unix自检与真实WSL/manager证据分开，不用前者替代后者。

## PG：真实TS owner与双锁helper

详见[PG集成报告](../../backend/pg-owner-round4/report.zh.md)。Windows显式Node22.23.3导入当前真实`BundledPostgres`构建，通过现有PostgresOperations接口逐次调用WSL Linux有限helper，未复制另一套owner。每次物理操作重新取得所需锁；removeDataDirectory重新检查当前精确data/uid/version/status并持双锁删除，不能借前次stop结果授权。

正常正控完成：原owner初始化/start、第二个原owner复用且startedHere=false、原resetConfirmed的running→readInstance→stop→remove实际分支、保留锁inode/socket/外部sentinel、重建查询及stop。start链仅继承独立PG锁；所有有限status及mutator按其物理寿命继承准入锁。

| 观察边界 | 真实结果 |
| --- | --- |
| ready前EOF、ready后operation前EOF/取消 | 最终adapter关闭准入后不再写operation；helper无操作退出后才报告未提交，PG未启动 |
| operation已写后stdin EOF | 实际操作继续结算，完整回复及正常carrier退出后采用成功 |
| start已结算但stdout读端丢失 | 报unknown，保留真实PG；不能因此前诊断settled帧当成有效result |
| 完整JSON缺最后换行 | 不采纳残帧，报unknown；实际PG仍运行 |
| helper/carrier/Node分别死亡 | 独立observer在真实PG停点记录kernel可取、PG锁busy、status3；恢复准确旧PG后查询成功，不将carrier close当未执行 |

最初ready前EOF探针暴露了late-ready后仍尝试写帧的缺口；修后在真实写点再核本次admissionClosed，原失败和源码快照保留。这个标志仅属当前调用，不是持久状态或第二startedHere。真实Node不存在exe的spawn错误另外验证为not-submitted，保留原Error/code/cause；它不是一次WSL死亡实验。

### 原owner误归属反例与两行研究修正

原`start()`在任何run错误后都会尝试status/readInstance并可能设置startedHere=true，再抛原错误；现main Quit会据此选择stop。本轮丢回复和未终止帧均实际观察到unknown仍设置该字段。

更强对照`foreign-before-readback`：本次start的operation-written计数为0，helper报告no-operation并正常退出；随后由另一个遵守双锁协议的有限调用真实启动同一专用PG，再放行原owner的catch读回。原owner抛PG_NOT_EXECUTED，却仍startedHere=true/mainQuitWouldStop=true。当前精确实例存在只能证明当前状态，不能证明它由这次未提交的启动创建。

研究变体从真实postgres.ts机械生成，仅在start catch增加两行：物理adapter明确标记not-submitted或unknown时直接重抛；真实PG已结算非零错误仍沿原status/verify readback。错误标记由是否写帧、完整result及carrier事实决定，不按场景名伪造结果。该标记是研究合同草案，尚未成为生产接口。

对照与reply-loss两项在变体下均保留PG/query成功和原错误，startedHere=false/mainQuitWouldStop=false。原PG测试8/8与同一原测试运行研究变体8/8通过；本轮未新增真实readiness非零恢复实验，其保留行为由未变源码路径及原测试支持。PG基线正常1项、最终协议6项、死亡3项、误归属对照1项及变体2项分开记录；故意杀Node的非零退出是预期故障事实，不是harness失败。

## 限制与下一步

本轮仍是output-only集成：terminal的文件RPC、command筛选、局部scope映射和Python ctypes不作为产品实现；PG的typed错误与两行guard未改入生产。独立标准库API、实际owner与受测故障次序可行，不等于所有时序已经穷举。

清理覆盖全部成功及失败实验。PG的14个独立home及可观察本次进程已收齐；relay标准自检的9个Linux身份/已记录私有路径无残留。真实任务审计254个历史Windows身份、73个Linux身份及25个私有目录，原精确owned范围已结算、Linux身份及目录无残留。一个历史PID的当前OpenProcess回查返回error5仍如实保留；它当时持续持有且creation匹配的同一HANDLE已经wait0，证明原实验实例退出，但不证明当前PID不存在或已复用。原严格回查失败和分对象结论均归档，不通过操作该当前PID解决观测限制。

PG专题agent与竞态专题agent只读审阅了root总报告及相关规范；交叉审查支持PG错误分类和terminal live谓词接缝，均限定output候选。审阅将本轮after-forwarded从“完整请求”收窄为“首块字节”，未增加实验去补写未经观察的事实。原失败保留，语义修改只体现在活动候选规范，不改写前轮历史报告。

归档88个所选文件约3MB；21份Python只作格式化/必要lint注释，规范化语法树与output原文件一致，其余所选文件逐字节一致，见[归档校验](archive-check.json)。root独立复核PG13项及原失败、terminal16项的12/2/2阶段、11项Unix-only自检、清理限定及文档链接，见[记录复核](verification.json)。锁定Node22检查归档CJS语法、仓库Ruff及Git空白检查通过。生产src/desktop无diff；记录校验不重复计入运行实验或G0资格。

仍待G0验证的关键范围包括：terminal诊断/宿主核验与最终Popen之间更多取消交错、relay/宿主失联组合、MCP/hooks/environment各自owner、真实Electron Quit与休眠；PG完整畸形/重复帧、所有操作的失联分支与端到端宿主退出顺序。尚未运行真实provider/GUI或原生Ubuntu x64/arm64资格验收，不用WSL替代原生平台证据。

后续继续G0研究，优先把已识别的typed PG执行结果边界与跨域scope失败分类固化为可审阅合同，再补上述剩余owner和宿主故障；不因此自动进入Phase B。
