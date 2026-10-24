# PG死亡实验独立交叉审查

2026-10-24，interop_jobs；只读审查，不重跑、不改PG探针/证据。

已读 `output/windows-g0/pg_death/round2/report.zh.md`、fault.c、helper.py、harness.py、control.py、audit.py、storage/provenance及ext4两份完整results JSON。**报告的核心反例与原始证据一致，可作为当前有限helper候选未通过的证据，不能据此宣布完整G0-P通过。**

- 真实阶段成立。fault hook用实际 `/proc/self/exe` 匹配pg_ctl/postgres；fork/setsid/exit都转发真实libc，PG constructor和 `_PG_init` 只暂停。日志显示pg_ctl或postgres的真实exe/cmdline及T态，不是 shell exec前暂停冒充PG阶段。`postgres -V`误拦的早期失败单列保留，最终hook要求本次home，未拿该失败作反例。
- ext4修正真实。storage.log明确 `/tmp` 是tmpfs；最终7+3项改用 `/home/plumwsl/.g0d2-*`，data device2096、uid1000/0700。报告没有沿用tmpfs结果宣称ext4资格；未带hook的plain正控和带hook不暂停正控均成功。
- status3反例因果明确。补充案例中helper死亡、后续新进程持同一kernel.lock调用pg_ctl status=3时，旧启动子1364/实际postgres1470仍暂停。随后resume同一进程，status0且psql返回精确data/socket与启动时间。说明可取锁+瞬时status3不足以授权reset；实际未执行破坏性reset。主矩阵pgctl-before-fork恢复后没有晚启动，报告如实限定，没有把所有点说成会晚启动。
- fd继承不是修复。inherit案例真实pg_ctl和postgres的fd3均指向本次kernel.lock；helper死后、pg_ctl退出后仍busy。正常无暂停inherit案例helper exit0后postmaster1247仍持fd3、后续lock取不到。报告正确限定为朴素pass_fds污染常驻PG生命周期，没有断言所有可能fd方案均不可能。
- cleanup证据足以支持本轮操作记录：按真实pidfile核data/socket、pg_ctl stop -w、status3、对应data argv扫描为空后删除私有home；最终audit核25个指定home不存在，cmdline或G0_HOOK_HOME无命中。**这些是具体观察，不是pidfd/creation identity原子保证**。control.py的signal/resume使用本次home/data cmdline核对再os.kill，未同时校验/protect Linux starttime或pidfd；audit对PermissionError跳过，故不宜扩写成所有权限域/任意后代的普遍无残留保证。当前报告末节没有提出该过强保证。

给作者的唯一措辞建议是把“能覆盖普通EOF/丢回复”限定为首轮已测子场景；本轮矩阵主要证明异常死亡反例，并未独立遍历所有EOF/丢回复情形。没有阻止本轮反例归档的问题。

未审完整生产PG/资源检索资格或破坏性reset；未要求重跑。主要风险仍是最后CLOEXEC持锁helper死亡后，旧真实PG启动继续，当前候选未能关闭维护准入空窗。
