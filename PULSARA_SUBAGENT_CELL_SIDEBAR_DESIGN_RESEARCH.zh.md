# Pulsara 子代理与 Cell 侧边栏实施规格

日期：2026-10-31。当前源码阅读和实时图生命周期以 [统一输入、Cell 输出与协作阅读规格](PULSARA_CELL_OUTPUT_AND_COLLABORATION_INPUT_IMPLEMENTATION_SPEC.zh.md) 为准；实现与激活验证进行中。既往证据保留其原验证范围。

本文确定两个侧边栏入口、共同阅读链、Work 输入存储 hard cut、Cell 局部编排图及 `open_in_pulsara` 接入。保留原文件路径供现有链接引用。

本规格范围内以本文为准；其余运行时语义沿用 [Agent／Work 设计](PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md)、[Code Mode 合同](PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md)及[工具查询与 Cell 身份规格](PULSARA_TOOL_QUERY_AND_CELL_ID_UNIFICATION_IMPLEMENTATION_SPEC.zh.md)。不改变 Agent／Work／Cell 身份、执行权限、停止范围、闲置期限或模型输入前缀边界。新 schema／SYSTEM 只在新 cold epoch 或显式采用的 compaction successor 安装；已有 epoch 的 SYSTEM／tools 保持原字节，messages 仅追加。

## 1. 已确定的产品语义

侧边栏顶层 tab 固定为以下五个，按此顺序展示，名称逐字采用表中值（`cell` 使用小写）：

| 顶层 tab 名称 | `PANEL.name` |
|---|---|
| 能力 | `capabilities` |
| 进程 | `background` |
| 子代理 | `subagents` |
| cell | `cells` |
| 网页 | `browser` |

本轮新增「子代理」「cell」两个入口；各自打开列表 tab，列表项继续打开独立详情 tab。依赖图是从 Cell 打开的详情 tab，不另增顶层入口。本文的 Cell 指运行时对象，界面顶层名称统一为 `cell`；界面名称与既有内部标识按上表映射。

未选择会话时，「能力」「进程」「子代理」「cell」仍可打开，页面以正常可读样式显示「创建或选择会话后查看内容」，不构造会话／Host 身份或发起会话读取。「网页」入口不依赖选中会话；切换会话保留已打开的网页 tab。

右侧栏展开状态属于窗口，不随会话切换；默认宽度和最小宽度为 350px，可向更宽拖动。切换会话的加载期间，会话内容停止读取并显示「正在加载会话…」，网页仍可使用。

网页状态及浏览器 partition 属于用户级浏览器 owner，不绑定 ROOT；清除或删除会话不关闭网页 tab。用户级网页共享该 owner 的 partition。

| 页面 | 展示单位 | 点击后的行为 |
|---|---|---|
| 子代理列表 | 一个 Subagent session 一行 | 用类似 MCP 的过渡动画打开该 Agent 的只读会话 tab |
| 子代理会话 | 同一个 Agent 的连续对话 | 复用 ROOT 会话主体，无输入框；复用只读排队展示 |
| Cell 列表 | 一个 Cell 一行 | 打开该 Cell 实际执行的冻结 JS 源码 |
| Cell 源码 | 冻结源码及必要状态 | 复用脚本阅读与 JS 高亮；涉及 Subagent 时提供依赖图入口 |
| Cell 依赖图 | 该 Cell 实际涉及的 Subagent 编排 | 不画普通工具调用；点击 Agent 打开其会话 tab，不播放列表展开动画 |

**列表只展示 ROOT 当前能够查询到的对象。Subagent 与 Cell 按现有生命周期消失。**

- 子代理范围与当前 ROOT 的 `query_agents` 清单一致；不是仅显示正在运行的 Agent，也不把已经退役删除的 Agent 补回来。
- Cell 范围与当前 Host 的 `query_cells` 清单一致；已结束但尚可查询的 Cell 可以存在，退役后移除。
- 不扫描历史工具结果、旧 transcript 或冻结源码字段来补历史列表，不新增“最近使用”“已归档 Cell”等集合。
- 打开列表、会话、源码或图，不保活对象，不刷新 Agent 的业务空闲时间，不推迟 Cell 输出结算和退役。
- 冻结源码从历史 exec_cell canonical attempt 继续阅读，不依赖 live Cell 或 Host；不扩充存活对象列表、不延长 Cell 寿命。

子代理与 cell 是会话范围内的辅助页面，绑定明确的 ROOT 身份；不扩展成用户级跨会话管理中心。

“与 ROOT 查询一致”指对象归属与存续集合；界面直接读取同一 owner，不以当前 turn 是否允许调用 Agent 工具作为显示开关。ROOT idle 或输入框切换权限不会让仍存在的对象消失。

### 1.1 界面密度、文案与过渡

本规格涉及的所有页面采用简洁、紧凑的布局，**不添加灰色小字说明、常驻注释段落或副标题式教学文案**。这些是界面验收要求，不以页面底部注释的形式交付。必要的状态、错误和覆盖不完整信息使用正常可读的短文案或明确状态标记，按需出现；不把内部机制写成界面说明。会话正文与源码仍忠实展示原内容。

「子代理」「cell」列表以现有「后台进程」（顶层名改为「进程」）的紧凑列表为密度基准：一条一行、控制垂直留白，不做高卡片或上下堆叠多行元数据。名称／短标识为主体，状态用简洁标记；详细信息进入详情页。

优雅而克制的过渡覆盖主要浏览场景：顶层与详情 tab 切换、列表打开详情、源码打开工作流图、列表条目出现／移除，以及局部展开收起。复用现有动画机制；列表到详情适合共享元素过渡，普通切换与内容变化使用轻量淡入／位置过渡，不让流式更新反复触发整页动画。图节点打开 Agent 会话沿既定例外直接导航，不播放展开动画。保留用户视角与焦点，快速切换可打断，尊重减少动态效果设置。

## 2. 子代理列表与只读会话

### 2.1 列表与导航

列表按 Agent session 展示名称和简洁状态标记；有排队 Work 时可在同一行放紧凑计数，不在行下堆放 Work 简述或其他元数据。执行多份 Work 的同一个 Agent 始终只有一行。具体排序可以复用现有查询顺序；不为界面新增业务排序字段。

列表点击保留列表 tab，并打开 Agent 详情 tab。过渡采用 MCP 现有共享元素／淡入方式：列表中的身份区域过渡到详情标题，正文淡入。动画不负责数据接纳或执行；不支持 View Transition 时直接打开。

同一 ROOT 下同一 Agent 只保留一个会话 tab，重复导航聚焦已有 tab。从依赖图跳转也使用这份身份。Work 定位开启该 Agent 的同一会话：未开始时定位排队卡片，已开始时定位 instruction entry／所属 turn；有 turn 的终态定位对应 turn；未启动即取消／依赖失败的 Work 没有 turn，沿同一请求读取展示终态定位卡片，不伪造 turn 或对话 entry，也不把终态塞回待执行队列。目标存在但内容尚未加载时正常分页读取，不制造另一份会话。

### 2.2 复用完整会话阅读体验

用户要求的是 ROOT 会话主体的只读版本：复用消息布局、Markdown、推理展示、工具过程与结果、压缩分隔、流式更新、历史分页、滚动跟随和阅读定位。不能只给一个 final 或自己另写简化聊天列表。

没有输入框、发送／停止／压缩按钮、模型设置、分叉及排队修改操作，也不提供“接管此会话”。阅读操作继续可用，例如滚动、展开、复制、加载历史、打开文件链接。这些页面操作不向 Agent 发送内容。

详情标题及消息发送者采用真实 Agent 身份。ROOT 派发的 instruction 不标成“你”；Agent 消息区分实际发送者。Subagent 不新增 HTML 可视化发布能力，展示既有会话中实际存在的内容即可。

### 2.3 Work 与 steer 的呈现

| 事实 | 页面呈现 | 保留的运行时语义 |
|---|---|---|
| 已接纳、尚未开始的 Work | 底部只读排队卡片 | 是 `AGENT_INSTRUCTION`／`NEW_TURN`，保留队列顺序 |
| 等待依赖的 Work | 同一排队区，说明正在等待的依赖 | 不伪装成正在运行，也不改变调度 |
| 已开始的 Work | 原始 instruction 进入真实对话位置 | 一个 Work 对应其实际请求／turn |
| Agent 之间的消息 | 当前 turn 的引导输入，标明发送者 | `AGENT_MESSAGE`／`STEER_ACTIVE_TURN`，不新开 Work |
| 已接纳但尚未消费的引导 | 独立展示“等待模型接收”，沿原队列顺序 | 不提前伪装成已进入对话或已被模型读到 |

Work 原始输入、后续 steer 和依赖材料保持各自的 entry 边界，不为了页面美观合并。无活动 turn 时消息是否接纳、首次调用前压缩期间的 safe point、停止确认等全部沿现有合同，UI 不另定规则。

排队卡片只展示，不出现发送为 steer、编辑、删除、拖拽重排等操作。复用的是排队 UI 和数据来源，不是 ROOT 的人类命令控制流程。

## 3. Cell 列表与冻结源码

### 3.1 列表

一个仍可被 ROOT 查询到的 Cell 一行，展示短标识和简洁状态标记，不增加多行脚本预览、路径或说明。纯工具组合、纯 Subagent 编排、混合脚本使用同一个列表，不要求用户或模型预先分类。

源码中出现某个 Agent 工具名不代表发生过编排。是否提供依赖图入口依据实际调用及可解析的 Agent／Work 关联，不做源码字符串匹配或静态 JS 流程推断。

### 3.2 源码阅读

复用当前文件代码阅读组件的版式、文本选择／复制、滚动和 JS 高亮，内容来源改为 Cell 对应 `exec_cell` attempt 已冻结的源码。

源码上方可以复用文件阅读页顶栏的尺寸与布局，但**顶栏只有一个轻量文字按钮「查看工作流图」**，按 §4.1 的参与条件显示。没有图入口时不保留空顶栏，源码直接接在 tab 栏下方。顶栏不出现后退、前进、刷新、复制、打开目录、路径导航或其他文件操作按钮；文本仍可正常选择和复制。它展示冻结内容，与冻结发布的可视化一样，不伪装成正在浏览的真实文件。

- `exec_cell(code=...)` 与 `exec_cell(path=...)` 都读取同一冻结结果。
- 显示经过 Hook 处理、实际读取并交给运行时的源码，不读取工具最初参数来冒充执行内容。
- `path` 仅可作为来源说明；文件后来修改、删除或同一路径再次执行，不影响这个 Cell 的源码。
- 不把源码写入临时 `.js` 文件再按路径打开，也不执行源码来显示它。
- 无文件名时显式使用 JS 语言标记或展示文件名即可；不必新增高亮器。

HTML 可视化可借鉴的是“引用已冻结内容而不是重读原文件”的模式。Cell 不需要进入 HTML 发布链，也不需要再复制一份冻结内容。

Cell 状态是动态观察，源码是固定值。Cell 完成或停止不代表其已接纳 Work 完成或停止。源码页可沿现有来源关联读取输出／错误，但本轮不建设通用工具流程图，也不通过 `manage_cell(wait)` 消费模型侧输出来刷新 UI。

### 3.3 对象消失

确认退役后立即从列表移除；刷新前不承诺实时发现。请求失败、连接暂时不可用与确认对象退役应区分，避免网络故障被显示成业务删除。

Cell 回收后，源码 tab 保留 canonical attempt 身份、正文与阅读位置，仍可重新读取；源内容因会话删除或 retention 确实不可读时才显示准确不可用结果。实时图确认退役则清除节点、边和临时缓存，保留 tab 并显示“实时工作流视图已关闭”，移除源码页的实时图入口，不重建历史图、不宣称全部 Work 结束。短暂网络错误保留现有内容并说明刷新失败，不冒充确认退役。

## 4. Cell 的 Subagent 编排图

### 4.1 入口与范围

Cell 实际涉及仍可见的 Subagent 时显示“查看工作流图”。参与包括派活、具体 Agent／Work 查询、消息、`wait_agent`／`wait_agent.works` 和 `stop_agent`；WORK 解析到其所属 Agent。ROOT 目标不生成 Subagent 节点。普通工具调用完全不进入图。

图只包含这个 Cell 实际关联、且 ROOT 仍可见的 Agent／Work，以及解释这些 Work 当前关系所必需的、仍可查询的依赖对象。不要把 ROOT 的全部 Agent 和所有 Work 历史塞入每一张图。

对只查询／等待、没有派活或依赖的 Cell，图可以只有 Agent 卡片，没有依赖边。无效目标不生成节点，无 target 的清单查询不构成参与。只验证 Agent 身份时不自动展开其全部历史 Work。观察覆盖不完整时保留图入口说明覆盖状态；不能把“不知道是否参与”显示成“确定没有参与”。只有 canonical Work 查询完成且 `participation_coverage=complete` 时，图的空状态才可表示没有 Subagent 编排；在途、incomplete／unavailable 显示覆盖情况，不将空数组当作确定无参与。

### 4.2 Agent 是视觉主体，Work 是依赖端点

用户看到的是可点击的 Agent 卡片。每个 Agent 一张卡片，内部列出该图相关的 Work，依赖线连接具体 Work 行。

这样既保留“点击 Subagent 节点进入它的会话”，也避免把一个 Agent 多次参与不同 Work 的关系混在一起：

```text
Agent A                       Agent B
  Work A1 ──成功依赖──────────→ Work B1
  Work A2 ←──成功依赖──────────┘
```

这是合法的 Work 顺序，不能因为 Agent 层面看似 A→B→A 就当成依赖环。依赖依据是具体 `depends_on` 的 Work，Agent 只是它们的执行者。

- 成功依赖用有向实线，方向从前置 Work 指向后继 Work。“成功依赖”是成功门槛，不声称来源已经成功；当前结果使用实际 Work 状态标识。
- 同 Agent 的串行位置不自动画成成功依赖。
- 消息只在卡片中标记参与，不画消息边、不声称已送达。
- 普通材料引用不等同于成功依赖；`Promise.all`、代码先后顺序也不自动变成业务边。
- 从图点击 Agent 身份打开会话 tab，不播放列表过渡动画；点击 Work 可以进一步定位到相关输入。
- 对象退役后移除对应节点及关联边，不用历史工具结果复活它。

### 4.3 视觉方向

图可以比普通流程图精致：Agent 卡片包含身份、状态和 Work 摘要；用清晰的有向曲线、柔和分组背景与克制的状态动效。提供缩放、平移、适配视图和当前选中关系强调。

数据更新尽量保持节点位置与用户视角，不因每次状态刷新重排整张图。暗色主题、窄侧栏、键盘打开节点和减少动态效果沿现有应用习惯。大图按相关范围渐进展开，不增加 Agent／Work 总量上限。

现有 Mermaid 最终渲染成图片，不能直接满足交互卡片与导航。实施时优先检查现有依赖可用的布局能力，再决定是否引入维护中的图组件／布局库；本文不预定新依赖，也不要求自研布局算法。

种子为当前 Cell 接纳的 Work 和已验证的具体参与对象；从已选 Work 沿 dependency 向前置 Work 按需展开，可以包含其他 Cell 或原生派活产生的前置 Work。只读取窄身份／状态／关系，不为绘图 hydrate 全部 instruction／final，不扫描 ROOT 全历史，不设图深或总 Work 数上限。

## 5. 所有权与读取合同

| 链路 | 当前所有者与职责 |
|---|---|
| Tab 与导航 | [desktop-workspace.tsx](frontend/components/desktop-workspace.tsx) 管理承载 ROOT、目标身份、标签去重与阅读视角；[agent-cell-api.ts](frontend/lib/agent-cell-api.ts) 沿共同读取入口核验目标 |
| 会话阅读 | [workbench-view.tsx](frontend/components/workbench-view.tsx) 复用共同阅读行为；[agent-conversation-pane.tsx](frontend/components/agent-conversation-pane.tsx) 装配只读 Agent 会话 |
| 排队与来源 | [runtime-adapter.ts](frontend/lib/runtime-adapter.ts) 和 [canonical_v3.py](src/pulsara_agent/terminal_protocol/canonical_v3.py) 投影队列身份、来源与依赖等待；ROOT 操作和 Agent 只读阅读各自装配 |
| Agent 与 Work | [collaboration_api.py](src/pulsara_agent/conversation_kernel/subagents/collaboration_api.py) 与 [_repository/subagents.py](src/pulsara_agent/conversation_kernel/_repository/subagents.py) 管理当前 ROOT 可见成员、Work 及其输入事实 |
| 只读打开 | [session_controller.py](src/pulsara_agent/desktop_app/session_controller.py) 与 [renderer_bridge.py](src/pulsara_agent/desktop_app/renderer_bridge.py) 校验 ROOT／Agent，沿 canonical reader 和 live observer 读取，不 resume 或取得 writer |
| Cell、源码与图 | [code_mode/host.py](src/pulsara_agent/code_mode/host.py) 管理当前存活 Cell 与进程内参与观察；[tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py) 复用冻结源码和图读取，Desktop 按 ROOT/session＋canonical attempt 校验源码读取范围，图仍使用 live Cell |
| 源码展示 | [file-code.tsx](frontend/components/file-code.tsx) 与 [code-highlight.ts](frontend/lib/code-highlight.ts) 显示冻结源码并提供 JS 高亮，不借用文件路径身份 |

### 5.1 输入身份与共同队列阅读

这一处不修改数据库身份，不给 Work 添加类似 `command_id` 的新字段；调整共同前端阅读模型，并补齐协议投影。

先区分现有三种身份：

| 身份 | 表达什么 | 用途 |
|---|---|---|
| `prompt_queue_items.id`，协议中的 `queue_item_id` | 一条已接纳输入；对 Agent instruction，这就是 Work ID | 队列卡片、正文读取、消费后 entry 关联与 Work 定位 |
| `command_id` | 一次有命令记录的提交或控制请求 | 提交去重、回执查询及本地提交状态核对；不是所有输入的共同身份 |
| `source_session_id`／`source_work_id`／`source_tool_attempt_id` 等 | 谁从哪里发起这条输入 | 来源展示与真实调用关联；一次 batch 可以接纳多个 Work，来源 attempt 不能替代每个 Work 的身份 |

数据库已明确约束 Agent 输入的 `command_id` 和 `client_submission_id` 为 NULL；其队列主键、顺序、内容、来源及消费后的 entry 关系都存在。给 Work 填 command 会引入它并不需要的命令语义；另增一个 `display_id` 或类似字段则重复现有队列主键。两者都没有独立产品理由。

共同阅读模型应围绕“已接纳输入”组织，ROOT 与 Subagent 都使用同一个模型：

- 会话身份加 `queue_item_id` 是阅读定位；在明确绑定单个会话的组件中，行 key 直接使用队列 ID。
- 内容、delivery mode、输入来源、目标 turn 和接纳时间属于共同展示数据。
- command 仅作为确实存在的来源关联；来源用明确类型区分，命令操作代码只接收对应类型。不要把所有字段改成可选，再让每个组件猜测。
- ROOT 尚未被接纳的本地提交仍以 command 为身份，留在提交层；接纳后关联真实队列项。不能让共同队列模型要求每条输入都经历浏览器提交。
- 非队列的 ROOT revision 输入可能只有 `command_id`（`canonical_v3.py` 的 revision 来源分支）；显式保留 command-only 来源，不丢弃、不伪造 queue ID。队列输入、revision 来源与本地未接纳提交使用闭合的不同类型。
- 排队卡片和对话阅读复用；ROOT 的编辑、取消、转 steer 及回执处理由操作层提供，Subagent 阅读装配不传入这些行为。

共同阅读链的要求：

1. `projectQueuedPrompts` 按队列身份与状态接纳展示项；`Message.inputSource` 不再因 command 缺失丢弃来源，保留 Agent 发送者信息。
2. `rememberQueueContent`、`decorateHistory` 的正文衔接缓存按会话／队列身份关联；队列消费后按 `queue_item_id` 找到 entry，读取完成后释放临时正文。
3. 可见队列、待处理 steer、队列计数和消息去重采用同一输入身份。多个无 command 的 Work 或消息不会共用空 key，也不会被合并或重复展示。
4. `hydrateQueuedPromptContent` 遇到 `CONTENT_QUEUE_NOT_PENDING` 时，共同阅读路径刷新 canonical 队列／entry 状态并沿真实输入身份核对；有命令的提交路径才继续调用 `queryCommand`。不能让 Agent 输入落入当前 `reconcilePromptQueueTransition` 的“缺少 command 身份”错误，也不另建 Agent 回执机制。
5. `PromptQueueControl` 投影 Agent 来源及依赖等待说明；历史 `CanonicalInputSource` 保留来源 session／Work。协议投影不另加数据库身份。

共同组件的目标是同一种输入阅读方式，保留来源之间真实不同的提交／控制流程；不是复制一套 Agent 队列，也不是在 ROOT 组件里散布 `isSubagent` 特判。

实施时重点覆盖：多个 Work 与 Agent steer 同时存在；内容加载时恰好被消费或取消；消费后正文未读完；ROOT 本地提交与已接纳队列去重；用户编辑／删除／转 steer 的原有回执语义。对象退役后沿共同不可用状态收束，不保留第二份历史队列。

### 5.2 Work 输入统一为一个 JSONB 字段

`prompt_queue_items` 使用单一 **`work_inputs` JSONB 数组**保存 Work 输入事实。沿原队列行锁原子更新，不建立关系表或图数据副本。每项为闭合对象 `{kind, source_work_id, ordinal, body}`：

| 字段语义 | 写入及变化 |
|---|---|
| 所属 Work | 数组所在的真实队列行，不在元素中重复存储 |
| 来源 Work ID | 接纳事务确定；身份不随结算变化 |
| 种类：dependency／material | 接纳时确定，保留两者区别 |
| 同种类中的顺序 ordinal | 接纳时确定，继续用于准备初始输入组 |
| 材料正文快照 body | JSON null 或 string；普通材料及已完成依赖在接纳时写入，未完成依赖成功后填入一次；JSON null 与空字符串严格区分 |

读取规则统一为：

- **图的成功依赖边**：读取 `kind=dependency` 的元素身份，不依赖正文是否已填充。
- **尚未满足的条件**：对仍 PENDING 的 Work，读取正文尚未物化的 dependency 项，再由共同依赖结算逻辑检查来源 Work；不另存等待条件副本。
- **可启动**：共同依赖处理已完成，且不存在未物化依赖；实际启动事务仍核对该条件。
- **模型初始材料**：读取已经物化的正文，沿现有 dependency 优先、ordinal 排序，保持现有独立 entry 及初始输入组边界。
- **失败／取消**：按既有请求状态结算，保留关系身份；终态请求不再进入依赖调度。失败诊断仍使用现有请求终止原因，不复制请求状态。

接纳与所有输入关系在原共同接纳事务中一次写入；Hook 后的真实请求、batch 解引用和所有权校验完成后才建立关系。确认不确定的接纳结果时，同时核对不可变的来源、种类和顺序。成功材料物化、依赖失败及后继请求处理继续由共同 repository 负责，不引入第二个调度器。

schema 保持 JSON 数组形状检查；共同接纳／读取边界校验闭合元素、canonical 来源 ID、kind、非负整数 ordinal 及唯一性。每种输入的原有单次数量和内容边界保持。同种类重复目标仍拒绝；跨 dependency/material 重复时 dependency 优先，材料省略但**剩余 ordinal 不重编号**。正文只从 JSON null 物化一次，空串不算待满足。`initial_context` 仍是原字段。

模型材料 entry ID 仍由 `queue ID + kind:原 ordinal` 派生；context → instruction → dependency/material 的顺序保持。准备、启动提交及未知启动结果确认核对同一完整有序输入。`FULL_HISTORY` 的完整组首截止与 `SNAPSHOT` 的继承来源截止分别保留，不改成 instruction−1，也不强制两者相等。环检测保留当前 PENDING／RUNNING Work 之间按 queue sequence 建立的同 Agent FIFO 隐式边，以及未物化 dependency 边；batch 新 Work 也参与同一检查，沿既有 ROOT 序列化校验。图展示不画 FIFO 边，不代表调度环检测可以省略它们。已物化及终态保留关系不能成为活等待的假环。

**来源 Work 不被引用保活。** 输入随所属队列行删除；来源只保留已验证 ID，不加跨 session 外键。已有正文快照在来源退役后仍供原任务使用；未物化依赖遇到来源不存在时沿现有失败语义处理。图不为这个引用建立幽灵节点。

来源读取／删除竞态采用短事务锁：维持既有 ROOT → 目标 Agent 顺序；同一接纳／物化事务先识别来源 session，按 session ID 排序，通过同 ROOT 的真实 Work join 取得来源 session `FOR KEY SHARE`，再读取 Work 与完整 final／diagnostic 并写入快照。来源删除的 `FOR UPDATE` 与之互斥。删除先赢：未接纳整批返回不可用并回滚；已接纳的未物化依赖按既有失败规则结算。物化先赢：正文随接收事务提交后独立可读。不加跨 session 外键或延长业务闲置期限，短锁沿既有操作 deadline 释放。

共同依赖解析器必须同步调整锁顺序：持 ROOT admission 锁后，先无锁扫描候选输入并识别来源，先取得上述来源 session 锁，**然后才锁接收方 queue 行**，在 READ COMMITTED 下重读并核对身份、状态和依赖关系。不能在现有“先锁全部待依赖队列”的实现后追加来源锁：来源自身也有 pending Work 时，会与删除器持 session 锁再级联删除 queue 的顺序死锁。接纳、调度、取消触发的共同解析都遵守这一顺序；若重读发现新的未锁来源，撤销本次事务尝试、释放接收 queue 锁后，在同一操作 deadline 内按序重试，不在持 queue 锁时返回补来源锁，不提交部分更新或重复事件。正常 ROOT 序列化下依赖不会并发增加。batch 在自身 Work 全部插入后、锁既存队列前完成来源识别，其新目标 session 已由接纳事务锁定；取消也须在修改目标 queue 前取得所需来源锁，不能原样沿用“先更新 queue 再调用解析”的顺序。

普通 materials 必须在接纳时快照已结束来源的 final／公开失败材料，未结束材料拒绝；只有 dependency 可以保留 null。接纳结果确认比较规范化后的身份／kind／原 ordinal，而不是未经去重的参数数组。取消和失败保留 work_inputs；终态请求的派生待满足集合为空，不再参与调度。

空材料沿用户确认的投影语义处理：`work_inputs.body == ""` 原样保存且视为已满足；在 `_queued_ordered_inputs` 的材料准备循环中，投影为固定运行时说明 **“此来源 Work 的结果不含文本内容。”**，仍生成独立 `AGENT_MATERIAL` entry。非空及仅含空白的正文保持原样，初始 context 不套用该分支。该说明不是来源模型的原文。prepare、启动复验、ACK 共用同一投影，预算／压缩／provider 读取投影后的 canonical 内容；不扩展 `FrozenPromptContent`，公开空输入仍拒绝。

这项存储调整的产品理由是：对象存续期间成功、失败、取消不能擦除已接纳编排。唯一事实归属仍是 Work 队列行；关系类别、事件、subject、guard、job 和授权类别不增加。同步 clean-v0、真实 catalog 摘要、接纳／确认、环检测、依赖结算、`head_ready`、prepare／启动确认、材料准备、取消及查询；删除旧两列和全部旧读写，不做双写兼容。

图显示对象存续期间完整的已接纳依赖，不以“只画剩余等待条件”作为过渡版本。

### 5.3 会话阅读与 ROOT 操作耦合：抽出完整阅读主体

ROOT 与 Agent 共用消息、历史 hydration／disclosure、阅读定位与 `ThreadFollowController`；ROOT 外层负责操作，Agent 外层只读装配。

ROOT 与 Agent 装配同一份 **ConversationReader／ConversationPane**（内部名称可随模块组织调整，职责不能分叉）：

- 数据侧管理 canonical 历史、正文读取、live draft／tool result 合并、内容落库后的衔接、队列与 TODO 投影。
- 视图侧管理阅读上下文、消息／工具渲染、压缩分隔、历史分页、滚动跟随、折叠与 entry 定位。
- ROOT 在外层组合 composer、提交／回执、队列操作、注释编辑和会话控制；Subagent 外层组合身份标题与只读队列，不依赖草稿 store。
- 阅读引用定位与“添加注释到草稿”分开；复制、展开、链接可以使用，Subagent 页不注册注释编辑行为。

共同阅读路径按当前被阅读 session 和真实 scope 投影 turn、TODO 与历史分组，不在子代理页面把 SUBAGENT 冒充 ROOT。

`runtime-adapter.ts` 按真实阅读 session／scope 衔接 live draft／tool result 与 canonical 正文，避免 SUBAGENT 提交时先闪空。已落库内容仍按原分页与释放策略读取，不把完整历史永久缓存。

其中真正 ROOT 专属的能力仍留在 ROOT 操作层，例如自动处理 `open_in_pulsara` 展示请求、分叉入口、Plan 决策和用户停止目标。不要机械删除所有 ROOT 判断；区分业务专属规则与阅读代码的历史假设。

发送者依据**每条输入的元数据**。读取实际来源 session／Work，分别呈现 ROOT instruction、某个 Agent 的引导和 runtime 材料；不得把材料 entry 当成人类或某个队友新发的消息。来源已经删除时显示可用身份说明，不补造活 Agent，也不为保存显示名新建历史成员表。

同屏 ROOT 与 Agent 阅读必须隔离 DOM 行为。现有 `ResponseAnnotations` 在 document 上监听选区、pointerup 和 copy；它的选区映射、来源查找、复制处理与定位要限定在自己的 thread 容器。Agent 页面不注册编辑行为仍不够，ROOT 的全局监听也不能捕获 Agent 选区。快捷键、滚动定位和异步 hydration 同样绑定各自 reader。

Workspace 切换 tab 会卸载选中内容，外层 scrollTop 不能恢复内部 thread-scroll。tab 保留该阅读对象的轻量位置描述：历史阅读窗口／锚点、相对偏移和显式折叠选择；重新挂载按需重读并恢复位置。隐藏时取消活动观察、定位和读取；不要求所有 pane 常驻。关闭清除对应阅读缓存；图退役清除实时缓存，源码按 canonical attempt 保留阅读身份，ROOT 与不同 Agent 互不串扰。

### 5.4 读取与启动／控制连接耦合：显式分离 Reader 和 Controller

[renderer_bridge.py](src/pulsara_agent/desktop_app/renderer_bridge.py) 的 Agent 只读入口与负责 resume 和控制的 `connect` 分开；前端沿共同 canonical 与 live 阅读机制装配 Agent 会话。

只读打开及共同 reader 遵循：

1. 解析明确的 ROOT／Agent 身份，读取 canonical session，并验证其仍属于该 ROOT 的可见清单。
2. 直接读取共同 canonical snapshot／history／content。已有 live Host 时，使用现有协议 observer 和 live bus；没有 live Host 时仍能读取存在的会话，不调用 resume、不申请执行槽。
3. 读取与 controller 连接分开装配。ROOT 的可交互入口继续负责 resume 和控制权，随后也使用同一 reader，避免第二套 live 投影。
4. Host 卸载时丢弃该 live owner 的临时观察；仍存在的 Agent 继续 canonical 阅读。后续正常调度创建新 Host 时，先撤销旧请求、清空 draft／live result／临时 TODO，再安装新 HELLO、live/control baseline 与 canonical snapshot。不同 Host 的 live epoch 可能都从 1 开始，必须同时按真实 `host_session_id` 和现有 attachment／reader generation 隔离，不能只比 epoch。
5. Agent 删除后终止该 reader；关闭 tab 只取消读请求、释放观察者和阅读缓存，不停止业务任务。

后端复用 `CanonicalProtocolReader` 和 `v3_gateway` 已有 snapshot／observe／gap 处理，不复制一套事件解释器。只读 observer 不新增控制角色、执行会话或数据库 writer。

Reader／Controller 是职责划分，可以共享现有物理连接；不要求 ROOT 为此再开一份观察连接，也不增加转发层。未加载的 Agent 没有 live 状态时应明确显示 canonical 状态，不能简单套用历史页的固定 `isRunning=false` 推断业务已结束。

具体接入必须覆盖以下现有边界：

- 协议服务的 `session_provider` 精确定位 Core 管理的 Agent Host，核对 ROOT／session／host，不为读取走 Desktop resume／publish。
- `BrowserRuntimeConnection.open(role=observer)` 使用只读附件隔离并发观察与请求，不申请 controller；reader 不纳入业务 admission／retirement gate。
- Agent reader 在 snapshot、history、content、queue、observe 入口核对明确 ROOT 成员关系；用户级历史页保持原有范围。
- 普通观察响应丢失沿相同 cursor 重读并幂等合并；GAP／owner 更换沿既有快照恢复。只读 open 的响应丢失时，重试按 browser instance＋精确阅读目标替换并回收旧阅读附件，失败／取消路径释放已建立附件；不新增持久回执或 lease。

### 5.5 清单与状态摘要耦合：分页和生命周期统一读取

不能把几个现成的“摘要数组”直接当完整列表：

- `CanonicalControl.prompt_queue` 是有界控制快照；完整队列阅读使用按 session／sequence 的只读分页。
- Desktop 与模型 `query_cells` 复用当前 Cell owner 的分页和可用性规则；`snapshot_cell_index` 不是完整清单。
- Agent 的 `list_active_agent_sessions` 是压缩交接候选筛选，不能代替 `query_agents` 的全体当前可见成员。

模型工具和 Desktop 读取同一业务查询 owner，各自做响应格式适配。UI 不执行模型工具，不经过 Hook，也不套模型 tool-result envelope 的预算裁剪。

共同队列按 session／queue sequence 提供只读分页，保留已有单页大小约束；ROOT 与 Agent 都能渐进浏览。队列总数、Work 排队数、待处理 steer 数按明确类型计算，不把所有 PENDING 输入都称作 Work。页面使用真实状态与真实身份合并变化，不能把“本页未出现”当作“已退役”。

Cell 统一列表和详情的存活／不可用判定；保留展示当前仍存在的终态 Cell。Agent 统一可见性查询；Host 卸载与 session 删除分开。刷新复用现有 session 观察与进程内变化通知，必要时在可见期间做只读刷新；通知只提示重新读取，不变成新的状态来源。

读取结果要区分页面未加载、对象确认不存在和整个服务暂不可用。使用已有 cursor、owner epoch 和请求取消；不为侧边栏增加 durable inventory、保活 lease、全局快照账本或总对象数限制。

列表分页是弱一致观察：按真实身份合并页，刷新从头另起扫描；已见对象本轮未出现时，用同一 owner 的单对象读取确认不存在后才移除。新建对象落在已扫游标之前，可由下一轮刷新发现。请求错误不是空列表。队列游标携带稳定 queue sequence，不通过已删除行反查位置；ROOT／Host／reader 改变后丢弃迟到页及旧的单对象确认结果。

### 5.6 代码阅读与文件路径耦合：复用视图，保留不同内容来源

`FileCode` 只负责原文、高亮和行号，适合直接复用；`FilePreviewProvider`／文件浏览页还管理 path、read token、路径历史、重新打开文件和“在文件夹中显示”。把 Cell 填成一个假路径会把两种内容语义混在一起。

共用 `FileCode` 和必要的阅读页壳：接收内容页、语言、加载状态与读取下一页行为。普通文件由原 `FilePreviewApi` 提供内容，保留路径导航和刷新；Cell 由既有冻结源码 reader 提供内容，保留 Cell 状态与图入口。两种外层使用同一显示组件；Cell 不继承文件工具栏，按 §3.2 仅装配工作流图按钮。不给 Cell 伪造 filesystem token，也不为了复用建立通用文档框架。

文件页使用字节 cursor，Cell 使用服务端字符 offset；UI 把下一页位置视为数据源返回的 opaque 值，不用 JS UTF-16 字符数自行计算，不强行统一两种分页单位。

后端源码阅读按 ROOT/session＋canonical source attempt 直接读取冻结 CanonicalContent，核对 attempt 属于可访问 ROOT；使用既有 inline/blob 与字符分页 owner。模型 query_cells 仍为 live scope；Desktop 历史源码不依赖 live Cell/Host，不复制冻结或分页算法，不新增 Cell 历史表或代码 hash。

源码读取期间 Cell 可正常退役；Desktop 在读取前后校验真实 ROOT/session 和 attempt 可访问性，不能把任意 attempt ID 当作授权。live Host/Cell 已回收或应用重启不使仍保留的 canonical 源码失效；会话删除、内容 retention 与暂时服务错误按实际原因返回，不持锁保活。

模型侧 `query_cells` 保持既有已捕获读取语义：capture 成功后，即使异步读取期间 Cell 退役，仍可返回已经捕获的源码页。共同 owner 负责 capture、canonical 解码与分页；Desktop 的尾部存续复核不能进入模型公共路径。已有对应合同测试必须保留。

### 5.7 Cell 参与关系与调用参数耦合：观察实际解析结果

派活可以沿 `Work.source_tool_attempt_id → tool_invocations.source_exec_attempt_id` 关联到 Cell；这条路径应保留。它不能单独覆盖只发消息、查询或等待既有 Agent 的脚本。

当前 `original_arguments` 在 PreToolUse Hook 前记录；`wait_agent` 会把 Agent 绑定到当时的 Work；`send_agent_message` 也可能因无活动 turn 返回未接纳。不能把工具参数里的 ID、返回文本中的名字或 JavaScript 变量当成参与关系事实。

在共同 Agent API 的**实际对象解析／接纳位置**提供小型 typed 观察，携带已解析的 Agent／Work ID 和参与种类；Code Mode 的现有 invocation context 把它归属到实际 Cell。`wait_agent` 在完整绑定后、进入等待前发布，`wait_agent.works` 在全目标校验后发布，不等工具返回；单派活／batch 只有实际接纳或 ACK 确认成功才成立。

- 已接纳 Work 从 canonical 来源 join 读取，不在 Cell 再存一份。其他参与只保存去重的 Agent ID、可选 Work ID 与参与种类；不保存调用流水、Work 状态、消息正文或依赖图。
- 具体 query／stop 完成目标校验后记录；wait 在完成绑定后记录；message 在实际目标验证后记录参与，即使随后未接纳也不声称送达。ROOT 目标不生成子代理卡片。
- 无 target 的纯清单查询不自动吸纳所有返回对象；无效目标不产生节点；所有图节点仍与当前 ROOT 可见性相交。
- 观察归于现有进程内 Cell owner，按下面的预算合同处理，退役时一起释放；不建立持久化参与者表，不扫描无限历史，观察失败不替换业务调用结果，也不重放副作用。
- 图的边来自 §5.2 的 Work 输入关系，参与者观察只解决“哪些对象与此 Cell 有关”，不成为第二份编排状态。

首版不画消息边，因此不为图补写消息 provenance 字段，也不增加事件或消息表。

此处新增进程内观察的唯一理由是：纯读取／等待属于 Cell 参与，却没有 Work 接纳行。图返回 `participation_coverage: complete | incomplete | unavailable`；未完成的预算申请、观察失败或丢失不能显示为完整空图。真正遗漏过观察后，incomplete 在该 Cell 寿命内保持，不能因后来一次 ACK 成功恢复 complete；只有尚无遗漏的在途操作完成才可恢复完整。canonical Work 关系仍独立可读；整个 Cell owner 确认退役时图进入 CLOSED；临时 sidecar/网络失败保留真实错误，不冒充关闭。

Python 集合不会自动计入现有 Cell／Host 预算，必须复用现有 retain／reserve 资源 owner，对每个新唯一引用的 canonical serialized UTF-8 字节计费；不宣称这是 Python 实际堆大小。参与种类使用固定宽度位集的固定长度序列化，不让后续种类合并产生未计费增长。共享预算有真实资源开销，不承诺零成本。

观察不在业务调用路径等待 `_retain_data`。Cell owner 使用至多一个在途、受现有单值大小边界限制的观察资源操作，不另建无限待处理队列；无法立即容纳的新观察标记 incomplete。收到成功 ACK 后只有同一 Cell 仍有效才安装引用；拒绝或失败仅降级覆盖。退役／取消后的迟到 accepted ACK 必须 release，或由已完成的 forget 保证回收，两者不能重复回收；不能直接丢掉 reservation 所有权。复用现有资源操作及其结算清理，不增加独立账本、抢占调度器、持久日志或逻辑任务总量上限，也不因 UI 观察请求推迟 Cell `_collect`。仅终态但尚可查询时，已安装的观察仍然存在。

### 5.8 Tab 归属与目标身份耦合：区分承载 ROOT 和被查看对象

`DesktopWorkspaceProvider` 按承载 ROOT 的 `sessionId` 归组会话范围 tab，网页 tab 不绑定会话；`WorkspaceTab` 使用明确的 discriminated target，打开 Agent 不切换承载 ROOT：

| 页面 | 承载归属 | 目标身份 |
|---|---|---|
| Agent 列表 | ROOT session | 同一个 ROOT |
| Agent 会话 | ROOT session | Agent session；不把临时 Host 纳入会话 tab 去重键 |
| Cell 列表 | ROOT session | 当前明确的 ROOT Host |
| Cell 源码 | ROOT session | canonical source attempt＋阅读位置 |
| Cell 实时图 | ROOT session | 原 Host＋Cell＋图视图 |
| 网页 | 用户级，不绑定会话 | 浏览器 tab 自身身份 |

Agent 会话在其 Host 卸载／重载后仍是同一会话；Cell 实时图绑定原 Host；冻结源码使用 canonical attempt，跨 Host/重启保持同一阅读身份。Work 是 Agent 会话中的定位参数，不形成额外顶层页面。对话消息使用 Agent 的内容读取 owner，不能误用承载 ROOT 的 artifact／image reader。

这包括 Markdown 图片经过的嵌套 `FileLinkContext.readImageReference/imageOwnerKey`，不能只替换 `onReadPromptImage`。保留现有工作区文件链接打开行为，图片／artifact／canonical 内容 reader 绑定实际 Agent。任何衍生内容 tab 的闭包也必须绑定这个 owner，退役／切换后丢弃旧响应。

列表点击、图节点点击和 `open_in_pulsara` 都转换为同一种导航意图，经过同一个准备／打开函数。入口只携带动画选项：有列表源元素时使用共享元素过渡；工具或图直接打开。页面分支保持穷尽，使未覆盖页面在类型检查时暴露；不新增动态页面注册框架。

### 5.9 活跃观察与退役

[subagent.py](src/pulsara_agent/conversation_kernel/subagent.py) 已区分卸载 live session 与最终删除。卸载不一定意味着 ROOT 查不到；UI 应服从查询结果，不自行以“没有 live Host”删除 Agent 行，也不为了读历史重新唤醒它。

Cell 的终止与退役同样不同：`_collect` 在物理终态、VM 释放、pending/结算完成及输出 FULL 等条件满足后移除对象；静默终态不等待模型 ACK。UI 按现有清单观察这个变化，不能把“脚本结束”提前当作退役，也不能占用输出观察 reservation 来延迟退役。

所有页面刷新只读事实，不产生模型工具调用、额外会话消息或 provider 输入。列表、详情和图共享一次明确对象定位；连接／ROOT 切换后丢弃旧响应，不把旧对象 ID 绑定到新 Host。

## 6. open_in_pulsara 的闭合合同

沿用现有批量准备、统一发布、聚焦 tab 和用户交互优先的交付链。输入仍以 canonical target 表达对象，不再增加业务 ID 或通用展示包装层：

| 输入 | 允许的目标与行为 |
|---|---|
| `targets` | 现有 PATH／URL／PROCESS／MCP_SERVER／PANEL，加 AGENT／CELL；CELL 打开源码 |
| PANEL | capabilities／background／subagents／cells／browser 分别打开「能力／进程／子代理／cell／网页」，名称及顺序见 §1 |
| AGENT | `{"kind":"AGENT","agent_id":"session:…"}`，打开所属 ROOT 的 Subagent 只读会话；ROOT 自身不作为该页面目标 |
| CELL | `{"kind":"CELL","cell_id":"cell:…"}`，复用现有 canonical Cell target |

根对象只接受必需的非空 `targets` 数组。元素沿对应闭合 target schema 校验，不新增总目标数量上限。外层类型、缺失／空数组或未知根字段错误仍整次拒绝；元素错误沿现有 `targets[index]` 逐项规范化／失败规则处理。按数组顺序准备目标，错误的 `index` 保持原数组的零基位置。仍返回 `targets` 展示结果数组与可选逐项 `errors`，最后一个成功目标获得焦点。

展示结果沿既有 `{target, ...}` envelope；新增 AGENT／subagents 结果绑定调用时 ROOT，CELL／cells 另绑定实际 Host；既有目标的输出合同保持。前后端 schema、严格结果解析、标签、准备与导航同步更新。参数统一经过一条规范化路径，不保留新旧包装格式或兼容分支。新增 schema 只在本文开头规定的 provider 前缀边界安装。

工具新增的对象入口只面向 Agent 和 Cell：分别打开只读会话与冻结源码，不提供 Work 定位或工作流图参数，也不在工具结果中增加视图选择字段。进一步查看工作流由用户通过 Cell 页面操作。详情跳转及工具打开使用 §5.8 的同一导航函数，工具触发不播放列表展开动画。

当前 `OpenInPulsaraTool.normalize_target` 主要做语法规范化，不能扩大成自行查库、查 Host、管理前端 tab 的第二个对象服务。新增对象目标在共同读取 owner 完成解析和可见性核对，工具层只适配其 typed 结果；ROOT／Host 身份由实际调用上下文绑定，前端不得从“当前激活会话”补齐旧请求的对象身份。

规范化与前端真正打开之间仍可能退役，页面准备阶段按同一读取 owner 再核对。单个无效／退役目标逐项失败，继续准备其他有效目标；承载 ROOT 已切换或用户更新了导航意图时，取消这次批量发布，不把剩余项投递到新工作区。复用现有交互优先机制，不建立后台导航队列或自动重新打开机制。

目标必须仍在 ROOT 可见范围内。不补历史条目、不启动模型、不重新运行脚本、不为导航保活。返回仍表示展示请求结果，不承诺用户已看见或读完。

## 7. 实施分工与 hard cut 边界

依次落地以下共同链路；各模块可在接口确定后并行实施：

1. 后端／协议先定义共同读取入口、ROOT／目标身份和只读连接合同；Work 输入存储由原 repository 唯一维护。
2. 完成 `work_inputs` hard cut，以及来源、队列分页、Cell 源码／参与观察接口。数据库变更只服务 §5.2，其余复用已有事实；不增加图表、持久观察或消息 provenance。
3. ROOT 先迁入抽出的共同阅读主体，保留原有阅读和操作行为；再接 Agent 只读装配，不留下两套长期分叉实现。
4. 接两个清单、详情和 Cell 局部图；`open_in_pulsara` 接入相同导航与准备函数。
5. 删除替代的 reader／投影／队列分支、两列旧输入及其读写；同步 clean-v0、接口文案、测试和 bundled Skill 中的展示例子。旧 Work 两列不双读双写。

冻结源码跨退役继续阅读；图确认退役温和关闭；具体解析后的读取、等待、消息和停止算参与，纯清单查询不算。这些均为确定行为，不留待实现阶段重新选择。

实现过程中若发现现有 owner 无法满足本文，应先给出具体冲突及最小改法；不以“方便 UI”为理由扩大持久化、执行权限、对象生命周期或 provider 重建边界。

## 8. 实施验收

验收针对真实共同链路，不能只证明新页面 mock 可渲染。下表列明关键合同与现有测试入口；可在对应模块增加必要用例，不复制一套测试框架。

| 验收面 | 必须证明的行为 | 主要入口 |
|---|---|---|
| 存储与启动 | `work_inputs` 唯一来源；成功、失败、取消保留关系；ordinal 缺口及 dependency 优先；完整初始组、启动确认和压缩边界保持；dependency＋FIFO 组合环拒绝，已物化关系不造假环；分别验证 null 未物化、空串已满足且原样存储、纯空白原样；依赖与普通空材料经过 prepare／启动／未知 ACK 确认均只生成一个固定说明 entry，预算及 provider 读取同一 canonical 投影 | `test_agent_requests_persistence.py`、`test_agent_collaboration_postgres.py`、`test_stage5_clean_migration.py` |
| 删除竞态 | 两事务分别覆盖删除先赢／物化先赢，以及来源自身有 pending Work 时解析与级联删除不死锁；整批接纳回滚；已保存正文不依赖来源继续存活；不增加跨 session 保留约束 | 同上，使用受控并发而非时间碰运气 |
| 只读与 Host | 打开不 resume、不取得 writer、不刷新业务闲置时间；Core 管理的 Agent 可观察；卸载后历史可读，重载即使 epoch 相同也清除旧 draft；连接响应丢失／取消不泄漏 observer | `test_stage2_protocol_v3.py` 与 Desktop bridge 集成测试 |
| 队列与来源 | 多份无 command_id 的 Work 和多发送者 steer 独立展示；ROOT 本地提交／revision 身份仍正确；消费与正文 hydration 竞态、分页、计数和原控制回执保持 | `tests/frontend/runtime-adapter.test.ts`、`test_stage2_canonical_reader.py`、`test_pr04_prompt_queue_actions.py` |
| 共同阅读 | ROOT／Agent 的历史分页、推理、工具、压缩、TODO、live settlement 与阅读定位一致；ROOT 注释监听不捕获 Agent 选区；图片及衍生内容使用实际 Agent owner | `history-paging.test.ts`、`history-paging-review.test.ts`、`thread-follow-controller.test.ts` 及对应组件测试 |
| 导航与生命周期 | 同 Agent 多 Work 仍一行一 tab；ROOT 切换和迟到响应不串数据；源码退役仍按 attempt 读取，图关闭清空节点但留 tab；暂时错误不假装删除；tab 切换恢复内部阅读位置 | `tests/frontend/desktop-workspace.test.tsx` |
| 冻结源码 | inline／path／Hook 后实际源码；文件后来修改删除无影响；Unicode 分页不丢不重；Desktop 源码跨 Cell/Host 退役与重启读取，越权 attempt 拒绝；模型侧已捕获读取仍可跨退役完成；不消费输出 | `test_query_cells_contract.py`，保留 captured-source-survives-release 合同 |
| 图与参与 | 纯工具／纯 Agent／混合 Cell；同 Agent 多 Work；跨 Cell 与原生来源依赖；失败、取消、退役无假边／幽灵节点；具体 query/wait/message/stop 及 batch／Hook 后身份；无 target 查询不吸纳清单 | `test_agent_code_mode_execution.py`、`test_agent_collaboration_api.py` 及只读图接口测试 |
| 观察资源 | 不等待观察 ACK 才完成业务；资源拒绝／真实遗漏保持 incomplete，UI 不显示确定无编排；在途 ACK 与 Cell 退役交错不泄漏、不重复释放、不延迟 collect；存续终态仍可读取观察 | 现有 Code Mode 资源与执行测试 |
| 展示工具 | 所有 target 分支、混合有效／无效元素的原数组错误索引、最后成功焦点；单目标失效不吞有效项；用户导航优先；严格结果解析与上下文绑定 | `test_open_in_pulsara.py`、`presentation-provider.test.tsx`、`presentation-result.test.tsx` |

Agent／Cell／队列超过一页时仍能完整渐进浏览，缺席一页不等于退役；不新增逻辑对象总量上限。图只读取需要的 ID、关系与状态，不顺带加载全部正文。

视觉验收核对五个顶层名称、两个列表的紧凑单行密度、Cell 顶栏仅有「查看工作流图」（无入口时整栏消失），以及所有页面没有附加灰色小字说明。动画验收包括 tab 切换、列表展开、源码到图、条目增减、局部展开收起、图节点直接打开的例外、快速重入／关闭时清理、减少动态效果设置及无 View Transition 降级；不产生重复共享元素名称。本轮保留必要 build／typecheck 与合同测试；按人类决议无需视觉验收或 Playwright。

定向测试、clean-v0／catalog 核对、前端构建和安装后的本地数据库重置记录见 [验证记录](output/subagent-cell-sidebar-implementation-20261030/verification.zh.md)。两组后端联合运行各有一项环境相关失败及独立补测，按记录分别报告，不合并为一次全绿。未运行全仓测试或真实 provider；本规格不要求真实 provider。本轮按人类决议无需视觉验收或 Playwright。
