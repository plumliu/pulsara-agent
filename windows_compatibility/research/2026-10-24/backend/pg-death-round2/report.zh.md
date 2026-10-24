# G0-W-P：真实 PG helper 异常死亡第二轮

本轮已将首轮 `helperDeathMechanismPassed=null` 补成**当前 CLOEXEC helper 机制未通过**。真实 `pg_ctl` 已 fork、子进程已 `setsid` 后，或真实 `postgres` 已 exec 后，杀死唯一持锁 helper 会让同一 `kernel.lock` 可取得；后续有限 helper 持锁取得 `status=3` 时，旧启动仍可在恢复调度后把同一专用数据目录启动起来。该反例在 ext4 已重复。它不能授权 reset，完整 G0-W-P/G0-W 均未通过，Phase B 未进入。

仅新增本目录小型探针、日志、报告；没有生产代码/spec/settings 变更，没有提交，没有调用 provider。没有管理/关闭发行版，也没有改 interop、Windows PATH、自动挂载或用户配置。

## 来源与边界

- 阅读根 `AGENTS.md`、`windows_compatibility/README.md`、当前 spec §3.3、G0 checklist §7.4，以及 `research/2026-10-24/backend/g0-w-initial/report.md`、`pg-helper.py`、`helper-harness.py`。
- 实际生产 owner：`desktop/src/postgres.ts`、`postgres-posix.ts`、`postgres-operations.ts`；锁来自 `src/pulsara_agent/desktop_runtime.py::acquire_kernel_lock`。TS `BundledPostgres` 生命周期所有权未改变，本探针不保存 `startedHere`，没有第二 PG owner。
- 实际 PG17.9 源码：Linux clone `desktop/build/postgres/postgresql-17.9/src/bin/pg_ctl/pg_ctl.c` 的 `start_postmaster`：`fork` → child `setsid` → shell `exec postgres`；`wait_for_postmaster_start` 后才报告 ready。另读实际 `postmaster.c`。没有假定 shell/daemon 的 fd 行为。
- 所有 Linux 命令显式 `wsl -d Ubuntu -u plumwsl`。clone `/home/plumwsl/workspaces/pulsara_agent`，Git `fb2c57da12d24ddda32da1320db1d7c645b5ebb6`，UID1000，Ubuntu26.04.1，Python `.venv/bin/python` 3.12.15，PG17.9，util-linux flock2.41.3。来源见 `provenance.log`。
- PG 二进制固定为该 clone 的 `desktop/resources/postgres/bin/{initdb,pg_ctl,postgres,psql}`，dist/resources 已核实存在。无 saved settings 读取、无凭据、无共享 PG。

## 存储审计与正控

`storage.log` 发现当前 `/tmp` 是 **tmpfs**，`/home/plumwsl` 才位于 `/dev/sdd` ext4。此前 `/tmp/g0d2-*` 轮次保留原始证据，证明对应生命周期行为，不能称为 ext4 PG 资格。随后完整重跑7项主矩阵和3项补充，全部新 home 为 `/home/plumwsl/.g0d2-*`，数据与 socket 均在 ext4、当前用户0700，`data_stat.device=2096`。socket完整地址字节数显式断言 `<104`，本轮 home 对应57字节。

ext4 `plain` 正控不设置 `LD_PRELOAD`、不加载额外 PG fault module，只使用原 PG 参数与 `shared_preload_libraries=pg_search`；真实 start/readback/stop 成功，helper退出后锁可得。另有加载同一 fault module 但不触发暂停的正控，也成功。

`fault.c` 只在指定实际进程/阶段写私有 marker 并 `SIGSTOP`，由观察者在杀 helper、检查锁后才 `SIGCONT`。fork/setsid/exit 转发真实 libc 实现；PG `_PG_init` 仅作调度暂停。它没有替换 PG 结果、删 PID 文件、改 data、伪造 readiness，实验不说明自然调度下该窗口的频率。实际 `/proc/PID/exe`、cmdline、status、fd、stat及数据uid/mode/device/inode全部保留。inode可能被删除后的新case复用，未作为跨case身份authority。

## ext4 主矩阵：7项

完整逐命令与帧见 `ext4.log`、`ext4.results.json`。锁列为 helper 死亡后（正控为正常结算后）新有限进程能否取得同一个 `kernel.lock`。

| 场景 | helper / 物理 PID | 观察 | 锁 | 结论 |
| --- | --- | --- | --- | --- |
| CLOEXEC、不暂停正控 | 448 / PG452 | start/readback成功；正常结束 | 可取 | 基本机制正控成立 |
| 真实pg_ctl进入fork前暂停 | 504 / pg_ctl505 | helper死后pg_ctl仍T，`status3`；恢复后pending SIGHUP令其退出，没有晚启动 | 可取 | 证明真实pg_ctl仍存活时锁已释放；此点未观察晚启动 |
| 真实postgres已exec、main前暂停 | 884 / pg_ctl885、PG888 | 尚无postmaster.pid；helper死后status3；恢复后status0且真实查询成功 | 可取 | 确定晚启动反例 |
| 真实postmaster `_PG_init`、readiness前 | 974 / pg_ctl975、PG978 | PID文件已有data但socket行空；status已为0；helper死后startup仍T，恢复后查询成功 | 可取 | `status0`也不等于ready；启动未结算即放锁 |
| pg_ctl确认ready、exit前暂停 | 1064 / pg_ctl1065、PG1068 | PID文件ready；helper死后真实pg_ctl仍T、PG仍运行 | 可取 | 有限子命令未退出即放锁 |
| lock fd经 `pass_fds` 继承，readiness前 | 1154 / pg_ctl1155、PG1158 | helper死亡后锁busy；恢复并pg_ctl消失后PG fd3仍指向kernel.lock | **不可取** | 缩小空窗但污染常驻PG生命周期，不可采用 |
| 继承fd、不暂停正常结束 | 1243 / PG1247 | helper正常exit0、start/readback成功；PG fd3仍持kernel.lock | **不可取** | 正常路径也阻断kernel/后续维护，不是可行修复 |

`pg_ctl-after-ready` 与 `pgctl-before-fork` 的 `/proc` 在 helper 死后仍明确显示真实 `.../bin/pg_ctl` 处于T；这不是首轮的“shell在exec前暂停”。`postmaster-before-ready` 的 PG 是真实 `.../bin/postgres`，并非PID猜测或历史pidfile。

## ext4 补充：同锁后续 helper 与重复晚启动

完整证据见 `ext4-supplement.log`、`ext4-supplement.results.json`。后续 `locked-status` 是新的有限进程，自行 `LOCK_EX|LOCK_NB`，在锁仍持有时调用真实pg_ctl status，结束后关闭fd；没有复用旧wait-owner结果作为授权，也没有实际执行reset/delete。

1. **fork-child已setsid、尚未exec。** home `/home/plumwsl/.g0d2-b6p2c7gq`。helper1360接纳start；实际pg_ctl父1361及fork子1364已产生，子1364完成真实setsid后暂停。杀helper1360后，新helper1383取得同锁（本case lock inode145750），持锁观察status3，原子1364尚未恢复。恢复1364后它exec为真实postgres，同PID存活；`psql`返回精确data/socket及 `2026-10-24 23:54:49.642521+08` postmaster启动时间。
2. **真实postgres exec后重复。** home `/home/plumwsl/.g0d2-f1h_zgd8`。helper1466、pg_ctl1467、实际postgres1470。杀helper1466后，新helper1485持同锁（inode145312）观察status3；恢复1470后status0且真实查询返回精确data/socket及 `2026-10-24 23:54:51.846375+08`。

Windows观察帧时间以kill调用完成为t0：两例分别在约+160ms/+155ms记录后续helper持锁status3；约+411ms/+388ms记录恢复；约+782ms/+759ms真实查询成功。它们是本次有限调用的记录时点，不是产品时限，也不是自然竞态概率估计。关键因果由暂停门控制：**status3及可取锁发生在旧启动恢复之前**。

## 候选方案的最小可行性结论

当前 `CLOEXEC + helper单独持锁 + ready/operation帧` 能覆盖普通EOF/丢回复，但不能满足 helper自身SIGKILL 后仍无inflight启动空窗的目标。该机制应明确为未通过，不能把首次status3、carrier结束或成功wait-owner当成维护/reset许可。

把同一个fd直接传给pg_ctl是最小实验候选，实证否决：PG17.9并未自动关闭该额外fd，常驻postmaster及其后代继承锁会阻断原kernel取得同一锁，也阻断后续有限helper。不能称为“仅pg_ctl继承”。

本轮没有找到满足现合同的已验证替代方案。仅增加一个可被杀死的flock监督父进程，或仅把最后持锁者换个名字，不能从现证据推出任意最后持锁者死亡后的安全性；未实现这种wrapper或第二owner。当前最小安全结论是保留本次unknown、禁止由瞬时status继续reset；下一步需要另行明确维护准入证明及支持故障边界，不能由此报告自动批准新机制。

## 失败留存、运行命令、结束与清理

- `first-run.*`：fault hook受PostgreSQL头文件 `snprintf -> pg_snprintf` 映射影响，暂停门未到达；其未暂停正控已成功。保留原失败；修为libc snprintf后继续。
- `second-run.*`：`postgres-exec` 初次误拦pg_ctl用于二进制校验的 `postgres -V`。身份断言拒绝继续，未当产品反例；随后按当时marker及私有 `G0_HOOK_HOME` 核实仅该PID876，SIGCONT结束，留 `second-run-version-probe-cleanup.log`。最终hook明确只匹配带本次home的真正data进程。
- `baseline.*` / `supplement.*`：修正后的tmpfs实验，无harness失败，保留原证据；`ext4.*` / `ext4-supplement.*`为最终资格使用结果，均无harness失败。`results.json`为最后一次运行结果，与`ext4-supplement.results.json`一致。

所有探针通过Windows仓库 `.venv/Scripts/python.exe` 执行 `harness.py`；每次guest调用完整argv已记录于日志。Linux编译命令：

```sh
cd /home/plumwsl/workspaces/pulsara_agent
cc -shared -fPIC -I desktop/resources/postgres/include/server -I desktop/resources/postgres/include -o output/windows-g0/pg_death/round2/fault.so output/windows-g0/pg_death/round2/fault.c -ldl
```

最终Windows调用与审计入口：

```powershell
.venv/Scripts/python.exe output/windows-g0/pg_death/round2/harness.py
.venv/Scripts/python.exe output/windows-g0/pg_death/round2/harness.py --supplement
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python /home/plumwsl/workspaces/pulsara_agent/output/windows-g0/pg_death/round2/audit.py
```

每case恢复仅本次marker对应进程；在真实pidfile核精确data/socket后，用真实 `pg_ctl stop -w -m fast` 停止本次服务器，要求status3及无对应data argv进程才删除精确私有home。`audit-input.json`收集所有轮次25个精确home；最终 `cleanup-audit.json` 为 `passed=true`、`remaining_homes=[]`、`owned_processes=[]`，还检查私有G0_HOOK_HOME标记进程，避免漏掉无data参数的版本检查后代。

未做破坏性reset，未终止共享distro，未向其他用户进程发送信号。没有声称完整PG/资源检索资格、G0-W或原生Ubuntu资格通过；本轮只把真实helper死亡边界从unknown补成反例与明确未通过。
