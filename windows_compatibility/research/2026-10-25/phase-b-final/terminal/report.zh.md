# alpha.10 正式 interop 最小深故障与主关闭链

2026-10-25。本专题选定的 **7 个逻辑边界均符合合同**：两处交接取消、双观察者丢失、MCP/hook 各一关键丢失、installed main 的活动任务正常 Quit 与宿主强退。最后一项是 §9.3 的 **unknown 负向门通过，不是清洁关闭**。没有修改生产代码、依赖、版本或构建资源；不据此单独授予 B5/签名发布、原生 Ubuntu 或所有故障排列资格。

## 真实来源与测试接缝

运行时是已安装 `/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3 -I`。`installed-source-provenance.json` 对 anchor/relay/scope、terminal manager、Host、MCP SDK/Supervisor、hooks executor、Application 共9个实际 site-packages 文件与当前 WSL clone 的完整 bytes 比较均相同；`installed-helper-provenance.json` 核 installed PE 与本次已构建 PE 完整 bytes 相同。没有 src/PYTHONPATH overlay、代码 SHA 或文档 SHA。

五个 scope 小项直接使用已安装原 ProcessRegistry、Supervisor/SDK、HookCommandExecutor。只有测试暂停点/故障注入与结果观察：

- anchor pin 门用一个私有 executable 启动适配，调用原 installed anchor.main 并以行 trace 在 O_PATH descriptor 已打开、尚未连接/发送 SCM 时暂停；不复制 anchor/relay 实现。
- receiver 门包装标准 asyncio.start_unix_server 的一次调用，原 receiver 已接纳 SCM FD、持 peer 身份，但尚未绑定发布 relay；取消仍走原 launch reservation。
- 双观察者测试只对其原 ready 中的精确 Windows creation/HANDLE 施加故障；另外预持原 fixture child 的 HANDLE 用于清理。
- MCP/hook 用受控本机 SDK 服务与真实 PE，无外部账号/业务操作、provider 或保存凭据。MCP pending call 的证据是原 SDK ConnectionClosed 与 Supervisor close 失败责任，不冒称经过 Kernel MCP tool effect adapter 的完整 public UNKNOWN DTO 验收。

主链两项直接启动已安装 `Pulsara.exe`，并非复制 main 或摘 AST 函数。入口前的 Node inspector 仅用于以下隔离和观察，随后释放原 main：

1. 在原 `app.requestSingleInstanceLock` 前设置私有 Windows userData，自己的 wsl-selection 仍由原 inspect 核 Ubuntu/plumwsl。
2. 仅替换本实例 `WslSetup.prepare` 的返回配置，明确给出 private home 和原 installed resources 的全部 DTO 字段；不替换 Runtime/PG/main 的生命周期或结果判断。每个 case 还核 installed asar main 与已构建 main 完整 bytes 相等。
3. 本次 HostOperations.spawn 用 installed Python 的私有预载脚本执行原 desktop_runtime。因为该测试脚本入口替代了原 `-m` 识别路径，预载显式保留原启动接缝的 SIGHUP-ignore，再 runpy 原 installed module。
4. 原 Application.start 返回后，经原 controller.create_session/Core 和 TerminalManager.execute 注入一个真实 Windows PE 长尾；不是通过 GUI/provider 公开命令入口。Application.aclose 包装仅记录前后、原样返回/重抛。PG.stop 包装仅记录原调用顺序。根任务的公开 GUI→保存模型→terminal→真实活动睡眠另有报告，不能由此替代。

正常 Quit 由原 app.quit→before-quit→requestQuit 发起，Computer Use 对唯一私有窗口点击原生“退出 Pulsara”确认；没有 stub 对话框、关闭函数、异常或返回值。宿主强退只对本次 Node 子进程的精确 handle 发 SIGKILL，未操作其他同名进程。

## 最终选定矩阵

| case | 实际结果 |
| --- | --- |
| pin-before-send | 原启动预约仍为1，真实 anchor 已 pin FD3；取消后不发布任务，预约/state 清空，scope joined/helper0/worker退/目录退休。独立 pidfd 确认该 anchor 退出。 |
| received-before-bind | receiver 实际持 FD14 与 SO_PEERCRED/pidfd 身份；尚未绑定 relay 时取消，原预约回滚，双域 join、任务/流归零、目录退休。 |
| both-observers-lost | 精确结束 helper+monitor 后，产品保 `unjoined/unknown`、physicalCompletion=false、容量仍1、两次原 close 均失败。外部早持 child HANDLE wait0 仅授权测试清理，不把产品状态改为 joined/retired。 |
| mcp-owner-lost | 真 Supervisor→SDK pending fixture_delay 遇 helper 丢失，SDK 为 ConnectionClosed；最终 scope physical joined，但结果 unknown、helper exit9。Supervisor 两次 close 重抛同一个错误，失败 slot 留存；不自动 reconnect 或重放 call。 |
| hook-owner-lost | 原执行返回 HOOK_EXECUTOR_FAILURE；scope physical joined/helper9，但 unknown 原因保持。原 attempt joined 后移除、16个同步 permit 归还；未当作 hook 成功。 |
| quit-final | 原 close-enter 时 Windows 尾部仍在、scope 未 joined；原 Application close 返回后 completion/join 为 true、reader/watcher/worker 全退、helper0/目录退休。随后原 carrier0/stopped→PG.stop→exe0；driver exit0。 |
| host-eof-exact | 原 Application 实际进入 aclose，但 scope control 丢失，原 close 抛 InteropScopeError，产品保 completion/join=false、unknown。原 runtime **尝试** emit failed/stopped 并 SystemExit1；不能说死宿主收到了回复。主 exe 是故障终止，不执行 PG.stop。 |

最终事实唯一汇总 `final-matrix.json`，`audit_results.py` 对原 raw 逐条核对；原五个 scope result、main 两域 jsonl 与外部 witness 均保留。

## EOF 的物理事实与后续许可

`host-eof-exact-windows-held.json` 在故障前从本次唯一 Job 查询成员，预持3个精确 PROCESS HANDLE；查询后立即关闭外部 Job HANDLE，以免改变产品 KILL_ON_JOB_CLOSE 的条件。故障后3个 wait 均0，`signalSent=false`。这是外部实际退出证据，**没有补发产品 joined 帧或洗掉它的 unknown**。

同时，Linux witness 在故障前持原 kernel pidfd、核 starttime 与 busy kernel.lock。故障后 pidfd 可读且原锁空闲；Application failure 原样保留，kernel 以1退出，reader/watcher/interop worker 已退。没有发现选定普通 payload 活漏或自动重放：同一 case 只有一次 Application start、一次 terminal 注入，没有恢复/重发任务。

私有 PG 仍活，system_identifier `7700620382223286981`。新显式清理实例先执行原 --wait-owner，再以原 `checkWslMaintenance` 得到 PG active、`postgresExecution=not-submitted` 的维护拒入；精确 data_directory/systemid 与 witness 相同。只有随后对这个独立可丢弃 home 调用原 `resetConfirmed`，按原锁/身份/stop/delete 规则清理。**外部 reset 不是死宿主的成功 Quit，也不是重试旧操作。** 该负向结果符合 §9.3，故未为“通过”改生产代码强行变 clean。

## 原失败与清理

保留三次主测试编排失败：首 inspector 太早在 Node 内部 bootstrap 调用模块 loader，尚无 kernel/PG；第二次 workspace 放在私有 home 旁，被原 WSL workspaces 约束正确拒绝，原 kernel failed/stopped/exit1、PG保留；第三次 Windows driver 误用 guest 事件路径且错误分支未断开 inspector，产品原 Quit/PG.stop 已完成，调试器阻止 exe 最终退出。第三次只结束外部 driver 断开调试，预持 main HANDLE 证实其自然 exit0，未 kill 产品主进程。最后修正 output driver 后 `quit-final` 独立干净复验通过。

首 EOF `host-eof-final` 保留其原 close-failed unknown；随后只补同一逻辑项的故障前 Windows HANDLE 与原 runtime emit/exit 观察，最终选 `host-eof-exact`。没有把两次当成两个不同通过门，也没有让失败后的外层清理倒填产品 clean。

五个小项的私有根见 `scope-cleanup.json`；五个实际启动/失败 main 实例各有原 PG owner cleanup，六个 Windows userData（含未用首例）见 `windows-cleanup.json`。私有 home/workspace/PG/data/socket 全部清理，无本次 child/observer 剩余。双观察者/EOF 的未 joined socket 仅在外部精确退出/原 caller 结束后作为测试资源移除，不把对象改成产品 retired。

没有打开日用 home、读写真实 settings、停止正式 PG、终止共享 Ubuntu、改 interop/TUN/default；Windows 控制仅对自己的实例。未改生产源和版本，无需为本分工重建候选。新 root R0–R5 报告已快速只读核对，71/10/12 scope、随机 provider 与确定性 R1、正常 Quit 和非 B5 结论均未发现夸大。

命令入口是原 installed Python `-I scope_cases.py <五个case名>`、锁定 Windows Node22.23.3 `main_cases.cjs quit quit-final` / `main_cases.cjs host-eof host-eof-exact`、之后 `cleanup_main.cjs <label>`。C#只在 output 构建测试观察器，未进入产品依赖。其余原生平台/硬件/发行版未安装、完整发布资格按主矩阵继续，不能据本页宣称全部 Windows 场景通过。

归档约128KB，不含 PE/应用、PG数据或 Electron cache。`scan_credentials.py` 经原 LocalSettingsStore/require_pulsara_home 只读加载实际保存凭据，检查原值和两种 JSON escape；`credential-scan-final.json` 为零命中且 settings inode/size/mtime 未变。完整失败/回复/物理事实仍保留，没有泛化涂黑错误或路径；`archive-files.txt` 是本分工的最终小材料清单。
