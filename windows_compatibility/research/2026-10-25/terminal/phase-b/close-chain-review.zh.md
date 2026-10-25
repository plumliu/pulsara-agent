# 最后一跳只读核对：重复 shutdown 不会洗掉 MCP close 失败

结论：当前顶层已有足够的失败责任保护，**无需新增生产修改**。本次只沿 Session → Core → Application → desktop_runtime → Windows runtime 的关闭链核对，未扩大审计或重跑新矩阵。

`KernelHostSession.aclose` 确实在 `host.py:7433` 左右先设 `_closed=True` 再抛 `close_error`。如果绕过生产 owner 直接再次调用该 Session 方法，会走早返回；生产路径由下面的已有 owner 阻止这个绕过：

1. `host.py:8914`，`KernelHostCore.close_session` 查原 `_close_attempts`，存在就继续 shield 同一个 `attempt.task`，不重新调用 Session。
2. `host.py:8944–8960`，原 `_close_session_owner` 捕获 Session 失败后标记 `CLOSE_FAILED_QUARANTINED` 并重抛。它不会执行后面的成功分支，不删除 `_sessions` 或 `_close_attempts`。之后的 close_session/shutdown 都仍持有首个失败 task。
3. `host.py:8989–9026`，Core.shutdown 再以 `_shutdown_task` 固定同一次 `_shutdown_owner`；每次 `task.result()` 都重抛原错误，原 shutdown_owner 通过 close_session 进入上述保护。
4. `desktop_app/application.py:365–383`，Application.aclose 固定 `_close_task` 并每次 `task.result()`。`_close_owner` 在 416 行收集 core.shutdown 的错误，430 行重抛。虽然它可能把状态字段设为 STOPPED，重复 aclose 不会再执行能早返回的 `_close_owner`，而是仍取失败 task 的 result。
5. `desktop_runtime.py:378–387`，Application.aclose 抛出的 `InteropScopeError` 属于 Exception，finally 捕获后固定本次 `failed=True` 并 emit failed；之后仍发送 stopped，但函数返回 exit 1。stopped 单帧不表示 clean。
6. 当前 `desktop/src/runtime.ts:43,109,177–185` 将 `code/signal/startSent/stopped/failure` 保存在本次 child 的 exit 结果；stop 即使 child 引用已空也等待该结果。已发 start 的 attempt 有非零 code、failed 帧，或 WSL 缺 stopped 任一项均拒绝 clean stop。MCP scope 只可能在已接受 start、Application 启动后存在，因此本次错误落在该检查范围内。

新 Supervisor 的失败因此到达原 KernelHostSession 的 `close_error`（`host.py:7411–7417`），沿已有的三个 memoized owner 固定为失败，最后变成 failed + stopped + exit1；不会因第二次 shutdown 而变为无 failed 的 exit0，main 也不能因此继续正常 PG stop。

已有 focused 代码证据还包括 `tests/test_stage2_terminal_host_lifetime.py:649`：真实 Core.close_session 入口对失败 Session 连续 close，断言唯一 attempt 处于 `CLOSE_FAILED_QUARANTINED`、session 仍在 inventory，重复调用仍抛。这里只读取该测试，未冒称本次重新执行，也未把它替代成新的真实 kernel/PG 实验。

生产冻结时间保持 `supervisor.py` UTC 2026-10-25 08:21:35；本次仅新增这页审阅说明。
