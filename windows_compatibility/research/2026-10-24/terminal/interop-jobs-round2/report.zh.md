# G0-W-C：Windows Job 与 WSL interop 第二轮机制实证

2026-10-24，interop_jobs。受测仓库提交 `fb2c57da12d24ddda32da1320db1d7c645b5ebb6`。原始日志时间戳保持设备实际时钟，不重写。本轮只在本目录增加研究探针/证据；未改生产代码、活动 spec、共享发行版配置、settings、数据库；未进入 Phase B，未提交。

**无 breakaway 的 Windows Job 可以真实覆盖本机 WSL interop 的 Windows PE 及其普通后代。** 将 `wsl.exe` 在 `CREATE_SUSPENDED` 状态加入只设置 `KILL_ON_JOB_CLOSE` 的 Job，再恢复线程，本轮 Windows Python PE 和普通后代均自查属于该 Job。取消、Job owner 崩溃、父先退后代不持 pipe、Node/Electron 外层宿主崩溃都有回收实证。此结论纠正了“WSL 一定打断 Job 继承”的初始假设。裸 WSL_INTEROP env 路由有已复现的失效 fallback；review代理随后用私有 AF_UNIX 透明 byte relay 对两个失联边界取得局部正证，仍须验证生产 owner/全面竞态与集成，整个 G0-W-C 未通过。

## 范围与平台

已读根 AGENTS、当前 Windows README/spec §5/§8/§17、G0 清单及首轮 terminal 报告/源码；生产 `terminal_process/manager.py` 和 `environment.py` 仍通过 Linux PGID 信号、root/drain/worker join 结算。没有用实验代替这些 owner，也没有将 Linux PGID 的权威扩大到 Windows。

Windows 10.0.26200.9711；WSL 2.7.11.0；内核 6.18.33.2-microsoft-standard-WSL2；已有 Ubuntu/plumwsl、uid1000。Linux 项目 Python3.12.15；Windows 项目 `.venv/Scripts/python.exe` 实际解释器3.12.13。锁定独立 Node22.23.3；真实 Electron44.9.0 main 自报内嵌 Node24.22.0、libuv1.52.1。`environment.json`、`scope-electron-nopipe-crash-3dc6b797.jsonl.host-output` 保留来源。

所有应用/guest cwd 是 `/home/plumwsl/workspaces/pulsara_agent`；仅小型研究文件/record 放 Windows output，guest 用 `/mnt/c` 写证据，没有将生产 Linux workspace/PG/资源搬到 DrvFs。没有读取凭据，也没有使用 provider。

## 最小机制及真实结果

Windows SDK 拥有 CreateProcess/Job/句柄和 wait；WSL 拥有原生 interop relay/PE 创建；原型只使用 Python 标准库 ctypes 调用这些 API。没有新增依赖、常驻 broker、服务、任务持久状态或生产框架。

每个 Job 仅设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，不设置两个 breakaway flags，也不设置资源/时间/进程数量上限。`CreateJobObjectW` 的 Job 句柄不可继承，最终轮实测 `GetHandleInformation=0`。`wsl.exe` 使用绝对路径、明确 `-d Ubuntu -u plumwsl --cd ... --exec ...`，没有 PowerShell 求值；清除本次 Windows launcher 环境的 WSLENV 清单，Linux interop/PATH/automount 保留。

原型次序：CreateProcessW(CREATE_SUSPENDED) → AssignProcessToJobObject → 记录归属 → ResumeThread。恢复前断言 guest/PE 就绪记录尚不存在。PE 仅为诊断临时 OpenJob/IsProcessInJob，并立即 CloseHandle；因此 crash 测试时没有诊断 observer 偷留 Job 句柄。

| 实验 | 实际证据 | 结论边界 |
| --- | --- | --- |
| 原生 Windows 根/普通后代正控 | `matrix-summary.json` 中 native 的 cancel/crash/rootexit-crash 三项 | Job API fixture 自身可工作 |
| 仅把 wsl.exe 加 Job，PE 不显式入 Job | 同 summary 的 carrier 三项，根/子 `inJobBefore=true` | 本机真实跨 WSL 继承成立，不是 PE 自愿 enroll 掩盖问题 |
| PE 显式入 Job | 同 summary 的 enroll 三项 | 协作 adapter 可工作，但前一行已证明本路径不需要它；不建议为任意 PE 新增拦截层 |
| TerminateJobObject | `scope-cancel-b5ba8159.jsonl`，故障前持有全部11成员的精确 process HANDLE，逐一 wait=0；owner 另观测 ActiveProcesses=0、members=[] | 比“已发送取消”“两个 PID 不见”更强的本次范围结算 |
| 直接杀实际持 Job 句柄的解释器 | `scope-nopipe-owner-crash-03cfcac1.jsonl`：实际 owner9420 / creation134373308894313022；其余3成员预持有 HANDLE 全 wait=0，Job reopen 错误2 | 排除 Windows venv launcher 和实际 Python PID 不同造成的歧义 |
| 父先退、后代不持任务 pipe | `scope-nopipe-crash-49072130.jsonl`，子 stdin/stdout/stderr 全 DEVNULL，普通 spawn、无 detached/breakaway | Windows 根、Linux root/carrier 已先完成，Job 仍拥有后代；仅依赖 pipe EOF 会漏掉它 |
| 实际 Node22 外层强退 | `scope-node-nopipe-crash-4604282f.jsonl` 及 host-output | 真实 libuv 全局 Job 周围的原型组合可回收；没有只据文档推断嵌套 |
| 实际 Electron main 外层强退 | `scope-electron-nopipe-crash-3dc6b797.jsonl` 及 host-output | 使用锁定 Electron 的独立 main，无 BrowserWindow/生产 main/DB；外层 main 死亡后全部预持有成员 HANDLE signaled，Job 已销毁 |
| 两个独立范围 A/B | `isolation-A-fdb10f09.jsonl`、`isolation-B-572d5ad8.jsonl` | A取消后 B的11成员 HANDLE 全 wait=258（仍运行），之后单独取消 B并确认范围空 |
| 专属 anchor 基础握手 | `anchor-summary.json`：stdin EOF、Job取消、owner强退三项 | guest返回 uid1000、专属 `/run/WSL/*_interop`、PID+starttime；EOF 后 Job snapshot=[]；三项最终 guest 确切身份均不存在 |

`scope-summary.json` 是上述强化场景的最终索引。取消时可以在 owner 还持有 Job 句柄时查询 ActiveProcesses=0；强退最后 Job owner 时 observer 必须先关闭自己的 Job query handle，否则会改变“最后句柄关闭”的故障。强退场景使用故障前全成员 process HANDLE 的 wait 和故障后 named Job无法 reopen(ERROR_FILE_NOT_FOUND=2)联合观察；没有声称收到了未安装的 ACTIVE_PROCESS_ZERO 通知。

所有等待25秒的 child 是有限实验工作负载；startup/wait 的5/10/20/30秒只用于诊断 probe 卡死，均不是产品寿命或任务总量 cap。小数组初始容量不足时从 OS 返回的成员数扩容，不构成进程数量限制。

## 为什么与首轮 Node 崩溃反例不同

[Node22.23.3 锁定 libuv 实现](https://raw.githubusercontent.com/nodejs/node/v22.23.3/deps/uv/src/win/process.c) 的全局 Job 同时设置 `BREAKAWAY_OK` 和 `SILENT_BREAKAWAY_OK`，仅明确加入 uv_spawn 的非 detached 进程；普通后代可以自然脱离其全局 Job。首轮普通 .NET 后代存活与此不冲突。不能把 Node自带 Job 当作递归范围，也不能因首轮反例断言 Windows Job整体无用。

[微软 Job 文档](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)描述普通 CreateProcess 后代默认继承及最后句柄关闭的行为；[AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)描述现代 Windows 的嵌套条件；本轮使用的成员列表/ActiveProcesses来自[QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject)。这些 API事实由本机观测补足，不把文档承诺直接当 G0通过。

[WSL2.7.11 interop.cpp](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/interop.cpp) 和 [SubProcess.cpp](https://raw.githubusercontent.com/microsoft/WSL/2.7.11/src/windows/common/SubProcess.cpp)中的直接 PE创建沿 Windows CreateProcessAsUser 路径；已核查路径不自动提供每 Pulsara Linux操作的 Job owner。review代理另查 svccomm/relay 路径，解释为什么由本次 wsl.exe承接的 interop 能继承 Job；其独立 socket路由实验见同轮 interop_review 报告，不能遗漏其负例。

## 不能据此进入 Phase B 的缺口

1. 每应用只有一个 kernel carrier 的 Job 最多给出应用范围；取消其中一个模型操作不能直接终止整个 kernel/所有并行工作。A/B正控是两个独立 carrier范围，尚不是生产 terminal manager的每操作集成。
2. 专属 anchor + 裸 WSL_INTEROP env能让外来 Linux父链选择某个 Windows relay；但 review代理已实测：指定 socket删除后 WSL可以 fallback到其他 relay，PE仍执行且不在 A/B Job。预先检查 socket存在不是修复。随后[独立 byte relay 证据](../../interop_review/round2/relay-ca30a7c3-summary.json)验证了更窄候选：0700 ext4私有 AF_UNIX socket保留路径，透明转发字节且用 O_PATH pin upstream inode，不解析WSL协议；anchor死亡后的尝试返回1且无PE记录，relay被SIGKILL后socket仍留存、连接拒绝返回1且无PE记录，独立B正常完成。这只闭合已测两个故障时点，尚不能推广为所有创建/取消竞态的生产 fail-closed资格。
3. 本轮没有指定可以修改现行产品规格的最终机制。若要引入 anchor/native adapter，需要先说明每操作 owner、创建/取消竞态、跨域失败/unknown结算、权限与 side-effect授权、现Linux PIPE/PTY/MCP/hook边界，再做受影响真实集成。当前探针不能成为生产 import。
4. 没有测 WMI/服务/计划任务/远端 broker 等另行创建进程范围、任意主动逃逸程序、休眠恢复、发行版外部终止；也没有做真实产品 GUI正常Quit、provider长程或完整 terminal8槽/MCP/hook集成。本轮的“普通后代”仅指已测 CreateProcess/标准 subprocess链。
5. 真实 Electron main实验只验证这套原型的物理回收组合，没验证生产安装/打包native API接入；没有引入 Windows Python作为产品依赖，也没有照搬 SDK实现。

## 复现、原始失败与清理

从仓库根用本地 uv管理 `.venv` 执行：

```powershell
.\.venv\Scripts\python.exe output\windows-g0\interop_jobs\round2\job_probe.py
.\.venv\Scripts\python.exe output\windows-g0\interop_jobs\round2\job_scope_checks.py
.\.venv\Scripts\python.exe output\windows-g0\interop_jobs\round2\anchor_smoke.py
.\.venv\Scripts\python.exe output\windows-g0\interop_jobs\round2\verify_cleanup.py
```

路径/版本是本机已观察值，复现者须显式核对自己的目标，不能将其当产品固定路径。所有记录名带新 uuid，避免读到旧就绪文件。

`matrix.log` 保留第一次探针的错误假设断言：当时预期 carrier Job不传入，实际两个 PE均 inJob=true，因此断言失败。原始记录 `carrier-cancel-90a88fd7.jsonl`显示真实正向结果及最终精确清理，没有删除/重写该失败来求绿。随后把待验证假设改为被实证支持的继承事实并独立重跑9项，不改生产断言。

Windows清理只对本次日志确认的 PID+GetProcessTimes creation FILETIME，OpenProcess后再比对，并通过同一个 process HANDLE TerminateProcess/Wait，拒绝PID复用误杀。OpenProcess只有错误87按不存在记录，错误5等为unknown，不能推断死亡。最终只读 `cleanup-final.json`/`cleanup-final.log`覆盖268个 Windows身份和59个 Linux PID+`/proc/stat`starttime：remainingWindows=[]、remainingLinux=[]。这些计数含重复试验和更早原始记录，不是产品规模限制。

没有停止/注销/重启发行版、修改interop或用户配置、泛杀进程、碰其他目录/DB。隔离 `electron-probe-profile` 仅是本轮 Electron自身生成的小型资料，保留原地；未删除上轮BrowserOwner被策略拒绝的目录。

小型可归档清单见 `evidence-list.md`。任务路由与relay生产 owner/完整竞态和集成资格仍待验证；未宣称 Windows支持激活。
