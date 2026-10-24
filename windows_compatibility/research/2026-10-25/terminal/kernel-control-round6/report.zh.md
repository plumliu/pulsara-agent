# 第六轮真实 kernel 控制链 G0 研究

2026-10-25，基线 `b741dd92`。本轮完成 Windows Node/Electron 研究壳 → WSL2 实际 bundled desktop_runtime → 专用 PG 的控制与失联观测，发现并保留 **原 Runtime 在 child 清空后跳过异常退出检查、原 main 随后停止 PG 的真实反例**。output 两行候选在三个明确窗口阻止该后续动作。未改生产/spec、未提交、未进入 Phase B；不宣称 Windows G0-C 全部通过。

后续获授权补充已经完成，见 `distro-signal-gui.zh.md` 与 `supplement-matrix.json`：独立发行版真实normal/terminate+新namespace观测、SIGHUP原反例与最小启动策略、实际可见BrowserWindow原main关闭链、用户实际睡眠后的同kernel/PG连续性和正常收尾。补充8case为7项窄正证/1原窗口fixture失败；前段22例历史范围保留。正式GUI/installer不是先进入B才可完成G0的循环门，补充准确区分机制资格与正式激活。

`final-matrix.json` 是单份最终证据：22 个实际运行case，18 个 observed、4 个保留 harness failure；observed 表示探针完成，不等于产品合同通过。`verification.json` 的18项窄断言涵盖清洁结算顺序、真实负例、候选及失联分类。实际原 `tests/test_desktop_runtime.py` **12 passed in1.53s**。最终源没有重跑整个矩阵：前15项形成Esc停止检查点；恢复后按真实退出事实补5项loss和1项EOF成功，另有1项EOF探针失败原样保留。

## 真实 owner 与研究接缝

固定 Windows Node22.23.3 与实际 Electron44.9.0（内嵌Node24.22.0单列版本）运行原 Runtime/BundledPostgres。`generate.cjs` 用仓库 TypeScript5.9.3 机械编译当前 `runtime.ts/postgres.ts`，AST仅抽取当前 `main.ts` 唯一完整 requestQuit；其控制分支原样执行，UI/dialog/app.exit只是观测stub。Electron没有BrowserWindow，因此它证明Windows Electron进程壳的控制链，**不构成生产GUI Quit/安装包资格**。

HostOperations 只把spawn/exec/目录/日志操作接到明确Ubuntu/plumwsl。实际执行 `desktop/resources/python/bin/python3 -m pulsara_agent.desktop_runtime`（3.12.15、资源site-packages），保留原stdin JSONL parser、DesktopApplication、schema/migrations、真实HTTP服务与application close。`sitecustomize.py`仅对实际start/aclose加日志/门/明确故障。研究stdout流故障只在HostOperations注入，反应仍由原Runtime解析器决定；不复制kernel parser或应用owner。

PG只读复用 `output/windows-g0/pg_admission_r3/round5/client.cjs` 的默认无fault operations，交给同一个原BundledPostgres；不是第二个PGowner。每例独立ext4 `/home/plumwsl/.g0p5-kernel6-…-中文 空`，0700，专用PGDATA/socket。没有生产settings、凭据或provider。kernel实际持有其原kernel.lock；PGstop前记录本次kernel精确PID/starttime、锁和PG实际status。

原 Runtime close callback 唯一清空child并resolve exit。研究候选仅把stop里的两行 `if (!child) return; this.send(child,…);` 替为 `if (child) this.send(child,…);`，随后保留原 `await this.exit` 与 reachedReady/code检查。见 `runtime-candidate.diff`。它不增加持久state、receipt、registry或产品边界，也没有把研究清理接入main当作成功。

## 已完成结果

| 范围与关键ID | 实际事实和结论 |
| --- | --- |
| Node正常分段UTF8 `240796c2`；Electron normal `dfc27874` | 原start frame在中文DSN的UTF8字符内部拆包，原kernel ready；实际application.close返回≤carrier close0≤原PGstop。PGstop前本次kernel已退且kernel.lock可取，随后原main请求exit0。 |
| ready EOF关闭门 `f41d42ec` | 真正对已ready kernel关闭stdin，实际application close进入门。原main pending时kernel仍活、锁忙、PGstop0；释放门后原close返回、carrier0、锁释放，才PGstop/main exit0。不是仅外层等完后伪称owner正确。 |
| start前拒绝/EOF `e7a0e1ba`，start前carrier loss `75732b63`，start前host loss `0405c9ee` | 原start frame未提交，实际application未start、专用PGDATA不存在。host通过当时持续持有且creation匹配的精确HANDLE终止；独立Linux pidfd确认该kernel退出。它不是graceful host退出。 |
| 完整start后EOF `d6e0e0e4` | 实际PG已start，原kernel/carrier最终0，原main停止本次PG。该例不表示数据库从未执行；最终无application.start事件也不抹除PG已启动事实。 |
| malformed `1c8b1719` / partial stdout `948d90ca` / 逐字节中文真实start故障 `49c3d654` | 原Runtime拒绝启动/原消息解析报错，原进程按其控制流结束并join。raw消息、UTF8/base64及中文错误保留；不把输出流故障注入算作整个协议conformance。 |
| 真实close后注入中文收尾故障 `4ed8cd1e` | child尚在时，原stop已能见code1并报退出未完成；PGstop0、appExit未请求、PG仍运行，外层之后才精确清理。 |
| **原kernel crash `3b5e04fc`** | 实际ready后核对starttime的pidfd SIGKILL本次kernel，载体code9、原callback清空child。原stop直接return，原main实际PGstop1/app.exit请求0；无application close。这是产品负例。 |
| **原carrier loss `0d0a152e`** | 精确本次carrier SIGKILL，原child清空；kernel已退/锁可取但无application close。原main仍实际PGstop1/app.exit请求0，第二个产品负例。OS释放锁不等于应用clean。 |
| 候选kernel crash Node `fe454dde`；候选carrier loss Node `b069fd26`/Electron `a828bac6` | 已退出attempt仍检查保存exit：code9或signal SIGKILL令原main保留quitError，PGstop0/appExit未请求；beforeCleanup PG仍live。只证明这三条具体窗口；没有把两行候选扩成所有故障合同通过。 |
| host loss Node `1cf0dad7` / Electron `0f1fb1de` | 精确HANDLE杀本次宿主，预先独立打开的Linux pidfd确认本次kernel随后退出；无application-close-entered，锁可取，PG仍运行。宿主已死不可能执行main Quit，故不是“candidate main已阻止PGstop”的正证；归为 **unclean/UNKNOWN**，无重发。只有明确研究cleanup随后停止本次PG。 |

宿主/载体强退没有实际进入关闭门。记录可以证明精确kernel结束/锁释放/PG继续存在，不能证明清洁应用关闭，也没有信号证据可断言kernel一定死于SIGHUP。原 `runtime.running=false` 只描述载体/准备状态，不是跨域clean事实。候选保留的非零exit检查也不适用于所有假想“载体0而内核失联”情况；未实测就不作保证。

## 原失败与最小收敛

三条旧loss探针 `5b8b6ae7/a25a0b09/df51f0ee` 错误地假设强退后必入application close门，等待超时，保留全部原失败。恢复后只改观测为精确PID/锁/PG/实际gate分类，补上述5条loss，而不把原失败改写绿。

`ready-eof-gated f52fad5e` 的同步 `spawnSync(wait-close-gate)` 阻塞Node事件循环，stdin.end不能及时推进，等待门超时；原host记录随后才stopping，证据保留。只把等待改为异步execFile，让同一个真实owner控制管道继续运行，单项 `f41d42ec` 成功。超时是研究观测截止，不是新增产品lifetime cap；任何超时后的cleanup都不算产品owner通过。

最终4条harness failure均在final-matrix含原failure及实际raw尾部，原文件未修写。Esc检查点文档与原始矩阵也保留其当时停止事实；用户确认误按后才恢复研究。finalize.py只读已有case，18条窄断言不弱化原断言/原tests；JS后续格式化不改变行为，未据此声称整套最终源码重跑。

## 精确清理与剩余资格

`cleanup-final.json` 审计22个本次Windows host PID+creation、22个Linux kernel PID+starttime、22个精确home：remainingExactHosts/currentHostPidUnobservable/remainingExactKernels/remainingHomes全空。每例cleanup均先释放本次门、确认kernel退出/锁可取，再经P5真实精确PG路径stop并核双锁和无本次进程，最后移除本次home；无SIGKILL清理升级。22个历史carrier PID当前查询均ERROR_INVALID_PARAMETER87/不存在，但启动时未保存carrier creation，因此报告只称当前PID观测，不将其冒充历史HANDLE身份。宿主loss时的kernel独立pidfd原事实在矩阵。Electron研究profile只是output闲置文件，无活动壳。

interop与Hiddify TUN保持；共享Ubuntu没有terminate/shutdown，未改默认发行版/生产配置。`disposable-distro-plan.zh.md` 已给官方26.04.1 rootfs/399M、磁盘实际空间、最小bundle资源及唯一名称/安装注册项/清理范围，**尚未下载/import**，待root审阅再执行。

本轮剩余具体C资格：独立可丢弃WSL2整distro终止/恢复；真实生产GUI与打包宿主退出链（root负责）；真实睡眠/恢复（需用户现场）；真实跨发行版资源/依赖启动后再做loss。宿主强退只有unclean/unknown事实，不能声称clean回收成功。原生Ubuntu按用户要求延期，不作本轮Windows阻塞。MCP第五轮另有独立报告，不把本轮扩成全G0通过，也不进入PhaseB。
