# writer 续约失败、物理关闭与冷恢复

2026-10-25。修复源码已冻结供本地 alpha9 构建；本报告候选运行使用私有源码快照，不能表述为已安装 alpha7 修好。正式 alpha7 原错误、探针本身失败及修正后结果均保留。未调用 provider，也未操作正式 PG、用户配置、interop/TUN 或发行版生命周期。

## 已确认的旧版本问题

正式 UI 在真实睡眠后空闲，正常 Quit 报 `StaleHostWriter: host writer generation is stale`，因此桌面原安全屏障保留 PG。只读数据库观察是同一 generation/owner 的 lease 已过期，并非证明另一 writer 抢占。旧 renewal 首个异常没有当场 traceback，不能推断它具体为网络错误或 Stale。

`idle-baseline-result.json` 在已安装 alpha7、独立真实 PG 上复现：新建原 Core/session，只将本次唯一 session 的 lease 设为过去，原 `Core.close_session` 在 `SessionEnd → read_session_model_call_binding → _require_writer` 抛相同 Stale。PG 的原 SQL 权限检查正确；问题是观察性质 SessionEnd 的确切失效读取错误也使已经进行的物理关闭成为失败。另一个接缝是 renewal task 异常后永久退出，旧 session 未封准入、controller 仍缓存它。

正式现场的进程身份、过期行及 Retry/正常 Quit 事实由 backend/desktop 的 quit-failure 报告负责；本报告不把健康探针或 PID 存活当 writer 有效资格。

## 最小生产边界

生产改动在 `src/pulsara_agent/conversation_kernel/host.py`，冻结 UTC `2026-10-25T11:05:03.6377037Z`。

- 一个原异常槽记录 renewal 的非取消失败。只读 `writer_unavailable` 表示本 session 不再能保持写者可用性；`writer_lost` 仅表示确切 `StaleHostWriter`，IO 错误不冒充 canonical ownership 已丢失。正常关闭取消 renewal 不触发恢复。
- `_require_open` 封旧 session 准入。原 SQL guard、不允许续活过期 lease 的规则均未改变；不会修改旧 epoch、重建 prefix、重放消息或工具。
- 原 renewal 已完成后，其 done callback 请求并观察原 `Core.close_session`；原 Core 的唯一 close attempt 仍负责物理资源，避免 renewal 自等。关闭失败仍留原 quarantined attempt，重复 close/shutdown 保同一个错误。
- SessionEnd 仅在 guarded binding read 的 `StaleHostWriter` 分支跳过观察 Hook。`fence_ordinary`、Hook dispatch、普通 DB 错误、真实 SDK/scope/IO 关闭错误都继续失败传播。
- 桌面 adapter 使用 `writer_unavailable` 沿原 reconnect/并发 resume 预约，先 join 原 Core close，再原 cold-open；退出期间不得冷开。controller/bridge/UI 具体实现及提示由 desktop 分支报告负责。

`host-working-tree.diff` 包含同文件此前 Phase B 的浏览器/工作区接缝，非全部属于本次 lease 修复。测试 fake 显式初始化新增异常槽；project capability fake 补 renewal callback 接口。一个原 compaction fake 缺少已经存在的 ephemeral invalidation 接口也补齐，不改变产品逻辑或断言。

## 验证事实

| 验证 | 结果与边界 |
| --- | --- |
| 已安装 alpha7 idle 过期正反例 | 旧错误精确复现；只在本次私有 cluster 改唯一 lease 行 |
| 候选 expired idle | 原 Core close/shutdown 成功，原 guard 不变 |
| 候选 expired + 活动 Windows terminal | 原 renewal 检出失效，原 Core 自动关闭；reader/watcher/interop worker 退出，scope physically joined、helper exit0、socket 目录退休；随后原 cold resume generation1→2 |
| 候选 renewal IO unknown | 受控原 repository renewal IO 错误封准入；原 close 完成后冷恢复1→2，不声称旧 ownership 已被别人取得 |
| 最新 controller 并发恢复 | 两个 resume 返回同一新 handle，旧 session 已 closed，generation1→2；原 controller/Core 正常结束 |
| 新定向测试 | 9/9，其中4项在真实私有 PG：失效 guard 仍拒绝、过期关闭可完成、普通 binding IO/fence Stale/dispatch Stale 错误均保原 cause 与重复关闭失败屏障 |
| 原相关调用回归 | 315 passed / 1 skipped；skip 是既有 PostgreSQL 条件用例，独立于本次另行真实 PG 的9项结果 |
| 最新 controller/bridge | 29/29，含退出时 join 后不冷开、失败 quarantine、取消 waiter 不取消 owner |

Ruff 与 `git diff --check` 成功。原 315 项覆盖 Host lifetime、MCP live status、控制反馈、用户控制、项目能力发布、compaction、协议、Plan、usage、图像输入；没有重复真实 provider 长程矩阵。

保留的失败：`baseline-result.json` 是探针误用不存在的 `.exec` API，不能作为产品反例，已精确结束该自有 harness；`candidate-1-result.json` 是探针漏传原 policy deadline/resume workspace 参数；`regression-pg-1-result.json` 是已安装运行时不带 pytest，随后用项目 venv 对私有快照运行；`regression-1/2.log` 保留 fake 未初始化新增字段及原缺 ephemeral 接缝的失败。均未弱化原产品断言。

## 命令与清理

编排为锁定 Windows Node22.23.3 执行 `owner.cjs <label>`；原 `BundledPostgres/createWslPostgresOperations/createWslHostOperations` 创建本次私有 PG，`finally` 原 `resetConfirmed()` 后才移除精确私有根。baseline/真实候选用已安装 alpha7 Python；候选 source 是私有 ext4 snapshot，未修改 Linux 主 clone。PG pytest 用项目完整依赖 venv 与同一私有 snapshot。

命令标签与结果：`idle-baseline`、`candidate-2`、`candidate-controller-final`、`regression-pg-2`。pytest 的真实参数、fixture 输入和断言在所附脚本/源码及原结果中。

`cleanup-final.json` 确认8个 exact `.hostlease-*` 试验根均经原 PG owner reset 且不存在；没有命令/cwd 指向这些根或本次 snapshot 的自有进程。私有源码快照已删除，Windows 本次 Node 编排进程空；真实 Windows scope 的原 helper exit0 已在关闭返回前核验。没有停止正式 PG、共享 Ubuntu 或其他用户进程。

仍须由新安装候选完成实际睡眠后会话操作/恢复/正常 Quit 验收；本次 source 回归不替代它。无需增加 provider 重放、lease 宽限、寿命 cap 或新持久恢复机制。
