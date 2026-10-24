# 对 PG 第三轮最终候选的只读交叉审查

2026-10-25，interop_tasks_r3。只读审查 `output/windows-g0/pg_admission_r3/round3/{helper.py,finite.py,guarded-reset.py,reset-harness.py,fault.c,control.py,report.zh.md}`，以及最终 results/stop-results/initdb-results/reset-results 与 cleanup-audit；未重跑、未改 PG 探针、生产代码或规范。

**未发现新增机制阻塞。** 源码与受测结果一致支持下列窄结论：start 的常驻链仅继承独立 PG lifetime EX，有限 initdb 链继承两锁；有限 stop 链继承 kernel 锁。锁以继承的同一 open file description 延长存活，helper 不 LOCK_UN。guarded reset 先当前 kernel，再当前 PG EX，重新确认当前 status3 后才删除 PGDATA，锁 inode 在 PGDATA 外保持；reset 原始 facts 确认 busy+status3 拒删、恢复真实 PG 查询、准确停止、双锁内删除、status4、真实重建查询及外部 sentinel/socket/inode 保留。没有把 lock busy 当 readiness 或实例身份。

报告已明确排除外部不遵守锁协议的启动、非原子 PID cleanup、完整 TS owner/JSONL/GUI/失联/真实维护全部分支；准确 running→stop→delete 的 guard 分支源码存在，但实际删除正控在独立调用先 stop，报告没有扩大为该分支已覆盖。

有一处窄的报告表述应收缩：helper.py 的 start 后 `readback = subprocess.run(pg_ctl status, ...)` 没有传 `pass_fds=(fd,)`。因此 action 表“stop/status 等有限命令继承 kernel”不能覆盖该 readback status 的 helper 死亡窗口。它只读，不提供迟到写入/删除能力，不推翻本轮 mutation admission 的结果；应注明此只读诊断不在已验证有限 mutator 继承合同内。已告知 PG agent 和 root，无需为此重跑。

本审查不升级完整 G0-W-P/G0-W 或 Phase B 资格。
