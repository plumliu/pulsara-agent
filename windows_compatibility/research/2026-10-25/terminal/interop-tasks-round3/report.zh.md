# G0-W-C 第三轮：真实 terminal owner 与候选跨域 scope 组合

2026-10-25，interop_tasks_r3。Windows 与 Linux clone 均为 `671f8fd21da5ac4bdc4190988d2c294395faa036`。遵循根 AGENTS、当前 Windows spec §5/18，已读 round2 interop-jobs/interop-route 报告、真实 `terminal_process/manager.py`、`environment.py` 和现有 terminal tests。仅新增本 output 目录文件，未改 src、desktop、spec、用户设置或 interop，未提交，未进入 Phase B。

**真实 owner 的负例已复现；输出目录内的内存组合原型通过本轮 14 项断言。** 原 manager 即使获得正确的每任务 interop 路由，仍会在无 pipe 的 Windows 普通子进程存活时发布物理完成并释放槽位。把额外 scope 的取消和 join 接进原 owner 的物理结算调用后，PIPE/PTY、8 槽、Unicode 输入、PIPE EOF、PTY EOT，以及同一个 ProcessRegistry 内 A/B 隔离可保持本轮受测语义。这里证明了一个必要接缝及其有限组合，不能据此宣布生产 owner 已正确或整个 G0-C 通过。

## 受测边界

Win32 Job/精确进程 HANDLE/等待由系统拥有；WSL 拥有原生 PE 创建与 stdio；Linux 真实 TerminalManager 继续拥有 shell、PIPE/PTY、PGID、reader、watcher、输入、结果与容量。研究适配仅建立每任务不可继承、无 breakaway、KILL_ON_JOB_CLOSE Job，先 CREATE_SUSPENDED→Assign→Resume 启动专属 anchor，再以 O_PATH 固定上游、0700 ext4 私有 AF_UNIX socket 透明双向转发字节。没有复制 WSL parser、stdio 协议、terminal manager 或 scheduler。

Windows 编排实际为仓库 `.venv/Scripts/python.exe` 3.12.13；Linux clone `.venv/bin/python` 3.12.15。guest 应用 cwd 在 `/home/plumwsl/workspaces/pulsara_agent`，socket 根 `/home/plumwsl/.g0tasks` 经 findmnt 确认 ext4，权限 0700。小型脚本和记录在 Windows output，guest 通过 `/mnt/c` 读取/写入这些研究文件。未读取 saved settings、凭据或运行 provider。本轮没有使用 Node/Electron，因此不扩大 round2 的宿主结论。

## 原 owner baseline：六项

最终证据为 `baseline-7cc458d9-matrix.json` 与六个同前缀 summary/manager/PE/anchor/relay 记录。真实环境 owner 使用已有 `parent_env` 和 `TerminalEnvConfig.passthrough_names={WSL_INTEROP}`；关闭 shell snapshot 以隔离该项。没有 monkeypatch 任何物理生命周期、输出、输入或容量逻辑。

| 场景 | 真实观察 |
| --- | --- |
| PIPE/PTY 正常父先退，普通 Windows child 的三个 stdio 均 DEVNULL | manager 报 success、PHYSICALLY_JOINED、completion=true、liveCount=0；同一时点精确 child PID+creation 仍 live。随后原 registry 允许再接纳 8 个 Linux sleep。 |
| PIPE/PTY 取消 | 原 kill_process 返回 killed、PHYSICALLY_JOINED、completion=true、liveCount=0；精确 Windows child 仍 live。其后 Windows TerminateJob 属于明确标注的研究清理，不是原 manager 的回收。 |
| PIPE Unicode 输入与 EOF | 原 write_process 写入 `héllo`，PE 返回 `ACK:héllo`，close_stdin 后返回 EOF 和 success。 |
| PTY Unicode 输入与 canonical EOT | 真实 PTY 允许 echo；ACK 与 EOF 都来自 Windows PE。原 close_stdin 的 EOT 路径成功，随后 success。 |

Windows parent/child 在 PE 起始时自查属于目标 Job，并立即关闭诊断 Job handle。本轮只允许普通 subprocess/CreateProcess 后代，不使用 detached/breakaway、自愿 enroll 或外部 broker。

## 内存候选：六项与共享 owner A/B

`guest_probe.py` 只在自己的 Linux 解释器内存中装配现有对象；磁盘生产代码和原测试断言不变：

1. 截获该实例 `_spawn` 返回的真实 state，只为本次受测 PE command 关联局部 scope。原 Popen、reader、watcher 仍创建真实 PIPE/PTY。
2. 装配该 state 的 `reader.join`：先执行原 join，再确认真实 Linux PGID 已消失，才请求 scope 正常 seal/join；watcher 继续作为唯一 normal completion 发布者。物理完成、输出 finalize、timer、状态与容量计算均沿生产原路径。
3. 解释器内装配 `_terminate_process_group`：对受测 state 先请求 scope cancel，并等待私有 relay seal 确认，再走原 Linux group 终止。Windows owner 封新请求并保留 socket，关闭 anchor stdin，取消时 TerminateJob；正常时只 EOF、等待自然结束。
4. Windows broker 在任务运行期间枚举并持有观察到的精确 process HANDLE，只有 Job ActiveProcesses=0 且全部所持 HANDLE signaled 才回 joined。最后原 manager release/join 后才杀自身 relay、unlink 私有 socket。

这只是验证接缝的 monkeypatch。command 字符串过滤、局部 state identity→scope map、output 文件 RPC/polling、研究 Python ctypes 均不是已存在的生产 API，也不是产品实现建议。没有新增 durable registry、receipt、事件、job 表或产品总量/寿命 cap。

最终证据 `patched-c6de6806-matrix.json`：

- PIPE/PTY 的 Linux root=0、reader 已退出时，仍处于 TERMINALIZING，watcher 存活、completion=false、liveCount=1。
- 在 Windows 无 pipe 尾部仍运行时，8 次新 Linux 任务只有 7 次 running，第 8 次按现行 8 槽规则 blocked。此阻塞来自原容量计算；没有修改它。
- normal scope 的 brokerTerminatedJob=false，Windows child 写出自然 pe-finished 后才发布 success；最终全部观察 HANDLE signaled、Job 范围空。没有用 TerminateJob 冒充正常成功。
- PIPE/PTY 取消返回时，目标 Windows PE 已死亡，原 owner 发布 killed/PHYSICALLY_JOINED 且 liveCount=0；Windows cancel 经装配后的真实 kill_process 路径触发。
- 两种模式的 Unicode ACK、PIPE EOF/PTY EOT 保持。

最终共享组合 `shared-ab-8e194a20-matrix.json`：同一个 Linux managerPid、同一 registryOwner、同一 host owner，先启动 B 的 PTY normal 尾部，再启动 A 的 PIPE cancel。取消 A 后，B snapshot 的 completion=false、liveCount=1，Windows broker 同时核验 B child 的确切 PID+creation HANDLE wait=258；B 随后自然 success。A brokerTerminatedJob=true、B=false。不是两个独立 manager 的隔离正控。

`verify_results.py baseline-7cc458d9 patched-c6de6806 shared-ab-8e194a20` 对上述 14 项受测场景及清理事实执行断言，结果见 `verified-results.json`。

## 原测试与原始探针失败

原 Linux 入口 `tests/test_round2_terminal_environment.py`、`tests/test_terminal_cognitive_subtraction.py`、`tests/test_round2_terminal_output.py` 共 **47 passed in 6.40s**。没有 skips/xfails、修改断言或把新探针当作旧测试替代。

原始失败均保留：

- `tasks-5fb9ff37-*` 六项首次研究 fixture 在 relay-ready 超时。socket 路径长 115 字节，超过 Unix sockaddr 路径容纳范围；`environment.json` 直接重现 `OSError('AF_UNIX path too long')`。之后仅缩短研究私有 socket 路径至 ext4 用户目录，没有变更产品路径规则或增加产品限制。六个空自有旧目录已精确 rmdir。
- `baseline-12832bb3-cancel-{pipe,pty}-summary.json` 保留早期诊断断言失败：TerminateJob 后观察到 ActiveProcesses=0，但立即零超时查询个别预持 HANDLE 仍为 258。后续明确逐 HANDLE 等待至 signaled，最终 baseline-7cc458d9 全部完成。没有把 ActiveProcesses=0 当作所有 HANDLE 已 signaled，或删除原始失败。
- `patched-569ade2a-*` 是早期内存组合，尚仅依赖 ActiveProcesses=0；最终采用 c6de6806 的观察成员 HANDLE join，并在 reader 后先等 Linux group，再正常 seal。旧证据不用于最终 14 项结论。

## 清理与尚未覆盖

`verify_cleanup.py` 扫描本目录全部轮次记录，审计 **222 个 Windows PID+creation 身份、62 个 Linux PID+starttime 身份**：remainingWindows=[]、unknownWindows=[]、remainingLinux=[]。Windows 只按已拥有 Job 或核对 creation 的精确 HANDLE 清理；Linux relay 用先持 pidfd、再核 starttime、pidfd_send_signal。全部私有 socket 在 manager 已 join 后精确 unlink/rmdir，`/home/plumwsl/.g0tasks` 当前为空且保留 0700。未停/重启发行版、泛杀进程、关闭 interop、改配置或碰 saved settings。

本轮未验证真实 MCP/hook owner、environment snapshot probe 的 Windows 后代合并、取消启动中/Thread.start 故障/决策 watchdog/owner 关闭全部交错、helper 崩溃后结果 unknown、半请求/丢回复、首次 pin 的身份绑定、应用 GUI Quit/Node/Electron shipping adapter、sleep/resume 或任意主动逃逸。原环境单测通过不等于环境 probe 的跨域生命周期已经集成。

本轮 relay 的“检查 sealed → await open upstream”仍有取消竞态；已有连接也未以完整产品 admission 协议与取消合并。本轮只在有限受测 shell/child 请求序列上取得正常/取消/容量正证。首次 pin、半请求和旧调用方 socket 退休应与 interop_races_r3 的独立报告共同审查，不宣称这些交错已在本真实 manager 组合内全部闭合。普通 task 环境若主动改路由/删 socket、WMI/服务/计划任务等外部创建不在本轮保证范围。

因此，**候选获得真实 terminal owner 的有限集成正证；现生产路径仍有 baseline 反例，完整 G0-C 和 Phase B 仍未放行。**
