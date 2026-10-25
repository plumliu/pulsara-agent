# macOS terminal/hooks/MCP 回归与窄修

当前结论：同意本次 terminal/hooks/MCP 窄修与 macOS 已执行证据通过，无本专题冻结阻断。最终 fresh bundled wheel 已实际重跑四场景并完成清理；真实 OS sleep/wake 依用户约束未执行，仍待验收，未据此宣称通过。

## 已执行回归

- 原 venv POSIX 套件：167 passed；私有 PG terminal host/persistence：4 passed。命令、平台、实际 module 来源、私有 PG 数据目录/system id 与 fixture cleanup 在 `suites-results.json`、两个 command.json 和原日志。显式 CI=true，零 skip/xfail。PG 仅 root 提供的临时 cluster，由 fixture 创建和删除 UUID DB。
- 初轮 fresh `desktop/resources/python/bin/python3 -I` 真实 provider：directories/PIPE/PTY/monitor_wake 全过；19 次 provider call、14 次 tool call，38 个工具保持。精确 SYSTEM/tools 不变，messages 全程 append-only；monitor 在38.20秒自主唤醒同 process，exit0，未人工 follow-up。完整 prompts/replies/tools 保留于 `provider-results.json`，仅实际 credential 值 scrub；`provider-validation.json` 有离线精确断言及 DB/home/workspace/repl 清理。此轮 wheel 尚未含本次源码窄修与 backend prospective 修复，因此最终候选仍需复跑。
- 两次输出 harness 自身错误分别为 tests import 与 scrub bytes/str 类型，日志原样保留；不当产品失败、不计成功证据。已修 harness 后完成上条完整 provider 运行。

## 找到并修复的真实回归

额外 native fork-tail 探针：原 leader exit0 后，尾子进程输出 NATURAL_TAIL_DONE 并自然退出，hooks 偶发返回 HOOK_EXECUTOR_FAILURE，吞掉成功输出。`hook-exceptions.jsonl` 捕到 `hooks/executor.py _wait_process_scope→_process_group_live→killpg(0)` 的 EPERM；清理 TERM 再次 EPERM。不是真实权限提升，脚本与子进程均为当前用户。原失败记录在 `real-scope-repeat-2.*`、`hook-focused*.log`，未覆写或删去。

Apple XNU 的 [killpg1 源码](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/kern/kern_sig.c) 过滤 SZOMB，仅含尚未 reap 的 zombie group 能返回 EPERM；未找到进程组时为 ESRCH。这与本机真实 unreaped child 观察一致；未声称精确匹配本机 kernel 的公开版本。

最小修改仅两条既有 leaf：`hooks/executor.py` 与 `conversation_kernel/mcp/sdk_facade.py`，直接对齐现 `terminal_process/manager.py`、`environment.py` 策略：EPERM 仍可能存活；signal EPERM 不覆盖原执行/reader/close 结果，继续原物理 join；只有后续 ESRCH 才证明组消失。没有新增 timeout、ps authority、durability、registry、重试上限或 Windows interop 协议。测试里的 ps 仅为证明实际 child 已处于 Z；生产不使用 ps。

- `zombie-before.log`：新增真实测试在旧源码 **2 failed, 1 passed**，分别直接触发 hook group existence EPERM、MCP close TERM EPERM。
- `zombie-after-focused.log`：同新增三项 + 原 hook subsystem/MCP transport lifecycle/windows interop failures 全套 **68 passed**，包括取消、spawn/task failure、close failure 与物理 join 负例，未减少断言。
- `hook-focused-after.json/log`：原0.25秒自然尾进程 **连续30次通过**，每次输出准确、group消失、8个worker完结、permit归还。
- `real-scope-after.json/log`：原 leader 已退、tail忽略 TERM 的 MCP close 连续两次 cancel 仍完成同一 close task，等待真实group空；hook natural/cancel 两种尾进程场景均通过，输出或 HOOK_CANCELLED、group消失、worker/permit全部结算。

## 边界

未操作 /Applications 已运行 Pulsara、其 PG 或用户 CLI/settings；生产设置仅通过原 resolver/store 只读取出并在内存覆写为 root 私有 PG，未写回或把 credential 导出 env。未执行 Node/frontend/build/mise/uv sync，root 统一构建。没有真实 OS sleep/wake 证据。backend ROOT 修复的只读交叉审在 `backend-cross-review.md`，最终真实 context 由 backend 复跑。

## 最终同一随包候选确认

root 的 verify-fixed 完成后，使用 fresh `desktop/resources/python/bin/python3 -I provider_final_probe.py` 重跑同四场景；启动前断言实际随包 hooks EPERM 与 ROOT later-head 分支存在。`final-wheel-source-facts.json` 进一步证明三条生产修复均来自同一 bundled site-packages，无 src/PYTHONPATH 覆盖。

- `zombie-final-focused.log` 与 command.json：当前 editable 最终定向 **68 passed in 6.43s**，CI=true，零 skip/xfail。
- `provider-final-results.json` 与 command/run.log：**19** 次真实 provider、**14** 次工具，四场景全 completed；完整提示、38tools schema、响应与所有工具结果保留。
- `provider-final-validation.json/log`：精确 SYSTEM/tools 连续，messages 全程 append-only；alpha/beta/alpha 显式 workdir 正确；某单命令 export inside 后下一独立命令 printf 空值（不把该输出过度声称为 distinguish unset vs set-empty）；PIPE write hello 不加换行、submit world 得 ANSWER:helloworld；PTY 得 ANSWER:delayed；两 REPL 最终 exit0。monitor 39.29 秒自主同 process 唤醒，output MONITOR_WAKE_DONE、exit0，未手工 follow-up。
- 同验证中查询私有 cluster 证实该 UUID DB 不存在，临时 home/workspace 已移除，按独有 workspace 路径检索 owned repl 为空。
- 最终离线输出校验器第一次错误固定了旧模型选择的 `<unset>` 展示及默认 cwd；本次模型选择显式 workdir 和 printf 空值。原日志保存 `provider-final-validation-initial-format-failure.log`。只按本次真实命令校验精确输出、所选 workdir 与未继承 inside，未改 prompt/响应/生产/tests 或删去失败记录。

backend prospective ROOT 源码交叉审仍无阻断；其最终长程 context 与 GUI 总证据以 backend/desktop/root 原始记录为准，不由本报告代替。

最终 lint 收口：root 定向 ruff 发现新增测试 `signal` 为未使用 import，已仅删除该一行；`.venv/bin/ruff check tests/test_posix_process_group_settlement.py` exit0、All checks passed，日志 `ruff-final.log`。无行为变化，按 root 指示不为删除 import 重复运行测试；此前最终68项与随包provider证据继续有效。
