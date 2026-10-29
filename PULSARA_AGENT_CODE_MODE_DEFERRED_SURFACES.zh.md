# Pulsara Agent／Code Mode：暂缓事项与本轮实施范围

日期：2026-10-30。状态：实施范围约定；暂缓内容待后续讨论。

本文配合 [Agent／Work 设计](PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md) 和 [Code Mode 接入合同](PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md) 使用。运行时业务语义继续以这两份设计为准；提示词、Skill 与前端的实施时机以本文为准。分工给实现 Subagent 时，应同时提供本文。

当前工具名、查询分支与 CELL 身份以 [工具查询、命名与 Cell 身份统一规格](PULSARA_TOOL_QUERY_AND_CELL_ID_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) 为准。本文接口表与示例采用该合同；所链接的既往实验与实施记录保留其原验证范围，不作为新接口已验证的证据。

## 1. 实现者的范围

本轮推进共同对话引擎复用、Agent／Work、Code Mode 执行与必要工具接口。以下暂缓项不要在实现过程中自行定稿或落地，也不以旧实现的文案和界面直接代替新设计。

| 内容 | 本轮处理 | 后续讨论 |
|---|---|---|
| ROOT／Subagent SYSTEM 提示词 | 两份完整文本独立定义；ROOT 提供原生／JS 共用对象与按需 Skill 入口；Subagent 仅保留工作区、权限、能力使用、运行时判读与交付规则，角色职责由 profile 提供 | 其余 ROOT 文案、与工具及 Skill 的分工、token 与理解成本 |
| 预设 profile 提示词 | 暂缓五种预设的正文修订；profile 作为独立 user message、正常压缩等运行时语义仍按主设计实现 | 各预设是否必要、职责与具体措辞 |
| 工具 schema 与说明文案的最终审阅 | 工具查询、命名与 Cell 身份统一规格所列 schema、紧凑分支签名和必要指引纳入实施；其他提示词优化仍暂缓 | 后续继续审阅其余信息量与使用负担 |
| 三个 Code Mode 工具 schema | 实施 `exec_cell`、`query_cells`、`manage_cell` 的闭合分支与简洁说明 | 其余环境教学随实际使用优化 |
| Bundled skill | `pulsara-code-mode` 统一提供普通工具组合与 Agent 编排教学；主文档展示交叉使用，两组参考目录按需读取 | 随实际使用优化表达与示例 |
| 前端展示 | 暂不实施新的标签页、脚本页面、成员页面或依赖图，也不把暂定布局当作完成验收条件 | 信息架构、交互、布局、图形和详细状态展示 |

暂缓文案不改变代码中的调用方、权限和工具可用性规则，也不取消 profile 输入位置等已经确定的业务语义。Skill 教学按下节实施；其余 ROOT 文案、profile 新文案和新的前端表面仍暂缓。

## 2. 三个 Code Mode 入口要求

| 工具 | 已定参数 | 首版说明需要准确表达 |
|---|---|---|
| `exec_cell` | `code`／`path` 恰一，可选 `yield_time_ms` | 启动独立脚本，返回本次观察到的输出和状态；观察超时不停止脚本 |
| `query_cells` | 无 target：可选 `cursor`、`max_items`；CELL target：可选 `offset_chars`、`max_chars` | 列存活 Cell，或查询状态和冻结源码，不消费输出；两分支字段不得混用 |
| `manage_cell` | wait：`action:"wait"`、CELL target，可选 `yield_time_ms`；stop：`action:"stop"`、CELL target | 继续观察同一脚本，或停止后续 JS 与提交；不重新执行代码，已接纳 Work 不自动取消 |

参数边界、默认值、结果格式、调用方范围和停止后的结算均沿 Code Mode 接入合同，不在 schema 中另立规则。首版可直接用于接入和验证，说明保持简洁，不嵌入实验过程或内部实现教程。此次工具命名不改变已定 cell 状态枚举。

压缩后的运行时摘要按 [运行时摘要与资源退役规格](PULSARA_RUNTIME_HANDOFF_AND_RESOURCE_RETIREMENT_IMPLEMENTATION_SPEC.zh.md) 实施：筛选活动／待结算对象，PMCA 计量视图上限 3,200 个 V3 tokens，完整 TODO 独立保留，预算省略仅提供条件查询指引。其余 TODO／Plan 整体文案清理继续暂缓。

## 3. Bundled skill

使用一份英文 [pulsara-code-mode](src/pulsara_agent/bundled_skills/pulsara-code-mode/SKILL.md)，包含两组按需参考目录：

- **`references/tool-calling/`**：同名 `tools.*` 接口、变量和结果处理、顺序与并发、正式输出、错误处理、脚本等待和停止。
- **`references/agent-orchestration/`**：单次派活、复用 Agent、batch 队友引用、成功依赖、消息、等待，以及调查—审查—修订—实现—代码审查和中断后继续工作的示例。

主文档直接展示普通工具结果进入 Agent instruction 的混合示例，并链接具体参考文件。正式输出统一使用 `console.log`；不介绍或示范 `return`，其运行时支持保持原样。Skill 说明 ROOT＋bypass 的适用范围，不新增 Skill 权限机制。

## 4. 前端暂定方向

暂定采用 **Subagent 与 JS 脚本两个顶级 tab**。这是后续界面设计的起点，本轮不实现。

| 视图 | 暂定展示内容 |
|---|---|
| Subagent | 按 Agent session 展示成员、连续对话、当前及排队工作；Work 之间的关系用更细致的有向图呈现 |
| JS 脚本 | 展示脚本代码、状态、正式输出和错误；普通工具调用在页面中简要展示，不展开成通用工具流程图 |

依赖图节点对应 Work，卡片显示执行它的 Agent。同一 Agent 执行多份 Work 时分别形成节点；连线表示明确的 `depends_on`，不把同 Agent 的串行顺序自动当作成功依赖。图从已接纳工作与实际状态生成，不解析任意 JavaScript 来预测未来分支。原生派活与 JS 派活使用同一工作事实。

两个视图可通过真实 Agent／Work 与调用来源互相定位。脚本状态与工作状态分别呈现：脚本结束或停止，不代表它派出的工作结束。具体布局、图的组织范围、交互与视觉样式后续再定；不为前端另建 workflow 对象、工作状态副本或执行恢复机制。
