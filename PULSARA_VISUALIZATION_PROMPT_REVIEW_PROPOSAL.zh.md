# Pulsara 可视化任务提示词修订建议

状态：语义交叉审查与修订已完成；行为验证包含已知残留问题，见文末。日期：2026 年 10 月 17 日。

本提案围绕会话 c26cljcu 暴露的四类问题：展示方式选择不明确、误解截图裁切、切换格式时丢失展示对象，以及把推测说成实现事实。优先改写现有表达，用明确的选择和观察范围替换含糊、重复的句子，减少模型猜测和返工。

主代理与用户指定的 GPT‑6 Astra high subagent 已逐处交叉审查，按下列最终替换稿修订生产提示词、工具说明和截图返回文字。Ponytail 是外部插件，本次未修改其文件、配置、hooks 或注入规则；运行逻辑保持原样。原文片段用于解释修订，不作为生产兼容路径保留。

## 修订原则

每一处修订都给出修订前原文及其替换稿。替换稿取代原段落，不与原文并存，也不在 SYSTEM 末尾追加针对本次故障的规则清单。

展示路径沿用现有可视化段落；对象连续性改写现有任务连续性规则；证据边界改写现有准确性规则。截图的具体裁切含义归工具描述和工具结果，不复制到全局提示词。独立 SVG 交付、尊重用户明确请求、必要时澄清等已有通用要求继续由原来的规则承担。

长度用于审阅措辞是否膨胀，不设置运行预算或机械的字数限制。优先删掉冗余表达，为必须讲清的产品事实腾出空间；不能为了缩短而丢掉已有语义。

## 会话证据与解释范围

目标会话为 `session:c26cljcub5zpnawa3bvt7dnsjq`，工作区为 `/Users/plumliu/.pulsara/workspaces/quick-20261017-201221-9740ad18`。本次观察覆盖北京时间 2026 年 10 月 17 日 20:35 至 20:44 的五轮用户请求。

证据来自数据库保存的 provider replay 中的 `reasoning_content`、会话消息与工具结果，以及 `provider_call_usage`。这是接口实际返回的 reasoning 文本，不代表模型未公开的全部内部计算。按 transcript entry 标记的[思考原文导出](/tmp/pulsara-c26cljcu-reasoning.txt)是本机临时诊断文件；长期定位应使用上述完整会话 ID 和 entry 序号。

| 用户请求 | 模型调用次数 | reasoning tokens |
| --- | ---: | ---: |
| 生成并展示动态 SVG | 5 | 4,120 |
| 尝试 SVG 围栏 | 2 | 885 |
| 查看用户提供的渲染截图 | 3 | 754 |
| 改用 HTML 展示 | 11 | 2,229 |
| 纠正展示对象并展示测试卡 | 4 | 1,121 |
| 合计 | 25 | 9,109 |

模型为 `deepseek-flash`，reasoning 配置为 `high`。provider 报告总输出 18,880 tokens，其中 reasoning 约占 48.2%。这些数字描述本次会话，不能直接当成可节省预算：SVG 几何设计、真实错误修复和必要验证也消耗 reasoning。

当前代码核对用于确认规则和排除错误解释；它不是该会话每次请求的完整历史 wire 快照。本文不声称已还原当时全部 SYSTEM、tools 和插件注入内容，也不把措辞修订的收益写成已验证结果。

## 展示方式应有明确的默认选择

### 实际发生的犹豫

Entry 2 在 SVG 围栏、发布 `.svg` 文件、HTML 引用 SVG、HTML 内联 SVG之间反复比较。它问：

> Does the visualization gallery support .svg files?

同一段 reasoning 多次改变交付方案，最终先写独立 SVG 和通过 `<img>` 引用它的 HTML。引用资源在截图过程中失败后，entry 7 又重新比较展示路径，才改成 HTML 内联 SVG。

当前根提示词已经把 SVG 围栏定位为静态图示，也说明 HTML 可用于交互，但没有直接给出“动态 SVG 如何展示”的默认路线。模型因此自行推导浏览器行为和 Pulsara 的资源处理方式。重复比较中有一部分来自这个信息缺口。

### 建议表达

建议改写两个现有片段。其余发布格式、数学排版说明、绝对路径链接及权限规则保持原位。

静态图示片段原文：

```text
Use Markdown tables, fenced mermaid or svg blocks for static diagrams, and LaTeX
($...$ inline; display math between separate $$ lines). SVG and Mermaid
are displayed as images.
```

替换为：

```text
Use Markdown tables or fenced mermaid or svg blocks (rendered as images)
for static diagrams, and LaTeX ($...$ inline; display math between separate
$$ lines).
```

HTML 片段原文：

```text
For useful interaction, create .html/.htm files. For simple visuals, consider
a single HTML file. Local image paths may be absolute or relative to the file
that references them. Use inline SVG for icons such as Lucide.
```

替换为：

```text
For animation or interaction, create .html/.htm files; prefer one file for
simple visuals. Inline animated SVG and SVG icons. Local image paths may be
absolute or relative to the referencing file.
```

改动用“animation or interaction”替换含糊的“useful interaction”，重组 HTML 选择句，并把内联 SVG 的用途从图标扩展到动画。简单图示继续优先单文件，不把所有交互都限制为单文件。将“SVG and Mermaid are displayed as images”合入图示选择句，保留围栏会渲染为图片的契约；分别保留 mermaid 与 svg 标签，避免斜线写法的歧义。已有后文继续负责 `pulsara-visualization` 的发布方式，不在这里重复。

这段规定推荐交付路径，不宣称 SVG 围栏必然转成 PNG，也不宣称任意 HTML 都一定能动。现有关于隐藏状态和 reducedMotion 的动画暂停要求继续适用。用户明确要求测试 SVG 围栏时，仍按请求测试；无需为此再写一个例外条款。

“提供动态 SVG”也可能包含独立文件需求。应保留用户需要的 SVG 产物，不能因为展示走 HTML 就擅自丢掉请求的文件格式。

### 预期改善

普通动态 SVG 展示请求可以直接选定 HTML 内联 SVG，避免围绕平台能力重新比较多个方案。用户指定展示方式时，模型仍有明确的例外入口。

## 截图说明应区分浏览器视口与返回图片

### 实际发生的误判

Entry 31 看到 800×601 的截图，把根元素外的说明没有出现解释为页面被窗口裁切，随后比较多种宽高和 CSS 方案并修改布局。Entry 37 再看到 704×528 的截图时仍困惑：

> Actually the screenshot should be 1200x800 normally.

它随后才猜到截图可能裁到 `data-pulsara-visualization-root`。

当前实现确实会选择内容根元素，并在根元素可用时截取该元素；否则截取浏览器视口。显式标记的根元素外可以存在未进入截图的内容。没有显式标记时，根选择器还可能在满足条件时选择唯一的可见 `main`。

因此，1200×800 是浏览器视口配置，不是返回图片尺寸保证。现有工具说明和返回文字强调 viewport，却没有同时解释裁切，容易让模型误把尺寸差异当成页面错误。

### 建议表达

`view_image` 工具描述中的相关原文：

```text
HTML screenshots use an isolated 1200×800 browser with light
theme, reduced motion and network access blocked. They inspect the current
file without publishing it or checking every interaction; edits require
a new screenshot to review the changed content.
```

替换为：

```text
HTML screenshots use an isolated 1200×800 browser viewport (light theme,
reduced motion, network blocked), capturing the content root's bounds or the
viewport. Root captures omit content outside those bounds. They show the current
file's rendered appearance without publishing it or verifying playback or every
interaction; edits require a new screenshot to review changed content.
```

这里需要补齐现有描述缺失的裁切事实，但通过重组原段落容纳，不另加截图注意事项清单。工具支持格式、引用方式和其它限制不在这个替换片段中。

HTML 截图结果文字原文：

```text
HTML current-version screenshot; viewport 1200×800, light theme, visible,
reduced motion. Publication reads the file again.
```

替换为：

```text
Current HTML still screenshot; 1200×800 viewport, light theme, visible,
reduced motion; capture may crop to content-root bounds. Playback unverified.
Publication rereads the file.
```

返回文字只保留解读本次图片所需的短提示，完整裁切语义由工具描述承担；不重复一段尺寸解释，也不假装知道本次实际选择了哪个根。本提案不增加截图模式字段、诊断协议或新的持久化记录。

### 预期改善

模型不会仅因图片小于视口、根外说明未出现，就改写卡片宽度或删除说明。它仍需检查真实的内容溢出；裁切规则不能成为忽略布局错误的理由。

## 切换展示格式时应保持内容对象连续

### 实际发生的对象偏移

对话先生成轨道动画，随后生成小型 SVG 测试卡。用户反馈测试卡是静态图，并要求改用 HTML 展示。Entry 23 却回到较早产物：

> We already have preview.html which inlines orbit.svg.

该轮最终展示的是轨道动画，之后用户再次指出要看“刚刚给我的”那张测试卡，模型才生成对应的 HTML。

该轮共有 11 次模型调用，其中也包含 CSS 修复和截图循环。不能把 11 次调用全部归因于对象偏移，但对象选错确实导致最终交付偏离相邻上下文，并需要用户纠正。

### 建议表达

任务连续性规则原文：

```text
Treat user messages addressed to work in progress as guidance for that work.
Incorporate corrections, added constraints, and clarifications while preserving
the current goal unless the user cancels it or replaces it with another goal.
Answer a status question briefly, then continue the task.
```

替换为：

```text
Treat user messages addressed to work in progress as guidance for that work.
Incorporate corrections, added constraints, and clarifications while preserving
the current goal unless the user cancels it or replaces it with another goal.
Resolve references from text, images and context; preserve the referenced
subject when changing presentation or format.
Answer a status question briefly, then continue the task.
```

这次改写保留原段落对用户消息、当前工作、取消与换目标的限定，在同一段落内说明换格式时保持指代对象。“subject”指对象身份，允许用户继续修改该对象的文字、数据或样式；不要求冻结已有内容。没有增加独立格式转换规则或新的澄清流程。

这里的优先级是用户指向的内容，不是机械选择最近生成的文件。用户明确指向更早的图片或文件时，应沿该指向继续；截图和正文也应一起用于判断。必要时澄清仍由现有歧义处理规则承担。

### 预期改善

“把刚才这个改成 HTML”会复用那张图本身，仅改变承载方式。一般情况下无需向用户重新确认，也无需重启视觉设计。

## 对现象的确认不能替代对机制的验证

### 实际发生的推断升级

用户说 SVG 围栏显示为静态图后，entry 23 直接写道：

> So the host rasterizes SVG-to-PNG for fenced svg blocks.

该解释后来进入正式回复，被说成已经确认的产品机制。

当前前端代码走的是 SVG 解析与净化、创建 `image/svg+xml` Blob、通过 `<img>` 展示的路径，并非这里声称的 PNG 转换。动画为何未运行仍需另行排查；不能仅根据净化步骤就断定具体动画元素被删除，也不能从用户的静态截图证明播放状态。

当前根提示词已有“区分证据、推断和不确定性”的要求。建议重写其所在段落，把抽象要求变成可执行的证据边界。

### 建议表达

准确性规则原文：

```text
Use actually available tools when they are needed to act or improve accuracy.
Check that referenced files and resources exist. Never claim to have inspected,
changed, run, or verified something unless observed results support that claim.
Distinguish evidence from inference and uncertainty. For time-sensitive facts, use
an available current source; if none is available, say so.
```

替换为：

```text
Use available tools when needed to act or improve accuracy. Check that referenced
files and resources exist. Report inspections, changes, runs and verification
only as supported by observed results. Separate observations, inferences and
uncertainty; if you offer an unverified causal explanation, label it as a hypothesis.
For time-sensitive facts, use an available current source; otherwise say current
information is unavailable.
```

保留观察、推断和不确定性的区分，将其落实为条件式动作要求：如果提出未经验证的因果解释，就标为假设。它不要求主动列出原因或额外调查，也不把截图一概排除出诊断证据。具体 SVG、PNG 和动画判断保留在案例分析与验证场景中，不再分别写成全局禁止条款。是否继续调查，由现有任务完成和工具使用规则决定。

例如，该场景中可以直接说：“你这里显示为静态图，我把同一张测试卡改成 HTML 展示。”没有必要为完成格式转换额外研究整个前端，也不应为让解释显得完整而补出一个未经验证的根因。

需要诊断实现时，再读取相关代码、工具证据或执行验证。已确认的事实仍应直接陈述，不必把每句话都改成“可能”。

### 预期改善

模型可以继续交付用户需要的内容，同时避免错误机制解释进入后续上下文并影响下一轮判断。这里减少的是无依据的解释和返工，不是取消必要验证。

## 修订归属与范围

| 内容 | 建议归属 | 修订方式 |
| --- | --- | --- |
| 静态图示与动画展示的默认路径 | 根 SYSTEM 的 Communication 可视化段落 | 改写两个现有片段，重组 HTML 选择句，将图片展示说明合入图示句 |
| 截图可能裁到根元素 | `view_image` 工具描述 | 重组现有截图段落，保留环境、重截图和不发布语义 |
| 本次返回是静态截图且可能裁切 | HTML 截图现有结果文字 | 替换原有短提示，避免重复完整工具说明 |
| 换格式时保持指代对象 | 根 SYSTEM 的任务连续性段落 | 原段替换，不增加独立格式转换规则 |
| 不把显示现象等同于实现机制 | 根 SYSTEM 的准确性段落 | 原段替换，不增加 SVG 或 PNG 专项警告 |

评审时应将每个替换片段与邻近原文一起阅读：同一职责只保留一个表达位置，避免“每句都合理，整体却越来越长”。

以下只统计上述替换片段的英文空白分词数，不是完整提示词长度，也不是 provider tokens：

| 位置 | 原文词数 | 修订后词数 |
| --- | ---: | ---: |
| SYSTEM 静态图示片段 | 28 | 25 |
| SYSTEM HTML 片段 | 36 | 31 |
| SYSTEM 任务连续性段落 | 44 | 60 |
| SYSTEM 准确性段落 | 56 | 60 |
| SYSTEM 小计 | 164 | 176 |
| 工具截图说明 | 38 | 54 |
| 截图结果短提示 | 15 | 23 |
| 全部替换片段合计 | 217 | 253 |

交叉审查后保留了原稿压缩时丢掉的语义，因此最终文本有所增长，主要用于指代对象连续性和截图裁切、验证范围；没有把四个案例各扩成一个全局规则。字数变化不能证明认知负担下降，仍需看后续任务表现。

Ponytail 不变。SVG 净化策略、HTML 资源支持、截图选根算法、文件编辑 revision 契约和前端渲染路径均不在本提案的实现范围内。也不增加模型调用、工具调用、重试或任务时长上限。

SYSTEM 和 provider tools 的新文本只通过仓库已有的新 cold epoch 或已明确采用的 compaction successor 边界安装；不能在现有 epoch 中重写已安装前缀，也不为本次修订发明新的刷新边界。工具结果文字属于调用后的观察内容，修订时仍应沿现有结果路径返回，不借此重建请求根。

## 后续验证建议

本提案起因于一段真实会话，后续正确性抽查不构成修订前后的配对实验，尚不足以给出节省比例。认知负担的比较仍应以交付正确性为前提。

| 场景 | 应观察到的行为 | 需防止的退化 |
| --- | --- | --- |
| 生成并展示动态 SVG | 默认用 HTML 内联 SVG 展示，保留用户需要的 SVG 产物 | 把 HTML 默认路径解释成 SVG 文件不可交付 |
| 明确要求测试 SVG 围栏 | 按指定方式输出并如实解释观察范围 | 以默认路线为由拒绝测试 |
| 标记根内有图，根外有说明 | 正确理解裁切，不单凭截图尺寸修改布局 | 忽略根内确实存在的溢出 |
| 先后有轨道图与测试卡，再说换成 HTML | 根据正文、截图和相邻上下文保持测试卡对象 | 为复用方便回到较早的轨道图 |
| 用户明确指向更早的轨道图 | 正确回到该图 | 机械选择最近产物 |
| 用户反馈画面静止，只有单帧证据 | 处理展示请求，不编造 PNG 转换或播放验证结论 | 遇到不确定性就停止交付或展开无关调查 |
| 用户明确要求查明静止原因 | 调查机制并给出证据支持的解释 | 用“无法从截图判断”代替实际诊断 |

使用同一模型配置和同一组场景比较修订前后表现；Ponytail 配置及其它输入尽量保持一致。每个样本通过合法的新输入 epoch 安装对应版本。真实 provider 验证沿用只读加载的用户生产配置，不修改或导出凭据；不复用原会话做破坏性实验。

复用现有 dogfood 路径记录交付对象、工具调用和 provider usage，并人工核对是否仍有反复选择展示路线、因裁切误判布局、无依据解释根因的行为。调用量和 reasoning tokens 只作比较指标，不作为运行硬上限；避免用缩短最终回复代替提高任务正确性。

本次实现只进行必要的行为验证，沿用现有截图和可视化测试，不为候选句子逐字建立一套新的断言，也不为证明提示词生效新增持久化机制。本次验证不作为修订前后的 A/B 对照，不推导 reasoning 节省比例。

## 相关生产代码

- [根 SYSTEM 提示词](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/ports/system_prompt.py)
- [内置工具描述](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/builtin_catalog.py)
- [HTML 截图结果文字](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [截图执行与截取范围](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/visualization_screenshot_worker.py)
- [内容根选择规则](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/visualization_root.js)
- [SVG 解析与净化](/Users/plumliu/Desktop/python_workspace/pulsara_agent/frontend/lib/svg-renderer.ts)
- [SVG Blob 与图片展示](/Users/plumliu/Desktop/python_workspace/pulsara_agent/frontend/components/diagram-block.tsx)

## 语义交叉审查与验证记录

主代理与 GPT‑6 Astra high 独立审查后共同修正了以下语义风险：保留图示的图片渲染契约；将内联要求限定为 animated SVG 与 SVG 图标；保留用户消息适用范围；以 subject 表达对象连续性而非内容不可修改；避免把截图一概排除出因果诊断证据；用根元素边界解释裁切，不把截图限缩成只能检查布局。

生产修改仅涉及根提示词、内置工具描述及现有 HTML 截图结果文字，不修改输入安装、截图选根、前端渲染或 Ponytail。验证结果如下。

### 代码与契约验证

- 208 项现有测试通过，覆盖 provider 输入前缀连续性、结构化输入编译、图像与工具结果预算、HTML 截图、浏览器隔离；另有一个既存 aiohttp 弃用警告。
- 将因果规则改为最终条件式后，另跑 14 项针对性复核，全部通过。
- 六处文档替换稿与生产字符串逐一匹配；原文与 Git HEAD 中的修订前文本一致。三处生产文件的 AST 差异仅为字符串常量，`git diff --check` 通过。
- 没有修改测试断言、添加跳过项或变更生产执行机制。

### 真实模型正确性抽查

使用用户已保存的 `deepseek-flash` Chat Completions 连接和 `high` reasoning，通过现有生产 Host dogfood 入口运行。配置只读加载，数据库由现有 helper 验证为本机目标后新建一次性 clean-v0 数据库，Pulsara home 和工作区均隔离，结束后清理。临时 home 不加载外部插件，因此这不是原会话启用 Ponytail 时的重放或 A/B 对照；用户的 Ponytail 文件、配置与 hooks 未修改。本次不验证新提示词与 Ponytail 共存时的行为。

首轮八个场景共 35 次模型调用，运行与交付结构检查通过：动态 SVG 的 HTML 展示与独立文件、保留 canvas 动画实现、用户指定 SVG 围栏、测试卡改为 HTML、同一卡片修改文字、明确指回较早轨道图、根裁切截图只读检查、单帧因果追问。调用数仅记录，不作为验收硬上限。

人工检查发现，首轮模型虽然正确保持了展示对象，也解释了根外说明不进入截图，但在 HTML 转换回复中断言“如果仍静止，说明容器把 HTML 做成了静态快照”。这没有证据支持，不能因结构检查通过就将因果解释判为通过。

主代理与 subagent 随后把原先的逻辑陈述“截图或症状本身不能证明原因”改成最终的条件式输出要求：“如果提出未经验证的因果解释，就标为假设”。这一步保留通用证据边界，不增加 SVG 或 PNG 专项规则，也不要求主动生成假设。通过全新 epoch 复测三轮、八次模型调用，检查 SVG 围栏、未主动询问原因的 HTML 转换、以及明确因果追问。

最终复测的交付结构和运行检查通过；明确追问时模型能否定“不动就证明 HTML 被快照化”这一特定推断，但其后提出的排他诊断结论仍未充分排除其它原因。但未被问原因的转换回复仍把 reduced-motion 与圆固定大小直接联系起来，并断言实际页面会脉冲，未取得足以证明这些机制及播放状态的观察。因此最终判定为：**语义交叉审查通过，交付与对象连续性抽查通过；因果解释仍未稳定受控，不能宣称四类问题已全部根治或认知税已经下降。**

原始 provider 输入、reasoning、回复、工具调用、结果与工作区产物保留在以下本地诊断证据中，未删改上述失败：

- [首轮八个场景证据](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-review-20261017/dogfood.json)
- [最终条件式的三轮复测证据](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-review-20261017/causal-dogfood.json)
- [首轮复现脚本](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-review-20261017/run_smoke.py)
- [条件式复测脚本](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-review-20261017/run_causal_smoke.py)

上述 JSON 的 `passed` 仅表示脚本中的运行与结构断言通过；因果解释的人工判定以本节记录为准。证据位于仓库忽略的 `output` 目录，不是新增运行时持久化或生效依据。
