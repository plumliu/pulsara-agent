# 终端模型调用认知减法：实施证据

日期：2026-09-26。对应根目录 `PULSARA_TERMINAL_MODEL_CALL_COGNITIVE_SUBTRACTION_SPEC.zh.md`。

## 结果与边界

独立命令以 workspace 为默认／相对 workdir 基准，不再创建记忆 cwd 的命名 session 或注入 EXIT trap。模型动作删除 `log`，由 `poll` 读取状态与输出；`list` 保留 command、stdin_closed、duration 等元数据。成功的观察／控制调用与进程自身退出状态分别表达。`write` 漏传 `append_newline=False` 的旧故障先独立修复；只有之后的有效输入才用于等待窗口对照。

`write`／`submit` 默认在输入成功写入并 flush 后观察最多 1000 ms，0 明确立即返回，进程组物理完成则提前返回。实现仅等待既有完成信号后读取既有保留快照，不凭 PTY 回显或输出安静推断回答完整。取消调用沿用物理 I/O owner 的精确结算，不重发已送达输入。8 个并发执行槽沿用原有物理资源边界；容量满时本次未启动、无 process_id，并返回 `PROCESS_CAPACITY_EXHAUSTED`。未增加累计调用数、耐久 job、事件、关系或恢复机制。

模型环境与所有 hook 公共 cwd／hook 子进程 cwd 统一为 workspace。compaction handoff 只移除伪 session 字段，真实 process_id、输出游标和 monitor 保留。旧 descriptor 与当前 owner 不匹配仍由现有 binding 闭合检查拒绝，SYSTEM／tools 不在 epoch 中途改写。

## 真实 provider 对照

实验脚本：`tools/run_terminal_cognitive_dogfood.py`。只读加载有效 home `/Users/plumliu/.pulsara` 的保存设置，使用保存的 `openai/gpt-6-luna` 连接。通过现有 owner 注入临时 home 与经过验证的本地临时 PostgreSQL 数据库，使用 clean-v0 后删库；不修改生产设置或导出凭据。报告保留实际提示、参数、结果和回复，凭据值不写入证据。

| 场景 | 修好 write 后的旧合同 | 新合同 | 观察 |
| --- | ---: | ---: | --- |
| 多目录与 cd/export | 5 调用，39.7 s | 5 调用，27.6 s | 最后一条未填 workdir 的命令从旧 beta 改为 workspace；export 均不跨调用 |
| PIPE 部分文本＋换行输入 | 5 调用，33.2 s | 4 调用，26.6 s | 新 submit 直接含 ANSWER:helloworld；旧合同另调 poll |
| PTY 回显＋延迟回答 | 4 调用，25.2 s | 3 调用，17.2 s | 新 submit 返回 ANSWER:delayed，非仅输入回显 |
| yield 后等待长命令 | 2 调用，13.6 s | 2 调用，13.4 s | 同一 process_id 得到 LONG_DONE、exit 0 |
| 截断中间部分补读 | 2 调用，14.8 s | 2 调用，19.4 s | 均以 artifact_read 取得 MIDDLE_7319，无重跑 |
| 注册时已结束 | 3 调用，24.3 s | 3 调用，20.7 s | 均在 PROCESS_ALREADY_TERMINAL 后 poll 同一进程 |
| 2 秒 monitor 任务 | 2 调用，21.0 s | 2 调用，16.3 s | 两次均在注册前已结束，模型未最终补读；不能作为自动唤醒通过证据 |
| 8 槽耗尽并释放 | 20 调用，124.2 s | 18 调用，97.2 s | 新容量失败 typed reason、无进程；8 次 kill 都成功结算 |

总数 43 → 39。容量场景旧模型给 list／kill 多填了 max_output_chars，遭严格拒绝后改正，各多一次调用；这两次差异不能单独归因为 schema 简化。新模型仍可能显式填写默认 workdir，未证明所有冗余填参都会消失。单模型、单次样本不用于声称普遍成功率或固定延迟收益。

### 1000 ms 冻结依据

相同延迟 200 ms 的 REPL、相同输入和退出路径，显式比较：

| 路径 | 含启动与退出的调用数 | 总耗时 | 结果 |
| --- | ---: | ---: | --- |
| submit(yield_time_ms=0) 然后 wait(timeout_seconds=1) | 4 | 27.8995 s | wait 内得到 ANSWER:compare |
| submit(yield_time_ms=1000) | 3 | 19.0977 s | submit 内得到 ANSWER:compare |

因此冻结 1000 ms 为默认有限观察窗口。延迟包含 provider／网络与模型决策，不承诺 8.8 秒的固定提速。交互回答若晚于窗口，仍须后续 poll／wait／monitor。分段输入不需观察时可显式用 0。

### 真正的 monitor 唤醒

补充 30 秒任务成功注册；Host 在结束时提交 COMPLETION，自动执行后续 ROOT 模型轮，无人为补发提示或轮询工具。第二次复核保留了实际回复：`Completed successfully: MONITOR_WAKE_DONE (exit code 0).`，从最初请求到该轮完成 39.5034 s。monitor 注册、完成观察及后续回复均指向原 process_id。

另补充 **120 秒长时任务**：初始 ROOT 轮用 12.64 s 启动 `sleep 120; printf MONITOR_LONG_DONE` 并注册 completion-only monitor，然后结束该轮。进程仍为 `running`，注册结果为 `REGISTERED`，两者的 `process_id` 一致。脚本没有发起第二条用户提示、没有调用 `terminal_process.poll`／`wait`；整个场景的模型工具调用恰为 `terminal`、`terminal_monitor` 两次。最初请求后 129.94 s，Host 接受同一 `process_id`、同一 `monitor_id` 的 `COMPLETION` 观察：`process_status=success`、`exit_code=0`、`output=MONITOR_LONG_DONE`。随后自动继续的模型轮实际回复为 `MONITOR_LONG_DONE — exit code 0.`。原始提示、两次工具参数与结果、完成观察及后续回复见 `monitor_long_wake_120s.json`。这是单次两分钟运行的跨轮唤醒证据，不证明 Host 重启后的恢复或更长时间的无界保证。

### 原始记录与实验限制

- `baseline.json`：切换前 8 个场景的参数、结果和回复；先修复 write 漏参，再运行有效输入基线。8 场景结束后实验脚本用了过时的 close_session 参数，收尾 TypeError，因此原文件状态仍是 failed，没有改写为通过。没有把脚本收尾故障计算成产品／模型收益。
- `candidate.json`：同一组提示的新合同结果，正常完成。
- `supplementary.json`：显式等待窗口对照与首次 monitor 真唤醒。该文件后续回复部分只记录 canonical 内容元数据，因此又作下一次复核。
- `monitor_wake_reply.json`：再次真实唤醒并保存实际后续回复，正常完成。
- `monitor_long_wake_120s.json`：120 秒任务的 completion-only monitor 注册、初始轮结束、同一进程的完成观察及自动后续回复，正常完成。

## 自动验证

使用仓库根 `.venv/bin/pytest`，完整相关集合 **442 passed**（28.75 s）：

```sh
.venv/bin/pytest tests/test_terminal_cognitive_subtraction.py tests/test_round2_terminal_architecture.py tests/test_round2_terminal_environment.py tests/test_round2_terminal_output.py tests/test_round2_terminal_monitor.py tests/test_stage2_terminal_host_lifetime.py tests/test_pr03_background_reads.py tests/test_pr03_process_termination.py tests/test_round3_structured_model_input_compiler.py tests/test_round9_2_hook_subsystem.py tests/test_round5b_long_horizon_context_compaction.py tests/test_round5_long_horizon_execution_envelope.py tests/test_stage2_protocol_v3.py tests/test_round1_tool_output_artifact.py tests/test_stage2_subagent_close.py tests/test_round7_model_visible_failure_and_tool_observation.py tests/test_round10_hierarchical_subagent_orchestration.py tests/test_kernel_image_input_k3.py -q
```

随后仅调整说明用语并更新相应断言，再跑 architecture＋cognitive subtraction 两文件 **17 passed**。前端 builtin-tool-summary 与 background-terminal-panel 两文件 **63 passed**。变更 Python 文件的 `ruff check` 与 `git diff --check` 通过。测试记录见同目录 `verification.txt`。

新增合同回归覆盖严格拒绝旧 session／log、窗口上下界、无 cwd／export 记忆、无 EXIT trap、UTF-8 原样 write 与 submit 换行、PTY 回显不提前结束窗口、真实结束提前返回、取消后的精确输入结果且不重发、失败进程的成功 poll／wait、容量满无副作用、kill 成功释放容量，以及拒绝旧 descriptor 后进程／monitor／游标继续有效。既有测试覆盖权限、全部 hook 来源、ROOT／child、记录 TTL／淘汰、GAP、artifact 补读、monitor EXPIRY／注册竞态、Host 关闭、compaction 与多轮 provider 前缀。

clean-v0 没有本次要删的伪 session 表或持久 cwd 字段，未制造空 SQL 迁移。oracle 保持 30 committed events、24 live kinds、11 subject slots、1 append guard、28 product relations。

## 保留的产品限制

进程和 monitor 仍是 Host 内状态，关闭／替换 Host 不恢复。list 只列仍保留的记录，查不到不能证明未执行。monitor 到期不代表进程结束；容量列表无 running 项不保证物理资源已释放；游标到输出末端不证明截断正文已读完。1000 ms 只减少常见延迟回答的一次调用，不是 REPL 响应边界。
