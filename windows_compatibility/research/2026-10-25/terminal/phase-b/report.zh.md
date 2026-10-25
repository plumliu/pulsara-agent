# Terminal / MCP / hooks / environment：Phase B 正式接入验证

本专题的生产接入已完成。最终同一份源码在私有 ext4 快照中通过 **131 项共享回归、28 项真实 Windows/WSL 集成**；后者记录 38 个 scope 的实际身份、join 和退休事实。它支持本专题原 owner 与 Windows scope 的正式组合结论，不替代其他专题的安装包、GUI、真实 provider 或 OAuth 验收。

生产源码最后变更为 `conversation_kernel/mcp/supervisor.py`，UTC 2026-10-25 08:21:35；之后未再修改。此前 `terminal_process/output.py` 最后变更为 UTC 08:06:51。验证基线 Git 为 `9e1150b7a4b16e212be36329182cbaa505c4df79`。未创建代码/文档 SHA。

## 正式边界

`windows_interop.configure_interop(helper_path=..., distribution=..., user=...)` 只接可信 desktop start 帧，按本次 Linux 非 root 用户核验并在进程内固定目标。模型参数、普通环境变量和 settings 不选择 helper 或目标。未配置 Windows desktop 的原 POSIX 路径继续直接执行，不实例化空 scope。

平台提供 Job、精确进程 HANDLE、pidfd、SCM_RIGHTS 和 Unix streams；WSL 继续拥有互操作协议与 PE 启动。新增模块只做物理所有权和不解析内容的字节转发，不复制 MCP/SDK/parser，不从 output/research 导入代码。

1. 原 owner 先拥有启动预约/attempt，再 prepare scope。
2. Windows helper 建无 breakaway 的 Job，挂起创建专属 `wsl.exe -d/-u` anchor，先 Assign Job 再 resume。
3. anchor 自己打开实际 `WSL_INTEROP` 的 O_PATH fd，经 SCM_RIGHTS 转交。receiver 持 pidfd、核 SO_PEERCRED/starttime，绑定私有 socket；同一路由启动诊断 PE，helper 用自己的 Job 与精确 creation/进程 HANDLE 核验。
4. 实际命令只在这之后收到私有 `WSL_INTEROP`。relay 只连接持有的 fd，不重开 PID 路径、不回退、不重放请求。
5. 正常结束先封新准入并等待已接受连接，关闭 anchor stdin，等待 Job 自然空和精确 HANDLE 退出。取消先 abort/cancel/gather relay，再向 helper 发 cancel，并由原 owner 结算 Linux 组和流任务。
6. socket 名保留到原 owner 的 Linux/Windows 范围都结算之后才 retire。只得到 Job 空不授权删路径或发布原任务完成。

| 原 owner | 接入点与原语义 |
|---|---|
| `ProcessRegistry` | scope 存于原 foreground decision/state；在原预约内 prepare，取消不在 registry 锁内等待。原 watcher 仍唯一 finalize/发布 physical completion。存活、8 槽、启动回滚均包含 scope。bootstrap 未知不能被 caller settle 擦除。 |
| MCP `_BoundedStdioTransport` | 原 credential guard 内 prepare/最终 spawn；scope 属于连接，SDK 继续拥有帧、工具流和单 call 取消。call cancel 后连接仍能调用，explicit close 才回收连接的 Linux/Windows 范围。重复 close 等待/重抛同一个 physical close task。 |
| MCP `McpHostSupervisor` | 复用原 `_all_slots` 在 `client.open` 前拥有物理 slot，未发布的失败 cleanup 也不会丢失。Host 总 close 只对成功结算者关闭 physical anchor，失败保留 slot/config 引用并向原 Host 的 close_error 抛出；重复 close 保留同一错误。 |
| `HookCommandExecutor` | 原 attempt、credential guard、16+8 permit、stdin/capture/parser/deadline 保留。正常 wait 包含 scope；取消/关闭包含 Linux 组。部分流 task 构造失败时，已创建 task 与进程仍归同一 attempt；未 joined 不释放 permit。 |
| `TerminalEnvironmentOwner` | 原 single-flight probe 内 prepare，cache 只在双域结算后写入；原 timeout/close 取消同一 probe。失败且未 joined 的 attempt 留在唯一 inventory，后续 close 报错，不重新探测绕过它。 |

每 scope 有一个有限的 Linux asyncio worker，以及 helper 内一个有限 monitor。monitor 只保留本次精确 Job/owner HANDLE，弥补 helper 异常死亡时最后物理观察者丢失；不接命令、不另建任务生命周期/daemon/registry。monitor 死亡由仍活的 helper 结算。两观察者都丢失而不能证明退出时，保留 unknown，不能声称 join。

helper 是构建期 `csc.exe` 生成的独立 x64 C# P/Invoke PE，产物由 desktop packager 放到 `resources/windows-interop/pulsara-interop.exe`。它依赖宿主 .NET Framework；不是“无 runtime 依赖”。运行时不使用 PowerShell/Add-Type、Windows Python 或 pywin32。setup 可用 `--version` 检查；配套 recipe/兼容检查由 desktop 专题维护。

## 共享缺陷与接入缺口分开处理

| 类别 | 实际缺陷与修复 |
|---|---|
| 原共享 POSIX | foreground `Timer.start` 故障泄漏启动预约；PTY `Popen` 失败泄漏 master，fdopen/reader/state 构造失败可能留下已生成进程。现在同一早期窗口由原预约/局部所有者回滚。新增 7 个回归在原 manager 全失败，修后与原 suite 一起通过。 |
| 原共享异步启动窗口 | hooks/MCP 已生成进程而第 2 个 drain task 构造失败时，前面创建的 task 必须仍被登记、取消/join；spawn 遭多次 cancel 也必须等同一个实际 child。采用原 attempt/transport 的任务集合，未新建 owner。各有真实 Linux 子进程故障回归。 |
| 原共享输出观察窗口 | 并行回归真实得到 127/128：revision/notify 早于锁外 Host retention callback，waiter 看见两个 owner 各 121、合计 242 字节，超过该测试原有 160 bound。阻塞 callback 的确定性测试在 Git 原 output owner 重现 1 fail。现在 `wait_for_revision` 退出 output 锁后复用同一 callback，再返回当前 revision；无新状态/cap/registry，不改变断言。 |
| 原 MCP 总关闭所有权 | 单个退休 slot 原来保留失败，但 Host 总 close 吞 client.close 异常后仍关闭全部 anchor，且 `_closed` 让重入直接成功；连接尚未 READY 时的 cleanup 失败也没进 `_all_slots`。两个确定性原 owner 负例证明这两点。现在提前登记到既有物理集合、只释放成功者，并记同一 close task；原 Host 已有的 close_error 链继续接收失败。 |
| Windows 接入 | 原 Linux root/PGID/drain 不证明 Windows 后代退出；必须正式组合上述 scope，不能由单独 POSIX 修复冒充跨域完成。 |

## 最终验证

共享结果见 [shared-final.json](evidence/shared-final.json)，真实结果唯一完整份见 [final-matrix.json](evidence/final-matrix.json)。后者保留各 case 的 Job/PE creation、joined reason、helper/diagnostic exit、worker、relay tasks/writers、retire 事实。

| 真实 case（pytest 数量） | 结果 |
|---|---|
| no-pipe Windows 后代正常完成，PIPE/PTY（2） | Linux root 先退，原 live/slot 保留到 Windows 自然结束；正常路径未发送 cancel，结果 success。 |
| Linux root 退后取消 Windows 尾部，PIPE/PTY（2） | 原终止入口得到 killed，双域 joined 后才释放。 |
| 输入与 EOF/EOT，PIPE/PTY（2） | 实际 PE echo，原输入/关闭语义保持。 |
| 同 registry A/B（1）；8 槽耗尽/释放/复用（1） | A 的取消不影响 B；第 9 次不产生 scope，已 join 的槽位可复用。 |
| reader/watcher.start 故障 × PIPE/PTY（4） | 未发布任务的 Linux child、Windows Job、流、worker 均结算，原预约无泄漏。 |
| helper owner / monitor / 控制 stdin EOF / anchor pidfd 强退（4） | 分别真实故障；原 owner 响应 critical loss，结果 unknown/error，与已证明的 physical join 分开。意外 EOF 在 helper 取消 Job 前明确发 failed，不能被后来正常 finish 掩盖。 |
| 原启动预约内取消三门（3） | bootstrap 前、控制 peer 已接受但 SCM recv 等待中、prepare/诊断后命令 spawn 前；原 caller 返回前回滚。recv 门不被说成已测 anchor pin 前后所有交错。 |
| 真实首个 upstream open 前、首 1 字节、首块转发后取消（3） | 观测转发量分别 0、1、282 字节；relay tasks/writers 为零，再结算原 Linux caller。首块不是“完整协议请求”。 |
| MCP 真 SDK call cancel→继续 echo→session close（1） | framing/SDK 原路；call cancel 不结束会话，close 回收该连接的 Windows 长尾。 |
| 真实 Supervisor→SDK→stdio scope 总 close（1） | 完整原 Supervisor 启动、发现、echo 和总关闭，Windows 长尾/relay/worker 结算后物理集合清除。 |
| hooks 正常 Windows 长尾 + timeout（1） | 原 executor 的正常返回/超时均等待物理边界。 |
| environment 正常 cache + timeout 不 cache（2） | 正常等待 Windows 长尾；超时不安装快照，attempt 结算后清除。 |
| 同一 live scope 连续 40 个短 PE（1） | monitor HANDLE 数及时回落，非只在整个连接结束时统一释放。 |

HANDLE 缺陷原负例见 [handle-retention-baseline.json](evidence/handle-retention-baseline.json)：`[191,207,223,239,255,271]`。修复为每轮仅在真实 `WAIT_OBJECT_0` 后关闭/移除退出实例，`WAIT_TIMEOUT` 保留、`WAIT_FAILED` 拒绝；最终矩阵样本为 `[189,189,189,189,189,189]`。40 次/30ms 是有限测试输入，不是生产次数或生命周期 cap。

[native-control-results.json](evidence/native-control-results.json) 另外证明 `--version` 正常，以及 helper scope/probe 均拒绝 EOF 前无 LF 的完整 JSON；Python 控制读口也有无 LF→unknown 的窄回归。

部分正常 case 的 teardown 在完成后沿原 `release_owner` 再次请求取消，因此最终 trace 的 `cancelled=true` 不能被解释为正常路径执行了 TerminateJob。正常 case 在 release 前明确断言未 cancel，helper 正常 join 后的 finally 不 TerminateJob。

## 原失败与修正记录

原始失败未改写，最小证据保存在 `evidence/`：

- `posix-original-failures.json`：7 个正式 POSIX 缺陷原失败；首候选另有一个隔离快照缺 Linux rg 资源的 fixture 失败，随后只补拷既有锁定 Linux rg，未弱化断言。
- `binding-quoting-failure.json`：早期所有参数一律带引号使 WSL 把 `-d` 当 Linux 命令；改用必要时才 quote 的标准 argv 转义。
- `initial-fixture-failures.json`：首个 C# 测试子进程错误持有重定向 pipe、测试 import 路径错误；最终 fixture 使用真实 CreateProcess/no inherited task stdio，不宣称原 fixture 已证 no-pipe。
- `mcp-fixture-stdin-failure.json`：测试 launcher 启动 PE 时继承了 MCP stdin，抢读了 SDK discover；只把该测试 launcher 的三个流改为 DEVNULL，保留原 SDK/server 的真实 framing 路径。
- `critical-loss-before-fix.json`：monitor 失联时 Linux caller 未响应，不能只等 Windows；后修原 owner critical-loss 响应。该文件同时保留一个 SDK 测试字段 `isError`/`is_error` 的 fixture 失败，不能混为生产缺陷。
- `launch-cancel-original-exception.json`：同步 Future 的取消异常类型原样保留；物理清理已完成，后来统一为 `InteropScopeError`。
- `snapshot-contract-before-fix.json`：已有无 subprocess 的 snapshot override 测试失败；保留原 override 的返回契约，未 joined 的真实 scope 仍禁止 cache/pop。
- `retention-load-failure.json`、`retention-deterministic-baseline.json`：原输出观察 race；最后确定性回归及完整共享 suite 通过。
- `supervisor-original-failures.json`：Git 原 Supervisor 两项确定性失败；不是用成功底层 transport 掩盖上游吞错。新增真实 Supervisor case 也通过。
- HANDLE 原负例与修后数据如上。没有把失败后的外层清理算作当时产品成功。

PG 专题对未 joined environment inventory、MCP close 重入、无 LF、terminal orphan settle 和 retention 锁顺序的只读反馈均已关闭；失败保留/重入等窄回归属于 131 项结果，不被当成真实 Windows OS 证据。最后 Supervisor 的跨 await inventory、close snapshot/取消、未发布 cleanup 和错误重入也获只读复核，无新增阻断；对方没有重跑或冒领本专题实验结果。

## 清理与范围

[cleanup-final.json](evidence/cleanup-final.json) 从最终 38 个 scope 事实核对 physical join、worker 已停、relay tasks/writers=0、私有路径已退休。最后额外只读扫描本次 `.pulsara-interop-*`/`.pulsara-terminal-fix-*` 私有目录与相关进程 argv，均为空。Windows 退出根据原 scope 持续持有的 Job/HANDLE 证据，不靠事后猜当前 PID；最后 Linux namespace 查询不冒充原 pidfd 连续观测。

本专题没有使用 provider/真实保存凭据，没有改 settings、interop、TUN/default distro，没有 terminate 共享 Ubuntu 或 global shutdown，没有修改 Linux 主 clone 源码，也没有新增 PG。所有 Linux 测试使用私有 ext4 源码/venv 快照和只读既有依赖，最后删除自己的快照。

当前矩阵没有重新穷举 G0 所有 pin/启动竞态、同时丢失两观察者、完整 distro 故障、睡眠恢复、MCP reconnect/所有工具 timeout 或正式安装包 GUI/provider 流程。已转发请求的外部效果不因 cancel/Job 空变成已知成功或已撤销，不自动重发。原 POSIX PGID 范围不被提升为任意主动逃逸/整发行版进程保证；系统打开外部编辑器/浏览器仍按原 OS 交接语义。其余专题由 root 合并验收。

执行入口见 [commands.ps1](commands.ps1)；脚本是隔离验证编排，不是生产依赖。生产文件和测试以 Git 为内容身份，未把研究探针 import 到产品。
