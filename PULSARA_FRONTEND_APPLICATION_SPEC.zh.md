# Pulsara Frontend Application Specification

状态：Active（2026-08-30）

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

全局 activity rail 只提供三个已有端到端实现的产品面：

- **总览**：当前 mission、最近 session、运行脉冲与本地内核健康；
- **会话**：canonical transcript、live output、plan、tool trace、queue/steer 与 composer；
- **设置**：真实生效的主题偏好，以及本机启动配置提供的模型与服务状态。权限不是全局
  偏好；它属于每一次用户发送。

子任务不是跨会话的独立产品面。选择会话后，其完整子任务清单进入右侧“当前会话”检查器；
会话列表只展示“进行中”“需留意”等紧凑聚合，不在全局导航或所有会话行中复制任务树。

任何尚无 Kernel/本地应用控制面端到端实现的功能都不得保留导航项、按钮、空卡片、
“即将开放”或“暂不可用”占位。例如能力注册表、记忆浏览、附件、文件变更列表、通知、
会话菜单和虚构上下文百分比在拥有对应实现前都不进入用户可见 DOM。

会话工作台使用四栏桌面布局：activity rail、session sidebar、conversation workbench、
inspector。窄屏时 inspector 变为抽屉，session sidebar 变为 overlay；手机宽度下 activity
rail 变为底部导航。

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
  结果带入会话并继续同样会创建一轮新处理，因此复用此刻 composer 选择的本轮权限；
- compaction 仅请求 safe-point operation；前端不得自行裁剪 canonical transcript。Kernel 在
  canonical FULL 前对 active/idle 使用同一 summary、candidate assembly、reclaim validator 与
  缩尾搜索，只在 FULL 后决定继续当前 turn 或等待下一条用户消息。两者的 `NOT_NEEDED` 使用
  同一成功语义，提示“当前上下文已经较紧凑，本次整理无法进一步缩小。”，不展示成失败或不可用；
- stop 按 exact turn/task ID 路由；没有全局“停止一切”的模糊操作；
- 已实现的 response action（当前为复制）贴近所属 assistant entry。复制只属于每轮最终的
  assistant 正文；工具调用前的中途正文与尚在生成的实时草稿不得绘制复制入口；
- 对话末尾的静态安全间距只保留折叠态 TODO 清单的高度与少量呼吸空间。“回到最新”和展开的
  TODO 清单继续作为消息上方的动态覆盖层，不得用大块永久留白为其预留最大尺寸；
- tool trace 默认压缩，高价值 terminal/live output 可原位展开。同一工具组以及跨记录连续执行、
  中间没有正文、思考、用户输入或其他可见语义边界的工具，使用同一条连续轨道与紧凑等距间隔；
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
- `npm run build` 通过 Cloudflare Worker-compatible Vinext build；
- 本地 route 返回非错误响应；
- 不打印、持久化或提交 `PULSARA_API_KEY`；
- social metadata 使用 Pulsara title/description 与可信 origin 下的品牌图；
- desktop、tablet、mobile CSS 均有确定性降级路径。
