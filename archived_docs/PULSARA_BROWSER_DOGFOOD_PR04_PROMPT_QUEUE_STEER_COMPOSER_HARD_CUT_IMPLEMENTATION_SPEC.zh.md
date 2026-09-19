# Pulsara 浏览器 PR04：输入队列、即时引导与 Composer Hard Cut 实施规格

状态：**IMPLEMENTED, REACTIVATION PENDING — 2026-09-12 审查后修正了 action 查询、编辑草稿保留与未观察到 PENDING 的 steer 终态收敛；修订后验收见第 23 节（工作树，未提交）**。

原 activation 证据：[PR04 activation evidence](output/playwright/pr04-dogfood/activation-evidence.md)。它不证明第 23 节修订后的最终安装产物和真实浏览器已重新验收。

日期：2026-09-12。

索引：[浏览器 dogfood 复盘与后续修复方案](PULSARA_BROWSER_DOGFOOD_REVIEW_AND_FIX_PLAN_2026-09-08.zh.md)。

建议 PR 标题：`feat: make queued prompts editable and atomically steerable`。

本规格承接 PR02 的 exact prompt queue、Subagent-R1 的 exact steer/wait 语义，以及同日对 Codex steer/finalization 路径的只读研究。本文只授权输入队列和当前 ROOT 引导体验的改动；它不重新开放 provider-input prefix、调度、权限、规划、子任务完成或 durable recovery 的其他设计。

## 1. 产品目标

当前 ROOT/agent loop 运行期间，用户的新输入先进入可见队列，不再依赖组合键直接把 composer 草稿改投当前轮。每条尚未处理的输入都在 composer 上方显示，并有三个平铺操作：

1. **发送**：把这条 exact 队列输入作为引导交给当前 ROOT；
2. **编辑**：取消这条 exact 队列输入，并把原文放回 composer；
3. **删除**：取消这条 exact 队列输入，不放回 composer。

若用户不操作，当前 loop 结束后，原 `NEW_TURN` 队列继续按既有 FIFO 自动启动后续 ROOT。不得要求用户逐条再次点击“发送”，也不得用浏览器本地队列代替服务端真源。

点击“发送”后，只要 kernel 已经接纳改投，界面立即在主时间线显示蓝色“引导”卡；用户不再看到它继续以“等待处理”形式停在 composer 上方。该视觉表示“用户已经发出引导”，不表示模型已经读取、理解或执行。

## 2. 冻结产品决定

### 2.1 输入与操作

- ROOT 运行时，composer 的 Enter 始终创建 `NEW_TURN` 队列项。
- Shift+Enter 仍换行。
- 删除现有“⌘/Ctrl+Enter 直接 steer composer 草稿”的产品路径和提示；当前轮引导只从已建立 exact 身份的队列项发起。
- 队列项按服务端 `queue_sequence` 显示，两条同文仍是两项。
- 每项右侧只显示带图标的“发送”“编辑”“删除”；不做三点菜单、不做“关闭排队”、不做侧边聊天、不做拖拽排序。
- “删除”不弹二次确认；只有服务端 CAS 成功后才从页面消失。
- “编辑”只有在 composer 当前无草稿时执行。已有草稿时保留两份内容、拒绝本次操作并提示先处理当前草稿；不得覆盖、拼接或静默丢弃任一文本。
- 编辑成功后恢复该输入的原始正文和已冻结的请求权限到 composer，并聚焦输入框；不恢复已经失效的模型快照或规划 workflow。
- 编辑在途／结果未知时保留空 composer，禁用所有草稿写入入口，包括已经打开的技能菜单，而不仅是 textarea。若恢复连接时已有另一份草稿，保留原输入的待回填正文并允许用户处理现有草稿；清空／提交后再恢复，不覆盖也不永久锁定输入框。未回填的 accepted edit 不因 connection generation 变化而丢弃。

### 2.2 loop 结束后的队列

未被发送、编辑或删除的 `NEW_TURN` 项继续由现有 Host queue owner 自动消费：

```text
当前 ROOT 结束
  → FIFO 第一条 NEW_TURN 启动下一 ROOT
  → 该 ROOT 结束
  → FIFO 下一条继续
```

“全部发送出去”指保留每条输入的独立 command/queue/turn 身份并依次处理，不指把多条正文拼接成一次模型请求，也不指浏览器在 idle 后批量重发。

STOP、provider failure、自然完成或其他真实 ROOT 终态都不改变这条 FIFO 规则；规划 interaction 等既有合法 admission fence 仍可让队列保持等待。

### 2.3 即时“引导”展示

下列事实必须分开：

| UI/运行事实 | 可以表达 | 不可表达 |
| --- | --- | --- |
| 用户点击发送，HTTP 尚无结果 | 正在提交，原队列项仍在原位并禁用操作 | 引导成功 |
| kernel 原子改投已接纳 | 立即显示蓝色“引导”卡 | 模型已读／已理解 |
| safe point 已消费为 canonical `USER_STEER` | 用 canonical 卡原位接替乐观卡 | 必然已经进入远端服务 |
| successor provider request 实际编译／发送 | 本 PR 不新增对应 badge | 用 queue status 推断请求包含 |
| 改投被拒绝 | 原队列项保持；显示真实原因 | 先显示永久成功再静默撤销 |

蓝色卡只显示图标、精确“引导”、原文和提交时间。无需可见的“排队中”后缀；可访问性状态可以说明“引导已提交，等待当前任务接收”，但不得宣称模型已经收到。

canonical `USER_STEER` 到达后，前端按 action command ID、replacement queue item ID 与 consumed entry 的 exact input source 去重；不得按正文匹配，不得出现两张同文“引导”。

### 2.4 turn 与 model round

一次 assistant 最终文本显示完成，不等于 ROOT 已完成 settlement。若改投在 completion fence 之前被 kernel 接纳：

```text
同一个 ROOT turn_id
  assistant terminal-looking message
  USER_STEER
  successor provider request
  assistant continuation
```

这是同一个产品/canonical turn 中的新 instruction boundary 和下一次 provider model round，不是新的用户 ROOT。主时间线继续只在该 `turnId` 首个 assistant 片段显示一次图标和精确名称 `Pulsara`。

若 completion fence 先赢，当前 ROOT 不再接纳改投；原 `NEW_TURN` 队列项保持 PENDING，随后按 FIFO 进入新 ROOT。前端不得在失败后自行把相同正文重新提交为 steer。

## 3. Codex 研究结论及采用边界

只读研究 `/Users/plumliu/Desktop/python_workspace/codex` 确认，Codex 把以下边界分开：assistant item 完成、provider response 完成、RegularTask 完成、product-visible turn 完成。

关键源码事实：

- TUI 在运行期间提交输入后先建立 `PendingSteer` 预览；它表示已经交给 Core，不证明已进入模型请求：`codex-rs/tui/src/chatwidget/input_submission.rs:350-362/409-415/451-455`、`input_queue.rs:39-43`。
- Core steer 只在 `active_turn.task` 仍存在时接纳，并把输入加入 pending queue：`codex-rs/core/src/session/turn_input.rs:512-602`。
- provider `ResponseEvent::Completed` 后仍检查 pending input；存在 steer 时，同一任务继续 successor sampling：`codex-rs/core/src/session/turn.rs:446-475/2650-2694`、`tasks/regular.rs:75-98`。
- 真正关闭 current-turn steer 的 fence 是 `on_task_finished` 取走 `active_turn.task`，随后才发 `TurnComplete`/idle：`codex-rs/core/src/tasks/mod.rs:618-647/784-865`。
- fence 后 app-server 收到 `NoActiveTurn`，客户端才改走新的 `turn/start`：`codex-rs/tui/src/app/thread_routing.rs:740-805`。

Pulsara 采用以下产品原则：

1. terminal-looking assistant message 不是 ROOT completion fence；
2. accepted steer 可以在同一 `turn_id` 中触发 successor provider round；
3. UI 可在服务端接纳后乐观展示用户的引导动作；
4. expected session/owner/turn 身份决定 stale steer，不由页面当前视觉状态推断。

Pulsara **不照搬** Codex 的窄竞态：Codex 存在 steer 返回 accepted、却恰好落在 RegularTask 最后一次 pending 检查之后，最终成为 leftover input 而没有 successor request 的窗口（`codex-rs/core/src/session/tests.rs:11717-11833`）。本规格要求 admission 与 settlement 有唯一原子赢家；不得对用户显示已接纳当前轮引导后又把它留给未来 ROOT。

## 4. 与既有规格的关系

- PR02 §6 的 command/queue/content/consumed-entry exact identity、blob hydration、observer 与网络未知纪律继续有效。
- 本规格在自身范围内取代 PR02 §1.2 “编辑/撤回队列不纳入”和 §6.3 “不提供删除按钮”的旧限制；PR02 其他激活结论不重开。
- Subagent-R1 的 exact steer 优先、`wait_agent → steer_available`、工具先闭合再由 safe point 接纳，以及 `NEW_TURN` 不打断 wait 的语义继续有效。
- PR03 的 Host closing、control owner、STOP、effect settlement 与 provider-prefix continuity 继续有效。
- F07 原文保真适用于队列正文、乐观引导、编辑回填、canonical steer 与复制；任何路径不得翻译或重写正文。

上述为实施前的 PR02/Subagent-R1 起点。PR04 已完成单一路径 hard cut；第 22 节保留原 activation，第 23 节记录审查后的修正和当前验证边界。

## 5. 唯一权威与职责

| 事实 | 唯一 owner | 前端职责 |
| --- | --- | --- |
| 队列项身份、正文、状态、顺序 | `prompt_queue_items` | 展示 canonical PENDING 项 |
| 原始提交幂等身份 | `session_commands`＋原 command ID | 不按正文猜测 |
| 取消/改投操作身份 | 新 action command ID＋`session_commands` | 网络未知时只查询原 action |
| 当前可 steer ROOT | 当前 Host/ROOT owner＋canonical RUNNING turn | 发送点击时冻结 exact target |
| 改投与 ROOT settlement 先后 | repository 同一 writer transaction/fence | 不在 React 拼接两次请求 |
| pending steer 消费 | 现有 steer queue/safe-point owner | canonical 到达后去除乐观卡 |
| loop 结束后启动新输入 | 现有 Host FIFO queue loop | 不做浏览器批量发送器 |
| 乐观卡 | 当前浏览器 process-local projection | 可丢弃、可重建，不是 receipt |

禁止新增前端持久队列、localStorage 恢复、scheduler、durable delivery queue、receipt 表、replay worker、fingerprint registry 或正文 hash 对账。

## 6. Kernel 操作合同

### 6.1 `CANCEL_QUEUED_PROMPT`

输入：

- action `command_id`；
- exact `source_queue_item_id`；
- 当前 session/attachment controller 身份。

校验：

- action command 非空且在 session 内唯一；
- source 属于同 session；
- source 是 `PENDING + NEW_TURN`；
- 当前 Host 未 closing/closed；
- 调用 attachment 仍为 controller。

同一 writer transaction：

1. 写入或确认 `session_commands.command_kind='CANCEL_PROMPT'`；
2. 对 source 执行 PENDING→CANCELLED CAS；
3. `terminal_reason='USER_CANCELLED'`；
4. 追加既有 `PromptCancelled` 事件。

不删除行、正文、permission snapshot 或原 command；不新增 cancellation event 类别。相同 action command＋相同 source 可幂等查询；相同 command ID 指向另一 source 返回 command conflict。

### 6.2 `STEER_QUEUED_PROMPT`

输入：

- action `command_id`；
- exact `source_queue_item_id`；
- 点击时冻结的 exact `target_turn_id`；
- 当前 session/attachment controller 身份。

请求不重复携带正文、不携带 permission、不携带模型配置。正文由 repository 从 source canonical content 引用读取；大 blob 不经过浏览器重传。

同一 writer transaction 必须完成：

1. 复核 source 为同 session 的 `PENDING + NEW_TURN`；
2. 复核 target 为同 session 的 RUNNING ROOT，且仍属于当前 Host 的可接纳边界；
3. 拒绝带 `pending_plan_handoff_*` 的 source，返回 `PROMPT_HAS_PLAN_HANDOFF`，不改变任何状态；
4. 创建 action `session_commands`，kind 为 `STEER_QUEUED_PROMPT`，target 指向 replacement queue item；
5. 将 source CAS 为 CANCELLED，`terminal_reason='USER_REDIRECTED_TO_STEER'`；
6. 使用 action command 派生新的 replacement queue item ID；
7. 为 replacement 分配新的 `queue_sequence`，其顺序表示用户点击“发送”的顺序；
8. replacement 为 `PENDING + STEER_ACTIVE_TURN`，target 为 exact ROOT；
9. replacement 复用 source 的 immutable content 引用、digest、size、media type 与 codec；
10. replacement 不携带 NEW_TURN 的 model binding、permission snapshot、permission 字段或 plan handoff；
11. 对 source 追加既有 `PromptCancelled`，reason 为 redirected；对 replacement 追加既有 `PromptQueued`。

操作只使用现有 command `semantic_digest` 做 action idempotency，digest 必须覆盖 source queue ID、replacement queue ID、target turn ID 和操作种类。这是现有 durable command 边界，不新增普通 DTO fingerprint。

不原地改写 source 的 delivery mode。原地改写会破坏原提交的 `queue_prompt.v2` 幂等确认，并让旧 command 对同一行观察到不同语义。

### 6.3 admission 与 settlement 原子赢家

| 先发生的事实 | 必须结果 |
| --- | --- |
| 改投事务先提交 | target ROOT 的正常 completion 不得越过该 pending steer；safe point 消费后继续同一 turn |
| ROOT completion/close fence 先提交 | 改投拒绝 `STEER_TARGET_CLOSED`；source 保持 PENDING NEW_TURN |
| source 被 FIFO 消费先提交 | 改投拒绝 `PROMPT_ALREADY_CONSUMED`；不得再造同文 steer |
| source 被编辑/删除先取消 | 改投拒绝 `PROMPT_NOT_PENDING` |
| 两个“发送”动作并发 | 只有一个 action 可取消 source 并创建 replacement；另一项按 exact 状态拒绝 |
| 网络 ACK 丢失但事务已提交 | 新连接只查询原 action command；不得重新改投 |

Host 不得先 poll 再调用两个 repository mutation。目标/root/source 的判断和状态迁移必须由物理 writer owner 在一个事务边界裁定；数据库读取期间不持有会妨碍 runner settlement 的 asyncio lock。

提交后由 Host 触发现有 queue wake 与 root input activity 通知。通知只是 level-triggered 重新检查提示，canonical queue row 才是真相。

## 7. 协议 hard cut

在 terminal protocol V3 的唯一 schema 中新增：

- `CommandKind.CANCEL_QUEUED_PROMPT`；
- `CommandKind.STEER_QUEUED_PROMPT`；
- `CommandRequest.target_queue_item_id`。

字段矩阵：

| command | text | target_turn_id | target_queue_item_id | permission |
| --- | --- | --- | --- | --- |
| SUBMIT_PROMPT | 必填 | 禁止 | 禁止 | 必填 |
| 旧 STEER_ACTIVE_TURN | 实施后删除浏览器调用；协议若无其他真实调用方则同次删除 | 必填 | 禁止 | 禁止 |
| CANCEL_QUEUED_PROMPT | 禁止 | 禁止 | 必填 | 禁止 |
| STEER_QUEUED_PROMPT | 禁止 | 必填 | 必填 | 禁止 |

实施前必须搜索所有非浏览器调用方。若 `STEER_ACTIVE_TURN` 仍是 CLI/外部正式协议能力，则保留协议能力但删除 composer 直达入口；不得保留两套浏览器 UX。若没有真实调用方，则在同一 hard cut 删除该 command kind、adapter 方法和测试。不得凭假设选择。

`CommandOutcome.prompt_delivery` 继续作为 action 的 typed 投影：

- cancel 成功指向 source `CANCELLED`；
- steer 改投接纳指向 replacement `PENDING/CONSUMED`，delivery mode 为 steer；
- 被拒绝且 source 未改变时，不伪造 replacement delivery。

协议生成物、wire fixture、browser bridge allowlist、gateway 验证矩阵与 runtime adapter 类型必须同次更新，不提供旧 JSON alias 或 fallback。

## 8. 数据库与事件边界

clean-v0 baseline 只增加 `session_commands.command_kind='STEER_QUEUED_PROMPT'` 及其合法 target 约束；`CANCEL_PROMPT` 已存在则复用。开发阶段按仓库规则更新基线和 expected catalog，不建立仅用于未发布内部状态的在线迁移链。

不新增表、queue relation column、receipt、job、guard、subject slot 或 event kind。source↔replacement 的操作一致性由 action semantic digest、同一事务中的两行状态和既有事件 payload 证明；不得新增永久 redirect registry。

允许既有事件 payload 增加：

- source `PromptCancelled`: `reason=USER_REDIRECTED_TO_STEER`、`redirected_to_queue_item_id`；
- replacement `PromptQueued`: `delivery_mode=STEER_ACTIVE_TURN`、`redirected_from_queue_item_id`。

payload 只用于可核验诊断，不成为执行 authority。执行与查询仍读取 session command 和 queue rows。

## 9. Command/query 语义

### 9.1 取消

| 结果 | status | public_code | source 状态 |
| --- | --- | --- | --- |
| 成功取消 | SUCCEEDED | PROMPT_CANCELLED | CANCELLED |
| 已消费 | REJECTED | PROMPT_ALREADY_CONSUMED | CONSUMED |
| 已被其他操作取消/拒绝 | REJECTED | PROMPT_NOT_PENDING | 原终态 |
| command identity 冲突 | REJECTED | COMMAND_CONFLICT | 不变 |

### 9.2 改投 steer

| 结果 | status | public_code | source | replacement |
| --- | --- | --- | --- | --- |
| 已接纳等待 safe point | PENDING | PROMPT_STEER_QUEUED | CANCELLED/redirected | PENDING steer |
| 已被 safe point 消费 | SUCCEEDED 或按现有运行态查询 | PROMPT_CONSUMED | CANCELLED/redirected | CONSUMED |
| ROOT fence 先赢 | REJECTED | STEER_TARGET_CLOSED | 仍 PENDING | 不存在 |
| source 已消费 | REJECTED | PROMPT_ALREADY_CONSUMED | CONSUMED | 不存在 |
| source 关联 plan handoff | REJECTED | PROMPT_HAS_PLAN_HANDOFF | 仍 PENDING | 不存在 |

原 submission command 在 source 被改投后查询时可以返回 `USER_REDIRECTED_TO_STEER`，UI 必须解释为“已改为引导”，不能显示成输入丢失或普通发送失败。

网络错误不等于拒绝。前端在可用的新 connection owner 上按原 action command 查询；查询前后都校验 session＋connection generation，绝不自动重发 cancel/steer action。

查询为空也不等于拒绝：source 状态校验可能在 action command 插入前拒绝，丢失该 ACK 后没有 action row 可查。此时只允许使用同 session 的 exact source command＋queue item＋NEW_TURN 身份补充只读判断：

- canonical consumed entry，或原 submission command 查询的 `CONSUMED + NEW_TURN`，证明 source 已被 FIFO 消费，该 action 不可能再成功取消或改投。前端据此结束未知展示并解锁 composer，不回填已经消费的正文，不伪造 action receipt。
- source 不可见、query 为空、同文不同身份、仅看见 CANCELLED 都不足以得出上述结论，继续保留未知，不自动重发操作。
- 原 action 查询由单一 reconciliation effect 持有；同一 session/connection/action 不并发查询。查询期间若到达更新 cut，完成后重新检查最新 cut；不依赖模型继续输出才能收敛。

## 10. wait_agent 与 steer

改投得到的 replacement 是普通 exact pending steer，必须复用 Subagent-R1 已激活的行为：

1. `wait_agent` 挂起时收到该 steer，返回 `steer_available`；
2. wait 工具先闭合；
3. 下一 safe point 消费 replacement；
4. 同一 ROOT 发起 successor provider request；
5. child 不因 steer 被取消；若仍需等待，模型自行再次调用 wait。

未点击“发送”的 NEW_TURN 队列不打断 wait。不得因为 composer 中有队列项就把 all/first 改回泛化 `input_available`。

## 11. 前端状态模型

前端按 exact 身份维护四类可丢弃展示：

| 状态 | 位置 | 操作 |
| --- | --- | --- |
| 本地 submit 在途、尚无 queue ID | composer 队列顶部/对应顺序 | 显示“正在加入”，无发送/编辑/删除 |
| canonical PENDING NEW_TURN | composer 上方队列 | 发送、编辑、删除 |
| action 在途 | 原队列行 | 三个操作禁用，保留正文 |
| accepted pending steer | 主时间线蓝色“引导” | 无队列操作；等待 canonical exact 接替 |

terminal local transition 仍按 PR02 typed 状态展示，但不得把已成功 redirected 的 source cancellation 作为错误卡重新插回队列。

乐观 steer 可由 action receipt/replacement queue projection建立，必须携带：session ID、connection generation、action command ID、source queue item ID、replacement queue item ID、target turn ID、原文与提交时间。它不是 Message 真源，不写 localStorage。

以下任一事实到达时移除乐观卡：

- exact consumed `USER_STEER` canonical entry 接替；
- action 明确 rejected/failed；
- owner/session 变化，随后由新 snapshot 重建 pending steer；
- replacement canonical terminal 且没有 consumed entry。

accepted steer 在当前快照中既无 exact pending replacement，也无 exact consumed entry 时，按原 action command 查询最终结果；不以“浏览器曾观察到 replacement PENDING”为前置条件。接纳后立即 STOP／失败的路径也必须移除被拒绝的乐观卡，不能永久停留在“引导”。

pending steer 在 reload/第二窗口中仍由 canonical `prompt_queue` 投影为蓝色“引导”，而不是退回 composer 的 NEW_TURN 列表。observer 可以看见，但没有操作按钮。

## 12. Composer UI/UX

### 12.1 布局

队列放在 `.composer-wrap` 内、`.composer-frame` 上方，视觉上与输入框组成一个整体：

```text
┌────────────────────────────────────────────┐
│ ↳ 第一条正文        ↳ 发送  ✎ 编辑  🗑 删除 │
│ ↳ 第二条正文        ↳ 发送  ✎ 编辑  🗑 删除 │
├────────────────────────────────────────────┤
│ composer                                   │
└────────────────────────────────────────────┘
```

- 不在 transcript 中保留旧的大块“等待处理的输入”区域。
- 队列整体与 composer 同宽，行之间用弱分隔，不为每项创建厚重卡片。
- 正文保留换行但收敛到最多两行预览；完整内容可通过 title/可访问描述读取，编辑后进入完整 textarea。
- 队列显示高度有界，建议 `max-height: min(190px, 28vh)` 并使用内部右侧滚动条；不能把 composer、TODO dock 或“回到最新”推出视口。
- 390px 窄屏允许操作换行成紧凑两列/一行图标按钮；不得水平溢出或隐藏删除/编辑。
- 多项队列时 composer 高度继续按现有 250px 上限增长；queue scroll 与 textarea scroll 独立。

### 12.2 文案和图标

- 发送：`CornerDownRight`（或既有同义 icon）＋“发送”，tooltip“作为引导发送到当前任务”。
- 编辑：`Pencil`＋“编辑”，tooltip“取消排队并放回输入框”。
- 删除：`Trash2`＋“删除”，tooltip“取消这条排队输入”。
- pending steer 时间线继续使用 `CornerDownRight`＋精确“引导”。
- 不使用“调整方向”、三点菜单、“侧边聊天”“关闭排队”。

### 12.3 焦点与键盘

- 所有操作使用真实 `<button type="button">`，有 aria-label、focus-visible 和 disabled/busy 状态。
- 编辑成功后焦点进入 textarea，光标置于末尾。
- 删除后焦点移到下一队列项的同类按钮；没有下一项则回 composer。
- action 在途时不能通过重复 Enter/双击创建第二 action。
- 输入法 composition 保护和 Shift+Enter 换行保持。

## 13. 权限、Hook 与计划边界

- NEW_TURN 入队时仍按现有规则冻结 model binding、permission snapshot 和 UserPromptSubmit Hook 结果。
- 点击“发送”把正文变为当前 turn steer，不把原 NEW_TURN 权限覆盖到运行中的 ROOT；steer 继承 current turn 的既有权限边界。
- 编辑后若重新提交，按当时 composer 中明确显示的权限创建新的 command；旧 permission snapshot 只保留为已取消 source 的历史事实。
- 改投不重新执行 NEW_TURN Hook，也不绕过当前 steer 的现有 safe-point/plan-conflict 检查。
- 带 plan handoff 的 queue item 本轮拒绝改投，不隐式取消、批准或迁移规划。
- observer、旧 connection、跨 session 或过期 owner 不获得 cancel/steer 权限。

## 14. provider-prefix continuity

本功能不改变 SYSTEM、tools、既有 messages 或 compaction policy：

- 同一 epoch 内，source NEW_TURN 被取消不会从已安装 provider prefix 删除内容，因为它尚未进入 prefix；
- replacement steer 只在既有合法 safe point 作为 suffix 追加；
- assistant terminal-looking message 后的 successor round 复用相同 context binding，并追加 steer suffix；
- 不因 UI 乐观卡、reload、action query 或 capability refresh 重建 provider input；
- compaction successor 仍是唯一已批准的非 cold root 重建边界。

continuity 测试必须逐字验证 SYSTEM/tools 不变、旧 messages 不改写、steer 只追加一次。

## 15. 删除的旧路径

实施时同次删除：

1. 运行中 composer 的“⌘/Ctrl+Enter 引导当前任务”提示和快捷路径；
2. 浏览器从 composer 草稿直接调用 `steerActiveTurn(text)` 的路径；
3. transcript 尾部旧 `.prompt-queue` 大卡区域；
4. 仅显示 queuedCount 的紫色单行占位（由真实 composer queue 取代）；
5. 把 pending steer 继续显示成“等待处理输入”的路径；
6. 任何 cancel→steer 或 steer→cancel 的前端双请求实现；
7. 按正文消除乐观 steer 的路径；
8. 若搜索证明无正式调用方，删除旧 `STEER_ACTIVE_TURN` 协议/adapter/browser command；否则记录保留调用方并仅删除浏览器 composer 入口。
9. 删除 queue action 对 `observedPending` 的依赖，以及 reconnect 分支内与 reconciliation effect 重复查询 action 的路径。PR02 普通 submission 的独立状态机不在本项删除范围内。

不保留 feature flag、兼容 UI、旧按钮隐藏样式或双提交 fallback。

## 16. Phase A：先复现

先写失败测试，不修改断言来适配现状：

### 16.1 Kernel/数据库红灯

- 证明现有前端若 cancel 后 steer，在 ROOT 结束竞态中会丢失；steer 后 cancel 可重复。
- source NEW_TURN、target RUNNING ROOT、改投和 settlement 两种顺序。
- cancel 与 FIFO consumption、两次 cancel、cancel 与 redirect 并发。
- >64 KiB blob source 改投后正文 exact，不经过浏览器重传。
- plan-handoff source 拒绝且完全不变。
- action ACK 丢失后 query 原 action；相同 command 冲突。
- redirect 后 `wait_agent` 精确返回 `steer_available`，未操作 NEW_TURN 不打断。

### 16.2 前端红灯

- 运行中 Enter 只入 NEW_TURN 队列，Meta/Ctrl+Enter 不再绕过。
- canonical queue 位于 composer，三项平铺操作及 icon/aria 完整。
- send 接纳后立即出现单张“引导”，不显示“模型已收到”。
- canonical steer 到达后 exact 接替，无重复/闪回。
- edit 在空草稿成功回填正文与权限；非空草稿不取消 source。
- delete 成功移除；失败保持。
- observer/reload/跨 session late ACK 不获得操作或串状态。
- 多条同文、乱序 action、loop 在点击时结束。
- 拒绝 ACK 丢失且 action query 为空：exact source 被 FIFO 消费后解锁；同文其他 source 不解锁；consumed entry 不在可见历史时查询原 submission。
- 编辑期间已打开的技能菜单不能写入草稿；意外存在另一份草稿时两份正文保留且 composer 可继续操作。
- replacement 未被任何 PENDING snapshot 观察到就拒绝，仍按原 action 查询移除乐观卡；查询期间的新 cut 不丢失，也不启动并发查询。

## 17. Phase B：Kernel 与协议

1. 冻结 action request/semantic digest 和 repository candidate。
2. 实现 cancel command 的 durable idempotency和 query。
3. 实现 source cancel＋replacement steer enqueue 的单事务改投。
4. 将改投与 ROOT completion fence 接到同一 canonical winner。
5. 复用 queue wake、root input activity、safe-point steer consumption。
6. 更新 clean-v0 constraint、protocol schema/generated fixture、gateway/bridge。
7. 证明没有新增表、event kind、subject、job、receipt 或 fingerprint。

## 18. Phase C：前端 hard cut

1. runtime adapter 暴露 `cancelQueuedPrompt`、`steerQueuedPrompt`；所有操作绑定 session＋connection owner＋action command。
2. app 按原 action query 处理网络未知，不重发。
3. Workbench 将 NEW_TURN queue 移入 composer；pending steer 移入主时间线。
4. 加三项 action、busy/focus/草稿保护和窄屏样式。
5. 删除 composer 直接 steer 与旧 queue presentation。
6. 保持 TODO dock、“回到最新”和 textarea 动态高度随 composer queue 重算。

## 19. Phase D：验证

### 19.1 Focused checks

使用仓库根目录 `uv`/`.venv`：

- repository prompt/queue PostgreSQL focused；
- Host queue/steer/runner settlement/wait focused；
- terminal protocol/gateway/browser bridge/generation check；
- runtime adapter/app/workbench focused；
- provider-input prefix continuity、compaction successor、permission/Hook/plan相邻测试；
- `npm run lint`、TypeScript noEmit、`npm test`、`npm run build:local`；
- `uv run ruff check ...`、`git diff --check`。

不得 skip/xfail、弱化 exact identity 断言或把旧截图当新证据。

### 19.2 完整回归

完成 Python 全量与前端全量。若 fixture 因新增 protocol command/clean-v0 constraint 失配，只能按新真相更新；重大架构或数据库冲突先报告。

### 19.3 Isolated wheel/launcher

构建最终 wheel，安装到临时 venv，从非源码 cwd 启动。核对 import/static 均来自 wheel，最终 bundle 包含新文案和 action，旧 composer steer 提示与旧 queue 区域不存在。

### 19.4 真实 provider/browser dogfood

用 `LocalSettingsStore`＋`require_pulsara_home()` 只读加载保存配置，OpenRouter 优先；凭据不输出。至少覆盖：

1. 一个真实长 loop/wait 中连续输入四条，均按 FIFO 出现在 composer queue；
2. 选择中间一条“发送”，立即显示蓝色“引导”，wait 被 steer 唤醒，同一 `turnId` 产生 successor provider round；
3. assistant final text 已显示但 settlement 未完成时发送，引导仍由同一 turn 回答；
4. 人为让 settlement 先赢，source 留在 NEW_TURN 并自动进入下一 ROOT；
5. 编辑一项、删除一项，剩余未操作项在 loop 结束后逐条自动处理；
6. 两条同文、>64 KiB 正文、reload、第二 observer 窗口、网络 ACK 丢失查询；
7. 390×844 与桌面宽度，多条队列内部滚动、textarea 250px、TODO 和回到最新均可用；
8. 控制台、DOM、command/source/replacement/turn/entry ID 与时间顺序均保存为证据。

真实 provider 请求计数必须证明：乐观 UI 不触发额外模型请求；只有 accepted steer 的 successor round 或 FIFO 新 ROOT 才调用 provider。

## 20. Acceptance criteria

只有全部满足才标记 ACTIVATED：

- [x] 运行中 composer 输入只进入 canonical NEW_TURN queue；旧直达 steer UX 已删除。
- [x] composer queue 展示 exact 正文和顺序，每项只有带 icon 的发送/编辑/删除。
- [x] edit/delete 使用 kernel exact CAS；无浏览器假删除、无草稿覆盖。
- [x] send 使用单事务 source→replacement steer 改投；不存在前端 cancel＋send 双请求。
- [x] 改投与 ROOT settlement 有唯一赢家；accepted steer 必有同 turn successor handling，失败时 source 不丢失。
- [x] wait_agent 被 replacement steer 精确唤醒，NEW_TURN 仍不打断。
- [x] accepted 后立即显示单张蓝色“引导”；canonical exact 接替，无“模型已读”伪承诺。
- [x] 未操作队列在 loop 结束后由现有 FIFO owner 逐条进入新 ROOT；浏览器不重发。
- [x] 权限、Hook、plan handoff、STOP/cancellation/effect settlement 与 observer owner 保持。
- [x] SYSTEM/tools byte-identical、messages suffix-only、compaction continuity 通过。
- [x] 无新表、receipt、scheduler、durable delivery queue、event kind、subject slot 或 fingerprint。
- [ ] 修订后的 focused/full regression、protocol generation、lint/typecheck/build、isolated wheel/launcher 全部通过；原验收不能代替修订后安装产物验收。
- [ ] 修订后的真实 provider/browser 覆盖 final-message/steer race、编辑、删除、自动 FIFO、reload/observer 和窄屏。
- [x] 规格、索引、证据与最终 bundle 同步；Git 状态如实记录，未自动 stage/commit。

## 21. 剩余边界

- 本 PR 不展示 subagent/provider-included receipt，也不新增通用“模型已读”状态。
- 本 PR 不支持队列拖动、批量删除、批量改投、关闭排队或侧边聊天。
- 本 PR 不允许改投带 plan handoff 的输入；未来若要支持，需单独冻结 workflow 转移/取消语义。
- 本 PR 不改变外部 CLI 是否保留 direct steer；实施阶段先以真实调用方搜索裁定，但浏览器 composer 必须只有队列单一路径。
- pending/optimistic UI 在进程崩溃后可丢失；canonical queue/action/entry 仍可通过 reload 重建，不为视觉连续性新增 durable receipt。

最终产品不变量是：**用户可以先排队、再选择任意普通待处理输入引导当前任务；界面立即确认用户动作，kernel 精确保证消息只进入当前 turn 或未来 FIFO 其中之一，绝不丢失、重复或伪称模型已读。**


## 22. 原实施与 activation 记录（2026-09-12；第 23 节修订之前）

Phase A 先保留双请求丢失/重复的确定性复现和新 queue/composer 红灯；Phase B–C 实现单事务 exact queue action、严格协议字段、composer queue 和即时蓝色引导，并删除旧浏览器直达 steer。非浏览器调用方搜索未发现正式 V3 direct-steer caller，故删除该协议 command 并 reserve 旧编号；内部 Host steer / queue delivery mode 继续由既有 kernel owner 使用，不构成协议兼容分支。

浏览器验收另复现了 reload 初始 live-control ROOT 指针在后续 ROOT 残留的问题，先补 exact 失败断言，再使 queue action 唯一从 CURRENT_CONTROL 的 RUNNING ROOT 冻结目标；不 fallback 到旧 live 指针，不改变 Host closing/权限/取消/effect settlement 语义。

最终 Python full **1758 passed**、explicit focused **293 passed**、前端 full **214 passed**；protocol generation、ruff、lint、TypeScript、build 和 diff check 全过。最终 wheel 从非源码 cwd 启动，import/static 均来自临时 venv；最终 bundle 为 `index-9FLB7y9d.js` / `index-Cryz142G.css`。

保存配置驱动的 OpenRouter `openai/gpt-5.6-luna` dogfood 共 **41 次真实 provider 调用**（独立 probe 14、浏览器 27）。覆盖 wait exact wake 与 child continuation、四项 FIFO、edit/delete、final-visible 同 turn successor、completion-first source 保留后自动 FIFO、同文、大 blob、lost ACK 原 command query、reload/observer、390×844 与桌面布局。SYSTEM/tools exact 与 messages suffix continuity 在自动化和真实输入中均验证；无新增未授权 durability/receipt/fingerprint/scheduler。

完整命令、red/green 日志、可见截图、实际 command/source/replacement/turn/entry ID、调用计数、原始 wire/canonical 证据、边界与 Git 状态见 [activation evidence](output/playwright/pr04-dogfood/activation-evidence.md) 和 [verified evidence](output/playwright/pr04-dogfood/verified-evidence.json)。没有 stage/commit；本次 activation 不改变 PR01/PR03 或其他批次的独立状态。

## 23. 审查后修正（2026-09-12）

本轮直接在既有 dirty 工作树实施三个 PR04 finding，并联修 Subagent-R1 的来源关系投影。没有覆盖、清理或代为提交既有改动。

- **原操作无 durable row 的未知结果**：先查询原 action，缺少 receipt 时用 exact source 的已消费事实结束不可能再成功的操作。原文已用于新 ROOT 时不恢复到草稿；未知事实保持未知。补充了来源 entry 不在可见历史、只能查询原 submission 的路径。
- **编辑占用草稿**：textarea、技能触发器和已打开菜单统一服从编辑保留状态。意外存在其他草稿时展示待恢复原文，允许处理草稿后恢复；重连不删除尚未回填的 accepted edit。
- **快速拒绝的 steer**：删除 action 的 `observedPending` 门槛；replacement 没有进入任一 PENDING snapshot 也查询真实终态并撤去乐观卡。删除重连分支内的第二查询者，同一 action 查询串行；查询期间的新 cut 在结束后继续检查。

三个原始 PR04 回归加 Subagent-R1 回归先确认失败，再修复生产路径；另补草稿冲突、不可见 consumed source 和查询中到达新 cut 的测试。没有降低原断言或增加 skip/xfail。一个旧来源文案 fixture 补齐 canonical ROOT 顺序和 task parent 身份；query fixture 按实际 command 区分 source cancellation 和 replacement rejection。

本轮验证命令与结果（frontend 命令 cwd 为 `frontend/`；协议命令 cwd 为仓库根目录）：

| 命令 | 结果 |
| --- | --- |
| `npm test -- app/pulsara-app.test.tsx lib/runtime-adapter.test.ts components/workbench-view.test.tsx` | 154 passed |
| `npm test` | 221 passed，15 个文件 |
| `npm run lint` | 通过 |
| `./node_modules/.bin/tsc --noEmit --incremental false` | 通过 |
| `npm run build:local` | 通过；入口 `index-DkOQlHP2.js` / `index-Cryz142G.css` |
| `uv run python tools/generate_terminal_protocol_contract.py --check` | 通过 |
| `git diff --check` | 通过 |

构建仍有既有的 500 kB chunk 提示；Vitest 启动仍有 Node `--localstorage-file` 提示。旧生成的 JS/map 已由标准 build 替换，CSS 内容未变。

本轮不修改 kernel、协议、schema、权限、effect settlement、provider prefix、保存配置或数据库，不新增持久状态、receipt 或 fingerprint。Python full、continuity、isolated wheel/launcher 和真实 provider/browser 未重新执行；第 22 节证据保留为历史，不计为修订后 activation。当前无 stage/commit，需完成第 20 节未勾选门槛后才能重新 ACTIVATED。
