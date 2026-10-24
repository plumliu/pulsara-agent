# 第六轮 kernel 控制链停止检查点

2026-10-25。用户按物理 Esc 停止 Computer Use，root 要求本 turn 停止新实验。收到后未再启动 kernel/PG/故障实验、未评估或创建新发行版；只保存已有结果并做精确资源审计。未改生产/spec、未提交、未进入 Phase B，共享 Ubuntu 未停止。

MCP 第五轮已经完成独立检查点：`../round5/report.zh.md`，六场景、21 个原测试、70 Windows/21 Linux 身份及6目录清理。这里仅记录随后获授权的真实 kernel 控制链进度。

## 实际调用链

- Windows 固定 Node22.23.3，以及实际 Electron44.9.0 无 BrowserWindow 研究壳；各壳原始 `process.versions/execPath` 在 hostRows。Electron 的内嵌 Node 单独记录，未当成独立 Node22。该壳不构成真实生产 GUI Quit 资格。
- `generate.cjs` 用仓库 TypeScript5.9.3 机械编译实际 `runtime.ts` 和 `postgres.ts`，并从原 `main.ts` AST 选取唯一 `requestQuit` 函数；其整个控制流在研究 VM 内执行，只有 UI/退出函数被观测 stub，实际 Runtime 和 BundledPostgres 保留。未手写第二个生命周期 owner。
- HostOperations 仅将三 pipe spawn/exec/目录/日志操作接到明确的 Ubuntu/plumwsl。实际运行 `desktop/resources/python/bin/python3 -m pulsara_agent.desktop_runtime`；已观察其模块来自资源 bundle 的 site-packages，而非项目 editable 模块。应用实际 DesktopApplication、真实 schema/migrations、private HTTP service 和关闭 owner 均参与。
- PG 只读复用 PG 分支 P5 `createClient(home,onEvent).operations`，保原 BundledPostgres 与严格双锁 helper/client，不复制 PG 协议。每例独立 `/home/plumwsl/.g0p5-kernel6-…-中文 空`、0700、ext4、专用 PGDATA/Unix socket；未读生产 settings、凭据或 provider。
- `sitecustomize.py` 只对实际 DesktopApplication.start/aclose 加日志、显式故障或关闭门；没有 fake application、替换数据库准备或复制 kernel parser。UTF-8/畸形/partial 注入只在研究 HostOperations 的 stream 接缝，解析和反应仍由原 Runtime/desktop_runtime 执行。

## 已取得的记录

`checkpoint-matrix.json` 选择 **15 个已运行案例，12 个 observed、3 个保留 harness failure**。observed 只表示该实验完成记录，不等于产品合同通过；原 kernel crash 就是产品反例。停止后没有重跑整套或补正这三条失败。

| 组 | 实际完成内容 |
| --- | --- |
| 正常链 | Windows Node 的真实 start frame 在中文 DSN 的 UTF-8 字符内部拆包；实际 bundled kernel ready/inspect/close，原 main 等 runtime.stop 后才调用 PGstop，PGstop 前 kernel lock 可取。独立 Electron normal 同样 ready、原 application close、carrier0、PGstop、main exit请求0。 |
| 启动/协议 | start 前 PG准备拒绝→原 Runtime EOF并join未启用kernel，无PGstart；完整start frame后EOF；畸形stdout消息；partial stdout终帧；真实application启动中文故障且回复逐字节交付；实际application close后注入中文收尾故障；start前精确carrier终止。原始数据/错误分类与退出事实保留，未把start后EOF说成数据库未执行。 |
| 原 child 清空反例 | `kernel-crash-original-node-3b5e04fc`：真实ready后以核对 starttime 的 pidfd SIGKILL 本次 kernel，载体 code9、原 close callback 清空 child；随后原 `stop()` 因 child不存在直接返回。原 requestQuit 继续实际 PGstop，并请求 app.exit0。原 kernel 没有 clean application close；该记录不能算正常关闭成功。 |
| 最小研究候选 | `kernel-crash-candidate-node-fe454dde`：只机械修改 stop 的两行，让 child已清空时也核对已有 exit promise。相同真实kernel crash后，原 main 保留退出错误，PGstopCalls=0、appExit未请求、专用PG仍运行，最后仅外层明确研究清理停止PG。生产未修改；只验证这一个具体窗口。 |
| start 前 host loss | 固定 Windows Node 被本次精确 HANDLE 终止；独立 Linux pidfd observer确认本次kernel结束，未启动PG。不是 graceful host退出。 |

## 三条未合格的 loss 探针

`carrier-loss-candidate-node-5b8b6ae7`、`host-loss-candidate-node-a25a0b09`、`host-loss-candidate-electron-df51f0ee` 都保留失败：初版研究逻辑假定强退 carrier/host 后，实际 kernel 必进入 application close gate，于是等待该 gate 超时。

原始 kernelRows 没有 application-close-entered；beforeCleanup 观察到本次kernel已结束、kernel lock可取，而专用PG仍运行。**不能把它们说成已 graceful join，也不能无信号证据断言一定是 SIGHUP。** 原始 PID/退出/锁/PG事实是真实证据，所假定 gate 不是事实。此分支在用户停止前尚未按实际状态重构并重跑，不能以最终清理代替 owner失联资格。

所有三例之后只对已验证本次目标进行清理：释放本次 close gate、核 kernel身份/锁已退、准确专用PGstop与双锁检查，再移除本次home。15例均无 SIGKILL 强制清理升级；清理不能恢复此前丢失的 clean exit/结果。

## 停止时资源审计

`checkpoint.py` 只读取已有结果和当前身份，不执行新案例。`cleanup-checkpoint.json`：15 个 Windows host PID+creation、15 个 Linux kernel PID+starttime、15 个精确 home；remainingExactHosts/currentHostPidUnobservable/remainingExactKernels/remainingHomes 全空。各案例的 PG cleanup 原事实均为 status3/4、kernel/PG 两锁可取、无本次 PG进程，再精确移除home。Node/Electron壳均已结束，关闭门不再挂起；Windows研究profile仅是 output 内闲置文件，未归档为运行状态。

原始 `*-summary.json`、host/kernel/observer JSONL、stdout/stderr、编译产物和候选差异均保留。检查点矩阵只去掉与 hostRows 重复的 hostResult.rows 字段，不修改原始证据。

## 待验与恢复位置

停止时仍未完成：三条loss分支的正确分类/修后重验；真实Node/Electron host失联时 kernel clean/unclean 与PG保留的完整合同；两行Runtime候选在其他启动/关闭交错的复验；更多异常frame/失联组合；真实GUI/安装壳的退出链；睡眠恢复。原生Ubuntu按用户要求延期，未用WSL代替。

唯一临时可丢弃WSL2发行版的官方rootfs/体积/空间/导入清理方案尚未评估，**未import、未terminate任何发行版**。该准备在停止后没有继续；恢复时应先交root审阅具体范围。物理睡眠仍须现场安排，不能以模拟声称实测。

完整 Windows G0-C 未通过，Phase B 未开始。本检查点允许从既有原始记录继续，不要求重做已完成的基础pipe或MCP矩阵。
