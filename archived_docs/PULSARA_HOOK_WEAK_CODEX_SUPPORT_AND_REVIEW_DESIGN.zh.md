# Pulsara Hook 有限 Codex 支持与用户审阅设计

日期：2026-09-30；2026-10-01 更新。状态：原生输入、匹配别名、完整审阅、v3 信任契约及四种 Plugin Hook 目录变量已实现；外部格式导入支持部分并报告未支持功能。相关非真实会话检查 240 项及 Ponytail 生产 GUI dogfood 已通过。原联合实施与复审见第 11 节，本轮验证见文末。

本文拥有本次联合 Hook hard cut 的合同，取代旧的混合工具名输入与“完整脚本等价转换”承诺。能力管理入口、安装作用域、权限与采用仍遵循 [应用与能力管理边界设计](PULSARA_APP_AND_CAPABILITY_MANAGEMENT_BOUNDARY_DESIGN.zh.md)。

本稿已按 [搜索工具拆分实施规范](PULSARA_SEARCH_TOOL_SPLIT_IMPLEMENTATION_SPEC.zh.md) 修订，以 `search_content` / `find_files` 的目标工具表为配套边界。搜索规格拥有工具参数、结果、私有 ripgrep 打包与 terminal PATH；本文拥有整体 Hook 输入、匹配别名与用户审阅。两者联合完成一次 hard cut 和一次 v1 → v2 信任契约切换。搜索规格第 7 节已明确引用本文的原生输入与唯一闭合别名表；其他工具的原生名字切换和完整审阅由本文负责，两份规格不并行保留不同 public tool_name 契约。

搜索拆分与私有 rg 接线已按上游规格实施；工具元数据减法不在本文重做。同一 critic 已完成联合代码复审，第 9 节的真实 GUI 激活验收已通过，记录见第 11 节。

## 1. 产品决定

Pulsara 提供一个小而明确的命令 Hook 系统。对外准确表述为：“支持部分 Codex Hook 配置与输出协议，脚本输入使用 Pulsara 原生契约。”这里的支持不承诺外部插件提供的 Hook 脚本无需适配就能运行。脚本收到的工具名与参数始终使用 Pulsara 原生契约。工具别名只决定是否触发，不改变脚本输入。

这里的“弱支持 Codex”不是“完整复刻 Codex，仅少一个功能”。明确不支持 PreTool 参数改写，也不复制其完整工具、会话记录、环境和结果替换机制。PostTool 保留追加上下文的能力，原工具结果完整保留。这个取舍使 Hook 负责观察、提示和有限阻止，使工具 owner 继续负责实际调用与结果结算。

不建设 Claude Code Hook 运行兼容层，不为其他 harness 添加专用输入、工具参数翻译、私有状态文件或控制协议。2026-10-01 用户授权补齐有限目录变量适配：Plugin command Hook 同时提供 PLUGIN_ROOT／CLAUDE_PLUGIN_ROOT 和 PLUGIN_DATA／CLAUDE_PLUGIN_DATA，各对值相同；执行 shell 展开这四个 braced 引用（POSIX 使用环境展开；Windows 转为 cmd 环境引用），避免把目录字节重新解释成 shell 代码；保留原始声明与脚本字节。MCP 不因此取得 Claude 环境别名。已有插件格式导入中的 Skills、MCP 与普通资源不因本设计被删除；Hook 部分统一落到同一个 Pulsara 契约。

Plugin 作者可以完全忽略 stdin，也可以只读自己需要的字段。普通安装流程不要求模型逐个阅读脚本、证明跨宿主等价或替作者改写程序。

### 1.1 兼容层与脚本可用性的明确边界

这里的“脚本”包括外部插件随包提供的 Hook 程序。第 4 节的“脚本收到的 tool_name”指该程序实际从 stdin JSON 读取的字段：本地工具传 Pulsara 原生名字，MCP 传已解析的实际远端身份；不是 matcher 别名，也不是给脚本保留的供应商工具名。

| 层次 | Pulsara 承诺 | 不据此承诺的行为 |
| --- | --- | --- |
| 配置导入与事件 | 导入支持的组件，明确报告不激活的宿主组件、事件和 handler；无效的支持项仍拒绝 | 不复刻供应商完整生命周期、运行环境或全部 handler |
| 工具匹配 | 原生名、通配规则及闭合别名按生产 matcher 决定是否触发 | 不替换脚本内部的工具名判断，不从命令文本推断语义 |
| 脚本输入 | 传递真实原生 tool_name、tool_input 和公开 tool_response | 不翻译成供应商工具名、参数或结果结构，不补造 transcript/宿主私有状态 |
| 输出控制 | 只解析第 5 节明确支持的上下文、阻止、许可及停止控制 | 不支持任意供应商控制扩展，也不保证脚本输出自然符合本合同 |

“参数不做兼容翻译”不等于“不传参数”或“所有读取参数的脚本都不可用”。只读取已提供字段、且字段结构和语义一致的脚本可能直接适用；依赖其他字段或供应商内部状态的脚本需要作者明确适配。是否实际可用还取决于脚本依赖、执行环境和输出协议，不能只根据 matcher 命中判断。

| 外部 Hook 的用法 | 按本文合同的判断 |
| --- | --- |
| matcher 为 Bash；脚本只读取 tool_input.command，不判断工具名 | terminal 提供 command，输入层面可以适用；仍须满足其运行依赖和输出合同 |
| matcher 为 Bash；脚本内部判断 tool_name == "Bash" | 会收到 terminal，该条件不成立；别名命中不会替作者改写判断 |
| matcher 为 Edit/apply_patch；脚本要求供应商专有编辑字段 | 会收到 Pulsara 的 path/base_revision/operations 等实际参数，需要作者适配 |
| 脚本不读工具名/参数，只在受支持事件输出固定上下文 | 输入名字差异不构成障碍；不据此声称整个脚本或插件已验证可用 |

支持范围也不限于“显式列出兼容别名的工具 Hook”：原生名字、生产 matcher 支持的通配规则，以及 SessionStart 等非工具事件都可使用。闭合表限制的是额外名字候选，不是所有 Hook 的使用方式。普通安装不要求 Pulsara 中的模型逐个审计或改写外部脚本；发现具体不适配时解释缺口，只有另行授权的插件编写工作才修改脚本。

## 2. 调研依据与结论

[Agent Plugins 1.0](https://agent-plugins.org/specification) 的可移植组成只有 Skills 与 MCP。Hook 属于客户端扩展；通过插件标准验证不等于 Hook 能跨客户端运行。Pulsara 保留既有扩展位置 `dev.pulsara/hooks/hooks.json`，不把它包装成协议已有的标准 Hook 组件。

2026-09-30 对公开仓库默认分支的源码抽样如下。数量是本次逐包统计，不是市场公布的统计，也不代表安装或执行验证：

| 样本 | 仓库内插件包 | 带 Hook 配置 | 解释 |
| --- | ---: | ---: | --- |
| [OpenAI plugins](https://github.com/openai/plugins) | 64 | 1 个草稿 | Figma 脚本明确标为草稿；不能当成已启用的 Hook。 |
| [Claude 官方插件库](https://github.com/anthropics/claude-plugins-official) | 39 | 7 | 官方维护的 25 个包中有 7 个带配置；其余 14 个是仓库内外部插件包。 |
| [Claude Code 的 plugins](https://github.com/anthropics/claude-code/tree/main/plugins) | 12 | 5 | 与官方插件库有重复，不合并计算比例；mods 不计入 plugins。 |
| [Fusengine Codex 集合](https://github.com/fusengine/codex) | 25 | 22 | Hook 命令大量委托给额外安装的 Bun harness，不能由目录存在推断完整运行依赖。 |
| [Awesome Copilot 的 plugins](https://github.com/github/awesome-copilot/tree/main/plugins) | 100 | 0 | 100 份 manifest 通过 Agent Plugins 1.0 schema 检查；未据此声称整个包完全合规。 |

Awesome Copilot 另有 8 套独立 Hook，[am-will/codex-skills](https://github.com/am-will/codex-skills) 有 51 套独立 Hook；它们不计入插件包数量。样本显示 Hook 在工作流类社区工具中有价值，但不足以支持“所有插件都依赖 Hook”或“Hook 已有统一跨宿主接口”的判断。

脚本依赖差异比外层工具名差异更重要：

| 例子 | 实际读取或依赖 | 行为 |
| --- | --- | --- |
| [Explanatory Output Style](https://github.com/anthropics/claude-plugins-official/blob/main/plugins/explanatory-output-style/hooks-handlers/session-start.sh) | 不读取 stdin 字段 | SessionStart 输出固定上下文。 |
| [Superpowers 启动脚本](https://github.com/obra/superpowers/blob/main/hooks/session-start) | 自身 Skill 文件与平台环境变量，不读工具参数 | 按平台选择上下文 JSON 形状；[Codex 发行声明](https://github.com/obra/superpowers/blob/main/.codex-plugin/plugin.json) 则显式设为 `hooks: {}`。 |
| [ECC SessionStart](https://github.com/affaan-m/everything-claude-code/blob/main/scripts/hooks/session-start.js) | 启动事件、source、项目与历史摘要文件 | 注入项目上下文；[Codex 配置](https://github.com/affaan-m/everything-claude-code/blob/main/hooks/codex-hooks.json) 与 Claude profile 分开。 |
| [危险命令拦截器](https://github.com/am-will/codex-skills/blob/main/hooks/aitmpl-codex/security/dangerous-command-blocker/.codex/hooks/dangerous-command-blocker.py) | `tool_input.command` | 正则判断命令，退出码 2 表示阻止。 |
| [Hookify](https://github.com/anthropics/claude-plugins-official/blob/main/plugins/hookify/core/rule_engine.py) | 自行判断 `tool_name`；按规则读路径、内容、命令，可能读 transcript | 返回警告、PreTool deny 或 Stop block；外层 matcher 命中不保证内部工具名条件成立。 |
| [Ralph Loop](https://github.com/anthropics/claude-plugins-official/blob/main/plugins/ralph-loop/hooks/stop-hook.sh) | session ID、Claude transcript JSONL、`.claude` 状态文件，不读工具参数 | 检查完成标记，要求停止前继续；没有可用 transcript 时不能保留其原行为。 |
| [Copilot Build Perf C++](https://github.com/github/copilot-plugins/blob/main/plugins/build-perf-cpp/scripts/write-correlation-id.ps1) | Windows 环境，不读事件 JSON | 写入关联 ID，不返回模型控制结果。 |

因此，不读取参数的 Hook 应保持简单；依赖特定 harness 的脚本也不会因增加别名而自动可用。安装器只承诺自己能够检查和执行的接口，不承诺任意程序的实际行为。

## 3. 一个原生输入契约

命令继续通过 stdin 收到一个 JSON 对象。公共字段保留 `session_id`、`cwd`、`hook_event_name`、`model` 与 `transcript_path: null`。事件各自携带现有的 source、prompt、permission_mode、tool_use_id、tool_response、trigger 或子 agent 信息；不为本设计增加新的元数据收集器。

工具事件的输入固定为：

- 本地 `tool_name` 是实际 Pulsara builtin descriptor 的名字，例如 `terminal`、`read_file`、`edit_file`、`write_file`。
- `tool_input` 是该工具实际收到的原生参数对象。`terminal` 保留 command/workdir；`edit_file` 保留 path/base_revision/operations；`write_file` 保留 path/content。
- 搜索拆分后，`search_content` 使用 pattern/path/file_glob/output_mode/limit/offset，`find_files` 使用 glob/path/limit/offset；必填项、默认值与闭合字段以搜索规格为准。没有 target，不把原生字段翻译成供应商 Grep/Glob 参数。
- MCP 保留实际远端工具身份及其原生参数，不把外层管理或调用入口冒充成远端工具。
- `tool_response` 保留现有公开结果；不暴露内部执行对象、凭据或私有 replay。
- 删除为了混合命名而提供的 `pulsara_tool_name`；不同时发送 native 与 vendor 两套工具名或参数。

例如，配置 matcher 为 `Bash` 的 Hook 命中 terminal 时，脚本仍收到 `tool_name: "terminal"`。匹配 `Edit` 的 Hook 命中编辑工具时，收到 `tool_name: "edit_file"`，不会收到伪造的 apply_patch command 或 Claude new_string。

配置 matcher 为 `Grep` 时，三个 output_mode 都收到 `tool_name: "search_content"`；`files_only` 仍是按正文匹配找文件，不变成 Glob。配置 `Glob` 时，只命中 `find_files` 并收到其原生 glob 参数。公开结果保留搜索规格的匹配行、文件或计数页单位，不改写为供应商结果；搜索 Hook 与搜索结果都不授予 read_file 的 content_revision 或 seen-line 编辑 authority。

Hook 命令的工作目录与 stdin cwd 继续是会话绑定的工作目录。terminal 单次 cd/workdir 不改变会话根；需要观察具体命令目录的脚本读取原生 tool_input.workdir。Plugin 命令继续使用 `PLUGIN_ROOT` 与 `PLUGIN_DATA`。不新增 CLAUDE/CODEX 环境变量镜像，也不创建供应商会话记录文件。

这个输入契约是有意与 Codex 不同的产品决定。当前 [Codex 文档](https://learn.chatgpt.com/docs/hooks) 使用公开名 Bash/apply_patch，并支持部分参数与结果控制；本设计只借用下文列出的子集。依赖供应商输入的脚本需要作者显式适配。

## 4. 别名只参与匹配

复用现有 RE2 matcher，保持大小写敏感、search 语义和既有解析边界；不另造名称识别器或语义模型。每个工具始终匹配自己的原生名字，外部别名限于以下闭合表：

| 配置中的外部 matcher 别名 | Pulsara 工具 | 插件 Hook 脚本 stdin 中的 tool_name |
| --- | --- | --- |
| Bash | terminal | terminal |
| Read | read_file | read_file |
| Edit | edit_file | edit_file |
| Write | write_file | write_file |
| apply_patch | edit_file、write_file | 各自实际名字 |
| Agent | spawn_agent | spawn_agent |
| Grep | search_content | search_content |
| Glob | find_files | find_files |

Read 是本稿新增的常用别名，Grep/Glob 随搜索拆分一起进入最终映射；其余保留既有映射的选中范围。terminal_process 与 terminal_monitor 不归入 Bash；Pulsara 的独立进程操作不等同于 Codex write_stdin 对原命令的透明续接。MCP 按实际远端名字匹配，不拆名称、猜语义或套用 builtin 别名。

Grep 只匹配 search_content，Glob 只匹配 find_files；不按 output_mode、结果是否含 files 数组或参数文本推导另一个别名。尚未拆分的 search_files 不获得这两个别名；联合 hard cut 删除该旧工具后才采用最终映射，不保留新旧搜索并行或旧调用转发。原 matcher 字节不变，不做正则文本替换；名称选择兼容仍不保证供应商脚本参数或输出兼容。不扩展 WebFetch、Task、exec_command 等其他别名。

事件 matcher subject 保留现行规则：SessionStart 使用 startup/resume/compact，没有 clear producer；SessionEnd 使用现有 reason；工具事件使用上述候选；压缩使用 manual/auto；子 agent 使用 Pulsara 现有 worker profile；UserPromptSubmit 与 Stop 不按工具筛选。相同事件名不表示供应商的完整生命周期已被复刻。

### 4.1 搜索后端与 terminal Hook 的边界

搜索规格固定随包 ripgrep 15.2.0，两个专用工具通过私有绝对路径与 --no-config 调用，删除系统 rg 探测及 Python 搜索 fallback。该后端是搜索工具的实现依赖，不新增 rg Hook 工具名；一次 search_content/find_files 调用只经过该工具的生命周期，不为底层 rg 子进程再触发一次 Bash Hook。

Pulsara terminal 按搜索规格第 2.2 节，在现有环境 owner 构建的最终子进程 PATH 前置同一私有 rg 目录。模型通过 terminal 执行 `rg` 时，Hook 仍观察 terminal，匹配 Bash，不因命令文本包含 rg 就改为 Grep 或 Glob。terminal 的权限、shell 参数和结果保持原路径，不获得专用搜索工具的只读分类、分页或 --no-config 合同；主/子 agent、前台/后台 terminal 的覆盖由搜索规格验收。

本稿不修改系统或主进程 PATH，不为 Hook executor 新增私有 rg PATH 注入。terminal 可直接使用随包 rg 的保证不自动扩展到 Plugin Hook 命令；Hook 自身写裸 rg 时仍依赖其执行环境，不由安装器下载依赖或复制一份二进制。搜索依赖不可用的可见诊断归搜索/terminal 原 owner，不能让系统 rg 偶然可用掩盖打包错误，也不让 Hook 模型自行修复应用依赖。

## 5. 支持的执行与控制

仅支持 command handler，复用现有进程执行、取消、超时、输出捕获、信任检查与安全时点采用。沿用既有单次解析、资源和执行边界；不增加总 Hook 数、会话时长、累计调用或重试上限。

保留现有事件集合与下列控制范围，不为了追随 Codex 后续扩展增加新的 handler 或 owner：

| 事件 | 支持范围 |
| --- | --- |
| SessionStart | 纯文本或 hookSpecificOutput.additionalContext；continue:false 阻止该入口的后续处理。 |
| SessionEnd | 观察与诊断，不控制已经结束的会话。 |
| UserPromptSubmit | 追加上下文；沿用退出码 2、continue:false、decision:block 的阻止形式。 |
| PreToolUse | 追加上下文；退出码 2、decision:block 或 permissionDecision:deny 阻止待执行工具。 |
| PermissionRequest | 现有 decision.behavior 的 allow/deny；未决定时继续普通权限流程。 |
| PostToolUse | hookSpecificOutput.additionalContext；原工具结果保持完整。 |
| PreCompact、PostCompact | 保留现有 continue:false 所作用的入口 gate，不撤销已发生的副作用。 |
| SubagentStart | 追加上下文，不用 continue 控制子 agent 创建。 |
| SubagentStop、Stop | 沿用现有停止 gate 与已验证的至多一次 Hook continuation；不成为自主循环引擎。 |

PreTool 不支持 updatedInput，也不新增 allow/ask 自动许可；自动允许只通过现有 PermissionRequest owner。PostTool 不支持结果替换、隐藏、decision:block 或 continue:false；不能将这些输出偷偷解释为普通追加提示。参数/结果改写、updatedPermissions、output suppression、prompt/agent/http/mcp_tool handler 均不在范围内。

异步 command 保留现行 advisory 行为，不接受阻止、许可、停止或继续控制。异步完成不建立 durable job、不自动开启新回合、不承诺崩溃恢复或跨重启交付。

空输出或合法的空 JSON 可以表示无贡献。stderr 与 systemMessage 是诊断，不自动变成模型上下文。追加上下文仍是一次性的、不可信的 advisory user-role 输入，不提升为 SYSTEM/developer 权威。

按既有 parser 顺序处理：退出码 2 先于 stdout 解析，PreToolUse/UserPromptSubmit 表示 gate block，Stop/SubagentStop 表示 continuation request，stderr 提供理由；此路径不把 stdout 作为 JSON 控制或追加上下文。其他事件的退出码 2 及其余非零退出码沿 HandlerFailure 诊断，不解释为拒绝。仅退出码 0 的 stdout 才进入空输出、JSON 对象或纯文本分支；纯文本只在 SessionStart/UserPromptSubmit/SubagentStart 追加上下文，其他事件沿现有诊断/不支持规则，不自动转成上下文。

退出码 0 的 JSON 若包含 hookSpecificOutput，必须带与当前事件外部名称完全相同的 hookEventName。PermissionRequest 的许可形状是 hookSpecificOutput.decision.behavior=allow|deny，不接受根级 permissionDecision 作为该事件的许可。根级 decision:block 必须同时有非空字符串 reason，仅用于表中允许的事件。Stop/SubagentStop 的 continue:false 表示 explicit terminalize，decision:block 表示 continuation；同一个 JSON 同时要求两者时整体无效，不选择部分控制。不同有效 handler contribution 的聚合沿既有 dispatcher 规则，explicit terminalize veto 阻止 continuation，不新建另一套优先级或循环机制。

超时、进程错误、畸形 JSON 和不支持的控制输出沿用明确诊断后 fail open 的规则；只有支持的显式退出码控制或完整有效解析的贡献影响 owner 操作。含 updatedInput 的退出码 0 JSON 对象整体无效，不采纳其中的部分 allow/context 字段。异步 handler 的任何控制仍不得影响 owner，即使退出码 2 本身可解析；既有 background settle 不聚合 gate/permission/continuation。实现时对这类可解析控制贡献沿现有 HookDiagnostic 补明确的不支持诊断，保持 advisory 边界，不新增事件、执行 owner 或交付保证。不将所有非零退出码都解释为拒绝，也不因弱支持而静默吞掉不支持项。

直接执行格式化、写日志等文件副作用属于脚本程序本身，不是 Hook 返回协议。信任本地命令不等于脚本只能修改工具结果，也不宣称 Hook executor 是工具权限沙箱。

## 6. 安装与说明的边界

继续复用一个选定格式 importer、一个 native validator、一个 Hook parser 与一个 executor。Codex 发行声明显式关闭 hooks 时，不激活邻近 Claude/Cursor Hook 文件；不将不同发行版的 Hook union 到同一个候选包。

安装器检查可确定的结构与声明：选定来源、事件、matcher、handler、配置字段、命令与资源位置。导入成功表示支持部分形成原生候选，不表示完整源宿主行为等价。已声明而不支持的宿主组件、未知顶层扩展、Hook 事件以及 prompt／agent／http／mcp_tool handler 不激活，必须在既有预览 notices 和安装 diagnostics 中说明；不新增兼容报告、持久登记或模型决策参数。保留普通资源，但不把这些声明注册为原生能力。

仅按选定发行版的声明和已知默认约定选择组件，不从其他发行版 manifest union。未引用的目录是资源，不因目录存在就认定为活动宿主组件；Claude 默认 commands／agents／outputStyles 只按其 Markdown 约定观察，其他发行版不从这些邻近目录推断活动组件。已支持 Skill／MCP／command Hook 自身的无效结构、正则或字段，及任何来源安全、凭据、race、取消和资源边界仍沿原生 owner 拒绝；不能将无效支持项归类为不支持而掩盖失败。已知不支持的 Hook 项保留原声明并由唯一原生 parser 跳过，诊断区分未支持与无效。

通过检查表示“支持的配置已转换为 Pulsara 契约”，不表示“脚本行为已经验证”，也不表示“完整供应商 harness 等价”。普通安装不执行脚本，不递归追踪依赖，不做通用静态程序分析；不要求模型提供语义兼容证明。已支持的声明完整保留，未支持部分明确报告，不放宽来源安全。

在现有安装预览中直接说明：

> 已导入支持的组件，未支持的功能见提示。Hook 提供插件目录兼容变量，但工具名与参数仍为 Pulsara 原生契约；脚本行为尚未验证。

目录别名由 Plugin 的唯一冻结 provenance producer 提供，检查、审阅、执行及失败快照使用相同环境；local Hook 不注入 Plugin 变量。固定四变量的执行展开不改写作者脚本，不处理任意供应商变量。新增环境与展开语义要求 trust contract hard cut 到 v3：旧信任不迁移，保留 enabled，用户重新审阅后才执行。已有 provider prefix 不因环境变化重建。Ponytail dogfood 在非真实测试通过后新建生产配置 GUI 会话，由模型下载并 USER 安装、启用／信任按真实审阅表单完成，验证主／子会话上下文和模式切换；不得据配置解析成功宣称端到端通过。

这是一个面向当前配置的说明，不新增独立兼容报告、持久验证状态或“完全兼容”标签。Native 校验、Plugin 启用、Hook 信任、实际运行各自报告自己的事实。

若作者声明、具体诊断或实际运行表明脚本依赖 transcript、不同参数结构或外部 harness，解释缺口，不自动包装或改写脚本。用户另行授权适配时，作为明确的插件编写工作处理；仍使用同一个目标契约。市场脚本没有额外声明并不构成自动可运行保证。

Bundled installer 的常规指令只需告诉模型：选择发行版、调用原生管理工具、处理实际 HITL、按结果使用能力。详细输入/输出表放在参考文件，只有具体问题才读取。模型不填写“用户已审阅”，不通过 terminal trust 绕过首方确认。

## 7. 用户确认与触发范围变化

确认页首先展示用户要判断的事实：

- 来源：插件名称或本地来源；“所有对话”或“具体项目工作目录（该项目的对话）”，显示来源当前开关及信任状态。WORKSPACE 接受按既有 workspace_state_key 共享，不表示仅授权眼前一个 session。
- 触发：中文生命周期，例如“工具调用完成后”。
- 适用操作：当前可核实的操作名称，例如“读取文件、编辑文件”。
- 执行命令：保留原始完整命令，长内容可滚动或换行，不截断为无法审阅的摘要。
- 确认文案：信任后，后续匹配事件可以自动运行这些本地命令。

默认不显示“范围：{}”、definition ordinal、digest、内部 source slot 或诊断枚举。JSON 空对象不充当用户范围说明。高级细节放入“原始匹配规则与执行详情”，包括原 matcher、别名映射、Windows 命令、声明环境、超时、异步及 context 设置。信息仍可完整审阅，但不逐字段强迫用户理解实现。

例子：配置 `^(Read|Edit)$` 的 PostToolUse 确认页显示“读取文件、编辑文件”；展开后显示 Read → read_file、Edit → edit_file，并说明脚本收到原生名字和参数。

搜索拆分后的 `^(Grep|Glob)$` 显示“搜索内容、查找文件”；展开后显示 Grep → search_content、Glob → find_files。count/files_only 都属于“搜索内容”。通过 terminal 执行 rg 的匹配仍显示“运行终端命令”，不由 UI 分析命令内容猜测成另一类工具。

匹配列表由 Host 在现有 review 准备阶段，使用当前可观察 tool snapshot 与生产 tool_matcher_subject/definition_matches 得出；Frontend 只展示 Host 提供的派生名称与目录完整性说明，不在 JavaScript 复制 RE2、别名表或名称推断。public_hook_snapshot 已在现有 review/result DTO 中接入这份进程内派生观察，包含 definitions、匹配操作、别名与目录完整性说明。MCP 匹配候选必须复用既有 MCP 观察/dispatch 的已解析 provider_tool_name 身份，再交同一个生产 matcher；不能从 call_mcp_tool、tool_ref 或模型入口名猜测远端身份。无法从当前观察确认的目录说明不完整，不创建新 inventory owner、工具 ref 或发现调用来证明清单完整。观察不加入 trust digest、canonical rows 或新的 authority，也不为目录变化增加 nonce/generation；信任提交仍只按既有 exact-definition revalidation 确认定义，目录变化不变成逐工具重新审批。复杂正则只列实际命中，不猜测一句宽泛概括。无命中显示“当前未发现匹配工具”；远端目录尚不完整时如实说明。通配符说明其覆盖后续满足原规则的工具，当前匹配清单只是观察，不是新的授权白名单，也不承诺永远不会触发。

不通过扫描命令文本或让模型总结来声称脚本“只读取文件”“不会修改文件”。如展示控制能力，描述该事件允许的返回控制，不冒充脚本实际会做的事情。

联合 hard cut 修改公开 tool_name、删除混合字段，增加 Read/Grep/Glob 别名并替换搜索入口，属于已审阅行为的变更。实施时统一使旧 Hook trust 失效，保留原启用设置，待用户重新审阅后执行；不把旧接受自动升级为新接受。Plugin enable 与 Hook trust 仍是不同边界。

历史联合实施复用搜索规格确定的同一次 `TRUST_DIGEST_CONTRACT` 更新：从 `pulsara.hook-definition-trust.v1` 切到 `pulsara.hook-definition-trust.v2`，覆盖最终搜索工具、全部新别名及原生 Hook 输入契约。联合实施不先对 Grep/Glob 重新授信，再让用户因真实 tool_name 切换重复审阅；v2 首次发布就表示这组完整语义。不增加新 trust store、逐工具 digest、别名注册表或数据库字段；该固定标识属于既有跨重启语义确认边界，不是普通代码文件 SHA。

激活前停止旧 runtime，新冷会话仅使用最终 descriptor、matcher、输入与 v2 digest。原 trust 文件、trusted_at、enabled 与 Plugin 包状态不作迁移或批量改写。任何 USER/WORKSPACE、local/Plugin 来源，包括稍后才加载的旧项目来源，只计算 v2 当前 digest：enabled 的旧已信任来源成为 MODIFIED，不能执行；disabled 来源保持 DISABLED，开启也不能恢复旧信任。完整 GUI 审阅与 exact-definition revalidation 后写入 v2；信任操作不顺带开启来源或 Plugin。不枚举旧项目、不批量 revoke、不接受 v1 fallback，重置 PostgreSQL 也不代表 trust 文件已清空。

如果实际发布顺序改为先独立发布搜索规格的 v2，后发布本文的其他输入/别名变化，后者必须显式修订为下一固定 trust contract 并再次审阅，不能让同一个 v2 代表两种语义。届时同步修订两份规格的激活记录；不为分批发布添加旧/新 profile 协商或兼容读写。当前计划仍是一次联合发布。

今后别名或输入/控制语义的变化也须更新同一确认契约并重新审阅，不随普通发布静默扩大。单纯文案、排版或相同语义的实现修复不使 trust 失效。规则未变时发现新的远端工具仍遵循已审阅的 matcher 语义，不增加逐工具批准机制。

## 8. Ownership 与一次性落地

RE2 继续拥有正则执行；现有进程 owner 拥有命令生命周期；工具与权限 owner 拥有真实参数、授权及副作用结算。Pulsara Hook owner 只拥有事件输入、闭合别名、支持输出、来源信任与未来事件 dispatch。UI 展示上述 owners 的当前事实，不成为执行 authority。

ripgrep 拥有搜索与文件发现，wcmatch 拥有纯 glob 匹配，现有 TerminalEnvironmentOwner 拥有 terminal 环境构建；本文不复制其机制，也不扩大其作用到 Hook 命令。搜索工具仍直接供模型调用，模型无需拼 terminal rg 命令或记忆二进制位置。工具参数、排序、分页、错误、发行平台与资源边界完整沿搜索规格实施，不由本稿另写一套。

本次联合实施作为一次 hard cut 完成：

1. matcher/input 路径改为真实 tool_name 与原生参数，删除 external_primary 与 pulsara_tool_name 的生产用途，不保留旧输入 fallback。
2. 配套完成搜索规格的两个 descriptor 与旧入口删除，加入 Read/Grep/Glob 的闭合映射；runtime 与 review 使用同一 matcher，不为底层搜索进程重复发 Hook。
3. 一次切换到最终 v2 信任契约，沿上述激活顺序失效旧接受并更新完整 review 的分层展示和 Host 准备的派生匹配清单/目录完整性说明，不在 Frontend 复制 matcher，不创建第二次安装审批或新的审阅注册表。
4. 同步 importer 说明、bundled Skill、模型/GUI 结果文案及相关现行规格，删除任意脚本完全等价的承诺；原生来源安全与活动配置不支持诊断保留。
5. 搜索依赖打包、terminal PATH 与后端删除沿搜索规格完成；更新两份规格共同受影响的测试、dogfood 与架构基线。两个搜索工具替换一个旧工具所增加的一个 builtin 由搜索规格说明，不改变 canonical 数据库 schema，不新增 durable/live event、subject、guard、relation 或 job 类别。

能力变更沿现有安全时点采用，管理调用继续使用自己的 predecessor Hook view，不由新配置在同一次 PostTool 自触发。SYSTEM/provider tools 在已有 epoch 内保持字节相同，历史 messages 只追加；Hook 输入变化不增加 provider rebase 边界。部署使用新进程和获准的 cold epoch，不向旧进程热补丁安装新输入协议。

已有启停、shadowing、权限、取消、资源边界与现有 continuation 限制继续有效。本设计不顺带删除整个 Hook 生命周期，也不新增执行恢复、依赖安装服务或统一 harness 框架。

## 9. 验收范围

实现时使用仓库 `.venv/` 与 uv 进行聚焦验证。联合变更完整执行搜索规格第 10 节的 schema、查询行为、打包、terminal、权限与 prefix 验收；本稿补充以下 Hook 可观察行为，不用 Hook 测试替代搜索或成品安装验证：

- Bash/Read/Edit/Write/apply_patch/Agent/Grep/Glob 的映射与输入真实名字一致；脚本内部没有自动别名替换，MCP 身份原样保留。
- `^Grep$` 只命中 search_content 的全部三种结果形式，`^Glob$` 只命中 find_files；公开参数没有 target，search_files 不存在。专用工具底层 rg 不额外触发 Bash；terminal 执行 rg 只沿 terminal/Bash 路径，不按文本推导别名。
- read_file 新命中、v1 旧 trust 失配、确认期间目标变化拒绝；覆盖 USER/WORKSPACE、local/Plugin 与稍后加载的旧项目。enabled 旧来源为 MODIFIED，disabled 保持 DISABLED；重新开启不等于重新信任，trust 也不顺带 enable。一次完整重审后写入最终 v2，无批量改写或 v1 fallback。
- WORKSPACE 范围说明项目共享而非单个 session；当前无匹配、复杂正则、通配符、尚不完整远端目录及高级详情正确显示，命令可完整审阅。匹配清单由 Host 复用生产 matcher 准备并传给 Frontend，前端没有第二份别名/RE2 逻辑；MCP 原生与 meta 路径的观察身份和实际 dispatch 一致，不把入口名当成远端名；目录变化不改变定义 digest 或产生逐工具白名单审批。
- 无 stdin 读取、只读 source/prompt、读 terminal command、读原生 path 的代表脚本可用；空输出、追加上下文、PreTool 拒绝与普通权限路径保持有效。
- 输出顺序：退出码 2 不解析 stdout JSON，其他非零为 failure；退出码 0 JSON 必须满足 hookEventName、事件对应的嵌套许可和非空 block reason。Stop 的 terminalize/continuation 单项、同对象冲突及不同 handler 聚合 veto 分别验证。updatedInput、PostTool 结果替换、未知控制与异步控制有明确诊断，不发生隐藏改写；异步退出码 2 不影响 owner，脚本失败不借用其他宿主的 fail-closed 语义。
- 兼容层：Bash matcher 命中但脚本仍收到 terminal；读取 command 的示例与内部判断 Bash 的示例分别证明字段可用和不翻译工具名，Edit/apply_patch 不获得供应商编辑字段。原生名、通配规则与非工具事件仍可使用。安装/说明不把配置转换、matcher 命中或输出解析成功描述成外部脚本已验证可用。
- 不依赖逐脚本模型审计即可完成普通安装；已知不支持的活动配置不被静默删除；显式空 hooks 不加载其他发行版文件。
- prefix 连续性、管理调用 predecessor view、取消与实际副作用结算保持原合同。

联合实施先完成非真实会话检查，再按搜索规格要求由 GPT-6.1 Sol / xhigh critic 审阅完整代码及证据，修订无阻塞后运行真实 Pulsara GUI dogfood。上述流程已经完成，实际实现与验收记录见第 11 节；文档审阅本身不作为代码通过的证据。

真实 dogfood 使用保存的生产配置作为只读输入，绑定隔离项目并使用 GPT-6 Luna / xhigh；覆盖搜索规格要求的文件查找、正文搜索、files_only、count 下一页与直接 terminal rg。通过现有 GUI 一次审阅最终 Hook 契约，验证 Read/Grep/Glob、无 stdin 读取的启动 Hook，以及读取 terminal 原生 command 并可阻止的 Hook；后续可追加实际新定义需要的审阅，不为同一联合契约安排第二轮失效。只在获准实施后运行，不把其他 harness 的完整市场插件强行跑通作为本契约的验收条件，不以本机 dogfood 冒充其他发行平台安装验证。

## 10. 实施前修订记录

2026-09-30 按最新搜索规格，将 Grep/Glob 从暂缓事项改为联合目标表，补齐原生搜索输入、结果与读取观察边界；明确私有 ripgrep、terminal PATH 和 Hook executor 的不同责任；复用一次最终 v1 → v2 信任切换及完整激活顺序，并同步 UI、实施与验收。不修改搜索规格或运行代码。本稿仍待用户审阅；搜索依赖打包修订和联合实现仍须按上述门槛验证。

同日联合复审完成：同一 GPT-6.1 Sol / xhigh critic 完整核对两篇文档及现行生产 owner，主 agent 修订后获“无剩余实施前阻塞”结论。主要闭合点为退出码 2/0 的解析顺序、hookEventName 和嵌套许可、同对象控制冲突/跨 handler veto、异步退出码 2 的既有诊断接线、WORKSPACE 项目共享范围、Host 生产 matcher 派生 review 清单以及 MCP 观察身份与 dispatch 一致。上游同步闭合离线准备、稳定资源路径和 terminal 窄依赖失败；联合首次发布只有一组最终 v2 语义。两文档本地链接及空白检查通过。当前只完成设计与审阅，不把文档结论描述成实现、平台验证或真实会话测试已通过。

同日用户澄清：新增第 1.1 节，将有限支持明确拆为配置/事件、匹配、原生输入与有限输出控制；明确外部插件脚本实际收到 Pulsara 工具名与参数、别名不改写脚本内部判断，参数字段一致的脚本可能直接适用但不保证整包行为。同步别名表表头、安装说明与验收；这是既有原生输入合同的语义澄清，不新增翻译层、脚本审计或信任切换。本次仅修订本文，未实施或运行测试，也未启动新一轮 critic 审阅。


## 11. 联合实施记录

联合实现、测试与验收记录已提交于 `807ef701`。用户后续单独要求实施本文时，主 agent 与同一 critic 再次对照第 3–9 节核对当前生产代码，确认目标已经全部联合落地，没有剩余功能实施项；仅澄清正文的实施前措辞，不再次切换信任契约或重复要求用户授信。

2026-09-30，本文与上游规格联合实现；同一 GPT-6.1 Sol / xhigh critic 修订后代码复审通过。public stdin 使用 Pulsara 原生输入，别名只用于 matcher；Host 以完整冻结 builtin 组合与当前 MCP 实际身份派生审阅清单，前端没有别名引擎。v2 只切换一次，不迁移信任或自动开启来源。新增异步退出码 2 不支持控制的明确诊断，安装说明仅承诺配置转换。

代表脚本、输出控制、信任失配、权限/前缀、完整 Host 审阅观察和前端已通过非真实会话检查。最终 Python 全量 2444 passed，前端 587 passed，相关类型、Ruff、协议及构建检查通过。

真实 GUI dogfood 已通过：用户选择隔离项目 `/Users/plumliu/Desktop/test1/search_split_20260930`，保存的 GPT-6 Luna / xhigh；会话 `6395e367` 在 GUI 一次完整审阅并接受三条定义，同项目新冷会话 `2736bf6e` 运行无 stdin SessionStart、Read/Grep/Glob 观察和读取 native terminal command 的 PreTool 拒绝。实际脚本收到原生名字与参数，PostTool 追加上下文正常，`printf PULSARA_BLOCK_ME` 退出码 2 被拒且未执行；内容计数三页及裸 rg 15.2.0 通过。GUI 的完整命令、项目范围、适用操作和高级原生输入边界均已实看。实际输入与结果位于隔离项目 `.pulsara/events.jsonl` 与 `.pulsara/start.log`。详细平台及验收记录见上游第 12 节。


真实 dogfood 发现并修复一处 P2 前端提示问题：后端 capability 表单正常关闭发送 `SUBMITTED`，adapter 曾仅将 `RESOLVED` 视为正常，因此误报未知结束原因。现在 `SUBMITTED` 静默收尾，`CANCELLED` 显示普通取消，未知原因继续提示；不把提交当成配置已应用，也没有新增协议、缓存或事件。三个回归通过，runtime-adapter 共 80 项通过，最终前端全量 587 项与 TypeScript/构建通过，同一 critic 对窄修订复审无阻塞。重新加载前端后，在会话 `2736bf6e` 对同一份已信任定义再次提交 GUI 审阅，实际只显示正常提交且配置已应用，错误提示未再出现；未撤销信任、修改定义或再次切换 v2。

### 2026-10-01 本轮适配验证

目录别名、旧 v1/v2 信任失效、受支持组件严格校验及未支持组件报告已完成代码落地。相关非真实会话测试 127 项通过，Ruff 通过。固定上游 Ponytail 4.10.0（e3ba2aa6f1e6f0bc4d69eb09c9f0d0a93af56156）的 Claude/Codex 两个发行版均可导入，分别得到 6 个 Skill、3 个 Hook；这是导入阶段验证，后续生产 GUI 运行结果见下文。

本轮最终相关非真实会话检查合并运行 240 项通过，修改文件 Ruff 和 diff whitespace 检查通过。生产 GUI 新建会话 16a2d26a（test1、GPT-6 Luna/xhigh）中，模型自主从官方仓库克隆 Ponytail，并用 INSTALL_PLUGIN／USER／codex 发布到 ~/.pulsara/plugins/packages/user/ponytail/pkg_68d5501344d6447f919ba494ed986829；4 个 Hook 脚本字节与下载源相同。启用前两次中断暴露目录资源预估问题，修订见来源查询规范第 13.1 节；重启并继续后进入真实启用审阅，列出 6 Skill、3 Hook；用户已完成启用和信任审阅，工具核实 enabled 与 TRUSTED，未重复安装。

生产 GUI 运行验证已完成：新冷会话 4f80aa1c（test1、GPT-6 Luna/xhigh）及真实子任务 hook-receipt-check 均在不读取 Skill／规则文件、不手动执行脚本的情况下报告 Ponytail full 规则。runtime 同时观察 SessionStart／SubagentStart 的 PONYTAIL:FULL。随后普通用户输入 /ponytail ultra 触发 UserPromptSubmit 的 PONYTAIL:ULTRA，主会话实际收到“PONYTAIL MODE CHANGED — level: ultra”，新子任务 hook-dogfood-ultra 报告 ultra 规则；Plugin data 的 .ponytail-active 为 ultra。/ponytail off 实际产生“PONYTAIL MODE OFF”，新子任务 hook-dogfood-off 明确报告未收到 Ponytail 规则。最后 /ponytail full 实际收到恢复通知，状态文件恢复 full，插件保持启用与 TRUSTED。三条声明均验证了真实执行和模型可见结果，修复后未再出现本轮资源中断；保存的生产设置未改写。

验证边界：Ponytail 的 Codex 分支在模式切换时只注入通知，后续子任务读取当前模式并注入相应规则；这不宣称已将主会话旧规则删除或替换。状态文件由上游脚本写到同一 USER Plugin data 目录，不能宣称存在逐会话状态隔离。未执行 /ponytail default，不改其用户默认配置。上述为当前包的实测结果，不扩大到任意供应商 Hook 输入或宿主行为。
