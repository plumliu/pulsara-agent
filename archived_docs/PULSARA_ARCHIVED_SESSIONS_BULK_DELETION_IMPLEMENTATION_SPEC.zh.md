# Pulsara 一键永久删除已归档会话实施规格

状态：已实施，2026-10-15。规格已完成 GPT-6 Astra（high）审阅、修订和聚焦复核；实施复用既有单条删除路径，完成聚焦测试和隔离数据库的真实浏览器验收。未删除用户真实会话，未修改保存的生产设置。审阅为静态规格/代码核对，实施验收见第 10 节。

## 1. 目标与范围

设置页的“已归档会话”增加“删除全部”：一次确认覆盖当前完整列表，显示进度及逐条结果。采用有限并发、每条独立事务、整批允许部分成功的语义。

本稿实施后，在批量入口范围内取代旧永久删除规格“不提供批量删除”的限制。原有单条删除、空闲归档、取消归档、记忆保留与 provider prefix 合同继续有效。旧规格位于 `archived_docs/`，只用其仍由当前代码实现的合同作参考；本稿不沿用其中过时的表数等库存统计。

不新增数据库表、列、FK、迁移、权限、committed event、live event kind、subject、append guard、durable job、回执、墓碑、指纹或执行恢复机制。clean-v0 schema 与现有 oracle 的各类别数量不变。已有单条无条件永久删除入口继续承担其原有产品行为，条件删除复用同一实现。

## 2. 当前实现与复用边界

| 当前真源 | 已有责任 | 本轮变化 |
|---|---|---|
| `frontend/components/archived-sessions.tsx` | 获取完整归档列表；每页显示 5 条；恢复及单条删除入口 | 增加全部删除入口，传出完整目标集合；分页仍只影响展示 |
| `frontend/components/session-deletion-dialog.tsx` | 永久删除确认、焦点与键盘管理、busy/error | 在该组件内增加单条/归档集合的显式目标变体，复用弹窗机制 |
| `frontend/app/pulsara-app.tsx` | 删除意图、确认结果、`forgetSession`、列表 revision、提示 | 持有本窗口的批量状态和有限并发循环；导航不丢失执行 owner |
| `frontend/lib/runtime-adapter.ts` | 单条 DELETE、归档列表读取、错误投影 | 增加条件删除 API 方法；不把批量直接交给无条件 `deleteSession` |
| `web_app/http_server.py` | 本地请求保护、严格确认 body、标准响应 | 增加归档条件删除路由，复用现有保护与确认校验 |
| `web_app/session_controller.py` | 单 session 操作互斥、shield、detach/close、提交确认 | 在现有 `SessionRetirementOperation` 增加条件删除 action，复用 owner 链 |
| `conversation_kernel/host.py` | retirement admission、物理 close、加入真实 DB worker | 将条件传给既有提交路径，复用 owner 身份和 worker 等待 |
| `conversation_kernel/_repository/deletion.py` | 行锁、writer 校验、记忆锁序、FK 级联、空 workspace 清理 | 在同一删除方法增加 ARCHIVED 条件和明确跳过结果 |
| PostgreSQL FK、psycopg、现有 blob GC | 单条原子事务、归属级联、孤儿 blob 回收 | 不替换、不增加批量 SQL 清理引擎 |

归档当前也会删除绑定的定时任务（`_repository/archive.py`）；批量删除不恢复这些任务。删除仍沿 sessions 的现有 FK 清理任何剩余归属记录。工作目录文件、保存的记忆和记忆关系保留；创建来源置空；共享 blob 沿原 GC 规则处理。

未入选的 fork 会话保留。某个 fork 本身若也在归档目标集合中，仍会作为独立目标删除，确认文案不得笼统承诺“所有分支保留”。

## 3. 一次确认究竟覆盖什么

1. 只有本次 `GET /api/sessions/archived` 成功返回、来源与当前 adapter/revision 一致、且列表非空时，允许打开确认。已有读取错误时不得用旧列表开启新批次。
2. 打开确认弹窗时，复制该完整列表为本次目标集合，按 session ID 去重，保留 ID、标题与展示路径。集合是前端短期 typed value，不持久化，不生成 batch ID 或 hash。数量不使用 `visibleItems` 或当前页。
3. 弹窗显示固定 N，文案为“永久删除本次列表中的 N 条已归档会话？无法撤销。执行时已取消归档的会话将跳过。”保留内容只列“已保存的记忆”和“工作目录”，不再提示其他会话会保留。只有明确确认后才发送写请求。
4. 后续刷新、跨窗口归档或列表 revision 变化不扩大目标集合。新 ID 不纳入；同一 ID 在执行时是 OPEN 则跳过。
5. 本轮使用“目标 ID + 执行时 ARCHIVED”条件，不承诺锁定某次归档 occurrence。同一目标取消归档后又重新归档，执行检查时仍为 ARCHIVED 就可以删除；若产品以后要求某次归档身份，应单独指定，不能暗加 generation 或时间戳 token。
6. 不冻结全库生命周期，也不承诺最终归档列表必然为空：其他窗口新增的归档、失败项、未发送项可能仍在。

## 4. 条件删除 API 与结果

新增 `DELETE /api/sessions/{session_id}/archived`，请求体严格为：

```json
{"confirm_permanent_delete": true}
```

复用现有 Host/same-origin/Sec-Fetch-Site、数据库 ready、draining、JSON body 资源边界与标准错误处理。必须将新路由的 `archived` suffix 明确加入 `_chat_write_admission` 的 connection-independent 分类，与普通 DELETE/归档/取消归档一致：冷归档删除不需要 `X-Pulsara-Connection-Id` 或聊天 controller 身份，不先加载 runtime 来取得连接。此分类不绕过 `_security` 或 memory domain 校验。false、数字 1、缺字段、未知字段均拒绝，且不能触发 retirement。该路由只是单条删除的条件变体，不创建批量 endpoint 或服务器批量 owner。

普通 `DELETE /api/sessions/{session_id}` 请求/结果保持原合同。新的 adapter 方法建议命名 `deleteArchivedSession`。新条件路由的成功响应明确携带 `status`、相同 `session_id` 和 `cleanup_warning`（无警告时为 null），不添加持久或通用通知机制：

```json
{
  "status": "NOT_ARCHIVED",
  "session_id": "session:…",
  "cleanup_warning": {
    "code": "SESSION_DELETE_QUARANTINED",
    "message": "本条已跳过，但本地会话准入未能安全释放；请重启 Pulsara 后再操作此会话。"
  }
}
```

`cleanup_warning` 只用于确定数据结果后本地 owner 被隔离的情况；code 固定为既有 quarantine 码，message 按 DELETED/ABSENT/NOT_ARCHIVED 描述数据结果及重启要求。adapter 必须保留警告，UI 逐条展示，不用单纯刷新代替重启。普通无条件路由的结果结构不因本批量入口改变。

| HTTP / status | 精确含义 | 前端处置 |
|---|---|---|
| 200 / `DELETED` | 已确认本条删除提交 | 标记已删除，调用 `forgetSession` |
| 200 / `ABSENT` | 当前 domain 下已确认不存在；不声称本请求删除了它 | 标记已不存在，调用 `forgetSession` |
| 200 / `NOT_ARCHIVED` | 同 domain 的 canonical 预检读取或事务行锁下观察到 OPEN，按该观察跳过；未执行 DELETE | 标记已跳过，不清草稿、不调用 `forgetSession` |
| 503 / `SESSION_DELETE_PREFLIGHT_FAILED` | 进入物理收尾前的数据库读取失败，且本次 admission 已安全释放；未 detach/close 或提交 DELETE | 标记未执行失败，保留条目，用户可重试 |
| 409 / 既有 busy、writer、quarantine 错误 | 本条未满足既有准入或物理安全条件 | 保留条目与具体原因，允许用户处理后重试 |
| 500 / `SESSION_DELETE_FAILED` | 实际 worker 已结束，提交后确认仍存在 | 保留条目，用户可重试 |
| 503 / `SESSION_DELETE_UNCONFIRMED` | 实际 worker 已加入，但数据库不可读，未能确认存在性 | 标记待确认；保留现有隔离和原单条 operation |
| 传输异常、无效 JSON、错 session_id、未知 status | 浏览器尚不能确认结果 | 标记待确认，不自行推断回滚或已删除 |

新条件 handler 必须将 `SessionControlRejected` 的 action 冲突明确映射为 `SESSION_CONTROL_BUSY`（409），保留具体 public code/message，复用标准 `HttpPublicError` 或当前公开错误响应 owner；不能落入通用 RuntimeError 而只显示 `LOCAL_RUNTIME_ERROR`。本轮不借此重写其他 HTTP 错误系统。

不因 `retryable` 自动重发。用户重试待确认项时仍调用条件入口，由原 owner 加入或确认；不改投普通无条件入口。HTTP 错误中的 `NOT_ARCHIVED` 不用于跳过结果，避免混淆确定跳过与运行失败。

## 5. controller/core：只扩展既有 owner

### 5.1 操作身份与重复请求

`SessionRetirementOperation.action` 增加 `delete_archived`，与 `delete`、`archive`、`unarchive` 互斥。同 ID、同 action 重复请求加入已有 task；不同 action 返回现有 control busy，不能把条件请求加入普通删除，也不能用普通删除接管待确认的条件请求。保留 exact owner、shield、prior resume/fork 加入、controller/core 的短期锁规则。

条件属于 operation 的冻结语义，沿 core 提交显式传给 repository；不能由 UI 的 lifecycle 推断执行权限，也不能在重试时丢掉条件。用显式 typed 变体或 `Literal`，不增加通用 predicate/filter 框架。

### 5.2 准入、预检与提交

1. 在现有 controller 单 session operation 准入下加入 prior resume 和相关 fork。继续验证 memory domain。
2. 复用 core `begin_session_retirement`，等待此前已获准的目标 open/fork/close owner；不在全局锁中等待。
3. 在 bridge detach / `quiesce_session_retirement` 之前读取 canonical lifecycle：无同 domain 行则返回 ABSENT，OPEN 返回 NOT_ARCHIVED；释放本次 admission，不能把“条件未满足”按 physical close 不完整隔离，也不能关闭已恢复的 Host 或连接。预检跳过只需一次 canonical 读取，不为此新增行锁事务；其返回描述当时观察，不能承诺之后一直为 OPEN。
4. ARCHIVED 沿现有 bridge detach、full close 与 `commit_session_deletion` 路径执行；不为冷归档启动 Host、模型或 MCP。预检只避免无必要的物理操作；执行授权仍在下面的行锁中重验。
5. `commit_session_deletion` 传递 ARCHIVED 条件；返回 NOT_ARCHIVED 时正常完成 gate settlement，不自动恢复或启动 runtime，不当成 quarantine。
6. 跨进程取消归档/恢复可能发生在预检之后；数据库条件保证不误删 OPEN，writer guard 继续保证不删除新 owner。跨进程停止能力不增加，依旧不能关闭本进程不掌握的 owner。

预检失败处理必须闭合：在尚未调用 bridge detach、也未开始 physical close 时，canonical 存在性/lifecycle 读取抛错应释放本次 controller/core admission，返回 `SESSION_DELETE_PREFLIGHT_FAILED`（503），不能进入模糊提交确认或以 `physical_full=False` 隔离仍正常运行的会话。如果释放本身失败，沿既有 quarantine owner 隔离，返回明确 `SESSION_DELETE_QUARANTINED`，告知本操作未删除、需重启后重试；不得误留 unconfirmed owner。core 此前已接纳的 open/fork/close owner 若未安全结算，沿其现有异常合同处理，不能将这种未结算归为纯读取失败。进入 bridge detach 后的错误继续采用原 quarantine 合同。复用现有短期操作的阶段/字段或局部 typed 结果表达这一区分；不添加持久状态或新阶段框架。

条件路径完全保留原物理关闭、exact writer release、冷 writer lease、模糊 ACK、请求 waiter 丢失及提交后清理失败的保证。不可绕开它们直接在 HTTP handler 执行 SQL。

### 5.3 模糊提交与重试

条件删除同样使用 `_confirm_session_deletion` 的 canonical 存在性确认：不存在返回 ABSENT；仍存在则原删除未达成，返回既有明确失败；不可读则保留 unconfirmed 和隔离。不能仅因现在变成 OPEN 就声称本次 worker 返回了 NOT_ARCHIVED，也不能为确认结果重新执行 DELETE。

NOT_ARCHIVED 是预检或既有 worker 正常返回的确定结果，必须在通用 `except Exception` / ambiguous-commit fallback 之前保留。明确 skipped 后的 gate 清理即使失败，也不得翻译成 DELETED；数据结果与本地清理是否安全分开。将 `_finish_session_delete` 当前已计算的最终 quarantine 结果作为明确的短期返回值交还调用方（例如 CLEAN/QUARANTINED），不再只吞掉日志；条件路由据此填充 §4 的 `cleanup_warning`。覆盖 preflight 正常退出、repository 结果及 ACK 确认后所有确定结果的 settlement 分支。

隔离时警告要求重启 Pulsara，刷新只能同步列表，不能解除隔离。已确认 DELETED/ABSENT 同样不得因刷新或 gate 清理失败改报数据仍存在。清理函数给普通无条件调用方增加内部返回值不改变其原 HTTP 合同；不得增加事件/持久告警来传递这一短期观察。

用户后续重试遵循既有单条 owner：第一次可能只完成结果确认，明确失败并释放 owner 后再发出的操作才允许重新尝试；规格不要求一次重试按钮跨两个 owner 自动重发。

## 6. repository：同一事务内多一个条件

扩展现有 `delete_session` 参数，显式选择无条件或 ARCHIVED 条件；返回 union 加入 NOT_ARCHIVED。只有新路由使用该条件；现有生产/测试调用点显式保留它们原来的无条件语义，不引入第二套删除函数体。

在现有 `SELECT … FOR UPDATE` 中读取 lifecycle，保持 READ COMMITTED：

```text
session row FOR UPDATE，按同 memory domain 查找
  -> 不存在：ABSENT
  -> 条件为 ARCHIVED 且 lifecycle=OPEN：NOT_ARCHIVED，无 DELETE
  -> 原 exact writer / 冷 lease 校验
  -> 原 workspace identity locks
  -> 原 memory fact rows FOR UPDATE
  -> 原 memory relation identity locks
  -> 原来源集合 fresh snapshot 重验
  -> DELETE session、FK actions、空 workspace 清理
  -> SET CONSTRAINTS ALL IMMEDIATE、提交
```

源集合漂移、DeadlockDetected、SerializationFailure 沿已有单次数据库 deadline 整笔重试；每轮重新锁 session、重验 lifecycle 和 writer。不得复用上一轮的“仍为归档”观察跳过校验。事务不跨网络、bridge 或 Host shutdown。

取消归档和 DELETE 继续用相同 session row lock 线性化：取消归档先提交且保持 OPEN，条件删除跳过；删除先提交，取消归档沿现有 missing 结果，不复活行。不同会话共享 workspace 或记忆来源可能争锁，遵守既有锁序和重试即可，不添加全库锁或多 session 大事务。

## 7. 前端执行、并发与生命周期

### 7.1 最小执行 owner

批量确认/执行状态放在已有 `PulsaraApp`，经 `SettingsView` props 传给 `ArchivedSessions`。复用现有删除意图、提示、`forgetSession` 与 session revision owner；SettingsView/ArchivedSessions 卸载不会重新创建任务或丢失运行中的批次。单条和批量删除共享本窗口 busy admission，防止双击确认、重复批次及同窗口操作重叠。

保留 typed 目标数组、取下一项的索引、逐条结果、少量运行中的 Promise 即可；不新增通用任务调度框架、持久队列、batch registry、客户端数据库或后台轮询服务。ID/status/title 的逐条结果用于用户查看，不是数据库删除的权威。

### 7.2 物理并发边界

本轮明确采用**每窗口一个批次、最多 3 条条件请求同时在途**。这是本批量入口的资源政策：当前 PostgreSQL provider 每次操作打开物理连接，core 还会使用 `asyncio.to_thread`；不能按全部归档数瞬间创建 DB worker 和持锁事务。3 是保守的产品默认，不声称来自性能测量或已有连接池配额。

仅创建 `min(3, N)` 个轻量消费循环，每条 await 完成或失败后再领取下一条；不能 `Promise.all(targets.map(delete))`。该边界不限制目标总数、会话历史、批次时长或重试次数；其余目标留在当前 typed 数组中等待，队列不会因超过 3 而拒绝。没有整批 deadline；每条使用原单次资源边界。多个窗口各自执行时由既有服务器 owner/数据库协调，本轮不声称提供全服务的批量并发配额。

### 7.3 进度、错误与退出

- 每条普通失败不终止其他消费循环。进度用“已处理 X / N”，同时区分已删除、已不存在、已跳过、失败、待确认；pending 与正在处理仍单独可见。确定结果附带的 cleanup warning 作为该条的额外提示，不改变计数分类、不抹掉数据结果；需要重启的提示不被统一刷新成功覆盖。错 ID/status 的响应不能更新另一个目标或触发 `forgetSession`。
- 对已确认 DELETED/ABSENT 复用 `forgetSession`，清理草稿、选中态与相关本地索引；NOT_ARCHIVED、失败、待确认不清这些状态。不得为归档集合另写一个只过滤列表的成功路径。
- 批次进行中禁用归档页恢复、单条删除、全部删除和会改变目标的页面操作；分页可以继续浏览，不影响集合。确认时同步登记根 busy admission 并关闭确认弹窗，恢复焦点与主界面 inert；执行过程不继续占用现有 busy modal，否则无法导航。根 owner 在右下角现有 toast 栈内显示“已处理 X / N”，点击可查看分页结果；不新建通知中心或独立悬浮入口。结果弹层打开时隐藏此 toast；处理中关闭弹层会恢复进度入口，最终结果未查看前保留提醒，查看完成结果后不再显示（包括查看期间完成、随后关闭弹层的情况）。重试或新批次重新提供提醒，不另发重复完成 toast。关闭结果弹层不停止执行，不能通过“取消”表达回滚。切到其他应用视图时根 owner 仍继续，未读结果与进行中进度仍可达。
- 批次结束后统一刷新归档与普通列表、夹紧分页并提升 revision；不每删除一条就重新读取完整清单。刷新失败独立提示，保留已确认结果和原失败原因，不改报删除失败；下次可重试刷新。
- 结果列表可分页，不截断失败详情，不设置归档数或错误数总上限。提供“重试未完成项”确认，只涵盖失败/待确认目标，不重发成功和已跳过项，不吸收新增归档。再次执行仍用条件路由。
- 正常导航不会停止批次；浏览器刷新/关闭或 `PulsaraApp` 真正卸载时停止领取下一项。尚未发送项未执行；已被服务器接纳的单条由原 shield owner 结算。必须用根 owner 的有效性检查阻止卸载后循环继续发送或旧回调改写新实例状态。无需持久化队列，不承诺全部执行完。
- 重新打开后只读取当前 canonical 列表，不从 localStorage 自动续删。用户可以再次开启一个新的、明确确认的批次。网络异常不得靠查归档清单缺失直接当作 ABSENT，因为目标也可能只是取消归档。

## 8. 测试与验收

复用并扩展既有测试文件，保持真实断言，不能靠 skip/xfail 获得通过。

### 8.1 PostgreSQL 集成

扩展 `tests/test_session_permanent_deletion.py`、`tests/test_session_archive.py`：

1. 条件删除 ARCHIVED 成功；OPEN 返回 NOT_ARCHIVED，原数据、writer、记忆来源不变；missing/其他 domain 返回 ABSENT。
2. 用真实两连接与 barrier 控制取消归档/删除锁顺序，覆盖取消归档先提交、DELETE 先提交；必要的等待和 fresh snapshot 不能只靠 mock lifecycle。
3. 重试后生命周期变化也必须跳过；有效远端 lease 拒绝；同 workspace 多归档删除并发无残留，保存记忆/关系与未入选 fork 保留。fork 本身入选时独立删除。
4. 沿既有 shared blob/GC 测试证明剩余引用不被批量移除；保留原约束立即检查与 rollback 测试。schema oracle 不增类别。

### 8.2 controller/core 与 HTTP

扩展 `tests/test_session_deletion_control.py`：

1. 同 action join、不同 action busy，条件不能丢失；普通单条请求保持原行为。
2. OPEN/missing 预检退出不 detach、不 close、不留 quarantine；注入 core admission 后的 lifecycle 读取异常，验证两层 gate 释放、原 Host/连接不变、后续用户重试可成功；此前获准的 resume/fork 先结算，其未结算失败不伪装成读取失败；controller/core 全局锁不包裹等待。
3. worker 返回 NOT_ARCHIVED 正常释放 gates；preflight 正常跳过、repository 结果和 ACK 确认后的清理失败均保留数据 status，并通过真实 HTTP 响应的 cleanup warning 告知需重启；读取失败后的释放失败则明确 quarantine，而非 unconfirmed。不能仅断言没有误报 DELETED。
4. HTTP waiter 取消不取消服务器已接纳的单条；模糊 ACK 用同 domain 存在性确认，unconfirmed 重试只确认、不得重发 DELETE。
5. 新路由在完全无聊天连接 headers 的冷归档下可成功，且不调用 `require_chat_controller` 或启动 Host；严格 body、same-origin/Sec-Fetch-Site、数据库 ready、busy/error JSON、错 domain、draining 均有效；真实 action 冲突响应须包含 SESSION_CONTROL_BUSY，而非 LOCAL_RUNTIME_ERROR；普通 DELETE 与归档/取消归档的测试继续通过。

### 8.3 前端

扩展 `tests/frontend/archived-sessions.test.tsx`、`tests/frontend/runtime-adapter.test.ts`、`tests/frontend/pulsara-app.test.tsx` 及现有删除弹窗测试：

1. 多页 12 条的确认数与目标是 12，取消零写入；列表加载失败/过期/空列表不能发起；确认快照不随 revision 扩大。
2. 用可控 Promise 验证在途最多 3、其余逐步领取；至少覆盖两个页面的目标，不写与实现同构的纯计数器测试。
3. 部分成功、ABSENT、NOT_ARCHIVED、busy、unconfirmed、错 ID/status；只有已确认成功调用根 `forgetSession`，批次继续处理其他项，刷新失败不抹掉结果；adapter 和 UI 保留 cleanup warning，确定跳过加隔离时可见重启提示。
4. 双击、单条/批量操作互斥、设置页卸载再进入不重复发送；根卸载停止领取，已在途结果不能污染新 owner。
5. 重试只含未完成目标；批次完成分页夹紧；确认后弹窗解除 inert，可导航且根进度入口可达；弹窗焦点、Escape/Tab、busy 状态和窄屏文案可用。

### 8.4 激活检查

使用仓库根 `.venv/` 的 `uv` 环境运行聚焦 Python/controller/PostgreSQL 测试，按 frontend 当前脚本运行聚焦 Vitest 与 `build:local`；新增类型应通过现有 TypeScript 检查。最终检查限于相关契约与现有永久删除回归，不重复无关全库测试。

在经核验的 disposable 本地测试数据库中建立多页归档、同 workspace、记忆、fork 与取消归档竞争样本，完成真实本地 HTTP + UI 检查。不得直接用用户真实归档记录验收。保存设置只读，用现有 resolver/注入 owner；不打印实际凭据。此功能没有模型输入或执行语义变化，不要求新增真实模型长程 dogfood；不修改 provider SYSTEM/tools/messages 或重建 prefix。

## 9. 实施顺序与完成条件

1. 在唯一 repository 删除路径和既有 controller/core owner 中增加条件语义，验证行锁竞争、preflight 清理、join/ACK。
2. 接入新单条条件 API/adapter，复用请求保护；不新增后端批量层。
3. 扩展既有删除弹窗与根前端 owner，接入归档列表全部入口、有限并发、进度和逐条结果。
4. 完成相关测试、构建、本地 disposable UI 验收，并更新当前用户文档描述新入口。旧文档可保留历史，本稿实施后只描述最终单一路径。

完成条件：一次确认覆盖全部分页；逐条确定结果可解释；OPEN 不被条件删除；保存记忆/目录和未入选会话保留；失败可处理且不自动重发；卸载不制造后台继续领取；所有既有删除保障与 schema/oracle 库存保持。

## 10. 本次实施验收

- 新条件路由、既有 controller/core owner、唯一 repository 删除事务已接通；归档条件在每次事务行锁重验。普通删除保持原行为，repository 调用显式选择 None 或 ARCHIVED。未改 clean-v0 schema、数据库权限或事件库存。
- 前端完整快照、三路消费循环、跨视图进度、逐条结果、失败项确认重试和根卸载停止领取已实现；复用原确认弹窗和 `forgetSession`。本地发布用静态资源已重建。
- PostgreSQL/controller/HTTP 聚焦、普通删除调用点及共享图片、孤儿 blob GC 回归：55 项通过。真实行锁竞争覆盖取消归档先提交与删除先提交；同 workspace 多条并发删除保留记忆和未入选分支。
- 归档列表、adapter、设置页及完整 PulsaraApp 测试：260 项通过。当前 Node 25 的原生 Web Storage 与 jsdom 冲突，运行时使用 `NODE_OPTIONS=--no-experimental-webstorage`，未改测试断言或增加 skip/xfail。TypeScript、ESLint、Ruff、`git diff --check` 和 `build:local` 通过；构建仍有现有大 bundle 提示。
- 真实本地应用 + Playwright：12 条、3 页归档；确认后用另一 HTTP 客户端取消归档 1 条，对另外 1 条注入明确事务回滚。首批结果为删除 10、跳过 1、未完成 1；移除测试触发器后，重试只发送那 1 条请求并成功，归档列表最终为 0。数据库确认留下被跳过的 OPEN 会话和未入选 fork；保存记忆保留、创建来源置空，工作目录文件保留。桌面与 390px 窄屏结果弹窗已视觉核验。
- 可复用的隔离验收入口为 `tests/dogfood/run_archived_sessions_bulk_deletion_dogfood.py`，只读加载有效 Pulsara home 的生产设置，再注入临时 home 和经验证的 disposable 本地数据库；不发模型 turn。Ctrl-C 记录 canonical 结果、关闭应用并删除临时数据库。本次浏览器会话、应用和临时数据库均已清理。首轮部分失败及重试验收可检查本地 `output/playwright/archive-bulk/report.partial-retry.json`、`partial-results.png`、`retry-mobile.png`；输出目录不提交进仓库。
- 2026-10-15 结果提醒调整：应用与 toast 组件测试共 160 项通过，类型检查、相关 ESLint 和本地构建通过。真实浏览器在隔离数据库删除 12 条归档，确认结果入口位于现有 toast 栈、距窗口右侧与底部各 18px；点击查看后关闭，再切换到总览，toast、旧悬浮入口和结果弹窗均为 0。运行报告为 `output/playwright/archive-bulk/report.json`，截图为 `result-toast-unread.png`；浏览器、应用与临时数据库均已清理。
