# G0-W-C 正常 EOF 后自然 join：单场景补充

2026-10-25。按 root 新任务，仅新增一个独立场景及证据；未覆盖已冻结文件，未重跑旧矩阵，未改生产代码/spec/配置，未进入 B。

**该正常回收子项通过本次观察。** Windows 父先退、普通子 stdin/stdout/stderr 全 DEVNULL；Linux 工作 runner 和它的 carrier 先退出。随后只向 anchor stdin 送 EOF，保持唯一 Jobhandle 打开，没有 TerminateJobObject、没有提前关闭最后 Jobhandle。anchor 的 Linux 进程和外层/内层 wsl.exe 随 EOF 正常退出；Windows 子仍继续有限工作并自然写 finished。最后 Job ActiveProcesses=0、TotalTerminatedProcesses=0、members=[]，EOF前持有的全部8个精确 process HANDLE 均 signaled，才关闭 Jobhandle。

## 唯一场景与真实次序

新脚本 `normal-join-probe.py` 只导入冻结 `job_probe.py` 的 ctypes/观察函数及其 Windows PE fixture。控制器自己持只设 KILL_ON_JOB_CLOSE、无 breakaway 的 Job；CreateProcessW(CREATE_SUSPENDED)后先 Assign、再 Resume。Job句柄 GetHandleInformation=0。anchor guest通过stdin持有寿命，外来Linux runner只在本次env指定其WSL_INTEROP socket。

完整记录为 `normal-join-4a197629.jsonl`，配套 `.linux.jsonl` 与 `-summary.json`。设备实际时钟原样保留：

| 事件 | 原始epoch时间 | 观察 |
| --- | --- | --- |
| Windows parent开始 | 1792857914.805928 | PID35524，creation134373315147052462，inJobBefore=true |
| 普通 child开始 | 1792857914.9299858 | PID37504，creation134373315148279197，inJobBefore=true；stdio=DEVNULL |
| parent主动先退 | 1792857914.9487011 | parent-early-exit记录 |
| Linux runner carrier已close0 | 1792857914.9686432 | guest runner427/starttime322已不存在，Windows child同一身份仍活 |
| anchor stdin EOF | 1792857915.1554983 | 仅关闭stdin写端，Jobhandle仍开，无TerminateJob |
| EOF后观察 | 1792857915.2515152 | guest anchor328/starttime260不存在；两个wsl.exe的持有HANDLE均signaled；Windows child仍活；Job ActiveProcesses=6 |
| child自然finished | 1792857939.9309566 | 仍为PID37504，本次25秒有限工作完成 |
| 正常范围空 | 1792857940.0696778 | ActiveProcesses=0、members=[]、全部8个持有HANDLE wait=0；此时Jobhandle仍开 |
| 关闭空Job、final cleanup | 1792857940.0696778之后 | cleanupAccounting.ActiveProcesses=0、cleanupMembers=[]；两个Linux精确身份均不存在 |

这次EOF后并非所有Windows基建立即退出：wslhost及三个conhost仍暂存，随后随普通子自然完成而排空。探针没有为求空范围强杀这些进程；完整记录保留了该中间态。`TotalProcesses=11`包含已先退的成员；EOF前仍活的8个成员按下面角色分别预持有process HANDLE。

| 角色 | PID | GetProcessTimes creation FILETIME |
| --- | --- | --- |
| anchor外层carrier，System32/wsl.exe | 36456 | 134373315110815973 |
| WSL内层wsl.exe | 22368 | 134373315111036235 |
| wslhost.exe | 34124 | 134373315140221960 |
| 基建conhost.exe | 7632 | 134373315110873926 |
| 基建conhost.exe | 33964 | 134373315140374122 |
| child配套conhost.exe | 8416 | 134373315148133826 |
| child venv launcher | 32232 | 134373315148065660 |
| child实际Python解释器 | 37504 | 134373315148279197 |

summary 的 held列表携带的是EOF前取得的初始facts，所以其中 `exists=true` 是取得句柄时的事实；最终退出authority为该同一HANDLE的 `waitResult=0`，结合仍持Jobhandle时的 ActiveProcesses=0。没有把旧snapshot的exists字段当成最终状态。

## 资格、清理与复现

本次唯一成功路径没有调用 TerminateJobObject/TerminateProcess、没有Linux信号清理。脚本中失败分支保留明确标记的精确自有Job/精确Linux pidfd cleanup，但本次日志没有进入这些分支。最终只在范围已经空后 CloseHandle(job)。仅留下小型文本记录，没有删除用户目录或触碰共享发行版配置。

Windows控制器使用本项目uv管理 `.venv/Scripts/python.exe`，WSL guest明确Ubuntu/plumwsl与Linux项目Python3.12.15。复现命令：

```powershell
.\.venv\Scripts\python.exe output\windows-g0\interop_jobs\round2\normal-join-probe.py
```

25秒是冻结Windows child fixture的有限负载；20秒startup/runner及40秒自然排空观察都是这次probe诊断边界，不是产品任务寿命cap。

该证据补的是“正常anchor EOF不提前杀无pipe普通后代、随后能自然确认整个Job范围空”的一个子项。**本次没有集成private relay封闭新执行准入、真实Linux terminal manager或socket退休顺序**，也没有测试生产MCP/hooks/8槽，未批准WSL_INTEROP路由或relay的生产owner/所有竞态，不宣称整个G0-W-C通过。冻结原报告中当时未测的正常join由本补充后补这一特定场景，其他未测范围保持。独立relay最新证据以interop_review的 `relay-4e1c8baf-summary.json` 为准；其25秒B负载替代6秒旧轮次的时序资格，旧日志保持冻结。
