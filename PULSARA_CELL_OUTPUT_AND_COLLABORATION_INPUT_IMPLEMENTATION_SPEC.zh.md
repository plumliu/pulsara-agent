# Pulsara：Cell 主动输出、协作结果选择与会话阅读实施规格

日期：2026-10-31。状态：**产品决议整理完成，待实施；本轮未复审。** 本轮只写文档，不修改代码、测试、数据库或用户配置。

## 0. 目标与权威

ROOT 应能派出并行、多阶段工作流，让中间结果在 Agent 间传递，自己只处理选中的结果。Cell 主动输出代替模型反复领取输出；原生工具保留，供简单派活、查询、等待、消息与停止。用户可阅读完整历史源码，而实时工作流图随运行时结束关闭。

根 AGENTS.md 与当前人类决议优先。本规格在本轮范围内替代以下文档中的冲突条款，实施时须同步正文、测试、Skill、示例及开发说明，形成单路径 hard cut：

- [Code Mode 执行与生命周期](PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md)：输出由 exec/wait 领取、return、终态消费与回收。
- [Agent／Work 设计](PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md)：Work 完成自动通知 ROOT，以及原生/JS 结果展示。
- [子代理与 Cell 侧栏](PULSARA_SUBAGENT_CELL_SIDEBAR_DESIGN_RESEARCH.zh.md)：Cell 退役导致源码不可读的合同。
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
| Cell 输出 | 产生后进入主动投递链路；活动 ROOT 在安全点接收，空闲 ROOT 按 monitor 的既有自主接纳语义开启处理 |
| 正常结束 | 无新输出的正常结束不额外向 ROOT 推送消息；UI/查询仍可读取状态 |
| return | 非 undefined 的值保留为正式输出兜底；提示词与 Skill 不主动教授 return，不改变运行包装为 async 函数体的实现 |
| 等待 | 保留等待能力，只返回真实目标状态，不再领取、重复注入正文 |
| 材料展示 | 当前 turn 补充材料复用 steer 气泡和过程分组，保留来源；已有来源链接可跳到 Agent/Work |
| 引用 | 主输入框正文与批注评论都支持 /技能、@文件；排队、转 steer 保留原内容；被引用的 quote 不识别 |
| Cell 源码 | 从已冻结数据库内容阅读，不依赖 live Cell；可在运行结束、回收或应用重启后打开 |
| 工作流图 | 保持实时视图；运行时图退役后不重建历史图，已打开 tab 温和显示“实时工作流视图已关闭” |

上述“主动输出”是脚本请求 ROOT 处理材料，不是人类授权。console.log 不承担普通调试日志用途；不要求每个中间 Work 都打印结果。

## 2. 当前实现事实与实施边界

当前 `exec_cell` 启动后调用 `CodeModeHost.observe`，`manage_cell(action:"wait")` 也调用同一个 observe；两者通过 `CodeModeObservation` 返回 target、state、output、has_more、pending_calls 与可用 error。ROOT 的工具结果 canonical commit 后 ACK observation，才消费输出和推进终态回收。

当前 console 输出先进入 Cell deque；非 undefined return 也作为输出。普通工具结果先交给 JS，只有选中的输出进入模型；VM 提前退出而未交给 JS 的在途结果有额外真实结算路径，不能丢失。

当前每个终态 Work 还经过 `AgentSessionRuntime.completed_work` 与 `KernelHostCore._deliver_agent_completion`，尝试向活动 ROOT 的队列投递 `AGENT_MATERIAL`。这与脚本再打印 wait 结果重叠。本轮删除这条自动完成材料路径；不删除真实 final 存储或工作依赖推进。

当前 monitor 已有活动 turn 的 ExistingTurnInstallation、ROOT idle 的 NewTurnInstallation、共同安全点、用户队列优先与精确写入确认。Cell 主动输出采用这些语义；只在适合的现有输入安装边界复用代码，不为 Cell 伪造 Process 或 Monitor 注册。

## 3. Work、原生工具与 JS API

### 3.1 取消自动 final 给 ROOT

所有由 ROOT 派出的 Work，无论使用原生入口还是 Code Mode，completed／stopped／failed／cancelled 等终态都不自动向 ROOT 追加正文或诊断输入。删除仅服务此行为的 `_completion_notifications`、完成材料重驱与 `_deliver_agent_completion` 接线；保留其中仍负责调度、状态变化观察、队列推进、依赖物化与容量唤醒的必要职责。

Work final 仍通过真实 turn 的最终 assistant entry 读取；失败仍有公开诊断。query、子代理会话、状态等待和依赖解析读取真实事实，不另存 Work 报告或完成事件。未启动即取消等没有 turn 的结果仍沿原请求终态读取。

`depends_on` 仍表达成功门槛，`materials` 仍读取已结束 Work 的真实结果，正文保存为接收方自己的输入；来源会话后续退役不撤回已交付材料。初始 context 继续沿原合同输入。删除的是“每份 final 自动给 ROOT”，不是 `AGENT_MATERIAL` 这一真实来源种类。

不为错误例外自动上报每个 Work 失败。需要 ROOT 处理失败的流程，应显式等待/查询或让脚本输出选定诊断。Cell 自己未捕获的执行错误另按 §5。

### 3.2 保留现有入口，共用业务 owner

| 操作 | 原生 ROOT | JS |
|---|---|---|
| run_agent | 接纳一份工作，返回 agent/work target、真实接纳状态，不附带历史 final | 相同业务操作；保留 batch/依赖编排 |
| query_agents | 显式查询 Agent/Work；精确 Work 查询可以取得其 final／诊断 | 返回同一业务值供脚本筛选、计算；不自动进入 ROOT 输入 |
| send_agent_message | 显式发送给目标活动 turn，保留原来源与接纳结果 | 相同业务操作 |
| wait_agent | 等指定 AGENT 在等待开始时绑定的工作／原消息条件，返回状态和目标，不附带长篇结果正文 | 保留供脚本继续编排的返回数据与原业务条件 |
| stop_agent | 显式停止原 target 的工作，保留原停止范围 | 相同业务操作 |

JS 的 `tools.wait_agent.works` 继续精确等待 WORK，可返回完整业务结果给 JS；默认 all、可选 first 等合同保持。ROOT 原生 wait 的轻量模型投影不包含 final、diagnostic 正文或消息正文；返回真实状态、终止原因码和来源 target，模型需要内容时主动查询。Subagent 原生 wait 保留其既有消息/结果读取能力，因为它没有 Code Mode；不能为了 ROOT 的上下文选择而切断队友协作。

本轮不扩展原生 wait 的 target 联合类型，不新增 wait_cell：Cell 等待仍为 `manage_cell(action:"wait")`。原生与 JS 不维护两套 Agent/Work 状态或参数解析器；区别是模型工具结果的投影位置与脚本内部业务值，不是另一个执行引擎。

原生 wait 仍按原等待条件自然结束。无关 Cell 输出、人类 steer 或非所等目标的变化不抢占等待，也不取消工具批次来制造安全点；投递在下一合法安全点继续。这意味着主动投递不是“任何时刻立即让模型读到”。

### 3.3 主动消息不是完成通知

`send_agent_message` 保留当前真实目标活动 turn 的接纳规则和 sender；idle/not_started 不接纳、不自动转投下一份 Work。子代理明确向 ROOT 发问/澄清，仍是主动通信，不是 Work final 自动广播。本轮不把它改成必须经 console.log 中转。UI 不能把它画成人类新请求。

### 3.4 跨 Cell 复用

Cell JS 变量互不共享；Agent/Work 独立于 Cell 存活。已知完整 target 可直接用于下一份脚本；未知时，原生或脚本内 query_agents 找到可见目标再派新 Work。脚本内部查询无须打印给 ROOT。复用同一 Agent 保留其会话，新的派活获得新 Work target；不得因切换入口创建替身或按名称猜 ID。

## 4. Cell 主动输出的内容合同

### 4.1 正式输出

- 每次 console.log 仍产生一条有序正式记录，字符串原样，其他值沿现有 JSON.stringify 规则，多参数空格连接；保留原类型/序列化错误。
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
  console.log(work.final ?? work.diagnostic ?? "The selected work ended without a text result.");
}
```

该例只输出选定 review 的结果，facts/risks 的正文直接交给 review。脚本可以输出有必要的阶段信息或供后续复用的 target，但不得在示例中一律打印整份 batch/wait 响应。真实参数/状态字段必须与实施时公共 API 对齐，不能为示例另造 schema。

## 5. 活动/空闲 ROOT 的接纳与安全点

### 5.1 Owner 与投递目标

CodeModeHost 是未交付输出的唯一进程本地 owner；继续使用既有输出队列和资源计费。Host 借用一个完整有序前缀，走现有外部输入安装和确认 owner。材料来源必须保留 ROOT session、源 exec_cell attempt 以及原记录顺序；这些现有身份足以定位源码/执行，不新增全局 output registry。

同一 Host 中，pending Cell 输出按现有对象/记录顺序进行公平推进，单次读取和输入安装沿现有资源/队列界限分页；Cell 内严格有序，跨 Cell 不承诺真实全局发生顺序。不得用无限处理一个高频 Cell 的循环饿死人类输入或其他输出。

| ROOT 事实 | 接纳规则 |
|---|---|
| 有活动 turn | 沿共同安全点追加具有真实 Cell 来源的材料观察，不取消模型流、编译或工具批次 |
| idle、可接受外部观察 | 借鉴 monitor 的 NewTurnInstallation，沿原自主新 turn 接纳、权限、compaction 与 writer 门禁开始处理 |
| 已有人类排队输入 | 保留人类优先与既有队列顺序；不抢先创建后台新 turn、不把输出伪装成人类输入 |
| 手动压缩/Plan fence/新 turn 接纳中 | 由原 owner 暂缓并重驱，不绕过 fence 或改写已安装前缀 |
| Host 正在关闭/已丢失执行 owner | 不启动新模型执行；按既有关闭和未决写入确认收尾 |

ROOT 的自动新 turn 仅由实际输出或必须报告的 Cell 错误触发；Work 完成与静默 Cell 正常结束都不触发。不照搬 Subagent monitor 到 ROOT，也不把输出投向之后不相关的 Subagent Work。

同一安装批次可包含若干完整记录；不逐条机械启动模型调用。若 ROOT 已被第一批唤醒，随后输出按活动 turn 的下一安全点追加。不存在人为凑批定时器或新生命周期 cap。

### 5.2 复用 monitor 的范围

复用/抽取的是已经存在的 ExistingTurn/NewTurn 目标选择、compaction 写入预约、安装确认与开始 turn 的机制；ProcessRegistry、Monitor 注册、到期/关闭和进程输出 cursor 不移植给 Cell。不能为了重用函数而给每个 Cell 创建假 monitor。

输入沿现有 `TERMINAL_OBSERVATION` 及对应 canonical occurrence 家族承载 runtime 材料，在原内容结构中保留真实 Cell source（非 Process/Agent）。必要时把现有监控专用的输入安装参数收窄为两个 typed 来源的适配入口；不得形成通用事件框架或增加 durable event kind/subject。

已有 prompt_queue 的 AGENT_MATERIAL 只允许 STEER_ACTIVE_TURN，不能拿它伪造 idle 的 NEW_TURN。本轮主动输出使用已有 runtime observation 的新/旧 turn 安装路径；这也是语义复用与具体存储约束的明确边界。工作流内部 AGENT_MATERIAL 仍使用原输入路径。

### 5.3 确认、重复与失败

一条输出成功进入接收方 canonical 输入后，就从原未交付队列消费/释放计费，不再从 exec/wait 回送正文。确认的是“接收方已接纳”，不声称模型已经阅读。

复用原安装确认协议和进程本地 immutable attempt：冻结本次完整前缀、目标及由源 exec attempt/原记录位置导出的既有 canonical identity；提交结果未知时，必须确认这同一次写入，不换 ID 重发。FULL 才消费；NONE 可沿原重驱；UNKNOWN 保留原输出和该 attempt，不能既丢输出又另造新输入。不新增回执表、durable cursor、output event ledger、delivery job 或重启补偿扫描。

这里的确认依赖由“原 output observation ACK”迁移而来，是 runtime→canonical 真正边界上的原职责，不增加第二套确认 owner。不得在两条路径分别 ACK 同一输出；观测/读取 UI 不代替交付确认。

主机/进程崩溃时，尚未接纳的进程本地输出允许丢失；已接纳输入保留。不得承诺跨重启执行恢复、自动重跑 Cell 或 exactly-once 外部副作用。尚未确认输出继续计入现有16 MiB/Cell、64 MiB/Host边界，超界保持明确资源失败，不静默丢日志或增设历史总量 cap。单条过大沿既有 artifact owner 提供准确正文入口；不能把截短预览当作完整交付。

### 5.4 错误与取消

源码/参数校验失败、Cell 尚未接纳的启动失败，直接作为 exec_cell 工具失败返回；不再额外生成同一份主动错误输入。

已接纳 Cell 的未捕获错误、stalled、资源失败、sidecar/Worker 失败，主动交付一次真实诊断；已有输出仍按顺序处理。保留真实 ToolError 状态及可读结果入口。Cell failed/terminated 后，已经接纳但尚未交给 JS 的工具结果仍按原结算规则报告，不能因 pending_calls 清零就丢失实际效果；不重新执行这些工具。

用户主动停止沿原控制反馈结算，不制造“意外失败”重复唤醒。Stop 当前 ROOT turn 仍按原范围停止该 turn 创建的 Cell；独立 Cell/Work 不扩大停止范围。停止后的晚到结果需真实结算，但不能借普通日志复活已显式停止的原 turn；原停止栅栏和后续合法外部输入接纳负责选择目标。

普通输出不取消未派发工具、不抢占 wait、不切断模型流。模型重试期间输出只排队，遵守已冻结 provider 请求与前缀连续性。

## 6. 材料、Cell 输出与 steer 的展示

steer 是“补充给当前 turn”的交付语义，AGENT_MATERIAL/Cell 输出/Agent 消息是内容来源；不得把来源抹平成人类角色。当前 turn 内的这些补充输入复用现有 steer 气泡、折叠、复制和过程分组，按真实顺序显示，不再用“大段任务材料正文”打断整个处理过程。

| 来源 | 默认展示 |
|---|---|
| Agent 主动消息/现存补充材料 | 真实 Agent 名称、短提示、可展开正文；来源 Agent/Work 可点击定位 |
| Cell 正式输出 | Cell 身份/脚本输出标记、可展开正文；可跳对应冻结源码 |
| Cell 错误 | 在同一过程位置显示错误状态与可展开真实诊断 |
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
6. 不再指导反复 wait 领取输出。等待只负责状态/编排；ROOT 想看 Work 内容时明确 query，脚本想交付时 console.log。
7. 说明 Agent target 可跨 Cell 复用、变量不共享；不鼓励用新 Agent 替代可复用的同一会话。
8. SYSTEM/provider tools/schema 改动只在新 cold epoch 或明确采用的 compaction successor 安装，已有 epoch 保持原字节、messages 仅追加；不加兼容双轨或静默 rebase。

## 10. 实施 owner 与持久化约束

| 位置 | 工作 |
|---|---|
| code_mode/host.py、contracts.py、sidecar/worker | 主动输出借用、状态等待、return兜底、终态/资源结算与回收 |
| conversation_kernel/tool_runtime.py | exec接纳结果、wait状态投影，移除旧output observation领取/ACK接线，保留普通工具业务结算 |
| Host、safe_point、现有runtime observation installer | Cell来源适配、活动/idle目标、用户优先、精确安装确认、已接纳输出释放 |
| subagent.py、host.py、共同Work读取/等待 | 删除Work完成自动ROOT材料及专用pending refs；保留依赖、调度、查询、原生/JS入口与明确消息 |
| desktop source读取、inspector、agent-cell workspace | canonical attempt源码阅读与历史入口；实时图关闭状态与错误区分 |
| runtime adapter、会话组件、prompt内容阅读 | 来源明确的steer复用、过程连续、正文展开/Markdown与精确来源跳转 |
| prompt-suggestions、composer、批注与后端用户引用提取 | 两处引用共用，quote/code/材料排除，目录与激活规则一致 |
| specs、bundled Skill、tests、dogfood | 删除被替代说明和例子，以新单路径验收 |

durable oracle 的 event kind、relation、job、subject、append guard 类别增量目标全部为零；不新增 Work final 副本、Cell历史表、输出恢复账本或 registry。原 runtime observation 内容的真实 Cell 来源不是新的 subject slot。若具体实现需要改变既有内容 codec，更新原 typed codec/consumer，不引入旧/新兼容读取双轨；若发现无法满足零增量，必须先指出具体需求并修订规格，不能偷偷补表。

## 11. 验收矩阵与激活

| 范围 | 必须证明的行为 |
|---|---|
| 原生/JS入口 | 相同target/参数/Hook/权限/副作用owner；原生全部保留；ROOT wait无final正文、Subagent原wait协作能力保留 |
| 静默Work | 原生/JS创建的Work成功、失败、取消、依赖失败均不自动进ROOT输入，不自动新turn；query仍可读真实结果 |
| 内部协作 | A/B结果直接给依赖它们的review；ROOT只看到console选中的review结果；源Agent删除不撤回已交付材料 |
| 主动输出 | 活动ROOT安全点接收；idle按原门禁启动；用户队列优先；同批有序交付；输出不抢占provider/tool批次 |
| 输出类型 | console多参数/字符串/空值序列化；return false/0/null/空字符串/undefined；无输出成功不唤醒；失败不发成功return |
| wait/查询 | wait只等状态不领正文、输出不误唤醒状态等待；query不消费；没有wait也可收到主动输出；tool调用均闭合 |
| 输出确认 | FULL才消费；NONE重驱同次候选；UNKNOWN确认旧ID；重复响应不重复输入，临时失败不丢输出；无新durable receipt |
| 大输出/资源 | 原单条/模型页/artifact与16/64MiB合同保持；慢接收不能绕过计费；无静默截断、无无限副本/新总量cap |
| 错误/停止 | 启动失败仅tool错误；接纳后未捕获错误主动报告；late工具结果真实结算；显式Stop无错误复活；Host关闭不新开turn |
| 长程/并发 | 多Cell混合输出，各Cell内序保留，人类和其他Cell不饿死；很多turn/Work后仍可用；无任务总调用次数限制 |
| 材料UI | 同turn过程不被材料切开；来源不冒充用户；长材料默认折叠、展开Markdown/复制；跳Agent/Work/源码准确 |
| 引用 | 主正文/comment均有相同候选、光标、IME、撤销、选择和发送表现；排队转steer内容不丢；quote/code/URL/材料不误识别 |
| 引用边界 | 未知/失效技能不冒充已执行；@不自动全文注入或越权读取；既有epoch不因新引用rebase |
| 源码 | Hook后实际源码、code/path、原文件删改、Cell回收、Host置换、重启后均能按已有attempt读取；历史读取越权被拒 |
| 图 | live按原关联显示；确认退役温和关闭、节点清空、源码仍读；临时网络故障不误判；不推断所有Work结束 |
| 回收 | 静默终态无需模型ACK即可回收；未FULL输出/UNKNOWN/pending工具阻止回收；阅读不保活；无TTL或新tombstone |
| 提示词/合同 | 无自动ROOT final承诺、无wait领输出指导、无显式return教学；无新provider前缀边界、无dual path |

先用根 `.venv` 的聚焦单元/真实PostgreSQL集成/前端测试迭代；不弱化断言、加skip/xfail。真实 provider dogfood 使用保存生产设置，经 LocalSettingsStore/require_pulsara_home 只读加载，必要时注入已核实的隔离数据库；不修改用户模型、代理或凭据。秘密只按实际值清理，保留有效输入/输出和失败证据。

激活至少覆盖一次“ROOT启动多阶段Cell后先回复并idle，选定console输出再唤醒处理”，一次“ROOT活动且多工具批次进行中输出到达”，一次“中间Work失败由脚本处理但ROOT未收到自动final”，以及原生简单任务与完整引用/历史源码阅读路径。视觉部分完成后按仓库纪律优先用户审阅；不因视觉调整无端扩大浏览器自动验证范围。

只有实现、所需测试、真实provider证据及用户视觉检查要求均完成后，才将状态改为实施完成。本文只记录已定产品语义和实施合同，本轮未启动代码实施，也未开展复审。
