# Phase A PG + desktop 跨专题实现审阅

2026-10-24。依据冻结根规格 §11.1/§13.1、AGENTS 与本轮只实现 A 的授权，审阅当前生产 diff、新 port/locator 和相关测试。不改他人生产文件，不进入 B。

**结论：当前 PG 与 desktop 切片没有阻断，可以进入 root 统一最终验证。** 这是代码审阅与本机 Node测试结论，不把未取得的 native Ubuntu/安装 GUI证据标为完成。

## PG owner / physical port

已读 `postgres.ts` 与 HEAD差异、`postgres-operations.ts`、`postgres-posix.ts`、`postgres.test.ts`。

- BundledPostgres 保留 initialized/startedHere、start/status/verify/readiness失败取回原错误、stop/reset的唯一业务顺序与所有中文错误；物理port不作额外 lifecycle决定。
- 原 PG binary路径/LC_ALL=C/4MiB env+exec、mkdir/chmod、PG_VERSION/postmaster.pid/realpath/stat/rm 位于POSIX叶；目录/socket/log路径以同一home构造。没有secret/PG设置副本、PID kill猜测、durable recovery 或WSLhelper占位。
- reuse使用 resolvedDirectory，reset使用 rawDirectory，未因共享 readInstance 改成接受reset别名；uid/mode、symlink拒绝、104-byte protocol边界与DSN转义保留。
- 失败不能更新错误state或继续删除的测试覆盖关键 owner结果；保留原真实 fake-pg_ctl进程/文件测试，没有用纯mock完全替代OS事实。统一 Node run 中PG 7条均过。

## Desktop locator / main / CLI

已读 `resource-paths.ts`、`main.ts`/`cli-install.ts` 与HEAD差异、main/CLI测试变化。

- locator仅纯path.join五个实际已消费字段；无I/O、manifest、settings、cacheauthority、fingerprint、platform容器或运行时freeze。
- main在原模块装配时同步调用原Pythonhome resolver，中文错误/app.exit时点保持；同一个paths/home/host注入runtime，并给PG port同一postgresBin/home。boot/reset/quit、旧owner等待和cover并行门未动。
- Linux /opt packageTarget判定继续使用原root startsWith与同一bin/pulsara结果，用户CLI注册/冲突/PATH owner不变。cliLauncherScript硬切输入为已解析Python路径后输出原脚本bytes与shell quoting，调用方/真实 argv及退出测试全部接齐，无旧签名fallback。
- 新packaged场景夹具缺setAsDefaultProtocolClient仅是测试缺API；补mock而未改生产后45条全部通过。原CLI实际child与路径重定位测试仍在。

## 全局边界

本候选只抽真实 Node资源/执行/PG物理接缝，未改终端/file/内部Unix transport，不夹带 TLS/OAuth/interop/WSL部署或原关闭unknown修复。未添加DB schema、durable事件、总寿命cap或多实现fallback。生产配置未触及。root应继续按规格分层记录真实macOS/Ubuntu/native资源/installedGUI证据，证据缺口不能由本次mocked main/Node测试补称。

唯一首次协作 Node实测：`npm --prefix desktop test` 最终 tsc成功、45 passed/0 failed/0 skipped；完整输出见本目录 `npm-test-candidate.log`。

后续 root 发现该候选 `readInstance` 总是计算 resolvedDirectory，会给 reset 的 malformed PID 路径新增已删除 cwd 的依赖。前轮审阅未识别这条异常行为差异，不能把上文 raw/reset 检查等价结论当作已覆盖此反例。backend 已改为必传 `'raw' | 'resolved'` 并加独立 child 回归；最终复核见本目录 `cross-review-final.md`。此处 45 passed 是修复前候选实测，不冒充修复后最终结果。
