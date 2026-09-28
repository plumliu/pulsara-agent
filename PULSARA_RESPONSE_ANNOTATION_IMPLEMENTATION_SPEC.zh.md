# 回复选区与批注实施规格

状态：**已实现并完成本轮验收**。日期：2026-09-28。

批注菜单、选区源码复制、同一草稿中的引用卡片与撤销、结构化输入、canonical 保存、compiler 投影、分页来源定位和 fork 重定位已落地。已完成自动化回归、真实浏览器及 GPT-6 Luna max 验收，并由 GPT-6 Astra xhigh critic 交叉核验；具体证据及验证范围见 §11。

本规格覆盖浏览器中选取 assistant 正文、复制选区对应的原始源码、添加一条或多条引用批注、发送后由模型回应。AGENTS.md 的前缀连续性、精简持久化和 hard-cut 规则继续适用。

## 1. 已确定的产品边界

1. 用户在已提交的 assistant 自然语言正文中拖动选取，松开后显示“添加注释”。支持 commentary 和最终回复；不要求该消息完全没有工具调用，合法区域是其中的自然语言正文。
2. 一次选区必须完全位于 **Pulsara 展示的一条独立自然语言消息** 内，以既有标准消息模型的 canonical assistant entry 为来源。跨两个 commentary、跨 commentary 与最终回复，均不显示菜单。同一消息内跨段落、列表、Markdown 元素或底层 TEXT block 允许。供应商、API、流式 chunk、content part 和存储 block 的切分均不改变这一规则。
3. 工具调用卡、工具结果、终端输出、思考过程、子任务控制区、消息标题/时间/操作栏、用户消息、输入框和弹窗不是批注正文。触及这些区域的选区不显示菜单，不自动截取合法部分。
4. assistant 正文中的 Markdown 图片、Mermaid、直接 SVG 和公式，属于该自然语言消息的渲染结果，可以整体引用它们对应的源文本。正文图形的“源码/复制/放大”等控件是 UI 装饰，不进入引用内容，也不阻断同一消息内从图前到图后的连续选择。独立 `visualization_render` HTML 展示不是正文，不能引用为正文源码。
5. `visualization_render` 外层卡片及 iframe 中的普通内容不可选中文本，不提供批注菜单。按钮、悬停、图表拖动、滚动和表单操作仍可用；iframe 自身输入控件内的编辑选区可保留，但永不进入对话批注。不可通过禁用整个 iframe 的指针事件实现。
6. 图片引用只向模型提供原始 Markdown 引用，不自动读取文件、不自动附加像素、不启动 `view_image`。模型可利用已有图片上下文，或按任务需要使用现有工具读取。
7. 引用位置用于前端回跳与高亮；模型只收到引用编号、源文本快照、可选批注和本次正文。
8. 不支持跨独立消息拼接引用或跨会话选取。用户可以分别选取多条消息中的片段，形成多条批注。本轮不实现 HTML 产物批注、图内局部区域标注或 OCR。
9. 对成功映射的合法正文选区，Command+C / Ctrl+C 及浏览器菜单复制，均复制与批注相同的原始源码。屏幕仍显示渲染结果；不要求浏览器原生 `Selection.toString()` 自动返回源码。

## 2. 当前代码真源与职责

| 层 | 当前文件/事实 | 实施职责 |
|---|---|---|
| canonical assistant 正文 | `assistant_message_blocks` 持有 `assistant_entry_id`、block ID、ordinal、TEXT 内容 | 从既有 entry 的正文内容确定性构造消息级原文 |
| 前端消息投影 | `frontend/lib/runtime-adapter.ts` 当前将 TEXT 内容用两个换行拼成 `body` | 保留既有一条消息的选择区域；与服务端使用相同的消息正文投影规则 |
| Markdown 渲染与复制 | `frontend/components/markdown-body.tsx` 使用 ReactMarkdown/remark/rehype；AST 位置不包含完整字符映射，数学预处理会改变源码长度 | 复用 micromark token 与 AST，在现有管线补齐到消息级原文的映射；批注和 copy 共用结果 |
| 图形渲染 | `diagram-block.tsx` 已持有 Mermaid/SVG source；公式已有原始 LaTeX；图形工具栏位于正文 DOM 内 | 区分源码原子节点与 UI 装饰，引用完整原文而非渲染器处理后的 source |
| 草稿与提交 | `prompt-draft.ts`、`prompt-content.ts`、`prompt-composer.tsx`；现有历史来自 Tiptap 文档事务 | 批注进入同一草稿文档和撤销历史，卡片是该状态的展示 |
| browser/协议 | `web_app/browser_bridge.py`、`terminal_protocol/schema/terminal_kernel_v3.proto`、`terminal_protocol/v3_gateway.py` | 传输 typed annotation，拒绝形状非法的输入 |
| prompt canonical codec | `llm/input.py`、`conversation_kernel/prompt_content.py` | 在有序 parts 中增加批注类型，保持用户原文不变 |
| admission/保存 | `conversation_kernel/host.py`、`_repository/prompts.py` 等既有提交 owner | 校验来源和范围，批注与同一用户提交原子保存 |
| 历史读取和编译 | `conversation_kernel/reader.py`、`model_input/contracts.py`、`model_input/lowering.py`、`model_input/compiler.py` | 传递 typed 批注并确定性生成模型可见文本 |
| 输入冻结与图片 | `conversation_kernel/image_validation.py` 当前按 text/image 分流；无图片路径仅保留 text | 明确透传/冻结 annotation，计入文本资源，不误走图片分支或丢失批注 |
| 主动意图 | `llm/input.py` 的 `prompt_text_projection`，Host Hook 与 `provider_dispatch.py` 的技能/记忆入口 | 正文与 comment 参与主动意图判断，quote/source 排除 |
| 压缩与分叉 | `model_input/lowering.py` 的 retained request 入口；`_repository/fork.py` 的事务内 `entry_map` 与正文复制 | 复用批注投影；将继承批注的来源重定位写入子会话 canonical 副本 |
| 历史定位 | `runtime-adapter.ts` 的 `backfillOlderHistory()` 当前循环读尽历史页 | 在既有历史读取 owner 中增加逐页定位、找到即停的用途，不将全量回填当作定位 API |

依赖边界：浏览器负责 Selection/Range、几何、输入与 copy 事件；ReactMarkdown/remark/rehype 及其 micromark 解析体系负责 Markdown 语法；现有浮层依赖 `@floating-ui/dom` 负责定位；Tiptap/ProseMirror 负责草稿事务与撤销历史。Pulsara 负责选择资格、源文本归属、批注语义和 canonical/compiler 映射。不另写 Markdown parser、通用撤销系统、全局选区框架或独立批注数据库。

### 2.1 统一的消息边界

批注消费 Pulsara 既有标准消息模型：一条独立 commentary 或最终回复对应一个正文选择区域。API 输出如何归一化为这一消息模型由现有输出 owner 负责，批注功能不修改输出归并规则，也不通过供应商名、wire API、响应 content part 或 TEXT block 数量决定菜单行为。

单条独立消息是批注的产品单位。同一个 run / agent loop 中，自然语言消息可能分布在多次思考和工具操作之间，表达不同探索阶段的判断、计划或结论。批注应保留用户所回应的具体表达及其来源，不因它们属于同一 run 就合并引用。这个边界不取决于两条消息之间是否实际存在工具或思考内容，也不要求判定两段文字是否讨论同一件事。需要回应多个阶段时，用户分别添加多条批注，并可在同一次提交中发送。

同一消息内的多个底层 TEXT block 只是原文存储结构，不产生额外选择边界；同一消息中任意段落换行也不产生边界。两个独立消息即使视觉相邻，也不能被一次批注跨越。不得为了方便保存 offset 而人为拆分正文或改变消息布局。

## 3. 选区规则与源码映射

### 3.1 合法性判定

每个可批注区域有明确的 `(session_id, entry_id)`，覆盖该条消息的自然语言正文。只有已 canonical 提交并完成正文渲染的消息可创建批注；生成中的草稿不具有稳定锚点，不弹菜单。

在 pointerup、键盘选择完成及 selectionchange 后检查当前 Selection。菜单只在恰好一个非折叠 Range、具有可引用内容、来源稳定且**整个 Range 归属于同一条消息的正文选择区域，没有跨越硬排除区域** 时出现。多 Range 选区首版不支持，不擅自选择其中一个。是否有可引用内容按映射后的源片段判定；仅包含图片的非折叠 Range 也可能合法，不能因 `Selection.toString()` 为空就拒绝。

不能只检查 anchor/focus 节点：选区中间也可能包含禁止区域。使用 DOM Range 与明确标记的来源、UI 装饰和硬排除区域进行完整覆盖判定；反向拖动按 Range 文档顺序归一化。边界碰触但不实际包含节点内容，不算选中该节点。

区域分类固定如下：

- **正文来源**：普通 Markdown 内容及绑定完整源码范围的图片/图形/公式原子节点。
- **正文内 UI 装饰**：明确标记的图形标题、源码/复制/放大/重试按钮、加载状态等前端生成控件。它们没有 canonical 源码，不进入提取结果；完整选区经过它们不构成拒绝理由。只选到这些控件没有可引用来源，不显示菜单。不能按 `button` 标签统一排除：正文图片引用标签当前本身就是按钮。
- **硬排除区域**：其他独立消息、工具调用/结果、终端、思考、独立 HTML 可视化、消息标题/时间/操作栏、用户输入与弹窗等第 1 节禁止区域。无论 `user-select:none` 或 `Selection.toString()` 是否省略它们，跨越时都拒绝整条选区，不拼接剩余正文。

只允许忽略明确标记的正文 UI 装饰，不能把无法映射的正文或实际工具输出冒充装饰。提取结果必须仍对应同一消息中的连续原文范围。

### 3.2 原文坐标

- 锚点使用一个 `source`：`entry_id`、`start`、`end`。无需给批注持久化 `block_id` 或一组随 API 分片变化的范围。
- 坐标基于该 entry 的**消息级原始 Markdown 正文**，采用 **UTF-16 code unit**，左闭右开 `[start,end)`。正文由 canonical rows 确定性投影，不基于数据库行 JSON/UTF-8 字节、DOM child index、屏幕坐标或 provider 上下文位置。
- 固定并复用当前正文投影规则：该 entry 的 TEXT blocks 按 `block_ordinal` 排序，解码原值，排除空字符串，以 `\n\n` 连接；工具/数据/思考内容不混入正文。前端 `message.body` 与 admission 使用同一语义，必须以跨语言 fixtures 验证一致。没有正文时不可批注，不引用消息 manifest。分隔换行属于这份消息级原文，跨底层 block 的选区可正常覆盖它。
- 这份正文是现有 canonical 内容的纯派生值，不另存一份消息正文、不建新的投影 authority。源码映射可临时追踪 block 到消息正文的区间，但选择合法性不依赖该区间的切分。
- UTF-16 与 JS 字符串/Markdown AST offset 对齐；Python 校验须显式换算，禁止直接拿 Python code-point 下标切片。拒绝切开代理对的端点；复杂字形选择以浏览器实际给出的字符边界为准。
- 渲染前的数学规范化当前会插入换行。实施时让该现有转换携带到原始输入的区间映射，或在 AST 中完成等价规范化以保留原始位置；不能直接使用规范化字符串的 offset。
- AST 节点的 `position` 只确定整段范围，不能直接与 DOM 字符下标相加。例如源码 `A &amp; B \* C` 显示为 `A & B * C`，`B` 的显示下标是 4，源码下标是 8。字符级映射按 §3.4 实现，不能用 `indexOf(选中文字)` 查找源位置。
- 当显示内容没有唯一、连续的原文对应时隐藏菜单，不能凭猜测保存偏移量。已承诺的实体、转义、代码空白和数学转换必须实现映射，不得全部归入“不支持”。
- 源码映射是当前渲染的可丢失派生值，不持久化映射表、代码哈希、DOM 路径或渲染版本注册表。

### 3.3 原子渲染节点

| 正文中的显示 | 引用内容 |
|---|---|
| 图片/本地图片引用标签 | 原始 Markdown 图片节点语法，保留原始路径/说明/title/转义 |
| Mermaid 图形 | 原始完整 Mermaid 代码块 |
| SVG 图形 | 原始完整 SVG 代码块或受支持的独立 raw SVG 源片段 |
| 数学公式 | 原始 LaTeX 及其原始定界符 |
| 普通代码、链接、格式化文字 | 选区对应的原始文本片段 |

选区实际包含原子节点时，将其扩展到该原子节点完整源码范围，并在引用卡片中明确显示为整体引用，不承诺“图内选中文字”或像素局部定位。SVG/Mermaid 的完整源码包括原有代码围栏、语言标记及原有空白，不能仅使用渲染器已经截取或规范化的字符串。图形源码/预览切换及绘制失败不改变这个源码归属。

真实图片禁用浏览器原生拖图。若拖动始终停留在同一张正文图片中且浏览器未建立文本选区，前端为整张图片建立原生 Range，再交给同一来源校验和复制入口；跨节点拖选沿用浏览器端点。该图片选区手势之后的 click 不打开图片或其外层链接，普通点击仍保留预览/链接行为。

节点内工具栏按 §3.1 的 UI 装饰处理，可设置 `user-select:none`，但提取不能依赖该 CSS 恰好让浏览器省略文字。允许图前段落到图后段落的正反向选择；工具栏不进入 quote/剪贴板，图形对应的完整原文仍须保留。无需仅为回避工具栏而改变消息布局或把图形另拆成一条消息。

普通围栏代码显示为代码卡片时，语言标题、行号和换行/复制/展开控件也属于 UI 装饰。语法高亮只拆分显示文本，每段仍保留到原始正文的字符映射；切换按容器宽度换行只改变布局，不改引用与复制内容。原始每行有一个行号，折出的续行在行号栏留空，下一原始行才显示下一个编号。

保持整体在同一条自然语言消息中，并截取其消息级原文的连续源码范围；其间的图片/图形不得被静默跳过。Markdown 引用式图片如果路径定义位于所选片段外，也只携带原始节点语法，不伪造内联链接、不另行读取图片；需要更多上下文时沿用现有工具。原始语法不能从解析后的 `src` 或 Blob URL 反拼。

### 3.4 字符边界映射

沿用现有解析体系：已安装 micromark 公开导出 `parse/preprocess/postprocess`，可获得实体、转义等 token 的准确源码区间。Pulsara 在这些 token 和 AST 位置上做窄适配，保存当前渲染所需的字符边界映射；不复制语法状态机。若生产代码直接导入 micromark 或相关扩展，应显式声明直接依赖，并与现有 remark/GFM/math 管线使用一致的扩展与选项。

映射顺序是：**canonical 消息正文 → 数学等必要预处理 → Markdown token/AST → 实际渲染的文本/原子节点**。每个会改写文字的步骤都必须保留或组合回原始正文的映射：

1. 普通文本保留逐段线性对应；实体、转义使用解析器 token 的完整源区间。一个 token 解码为多个字符、端点无法在其源码中再分时，引用覆盖整个 token，不截断实体或转义。
2. 行内代码的裁边空格、换行变空格，代码块的缩进/换行处理，以及普通文本的换行规范化必须按实际渲染结果映射。强调、链接等保留子文本位置，不把 UI 属性值当原文。
3. 图片/图形/公式按 §3.3 作为整体原子映射；数学预处理插入的字符不能直接成为 canonical offset。
4. 按选区的文档顺序检查源码区间单调且能还原一个连续源片段；语法定界符和源码空白可包含在该片段中。脚注重排、GFM 生成文字或其他没有连续对应的情况，整条选区拒绝。不能简单取所有 offset 的最小值/最大值，把未选中的无关正文夹带进来。
5. 不改变 Markdown 的合法 DOM 结构。表格结构节点间的生成空白保留为文本，由现有 `hast-util-to-jsx-runtime` 移除；不可包成装饰 `span`，否则会生成匿名表格布局并使表头/数据列错位。单元格正文照常建立源码映射，跨单元格/行引用保留原始管道符、分隔行和换行。

添加批注、选区复制、来源回跳高亮共用这份映射语义。用具体 `start/end/quote` 验证转换链，不以“菜单出现了”代替准确性验证。

### 3.5 复制原始源码

在正文的真实用户 `copy` 事件中，对成功映射的合法选区，将原文片段写入 `event.clipboardData` 的 `text/plain`，再 `preventDefault()` 替换默认复制结果。Command+C、Ctrl+C 和浏览器菜单复制复用此入口，不只监听某个快捷键；不靠异步读文件或请求后端临时重建源码。浏览器支持这一事件机制，见 [MDN copy 事件](https://developer.mozilla.org/en-US/docs/Web/API/Element/copy_event)。

复制与添加批注使用同一个来源校验和源码截取结果。图片复制其 Markdown 引用，SVG/Mermaid 复制完整原始代码块；不复制图片像素、Blob URL、渲染 HTML、生成的 SVG 或工具栏文字。不额外写入渲染后的 `text/html` 来让富文本粘贴绕过源码语义。

只接管已成功映射的正文选区；跨消息、硬排除区域、无法映射内容，以及输入控件/批注编辑器/弹窗/iframe 自身的复制仍使用各自原有复制行为，不承诺源码复制。失败时不覆盖剪贴板为空值或猜测文本，也不伪造“已复制源码”的反馈。正文 UI 工具栏的独立按钮行为继续由各自控件负责。

## 4. 交互与草稿

- 浮层提供一个“添加注释”动作，沿用 Pulsara 风格；复用 Floating UI 的 offset/flip/shift，把菜单放在选区旁并限制在当前会话视口内。
- 点击菜单前保存已校验的选区快照，避免按钮获取焦点后 Selection 丢失；增加批注时再核对仍是同一会话/独立消息。
- 点击空白、Escape、折叠来源区域、切换会话或选择变成非法时收起菜单；滚动/resize 时重新定位，不保留悬空菜单。
- 点击后在输入框内添加一条紧凑批注条目，同时在原文旁打开悬浮编辑框并聚焦评论。多条条目逐条横向排列，空间不足时换行；每条独立显示编号、评论或引用摘要及移除按钮，不汇总为堆叠卡片。
- 当前草稿的每条批注在来源正文右侧显示棕金色编号标记，与输入框条目编号一致。悬停或键盘聚焦标记时以浅琥珀色高亮对应原文；点击标记或草稿条目再次打开编辑框。编辑期间持续高亮，悬停另一条批注不清除当前编辑高亮。重复/相邻选区的标记错开，避免互相遮挡；滚动到来源视口之外时隐藏标记。明暗主题分别提供棕金前景与编号文字色，沿用 Pulsara 的暖纸色视觉体系。
- 原文旁的悬浮编辑框只包含评论输入和删除图标，不重复显示引用或标题。输入框条目打开的预览则保留可展开的原始引用、评论、删除与关闭按钮，两者分别布局。均删除定位、完成按钮和快捷键说明；评论随输入实时保存到同一草稿节点，Enter 使用文本框原生换行，点击外部或 Escape 收起后保留内容。弹窗沿用 Floating UI 的碰撞与视口定位规则。
- 预览标题显示 `Annotation N`；引用折叠时最多显示四行（当前字号下为 72px），右侧“更多”可展开完整引用，展开后可“收起”。评论分界线上下使用相同的 6px 留白。
- 动效使用短促的一次性淡入、轻微位移和缩放，配合编号悬停反馈；条目增删、撤销或内容变化时，存留条目平滑移动到新的布局位置。复用 CSS 与浏览器 Web Animations，不延迟编辑器事务或权限撤销，不让滚动中的标记拖尾；系统开启减少动态效果时停用这些动效。
- 发送后的历史引用沿用只读展示和来源回跳，不通过这些草稿控件修改已提交用户消息。切换会话、删除对应批注或成功清空草稿后，相关编辑框和高亮立即撤销。
- 批注编辑入口遵循当前会话控制权：旁观模式、没有控制权或连接不可用时不能添加、修改、删除批注。旁观模式可选择/复制正文，查看已保存引用及回跳来源。窗口失去控制权时立即关闭添加菜单、草稿标记和编辑浮层、清除相关高亮，保留草稿内容；重新取得控制权后由新的用户动作打开编辑，不恢复残留浮层。
- 引用卡片只按数据类型进行纯文本/受控摘要展示，不执行被引用的 HTML/SVG/script，也不因图片引用自动加载本地文件。
- 多次分别选择可产生多条批注，按加入顺序排列；正文仍由现有编辑器管理。提交固定为按加入顺序排列的 annotation parts，随后是原编辑正文的 text/image parts；正文内部图片与文字的相对顺序不变。compiler 按本次消息内的批注顺序从 1 编号，不维护全会话计数器。
- 允许仅引用、无批注的提交，模型结合本次正文解释其意图；允许只发送一条带批注的引用。引用源码有内容即可满足现有非空提交判断。
- 草稿、撤销、发送快照、失败保留、排队修改/删除/发送、会话切换全部沿用现有 owner，具体事务归属见 §4.1。批注修改推进现有 draft revision；不让发送成功后的清空覆盖用户刚添加的新批注。
- 消息历史中的引用卡片用 canonical 批注重建。按来源回跳并高亮原始范围；原消息未装载时按 §4.2 逐页定位，不能为高亮全量加载会话。
- 原来源不可达时保留引用快照，显示来源不可定位，停止跳转；不触发重新执行或全局搜索相似片段。

### 4.1 草稿、撤销与有序 parts

现有撤销属于 Tiptap 文档历史，`revision` 由编辑器更新推进。批注使用同一文档中的专用节点承载，在现有单段正文前允许一组有序 annotation 节点；卡片是节点的 UI 呈现，不另维护一份独立 annotations 数组作为真源。正文仍沿用现有 paragraph 与文字/图片/引用节点。

添加、修改 comment、删除批注通过编辑器事务更新节点，进入现有 UndoRedo 历史并推进同一 revision；卡片编辑处的撤销/重做需作用于同一历史。批注属性按 typed 数据处理，quote 不作为 HTML 或技能/文件引用节点重新解析。无需另建通用撤销系统。

草稿节点带进程内 `draftId`，仅供浮层和编号定位到具体节点，避免重复引用、删除前项或撤销时编辑错位。恢复草稿时重新生成；序列化仅提取 typed `value`，这个 UI 标识不进入 canonical、协议或模型输入。条目编号从当前文档顺序派生。悬浮编辑、悬停和几何信息是临时展示状态，不形成第二份批注内容真源。高亮通过原文映射重建 Range 并使用 CSS Highlight；图片/图形补充整体轮廓，不插入正文 DOM 包装或更改表格结构。

`summary`、`capture`、`restoreIfEmpty`、`clearIfSnapshot`、队列编辑还原与 serializer 同时支持这些节点。当前按 `document.content[0]` 假设正文总是第一个节点的调用点必须改为按明确节点类型访问；不能因加入批注而漏掉图片等待、文件导入或非空判断。发送仍冻结 owner/revision/content 快照，等待图片或后端期间发生的批注增删改不得被旧快照成功后的清空覆盖。

序列化和还原固定使用上述 annotation-first 的顺序，恢复与队列往返不得丢失 quote/comment/source，不把 quote 中的 `$skill`、`@path` 当作可执行的编辑器引用。

### 4.2 来源回跳与历史分页

当前 `backfillOlderHistory()` 会循环读取全部旧页，不能直接用它来履行“定位到来源即停止”的合同。引用回跳先查当前会话已经装载的 entry；未命中时，在既有历史读取 owner 内复用 wire 分页和合并逻辑，按需逐页查找，找到目标后仅加载定位所需正文并停止该定位操作。

该用途与原有全量回填目的明确区分，不改写普通会话加载策略，不并行建立另一份历史状态或游标真源。会话切换、用户取消或新定位替换旧定位时，停止旧操作且不得跳到过期目标。读取失败保留引用卡片并提供重试反馈；页面耗尽仍无目标时显示不可定位。保持现有单页资源边界，不增加总页数或总历史 cap，不为一次定位重新执行模型。

## 5. Typed 输入、校验与 canonical 保存

新增 `annotation` part。以下示例假定所引用消息的正文恰好是该段原文：

```json
{
  "type": "annotation",
  "quote": "重新投影上下文不会清除这些历史。",
  "comment": "这里能不能解释得更详细？",
  "source": {
    "entry_id": "entry:…",
    "start": 0,
    "end": 16
  }
}
```

`comment` 缺省表示无批注；`quote` 是源文本快照，允许 Markdown 语法。新建批注必须提供非空 `source`。消息所属 session 是外层已有 authority，不再给每条 annotation 添加可任意指定的来源 session。canonical 历史中的 `source` 仅在 §7 的 fork 来源未导入情形允许为 `null`，保留 quote/comment 的独立可读性；此历史表达不能用来绕过新建来源校验。

admission 在现有会话授权和 canonical 读取边界中验证：entry 属于当前会话且为已提交 assistant 消息（有正文的 ASSISTANT_MESSAGE 或 ASSISTANT_TOOL_REQUEST，含 IMPORTED_HISTORY），存在消息级原文，范围非空且合法，客户端 `quote` 与该原文的 UTF-16 区间完全一致。不要求来源仍位于当前 provider 上下文中。多来源消息、越界、将工具结果冒充来源、伪造来源或文本不匹配，拒绝本次提交并保留草稿，返回可读错误；同一消息内跨底层 TEXT block 不拒绝，也不静默降为普通用户文字。

admission 校验不交给 compiler 访问数据库；compiler 消费冻结后的 typed 值。范围只能证明来源，不能授予读取图片/文件、调用工具或提升权限。

批注与用户正文/图片一起进入现有有序 prompt `parts`，保存于原来的 canonical 用户输入/队列内容中。`quote` 保存快照是产品需求：即使原消息不在当前模型窗口或来源无法回跳，批注内容仍完整。source 锚点仅用于前端关联，不单独建外键产品关系或触发依赖式回放。

原用户 `text` 不追加隐藏 XML/JSON，不替换成渲染后的聊天文本。沿用 `pulsara.prompt/v1` 的有序 parts 外壳，将用户输入的 part union 扩展为 text/image/annotation；text 和 image 仍是当前有效类型，不另建旧格式 decoder。协议 `PromptContentPart.oneof` 使用当前空闲的 field 3 承载 annotation。codec、proto 生成代码、browser JSON、网关、队列、历史 UI、fork 相关读取在同一次实施中接受同一个新增 typed 分支，不保留前端字符串批注或其他双写/解析 fallback。数据库表结构无需为这一输入内容类型增加版本迁移。

不新增 durable 表、event kind、subject slot、append guard、job、回执、独立编号服务或 fingerprint。canonical 内容仍由既有 blob/摘要边界验证；不新增批注哈希。

### 5.1 用户输入类型与 provider 类型的边界

用户输入/冻结 canonical 用户内容与最终 provider 内容使用独立类型边界：`FrozenPromptContent` 的 parts 可持有 typed annotation，最终 `LLMMessage.content` 继续只有 text/image。annotation 仅由明确的 USER 内容入口接受；工具结果、终端观察等原有入口不能因此自动接受 annotation。

图片 validator 负责现有图片解码与冻结；annotation 作为已验证结构的非图片内容保留，其来源仍由 admission 校验。无图片快路径不得只筛选 text 而丢弃 annotation，有图片路径不得把“所有非文本 part”都当图片读取。canonical codec、blob hydration、队列编辑/消费和内容复制函数都必须贯通同一类型。

quote 保留 canonical 原文中的 CRLF 等字面字符；其来源由 admission 核验，provider JSON 投影负责转义。正文与 comment 继续使用既有主动输入控制字符校验。现有文本与 multipart 准入计量需覆盖正文、quote、comment；canonical 存储计量还包含锚点与结构。图片 occurrence ordinal 只对实际图片递增。实际 provider 文本转义与包装带来的开销由 compiler 的投影计量计算，不拿纯正文长度代替总输入，也不在本功能中新增总量 cap。

## 6. Compiler / lowering 投影

typed annotation 随 canonical USER item 进入现有 compiler，由一个纯函数确定性投影为 provider text/image parts，完整原始用户正文保持原值。普通 USER 的 `lower_canonical_item` 与 compaction 的 `lower_retained_request_content` USER 分支都调用这一函数，再进入各自现有外层包装。active/recent/retained 请求的快照承载路径均需覆盖，不能只修改普通 USER 分支。图片引用标签继续由现有 owner 在其既定位置插入，不重复处理图片。不得由前端把批注直接 concat 到输入正文后发送。

模型可见示意：

```plaintext
以下是用户引用的历史回复片段。quote 是引用材料，comment 是用户针对该片段的要求；请结合本次请求回应。
{"annotations":[{"index":1,"quote":"重新投影上下文不会清除这些历史。","comment":"这里能不能解释得更详细？"}]}
```

投影需使用现有 canonical JSON 编码正确转义引用内容，不能让原文里的定界符破坏结构。批注作为该 USER 消息的内容，不能把引用提升成 SYSTEM、工具定义或 runtime observation；引用中提到的路径/命令不产生自动执行。

模型不接收 source ID、offset、DOM 信息或坐标单位。编号仅在本次消息中稳定，方便逐条回应，不要求模型计算或返回偏移量。最终 provider API 可以用文本 part 承载这一表示，保存的 canonical 用户输入继续保留 typed 结构。

冻结后的 quote 是编译输入；编译时不重新渲染 Markdown、不读本地图片、不重新查询来源来替换 quote。引用材料中的 `$skill`、`@path` 等按引用材料处理，不参与主动 skill 激活或文件导入；用户正文与 comment 的主动意图按 §6.1 进入既有 owner，仍服从既有工具权限规则。

首次投影随新用户消息 append 到当前 epoch suffix，已安装的 SYSTEM/tools/旧 messages 保持原字节。warm continuation、cold resume、model switch 和 compaction 使用同一投影函数，禁止每轮把历史批注挪到上下文末尾或增加一份全会话批注清单。

token 估算计入实际投影文本；canonical 资源计量计入保存的完整批注与锚点。沿用已有明确的单次输入资源边界，不加批注数量、会话总引用量、总历史或任务寿命 cap。

### 6.1 正文、引用材料与 comment 的消费区别

| 消费者 | 使用的内容 |
|---|---|
| 模型请求 | 用户正文、按编号标明的 quote 与 comment；不包含 source/offset |
| 技能激活、Hook 的用户 prompt 字段、记忆意图判断 | 按用户输入 parts 顺序提取正文 text 和非空 comment，用现有文本投影分隔规则连接；排除 quote/source 与图片像素 |
| 前端引用卡片和来源回跳 | quote/comment/source；无法定位来源时仍保留引用内容 |

在 `llm/input.py` 现有 `prompt_text_projection` 的用户输入语义中落实主动意图规则，并核对所有调用方；不要直接把 compiler 的模型可见引用包装交给技能扫描或记忆分类。NEW_TURN 的 Host/Hook/准备路径与 STEER 的消费/激活路径使用相同规则。

例如 `$某技能` 或“不使用记忆”仅出现在 quote 时是历史引用；出现在 comment 或当前正文时，按既有显式用户请求处理。这不改变 canonical 正文原值，不新增权限、自动文件导入机制或 SYSTEM/tools 重建边界；技能、记忆和 Hook 的后续行为继续由既有 owner 决定。

## 7. 恢复、分叉与原文高亮

### 7.1 重新载入、compact、冷启动后创建新批注

| 用户操作后再拖选旧回复 | 应保证的行为 |
|---|---|
| 重新载入当前会话运行时 | canonical entry 和正文不因运行时重建而改变；前端重新建立映射即可创建批注 |
| compact 后继续，或 compact 后冷启动 | 从持久历史中的原始消息取源，不要求它仍在模型当前窗口内；新 quote/comment 随本次用户消息进入当前上下文 |
| fork 后选择已导入的历史消息 | 直接使用当前子会话 entry ID 和其正文坐标创建新批注，不引用父会话身份 |

目前 compaction 保存快照并更新上下文绑定，不将原始 transcript rows 改写为摘要。模型上下文的取舍与历史正文的可引用性分别处理；为了回应新批注，无需恢复整段旧上下文或重写已安装的消息前缀。新提交携带完整 quote，即使模型当前只持有早期内容的摘要，也能读到用户本次引用的原文。

fork 可能基于“压缩快照＋后续历史”建立子会话。只允许选择当前会话实际导入并展示的原始消息；未导入的父会话原文不能借摘要伪造为可定位来源，也不为批注强迫导入额外历史。

### 7.2 fork 继承已经保存的批注

这与 fork 后重新选取文字是两条不同路径。`_repository/fork.py` 复用事务内的 `entry_map`，将批注来源重定位写进子会话的 canonical 内容，不仅保存在临时 UI 映射中。

在既有 fork 事务中，解析被复制的用户输入及有关 snapshot carrier：

1. 来源 entry 一起导入时，用 `entry_map` 替换 annotation.source.entry_id；正文未变化，start/end/quote/comment 保持原值。
2. 来源没有导入或原本已不可定位时，将新副本的 source 置为 `null`，保留 quote/comment。前端显示“来源不可定位”，不持有可跨会话读取的父 ID，不增加单独状态表。
3. 使用现有 canonical codec/blob publisher 重新发布被修改的输入，更新其现有 content columns 和图片引用；inline/blob 及保留请求的快照内容都覆盖。没有批注或无需改写的内容继续由既有复制路径处理。

原会话内容和前缀不变，子会话刷新或再次 fork 后仍能恢复同一语义。模型投影不包含锚点，因此不因 ID 重定位或 source 置空而改变引用文本。

### 7.3 已有批注的读取与高亮

- 普通恢复、历史 hydration、队列消费从已保存的 typed 值恢复批注；不依赖旧 Selection、DOM 对象或旧进程缓存。历史读取不重新执行“新建批注”的来源 admission，不能因 fork 后 source 为空而丢弃 quote。
- compaction 对已经存在的历史批注遵循现有内容取舍，不为了批注强留整个原回复或所有历史请求。被保留请求中的 quote/comment 仍经同一投影处理；本次新批注始终自包含。
- 高亮通过消息级原文范围映射到当前 DOM，按 §4.2 装载来源。字体、折行、屏幕宽度变化不得改变坐标；无法准确重建高亮时只定位到来源消息，不高亮另一个相似文本。source 为空或来源确实不可达时保留快照并停止跳转。

## 8. 实施顺序

1. 统一前后端的消息级原文投影，以既有 canonical entry 标识独立消息；补齐 token/AST 字符映射、原子节点和 UI 装饰分类，先证明批注与复制能得到准确原文。
2. 定义用户输入 typed annotation 与唯一 codec/protocol 分支，贯通来源 admission、图片冻结、canonical hydration、queue 和 fork 重定位；保持 provider 内容类型独立。
3. 普通 USER 与 compaction 的保留请求调用同一模型投影；正文/comment 的主动意图贯通 NEW_TURN/STEER、技能、Hook 和记忆入口；完成计量和前缀连续性回归。
4. 接入选区菜单、Command+C/Ctrl+C 源码复制、同一草稿文档中的卡片与撤销、发送历史、按需分页回跳和高亮。
5. 完成下列测试与真实浏览器验收；只有完整链路通过后，才宣称批注功能已实现。

HTML 可视化不可选中可以先独立发布：外层卡片设置 `user-select:none`，`SandboxedHtmlPreview` 的 inline HTML 来源接受显式选择开关，在实际内层文档中设置样式和选择事件约束；文件预览弹窗的默认阅读行为保持既有设置。该开关不修改 canonical HTML 文件/blob，也不改变 sandbox、CSP 或布局测量协议。

## 9. 必须覆盖的验收

### 选择与界面

- 同一消息的正向/反向、跨段、跨行、跨底层 TEXT block、键盘选择；中文、emoji、组合字符、转义、实体、重复文字、粗体/链接、表格与代码；Markdown 数学规范化后的原文坐标准确。
- 对实体和转义后面的节点内部选择、一个实体生成多个字符、行内代码裁空格/换行、代码缩进和数学前后正文，断言准确 `start/end/quote`。脚注重排、生成文字或不连续源码对应必须整体拒绝，不能用最小/最大 offset 混入其他内容。
- 图片、SVG、Mermaid、公式的整体引用来自原始源码；不得出现预览 Blob URL、按钮文字或渲染生成的 SVG 代替原始 Mermaid。
- 图片引用标签与真实图片都可整体引用；仅图片且 Range 文本为空的情况有效。SVG/Mermaid 从图前到图后正反向选择，允许经过 UI 工具栏但不带入其文字；源码/预览切换、加载及绘制失败时来源一致，原始围栏和空白不丢失。
- 两个独立 commentary、commentary + 最终回复：无菜单。同一消息的正文由一个或多个底层 TEXT block 构成时行为一致，合法选区均出现菜单。中间存在工具/思考/HTML 区域，即使选区字符串省略它，也无菜单。
- 工具调用和结果、终端、输入框、用户消息、HTML iframe、消息操作栏、流式草稿：无菜单。只选正文图形工具栏没有可引用来源，也无菜单；不得据此拒绝包含该图形正文的合法连续选区。
- Command+C、Ctrl+C、浏览器菜单复制同一合法选区均获得与批注完全相同的原始源码；只写源码纯文本，不出现渲染 HTML、按钮文字或图片像素。输入框、批注编辑器、弹窗和 iframe 内复制保持原行为；非法/无法映射选区不覆盖剪贴板为猜测或空值。
- “添加注释”点击不丢选区；合法选区变非法立即收起；滚动、窄屏、Escape、会话切换无残留。
- 多批注、无 comment、只有引用的消息；添加/修改/删除批注后的撤销与重做，正文编辑与批注编辑共享历史；失败保留、重复点击、草稿新 revision、排队编辑/发送、恢复。检查 annotation-first 与正文 text/image 顺序往返不变，等待图片或发送期间新增/修改批注不会被旧快照清空。
- 来源已装载时不请求额外历史；未装载时定位到目标即停止该定位操作，不由定位继续请求其余页。覆盖会话切换取消、被新定位替换、读取失败重试、页面耗尽以及 source 为空的反馈。
- HTML 卡片普通文字不能选中，内部按钮/图表/输入控件仍可操作；文件 HTML 弹窗保持独立阅读语义。

### 协议、保存与模型输入

- proto/JSON/typed canonical 全链路 roundtrip；非法 source/跨独立消息/UTF-16 边界/quote 不一致按合同拒绝，不弱化为成功。对单个与多个 TEXT block 组成的消息，前后端正文投影、范围校验和高亮一致。
- 冻结 quote/comment 原样保存，普通 text 不被拼接污染；批注和图片混排正确，图片 occurrence ordinal 不受批注计数影响。
- 仅批注输入、无图片快路径、批注加图片冻结、inline/blob hydration、队列消费都不丢 annotation；非 USER 入口不接受新增 part。文本与 multipart 计量包含 quote/comment，provider 计量包含实际转义和包装。
- provider 捕获只包含编号/quote/comment，无锚点字段；无自动图片像素、文件读取或额外 provider call。
- 同一 `$skill` 或记忆指令仅在 quote 中出现时不触发主动意图，在 comment 或当前正文中出现时按既有规则处理。覆盖 NEW_TURN 与 STEER 的技能、Hook 和记忆入口，canonical 原文保持不变。
- warm append 前缀保持不变；cold resume 和 model switch 投影一致；compaction 的 active/recent/retained 请求复用批注投影且无锚点泄漏。重新载入以及 compact 后冷启动，再选择模型当前窗口外的已保存旧回复，新 quote 随新提交进入上下文，无需恢复整段历史。
- fork 后新建批注使用子会话 entry；继承批注的来源在 canonical 副本中正确重定位，刷新及连续两次 fork 后仍可回跳。覆盖 inline/blob 用户正文及有关 snapshot carrier；未导入来源变为 source:null，quote/comment 保留且不触发跨会话读取。历史读取允许此状态，新建来源 admission 不因此放松；模型投影不因来源重定位变化。
- 使用已配置的 GPT-6 Luna 做适量真实验收，要求它分别回应两条不同批注，检查发送请求和回复；不靠“模型说看到了”代替请求证据，不修改生产设置。

DOM 测试证明选区归属与源码映射；至少在真实桌面浏览器验证拖动、选区菜单、原子图片选择、Command+C/Ctrl+C/菜单复制、iframe 选择和交互。合成 copy 事件只能测试处理逻辑，不能代替真实系统剪贴板验收。测试应检查真实边界和行为，不复制实现条件来凑断言。

## 10. 设计审阅意见的落实与验证状态

| Critic 发现 | 本规格确定的处理 |
|---|---|
| P1：AST 位置不足以定位解码文本内部字符 | §3.2/§3.4 使用既有解析器 token、字符边界和预处理映射，补充具体 offset/quote 验收 |
| P1：跨正文图形会碰到工具栏，与原拒绝规则冲突 | 按用户澄清，§1/§3 区分正文 UI 装饰与硬排除内容；工具栏不进入结果、不阻断正文选区，独立 HTML 仍排除；§3.5 将源码复制复用同一映射 |
| P1：comment 中的主动要求被现有文本消费者忽略 | §6.1 明确正文＋comment 的主动意图投影，quote/source 排除，覆盖 NEW_TURN/STEER |
| P2：canonical/provider 共用类型、图片冻结与 compaction 接线不明确 | §5.1/§6 保持 provider text/image 边界，贯通 annotation 冻结与 hydration，普通 USER/retained request 共用投影 |
| P2：fork 的临时 entry_map 不能保证重启后来源正确 | §7 区分 fork 后新选区与继承旧批注，将重定位或 source:null 写入子会话 canonical 副本 |
| P2：外置卡片无法自动获得现有草稿撤销 | §4.1 用同一 Tiptap 文档节点、事务和 revision，明确序列化顺序与快照清空条件 |
| P2：现有历史回填会读尽所有页 | §4.2 在既有 owner 内明确按需来源定位，找到即停、可取消且有失败反馈 |

设计审阅意见已按上表落到生产代码。实施 critic 随后独立检查源码映射、意图投影、分页取消、fork/compaction 和浏览器交互，发现的问题均已修复并增加针对性回归，见下节。

## 11. 实施验收记录（2026-09-28）

### 11.1 代码落点与边界

- 选择与原文映射：`frontend/lib/markdown-source.ts`、`response-selection.ts`，以及 `components/markdown-body.tsx`。Micromark 负责解析，Pulsara 的适配保留数学预处理、实体/转义、代码空白、CRLF 和 UTF-16 坐标；所有被选中的实际正文必须可映射。
- 界面与草稿：`components/response-annotations.tsx`、`prompt-annotation-node.tsx`、`annotation-quote.tsx` 和 `lib/prompt-draft.ts`。引用卡片支持展开、批注、移除、来源回跳；新增卡片聚焦批注输入，修改纳入原有 Tiptap 撤销与 revision。
- 历史定位：`runtime-adapter.ts` 复用现有分页读取；新定位替换已取消的读取后可继续，不安装未完成的正文；已载入但被折叠的 commentary 通过既有来源聚焦路径展开。
- 输入与保存：`llm/input.py`、`conversation_kernel/annotations.py`、`prompt_content.py`、图片冻结、协议和既有提交/队列 owner。来源核验与保存共用原有事务；无新增持久化表、事件或任务。
- 模型与分叉：`model_input` 的 USER lowering 及 retained request 共用引用投影；主动意图只读正文/comment。fork 在原有事务内写入子会话来源，缺失来源保留快照并置空。现有 epoch 前缀合同不变。

### 11.2 自动化与真实运行

| 验证 | 本轮结果 |
|---|---|
| 前端 7 个聚焦测试文件 | 308 项通过；覆盖原文映射、草稿/历史卡片、队列、来源分页及应用接线 |
| 后端 6 个聚焦测试文件 | 203 项通过；覆盖 typed roundtrip、图片冻结、编译与前缀连续性 |
| PostgreSQL 集成、fork 与图片输入回归 | 50 项通过；覆盖 inline/blob、跨 TEXT block、NEW_TURN/STEER 队列、连续 fork 及 compact 后缺失来源 |
| 静态与构建检查 | TypeScript、修改范围内 ESLint、协议生成一致性、生产前端构建通过 |
| GPT-6 Luna max | 三次真实调用通过：生成来源、分别回应两条批注、关闭并重新载入会话后引用旧来源；捕获请求确认不暴露 source/entry ID，并验证同 epoch 的 tools 和已有 messages 前缀保持不变 |
| macOS 真实桌面浏览器 | 鼠标拖选、图片标签、纯图片、跨 SVG 源码复制、浮层窄屏定位/滚动/Escape、卡片输入及撤销、来源回跳通过；Command+C 与浏览器右键“复制”已核对系统剪贴板 |
| 独立 HTML 可视化 | 普通内容无法拖选；内部按钮和输入筛选仍可用，保留原有 sandbox/CSP |

前端文件：`response-selection.test.tsx`、`prompt-draft.test.tsx`、`markdown-body.test.tsx`、`prompt-content-view.test.tsx`、`workbench-view.test.tsx`、`runtime-adapter.test.ts`、`pulsara-app.test.tsx`。新增后端测试为 `tests/test_response_annotations.py`、`tests/test_response_annotations_postgres.py`；同时运行既有图片输入、compiler 和 prefix continuity 回归。

可重复的真实模型验收入口为 `.venv/bin/python tools/run_response_annotations_dogfood.py`。它只读有效 Pulsara home 的已保存设置，在已核实的本地 PostgreSQL 目标建立独立临时数据库，结束后删除；不更改生产连接设置或用户会话。请求与回复证据保存在 `output/playwright/annotation-provider-evidence.json`，实际凭据按既有脱敏逻辑移除。浏览器验收截图保存在同目录的 `annotation-*.png`。

### 11.3 实施 critic 的收敛结果

交叉核验后完成以下修复：普通代码块及代码到正文的选区映射、元素端点仅接触边界的误包含、CRLF 精确引用、部分 tab 缩进映射、取消后的分页读取重试、comment 参与 NEW_TURN/STEER 激活、折叠 commentary 来源回跳、菜单视口定位、新卡片输入聚焦，以及带链接图片拖选后的误点击。最后一轮针对性复核通过全部 19 项选区/草稿测试，critic 确认无剩余阻塞。

本轮原生剪贴板验收平台为 macOS Chrome；Windows/Linux 的 Ctrl+C 走相同 `copy` 事件入口，但未在这些系统上另做原生手工验收。保留既有构建的大 chunk 提示和既有依赖弃用提示，不把它们作为本功能的成功证据或新增限制。

### 11.4 表格渲染回归修复（2026-09-28）

用户反馈表头与数据列错位后，确认来源映射曾把表格结构节点间的生成换行包成 `span`，绕过渲染依赖的空白移除并破坏表格布局。现已保留这些换行的文本类型，复用依赖的处理；单元格使用正常换行规则，避免把完整数字拆行，宽表沿用现有横向滚动。

新增七列表格回归，覆盖合法表格结构、列对齐属性、跨单元格/数据行及表格前后段落的精确源码引用。选区与 Markdown 两个测试文件共 28 项通过，TypeScript、ESLint 和生产构建通过。真实会话 `c3c88c2c` 的七列表头与全部数据行几何位置、列宽已逐列核对一致；截图为 `output/playwright/annotation-table-before.png` 和 `annotation-table-after.png`。

### 11.5 草稿批注交互更新（2026-09-28）

- 草稿改为独立横向条目，配套正文编号、悬停高亮和悬浮编辑框；当前编辑高亮不被其他条目的悬停覆盖。使用棕金/琥珀配色和明暗主题独立前景色。条目增删/撤销保持同一文档历史与稳定临时身份，重复引用不会串改。
- 前端新增 `response-annotations.test.tsx`，与 §11.2 七个文件合计 314 项通过。覆盖浮层聚焦、持续高亮、重复引用编号、撤销/重做、切换/清空草稿以及失去控制权后立即禁止批注变更、保留草稿并允许正文复制。TypeScript、修改范围 ESLint、`git diff --check`、生产构建通过。
- 真实 Chrome 检查横向条目、相邻编号错位排布、编辑复开、删除和撤销；浅色和深色截图为 `annotation-warm-desktop.png`、`annotation-warm-dark.png`。500×850 窄屏中浮层完整位于视口，页面无横向溢出，截图为 `annotation-warm-narrow.png`。
- CSS 与 Web Animations 的实际浏览器检查确认浮层淡入 180ms、删除后存留条目平移 200ms；开启减少动态效果时两者停用。第二个真实标签页以旁观模式进入同一会话，拖选仍有原文选区，但没有添加菜单、草稿标记或输入框，截图为 `annotation-observer.png`。
- 此次验收未向用户会话发送消息；仅使用临时草稿，验证后清除。未改变 canonical/协议/模型投影与已验证的 provider 前缀语义。

### 11.6 批注编辑浮层精简（2026-09-28）

保留正文编号圆点。正文旁改为仅有评论输入的紧凑浮层；输入框条目打开的预览保留引用，两者分别布局。删除草稿浮层的返回定位按钮、操作说明和完成按钮；输入即更新现有草稿，点击外部或 Escape 收起，Enter 原生换行。

批注 UI、工作台、草稿三组共 82 项测试通过，覆盖两种浮层、输入自动保存、关闭/复开、撤销、控制权与草稿恢复。TypeScript、修改范围 ESLint、生产构建通过。隔离浏览器页面使用真实组件验收两种布局及评论换行/复开；390px 窄屏无浮层溢出，多行输入按实际高度展开。截图为 `output/playwright/annotation-compact-inline.png`、`annotation-compact-preview.png` 和 `annotation-compact-narrow.png`。未取得或改变用户会话的控制权，未发送消息。
