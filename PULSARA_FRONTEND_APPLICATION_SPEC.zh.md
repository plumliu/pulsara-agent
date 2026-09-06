# Pulsara Frontend Application Specification

状态：Active（2026-08-30）

2026-09-06 能力管理面同步：导入、凭据、编辑、删除与自动采用的当前契约由
`PULSARA_CAPABILITY_PAGE_MCP_EDIT_CREDENTIAL_AND_INSTALLABLE_CAPABILITY_DELETION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`
收敛；本页不另设控制面或兼容路径。

本规范定义 Pulsara 的第一套完整前端产品面。它消费 Kernel 现有的 renderer-neutral
Terminal Protocol v3，但不成为 conversation、tool、permission、memory、subagent 或
execution 的第二套 authority。

## 1. 产品定位

Pulsara 前端是面向本地长时 Agent 工作的「可观测工作台」。它同时服务三类阅读：

1. 用户快速理解目标、当前进度和最终结果；
2. 开发者检查 reasoning、plan、tool、terminal、permission、compaction 与失败边界；
3. 操作者在不中断主任务的情况下观察 Kernel 已发布的子任务和依赖状态。

产品不采用普通 IM 式聊天窗口作为唯一中心。主视图以 canonical conversation 为叙事轴，
把 process-local live observation 作为可丢失的当前运行层叠加，并把执行相关上下文放到
独立 inspector 中。

## 2. 信息架构

全局 activity rail 只提供四个已有端到端实现的产品面：

- **总览**：当前 mission、最近 session、运行脉冲与本地内核健康；
- **会话**：canonical transcript、live output、plan、tool trace、queue/steer 与 composer；
- **能力**：只管理用户目录 `~/.agents` 与 `~/.pulsara` 中的 Plugin、MCP 与 Skill；
- **设置**：真实生效的主题偏好，以及本机启动配置提供的模型与服务状态。权限不是全局偏好；
  它属于每一次用户发送。

子任务不是跨会话的独立产品面。选择会话后，其完整子任务清单进入右侧“当前会话”检查器；
会话列表只展示“进行中”“需留意”等紧凑聚合，不在全局导航或所有会话行中复制任务树。

任何尚无 Kernel/本地应用控制面端到端实现的功能都不得保留导航项、按钮、空卡片、
“即将开放”或“暂不可用”占位。例如记忆浏览、附件、文件变更列表、通知、
会话菜单和虚构上下文百分比在拥有对应实现前都不进入用户可见 DOM。

会话工作台使用四栏桌面布局：activity rail、session sidebar、conversation workbench、
inspector。窄屏时 inspector 变为抽屉，session sidebar 变为 overlay；手机宽度下 activity
rail 变为底部导航。

窗口从桌面缩窄时自动收起 inspector；窄屏仍允许手动展开，通过遮罩或关闭按钮收起。
重新放宽窗口不强制覆盖用户的收起选择。composer 按自身可用宽度而非整个窗口决定布局：
模型标签保持单行、省略超长部分，悬停和模型菜单保留完整名称；发送/停止按钮不被压缩。
空间不足时，推理、Skills、规划与权限使用同一组控件收进向上展开的“选项”面板，
当前权限仍在入口可见。Escape 或点击输入框之外关闭面板，不重置已选设置。

## 3. 视觉系统

视觉语言称为 **本地智能工作台**：

- 温暖的 paper surface 表达本地、可触摸与可检查；
- ink rail 表达稳定的 host shell；
- amber 表达正在运行与用户注意；
- blue 表达 navigation、model/context 与 provider surface；
- violet 表达 meta、Skill、memory relation 与 waiting dependency；
- green 表达 accepted、verified、healthy；
- red 只表达 destructive、failed 或 interrupted。

品牌轨道与星点使用 CSS 几何绘制，不引入模型生成插图。仓库现有 banner 作为社交分享图，
不改变原始品牌素材。

界面支持 light/dark theme、reduced-motion、键盘焦点、可读的状态文字与不依赖颜色的状态
图标。主要内容在 620px 以下保持单列可用。

排版使用全局统一字号阶梯：最小状态与徽标不低于 10px，辅助信息为 11–12px，控件与短标签
为 13–14px，对话及长文本正文以 15px 为基线，标题随层级继续放大。响应式布局可以隐藏次要
信息或改变排列，但不得通过缩小正文来维持原有密度。

会话工作台的中间对话版心统一使用与 Pulsara /“你”身份标识相同的 serif 字体，使用户输入、
模型回复、可见思考和原位执行叙事形成同一阅读语气。侧栏、顶栏、检查器、composer 等外围 UI
继续使用既有 sans 字体；代码、终端输出、时间和机器状态继续使用 mono 字体。

## 4. Canonical / live 边界

前端投影严格遵守以下所有权：

| UI 内容 | Authority | 前端行为 |
| --- | --- | --- |
| user/assistant/tool result entry | canonical relational row | 只由 snapshot/committed observation 建立 |
| active turn、prompt queue、plan、task board | canonical control | 收到 current control 时整体替换对应控制投影 |
| streaming text/thinking/tool output | process-local live owner | 按 owner epoch/revision 追加；GAP 后重新取 live snapshot |
| terminal monitor、TODO、subagent progress | process-local live owner | 可在 Host replacement 后消失，不写成本地 durable truth |
| renderer tab、filter、theme、draft | renderer local state | 不影响 Kernel commit 或 Host close |

前端不得从 live event replay 推导 canonical completion，不得把 UI cache 提升为 recovery
checkpoint，不得发明 job、receipt、lease、generation、projection authority 或 replay reducer。

## 5. Protocol v3 映射

浏览器端通过 `RuntimeAdapter` 接口隔离 transport。真实 desktop/localhost bridge 负责
protobuf frame 与 renderer model 的翻译，Kernel 继续拥有全部 authority。

连接流程：

1. `HelloRequest` 申请 observer 或 controller attachment；
2. `SnapshotRequest` 获取 bounded `CanonicalSessionSnapshot`；
3. 以 event sequence、live owner epoch/revision 与 live-control revision 长轮询
   `ObserveRequest`；
4. committed observation 更新 transcript/control；live observation 更新当前 draft；
5. 任一 typed GAP 都触发对应 snapshot refresh，不猜测缺失内容；
6. detach/close 时释放 attachment，不把 process-local state 持久化到浏览器。

用户动作映射：

- 普通发送 → `SUBMIT_PROMPT`；
- active turn 中显式 steer → `STEER_ACTIVE_TURN`；
- 停止 → `STOP_ACTIVE_TURN`；
- 进入 Plan → `ENTER_PLAN`；
- 压缩上下文 → `COMPACT_CONTEXT`；
- tool/permission interaction → `ResolveInteractionRequest`；
- Plan question/draft → `ResolvePlanInteractionRequest`；
- 带入未处理的 worker result → `ACCEPT_SUBAGENT_RESULT`。未指定已有目标 turn 时，它以
  composer 当前选择的 permission mode 创建新的 ROOT turn 并立即继续；指定已有目标 turn
  时不得重新携带或解释权限。

command receipt 的 `PENDING` 不是成功。前端使用 `QueryCommandRequest` 查询不确定 outcome，
不能因为网络中断重复提交同一个逻辑 command。

## 6. 会话交互

- 新建会话只选择工作目录来源：**快速开始** 或 **指定目录**。快速开始由 Pulsara 在
  受管根目录中创建一个持久工作目录；它不是会随退出清理的临时对话。指定目录要求用户
  提供现有本地绝对目录。两种会话都必须可在应用重启后恢复；
- 创建会话不接收目标文本、Plan 开关或 permission mode。创建成功并建立连接后，用户才在
  composer 中提交第一轮输入；
- active turn 时 Enter 创建 future prompt queue item；Cmd/Ctrl+Enter 才 steer 当前 turn；
- composer 正处于输入法组合输入时，Enter 完全交给输入法处理：不得发送、排队或 steer，
  也不得 `preventDefault` 阻止输入法确认候选或保留原始拼音；组合输入结束后的普通 Enter
  才恢复上述发送语义，并兼容浏览器以 `isComposing` 或 `keyCode=229` 暴露组合状态；
- pending queue 在 current turn 完成前保持可见，不混入 active assistant draft；
- 普通用户输入以右侧独立说话者块呈现，使用用户图标与“你”标识；运行中的显式引导则以
  紧凑的“你 · 引导”事件嵌入当前执行流，不伪装成一轮新的普通对话；
- 已接纳的子任务结果以中性的“已带入子任务结果”事件开启后续处理，不显示成用户亲自输入，
  不暴露结果 ID 或工具确认回执；悬停或键盘聚焦时解释它已作为上下文交给 Pulsara；
- 每个普通用户输入后的首段 assistant 输出建立一次 Pulsara 说话者边界，即使首段内容是
  思考或工具；用户引导仍属于当前执行流，不重新开启边界。其后的连续思考、工具、计划和
  子任务活动不得因底层记录分段而重复插入 Pulsara 标识；
- 后续模型非空自然语言正文真正开始时，再紧贴正文绘制 Pulsara 头像与名称；若同一条正文
  记录在正文前含有可见思考，先连续展示思考，再建立正文说话者边界。若首段 assistant 输出
  本身已经包含正文，只使用开头的一个标识。尚无任何模型输出的实时草稿只显示中性的处理
  状态，不伪造自然语言回复；
- composer 下方提供独立的“先规划”按钮。它只修饰下一次发送：发送时先请求进入 Kernel
  既有 Plan workflow，再提交同一轮目标；它不是会话创建属性，也不是项目级持久设置；
- permission selector 与发送按钮相邻，只提交这一次新 turn admission 所需 mode。下一轮可
  重新选择，已冻结 turn snapshot 不被改写；active turn 的 steer 不重新解释权限；把子任务
  结果带入会话并继续同样会创建一轮新处理，因此复用此刻 composer 选择的本轮权限。菜单从
  上到下固定为“只读”“每次询问”“接受编辑”“完全访问”，新打开的应用默认选择“完全访问”；
  每次普通发送或用子任务结果继续成功创建新 turn 后，composer 也必须立即重置为
  “完全访问”，不继承刚刚送出的低权限选择；
  四种模式共用同一 host-local 能力语义：“只读”可读取本机文本但禁止写入与 Terminal；
  “每次询问”允许本机操作，但结构化写入、Terminal 与其他外部副作用逐次确认；
  “接受编辑”直接允许 workspace 内结构化写入，workspace 外写入与 Terminal 仍逐次确认；
  “完全访问”直接允许普通本机读写、Terminal 与网络访问，此时 workspace 只是相对路径和
  初始工作目录，不是沙箱。操作系统权限、Plan 的 read-only overlay 与既有 hardline 灾难
  底线不因 mode 改写；
  “完全访问”的当前选择与菜单项均使用红色文字和红色三角警告图标；
- compaction 仅请求 safe-point operation；前端不得自行裁剪 canonical transcript。Kernel 在
  canonical FULL 前对 active/idle 使用同一 summary、candidate assembly、reclaim validator 与
  缩尾搜索，只在 FULL 后决定继续当前 turn 或等待下一条用户消息。两者的 `NOT_NEEDED` 使用
  同一成功语义，提示“当前上下文已经较紧凑，本次整理无法进一步缩小。”，不展示成失败或不可用。
  真正发生 `CompactionAdopted` 后，前端从 canonical control 派生当前最近一次压缩边界，在边界
  两侧使用 CSS 实线并居中显示“上下文已压缩”；它不得改写或裁剪 transcript，刷新与重连后必须
  可恢复，`NOT_NEEDED` 不得创建该标识；
- stop 按 exact turn/task ID 路由；没有全局“停止一切”的模糊操作；
- 已实现的 response action（当前为复制）贴近所属 assistant entry。复制只属于每轮最终的
  assistant 正文；工具调用前的中途正文与尚在生成的实时草稿不得绘制复制入口；
- 对话末尾的静态安全间距只保留折叠态 TODO 清单的高度与少量呼吸空间。“回到最新”和展开的
  TODO 清单继续作为消息上方的动态覆盖层，不得用大块永久留白为其预留最大尺寸；
- tool trace 默认压缩，高价值 terminal/live output 可原位展开。同一工具组以及跨记录连续执行、
  中间没有正文、思考、用户输入或其他可见语义边界的工具，使用同一条连续轨道与紧凑等距间隔；
- `reload_capabilities` 是刷新当前 Host 后续 Skill、MCP 与 Hook view 的唯一产品名；前端不得继续
  显示已删除的 `reload_plugins` 名称。`list_mcp_servers` 展开后从其既有 canonical result 呈现
  实际 MCP 服务、状态与工具数；带 server filter 时只呈现该服务的真实工具名。
  `inspect_new_mcp_tool` 只展示模型检查的 exact server/tool；`use_new_mcp_tool` 仅通过前序 inspect
  result 中的 exact `tool_ref` 做页面内关联，只展示最终调用的 server/tool。Provider 调用名、effect、
  schema、参数清单、完整工具说明与原始调用参数属于执行细节，不直接倾倒给用户。前端不猜测 ref、
  不新增 Kernel 字段，也不把 endpoint 可达冒充工具调用成功；
- ordinary `read_file` 的 exact path 若与当前 effective Skill catalog 中某个 `SKILL.md` 的 canonical
  path 或 catalog location 完全相同，tool trace 将“读取文件”改写为“正在使用 `<name>` Skill”。
  这覆盖 workspace/user 的 `.pulsara/skills` 与 `.agents/skills` 四个 loose roots，也适用于当前
  catalog 已证明的 bundled/Plugin Skill；仅凭文件名、父目录形状或模糊 suffix 不得伪造 Skill 使用；
  任一可见语义边界都必须断开轨道；
- 超出 inline projection 的完整输出通过 `ReadContentRequest` / artifact path 分页读取。

### 6.1 产品语言边界

前端不得直接展示协议名、版本号、内部 owner/attachment/generation/epoch、canonical、
projection、Kernel、HostSession、provider prefix 或其他实现术语。状态、错误、检查器和设置页
统一翻译为用户可行动的产品语言，例如“本地服务”“正在连接”“当前会话”“已载入”“可恢复”
和“本轮权限”。会话侧栏必须区分：当前页面实际连接的会话显示“当前会话”；已由本地进程
载入但不是当前页面连接的会话显示“已载入”；只有 durable 会话事实、尚未载入当前进程的
会话显示“可恢复”。成功 create/resume/reconnect 后必须重新读取 authoritative session list，
不得继续展示打开前缓存的 availability 文案。
这些术语可存在于源码、规格和诊断中，但不得出现在用户可见 DOM 文本。

### 6.2 供应商可见思考

前端只展示模型供应商实际返回的可见 reasoning 文本，不按模型名称猜测，不从回答反推，也不
生成伪造思维链：

- Chat Completions 的文本 `reasoning_content` / `reasoning` 以及 Responses 的
  `reasoning_text` 映射为“思考”；
- Responses 的 `reasoning_summary_text` 或最终 reasoning item 的 `summary` 映射为
  “思考摘要”；
- encrypted carrier、token 计数、opaque details 及没有文本的 reasoning item 不进入 DOM；
- 流式阶段使用 process-local thinking event 展示最新非空行与“思考中”，完成后使用首个
  非空行作为折叠预览；展开后显示供应商返回的完整纯文本；
- 已完成内容从 assistant entry 已关联的 durable provider replay 派生，只增加 read projection，
  不新增数据库表、事件、canonical assistant block 或 provider input 内容；刷新与重连后仍可恢复；
- 大于既有 inline 边界的思考沿用 `ReadContentRequest` 分块读取，不另设前端总长度上限；
- 没有供应商可见文本时不渲染任何“思考不可用”占位；子任务 scope 使用同一投影并在其原位
  活动中展示。

### 6.3 用户能力面与会话有效目录

“能力”是一等页面，但不得维护独立注册表或数据库副本。该页面只枚举用户拥有的两棵根：
`~/.agents` 与 `~/.pulsara`。项目目录、Pulsara bundled Skill 以及 workspace Plugin 不进入这个
页面。浏览器进入页面、重新获得焦点或显式刷新时重新读取文件系统与 Plugin store，不从旧 UI
cache 恢复目录真相。

- 页面使用 Plugin、MCP、Skill 三个 tab、统一搜索、真实计数与一致的开关交互。Skill 开关按
  `SKILL.md` 的 canonical path 写入 `~/.pulsara/skills.yaml`；未配置路径默认开启，关闭的 Skill
  仍留在用户能力清单中，但不得进入会话 effective Skill catalog、自动提示或显式 `$skill-name`
  解析；关闭高优先级定义后，同名低优先级定义按既有解析顺序自然接替；
- Skill 通过既有 atomic local installation service 安装到用户根。安装结果及 diagnostics 来自
  该 service，前端不自行复制、校验或覆盖 Skill 文件。Skill 开关配置同样使用 atomic replace，
  单文件读取边界为 1 MiB；配置损坏时用户 Skill fail-closed，能力页显示需留意而不静默重启；
- MCP 新增、编辑、启停与删除写入 `~/.pulsara/mcp.yaml` 单一配置真相，认证值只进入
  `local-settings.yaml`。导入预览与保存、连接测试分别结算；OAuth 保存不等于登录。
  Plugin 先选目录并自动识别；唯一发行版直接预览，多个发行版展示组件差异后才让用户选择。
  经一次性官方转换后进入唯一 native validator，安装为 disabled；
  实例连接参数与凭据在独立编辑器填写。启用必须提交当前 exact package 的完整 review，
  不是把普通开关点击当作已经审阅；删除清理实例专属凭据、保留 Plugin data；
- loose Skill 可从普通或外部宿主目录独立导入，完整保留资源。只有用户拥有的 managed
  roots 支持删除，bundled/inherited/Plugin child 不出现假删除按钮；
- 模型侧 ROOT `manage_capability` 与 GUI 共用 typed owner 和表单。用户私有输入不进入
  模型参数、返回值或 live 投影；permission、配置 mutation 与 adoption 独立结算；
- 每次 MCP/Plugin 变化后，控制面并行通知所有当前已打开会话重新读取用户 MCP 配置与 enabled
  Plugin view，并通过既有 safe-point owner 采用。一个会话失败不会阻止其他会话采用，页面会
  显示需重试的会话数量；Skill 配置由每个会话在同一既有 safe-point Skill observation 中读取，
  不新增 reload 通道、provider 调用或 epoch 重建；不自动恢复进程重启前的执行；
- 用户能力页面可以把实时状态叠加到当前已打开会话，但必须同时 exact-join 当前用户配置来源与
  resolved config identity；workspace/Plugin/Host override 中同名 MCP，以及用户配置更新前的旧
  Host 结果，其状态、计数、说明与工具都不得泄漏进用户清单；
- 可选 MCP 的首次连接可能晚于 Host 快速启动窗口。连接一旦完成，能力页必须从同一个
  bounded discovery candidate 展示真实工具、资源与说明，即使该 candidate 尚未在下一次合法
  provider safe point 进入 effective tool generation；这个 disposable inspection 只读，不安装
  provider surface、不取得 dispatch authority，也不得把“已发现”伪装成当前正在运行调用的工具；
- 浏览器仍按当前 `session_id` 读取该 Host 的一次性 effective capability inspection，供 composer
  展示所有本轮可见 Skill。该 inspection 可以包含 workspace、user、Plugin 与 bundled 来源，
  但它不是一等能力页的数据源；
- composer 的 Skill 选择器只把可见 `$skill-name` 写入本轮草稿。发送仍走普通 prompt admission
  与统一 prompt compiler，不增加隐藏 activation API 或旁路模型调用；一等能力页不再提供
  “用于下一轮”按钮，Skill 开关只改变用户配置和后续 safe point 的 effective catalog；
- 能力页不得直接执行 MCP tool/resource/prompt，也不得为了展示而安装一套新的 provider tool
  surface；
- “已发现”不等于“已经进入正在运行这一轮的直接工具列表”。前端只陈述 catalog 事实，并用
  “Pulsara 会按需使用”表达产品语义，不猜测某个工具在某一轮会走直接调用还是先检查详情；
- 没有配置 MCP、没有匹配 Skill 或某个来源读取失败时，只显示对应真实目录状态；不得渲染
  无后端动作的 enable/delete/edit 占位控件。

## 7. 子任务面板

子任务同时出现在当前会话检查器和创建它们的主对话消息下方。用户无需离开当前工作就能
看到 Agent 是否真的完成了委派、正在做什么以及返回了什么。两处视图共享同一份合并投影，
不维护第二套状态；不再存在独立任务页。

检查器先按 `session_id` 通过 keyset cursor 分页读完 durable task inventory，再叠加当前进程的
control 与 live progress。切换会话或重新打开应用时先恢复数据库中的完整任务清单；进程重启
只会把未闭合任务置为明确的中断终态，不恢复 child coroutine、scheduler 或 mailbox：

- label、role/profile、objective、context mode、batch、parent/dependency、全部状态、terminal
  result、output preview、diagnostics 与 result acceptance 均来自 durable/current task facts；
- 检查器按创建批次或发起它们的主 turn 分组，支持全部、进行中、需留意、已结束筛选；展开后
  展示 Markdown 目标、依赖状态、最新进展、最终摘要与输出。未处理结果提供紧凑的“带入会话
  并继续”动作；详细语义不常驻占用版面，而在悬停或键盘聚焦时说明：结果会作为一条新消息
  交给 Pulsara，并按当前选中的本轮权限立即继续处理；
- 点击检查器任务可定位并展开对话中对应的原位执行块；对话内展示真实 reasoning、工具活动、
  ROOT 发给 worker 的补充消息和 Markdown 结果；
- 运行中的子任务默认展开，完成项可折叠；失败、取消、中断与依赖失败阻塞使用各自明确状态；
- 已提交的任务终态优先于迟到或残留的 live fragment；残留片段不得让已完成会话继续显示运行中；
- tool 结果状态依据 typed control 或具体结果 envelope 判定，不得通过“完成”“失败”等展示文案
  猜测；没有顶层 `status` 字段的成功 envelope 也不得误判为失败；
- UI 不伪造进度百分比、耗时、依赖连线或 ROOT 节点；
- `PENDING_START` 以“待开始”显示，不解释为 admission failure；
- task 数量不被前端施加总量上限，分页一直读取到服务端明确返回 EOF；
- 前端不提供 Kernel 尚无 controller contract 的直接 child 消息、DAG 编辑或重试按钮。模型已能
  使用的批量创建、依赖调度、等待、列表、补充消息、停止和结果提交都必须由真实执行投影体现。

## 8. 未接入产品面的可见性门槛

某个 Kernel 子系统已经存在，不等于前端可以展示它。只有当浏览器 bridge、typed adapter、
刷新/恢复路径、用户动作及测试全部闭合后，产品面才可加入信息架构。未闭合时直接不渲染，
不得以 demo data、禁用控件、占位卡片或 toast 代替实现。

## 9. 本地真实连接

本地应用生命周期、裸 loopback 访问边界、控制面、Protocol attachment 与关闭顺序由
`PULSARA_LOCAL_WEB_APPLICATION_LIFECYCLE_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 定义。
Production UI 必须 hard-cut 使用真实 localhost adapter；demo data 只可作为测试或设计 fixture，
不得在连接失败时成为 runtime fallback。

真实 integration 通过以下证据激活：

- observer/controller hello 与 attachment lifecycle；
- snapshot/history pagination；
- committed/live/live-control observation 与三类 GAP；
- queue、steer、stop、Plan、permission、content chunk；
- Host replacement 后 cold rehydrate，不恢复 live coroutine；
- ROOT/worker scope isolation；
- 同一 epoch 的 SYSTEM/tools continuity 不受 UI refresh 影响。

## 10. 验收

前端基线要求：

- `npm run lint` 通过；
- `npm test` 覆盖主要导航、两种工作目录创建、逐轮 Plan/permission、command mapping，以及
  session-scoped durable task pagination、全部终态、依赖、live overlay、定位与结果接纳；
- unit/integration 覆盖完整思考、思考摘要、实时 presentation kind、历史回放恢复与分块内容读取；
- unit/integration 覆盖 session-scoped effective capability inspection、用户级 Plugin/MCP/Skill
  清单与变更、Skill path enablement 的持久化与 fail-closed、所有 live Session 的动态采用，以及
  composer 显式 Skill 标记仍走普通发送路径；
- `npm run build` 通过 Cloudflare Worker-compatible Vinext build；
- 本地 route 返回非错误响应；
- 不打印、持久化或提交 `PULSARA_API_KEY`；
- social metadata 使用 Pulsara title/description 与可信 origin 下的品牌图；
- desktop、tablet、mobile CSS 均有确定性降级路径。
