# Pulsara PR05：同 Host 工具确认保留与重连 Hard Cut 实施规格

状态：**ACTIVATED（2026-09-12 reviewer 修正及第 20 节重新验收完成，未提交）**。

日期：2026-09-12。源码核对基线：`0828161f`；首次编写前工作树干净。规格阶段根据 reviewer 意见补齐提交确认、接管撤权、延期失效收尾与 MCP 发布中保留；随后完成 Phase A–D，并在后续三项审查修正后重新验收。

索引：[浏览器 dogfood 复盘与修复方案](PULSARA_BROWSER_DOGFOOD_REVIEW_AND_FIX_PLAN_2026-09-08.zh.md)。

建议提交主题：`feat: retain tool confirmations across same-host reconnects`。

本文冻结 M02 的新产品语义，取代索引第 7 节对普通工具确认的“detach 即结束、离开前警告”旧方案。初次 Phase A–D 之后 reviewer 发现真实工具等待者取消的 MCP permit 遗留、确证未提交未传达给前端，以及首条未知意图阻塞后续查询三项缺口，曾撤回 ACTIVATED。三项修正和第 20 节重验现已完成，见[本次重新验收记录](output/playwright/pr05-review-fixes/README.zh.md)。[初次验收记录](output/playwright/pr05-confirmation-retention/README.zh.md) 保留为历史证据，不代替本次重验。

## 1. 产品目标

**待确认的工具操作属于仍在运行的会话，不属于正在查看它的浏览器窗口。离开不等于拒绝。**

业务场景：用户要求助手修复项目，助手运行到一个需要人工授权的操作。用户暂时切到另一会话，之后返回；只要原 Host 和执行 owner 仍存活、确认没有失效，就能看到并批准原来那一次操作，不需要重新发任务，也不会重复执行。

同一规则覆盖“离开前已经出现确认”和“离开后后台才运行到需要确认的位置”。不能只保留已经弹出的确认，却将后者直接拒绝。

这是**同 Host 内的执行保留**，不是跨重启审批恢复。确认仍在内存中；没有浏览器连接时不能自动批准，但可以继续等待。

## 2. 范围与明确非目标

### 2.1 本 PR 必须实现

1. 普通 `TOOL_CONFIRMATION` 在 controller detach 后保留。
2. 存活的交互 owner 在没有 controller 时，也可接纳新的普通确认候选。
3. 同 Host 的切会话、刷新、断线重连和 controller 接管可重新展示仍有效的原确认。
4. 普通确认的期限、STOP、子任务取消、能力失效、正常关闭、崩溃与未知批准结果有明确收尾。
5. 决策始终绑定 exact 原工具调用；旧窗口和 observer 不获得授权能力。
6. 前端区分等待、决定提交中、决定已接纳、操作执行结果与未知事实。
7. 保留 existing permission、Hook、MCP admission、effect settlement 和 provider-prefix continuity。

### 2.2 不做

- 不持久化 pending confirmation、future、表单草稿或待执行闭包；不新增审批表、receipt、scheduler、durable delivery queue、job、fingerprint 或恢复 registry。
- 不在 Host 重启后重建原工具等待，不自动重放原调用，不将旧授权迁移到新工具调用。
- 不新增审批中心、确认历史版本页、跨会话批量批准或后台浏览器连接池。
- 不修改 plan question/draft 的既有独立生命周期；不建设 F04 的完整历史规划视图。
- 不调整 M04 observation scope，不增加 per-model seen 证明。
- 不将普通工具确认超时解释为停止整个 ROOT；不改变独立 NEW_TURN FIFO、子任务或 terminal monitor 的唤醒策略。
- 不为了 UI 保留而承诺无限等待、延长所有 watchdog 或扩大现有并发／资源容量。

## 3. 实施前源码事实与 hard cut 落点

以下是编写时直接核对的事实，不是已完成验收的声明。

| 真源 | 实施前事实 | PR05 的改动边界 |
| --- | --- | --- |
| [interaction.py](src/pulsara_agent/conversation_kernel/interaction.py) 的 `_PendingToolInteraction`、`_request` | 候选、future、exact turn/entry/tool-call 身份已在 Host 内存；无 controller 时直接 DENY | 复用原对象与队列，普通确认允许无 controller 入队 |
| 同文件 `controller_detached`、`_abort_current_if_no_controller` | detach 会调用 `_abort_all` | 普通确认不因 detach 终止；保留真实执行失效路径 |
| 同文件 `_promote_next` | 单个可见槽位＋FIFO dormant；依赖 controller，并有发布中的二次 owner 检查 | controller 缺席暂停新候选发布，不销毁它；重新 attach 后由同一 owner 推进 |
| 同文件 `resolve_tool_interaction` | 校验 writer、live epoch/revision、interaction，保存 `actor_id`；没有联合校验它此刻仍是 current controller | 在 coordinator 接纳决定的临界区加入真实 controller 检查，不只信 gateway 旧 role |
| 同文件 `_request`、`_abort_candidate`、`_abort_all` | promotion 在 timeout/取消保护之前；提交中到期可先返回，提交异常随后仅清除 resolving | 保护范围覆盖入队后的 promotion；确认提交结果后重新裁定原期限／失效，不能遗留 future |
| [interaction_arbiter.py](src/pulsara_agent/conversation_kernel/interaction_arbiter.py)、[tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py) | 已有 64 个候选的资源边界、MCP before_publish/discard permit | 复用，不增总任务上限，不在断线时重复获取／释放 permit |
| [tools.py](src/pulsara_agent/conversation_kernel/_repository/tools.py) 的 `accept_tool_interaction_decision` | ALLOW 与 exact attempt 同事务；DENY 与 no-attempt result 同事务；pending 不落库 | 原事务保留，离线等待不新增记录 |
| 同文件的决定写入幂等分支；[io.py](src/pulsara_agent/conversation_kernel/io.py) 的 `run` | 完整决定读取嵌在写入方法中；I/O 取消／超时会等待物理任务退出后抛出，操作可能已提交；coordinator 异常分支直接清除 resolving | 提取唯一只读 exact-confirm，原 coordinator 在异常后消费完整事实；不能把异常当未提交 |
| [host.py](src/pulsara_agent/conversation_kernel/host.py) 的 `query_command` | 可查询已保存的 `INTERACTION_ALLOW`／`INTERACTION_DENY`，但不是恢复工具 future 所需的完整 attempt/result | 浏览器沿用公开查询；内核使用第 9 节的完整只读确认，两者不混用 |
| [browser_bridge.py](src/pulsara_agent/web_app/browser_bridge.py)、[pulsara-app.tsx](frontend/app/pulsara-app.tsx) | 切换会关闭旧 connection；bridge 的 disconnect 不等于关闭整个 Host | 保留连接释放；不靠偷偷保持旧连接实现本功能 |
| [v3_gateway.py](src/pulsara_agent/terminal_protocol/v3_gateway.py) 的 `_accept`；[protocol_client.py](src/pulsara_agent/web_app/protocol_client.py) 的 `_close_owner` | gateway 串行 await 请求；忙时 client 物理关 socket，controller 释放可迟到至 gateway finally；bridge 遇到 CONTROLLER_UNAVAILABLE 会退为 observer | 合法接管／disconnect 经原 attachment owner 先精确撤权，不依赖旧请求完成 |
| [live_control.py](src/pulsara_agent/conversation_kernel/live_control.py)、[v3_gateway.py](src/pulsara_agent/terminal_protocol/v3_gateway.py) | 已有 snapshot-and-subscribe、interaction view、epoch/revision 和替换事件 | 复用恢复／GAP 机制，窄补决定进行中投影 |
| [kernel.py](src/pulsara_agent/conversation_kernel/_repository/kernel.py) 的 `_interrupt_prior_generation` | 新 writer 接管时标记旧 RUNNING turn／未结束子任务中断 | 不恢复 pending，不自动重新执行旧工具 |

关键判断：生命周期对象已经在正确的 Host 层；需要 hard-cut 的是“连接缺席等于取消”的策略及相关入口，不是再造 scheduler。

### 3.1 审查证据与工作量判断

四项修订都有源码依据：普通决定缺少异常后的完整只读确认；客户端关闭不能证明 controller 已释放；提交中到期后再失败可能失去收尾者；MCP 的 before_publish 会消费 admission，不能重跑。

reviewer 提供的 coordinator/I/O 探针使用 repository 测试替身；Unix 接管探针使用真实协议与 socket，但 Host 适配和 repository 仍是替身。探针结果支持这些时序判断，不能证明真实数据库的 commit／rollback，也不是 provider/browser activation。本轮独立核对源码并阅读探针脚本和结果，未重新执行这些探针；reviewer 报告的 `20 passed` 也不计作本轮新验收。

因此这是一次有明确范围的生命周期／结算 hard cut，不能估算成只删除 detach 取消判断。以下决定把缺口收敛到原 owner，不授权通用审批恢复框架。

## 4. 冻结生命周期合同

### 4.1 普通工具确认

| 事件 | 唯一行为 |
| --- | --- |
| 已出现确认，用户切走 | 保留原 interaction、future、工具身份和原 deadline；不产生 DENY |
| 用户离开后才产生确认 | 接纳到原 dormant FIFO，等待 controller；不得因连接缺席拒绝 |
| 同 Host 重新 attach controller | 恢复已有 visible 确认；若没有 visible，推进仍有效的 FIFO 头 |
| observer attach／查看 | 只读既有允许公开的摘要，不触发批准，也不冒充 controller 来发布授权 |
| 当前 controller 转移到另一个窗口 | 待确认对象不迁移、不复制；只更换谁有权提交新决定 |
| 普通确认到期且无决定在提交 | 结束该候选，释放其资源，返回真实未授权结果；不冒充用户点击拒绝 |
| exact ROOT STOP／所属 child 被取消 | 由原执行取消路径结束对应候选；不清掉不相关任务的确认 |
| 原工具／MCP generation 被合法撤销 | 原 admission owner 失效并取消对应候选，不改绑新版本工具 |
| Host 正常关闭 | 停止接纳，取消／结算原运行与确认；不留下可再次批准的旧按钮 |
| Host 崩溃、强杀或停电 | 内存确认消失；后续按新 Host 接管和 canonical 真相处理，不 replay |

“没有 controller”不等于“没有交互 owner”。`tool_runtime._interaction is None`、Host 已关闭等真实 owner 缺失仍必须失败，不能为了允许离线等待而制造孤立 future。

### 4.2 三类交互不强行统一

| 类别 | 切换／detach 后 | 重启后 |
| --- | --- | --- |
| 普通 TOOL_CONFIRMATION（含正常 MCP 工具授权） | 本 PR 保留，前提是原执行仍有效 | 不恢复原等待 |
| CAPABILITY_FORM（配置／敏感输入） | **本 PR 保留现有 detach 取消语义**；未提交草稿不迁移，继续需重新发起 | 不恢复表单草稿 |
| plan QUESTION／DRAFT_REVIEW | 原 owner／canonical 合同不变；不要统称“所有规划交互都可恢复” | 按现有分别规定的恢复／中断规则 |

共享 coordinator 不构成同时保留所有类别的理由。对能力表单应使用明确种类判断，不新增通用可配置 retention policy、feature flag 或兼容分支。其解析／提交与 detach 的既有竞争纪律保留，不能取消已经获准开始的独立效果。

## 5. 最小 owner 与状态模型

### 5.1 唯一 owner

- `KernelInteractionCoordinator`：候选 FIFO、current slot、future、期限、决定接纳、异常后的只读确认及失效收尾。
- Host／runner／subagent：原运行是否存活、取消和工具调用结算；交互等待是运行中的一部分，不创建新的 ROOT。
- controller attachment：此刻谁有权提交决定；不拥有候选的生命期。
- repository：已接纳决定、attempt/result 和既有 command 幂等事实。
- live-control／live-bus：当前快照和可丢弃观察，不成为审批真源。
- browser：当前展示及本窗口提交意图；不持有执行权威，不把 localStorage 当恢复源。

### 5.2 不把四种事实合成一个状态

```text
原运行：RUNNING（可派生“等待确认”）→ TERMINAL

确认候选：DORMANT → VISIBLE → RESOLVING → CLOSED
             └────────────→ CLOSED（到期／取消／失效）

控制连接：ATTACHED / ABSENT（独立于候选是否存在）

已接纳决定：ALLOW / DENY（数据库事实，非执行成功证明）
```

这是对原字段和 owner 行为的说明，不要求新增四套 enum／reducer。RESOLVING 包括正在写入和正在核实原写入结果；只有确证未提交且确认仍有效时才可回到 VISIBLE。提交是否已发生尚不确定时不能先回到可再次批准状态。

从 FIFO 取得 head 到发布成功之间，是原 candidate 的 promotion 子阶段：它已占 `_pending`，但 `visible=False`。它不是第二条队列，也不等于 controller 正在显示此操作；具体保留规则见第 6.3 节。

DORMANT 尚未取得可见槽，不等于工具已获准；VISIBLE 不等于用户已看过；RESOLVING 不等于数据库已提交；ALLOW 不等于工具已执行完成。

## 6. 无 controller 入队与重新展示

### 6.1 入队和断线

1. 普通确认通过原执行／permission 检查后，在 coordinator 锁内建立一个候选；原 task/turn/entry/tool_call 身份不变。
2. 无 controller 时保留 dormant，不执行工具、不获取新的可见确认 permit、不写数据库 pending 行。
3. 已经 visible 的普通确认在 detach 后仍占原 slot，其原 permit 由原 owner 保留，直到决定或真实失效；不能用 detach 作为 discard。
4. detach 只清除匹配的 controller attachment；对能力表单执行既有有界取消。旧 attachment 的迟到 detach 不能清除新 controller，也不能影响普通候选。
5. `_promote_next` 在发布前／发布中看到 controller 缺席时保留候选，不能在二次检查中将其变成 `interaction:owner-unavailable`。真实 Host／capability owner 失效与连接缺席必须分开判断。

### 6.2 attach 接入

重新取得 controller 后，Host 必须让原 coordinator 推进 FIFO。不能只更改 `_controller_id` 而把没有发布者的 dormant 永久留下。

实现采用一个由 Host 生命周期管理、可等待完成的 attach→promote 路径，gateway HELLO／其他真实 controller 调用方统一接入。不得在同步 attach 中散落未追踪的 `create_task`，也不得新增轮询扫描器。初始快照与随后的 subscribe 必须通过原 snapshot-and-subscribe 机制覆盖 promote，不能在两个读取之间漏掉新确认。

已有 visible 不重新创建 interaction ID、不复制 future、不重排 FIFO。重新订阅可以取得更新 revision，但不会续期。snapshot/GAP 后按当前 Host 的真实 slot 重建视图，不以浏览器记忆强行复活旧 ID。

### 6.3 MCP 已取得 permit、尚未发布

冻结为**原 FIFO head 保留，重连只续接未完成的发布**：

1. coordinator 取得 head 后保留该 candidate 在原 `_pending` 槽，其他 promotion 调用不能再取得它或越过它。
2. 在同一 candidate 内使用一个窄的 `admission_completed` 事实记录 before_publish 已成功返回；没有 hook 的候选同样完成该步骤。不复制 permit 对象或 permit registry，物理许可仍由原 tool runtime 持有。
3. before_publish 对此候选只调用一次。成功后 controller 消失，保留 head、原 permit、原身份和期限，`visible=False`；不放回 dormant，不 discard，不重新执行原 admission。
4. 重新 attach 发现这个尚未发布的 head 时，先核对原执行／期限／capability 有效性，再发布原 view；不能因 `_pending is not None` 就直接退出而永远不发布。
5. 真实到期、取消、能力失效或 Host close 仍清理这个阶段。若 hook 已产生资源后失败，仍经原幂等 discard 清理；迟到 hook／promotion 不得重新安装已关闭 candidate。
6. 一次 promotion 只拥有一个 head；并发 attach/promote 不得重复执行 hook。所需子阶段事实仅存在于原 candidate，不新增发布队列、worker、通用 retention 状态机或另一份许可真源。

## 7. 超时与资源边界

第一版明确保留现有 `INTERACTION_TIMEOUT_SECONDS = 10 * 60`，不提供新增用户配置、不延长为永久审批。

保留的是 10 分钟这个既有上限；**统一从候选入队计时**是本 PR 明确的计时语义修订。当前 `_request` 在 `_promote_next` 返回后才进入 timeout，而展示截止时间另行计算，不能宣称现有代码已经具有统一起算点。

- 普通候选成功进入 coordinator 队列时，冻结**一次**执行期限；dormant、controller 缺席和等待展示均计入这 10 分钟。
- 入队后的初次 promotion、重连续接发布、等待 future 都受这个期限及原取消清理保护；不能仍把首次 `await _promote_next()` 放在 timeout／取消保护之外。超时或取消发生于 hook／发布交界时，也必须清理确切 candidate 和原许可。
- 使用既有单调时钟／timeout machinery 驱动执行判断；同一候选保存对应 UTC 截止时间供 UI 展示。
- `_promote_next`、重新 attach、reload、takeover、获取新 live revision 都不重新计算 10 分钟。
- `expires_at_utc` 表示同一候选期限，不采用“展示时 now + 10 分钟”的另一套时钟。UI 倒计时不是执行权威，浏览器时钟不能宣布后台已完成取消。
- 到期时无决定提交：关闭候选、丢弃未获准 permit、唤醒原工具 future；由既有工具结果／结算路径记录未授权原因。不是生成一条 HUMAN DENY 决策，也不是自动 STOP 整个 ROOT。
- 到期时决定正在提交：按第 9 节确认原事务结局，不回滚或覆盖可能存在的 winner；也不无界延长提交等待。继续服从现有 canonical I/O、执行与 Host close watchdog。
- 10 分钟不是执行存活保证。原运行先被取消、失败或到达既有执行期限时，确认随原 owner 结束；不得为凑满确认期限延长 ROOT／工具 watchdog。
- 64 个候选沿用原 arbiter 的物理资源边界，包含 current 与 dormant；不增加新的总任务数、总轮数或会话寿命限制。
- MCP permit 只按原 FIFO head/admission 规则取得，且 discard 幂等。普通候选保留不会使所有 dormant 同时获取 permit。
- 真正执行 `request_confirmation()` 的工具等待者被取消时，必须先让原 candidate 的写入／只读核实闭合，再退出等待并释放不再执行的原 permit；重复取消和到期后的结算等待也不得截断此交接。已提交 ALLOW 保持不变，取消不生成替代 DENY。socket 提交等待者的取消仍独立，不得释放仍将执行的工具许可。

本 PR 的承诺是“未过期且仍有效时可继续批准”，不是“回来多晚都行”。调整超时或暂停离线计时必须另修产品合同。

UI 必须展示实际剩余时间或原截止时间。排队 9 分钟后才展示的确认约剩 1 分钟，不能显示“还有 10 分钟”；浏览器倒计时到零只禁用过期操作，不凭本地时钟伪造后端取消已经完成。

## 8. 决策身份、授权与接管

### 8.1 复用唯一请求

继续使用 `ResolveInteractionRequest` 和原 ALLOW／DENY；不新增另一套浏览器批准 endpoint。

请求复用：`command_id`、`interaction_id`、`expected_writer_generation`、`expected_owner_epoch`、`expected_live_revision`、decision，以及 transport 当前 attachment 的身份。session／Host 由已验证连接绑定，不能让正文指定另一个目标。

UI 在点击时冻结 exact 确认与决定，不按 tool name／prompt 文本找到“看起来同一个”的新操作，也不把 A 的按钮改投 B。

### 8.2 authority 必须在 owner 中闭合

gateway 的 `granted_role == CONTROLLER` 只是第一层过滤。coordinator 必须在决定接纳的同一临界区联合验证：

1. Host 未 closing/closed，原执行／候选有效；
2. `actor_id` 对应当前 coordinator 的真实 controller，observer 和旧 controller 不可提交；
3. writer／owner epoch／live revision／interaction ID 精确匹配；
4. 候选未过期、未关闭、未有另一决定在提交，原 capability admission 未失效；
5. 验证成功后才冻结该次决定并设置 resolving，随后将 I/O 交给既有 owner。

校验和取得 resolving 槽之间不得留下 detach／takeover 可插入的无保护窗口。不得持有 controller 的 threading lock 跨 await 或数据库 I/O。

若旧 controller 在被替换**之前**已合法进入决定提交，之后 detach 不追溯取消其授权；原事务按 exact 结果结算。若 controller 已被替换，旧窗口的新决定必须拒绝。不是“最后到的点击覆盖前一个”。

同一确认最多一个实际决定／attempt。请求绑定原参数、permission snapshot 和工具版本；批准不是重新生成参数、换工具或扩大权限的许可。等待期间文件或 capability 变化，由原 content_revision、permission/admission 和执行检查拒绝／处理，不能因用户回来批准而绕过。

### 8.3 撤权先于 socket 清理，已接纳决定继续结算

`client.aclose()` 只证明客户端完成自己的关闭，不能证明 server 已执行 finally。普通 disconnect、同浏览器替换和已获准 takeover 必须经现有 Host attachment owner 完成 **compare-and-detach 原 attachment**，再将新连接交给同一 attach→promote 入口。

- browser bridge 从自己持有的 connection 取得原 session、host_session、attachment ID／generation；经现有 session／protocol server 所解析的原 Host 做窄适配。不得接受用户随意指定“撤掉任意 controller”，不得直接修改 coordinator 私有字段，不建新 attachment registry。
- 撤权在原 controller authority 的短临界区完成，不等待旧 socket 当前数据库请求结束。此前合法进入 resolving 的决定由原 coordinator 继续写入／核实／结算，不迁移给新窗口，也不因撤权重写成 DENY。
- 新 controller 能取得权限并订阅原候选的进行中状态；不能以 busy 决定为由持续占用旧 controller 权限。能力表单仍按原秘密／取消合同收尾；其清理等待不延迟撤权事实生效。
- 旧连接 finally／迟到 DETACH 再次释放同一 attachment 时必须幂等，只匹配旧对象，不能清除新 controller。不要先关闭 client 清掉本地 attachment ID，再尝试从已清空字段猜撤权目标。
- compare 发现 owner 已是另一 attachment 时，不能重取“当前 controller”并无条件撤销。真实竞争按原 controller 不可用／observer 规则反馈；**已授权接管不能仅因旧请求尚未退出而静默降级 observer，并把它当接管成功**。
- 提前撤权扩大了“旧 transport 尚在但无控制权”的可见窗口。因此所有仍依赖 controller 的协议入口都必须拒绝撤权之后新接纳的旧 attachment 请求，而不是只检查连接缓存的 role；普通决定还须在第 8.2 节 coordinator admission 再次联合验证。既有只读 observer entitlement 不扩大。撤权前已合法接纳的其他操作保持原结算合同，不追溯取消。

这是原 attachment 生命周期适配，不新增公网接管接口、协议 request kind 或通用控制 job，也不要求把整个串行协议改写成通用并发请求框架。普通 Unix 断线最终仍由 gateway 清理；已获准接管不必等那个 finally 才能撤销 exact 旧权限。

## 9. 决定提交与操作执行分开结算

### 9.1 原事务与唯一 winner

沿用 repository `accept_tool_interaction_decision`：

- ALLOW：决定与 exact attempt 同事务。
- DENY：决定与 no-attempt result 同事务。
- 提交后通过原 FULL facts、future 和 effect settlement 继续执行／记录，不建立新 receipt。

| 先取得有效边界的事实 | 必须结果 |
| --- | --- |
| expiry／STOP／工具失效已结束候选，再到 ALLOW | 拒绝旧决定，无新 attempt |
| 决定合法进入提交，再发生 detach | 完成原结算；不因连接消失改成拒绝 |
| 决定合法进入提交，再到 expiry／STOP／Host close | 原提交 owner 负责确认；已提交决定不能被清除。取消仍按原执行纪律生效，不承诺 ALLOW 后必定开始物理调用 |
| 已提交 ALLOW，物理工具尚未开始时 STOP | 不能将授权改成用户拒绝；工具是否开始／取消由原 attempt owner 裁定 |
| 工具已经开始，再发生关闭／断连 | 保留真实副作用及结果；结束窗口不构成回滚 |
| 决定写入结果未知 | 保留原 candidate／command 身份，由 coordinator 调用第 9.2 节只读确认；浏览器 query 仅作公开结果查询，不代替恢复原 future |

### 9.2 明确新增窄的只读 exact-confirm，不重跑 writer

实施前没有完整接入普通决定的只读确认路径。本轮按授权从 `accept_tool_interaction_decision` 的幂等读取／校验／完整事实构造中提取共享逻辑，并提供窄的 `confirm_tool_interaction_decision(...) -> AcceptedInteractionDecision | None`；同类 `confirm_tool_result_winner` 的只读连接／writer 校验方式可复用。

- 输入由原 coordinator 在第一次接纳时冻结：原 guard/session、command、decision ID、assistant entry／tool call、ALLOW/DENY、对应 attempt 或 DENY result/entry 身份与原 permission 事实。核实时使用原已接纳 actor，不换成新 controller；不新增摘要字段、receipt 或全局映射。
- 返回原决定的完整 `AcceptedInteractionDecision`：包含 ALLOW 的 exact attempt，或 DENY 的 result、entry、sequence、observed_at 等原 future／settlement 所需事实。不得用 `query_command` 的 `SUCCEEDED`／target_id 代替。
- 只读查询使用一致快照，校验原 command kind、目标身份和对应必要关系。找不到已接纳决定时才返回 None；身份不符或必要事实部分存在返回既有 conflict，读取失败保持未知。不能通过 INNER JOIN 隐去部分状态后误报 None。
- 所有同一事实的构造／校验只有一处，由 writer 幂等分支和只读确认复用；ALLOW 后合法追加的工具结果不改变原 ALLOW 决定，也不应被误当成 DENY 的 result union。
- 此入口不得 INSERT／UPDATE、创建新 attempt/result、更新批准时间或重新取得许可；不得再次调用写入方法来“确认”，即便复用同一 command ID 也不允许借查询触发首次写入。
- 原 coordinator 是唯一提交和异常确认 owner。`CancelledError`／timeout／数据库异常后，先按 `KernelSessionIO` 确认原物理写入已退出，再由受原生命周期追踪的结算执行只读确认；不能由已取消的 socket waiter 把这段结算一并丢弃，也不能持有 coordinator／controller 锁跨 I/O。

复用原 canonical I/O、watchdog、settlement_changed 和关闭 machinery。进入写入及结果核实期间 `resolving` 连续保持 true；每次浏览器重连不创建新的核实 owner，不重置已有结算期限。核实沿用既有有界 canonical I/O／关闭预算，不把已过期的确认期限当成“禁止再核对是否已提交”，也不因反复查询创建无界续期。

### 9.3 核实后重新裁定原失效事实

唯一顺序：**确认写入结果 → 在 coordinator 中重查原期限／取消／能力失效／owner 状态 → 完成原 future 或恢复原候选 → 唤醒结算等待者**。

| 核实结果 | 后续处理 |
| --- | --- |
| 找到 exact 已提交决定 | 消费原 FULL facts，闭合原 view/future；随后按原执行取消与 effect settlement 处理，不修改决定，不再写入第二次 |
| 确认没有决定，原物理写入已退出，原候选仍有效 | 清除 resolving，恢复原候选；不改 ID、FIFO、许可和原 deadline。通过原协议错误响应明确返回 `INTERACTION_NOT_ACCEPTED`，不把原写入异常作为未知提交传给浏览器。无 controller 时只保留，不直接拒绝 |
| 确认没有决定，但原期限已过／取消或能力失效已成立 | 直接按真实失效原因结束候选，释放许可并完成等待；不能仅清除 resolving 后等待一个已经消费掉的 timeout 再来一次 |
| 读取失败、身份冲突、writer 已丢失或物理结果仍不可确认 | 不恢复按钮、不补造 DENY；在原有界结算／关闭 owner 内按未知或中断语义收尾，不遗留无 owner 的 future，不承诺未发生副作用 |

expiry 可由原单调 deadline 重查；提交期间到达的 STOP／能力失效等若不能从原 owner 重取，允许仅在**原 candidate** 上保留一个封闭的待收尾原因，第一项真实失效不被后来的 detach 或其他文案覆盖。不新增取消队列、事件或持久状态。普通 detach 本身不是失效原因。

任何不再 resolving 的退出分支都必须推进原 settlement_changed／future 清理；不能因先前到期处理已返回而永久挂起。预算耗尽后的未知收尾不能变成一条确定的“未授权、没有 attempt”事实来覆盖可能已提交的 ALLOW。没有独立恢复 worker、后台重写或跨重启延续。

## 10. 网络未知与浏览器提交状态

### 10.1 点击前就保存原 command

当前 adapter 在 `resolveInteraction` 内生成 command ID，失败后上层可能只知道“确认失败”。普通工具分支必须 hard-cut 为：在 app 的提交 owner 创建并保存 command ID，再经 typed tool resolution 参数传给 adapter。adapter 不得为同次恢复生成新 ID。

同一 session／Host／interaction 的本窗口提交事实保存：原 command、decision、连接 generation 和当前查询状态。只使用现有 app process-local 状态／请求纪律；不建 durable/localStorage receipt，也不把 capability/plan 决策顺手改成另一产品。

### 10.2 恢复规则

- ACK 未知：只查询原 `command_id`，不自动重发 ALLOW／DENY，不改投其他 interaction。
- query 找到 `INTERACTION_ALLOW`／`INTERACTION_DENY`：显示对应“决定已接纳”；操作是否完成仍由原 attempt／result 证明。
- query 为空：不证明拒绝或未提交。刷新同 Host 的 slot／决定进行中事实；未知时不能把按钮当作全新操作重新开放。
- 明确未接纳的 stale 请求：刷新 exact 当前 view。仍是原确认且有效时，用户可以重新作出明确决定；这是新点击，不是后台自动重试。
- 原 coordinator 只读 exact-confirm 返回 None 后的 `INTERACTION_NOT_ACCEPTED` 同样是明确未接纳事实；浏览器只解除这个原 command 的未知意图，按钮仍受 fresh view、controller、原期限和进行中投影约束。读取失败／冲突及普通 `SERVER_OPERATION_FAILED` 不具有这个含义。
- 写入后核实失败（含 identity conflict）经原错误响应返回 `INTERACTION_OUTCOME_UNKNOWN`；不得被网关的 admission stale 捕获分支改报未接纳。上述两种封闭结果复用既有 ErrorFrame，不新增协议字段或持久记录。
- 同一页面的每个未知 command 独立查询，不以首条长期未知或在途查询阻塞后续 command。空查询之后，若当前合法连接已确认原 Host／interaction 不再是可操作的原 slot，可退役该 command 的自动查询，但不宣称拒绝、未提交或没有副作用。相同原 slot 仍存在时，空查询或 busy=false 都不能解除未知保护。迟到回调按 exact command 更新，不能覆盖新点击或倒退已确认事实。
- 每个 command 在同一连接最多一个在途查询；期间到来的新投影在该次查询结束后再考虑。完成的当前 cut 不自行重复查询；重连可在新合法连接上查询，不新增计时轮询或总重试次数限制。
- 新 Host：原 pending 确认不能恢复；若本窗口仍知道旧 command，可经当前合法 read/query 路径核对已保存决定。查询既有决定不等于恢复执行。若没有足够事实，显示“运行中断，操作结果待核实”。
- 浏览器整体重载可能丢失本窗口意图；此时以新快照与 canonical 事实为准，不能恢复一条猜测 command 或一条相同文本的确认。
- 迟到的 success/error/finally 只更新其原 session／请求状态，不能解锁新会话的按钮、覆盖新草稿或切回旧会话。

## 11. 最小只读投影与提示来源

### 11.1 恢复确认的投影

继续使用现有 `SessionLiveControlSnapshot.current_interaction`、`LiveInteractionView`、expires_at、snapshot-and-subscribe 和 GAP 刷新。

本 PR 明确授权一个窄的只读增量：在现有 `CurrentInteractionView`／`LiveInteractionView` 增加 `decision_in_progress: bool`，唯一 producer 是 coordinator 的当前 pending.resolving，覆盖第 9 节的写入和结果核实全阶段。它用来防止重连窗口把正在提交／核实的确认重新显示成可重复批准，不是另一份状态真源。

- 新候选为 false；进入决定提交为 true；确定未提交且可重新开放时为 false；已结算则关闭原 view。
- 复用现有 interaction replacement/live revision 发布同一 interaction 的变化，不能用新 ID 表达“正在处理”。candidate.revision 和 snapshot.revision 同步推进，不能自行制造 stale。
- 不增加 committed/live event kind；更新原类型、proto/generated/wire fixtures、gateway、bridge 和 frontend，生产只保留新单一路径。
- UI 正常等待时显示允许／拒绝；true 时显示“正在处理确认”，按钮禁用。卡片自身 busy 只是一层本地防双击：按钮还必须受 current controller、原确认有效性、该投影和 app 中同一确认的提交中／ACK 未知意图共同约束。刷新组件或收到 false 不能单独清除 app 的未知意图；恢复可用须满足第 9–10 节证据条件。
- expires_at 由第 7 节唯一截止来源投影，不另加 heartbeat、轮询任务或期限 registry。

### 11.2 结束原因与证据不足

复用已有 live-bus `INTERACTION_CLOSED` 的 exact interaction ID、turn 和 reason；它已经携带 `InteractionClosedPayload.reason`。live-control 的 slot 变化仍是 current view 真源，live-bus 原因只作可丢弃说明，不因为缺少一条 close 事件保留旧按钮。不要再给 live-control 并造一套结束原因事件。

固定原因按 producer 提供的封闭含义映射：

| 已有事实／reference | 可显示 | 不可显示 |
| --- | --- | --- |
| 当前确认存在、decision_in_progress=false | 等待你的确认；只决定本次操作 | 工具已开始／用户已看到 |
| 原决定正在写入或核实写入结果 | 正在处理确认 | 已批准、已执行 |
| `INTERACTION_ALLOW` query／ACK | 已允许本次操作 | 文件已修改、外部调用成功 |
| `INTERACTION_DENY` query／ACK | 已拒绝本次操作 | 所有工作已停止 |
| `interaction:expired` 且原执行未获批准 | 确认已过期，本次操作未获授权 | 用户拒绝 |
| 原 STOP／turn-cancelled 事实 | 原任务已停止，这项待确认操作已结束 | 已发生的副作用已撤销 |
| 原 Host closing 事实 | 运行环境已关闭，原确认已结束 | 所有物理效果均已回滚 |
| capability/config owner 失效 | 操作条件已变化，这项确认已失效 | 允许后将自动换用新工具 |
| 只有新 Host／旧 turn interrupted | 原运行已中断，未恢复旧确认 | 确定是停电、确定文件未修改 |
| slot 消失，但没有可证明原因 | 这项确认已结束；原因暂不可确认 | 根据浏览器时钟猜测超时／根据相邻结果猜测拒绝 |

`RESOLVED` 本身不足以区分 ALLOW/DENY，必须使用真实决定。历史展示只利用现有 canonical 决定／工具结果／中断事实；不要求每条已丢失的内存确认都拥有持久失效记录，不新增 tombstone。模型和工具原文保持不变，UI 标签是旁侧解释，不能全局替换结果正文。

## 12. 前端交互边界

- 切会话继续释放旧连接，回来重新连接；普通确认不弹“离开将拒绝”的旧警告，不阻止导航。
- 同 Host 返回使用 fresh snapshot 恢复原确认，不复制 DOM 或旧闭包。observer 无批准／拒绝按钮；合法接管后再取得 fresh controller view。
- 当前会话可显示“等待确认”，不用装成模型正在生成。左侧列表若缺当前 Host 的实时事实，不新增常驻连接或通用活动服务来承诺全局即时 badge。
- 能力配置表单仍会因真实 detach 结束，主动离开可针对该类别提示；打开不造成 detach 的能力／记忆页面不误弹。
- 不在切到 B 后把 A 的确认弹到 B 主对话；返回 A 再处理。对 A 的旧响应不能影响 B 的草稿、输入队列和按钮状态。
- 超时／取消／关闭后旧操作按钮不可点击；若用户仍需要原目标，可重新要求助手检查当前状态再发起操作，不提供自动重放按钮。
- 不新增“永久允许”“默认批准离线操作”或批量审批。现有 permission 模式入口保持独立。

## 13. 正常关闭、崩溃和重启

### 13.1 正常关闭

Host stop-admission／取消／I/O 和 effect settlement 的既有关闭顺序保留。未批准候选结束；合法决定若已提交，不能被关闭改写成 DENY。保留有界关闭预算，禁止等待一位永远不会回来的 controller 才让进程退出。

关闭超时、强杀和停电不能宣称完成了优雅收尾。PR05 不新增后台重试关闭任务或持久恢复程序。

### 13.2 非正常退出

pending／future 仅在内存，进程消失即丢失；没有机会补写“确认过期”或“用户拒绝”。数据库恢复正常、会话由新 Host 接管后，复用 `_interrupt_prior_generation` 对旧运行／子任务的中断处理。没有原等待的自动恢复，也不将旧 tool arguments 作为新调用重新执行。

必须区分：

1. 确认尚未批准：不能凭重启补出授权。
2. 决定已提交、物理操作未开始：批准记录不是执行成功。
3. 物理操作已发生、结果未保存：缺少结果不是未执行，需核实实际文件／外部状态。
4. 决定与结果都已保存：读取原事实，不重复执行。

重启后提示只能依据已知事实。不承诺 PostgreSQL／磁盘损坏时历史必然可恢复，本 PR 不做数据恢复；不得为测试本功能重置用户未核实的数据库。

### 13.3 不扩大“安静下来”的承诺

不恢复旧确认，不等于整个 session 再无自动活动。此前已排队的独立 NEW_TURN 仍按原 FIFO，terminal/subagent owner 仍按各自既有产品规则。STOP 仍绑定 exact 运行，不获得“暂停所有未来工作”的新含义。

## 14. provider、权限、Hook 与存储不变量

1. 离开、attach、snapshot、query、决定进行中投影不触发 provider 请求，不改 SYSTEM/tools，不重建已安装 prefix。
2. 工具等待是原异步 future；没有“为了等待用户而持续采样”的模型回合。结果产生后才经既有 tool closure/safe-point/runner 路径继续。
3. 批准不改变已冻结工具参数／权限，不重新执行 UserPromptSubmit，不重复获取 MCP permit；原 Hook 与 permission gate 保留。
4. 新确认或结果只用已有 canonical/tool-result/accepted decision 路径；不把过期、断线或关闭伪装成 HUMAN DENY，不为离线保留新增 durable occurrence。
5. pending、deadline、controller、decision_in_progress 均归唯一 process-local owner；已接纳决定仍是既有关系事实。
6. 不新增表、关系、guard、subject、job、committed/live event kind、fingerprint。仅第 11 节的只读 view 字段为协议必要增量；clean-v0/catalog/oracle 类别计数保持。
7. 不改 provider lowering、wire tool schema 或 SYSTEM 文本。本 PR 无需新 cold boundary；现有 epoch 内 prefix 必须保持不变。

## 15. 实施落点与删除清单

| 落点 | 必需修改 |
| --- | --- |
| `conversation_kernel/interaction.py` | tool/form 分流，保留普通 pending/dormant 和未发布 head；唯一 deadline；attach 推进；exact controller admission；原决定写入／只读确认／延期失效收尾；进行中投影 |
| `interaction_arbiter.py`、`tool_runtime.py` | 保留 FIFO／容量／MCP hooks，区分 owner 不存在和 controller 缺席，不重复 admission/discard |
| `_repository/tools.py`、`io.py` | 提取共享完整决定读取／校验，新增窄只读确认；复用 I/O 物理任务退出和 canonical 只读连接，不改成重复 writer 或另建物理 I/O owner |
| `host.py` | 原 attachment 的精确撤权与 attach/promote；原公开 command query、关闭与执行 owner 接入；不新增运行循环 |
| `live_control.py`、protocol schema/gateway/generated | 第 11 节唯一 view 字段；原快照与 revision 传播；controller 入口核对当前权限，旧请求结算与后续请求拒绝分开 |
| `web_app/browser_bridge.py`、`protocol_client.py` | 保存 exact 旧 attachment，经原 Host owner 撤权后释放／替换连接；不以旧请求在途为由伪装接管成功；body/owner allowlist 同步 |
| `frontend/lib/runtime-adapter.ts` | 保留 app 给定的 tool decision command，恢复新 view，投影 deadline／进行中／已有 close reason |
| `frontend/app/pulsara-app.tsx` | 提交 intent、原 command 查询、session/connection 异步隔离 |
| `frontend/components/workbench-view.tsx` | 原确认恢复、busy／结束原因／权限可见性；不建设审批历史界面 |

同次删除：

- 普通确认的 `no controller → DENY`，保留真实 owner 不存在的失败。
- 普通确认的 `controller_detached → abort_all` 和发布中仅因 controller 缺席而 discard 的路径。
- 普通 detach 的旧 fail-closed 产品提示；能力表单例外必须显式保留，不用兼容 flag。
- 普通确认在每次显示／重连时重算期限的路径。
- adapter 内为一次 tool confirmation 恢复重新生成 command 的路径。
- 只信 gateway 旧 role、不验证 coordinator 当前 controller 的决定入口。
- 普通决定在任意 I/O 异常后无条件清除 resolving／恢复按钮的路径；禁止以重调 writer 充当 exact-confirm。
- 先到期、后提交失败时只复位 resolving、不闭合已失效 future 的路径，以及入队后 promotion 不受超时／取消清理保护的路径。
- 接管仅依赖 client socket close／旧 gateway finally 的撤权路径；以及已消费 MCP admission 的 head 被放回 dormant 后重跑 hook 的实现方式。
- 错把所有结束归为用户拒绝、根据 UI 时钟证明未授权、把 ACK 成功当物理成功的展示。

不删除 Host close／STOP／配置失效等真实取消，不删除既有 resolving/winner 仲裁，不在另一个函数或 fallback 中保留旧普通 detach 语义。检查 CLI／protocol controller 调用方，不能只修浏览器入口。

## 16. Phase A：先复现与冻结 owner

1. 完整阅读 AGENTS.md、本文、interaction/arbiter/runtime/Host、MCP admission、decision writer、gateway/bridge、frontend 和现有取消测试。
2. 先写红灯：visible detach 被拒绝、controller 缺席时新确认失败、重新 attach 没有推进 dormant、旧 controller 可用原身份提交，以及 I/O 取消后已提交事实丢失、提交中到期后失败挂起、真实协议 busy 接管被降级等实际可达场景。MCP 已持许可未发布的交错同样使用明确屏障验证。
3. 冻结唯一 deadline、controller admission 和 submitted-winner 的线性化边界；现有 tests 的旧 fail-closed 断言按新合同替换，但先证明旧行为会失败。
4. 核对 tool/form 共享路径，保留 form cancellation 回归；确认没有引入新持久分类或总量 cap。

## 17. Phase B/C：内核与前端 hard cut

### Phase B

实施第 6–11 节：同 Host 候选保留、controller 精确撤权与 attach 推进、一次 MCP admission 后续接发布、覆盖 promotion 的统一期限、只读 exact-confirm／失效复查、写入及核实全阶段投影。将普通/form 边界一次切完；已有 decision transaction、公开 query、MCP permit 和关闭 owner 不复制，新的内核只读入口共享原事实构造。

先在 focused 层证明 no execution before ALLOW、单次决定／attempt、expiry／STOP／takeover／unknown commit 仲裁；再更新协议生成物。不要把“禁掉超时或关闭测试”作为新保留语义的实现。

### Phase C

app 在点击前拥有 command，adapter 单次发送；返回同 Host 读取原确认，进行中禁用重复决定，ACK 未知查询原身份；切会话保留 draft 和 PR04 queue 行为。UI 只使用已验证的交互事实，不重构整个 workbench。

## 18. 确定性测试矩阵

新增 focused 文件建议 `tests/test_pr05_tool_confirmation_retention.py`，与原测试共同验证，不复制生产状态机来测试伪实现。

| 编号 | 场景 | 必须断言 |
| --- | --- | --- |
| R01 | visible 普通确认 detach→attach | 原 ID/future/目标不变；无 DENY／attempt；回来可以批准一次 |
| R02 | controller 缺席后首次产生确认 | dormant 等待，无直接拒绝；attach 推进，无额外模型请求 |
| R03 | detach 在 before_publish 前／已取得 permit 后但未发布 | 原 head 留在 `_pending`，不退回 dormant；重连只发布；hook/admit 各一次，无 FIFO 越过；到期／取消后晚到发布被拒绝 |
| R04 | 多候选和多 turn/child | 一个 visible、FIFO、容量沿用；无全部同时 admission |
| R05 | 截止前后、dormant／promotion 到期或取消 | 保护覆盖入队后首次 promotion；不遗留 future／permit；排队 9 分钟展示约剩 1 分钟，不以真人拒绝落库 |
| R06 | 反复 reload／takeover | 不续期，不复制 ID，不重复 Hook／工具调用 |
| R07 | 原任务 STOP、child 取消 | 精确取消相关候选；不影响其他任务；真正取消工具等待者（含写入／核实／到期后等待与重复取消）完成原结算后释放不再执行的 permit；已提交 ALLOW 不改写 |
| R08 | 旧 controller 决定／其他控制请求晚到 | 撤权后新请求拒绝，即使旧 transport role 仍写 CONTROLLER；普通决定由 coordinator 联合验证；旧 finally 不撤销新 controller |
| R09 | controller 先通过 admission、随后 detach | 原合法决定照常结算；不能抹除 FULL winner |
| R10 | observer／跨 session／新 Host／旧 revision | 不可批准，不泄露其他 owner 的正文，不改投新对象 |
| R11 | ALLOW／DENY、双击、重复 command | 最多一个决定及合法 attempt/result，无第二物理调用 |
| R12 | 决定写入／核实期间，真实 Unix 连接关闭与合法接管 | 原请求保持在途，新 controller 仍能取得权限，不因旧权限未释放降级 observer；in_progress 为 true，原决定继续；迟到 detach 幂等 |
| R13 | 提交中先 expiry／STOP／close，再返回提交成功或失败 | 成功消费 exact FULL facts；确证失败后处理已发生的期限／取消，闭合 future 与 settlement_changed，不等待第二次 timeout |
| R14 | 写后异常／调用方取消／ACK 丢失与只读核实 | ALLOW/DENY 完整事实恢复原 future；writer 调用一次，核实零写入；部分行／错身份不报 None；公开 query 不冒充内核 FULL；未知持续禁用，本窗口 intent 不被卡片重挂载清除 |
| R15 | 确证未提交／查询不可用与旧 deadline | 重查期限、执行与能力失效；过期不续期、不过期才恢复；明确未接纳经实际协议传至前端，可在同一页面新点击；查询空／失败不放松未知保护，旧未知 command 不饿死后续查询，已结束意图可退役；核实未知有界收尾，不留悬挂或伪造 DENY；重连不重置结算预算 |
| R16 | MCP disable／reload、Hook 取消，含决定在提交 | 原失效事实不因 resolving 被丢掉；确证未提交后释放旧 permit，不允许借旧确认执行新 generation；已有 winner 按原执行 owner 结算 |
| R17 | CAPABILITY_FORM detach／解析进行中 | 保留旧取消／秘密边界；不把未提交内容转给新窗口 |
| R18 | 正常 Host close | 所有等待可有界结束；合法已提交 winner 保留，无等待 controller 的退出死锁 |
| R19 | pending 时子进程强杀→重新打开 | 原 pending 不恢复、旧 turn 中断、无自动工具 replay |
| R20 | ALLOW commit 后／效果后 crash | 根据实际决定／attempt/result 展示；无结果不声明无副作用，无自动重试 |
| R21 | live close 原因缺失／GAP／reload | 原 view 以快照为准；原因不足降级，不伪造过期／拒绝，不新增 tombstone |
| R22 | A/B 会话切换与迟到回调 | A 的响应不影响 B 草稿、queue、按钮／会话导航 |
| R23 | 等待期间 NEW_TURN 和 exact steer | 原输入身份／队列策略不变；不把新输入当授权，不额外修改 safe-point 唤醒合同 |
| R24 | plan question/draft 和普通/form 混合 | 原 plan 恢复／权限不变，不因统一 UI 获得历史审批权 |
| R25 | 同 epoch、批准后的 tool continuation | SYSTEM/tools 逐字不变，messages suffix-only；等待／重连本身不调用 provider |
| R26 | 等待期间文件被其他方修改 | 原 revision／执行校验生效；批准不等于允许覆盖未验证新版本 |

时间类测试通过可控 clock／deadline 运行，不 sleep 10 分钟。强杀只针对测试创建并记录 PID 的隔离进程，不能杀用户正在使用的 kernel 或停掉真实 PostgreSQL。

R13–R15 还须有真实 PostgreSQL 的事务证据：覆盖真实提交之后人为丢弃响应／抛出异常，以及明确 rollback 后恢复；只读确认重建 ALLOW/DENY 原事实、确认没有二次写入，并区分空查询与读取失败。使用替身控制时序的单元测试不能替代这部分。R12 经过实际 gateway/client/bridge 接管路径，不只直接调用 coordinator.attach 来证明 UI takeover。

## 19. Phase D：验证与证据

### 19.1 自动化

使用根目录 uv/.venv。reviewer 修正后的最终完整回归为 Python **1798 passed**、前端 **236 passed**；真实 coordinator／PostgreSQL／Unix 协议 focused 为 **40 passed**。原始命令、红灯、迭代和最终日志见[本次重新验收记录](output/playwright/pr05-review-fixes/README.zh.md)。初次 1788／230 仅为历史，不作为本次通过依据。

```sh
uv run pytest -q tests/test_pr05_tool_confirmation_retention.py tests/test_pr05_confirmation_postgres.py tests/test_stage2_protocol_v3.py tests/test_capability_form_interaction.py tests/test_round6_mcp_production.py
uv run pytest -q tests/test_local_web_browser_bridge.py tests/test_local_web_http_surface.py tests/test_stage2_conversation_runner.py tests/test_stage2_kernel_host_dogfood.py tests/test_stage2_subagent_close.py
uv run pytest -q tests/test_round4_plan_host.py tests/test_round4_plan_postgres.py tests/test_round3_1_provider_input_prefix_continuity.py tests/test_pr04_prompt_queue_actions.py
uv run python tools/generate_terminal_protocol_contract.py --check
uv run ruff check .
uv run pytest -q
npm --prefix frontend test -- app/pulsara-app.test.tsx lib/runtime-adapter.test.ts components/workbench-view.test.tsx
npm --prefix frontend test
npm --prefix frontend run lint
frontend/node_modules/.bin/tsc --project frontend/tsconfig.json --noEmit --incremental false
npm --prefix frontend run build:local
git diff --check
```

clean-v0/catalog/oracle、fingerprint subtraction、long-horizon／MCP 生命周期相邻测试按实际接入点同时覆盖。现有总量分类不增，不新增迁移链；只有真实本地数据库目标经核实且测试确实需要时才按 AGENTS.md 操作。

### 19.2 Isolated wheel／launcher

构建最终 wheel，安装到独立 venv，从非源码 cwd 启动。核对模块与 static 来自安装包，并在安装版验证切会话／刷新后的原确认、批准后工具结果与最终产物入口。代码／协议修改后不得沿用旧 bundle 截图冒充验收。

### 19.3 真实 provider／浏览器

只读使用 `LocalSettingsStore`＋`require_pulsara_home()` 解析保存配置；优先已保存且可用的 OpenRouter。只遮蔽实际密钥，不删掉可核查的请求、工具结果和身份。

至少运行：

1. A 中真实模型发起需要确认的无害测试操作；切到 B，再返回 A，原 interaction 恢复，批准后只产生一次实际效果。
2. 用户先离开 A，模型随后才走到确认；返回后能处理，不是先被 no-controller 拒绝。
3. 同 Host refresh、断线重连、第二 observer 窗口、合法 takeover；特意将原决定停在写入／核实阶段，证明接管可以完成而原结算继续，旧按钮及其他新控制请求不可越权。
4. 实际等待期间不点击，证明无物理调用、无等待轮询采样；批准／拒绝后工具结果与同一原运行对应。
5. harness 对确切请求注入 ACK 丢失、决策提交延迟、提交中到期后失败或 close 交错；分别核对 coordinator 的只读完整确认和浏览器原 command 查询、无重放与无悬挂。区分故障注入与自然时序证据，不伪称真实停电。
6. 隔离 Host 进程的 graceful close 和强杀恢复；分别覆盖尚未批准和批准后效果未知。保留实际文件／外部测试资源状态，不能仅根据 UI 推断。
7. 普通确认保留与能力表单离开取消对照，plan／PR04 queue／STOP 相邻流程未变。
8. 桌面与 390×844 可见检查：确认可读、busy/失效状态准确、按钮可用性与 owner 一致，composer 不被挤出视口。

保留必要截图、command／interaction／turn／attempt／entry 身份和时序、provider 请求计数、控制台与实际操作结果。本次证据目录为 `output/playwright/pr05-review-fixes/`，初次记录保留于 `output/playwright/pr05-confirmation-retention/`；只保留可复查的必要材料，不生成代码／文档／证据 SHA，不将庞大重复 trace 当覆盖率。

## 20. Activation acceptance criteria

以下项目已在 reviewer 修正后逐项重新核验；自动化、真实事务／协议、安装包、provider／浏览器、三阶段 SIGKILL 和 R01–R26 对照均见[本次重新验收记录](output/playwright/pr05-review-fixes/README.zh.md)，不引用旧结果代替本轮通过证据：

- [x] 普通确认在同 Host 切会话／重连／接管后可继续批准，interaction／工具身份不变。
- [x] controller 缺席时新产生的普通确认也能等待；attach 有唯一推进路径，无孤立 future。
- [x] 单 visible／FIFO／容量／MCP permit 和原 Hook／permission 语义保持；取得 permit 未发布时保留原 head，重连不重跑 admission。
- [x] 10 分钟原期限包含离线、dormant 和入队后 promotion；不因展示／重连续期，UTC 与剩余时间展示一致；提交中到期再失败不会悬挂。
- [x] exact 当前 controller 在 coordinator admission 中验证；合法接管不依赖旧 socket 请求结束才撤权，旧请求结算不丢；撤权后的新控制请求不可越权。
- [x] 原 coordinator 用窄只读入口恢复 ALLOW/DENY 完整事实，异常／取消不无条件复位；写入及核实全阶段投影真实，winner／unknown 与延期失效分支有真实事务和确定性证据。
- [x] ACK 未知查询原 command，不自动重发，不把批准成功当物理执行成功。
- [x] 超时、取消、关闭、capability 失效和未知原因准确展示；不伪造用户拒绝或未发生副作用。
- [x] 正常关闭可有界收尾；crash 后不恢复 pending、不 replay，原 canonical 事实不改写。
- [x] 能力配置表单与 plan 独立语义保留，秘密／权限边界无扩大。
- [x] SYSTEM/tools byte-identical、messages suffix-only、无额外等待采样；NEW_TURN／steer／子任务取消保持。
- [x] 无新增 durable 分类、pending 表、receipt、job、registry、fingerprint、迁移链或 feature flag；第 11 节唯一只读协议增量已同步。
- [x] 旧普通 detach/no-controller 取消路径完全删除，所有正式 controller 调用方接入，未保留双路径。
- [x] R01–R26、focused/full regression、lint/typecheck、protocol generation、静态构建和架构检查全部通过，无 skip/xfail 或弱化断言。
- [x] 最终 isolated wheel／launcher、真实 provider/browser 和视觉证据完成；异常场景如实分类。
- [x] 本规格／索引／最终静态产物同步，验证命令、剩余风险和 Git 状态如实记录；不自动 stage/commit。

最终契约：**用户可以离开，再回来决定原操作；离线不等于拒绝，批准不等于执行成功，重启也不等于恢复旧授权。**
