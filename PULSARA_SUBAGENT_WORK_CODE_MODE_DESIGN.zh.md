# Pulsara：Subagent／Work 与双入口编排设计

日期：2026-10-30。状态：本轮协作运行时已实施；实际验收与未覆盖边界见 §9，暂缓表面仍按配套范围执行。

提示词、profile 文案与前端暂缓范围，以及 bundled Skill 和 Code Mode 工具 schema 要求，见 [暂缓事项与实施范围](PULSARA_AGENT_CODE_MODE_DEFERRED_SURFACES.zh.md)。

本文负责 AGENT／WORK、连续历史、派活、交流、等待、完成及编排示例。通用 JS 执行、工具子集、结果桥接、计算限制和生命周期由 [Code Mode 接入合同](PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md) 唯一定义；两篇共同构成本轮实施合同；示例中的 ID 仍须替换为运行时真实返回值。

当前工具名、查询分支与 CELL 身份以 [工具查询、命名与 Cell 身份统一规格](PULSARA_TOOL_QUERY_AND_CELL_ID_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) 为准。本文接口表与示例采用该合同；所链接的既往实验与实施记录保留其原验证范围，不作为新接口已验证的证据。

运行时状态交接与资源退役以 [runtime handoff 与资源退役规格](PULSARA_RUNTIME_HANDOFF_AND_RESOURCE_RETIREMENT_IMPLEMENTATION_SPEC.zh.md) 为准。handoff 只主动展示物理存活 Process、当前范围未关闭 Monitor、需要继续执行或交付的 Cell，以及有当前／排队 Work 的 Agent；PMCA 状态视图受 3,200 个 V3 启发式 token 约束，完整 TODO 独立保留。未展示的保留对象仍由既有 query 按执行时权限读取；不改变两段退役、请求执行或 provider 前缀边界。

## 1. 核心语义

只有两个协作领域的一等对象：

| 对象 | target | 职责 |
|---|---|---|
| Agent | `{"kind":"AGENT","agent_id":"session:…"}` | 执行者的稳定身份与连续对话历史，包含 ROOT 和 Subagent |
| Work | `{"kind":"WORK","work_id":"queue:…"}` | 已接纳的新轮请求的稳定引用；状态和结果从请求队列与对应 turn 读取 |

`agent_id:"ROOT"` 是保留标识，指向本次协作的 ROOT session；普通 Subagent ID 不使用这个值。统一身份格式不改变各操作的适用对象：消息可以发给 ROOT，派活与委派工作查询仍针对 Subagent。ROOT 不因此成为 `run_agent` 的派活目标。

ID 遵循 [统一身份规格](archived_docs/PULSARA_UNIFIED_IDENTITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)：同一对象在数据库、工具参数、返回值和 UI 中使用同一个完整 canonical ID，格式为 `<kind>:<26 位小写 Base32>`。普通 `agent_id` 使用 session 的完整 `session:` ID，`work_id` 使用 `prompt_queue_items.id` 的完整 `queue:` ID；不截短、不去掉前缀，也不包装为另一份 agent／subagent／work ID。`ROOT` 仅由可信调用上下文解析为本次协作的 ROOT，不需要别名映射表。模型复制真实返回的 target，无需推导 ID 或理解内部请求与 turn 的对应关系。

本文调用示例使用符合格式的示意 ID，不代表实际存在的对象；运行时须替换为真实返回值。概念说明中的 `session:…`／`queue:…` 不是可提交参数。

一次派活接纳一份请求，返回的 `work_id` 就是该请求的稳定 ID。首次派活可以同时创建 Subagent，不要求先创建空执行者。复用时向同一个 Subagent 追加请求。同一 Subagent 按接纳顺序串行处理工作，后续工作可以排队；同一 batch 内按 JavaScript 对象键的枚举顺序确定位置，不为依赖隐式重排。不同 Subagent 可以并行。

同 Agent 的后项只要求前项结束；成功要求仅来自显式 depends_on。前项失败或取消后，未显式依赖其成功的下一项仍可启动。

每个 Subagent 是一份 session，归属由其 `root_session_id` 指向 ROOT；该字段为 NULL 的 session 就是 ROOT。ROOT／SUBAGENT 的 kind 由归属派生，不单独存储。Subagent 的 `agent_id` 直接使用其完整 session ID，Work 是其中一次已接纳的请求和执行。复用 Agent 时，直接读取该 session 的有效摘要与尾部，然后追加下一份请求，复用 ROOT 的 reader、compiler、压缩与 UI 历史读取链。协作接口不提供 Agent／Work 历史的 fork：继续已有上下文就复用原 Agent；新建 Agent 则建立独立 session，不从另一 Agent 或某份 Work 复制对话历史。

已有内容原样保留，包括旧请求、消息、工具结果和已采用的压缩摘要；不根据新 Work 重写旧 snapshot、ROOT 消息或 handling。profile 作为独立 user message 放在本次 instruction 前，不拼入通用 SYSTEM，也不作为开启 entry。ROOT 派活的 instruction 是本次 turn 的开启 entry；各份前置 final 分别作为后续 user entry 追加，明确选择的材料同样由接收方持有，不合并改写 instruction。模型沿 context binding 读取有效摘要与尾部；UI 沿同一 Agent scope 分页展示已接纳的 canonical 历史，可以查看压缩前记录。两者共用事实与既有投影，UI 的历史浏览不把旧全文重新塞回模型输入。

Subagent 仅在 bypass-permissions 下使用，不提供独立的权限模式选择或权限确认入口。共同请求接纳时直接继承并冻结发起者 ROOT 的完整 `ModelCallBinding`（连接选择与 reasoning）及 bypass 权限事实。原生派活取该次 ROOT 调用的快照；JS 派活取启动该 cell 的 exec_cell 快照。排队期间设置变化不改变已接纳请求；新派活可取得新的快照。实际执行沿现有合法 resolver 观察，不永久冻结 catalog、allowance 或凭据；已安装 epoch 的 target bundle 保持冻结。启动时由共同 turn admission 承接这些事实，协作层不增加权限或配置权威。公共接口不提供模型配置枚举工具、独立选模或 reasoning 覆盖参数。

新请求沿 ROOT 的共同接纳与 provider-input 安装链路进入同一个 Agent scope：目标配置相同时沿已安装 epoch 追加；真正切换模型配置时，复用已有模型切换接纳与测量、合法 cold 边界或已采用的压缩 successor。Work 开始、结束及 profile 变化本身不触发重建。已安装 epoch 内 SYSTEM／tools 字节不变，messages 只追加；压缩和模型切换均使用共同 reader、planner 与 coordinator。

turn 结束只结算本轮执行与资源，Agent 的有效历史和已安装 scope 按 ROOT 的连续对话生命周期保留，不随一份 Work 结束而销毁。

新建 Agent 的 `context:last_n` 取发起调用当时的已接受 ROOT 上下文作为初始背景；JS 使用源 exec_cell 的固定截止位置，不读取随后到来的用户消息冒充原始背景。它与 `materials` 都是内容传递，不建立历史分支或继承关系。选定内容进入接收方自己的输入，不依赖源 session 持续存在，也不长期借用旧 provider view。

Subagent 的普通 final 经共同完成链路接纳后，本次 turn 完成；WORK 沿请求关联读取这份 final。完成不是对结论正确性的认证。执行失败、取消、中断读取实际终态与原因。无需结果提交工具。

ROOT 负责分工、判断结果、组织讨论和决定用户目标何时完成。脚本执行已经明确的顺序、并行、等待和结果传递。多人协作与 workflow 不要求创建协作组对象，也不引入专门协调 Subagent。

### 1.1 共用请求队列与 turn

ROOT 与 Subagent 使用同一套 session 请求接纳、排队、turn 执行及上下文引擎。通过 AGENT 定位目标 session 后，使用该 session 的 `prompt_queue_items`；`WORK` 是其新轮请求的公开引用，不另建 Work 状态表、结果表或 WORK→turn 映射表。

协作接口的 WORK 只引用委派给 Subagent 的请求；共用队列表不把 ROOT 的人类输入或 steer 自动变成可查询、等待的 WORK。一次请求执行中的工具调用、压缩与 Hook 续接仍属于原 turn，WORK 引用不变。

| 事实 | 唯一保存位置／读取方式 |
|---|---|
| Agent 身份、显示名称、归属、连续历史范围 | session 及其 `root_session_id` 和既有历史范围；kind 从归属派生 |
| 请求正文、队列位置、接纳时配置、有效 profile、所选材料和委派来源 | 接收 session 的共用请求记录；材料保存实际内容，来源标识仅用于归因，不形成跨 session 外键 |
| 尚未启动、启动前取消／拒绝 | 请求队列状态及实际原因；未启动不创建空 turn |
| 已启动、完成、中断及具体结束原因 | 共用 `turns`；通过请求的 `consumed_entry_id → transcript entry.turn_id` 定位 |
| final | `turn.final_entry_id` 指向的 canonical 内容，展示时按需生成预览／artifact |
| 成功依赖 | 待处理请求中尚未满足的 WORK 等待条件；满足时接纳实际结果正文并清除相应等待条件，不建立跨 session 依赖表或外键 |

启动沿 ROOT 既有事务同时创建 turn 和本次有序输入 entry，并把请求标记为已消费；`turn.initial_entry_id` 与 `queue.consumed_entry_id` 均指向 instruction 的 entry。`CONSUMED` 只表示请求已进入 turn，不表示工作完成。接纳请求时即可给出稳定 WORK 引用；既有队列 ID 也能确定未来 turn ID，无需提前创建 turn 或另存映射。

调度按 session 选取队首：该 Agent 无活动 turn、成功依赖已满足且有实际执行容量时启动。等待依赖不会阻塞其他 Agent。普通派活与 batch 共用这个接纳和调度 owner，batch 只是一次事务提交多份请求及其等待条件。委派请求保留真实来源，不伪装成人类提交或借用 ROOT 的 Plan 状态。

查询、等待、依赖推进、完成通知和 UI 都读取这组事实；运行中的观察使用进程内通知，通知本身不成为第二份工作状态。ROOT 原有即时请求与 steer 使用共同引擎，steer 仍追加到已有 turn，不因此变成新的 Work。

停止 ROOT 当前 turn 不取消独立排队的 NEW_TURN，后者按共同调度继续；Subagent 的单份 WORK 停止与整个 AGENT 停止范围见 §4.5。

ROOT 输入队列既有的待输入限制保持其原适用范围，不把所有 Agent 的请求计入 ROOT 的待输入配额。委派使用本规格的单次接纳边界、分页读取和物理执行容量，容量不足排队。

Host 关闭或接管沿共同队列／turn 语义：已运行 turn 如实中断，未启动的新轮请求保留；重新打开后按依赖和实际可用性判断首次启动。已中断 turn 不自动重跑，失去 VM 的脚本也不恢复。这是既有请求队列的持久性，不增加执行恢复机制。

### 1.1.1 开启 entry 与前置结果的追加顺序

所有 Subagent 的 Work 都以 ROOT 派活时的 `instruction` 作为开启 entry。profile 是身份预设，前置 final 是输入材料；它们有自己的 entry，但不各自开启 turn。以 B 依赖 A、C、D 为例，首次模型输入的业务消息顺序为：

```text
system：运行时组装的系统提示词
user：B 的 profile
user：ROOT 给 B 的 instruction  ← turn.initial_entry_id
user：A 的 final
user：C 的 final
user：D 的 final
```

native tools 位于 provider 请求的工具定义中。复用已有 B 时，本次 user 消息追加到其有效历史末尾，不插入旧 SYSTEM 后面。前置结果按 `depends_on` 的声明顺序排列，不按完成时间排列；每条正文独立保存、独立呈现为 user 消息。共同 runtime observation 仍可在现有输入准备点插入；需要说明材料来源时，最多追加一条简短说明，指出 instruction 后的前置结果及其顺序，不重复正文或逐条包成 JSON。

请求已接纳时就有稳定 WORK 引用。等待前置期间，结果陆续进入 B 自己的待处理请求材料；全部前置成功且具备执行容量后，由共同启动事务建立真实 RUNNING turn、完整初始 entries、初始 binding revision 和 queue 消费关系，再准备首次 provider 输入。首次准备中的压缩、补充与停止由同一个执行 owner 承接。若还有显式 `materials`，同样作为独立输入材料追加。不得先调用模型，再补入本应等待的前置结果。

共同启动与读取链路分别表示 instruction 和整组输入边界：按 ID 定位开启 entry，从启动确认的实际有序初始 entries 确定组首和组尾，前驱截止为组首 sequence 减一。复用现有 reader、compiler、lowering 与接纳事务，写入并确认整组 entry 的顺序和内容，启动后计量和准备完整输入；不能只提交或确认 instruction，也不能用它的序号截掉后续结果。图片能力检查覆盖整组输入。

profile 与前置结果保留真实的非人类来源，不因使用 user role 而被当作人类请求。首次普通 provider 调用前，压缩仅处理完整初始组之前的历史，整组原文和图片保留为新 turn 的精确 suffix；摘要采用目标是本次 RUNNING turn。首调用后的压缩继续遵守既有当前 instruction 精确保留与普通摘要／尾部处理规则。输入边界与共同 safe point 遵守 [压缩实施规格](PULSARA_COMPACTION_MODEL_CONFIGURATION_AND_MANUAL_IDLE_ONLY_IMPLEMENTATION_SPEC.zh.md)。WORK 始终使用原 `queue:` ID，不改成 instruction 的 entry ID 或执行 turn ID。

### 1.2 会话种类、展示与检索

session 表增加可空的 `root_session_id`，作为指向 session 主键的外键，并建立索引。NULL 表示 ROOT；非 NULL 表示 SUBAGENT，且必须指向一份 ROOT session。每个 Subagent 只属于一个 ROOT，并可在该 ROOT 下连续接活；不单建成员关联表，不单独存 kind。对外需要 kind 时，按这个字段派生。

主侧边栏查询 `root_session_id IS NULL`；ROOT 的 `query_agents` 与协作侧栏查询 `root_session_id = 当前 ROOT 的完整 session ID`。Subagent session 存于数据库，通过协作详情查看和管理。该归属用于成员枚举与协作目标范围判断，不限制既定会话检索范围。会话的用户层管理操作不因此成为 Subagent 的工具。

Subagent session 不运行自动标题生成或后续输入建议。显示名称由 ROOT 在新建时通过原生 `run_agent`、`tools.run_agent` 或 `tools.run_agent.batch` 的 `name` 参数填写；复用时沿用已有名称。

不为 Subagent 增设用户侧的工作区选择、手动压缩、上下文用量预览、会话分叉或定时任务入口，也不向其暴露 `query_schedules`、`manage_schedule`。自动压缩和协作详情中的历史、状态展示沿共同引擎处理。用户可以单独停止某份 Work，复用 §4.5 的 WORK 取消与结算链路。

Subagent 不提供 `open_in_pulsara`，其 SYSTEM 不注入面向用户的可视化发布说明。它可以按任务生成图片、HTML 或图表，将文件路径和必要说明交给 ROOT 组织展示。

两种 session 复用现有多会话加载、请求处理、历史读取、压缩及关闭／删除链路。已有 Core 共享服务和每个 HostSession 自己的执行资源保持各自职责，不为 Subagent 另造一套对话引擎或共享服务框架。

ROOT 使用主动会话检索工具和隐式历史召回时，ROOT／SUBAGENT session 的行为一致：采用相同的现有可访问范围、检索、排序和内容读取规则，不因 kind 排除 Subagent，也不把其历史限制为 final。侧边栏过滤不影响检索。当前不为检索工具增加 kind 参数。

Subagent 不提供 `search_sessions`、`search_session_content`、`read_session_content`，也不自动注入记忆、历史会话或响应偏好；任务所需背景由 ROOT 提供。已经进入该 session 的内容仍按普通历史读取和压缩。

规划模式、规划提问与计划审批交互不适用于 Subagent；不提供 `enter_plan`、`ask_plan_question`、`exit_plan`。需要 ROOT 决策时，通过普通协作消息或 final 说明。

设置页的“历史会话检索”开关保持现有语义：关闭时仅关闭隐式历史召回，主动检索工具不受影响；该开关不承担普通记忆或响应偏好的控制。

### 1.3 跨 Agent 传递的是接收方自己的输入

| 行为 | 使用的共同链路 |
|---|---|
| 给正在工作的 Agent 发消息 | 仅走当前 turn 的 steer／补充输入链路；无活动 turn 时通过工具结果返回未接纳，不开启新工作 |
| 派出新工作 | 在目标 session 接纳一条新的用户请求，由具体工作请求开启 turn |
| 交付 final | 把实际正文作为接收方的用户输入；用于后续工作时，分别追加在该轮 instruction 之后，不替代开启 entry |
| 提供已完成工作的 materials | 在接纳新请求时读取实际内容，保存到接收方的输入中 |

目标 ID 用于决定发给谁，来源标识用于说明谁发来的；两者都不要求永久保留发送方。接收后的正文由接收方的队列／entry 持有，不通过源 entry 指针临时展开，不因源 session 删除而消失。内容采用既有 inline／blob 存储时，由接收方自己的引用保持可读。

`root_session_id` 只记录 ROOT 对所属 Subagent 的归属；消息、材料和结果传递不建立发送方与接收方之间的跨 session 外键或保留约束。`depends_on` 是待处理请求的启动条件：其中尚未满足的 WORK 标识用于等待确切工作的结果，不是 session 之间的生命周期关系。前置成功时，把实际结果接纳为后继请求的输入材料并清除对应等待条件；全部满足后，沿共同队列开启 turn。前置失败、取消或中断时，按既定失败依赖语义结束后继请求，不创建空 turn。

查看、等待、停止仍按调用目标定位现存的请求／turn；它们不使目标永久保留。来源删除后，旧目标查询可以返回不存在或不可用，已经交付的正文继续保留在接收方历史中。

### 1.3.1 成功依赖与级联停止

`depends_on` 要求所有前置 Work 均为 `completed`。任一必需前置为 `stopped`，尚未启动的后继请求即以“前置工作未成功”为原因结束为 `stopped`，不创建空 turn；该终态继续传给依赖它的后继。接纳时已有前置终态和接纳后前置结束使用同一规则。

例如 `A → B → C`：A 停止后，等待 A 的 B 停止，等待 B 的 C 也停止。其他独立分支继续执行，已经完成的工作、已交付材料和已发生的修改保持原状。一个前置停止不要求取消其他前置。

级联由共同请求／调度 owner 根据已接纳的等待条件和真实终态推进，不依赖 ROOT 在线或 JS cell 存活；Host 重新接管时也沿同一规则处理待处理请求。查询后继时给出依赖未成功的原因及直接导致停止的前置 WORK 引用，不逐层复制错误正文，不另存 workflow 执行状态。

ROOT 后续向原 Agent 派出 A₂，不改变旧 A、B、C 的终态，也不把旧依赖自动改绑到 A₂。需要继续的后继由 ROOT 重新派活并明确依赖；Agent 的身份和有效历史保持连续。

### 1.4 两段退役与 A／B 的等待、删除

两段退役暂定为：连续闲置满 **1 小时**卸载 Subagent 的常驻运行资源、保留 session 历史；连续闲置满 **3 天**删除该 session，复用现有会话删除及内容回收链路。两个期限都从同一次进入闲置状态起算，不从卸载时重新计算删除期限；它们不是工作执行时长上限。

删除 ROOT session 时，一起删除所有 `root_session_id` 指向它的 Subagent session；归属外键采用 `ON DELETE CASCADE`。删除操作先沿共同关闭链路处理整个归属范围的活动运行资源，再删除会话数据并沿既有内容回收链路清理。单独删除一个闲置 Subagent 不删除 ROOT 或其他成员，已交付到其他会话的正文仍由接收方保留。此处是明确删除操作，下面的闲置判定用于自动退役。

**有工作在等待，就不算闲置。** 只有没有正在执行或等待处理的请求时，才开始计算闲置退役时间。未启动、等待容量、等待前置结果的请求均属于待处理工作，不触发基于闲置时间的卸载或删除。接纳新工作后退出闲置；工作结束且没有待处理请求时重新开始计时，不能只按最后一次模型调用或唤醒时间判断。

原生 `run_agent`、`tools.run_agent` 和 `tools.run_agent.batch` 复用同一按需加载路径：目标 session 存在但已卸载时，仍可接纳新请求，准备执行时自动加载已有历史和运行环境。若需等待前置结果，可先保留请求与材料，到具备执行条件时再加载。已载入且正在等待的 Agent 不因闲置策略卸载；派活前已经卸载的 Agent 则不必为等待立即加载。加载只承接新工作，不恢复已结束的执行或重跑旧 Work，不提供独立恢复工具。目标已物理删除时返回目标不存在，不暗中重建同名 Agent。

| 情况 | A 与 B 的实际状态 | 自动删除语义 |
|---|---|---|
| B 等待 A，A 长时间尚未交付 | 批次接纳后 B 的 session 已创建或复用，工作请求已存在，但本次 turn 尚未启动；A 仍在执行，B 的请求等待结果 | A 有运行中的工作，B 有等待中的工作，两者都不因闲置期限自动删除 |
| A 已把结果交给 B，之后长期不活跃 | 结果正文已属于 B 的输入；B 按自己的队列执行，A 如无其他工作则开始闲置计时 | A 可按退役策略卸载并删除，B 的输入、执行和历史不受影响 |

A 因失败或停止结束时，沿共同执行链路结算并解除 B 的等待；runtime 故障造成的执行状态问题由共同链路处理，不通过闲置删除判断工作是否完成。正常自动退役不会删除仍在执行的前置工作，也不会删除仍有排队请求的后继 session。

### 1.5 共用能力链路与 MCP 提供范围

ROOT 与 Subagent 共用能力发现、`query_capabilities` 查询、刷新、启停、执行检查及 runtime observation 链路。Skill、Hook 与安装来源查询采用共同规则，不另设 Subagent 专用查询禁令。读取目录不授予管理权限，原生工具仍按本文的调用方范围暴露。

文件查找、文本读取与修改、终端执行及进程交互、todo、Skill 发现与使用，以及 `view_image`、`read_artifact`／`export_artifact` 均复用 ROOT 的工具语义和现有执行链路。ROOT 可在 instruction 中提供图片引用或路径，由 Subagent 按需查看。terminal monitor 的投递与生命周期按 §3.4 绑定当前 Work。

扩展、连接和授权配置由用户能力页或 ROOT 的能力管理工具操作。Subagent 不暴露这些管理工具及 `reload_capabilities`；共同刷新、启停链路负责把配置变化反映到各受影响 session，不要求 Subagent 自己修改配置。

Hook 共用 ROOT 的发现、匹配、执行、输出处理与上下文追加链路。用户在配置阶段授权其适用于 ROOT、Subagent 或两者，不逐次询问；Hook 输入如实提供会话身份与输入来源。适用范围由配置决定，不为特定插件增加引擎分支或自动改写插件行为。

MCP 的“为子代理也提供”开关只决定该 MCP 是否进入 Subagent 的能力范围，覆盖其工具、资源、资源模板和 prompt；其余启停、连接、发现及执行规则与 ROOT 一致。目录可展示当前状态，看到条目不等于当前可执行。

手动配置与插件附带的 MCP 使用同一项 `scope_policy` 配置及同一个“为子代理也提供”开关：`ROOT_ONLY` 仅向 ROOT 提供，`ROOT_AND_SUBAGENTS` 同时向两者提供。安装插件时，向用户展示各 MCP 的这项设置并允许审阅、调整；安装后沿共同 MCP 配置入口管理。来源是插件不自动决定向 Subagent 提供，适配器读取用户配置，不硬编码另一套提供范围。

| MCP 对 Subagent 的变化 | 共同链路的行为 |
|---|---|
| 初始不提供 | 不安装其远端工具 schema |
| 后来打开“为子代理也提供” | 正常刷新、发现，在后续输入准备点告知新增能力及当前状态；同 epoch 新发现工具沿既有 META 路由提供 |
| 后来关闭该开关，或禁用 MCP | 告知该能力已不可用，阻止新的调用；已经接纳的操作沿共同执行与结算规则处理 |
| 同一 epoch 已安装过工具 schema | 保持原 schema 字节不变，通过当前路由和观察反映可用性，不改写已安装前缀 |

相关变化由每个受影响 session 的共同通知与输入准备链路处理，在其下一次正常准备输入时追加 runtime observation。一个 session 消费通知不应使其他受影响 session 漏掉变化；通知不单独唤醒闲置 Agent 或创建新 Work，也不引入持久通知、回执或新的 epoch 边界。冷 epoch 与已采用的压缩 successor 继续按共同规则确定工具集合。

初始工具集合内的 MCP 工具使用 native 调用；用户在能力页或 ROOT 通过管理工具带来的同 epoch 新能力，沿 `query_capabilities` 与 META 调用链路渐进披露。两种路径共用当前可用性检查和 append-only runtime observation。即便某 MCP 工具已经作为 native schema 安装，后续禁用或关闭对子代理提供时，也只追加不可用说明并由执行端拒绝新调用，不修改既有 SYSTEM、provider tools 或历史消息。实施范围包含开关变化对 Subagent 的渐进披露，以及插件 MCP 同一提供范围配置的安装审阅与后续管理。

## 2. 两种入口，共用一套操作

原生工具用于直接派活、交流、查看、等待和停止；JS 用于组合这些操作。

原生工具与同名 `tools.<name>(...)` 调用的参数、target、默认值、返回结构、错误以及执行语义一致。JS 专用组合扩展挂在对应操作下：`tools.run_agent.batch(...)` 组合派活，`tools.wait_agent.works(...)` 等待指定工作。batch 可以接收延迟生成参数的函数，最终形成同一底层操作接受的具体参数。业务入口统一使用 `tools` 命名空间，正式输出使用 `console.log`。两种入口进入同一执行 owner，共用权限、Hook、排队、取消和结果保存机制。JS 不通过重新调用模型实现工具执行，也不拥有另一套业务 scheduler。

工具定义维护一份，生成原生 schema 和 JS API 说明。JS 写法统一为：

```javascript
await tools.send_agent_message({
  target: { kind: "AGENT", agent_id: "session:aaaaaaaaaaaaaaaaaaaaaaaaaa" },
  message: "Please also check empty input."
});
```

Code mode 仅供 ROOT 的 bypass-permissions 调用使用，Subagent 没有 JS 入口。ROOT 管理工具要求 bypass；Subagent 在其活动 Work 中使用原生消息和等待工具。运行时从真实调用上下文确定发送者和当前 Work，模型不能自报身份。

## 3. 完整工具及 JS 环境清单

本表只列协作与 code mode 入口，不枚举文件、终端、MCP 等其他工具。

### 3.1 原生与 JS 共有：五个协作工具

| 工具 | 原生 | JS | 原生调用方（JS 均仅 ROOT＋bypass） | 作用 |
|---|---|---|---|---|
| `run_agent` | 是 | `tools.run_agent` | ROOT | 新建或复用 Subagent，提交一份 Work |
| `query_agents` | 是 | `tools.query_agents` | ROOT | 无 target 时分页列 Subagent；AGENT target 分页列 Work；WORK target 只读具体状态和结果 |
| `send_agent_message` | 是 | `tools.send_agent_message` | ROOT、Subagent | 向目标 Agent 的活动 turn 发送 steer；无活动 turn 时返回未接纳 |
| `wait_agent` | 是 | `tools.wait_agent` | ROOT、Subagent | 只接受 AGENT，等指定 Agent 的消息或本次等待绑定的工作结果 |
| `stop_agent` | 是 | `tools.stop_agent` | ROOT | WORK 停止一份工作；AGENT 停止当前工作并取消已排队工作，保留身份和历史 |

`query_agents` 的名字用于协作查询，具体查询对象由 target.kind 决定。`stop_agent` 同样接受 AGENT／WORK，分别指定整个执行者当前已有的工作或其中一份工作；停止不删除执行者。

### 3.2 只有原生入口：三个 code mode 入口

| 工具 | 参数 | 作用 |
|---|---|---|
| `exec_cell` | `code`／`path` 恰一；可选 `yield_time_ms` | 启动一次 JS 执行 |
| `query_cells` | 无 target：可选 `cursor`、`max_items`；CELL target：可选 `offset_chars`、`max_chars` | 分页列存活 Cell，或读取状态与冻结源码；不消费执行输出 |
| `manage_cell` | `action:"wait"`、CELL target，可选 `yield_time_ms`；或 `action:"stop"`、CELL target | 等待后续输出或停止脚本后续执行 |

这三个工具不通过 `tools.*` 嵌套调用。终止脚本不等于取消已接纳的 Work；需要取消工作时调用 `stop_agent`。

Cell 使用 `{"kind":"CELL","cell_id":"cell:…"}` target，ID 是现有 canonical 编码的完整 `cell:` 值；不截短、转换大小写或生成别名。Cell 是脚本执行对象，Agent／Work 仍是协作领域的两个对象。exec_cell／manage_cell.wait 到观察时间后返回，原脚本持续执行；压缩不暂停它，manage_cell.wait 不负责恢复它。每个 cell 独立持有自己的变量和 Promise。跨 cell 使用已经返回的 target 查询真实对象。

### 3.3 只有 JS 环境入口：编排与输出

| API | 作用 | 与原生工具的关系 |
|---|---|---|
| `tools.run_agent.batch(specs)` | 按局部名称声明一批工作，先准备身份，再生成包含队友引用的初始请求，最后接纳工作 | ROOT 专用的 JS 组合扩展，复用 `run_agent` 的创建与派活语义，不是新的原生工具 |
| `tools.wait_agent.works({targets, settle?})` | 持续等待一个或多个指定 WORK 结束；默认等全部，也可等任意一个 | ROOT＋bypass 的 JS 专属扩展，复用工作状态观察与结果读取，不接收 AGENT |
| `console.log(...values)` | 将脚本选择的文本或结构化结果作为正式输出返回给调用模型 | 不可丢弃的模型输出通道，不创建工作，不代表业务完成 |
| `Promise.all`／`Promise.allSettled`、变量、条件、循环 | 标准 JS 组合能力 | 不新增工具 schema 或 workflow DSL |

`console.log` 是正式输出，按顺序由 exec_cell／manage_cell.wait 交给 ROOT；不静默丢弃。序列化、结果分页、artifact 与资源失败由 Code Mode 接入合同规定，协作接口不另设输出通道。

`tools.wait_agent.works` 直接订阅工作状态，不后台轮询原生工具。等待期间没有 JS 计算，也不占模型调用、Agent 执行名额或旧 provider surface。Work 结束后 Promise 正常完成，脚本立即执行下一行，不需要 ROOT 再调用 manage_cell.wait。

### 3.4 记忆、终端监控与完成方式

这些是 Subagent 的公共工具暴露规则，不增加协作对象或另一套 JS 权限。

| 能力 | ROOT | Subagent | 两种入口 |
|---|---|---|---|
| `search_memory`、`get_memory` | 原生保留 | 不提供 | 不进入 JS |
| `remember`、`mark_memory_relation` | 原生保留 | 不提供 | 不进入 JS |
| `search_sessions`、`search_session_content`、`read_session_content` | 原生保留；两种 session 同等检索与读取 | 不提供 | JS 暴露沿配套 Code Mode 合同，调用方仅 ROOT |
| `query_monitors`、`manage_monitor` | 原生保留全局查询与管理能力 | 原生查询、注册、取消当前 Work 的监控 | 不进入 JS |
| 工作完成 | ROOT 接收并判断结果 | 普通 final 完成本次 Work | 不需要额外结果提交调用 |

Subagent 不自动注入记忆、历史会话和响应偏好。ROOT 提供任务所需背景；已经进入 Agent 有效历史的记忆文字与工具结果照常保留，不因工具暴露变化改写历史。ROOT 的记忆能力与设置规则保持，历史检索覆盖两种 session，见 §1.2。

monitor 记录注册者 AGENT，投递目标与订阅生命周期绑定注册时的当前 WORK，归属由实际调用上下文确定。模型不填写任意接收 Work。公开参数继续使用已有 PROCESS／MONITOR target；AGENT／WORK 是协作对象范围，不替代终端工具自己的目标类型。

监控继续使用 ROOT 已有的 manager、注册容量与重复订阅裁决、采样／过滤、通知接纳与结算、压缩状态读取和 UI 投影。仅把接收范围参数化为真实 Agent／turn，并按该请求的 turn 终态关闭成员订阅；不另建 Subagent monitor manager 或通知记录链。

- Subagent 查看、取消自己当前 Work 的 monitor；ROOT 保持同 Host 全部 monitor 的查看、取消能力。
- 进程来源与监控注册者是两回事。注册仍遵循原有进程可访问范围，不额外限制只能监控自己启动的进程。
- 通知在注册 Work 的正常输入准备点追加；模型调用和已冻结输入不被临时改写。压缩交接保留当前 Work 的监控状态。
- 注册 monitor 不阻止 final。Work 真正完成、失败、取消或中断后关闭订阅；既有完成 Hook 要求继续时，仍按活动 Work 处理。
- 关闭订阅不停止被监控进程。未接纳通知不再投递，已接纳通知保留在历史；已冻结的 immutable attempt 先沿原精确确认路径结算，不转投下一份 Work。UNKNOWN 保留原 attempt，结束该次投递尝试；注册 token、关闭处理与 in-flight 全部结算后由原 coordinator 删除 CLOSED 登记，不保留关闭历史。
- 同一 Agent 开始下一份 Work，继续使用本 session 的有效历史，不接管上一份 Work 的活跃订阅；需要时重新注册。
- Subagent 的 monitor 不自动创建新 Work。ROOT 原有活动轮投递与空闲时开启新轮的行为保持。

例子：同一 Agent 在第一份 Work 注册进程监控并接收进展；本轮 final 后订阅结束，进程可以继续运行。它在下一份 Work 中能看到已经进入历史的进展，但需要新的订阅才能接收后续通知。

普通 final 使用 ROOT 的 assistant settlement 与 turn 完成链路：共同完成 Hook 和待交付输入裁决后，写入真实 final entry 并更新 turn。完成通知、等待和成功依赖读取这一已接纳结果，不再次写一份 Work 成功报告。Hook 的适用范围与 matcher 保留真实调用方；Hook 要求继续时 turn 仍活动。`report_agent_result` 的工具定义、专用执行／结算和 Skill 指导随 hard cut 一并移除，完成不需要额外入口。

查询、依赖和 materials 读取确切 Work 的真实完成内容，不能把有界 summary 改名为完整 final。展示预览和完整材料区分；跨 session 交付时接收方保存完整正文，较大正文复用自己的既有内容存储，不以源会话的 artifact 链接替代实际交付。

## 4. 参数与结果合同

### 4.1 `run_agent`

| 参数 | 含义 |
|---|---|
| `instruction` | 必填文本，本次工作请求；需要看图时在文本中提供图片引用或路径 |
| `target` | 可选 AGENT，指向已有 Subagent；省略时创建新 Subagent，提供时复用；不接受 `agent_id:"ROOT"` |
| `name` | 仅新建时可选的显示名称，由 ROOT 填写；不调用自动标题生成 |
| `profile` | 可选身份／工作方法预设，作为独立 user 消息放在 instruction 前；新建默认通用方法，复用默认沿用原方法 |
| `context` | 仅新建时可选：`{"mode":"none"}` 或 `{"mode":"last_n","turns":1}`；turns 沿用 1–3，省略为 none |
| `materials` | 可选 WORK target 数组，接纳时读取已结束工作的 final 或公开失败材料，作为本请求自己的输入保存 |
| `depends_on` | 可选 WORK target 数组，作为本请求尚未满足的启动条件；取得成功结果正文后清除相应条件，全部满足才启动 |

原生 run_agent 与 JS／batch 的 instruction 均只传文本，不增加派活图片参数。ROOT 在文本中给出可访问的图片引用或路径，由 Subagent 按需调用原生 `view_image` 查看；引用文字本身不等于模型已经看到了图片。

profile 先沿用现有五种工作方法，具体文本另行审阅。每份 Work 在接纳时确定其有效 profile：显式提供时采用该值；省略时继承该 Agent 最近一份已接纳 Work 的有效方法，新 Agent 使用通用方法。同一 batch 按 Object.entries(specs) 顺序解析。后续取消某份 Work 不改变其他已接纳 Work 的有效 profile，也不改写旧消息。模型不用搬运旧 profile 文本。

`materials` 仅提供材料，不等待成功；`depends_on` 表达真实成功依赖，前置失败会阻止该 Work 启动。已经成功的前置工作在接纳时即可交付正文；尚未结束时保留等待条件。两者引用同一 Work 时，沿用依赖结果优先的规则。instruction 保持独立的开启 entry，各份前置结果在其后分别追加为 user entry，具体顺序见 §1.1.1。程序也可在取得结果并判断后再派下一份工作；不要求所有流程先构造成完整依赖图。

接纳后立即返回，不隐含“等待到 final”：

```json
{
  "agent": {"kind":"AGENT","agent_id":"session:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "work": {"kind":"WORK","work_id":"queue:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "status": "running"
}
```

返回时已完成可以附带 final；调用者仍按实际 status 判断。公开状态是共同请求与 turn 的只读投影，不持久化另一份 Work.status：

| 公开 status | 事实来源 |
|---|---|
| `queued` | 请求已接纳、尚未开始；包括串行排队、等待容量或等待成功依赖 |
| `running` | 请求已消费，对应 turn 为 RUNNING；包括工具／消息等待及现有临时重试 |
| `completed` | 对应 turn 正常完成，final 从其 entry 读取 |
| `stopped` | 请求启动前被取消／拒绝，或对应 turn 中断；保留真实 `terminal_reason` |

`completed` 和 `stopped` 为终态。只有 stopped 带终态原因，其他状态的 `terminal_reason` 为 null。继续工作接纳新请求，不改变旧请求终态。共同读取合同见[统一请求执行四态规格](archived_docs/PULSARA_REQUEST_EXECUTION_STATUS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。队列交付和内部 turn 状态继续各自承担权威事实；公开四态不单独存储。

派活成功是接纳成功，不是工作成功。查询和等待正常返回失败 Work 的状态，不因 Work 失败而把这次查询变成工具异常；工具参数、调用权限或执行失败则走共同 ToolError 合同。ROOT 可以继续独立工作，也可以等待。

### 4.1.1 `tools.run_agent.batch(specs)`：先解析引用，再启动工作

`specs` 是以本批次局部名称为键的对象，每项是一份 `run_agent` 工作说明。返回对象保持相同的键，每项返回普通 `run_agent` 的 agent、work、status。局部名称只用于脚本内引用，不形成协作组、持久成员关系或第三种 target。

```javascript
const { a, b } = await tools.run_agent.batch({
  a: {
    name: "Module A developer",
    instruction: ({ b }) =>
      `Implement module A. Coordinate the interface with the module B developer: ${JSON.stringify(b.agent)}.`
  },
  b: {
    name: "Module B developer",
    instruction: ({ a }) =>
      `Implement module B. Coordinate the interface with the module A developer: ${JSON.stringify(a.agent)}.`
  }
});
```

执行顺序：

1. 读取全部成员说明。省略 target 的项预分配新 AGENT 引用；已有 AGENT target 的项复用执行者。为各次新轮请求预分配稳定 ID，作为 WORK 引用。准备阶段只持有临时参数与引用，不创建空 Agent、请求或 turn。
2. 构造按局部名称索引的引用对象，如 `{a:{agent,work}, b:{agent,work}}`。每项提供将用于接纳的确切引用，不表示新对象已经存在，也不提供尚未产生的结果。
3. 以同一份引用对象求值延迟参数，生成完整初始请求；检查全部参数、成功依赖以及同一 Agent 的队列先后关系。
4. 共同请求接纳 owner 在一次事务中写入所需的新 Subagent session 及其 `root_session_id`、整批请求及各请求的等待条件。整批接纳成功后唤醒共同调度；启动遵循实际容量、Agent 队列和成功依赖条件，不建立成员之间的 session 依赖关系。

因此，成员第一轮就能看到 ROOT 明确写入的队友身份、职责与交流目的。ROOT 不必先查询 ID、复制到提示词，再发介绍消息。helper 不自动广播全体成员信息，也不自动给双方添加讨论义务。

只有 instruction 与 depends_on 支持延迟求值；materials 始终是已结束工作的普通引用数组：

| 字段 | batch 支持的值 | 求值结果 |
|---|---|---|
| `instruction` | 字符串，或 `(peers) => string` | 本次请求正文 |
| `materials` | WORK target 数组 | 已结束工作的材料引用；本批次尚未完成的 Work 使用 depends_on 表达结果依赖 |
| `depends_on` | WORK target 数组，或 `(peers) => WorkTarget[]` | 成功前置依赖；可以引用同一批次的其他 Work |

参数函数同步生成参数，返回 Promise／thenable 不合法。peers 及其中 target 深冻结，只有 agent／work；使用 Object.entries(specs) 的标准顺序。求值函数的同步调用栈内，公共工具 bridge 拒绝工具调用，避免参数准备顺带派活；抛错使本批不接纳。这里不承诺 JavaScript 纯函数或回滚作者对自己局部变量的修改，函数自行安排的其他异步代码也不属于 batch 事务。

沿用现有单次接纳边界：每批至多 16 项，每项 depends_on／materials 各至多 16 项，单批显式依赖至多 64 条。它们约束一次请求，不限制累计 Agent／Work 数；后续可继续派活。目标在参数准备期间消失或状态改变时，事务接纳按最新真实事实裁决，不预先启动成员。

真实结果仍由等待或查询取得。普通 `tools.run_agent(...)` 和原生 schema 只接收具体字符串与 JSON 值。所有工具与 batch 的最终参数使用同一严格本地校验；不依靠 provider 是否完整实施 JSON Schema。

队友身份引用不等于成功依赖。两份 instruction 可以相互引用 agent 而并行执行。启动顺序同时受 depends_on 和同一 Agent 的串行队列约束：同一 Agent 按接纳顺序追加，同批按 Object.entries(specs) 顺序确定位置，不因依赖自动调换顺序。将两种先后关系合起来会形成等待环的请求，在接纳前拒绝。

例如同一 Agent 的两份工作，先产出再使用是合法的：

```javascript
// agentX 是已有的真实 AGENT target。
await tools.run_agent.batch({
  produce: { target: agentX, instruction: "Produce the proposal." },
  use: {
    target: agentX,
    depends_on: ({ produce }) => [produce.work],
    instruction: "Review the proposal from the prerequisite."
  }
});
```

下面的顺序不能接纳，因为 use 排在 produce 前，却要求等待 produce；应由 ROOT 调整声明顺序，不由调度器暗中重排历史：

```javascript
await tools.run_agent.batch({
  use: {
    target: agentX,
    depends_on: ({ produce }) => [produce.work],
    instruction: "Review the proposal from the prerequisite."
  },
  produce: { target: agentX, instruction: "Produce the proposal." }
});
```

所有初始参数生成完成后才开始本批次执行，不等于所有成员在同一时刻进入模型调用。排队、前置依赖和工作结束仍会影响消息接收方是否活动；引用存在不承诺对方一直在线。

batch 成功返回表示整批请求已经接纳，不表示 final 已产生。参数函数抛错、校验失败或接纳事务失败时，本批不产生新的 Agent 或请求，也没有成员开始执行；已存在的 Agent 保持原状。提交结果尚不能确认时沿 ROOT 既有精确接纳确认链路核实，不能直接断言未接纳并重派。

整批接纳后，各 Work 独立执行：某份工作失败按实际结果结束，成功依赖它的工作按依赖失败结束；不回滚其他已发生的执行。接纳原子性不等于执行原子性，也不要求所有成员同时进入模型调用。

普通 run_agent 和 batch 都使用从 ROOT 推广的共同请求接纳 owner；普通调用是一项请求，batch 是同一事务中的多项请求。batch 不能仅实现成逐项调用 run_agent 的 Promise.all，也不向公共 schema 暴露预分配 ID 字段。不新增批次领域对象或独立 workflow scheduler。

### 4.2 `query_agents`

- `query_agents({cursor?, max_items?})`：返回 agent target、显示名称、当前工作与排队概览、`next_cursor`。不默认输出全部历史。
- `query_agents({target: AGENT, cursor?, max_items?})`：查看指定 Subagent，以及分页的工作请求、状态和结果概览；本委派查询入口不接受 `agent_id:"ROOT"`。
- `query_agents({target: WORK})`：查看一次工作及其实际结果。返回所属 agent、instruction、status、terminal_reason，以及完成时的 final 或停止时的诊断；依赖未成功时说明直接前置 WORK，见 §1.3.1。

排队管理只提供上述查看及 `stop_agent(WORK)` 取消，不提供将已接纳 Work 转为当前 turn 的 steer。新的补充通过 send_agent_message 独立发送，不隐含修改、消费或取消已有排队请求。

WORK 示例：

```json
{
  "target": {"kind":"WORK","work_id":"queue:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "agent": {"kind":"AGENT","agent_id":"session:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "status": "completed",
  "final": "The parser reads the previous field name. See parser.py:42."
}
```

final 通过这个请求对应的 turn 定位，不取“该 agent 最新的回答”。list／inspect／wait 共用请求→turn 的读取与状态投影，沿 ROOT 已有队列查询关系实现。结果长度与 artifact 引用复用公共工具输出规则，截断展示不冒充完整 final；`materials` 由运行时读取实际结果，不要求模型复制展示预览。

### 4.3 `send_agent_message`

同一参数形状，通过 target.agent_id 指定接收者：

```json
{"target":{"kind":"AGENT","agent_id":"session:aaaaaaaaaaaaaaaaaaaaaaaaaa"},"message":"Please also inspect null input."}
```

```json
{"target":{"kind":"AGENT","agent_id":"ROOT"},"message":"The two approaches change different public behaviors; a decision is needed."}
```

接收者统一由 AGENT target 表达；`agent_id:"ROOT"` 指本次协作的 ROOT。发送者从真实 invocation 取得。消息正文作为接收方用户输入，复用 ROOT 的 steer／补充输入 owner 和输入安全点；来源仍标识实际 Agent，不冒充人类授权，不另建跨 session 消息依赖。

返回形状为 `{"recipient":AGENT,"work":WORK或null,"delivery":"queued|not_started|inactive","message":"接纳结果说明"}`。delivery 表示本次消息接纳结果，不是 Work.status；发给 ROOT 时 work 为 null。queued 表示已接纳为活动 turn 的 steer，不等于已阅读；not_started 表示只有排队工作、尚无活动 turn；inactive 表示已完成工作、空闲或已卸载。后两者均表示发送失败、正文未接纳，通过正常工具结果明确返回，不伪装成已排队等待回复。

AGENT target 在接纳消息时解析到接收方当前正在执行的 turn；Subagent 同时返回实际接收 work 的 target。ROOT 和 Subagent 使用同一规则：没有活动 turn 就不接纳，不自动加载、不开启新 Work，也不保存为下一轮输入。已接纳消息不因 Agent 开始下一份工作而改投；与完成发生竞态时，由共同输入／完成 owner 裁决并返回实际结果。

queued 表示进入当前 turn 的共同待输入链路；正文在既有输入安全点接纳为接收方自己的用户消息并追加，不重写已安装历史。消息的保存与交付沿共同 steer owner，不另造一份消息存储。目标已卸载时的返回示例：

```json
{
  "recipient": {"kind":"AGENT","agent_id":"session:bbbbbbbbbbbbbbbbbbbbbbbbba"},
  "work": null,
  "delivery": "inactive",
  "message": "发送失败：接收方已卸载，当前没有正在执行的工作；消息未接纳。"
}
```

调用者依据 delivery 判断是否发送成功；未接纳时不能假定问题已经送达并等待这条消息的回复。需要接收方开始新工作时，由 ROOT 通过 run_agent 派活。

普通 final 与消息接纳由共同 turn／输入 owner 裁决：正常完成时，消息先进入当前 turn 的待收队列则在原 turn 处理后再判断完成；终态先成立则返回未接纳。停止 turn 时，尚未消费的 steer 按共同链路拒绝，已经进入历史的内容保留，均不转投下一份请求。完成交付使用真实 final 正文，作为接收方的用户输入；作为后继工作材料时通过其已接纳请求交付，并分别追加在 instruction 后，不借助 send_agent_message 唤醒空闲 Agent。接收方输入是已交付内容，不是另一份 Work 结果状态或报告表。

接纳时确定接收方的当前请求／turn，消息不转投接收者的下一份 Work。发送者标识仅作归因；发送方结束或 session 被删除都不撤销已交付正文，不需要跨 session 外键。重复发送是新的消息，不按正文去重；不新增送达／阅读回执。

直接交流所需的对方 target、职责与问题由 ROOT 提供。成员不会仅因同时运行就自动获得全体通讯录。Subagent 使用原生消息工具即可，不需要为单条消息编写脚本。

### 4.4 AGENT 等待与 JS 专属 WORK 等待

ROOT 与 Subagent 都可以调用原生 `wait_agent`；ROOT＋bypass 还可以调用同参数的 `tools.wait_agent`。两种入口只接收 AGENT。精确等待 WORK 使用 ROOT 的 JS 专属 `tools.wait_agent.works`。Subagent 等待时保持当前 Work 活动，不输出 final、不创建或结束 Work。

| 接口 | targets | 等待含义 |
|---|---|---|
| 原生 `wait_agent`／JS `tools.wait_agent` | `AGENT`，普通 Subagent | 对方发来新消息，或本次等待绑定的工作结束 |
| 原生 `wait_agent`／JS `tools.wait_agent` | `AGENT`，`agent_id:"ROOT"` | 等 ROOT 发给调用者的新消息；ROOT 不被虚构成一份可等待的 Work |
| JS `tools.wait_agent.works` | `WORK` | 精确等待指定工作结束并取得结果，不随 agent 后续工作变化 |

等待消息或结果：

```json
{
  "targets": [{"kind":"AGENT","agent_id":"session:bbbbbbbbbbbbbbbbbbbbbbbbba"}],
  "timeout_seconds": 30
}
```

在 JS 中精确等待一份工作：

```javascript
console.log(await tools.wait_agent.works({
  targets: [{ kind: "WORK", work_id: "queue:aaaaaaaaaaaaaaaaaaaaaaaaaa" }]
}));
```

等待多份工作中的任意一份结束：

```javascript
const result = await tools.wait_agent.works({
  targets: [
    { kind: "WORK", work_id: "queue:aaaaaaaaaaaaaaaaaaaaaaaaaa" },
    { kind: "WORK", work_id: "queue:bbbbbbbbbbbbbbbbbbbbbbbbba" }
  ],
  settle: "first"
});
console.log(result);
```

**目标绑定。** 开始等待时，用共同请求／turn 读取将普通 AGENT 绑定到其当前 turn 对应的 WORK；若没有活动 Work、工作已排队但尚未启动，立即返回 `state:"unavailable"`，在该目标的诊断中给出 `reason:"not_started"`，不挂起等待、不启动对方。若没有未结束工作，则可以返回最近一次已结束 Work 的实际结果。既没有工作也没有待交付消息时，立即返回该 Agent 无工作可等待的状态。已有匹配消息先按新消息条件交付，不因对方已经结束而漏掉。

返回所绑定的 WORK 引用，不在一次等待中追随下一份工作。需要精确等待已排队或指定的工作时，在 JS 中调用 `tools.wait_agent.works`；它可以等该 Work 按正常调度启动并结束，但等待本身不改变其启动条件。ROOT 只作为消息来源，没有 Subagent Work，不套用上述 not_started 判断。

**新消息。** 等待接纳时先检查发给当前调用者、来自所选 Agent、尚未进入模型上下文的消息，再订阅后续到达；发送与等待之间已经到达的回复不能漏掉。收到消息即满足消息条件，不要求 reply_to，也不把消息自动认定为问题已解决。消息正文沿正常消息链路进入当前上下文，工具结果只给出唤醒原因和发送者引用，不再复制正文。不订阅双方所有通信或读取对方私有对话。

**不通知对方。** wait_agent 只观察调用者的收件消息与指定工作状态，不自动向目标发送“有人在等你”、不创建问答请求、不启动对方工作。需要对方做什么由 send_agent_message 明确表达。UI 可以展示等待状态，但不因此增加模型输入。

**AGENT 集合与返回。** wait_agent 的 targets 必填且非空，每项均为 AGENT。来自任一所选 AGENT 的新消息会立即返回 `message_available`，不受 settle 限制。没有匹配消息时，默认 settle=all，只有每个目标都绑定了已结束的 Work 才返回 settled；settle=first 则只需任一绑定 Work 结束。ROOT 只有消息条件，不被排除出 all 的目标列表，也不虚构完成的 Work。没有可等待工作的普通 AGENT 返回 `unavailable` 及该目标，不进入无对象的等待。

例如等待 `[ROOT, AGENT B]`，B 绑定的工作已结束但 ROOT 没有消息：all 继续等待 ROOT 消息或超时，first 可以因 B 的工作结束而返回。纯 ROOT 等待只由 ROOT 新消息、超时或执行中断结束，不因空工作集合自动完成。任何模式下，一条匹配消息都立即唤醒，不要求收齐所有人的消息。

state 为 message_available／settled／timeout／interrupted／unavailable。返回保留所解析的引用和真实结果；not_started 等目标诊断不混入 Work.status。示例字段如下：

```json
{
  "state": "settled",
  "bindings": [
    {
      "target": {"kind":"AGENT","agent_id":"session:bbbbbbbbbbbbbbbbbbbbbbbbba"},
      "work": {"kind":"WORK","work_id":"queue:bbbbbbbbbbbbbbbbbbbbbbbbba"}
    }
  ],
  "works": [
    {
      "target": {"kind":"WORK","work_id":"queue:bbbbbbbbbbbbbbbbbbbbbbbbba"},
      "agent": {"kind":"AGENT","agent_id":"session:bbbbbbbbbbbbbbbbbbbbbbbbba"},
      "status": "completed",
      "final": "The interface includes an errors array."
    }
  ],
  "pending_targets": []
}
```

message_available 另给 `message_sources`（匹配的 AGENT target 数组），实际正文由正常消息投递。works 是已绑定工作的当前状态和可用结果；ROOT 不占一个伪造的 work 项。失败、取消、中断、依赖失败等终态满足“工作结束”，但不冒充 completed。pending_targets 列出当前尚未满足的目标；发生消息唤醒后由模型判断是否继续等待。

**等待期间。** timeout_seconds 默认 30，范围 0–300，沿用当前单次观察边界；0 为立即查询；超时只结束这次等待，不停止当前 Work 或目标 Work，也不重新发送问题。调用者可以再次等待。等待期间不空转模型，不占用模型调用与普通工具执行资源；让出物理执行名额，使需要回复的队友可以推进。调用者仍占据自己 Agent 的当前 Work 串行位置，同一 Agent 的下一份 Work 不得越过它；恢复时重新取得执行名额。不能通过把等待中的 Work 当成已结束来腾出容量。Subagent 调用者的 Work 进入任一终态时，结束归属于它的尚存等待；同一 Agent 的后续 Work 不接管该等待。ROOT 没有这里的 Subagent Work 生命周期，不能据此把 ROOT 的普通 final 当成删除 code mode cell 的指令。

未被选中的普通消息沿正常收件链路排队，不仅因到达就提前满足这次等待；显式用户 steer、取消等控制沿既有行为处理。ROOT 需要在等成员结果时接收其求助，应使用 wait_agent；`tools.wait_agent.works` 只等待指定工作结束，普通消息不满足它的完成条件。

**ROOT 等待指引。** 原生 wait_agent 与 JS tools.wait_agent 的说明共同包含：

> Wait for a known subagent's message or current work result. Do not wait for ROOT unless ROOT has explicitly asked you to wait for its reply.

这是一条提示词使用约定。schema 和执行端不因 agent_id 为 ROOT 而拒绝等待，也不增加“ROOT 已同意”的验证参数或特殊授权分支。向 ROOT 发消息始终与等待分开。

**JS 专属 WORK 等待。** `tools.wait_agent.works({targets, settle?})` 仅向 ROOT＋bypass 开放，targets 必填且非空，只接受一个或多个 WORK。每个引用始终指向确切的一次派活；已排队、等待依赖的工作也可以等待，启动仍由正常调度与依赖条件决定。等待本身不通知或启动对方。

默认 `settle:"all"`，所有指定工作进入终态后返回；`settle:"first"` 在任意一个指定工作进入终态后返回，其余工作继续运行。成功、失败、取消、中断和依赖失败都算结束，结果保留真实状态与可用 final。已经满足条件时立即返回。普通消息不使这个 Promise 完成。

该接口订阅共同请求／turn 的变化，醒来重新读取其真实状态；不设 timeout_seconds，也不在后台循环调用 wait_agent。订阅只负责唤醒，不保存另一份 Work 状态或结果。正常返回 `state:"settled"`、`works`（指定工作的状态与可用结果）及 `pending_targets`（尚未结束的 WORK 引用）。它不解析 AGENT 当前工作，不需要 AGENT bindings 或 message_sources。first 判断的是当前集合状态，不消费完成事件；继续等待剩余工作时，使用返回的非空 pending_targets。

JS 的 AGENT 等待匹配消息后立即解决 Promise，脚本继续；消息正文仍走 ROOT 正常输入链，不要求模型先读取。为了连续脚本不会反复收到同一个唤醒，每个 cell 按发送者保存已返回的消息序号，并结合 ROOT 已进入上下文的消息位置过滤。这个游标仅在进程内存在，不是阅读回执或新的消息确认状态。

同一消息可唤醒多个已经登记的观察者；它们不互相消费消息。一旦匹配，结果就固定，不因稍后模型接纳消息或另一个等待返回而丢失。后续串行调用不重复命中同一消息，也不会因先收到 A 的高序号消息而跳过 B 尚未观察的低序号消息。

两种 JS 等待都独立于 exec_cell／manage_cell.wait 的观察超时与 ROOT 压缩。明确终止 cell 时取消它的观察；目标 Work 不因此取消。新一次 AGENT 等待重新解析当前 Work，若需继续等原来的工作，将 bindings 中的 WORK 传给 `.works`。

### 4.4.1 接纳、等待与并发的实施约束

- 协作 target 在当前 ROOT 的协作范围内解析到真实 session／请求；它们不必属于同一个 session 或 Host。不存在或不可访问的引用是明确错误，不伪造一个空闲 Agent。该路由范围不改变 §1.2 的会话检索规则，也不建立要求保留源 session 的数据库关系。
- 不能等待自己当前的 Work：普通 Subagent 等待自己的 AGENT 会要求自己结束后才能继续，返回明确参数错误。等待 ROOT 仍只受上述提示词约定约束。
- 接纳检查“成功依赖边＋同一 Agent 串行边”的组合环；不尝试证明自然语言交流不会死锁。等待未启动成员如实返回 not_started，ROOT 可以调整安排。
- 多个 Work 同时接纳时，由数据库接纳顺序确定每个 Agent 队列位置。工作总数不受并发槽数限制；容量不足排队。等待消息的 Work 让出实际执行槽，但不释放该 Agent 的串行位置。
- batch 不是一组依次提交的原生调用。其内部 descriptor 为 run_agent.batch，保存最终具体参数；每项使用 run_agent 的校验和 Hook matcher，以 batch invocation＋member key 归因。Hook 改写后重查依赖与队列；如果更改预分配目标身份，整批在接纳前拒绝，不能重算已写入其他 instruction 的引用。任何一项未获接纳则整批不提交。提交后独立执行，PostTool 失败附真实诊断，不能回滚或误报已接纳的工作。完整调用来源见配套接入合同。

### 4.5 `stop_agent`

只接受 target，不提供 reason 参数。原生与 JS 的参数和停止语义一致；调用方为 ROOT，AGENT target 指向 Subagent，不接受 `agent_id:"ROOT"`。

| target | 停止范围 |
|---|---|
| WORK | 只停止指定请求：未启动则取消，已启动则停止对应 turn |
| AGENT | 停止该 Subagent 当前运行的工作，并取消本次停止操作所覆盖的已排队工作，包括等待前置结果的请求 |

停止一份工作：

```json
{
  "target": {"kind":"WORK","work_id":"queue:bbbbbbbbbbbbbbbbbbbbbbbbba"}
}
```

停止一个 Agent 当前已有的工作：

```json
{
  "target": {"kind":"AGENT","agent_id":"session:bbbbbbbbbbbbbbbbbbbbbbbbba"}
}
```

WORK 从接纳到结束始终使用同一个 `queue:` ID。未启动时直接读取队列请求；启动后沿 `queue.consumed_entry_id → instruction entry.turn_id → turn` 定位执行，复用共同 turn 取消与工具结算链路，不另建映射、不要求模型改传 turn ID。这些关联均属于该 session 内部。

已完成、失败、中断或取消的 WORK 返回其实际终态，并明确本次没有执行停止；不改写状态、final 或历史。AGENT 停止只处理当前运行和已排队的工作，不处理历史终态工作；目标没有未结束工作时返回当前无工作需要停止。

共同请求／调度 owner 裁决停止与派活、启动、完成的先后关系，确定本次覆盖范围；停止 AGENT 时不能在结束当前工作后又启动本次已覆盖的排队请求。返回实际停止、取消的 WORK 及各自状态，已经结束或未能停止的情况如实说明，不把请求停止等同于所有执行已经结束。停止操作之后新接纳的工作仍可正常执行，不为 Agent 设置永久停用状态。

停止 WORK 不隐含取消同一 Agent 的其他工作；显式依赖该工作成功的后继沿既定依赖失败语义处理。停止 AGENT 会取消其本次覆盖的排队工作。两者都保留 session 和历史，不撤销已发生的修改，也不一律杀死后台终端进程；工具取消与副作用结算沿共同链路，当前 Work 的 monitor 随其真实终态关闭。没有运行或待处理请求后，Agent 开始闲置计时；以后可继续派活。

## 5. 单 Subagent：全部使用原生工具

### 5.1 调查

调用 `run_agent`：

```json
{
  "name": "Parser investigator",
  "instruction": "Investigate failures caused by empty input. Report the cause and code locations; do not edit files."
}
```

假设返回 `session:aaaaaaaaaaaaaaaaaaaaaaaaaa` 与 `queue:aaaaaaaaaaaaaaaaaaaaaaaaaa`。这些 ID 只是示例；实际调用必须复制真实返回值。

中途补充，调用 `send_agent_message`：

```json
{
  "target": {"kind":"AGENT","agent_id":"session:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "message": "Also distinguish missing input from an explicit null value."
}
```

调用 `wait_agent`：

```json
{
  "targets": [{"kind":"AGENT","agent_id":"session:aaaaaaaaaaaaaaaaaaaaaaaaaa"}],
  "timeout_seconds": 30
}
```

成员发来消息时 ROOT 处理并回复，然后可再次等待；成员先完成时直接取得相应 Work 的 final。超时后工作继续，ROOT 可以继续其他工作，再等待。

### 5.2 复用原执行者

ROOT 读完结果后调用 `run_agent`：

```json
{
  "target": {"kind":"AGENT","agent_id":"session:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "instruction": "Continue from your investigation and check whether the same issue exists in batch input."
}
```

返回同一个 agent target 和新的 work target。原调查历史继续可用，不需要把上次 final 再作为自己的 materials 重复注入。

如果这个 Agent 已因闲置满 1 小时而卸载，上述调用仍使用同一个 target：先接纳新请求，执行前自动加载原 session。如果 session 已因闲置满 3 天而删除，返回目标不存在，ROOT 可按需要新建 Agent。没有独立 resume 调用。

## 6. 多人协作：初始请求介绍队友，原生工具交流

以下代码放入 `exec_cell.code`；也可保存为普通 JS 文件后使用 `exec_cell.path`。

```javascript
const { a, b } = await tools.run_agent.batch({
  a: {
    name: "Module A developer",
    instruction: ({ b }) =>
      `Implement module A. The module B developer is ${JSON.stringify(b.agent)}. Agree on parse(input) -> Result before integrating the modules.`
  },
  b: {
    name: "Module B developer",
    instruction: ({ a }) =>
      `Implement module B. The module A developer is ${JSON.stringify(a.agent)}. Agree on parse(input) -> Result before integrating the modules.`
  }
});

console.log({ moduleA: a, moduleB: b });
console.log(await tools.wait_agent.works({ targets: [a.work, b.work] }));
```

两名成员在第一次模型输入中就看到对方的真实 target、职责和具体协作问题。后续讨论直接调用原生 `send_agent_message`，无需为发消息编写脚本，也无需 ROOT 再补发介绍。

两个成员没有 depends_on，因此在容量允许时并行工作。ROOT 可用原生工具继续发送指导、查询状态或停止某次工作。

A 需要 B 确认接口时，直接连续使用两个原生工具，不需要自己写脚本。以下 session:bbbbbbbbbbbbbbbbbbbbbbbbba 表示初始请求中提供的真实目标。

先调用 send_agent_message：

```json
{
  "target": {"kind":"AGENT","agent_id":"session:bbbbbbbbbbbbbbbbbbbbbbbbba"},
  "message": "Please confirm whether parse returns an errors array. I need this to finish the integration."
}
```

确认发送结果表示消息已接纳（例如 queued）后，再调用 wait_agent：

```json
{
  "targets": [{"kind":"AGENT","agent_id":"session:bbbbbbbbbbbbbbbbbbbbbbbbba"}],
  "timeout_seconds": 30
}
```

A 的 Work 保持活动。B 回复时，A 收到新消息后继续；B 先完成时，A 读取其 final。超时可以再次等待，不重复发送问题。等待本身不通知 B；前一条消息已接纳意味着问题会沿消息链路交给 B，不代表 B 已经阅读。A 只有完成自己的工作才输出 final。

若 B 尚未开始执行，send_agent_message 返回目标 not_started，消息不接纳；若 B 已完成或已卸载，返回 inactive 并说明未发送，A 不应把该问题当作已交付。以尚未启动的 B 的 AGENT target 调用 wait_agent 同样立即返回目标 not_started，不靠发消息或等待启动 B。JS 中使用 tools.wait_agent.works 可以等待确切的已排队工作，执行由原有调度和依赖条件决定。

如果 ROOT 明确安排“先等我回复再继续”，Subagent 可以这样使用同一个原生等待工具：

```json
{
  "targets": [{"kind":"AGENT","agent_id":"ROOT"}],
  "timeout_seconds": 30
}
```

这个调用等待 ROOT 发给自己的消息，不把 ROOT 的最终回答当成一份 Subagent 工作结果。没有上述明确安排时，按工具说明不主动等待 ROOT。

## 7. 复杂 workflow：调查、审查、修订、实现、代码审查

示例分为两个批次：调查与方案审查；ROOT 作出选择后的修订、实现与代码审查。每一批通过 `tools.run_agent.batch` 一次写出成员说明与依赖。ROOT 仍负责实质决策，脚本负责执行已经确定的安排。

### 7.1 第一批：并行调查，再由 critic 审查

```javascript
const research = await tools.run_agent.batch({
  schema: {
    name: "Schema researcher",
    profile: "research_worker",
    instruction: ({ runtime }) =>
      `Investigate the parameter schema and parser. Propose a change with code evidence. Coordinate cross-layer questions with ${JSON.stringify(runtime.agent)}, who investigates execution and callers.`
  },
  runtime: {
    name: "Runtime researcher",
    profile: "research_worker",
    instruction: ({ schema }) =>
      `Investigate execution and callers. Identify changes needed to preserve current behavior. Coordinate parameter-contract questions with ${JSON.stringify(schema.agent)}, who investigates the schema and parser.`
  },
  critic: {
    name: "Critic",
    profile: "review_worker",
    depends_on: ({ schema, runtime }) => [schema.work, runtime.work],
    instruction: "Review the two prerequisite investigation results. Identify gaps, tradeoffs and decisions ROOT needs to make. Report your assessment in the final answer."
  }
});

console.log({ research });
console.log(await tools.wait_agent.works({
  targets: [research.schema.work, research.runtime.work, research.critic.work]
}));
```

schema 与 runtime 从第一轮起知道对方是谁，可以用原生消息工具直接核对调查中的问题。critic 在两项调查成功结束后才启动，前置工作的结果由依赖机制提供，不重复填写 materials。

critic 的 session 虽然已经存在，但等待依赖时只有待处理请求，没有本次活动 turn 或模型执行，不会因闲置期限卸载或删除。调查者不需要向尚未启动的 critic 发消息。两个调查结果均进入 critic 自己的请求后再启动；首次输入中 profile 在前，instruction 是开启 entry，其后分别追加 schema、runtime 的 final。critic 不依赖两位调查者继续在线或保留源 session。需要进一步讨论时，ROOT 可以给仍存在的原调查者追加 Work，若其已卸载则执行前自动加载。

前置调查失败会使 critic 的依赖无法满足；等待结果保留实际状态。ROOT 阅读各项结果及 critic 的 final，决定接下来采用哪种方案。这里没有把执行 completed 当成方案获批。

### 7.2 第二批：修订、实现、原 critic 审查代码

ROOT 已决定统一缺失参数处理、保留显式 null 语义。以下 `research` 使用第一批实际返回的对象；若是另一个 cell，ROOT 将前一段正式输出的该对象作为输入写入脚本。代码示例中的完整格式示意 ID 必须替换为真实返回值，不按局部名称猜 ID。这个跨 cell 传入已有对象的步骤不同于批次内部互相填写尚不存在的成员 ID；批次内部仍由 helper 自动解析。

```javascript
const research = {
  schema: {
    agent: { kind: "AGENT", agent_id: "session:ccccccccccccccccccccccccca" },
    work: { kind: "WORK", work_id: "queue:ccccccccccccccccccccccccca" }
  },
  runtime: {
    agent: { kind: "AGENT", agent_id: "session:ddddddddddddddddddddddddda" },
    work: { kind: "WORK", work_id: "queue:ddddddddddddddddddddddddda" }
  },
  critic: {
    agent: { kind: "AGENT", agent_id: "session:eeeeeeeeeeeeeeeeeeeeeeeeea" },
    work: { kind: "WORK", work_id: "queue:eeeeeeeeeeeeeeeeeeeeeeeeea" }
  }
};

const delivery = await tools.run_agent.batch({
  revision: {
    target: research.schema.agent,
    materials: [research.runtime.work, research.critic.work],
    instruction: "ROOT chose to unify missing-input handling while preserving explicit-null behavior. Continue from your investigation, incorporate the other investigation and critique, and produce the complete implementation plan."
  },
  implementation: {
    name: "Implementer",
    depends_on: ({ revision }) => [revision.work],
    instruction: "Implement the agreed plan supplied by the prerequisite. Report the actual changes and verification results."
  },
  review: {
    target: research.critic.agent,
    depends_on: ({ revision, implementation }) => [revision.work, implementation.work],
    instruction: "Review the actual implementation against the final plan and your earlier findings. Report concrete defects and any remaining decisions for ROOT."
  }
});

console.log({ delivery });
console.log(await tools.wait_agent.works({
  targets: [delivery.revision.work, delivery.implementation.work, delivery.review.work]
}));
```

revision 复用原调查者，review 复用原 critic，各自的旧历史保持原样。implementation 是新 agent。批次内通过局部名称引用 WORK 依赖，helper 准备真实引用；启动顺序为修订成功、实现成功、代码审查。review 同时取得定稿与实现结果。这里不再手写一连串等待与下一步派活。

复用的成员已卸载时，batch 仍接纳请求，等待条件满足后按需加载。review 的 instruction 是其新 turn 的开启 entry；revision 与 implementation 的 final 按声明顺序作为两条 user 消息追加在后，首次模型调用前全部就绪。

已接纳的依赖工作由共同调度链路推进，不依赖脚本保持在 await；脚本终止不自动取消它们。ROOT 要停止后续工作时，对相应 WORK target 使用原生停止工具。

ROOT 阅读代码审查的 final，决定完成还是继续修改。若需要单次修订，原生 `run_agent` 即可：target 使用 delivery.implementation.agent，materials 使用 delivery.review.work；无需为了这一条请求再写 batch。

本例中的审查与实现是阶段依赖，不要求它们在运行中互相聊天。需要双向实时讨论的工作应安排为可同时活动的请求，并在 instruction 参数函数中明确介绍对方；不能一边让 B 等 A 完成，一边要求 A 等 B 在线回复。

### 7.3 停止不再需要的工作

ROOT 决定某份工作不再需要时，既可原生调用，也可在 JS 中执行同一个操作：

```javascript
console.log(await tools.stop_agent({
  target: { kind: "WORK", work_id: "queue:fffffffffffffffffffffffffa" }
}));
```

若 ROOT 要停止实现者当前工作及其全部已排队工作，直接使用其 AGENT target：

```javascript
console.log(await tools.stop_agent({ target: delivery.implementation.agent }));
```

脚本的明确条件也可以触发停止；条件必须表达实际产品判断，不能把“任意一个成员结束”自动等同于“其他成员都不需要”。

### 7.4 中断后继续协作

ROOT 主要围绕 Agent 会话查询与派活，需要精确查看、等待或停止某次工作时使用 WORK。继续协作复用 `query_agents`、`run_agent`／batch 和等待工具，不增加恢复工具或状态。ROOT 与 Subagent 共用模型配置和本机网络，故障可能同时影响多个请求；恢复不以 ROOT 在故障期间持续在线为前提。用户重新向 ROOT 发消息后，通过共同对话链路继续。

ROOT 先用 `query_agents` 找到已有成员及当前／排队概览，再用 `query_agents(AGENT)` 查看相关工作列表、用 `query_agents(WORK)` 读取确切状态、原因与结果。已经接纳但尚未执行，与根本没有接纳是两种情况；提交结果不确定时沿既有确认链路核实，不直接重派。

| 查到的事实 | 下一步 |
|---|---|
| queued／running，含现有临时重试 | 保留原请求，按需等待；不因 ROOT 曾中断而重复派活 |
| completed | 使用已有 final 和成果，不重复执行 |
| stopped，仍需完成目标 | 向同一 Agent 提交新 Work，结合有效历史和实际工作区继续；已有修改不回滚，旧工具调用不机械重放 |
| 后续工作尚未接纳 | 只补派缺少的工作 |
| Agent 已卸载、session 仍存在 | 派活时沿 §1.4 按需加载；旧 Work 不复活 |

若旧 cell 仍存活，ROOT 可用 `query_cells` 查当前库存或持有 target 的状态和源码，再通过 `manage_cell.wait` 读取输出。保留原安排就继续观察；要替换安排时先 `manage_cell.stop` 停止后续派发，再查询已接纳的 Work。脚本终止不取消这些 Work，所需停止仍由 `stop_agent` 完成。宿主崩溃后旧 VM 不恢复，执行与排队事实由共同 Host 接管链路结算。

复杂 workflow 的继续方式如下：

| 中断位置与查询结果 | ROOT 的后续安排 |
|---|---|
| 第一批 schema 调查完成，runtime 调查停止，critic 因依赖未成功停止 | 复用 runtime Agent 派出新调查；复用 critic 派出新审查，依赖新调查，并以 materials 提供已有 schema 结果 |
| 第二批 revision 完成，implementation 修改了部分文件后停止，review 级联停止 | 向原实现者派新 Work，先核对当前改动和执行结果，再完成剩余方案；向原 critic 派新审查，依赖新实现，并提供已有定稿 |
| ROOT 中断，但 implementation 仍 running、review 仍 queued | 等待这两份原 Work；若它们已经完成，直接读取结果 |
| 已完成实现，但后续审查从未接纳 | 只派审查，提供已有方案和实现结果 |

新脚本只表达剩余安排，引用已有 Agent、仍有效的 Work 和已完成成果，不重建已完成阶段，不恢复旧 JS 的变量、Promise 或执行位置。等待条件属于共同待处理请求，脚本丢失不会抹掉已接纳工作；新 Work 也不会自动替代旧依赖目标。

## 8. 提示词与 bundled skill 分工

| 位置 | 内容 |
|---|---|
| 原生工具定义 | 本工具参数、target、结果、派活／消息／等待／停止的必要语义 |
| code mode 环境说明 | exec_cell／query_cells／manage_cell、工具代理、输出、持续等待与脚本生命周期 |
| SYSTEM | ROOT 与 Subagent 各自为完整文本；ROOT 简述原生／JS 共用对象及 Skill 入口，Subagent 只保留通用执行与交付规则；角色职责由 profile 提供，工具组合教学由 Skill 承担 |
| `pulsara-code-mode` bundled Skill | 主文档展示普通工具与 Agent 交叉使用；`tool-calling` 参考目录介绍工具组合与结果，`agent-orchestration` 参考目录介绍复用、batch 队友引用、依赖、等待、多人协作与多阶段示例；正式输出只教 `console.log` |

Subagent 发消息不以读取复杂 workflow skill 为前提。模型不需要知道实验过程、历史接口或内部协议名称。这里不新增独立的群组 skill 或 workflow 语言。

## 9. 与现有规格及生产的关系

### SUPERSEDES 范围

本文为本轮协作运行时的实施合同，替代旧方案中以下产品表述。实施及实际验收见 [实施记录](output/agent-code-mode-implementation-20261029/integration/IMPLEMENTATION.zh.md)。

| 来源 | 本文负责的新语义 |
|---|---|
| `archived_docs/ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md` 及现有 bundled subagent skill | 本文的五个共享操作、AGENT／WORK 身份、普通 final 完成、原生消息和 ROOT 统一协调。 |
| `experiment/quickjs-code-mode` 分支的 Code Mode 规格 | 本文的原生／JS 暴露矩阵、等待合同及正式模型输出接口；`console.log` 承担不可丢弃的正式输出，不采用原实验的 best-effort 诊断日志语义。 |

记忆工具、monitor 和普通 final 的目标合同见 §3.4。旧文件保留为上下文，不作为新对象与接口的定义来源。

### 实施落点与完成条件

| 责任 | 主要复用基础 | 本合同要求 |
|---|---|---|
| 身份／请求 | ROOT session、`prompt_queue_items`、queue identity | `root_session_id` 外键及索引记录归属，kind 派生而不存储；AGENT 使用完整 `session:`，WORK 使用完整 `queue:`，无额外身份映射 |
| 接纳／调度 | `_repository/prompts.py`、`turn_admission.py`、Host delivery loop | 按 session 选队首，等待条件属于请求；取得前置正文后清除对应条件；instruction 是 initial／consumed entry，profile 与各结果独立排列，同一事务提交并确认整组；原生和 JS 共用按需加载 |
| 上下文／模型切换 | ROOT reader、provider dispatch、continuity、compaction 与 UI 历史投影 | 开启 entry 与完整初始组分别定位；首调用前仅摘要组首之前的前驱并精确保留整组，之后沿原 instruction 保留与普通摘要规则；同 scope 连续追加 |
| 状态／结果 | `_repository/conversation.py` 的请求联查、assistant settlement、turn final／终止原因 | 统一公开投影，无 Work 状态或结果副本；材料与 final 交付时保存为接收方输入，之后不依赖源 session |
| 等待／停止 | 共同请求／turn 读取、变化通知、队列取消与 ActiveTurnCancellationIntent | WORK 按 queue→entry→turn 处理；AGENT 停止当前工作并取消本次覆盖的排队工作，历史终态不变；与调度竞态共用 owner，返回实际结果 |
| 消息 | ROOT steer／补充输入、待输入与完成裁决、Hook 上下文接纳 | ROOT／Subagent 均只向活动 turn 接纳 steer；无活动 turn 则工具结果明确返回未接纳，不暂存、不加载、不开启工作；发送与完成竞态由共同 owner 裁决 |
| Monitor | ROOT monitor manager、通知接纳／结算、compaction 与 UI | 参数化真实接收 scope；成员订阅绑定当前 turn，终态关闭，进程仍独立 |
| 工具／提示 | built-in catalogue、caller gating、bundled skill | 五个协作工具，记忆和三个会话检索工具仅 ROOT；Subagent 不隐式注入记忆、历史或响应偏好；monitor 原生可供成员 |
| 展示／检索 | 侧边栏、会话检索工具及隐式历史召回 | 侧边栏仅 ROOT；检索两种 session 行为一致，不新增 kind 参数；历史检索开关仅控制隐式召回 |
| 能力／MCP | 共同 capability 查询、planner、MCP supervisor、配置入口与 context sources | ROOT／Subagent 共用发现、查询、启停和执行规则；两种 MCP 来源共用提供范围开关，插件安装时供用户审阅；各受影响 session 正常输入时追加变化观察，保持 epoch 前缀 |
| 退役／删除 | 现有 session 关闭、删除与内容回收 | 同次连续闲置满 1 小时卸载、满 3 天删除；运行或等待请求不计闲置；重新派活按需加载，不恢复旧执行；单独删除成员不影响其他会话已交付正文，删除 ROOT 一并关闭和删除所属成员 |

Agent 使用 session 身份及 `root_session_id` 归属；委派来源、profile／初始材料和未满足的等待条件是请求的业务信息。其余接纳、turn、历史、完成与结果职责都由共同引擎承担。归属之外，消息、材料与结果不建立跨 session 外键或保留约束；不保留旧 task 状态／报告与 turn 双写，也不为 WORK 建映射表、结果表或独立生命周期。已有 event、subject、guard、job 类别不因“便于验证”增加。

实施验收关注行为：WORK 请求 ID 从排队到完成不变；启动与取消只有一个胜出的实际阶段；失败依赖不创建空 turn；同 Agent 连续请求共用历史与允许的 epoch，压缩后不改写旧内容；batch 第一次模型调用已带正确队友；组合环不启动成员；消息接纳与 turn 交接不转投；容量满时等待者让槽；大 final 直接从原 entry 可读；完成 Hook 续接不提前终态；monitor 终态关闭不杀进程；重新打开只推进未启动请求。与 Code Mode 的跨压缩、取消及 provider 前缀验收一起完成，不新增冻结文案测试。

会话验收同时覆盖：侧边栏仅列 ROOT，ROOT 对两种 session 的主动与隐式历史检索行为一致；关闭历史检索只关闭隐式召回；Subagent 无三个会话检索工具及上述隐式注入；A 长时间执行时，等待 A 的 B 不计入闲置删除；A 已交付后被删除，B 的请求正文和历史仍完整可读。

本次输入与恢复验收覆盖：首次／复用 Work 均以 instruction 为开启 entry，profile 与前置 final 分别作为同轮 user 消息，依赖完成先后不改变声明顺序；整组输入计量、截止位置、提交确认及首次实际 provider 输入一致；压缩不误将 profile／结果当成人类请求。向已完成、未启动或已卸载的 Agent 发消息明确返回未接纳，ROOT 同样适用；1 小时／3 天共用闲置起点，等待工作不触发两段退役；原生及 JS 给已卸载会话派活可自动加载，旧执行不重跑。

身份与归属验收覆盖：ROOT／SUBAGENT kind 仅由 `root_session_id` 派生；ROOT 成员列表按归属读取；删除 ROOT 同时关闭并删除所属成员，单独删除成员不删除 ROOT；模型接口始终传递完整 `session:`／`queue:` ID，`ROOT` 按可信协作上下文解析；WORK 从接纳、排队到执行结束使用同一个队列请求 ID，不改用 `turn:` ID。

能力验收覆盖 §1.5 的四种变化：手动配置与插件 MCP 使用同一提供范围设置；每个受影响的活动 session 收到相应观察；禁用后阻止新调用，同 epoch 已安装 schema 保持不变；能力查询不扩大原生管理工具权限。

前置调查依据见 [ROOT 复用审阅](output/code-mode-root-reuse-20261028/root/REVIEW.zh.md) 与 [请求链路证据](output/code-mode-root-reuse-20261028/requests/REVIEW.zh.md)。本轮运行时实施、真实共同 owner 和保存 provider 验收见 [实施记录](output/agent-code-mode-implementation-20261029/integration/IMPLEMENTATION.zh.md) 与 [逐项验收矩阵](output/agent-code-mode-implementation-20261029/integration/ACCEPTANCE_MATRIX.zh.md)；矩阵明确区分实际执行层级与未覆盖边界。SYSTEM 简短入口与 Skill 教学见 §8，其余 SYSTEM 整体修订、profile 正文和新的前端表面仍按暂缓范围处理。
