# 第五轮 MCP：真实 SDK/owner 的跨域 stdio 生命周期

2026-10-25，interop_tasks_r3。两 clone 基线 `b741dd92b06a2d167d882e312e18f503e23b8a89`；已读 AGENTS、当前 Windows spec §5/13/20、实际 MCP SDK/API、stdio owner、credential boundary、supervisor/executor 与测试入口。仅本 output 目录新增文件；未改生产/spec/依赖、未提交，interop 保持，未进入 Phase B。没有读取 saved settings、真实凭据、provider 或调用 Notion 业务工具。

**原 MCP supervisor 的真实关闭仍遗漏无 pipe 的 Windows 普通后代。最小 output 适配把同一 session 的跨域 scope 合并进真实 transport close，正常自然收尾、显式 owner 关闭、timeout 及 server 崩溃获得本轮有限正证。调用取消与 session 回收保留不同语义。** 六场景及记录断言见 final-matrix.json、verification.json；不是生产修复或完整 G0-C。

## API 与最小所有权接缝

实际 Linux 安装为 `mcp==2.1.0`、`mcp-types==2.1.0`。已直接 inspect SDK `mcp/client/stdio.py::stdio_client`、`ClientSession`、公开 `MCPServer.tool/run`，并读当前 Pulsara `conversation_kernel/mcp/sdk_facade.py`、supervisor、wire 与 `ProcessCredentialBoundary`。SDK stdio 接口返回 read/write streams，管理自己的 spawn/shutdown；Pulsara当前产品实际走 `_BoundedStdioTransport`，保留自己的 byte/frame admission、exact credential guard 和 Linux process owner，官方 ClientSession/公开 MCPServer 负责协议和工具会话。本轮使用真实生产这条路径，没有改为另套 SDK stdio transport，也没有复制 MCP parser、服务器调度器或 SDK shutdown。

真实 peer 是 MCPServer 注册的一个 EXTERNAL_EFFECT 工具。工具创建普通 Windows PE，PE 再普通 spawn 一个三个 stdio 均 DEVNULL 的有限 child；normal parent 提前退，工具返回现代 typed `resultType=complete` 的 `MCP-ACK:héllo:0`。其他模式让真实 SDK 接收 cancel/timeout，故意不把通用 Windows 清理知识塞入工具 finally，以验证 session owner 的范围。协议 stdout、工具结果验证、permission/dispatch permit、sessionful stdio lane、原 stderr 丢弃政策均走原实现。

研究适配只在自己的 Linux 进程内：

- 观察真实 `_BoundedStdioTransport.start` 的上下文，并在其实际 `asyncio.create_subprocess_exec` sink 内、原 credential async_guard 仍持锁时准备 scope。路由 path 已是原 config environment 的受检查值，不在 guard 外另走实际 MCP spawn。
- 只读复用第四轮 Scope、WindowsScope 和竞态分支的标准 SCM_RIGHTS relay：anchor自开 FD→SO_PEERCRED/pidfd 绑定→host 精确 creation/Job 诊断核验→operation admission。本轮没有旧 path pin。
- 只装配真实 `_BoundedStdioTransport.aclose`：正常先走原 stdio close/Linux group 收回，再 normal seal、anchor EOF、Windows Job/精确 HANDLE 自然 join；取消/故障 owner close 先 cancel seal ACK，再 Job cancel 与原 Linux group cancel/join。正常不用 TerminateJob。
- 同一个 transport 的 close task 被该研究接缝 join；原 `BoundedMcpSdkClient._close_resources` 仍在 transport 物理收回后才发布 client `_closed`。socket 仅在原 supervisor close 已返回后退休，诊断与 late-caller 的独立 Linux PGID 也检查结束。

此处的 Scope 只借用第四轮研究 OS 机制；没有创建 TerminalManager、占用 terminal 8 槽或复制其 scheduler。command 精确匹配、ContextVar、文件 RPC、ctypes 和内存 patch 都不是生产 API，未新增 durable receipt/registry/job/event，也没有产品寿命上限。

## 六项真实结果

| 场景 | 实际观察 |
| --- | --- |
| 原 owner baseline normal，77977e18 | 真实 executor SUCCESS/permit RELEASED；原 supervisor close 已返回，clientClosed=true、Linux group=false、stdio tasks done，但同 creation 的无 pipe Windows child HANDLE wait=258。之后显式标注的外层研究 cleanup 等其自然结束，不能算原 owner 已正确。 |
| candidate normal，9c414b75 | close 仍 pending、clientClosed=false 时，精确 Windows child 仍 live；自然 pe-finished 后全部所持 HANDLE signaled、Job ActiveProcesses0，原 supervisor close 才返回。无 TerminateJob。 |
| candidate call-cancel，126280df | 真实 permit/SDK call 被取消，peer SDK 确实收到 cancellation；permit RELEASED、operation lane0，原 session 仍 READY、client未closed。随后明确调用 supervisor.close 才 cancel/join 两域。没有把单 call cancel 偷改成 session close。 |
| candidate timeout，cf6c3921 | 原 executor 返回 SYSTEM_ERROR，原分类 remote_outcome=UNKNOWN、retry_performed=false；REQUEST_TIMEOUT 由当前原 owner 判 transport failure，旧 slot fence/close，并保留原重连策略。研究调用立即关闭本次 host，只有一次 MCP spawn/一次工具执行，未重发 tools/call。 |
| candidate server crash，6df57087 | Windows parent/child 已启动后，对本次 Linux MCP server 的精确 pidfd 发 SIGKILL；真实 SDK/stdio 断连，原 executor 返回 SYSTEM_ERROR/UNKNOWN/retry=false。cancel 前 Windows Job 仍有 11 个 live 成员；原 host/slot close 合并 scope 后精确 join。没有把物理回收改写成操作未执行。 |
| credential denial，85cf08c5 | 仅进程内生成的合成 caller sentinel 出现在待提交参数，真实 guard 在 sink 前拒绝；actual MCP spawn count=0、scope未prepare、无server/PE。没有读取或记录真实凭据；正常四项也断言实际 sink 发生在同一真实 guard 持锁期间。 |

单 call 取消没有独立进程归属或 per-call Job API，本轮不发明它。当前 owner 对 timeout 的行为与单 CancelledError 不同：`_mcp_error_is_transport_failure` 将 SDK REQUEST_TIMEOUT/CONNECTION_CLOSED 视为连接故障；这是源码与实证，不把两者改成相同政策。UNKNOWN 结果和 connection 的物理 join 是两类事实。

所有有 session 的候选记录均核：guard→scope prepare/FD绑定→host Job核验→真实MCP spawn；seal ACK task/writer0，ACK后无 bytes-forwarded；Windows全观察 HANDLE wait0、Job空及Linux group/stdio tasks结束→client/supervisor close返回→路径退休。公开SDK framing及现代typed结果实际通过。没有更改原权限或 credential guard 断言。

原聚焦入口实际执行：`test_mcp_transport_lifecycle.py`、`test_mcp_scheduling_isolation.py`、`test_mcp_credentials.py`，**21 passed in 6.68s**。Ruff 通过。baseline是保留的真实产品反例，本轮没有另外覆盖掉它来求绿。

## 清理与复现

`verify_cleanup.py` 审计本轮 **70 个 Windows PID+creation、21 个 Linux PID+starttime、6 个精确私有目录**，remainingWindows/unknownWindows/remainingLinux/remainingDirectories 均空。Windows 仅自有 Job/精确 HANDLE；server crash 使用自有 pidfd；未停止共享 Ubuntu、泛杀、修改 interop/配置。审计与调用命令在 commands-results.zh.md；一份 final-matrix 包含本轮原始关键数据，避免重复归档同组 stdout/快照。

## 本项与 G0-C 的剩余边界

本轮是单个 stdio connection/session 的有限集成，不是每个工具调用的操作进程归属方案。尚未验证：连接启动在 credential gate/SCM准备期间的全部取消/失败；多次 reconnect/reload/替换旧 slot 交叠与每次新 scope 的唯一创建/退休；Windows stdio backpressure、异常frame/超大frame与跨域回收的全部合成；真实 MCP HTTP/TLS/OAuth 和网络变化（其无本地 stdio spawn，不由此 Job 证明）；MCP host/Windows helper本体强退及休眠/共享发行版外部终止。第四轮 terminal/第二轮 Job 的证据不冒充这些 MCP 合成门。

剩余 C 还包括真实 kernel/Windows Node/Electron 控制链的 start/shutdown/EOF/载体或host失联、协议边界与“先kernel clean join再PGstop”；hooks由另一分支负责；环境 snapshot probe/截图等其余实际 spawn owner仍须按受影响合同核查。原生 Ubuntu 资格按用户本次要求延期，不作为本轮 Windows G0 阻塞，但也没有由 WSL结果替代。生产接缝、shipping native adapter 和其余 G0仍未获放行，Phase B不开始。
