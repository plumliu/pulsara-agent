# 第三轮 PG 双锁候选独立交叉审查

2026-10-25，interop_races_r3；只读源码与证据，不重跑PG、不修改PG分支。已读同轮 `pg_admission_r3/round3` 的 helper.py、finite.py、guarded-reset.py、fault.c、control.py、最终report.zh.md，以及 results/stop-results/initdb-results/reset-results 的完整相关case事实与必要records。

**最终报告对候选的核心结论与源码/事实一致：独立 PGDATA 外 EX生命周期锁可以阻断已知迟启动空窗；当前reset guard在status3但PG锁busy时拒删，重新取双锁和当前status3后才实际删除。它是机制候选正证，不是完整G0-W-P或产品采用批准。未发现需要阻止本次有限证据归档的矛盾。**

核对要点：

- start helper在fork前先取kernel EX和独立PG EX，只把PG fd pass给真实pg_ctl→postgres；helper只close，没有显式LOCK_UN。真实setsid/exec case helper死亡后kernel可取而PG EX busy，竞争start返回74、不接纳operation；旧启动恢复后query真成功。运行PG不再占kernel，这直接对应第二轮空窗。
- 真实stop有限后代只继承kernel。stop执行前门保留原PG运行，不假称停止；stop完成但pg_ctl exit前门中status3/PG EX可取而kernel仍busy，防止旧有限stop未结束就准入新kernel/start/reset。最终报告准确区分。
- 最终initdb两门的真实initdb和bootstrap postgres fd快照均带kernel和PG两锁；kernel-only历史结果另名保留，没有拿它替代最终双锁事实。
- guarded-reset当前调用取kernel→fresh status；如running先核准确data/socket并stop，子继承kernel；之后非阻塞PG EX→再次fresh status3→双锁仍持有时rmtree仅PGDATA。PG锁文件位于其外，未unlink；相同inode/socket/outside sentinel实际保留。未知status/stop失败走断言，不到删除；PG busy返回deleted=false。
- reset-results负控为旧真实postgres625 exec暂停、helper死、status3但PG EX busy：sentinel保留；恢复旧PG后真实query成功，准确stop；新guard重新取两锁再删除，status4准确区分目录不存在，真实重建/query成功。这是实际删除闭环，非纸面delete_admitted标志。

报告已采纳两项必要限制。第一，guard初始status0分支可能先stop再遇PG busy；busy保证不删除，不能说整个调用无副作用。第二，报告的删除正控先在独立调用停库，再让guard重新取事实；guard内部running→stop→delete分支尚未单独验证，不应宣称覆盖全部reset分支。

`control.py` signal/resume仍是cmdline/环境核对后os.kill，未持pidfd或原子starttime身份保护；cleanup扫描跳过PermissionError。最终报告已明确仅为本次可观察当前用户范围，未把清理工具当生产原子身份机制。PG锁busy不提供身份/readiness/成功，原EOF/截断/失联仍unknown、禁止重放；最终报告也已保留这些限定。

尚未验证真实TS owner/JSONL生命周期、普通EOF与取消/Windows载体失联、guard各失败分支、全入口hard cut和外部未持锁实例的明确接纳规则。升级PG后须复验继承，不把lock文件内容/存在当执行authority。本审查未运行删除、未读取saved settings或provider，未对PG做任何新变更。
