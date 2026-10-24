# 第五轮：真实 hooks 与 environment probe 的跨域生命周期

受测两clone基线 `b741dd92b06a2d167d882e312e18f503e23b8a89`。本分支只写interop_races_r3/round5，未改生产/spec/第四轮或他方文件，未提交，未进入B。读取AGENTS、当前spec §5/13/20、实际HookCommandExecutor、ProcessCredentialBoundary、output_parser及TerminalEnvironmentOwner后，先跑baseline，再做output-only接缝。

**真实baseline确认：hook成功、hook超时和环境snapshot缓存成功返回时，普通Windows子进程仍可存活。复用第四轮SCM/Job候选后，保留hooks各自结果/凭据/16+8槽位与environment自己的snapshot/close政策，选定12个正负case离线断言通过。生产缺口仍未修复，不据这些有限case宣布整个G0或全部交错通过。**

## 所有权与最小适配

Windows编排直接只读复用第四轮WindowsScope及其无breakaway、KILL_ON_JOB_CLOSE、CREATE_SUSPENDED→Assign→Resume、精确HANDLE/Job join。Linux只读复用第四轮Scope的标准SCM_RIGHTS/peer/pidfd绑定、seal/ACK/退休。未创建TerminalManager，没有借terminal8槽或其状态机处理hooks；没有复制WSL parser、hook dispatcher、输出parser、credential guard或environment snapshot实现。

Hook真实执行仍由原HookCommandExecutor：16同步/8异步slots，声明timeout、cancellation_signal、Task.cancel、begin_host_close/aclose、stdin公共JSON、两路bounded drain，以及parse_handler_output全部沿原实现。研究子类仅在原_spawn_and_drain入口前准备物理scope；准备在原最终credential guard之前，因此凭据变化仍由原不可逆spawn sink的第二次真实guard复核。真实create_subprocess_shell返回后，只给这个Process实例的wait追加Windows自然join；原_abort_process_group接缝先封relay并收Windows，再调用原Linux abort。正常非零不因scope变成成功，取消/超时仍由原executor产生原failure_code。

声明环境只增加私有WSL_INTEROP路由，没有改写已审hook command或public JSON来通过guard。ProcessCredentialBoundary只持本次合成值，未读取saved settings/真实credentials/provider。合成凭据从Linux子环境移除、public stdin由原scrub替换，公共canary和中文到达真实PE；额外command含同一合成值时，原guard在scope准备与spawn之前拒绝。

Environment另使用真实TerminalEnvironmentOwner._snapshot/_run_probe：原default-deny env、锁内Popen/attempt发布、selector/sentinel解析、timeout/close condition及成功缓存规则保留。私有测试shell为当前用户新建的0700脚本，它先运行真实PE工作负载，再exec实际/bin/sh执行原probe command；没有mock _run_probe或改用户启动文件。研究仅在该确切Popen的env加入私有interop route，wait扩展Windows scope，_terminate_and_join先收Windows再原Linux回收。snapshot输出仍经原sanitize，不把路由或合成credential当可缓存环境。environment关闭沿原合同报告PROBE_TIMEOUT，不套用hook的HOOK_CANCELLED。

## 有效baseline三项

`baseline-7274d990-results.json`，没有上述wait/abort适配，只有正确私有路由及物理观察fixture。

| case | 原owner返回 | 同时Windows事实 |
| --- | --- | --- |
| hook-baseline-normal-8b1f3335，无pipe普通子 | exit0、ValidHandlerContribution、约0.58秒，processes0/原16+8槽已归还 | 对应child的精确creation HANDLE wait258，仍活 |
| hook-baseline-normal-f0d9c279，子持pipe | 原timeout2秒产生HOOK_TIMEOUT | 对应Windows child精确HANDLE仍wait258；Linux取消没有回收它 |
| environment-baseline-environment-010fd6f6 | 原snapshotUsed=true/error=null/cache1/attempts0 | 对应Windows child精确HANDLE仍wait258 |

这些live成员断言匹配实际pe-start child PID+creation，不把仍活的anchor基础设施误当任务后代。baseline返回之后显式取消本次自有Job并退休路径，未留下工作负载。没有把baseline标成产品通过。

## 保留原hook结果的六项及guard阴性

`hooks-9cad5e67-results.json`：

| 场景 | 原执行器/原parser结果 | 物理结果 |
| --- | --- | --- |
| normal-e4396da4 | exit0、failureCode=null、ValidHandlerContribution，中文stdout/stderr保留 | Windows普通无pipe子自然结束，Job未调用TerminateJob |
| nonzero-13f9556f | exit7、failureCode=null、HandlerFailure | 非零保真，普通无pipe子自然结束，未TerminateJob |
| timeout-f4ecb9bd | 原声明3秒、HOOK_TIMEOUT、HandlerFailure | 持pipe普通Windows后代被本次Job收回；原Linux abort/drain随后结算 |
| cancel-tail-a013a0fc | cancellation_signal→HOOK_CANCELLED | 父先退、无pipeWindows尾部仍活时取消有效 |
| task-cancel-9b2197bf | 实际task.cancel传播CancelledError | 原shielded abort/drain走完；未把取消变成功结果 |
| host-close-0b48abf8 | 原begin_host_close+aclose→HOOK_CANCELLED | 原关闭lane与在途执行共同结算，非杀共享distro |

六项都通过原executor processes集合归零、syncSlots16/backgroundSlots8断言；WindowsScope在原hook结果/取消传播之前已ActiveProcesses0、members=[]、所持精确HANDLE全部signaled。正常与非零等待自然结束，不用TerminateJob冒充完成。

`guard-7f3b11a5-results.json`：command含本次合成credential，原guard给API_KEY_VALUE_PRESENT/exit=None/HandlerFailure；无relay、无工作负载spawn、无PE。其余实际hook子观察synthetic credential环境项不存在，public stdin到PE带原REDACTED_CREDENTIAL占位和中文，而公共环境canary保留。这里只证明本次真实guard对这个caller-owned值的边界，不声称清扫所有环境秘密。

## Environment真实timeout与close

`environment-ef9ce490-results.json` 两项：

- environment-b16a7028：正常probe shell根可退出、无pipeWindows子继续20秒工作负载；把scope纳入原Popen wait后，原2秒probe timeout触发跨域终止，PROBE_TIMEOUT、无snapshot、cache0/attempts0。
- close-576be31f：Windows普通子持pipe，真实worker在原_snapshot内；主线程调用原close_and_join，原selector观察closed并经原terminate路径加scope收齐。关闭返回与worker结束，PROBE_TIMEOUT/cache0/attempts0，无成功缓存。

Environment没有进hook slots或terminal8槽，也未把取消/timeout失败伪造成snapshot成功。私有probe shell/home最后精确删除。

## 共用physical scope的独立断言

`verify_results.py` 对以上12项离线重新断言，不执行新工作负载：binding完成并由Windows宿主对实际诊断PE精确Job核验后才执行工作负载；seal ACK task/writer0且之后没有bytes-forwarded；ACK先于Windows Job cancel；Windows全部已持HANDLE signaled/成员空；相关scope join早于原hook/environment返回；路径保留到原结果/关闭结算之后才retire。每次ack还通过原第四轮late caller机制验证旧路径拒绝、无新PE及byte计数稳定。已有转发的取消仍为unknown物理边界，不据回收推断副作用未发生，不重发hook。

Hook成功/非零的完整stdout/stderr、原parser分类与实际exit保留。scope日志里的conservative unknown只是byte转发后回收时的执行不明提示，不能改写已由原owner取得的hook正常结果或抹去其输出。

## 原失败、回归与清理

首轮 `baseline-5d0a12f7-results.json` 的两个hook工作负载用Windows默认GBK输出✓，真实UnicodeEncodeError使预期成功变exit1。原结果完整保留，不用于成功baseline结论；仅将研究PE stdout/stderr显式设UTF8后，独立新uuid重跑有效三项。首次环境baseline不受该打印影响，但最终选择仍用新组。

真实Linux聚焦回归 `tests/test_round9_2_hook_subsystem.py` 与 `tests/test_round2_terminal_environment.py`：55 passed，无skip/xfail；见focused-tests.log。没有修改生产断言。

`verify_cleanup.py`只读覆盖包括首轮在内210个Windows PID+creation实例、61个Linux PID+starttime实例、32个已记录私有relay/probe-home目录；remainingWindows/unknownWindows/remainingLinux/remainingDirectories均空。Windows操作只用自有HANDLE；Linux由实际owner处理本次Popen/进程组，诊断进程pidfd及精确scope清理沿第四轮fixture，没有泛杀或结束共享Ubuntu。审计仅证明记录的本次实例/路径，不把它扩为跨权限全发行版枚举。

Windows解释器3.12.13、Linux项目解释器3.12.15；生产模块实际来自Linux clone src路径。WSL launcher清本次WSLENV传递，interop/PATH/automount与用户配置不改。2/3/8秒为原owner明确声明的本次timeout或有限诊断预算，20秒为试验工作负载；没有新增产品总寿命或调用数cap。

## 复现与剩余

仓库根用Windows本地uv环境运行windows_probe.py的baseline/hooks/guard/environment子组；真实Linux执行由显式Ubuntu/plumwsl、Linux绝对.venv入口承担。验证用verify_results.py及verify_cleanup.py。旧r4 Scope/WindowsScope/relay_fixture只读依赖须同时可定位；archive-files.txt选择小型源码、结构化原始结果、必要失败和审计，不复制同内容console日志。

仍未覆盖所有hook dispatcher事件/async completion政策、密集slot并发、credential rotation与scope准备同时发生的专门故障、创建/owner-close/receiver死亡所有交错、长程和真实GUI关机全链。environment只关闭timeout与close两个关键scope子项，TTL/single-flight/default-deny原回归支持，不冒称全新跨域穷举。完整Windows G0与生产hooks/environment接入未获激活；下一轮继续其他G0门，不进入Phase B。
