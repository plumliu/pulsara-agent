# G0-W-C 第二轮独立审查：每操作 interop ownership 与最小候选

2026-10-24 开始、跨日至2026-10-25定稿，interop_review。受测提交 `fb2c57da12d24ddda32da1320db1d7c645b5ebb6`。遵循根 AGENTS、当前 Windows README/spec §5/§8/§17、G0 清单和首轮 terminal 报告；未改生产代码、活动规格、共享发行版配置或数据库，未提交，未进入 Phase B。本轮按用户授权并行研究，使用 ponytail 技能收敛最小机制；没有安装依赖。

**本机已证实“无 breakaway Job → 专属 wsl.exe/wslhost → interop PE 及普通 CreateProcess 后代”的继承成立。原路线并非机制上必然不可能。裸 `WSL_INTEROP` 环境注入有真实 fallback 反例；一个不解析协议的私有 Unix byte relay 已局部闭合这个失败窗口。整个 G0-W-C 仍未通过，候选没有生产采用批准。**

## 1. 现有 owner 与平台事实

`terminal_process/manager.py` 的 `_spawn` 仍在 Linux 创建 PIPE/PTY、`start_new_session=True`，其 `_watch_process` 等 root、reader、Linux PGID；`_terminate_process_group` 发 SIGTERM/SIGKILL，`_join_physical` 组合等待 reader/watcher/timer/group。`environment.py::_terminate_and_join` 同样只拥有自己的 Linux group。它们不能凭 PGID 消失证明 Windows 后代退出，也不应被本原型替换为 Windows terminal owner。

`environment.json` 保留实际来源：Windows build26200.9711、WSL2.7.11.0/kernel6.18.33.2、Ubuntu/plumwsl uid1000；Linux clone 与 `.venv` Python3.12.15。Windows研究调用本仓库 `.venv/Scripts/python.exe`，实际解释器3.12.13；这只是 ctypes 实验工具，不是建议引入 Windows Python产品依赖。锁定独立 Node22.23.3/libuv1.51.0；实际 Electron44.9.0 自报内嵌 Node24.22.0/libuv1.52.1，不能把独立Node的版本当作Electron内嵌版本。

应用/guest cwd 仍为 `/home/plumwsl/workspaces/pulsara_agent`。仅小型 record/脚本经 `/mnt/c` 写 Windows output；byte relay 的 Unix socket 位于 Linux ext4 自有 `output/windows-g0/interop_review/round2/byte-relay-*`，目录权限0700。没有用 DrvFs 存生产 workspace/PG/应用资源。

## 2. 官方实现解释了哪些事实

已下载的官方源码快照在 `sources/`，版本均明确为 WSL tag2.7.11 或 Node tagv22.23.3，没有生成文件指纹。关键路径：

- [WSL interop 说明](https://wsl.dev/technical-documentation/interop/)与 [util.cpp::UtilConnectToInteropServer](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/linux/init/util.cpp)：`WSL_INTEROP` 是 Unix socket 路由。未设置，或该路径 `access(F_OK)` 返回 ENOENT 时，会沿 Linux 父链找其他 interop socket；不是 Job身份或防fallback开关。
- [init.cpp::CreateProcessUtilityVm](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/linux/init/init.cpp) 为 Windows CLI这次Linux执行创建专属 interop socket，并把路径放进子进程环境；普通 Linux `fork/exec` 的子 shell不会自动取得每操作独立 Windows Job。
- [svccomm.cpp::SvcComm::LaunchProcess/SpawnWslHost](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/svccomm.cpp) 在当前 `wsl.exe` 中运行 `interop::VmModeWorkerThread`，也普通启动后备 `wslhost.exe`；[helpers.cpp::LaunchWslHost/ProcessLauncher](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/helpers.cpp) 经现有 SubProcess启动，没有这里专用的 Job breakaway。`wslhost` 等当前wsl父进程退出才接管 interop，见其 [main.cpp](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/wslhost/main.cpp)。因此把CLI在执行前放入无breakaway Job，可以沿真实Windows创建关系继承，jobs代理已有实测。
- [interop.cpp::CreateProcess/ProcessInteropMessages](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/interop.cpp) 与 [SubProcess.cpp](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/SubProcess.cpp) 创建直接PE、转发退出和直接进程句柄；没有每 Pulsara操作的 Job参数。`PROCESS_CREATION_DESKTOP_APP_BREAKAWAY_OVERRIDE` 是桌面打包策略属性，不能误读成已调用 `CREATE_BREAKAWAY_FROM_JOB`。
- [SocketChannel.h](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/shared/inc/SocketChannel.h)、[socketshared.h](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/shared/inc/socketshared.h)、[config.cpp::ConfigHandleInteropMessage](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/linux/init/config.cpp) 与 util 的 Accept 实现表明：本次 WSL2 Unix创建请求通路是普通 write/recv 字节，没有 SCM_RIGHTS、SO_PEERCRED 或需转交的OS句柄。创建请求里的vsock port由WSL解释；真正stdio/control继续走WSL原vsock。因此纯byte转接不需要复制消息布局、parser、stdio relay或进程状态机。此结论仅覆盖已查的WSL2创建路径，不宣称所有未来WSL协议都等价。

## 3. 独立实验与反证

以下脚本均仅研究；所有时间是有限实验负载/探针等待，不是产品任务寿命或总量cap。

| 实验 | 原始证据 | 实际结论 |
| --- | --- | --- |
| 不同 Linux父链显式路由到A/B | `socket_probe.py`、`fe6645af-summary.json`、同名route-A/B records | PE普通父/子分别且仅在所选 Job；A父先退、子明确继承stdout时runner尚未退出。取消A后A确切实例结束，B仍运行并自然完成。无需拦截shell语法或修改任意PE。 |
| socket删除后的裸env | 同summary的 missingSocket | A anchor已结束、旧WSL_INTEROP路径消失后，从另一Linux父链启动普通PE仍成功，父/子 `in A=false/in B=false`。WSL fallback到调用者自身relay；预检查路径存在不能消除竞态。 |
| 仅把实际任务放同carrier祖先 | `same-chain-66dccd3c-summary.json`、`same-chain-3c90111a*.linux` | 显式Linux setsid子可在carrier结束后reparent到PID1并fallback；这属于Linux主动逃逸，不用它证明原PGID内普通任务失败。改为普通同PGID子后，anchor死时子也结束，未观察到后续spawn；该次等待worker-done超时原样保留，不记成功。 |
| O_PATH pin真实interop | `pinned_socket_probe.py`、`pin-36e45fe3-summary.json` | owner仍live时 `/proc/<owner>/fd/N` 可路由到AJob。anchor死、原路径删后pin的nlink0仍F_OK，connect111，PE未启动；但owner死、FD关闭/复用或procPID复用后路径仍可能失效。仅O_PATH不能作全失败边界保证。 |
| 硬链接 | 下述保留的errno事实 | 当前socket root所有、`/run/WSL` root755，用户ext4目标EXDEV18，同`/run/WSL`唯一目标EPERM1，protected_hardlinks=1。无用户配置修改/root协助的同inode持久别名在本机不可用；没有因此引入privileged mount/binfmt。 |
| 私有opaque byte relay第二轮 | `relay_probe.py`、`relay-4e1c8baf-summary.json`及A/B relay/live/dead records | alive分别命中A/B Job；A取消不伤B；anchor A死后向固定pin连接失败，无PE启动；relay B自己SIGKILL后pathname仍为socket，后续PE connect111且未启动；既有B父/子在relay死亡前后均确认同creation live，之后自然finished。 |

最终relay使用标准库 `asyncio.start_unix_server/open_unix_connection`，每条连接仅双向read/write/drain/half-close，零消息解析。连接上游用O_PATH pin，只在relay自己活着时引用自己的fd；relay死后客户端指向的独立filesystem socket仍在，所以不会落入WSL的ENOENT分支。私有socket直到该实验所有runner收回后才精确unlink/rmdir；没有自动重新绑定到另一个anchor。

anchor死时WSL在该次请求未得到vsock连接，原WSL等待约10秒后报 `UtilAcceptVsock ...110`，随后失败；这是WSL锁定实现自己的请求边界，不是新产品timeout。生产取消若丢回复仍须保留unknown，不能把“未看到PE记录”推广成任意事务未执行。relay死后新connect111的场景则没有发出创建请求。

第一轮 `relay-ca30a7c3-summary.json` 使用6秒B负载，而dead-anchor请求耗时约10秒；它不能证明B在relay死时仍在运行。第二轮改有限25秒负载并增加 `BBeforeRelayKill/BAfterRelayKill` 精确身份断言后才证明该边界。首个 `4613808b-summary.json` 的Python子未显式继承pipe，root退出后runner已经返回；后续显式传stdout/stderr才覆盖“持pipe”场景。两份原始弱证据/断言失败均保留，没有修改产品断言求绿。

硬链接观察的确切目标为本轮临时ext4目录 `.../output/pulsara-interop-hardlink-af5mzs2g/socket`（EXDEV）和 `/run/WSL/pulsara-interop-review-303`（EPERM）；源 `/run/WSL/301_interop` 的device63、uid0、mode0777，ext4临时目录device2096、uid1000、mode0700。未成功创建链接，空临时目录已rmdir；普通AF_UNIX O_PATH原理自测的临时目录/文件同样已精确清理。

## 4. Node/Electron实际API与最小生产缺口

[Node22.23.3 child_process API](https://nodejs.org/download/release/v22.23.3/docs/api/child_process.html)与锁定 `lib/child_process.js` 的 spawn选项没有任意Job handle、Job limits、CREATE_SUSPENDED/ResumeThread或PROC_THREAD_ATTRIBUTE_JOB_LIST入口。其 [libuv win/process.c](https://raw.githubusercontent.com/nodejs/node/v22.23.3/deps/uv/src/win/process.c) 自建全局Job明确设置 BREAKAWAY_OK 与 SILENT_BREAKAWAY_OK；不能借用这个全局Job证明普通后代递归拥有。普通spawn在CreateProcess后再加入该全局Job，也不是调用者自选Job的原子admission接口。

本机 Electron44.9.0 包内 `electron.d.ts::ForkOptions` 只提供env/argv/cwd/stdio/serviceName等，未暴露Job/Windows suspended创建；其实际Node版本见环境证据。不能把本次Python ctypes原型直接写入产品launcher，也不能用调用spawn后按PID补Assign掩盖已开始执行的窗口。

若后续选择该候选，必要而小的Windows适配边界是原生helper或native addon：仅为本应用每操作创建不可继承、无breakaway、KILL_ON_JOB_CLOSE Job，启动明确目标的wsl.exe（CREATE_SUSPENDED→Assign→Resume，或受支持的原子Job-list创建），拥有该Job/精确process handles、三pipe/EOF、取消及可核验join。Win32负责机制，WSL仍负责原interop；Pulsara负责每操作admission、授权、取消/unknown与Linux原owner合并。无需复制Windows shell、PTY/ConPTY、WSL协议、调度器或持久恢复框架。

[Windows Job文档](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)和[AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)支持此窄边界，但仍须实测所选shipping helper及Electron打包。WMI/服务/计划任务/外部broker启动不是普通CreateProcess继承范围；这里没有承诺覆盖主动改WSL_INTEROP、删除私有socket或主动外部交接等逃逸动作。

## 5. 尚未闭合的产品条件

最小候选是“每操作专属Windows Job/WSL anchor + 每操作私有opaque Unix byte relay + 原Linux manager仅给该次shell注入relay路径”。实际shell/PTY/root/group/drain仍由原Linux owner创建，不接管共享发行版。这个新增relay有具体未满足要求：WSL无关闭ENOENT fallback的扩展点，纯env和无privilege同inode持久别名已反证；范围只到opaque stream forwarding。

它仍是spec待决项：需明确该窄interop Job/relay与原排除宽native Windows kernel/ConPTY/Job重构的区别，说明唯一owner、创建/取消竞态、权限与副作用、环境probe/MCP/hook各自范围、launch reservation和8槽、开始前取消/reader故障、失联unknown。不得先把研究代码生产import，也不能直接以application级kernel carrier的一个Job取消单个并行任务。

另有启动绑定窗口尚未验证：guest返回的原socket路径与relay实际 `os.open(O_PATH)` 之间，anchor可能已死，路径可能消失或被复用。pin只保证打开之后不再换inode，不能反向证明打开的就是指定Job的那次anchor。生产候选须在任何模型命令可执行前，用当前live owner/精确anchor实例把“已pin的这个socket→指定Job”绑定完整，再开放准入；不能凭路径字符串、inode数字或预检查代替这件事。已有PE经relay自查A/B Job支持本次成功运行的实际绑定，没有覆盖该故障竞态。

同样，relay在半请求/整请求已转发但调用者尚未见回复时死亡，WSL可能已启动PE。该次执行/输入须按原unknown处理、禁止自动重发，并继续核验所持Job实际范围；不能从relay进程已退出、私有socket仍存在或后续connect111推断此前请求未执行。本轮只在故障发生后发起的新请求上观察fail-closed，未覆盖全部半请求时序。

正常完成尤其未测齐：Job包含anchor/wslhost基建。应先封relay新准入并保留socket pathname，Linux原scope确认不能再发请求后正常EOF关闭anchor基建，再保持Job handle等待Windows普通无pipe后代自然退出/范围为空，最后与Linux root/group/drain/worker组合join。不能在Linux已join时直接TerminateJob并把提前杀掉无pipe后代叫正常成功。取消才走明确的终止范围及成员join。这个“正常EOF关闭anchor + 无pipeWindows后代继续自然完成 + 最终全范围空”的组合本轮未测，jobs的空anchor EOF smoke不能代替它。

本轮未覆盖真实terminal/MCP/hook端到端、8槽容量释放、input/PTY/EOT、真实GUI正常Quit、睡眠恢复、外部发行版终止、provider长程或任意主动逃逸后代。没有新增durable事件/receipt/registry/cap，也不改变provider prefix安装边界。

## 6. 交叉审查、清理与复现

已交叉读 jobs代理 `report.zh.md`、`scope-summary.json`、`scope-nopipe-owner-crash-03cfcac1.jsonl`。它在故障前枚举Job全成员并预持精确process HANDLE，故障后逐一wait=0；实际持Job句柄的Python解释器9420被精确强退，排除了venv launcher歧义；observer先关闭Job查询句柄再注入最后owner死亡，避免observer改变KILL_ON_JOB_CLOSE语义。cancel路径另外核ActiveProcesses0，强退路径核Job不能重开(error2)。这些证据支持所声明范围，比仅观察两个PE PID消失更强；实际Node22/Electron44强退和A/B隔离结果也未发现证据矛盾。

自己的路由/relay实验只检查所启动的确切PE与独立scope，不把它们冒充Job所有成员的最终join证明；完整Job结算交叉证据仍引用jobs代理。`verify_cleanup.py` 只读扫描本目录记录，Windows用PID+creation FILETIME及OpenProcess后同handle事实，只有error87按不存在，其他错误保留unknown并使审计失败；Linux用PID+`/proc/stat`starttime。最终 `cleanup-final.json` 覆盖34个Windows身份、33个Linux身份：remainingWindows/remainingLinux/unknownWindows均空。已死后被复用的PID不操作。两个relay目录/Unix socket均在runner结束后精确删除，原始记录保留。没有泛杀、kill共享发行版或碰他人进程。

仓库根复现入口：`.venv/Scripts/python.exe output/windows-g0/interop_review/round2/socket_probe.py`、`pinned_socket_probe.py`、`same_chain_probe.py`、`relay_probe.py`、`verify_cleanup.py`；后几个文件同目录，均生成新uuid记录。`same_chain_probe.py` 当前普通同PGID版本保留了“worker被回收、等不到done”的观察，直接运行可能按其原断言超时，不是产品失败测试。每次须先核对本机目标/路径，不能把这些观察路径当产品常量。

建议仅归档本报告、environment/cleanup、五个小型probe和最终fe6645af/pin36e45fe3/relay4e1c8baf及必要原始失败记录。官方源码可由明确tag URL重取，不需要归档整个sources。未做生产实现或完整G0；当前实质剩余风险是新scope的产品owner/取消竞态、正常无pipe后代join与集成验证，不能宣称Windows支持激活。
