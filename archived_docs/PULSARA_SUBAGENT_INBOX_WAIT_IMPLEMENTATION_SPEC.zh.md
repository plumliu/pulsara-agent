# Pulsara 子代理来信等待实施规格

日期：2026-11-08。状态：已实施，已完成并行实现、交叉审阅与相关回归验证。

## 1. 目标与边界

Subagent 发出问题后，需要暂停自己的执行，等待 ROOT 或其他 subagent 的回复。暂停期间，任何发给当前 turn 的已接纳 Agent 来信都应唤醒它，不要求模型提前枚举发送方。

Subagent 负责完成当前分配，不承担任务编排与调度职责；它可以与 ROOT／其他 subagent 通信，但不需要等待指定 subagent 或 Work。对象协调与任务完成观察由 ROOT 在 Code Mode 中负责。

原生使用 `wait_message` 等待自己的收件队列。ROOT Code Mode 仅通过 `tools.wait_agent` 观察指定 Agent，完整 until／settle 合同由 [Agent 等待条件规格](PULSARA_AGENT_WAIT_UNTIL_IMPLEMENTATION_SPEC.zh.md) 规定；本文只规定原生收件等待及 final answer。

“等待自己”指暂停当前执行、观察自己的来信，不是把自身 AGENT target 传给一个 Work 完成观察器。原生调用不接受 target。

### 1.1 当前痛点与命名原因

最初的需求是让 subagent 的 final answer 更详细，供 ROOT 和后续任务使用，并与 `send_agent_message` 的过程协调区分。讨论过程协调时发现：subagent 提问后需要暂停等回复，而原先的原生 `wait_agent` 要求指定 Agent，并复用了主要服务 ROOT Code Mode 的对象观察语义。等待列表外的 Agent 即使已经发来消息，也不能提前结束等待；直接传自身则因绑定自己的当前 Work 被拒绝。这与“暂停自己，收到来信就继续”的需求不一致。

因此原生工具改名 `wait_message`，直接表达收件等待，去掉不必要的 Agent／Work 参数。Code Mode 的对象等待使用 wait_agent，具体等待条件由 until 表达；Work 身份沿现有结果查询使用。名称按实际职责区分，不另建等待机制。

## 2. 现有 owner 与接入边界

- `capability/builtin_catalog.py` 分别定义原生 `wait_message` 与脚本专属 `wait_agent`，原生仅向 SUBAGENT owner 开放，对象等待仅向 ROOT Code Mode 开放。
- `subagents/collaboration_api.py` 将两个工具分别送入 `_wait_message`／`_wait_agents`。Agent 对象观察按指定发送方查消息并使用 until 条件；原生收件等待不绑定 Work。
- `subagent.py` 的 `suspend_execution` 已负责等待期间释放物理执行槽，恢复执行前重新取得槽位。
- `_repository/subagents.py` 的 `has_pending_agent_message` 按接收 session、目标 turn 和 provider 已接纳位置查询未读来信存在性；`pending_agent_message_sources` 保留 Code Mode 的发送方过滤与游标查询。
- `host.py` 的 `_send_agent_message` 将消息接纳到接收方当前 active turn 的普通输入队列，随后通知运行时变化。它不启动 inactive Agent，也不把迟到消息改投后继 turn。

实现复用这些 owner，只增加原生收件等待分支与最小查询适配，不新增调度器、消息存储或交付机制。

## 3. 原生与 Code Mode 的产品合同

| 项目 | Subagent 原生 `wait_message` | ROOT Code Mode `tools.wait_agent` |
| --- | --- | --- |
| 用途 | 暂停当前执行，等待当前 turn 的 Agent 来信 | 观察指定 Agent／Work |
| 参数 | 可选 `timeout_seconds` | `agents` 与 until／settle／timeout_seconds，见 Agent 等待条件规格 |
| 发送方 | 任意已合法接纳到当前 turn 的 Agent 消息，包括 ROOT | 保持现有指定发送方过滤 |
| Work 绑定 | 无 | 由 until 决定 |
| 无关 Work 状态变化 | 不构成返回条件 | 保持现有行为 |
| 来信正文 | 不在等待结果中复制 | 保持现有行为 |
| 取消与权限 | 沿用当前 turn 的取消及 bypass-permissions 要求 | 沿用现有 Cell／Code Mode 控制合同 |

ROOT 不能直接调用原生 `wait_message`；subagent 不能调用 ROOT 的 Code Mode 等待入口。不增加权限、通信对象或创建任务的能力。

### 3.1 原生参数

```json
{}
```

或：

```json
{"timeout_seconds": 30}
```

`timeout_seconds` 保持现有单次观察边界：默认 30 秒，允许有限数值 0 至 300 秒，拒绝布尔值、非有限值及其他非法类型。0 表示立即检查，不进入等待。

原生 schema 为封闭对象，拒绝 `agents`、`works`、`target`、`settle` 及未知字段。Code Mode 参数沿 Agent 等待条件规格，不接受原生空对象或收件等待分支。

保留 300 秒是沿用既有单次工具观察上限，不是新增任务时长或等待总量限制。不得增加等待次数、turn 次数或任务生命周期硬上限。

### 3.2 原生返回

正常返回只包含必要状态：

```json
{"state": "message_available"}
```

或：

```json
{"state": "timeout"}
```

原生等待不返回 `bindings`、`works`、`pending_targets`、`message_sources`、`unavailable_targets` 或消息正文。它只表明存在当前 turn 尚未交给模型的 Agent 来信，或本次观察已超时；不充当通信回执或任务结果。

Stop、close、接管等控制继续沿现有执行取消与物理收尾路径处理，不伪装为超时或来信成功，不增加一种原生等待专用取消协议。若原有公共执行层要求中断结果，沿用其合同。

## 4. 来信范围与正文交付

唤醒条件是当前接收 session、当前 active turn 中存在尚未进入已安装 provider 输入的 `AGENT_MESSAGE`。既包括仍为 PENDING 的输入，也包括已转为 canonical entry 但位于 provider 已接纳位置之后的输入；复用当前查询对这两种状态的判断。

- ROOT、已知或新合法引入的其他 subagent 来信都可以唤醒，不按模型列出的发送方过滤，也不依赖等待开始时的 Agent 名单。
- 等待开始前已排队且尚未交给模型的来信立即返回；等待开始后新接纳的来信同样唤醒。
- 消息必须已经通过现有发送 owner 的身份、可见性、权限与目标 turn 检查；“任意发送方”不扩大谁能发送消息。
- 其他 session、其他 turn 的消息不唤醒；普通 Work 完成、无关运行时通知和未被接纳的发送尝试不唤醒。
- 人类 steer／stop／close 继续沿原有输入与控制语义处理；本规格不把它们转换成 Agent 消息，也不重定义人类输入是否中断工具。

等待只观察存在性，不取走、确认、标记已读或复制消息。`message_available` 只证明观察时存在尚未安装的来信，不保证下一次 provider 请求包含全部消息。工具返回后，正文沿既有队列与 provider 编译路径处理，保持 FIFO、按预算接纳前缀及现有资源拒绝合同；正常接纳时保留原发送方和来源。队头消息无法在既有资源边界内接纳时，沿当前明确拒绝与 turn 中断规则结算，不增加消息保送、重试或回执。多个发送方同时来信不会因等待操作本身而被消费、覆盖、丢弃或合并成一条替代消息。

已进入 provider 输入的旧消息不得反复唤醒后续原生等待。消息在超时边界到达时，可以由已结束等待返回 timeout，随后由普通输入路径交付；不得丢失消息、改投 turn 或承诺严格的到达时刻裁决。

ROOT 向同一 subagent 再安排 Work，只会沿现有调度进入队列，不作为来信唤醒当前等待。当前 Work 结束后才执行后续 Work；超时只结束这次等待，不结束当前 Work。需要立即补充或调整当前任务时，ROOT 使用 `send_agent_message`；需要独立后续任务时使用 `run_agent`。

## 5. 等待生命周期与竞争

原生等待只有在确实需要挂起时才进入当前 `_suspend`／`suspend_execution`：先取得变化 revision 并执行首次收件存在性检查，已有来信则直接返回；没有来信且超时参数为 0，也直接返回，不释放或重新争抢物理槽位。否则挂起时释放物理 Agent 执行槽，恢复时遵循现有调度。等待不结束 Work，不创建新 Work，也不启动额外 turn 或 provider 请求。

观察循环遵循现有变化通知与重查模式：先读取变化 revision，再检查当前收件状态，无来信且仍有观察时间时才等待变化；首次挂起沿用首次检查前取得的 revision，不能在检查与挂起之间重置而漏掉通知。每次通知后重新查询。必须覆盖消息在检查前、检查与挂起之间、挂起后到达的竞争，避免已经存在的来信一直等到超时。单次观察 deadline 在调用开始时固定；实际挂起后的槽位重新取得时间沿用现有调度，不承诺在观察 deadline 内恢复执行。

全局 Agent 状态变化可以唤起内部重查，但只有当前 turn 的未读 Agent 来信才满足 `message_available`。无关变化不得延长原始 deadline，也不得导致热循环。

Stop／close／接管沿现有 owner 中止等待并正确结算工具与物理槽位；不取消发送方的 Work，不恢复已结束的接收 turn。会话关闭或进程崩溃后，不恢复等待，不持久化订阅，不增加检查点或补发任务。

## 6. 提示词与工具说明

原生工具说明应直接描述“暂停自己，等待 ROOT 或其他 agent 发来的消息”，列出超时参数并说明正文会通过正常输入到达。删除只适用于旧原生语义的发送方数组、Work 绑定和“ROOT 明确要求才能等待其回复”等表述。

Code Mode 的 wait_agent 说明按 Agent 等待条件规格描述 until／settle、消息、有限超时与准确结果查询。同步更新 bundled `pulsara-code-mode/SKILL.md`、工具调用与协作引用示例、ROOT system prompt 的调用示例，以及 `query_agents` 对 ROOT target 的说明。不得残留 `tools.wait_agent({works: ...})` 或暗示原生等待需要传 ROOT target 的当前指导。

Subagent system prompt 保持两种输出职责清晰：

- `send_agent_message` 用于过程中的问题、进展、阻塞和发现。需要回复时，可调用原生 `wait_message({timeout_seconds: ...})` 暂停自己；无须填写自身或发送方 target。超时后继续独立工作或在最终结果中报告未解决依赖，避免没有新理由地反复等待造成长时间阻塞。这是模型行为指导，不是运行时累计次数限制。
- final answer 是结束当前任务且不含工具调用的完整交接结果；已发送过工具消息不能替代它。静态提示词采用下列已确认并先行应用的英文措辞：

> End your assignment with a detailed, self-contained final answer containing no tool calls. Include the outcome, relevant findings or changes, evidence, artifacts, checks and remaining issues. Present the complete result even if you already shared parts of it through send_agent_message. Use descriptive Markdown links with absolute paths for local files.

只修改当前代码中的静态提示词与新 epoch 的正常编译输入；不得在已有 epoch 中重写 SYSTEM、tools 或已安装 messages。

## 7. 实施落点与减法要求

1. `subagents/collaboration_contracts.py`：分别定义 `wait_message`、`wait_agent` 的 schema 和参数校验；实际调用边界也必须按 owner 拒绝另一入口的参数，不能只依赖模型可见 schema。
2. `capability/builtin_catalog.py`：原生只注册 `wait_message`，脚本注册 `wait_agent`，保留对应角色和权限 owner；更新共享工具集合、执行绑定及工具词汇相关断言。
3. `subagents/collaboration_api.py`：原生 `wait_message` 进入收件等待；ROOT Code Mode 的 `wait_agent` 进入 `_wait_agents`。删除旧原生 `wait_agent` 暴露与脚本 `works` 旧分支，不做旧参数兼容；同步权限分类、工具路由和实际调用检查。
4. `subagent.py` 与 `_repository/subagents.py`：在现有收件查询 owner 内提供当前 turn 是否存在未读 Agent 来信的最小查询能力。原生只需存在性，不为得到一个布尔值枚举全部发送方；Code Mode 保留现有发送方过滤和 Cell 自有游标语义。
5. `ports/system_prompt.py`：落实第 6 节；更新当前合同测试及受影响示例、dogfood，不改写历史归档材料。
6. `conversation_kernel/tool_surface.py` 的 `CODE_MODE_SHARED_TOOL_NAMES` 与 `tool_runtime.py` 的 `_entry_unavailable`：保持目录与实际角色检查一致，按真实调用来源区分原生 `wait_message` 与脚本 `wait_agent`，不能仅修改描述符与 collaboration port。

不新增表、durable event、live event kind、subject、guard、relation、job、回执或投递确认。沿用现有 Agent 变化通知、队列、provider 已接纳位置及物理槽位 owner。不得为原生等待添加发送方游标、消息扫描登记表、长期订阅或全部 Agent 列表缓存。

这是开发期 hard cut。旧会话按仓库合同丢弃，不添加旧 schema fallback、旧参数双读或历史会话迁移。ROOT Code Mode 的现有功能作为当前有效产品合同保留。

## 8. 验收要求

### 8.1 Schema 与实际 owner

- 原生接受 `{}`、合法超时，拒绝全部旧目标参数、未知字段及非法超时。
- ROOT 原生调用仍被拒绝；subagent 不能通过低层 port 或伪造脚本参数进入 ROOT 等待分支。
- ROOT Code Mode 的 wait_agent 按 Agent 等待条件规格接纳参数并保留角色限制，不接受原生空参数分支。旧原生名称不可调用，脚本 `wait_agent({works: ...})` 明确拒绝。

### 8.2 来信与竞争

- 分别用 ROOT、其他 subagent、新合法引入发送方验证等待期间的来信唤醒；无需预先指定发送方。
- 覆盖已有未读来信立即返回、检查与挂起之间来信、挂起后来信、多发送方同时来信。
- 无关 Work 完成、其他 session／turn 消息、已交给模型的旧消息均不满足唤醒条件。
- 验证普通队列接纳到 canonical entry 后、provider 尚未安装期间仍能唤醒；不得因只查 PENDING 漏掉消息。
- 等待不修改消息状态、不复制正文；在预算足够时，后续正常 provider 请求收到原始正文及来源。预算仅容纳 FIFO 前缀时按现有规则分批接纳，剩余消息不因等待操作丢失；最小前缀无法接纳时保持原有明确资源拒绝和 turn 结算。
- 超时无来信、0 秒检查、deadline 边界来信正确；无关变化不刷新 deadline。
- ROOT 对等待中的 subagent 追加 Work 不唤醒、不结束当前 Work；发送 Agent 消息则唤醒。当前 Work 正常结束后后续 Work 才开始。

### 8.3 生命周期与集成

- 真实 Host 中 subagent 原生等待 ROOT／其他 subagent，消息到达后正常继续并形成详细 final；无额外等待专用模型调用或新 turn。
- 等待释放物理槽位，发送方可以执行；Stop、close 和既有接管路径不遗留等待任务或槽位占用。
- 物理槽已满且存在排队 Work 时，0 秒无信检查和首次已有来信检查均不释放／重取当前槽，不因排队 Work 抢槽而阻塞；真正挂起后的恢复沿现有调度。
- ROOT Code Mode 的指定发送方过滤、until 条件、后继 Work 边界、Cell 取消和消息游标回归通过。原有“列表外来信不完成指定观察”的测试仍属于 Code Mode 合同，原生对应测试替换为任意合法来信唤醒。
- 同 epoch SYSTEM／tools 不变，messages 按 suffix 安装。无新持久类别、恢复机制或任务总量限制。

使用仓库 `.venv` 运行 schema／协作 API 聚焦测试、真实隔离 PostgreSQL 收件查询测试及 Host 执行集成测试，再执行受影响的工具目录、Code Mode 与提示词回归。测试证明 Pulsara 的入口边界、队列交付与生命周期，不依赖模型自觉遵守参数说明。

## 9. 验收结果

2026-11-08，ROOT 与 GPT-6.1-Sol high 分工实现，并交叉审阅对方负责的代码与测试；最终增量审阅无未关闭问题。工具按本规格完成 hard cut，无旧原生入口、脚本旧参数分支或兼容别名。

| 验证批次 | 结果 |
| --- | --- |
| 协作 API、三工具参数、原生收件 Host、协作 PostgreSQL、等待控制、入口权限、Agent Host、统一输入队列、压缩上下文载体 | 151 项测试、15 项子测试通过 |
| 参数合同、入口矩阵、能力目录、默认空值、Code Mode runtime、bundled Skills | 353 项通过 |
| Code Mode 执行、Agent Host、canonical 上下文、Cell 观察、MCP readiness、Code Mode dogfood 合同 | 80 项通过；图像用例以正常 multiprocessing 启动方式单独重跑 1 项通过 |
| 前端 runtime adapter | 167 项通过 |

Python 批次间存在重叠，不合计为独立用例总数。PostgreSQL 使用保存配置所指向的已核实本地服务创建隔离临时数据库，未重置主库或修改保存配置。原生 Host 验证覆盖 ROOT／其他 subagent 来信、追加 Work 不唤醒、正常正文与来源交付、provider frontier 未读判断、stop／close 与槽位释放，以及同 epoch 的 SYSTEM／tools／messages 前缀连续性。真实调度槽的竞争测试验证 0 秒和已有来信检查不释放槽位；API 测试覆盖检查与挂起之间的来信、多个发送方、无关变化、deadline 和取消。
