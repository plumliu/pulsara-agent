# Pulsara 空闲会话归档 Hard-cut 实施规格

状态：按 2026-09-22 用户确认实施。权威为本稿、AGENTS.md 与当前代码；本稿取代永久删除规格中 OPEN/CLOSED 与 canonical close 的定义，删除合同其余部分不变。

## 产品与 schema

- `sessions.lifecycle` 硬切为 `OPEN / ARCHIVED`，无 CLOSED、归档布尔字段或兼容读取。表数不变，不新增事件、任务、回执或墓碑。归档时间使用该次状态转换写入的 `updated_at`，归档中不接纳执行写入。
- 普通运行时关闭和 CLI 退出只释放运行时，保持 OPEN；移除 `close_conversation` 参数、canonical close 升级及旧强制结束会话分支。`POST /api/sessions/{id}/close` 仅接受空对象，仍可停止运行时。
- 仅 idle 可归档；不存在运行中 turn、待消费输入、未结算工具 attempt、未结束子任务、ACTIVE plan workflow 或 OPEN plan interaction。已加载 Host 还须通过现有安全重载的进程内空闲门禁，且没有运行中的后台进程、监控或待交付终端观察。归档不替用户取消工作。
- 冷会话只检查 canonical idle 和 writer；不为归档启动 Host、模型或 MCP。其他有效 writer 拒绝；过期 writer 遵循已有冷删除的有限权限边界，不声称停止不可观察的远端进程。有遗留待处理工作时拒绝，需先打开并处理。
- 归档保留历史、fork、记忆、来源 FK、文件与 blob 引用。记忆来源投影为 ARCHIVED，提示先取消归档；不能说来源已删除。已归档源不接受普通 resume、reopen、fork 或旧窗口自动连接。
- 取消归档仅 ARCHIVED → OPEN；不创建缺失会话、不加载 Host、不发送输入、不恢复已终止任务。之后用户打开走既有 cold epoch，不新增 prefix 重建边界。

## 唯一控制路径

复用删除已有的 session 级 controller/core admission、bridge detach 与 full close owner，扩展为删除/归档两种收起操作，不复制另一套调度器。归档先冻结准入并验证空闲，再断开窗口、关闭空闲 Host，最后在 session FOR UPDATE 下重新验证 domain、exact writer generation/owner 和 canonical idle，原子改为 ARCHIVED 并清 lease。任何 idle 检查失败不关闭原 Host，释放检查门禁。未完成物理关闭则隔离且不改状态。

DB 事务不得等待物理关闭或网络。接纳后的操作由已有 process-local task shield，等待实际 DB worker；模糊提交读取 canonical lifecycle，无法确认就保留门禁，重试仅确认。不因为 ACK 丢失重建会话。取消归档与永久删除按同一 session row lock 串行，缺失为 unavailable；归档/取消归档的重复请求按当前状态幂等。

## API / UI

- `POST /api/sessions/{id}/archive`、`POST /api/sessions/{id}/unarchive`：严格空对象。返回 session_id 与 ARCHIVED / OPEN；不存在 404，忙或非 idle 409，结果不确定 503。
- `GET /api/sessions/archived`：同 memory domain 的完整已归档清单，不加载运行时、不加总量上限。
- 普通列表与读取仅 OPEN；菜单增加“归档会话”，服务端返回可否归档供 UI 禁用及说明，但执行时必须重新检查。
- 设置页增加“已归档会话”，显示名称、目录和归档时间，支持取消归档及复用永久删除确认。取消归档刷新列表但不抢焦点；归档当前会话进入未选择态，不创建空会话。其他窗口重连不能隐式取消归档。

## 验收

聚焦测试覆盖 idle/busy、canonical 队列/plan/tool/subagent、live 终端门禁、过期/有效 writer、显式取消归档、missing 不复活、fork 拒绝归档源、归档记忆来源与永久删除、模糊 ACK 和 HTTP waiter 取消。验证设置页两种操作、侧栏状态及前端构建。clean-v0 catalog 从实际新数据库提取验证，允许重置已核验的本地生产库。无需新长程模型测试。
