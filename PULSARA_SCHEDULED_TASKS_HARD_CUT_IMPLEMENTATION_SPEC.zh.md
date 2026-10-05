# Pulsara 定时任务硬切实施规格

日期：2026-10-05。状态：冻结；主代理与 GPT-6 Astra / high critic 均认可，可作为后续实施的当前权威规格。

本规格记录本轮产品讨论及本地 Codex 源码参考，规定后续实现与验收。本定时规格仅交付文档；本文提交不代表定时任务功能已实现，不修改其生产代码、依赖、数据库或保存配置。

实施前置为 [会话统一只读与工作目录恢复规格](PULSARA_SESSION_READ_ONLY_WORKSPACE_RECOVERY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)：普通会话已落地冷历史、显式目录恢复与具体非抢占 writer 接缝，代码验收见其 §15。定时入口复用这些 owner，不静默 mkdir、不另写恢复路径。本次仅更新前置依赖说明，定时任务尚未实现。

第二项前置为 [逐轮中断历史提示规格](PULSARA_TURN_INTERRUPTION_HISTORY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)，须在定时任务实现前完成。它让普通 turn 的 runtime 中断事实持久投影到历史，并继续由现有 compiler 在下次请求提供通知；定时触发复用同一路径，不人工插入 final assistant answer，不另建 scheduled 结果历史。实施顺序为：已完成的统一只读/目录恢复 → 逐轮中断历史提示 → 定时任务。第二项前置已在 main 落地并通过主代理与 Workspace readonly spec critic（GPT-6 Astra / high）的代码复审；实现冻结和验收证据见该规格 §12。定时任务尚未实施。

## 1. 产品目标与明确范围

定时任务是一份“何时向哪个普通会话提交什么提示词”的持久配置。每个任务绑定一个会话，一个会话允许绑定多个任务。用户既能选已有会话，也能先通过普通创建入口新建会话，再绑定任务。后续触发始终进入该会话，不为每次触发另开会话。

会话仍可正常聊天、改模型、压缩、停止回复、归档和删除；不增加会话类型、定时专用 Runner 或第二套执行历史。任务只驱动输入，不替代会话的上下文与执行所有权。

会话成功归档或删除时，删除全部绑定的定时任务配置。取消归档不会重建任务；此前聊天与执行历史仍遵守普通会话的保留/删除行为。

首版包含：侧边栏「定时任务」页；创建、修改名称/提示词/时间、暂停、恢复、删除、立即运行；绑定普通会话；一次性、固定间隔、每天、每周、每月指定日；聊天中的同一管理工具。运行仅依赖本地 Pulsara 服务存活，不要求管理页或目标聊天页可见。

不包含：事件触发、独立云调度、OS 定时服务安装、自动创建 worktree、每次创建新会话、多项目 fan-out、每个错过时点都补跑的模式、后台自动批准权限。未来增加这些功能须另定产品边界。

## 2. 本地 Codex 参考及其证据边界

核对目录：`/Users/plumliu/Desktop/python_workspace/codex`，检视时 HEAD 为 `b172810921`。只作为实现参考，Pulsara 当前代码和本规格拥有产品语义。没有为文档增加代码 SHA/文件证据 hash。

| 本地源码 | 实际行为 | 本次采用的原则 |
|---|---|---|
| `codex-rs/history/src/heartbeat.rs`，`UserInputOrigin`、`Heartbeat` | host 标记 heartbeat 来源；消息带任务 ID、时间和保存指令；普通文本标签不认证来源 | 定时输入必须由可信宿主入口赋予 typed origin，不能伪装真人消息 |
| `codex-rs/core/src/session/mod.rs`，`record_user_prompt_and_emit_turn_item` | heartbeat 来源转为 `user.heartbeat` metadata 并保留到历史 | 来源从接受到历史与模型投影贯穿，不凭 XML 标签推断 |
| `codex-rs/core/src/session/turn_input_tests.rs`，`steer_preserves_request_origin` | 单条输入的来源与当前 turn trigger 分开，正在运行的 turn 不改变后来输入的来源 | 人工与定时输入可进入同一会话，仍各自保留来源 |
| `codex-rs/history/src/reconciled_retained_context.rs`，`ordered_entries` | 保留指令的投影按任务 ID 和指令版本合并重复 heartbeat，时间不作为指令差异 | 可参考其区分调用与指令的思路；首版不移植 retained-context/reconciliation，不删除 Pulsara 已安装消息 |
| `codex-rs/core/tests/suite/guardian_heartbeat_authorization.rs` | 连续 heartbeat 不冲掉真人限制；较新的用户限制仍约束执行 | 定时触发不变成一份新的真人授权，不清空原有约束 |
| `codex-rs/app-server-protocol/src/protocol/v2/plugin.rs`，`ScheduledTaskSummary` / `ScheduledTaskSchedule` | 插件定时任务声明，包含 hourly/daily/weekdays/weekly | 声明与实际调度 owner 分开，时间配置使用闭合类型 |

在该仓库未找到完整的任务定义存储、RRULE/cron 时间调度与到点派发实现。上述源码证明 agent 输入侧的支持，不能称为可直接搬来的完整 scheduler；调度由桌面宿主负责是根据代码边界的推断，不声称取得了桌面调度源码。

官方产品文档亦区分既有会话续跑与独立运行，并说明本地项目执行依赖机器及应用运行：[Scheduled tasks](https://learn.chatgpt.com/docs/automations?surface=app)。本次只采用绑定普通会话的模式。

## 3. 现有 Pulsara 接缝

- `conversation_kernel/host.py`：`KernelHostSession.submit_prompt`、`_submit_prompt_owner`、`_submit_prompt_reserved`、`cancel_queued_prompt`、`KernelHostCore.open_session/resume_session`。
- `conversation_kernel/_repository/prompts.py`：`enqueue_prompt`、FIFO prospective prepare/consume、`apply_queued_prompt_action`；既有 session writer 事务已经串行化接受、消费与取消。
- `prompt_queue_items` / `session_commands`：拥有接受输入、FIFO 顺序、命令兼容性、取消及消费结果。已有的 occurrence 与 turn/消息/工具表继续保存执行历史。
- `model_input/contracts.py`：`CanonicalInputOriginKind` 尚无 scheduled origin；队列消费中存在固定 `HUMAN_MESSAGE`，不能直接从定时器调用当前公共人工入口并保留此归类。
- `conversation_kernel/session_deletion.py` 与 Host retirement：拥有归档/删除及物理执行清理，调度不能绕开它们。
- `web_app/session_controller.py`：`LocalSessionController.resume_session` 复用 `_by_session` 的现有 handle，以 `_ResumeInFlight` 合并并发打开；调度应复用这个获取 owner，不能直接重复调用 Core.resume_session。上述 Python 路径均以 `src/pulsara_agent/` 为根。
- `frontend/components/session-sidebar.tsx`、`frontend/app/pulsara-app.tsx`、`frontend/lib/runtime-adapter.ts` 与 HTTP/session controller：管理页、会话导航与授权沿用现有宿主和前端体系。

实现应扩展以上 owner 的具体 scheduled 输入路径。不要新增通用消息总线、调度执行恢复框架或 queue wrapper 再复制现有协议。

## 4. 时间规则与依赖边界

### 4.1 闭合规则

`schedule` 为有版本的闭合 JSON 值（`scheduled-rule:v1`），不接受任意 Python、shell、裸 cron/RRULE 或模型自造字段。首版类型如下：

| kind | 字段 | 语义 |
|---|---|---|
| `once` | `run_at_utc` | 指定 UTC instant，执行一次 |
| `interval` | `anchor_at_utc`, `seconds` | 从固定 UTC 起点计时，seconds 是正整数，使用整秒精度；不以执行完成时间重置起点 |
| `daily` | `start_date`, `time` | 指定 IANA 时区中，每天 HH:mm |
| `weekly` | `start_date`, `weekdays`, `time` | 非空、不重复的 ISO weekday 1..7，每周相应日 HH:mm |
| `monthly` | `start_date`, `day`, `time` | day 为 1..31，每月该日 HH:mm |

任务单独保存非空 IANA `timezone`。UI 默认使用浏览器解析的时区并让用户确认，保存后不跟随宿主时区变化。服务器验证 IANA 名称和规则全部字段；bool、小数、空 weekday、未知字段及无效时间应拒绝。once/interval 用 UTC 运算，timezone 只影响显示；calendar 用本地日历生成、再转为 UTC 存储与比较。

每天八点是 calendar，不是 interval 86400。忙碌导致今天十点才执行，不改变明天八点。每月一号不能近似成 30 天。月内没有指定日（例如二月 31 日）就跳过该月，不截到月底。

### 4.2 DST 与一次性输入

calendar 的不存在本地时间跳过该次；重复本地时间只执行第一次（`fold=0`）。once 表单遇到不存在时间应拒绝并提示，重复时间明确选择第一次后显示时区/offset 与预览；API canonical 保存确定的 UTC instant。未来支持选择第二次须修改产品协议，不能悄悄双跑。

### 4.3 依赖的实际核对与最小适配

采用 `python-dateutil==2.9.0.post0` 的 `rrule`、`after`/`before`、`tz.datetime_exists` 与 `datetime_ambiguous`，标准库 `zoneinfo.ZoneInfo`，并声明跨平台所需 `tzdata` 依赖。后续落地锁定 tzdata 实际版本到 `uv.lock`；不把临时验证环境写入生产依赖。

依赖拥有日历枚举、日期有效性与本地时间检测；Pulsara 只把闭合 DTO 转成规则，补齐本产品的 DST 策略与 UTC occurrence。此处不使用另一套持久化 scheduler/job store；已有 PostgreSQL 与 Host 拥有任务和派发。

本轮通过临时 uv 环境、仓库 `.venv` 的 Python 验证并读了实际 `rrule.py` 与 `tz/tz.py`：

- `rrule.after(self, dt, inc=False)` 与 `before` 可查询邻近 recurrence；`cache=False` 避免长期无限增长的缓存。
- monthly 31 日序列为 2026-01-31、03-31、05-31，按库跳过无该日期的月份。
- `America/New_York` 的 2026-03-08 02:30 被 rrule 枚举，但 `datetime_exists` 返回 False。虽然文档描述应忽略不存在时间，实际枚举不能直接当作已验证 occurrence。
- 2026-11-01 01:30 存在且 ambiguous，ZoneInfo 默认为第一次。适配器先检查 exists，再按产品规则处理 ambiguous，最终转 UTC；不自己编写 DST 规则或历法引擎。

[rrule 官方文档](https://dateutil.readthedocs.io/en/stable/rrule.html)，[tz 官方文档](https://dateutil.readthedocs.io/en/stable/tz.html)。这些事实为 API 与集成差异验证，不是功能激活测试。

next-occurrence 查询只求邻近有效值，禁止物化从起点到未来的全部序列。`after` 本身从 dtstart 枚举，不能直接对多年以前的起点调用它并称为 seek。首版采用以下确定路径：

- interval 用 UTC 整数时间差与 seconds 求最小有效序号，直接得到严格晚于查询 instant 的下一个值；once 直接比较其唯一 instant。
- calendar 只允许频率 DAILY/WEEKLY/MONTHLY、interval=1；显式指定 BYHOUR、BYMINUTE、BYSECOND=0，weekly 指定 BYDAY 与 WKST=MO，monthly 指定 BYMONTHDAY。没有 COUNT、UNTIL、BYSETPOS 或隔周/隔月规则。
- 将查询 UTC instant 转为任务时区，取所在日零点、所在周一零点或所在月一日零点；与原 start_date 零点取较晚者。仅临时查询对象用 `rrule.replace(dtstart=window_start, cache=False)`，其余显式规则不变。这一闭合规则集不依赖 dtstart 的隐式星期、月日或多周期相位，因此窗口裁剪保持剩余序列等价。
- 从窗口枚举候选，过滤不存在时间，强制 fold=0，再转 UTC，返回第一个严格晚于查询 instant 的值。UTC instant 是最终比较依据；不能使用同 tzinfo 的 wall-time 比较判定 fold。无下一值或超出日期库可表示范围时返回明确的时间范围结果，不加任意循环次数上限。

持久规则锚点保持不变，临时 seek 不写回任务。本轮已用实际库对照从原锚点枚举的结果验证 180 组窗口等价性，覆盖 5 个时区、三类 calendar、多年间隔、DST 与 Apia 日期跳跃；落地时将这些边界纳入正式行为测试。

创建、有效时间编辑或周期恢复若无法得到所需未来 occurrence，返回明确时间范围错误并保留原行；once 恢复已有过期时刻遵守 §6 的立即 due 规则。周期派发已经接受/合并后，若求不出后续时点，同事务改 PAUSED/next NULL、revision+1，显示范围原因；已接受项仍正常执行。不新增状态或任意年份上限。

## 5. 忙碌、排队与重复到点

所有定时触发仅为 `NEW_TURN`，不自动 steer、打断、抢占人工输入或绕过 Plan/压缩 fence。

| 情况 | 行为 |
|---|---|
| 会话空闲且可接受输入 | 按原队列入口接受，由原队列启动普通 ROOT turn |
| 正在生成、工具执行、等权限/Plan 确认或仍有 ROOT 执行，且原入口允许接受 | 接受为 PENDING，按原 FIFO 消费；必要的许可继续等待 |
| Plan exit fence 或压缩写 reservation 暂时阻止接受 | 保留 due，fence 解除后重观测，不绕过 `PLAN_TRANSITION_BUSY` / `COMPACTION_IN_PROGRESS` |
| 同一任务已有一个 scheduled PENDING 项，又到点 | 不新增项、不改旧快照，合并为该项；推进时间游标 |
| 同一任务已有 RUNNING turn，但无 PENDING 项 | 可新增一个 PENDING 项，供当前执行之后续跑 |
| 不同任务或人工输入 | 各自进入原 FIFO；不按来源改优先级 |

“一个待运行项”是本产品针对周期检查的合并语义，不是会话/任务/历史总数上限。不能用队列数量、运行次数或任务寿命添加任意新 cap。既有真实资源拒绝继续使用原 typed outcome。

FIFO 以实际成功接受的队列顺序为准；尚未通过 admission fence 的 due 候选不占队列位置，解除后仍经普通入口接受。

合并以任务 ID 判断，修改提示词不会改写已接受项；其原提示词和 due 时间继续有效，新的提示词用于未来真正新增的触发。UI 必须能让用户看到旧待运行项，并通过原队列取消入口单独取消。

## 6. 停机、晚醒与时钟变化

首版采用“合并漏过时点为一次检查”。当服务启动、从睡眠恢复或 timer 晚醒，若 ACTIVE 任务的 `next_run_at <= database_now`，只尝试接受一个 occurrence，due 使用当前持久游标值，随后直接求第一个严格晚于 database_now 的有效时点。不逐项补历史，不宣称每个时点必执行。

游标是未决下次检查的产品状态，计算派发时使用 PostgreSQL 当前 UTC 时间。进程 monotonic clock 只负责等待；实际执行准入仍读数据库时钟。服务与机器关闭期间不执行，也不保证到点硬实时。

once 如果关闭期间到期，恢复后仍尝试一次；成功接受或与已有 PENDING 合并后变为 `COMPLETED` 且 next_run_at NULL。COMPLETED 表示该次派发已处理，不表示模型执行成功。最终准备拒绝按 §10 置 PAUSED，保留显式恢复机会。周期任务的普通执行失败不自动重试同一个 occurrence，下一正常时点仍可触发。

显式恢复 PAUSED 任务时，周期任务从恢复时刻计算下一未来时点，不补暂停期间次数；once 若尚未成功触发而已过期，在恢复时变为立即 due。COMPLETED 的 once 不可“恢复”；用户可以修改时间得到新任务 revision，或显式「立即运行」。

时钟回拨不撤销已接受输入；同 revision + due 的 command identity 保持相同，避免重复接受。时钟前跳沿用一次合并规则。timer 每次醒来重查数据库，不以进程记忆授予执行权。

## 7. 一张任务表与最小 durable 增量

新增 `pulsara_v3.scheduled_tasks`，拥有任务当前配置与时间游标：

| 字段 | 约束/用途 |
|---|---|
| `id` | 独立任务 ID |
| `session_id` | FK 到 sessions.id，ON DELETE CASCADE；绑定创建后不可修改 |
| `name`, `prompt` | 非空；prompt 使用现有文本输入/内容资源边界，无额外小长度 cap |
| `schedule` | §4 闭合 JSON，含 contract 与锚点 |
| `timezone` | 验证后的 IANA 名称 |
| `status` | `ACTIVE` / `PAUSED` / `COMPLETED`；COMPLETED 仅 once |
| `permission_mode` | 任务创建时由用户明确选择的既有 preset；复制界面当前选择并显示；不存另一套权限 |
| `next_run_at` | timestamptz；ACTIVE 非空，其他状态 NULL |
| `revision` | 正整数；配置编辑/状态改变递增，作为同一可变行的并发编辑与接受检查；timer 仅推进游标不递增配置 revision |
| `created_at`, `updated_at` | 数据库时钟 |

不加 last_run_result、pending pointer、receipt、持久 timer、任务 lease、job、delivery ack、执行 checkpoint 或投影权威。最近结果通过既有 queue/turn/history join 派生，允许分页。

任务表增加 ACTIVE 行的 `(next_run_at, id)` 候选扫描索引；这是按时间发现配置的查询支持，不是新的派发权威。

必要地扩展既有 prompt_queue_items：持久 `input_origin`（原人工流程保存 HUMAN_MESSAGE/HUMAN_STEER）、nullable `scheduled_task_id`、`scheduled_task_revision`、`scheduled_due_at`。后面三字段同有同无，仅 `SCHEDULED_TASK` origin 使用；scheduled 项只能 NEW_TURN。task ID 为已接受输入的历史来源值，不 FK 到可删除任务定义；删除任务不破坏历史。新增一个索引查 `(session_id, scheduled_task_id, status)`，不创建运行记录表。

来源继续写入原 transcript/provider-input 语义与既有 committed occurrence 的 typed payload，输入 command 的完整快照拥有该来源值。canonical input origin 新增 `SCHEDULED_TASK`；无需增加新的 event kind、subject slot、append guard 或 durable job。现有 hash/内容 digest/命令语义兼容性只按原 owner 扩展完整输入，不加 schedule/DTO 的冗余 hash。

本表是独立的用户可编辑产品真相，FK 是独立产品关系。除这张表、明确的会话 FK、候选扫描索引、现有队列来源字段/索引和 §9 的具体 session command 种类外，不扩大 durable categories。落地时重跑仓库现有 oracle 并记录准确差异；若需要新 event/subject/guard/job，先修订本规格说明必要性。

## 8. 派发事务与 owner

`ScheduledTaskService`（具体产品 owner）负责管理、时间计算、派发及 Host 路由；不拥有模型/工具运行。获取目标 session 必须复用 `LocalSessionController.resume_session` 的 live handle / in-flight 打开路径；若 Host 层工具接入需要共享，把现有获取部分移到唯一可注入 owner，再让 controller 和工具使用它。`KernelHostCore.resume_session` 是底层打开操作，不能作为第二套 handle 缓存或另一路无条件打开。浏览器 controller 授权与 session writer 是不同边界，复用 handle 不夺取浏览器控制权。其他进程已有活跃 writer 时保持任务 due，等现有 writer 机制可用，UI 显示暂不能派发。

非抢占获取复用前置实现 `conversation_kernel/_repository/authority.py::acquire_host_writer` 在原 session 行锁事务内的有效他者 writer 检查；普通默认获取仍允许接管，不能直接调用默认再宣称安全，也不能先读 lease 再无条件获取。已落地的 RESTORE 意图仅用于明确目录恢复。未来定时准备须复用该事务内排他检查与普通 EXISTING 目录校验，不调用 `restore_missing_workspace`、RESTORE 打开或创建目录的 owner；届时只补齐其具体非抢占打开意图，不增加任务专用 lease 或第二套恢复路径。原 `LocalSessionController` 继续拥有 live handle/in-flight 获取，原目录 gate 继续拥有已接受工作下一准入的阻塞。

任务新增/管理与派发均以现有 session row 串行化，再锁 task row、prompt queue rows；不持有数据库锁等待模型、Hook、网络或 sleep。打开的 Host 使用原 writer 事务。纯管理、无 Host 时的合并及终止准备失败可使用具体冷态 mutation 接缝，复用 `archive.py` 的 session FOR UPDATE、memory_domain 可访问范围与既有 writer ownership 检查：存在活跃 writer 必须经其原 owner；无有效 writer 时在该短事务内操作，不申请新的 lease、不伪造 HostWriterGuard，不启动模型/MCP/Hook。controller retirement/draining/quarantine 仍须验证。

冷态接缝只允许任务行变更、同事务取消尚未消费的 scheduled 项及其原 canonical cancellation effects、已接受 command 的确认和既存 PENDING 合并。接受新的输入仍须正常 resume 和完整 Host writer admission。queue cancellation 的事务内小操作继续来自原 repository owner；冷态权限来自已验证会话访问与管理动作，或任务行已授权计划的同 cut 条件写，不授权执行模型，不新增执行 owner 类型、事件或恢复路径。工作区丢失时用户仍能暂停/删除任务，自动打开失败也能按同 cut 置 PAUSED。timer 扫描与 UI 读取只是无权威的候选发现。

自动派发先在上述短 session 事务（live writer 或允许的冷态接缝）中核对 ACTIVE、revision/due、`due <= 数据库当前 UTC` 及 session lifecycle，并查询本任务 PENDING。已有项就合并并原子推进游标，不准备新模型/权限、不执行 Hook。未合并才释放事务，采用原输入准备流程：读任务完整值及 revision/due；冻结文本、模型选择和既有 permission snapshot；执行原 Hook；再通过 `enqueue_prompt` 的现有事务内接缝提交。必须新增一个具体 typed scheduled admission 值，携完整配置 cut 与 revision/due，不建 ticket 注册表。

自动到点派发的最终事务重新读并锁任务，验证 ACTIVE、session 可接受、revision/due 和许可 cut 没变，且仍满足 `due <= 数据库当前 UTC`。时钟回拨后的未来候选不得提前接受：

1. 再查同任务 PENDING；如准备期间有另一个接受者先创建了该项，则释放本次未使用的 Hook reservation，仅推进 next_run_at，不修改 PENDING 内容；once 同时 COMPLETED。
2. 否则沿用原 enqueue，原子写 queue item、session command、原 committed occurrences，再推进 next_run_at / once status。
3. 配置/权限/生命周期 cut 变动则拒绝本候选，释放 Hook reservation，由普通准备入口重新观察；不重用失效冻结值。

同一事务拥有“接受输入 + 推进游标”，不能在调用 submit_prompt 之后另起事务更新任务，也不能先推进游标再尝试入队。现有 enqueue/取消事务须提取可在既有 connection 中组合的小操作；保持唯一 repository owner，不复制一整套 SQL 接受状态机。

自动派发 command ID 由已有 canonical ID builder 根据 task ID、configuration revision、UTC due 构造，既有 session_commands 继续拥有重试兼容性。相同 ID 不允许不同 prompt/origin/许可/任务 cut。COMMIT 回执丢失时，对原完整冻结输入先沿用现有 confirm_prompt_ingress 的 FULL_COMPATIBLE 判断和 queue wake hint，再考虑未接受候选的配置/游标校验；不能因为已提交事务推进了游标而将原接受误判为失败。不重新执行 Hook 或重新生成许可/模型快照来确认原命令；不新增确认记录。

冻结事务前执行的 Hook 不保证跨 crash exactly-once，复用现有 Hook contract；不以 scheduler 添加恢复或重试语义。同一个已接受 queue item 的业务工具副作用仍由原执行 owner 处理。

## 9. 暂停、删除、编辑与执行取消

- 暂停：锁 session/task 后，ACTIVE 改为 PAUSED/next NULL，PAUSED 保持，COMPLETED 保持 COMPLETED；所有状态都在同一事务通过原 queued-action cancellation 接缝取消本任务仍 PENDING 的项。保留任务与会话。有效状态变化递增 revision，已是目标状态的重复暂停不递增。
- 删除（界面「取消定时任务」）：同样取消 PENDING 并删除任务行，保留已消费输入、运行中 turn、会话与历史。来源 task ID 可以继续显示为已删除任务。
- queue consumer 与上述动作复用同一 session writer 锁。暂停/删除先赢则不能消费；消费先赢说明已经启动，管理操作不隐式取消 RUNNING。
- 「停止当前回复」只通过原执行取消入口停止当前 turn，不修改任务定义或时间规则。
- 修改配置：expected_revision 乐观检查；实际值变化原子 revision+1，完全相同值返回当前值；旧 PENDING 快照保持。只改 name/prompt/permission，或只改 once/interval 的显示时区，保留 status/next，不吞掉已有 due。有效时间规则变化才重算：ACTIVE 从编辑时刻求下一未来时点；PAUSED 保持 PAUSED/next NULL；COMPLETED once 重新 ACTIVE。once 新建或修改 run_at_utc 须在提交时仍为未来，名称/提示词编辑不对已保存的旧时刻重新施加此检查。once↔周期转换遵守同一状态规则；COMPLETED once 仅改提示词不会重新运行。
- 「立即运行」：允许 ACTIVE/PAUSED/COMPLETED，使用同一 scheduled origin 和普通入队许可；成功或失败都不改 status/时间游标。已有同任务 PENDING 时返回已有项；无 PENDING 时才接受一个。typed admission 明确为 manual，而自动派发为 timer；manual 校验配置 revision、会话和许可 cut，不要求 ACTIVE 或 next_run_at 相等，不调用时间推进操作。暂停先完成后，新的明确 run_now 仍可执行；run_now 先完成后，暂停可取消其尚未消费项。两种 admission 共用原 enqueue 事务接缝，不复制输入状态机。
- 管理工具在正在执行的该定时 turn 中暂停/删除自身时，当前调用仍正常结算，未来触发停止。文本声称已取消不算成功，必须来自管理 owner 返回结果。

暂停/删除与队列取消的原 accepted occurrences 合并在同一数据库事务。不得嵌套调用另一个持久 command 接受事务；使用原 queued-action owner 的事务内操作并保留其 canonical effects。任务管理本身以 canonical task row 为真相，不加管理审计流。

### 9.1 立即运行的具体命令确认

显式 run_now 沿用 session_commands 的动作幂等，增加一个具体 `RUN_SCHEDULED_TASK` kind / `run_scheduled_task.v1` schema，target_kind 仍为 QUEUE_ITEM。独立产品理由是用户动作的回执丢失重试：即使所返回旧 PENDING 已被消费，也不能再接受第二个输入；不是 scheduler 送达 receipt。

完整动作请求为 `client_command_id, session_id, task_id, expected_revision, request_at_utc`，前端/工具重试保持原值，request_at_utc 是明确调用时刻。原 canonical command builder 对此动作请求生成现有 semantic_digest；permission 由锁定的配置 revision 指定，不新增 DTO hash 或重复保存任务快照。新请求的绑定 session、可访问范围、revision 必须与当前任务一致；server 冻结模型、文本和许可的普通检查仍不可省略。

1. **已有兼容命令**：先核对动作完整请求，再返回原 target 的当前派生状态并按原 owner wake。无需重新准备、重跑 Hook 或读取现在的 task；任务后来被删除也可确认原动作，新动作则 NOT_FOUND。不将“当前存在另一个 PENDING”当成原动作确认。
2. **合并分支**：在首次动作事务中查到该任务 PENDING，写一条 RUN_SCHEDULED_TASK command 指向它，不改 queue 内容，不运行新 Hook。queue.command_id 与本动作不同是明确语义；确认动作请求和原 target，不用本次配置/许可与旧项比等。
3. **新建分支**：原入队事务写 queue + 一条 RUN_SCHEDULED_TASK command，queue.command_id/client_submission_id 属于该动作，scheduled_due_at 等于动作 request_at_utc。复用原 enqueue 的唯一内部写入和冻结输入校验；仅增加具体 command kind/schema 分支，普通 QUEUE_PROMPT 的 FULL_COMPATIBLE 检查保持严格。该动作确认同时核对 queue 与其冻结 origin/task revision/due 及输入结构自洽。

现有 confirm_prompt_ingress 固定 QUEUE_PROMPT，须在原 command owner 内增加上述具体确认分支；不能宣称原 FULL_COMPATIBLE 无修改即可涵盖合并，也不能另写第二条 mapping command 或新确认表。准备期间出现 PENDING 的竞态在最终事务合并，动作 command 与入队/合并同 COMMIT。

## 10. 接受失败与进程生命周期

事务回滚时任务仍 due，未产生 queue item；暂时不可获得 writer、已有 retirement fence、现有 admission cut stale 等可等待/重观测。timer 不持续热循环失败任务；按已有服务 wake/实际 next due 与有界单次重观测退避等待，不加逻辑重试总次数上限。

模型未配置/不可执行、工作区不存在、权限或 Hook 明确拒绝等自动派发的最终准备失败，在 session→task 锁中重新核对原 ACTIVE/revision/due、due 仍已到期及 lifecycle；旧 cut 失效则丢弃失败，不暂停新版本或复活已删除任务。若已出现本任务 PENDING，则走合并；否则置 PAUSED、next NULL、revision+1，展示 process-local 失败原因；重启后至少显示已暂停，用户可恢复。失败没有 provider 调用；未接受 prompt 不伪造 turn，不无休止重试明确拒绝。manual 的失败仅返回该动作错误，保持既有 status/next，不作为 timer backlog 自动重跑。

已有队列真实容量/资源拒绝属临时阻塞，任务保留 due，等原队列/资源 owner 可用；不同任务轮流考虑，不能一个失败任务阻塞全部 due 任务，也不拒绝额外逻辑任务。扫描分页、每次准备按已有实际 admission 边界；不加永久任务总数/会话数/调用数 cap。

服务停机只取消 process-local timer/未提交的准备，不删除任务或已接受队列。启动既扫描 ACTIVE/due，也扫描既有 scheduled PENDING 涉及的可恢复 session，通过同一 controller resume/wake；COMPLETED once、PAUSED 的 run_now 已接受项不能因无下一 due 被漏掉。已接受 PENDING 的启动处理沿用现有普通队列 resume contract，不恢复崩溃时正在运行的模型/工具 turn，也不自动重执行 CONSUMED。

浏览器未打开不影响触发。关闭某个 UI 标签不算服务停机。Host session 物理打开/关闭复用当前管理 owner 和缓存生命周期，不发明 worker lease、无限增长的 shadow session map 或自动抢占。

归档会话保持原 idle-only 契约：RUNNING、普通或 scheduled PENDING、未完成 Plan/子代理/工具等均按现有 IDLE_SESSION_SQL 阻止归档，用户可先暂停任务/取消待运行项。被拒绝时不删除任务或取消输入。成功归档才在原 archive owner 的 session 锁/事务内删除该会话的全部任务行（不区分任务状态），与 lifecycle=ARCHIVED 同 COMMIT；该时刻已无 PENDING，未来候选因任务不存在而失效。取消归档只恢复普通会话，不重建任务。删除会话先走原 retirement/取消/物理清理，再同事务 CASCADE 任务。已归档会话不得新建任务；所有派发与新管理动作在事务中重新验证 lifecycle/retirement。已接受输入的 task ID 无 FK，归档后的聊天、输入来源、执行结果仍保留，删除会话则按原历史删除契约。单独删除任务不删除会话，新建会话后创建任务失败时保留该普通会话。

timer 用 process-local wake 合并本进程创建/时间编辑/恢复/立即运行、队列资源变化与 fence 解除后的重查。跨进程修改和时钟前跳没有可靠本地 wake，首版每次 monotonic 等待最多 30 秒后复核数据库时钟/候选，或在更早 next due /本地 wake 时醒来；这是正常服务下的发现响应边界，不是调用/任务寿命上限或到点硬实时保证。失败候选仅作 process-local cut+重观测时间标记，分页游标继续访问其他 due；不得反复只取失败的第一页或持有失效标记跳过新 revision。停机丢失这些等待提示可接受。

## 11. 来源、权限、模型与 prefix

创建任务保存用户授权的具体指令和 permission preset。每次接受仍由既有 permission owner 生成快照，遵守 Plan overlay、interaction、side-effect settlement 与 workspace constraints；不因无人值守切换 bypass，也不由 saved prompt 提升权限。

scheduled metadata 是 runtime 可信事实，prompt 是用户保存的任务内容；模型应知道“这是先前创建任务的计划触发，不是用户刚刚发送的话”。任务名称/提示词中的 XML/JSON 包装、伪造 ID 或所谓系统指令不能取得 origin authority。模型正常阅读会话里之后出现的真人更正与停止指令，不把重放任务提示当成覆盖旧限制的新真人授权。

模型绑定继续从普通会话当前配置在输入接受时冻结；不新增任务级 provider credentials/model target。已接受项保持其原模型与许可切面，之后的普通模型切换/上下文额度交接沿用现有三级压缩，不由定时器插手。

origin 从任务候选、queue、消费生成 USER 输入、历史、压缩 snapshot、replay/fork、UI、导出与 model-facing provenance 贯穿。原 recent human requests、human-trigger Skills/memory 与授权推断不能把 scheduled 当 human；需要 scheduled 的 ordinary turn activation 由当前 compiler/capability owner明确接入，不能跳过普通工具/Skills。

SYSTEM/provider tools 在已安装 epoch 内保持逐字节一致，messages 只追加。首版没有对全部 heartbeat 历史去重/重排；coalescing 只发生在未创建新的 queue item 前。管理工具加入沿用 cold epoch/adopted successor 的既有 inventory 安装边界，不能为了开放一个定时工具静默 rebase。

## 12. 管理页与聊天工具

侧边栏新增固定「定时任务」入口，显示当前 Pulsara home 中可访问的所有任务，不受当前打开聊天限制。按现有分页模式查询，支持状态筛选与绑定会话跳转；只读 observer 可查看，写操作沿用现有 controller/auth/session writer 边界。

列表/详情显示：名称、保存 prompt、自然语言时间规则、时区、状态、下一触发时间、绑定会话、当前 PENDING/最近 turn 的派生状态；执行结果点击跳转普通历史。跨时区显示须明确标签。最近记录从 queue/turn 关联读取，删除任务后其历史仍在聊天里。

创建表单：选已有会话或进入普通新会话创建；名称、prompt、规则、时区、既有 permission preset；显示下一次发生时间。目标会话须有可执行的模型配置。编辑冲突返回 409 并重载当前值，不能 last-write-wins 覆盖另一个编辑。取消/失败不留半份任务行。

暂停、恢复、取消任务与停止当前回复使用不同文案。管理页的「取消任务」明确会话仍保留、正在执行的回复继续；另有当前会话普通停止按钮。time/prompt 修改旁提示已入队项保持旧版本，允许跳转取消该项。

会话归档/删除界面说明会同时删除绑定的定时任务；取消归档后需要显式重新创建。沿用原生命周期操作，不新增第二套确认流程。

新增具体 `scheduled_tasks` builtin tool，actions `list/get/create/update/pause/resume/delete/run_now`，参数与 HTTP 共用闭合 DTO 和 ScheduledTaskService。工具默认当前会话，操作其他任务需精确 task ID 与现有可访问范围；不靠 prompt 名称模糊删除。操作型调用按原工具权限与调用 settlement 授权，不以任务自己产生的文字当作用户新的批量授权。

UI、HTTP、模型工具都调用同一 owner；建议 HTTP `/api/scheduled-tasks` 的 collection/detail/actions 接口，最终命名遵循既有 web controller 规范。任务 prompt 无 attachments/v1 rich source 编辑，用户仍可在普通会话追加文件。

## 13. 硬切与 schema oracle

实施为一次完整硬切：clean-v0 新表、队列来源字段/constraints/index、origin codec、compiler/renderer、HTTP/tool/frontend、启动/停机 owner、测试与文档同时更新。

不保留旧 HUMAN_MESSAGE scheduled 消费路径，不做旧/新双写、在线迁移链、兼容别名或影子 job store。刷新既有预期 schema catalog 与 oracle；生产保存配置只读，若 activation 需要 clean-v0，仅按 AGENTS 的授权重置已核实的本地可丢弃数据库。

变更清单至少精确记录：新表 1、task→session FK 1；既有 queue 来源扩展及索引、task due 索引；新增 canonical origin 1；既有 session_commands 增加 RUN_SCHEDULED_TASK closed kind/schema 与对应 CHECK。event kind / subject slot / append guard / durable job 数保持不变。FK/index/command CHECK 是否算入 repository oracle 的 relation/guard 项按实际工具定义列出，不用“只有一张表”掩盖约束差异。

## 14. 实施顺序

先完成开篇两项普通会话前置及其各自验收，再开始以下步骤。普通 ROOT 的中断提示不改变任务配置状态；尚未接受 scheduled 输入的准备失败仍按本规格处理，不生成人工回复或伪造 turn。

1. 时间 DTO、dateutil/ZoneInfo 最小适配、冻结行为测试；模型与 UI 用同一 next occurrence 预览。
2. clean-v0 表、queue provenance、repository 事务接缝与 codec；证明入队/游标和暂停/消费线性化。
3. 复用唯一 controller/session acquisition 路由、具体冷态管理接缝、process-local timer、原 queue wake、启动 late due；实际 Host 验证人工/定时混用。
4. compiler/provenance/压缩/replay 贯穿；现有权限和三层额度交接回归。
5. HTTP/controller、侧边栏管理页和 builtin tool；编辑/暂停/删除与历史导航。
6. focused → 受影响 backend/frontend → 必要全量与真实供应商 dogfood，critic 审代码闭合后激活。

每一步形成新单一路径，不作为分批兼容发布。不得先发布能创建任务但 origin 仍被当成人类、暂停仍有竞态或不具备普通队列恢复行为的半成品。

## 15. 必须通过的验收

### 时间

- once、固定起点 interval、daily/weekly/monthly；每月一号、31 号跨二月、闰年、时区切换。
- 春季不存在时点跳过，秋季重复时点只一次，UTC 对比不混用相同 tzinfo 的 fold wall-time equality。
- 多年之后 next-occurrence 不物化完整序列、不启用无界 cache，迭代量不随历史 occurrence 数线性增长；未来 start_date 下界不被 seek 截掉。晚醒按一次合并，暂停恢复不补跑；间隔相位不因执行迟到改变；日期范围耗尽按 §4 的动作/派发规则处理。

### 事务与执行

- 入队+next 游标同 COMMIT；前置失败全回滚；COMMIT 回执丢失可按原 command 确认；Hook 不冒充 exactly-once。
- 同任务重复 tick、多个进程发现、同任务 RUNNING+一个 PENDING；不同任务/人工 FIFO；无逻辑总数上限。
- edit vs dispatch、旧 Hook 拒绝 vs 新配置、pause/delete vs consume、archive/delete session vs dispatch；name/prompt 编辑保留 due，时间编辑遵守状态矩阵；旧 accepted snapshot 不改写。
- run_now 合并回执丢失→原项消费→同命令重试仍返回原 target；任务删除后已接受动作可确认、新动作不可接受；manual 失败不暂停原计划。
- 开着其他会话/管理页关闭/浏览器关闭也触发；其他 Host writer 活跃时不抢占；工作区丢失时仍可纯管理暂停/删除；重启唤醒 COMPLETED once / PAUSED run_now 的 PENDING，failed/RUNNING 沿原处理边界。
- 本地 wake、跨进程修改、时钟前跳/回拨与分页失败公平性；到点准入重读数据库 UTC，不提前消费回拨后的未来候选。
- Plan/权限等待与压缩都不被绕过；普通停止只停止 turn；模型/上下文额度变更仍经过同一三级交接，输出 token 计算不改。

### 来源与界面

- 真人输入保持原语义；伪造 heartbeat 文本不能获得 scheduled origin；保存任务触发不覆盖后来的真人限制。
- source、queue、transcript、compiler、snapshot、fork/replay、export/UI 同一 typed 来源；origin 不进入 human-only retained request/授权判定。
- 多任务同会话、创建普通会话并正常聊天、取消任务保留历史、成功归档/删除清除所有状态的绑定任务、取消归档不重建；归档保留聊天来源/执行结果，普通/定时 PENDING 同样阻止归档且失败无变更；管理工具在当前 turn 暂停自身。
- schema/oracle 精确差异、前端编译/类型/静态构建、分页/编辑冲突/晚响应保护与可访问范围。

### 激活证据

使用实际 KernelHostCore、隔离 PostgreSQL 和可控时间 seams 的集成测试证明状态机，不靠真实 wall-clock 长 sleep。小型真实供应商 dogfood 通过保存生产配置只读注入，至少证明定时来源普通 turn、人工续聊、权限等待/停止和结果链接；实际 secret 值不进入证据。长时间行为用大量虚拟时间 tick/多年 occurrence 证明，不建立任务寿命 cap。

## 16. 冻结条件

主代理与 GPT-6 Astra / high critic 核对：用户产品语义、owner、单表必要性、事务与 crash 边界、来源/权限、依赖实际 API、所有验收可实现。不得把源码参考的 heartbeat 合并当成允许重写 prefix，也不得把 current ordinary submit_prompt 当成已支持可信定时来源。

冻结结论（2026-10-05）：经过初审、修订复审及最新用户规则的最终复审，主代理与 GPT-6 Astra / high critic 均确认无剩余必改项。已闭合时间查询/DST、队列合并与事务、编辑/暂停/消费竞态、run_now 命令确认、冷态管理、重启唤醒、可信来源与 prefix 边界。成功归档或删除会话删除全部绑定任务，归档失败无变更，取消归档不重建任务；这是本轮最终用户决定。

本定时规格只交付已冻结文档与参考核对，不修改定时任务生产实现、保存配置或数据库，也不创建实际定时任务。后续实现仍须完成 §15 验收和代码 critic，文档冻结不等于功能激活。
