# 演示任务可视化问题记录

状态：2026-10-21 已按用户“修复所有 bug”的最新指令实施，包含此前暂缓的闪烁问题。以下各问题保留修复前的证据；实施结果与验证以本节和文末为准。

## 已落地的修订

| 问题 | 生产修订 |
| --- | --- |
| SVG/Mermaid 没有围栏 | 系统提示词及可视化参考明确要求成对的三反引号、语言标识，提供完整示例；不猜测裸源码并自动执行 |
| SVG 查看截断 | `view_image` 直接支持 SVG，复用已有可终止 Playwright 截图进程和图片预算；按浏览器解析的尺寸、viewBox、比例设置画布，超像素预算报错而不裁剪 |
| 任务/TODO/Plan 混淆 | 系统提示词、TODO 和打开面板工具说明明确：顶层任务 tab 只展示子代理任务 |
| idle 后 TODO 驻留 | ROOT 真正结束且结算完成后，owner 清空快照并发送已有 CLEARED 通知；不把未做事项标为完成，不影响运行中的子代理 |
| 中文文件打不开 | Markdown 入口按 URL 处理已编码的绝对链接，保留转义和片段，不再作为原生路径二次编码；原生文件入口不变 |
| 内嵌 HTML 滚动条过粗 | 内嵌与侧栏本地 HTML、截图共用一份细滚动条样式，10px 命中范围、3px 两侧透明边距，无端部箭头；提示词优先多个紧凑 HTML、独立可视化围栏 |
| 空白/闪烁 | 删除流式中间文本的空操作栏；空 draft 不抢先建立处理过程布局；已知正文与思考同时保留，等正式思考内容加载完成后再释放 draft |
| 水平居中 | 回到最新按扣除滚动条占位后的区域居中，与输入栏和 TODO 使用同一中心；不再额外偏右约 5px |

边界：SVG 解析、单位和图像解码由 Chromium 负责，Pulsara 复用现有文件授权、隔离、截止时间及图像字节/像素预算。SVG 在图片上下文渲染，脚本和外部资源不可用；没有依赖系统缩略图。ROOT idle 只保留空 run 身份用于既有后续续接，未保留清单、未新增持久化事件或恢复路径。阻塞等待不是 idle，续接中的旧 turn 结束也不能清空新 turn 的清单。

提示词/工具描述变更适用于之后安装的新 cold epoch；不改写已有 epoch 的 SYSTEM/tools 前缀。

## 1. 咖啡流程图：SVG 源码未使用围栏

用户观察：模型正确生成 SVG，但回复中的源码没有用代码围栏包裹，未按预期直接展示图。

修复前实现：

- `src/pulsara_agent/ports/system_prompt.py` 已要求静态图使用 fenced mermaid/svg，但没有用明确的完整示例强调开闭围栏和语言标识。
- `src/pulsara_agent/bundled_skills/pulsara-data-analysis/references/visualization-render.md` 也已有 fenced svg/mermaid 指引。
- 当前尚未核对该会话实际最终回复和当时安装的完整提示词，不能将现象直接归因于当前源码完全缺少提示。

待实施方向：明确要求回复中展示 Mermaid、SVG 和其他代码时使用成对、闭合的三反引号围栏，并标明语言，例如 `svg`、`mermaid`、`python`；不要裸输出 SVG/XML 或图表源码。保留 HTML 文件和 `pulsara-visualization` 的现有发布语义。提示词修订不得改写已有 epoch 的冻结 provider 前缀。

验收关注：指令有清晰示例；生产 Markdown 渲染链路对闭合 SVG、Mermaid 和普通代码围栏分别正确渲染。提示词约束不等于能保证模型百分之百遵守，不通过猜测裸文本类型新增隐式渲染路径。

## 2. 咖啡流程图：检查图片时出现截断和大面积空白

### 已有证据

- 第一张截图中，`view_image` 读取 `.check/coffee-journey.svg.png`，展示方形画布：上方只看到前三步及右侧截断，下方大面积白色。
- 第二张截图中，模型将其归因为 `qlmanage` 生成的 1200×1200 缩略图。这是模型当时的判断，尚未通过原始 PNG 独立验证。
- 第三张截图中，模型用 Chrome headless，以 1200×380 视口重新截图生成 `.check/shot.png`，命令输出确认尺寸为 `(1200, 380)`；随后 `view_image` 完整显示种植、处理、烘焙、研磨和萃取五步。
- 本地源文件 `/Users/plumliu/.pulsara/workspaces/quick-20261020-232839-624d8908/coffee-journey.svg` 的根元素确实包含 `viewBox="0 0 1200 380"`、`width="1200"` 和 `height="380"`。
- 在该工作目录内未找到截图对应的两个原始 PNG，暂不能直接检查其像素内容。

### 排查时的代码边界

- `src/pulsara_agent/tools/builtins/filesystem.py` 的 `ViewImageTool` 读取文件；Host 负责图像校验。
- `src/pulsara_agent/conversation_kernel/tool_runtime.py` 将 HTML/HTM 分派给截图路径，其他文件走图片读取路径。
- `src/pulsara_agent/conversation_kernel/image_validation.py` 使用 Pillow 校验 PNG、JPEG、静态 WebP，不直接解码 SVG；所检查的校验逻辑没有裁切或缩放图片。
- `src/pulsara_agent/conversation_kernel/visualization_screenshot_worker.py` 已有浏览器截图实现，可作为后续复用入口评估。
- `frontend/app/styles/workbench.css` 的工具图片预览使用 `height: auto`、最大高度和 `object-fit: contain`；当前样式意图是等比完整展示。

### 初步判断

证据更支持第一次 SVG 转 PNG 时发生了截断，而不是 `view_image` 将完整图裁掉。同一源图在更换截图方式后能完整展示，不能仅凭第一张截图认定前端有裁切缺陷。

实际可用性缺口是：模型查看 SVG 时需要自行选择转换方式，容易使用系统缩略图工具并获得不适合检查完整图的结果。即便让前端更自适应，也无法恢复已经在输入 PNG 中丢失的像素。

### 待实施方向和待核实项

1. 评估让 `view_image(path=...)` 直接检查 SVG，复用现有浏览器、资源隔离和图像校验机制，不另建转换框架，也不依赖用户系统的 `qlmanage`。
2. 阅读现有截图实现与依赖 API 后确定尺寸策略：结合 SVG 的有效尺寸、viewBox 和宽高比适配，覆盖宽图、长图、仅有 viewBox、百分比尺寸等情况，避免固定方形截图导致截断。
3. 保留现有资源和安全边界，包括网络资源限制、文件访问范围及像素/字节预算；不能以“自适应”为由放开这些边界。超界行为需明确，不静默裁掉内容。
4. 同时核对前端窄容器下的等比预览和放大查看，仅修改证实有问题的路径。

验收关注：通过真实工具分派、浏览器渲染、图片校验和前端展示链路，验证宽幅咖啡图的五步都可见；补充竖图及缺少显式宽高的 SVG。既有 PNG 不应被裁切，也不应把原文件自身已有的白边误判成预览故障。具体测试范围在问题收集结束后确定。

## 3. 长任务工作台：把 TODO 清单误当作侧栏「任务」

来源：会话 `session:5k2oii2barqsps3mjzzpldr5uq`，显示名“会话 5k2oii2b”。本轮通过只读数据库查询核对了 canonical transcript 和 assistant message blocks，未修改会话。

### 实际对话与工具记录

- 用户要求拆成四到六项任务、设置依赖，仅完成需求和内容结构，开发/测试/发布保持待办。
- transcript #4：模型调用 `todo`，建立六项清单；所谓依赖仅写在条目文本中的“前置 T1”等字样。
- #9：模型再次调用 `todo`，T1/T2 标为 completed，T3–T6 保持 pending；#10 返回 completed=2、in_progress=0、pending=4。
- #11–12：模型调用 `open_in_pulsara`，同时提交两个文档和 `{"type":"panel","name":"tasks"}`。整个会话没有创建子代理任务的工具调用。
- #13 最终回复称：“任务工作台：已用当前的任务清单工具记录 6 项任务……该工具只有状态字段，没有显式的依赖字段，因此我把依赖关系写在每项任务文本的‘（前置 Tn）’中。”

### 判断

确认存在概念及导航混淆。TODO 是当前工作的有序进度清单；侧栏 `tasks` 是子代理任务视图。模型虽然准确承认 TODO 没有结构化依赖字段，却仍打开了不承载该清单的任务面板。

当前 `frontend/components/inspector-panel.tsx` 将 `tasks` 渲染成接收子代理任务/分组的 `TaskWorkspace`；`open_in_pulsara` 的工具描述只笼统说明 overview panels，schema 列出 `tasks` 名称，没有明确其与 TODO 的区别。`todo` 描述则使用较宽泛的 “current task”。

此前用于演示的提示词也把“任务管理、依赖、留待办”和“侧栏任务列表”混在一起，是诱发误解的因素。不能因此为演示强行创建无必要的子代理，或把纯文本前置关系宣称为运行时依赖。

用户已确认的修订：在模型可见的提示词中明确，顶层「任务」tab（`tasks` 面板）只展示 subagent 的任务，与 TODO 清单、Plan Mode 无关。同步检查相关工具描述，避免仍用含糊的“任务”指代不同功能；修订演示口径，不改用户可见标签，也不增加新面板。

## 4. 长任务工作台：回合 idle 后 TODO 仍驻留输入框上方

### 实际发生了什么

- 用户观察：第一轮已 idle，但输入框上方仍显示 TODO。
- #14 用户追问：“你是不是忘记收尾todo了？”
- #15–16 模型仅用 terminal 检查 `brief.md` 和 `sitemap.md` 是否存在，**没有调用 TODO 更新或清空**。
- #17 模型回答：“没有忘记收尾……2 项已完成、0 项进行中、4 项待办”，又提出可以把 T3 改成进行中作为下一步提示。
- 用户观察：发送追问后清单消失。因此不能解释成模型在第二轮主动清理了 TODO。

### 已定位的生命周期与显示规则

- `conversation_kernel/todo_runtime.py`：`mark_root_idle` 将 ROOT run 从 ACTIVE 变成 `IDLE_RETAINED`，没有清除 snapshot；`current_snapshots()` 仍返回它。
- `activate_root_run` 接受新的用户回合时关闭旧 run，创建空的新 run。Host 将旧 run 的 CLOSED 投影提交给展示链路。
- `frontend/lib/runtime-adapter.ts` 的 `projectTodo` 只排除 CLOSED，并不按会话 idle 隐藏清单。
- `frontend/components/workbench-view.tsx` 的 `TodoDock` 只判断清单是否存在且非空，没有 idle 条件。
- 已有 `test_todo_next_root_activation_closes_old_exact_run` 明确覆盖“下一用户回合关闭旧清单”。
- 历史文档 `archived_docs/PULSARA_LIGHTWEIGHT_TODO_TOOL_REFINEMENT_IMPLEMENTATION_SPEC.zh.md` 也记载 terminal 后保留、下一 accepted human message 清空；这里只用于解释来源，不将归档文档当作当前修订的权威。

结论：用户观察与当前代码的显式生命周期一致，不需要假设关闭事件丢失才能解释。追问开启了新的用户回合，旧 TODO 因换代而消失；并非“追问收尾”这句话触发特殊处理，也不是模型补调用工具。历史时刻的 process-local 状态不可从 transcript 完整还原，但当前代码和工具记录对这一解释相互印证。

### 用户已确认的修订语义

- 当前会话进入真正的 idle 时，由 runtime 自动清空当前 ROOT TODO；不再保留到下一条用户消息，也不只是前端隐藏。
- 在模型可见的提示词中明确：TODO 用于当前运行过程的进度跟踪，进入 idle 后会自动清空，不是跨回合保留的项目待办库；无需模型额外调用清空工具来完成 UI 收尾。
- 清空当前 TODO 不等于完成未做的事项，不将 pending 项改成 completed，也不把未开始的事项标为 in_progress。
- 不删除历史工具调用及结果，不增加持久化待办机制。子代理各自的清单仍按其自身执行作用域管理，不因主会话 idle 而误清理仍在运行的子代理。
- 阻塞等待和运行链内部自动续接不等于真正 idle，不能因中间状态切换误清空。

实施时完整替换已过时的 idle 保留路径，更新对应规范和测试；提示词变更遵守既有 epoch 前缀冻结边界。当前仍处于问题收集阶段，仅记录上述决定，尚未修改生产代码或提示词。

验收关注：正常 final 后 idle、存在 pending 项、全部 completed、停止/失败、阻塞等待、steer、自动续接、新用户回合以及切换会话/刷新。阻塞等待不等于工作结束；自动续接不能因短暂状态切换误清理清单。需要验证真实 Host 状态到前端的链路。

## 5. 视频案例：中文文件链接无法在侧栏打开

来源：会话 `session:kkx5o5bqnat6q2yovypvmkizle`（“会话 kkx5o5bq”）；2026-10-21 补充。已只读核对实际最终回复及文件。

- transcript #146 的原始 Markdown 链接是 `[促销复盘看板.html](/Users/plumliu/Desktop/网店经营数据/促销复盘看板.html)`，模型没有预先写入错误的百分号编码。
- 该文件实际存在，核对时大小 74363 字节。工作区副本链接为 `/Users/plumliu/.pulsara/workspaces/quick-20261020-223045-acea1a21/analysis/dashboard.html`。
- 用户截图中侧栏标题显示百分号编码，错误地址包含 `file:///Users/plumliu/Desktop/%25E7%25BD...`，提示文件已移动或不存在。
- 使用仓库安装的 React、react-dom/server、react-markdown 做只读最小复现：Markdown 的链接组件收到 `/Users/plumliu/Desktop/%E7%BD%91...`；按当前 `pageAddress`/`fileUrl` 路径继续编码，得到与截图一致的 `%25E7%25BD...`。

定位：`MarkdownLink` 把 Markdown 生成的 URL 交给文件浏览器；`frontend/components/file-browser.tsx` 的 `pageAddress` 把以 `/` 开头的地址当成原生文件路径，调用 `frontend/lib/file-preview.ts` 的 `fileUrl` 再次编码。URL 与原生路径的输入边界混淆，导致中文目录和文件名双重编码；并非文件不存在或模型未生成文件。

待实施方向：明确各入口接收原生路径还是 URL，复用标准 URL 处理机制，在正确边界进行一次转换。不要全局反复 decode，也不要破坏文件名中真正的 `%`、`#`、`?`。

验收关注：真实 Markdown 点击到侧栏文件读取的链路，覆盖中文目录/文件名、空格、百分号及字面 `%20` 文件名、片段导航、原生文件入口与 file URL；同时验证英文工作区副本和页内相对链接。排查阶段仅完成最小复现；本轮实施与回归结果见文末。

## 6. 视频案例：内嵌看板的纵向滚动条过粗

用户补充的常规展示原则：提示词应鼓励将可独立阅读的分析拆成多个紧凑 HTML，使用多个独立的 `pulsara-visualization` 围栏穿插在相关说明旁；不要默认把全部内容拼成一个需要长距离内部滚动的 HTML。同一个围栏中的多个路径是画廊分页，不等于多个图块直接嵌入。确实共享筛选器等交互状态的内容仍可合理放在同一文件，不机械拆碎。细滚动条是长内容不可避免时的 fallback，不是期望的常规展示方式。此要求先记录，收齐问题后再修改提示词。

用户截图 2 显示内嵌 HTML 看板右侧有明显的浅色轨道和较粗滑块。用户要求采用右侧侧栏 tab 横向滚动条相似的轻量风格：视觉细、无上下小箭头，同时可适度保留更宽的拖拽命中区域。

初步边界检查：

- 宿主样式在 `frontend/app/styles/base.css` 已有滚动条设置；`desktop.css` 的 `.workspace-tabs` 以透明边框让可见滑块比实际滚动条区域更细。
- `SandboxedHtmlPreview` 使用隔离的 HTML 文档；外层 CSS 不会自动作用于内部文档。当前检查的 `sandboxed-html-preview.tsx` 和 `visualization_shell.js` 未提供统一的 scrollbar 样式；实际看板源文件也未声明 scrollbar 样式。
- 截图中的滚动条很可能属于预览内部文档，实施前需用实际 DOM 确认是内层页面、外层框架还是宿主滚动容器。

待实施方向：在实际拥有滚动条的预览边界提供一致的细滑块、透明轨道和无箭头样式，复用现有配色及交互习惯；不要求模型为每个生成的 HTML 手写这套样式，不扩大脚本权限或改变页面内容布局。

验收关注：长看板纵向滚动、拖拽命中范围、浅色/深色、内嵌与侧栏预览，并确认父会话滚动及原有 tab 横向滚动不受影响。

## 7. 视频案例：运行状态、首个工具和 commentary 周围出现空白/高度闪烁

范围更新：此前用户暂缓此项；随后明确要求修复本文所有 bug，本轮已纳入实施。

用户观察：

- “准备开始”切到“正在处理”时闪烁。
- 首个工具开始时出现额外空白，下一时刻空白消失；其他工具开始时不一定出现。
- 工具完成时也可能发生高度变化和闪烁。
- 中途 commentary “Now I'll build the dashboard. First the HTML template:” 后同样出现异常空白。
- 用户怀疑与等待工具结果有关，尚未确定。不能据此归因于审批阻塞。
- 用户进一步观察：当该轮 Pulsara 图标/标题被增长的思考块、工具块推到视口上方后，工具结束时的闪烁尤其明显。定位时应分别覆盖标题可见与已滚出视口的情况。

实际 transcript 中 #2 和 #48 分别就是截图里的开场和中途 commentary，确认不是只发生在最终回复展示时。

已找到相关历史修复：`8dd593f9`（2026-10-20 09:38:45，东八区；提交时间不等于具体调试时间），涉及 `workbench-view.tsx`、`runtime-adapter.ts` 和 workbench 样式，处理了先前的流式内容交接及 final 收起闪烁。应在现有修复基础上定位遗漏，不回退其已覆盖行为。

本轮只读检查的候选环节：

1. `preparingSubmission`、`awaitingFirstActivity` 与真实 assistant run 的挂载交接；前两者展示独立的运行状态 article。
2. `ConversationRun` 按 message 拆分、每段 `AnimatedDisclosure` 包装，以及 commentary 与工具消息间的 margin/高度。
3. 实时工具数据、提交后的历史内容加载和已知内容保留之间的交接。
4. 自动跟随滚动、布局测量及折叠动画是否放大了中间帧的高度变化。

以上为最初的只读排查入口，后续浏览器定位结果见下节。

### 2026-10-21 定位进展

已使用 Playwright 浏览器加载实际 `WorkbenchView`、`ConversationMessages` 及生产 CSS，构造可控状态序列读取布局。诊断入口：`output/playwright/stream-layout-diagnosis/index.html`；状态夹具：同目录 `probe.tsx`；测量输出：`measurements.txt`。这些是本地诊断文件，不是生产改动或正式回归测试，也没有调用真实模型。下述像素数是该浏览器视口下的测量值，不作为未来实现常量。

**已复现：commentary 下方空白来自操作栏占位。**

- `AssistantMessage` 在没有可显示的回复操作、但消息仍为 running/live 时，渲染空 `.response-actions`。
- 其 CSS 为 `min-height: 21px` 加 `margin-top: 6px`，实际贡献 27px。
- 相同 commentary 和工具卡片从 live 变成正式记录时，空操作栏被移除；测得正文容器高从 51.3125px 降到 24.3125px，工具轨道顶部从 331px 上移到 304px。
- 不需要阻塞、排队或工具输出变化即可复现。第一段与中途 commentary 都会触发，不限于首个工具。
- 待修方向：区分中途 commentary 与最终回复操作栏的展示职责，不能为所有流式自然语言一律预留最终操作栏空间。

**已复现：首个空 draft 与等待状态使用不同布局。**

- 尚无 assistant message 时，`awaitingFirstActivity` 使用独立的 heading + progress article。
- 收到尚无正文、思考内容或工具内容的 live draft 后，等待 article 被替换为 `ConversationRun`，增加“中间过程”toggle 等结构。
- 在相同用户消息、没有新增实际内容的条件下，测得“正在处理”状态顶部从 235.1875px 跳到 263.6875px，相差 28.5px。
- 这确认首段交接存在布局跳变；“准备开始 → 正在处理”此前更早的 admission 切换仍需单独纳入回归，不把两者混为已完全复现。

**另外复现：延迟加载思考内容会暂时替换整张工具卡片。**

- Adapter 在 `historyContextState` 和普通 `contentState` ready 时就删除已提交 draft，没有同时检查 `reasoningContentState`。
- `HistoryMessageHost` 展开过程时将思考加载状态并入整体 contentState；没有 `pendingAssistant` 时，会用“已保存，正文读取中…”替换整段已有内容。
- 通过“普通正文 ready、思考 unloaded → ready”的状态序列，测得含思考和工具的过程段从 98.6875px 缩到 40.5px，再回到 98.6875px。长列表跟随底部时，scrollTop 对应为 2189 → 2131 → 2189，前面可见内容也随之移动。
- 还有一处不连续：`knownAssistant` 遇到 `fallback.body` 非空会主动省略已有 reasoning；加载完成后 reasoning 恢复。同一组夹具中段高为 118 → 131px，其中还混合了前述空操作栏移除的变化。
- 这些是确认存在的加载边界缺陷，但**尚未证实是录像里每次工具结束闪烁的直接原因**。只读检查 kkx5o5bq 的实际 provider replay：其思考块最大 22551 字节，全部低于当前 65536 字节内联阈值，因此不能直接套用“大思考块需额外读取”的解释。

**目前排除与仍待核实：**

- 在其他内容、分组与加载状态不变时，仅将工具 running 改为 completed，短列表的段高保持 153.5px，没有复现跳变。
- 实际 Workbench 的滚动容器通过内联样式设为 `scrollBehavior: auto`、`overflowAnchor: none`；不能把样式表中被覆盖的 smooth 声明误认成这次根因。
- 标题滚出视口后，内容高度变化会让自动跟随底部调整 scrollTop，确实可放大视觉跳动；但标题滚出视口本身不是已确认的故障触发器。
- 录像中不伴随 commentary 的工具结束闪烁仍需更完整的 live → committed → history context/content 事件序列验证。不得将状态夹具复现写成已重演原会话全部时序。

待实施与验收方向：复现“发送 → 准备 → 运行 → 首段 commentary → 首个工具参数流入 → 工具结果 → 后续 commentary/工具 → final”的真实前端状态序列，观察中间帧的节点身份、占位内容、边界位置及滚动偏移。同时覆盖滚动跟随/用户已上滑、展开/收起过程、延迟历史加载，确保已有内容不会短暂消失、重复占位或出现额外大块留白。不采用全局禁用动画、任意固定高度或延迟显示来掩盖问题。

## 8. TODO 与「回到最新」的水平居中基准不同

用户观察：TODO 清单和“回到最新”似乎有水平偏差，怀疑与此前输入栏布局调整有关。

已用实际 Workbench 组件和生产 CSS 在浏览器测量：

| 元素 | 水平中心坐标（px，本次视口） |
| --- | ---: |
| 整个 workbench | 880.59375 |
| 对话内容列 | 875.59375 |
| 输入栏 | 875.59375 |
| TODO 入口 | 875.5859375 |
| TODO 展开清单 | 875.578125 |
| 回到最新 | 880.59375 |

确认：“回到最新”相对输入栏、正文列和 TODO 偏右约 5px。TODO 自身与输入栏已基本对齐，差异不足 0.02px，属于布局子像素舍入；本次未复现 TODO 独立偏移。

原因：桌面布局为右侧滚动条保留 10px，输入栏右 padding 也增加对应宽度，使正文列和输入栏在可阅读区域内居中；TODO 位于输入栏内部，继承这一中心。“回到最新”却仍用整个 workbench 的 `left: 50%`，没有扣除滚动条保留区域。`updateJumpPosition` 只计算纵向 bottom，没有处理水平基准。

待修方向：让“回到最新”与正文列、输入栏、TODO 共用可阅读区域的水平中心，不给 TODO 额外添加补偿，也不硬编码本次测得的 5px。验证侧栏开关/拖动宽度、不同窗口宽度及 TODO 展开/收起时的对齐。本轮已按此方向修订生产样式，验证结果见文末。

## 修复后验证

- 前端 Vitest：72 个测试文件、1122 个用例通过，包含真实 Markdown → FilePreviewProvider 调用、新草稿布局、正式记录思考 hydration 接管，以及既有 final/工具交接回归。
- Python 专项：`test_local_image_tool_executor.py` 31 项通过（新增 4 个真实浏览器 SVG 工具调用），TODO/Host 结束通知/续接隔离、HTML 资源、可视化、安全隔离、文件预览、结构化模型输入等相关回归全部通过。超界 SVG、畸形 SVG 会失败，不截取部分内容冒充成功。
- SVG 真实链路验证覆盖 1200×380 宽图、120×600 竖图、仅 viewBox、百分比尺寸、单边尺寸；检查实际图像尺寸和两角/中心像素。同一输入包含脚本及外部图片引用，仍保持完整本地图像。
- TODO 回归从真实工具调用与结算进入 Host ROOT 结束方法，再检查 owner、live bus 和 wire 投影；覆盖重复 idle、陈旧 turn、未完成结算、自动续接和子代理清单不被清除。idle 时清空不改变先前快照的 pending 事实。
- 浏览器使用真实 Workbench 与生产 CSS 测量：空 draft 前后状态顶部均为 235.1875px；commentary live/committed/tool completed 的工具顶部均为 304px；混合正文和思考的交接前后均为 344px。
- 长列表中标题已滚出视口：思考交接三阶段高度均为 2764.875px、scrollTop 均为 2230；commentary 提交及工具完成三阶段高度均为 2757.1875px、scrollTop 均为 2222，没有原先的收缩再弹回。
- 1600/1100/800px 视口测量：输入框与回到最新的水平中心分别为 1115.59375/740.59375/515.59375px，逐组一致；TODO 差异小于 0.01px，为子像素舍入。
- TypeScript 类型检查、Ruff、前端构建通过。ESLint 无错误，保留 5 条原有 hooks 警告；构建保留现有大 chunk 提示。Python 有既有 aiohttp shutdown_timeout 弃用警告。

诊断证据保存在 `output/playwright/stream-layout-diagnosis/`（忽略提交的本地输出）。这些测量证明已定位的边界修复，不声称重演了录像中的全部事件。未运行模型实测；未重新安装或重启用户当前桌面应用。

保留用户配置、会话以及既有 `assets/Pulsara-demo.mp4`，没有提交或推送。本轮同步构建仓库跟踪的桌面静态资源。
