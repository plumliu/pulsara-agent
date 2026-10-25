# G0之后的共享owner独立修正

2026-10-25，修改基线`9e1150b7a4b16e212be36329182cbaa505c4df79`。用户授权澄清README并修复三类问题，同时要求不进入Phase B。复用原三位gpt-6.1-sol/high分别实施Runtime、PG及只读边界/交叉审阅。

## 完成与未完成

1. **Runtime共享缺陷已修复。** `stop()`即使发现child已清空，仍检查原exit Promise；ready事实移入同一close结果，避免新start清空旧attempt的事实。未启动stop仍无操作；正常退出允许继续；曾ready的非零或signal退出仍报收尾失败。原main在该失败下保留PG、不调用app.exit(0)。未改变ready后failed帧但最终exit0的既有判据；这不是Windows carrier/EOF/SIGHUP完整接入。
2. **PG共享owner错误分类已修复。** 保留Node维护的execFile及原Error/output对象，POSIX机制将启动前错误标为not-submitted，完整数值非零且无signal/killed标为failed，其余不完整结果标为unknown。BundledPostgres仅对明确failed启动走原实例读回；未知错误不能认领其他调用启动的同库，也不清空此前startedHere。启动/reset的status3须同时为failed才可准入。WSL helper、双锁、迟启动与跨域装配未实现，分类本身不替代准入锁。
3. **Windows后代跨域回收未实施。** 原POSIX状态没有Windows Job/HANDLE/relay范围，Linux进程全部结束也无法推出Windows无pipe后代已退出。完整修复需要spec §5及§11.2的正式Windows接入，不能通过空接口、延迟Linux完成或导入研究探针解决。本轮已向用户说明范围冲突并请求明确取舍，收到新指示前保持不进入B。

这三项不能统称为已在macOS原生复现的缺陷。Runtime是现有macOS也使用的共享逻辑问题；PG未提交/unknown反例来自跨域研究接缝；terminal原反例是Windows接入缺口。本轮没有原生Mac/应用包GUI验收，旧Phase A实证保留为历史基线。

## 验证

| 检查 | 结果及范围 |
| --- | --- |
| Runtime修前/修后 | 原实现配新增可移植回归13/15，已关闭非零退出和旧stop/newstart两项复现；修后Windows Node22.23.3与WSL各15/15。真实Node child/pipe，测试入口直接使用Node，取消POSIX shell包装。 |
| PG Windows定向 | tsc通过；真实本机execFile边界5项（含父测试）及owner分类4项通过。缺文件、非法argv、真实exit7保stdout/stderr、先产生副作用再超4MiB导致unknown均覆盖。 |
| PG WSL定向 | 私有ext4源码快照编译通过，PG测试15/15；额外真实SIGTERM得到code=null、unknown。没有改Linux主clone源码。 |
| main退出正负控 | 新增正常退出停PG与Runtime失败保PG两项均通过。Windows运行整个main.test为15/16：既有Linux路径断言期望`/opt/...`，Windows实际path.join产生反斜杠；未改断言，在适用POSIX环境重验。 |
| WSL全desktop回归 | 当前稳定源码复制到Linux output私有目录，使用Linux本地依赖和固定Node编译；9个测试文件60/60通过，0 skip/xfail。包含上述main原路径断言。 |
| 全集首次失败 | 初次快照漏复制scripts/linux-cli-entry.cjs，55通过/1失败；补齐scripts后完整重跑60/60，原日志保留。未修改产品或测试断言来消除此探针装配失败。 |
| 静态及交叉审阅 | Windows全desktop tsc、git diff --check通过；第三位agent只读审阅实际上下游，未发现新增阻断。 |

测试使用隔离文件和真实有限子进程；本轮没有重新运行真实PG服务/模型provider/Notion/睡眠/完整GUI，未声称这些场景获得新生产资格。当前生产修改仅为desktop/src/runtime.ts及PG三文件；main.test/runtime.test/PG tests随合同修正。未实现新Windows adapter、第二状态owner、持久receipt或新生命周期上限。

本地完整日志位于`output/windows-g0/root/shared-owner-fixes/`、`output/windows-g0/pg_admission_r3/owner-fix/`。本报告随附WSL全集的原失败/最终日志和PG结果；不保存代码/文档内容hash。

## 另外发现的共享terminal问题

边界核对另发现两项独立POSIX早期启动失败问题：foreground Timer.start异常会留下启动预约，PTY openpty后Popen失败会漏关master fd。隔离探针已复现，现有terminal测试28项通过并不能覆盖它们。它们不是原Windows跨域问题的替代修复；本轮保留复现与说明，尚未修改生产。细节见附带terminal-note.zh.md及terminal-observed.json，探针精确关闭其fd，没有运行模型任务或终止共享Ubuntu。

interop、Hiddify TUN及共享Ubuntu配置保持；Windows支持未激活。本轮更改尚未提交，Linux主clone保持原提交，测试在隔离源码快照执行。
