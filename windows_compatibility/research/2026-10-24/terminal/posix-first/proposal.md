# Phase A：先交付POSIX接口抽取与回归

本轮只读复核现行spec及生产`terminal_process/{manager,environment,models,output,monitor}.py`、MCP/hooks/screenshot进程调用和既有测试定位。没有改根spec/生产，没有运行测试或G0。用户本轮明确授权改变“所有抽取必须等G0全过”的原顺序；A可先做Linux/macOS，B全面Windows仍需G0通过。

## 1. 可独立交付的A

Phase A应是**Linux/macOS现有能力等价的生产hard cut**，不是为了Windows先搭一组空接口。可以单独合并/交付并继续使用当前POSIX安装包，无需等待Windows库/pg_search/NTFS证明。每个切片必须实际接齐其消费者并同次删除被替代的POSIX路径；不得提交“typed接口已加、业务仍用旧fd/Popen”、旧新双执行、兼容aliases或默认fallback。

A不宣布Windows支持，不创建空`_windows.py`、Job/ConPTY伪实现、Windows参数占位、runtime capability列表或用户可选backend；不改变tool schema/SYSTEM/command语义，不增加任何event/subject/guard/relation/receipt/hashregistry或累计任务寿命上限。不为新模块先引入Windowsnative依赖和PS7资源。尚未支持的平台应在真正需要OS实现的factory处明确unsupported，不能退到POSIX假装可用。

原spec未来Windows文件树继续作为B计划；A只创建已被POSIX生产caller使用的共同入口与POSIXleaf。公共接口表达当前真正可观察事实，私有方法签名可在G0证据出现后按相同产品合同调整，不把本阶段的API名字当外部稳定SDK或包格式。

## 2. 继续共用的终端业务

`manager.py`仍唯一拥有ProcessRegistry/_ProcessState、process_id与Host/child授权、foregrounddecision/adoption、launchreservation、8槽容量、terminal状态、terminationintent、readonly观察、输出订阅/retention触发、reader/watcher/Timer安装与组合join、physical_completion和completion subscriber。`output.py`仍唯一sanitizer/cursor/GAP/artifact来源；`monitor.py`仍唯一best-effort观察与唤醒owner。`ports/terminal.py`、tool_runtime与权限规则不因抽取换产品。

当前reader/watcher/Timer部分安装标志及rollback不能散到多个owner。只有平台OS资源移到physical对象，业务`PHYSICALLY_JOINED/PRUNABLE`不移到leaf。继续区分root退出、受管范围存在、输出结束、reader/watcher/timer回收；只有组合条件满足才释放容量/发布completion。不要把POSIX当前某一等待次序写成未来ConPTY唯一顺序，持续drain要求保留。

## 3. 最小进程范围接缝

建议落点仍是根扁平窄模块`process_scope.py`与`_process_scope_posix.py`。A只开放已有caller确实需要的操作，不创建通用service/registry或全部subprocess参数的镜像。

### 3.1 共同事实与控制操作

以下是语义与可用方法名建议，不是发布稳定API；实现可用等价封闭类型：

| 操作 | A必须定义的含义 | 不可暗示的含义 |
|---|---|---|
| `poll_root_exit() -> int | None` | 原root对象当前退出码；保留POSIX原生实际数值/负signal编码 | 非null不代表整个范围空、输出drain或业务完成 |
| `wait_root(timeout/deadline)` | sync版本等待root；async版本可await；保留caller原单操作deadline与异常 | 不给整个任务增加总wallclockcap，也不隐含等所有后代 |
| `range_is_live() -> bool` | 当前该执行owner持有的受管范围是否仍有成员/不能确认空；POSIX内部可用原groupprobe | 不是任意process-tree扫描，不是worker全局liveness，更不是“所有被命令间接触发的系统进程” |
| `request_stop()` / `request_force_stop()` | 向原范围发当前TERM/KILL语义请求；已消失与permission/error按原caller可判定结果处理 | 请求返回不等于已杀/已join；不在primitive内部固定hook/MCP/terminal统一宽限期 |
| `close_resources()` | 对自己仍持有的OS资源幂等释放，检查状态/异常；不释放别人的端点或借用 | 不偷偷发新signal、后台重试、不丢失live范围的原清理责任 |

PID/PGID、Popen/asyncio.Process、signals、fds都在POSIXleaf及其私有绑定中；若现行DTO确实需要PID作诊断，只给诊断观察，不能把PID重新传入`adopt(pid)`重建执行权。不能让`ProcessScope(pid)`成为可公开构造的跨平台scope：root handle与scope必须来自同一次受管创建并一直归同一owner。Windows未来是否能满足这些事实仍由G0决定。

### 3.2 同步与异步创建

不强制一个返回类型同时伪装Popen、asyncio.Process、ReadFile和ConPTY：

- terminal/probe当前用同步创建和blocking byte reads，保留同步managed handle。
- MCP/hooks/screenshot当前用asyncio创建/StreamReader/StreamWriter/tasks，保留awaitable创建和异步端点；两类只共享scope语义和POSIX原语，不把blockingwait塞eventloop。
- 创建入口可以分`spawn_sync_exec`、`spawn_async_exec`、`spawn_async_shell`，或用少量已使用的等价方法；参数仅收完整argv或已选shellcommand、cwd、已审查env和现行所需stdio/limit，不能靠`**kwargs`把任意SDK开关泄露给业务。
- A的POSIX创建仍是当前支持的`Popen/asyncio` + `start_new_session=True`。公开契约说“已持有该执行的受管范围”，不把`start_new_session`参数本身当portable保证。Job/JOB_LIST/suspended/resume、Windowsconsole/PATHEXT等参数全部B/G0再定。
- MCP/hooks的现有credential_guard必须覆盖**真实OS创建、取消时取回仍在运行的spawn以及安全publication**；factory不能返回lazyplan后guard外启动。异步spawn被取消时必须shield/retrieve同一attempt，不能启动第二次，也不能丢掉root/scope。

共享scope不是共享业务完成policy。MCP有frame/stdio/SDKsession和2秒等现有边界，hooks有事件/deadline/输出，screenshot有communicate与renderdeadline，probe有singleflight/snapshot；A不把它们一律改成terminal的“groupempty+outputfinalize才tool完成”，不改env继承，不把调用者都计入terminal8槽。不在“复用”中默默改变非终端的成功、timeout或close语义。

## 4. terminal物理I/O最小接口

`terminal_process/physical.py`提供同步terminal当前需要的typed返回，`_physical_posix.py`提供真实PIPE/PTY操作；它复用前述ProcessScope控制，不复制killpg框架。

| 操作/字段 | A的必要行为 |
|---|---|
| `io_mode` | 当前PIPE/PTY枚举，不新增capability negotiation |
| `read_chunk(maximum_bytes) -> bytes` | 原`os.read`增量读取，空bytes表示本来源EOF，异常保留原结算；不在BufferedReader.read(n)等待凑满而丢实时性 |
| `write(payload: bytes)` / `flush_input()` | 保留当前原样UTF8/write与submit换行的已写/未写/不明副作用边界；底层短写/异常不得假装完整成功，不后台继续发送未确认尾部 |
| `end_input()` | PIPE关闭写端；POSIXPTY按现行canonical EOT+退休写副本；不关闭仍需drain的读端，不保证任意rawmode程序EOF |
| `root/scope`观察与stop | 转到同一次创建的受管ProcessScope，不允许只存数字PID再找回 |
| `close_input_resources()` / `close_output_resources()` / `join_backend_workers(...)` | 只管理leaf拥有的byte/native资源；A若无额外worker，可不公开空worker接口。terminalreader/watcher/Timer继续由Registry组合join |

命名`end_input`可避免把POSIX EOT硬抽象成“所有平台都已提供真正halfclose”。对模型的现行close_stdin合同和POSIX真实cat测试保持不变；Windows是否可支持必须在G0-T证明，不能因A有此方法就声称ConPTY也能做。A不添加unsupported typed模型结果，不悄悄改现有工具面。

## 5. shell与environment的A范围

`shell.py`与`_shell_posix.py`只抽现行shell选择/调用计划：

| 操作 | A保留 |
|---|---|
| `detect_shell(parent_env) -> shell descriptor` | SHELL→zsh/bash/sh等现有候选、可执行性/绝对路径判断与失败行为；不切换已接受owner的shell |
| `prepare_command(shell, text) -> invocation` | 当前真实`(shellpath, '-c', text)`；每次独立shell、workspace/workdir规则不改 |
| `prepare_probe(shell) -> invocation` | 当前`-l/-i/-c`及sentinel/env-0脚本；probe初始env仍来自环境owner的受限policy |
| invocation生命周期 | typed不可变argv/数据；A没有PS临时载荷，不应为未来Windows给所有POSIXcommand造临时文件/contextmanager空资源。B确有载荷时由计划→physicallaunch明确转移所有权 |

`environment.py`继续拥有TerminalEnvConfig/defaultdeny/passthrough、nearestvenv与PATH组织、snapshotkey/TTL/singleflight、probeattempt状态与cacheadoption。POSIXselector/os.read/probeprocess操作可以放`_shell_posix.py`的窄probe执行件或`_physical_posix.py`的pipe读件，避免共同environment残留OSfd细节，但不要复制一套probe业务owner。

probe必须保留spawn与attemptpublication在环境owner同一锁下的线性化：close或者先封准入，或者看见原process并等待其join。不要把它替换成`run_probe()->bytes`黑盒，以至close无法掌握精确inflightprocess；也不要让leaf在owner锁外晚到spawn。只在EOF、root成功、范围空且reader/native资源已收回后adoptcache；失败仍为原PROBE_TIMEOUT/OVERSIZE/FAILED等，不额外加重试/寿命cap。

## 6. 资源handoff、rollback与lifetime

每次launch必须清楚以下区间，不能只定义“返回一个handle”而遗漏异常：

1. **OS创建前**：原caller持有admission/guard、launchreservation、准备的argv/env。取消或参数失败无用户代码运行，按原结果释放本次reservation；没有新资源authority。
2. **OS已创建、handle未完整返回**：leaf拥有已开fd、root、scope、stdin/stdout等；任何后续dup/fdopen/streamwrap/Thread构造失败都必须终止并join已启动范围、关闭自有资源。若无法join，必须把该仍活的physicalowner交还原caller的隔离/收尾路径，不能raise普通spawnerror后让Registry释放reservation并遗失实际进程。
3. **handle已返回、_ProcessState尚未公开**：Registry/caller拥有它，启动reader/watcher/Timer、输出subscription与closed-during-launch等原安装步骤；任一失败走同一精确rollback，不重启/重发。
4. **已公开**：只有原process_id/Hostscope可输入/终止；endinput/kill/naturalexit/ownerclose的原线性化不变。input资源退休后不可被后继launch复用，scope不被另一owneradopt。
5. **物理结束**：root/range/byte/worker组合事实成立后，原ownerfinalize输出/终态，置physical_completion，之后才prune/releasecapacity；close后重复close幂等，结果不明仍保留责任，不卸载旧实现或GC掩盖活任务。

当前manager在`_spawn`抛出前会释放launchreservation，而`_spawn`内Popen后还有fdopen/Thread构造；A新资源边界必须明确这一段partialfailure如何回收/交还，不能把旧内联代码机械包成会丢进程的constructor。既有post-spawnrollback/quarantine仍复用，不新建receipt/lease/globalorphanregistry。

## 7. A内部切片顺序（每片POSIX真实可用、旧路径同次删）

| 切片 | 生产改动 | 退出条件 |
|---|---|---|
| A-T0 基线与映射 | 确认当前git/settings/支持矩阵、标出直接和间接rawprocess调用；读取本proposal所列现有断言 | 基线仍是当前POSIX语义；仅研究不把测试报告当新durabletruth |
| A-T1 POSIX scope | process_scope + _posix；先让一个同步caller（probe或terminalphysical）实际消费，再接需要的异步创建边界 | 同一scopeexact创建/控制/回收，guard不离开真实spawn；不用空接口提交、没有Win占位 |
| A-T2 terminalphysical | PIPE/PTY byte端点移入_posix，Registry状态/reader/watcher/Timer与join保留；_ProcessState持ownedhandle | realPIPE/PTY交互、leader先退出/后代、rollback与容量不回归；删manager被替代raw操作 |
| A-T3 POSIXshell/probe | 原shell候选、argv、probe读件提取；environment保留policy/attempt/cache | 独立shell/cwd、defaultdeny/venv、singleflight/timeout/closejoin相同；无PSpayload/foreignshell |
| A-T4 非terminal消费 | MCP、hooks、screenshot逐个接同一scope/创建机制，原SDK/streamtasks/env/deadline保留 | 每caller真实取消/close/guard/输出/失败合同通过，旧OSstop/spawn分支同片删除；不合并其业务slots |
| A-T5 收口与交付 | import/raw引用闭合、docs、现有POSIXresource/buildcache输入、Linux/macOS整体验证；与文件/IPC专题的A接口片对齐 | 当前macOS/Linux安装与工作流可独立交付；无新增Win支持声明。A完成后再按G0结果进入B |

A-T1–T3可按最小依赖调整顺序，但不能先合公共接口再长期留旧caller；A-T4每个caller可独立切片，不必同时重写所有外部进程流。typed安全文件binding/私有IPC/Node资源抽取由对应专题给A计划；终端A不要求同时引入Windowsauth/PGTCP/ConPTY/NSIS。

## 8. 当前测试与真实POSIX故障矩阵

不为每个新转发方法写镜像单测；复用已证明业务/真实OS失败的现有测试并只补新handoff边界缺口。macOS arm64和现有Linux x64/arm64目标按现有支持面验证；当前机器的macOS结果不能当Linux结果。不得skip原有难测物理断言。

| 真实行为/故障 | 现有测试文件/定位 | A验收 |
|---|---|---|
| 增量PIPE/PTY、EOF、无换行尾部、游标/artifact/保留 | test_round2_terminal_output.py；test_round2_terminal_monitor.py:81；test_round2_terminal_output.py:607猫程序EOF | 原始byte来源/UTF8sanitizer/游标/GAP和真实EOF不变，不依靠echo即判回答完整 |
| root退出后同group后代持stdout或关闭stdout仍活 | test_round2_terminal_output.py:334,390,426；test_pr03_process_termination.py:125 | 仍可stop，capacity/monitor/wait不早完成；不把scope换成只看Popenpoll |
| 同时launch到8、spawnfail、ownerclose跨spawn | output.py:452,513,541 | reservation线性化正确，第9未执行；无尚活未计槽的scope |
| postspawn reader/watcher/timer/subscription安装失败 | output.py:745及:837；新增精准覆盖Popen后dup/fdopen/constructor失败 | 每个异常点真实root/group/端点可回收或回到原quarantine；不是仅assertmockmethodcalled |
| kill与自然结束竞态、结果ready前adoption失败 | test_pr03_process_termination.py:46,89,156 | 不改已冻结自然终态，不把晚kill报成新终止，未发布process不遗失 |
| Host/childclose、重复取消、exactowner、joinfailure隔离 | test_stage2_terminal_host_lifetime.py:220,339,389,455,649 | 原closejoin与sameHost身份不变；抽取不引入新globalprocessregistry |
| write/submit延迟reply、观察取消后的输入结算 | test_terminal_cognitive_subtraction.py:136,201 | noresend；rawinput/换行不变；不加后台写重试或总时限 |
| 环境defaultdeny、venv、shellsnapshot | test_round2_terminal_environment.py全文件，尤其:78,144,209,252,294,339 | PIPE/PTY相同净化env；probe非零/oversize/timeout不cache；close等exactprobe已join |
| monitor注册完成race、冻结/投递/expiry、handoff | test_round2_terminal_monitor.py:321,386,457,534；safe_point与architecture测试 | 只有原physicalcompletion触发；无epochprefix变化，无额外durability |
| MCP blockedstdin、TERMignore与close | test_mcp_transport_lifecycle.py:66；test_round6_mcp_production.py:1563,4416 | 保留frame/SDK/streamtask与既有close结果；真实process退出，不第二次spawn |
| hookcredentialrotation与spawncancel/timeout | test_round9_2_hook_subsystem.py，尤其:1642；hook集成测试 | credential_guard在最终真实sink仍拒绝变更secret，取消不遗失asyncspawn |
| screenshotworker与Chromium回收 | test_visualization_preview.py:91等真实preview测试 | render取消/超时后worker树/管道退出，保留当前error/output行为 |

还应做一次同一POSIXHost多轮长任务与PIPE/PTY交互、compactionhandoff、childclose周转，观察进程/线程/fd是否回到基线；使用既有长程/dogfood合同，不为证明抽象新增总轮次或总运行时长cap。真实provider如当前active合同要求，仍用保存生产配置只读注入；本轮编写spec不执行它。

## 9. 不提前锁死WindowsG0

A冻结的是原有POSIX外部行为、资源所有权/移交、接口所表达的事实及非重复业务owner；不冻结Win32method签名、ConPTY文本/字节桥、JobList/suspend策略、WindowsEOF能力、helperIPC、PS载荷文件、SCRAMdatafile、NTFSbinding/ACL实现或Windows打包recipe。B/G0可在不改变这些外部合同的前提缩小/调整私有接口；若真正产品语义改变仍先修spec。

尤其不能把`pid==pgid`、`killpg(pid,0)`、Unixfd可select、EOT等于半关闭、shell`-c`、rootwait同步线程模型写进portable共同合同。它们是A_POSIXleaf实现事实；未来Windows无法满足某项外部行为时，按原G0失败路径停止并修订，不能说“A接口已有该方法所以算通过”。

建议根spec同时修改原3.1首句/3.5/4节/11节/11.1/末节所有全局“G0先于接口抽取”措辞，明确A独立进入/退出条件及B仍G0前置，避免只新增PhaseA表而旧句继续否决A。A阶段不是新的运行时phase字段或持久job，只是可独立review/交付的工程顺序。

## 10. A的本专题净人日拆账

若A纳入上述terminal/scope/shell与MCP/hooks/screenshot全部消费者，净估**8–14人日**：POSIXscope+terminalphysical/Registryhandoff3–5，shell/probe1–2，非terminal逐caller创建/close接入2–3，Linux/macOS物理回归与故障收口2–4。这里来自原W1/W2/W7预算，不在原88–154人日之外叠加。

若根spec把MCP/hooks/screenshot明确列为A条件扩项，必选terminal/scope/shell及真实POSIX回归可拆为**6–11人日**，扩项**2–3人日**。此时A只对已接入caller声明完成，不能同时声称所有非terminal平台分支已消失。上述数字不含backend安全文件binding/其消费者、desktopIPC/资源/cache/安装验证，不重复计算其他专题。必要partialspawncleanup包含在接缝内；新发现的独立产品语义问题另议，不用重构预算无限吸收。
