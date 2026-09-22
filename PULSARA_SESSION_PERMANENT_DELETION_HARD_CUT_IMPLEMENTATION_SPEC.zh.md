# Pulsara 会话永久删除 Hard-cut 实施规格

状态：已冻结（2026-09-22），可直接实施

范围：单会话永久删除的 clean-v0 schema、repository、Host／Web 生命周期、HTTP 与前端

前置：`PULSARA_PROJECT_SESSION_MEMORY_DECOUPLING_HARD_CUT_DATABASE_DESIGN.zh.md` 已落地

## 1. 目标与权威

用户可以从会话列表永久删除不再需要的会话。删除不是归档、隐藏或关闭运行时：成功后该会话的 canonical 数据不再存在，不能恢复打开，也不能被旧窗口重建。

本稿、`AGENTS.md` 与当前代码真源共同约束实施。本稿承接项目／会话／记忆解耦规格第 7 节，取代该节尚待决定的会话删除边界；其余记忆合同不变。涉及以下既有行为的窄 hard-cut 在本稿明确授权：

- 现有 `DELETE /api/sessions/{id}` 从“关闭运行时”改为“永久删除”；关闭另用显式路由，不保留旧 DELETE 请求体的兼容解释。
- writer 获取区分新建与恢复，恢复不再创建缺失会话。
- HTTP fork 由服务器生成 child ID，不再接受浏览器指定 child ID。
- 会话 aggregate 的归属 FK 改为级联删除，内部语义引用使用可延迟检查。

本轮不提供批量删除、会话归档、自动保留期限、项目删除、目录物理清理、同时删除记忆选项或模型删除会话工具。不得顺带更改记忆治理、召回、关系语义或 provider prefix 边界。

## 2. 用户语义

### 2.1 删除和保留

| 对象 | 删除当前会话后的结果 |
|---|---|
| 会话及其消息、工具调用／结果、任务、计划、上下文快照、事件、可视化附属记录 | 在一笔事务中硬删除 |
| 已保存的全局记忆、项目记忆、记忆关系、embedding | 保留；仅被删除 ToolResult 的创建来源 FK 置空 |
| 已存在的 fork 会话 | 独立保留，包括已复制的历史、图片、可视化与模型配置 |
| 项目 canonical workspace | 仍有会话或项目记忆就保留；两者均无才清理数据库行 |
| 项目目录、快速开始目录及其中的文件 | 一律保留；不执行文件删除命令 |
| 共享 blob | 删除本会话引用；二进制内容由现有孤儿 GC 最终回收，其他有效引用保留 |
| 模型服务商日志、外部工具已经产生的副作用、备份 | 不在删除范围；不声称撤销或安全擦除 |

“永久删除成功”指 session aggregate 的 canonical 事务已提交，不表示 PostgreSQL 文件、WAL、备份或孤儿 blob 字节已经物理擦除。

### 2.2 记忆不随来源失效

沿既有 FK 执行：

```text
删除 session -> 删除其 ToolResults
             -> fact.created_by_tool_result_id = NULL
             -> relation.created_by_tool_result_id = NULL
```

正文、kind、context、生命周期、关系端点和关系类型不变。特别是：删除产生 `SUPERSEDES` 的会话不会删除该边，也不会把旧记忆恢复为正在使用。

NULL 继续投影为“最初保存位置已删除”，不显示来源导航。其他会话的来源保持原值；不得把来源改绑到 fork 中的复制 ToolResult。记忆页项目列表仍从 `workspaces + memory_facts` 读取，与当前会话无关。

### 2.3 停止的承诺边界

本进程已加载的会话，必须先完成本进程掌握的物理执行 owner 收尾，才能删除数据库记录。包括已有 Host close 合同覆盖的模型调用、工具、子代理、后台终端、交互和结算任务。不能只中断 root turn 就认为已停止。

无本进程 owner 的冷会话不应为删除而启动模型、扫描技能、连接 MCP 或恢复 runtime。服务器在数据库事务中判断 writer：

- 其他 owner 仍有有效 writer lease：拒绝删除，不抢占、不伪称替它完成物理关闭。
- 没有 writer lease，或 lease 已过期：允许冷删除；这只保证旧 writer 无权继续提交 canonical 写入。
- 本进程已知 close-failed／quarantined／尚未结算的 owner：即使 lease 已过期也拒绝走冷路径。

这是有意选择的崩溃后可用性边界：过期 lease 不证明远端进程已经死亡。冷删除不保证停止本进程无法控制的外部进程，也不能撤销已经发出的外部动作。本轮不建设跨进程停止协议；不得把上述有限保证写成“所有外部执行已停止”。

## 3. 数据形状与删除归属

### 3.1 不新增持久对象

产品表仍为 28 张：新增 0，移除 0。不增加列、删除墓碑、删除任务表、receipt、operation 表、清理队列或持久删除状态。不新增 committed event kind、subject、append guard 或 durable job。`sessions.lifecycle` 仍只有 `OPEN / CLOSED`；不得增加 `DELETING / DELETED`。

执行中的删除属于 typed process-local control operation，可在已有 owner 中增加变体和必要的短期任务索引。它不是重放恢复权威；进程退出后依靠数据库存在性重新判断。

### 3.2 唯一删除入口与 FK 矩阵

repository 只提供整会话 aggregate 删除入口。普通工具、后台清理和记忆管理不得单删 ToolResult、消息或本稿列出的会话子记录。运行角色给 `sessions` 增加 DELETE；子表不因本功能获得任意直接 DELETE 权限。PostgreSQL FK 负责归属级联。

以下 17 张表已有到 `sessions` 的直接归属 FK，统一改为 `ON DELETE CASCADE`，保留原来的完整 session／workspace 组合身份：

| 直接归属于 session 的表 |
|---|
| `context_snapshots` |
| `subagent_tasks` |
| `turns` |
| `session_commands` |
| `imported_history_groups` |
| `transcript_entries` |
| `session_context_genesis` |
| `assistant_message_blocks` |
| `provider_assistant_replay_fragments` |
| `tool_results` |
| `prompt_queue_items` |
| `canonical_image_refs` |
| `assistant_visualizations` |
| `interaction_decisions` |
| `plan_workflows` |
| `plan_interactions` |
| `agent_events` |

另外 5 张表沿已有归属边级联，不新增冗余 session FK：

| 子表 | CASCADE 的归属边 |
|---|---|
| `turn_context_binding_revisions` | 所属 turn |
| `tool_execution_attempts` | 所属 assistant tool-call block |
| `imported_tool_call_closures` | 所属 assistant tool-call block |
| `subagent_task_dependencies` | 依赖声明所属 task；不是它依赖的另一 task |
| `subagent_task_children` | 所属 task |

由此 session 加上 22 张归属子表一起消失。表内／表间其余 session-local 语义引用（例如 final entry、attempt 前驱、plan 回指、task origin、event subject）统一使用 `ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED`；不使用不可延迟的 `RESTRICT` 阻断同一 aggregate 的级联。已有 `canonical_image_refs`、`assistant_visualizations` 对其实体 owner 的 CASCADE 保留。不得把这些语义引用改成额外 CASCADE 以扩大删除面。

跨 aggregate 的边维持：

- session → workspace：RESTRICT。
- session 子记录 → blob：RESTRICT。
- memory fact／relation → 创建 ToolResult：现有 deferred SET NULL。
- project memory → workspace、memory relation → memory endpoints：保持现有约束。
- embedding → fact：保持现有 CASCADE；本轮并不删除 fact。

继续保留所有 unique/check、typed lineage、canonical immutability 与 deferred completeness constraints。删除路径不得禁用 trigger、切换 replication role、临时放宽 FK 或伪造终止事件。事务结束前显式 `SET CONSTRAINTS ALL IMMEDIATE`；任一错误整笔回滚。

归属 FK 的 child-side 反查必须有可用索引前缀，优先复用现有 PK／unique／普通 index。对当前仅有 nullable-owner partial indexes 的 `canonical_image_refs`，新增非 partial `(session_id, workspace_id)` 索引；三个 nullable owner 分支的 partial indexes 不能替代仅按 session 删除时的索引。实施时核验全部 22 张子表归属边及 memory SET NULL 边，不盲目重复已有索引，也不以全表扫描超时为理由增设会话总量限制。

### 3.3 workspace 清理与记忆锁序

删除事务采用 READ COMMITTED，与既有记忆管理锁序一致：

```text
目标 session row FOR UPDATE
 -> 有序 workspace identity advisory locks
 -> 有序 memory fact rows FOR UPDATE
 -> 有序 memory relation identity advisory locks
 -> DELETE session、FK actions、清理空 workspace
```

锁集合包含目标 session 的 workspace，以及其 ToolResults 作为创建来源的 facts／relations；关系涉及的两端 facts 也纳入 fact 集合，相应非 global context 纳入 workspace 集合。按既有 helper 的 canonical 排序加锁，不引入第二套锁 namespace。

在 session row 锁内先取得集合，再按序锁定，使用 fresh statement snapshot 重读集合。集合变化时整笔回滚重算，不在取得后层锁后补锁前层对象。锁定 session 后其他 canonical writer 不能再为这个 session 新建 owner；记忆页仍可能删除／编辑已存在的 facts，故不能用过时集合或预览作为执行权威。死锁、序列化失败与锁集合漂移只沿已有单次数据库操作 deadline 重试，不增加 retry-count 或会话大小上限。

每次重试必须从目标 session 的 `FOR UPDATE` 重新开始，再次验证 domain、本地 exact writer generation／owner 或冷路径 lease 条件。上一轮的 physical-full 观察可以继续描述那个已经停止的本地对象，但不能代替本轮数据库授权；两轮之间发生 takeover 时必须拒绝，不能跳过第 4.3 节直接重锁 memory 集合。

删除后，仅对涉及的 workspace 执行既有 `NOT EXISTS(sessions) AND NOT EXISTS(project memory_facts)` 条件清理。全局记忆不要求保留原 workspace；来源消失后的事实也不靠 workspace 墓碑显示原目录。所有文件系统目录不受影响。

## 4. 运行时、并发与事务

### 4.1 分层 owner

| 层 | 责任 |
|---|---|
| Session controller | 用户删除意图、domain、单 session 操作互斥与最终返回 |
| Browser bridge | 冻结该 session connect／接管，移除所有窗口连接并等待 close |
| Kernel Host core／session | 冻结新 runtime admission，加入已有 open／fork／close，收尾物理 owner |
| Repository | 行锁内验证 writer／domain，原子删除 aggregate 与置空来源 |
| 前端 | 确认、进行中状态、刷新列表和选择；无删除数据库的独立权威 |

扩展已有 typed operation／detach／settlement owners，不另写平行的“取消所有 asyncio task”框架。不把原 `prepare_raw_close` 整段当删除：它的冷 canonical close 分支可能主动 resume，且旧 finalize 的时机不覆盖删除提交。

### 4.2 单次删除步骤

1. 校验 same-origin 本地请求、严格请求体；在任何 detach／close 之前以 canonical row 或现有 exact handle 确认目标属于本服务的 memory domain，repo 在事务内再次校验。在 controller 的现有互斥范围登记目标 session 的删除 operation；相同删除请求加入同一个任务，其它该 session 变更返回明确 busy。
2. 在 Host core 现有 admission owner 下冻结针对该 ID 的 resume／reopen／fork；把已有 `_open_attempts` 与 fork owner 关联到具体 session，fork 同时关联 source 与本次 server-issued child ID。删除等待此前已经获准的目标操作结算，再重新识别实际 handle，覆盖 child 已提交但尚在打开的窗口。不得只看 `_by_session` 的瞬时空值。登记／查询使用短期互斥，等待任何 owner 时不持有 controller／core 的全局锁，不能造成被等待者无法发布／结算。
3. bridge 持有该 session gate，detach 全部 controller／spectator 连接。失败则隔离该操作，不进入 DELETE。冷会话没有连接也须持有 gate；不得返回 None 后失去 admission fence。
4. 若有本进程 handle，调用现有 Host close，`close_conversation=False`，等待完整 physical close。包括关停过程中已经提交的合法工具结算；随后都由删除事务一起移除。没有 handle 且没有未结算 owner 时走冷分支。
5. repository 执行第 4.3 节事务。数据库事务不得跨越 bridge detach、Host shutdown 或网络调用。
6. 明确提交／确认不存在后，清理 controller／Host 中的短期句柄和连接索引，完成 bridge settlement，释放本次删除 fence。前端随后刷新。失败按第 4.4 节处理，不提前放行重开。

本功能不新增“正在删除”的 canonical 事件。正常停止本来产生的既有结算仍遵循其原合同；不存在为了证明删除完成而追加的事件。

本轮在普通 clean Host close 后增加 exact writer release：全部物理 owner／结算成功结束后，由 core 调用 repository，将现有 writer lease 两列同时置 NULL（不改变 lifecycle），避免运行时已干净关闭却要等待旧 lease 自然过期。按 session ID、owner ID、generation 精确条件更新，不要求 lease 此刻仍有效；不得清除已经接管的新 owner。物理关闭结果与 release 的数据库结果分别处理：release 不匹配／ACK 不确定不会推翻已经确认的 physical-full，但也不授予删除权限；删除事务仍须重新验证，无法读取数据库时保留不确定状态。此步骤位于 session 资源关闭后、core 共享 repository 关闭前，不让停止 lease renewal 等同于 release。

### 4.3 repository 事务授权

请求的 session 必须属于当前服务的 memory domain。不存在／不属于该 domain 一律返回 `ABSENT`，不得泄漏其他 domain 数据。查询包含 OPEN 与 CLOSED，不使用“可恢复会话查询”代替 canonical 存在性。

`SELECT ... FOR UPDATE` 取得目标行后，采用精确分支：

- 本地已 full-close：operation 持有关闭对象的 exact writer owner／generation。只允许该 generation 仍为同 owner，或由该 owner 精确 clean-release 后的 NULL owner。发生 takeover／generation 改变就拒绝本次事务；不得拿旧 close 成功证明新 owner 已停止。
- 冷会话：在锁内确认 owner 为 NULL，或 lease 已过期；任何有效 lease 都返回 busy。已知本进程 quarantine 不得通过这个分支。

随后按第 3.3 节锁定来源影响集合，删除 session，执行 FK SET NULL 与 workspace 清理，验证 deferred constraints，提交。没有 session update／commit 与 DELETE 之间的数据库间隙。其他 writer acquisition／canonical 写入必须继续锁同一 session row：先提交者决定后续看到新 owner、缺失 row 或 stale guard。

普通 close 保留会话，永久删除移除会话，两者不能共用一个模糊 `close_conversation` 标志决定行为。

### 4.4 取消、失败与模糊提交

- 用户尚未确认：只关闭弹窗，无写入。
- 请求已由服务器接纳：HTTP 断开只移除 waiter，现有 process-local owner shield 并加入真实工作；不能取消数据库线程后就释放 gate。
- bridge／Host physical close 不完整：不执行 DELETE，保留数据并隔离。显示“未能安全停止，会话尚未删除，请重启 Pulsara 后重试”。已有外部副作用不能回滚。
- 运行时已关闭，但数据库明确回滚：会话仍在，可能由“已载入”变为可重新打开；不自动 resume。安全释放 fence，允许用户重试。
- 数据库提交 ACK 不确定：先加入实际数据库 worker，再按 exact domain＋session ID 读取包括 CLOSED 的 canonical 存在性。不存在即可报告 ABSENT；仍存在则报告未删除并保留记录。数据库不可读时报告 `SESSION_DELETE_UNCONFIRMED`，不能以网络失败断言回滚；在本进程保留隔离，重试只重新确认／加入同一 owner，不启动平行删除。
- 数据库已确认提交，随后 bridge gate／内存索引清理失败：保留已确认的 DELETED／ABSENT 结果；对无法安全释放的本地 owner 做隔离并提示刷新，不得改报“会话尚未删除”，更不能补偿重建 canonical row。
- 服务崩溃：数据库保证全删或全保留；重新启动后允许按冷分支处理。没有持久删除恢复流程，不要求记住是哪个旧请求完成了删除。

不存在不会被重建，所以 `ABSENT` 足以满足删除目标，但不声称“本次请求删除了它”或证明历史上曾存在。失败不清掉前端卡片；确认删除或确认不存在才移除。

## 5. 防止复活与 fork

### 5.1 新建和恢复彻底分开

`acquire_host_writer` 当前“找不到就 INSERT”不得继续供 resume 使用。hard-cut 为明确的新建／已有会话 writer 获取路径（两个方法或 closed typed intent 均可，不能默认推断）：

- 新建：仅服务器本次操作生成的新 ID；INSERT 冲突不能变成恢复。
- 恢复／冷重开／连接／takeover：仅现有 canonical session；缺失就返回 unavailable，不 UPSERT session，不先留下一条孤儿 workspace。
- `resume_most_recent_session` 与所有 CLI、Web、测试／辅助调用点同步迁移。外层先 read 再 acquire 不足以代替 repository 内的 existing-only 校验。

不得用空消息会话、workspace 信息或相同目录重建原 ID。新建同一目录的会话有新 ID；其原有项目记忆仍可正常召回。

### 5.2 fork 与删除线性化

fork repository 改为 READ COMMITTED，在读源历史之前以 `SELECT ... FOR UPDATE` 锁住 source session row，保持到子会话完整提交，顺序为 source session → workspace → 新 child。取得锁后的查询使用 fresh statement snapshot；现有 canonical writer 同样先锁 source row，因此历史在这段事务内稳定。读取源不存在即失败，不拿删除前缓存的 material 建 child。所有向 session aggregate 写入的生产 owner 都须维持这条源行锁规则；不得把锁前读取结果当作 fork 真源。

`fork_history.py` 当前要求 REPEATABLE READ 的 reader 入口也要同步 hard-cut：改为 READ COMMITTED 下的锁内读取合同，并在入口实际取得同一 source row lock 后才执行任何历史查询，不能仅删掉隔离级别断言。repository 的提前 domain／source 校验使用同一事务；重复取得已持有的行锁不释放它。其下游共用 connection 的 snapshot hydration／历史读取 helper 一并核验，不能遗漏嵌套的隔离级别要求。测试验证并发写入受阻及等待删除后读取缺失，不能只改旧测试的字符串期望。

本轮不增加 session identity advisory lock：existing-only 路径有真实 session row 可锁，缺失就失败；新 session／child 使用服务器新生成身份及 PK 排他插入。事务级 row lock 足以线性化这些路径，没必要再维护缺失 ID 的锁／墓碑。

- fork 先获得源锁并成功提交：新 child 保留；之后删除 source 不影响它。
- DELETE 先提交：fork 返回 source unavailable，不复制残缺历史。
- 本进程已进入删除 admission：不再接纳新的该 source fork；此前接纳的 fork 加入其实际 owner。

既有 fork 重绑 transcript、snapshot、prompt refs、replay、ToolResult 与 visualization 的合同不变。共享 blob 必须有 child 的真实引用；不因删除父会话重建 provider roots、重写 child 的 installed prefix 或改绑 memory owner。

### 5.3 child ID 的 HTTP hard-cut

`POST /api/sessions/{source_id}/fork` 请求体仅含 `{ "anchor_entry_id": "..." }`。由 controller 在每个新接纳请求中生成一次新的 `session:<uuid>`。响应仍返回 child ID 和既有明确 outcome。传入旧 `child_session_id` 字段拒绝为 400，不兼容读取。

repository 的 child ID 参数保留为服务器 trusted one-shot 操作内部身份，供其 exact commit confirmation 使用，不能再让浏览器选择／重放。明确回滚后的同 owner 事务重试可以复用该 ID；提交不确定后先确认，不得将“查无 child”当再次 INSERT 同 ID 的授权，因为 child 也可能已经被用户删除。

浏览器丢失 fork 响应时显示“尚未确认分叉结果，请刷新会话列表查看”，刷新但不自动重复 POST、不编造 `NOT_CREATED`。用户再次点击属于另一次新建，得到不同 child ID。这是无持久幂等映射／墓碑时的显式取舍，不增建回执系统。

## 6. Blob 与读取边界

删除事务不 DELETE `blobs`，也不将目标 workspace 当 blob 删除范围。原因是现有 publication 可先独立提交，canonical ref 后续才提交；瞬时 `NOT EXISTS` 不能证明另一个会话未在准备复用它。

继续使用现有 process-local blob GC、原有 grace、批大小与调度。全引用检查必须覆盖：

1. `context_snapshots.blob_id`
2. `transcript_entries.blob_id`
3. `assistant_message_blocks.blob_id`
4. `prompt_queue_items.blob_id`
5. `canonical_image_refs.blob_id`
6. `assistant_visualizations.blob_id`
7. `tool_results.output_artifact_blob_id`

这些精确引用和 FK 保证已提交的共享引用不被回收；grace 是现有 best-effort orphan policy，不是 publication 的持久 pin，不承诺阻止一切跨事务 pending publication 竞争。本轮不通过删除路径新增即时 GC、不缩短 grace，也不扩建 blob 生命周期协议。GC 异常不改变已提交的会话删除结果。

消息、图片、可视化、artifact 下载与 task/activity 读取必须仍验证其 canonical session／ref 归属。会话删除后，知道旧 blob ID／旧 URL 不等于仍可通过原会话端点读取孤儿内容；不得因为 blob 尚未 GC 就绕过引用校验。已经发出的流式响应和客户端缓存无法撤回，不列为删除失败。

## 7. HTTP 与前端

### 7.1 API

| 操作 | 冻结形状 |
|---|---|
| 永久删除 | `DELETE /api/sessions/{id}`；严格 JSON `{ "confirm_permanent_delete": true }` |
| 明确完成 | 200 `{ "status": "DELETED", "session_id": "..." }` |
| 已不存在／确认不存在 | 200 `{ "status": "ABSENT", "session_id": "..." }` |
| 删除／其他控制忙，或另一个有效 writer | 409 `SESSION_DELETE_BUSY`，保留并允许稍后重试 |
| 关闭失败隔离 | 409 `SESSION_DELETE_QUARANTINED`，不声称已删 |
| 数据库明确回滚／约束失败 | 500 `SESSION_DELETE_FAILED`，会话保留；诊断记录具体原因 |
| 数据库／网络导致无法确认 | 503 `SESSION_DELETE_UNCONFIRMED`，保留“不确定”语义 |
| 永久删除 body 为空、false、多余字段或旧 close body | 400，不做删除 |
| 原关闭功能 | 移至 `POST /api/sessions/{id}/close`；严格 `{ "close_conversation": boolean }`，保留原运行时／canonical close 语义 |

现有 `DELETE /api/connections/{id}` 仍只断开该窗口，不删除会话。`POST .../runtime/reopen` 继续仅重开已有会话。旧 DELETE close 路由与调用方一并移除，不保留别名、字段探测或双模式。

GET 可恢复会话返回 null 不足以确认永久删除（CLOSED 也可能返回 null）；服务器删除确认使用新的 repository canonical existence read，而不是这个 UI 查询。前端网络失败后可由用户“重试确认”重新提交同一目标 DELETE；服务器加入已有操作或重新按 canonical 现状处理，禁止浏览器直接判定原事务失败。

本地服务原有 same-origin／认证／请求校验边界继续使用；删除是记忆 domain 内的管理操作，不依赖当前聊天窗口是否 controller，也不得接受请求体覆盖 domain 或 workspace。

### 7.2 UI

会话列表每项的更多菜单提供“删除会话…”，可删除当前或非当前会话。入口不嵌套在整项按钮中；hover/focus 能发现，键盘可达。

使用模态弹窗，不拉长页面。冻结主要内容：

- 标题：“删除这条会话？”
- 显示目标会话名称，必要时显示其工作目录，防止误删另一个同名项。
- 标题下明确提示：“会话及其记录将永久删除，无法撤销。”目标会话使用中性的独立卡片；其下以“以下内容仍会保留”分组列出“已保存的记忆”“其他分支会话”“工作目录”，与删除范围分开呈现。弹窗采用紧凑宽度，危险色仅强调删除图标与主按钮，不把会话卡片整体染红。
- 若会话在本进程已载入，补一句：“将先停止此会话及其后台任务。”
- 取消；危险主按钮按已载入状态显示“停止并删除”或“永久删除”。不要声称仅凭 UI 的“已完成”即可省略 Host close。

提交后主按钮禁用、防止双击，显示“正在停止并删除…”或“正在删除…”。不提供会让用户误以为能撤销已接纳操作的取消按钮；网络断开后保留不确定提示与重试确认入口。

成功后：

- 从会话列表移除目标，重新获取列表；不自行新建空会话。
- 目标仍是当前选择时清空当前选择、消息和连接状态，显示会话页未选择态。
- 用户等待期间切换到另一会话时，不清空新会话，不抢焦点。
- 清除目标 session 的前端临时草稿、缓存、选中 ID 与恢复导航引用；不清理其他 session 的数据。
- 其它窗口因 bridge detach 失去连接，使用现有重连／列表刷新／窗口重新聚焦链路读取 canonical 状态；缺失时退出目标，不能进入自动新建。无需增加 durable/live 删除 event；本地可共享的失效通知仅作加速，不能作为删除成功证据。
- 记忆来源导航遇到旧 locator 时重新读取详情，显示已删除；项目记忆列表依然能找到没有会话的项目。

失败时保留列表卡片，并给具体反馈：忙、没有安全停止、数据库明确失败或结果未确认不能统一显示成“删除失败”。不自动恢复已停止 runtime，不自动发模型消息。

## 8. 实施顺序与删除旧路径

1. clean-v0 FK／grant hard-cut 与 repository aggregate delete／canonical existence read；覆盖 memory SET NULL 与 workspace 清理。
2. writer NEW／EXISTING 分流，迁移所有调用方，封住恢复复活。
3. core/controller/bridge 目标 session admission 与全链路 settle；实现删除 owner 和异常确认。
4. fork 源行锁、服务器 child ID 与 HTTP／前端 ACK 语义同步 hard-cut。
5. 删除 HTTP、关闭新路由、前端确认弹窗与多窗口失效；核验附件读取归属。
6. 更新相关权威文档中本稿取代的 API、fork ID 和 FK 说明，移除矛盾断言／旧测试合同。历史归档只作背景，不实现兼容。

使用已验证的本地开发数据库重置 clean-v0，不为本次未发布内部形状另建 online migration 链。不得为了此功能增加会话历史总量、关系图大小、fork 数量或工具执行次数上限；事务超时明确失败、原子回滚，不分批提交半个 session。

## 9. 验收标准

用聚焦单元／PostgreSQL 集成测试及少量 UI 测试验证，不要求新的长程模型 benchmark。关键验收是以下可观察结果：

1. 全形状 session 夹具包含 turn／tool／task／plan／fork imported history／queue／image／visualization／events；删除后 22 张归属子表无残留，deferred constraints 全通过。失败注入不产生半删。使用 PostgreSQL catalog 精确验证第 3.2 节的引用列、删除动作、deferrability、grant 与 child-side 索引覆盖，不能只靠一条简单 session 的删除成功证明矩阵完整。
2. 全局、项目 facts 与三种关系保持正文／状态／图；只有 exact 被删 owner FK 变 NULL；embedding 保留；来源已删除后仍可编辑、召回、解释和执行记忆页删除预览。
3. 最后会话删除：有项目记忆则 workspace 和记忆页项目仍在；无任何引用则只清 workspace 行。其他 sessions/forks 与物理目录不受影响。
4. 本地 running turn、后台终端、子代理、未完成工具 settlement 能完成既有 close 流程再删除；失败／quarantine 不 DELETE。冷删除不启动 runtime／MCP／模型。
5. 新建／resume／reopen／connect／控制权接管与删除竞争：不存在被自动重建；local close 后 foreign takeover 导致精确拒绝；foreign live lease 拒绝，expired cold 路径按限定合同可删。
6. 源 fork 与 delete 两种锁顺序、child 开启延迟、child 删除后旧 HTTP 请求再次抵达：父子完整性保留，无相同 child ID 复活，无重复自动 fork。
7. 同一 blob 由父子／两个会话引用，删任一 session 后另一会话读图、可视化和 artifact 正常；删除事务不删 blob；旧 session 的资源 URL 不绕过 canonical ref 授权。
8. 删除与记忆编辑／删除／remember 并发，按锁序与 fresh replan 收敛，无半删、来源重绑、图误恢复或空 workspace 误清；锁集合漂移回滚后插入一次跨 Host takeover，确认下一轮重新验证 writer 并拒绝沿用旧授权。
9. 请求断开、数据库 worker 尚未完成、提交 ACK 丢失、数据库不可读、重复 DELETE：不会提前释放 fence、重建 session 或把不确定说成已失败。存在性查询包含 CLOSED 且验证 domain。
10. 前端当前／非当前会话、等待期间切换、多窗口、双击、旧 close body、键盘与模态滚动：确认和结果正确。保留原“关闭”和“重载”能力但无旧 DELETE 兼容路径。
11. oracle 检查产品表仍 28 张；无新增事件／subject／guard／job／墓碑；现有 epoch 内 provider SYSTEM／tools／message prefix 不因别的会话删除被改写。

真实 UI dogfood 只需创建两条短会话、保存一条项目记忆、做一次 fork，再删除源并验证 child 与记忆、编辑来源已删除的记忆、删除最后会话后的项目展示。优先使用已保存的生产连接；无需保存大体积证据，不要求模型长程重跑。不得真的删用户目录或依赖本轮外的 destructive 操作来凑验收。

## 10. 冻结记录

2026-09-22，主审与既有 Session memory schema critic 按当前生产代码交叉核验。正式审阅结论为无 BLOCKING、无 MAJOR；两项 MINOR（FK／索引 catalog 验收、重试重新验证 writer）已并入第 3、9 节。产品与实施边界无待决项。

本次冻结只表示规格可实施，不声称功能已经实现、数据库已经切换或验收测试已经通过。实施中若发现无法满足本稿的真实约束，应修订明确边界，不以兼容分支、放宽断言或新增持久恢复机制绕过。
