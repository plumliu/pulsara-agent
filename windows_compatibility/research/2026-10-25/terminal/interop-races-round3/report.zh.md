# G0-W-C 第三轮：首次绑定、半请求与取消次序

2026-10-25，interop_races_r3。受测两份 clone HEAD 为 `671f8fd21da5ac4bdc4190988d2c294395faa036`。只写本目录研究文件，未改生产/spec、未提交、未安装产品、未进入 Phase B。已读根 AGENTS、现 Windows spec §5/18、round2 interop-jobs/interop-route 及 PG 交叉审查。使用 ponytail 收敛标准 API 小候选。

**首次 pin 可前移到 Job 所拥有的 Linux anchor 内，再经真实 SCM_RIGHTS 一次转移 O_PATH fd；所测取消门需要组合 Windows Job 与原 Linux PGID，不能只等 Job 空。完整请求可能已执行但调用者失联时仍是 unknown。旧调用者尚可能使用路径时 unlink 私有 socket，会重新打开 WSL fallback。** 以上是机制子项证据，完整 G0-W-C 仍未通过，研究代码不得生产 import。

## 所有权与环境

Windows API 拥有无 breakaway、KILL_ON_JOB_CLOSE Job、suspended 创建/Assign/Resume、精确 HANDLE 与 Job 统计；WSL 原实现拥有 interop PE 创建及后续 vsock；Python 标准库仅负责 AF_UNIX fd 转移、opaque 双向 byte forwarding 和故障门。Pulsara 原 Linux manager 仍应拥有实际任务 shell/PTY/PGID、root/group/drain/worker join，实验没有实现替代 terminal manager、WSL parser、receipt、持久恢复或框架。

`environment.json`：Windows Python3.12.13，Linux 项目 Python3.12.15，Ubuntu/plumwsl uid1000，WSL2.7.11/kernel6.18.33.2；Linux cwd 为 `/home/plumwsl/workspaces/pulsara_agent`。私有 socket 在该 clone 的本轮 ext4目录；findmnt 明确 `ext4 /dev/sdd /`，目录由 mkdtemp 创建0700。Windows output/DrvFs仅放小型探针和证据。interop/PATH/automount、共享 distro 和 saved settings 均未更改；无 provider/PG 调用。

复用已归档 round2 `job_probe.py`/`socket_probe.py` 的 Win32 小 fixture；本轮改写其研究模块的输出目录而未修改原文件。WindowsPython仅为研究工具，不是 shipping 依赖决定。

## 首次绑定：anchor 内 pin 后传真实 fd

`fd_relay.py` / `fd_binding_probe.py`，最终 `binding-98a81767-summary.json`；较早四门正控 `binding-89374714-summary.json`。

最小次序为：

1. Windows owner 创建私有 Job，wsl.exe 在 CREATE_SUSPENDED 时 Assign，再 Resume；断言 guest ready 尚不存在。
2. 该 wsl.exe 启动的 Linux anchor 读取自己的真实 WSL_INTEROP，自己 `os.open(O_PATH|O_CLOEXEC)`，不是另一 runner 之后按旧路径打开。
3. anchor 经本次0700目录内 AF_UNIX 控制连接，以 `sendmsg(SCM_RIGHTS)` 一次传实际 Linux fd；receiver 用 `recvmsg(MSG_CMSG_CLOEXEC)` 接收。没有把 Windows pipe 中的整数当 Linux fd。
4. receiver 用 SO_PEERCRED 的 PID/UID，先持 pidfd，再核对这次 anchor 已直接报告的 starttime；门开启前核 pidfd 未退出，然后才 bind 独立 interop socket。数据面只连接 receiver 自持 fd 的 `/proc/self/fd/N`，不重新打开原 upstream 路径。
5. 正控 PE 经该 relay 启动；Windows owner 对精确 creation 的 process HANDLE 查询自己的真实 Job HANDLE，确认 membership。inode只作诊断，未当 owner authority。

| 确定性门 | 观察 | 本门结论 |
| --- | --- | --- |
| anchor pin 前取消 | Job 全范围空，未向 receiver 发送 fd | 无 operation admission |
| pin 后/send 前取消 | 已 pin 的 anchor 退出，receiver 未收到 fd | 无 operation admission |
| send 后/bind 前取消 | receiver 已收到 fd；取消后 pidfd 可读，拒绝 bind | 无 operation admission |
| anchor live | 精确 peer/FD 接收后 bind；PE 属自有 Job、非另一 Job | 正向绑定成立 |
| 成功 bind 后再取消 anchor | 后续请求经保留 relay 路径连接固定旧 fd失败，无 PE记录 | 未 fallback；不追认旧请求未执行 |

此前 `races-d7967837-summary.json` 的 wrongPinBinding 刻意将预期 A 的 relay pin 到真实 B。Windows owner 用精确 HANDLE查 Job 后拒绝 admission；它证明仅凭 path/inode不够，**不是同一路径被内核复用的穷举试验**。最终采用的更窄候选直接在原 anchor 内 pin 并转移对象，避免这个重新 open 窗口。

receiver live 检查与 bind 之间仍有进程死亡可能；此时已转移 fd仍固定旧对象，连接失败而非换路由，但产品唯一 owner 的 cancelled/admission 状态仍须在提交任务前结算。本轮没有把 pidfd live 瞬时事实当取消授权，也没有证明对同UID主动篡改环境/目录或主动外部 broker逃逸的安全边界。

## 字节门与 unknown

`opaque_gate.py` / `race_probe.py`，最终 `races-d7967837-summary.json`。

| 门 | 原始事实 | 分类 |
| --- | --- | --- |
| first read 后、任何 forward 前杀 relay | 已读494 bytes，尚未发给 upstream；本次PE无记录；真实WSL调用报 accept4 110 | 仅 fixture 知道未转发；不推广成任意失联未执行 |
| 仅转发首1 byte 后杀 relay | raw gate total=1；无PE记录，WSL调用失败 | unknown；禁止由缺少PE记录推定未执行/重发 |
| 首完整块转发后杀 relay和真实调用者 | 493 bytes已发；Windows PE已写 pe-start，持其精确HANDLE；调用者无 caller-exit | unknown，未自动重发；取消后该PE HANDLE wait0，Job ActiveProcesses0 |

首跑 `run-1.log` 曾试图挡住 Unix stream reply，但本机真实 WSL 创建请求通道没有返回字节：PE已经执行/完成，relay connection-complete而无 replied。这与 round2 所查后续 vsock分工一致。reply门超时原样保留，未捏造“挡住reply”的成功，最终改为在已转发请求后切断实际Linux调用者。未操作或实现 WSL vsock协议。

## 取消必须组合两个物理域

`job_cancel_probe.py` 的单 Job 探针与最终 `--combined` 分开保留。

先 `server.close()` 停止 accept、保留 filesystem socket，再 TerminateJob，Windows ActiveProcesses0；如果已整块转发的请求来自另一 Linux父链，**真实 Linux Python caller及其 `/init` binfmt子仍可能等待**。`jobcancel-run-2.log` 原始20秒超时和 `jobcancel-02dfb85d-summary.json` 的 typed probe-20s-timeout-unknown 保留此事实；之后按 pidfd精确回收本次父子。Job 空不能替代 Linux join。

最终组合为 `jobcancel-5d292f5f-summary.json`，三门均通过：

1. 将本次 Linux caller 放入独立真实 PGID，ready报告 PID=PGID=SID。
2. relay `server.close()` 后记录 admission-sealed，确认 socketStillExists；已有连接保留，不再接新连接。
3. TerminateJob，并对故障前持有的所有 Windows成员 HANDLE wait，核 ActiveProcesses0、members=[]；after门还持有实际 PE精确 HANDLE，wait0。
4. 对本次已绑定 Linux PGID 执行真实 `os.killpg(SIGTERM)`，持当时成员 pidfd等待；本轮三项无需 SIGKILL，Python root和 `/init` 子均 pidfdReadable，非僵尸group=[]，实际Windows wsl.exe调用者已join。
5. 封闭后的旧路径新请求均连接失败且无PE记录；所有本次调用者收齐后，最后精确终止 relay，unlink/rmdir 本次私有路径。

before/half/after三项分别 PGID492/505/518，每组两个故障前持有pidfd的Linux成员；after实际PE精确HANDLE wait0。`combinedLinuxGroup=true`、`passedWindowsCancelAndSealGates=true`、`linuxCallerJoinedWithoutExplicitCleanup=true`，最后一个字段只表示没有再落入20秒异常兜底；正常取消本身已显式调用上述 Linux group owner。

这与原 Linux manager 的责任边界一致：取消需要组合双方，不是 Windows Job破坏了原Linux owner，也不是增加等待上限。2秒/20秒等仅有限实验观察和卡死诊断；没有产品任务总寿命、重试或调用数 cap。没有用 TerminateJob冒充正常完成。本分支没有实测完整正常 EOF/no-pipe自然完成组合，仍由相邻terminal集成证据单独负责。

## socket 退休反例

同 `races-d7967837-summary.json`：旧 caller已读取环境中的 relay路径，在 PE调用前停门。relay已死亡时保留该 socket inode/path，放行旧 caller：真实 `UtilConnectUnix ...111`，调用码1，无PE。再次创建同样旧caller，**先 unlink，再放行**：PE实际执行且 `inA=false,inB=false`，返回0。

因此不能以“listener已关闭/Job已空/relay已死”单独授权 unlink。必须先由本次所有相关Linux owner完成join，证明没有仍可尝试该环境的旧普通调用者，再退休路径。实验反例仅为确定这条必要次序；没有让逃逸PE持久运行，已自然完成并精确审计。

## 原始失败与清理

保留全部原始文件；归档的小文件选择见 `archive-files.txt`。

- `run-1.log`：Unix reply门假设错误，真实请求已执行但没有Unix reply bytes，门超时。
- `fd-run-1.log`：Job取消已使Linuxanchor退出，严格 pidfd_open遇 ESRCH；随后区分确切不存在与其它错误。
- `fd-run-2.log`：recvmsg零flags假设导致未到fd-received；最终raw接收flags=1073741824，即请求的 MSG_CMSG_CLOEXEC；只拒绝 MSG_TRUNC/MSG_CTRUNC，未忽略真实截断。
- `jobcancel-run-1.log`：把 Server.wait_closed当封准入前置门，已有连接使其等待；最终先close同步停止accept，再单独等已有连接。
- `jobcancel-run-2.log`：上述 Windows已空、Linuxcaller未join的真实反例；没有加长产品期限掩盖。
- `jobcancel-combined-run-1.log`：caller ready未出现；改为已独立PGID时不重复setsid，最终仍断言 PID=PGID=SID，未弱化scope断言。

Windows操作仅针对已持HANDLE或PID+creation比对后的HANDLE；Linux研究进程用pidfd+starttime，group取消只作用于明确本次新建的真实PGID。未泛杀、未结束用户共享distro。`cleanup-before-cancel-fix.log` 保留发现caller980残留的失败审计；`cleanup-owned-caller.json`保留父980及其确认的直接子981精确pidfd清理。

最终只读 `verify_cleanup.py`/`cleanup-final.json` 覆盖157个Windows实例、113个Linux实例、38个私有目录：remainingWindows、unknownWindows、remainingLinux、remainingPaths均空。Linux僵尸按已终止区分；pid复用按starttime区分、不操作。此审计针对记录的本次实例和私有路径，不声称枚举共享发行版所有权限域或任意逃逸后代。

## 复现与剩余门

仓库根、本机显式目标核对后运行：

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_races_r3/round3/race_probe.py
.venv/Scripts/python.exe output/windows-g0/interop_races_r3/round3/fd_binding_probe.py
.venv/Scripts/python.exe output/windows-g0/interop_races_r3/round3/job_cancel_probe.py --combined
.venv/Scripts/python.exe output/windows-g0/interop_races_r3/round3/verify_cleanup.py
```

非combined模式是有意保留的“仅Windows Job不能组合join”诊断，会按typed unknown结束并精确清理，不能将其输出视为完整通过。每轮生成新uuid避免旧ready证据。

仍未验证：SCM首次绑定、relay seal/半请求、真正TerminalManager owner装配三者的同一条完整生产候选；原PTY/input/EOT/8槽、MCP/hook/environment各自scope与权限/副作用；真实shipping native Windows适配、Electron正常Quit/失联、完整正常完成及长期回归；休眠/外部发行版终止、主动逃逸或同UID恶意篡改。原Linux生产代码未接入候选，本轮不授予完整G0、Windows激活或PhaseB资格。
