# G0-W-C 控制 / Windows interop 实测

2026-10-24，terminal / gpt-6.1-sol high。所有新增探针和证据仅在本目录；没有修改生产代码、活动规格、用户配置、凭据或数据库，没有 commit。根 AGENTS、当前 README/spec/G0清单及terminal历史已读；专用发行版/禁interop的旧路线不再适用。

**当前 Linux PGID 不能证明普通 Windows interop 后代已结算。该 G0-C 子项未通过，完整 G0-C 未通过。** 这不是仅凭刻意 detached 得出的结论：普通 `.NET ProcessStartInfo` 子代的取消反例已独立复现两次。探针自身退出0表示观察/清理断言完成，不能被整理成产品生命周期通过。

## 环境与边界

完整事实在 `environment.json`：Windows 10.0.26200.9711，WSL2 2.7.11.0，Linux kernel 6.18.33.2-microsoft-standard-WSL2，已有Ubuntu26.04.1 LTS / plumwsl uid1000，home `/home/plumwsl`。最终 Windows Node 与Windows PE均22.23.3，Linux Python3.12.15。Windows/Linux clone均 `da397a2bb701c6371ed19155f66328334684bf38`。

早期有标明系统Python3.14.4的机制初探，另留原始失败日志；最终基础pipe、interop矩阵与重复、源码/随包EOF均已用3.12.15验证。没有使用系统Windows Node24.18.0的结果替代pin资格。最早未等子就绪的试验确实使用了全局Node24，不能用于后代结论。

宿主通过系统 `C:\Windows\System32\wsl.exe`，显式 `-d Ubuntu -u plumwsl --cd <Linuxcwd> --exec <LinuxPython>`，Node `shell:false` 和三条pipe；仅清理宿主WSLENV自动传递列表，保留Linux interop/Windows PATH/automount。PowerShell只作为**被Linux任务显式启动的Windows测试PE**和Windows进程事实观察器，不是外层launcher的求值包装。

Windows mise pin路径最初在本次DrvFs观察中ENOENT，而Windows可直接执行；未推测成已解决的WSL问题。为固定被测PE版本，仅把同一node.exe复制到本目录 `node-pin.exe`。最终清理前比较源与副本完整Buffer相等（86,973,768字节），已删除副本；未生成摘要或注册表。复现需先重新复制，见 `commands.md`。

## 子项结果

| 子项 | 实际状态 | 证据与限制 |
| --- | --- | --- |
| argv / 三pipe / EOF / code0,1,7 | 通过该基础子项 | `host-pipes.node22-python312.log`；每次准确回显1,048,596字节输入并drain 2,097,152字节stderr，assert完整Buffer相等，不经PS文本pipeline |
| 中文、emoji、空参数、引号、反斜线、shell符号 | 通过该基础子项 | 同日志；argv按完整数组相等验证，Linuxuid1000；输入从UTF8首字节处分段，输出JSONL逐字节写，readline恢复完整一帧 |
| 真实源码runtime pre-start EOF | 通过该子项 | `runtime-eof.node22-python312-source.log`；显式PYTHONPATH/src，来源不是随包；stopping/stopped、exit0、wait-owner完成、flock可取得 |
| 真实随包runtime pre-start EOF | 通过该子项 | `runtime-eof.node22-python312-bundled.log`；PYTHONPATH显式unset，moduleOrigin为资源Python site-packages，stopping/stopped、exit0、wait-owner与flock，未创建settings/DB；两个自有home均已删除。日志尾的早期统一scope文案仍写source，前面的实际argv/origin/moduleOrigin准确，未改写原始日志 |
| 普通Windows父先退、子持stdout | 通过这次观察，非整个join资格 | `interop-native-root-repeat.node22-python312.log`：Windows父32544已不存在，普通子32504仍活，carrier尚未close；随后子finished/输出EOF，Linuxwait0/root退出、carrierclose0。证明本场景bridge会等pipe drain，不能推广到没有pipe的后代 |
| 普通Windows后代 + Linux killpg SIGTERM | **未通过结算子项；可复现反例** | 第一次Windows子33492；独立第二次子38396。Linuxproxy被SIGTERM终止、Linuxroot退出、carrierclose0后，子仍被WindowsCIM确认在运行；Windows父已不存在。之后它仅因本次有限工作完成才退出 |
| 普通Windows后代 + 仅carrier终止 | **未通过“carrier结束证明全范围结束”** | `interop-native.node22-python312.log`：仅终止自有wsl.exe34584，close SIGTERM后普通子36732仍活，最终finished；不称正常Quit |
| Windows Node宿主崩溃 | 已实测失联反例；产品恢复/关闭仍待验证 | `node-crash.node22-python312.log`：只SIGKILL自有Windows Node宿主37156；宿主不存在后普通Windows父38312和子38160仍活，后来分别正常完成。Linux记录未收到最终结果，仍按该次结果不明；不能以宿主消失宣布joined |
| Node普通 / detached / detached+pipe 后代 | 观察完成，范围明确区分 | `interop-lifecycle.node22-python312.log`共9场；普通Node父有libuv Job，父退/取消通常带走普通子，因此该现象不证明WSL的普遍回收能力。显式detached子可在父/PGID/carrier结束后继续；不因此单独要求覆盖任意逃逸后代 |
| kernel start后 / shutdown / malformed或partial JSONL / 启动中取消 | 待验证 | 本轮只实测pre-start EOF与独立pipe JSONL，不把缺失的PG/start/取消authority实现写成已测 |
| Linux terminal PIPE/PTY、submit/EOT、8槽、monitor、MCP/hook集成 | 本目录未独立执行 | root另有聚焦Linux测试结果；本报告不把它们当Windows完整链路实测 |
| 整个发行版外部终止、休眠恢复、真实Windows GUI正常Quit | 待验证 | 没有可丢弃故障发行版，不在共享Ubuntu上试杀；未请求/执行睡眠或GUI正常Quit |

所有6/8/14/16秒均是这次有限测试负载的持续时间，**不是产品总寿命/重试/任务cap**。

## 普通非detached反例的确切次序

`windows-native-parent.ps1`使用标准 `.NET ProcessStartInfo`，`UseShellExecute=false`、`CreateNoWindow=true`，默认继承stdio；没有设置DETACHED_PROCESS、CREATE_NEW_PROCESS_GROUP、breakaway或自建Job。Windows子只执行 `windows-pe.cjs child ... native`，写自己的PID/ppid/time/版本、等待有限工作、写finished，不spawn其他child。Linux只为任务根proxy创建独立session/PGID，等价于现Linux任务范围的起点，不把Windows子进程设置成显式逃逸。

独立重复的记录 `interop-native-repeat.node22-python312.log`：

1. Linuxroot19214创建proxy19215，PGID19215。WindowsPowerShell父34580普通启动WindowsNode子38396。
2. WindowsCIM在故障前确认子38396、父34580的完整命令行/CreationDate/可执行文件均匹配本次唯一record。
3. Linux收到确切 `cancel` 控制；`os.killpg(19215,SIGTERM)`成功；`p.wait()`返回-15；Linuxroot19214退出；Windowscarrier35056 exit/close为0。
4. `windows-facts-after-one-second`仍确认**同一CreationDate**的子38396活着，父34580为null；不是PID复用猜测。
5. 子38396写 `windows-child-finished`，随后CIM子/父均null；Linux `/proc/19214`不存在。

第一次独立运行同样留下Linuxroot14722/proxy14739与普通子33492的负例。两个案例完整Windows事实和Linux控制/exit事件在日志中，不把“取消已发”“Linuxproxy已退出”“carrierclose0”写成Windows子代已joined。有限子代之后自退只是实验cleanup，不是取消机制已正确。

普通父先退/持pipe的复验补了Windows父的独立事实：Windows父32544为null、子32504仍在、carrierClosed=false；Linuxwait直到子写finished后才返回0。证明未受故障时读pipe确实能观察到这一个持pipe子代，SIGTERM杀掉Linuxproxy后则不再拥有这个drain观察点。

## SDK / 平台所有权核对

本轮没有写替代parser、relay、Job framework或生产WSL adapter。边界仍为：Node/libuv拥有Windowsspawn和pipe；WSL拥有binfmt/interop relay和直接WindowsPE进程；Pulsara原Linuxowner拥有自己的PGID/drain/join语义。当前缺口是普通Windows后代在Linuxproxy失联/取消后继续，却没有当前任务范围的Windowsjoin事实，不能把Linuxgroup状态升格为全范围authority。

已读锁定版本的官方实现：

- [Node22.23.3内置libuv win/process.c](https://raw.githubusercontent.com/nodejs/node/v22.23.3/deps/uv/src/win/process.c)，`uv__init_global_job_handle`及`uv_spawn`：非detached直接子被放进Node的KILL_ON_JOB_CLOSE Job；detached分支使用DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP。因此仅用Node父probe会掩盖普通WindowsCreateProcess后代问题。
- [WSL2.7.11 Linux binfmt.cpp](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/linux/init/binfmt.cpp)，`CreateNtProcessUtilityVm`：relay与控制通道分离；直接进程退出后继续读取stdout/stderr到EOF，符合父先退持pipe实测；signal侧显式处理SIGINT/SIGWINCH，不能把LinuxSIGTERM当作Windows树信号。
- [WSL2.7.11 Windows interop.cpp](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/interop.cpp)，`ProcessInteropMessages`：连接关闭时对非GUI的`Result->Process`调用TerminateProcess，查看到的对象是直接PE句柄；没有在此代码中见到普通子代范围的join证据。
- [WSL2.7.11 SubProcess.cpp](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/SubProcess.cpp)及[头文件](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/SubProcess.h)：配置环境、cwd、标准handle及CreateProcessAsUser；核对过的创建路径未提供供当前wsl.exe CLI调用者注入每任务Windows后代scope的扩展点。此处是已查路径的观察，不宣称遍历所有Windows/WSL API后证明“不可能”。
- [Node锁定版本child_process文档](https://nodejs.org/download/release/v22.23.3/docs/api/child_process.html#optionsdetached)与[WSL官方interop说明](https://wsl.dev/technical-documentation/interop/)用于确认原有supported机制，没有用communityissue替代本机实证。

下一步只能先在规格层明确现有任务范围如何对应Windows普通后代与何时保留unknown，再据真实SDK/API扩展点做最小后续实验；本轮没有授权进入B，也没有把不了解的Job/fork机制接生产。

## 试验误差和保留的原始失败

`initial-no-child-ready.*`：首轮Windows父太快退出，子未就绪；不能用来推断“没有后代”。之后用仅探针IPC/文件就绪握手确认child实际已启动。

`initial-pin-path-not-visible.*`：WSL读取Windows mise pin路径ENOENT；改为output内明确副本后pin PE实际自报22.23.3，最后完整字节比较并删除副本。

`initial-child-already-gone.*`：Node父Job让普通Node子在CIM前已消失；之后保留此观察并加入不经Node父spawn的.NET普通child，不删弱化断言求产品通过。

系统Python3.14首轮有PE继承控制stdin导致relay抢读、Linux收到空字符串的情况；3.12复验显式给PE `stdin=DEVNULL`，外层JSONL/control独占自己的stdin，实际收到cancel。该调整只隔离测试控制通道，不改变生产terminal输入合同。

Node崩溃首轮子工作太短，CIM查询开销让故障后观察落在自然完成之后，保存为 `node-crash-initial-short-work.*`，不拿来断言回收；第二轮曾误读前一轮record，保存 `node-crash-initial-stale-record.*`，最终换成每次宿主PID唯一record后重跑，负例和cleanup均已完成。没有重写原始失败输出。

## 清理与可归档证据

`cleanup-final.json`/`cleanup-final.log`对全部原始log和record提取的74个WindowsPID、51个LinuxPID做只读观察，**remainingOwnedWindows=[]、remainingOwnedLinux=[]**。PID已复用给其他命令的实例只记录为复用，未清理或操作；未触碰用户bash403。两个runtime独占home在原wait-owner完成且锁可取得后精确删除，日志保留前后事实。

没有terminate/shutdown/unregister Ubuntu，没有全局kill/taskkill；故障注入只针对确切自有carrier或WindowsNode宿主，LinuxSIGTERM只发给本次独立PGID。Windows有限子代都已完成或被其测试Node父Job结束，最终无自有进程残留；没有操作用户DB、settings或凭据。`node-pin.exe`完整Buffer等同性与删除事实在第一次 `cleanup.log`，最终再次确认不存在。

建议root归档下列小文件，不要归档全部output或二进制：

- 本报告、`commands.md`、`environment.json`、`cleanup-final.json`和`cleanup.log`。
- `host-pipes.cjs`、`host-pipes.node22-python312.log`。
- `windows-pe.cjs`、`windows-native-parent.ps1`、`interop-lifecycle.cjs`。
- `interop-native.node22-python312.log`、`interop-native-repeat.node22-python312.log`、`interop-native-root-repeat.node22-python312.log`，以及对应 `python312-native-*.windows.jsonl*` 原始record。
- `node-crash.cjs`、`node-crash.node22-python312.log`、`nodecrash-36916-nodecrash-native.windows.jsonl*`。
- `runtime-eof.cjs`、源码/随包两份EOF日志。

未补齐的完整G0-C场景见上表；本轮最实质风险是普通Windows后代取消/失联的结算缺口。未实现Phase B、未宣称Windows支持激活。
