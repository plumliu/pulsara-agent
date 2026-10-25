# 正式 PG helper 故障边界：已有覆盖与最小补项计划

本次只读盘点，不执行第二批实验。R0–R5共享差分基线优先；本轮R1的27项证明ROOT准入/prefix，不是PG helper故障资格。历史研究探针不得改称正式生产helper故障实测。

| 边界 | 已有正式证据 | 限定/未闭合 |
| --- | --- | --- |
| 提交前EOF/取消、晚ready不得写帧 | `desktop/src/postgres-wsl.test.ts`真实Node三pipe，取消后字节0/原close已到、not-submitted；`tests/test_desktop_guest_ops.py`真实helper入口门拒early frame、完整operation前/无EOF不得mutate | TS运输对端是Node fixture；helper用例为测试布局，不是完整Windows→WSL→真实PG失联链 |
| 提交后EOF/取消、缺完整result/异常close为unknown | 原TS finite tests覆盖写后取消、partial/duplicate/畸形/invalid UTF8/非零carrier/4MiB边界；等待真实child close；仅settled结果允许采纳 | 这些是生产client的运输实测，没有在真实PG mutator在途时杀生产helper |
| 双锁/FD继承 | 原Python tests真实OS flock/dup/有限子进程fstat；start lifetime不挡kernel、挡竞争start；command ENOENT/exit3/signal/超界分类；Phase B `real-result.json`真实TS owner→正式helper→PG17.9 start/reuse/stop/reset/重建，正常PG期间kernel可取/lifetime busy | 真实PG正常继承有证；helper死亡后PG/pg_ctl继续持锁的确定性门仍主要来自R3–R5研究，不等于生产helper重跑 |
| unknown不得误认startedHere；settled非零保原readback | 原`postgres.test.ts`覆盖typed not-submitted/unknown保原Error、不readback认领/不清既有所有权；POSIX真实ENOENT/非零/signal/截断有分类实测；status3须settled | 真实WSL生产helper的丢result+他者PG/readback归属，及真实pg_ctl readiness非零而PG晚ready的整链对照尚无独立正式故障矩阵 |
| reset/remove/维护拒入 | 正式Python helper实际rmtree/fresh status3/外置锁inode与outside/socket保留，wrong实例/运行中维护拒绝；Phase B真实PG纵切运行中remove拒绝、原reset先stop→双锁fresh guard→删除→重建；正式installer maintenance busy/stopped亦有原记录 | helper死后有限stop仍在途、PG lifetime已空而kernel仍busy，及initdb双FD中断等关键拒入尚未作为正式helper整链新实测 |

证据定位：`windows_compatibility/research/2026-10-25/backend/phase-b/{report.zh.md,log-index.zh.md,real-result.json,real-run.json}`；测试生产实现仍在原`src/`与`desktop/src/`，不import归档生产副本。研究的PG29及typed候选只作选门依据，不能直接升级资格。

R0–R5完成后，最小有区别的正式补证建议如下；不是无目的组合矩阵，也不新增产品队列/receipt/replay/第二startedHere：

1. **真实提交前边界**：生产TS owner+client→实际WSL helper，确定性ready/写点前取消或EOF，确认operation字节0/无mutator，not-submitted且不认领。可用同一私有cluster的独立已核准控制owner启动真实PG作归属对照，原取消owner仍不得靠readback认领。
2. **真实提交后start丢result**：在真实pg_ctl已spawn、PG尚未发布pidfile时暂停并结算helper死亡，观察旧owner unknown；pg_ctl/PG持lifetime锁，status3不能准入竞争start/reset/maintenance，不删sentinel。独立观察者收齐锁/数据事实后才恢复准确gate，读回PG晚ready；不把child.close当成mutator没发生、不自动重发。
3. **有限stop尾部**：PG已退出而原stop命令仍在途时让helper失联，证明有限子仍持kernel锁；此时PG lifetime释放也不能接纳新start/remove。结算旧stop后才恢复准入，不让旧stop误触新PG。
4. **有限initdb中断**：真实initdb在途、helper死后两FD仍覆盖有限mutator；新kernel/维护拒绝，原owner initialized不得因unknown变true，不自动initdb重放。精确子命令退出后显式清理自有数据。
5. **settled非零/readiness对照**：用支持的PG readiness边界令真实pg_ctl结算非零但同实例随后可readback，保原start失败/精确认领语义；与缺result的unknown严格区分。只用测试门/平台注入，不改变产品owner或放宽helper argv合同。

每项都应使用新安装版本的原BundledPostgres/finite client/helper、独立短ext4 home，固定WSL target和正式installed helper。保原失败、实际锁/PG身份/数据/owner字段与精确清理；不可只以测试flag/PGID/Node child.close代替物理事实。不碰用户正式PG、settings、发行版生命周期、interop/TUN。本文件是计划，不是这些补项已执行的声明。
