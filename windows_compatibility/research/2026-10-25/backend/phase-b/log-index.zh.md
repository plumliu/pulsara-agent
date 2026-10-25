# 小日志索引

| 文件 | 实际结果/用途 |
| --- | --- |
| real-run.json | 最终 Windows Node→真实 WSL PG17.9 纵切 exit0；cleanup exit0/status3/两锁可得/专用目录删除 |
| real-result.json | 最终 SQL、kernel/PG 锁、reset 实删/重建、维护准入事实 |
| first.real-result.json | 增加维护检查前的首个成功正控，历史结果保留 |
| cleanup-result.json | 首个专用 PG home 的精确清理 |
| backend-full-progress.json | 64项完整运行，64 passed，14.85秒；私有源码 removed=true |
| backend-quick-result.json | 最后增加 canonical 接纳替换负例后的后端9项，9 passed，2.48秒；removed=true |
| first.backend-quick-result.json | 初始 DSN/socket fixture 三项失败，不改写 |
| backend-test-result.json | 首次组合未完成，保留中断事实 |
| first.backend-full-progress.json | 原 OAuth fixture 等待处中断，保留45项后现场 |
| oauth-diagnose.json | 定位隔离快照缺静态logo→callback500；随后仅修测试快照 |
| installed-first-start-failure.jsonl | 真实已装首启catalog drift、精确systemid与canonical sessions0、仅内存换行对照 |
| lf-resource-tests.json | 同一新增资源回归原registry2fail/修复2pass，私有源码 |
| lf-fresh-result.json | 新wheel实际安装后原TS owners首启/正常关闭/重开、deep verify及同实例Windows HTTPS逐次时间 |
| first/second/third.lf-fresh-failure.json | InvalidURL、即时ECONNREFUSED、错误route404的原测试失败，分别保留 |
| lf-fresh-cleanup.json及first/second/third.lf-fresh-cleanup.json | 四个自有测试root真实status3/两锁可得后精确删除 |
| windows-resource-tests-missing-dependency.log | Windowsvenv缺psycopg，conftest导入exit4；不当作测试通过 |
| existing-migration-regression.json | 原stage5迁移套件+新portability：14pass/1缺pgcrypto资源前置失败；原断言不改，专用PG清理removed=true |

Windows tsc、新 finite client7项及 Ruff 的工具输出由会话/root汇总记录，本目录没有伪造对应 JSON。复跑命令从仓库根使用 `.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/phase-b-pg/verify_real.py`；Python聚焦为相同解释器调用 `test_backend_snapshot.py --full-progress`。这两条都会创建新的精确隔离目录，且覆盖自己的最终结果文件；归档后再跑须先另存历史结果。
