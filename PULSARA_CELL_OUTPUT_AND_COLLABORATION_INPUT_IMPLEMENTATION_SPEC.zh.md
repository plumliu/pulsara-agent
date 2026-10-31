# Pulsara：统一输入队列、Cell 主动输出与协作阅读实施规格

日期：2026-10-31。状态：**实施与激活完成，已通过 ROOT 最终语义验收。** 逐条 Cell 输出、统一队列、协作阅读、定时触发及源码/图边界已按本规格验收，见[激活报告](output/unified-input-retry-20261031/activation-report.zh.md)、[逐项语义矩阵](output/unified-input-retry-20261031/verification-plan.zh.md)与[原始证据索引](output/unified-input-retry-20261031/activation-index.json)。保存生产配置只读。

## 0. 目标与权威

ROOT 应能派出并行、多阶段工作流，让中间结果在 Agent 间传递，自己只处理选中的结果。Cell 主动输出代替模型反复领取输出；原生工具保留，供简单派活、查询、等待、消息与停止。用户可阅读完整历史源码，而实时工作流图随运行时结束关闭。

ROOT 的未来 turn 输入共用现有接纳队列，人类消息、定时任务及 Cell 主动输出按接纳顺序平权执行。前端同一排队列表预览真实队头与队尾，中间超出展示预算的消息以省略条数表示；取消只作用于选中的一条输入。

monitor 独立产品路径删除。协作/脚本自动反馈中，只有脚本显式输出及本规格要求报告的 Cell 执行错误/异常结算材料允许触发 ROOT 处理；人类正常发送、定时触发等 §2 明确来源保留各自启动合同，不保留进程完成/进度订阅的自动唤醒工具。

根 AGENTS.md 与当前人类决议优先。本规格在本轮范围内替代以下文档中的冲突条款，实施时须同步正文、测试、Skill、示例及开发说明，形成单路径 hard cut：

- [Code Mode 执行与生命周期](PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md)：逐条正式输出、状态等待、终态与资源回收。
- [Agent／Work 设计](PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md)：Work 静默终态、原生/JS 轻量等待及显式结果查询。
- [子代理与 Cell 侧栏](PULSARA_SUBAGENT_CELL_SIDEBAR_DESIGN_RESEARCH.zh.md)：canonical 源码历史阅读与实时图退役。
- [统一安全点输入与显式停止](PULSARA_UNIFIED_SAFE_POINT_INPUT_AND_EXPLICIT_STOP_IMPLEMENTATION_SPEC.zh.md)：保留其共同安全点、来源身份、输入不抢占及 Stop 边界；删除普通 Work 完成自动输入的接线。
- [资源退役](PULSARA_RUNTIME_HANDOFF_AND_RESOURCE_RETIREMENT_IMPLEMENTATION_SPEC.zh.md)：Cell 原输出确认条件改由主动输入交付结算满足，不延长 VM 寿命。
- [模型流式重试](PULSARA_PROVIDER_STREAM_RETRY_IMPLEMENTATION_SPEC.zh.md)：独立实施范围；重试仍复用已安装输入，新 Cell 输出仅追加到未来安全点，不修改已发送请求。

## 1. 已确定的产品合同

| 范围 | 决议 |
|---|---|
| Work final／失败诊断 | 保存真实结果；不自动交给 ROOT，也不因 Work 进入终态而唤醒 ROOT |
| 工作流内部材料 | depends_on／materials 按原合同传递完整结果，不需要 ROOT 中转 |
| ROOT 主动获取 | 原生/JS query 可读取选定结果；脚本通过 console.log 或非 undefined return 主动向 ROOT 交付 |
| 原生工具 | 保留 run_agent、query_agents、send_agent_message、wait_agent、stop_agent；不造轻量版别名 |
| wait 一致性 | 原生与 JS 的同名操作使用同一轻量返回合同；ROOT/Subagent 均不从 wait 取得结果正文，显式 query 才读取结果 |
| monitor | 删除 manage_monitor、query_monitors 及独立订阅/通知产品路径；仍必需的来源继承/确认职责收敛到共同队列 |
| Cell 输出 | 正式接纳时，有活动 ROOT 则绑定该 turn 的 steer；idle 则进入 NEW_TURN 队列，允许开启新 turn；接纳后路由固定；异常结算材料保留原报告合同 |
| 统一排队 | 共用现有 prompt_queue_items、session queue_sequence 和 writer；不保留 Cell 独立安装/调度分支 |
| 平权顺序 | 同一可交付队列内按接纳顺序 FIFO；人类、定时任务和 Cell 无来源优先级，不比较本地时钟 |
| 定时触发 | 每个独立 TIMER 时点或 RUN_ONCE 请求接纳一条新输入；活动 ROOT 为精确 steer，idle 为 NEW_TURN；不按 task/正文合并已有排队或运行输入，同次请求重传保持幂等 |
| Cell 信息粒度 | 一次 console.log 是一条独立信息；多次调用保留多个独立 steer/队列项，可同批接纳并在同一安全点一起交付，不合成一项 |
| 内核停机 | 内核未运行期间未接纳的定时时点不补发；已经接纳的输入保留原生命周期，不因重启被当成错过时点丢弃 |
| 错误与辅助状态 | Cell 保留一个终态和含根因的组合诊断；Hook advisory 保持现状；能力变化只报告实际生效的最终状态，不逐次播报中间操作 |
| 排队展示 | ROOT 全部 PENDING / NEW_TURN 输入共用同一列表读取范围，按执行顺序预览头尾，中间省略计数；三类图标表示来源 |
| 取消排队 | ROOT 各来源的待开启 turn 输入均可逐项取消；不停止来源 Cell、不影响定时任务后续触发；编辑/转 steer 保留现有范围 |
| 正常结束 | 无新输出的正常结束不额外向 ROOT 推送消息；UI/查询仍可读取状态 |
| return | 非 undefined 的值保留为正式输出兜底；提示词与 Skill 不主动教授 return，不改变运行包装为 async 函数体的实现 |
| 等待 | 保留等待能力，只返回真实目标状态，不再领取、重复注入正文 |
| 材料展示 | 当前 turn 补充材料复用 steer 气泡和过程分组，保留来源；已有来源链接可跳到 Agent/Work |
| 引用 | 主输入框正文与批注评论都支持 /技能、@文件；排队、转 steer 保留原内容；被引用的 quote 不识别 |
| Cell 源码 | 从已冻结数据库内容阅读，不依赖 live Cell；可在运行结束、回收或应用重启后打开 |
| 工作流图 | 保持实时视图；运行时图退役后不重建历史图，已打开 tab 温和显示“实时工作流视图已关闭” |

上述“主动输出”是脚本请求 ROOT 处理材料，不是人类授权。console.log 不承担普通调试日志用途；不要求每个中间 Work 都打印结果。

## 2. 输入来源、交付方式与操作清单

来源说明内容由谁产生；delivery_mode 说明它发给未来 turn 还是精确活动 turn；接纳顺序说明同一可交付队列内的先后。三者分别保存，不能从 provider 的 user role、卡片颜色或是否来自工具反推人类身份、权限或调度优先级。

### 2.1 进入现有输入队列的来源

| 来源/方向 | 交付方式 | 接收方空闲时 | 内容与生命周期 |
|---|---|---|---|
| 用户聊天框正常发送，含正文/附件/批注 | NEW_TURN；运行中发送也不自动成为 steer | 排队后由队首启动 | 正常发送与排队是同一路径；批注 comment/quote 是原 prompt 的组成，不另开输入 |
| 用户明确 steer，包括排队转 steer | STEER_ACTIVE_TURN，精确 target_turn_id | 不接纳，不能静默改投 | 目标终态竞争及转换原子性见 §2.3、§5 |
| 定时任务到期触发 | 按接纳事务活动事实选 STEER_ACTIVE_TURN 或 NEW_TURN，保留 SCHEDULED_TASK 来源 | 独立入队，由队首启动 | 每个真实触发时点是独立输入，保留 task、revision、due time、TIMER；不合并同任务旧项，不因到期而抢占 |
| 定时任务立即执行一次 | 按接纳事务活动事实选 STEER_ACTIVE_TURN 或 NEW_TURN，保留 SCHEDULED_TASK 来源 | 独立入队，由队首启动 | 每次独立 RUN_ONCE 请求是独立输入；不复用已有 pending/running 输入；同一 command 的重传只确认原项 |
| ROOT 向 Subagent 派 Work instruction | 目标 Agent 的 NEW_TURN / AGENT_INSTRUCTION | 原依赖及容量门禁满足后启动 | 同一 Agent 的工作 FIFO；不是 ROOT 自己的未来 turn，不出现在 ROOT 输入队列卡片中 |
| ROOT→Subagent、Subagent→ROOT、Subagent→Subagent 主动消息 | STEER_ACTIVE_TURN / AGENT_MESSAGE | idle/not_started 不接纳 | 只到真实活动 turn，不开启新 turn、不转投下一份 Work；保留 sender session/work |
| ROOT Cell 的 console.log / 非 undefined return | 接纳事务按活动事实选择 STEER_ACTIVE_TURN 或 NEW_TURN | NEW_TURN 队列平权启动 | 保留 Cell、源 turn、exec attempt 和单条原记录位置；正文走真实 runtime 内容类型 |
| 已接纳 Cell 必须报告的执行错误 | 与正式输出共用接纳规则 | NEW_TURN 队列平权启动 | 保留错误来源/类别/诊断；启动前失败和主动 Stop 按 §5.4 处理 |
| 非主动 Stop 的失败 Cell 尚未交给 JS 的真实工具结果 | 原正式结算记录改由共同接纳规则交付 | 可沿 NEW_TURN 报告实际结果 | 保留调用序号、工具名、状态及公共结果/准确读取入口；主动 Stop 之后不凭此唤醒 |

当前 Code Mode 仅供 ROOT 在既有权限下使用；本规格不向 Subagent 新增 exec_cell 能力。所有协作入口保持既有可见性和权限；共享队列不扩展 Agent 的派活、停止或通信权限。

定时触发与其他外部输入共用现有接纳/序号/消费 owner。一个独立 TIMER occurrence 或一个新的 RUN_ONCE command 成功接纳后，获得自己的 queue ID、queue_sequence、完整 prompt、scheduled provenance 与固定 route；同 task 已有 PENDING/RUNNING 不构成复用、覆盖、吞掉或合并该次输入的理由，正文相同也不去重。每次真正接纳的手动触发均保留自己的 RUN_ONCE 来源，不返回另一轮旧执行冒充本次接纳。

“独立触发”与“同次请求再次送达”分开：同一个 TIMER occurrence 沿现有 task/revision/due 身份确认，同一个 RUN_ONCE command 沿现有 command 身份确认，丢失 ACK、网络重传或轮询重复发现同一 occurrence 不生成第二条输入。不同真实时点/不同手动 command 是新输入，即使 task 和正文一致也不能复用旧项。先确认原接纳，再判断当前任务配置/route；已接纳输入的重传不能因配置后来改变而换目标或重建。继续使用现有身份构造/事务约束，不新建 occurrence ledger、补偿任务或恢复框架。

TIMER 接纳与该触发对应的 schedule 推进保持同一事务，不能先推进再丢输入或入队后重复推进；RUN_ONCE 保留既有手动触发的 schedule 生命周期 owner，不由新消息路由改写周期规则。内核运行时到点即进入输入接纳路径，ROOT 忙只影响下一安全点读取，不使触发等待 ROOT 空闲或合并到旧项；运行中的短暂调度/准备延迟不授权将多个有效触发折成一次。

**内核关闭期间未接纳的时点不补发。** 恢复时按本次内核运行起点区分已过期计划，沿现有 schedule owner 推进到未来有效时点；已过期的一次性计划沿原终态/清理 owner 收束，不为它生成补发输入。GUI 是否显示不是判断边界，内核仍运行则定时合同仍有效。不新增停机 occurrence 回放、后台补偿、心跳表或持久恢复游标。已经持久接纳的队列项仍按原读取/消费/取消/终态合同处理，不属于未触发补发，也不能因重启静默删除。

### 2.2 模型可见内容与其他消息边界

| 内容 | 原有 owner 与交付合同 |
|---|---|
| 初始 context | 派活时冻结，按新 Agent 原范围放入初始输入；不作为运行中 steer 或 ROOT 通知 |
| depends_on / materials | 接收 Work 的有序初始输入；成功依赖控制启动，materials 读取已终态完整结果/公开诊断；接收方持有自己的正文 |
| Work final / 失败诊断 | 原 Work 的真实结果；不自动向 ROOT 入队，不因成功或失败唤醒 ROOT；显式 query 或材料读取仍可获取 |
| 原生工具结果 | 原调用模型的协议结果；query 可以有完整正文，wait 使用 §3 的轻量合同；不能用 steer 代替 tool result |
| JS 内部工具结果 | 真实工具 owner 结算并交给 JS；正常不进 ROOT 模型输入；脚本决定是否输出所选内容 |
| Cell 终态后未交给 JS 的晚到工具结果 | 原真实业务结算及 canonical 结果可读；非主动 Stop 的失败 Cell 保留原正式结算材料主动交付，见 §5.4；不重跑工具、不丢弃作用事实，不借晚到结果唤醒被停止的执行 |
| Plan 问题答案/审批、权限/配置回答、用户控制反馈、Plan continuation | 各自交互/控制 owner；不是普通排队输入，不随本规格变成通用消息或被普通消息代答 |
| Hook 上下文、Stop/SubagentStop continuation、记忆、时钟、环境、能力及压缩交接上下文 | 现有上下文/控制/编译 owner；沿已批准 epoch 与 suffix 边界提供，不为“统一消息”新建排队项 |
| monitor 通知、Work 自动完成材料 | 删除其独立产品与自动 ROOT 投递路径；不删除依赖推进、wait 观察、真实结果或共享进程日志 |
| 静默 Cell 正常结束 | 只更新真实执行状态，不产生额外输入 |

工作流内部的 AGENT_MATERIAL 仍是接收方真实输入来源；删除的是 Work 完成后自动广播给 ROOT 的接线，不是材料种类。工具结果、控制结果、编译上下文与外部输入可以在同一模型 history 中出现，但不为此强行使用同一个入队动作。

context:last_n 保持现有公共初始上下文范围：按原规则选择人类/定时公共输入及 assistant 文本，不扩入 Agent 消息、协作材料、Cell runtime 输出或工具结果；源会话自己已接纳的内容仍保留。这是新 Agent 的上下文选择，不是原消息合并或撤回。

### 2.3 排队操作不是额外来源

| 操作 | 唯一合同 |
|---|---|
| 正常发送/等待队首消费 | 原 NEW_TURN 行被消费后开启 turn；idle 时也走该路径，不另造直接发送入口 |
| 排队转 steer | 现有允许范围内，同一 writer 事务将旧项设 CANCELLED，创建新的精确 steer，复制完整正文及引用并分配新 queue_sequence；不保留旧优先位置 |
| 编辑排队输入 | 保留现有允许范围；先准确取消旧项，再将原内容放回输入框；重新提交是新的接纳 |
| 取消排队输入 | ROOT 所有来源的 PENDING / NEW_TURN 均可逐项取消；只取消该行，见 §5.4 |

转换先于 final 提交，则新 steer 阻止该 final 终结原 turn；final 先提交，则转换整体失败，旧 NEW_TURN 保留。取消、转换和消费由同一 writer 裁决；不能出现旧项已取消却没有新 steer 的半次转换。原 UI 队列卡片的“发送”是转为当前任务引导，本规格不增加跳过 FIFO 的“立即开启这项”操作。

### 2.4 辅助上下文与引用归一的保留边界

Hook 额外上下文保持现有 process-local、advisory、one-shot 合同，不升级为必须持久接纳的消息。缓冲/handler 阈值阶段省略的条目不进入后续被选中的 Hook source，不形成 canonical 接纳消息；未安装准备被放弃时沿原 reservation 退役，不新增重投/恢复。已经安装的 provider 输入及已有 canonical 事实按原不可改写边界保留，不能借“advisory”撤回。诊断日志不等于被省略正文已被接纳；Hook 真实阻断、参数修改、权限及工具结算保持各自 owner，不被这个辅助上下文合同降级。

能力变化通知只需汇报最终实际生效状态，不保留或逐条投递中间开关动作，也不需要变更次数。相同能力来回变更可收敛到最终值；不同能力分别变更时，终态必须能表达各自实际状态，不能把“最后一个 target 的动作”冒充整个最终状态。复用既有能力发现/查询与生效状态 owner 提供终态，不新建操作账本/消息队列；待采用或采用失败不谎报已生效，仍遵守原 cold epoch/明确 compaction successor 边界。

同一源 Work 同时列入 depends_on 与 materials，保留现有依赖优先：依赖成功门槛已经携带该 final，接收方只取得一份同源结果。它是重复引用归一，不合并不同 Work 的结果；相同正文但不同 Work 仍分别保存/交付。不教模型故意重复声明，也不因通常不会重复而移除运行时兜底。

## 3. Work、原生工具与 JS API

### 3.1 取消自动 final 给 ROOT

所有由 ROOT 派出的 Work，无论使用原生入口还是 Code Mode，所有真实终态（包括失败/取消原因）都不自动向 ROOT 追加正文或诊断输入。真实 status/terminal_reason 使用现有公共词汇，不为规格概念另造 failed/cancelled 状态。删除仅服务此行为的 `_completion_notifications`、完成材料重驱与 `_deliver_agent_completion` 接线；保留其中仍负责调度、状态变化观察、队列推进、依赖物化与容量唤醒的必要职责。

Work final 仍通过真实 turn 的最终 assistant entry 读取；失败仍有公开诊断。query、子代理会话、状态等待和依赖解析读取真实事实，不另存 Work 报告或完成事件。未启动即取消等没有 turn 的结果仍沿原请求终态读取。

`depends_on` 仍表达成功门槛，`materials` 仍读取已结束 Work 的真实结果，正文保存为接收方自己的输入；来源会话后续退役不撤回已交付材料。初始 context 继续沿原合同输入。删除的是“每份 final 自动给 ROOT”，不是 `AGENT_MATERIAL` 这一真实来源种类。

不为错误例外自动上报每个 Work 失败。需要 ROOT 处理失败的流程，应显式等待/查询或让脚本输出选定诊断。Cell 自己未捕获的执行错误另按 §5。

### 3.2 保留现有入口，共用业务 owner

| 操作 | 原生 ROOT | JS |
|---|---|---|
| run_agent | 接纳一份工作，返回 agent/work target、真实接纳状态，不附带历史 final | 相同业务操作；保留 batch/依赖编排 |
| query_agents | 显式查询 Agent/Work；精确 Work 查询可以取得其 final／诊断 | 返回同一业务值供脚本筛选、计算；不自动进入 ROOT 输入 |
| send_agent_message | 显式发送给目标活动 turn，保留原来源与接纳结果 | 相同业务操作 |
| wait_agent | 等指定 AGENT 在等待开始时绑定的工作／原消息条件，只返回状态、绑定目标及来源，不附带结果正文 | 同一轻量业务值；不因 JS 入口而额外返回正文 |
| stop_agent | 显式停止原 target 的工作，保留原停止范围 | 相同业务操作 |

JS 的 `tools.wait_agent.works` 继续精确等待 WORK，默认 all、可选 first 等等待条件保持，但也只返回状态、目标及终止原因码，不返回 final、diagnostic、instruction 等正文。`wait_agent` 的原生/JS 入口及 ROOT/Subagent 返回同一轻量合同，不保留按入口/执行者 kind 分叉的正文投影。等消息时可返回 message_available 和来源身份，不在 wait 结果里重复正文；真实 Agent 消息继续由共同活动 turn 输入链路交付。选定 Work 的 final/诊断通过原生或 JS `query_agents({target: WORK})` 显式读取；这条查询同样供具有既有可见性的 Subagent 使用。

本轮不扩展原生 wait 的 target 联合类型，不新增 wait_cell：Cell 等待仍为 `manage_cell(action:"wait")`。原生与 JS 不维护两套 Agent/Work 状态或参数解析器；区别是模型工具结果的投影位置与脚本内部业务值，不是另一个执行引擎。

Subagent 保留现有原生 query_agents/send_agent_message/wait_agent，不获得 JS 或 ROOT 专属 run/stop 能力。相同操作的返回合同一致只覆盖实际可调用入口。本轮缩减 wait 与 run 接纳回执，不顺带改变 stop_agent 现有结果投影或 query_agents(AGENT) 的分页读取范围；“显式 query 读取结果”是编排指导，不宣称其他既有控制结果从此绝无正文。

原生 wait 仍按原等待条件自然结束。无关 Cell 输出、人类 steer 或非所等目标的变化不抢占等待，也不取消工具批次来制造安全点；投递在下一合法安全点继续。这意味着主动投递不是“任何时刻立即让模型读到”。

### 3.3 主动消息不是完成通知

`send_agent_message` 保留当前真实目标活动 turn 的接纳规则和 sender；idle/not_started 不接纳、不自动转投下一份 Work。子代理明确向 ROOT 发问/澄清，仍是主动通信，不是 Work final 自动广播。本轮不把它改成必须经 console.log 中转。UI 不能把它画成人类新请求。

### 3.4 跨 Cell 复用

Cell JS 变量互不共享；Agent/Work 独立于 Cell 存活。已知完整 target 可直接用于下一份脚本；未知时，原生或脚本内 query_agents 找到可见目标再派新 Work。脚本内部查询无须打印给 ROOT。复用同一 Agent 保留其会话，新的派活获得新 Work target；不得因切换入口创建替身或按名称猜 ID。

## 4. Cell 主动输出的内容合同

### 4.1 正式输出

- 每次 console.log 仍产生一条有序正式记录，字符串原样，其他值沿现有 JSON.stringify 规则，多参数空格连接；保留原类型/序列化错误。
- 一次调用中的多个参数组成同一条信息；连续两次调用产生两条独立信息，即使正文相同也不合成一个输入。允许在同一安全点按顺序一次读取多条 steer，不等于把其来源/接纳/取消边界合并。
- 非 undefined return 在成功结束且原必要结算完成后，产生最后一条正式记录；undefined 不生成记录。空字符串、false、0、null 不因“看起来为空”而丢失。
- 失败 Cell 不生成伪成功 return；此前已经接纳的输出继续保留。运行时外层完成/无新输出不产生额外“执行完成”材料。
- 内部工具的普通结果继续只交给 JS。不是所有工具结果都自动送给 ROOT。UI 中父 exec_cell 下的工具过程展示沿既有 canonical 数据来源，不等于模型已经收到其正文。
- console.log/return 输出不扫描 /skill 激活技能，不因含路径自动读文件，也不冒充用户批准。

输出按记录原样交付，不在运行时用模型或启发式再摘要，不按相同文本去重。脚本选择输出范围，模型上下文投递沿原完整内容/单次资源约束。

### 4.2 exec_cell 与等待结果

`exec_cell` 完成源码冻结与 Cell 接纳后，立即返回 accepted 和完整 CELL target；如果启动失败则返回真实错误。接纳成功不承诺此时仍 running，也不隐式等到输出/终态。删除其 `yield_time_ms` 参数及相关观察指导，形成硬切换，不保留无效兼容参数。

`manage_cell(action:"wait")` 保留 target 与既有单次 `yield_time_ms` 0–30000（默认1000）观察边界；等待 Cell 终态或观察超时，仅返回 target、state、pending_calls 等状态。输出到达不完成这次“等状态”的条件；输出不在 wait/exec 工具结果中再次出现。不新增累计等待次数或运行寿命 cap。

query_cells 继续提供存活运行对象的状态与原允许的源码读取，不消费主动输出。既有 `output`、`has_more` 等领取输出字段从 exec/wait 模型结果删除；派生的待交付状态如确有现有 handoff 消费者，按真实主动投递事实调整，不能继续指导模型 wait 领取。

wait 返回 failed/terminated 时可给真实状态/错误类别，不重复错误正文。完整诊断随 §5 的主动错误输入交付；主动查询原 canonical 结果时可读取正文。工具调用必须有协议要求的工具结果，不允许悬空 tool call 或用 steer 替代其接纳回执。

### 4.3 例子

```js
const research = await tools.run_agent.batch({
  facts: { instruction: "Investigate the relevant facts and report your findings." },
  risks: { instruction: "Investigate the risks and report your findings." },
  review: {
    instruction: "Review the two prerequisite findings and give the combined assessment.",
    depends_on: ({ facts, risks }) => [facts.work, risks.work]
  }
});
const result = await tools.wait_agent.works({ targets: [research.review.work] });
for (const work of result.works) {
  const selected = await tools.query_agents({ target: work.target });
  console.log(selected.final ?? selected.diagnostic ?? "The selected work ended without a text result.");
}
```

该例只输出选定 review 的结果，facts/risks 的正文直接交给 review。脚本可以输出有必要的阶段信息或供后续复用的 target，但不得在示例中一律打印整份 batch/wait 响应。真实参数/状态字段必须与实施时公共 API 对齐，不能为示例另造 schema。

## 5. 统一接纳队列、固定路由与安全点

### 5.1 唯一接纳与消费 owner

继续使用现有 `prompt_queue_items` 和 session `latest_prompt_queue_sequence`。Cell 输出增加真实 typed 来源及必要内容适配，复用同一个 session writer 接纳事务、既有队列身份与 exact confirmation；不新建输出收件箱、独立排序器、优先级、全局 registry 或第二套持久队列。

CodeModeHost 是尚未向 ROOT 接纳的输出的进程本地 owner，沿既有 deque 和资源计费借用完整有序前缀。借用可包含若干完整记录，但**每条正式记录独立成为一个 queue 输入**，保留自己的 queue ID、queue_sequence、ROOT session、源 turn、exec_cell attempt、原记录位置和正文。批量接纳/读取只摊薄调用开销，不将多次 console.log 合成一条输入。Cell 内严格有序，跨 Cell 不承诺真实全局产生顺序；各输出源公平推进，不用无限处理一个 Cell 的循环饿死其他接纳。前缀选择使用既有单次物理边界，不新增凑批定时器、累计条数或寿命 cap。

共同队列消费继续由既有 NEW_TURN admission、SteerConsumptionCoordinator、预算/compaction、Plan drain 和 final fence 负责。Cell 内容在消费后仍使用真实 `TERMINAL_OBSERVATION` entry 和既有 `TerminalObservationAccepted` occurrence，并配合既有 `PromptQueued` / `PromptConsumed` / `PromptCancelled` 等队列 occurrence；不冒充 USER_MESSAGE、USER_STEER、AGENT_MATERIAL 或用户授权。外部卡片和 steer 气泡可以复用，不要求 canonical entry kind 相同。

不能原样套用强制人类 command/client 身份的 PromptIngress DTO。将现有 queue 写入及精确确认 owner 扩展到明确的来源联合；Cell 使用源 exec attempt 与单条原记录位置构造稳定 queue 身份，不伪造 human command。利用现有 source_session/turn/tool_attempt 字段保存真实关联；只有独立必要的 typed 来源/内容字段可调整，不增加新关系表或身份注册表。

新 turn 的 Cell 输入保留源 turn 的冻结 model binding、effective mode 作为权限请求上限及 inherited_from_turn_id，不读取当前 UI 选择来替换原来源。**最终执行 permission snapshot 在该 NEW_TURN 真正消费/创建 turn 的 writer 事务中，沿原 freeze_root_permission_snapshot owner 按当前 Plan overlay 冻结**，admission_source 保持 TERMINAL_OBSERVATION；不得比源 effective mode 放宽，也不得沿排队时的旧 Plan snapshot 越过当前 Plan。准备后 Plan enter/exit/workflow 变化沿原 candidate 精确校验/重新准备，保留 queue 身份/顺序/route，不使用陈旧权限硬启动。定时 NEW_TURN 的原冻结权限及 PLAN_CONTEXT_CHANGED_BEFORE_DELIVERY 等合同保持，不将 Cell 的继承规则扩展给其他来源。

Cell 不走人类 UserPromptSubmit Hook、显式技能/文件解析或 USER_SUBMISSION 权限分支；当前 turn 的 Cell 补充使用该 turn 原权限/输入 owner。队列共享不新增前缀重建边界。

### 5.2 接纳时选路、同一可交付队列内平权

“接纳顺序”是目标 session writer 事务成功提交、获得 queue_sequence 的顺序。UI 点击、本地 console.log、网络到达、Hook 开始时间都不是跨来源排序权威；取消或消费不会重排其余项。时间戳只用于阅读，不参与 FIFO 比较。

Cell 输出和 TIMER/RUN_ONCE 每次新接纳，在同一个 writer 事务内先确认本次稳定请求/queue 身份是否已存在；已有项沿原 source/content/route 确认，不能重新选路。确定原身份未被接纳且仍允许投递时，才按当前 canonical 事实选择：

| 接纳时事实 | 固定 delivery_mode / target |
|---|---|
| ROOT 有 RUNNING turn | STEER_ACTIVE_TURN，绑定精确 target_turn_id；同一目标的各来源按 queue_sequence 消费 |
| ROOT 没有 RUNNING turn | NEW_TURN，target 为空；同人类/定时任务的未来输入按 queue_sequence 消费 |
| 原接纳/结算事实未知 | 确认原候选，不换 queue 身份、不改 target、不旁路重发 |
| Host 正在关闭、失去 writer 或对应 Cell 已被停止 | 不开始新的普通输出接纳；原未决写入仍须准确确认 |

**一旦成功接纳，delivery_mode 和 target 不再因 ROOT 后续活动状态而变化。** idle 时接纳的 Cell 输入即使排在人类消息后，也继续是独立未来 turn；不因前项启动就变成 steer。不增加 AUTO mode 或运行时“尽量附到当前轮”的再次分流。

例如 ROOT 尚无活动 turn 时已按序接纳 `用户 A → Cell C → 用户 B`，实际开始顺序是 `A turn → C turn → B turn`。A 启动后新接纳的 Cell 输出 D 可以成为 A 的 steer，已经排队的 C 保留位置。不同 Cell 记录即使同批处理，也各自固定 route/身份；两个已接纳 NEW_TURN 继续分别开启 turn，不因同批而合并。多个指向同一活动 turn 的 steer 可在同一安全点一起读取，并在同一模型调用中保持各自输入边界。全 session 有共同序号，但交付按 NEW_TURN 和精确 target 的 STEER 两种可交付范围分别 FIFO；未来任务不阻塞当前任务合法的后到补充。

普通聊天发送和 Work instruction 保持 NEW_TURN，不因接纳时有活动任务而成为 steer。定时触发与 Cell 使用上表的接纳时路由；idle 接纳的定时项即使后来 ROOT 启动，也继续独立 NEW_TURN，不自动改成 steer。Agent 主动消息仍只有精确活动目标，final 先提交则拒绝；它不因 Cell/定时触发允许 NEW_TURN 而获得自动改投能力。

普通 final 与输入接纳使用同一 writer：Cell steer 先接纳则保存 assistant 后仍不能结束 turn，下一安全点继续原 loop；final 先成立则后来 Cell 新接纳按 idle 事实排 NEW_TURN。新 turn admission 与后续接纳同样串行化：已有 NEW_TURN 项先后固定，新 turn 已成立时的新 Cell 输出才是该 turn 的 steer。Plan 的 successor、开放 interaction、手动压缩及其他现有 fence 保持原 owner 和顺序；gate 暂缓消费不授权跳过队首或重写 route，不因队首 Cell 来源而插入人类优先。

TIMER/RUN_ONCE 与 final、新 turn admission 的竞争同样使用这个 writer 边界：先接纳为 steer 则原 turn 继续，final 先成立则新请求可按 idle 排队；原请求已成功接纳后 ACK 丢失，重传只确认原 route，不能改投下一轮。定时 steer 保留真实 SCHEDULED_TASK/TIMER/RUN_ONCE 来源及完整 provenance，使用目标 turn 现有权限与共同输入 owner；任务自己的权限配置不升级/替换活动 turn 的权限，不冒充人类 steer 或触发人类 Skill/文件解析。NEW_TURN 才沿定时新轮的原模型/权限冻结合同执行，来源字段与执行权限字段分开适配，不能为复用 DTO 丢失 task provenance 或伪造 USER_SUBMISSION。

定时 DTO、canonical queue 约束、原 command/occurrence 确认、prepared 候选、消费和协议投影统一支持两种 delivery_mode；移除 SCHEDULED_TASK 只能 NEW_TURN 的硬编码，不以 HUMAN_STEER 包装来绕过来源类型。独立 RUN_ONCE 回执标识本次 queue/route/接纳事实；已有 ROOT 正在运行不等于本条补充已消费或模型已读。删除旧 coalesced 业务结果和复用旧执行的反馈，保留同一请求 UNKNOWN/重传时对原接纳的准确确认，不新建确认系统。

普通输入不取消编译、provider 流、未派发工具或工具批次，不抢占 wait。每次 provider cut 冻结后只在下一合法安全点消费追加；模型重试保持原冻结请求，不把新输出塞入已发送调用。Plan 原子终态 drain 也必须按同一序号完整消费目标 lane，按真实来源生成 entry/occurrence，保留原图片、分页、事务原子性与 exact confirmation，不能硬编码全部 USER_STEER。

### 5.3 接纳确认、所有权转移与失败

明确区分三个阶段：**Cell 本地产生输出 → ROOT 队列持久接纳 → 消费到 transcript 并在合法调用中供模型读取**。本规格交付 ACK 的边界是第二阶段，不是 transcript entry 或“模型已读”。

CodeModeHost 为每条正式记录保留不可变接纳候选。FULL 表示该条原队列身份、完整正文和来源已经在单项接纳事务提交，沿原 deque 推进连续已确认前缀并释放对应计费；以后由各自队列行独立持有和消费，Cell/VM 回收不影响正文、来源跳转或取消。可以批量准备并按序调用既有单项接纳/确认，但不增加跨项原子批次或第二套 ACK；未知项先确认，不越过它新接纳同 Cell 的后续记录而打乱顺序。消费到 transcript 不再 ACK Cell，exec/wait 也不返回同一正文。UI 阅读或卡片出现不构成 ACK。

提交结果未知时复用现有队列精确确认：FULL 才释放；NONE 且仍允许投递时由原 owner 重驱；UNKNOWN 保留原输出和原候选，只确认原 queue 身份。确定 NONE 后若需按新活动事实重新准备，保留同一单条输出身份并经原冲突/准备 owner 更新，不能将不明旧写入留着又制造第二份输入。已经存在的原行即使已 CONSUMED、CANCELLED 或 REJECTED，仍确认其曾完整接纳；不能因为不再 PENDING 而重送已取消的输出。内容/来源冲突沿原明确失败处理，不猜测成功。

数据库 commit 成功但 ACK 丢失时，内容已被接纳；Stop、用户取消或 Host 关闭不能用“尚未确认”删除本地未决 owner或换 ID 重发。FULL 确认只完成原所有权转移，不重新激活已经被取消/停止的输入或执行。

队列外尚未接纳的进程本地输出允许随崩溃丢失；已接纳队列项沿现有读取/消费规则继续，不新增 Cell 执行恢复、自动重跑、delivery job、补偿扫描、receipt 或输出账本。尚未确认的前缀继续计入现有 16 MiB/Cell、64 MiB/Host；超界保持明确资源失败，不静默丢日志或设置新的历史总量 cap。完整内容沿现有 inline/blob/artifact owner 保存，不把预览/截短内容当完整交付，也不把 monitor 的 32,000 字节/head-tail 内容合同套给 Cell。

16/64 MiB 是本地未 FULL 输出的物理计费；FULL 后由既有 queue/content owner 承担持久正文，不继续占用 Cell 缓冲。队列接纳、内容存储和单次输入预算的既有资源限制/明确拒绝继续有效，不能因迁移 ACK 而绕开；不据此新增任务累计输出或历史总量 cap。暂时无法接纳时保留原前缀/错误候选及计费，沿原进程本地 owner 在条件允许时重驱，不递归产生第二份“投递失败”错误输入，也不因必须报告错误而越过队列顺序/资源边界。真实不可恢复失败沿原 typed 失败及可读诊断收束，不伪造已交付。

### 5.4 错误、Stop 与逐项取消

源码/参数校验失败、Cell 尚未接纳的启动失败只作为 exec_cell 工具失败返回。已接纳 Cell 的未捕获错误、stalled、资源或 sidecar/Worker 失败主动接纳一次真实诊断，保持此前记录顺序；失败不产生伪成功 return。用户主动 Stop 使用真实控制反馈，不制造重复意外失败输入。正常无输出终态不产生消息。

Cell 保留一个真实终态与一份保留根因的组合诊断：首次具体脚本/工具或 sidecar 根因不得被后来的通用“Worker 已退出”、清理失败或资源错误覆盖。首次诊断接纳前，沿原错误 owner 组合已知的具体原因与必要后续信息，不要求每个底层异常分别变成 steer，不新增错误账本/事件种类；不能只取任意第一个 reason 后丢失解释失败所需的真实原因。诊断已接纳后保持原正文及 provider 前缀不变；晚到真实工具结果按下段独立结算，后续有实质新增诊断时只能沿原允许的追加路径说明，不回写原输入或用笼统错误替换根因。不制造与原失败相同的重复错误输入。

非主动 Stop 的异常终态保留原 `kind:tool_result` 正式结算合同：已真实接纳但未能交给 JS 的成功/失败工具结果，在原业务结算后成为必须报告的 Cell 结算记录，沿共同队列交付调用序号、tool name、真实状态和公共结果/准确 canonical 读取入口。先前输出、失败诊断与晚到结果按真实记录顺序追加，不能因 exec/wait 不再领取正文而只剩一个无人获知的可查询结果。正常成功交给 JS 的工具结果不因此自动上报；不是所有工具结果广播 ROOT。显式 Stop 后的结算继续准确保存，但仍遵守下表，不因此另开 turn。

Stop 当前 ROOT turn 仍按原范围停止所属 Cell，不扩大到独立 Cell/Work；停止后的真实工具作用和晚到结果继续由原 owner 结算并保留可读结果，不重跑工具，不借普通日志或晚到结果另开 turn。其他独立且未被停止的 Cell 继续正常接纳。

| 输出所处阶段 | Stop 来源 Cell | Stop 精确目标 turn |
|---|---|---|
| 只在本地、确定未向 ROOT 接纳 | 取消普通输出，不再入队；原 owner 释放计费 | 原范围停止所属 Cell，阻止后续普通输出接纳 |
| 接纳写入 UNKNOWN | 先确认原候选；NONE 后取消，FULL 后只结算所有权，不重发 | 同样准确收束；原终态和输入接纳事务决定结果 |
| 已持久接纳为 NEW_TURN、未消费 | 已接纳输入保留，不能因停止来源而自动撤回 | 独立未来队列保留，不扩大 Stop 范围 |
| 已持久接纳为 exact STEER、未消费 | 单独停止来源不撤回目标已接纳输入 | 沿原终态事务将 pending 项明确 REJECTED，不转投后续 turn |
| 已消费到 transcript | 保留 entry | 保留 entry；停止执行不承诺模型必读 |

**用户可取消 ROOT 任意来源的单条 PENDING / NEW_TURN 输入。** 复用现有 queue action、用户控制授权、writer/CAS、command 幂等、PromptCancelled 和准确确认：只把所选 queue 行设 CANCELLED，不删除正文/来源事实、不重排余项、不产生新的模型输入。动作 command 是用户操作身份，不要求来源输入本身有人类 command。禁止用 sourceKind=command 或 SCHEDULED_TASK/Cell 来源过滤取消能力；同时不扩展到 ROOT 列表之外的 Agent Work 或已消费记录。

取消 Cell 输入只取消所选的一条正式记录，不停止 Cell、不屏蔽它今后的新输出、不撤回其工具作用，不影响同批交付的其他记录，也不重新投递被取消的记录。取消定时输入只取消这次 TIMER/RUN_ONCE 已触发项，不禁用/删除任务，不撤回已接纳触发的 schedule 推进，不影响后续触发。同一 task 的其他排队项不被批量取消；取消必须使用精确 queue ID，不使用 task/Cell 身份当批量目标。

暂停、删除或切换定时任务，不撤回已经接纳为 exact STEER 的输入，即使尚未到安全点也让目标 turn 正常消费。任务管理先于该次触发接纳生效，则沿原任务状态/版本门禁阻止后续接纳；输入先接纳则管理动作不得悄悄删除它。既有任务管理取消未来 NEW_TURN 项的范围保持，与当前 turn steer 区分；不得把“按task取消所有PENDING”的旧查询直接用于新scheduled steer，也不借这个规则绕过真实用户Stop/目标turn终态的既有结算。

**PAUSED 的一次性任务在原执行时刻到达后自动静默删除。** 到期以数据库时钟为准，复用既有 tick、任务查询和状态 writer 在同一事务内核对 PAUSED、一次性计划、原 due time 与 revision，再沿原删除 owner 收束；不补发输入、不通知模型或用户、不新增 job、事件或恢复账本。清理尚未运行不能让到期后的 resume 复活该任务：管理写入同样先判断数据库中的到期事实，并保留原 command 的已提交结果确认优先级。暂停的周期任务继续遵循原周期规则。到期删除仅沿既有任务管理范围取消未来 NEW_TURN 项，不撤回已接纳的 scheduled steer。

取消与消费使用同一 writer：取消先成立则该项不能开 turn；消费先成立则返回已消费/不可取消，前端呈现真实执行状态，不谎称取消成功或转而停止新 turn。ACK 丢失确认同一次操作，UNKNOWN 保留待确认状态；不允许从 UI 先删掉后默认成功。已取消项不再参与消费，后续 ACK/重连/查询不能恢复它。

编辑和手动转 steer 保持当前允许范围：人类项保留两者；已排 NEW_TURN 的定时输入保留现有“取消原输入并将正文放回输入框”的编辑，之后重新发送是新的人类输入，不修改定时任务定义或后续触发，不新增将该已排项手动转 steer 的能力。接纳时因 ROOT 活动而选择定时 steer 是 §5.2 的自动路由，不是这个队列操作。Cell 输入不新增编辑/手动转 steer。现有 Plan draft review 的 revise 仍是独立真实控制，不能借它绕过来源限制。

### 5.5 删除 monitor，收敛单一队列路径

删除 manage_monitor/query_monitors 工具、schema、注册/取消/查询、monitor coordinator、策略/预算/生命周期、进程回调、独立自主投递循环及其 UI/环境索引/提示词/Skill/活动文档说明。共享进程日志、poll/wait、真实工具结算和仍有独立用途的资源 owner 保留；不隐藏入口后保留后台实现，不换名保存订阅或给 Cell 建假 Process/Monitor。

只复用原自主输出中确有用途的 model/permission 继承、compaction reservation 和精确提交确认职责，统一到 §5.1 的队列接纳与消费 owner。移除绕过队列的 ExistingTurn/NewTurn 直接安装及“存在人类队首便让路”的分叉，不维护两种输出消费/ACK。保留既有 TERMINAL_OBSERVATION entry/occurrence 家族，但将 monitor/process 强制字段的正文 codec 硬切为真实 Cell 来源和完整记录；provider planning/lowering、reader、预算、Plan drain、协议和 UI 同步更新，不添加兼容双读或新的 durable 分类。

## 6. 统一排队卡片、来源与当前 turn 展示

### 6.1 ROOT 未来 turn 的头尾队列预览

ROOT 当前会话所有已经持久接纳的 `PENDING / NEW_TURN` 输入属于输入框上方同一排队列表的读取与计数范围，复用现有消息卡片。人类输入、Cell 正式输出/错误、TIMER 和 RUN_ONCE 按同一 queue_sequence 排列；不按来源分组，不设后台隐藏队列，不因卡片是否含人类 command 而过滤。可见卡片仅按下述位置预算选取。ROOT 派给 Subagent 的 Work 属于目标 Agent 队列，不混入此列表。

队列卡片仅来自已确认的 PENDING / NEW_TURN 行，不把本地 sending、unknown、cancelled、rejected、consumed 记录补成排队卡片。未确认提交原文保留在输入框，异常沿现有 toast 提示；查询仍按原 command 身份进行，不自动重发。准确确认取消/拒绝/消费后退出队列，不保留终态卡片，也不计入头尾或省略数；精确终态回执覆盖尚未刷新的旧 pending 页面。已确认 pending 项不得仅因本地曾把提交画成聊天气泡而从列表消失；复用 queue ID/原提交关联避免重复，按服务端 queue_sequence 排列，不用 optimistic 删除伪装取消成功。

列表计数、后台分页读取和卡片覆盖同一 ROOT / PENDING / NEW_TURN 范围；不把 pending steer 算成未来轮次，不只统计用户 command，不丢掉页面外其他来源。复用现有 canonical 控制投影、队列读取/分页和实时刷新，自动读至队尾，不展示“加载更多排队任务”。不新增 live event kind、独立队列缓存权威或全量历史列表；读取失败保留已确认记录，沿现有刷新/重连重读。

预览预算复用现有布局测量：H 为会话高度扣除原有 105px 顶部保留区、输入框等非队列区域和展开 TODO 占用后的可用高度；K = max(2, floor((H - 24px) / R))，24px 预留中间提示，卡片宽度大于 480px 时 R=60px，窄卡 R=80px，均为含间距的近似展示高度。首次有效测量前使用 5 个名额，不以未布局的零尺寸重置预算。N 为当前已知、确认待处理的队列卡片条数，N≤K 全显；N>K 时显示前 ceil(K/2) 条和后 floor(K/2) 条，中间淡灰文字“中间省略 N-K 条排队消息”，无加载/展开中间队列按钮。奇数名额多一条给队头；可见区不重叠、不按来源倾斜。已取消编辑的原文与新草稿冲突时，原 action 继续保留恢复内容，沿现有 toast 提醒先处理草稿；不生成队列卡片，清空草稿后仍准确恢复。

例如 K=5、N=50，显示 1/2/3、提示省略45条、49/50；新增51后显示1/2/3、提示省略46条、50/51，49仅进入省略区。消费或取消1后显示2/3/4、提示省略45条、50/51。预算只随窗口、输入框和 TODO 占用变化，不按已渲染卡片高度反馈计算；展开单张正文不挤掉其他卡片。仍保留现有高度边界内滚动供正文展开或极小窗口阅读。进入省略区的卡片直接隐藏，只有真正离开已知队列的可见卡片才执行原有退出动画并立即 inert；省略不改变后端 FIFO、消息身份、内容或取消范围。

后台分页未齐且尚有未知项时，不能把已加载页末当真实队尾：暂只显示已知队头，灰字“还有 X 条排队消息”（已知隐藏项加未知项）；读齐后切换为准确头尾和中间省略数。未确认本地提交不占队列位置，不为视觉位置伪造接纳序号。省略数不包含退出动画残影，不用有意延迟消费来保持可见时长。

所有排队卡片仅在左侧显示三类通用图标，不显示来源副行、来源名称、Cell/Agent/任务标识、手动/自动触发文字或查看源码/定位任务等来源链接；不把这些精确信息移入 tooltip。可保留通用无障碍名称。

| 来源类别 | 左侧图标 | 展示要求 |
|---|---|---|
| 人类输入 | 现有 CornerDownRight | 保留正文、附件、批注及既有阅读能力 |
| 助手 / Agent / Cell 输入（含 log、error、tool_result） | Bot | 每条正式记录独立一项，不合并 log；保留真实错误正文、诊断和调用/结果，不另造错误来源副行 |
| TIMER / RUN_ONCE 输入 | AlarmClock | 两者使用同一图标；每次独立触发保留新输入及原 trigger provenance，不返回旧执行冒充本次接纳 |

来源的精确身份、queue 关联、Cell 正式记录类型和定时 trigger provenance 仍保存在原元数据中；展示简化不修改接纳/消费、FIFO、计数、分页、权限或取消目标。队列展示不为来源标签读取任务名称。排队正文按卡片宽度自然换行，不设横向字符数阈值；沿用当前小号字体 line-height × 2 的自然高度阈值。未超高时完整自然显示；仅自然排版高度超阈值且未人工展开时压为一行预览，右侧同一行以“...”展开，收起也位于同一右侧位置，不新增垂直操作行或原生省略伪行。测量复用现有正文 DOM 的自然排版与 ResizeObserver，不以压缩后的单行高度判定，不复制 Markdown DOM 或截断正文字符串。正文展开可读 Markdown、复制完整原文或沿已有完整内容入口阅读；UI 预览不替代接纳正文、不改变模型输入，不增加新的正文总长度 cap。本轮 steer / 自动材料气泡继续遵守 §6.3 的来源与定位展示。

已入队 A、Cell C、B 就显示三项同序卡片；A 开始时只消费 A，C 仍在 B 前等待独立 turn。开始处理后沿对应 turn 起点保留真实来源及原 queue 关联，卡片准确退出待处理列表；即使 idle 接纳后很快启动、用户来不及看到 pending 动画，也能从 turn 起点看出来源。不人为延迟执行以保证卡片展示时长，不把新 turn 画回旧处理过程。真正尚未触发的定时计划留在任务页面，不提前变成队列项。

### 6.2 卡片操作范围

| ROOT 队列卡片来源 | 取消这一条排队输入 | 编辑 | 转为当前 turn steer |
|---|---|---|---|
| 人类 NEW_TURN | 支持 | 保持现有范围和草稿保护 | 保持现有目标及权限/Plan 范围 |
| Cell 输出/错误/异常结算 NEW_TURN | 支持 | 不新增 | 不新增 |
| TIMER / RUN_ONCE NEW_TURN | 支持 | 保留现有取消并恢复正文的编辑 | 不支持，保持现有来源限制 |

操作仍受现有 controller/observer 和用户控制权限限制；observer 读取同一队列并使用相同头尾预览，但不能操作。所有排队卡片只渲染当前可操作的“发送”“编辑”“删除”按钮，不显示灰色禁用操作；各按钮沿现有完整可用条件决定是否显示，包含来源限制、接纳状态、busy/UNKNOWN、编辑保护、控制权限及发送的运行/Plan 条件，不简化成仅按来源过滤。操作统一在卡片右上角以纯图标呈现，顺序为“复制、编辑、删除、发送”；发送使用向左弯箭头 CornerDownLeft。复制保留既有可读条件及 observer 阅读能力，其余操作沿上述可用条件隐藏；无任何按钮时不保留空操作容器。正文以真实布局为右上操作组预留空间，窄卡也不把操作移到底部或遮挡正文。按钮默认安静可见；简短说明使用 document.body portal 的 fixed 独立浮层 tooltip，不参与卡片/队列布局或 overflow，pointer-events:none，不遮挡队列点击；hover 不改变卡片尺寸、间距或正文位置。支持 hover 与键盘 focus，Escape、点击或滚动隐藏。按钮和 tooltip 使用小幅淡入/位移/缩放过渡，prefers-reduced-motion 下不产生位移动画。点击编辑/删除/发送后，该项操作图标替换为同尺寸、不可重复点击的小转圈；包括准备编辑原文、submitting 和 UNKNOWN 确认阶段。原 pending 消息在结果确认前保留，不另占一行显示操作状态；确认后恢复图标、恢复编辑原文或退出卡片，失败/未知沿现有 toast 反馈。不以固定时长把未知冒充完成，不刻意停留以保证加载可见；prefers-reduced-motion 下图标静止。取消按钮明确为取消排队输入，不标成停止 Cell、停止任务或关闭定时计划；四项操作的 aria-label 与 tooltip 文案分别严格为“复制”“编辑”“删除”“发送”。等待确认的转圈与后续按钮恢复/卡片退出基于原操作事实，不通过按钮显示与否猜测后端结果；取消先赢、消费先赢和 UNKNOWN 的展示遵守 §5.4。多条Cell记录即使批量到达也保留独立卡片/取消目标，不因UI折叠扩大取消范围。

### 6.3 当前 turn 的 steer 与材料

steer 是“补充给当前 turn”的交付语义，AGENT_MATERIAL/Cell 输出/Agent 消息是内容来源；不得把来源抹平成人类角色。当前 turn 内的这些补充输入复用现有 steer 气泡、折叠、复制和过程分组，按真实顺序显示，不再用“大段任务材料正文”打断整个处理过程。

| 来源 | 默认展示 |
|---|---|
| Agent 主动消息/现存补充材料 | 真实 Agent 名称、短提示、可展开正文；来源 Agent/Work 可点击定位 |
| Cell 正式输出 | Cell 身份/脚本输出标记、可展开正文；可跳对应冻结源码 |
| Cell 错误 | 在同一过程位置显示错误状态与可展开真实诊断 |
| TIMER / RUN_ONCE steer | 定时任务名称及自动/手动触发标识、可展开本次完整正文、任务来源跳转；放在当前 turn 处理过程中，不冒充用户或未来队列卡片 |
| 新 Work 的初始 context/依赖/materials | 保留新 Work 的原输入位置及各自 entry，不伪装成中途 steer |

默认折叠长正文，展开可读 Markdown 和复制原文；不设置新的正文总长度 cap，不把完整内容换成摘要。复用当前组件能力，缺少的折叠/Markdown 阅读仅加在现有输入阅读组件的来源分支，不另建“材料消息系统”。模型收到完整材料与前端折叠互不影响。

Agent/Work 跳转复用已有详情与精确 Work 定位，不开启输入框或发送内容。来源 Agent 已删除时显示真实不可定位结果，接收方自己已经保存的材料仍可阅读；不通过源会话复活或补发 Work。

Cell 输出使 idle ROOT 开启新 turn 时，保留真实 runtime 输入起点；不能为视觉连续谎称它属于已经结束的旧 turn。只在同一真实 turn 内合并过程展示，canonical entry、source 和实际边界不变。

## 7. 正文与批注的技能/文件引用

### 7.1 输入范围

主输入框普通正文与批注 comment 都支持相同的 `/技能名`、`@文件/目录` 候选、插入、显示和发送内容。正常发送、排队、排队转 steer 沿同一 prompt 内容；转为 steer 不重新做另一套字符串替换，也不丢批注和已插入引用。

批注 quote 始终是历史引用，排除技能激活/文件引用识别；自动材料、Cell 输出、工具结果及模型回复也不适用用户显式调用解析。保留 `prompt_text_projection` 中“正文＋comment，排除quote”的真实意图边界。

### 7.2 技能识别与激活

用户在正文/comment 的普通文本手打合法 `/name`，或选择候选插入，均可表达显式技能引用。代码跨度/代码块、明确引用内容、URL/文件路径等不识别为技能。未知名称保持普通文本，不画成“已使用技能”；可用目录失败按既有不可用状态处理，不能假装验证成功。

前后端共用一致的识别规则和上下文边界；不再全局对任何字符串做 slash→chip 替换。标签只表达技能引用，不证明技能已读取、已执行或获得权限。后端继续按原 activation subject、有效技能 owner、cold epoch/明确 compaction successor 执行；不能因为 steer/comment 有新引用而重建已有 SYSTEM/tools。显示和候选支持不等于本轮新增 prefix rebase 边界。

批注 comment 使用与主输入框一致的候选数据 owner、取消/光标/IME/键盘选择语义。可以提取现有 suggestions 与引用编辑能力用于两处，不必把整个主输入框及附件操作复制进批注。quote 选择、批注来源范围、撤销/重做保持完整。

### 7.3 文件引用

两处 @ 候选都使用已有工作区路径补全/文件导入 owner 与规范化引用文字；保留现有 file-reference 的完整路径表达，文件/目录显示一致。@ 是引用入口，不是自动注入文件全文，也不自行授予模型外部文件权限。

普通手打 @xxx 在未形成既有合法文件引用时保持普通文本，不凭正则猜路径或静默读取文件。批注可以输入普通路径供模型按工具读取，但不能把普通路径冒充已选择/导入的附件。被引用 quote 中已有的 @ 或规范化文件文字仍忠实展示，不变成新的主动文件引入。

历史显示以已发送原文及来源事实为准；当前目录/技能目录变动不改写历史 prompt。失效来源可展示无法定位/当前不可用，不凭当前候选假造当时使用情况。不新增引用证明、指纹、读取回执或持久技能使用注册表。

## 8. Cell 源码与实时图生命周期

### 8.1 运行对象可以回收，源码继续阅读

源码继续使用源 `exec_cell` attempt 的 canonical code_source inline/blob，内容是 Hook 处理后实际执行的 JS。code/path 同一冻结来源；之后文件修改/删除不改变记录。不复制 `.js`，不从最初参数重构，不新增历史 Cell 表或代码哈希。

源码阅读使用 ROOT/session＋canonical source attempt 作为稳定阅读身份，不以当前 Host＋live Cell 为授权前提。已有 Cell target 可从原 exec_cell canonical 工具结果/attempt 的既有关系定位 source；打开 tab 时保存既有 attempt 身份和阅读位置，不能只保存转瞬即逝的 Host ID。检索必须核对来源属于可访问的该 ROOT/session，任意 attempt ID 不能越权。

点击历史 exec_cell 的记录可再次打开源码；已打开源码页在 Cell 回收、Host 置换、应用重启后仍可按 canonical 来源读取。存活 Cell 列表保持现有查询范围，不为了“源码随时可看”另建无限历史列表；历史入口来自真实会话工具记录。数据确因会话删除/内容 retention 等不可读时才给准确不可用提示。“随时”不意味着源会话被删除后仍永久保留。

源码阅读不保活 VM、未交付输出队列、Agent 或 Work。query_cells 的模型运行对象查询仍是 live scope；用户历史源码阅读独立于它，不借历史阅读恢复已失效 Cell 操作 target。

### 8.2 实时工作流图

沿现有 Cell 实际关联 Work/参与观察生成，不通过源码猜流程。图仍是实时运行观察，回收后不扫描历史记录重建，不增加参与事件或历史图快照。

确认图的原运行时对象已回收/Host 已结束后，清理节点、连线与运行时缓存，保留已打开 tab，显示固定温和文案：**实时工作流视图已关闭**。不显示“对象已不可用”，不宣称所有 Work 已完成，不自动改绑新的 Cell。短暂网络错误保留已有图并说明刷新失败，不伪装为确认关闭。

源码页在图仍可读时提供原“查看工作流图”入口；图确认关闭后移除该实时入口，源码内容继续显示。用户可关闭图 tab；重复点击旧历史入口不重新生成已回收图。Cell 已终态但尚未回收、图仍实际可读时可以继续展示真实状态。

### 8.3 回收条件与控制

保留现有物理终态、VM 已释放、内部 pending 为空和结算完成条件；输出是否已消费改由 §5 主动输入接纳结算裁决。无输出的正常完成无需等待一个模型永远不会调用的 wait/terminal ACK，内部终态处理完成后即可回收。

有输出但尚未 FULL 确认、有 immutable UNKNOWN 安装、未结算工具、借用中的交付前缀时不可回收；VM 可以按原合同先释放。UI 读取不设置 terminal_delivered，不消费输出，不延迟回收。不新增 Cell TTL、数量/寿命 cap，也不为了温和提示保留运行时 tombstone。

用户停止仍使用精确 live Cell 控制目标；历史源码只读，没有“再次停止”或隐式重跑。重新执行是新的 exec_cell/new attempt，不复活旧对象。

## 9. 提示词与 Skill hard cut

1. 明确区分“普通工具结果先给 JS”和“console.log 是主动交给 ROOT 的正式输出”。不要将全部输出称为调试日志。
2. 编排 Skill、coordination/workflows/examples、原生工具说明同步移除每个 Work 自动向 ROOT 发 final 的承诺。
3. 示例只输出真正需要 ROOT 处理的结果、诊断、阶段或供跨 Cell 使用的目标；移除无条件 console.log(batch)／console.log(整个wait响应) 的套路。需要查看整体对象时允许显式打印，不做运行时文本审查。
4. JS 中间结果通过 depends_on/materials 或脚本计算流转；简单单次动作可用原生工具，不要求所有操作都 Code Mode。
5. 不教授非 undefined return 作为输出入口，不把 async 包装实现写成模型必须理解的工作流语法；保留运行时兜底和开发者测试。
6. 不再指导反复 wait 领取输出。所有实际可调用的原生/JS wait 只负责状态/编排；ROOT/Subagent/脚本主动读取 Work 结果时指导 query，脚本想交付时 console.log；Subagent 只有原有原生入口。删除按入口或调用者提供不同 wait 正文的说明和示例，不借此缩减 stop 等未改动的既有控制结果。
7. 说明 Agent target 可跨 Cell 复用、变量不共享；不鼓励用新 Agent 替代可复用的同一会话。
8. SYSTEM/provider tools/schema 改动只在新 cold epoch 或明确采用的 compaction successor 安装，已有 epoch 保持原字节、messages 仅追加；不加兼容双轨或静默 rebase。

## 10. 实施 owner 与持久化约束

| 位置 | 工作 |
|---|---|
| code_mode/host.py、contracts.py、sidecar/worker | 单条输出身份与借用、状态等待、return兜底、保留根因的组合诊断、终态/资源结算与回收 |
| conversation_kernel/tool_runtime.py | exec接纳结果、wait状态投影，移除旧output observation领取/ACK接线，保留普通工具业务结算 |
| Host、repository prompts/subagents、steer、steer_consumption、safe_point与dispatch | 共用queue接纳/精确确认、session序号、固定route、NEW_TURN/target FIFO、Cell真实内容/权限与Plan/final消费；删除独立直装分支 |
| subagent.py、host.py、共同Work读取/等待 | 删除Work完成自动ROOT材料及专用pending refs；保留依赖、调度、查询、原生/JS入口与明确消息 |
| terminal monitor、工具目录、Host与相关消费者 | 删除 monitor 独立产品路径；共享输入安装只服务现有明确来源，不保留 monitor 兼容层 |
| desktop source读取、inspector、agent-cell workspace | canonical attempt源码阅读与历史入口；实时图关闭状态与错误区分 |
| 队列读取/协议、runtime adapter、composer与会话组件 | ROOT所有NEW_TURN完整投影/计数/分页、同序同卡片来源展示、逐项取消和准确竞态反馈；编辑/转steer保持范围 |
| scheduling service、repository scheduling/prompts、scheduled DTO与协议/前端 | 删除按task的pending/running复用/合并；每次独立触发共用writer接纳固定route，保留同请求幂等、schedule原子推进、scheduled steer来源/反馈与准确确认 |
| capability change observation、Host与既有能力发现/生效状态owner | 通知收敛为实际生效终态，不广播中间动作或次数；保持辅助状态及原provider前缀边界，不造操作账本 |
| prompt内容阅读、会话组件 | 来源明确的steer复用、过程连续、正文展开/Markdown与精确来源跳转 |
| prompt-suggestions、composer、批注与后端用户引用提取 | 两处引用共用，quote/code/材料排除，目录与激活规则一致 |
| specs、bundled Skill、tests、dogfood | 删除被替代说明和例子，以新单路径验收 |

durable oracle 的 event kind、relation、job、subject、append guard 类别增量目标全部为零；不新增 Work final 副本、Cell历史表、输出恢复账本或 registry。原 runtime observation 内容的真实 Cell 来源不是新的 subject slot。若具体实现需要改变既有内容 codec，更新原 typed codec/consumer，不引入旧/新兼容读取双轨；若发现无法满足零增量，必须先指出具体需求并修订规格，不能偷偷补表。

现有 queue 的来源约束、内容联合、model/permission分支及消费者需要完整硬切适配 Cell；不以增加别名来源或假 human command 绕过约束。将输出接纳到既有持久队列的独立产品理由是跨来源 FIFO、统一的头尾预览与逐项取消；权威仍是 canonical queue 行，接纳/取消/消费各使用现有 writer 事务，失败由原 exact confirmation/明确拒绝收束。它不引入新的持久执行恢复承诺。

## 11. 验收矩阵与激活

| 范围 | 必须证明的行为 |
|---|---|
| 原生/JS入口 | 相同target/参数/Hook/权限/副作用owner；原生全部保留；原生/JS及ROOT/Subagent的wait均无final/diagnostic/instruction正文；query显式读取；消息依旧经共同输入链路交付 |
| 静默Work | 原生/JS创建的Work成功、失败、取消、依赖失败均不自动进ROOT输入，不自动新turn；query仍可读真实结果 |
| 内部协作 | A/B结果直接给依赖它们的review；ROOT只看到console选中的review结果；源Agent删除不撤回已交付材料 |
| 消息清单 | §2各来源逐条有生产/接纳/消费/显示owner；初始context/依赖材料、Work结果、原生/JS工具结果、late结果、交互控制与编译上下文无遗漏或误入外部队列；Code Mode仍仅ROOT |
| 主动输出 | 一次console.log一条信息，多次调用各有独立ID/entry/序号，两个steer可同安全点同调用交付不合并；idle各自NEW_TURN；不抢占provider/tool批次 |
| 平权/固定route | 人类A→Cell C→人类B及TIMER/RUN_ONCE混合顺序与启动顺序一致；C不因A启动变steer；A运行中新接纳D可为A的steer；无来源优先或跨lane伪总FIFO |
| 接纳/终态竞争 | Cell steer先接纳则final不结束turn；final先提交则Cell可按idle排NEW_TURN；Agent消息/人类精确steer仍拒绝旧target；新turn admission、Plan successor和compaction fence不绕行 |
| 排队权限 | Cell保留source model/effective上限；等待期间Plan enter/exit/workflow变化，消费按当前overlay冻结runtime权限且candidate不使用旧值；scheduled NEW权限/Plan拒绝保持；定时steer沿目标turn权限不授权升级 |
| 输出类型 | console多参数/字符串/空值序列化；return false/0/null/空字符串/undefined；无输出成功不唤醒；失败不发成功return |
| wait/查询 | wait只等状态不领正文、输出不误唤醒状态等待；query不消费；没有wait也可收到主动输出；tool调用均闭合 |
| 输出确认 | queue FULL即释放本地前缀，无第二次transcript ACK；NONE沿原owner准备；UNKNOWN确认旧ID/route；入队后已消费/取消/拒绝再确认不重送；正文和来源完整，不伪装human command |
| Stop与提交未知 | 屏障覆盖Stop先于提交、commit成功但响应丢失后Stop、FULL/NONE/UNKNOWN；不把未确认当未接纳、不撤回已提交输入、不换ID重发、不因晚到确认重新唤醒已停止执行 |
| monitor删除 | 工具/schema/UI/索引/提示词无monitor产品入口；无专用注册/观察/唤醒循环或换名替身；共享进程wait/日志与Cell输入安装正常 |
| 大输出/资源 | 原单条/模型页/artifact与本地未FULL的16/64MiB合同保持；FULL后交queue/content owner；接纳受阻保留原输出/错误候选不递归造错；无静默截断/无限副本/新总量cap |
| 错误/停止 | 启动失败仅tool错误；接纳后未捕获错误主动报告；非Stop失败后未交JS的late成功/失败工具结果正式结算材料入队；显式Stop只保存真实结算不复活；Host关闭不新开turn |
| 根因诊断 | 具体脚本错误后再有Worker退出/资源错误，保留主因并组合必要后续信息；不以通用错误覆盖、不逐异常广播；已接纳诊断不改写 |
| 长程/并发 | 多Cell混合输出，各Cell内序保留，人类和其他Cell不饿死；很多turn/Work后仍可用；无任务总调用次数限制 |
| ROOT排队UI | 所有来源PENDING/NEW_TURN同序读取、计数一致；按空间预算显示头尾，中间省略准确条数；50→51追加、队首消费、缩放及未读齐页末均不冒充真实队尾；省略不播放消费动画；未确认提交保留输入框原文，异常仅 toast，不占队列；终态立即退出，精确取消覆盖陈旧页面；操作在对应图标转圈、无额外状态行；三类通用来源图标，无来源副行或来源定位链接；右上按复制/编辑/删除/发送排列当前可用的图标与同词hover提示，observer保留阅读/复制；正文自然换行，超原两行自然高度阈值才压单行，右侧同一行展开/收起；快速开始仍在turn起点标来源 |
| 取消排队 | 人类/Cell输出和错误/TIMER/RUN_ONCE均能精确取消单行；Cell多记录批量交付仍只取消所选记录；不停止Cell/回滚工具/关闭定时或改变后续触发；来源已消失仍可取消；编辑/转steer不扩权 |
| 取消竞态 | 取消先提交不启动、消费先提交准确已消费且不停止turn、丢失ACK确认原操作；已取消输出不因Cell ACK/重连重投；其他排队项不受影响 |
| 定时独立触发 | 同task已有pending/running时，新的TIMER时点/独立RUN_ONCE各有新queue ID/序号/完整正文/provenance；活动为exact steer，idle为NEW；取消单项不影响其他触发，无task/正文级合并 |
| 定时幂等/竞态 | 同occurrence/command重复发现、重传及ACK丢失只确认原项/route；不同真实触发即使同task同正文仍各接纳；TIMER接纳和schedule推进原子，final两种提交顺序及Stop按既有共同边界结算 |
| 定时UI | 所有定时NEW项纳入同序头尾预览；活动定时steer在过程内保留来源；每次新RUN_ONCE反馈本次route，重传不重复显示，不用“合并/旧执行运行中”代替新输入接纳 |
| 停机与任务管理 | 内核停机未接纳时点不补发，重启不丢已接纳队列；内核运行中ROOT忙不抑制触发；暂停/删除/切换先后与steer接纳线性化，已接纳steer不撤回，未来NEW取消范围不扩至steer；PAUSED一次性任务按DB时钟到期静默删除，清理延迟不允许resume复活，不补发/不通知，周期PAUSED规则保持 |
| 辅助/引用保留 | Hook被省略正文不冒充canonical已接纳消息；能力通知只表达实际生效终态不逐操作计数；同Work依赖/material重复引用仍一份，不同Work同文仍两份；last_n范围保持 |
| 材料UI | 同turn过程不被材料切开；来源不冒充用户；长材料默认折叠、展开Markdown/复制；跳Agent/Work/源码准确 |
| 引用 | 主正文/comment均有相同候选、光标、IME、撤销、选择和发送表现；排队转steer内容不丢；quote/code/URL/材料不误识别 |
| 引用边界 | 未知/失效技能不冒充已执行；@不自动全文注入或越权读取；既有epoch不因新引用rebase |
| 源码 | Hook后实际源码、code/path、原文件删改、Cell回收、Host置换、重启后均能按已有attempt读取；历史读取越权被拒 |
| 图 | live按原关联显示；确认退役温和关闭、节点清空、源码仍读；临时网络故障不误判；不推断所有Work结束 |
| 回收 | 静默终态无需模型ACK即可回收；未FULL输出/UNKNOWN/pending工具阻止回收；阅读不保活；无TTL或新tombstone |
| 提示词/合同 | 无自动ROOT final承诺、无wait领输出指导、无显式return教学；无新provider前缀边界、无dual path |

先用根 `.venv` 的聚焦单元/真实PostgreSQL集成/前端测试迭代；不弱化断言、加skip/xfail。真实 provider dogfood 使用保存生产设置，经 LocalSettingsStore/require_pulsara_home 只读加载，必要时注入已核实的隔离数据库；不修改用户模型、代理或凭据。秘密只按实际值清理，保留有效输入/输出和失败证据。

激活至少覆盖一次“ROOT启动多阶段Cell后先回复并idle，选定console输出入队再唤醒处理”，一次“ROOT活动且多工具批次进行中输出到达”，一次“中间Work失败由脚本处理但ROOT未收到自动final”，一次“人类/Cell/TIMER/RUN_ONCE混合排队同序读取、头尾预览并逐项取消”，以及原生简单任务与完整引用/历史源码阅读路径。

只有实现、交叉审阅、所需测试及真实 provider 证据均完成后，才将状态改为实施完成。按本轮人类决议，无需 Playwright 或视觉验收门禁；必要前端单元测试、静态检查与构建仍须完成。产品/实施规格冻结与代码激活是两个状态。
