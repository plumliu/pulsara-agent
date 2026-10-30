# 统一安全点输入与显式停止实施规格

状态：已实施并完成验收，ROOT 完整 diff 与交叉复审已完成，可冻结。实际验证、保留的失败和范围见 [验收记录](output/unified-safe-point-20261030/verification.zh.md)。

## 1. 目标与范围

人类转为当前 turn 引导的输入、其他 Agent 主动消息、Cell 正式输出和活动定时触发，统一按各自真实来源接纳到目标 turn 的现有队列，按接纳顺序在共同输入安全点交付。输入到达本身不取消未派发工具、不打断模型或工具批次，也不人为中断批次以制造安全点。只有显式 Stop 才按现有停止合同停止 turn；其他独立资源的显式控制、真实超时、失败和权限拒绝仍按其各自合同生效。ROOT 与 Subagent 使用同一交付原则，不由来源或执行者 kind 决定是否抢占。

普通发送仍是未来的新 turn；只有用户明确将排队输入转为当前 turn 引导，才成为当前 turn 的补充。Agent 消息只在真实目标活动 turn 接纳；idle／not_started 返回原有未接纳结果，不唤醒或转投后续 Work。各项输入保留各自真实身份、sender、queue ID、来源及 entry 边界；不能把 Agent 输入冒充用户授权或批准。Work 终态不自动生成 ROOT 输入；Cell/定时输入按共同接纳事务固定 route，详见 [统一输入规格](PULSARA_CELL_OUTPUT_AND_COLLABORATION_INPUT_IMPLEMENTATION_SPEC.zh.md)。

不新增产品关系、持久事件类别、live event kind、subject、guard、receipt、队列、兼容路径或总量 cap。既有 canonical 队列、accepted 输入 occurrence、资源边界、确认与停止结算各自保留产品用途；这里删除的是仅为补充输入抢占执行而存在的进程内信号和分支，不删除表示已接纳输入的事实。保留用户原有排队编辑、取消和转引导行为，不增 UI 入口。

## 2. 接纳、顺序和安全点

现有 writer／接纳事务决定队列提交与活动 turn 终态的先后。以 `session_id`、精确 `target_turn_id` 和 `queue_sequence` 标识同一目标的待输入；普通 provider 安全点按顺序选取允许的前缀，通过现有 `SteerConsumptionCoordinator` hydration、输入预算、原子消费及 exact confirmation 保存为各自 canonical entry。Plan 终态交接安全点按 §4.1 只原子保存 canonical 输入；在后续真实调用时才按该调用的 surface 测量预算，不表示模型已读。单次读取／材料化的现有物理边界继续适用，普通安全点未选中的队首后缀等待下次安全点，不把每次读取边界当成历史或队列总量上限。

一次 provider 调用冻结的 cut 之后接纳的输入不改变其已安装内容。模型调用和它产生的工具批次按原执行与结算规则完成；Plan 选中控制取消兄弟调用的 barrier、真实权限拒绝、显式 Stop 和错误等独立规则继续生效。随后在已有的批次后／下一调用前安全点选取队列，不在批次内部插入用户消息。已选但尚未结算的副作用、工具结果、Hook、资源 reservation 和已启动 canonical settlement 沿现有 owner 收束；补充输入不触发重发或撤销。

普通模型 final 与补充接纳由同一 writer 事务裁决：补充先接纳则当前 turn 不能以该 final 终结，已有 assistant entry 保留并在下一个安全点消费，再决定是否继续；final 先成立则对该终态的消息按现有规则未接纳或拒绝。显式 Stop／Host close／writer replacement 等控制可以结束 turn，尚未消费的输入按原精确终态规则结算，不转投后续 turn；已消费的 entry 保留。真实资源不足、超时、权限和 provider 故障按既有明确失败路径处理，不能伪装为用户取消。

维持现有前缀合同：同一 epoch 的 SYSTEM／tools 保持字节不变，messages 只按 suffix 追加；只有 cold epoch 或明确采用的 compaction successor 是重建边界。禁止因队列变化、消息到达或 UI 需要重建前缀。

## 3. 运行中的模型、工具与等待

- **模型流与编译：** 不取消正在收集的 provider 输出；cut 后新输入等待下一次合法安全点。正常收集、资源报价、assistant canonical settlement 与最终完成栅栏不跳过，也不追加到已发送调用。Stop 与真实失败仍走原收束路径。
- **原生工具批次：** 所有已接纳的模型工具调用按原顺序／合法并发及授权继续，直到正常结算、显式控制或真实错误；不因 pending 输入将未派发调用判为 `CANCELLED_BEFORE_DISPATCH`，不创建 `interaction:user-steer` 伪取消结果。保留 Plan 固有 batch barrier、权限拒绝、真实 Stop／关闭等有独立理由的 no-attempt 结果。
- **并行图片调用：** 既有 `view_image` 分段预授权、并发执行和按序结算不因补充输入改变；显式 Stop／真实异常仍取消适当任务并等待已开始结算，保留资源 borrow 与 Hook 顺序。
- **授权／MCP／配置：** 等待授权卡片、配置表单、MCP lane 和真实物理操作时，新补充不取消等待或替用户选择。按真实答复、显式取消／Stop、原超时和关闭结算；已派发操作仍完成其原副作用和资源结算。MCP lane 的独立控制与 permit 获取／取消竞态不能被误删。
- **Agent wait：** `wait_agent` 的选中发送者消息和所绑定 Work 终态仍可按原工具定义自然满足等待；消息正文仍按共同队列处理。人类引导或未选中的消息不以抢占信号令等待返回 `interrupted`。`tools.wait_agent.works` 仍只等待指定 Work 终态，普通消息不构成其完成条件。真实 timeout、turn Stop 和 Cell Stop 按原合同处理；停止等待不停止目标 Work。
- **Code Mode：** `exec_cell` 所启动的 Cell 及其 Promise／已提交操作不随补充消息停止。`manage_cell.stop`、Cell 自身取消、Host 故障／关闭仍作用于真实 Cell owner；Cell 内等待或尚未取得 MCP lane 的操作必须继续接收 Cell 自身取消，且不误停被等待的 Agent Work。区分这个有独立用途的 Cell cancellation 与待删除的前台 steer 抢占信号，不按变量名机械删除全部事件。
- **无自然安全点：** 正在等待真正的工具结果或人的答复时，输入可保持待处理；不承诺即时处理，也不通过虚假完成、强制超时或增加隐式中断创建安全点。

## 4. Plan、交互与终态边界

Plan 问题、工具权限确认及配置表单保持原有交互和权限 owner。输入到达不自动取消问题、不代答、不构成授权；用户真实答案／显式取消或 Stop 按各自合同结算，关闭相应未决工具结果后，原 turn 在下一个合法安全点消费此前及期间按序接纳的输入。不得因 pending 输入拒绝真实 Plan 答案或让问题永远无法解决。

Plan 控制批次自身原有的选择、同批其他项 barrier、真实权限与结算仍由 Plan owner 处理；不能以有待输入为由把批次标成被用户取消。现行 `plan_runtime.py::accept_batch` 和 `_repository/plans.py::accept_plan_tool_batch` 的 pending-steer 抢占门禁须与 `runner.py` 相应伪取消链一并替换；`host.py::cancel_plan_question_for_steer` 和 `_repository/conversation.py`／`_repository/plans.py` 中只为输入到达而取消问题／拒答的条件也须一并移除。

### 4.1 Plan 控制结束原 turn 时保留输入

ENTER、空 EXIT 和 DRAFT 按原 Plan 语义正常结算，不因补充输入到达取消控制。Plan 终态交接安全点在事务中将已接纳的原 turn 补充按队列顺序保存为原 turn 的独立 canonical entry，再结束原 turn；fresh ENTER／空 EXIT 按原语义创建 successor，其初始 history cut 包含这些 entry，后续合法模型调用才能实际读取和测量预算。DRAFT 保持 review，不直接创建 successor；之后尚无模型调用时这些 entry 可以暂未读。补充不是批准、修改或取消 proposal，也不声称发出控制调用的旧模型已经读过。

在 `accept_plan_tool_batch` 原有 `_writer_transaction` 内，由已有 session writer 锁串行化输入接纳、控制结算和 turn 终态，采用以下一次提交的唯一顺序：

1. 验证既有 candidate、writer、RUNNING origin、权限、workflow 和 assistant blocks；按原 Plan 规则写 workflow／interaction 及选中和兄弟工具结果，保持 Plan barrier 的真实 no-attempt 结果。无效或被权限／Hook 拒绝的控制沿原结果结算，不伪造成功；未终结 origin 的分支回到普通安全点处理输入。
2. 对要终结的原 turn，使用当前连接按 `(queue_sequence, id)` 分页锁定实际 `PENDING / STEER_ACTIVE_TURN` 队首，包括 Plan candidate 准备后、writer 锁取得前接纳的项。每项沿现有 canonical owner 和单条资源边界完整校验／hydration，核对真实 session、target、状态、canonical content／图片引用及其原来源；复制正文与图片引用后释放临时材料，再处理下一项，不同时水合一页大图片。抽取 `consume_prepared_prompt_steer` 中实际写入职责为可在当前连接调用的内部 owner：按真实来源为每项分配原 turn 的 canonical entry（Cell 为 `TERMINAL_OBSERVATION`，其他来源沿现行 typed owner），复制原 canonical 内容及图片引用，按现有 queue CAS 设 `CONSUMED` 和 `consumed_entry_id`，生成既有 `PromptConsumed` 及该真实来源的 accepted occurrence，保留真实 actor／origin。原控制基底和权限在 Plan 变更前精确校验，之后只接受本事务自己的已知状态变化；复用原身份构造与约束，不在事务内调用独立提交的 `consume_prepared_prompt_steer`，不把 provider suffix planner 当作未结算工具批次的提前 dispatch。
3. 继续有界分页直到目标 lane 为空；既有 128 项单次读取与 16 MiB 单次展开边界只约束每段操作，不是可丢弃余项的总量边界，不将 128 项全部无界水合。持有同一 writer 锁下完成最后空检查，随后在同一事务终结 origin，并按原规则创建 successor／Plan interaction、其 binding 和初始 cut。先后确定写入的工具结果、补充 entry 与事件、终态及 successor 在一个事务提交，不能只处理首个截面后让后续已接纳输入落到 terminal target。
4. 提交前发生的任何校验、图片引用、容量或数据库失败使整个控制／消费事务回滚；以原控制和输入身份沿既有冲突重规划与确认路径处理，不重复发布结果、不生成半成品 successor，也不把失败伪装为工具成功或用户取消。提交结果未知时复用现有 Plan batch candidate 的身份确认：确认该原子事务已提交即确认整组提交；缺失或冲突按原明确结果处理。不得独立重放输入，也不新增逐项确认协议、receipt、指纹或索引。事务 deadline 和既有资源边界保留，不增加新的累计上限、后台修复、收件箱或持久中间态。

输入在 writer 锁前已经接纳时属于本次原 turn 队列；终态事务先胜出时，后续针对原 turn 的接纳按既有终态规则失败，不补投 successor。当前 Plan owner 将结果事件先积累、最后一次追加；若输入 owner 提前追加事件会颠倒顺序。因此同事务内须先追加原 Plan open/control 和全部 `ToolResultAccepted`，再依队列顺序追加输入消费和各来源 accepted occurrence，最后追加 `TurnCompleted` 与 `PlanContinuationAccepted`；可复用现有事件 draft 聚合或分段追加，不新增事件批次类别。无输入时原事件顺序、Plan sibling barrier、既有事件类别与身份保持；观察者只在事务提交后看到完整结果。后继的真实调用按其实际 SYSTEM／tools／权限进行完整输入预算测量，必要时使用现有 compaction 或 typed resource failure；不借旧 surface 冒充 provider 已读，也不因 DRAFT 没有 successor 而伪造调用。

## 5. 压缩、首次准备与停止

首次调用前、turn 内和显式采用的 successor 的压缩沿现有 coordinator、完整输入预算和 suffix 规划；摘要运行中接纳输入不重启摘要，采用后在实际安全点读取并消费合法截面。截面之后到达的项留给下一安全点。继承模型 binding、初始输入组及材料先后顺序沿既有压缩规格；不借此改写已冻结 source、压缩身份或跨 epoch 前缀。

显式 Stop 保持原正确目标、并发线性化和结果：停止当前 turn、取消其应取消的等待和未派发操作，并对已开始物理执行及已开始 canonical 写入按既有 owner 的停止／shielded settlement／确认路径收束。已发生副作用不伪称未发生；被停止 turn 不重放，其工具不自动重试。独立排队的 NEW_TURN／Work 按现有调度继续，显式 `depends_on` 沿原失败规则；所属 Cell 按原 Stop 合同停止，独立 Cell、Agent 和后台进程不扩大停止范围。Stop 与接纳、消费、final、问题答案、压缩采用及物理派发竞态按各自真实提交次序给确定结果。Host close／崩溃／writer replacement 的资源退役和恢复门禁不随普通输入语义改变。

## 6. 具体 hard cut 与复用 owner

| 链路 | 必要变更 |
|---|---|
| `host.py` 人类输入与队列转换 | 接纳只唤醒共同队列／观察者；移除 `_wake_accepted_steer` 对前台 batch 和 Plan question 的抢占，保留其仍有独立用途的唤醒及结算 |
| `tool_execution.py`／`runner.py` | 删除仅为输入到达而建立的前台抢占 event、`steer_tool_batch`、`cancelled_by_steer` 及 pending 入场伪取消、抢占分支与对应文案；不能删真实 Cell cancellation、Stop、Plan barrier 和实际资源授权 |
| `tool_runtime.py`、交互／MCP、`subagents/collaboration_api.py` | 移除仅传播普通补充抢占的参数与等待 race；精确保留 Cell／显式停止与相关 permit 清理及自然等待完成条件 |
| `plan_runtime.py`、`_repository/plans.py`、`_repository/conversation.py` | 移除输入导致 Plan batch 被取消、问题自动取消及答案被 pending 输入拒绝的专用分支；终态竞争按 §4 实施 |
| `_repository/prompts.py`、`steer.py`、`steer_consumption.py`、`safe_point.py`／dispatch | 复用原队列、目标、截面、消费／预算／确认、completion fence；只删确实无其他用途的抢占路径，不删除物理资源失败或合法 Plan 冲突诊断 |
| Code Mode、MCP 和 Plan 展示／提示 | 删除把普通输入说成用户取消工具的文案；保留用户确实选择取消和真实执行结果 |
| 活跃规格、当前测试与 dogfood | 修正过时的普通 steer 抢占断言及调度重试 fixture；保留可复现的历史验收记录，不以伪取消当作当前正常行为 |

逐处追踪调用和所有信号的生产用途后再删除；不得残留空事件／空参数、兼容双路径或借名改造第二个取消框架。既有 Stop、Cell cancellation、权限管理和资源 close 的所有控制线需以独立测试证明未被误删。

## 7. 实施与验收

本次 hard cut 覆盖接纳唤醒、批次执行、Plan、交互／MCP 与 wait 的整个调用链；活跃规格、测试和当前可执行 dogfood 与之保持一致。测试保留真实 Stop、失败及原权限／前缀断言；没有新增 schema 数据分类或在线兼容迁移。

至少覆盖：

1. 使用真实 Host 与 PostgreSQL 的代表性可控屏障：混合三类输入验证同一目标 FIFO、source、entry、精确一次、cut 前后及 prefix 字节；分别覆盖模型流、批次未派发项、MCP lane、交互等待和 final 提交关键边界。工具按原授权、Plan barrier、显式 Stop 及真实失败规则执行／结算，不凭消失的取消结果推断成功。
2. ROOT 与 Subagent 的相同输入语义；Agent A 主动消息、Cell/定时补充不冒充人类，不扩展已有通信权限或已知对象集合语义。选中 wait 目标自然唤醒，未选消息／人类 steer 不抢占；真实 timeout 与 turn／Cell Stop 仍可结束相应等待，Cell Stop 不停止目标 Work。
3. Plan question 保持 OPEN 直到真实答案并在结果后按序处理补充，保留答案提交失败与丢失 ACK 的原强度；覆盖 ENTER、空 EXIT 与 DRAFT 在控制批次／终态竞争中不丢失、不改投输入，DRAFT 补充不代替审批。验证超过 128 项跨页、最后一项含图片、candidate 准备后接纳、途中失败整体回滚，以及提交 ACK 丢失时由原 Plan 确认恢复；保留 Plan batch barrier、权限约束、显式取消和原有限制，不得弱化以换取通过。
4. 首调用前与 turn 内摘要中接纳输入，采用前后真实截面与 late 输入，调用时序、memory／Hook source、SYSTEM/tools、suffix、原资源报价与 Stop 结算保持；同时验证真实 provider final 与输入接纳两种事务顺序。
5. 明确 Stop 与多项工具、已派发效果、未派发步骤、Cell 自身关闭、MCP lane、interaction 以及输入接纳并发；实际 side effect／attempt／reservation／结果按原 owner 结算；独立 Cell 和被等待 Work 不误停。
6. 资源不足、连接故障、Host close／writer replacement 保持真实失败和已提交内容，保留单次限制、不新增总量限制。更新 `test_blocking_steer_host.py`、`test_agent_session_host.py`、`test_round4_plan_host.py`、`test_agent_wait_controls_host.py`、`test_agent_collaboration_api.py`、`test_capability_management_execution.py` 的受影响场景和原 `CANCELLED_BEFORE_DISPATCH` 重试 fixture；保留真实 Stop／Cell Stop、Plan barrier、OAuth 和实际错误断言。

真实 provider 验证合同：通过 `LocalSettingsStore` 和 `require_pulsara_home()` 只读用户保存配置，使用内存配置覆盖与核实后的本地隔离数据库；在模型真实发出多工具批次时用屏障于中途接纳用户引导、Agent 消息及 Cell 正式输出，保存实际模型请求、工具参数、真实结果、canonical 消费顺序和后续输入，证明未发生普通输入诱发的 no-attempt 取消，并保留显式 Stop 对照。实际协议、模型、reasoning、失败和凭据清理按真实运行记录，不用模拟模型输出冒充 provider 验证。实际双协议运行结果及边界见 [验收记录](output/unified-safe-point-20261030/verification.zh.md)。
