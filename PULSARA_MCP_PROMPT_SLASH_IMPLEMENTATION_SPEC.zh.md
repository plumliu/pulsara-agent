# Pulsara MCP Prompt Slash 实现规格

状态：已实现并重新构建安装，待用户视觉审阅。日期：2026-11-02。

MCP Prompt 改为用户编写任务的入口：与 Skill 共用 `/` 菜单；有参数时在输入框上方填写并自动预览，再插入草稿；没有参数时直接转换为文字。删除模型侧 `get_mcp_prompt`。最终仍由用户正常发送消息。

## 1. 用户交互

### 统一菜单

- 扩展现有 `/` 候选，混合展示有效 Skill 和可用 MCP Prompt，不增加 `/mcp` 前置菜单。
- 候选使用 slash 路径：Skill 为 `/技能名`，MCP Prompt 为 `/服务器名/提示词名`，不使用 `·` 拼接来源。每项采用单行布局，左侧图标和路径，最右侧固定显示键帽式类型标识 `Skill` 或 `MCP Prompt`；不增加灰色小字或第二行注释。
- 同时支持直接按 Prompt 名称和多级 slash 路径筛选：输入 `/` 混合显示候选；一级输入 `/build` 可按 Prompt 名称前缀匹配 `/open-components/build-component`，也可匹配同名 Skill；一级输入 `/open` 可按 Server 名称前缀匹配该 Server 的 Prompt；输入 `/open-components/` 只显示该 Server 的 Prompt，继续输入 `/open-components/build` 按 Prompt 名称前缀缩小范围。两个一级匹配入口取并集，同一个 Prompt 只展示一次。
- 无论使用哪种搜索方式，MCP Prompt 候选始终显示完整 `/服务器名/提示词名` 路径及 `MCP Prompt` 键帽；跨 Server 同名 Prompt、同名 Skill 均保留独立候选，不自动选择默认来源。中间的 `/` 不结束补全、不触发发送或取用模板；只有用户选中最终 Prompt 条目才进入参数/转换流程，不新增根据裸名称自动执行的命令解析路径。
- 类型标识必须仿照输入框 suggestion 中的 Tab 键帽（`prompt-composer.tsx` 的 `.prompt-composer__accept-hint`、`workbench.css` 对应规则）：相同的细边框、5px 圆角、浅底色、紧凑留白和轻微字距。沿用其形状，不照搬 faint 字色、11px 字号、左边距及仅聚焦时显示的规则；文字与候选同级清晰可读，所有条目始终可见，右侧对齐且不被长名称挤掉。窄菜单先省略名称/来源，悬停或焦点可查看完整名称和来源。
- 键帽只表示类型，没有独立点击行为或快捷键含义，使用普通 `span`，不使用语义表示按键的 `kbd`；辅助技术应读出类型。可以提取 Tab 键帽的共用视觉样式，不改变已有 Tab 接受 suggestion 的行为。
- 同名条目独立显示，以类型、路径区分。路径分段的匹配不区分大小写，真实名称原样保存在 target；名称内的保留分隔字符按路径段转义展示。选择始终依据条目携带的真实 target，不解析显示路径建立身份，也不要求远程名称满足 Skill 名称语法。Skill 原有名称前缀匹配继续有效。
- Skill 的插入、显式激活、发送行为保持现有语义；只共用菜单，不把 Skill 也改成文本模板。

```text
  /review-code                                      [Skill]
  /open-components/build-component                  [MCP Prompt]
```

### 有参数的 Prompt

选择后，在输入框正上方打开面板，保留原草稿和本次 `/` 选择范围。面板分为标题、参数、预览、操作四部分：

```text
┌ build-component                  open-components  × ┐
│ component（必填）                                  │
│ [                                                 ]│
│ framework                                          │
│ [                                                 ]│
│                                                    │
│ 提示词预览                                         │
│ 请填写 component                                   │
│                                                    │
│                                取消    插入输入框  │
└────────────────────────────────────────────────────┘
  原有输入框
```

- 按 Server 声明顺序显示参数名，必填项标注“必填”；输入控件使用可增长的多行文本框，支持代码和长文本。
- 参数描述确有帮助时，点击字段旁的信息按钮展开，以正常正文显示；不在每个输入框下堆放注释，不使用灰色小字。
- 预览区从打开时就存在。必填项不完整时显示“请填写 {字段名}”；齐全后自动请求真实预览，无“生成预览”“更新预览”按钮。
- 输入停止 300ms 后自动更新，合并连续键入；中文输入法组词期间不请求，组词结束后更新。该延迟仅用于减少重复网络请求。
- 若只有选填参数，打开面板即以空参数请求预览。选填项为空时省略；必填项只检查是否全为空白，实际值原样传递，不裁剪代码或用户空格。
- 加载显示“正在更新…”，失败显示具体、已清理凭据的错误和“重试”。变更参数后旧预览立即失效并被更新状态替换；仅最新参数对应的成功预览可插入。
- 预览只读，可选中、复制、滚动；用户在插入草稿后编辑。不执行返回内容中的代码、HTML、工具调用或修改指令。
- “插入输入框”只替换本次 `/` 选择范围，保留周围文字、图片、引用和批注；不自动发送。插入作为一次可撤销编辑。
- 取消、关闭或 Escape 恢复编辑焦点，草稿不变；面板内 Enter 不得触发主输入框发送。

### 无参数的 Prompt

“无参数”指参数声明列表为空，不是“所有参数都选填”。选择后立即获取内容，并在成功时用文字替换本次 `/` 范围，不打开参数面板。等待中显示简短状态；失败或取消时保留草稿，可重试。用户照常编辑、发送。

### 草稿与并发

面板只属于选中的会话及该草稿。打开期间保留会话内编辑位置，防止主输入框提交；不锁住正在运行的 agent，也不要求停止当前任务。切换会话、进入只读状态或销毁编辑器时关闭并取消请求。原选择范围已被修改、草稿被替换时，放弃自动插入，保留用户新内容。迟到响应不能写入其他草稿。

每个面板至多一个有效网络请求，参数变化合并为最新待处理值；通过现有取消能力结束失效请求。用草稿 owner/revision、捕获的 target、参数值和本地请求对象判断归属，不新增请求序号体系、fingerprint 或持久预览记录。

## 2. 必须遵循的视觉样式

**同时以 Plan 批准卡片和权限批准卡片为视觉基准，不新创一套模态弹窗风格。** 当前基准代码是 `workbench-view.tsx` 的 `InteractionCard`、`interaction-card-header.tsx`，以及 `workbench.css` 的 `.interaction-card`、`.plan-draft-body`、`.interaction-actions`。

- 面板放在 `.composer-frame` 内、编辑器上方，宽度跟随输入框，不盖住编辑器，不使用全屏遮罩；内容过长时面板内部滚动，保留标题和操作区。跟随现有输入区布局，禁止硬编码固定屏幕位置。
- 复用批准卡片的纸面背景、14px 外圆角、细边框、轻阴影、浅色分区，以及带柔和色底的标题图标。标题延用其衬线字体，字段和操作延用无衬线字体。
- 预览沿用 Plan 正文框的纸面层次、内边距与排版；主按钮沿用两类批准卡片的深色实心按钮，次按钮沿用描边样式。浅色、深色主题均使用现有 token。
- 参数标签、正文、错误、来源标识均须正常字号和清晰对比度；**不复用现有卡片的 muted 小副标题、`small` 注释和灰色说明文字**。可以复用结构和视觉 token，不能连带照搬这些小字。
- 界面只保留名称、来源、字段、预览和必要状态。按钮固定为“取消”“插入输入框”；错误时额外出现“重试”。不添加实现说明、权限免责声明或操作教程。
- 有参数面板采用具名非模态 dialog 的可访问语义；关闭后焦点回到原编辑位置。优先呈现现有必须处理的批准事项，不覆盖或替代其状态；新 Prompt 选择暂不可用，已开面板取消失效请求并关闭，草稿保留。

## 3. 预览的真实内容边界

MCP `prompts/list` 只给出名称、描述和字符串参数声明，不提供本地可替换的原始模板。因此不生成假占位预览，不用模型补写模板；预览来自实际 `prompts/get` 响应。

本次明确支持**能够无损转换为一份用户文本草稿**的结果：

- 一条或多条 `role=user`、`content.type=text` 消息；按原顺序以两个换行连接，保持各段正文原文，不翻译、不改写、不插入来源标签。
- 没有消息或连接结果全为空白时，显示“提示词内容为空”，禁止插入。
- 若存在 `assistant` 消息、图片、音频、资源链接或嵌入资源，返回“此提示词包含暂不支持的消息内容”，不插入任何部分；不能丢弃附件、忽略角色、抽取部分文字后宣称成功。无参数流程也采用相同规则。

这一支持范围是当前草稿文本转换的明确边界；不是 MCP SDK 的限制。后续扩展附件或多角色任务编辑需单独定义，不在本次增加新的消息模型。

预览展示的文字与待插入文字必须来自同一响应。大文本使用可滚动预览，不静默截断后插入；继续遵守现有 MCP 响应、Desktop 传输和用户输入资源边界，超过边界给出对应错误，不新增总字数、调用次数或会话寿命上限。最终发送沿用现有输入校验与预算判断。

## 4. 后端所有权与接口

| 层 | 本次职责 |
| --- | --- |
| MCP SDK | 继续负责协议、传输及 `list_prompts` / `get_prompt`；不复制 SDK 实现 |
| MCP supervisor / Host | 复用已连接 Server、发现快照、作用域、凭据、超时、取消及连接生命周期；新增用户主动取用入口 |
| Desktop controller / HTTP bridge | 提供当前明确会话的 Prompt 目录和预览读取，不启动模型轮次 |
| 前端草稿 | 统一候选、表单、自动预览、精确插入及撤销 |

### Prompt Target 与轻量目录

保留现有 `MCP_PROMPT` Target，供 slash 条目、预览接口及后端校验定位具体模板。例如：

```json
{"kind":"MCP_PROMPT","server_id":"open-components","name":"build-component"}
```

Target 由菜单选择传到后端，不靠显示名称反推，也不另建替代身份。模型可通过 `query_capabilities` 列出或检查同一 Target，只获取名称、描述和参数声明；不获取展开正文。Server 详情提供 Prompt 数量和列表查询入口，概览不自动展开全部模板。Target 不插入草稿；用户最终编辑并发送的普通文字才作为任务内容进入模型。

### 用户接口

新增 Desktop `list-mcp-prompts`、`preview-mcp-prompt` 两个操作，与当前 bridge 命名风格一致：

- 列表输入：`session_id`，可选 `query`、`offset`、`limit`；按现有 Desktop 分页约定返回全部可用条目，各项包含现有 `MCP_PROMPT` target、`name`、`description`、参数声明和 Server 来源显示名。目录未就绪须返回明确状态，不能冒充“没有提示词”。
- 预览输入：`session_id`、完整 target、`arguments: {string: string}`；返回 `ready + text`，或类型化的字段错误、不可用、远端失败、内容不支持等结果。UI 不依赖错误字符串判断状态。
- 接口操作指定会话的现有 ROOT MCP owner；不从“当前活动会话”重新猜目标，不另开同一 Server 的连接，不授予 SUBAGENT UI 入口，不改变信任或自动弹 OAuth 登录。
- Server 必须对该会话可见且已启用，Prompt 必须属于所用发现快照；校验必填项、未知参数和字符串值。目录刷新、断连或配置改变使目标失效时，按现有生命周期拒绝请求并刷新候选，不调用另一个同名 Prompt。
- 将现有远端 `get_prompt` 调用抽为模型和 UI 无关的底层能力，再由唯一用户入口使用；不保留依赖旧工具名的隐藏 dispatch 或伪造模型 tool call。复用现有资源调度及 cancellation settlement，用户请求不能绕过连接串行约束或挂起关闭。
- 请求只取用远端 Prompt，不执行其指令。预览不得生成对话工具结果、模型消息、permission/plan interaction 或新的持久事件；错误通过现有凭据清理边界输出。

## 5. 模型查询边界与前缀连续性

- 模型侧 MCP Server 列表中，已关联运行连接的 `status` 只显示实际连接状态，例如 `READY`；本地及插件连接详情不输出 `adopted` 占位字段或“配置采用情况未知”文案。未关联、禁用及失败情况继续保留可确认的状态与诊断。
- 从 builtin catalog、ROOT/SUBAGENT 工具集合、Code Mode 工具目录、executor/authorization 分支中移除 `get_mcp_prompt`，删除模型目录中只能指向它的 Prompt 取用项和说明。
- 模型侧 `query_capabilities` 支持 `MCP_PROMPT` 列表、元数据检查及其 Target schema；列表只含名称、描述、Target、来源和用户 slash 入口提示，检查补充参数声明。不返回正文、示例展开消息或可执行 invocation，不调用 `prompts/get`。复用现有发现快照、作用域和分页，无额外连接或目录权威。
- 保留服务端 Prompt 发现事实及用户能力页中的数量/信息，新 slash 目录直接使用 Host 的原始发现 owner；模型能力目录不再承担 UI 目录所有权。`read_mcp_resource` 和 MCP Tools 的语义不变。
- 更新 bundled MCP installer、Code Mode 指引、当前测试和对外文档，删除“模型调用 get_mcp_prompt”的现行说明；历史文档不作为本次实现 authority，不增加兼容别名或 fallback。
- 工具定义及查询 schema 的更新仅在新 cold epoch 或已明确采用的 compaction successor 生效；不为工具删除、查询恢复或 UI 获取重写已安装 epoch 的 SYSTEM、tools 或已有 messages，不新增 rebase 边界。
- `/` 选择、参数、预览和来源标签不进入模型输入。插入后的普通文字在用户发送时走现有 PromptContent → 队列/steer → 模型链路，不新增执行通道或身份包装；后续实际工具操作仍遵循当时权限。
- 不新增表、事件种类、job、持久表单、预览缓存权威、receipt 或恢复机制。身份复用现有 target 与草稿 owner；必要的新 Pulsara 对象身份只能使用共享 ID 层。

## 6. 代码落点

| 当前文件 | 需要的改动 |
| --- | --- |
| `frontend/components/prompt-suggestions.tsx` | 从 Skill 专用候选扩为带类型和完整 target 的混合候选；支持 `/Server/Prompt` 路径及中间 slash，替换完整活动路径；Skill 原语法不受影响 |
| `frontend/components/prompt-composer.tsx`、`frontend/lib/prompt-draft.ts` | 选择范围、请求取消、草稿归属和一次性文本插入；复用 Tiptap transaction / undo |
| `frontend/components/workbench-view.tsx`、`interaction-card-header.tsx`、`frontend/app/styles/workbench.css` | composer 上方面板及批准卡片视觉复用；面板独立于批准工作流 |
| `frontend/lib/runtime-adapter.ts`、Desktop `http_server.py` / `session_controller.py` | 类型化用户目录和预览接口，接到选定会话 Host |
| `conversation_kernel/host.py`、`mcp/supervisor.py`、`mcp/contracts.py` | 直接用户取用入口、发现 target 校验、SDK 调用及文本转换；按需小范围拆出已有逻辑 |
| `capability/builtin_catalog.py`、`capability/source_query.py`、`mcp/directory.py`、`tool_runtime.py`、`tool_surface.py` | 删除模型取 Prompt 正文的路径，保留轻量元数据查询，区分模型目录与用户取用 |

实现前再核对相应位置的当前代码，避免按旧行号机械修改；不修改数据库基线形状。

## 7. 验收

1. 同一个 `/` 菜单能选 Skill 和 MCP Prompt；`/build` 能直接按 Prompt 名称匹配完整路径，`/服务器名/提示词名` 也支持逐级筛选；同名候选独立、联合匹配去重。中间 slash 不关闭候选，最终选择替换完整活动范围；不出现 `·` 来源分隔。每项末尾的 `Skill` / `MCP Prompt` 标识复用现有 Tab 键帽视觉、常显、右对齐，且不是快捷键。同名、带特殊字符的远端名称能正确选中；Skill 现有行为不变。
2. 无参数 Prompt 成功后直接插入文字，失败不丢草稿；有参数 Prompt 打开即有预览区，自动更新，无生成/更新按钮。
3. 表单支持中文输入和多行代码；空必填不请求、空选填省略、正文空格保留；迟到响应、取消、切换会话、断连均不能误写草稿。
4. 插入精确保留周围内容和附件，可一次撤销；面板内键盘操作不误发消息；只有发送后的最终文字进入模型输入。
5. 多条 user 文本按顺序完整插入；assistant/多模态/嵌入资源响应明确拒绝，禁止部分成功。大文本不静默丢失。
6. 新 epoch 的 ROOT、SUBAGENT、Code Mode 均不暴露 `get_mcp_prompt`；模型侧能力查询可在原有可见作用域内列出或检查 `MCP_PROMPT` 元数据，不触发正文获取。用户接口用同一 Target 精确选取模板。预览不改 provider prefix、不调用模型、不产生工具历史。原 MCP Tools、资源读取、作用域及取消行为回归通过。
7. UI 必须与 Plan 批准、权限批准两种卡片并排人工审阅：纸面、字体、图标底色、边框、按钮、明暗主题一致；无灰色小字注释，窄窗口可操作。

用仓库 `.venv` 运行相关 pytest，用现有 Vitest 补充候选、草稿、竞态和结果转换测试，并完成 renderer build。真实协议验证使用本地 SDK fixture，覆盖有/无参数及不支持内容；Firecrawl 没有 Prompt，不能把其连接成功当作本功能通过。视觉实现后交用户审阅；不因纯视觉细节另行扩大 Playwright 验证范围。


实施验证：相关 MCP、能力目录、Desktop bridge 与 provider prefix 测试通过；本次目录及预览相关测试 137 项通过、1 项跳过；前端、TypeScript、ruff、renderer build、桌面端 138 项测试及离线 payload 检查通过。已重新安装到 `/Applications/Pulsara.app` 并启动，安装包中的查询 schema、正文工具移除、连接状态与 installer 指引已核对，视觉验收由用户继续审阅。当前配置的 Firecrawl 不提供 Prompt；Prompt 验证使用本地协议 fixture 及用户已配置的 Open Components。
