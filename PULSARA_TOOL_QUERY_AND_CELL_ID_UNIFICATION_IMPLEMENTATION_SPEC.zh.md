# Pulsara：工具查询、命名与 Cell 身份统一实施规格

日期：2026-10-30。状态：本规格实施与要求验收完成；实际证据与未测边界见实施记录。

本文确定本轮目标合同。生产代码、公共消费者与现行设计已按本规格完成 hard cut，不保留旧工具别名、旧参数双读或第二条执行路径。实现落点、验收结果与未测边界见 [实施记录](output/tool-query-cell-unification-20261030/IMPLEMENTATION.zh.md)；该结论不代表全仓测试或全部外部连接均已验证。

## 0. 权威、范围与非目标

本文负责模型可见内置工具的查询入口合并、命名、参数分支、target、查询内容、分页、调用方边界，以及 Cell canonical ID。本文覆盖的接口以本文为准；其他生命周期与业务语义继续遵循：

- [Subagent／Work 与双入口编排设计](PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md)。
- [Code Mode 接入合同](PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md)。
- [子代理与 Cell 侧边栏规格](PULSARA_SUBAGENT_CELL_SIDEBAR_DESIGN_RESEARCH.zh.md)：侧边栏与 `open_in_pulsara` 展示接入合同。
- [统一身份合同](archived_docs/PULSARA_UNIFIED_IDENTITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)的 canonical ID 编码与唯一 owner 原则。
- [target 共同规则](archived_docs/PULSARA_TOOL_TARGET_HARD_CUT_OVERVIEW.zh.md)中仍适用的对象身份、角色与参数分离规则。历史文档中的旧 Subagent／Task 表面不复活。

Provider schema 的实验证据必须同时阅读：[Qwen／DeepSeek 工具 Schema 兼容性报告](output/tool-schema-compatibility-20261026/REPORT.zh.md)。它约束本规格的 schema 投影与验证方案（§3.4、§16.4）；报告中的候选不是已经发布的生产合同，也不授权取消现有适配。

已有 target 合同及其 [target 实际接口实验](output/target-schema-2x2-20261028/REPORT.zh.md)作为本轮基线继承：复用对象形状、角色区分、lowering、本地校验和简短函数说明。本轮只验证查询合并、新增详情与 Cell 身份等实际差异，不重新选型整套 target 接口。

### 0.1 本轮必须完成

1. 将对象发现与详情查询收敛为领域级 `query_*`。
2. 将 Process、Monitor、Cell 的控制动作保留在各自 `manage_*` 中；不逐个动作增加工具。
3. 保留执行、内容读取、搜索、导出的区别。
4. 补齐 Process 精确元数据查询、Monitor 实际监听配置查询、Cell 存活列表与实际执行源码查询。
5. Cell 使用 `CELL` target 和现有 canonical ID 编码。
6. 同步所有 native／Code Mode 消费者、权限分类、结果交付、Hook、SYSTEM、bundled Skills、测试与当前设计中的接口表。

### 0.2 明确不做

- 压缩后的运行时状态交接与资源退役由 [runtime handoff 与资源退役规格](PULSARA_RUNTIME_HANDOFF_AND_RESOURCE_RETIREMENT_IMPLEMENTATION_SPEC.zh.md) 单独定义，见 §15；本文不增加该范围。
- 不改变扫描与能力选择、Agent／Work 调度、进程执行、Monitor 投递、定时触发、记忆检索或 artifact 存储语义。
- 不增加历史对象总量、工具调用总次数、运行总时长或重试次数上限。
- 不新增 Cell 持久表、执行恢复、源码副本库、身份注册表、通用对象查询框架、cursor 注册表或新的指纹。
- 不改变外部 MCP 工具名称、远端协议、SDK API，亦不把任意第三方工具重命名为本规格的格式。
- 不增加 UI 页面或标签页；现有渲染器、工具结果展示、停止入口中受本次字段与名称变更影响的消费者必须同步。

## 1. 统一语义

### 1.1 工具名、action、target 各负其责

| 部分 | 含义 | 约束 |
|---|---|---|
| 工具名 | 领域和主要职责 | `query_*` 用领域复数；`exec_*`、`manage_*` 等沿下文封闭名称 |
| `action` | 同一管理入口内的具体操作 | 查询工具不再要求 `action=list/inspect/get`；管理 action 为闭集 |
| `target` | 本次选定的对象或地址 | 已有对象复制完整 target；拟创建地址沿对应工具合同提供；不推导、截短或替换 ID |
| `parent`／`session`／`replacement` | 父范围、会话角色或替换角色 | 不因它们也是 target 形状就改名成主 `target` |
| 其他参数 | 内容、过滤、观察预算、页位置、修订条件 | 不混入对象身份 |

创建 Cell／Process 时对象尚不存在，不要求伪造 target；成功接纳后返回对象引用。一般情况下，已经持有 target 就直接操作，不要求先枚举或查详情；Code Mode 中 MCP 调用的先查询要求沿 §4.3 保留。

沿已有 target 合同，PATH 可以表示拟创建文件的位置，ADD_LOCAL_MCP 的 MCP_CONNECTION 可以表示拟创建连接地址；本轮不改变这些入口。target 本身不证明对象存在、存活、可见或已获授权。

`query_*` 表示查询该领域：无主 target 时查询集合或概览，有 target 时查询所选对象。所选对象的详情可以包含分页子项，例如 AGENT 下的 WORK 列表；不把“有 target”误定义为“只能返回一行”。

### 1.2 query 的行为合同

- 不改变目标业务状态，不消费业务输出，不推进 Cell 的输出确认位置，不注册／取消 Monitor，不启动进程或工作。
- 可返回 target、cursor、MCP 调用引用与当前执行资格。继续复用现有引用生成、交付和结算机制；不添加额外确认流程。
- 查询结果是本次观察，不承诺之后目标仍存活或状态未变化。执行时继续由真实 owner 检查权限、归属和可用性。
- 保留每个领域的结果结构与业务值，不引入统一 `data` wrapper、跨领域查询 target union 或可交换 cursor。
- 输出大小受现有 40,000-byte 模型工具结果边界及真实 provider 输入预算约束。页面、artifact 或 typed 失败必须明确，不能把截断内容伪装成完整数据。
- 同时携带互斥分支参数必须由本地校验拒绝，不静默忽略。本地 canonical JSON Schema 与运行时校验同源；provider wire schema 可沿既有 lowering 成为 superset，不能把 wire 接受等同于本地可执行，详见 §3.4。

## 2. 全量变更矩阵

下列矩阵列出所有改变入口名称或职责的工具及全部原管理动作。三项 session 工具列入对照以说明边界；未列明的内置工具保持原行为。

### 2.1 能力与协作查询

| 当前入口 | 目标入口与分支 | target／范围 | 保留的核心语义 |
|---|---|---|---|
| `list_capabilities` | `query_capabilities` 无 target | 可选 kind、source_kind、scope、parent、offset、limit | 概览／过滤／父组件列表，完整性和诊断 |
| `inspect_capability` | `query_capabilities` 有 target | 当前支持的 capability target | 详情、资格、MCP schema／invocation 与原引用交付 |
| `list_agents` | `query_agents` 无 target | ROOT 全成员；Subagent 自身及已知成员 | 按 Agent session 分页，不限于正在运行的 Agent；单独返回 ROOT 别名 |
| `inspect_agent` + AGENT | `query_agents` + AGENT | 调用方可查询范围内的 Subagent | 该 Agent 的 WORK 分页列表 |
| `inspect_agent` + WORK | `query_agents` + WORK | 调用方可查询范围内 Agent 的委派工作 | 精确状态、instruction、final／diagnostic 等 |

`run_agent`、`send_agent_message`、`wait_agent`、`stop_agent`、`tools.run_agent.batch`、`tools.wait_agent.works` 不改名、不改变语义。`list_agent_models` 不是当前工具，不重新引入。

### 2.2 Process 执行、查询与管理

| 当前入口／action | 目标入口／action | 必需参数 | 可选参数与默认值 |
|---|---|---|---|
| `terminal` | `exec_process` | `command` | `workdir`、`tty=false`、`yield_time_ms=10000`、`max_output_chars=32000` |
| `terminal_process.list` | `query_processes` 无 target | 无 | `include_running=true`、`include_finished=true` |
| 无独立精确查询 | `query_processes` 有 target | PROCESS target | 无；复用已有进程信息，新增不返回日志的精确查询入口 |
| `terminal_process.poll` | `manage_process.poll` | PROCESS target | `since_cursor`、`max_output_chars=32000` |
| `terminal_process.wait` | `manage_process.wait` | PROCESS target | `since_cursor`、`timeout_seconds=30`、`max_output_chars=32000` |
| `terminal_process.write` | `manage_process.write` | PROCESS target、`data` | `yield_time_ms=1000` |
| `terminal_process.submit` | `manage_process.submit` | PROCESS target、`data` | `yield_time_ms=1000` |
| `terminal_process.close_stdin` | `manage_process.close_stdin` | PROCESS target | 无 |
| `terminal_process.kill` | `manage_process.kill` | PROCESS target | 无 |

`manage_process` 不再接收 `list`。不添加独立 `poll_process`、`wait_process`、`read_process_output` 或 stdin 工具。`process` 始终是当前终端 owner 管理的操作系统进程／进程组，不是任意系统 PID，也不是动词“处理”。

### 2.3 Monitor

| 当前入口／action | 目标入口／action | 输入 | 含义 |
|---|---|---|---|
| `terminal_monitor.list` | `query_monitors` 无 target | `{}` | 当前可见的未关闭登记 |
| 无独立精确查询 | `query_monitors` 有 target | MONITOR target | 当前登记状态、绑定进程、实际生效的监听／投递／寿命配置 |
| `terminal_monitor.register` | `manage_monitor.register` | PROCESS target；可选 `conditions`、`delivery`、`lifetime` | 沿当前注册与结算 owner 创建监听 |
| `terminal_monitor.cancel` | `manage_monitor.cancel` | MONITOR target | 停止后续监听投递；不停止进程 |

### 2.4 Cell

| 当前入口 | 目标入口／action | 输入 | 含义 |
|---|---|---|---|
| `exec` | `exec_cell` | `code`／`path` 恰一；可选 `yield_time_ms=1000` | 执行一次独立 JS 脚本并返回本次输出观察 |
| `wait_exec` | `manage_cell.wait` | CELL target；可选 `yield_time_ms=1000` | 等待并交付同一 Cell 的后续输出 |
| `stop_exec` | `manage_cell.stop` | CELL target | 停后续 JS 与工具提交；不自动取消已接纳 Work |
| 无 | `query_cells` 无 target | 可选 `cursor`、`max_items` | 当前 Host 仍持有的 Cell 分页清单 |
| 无 | `query_cells` 有 target | CELL target；可选源码页参数 | 元数据与实际冻结源码；不读取执行输出队列 |

不同时提供 `wait_cell`、`stop_cell`、`list_cells`、`inspect_cell` 别名。Cell 生命周期控制收敛为三个原生入口：`exec_cell`、`query_cells`、`manage_cell`。

### 2.5 定时计划

| 当前 `scheduled_tasks.action` | 目标入口／action | 必需参数 | 其余合同 |
|---|---|---|---|
| `list` | `query_schedules` 无 target | 无 | 可选 cursor、status、session；status 省略表示不过滤，仅接受 ACTIVE／PAUSED，拒绝 null |
| `get` | `query_schedules` 有 target | SCHEDULE target | 无列表筛选和分页参数 |
| `create` | `manage_schedule.create` | values | 可选 session、status；status 省略／null 默认 PAUSED |
| `update` | `manage_schedule.update` | target、expected_revision、values | 完整值校验与原更新事务 |
| `pause` | `manage_schedule.pause` | target、expected_revision | 原修订检查 |
| `resume` | `manage_schedule.resume` | target、expected_revision | 原修订检查 |
| `delete` | `manage_schedule.delete` | target、expected_revision | 原删除语义 |
| `switch` | `manage_schedule.switch` | target、expected_revision、replacement | 原子切换；replacement 的 existing／create 分支不变 |

### 2.6 纯改名与明确保留

| 当前入口 | 目标入口 | target／主要参数 | 改动范围 |
|---|---|---|---|
| `artifact_read` | `read_artifact` | ARTIFACT；offset_chars、max_chars | 只改名，保留分页内容与 coverage |
| `artifact_export` | `export_artifact` | ARTIFACT、目的 path | 只改名，保留不覆盖现有文件及写权限 |
| `memory_get` | `get_memory` | MEMORY | 只改名，保留语句、上下文、生命周期、直接关系 |
| `memory_search` | `search_memory` | query；可选 kind、limit | 只改名，保留召回、排序、范围放宽与完整性说明 |
| `search_sessions` | 不变 | query／lifecycle 或 continuation cursor | 空 query 的活动会话列表功能保留 |
| `search_session_content` | 不变 | SESSION、query 或 continuation cursor | 内容搜索不并入对象 query |
| `read_session_content` | 不变 | SESSION／SESSION_ENTRY 或 continuation cursor | 内容读取不并入对象 query |

18 个对照入口变为 19 个：能力 2→1、Agent 2→1、Process 2→3、Monitor 1→2、Schedule 1→2、Cell 3→3、artifact／memory 4→4、session 3→3。真正改变名称／拆并的旧入口为 15 个，目标入口为 16 个；三个 session 工具仅作保留对照。统计不包含未改动的其他工具及动态 MCP 工具。

## 3. target 矩阵与输入分支

### 3.1 运行对象与其他已有身份

| 对象 | 公共 target | 使用范围 |
|---|---|---|
| Agent | `{"kind":"AGENT","agent_id":"session:…"}` | query_agents；已有协作工具按各自合同 |
| Work | `{"kind":"WORK","work_id":"queue:…"}` | query_agents 精确工作查询；不把 ROOT 人类输入变成 WORK |
| Process | `{"kind":"PROCESS","process_id":"process:…"}` | query／manage_process；注册 monitor |
| Monitor | `{"kind":"MONITOR","monitor_id":"monitor:…"}` | query_monitors；cancel |
| Cell | `{"kind":"CELL","cell_id":"cell:…"}` | query／manage_cell；新对象类型但无新注册表 |
| Schedule | `{"kind":"SCHEDULE","task_id":"schedule:…"}` | query_schedules、manage_schedule |
| Session | `{"kind":"SESSION","session_id":"session:…"}` | 定时计划的 session 角色及原会话工具 |
| Artifact | `{"kind":"ARTIFACT","artifact_id":"artifact:…"}` | read／export_artifact |
| Memory | `{"kind":"MEMORY","memory_id":"memory:…"}` | get_memory 及原记忆变更工具 |

上述省略号仅为说明，不能作为工具实参。SCHEDULE 的 `task_id` 是现有定时领域字段，不属于已退役 Subagent Task；本轮不为命名整齐改数据库列或这个已有 target 字段。

### 3.2 Capability target 保持现有形状

| kind | 身份字段（除 kind 外） |
|---|---|
| PLUGIN | scope、plugin_id |
| SKILL | skill_path（精确能力查询仍要求已观察到的绝对文件路径） |
| HOOK_SOURCE | scope、source_kind；PLUGIN 来源另有 plugin_id |
| MCP_CONNECTION | scope、source_kind、server_id；PLUGIN 来源另有 plugin_id |
| MCP_SERVER | runtime_server_id |
| MCP_TOOL | server_id、tool_name |
| MCP_RESOURCE | server_id、uri |
| MCP_RESOURCE_TEMPLATE | server_id、uri_template |
| MCP_PROMPT | server_id、name |

继续区分安装配置 MCP_CONNECTION、运行实例 MCP_SERVER 和远端叶子；不因为 query 合并而统一其 ID。`MCP_TOOL_REF` 是 META 调用返回的调用引用，沿现有 `use_new_mcp_tool` 使用，不冒充可枚举能力配置 target。

### 3.3 公共参数规则

所有新入口采用封闭参数分支；不接受额外键。`target:null` 不等于列表分支，必须拒绝；缺少 target 才进入集合分支。沿旧接口明确允许的其他 null 语义保持，例如 Process 的 since_cursor、定时计划列表／创建的 session，以及创建时的 status；定时计划列表的 status:null 仍须拒绝。

集合与详情分支允许的分页参数不同，按各章精确校验。不能采用“所有字段可选，再在 executor 随意忽略”的开放 schema。复用各领域现有目标构造器与 owner，不建立一个全对象 target 类型库。

### 3.4 Canonical schema、provider wire 与模型填参

#### 3.4.1 必须保留的实验结论

2026-10-26 的报告是在指定两个目标与配置下得到的证据，不能推广成所有 OpenAI-compatible 服务的永久能力表：

| 已观察事实 | 对本轮的约束 |
|---|---|
| 精确生产根 union：共同 auto 配置下 Qwen 失败，DeepSeek 成功；现行展平两端可用 | 不删除 lowering，不直接把 canonical 根 oneOf／anyOf 发给所有 provider |
| 根 union 补 properties:{} 可被 Qwen 接受，但样本参数仍违反 canonical | HTTP 成功或 response.completed 不足以证明 schema 兼容；不采用补空 properties 的假修复 |
| 展平接受了 poll 携带 wait 专属 timeout_seconds，本地 parser 拒绝 | wire 是有意的 superset；跨分支限制必须在执行前严格校验 |
| Qwen 不能可靠利用根 parameters.description；function.description 对照能利用 | 必要分支签名放在工具 function.description，不能只留在 schema 根说明或 SYSTEM |
| 内联嵌套 oneOf 在两个目标的小样本中可用，但引入 request 包装且体积更大 | 属于可选的新参数合同，不自动采用，也不宣称天然省 token |
| $defs/$ref 候选在 Qwen 上填参不可靠 | 不为压缩新工具定义而新增依赖 $ref 的共同 wire 方案 |
| 原实验未测全工具集、真实执行、修正轮或前缀连续性 | 新名称与新分支不能继承“已经端到端验证”的结论；按 §16.4 补证据 |

#### 3.4.2 本轮明确采用的生产路径

保留现有参数层级：`target`、`action`、cursor 等仍按各章直接位于参数根，不增加 `request` wrapper 或解包器。复用 `llm/adapters/openai/function_tools.py` 的共同 lowering 与显式 `strict:false`，Chat Completions 与 Responses 继续走同一 function definition owner；不新增按 provider 名字分叉、失败后换 schema 重试或旧／新参数协商。

| 层 | 负责什么 | 不代表什么 |
|---|---|---|
| 本地 canonical descriptor／parser | 精确分支、必填、额外键、target 类型、null、默认值及执行前校验 | 不要求原始根 union 直接成为 provider wire |
| Provider wire projection | 从 canonical 确定性生成兼容参数定义；保留所有本地合法调用的表达能力 | 展平后可能容纳本地非法组合，不是执行授权或参数真值 |
| function.description | 用紧凑签名解释模型应该选哪个分支、该传什么 | 不是强制解码，也不能取代 parser |
| JS 工具目录／dispatch | 沿已有 Code Mode 合同使用同一 canonical 参数语义与执行校验 | 不为复制 native 展平而降低 JS 校验，不新增一层 request/data |

对支持的内置工具，必须证明 canonical 合法样例经过 wire 投影后仍合法；不反向要求所有 wire 合法值都可执行。根分支使用 lowerer 已支持的完整 object 分支形状，避免为表达互斥引入尚未支持的根 not／if／then 等约束。每个分支仍必须列完整允许字段并关闭额外键；跨层复用原 owner，不复制 lowerer。

展平损失分支对应关系是已知取舍。本地拒绝错误参数并沿正常工具结果返回可修正错误，不能剥掉多余字段后偷偷执行，也不能把数值字符串强制转为数值或将 target:null 当省略。能力查询没有 action discriminator，不能依赖 lowerer 自动生成 action 说明；必须在 function.description 明确“无 target”和各 target.kind 分支。

target.kind 继续判别对象类型，管理工具的 action 判别操作；查询合并不增加 action。嵌套 target 的内联闭合形状继续由既有 lowerer 投影，不能把根分支关系的损失理解成所有 target 身份约束都消失。模型偶发先查列表再查精确 target 属于正常可恢复调用，不因此增加选择字段、阻止合并或要求零错误证明。

当前 terminal 描述已经采用 function.description 动作签名，schema 导出也移除了重复 discriminator 说明。新工具应延续这一实际路径；这是当前实现事实，不等于新工具的模型行为已验证。

#### 3.4.3 必要分支签名矩阵

下列是 function.description 必须覆盖的信息；字段类型、取值范围与默认值继续留在 canonical schema 中，不把整套 schema 再抄成 prose。签名中的 `?` 表示可省略，并不表示接受 null。工具描述必须写明只使用所选分支列出的字段，并单独保留确实允许 null 的语义。

| 工具 | 必须说明的分支 |
|---|---|
| query_capabilities | 概览／列子项：省略 target，允许 kind?、scope?、source_kind?、parent?、offset?、limit?；对象自身详情：仅 target。parent 是父范围，根 kind 是类别过滤，均不与详情 target 同传 |
| query_agents | 列 Agent：省略 target，允许 cursor?、max_items?；列某 Agent 的 Work：AGENT target、cursor?、max_items?；单个 Work 详情：仅 WORK target，禁止分页字段 |
| query_processes | 列进程：省略 target，允许 include_running?、include_finished?；单个进程元数据：仅 PROCESS target，禁止列表过滤；输出通过 manage_process 观察 |
| query_monitors | 列可见登记：空参数；单个登记状态／配置：仅 MONITOR target；两者均不接收分页参数 |
| query_cells | 列存活 Cell：省略 target，允许 cursor?、max_items?；状态／源码页：CELL target、offset_chars?、max_chars?；列表页参数与源码页参数不得混用，执行输出通过 manage_cell.wait 获取 |
| query_schedules | 列计划：省略 target，允许 cursor?、status?、session?；单个计划详情：仅 SCHEDULE target，禁止列表字段。session 省略／null 表当前会话；列表 status 省略表示不过滤，显式值只允许 ACTIVE／PAUSED |
| manage_process | poll／wait／write／submit／close_stdin／kill 各自签名按 §2.2；只有 wait 使用 timeout_seconds，只有 stdin 写操作使用 data |
| manage_monitor | register：PROCESS、conditions?、delivery?、lifetime?；cancel：仅 MONITOR |
| manage_cell | wait：CELL、yield_time_ms?；stop：仅 CELL |
| manage_schedule | create／update／pause／resume／delete／switch 各自签名按 §2.5；保留 revision 与 replacement 两分支要求 |
| exec_cell | code／path 恰一、yield_time_ms?；不能两个都传，也不能都省略 |

仅改名的单分支工具不为统一形式增加 action。签名文字必须与 schema／parser 做行为核对，不能仅靠字符串快照保证准确。不同层不重复长篇解释，不通过删掉必要互斥说明来制造 token 下降。

以下英文是六个合并查询入口的核心 function.description 文案，明确意图、参数和角色；实施时保留各领域仍必要的结果使用说明，例如 MCP invocation 的调用方式，不复制实验历史或内部适配解释。实际默认值和数值范围留在字段 schema；只明确会影响分支选择的省略／null 差别。

**query_capabilities**

```text
Query capabilities. Omit target for an overview or listing; optional fields: kind, scope, source_kind, parent, offset, limit. parent selects whose children to list; kind filters the catalog category. To inspect an object itself, pass only its complete target, without list filters or pagination. target.kind identifies the object type. Omit unused fields; target does not accept null. Read Skill bodies with read_file using a PATH target built from skill_path.
```

**query_agents**

```text
Query agents and their delegated work. To list agents, omit target; optional fields: cursor, max_items. To list one agent's work, pass its AGENT target; optional fields: cursor, max_items. To inspect one work item, pass only its WORK target, without pagination fields. Continue a page in the same branch and, for an agent's work, with the same AGENT target. Omit unused fields; target does not accept null.
```

**query_processes**

```text
Query managed process metadata without returning logs or consuming output. To list processes, omit target; optional fields: include_running, include_finished. To inspect one process's command and current metadata, pass only its PROCESS target, without list filters. For output, use manage_process with poll or wait. Omit unused fields; target does not accept null.
```

**query_monitors**

```text
Query visible monitor registrations. Use no arguments to list registrations. To inspect one registration's state and effective monitoring policy, pass only its MONITOR target. Neither branch accepts pagination fields. This query does not register, cancel, or refresh a monitor. Omit target for the list; do not pass null.
```

**query_cells**

```text
Query cells still held by the current Host without consuming execution output. To list cells, omit target; optional fields: cursor, max_items. To inspect one cell's state and frozen source, pass its CELL target; optional fields: offset_chars, max_chars. List cursors and source offsets are separate: do not mix their fields. Continue source pages with the same CELL target. Use manage_cell with wait for execution output. Omit unused fields; target does not accept null.
```

**query_schedules**

```text
Query schedules. To list schedules, omit target; optional fields: cursor, status, session. Omit status for all statuses, or use ACTIVE or PAUSED; status does not accept null. Omit session or use null for the current session, or pass a SESSION target to select the list's session scope. Continue with next_cursor and the same filters. To inspect one schedule, pass only its SCHEDULE target, without cursor, status, or session. target does not accept null.
```

#### 3.4.4 已知 lowerer 缺陷与替代方案边界

报告发现 `_lower_schema_node` 把属性映射中的 oneOf／const／discriminator 字段名当 schema 关键字处理；当前递归实现仍需区分 schema 节点与属性名映射。这是独立实现缺陷，不能用来否定 intentional superset 的产品取舍，也不能声称此次工具改名已经修复它。本轮不顺带重构 adapter；记录离线回归，并在新增定义实际触发时先完成独立窄修复，不能绕过失败或删除合法属性发布。

内联 `request.oneOf`、保留完整 wire 分支、切换 strict、采用 $ref 压缩都不属于本轮既定方案。若最终 wire 与模型填参验证表明现行路径无法承载新合同，应先修订本节和参数矩阵，再实施一个明确的新合同；不能在实现中暗加 wrapper、provider fallback 或扩大本地合法参数集合。

## 4. query_capabilities

### 4.1 输入与选择

| 分支 | 允许输入 | 选择规则 |
|---|---|---|
| 集合／概览 | kind?、scope?、source_kind?、parent?、offset?、limit? | 原 list_capabilities；无 kind 的概览不自动展开所有 MCP 子项 |
| 精确详情 | target | 原 inspect_capability；禁止夹带 kind、parent、筛选及分页字段 |

kind 保持 `SKILL / HOOK_SOURCE / MCP_SERVER / MCP_TOOL / MCP_RESOURCE / MCP_RESOURCE_TEMPLATE / MCP_PROMPT`。PLUGIN 可作 target 或 parent，但不新增为列表 kind。

parent 保持 PLUGIN、MCP_CONNECTION、MCP_SERVER。PLUGIN 子列表允许 SKILL／HOOK_SOURCE／MCP_SERVER；MCP 父对象允许已有远端子项种类。scope、source_kind 与明确 parent 冲突时继续拒绝，不隐式换父范围。BUNDLED 来源只适用于 Skill。

### 4.2 内容与分页

列表沿现有 `items / returned_count / total_count / truncated / next_offset / completeness / diagnostics`。`total_count` 是本次观察到的行数，不能把来源 UNAVAILABLE 或未发现远端目录误报成完整零结果。

offset 从 0 开始；limit 默认 50，范围 1–200，复用既有 MCP 目录单页边界。续页使用 next_offset 并重复同一组 kind／scope／source_kind／parent；数据变化时是新的即时观察，不承诺跨页冻结快照，不新建快照或游标 registry。

详情保留各 capability 的现有字段，包括 Skill 的 skill_path／skill_root、Plugin 组件、资格和诊断。Skill 正文仍由 read_file 读取，不在能力查询中复制全文。

MCP_TOOL 分支保留完整 description／input_schema／invocation。native 与 JS 的 DIRECT 判定继续遵循各自既有 surface；JS 在冻结目录不匹配时沿 META 返回当前 cell 的调用引用。不能因为两种查询合并而把 native epoch 引用和 JS cell 引用互换。

### 4.3 MCP 调用指引

native 已列出的 direct 工具按当前路由直接调用；META 通过 `query_capabilities(target=...)` 获得实际 invocation.mode。Code Mode 的 bundled Skill 继续要求在脚本内查询 MCP 工具再按 mode 调用；本轮只替换入口名称，不删除这一先查询要求。

列表页和 MCP_TOOL schema 继续要求 FULL 交付；判定必须从旧工具名改为 `query_capabilities` 的参数分支。普通详情仍可使用既有 artifact 投影。引用准备、结果交付确认、失效和重查沿当前 owner，不新增审批或兼容机制。

## 5. query_agents

| 分支 | 输入 | 输出主体 | 分页 |
|---|---|---|---|
| 当前协作成员 | cursor?、max_items? | `agents`、next_cursor | 当前 ROOT 下按 session ID 顺序 |
| 指定 Agent | AGENT target、cursor?、max_items? | `agent`、`works`、next_cursor | 指定 Agent 内按 queue_sequence 顺序 |
| 指定 Work | WORK target | 原 `_public_work` 对象 | 不接受分页参数 |

max_items 默认 50、范围 1–50，沿当前协作单页限制。Agent 列表 cursor 为 session 位置；Work 列表 cursor 为该 Agent 内的 queue 引用。客户端把它们视为 opaque，不能跨分支、跨 Agent 或跨 ROOT 互换。AGENT 续页须继续携带同一 target；WORK 请求必须只有 target。

保留当前 ROOT 的归属过滤、仍存在的完成／空闲／排队成员，以及 Work 的 instruction、final、diagnostic、失败依赖等结果。Agent 空 Work 列表仍应验证归属；不存在与越界目标不得退回到全局搜索。此查询不开放 ROOT 的非委派请求作为 Work。

该入口名称表示协作领域，因此允许查询 WORK；不新增 `query_works`。不自动把 ROOT 保留标识加入查询：ROOT 作为消息接收者的例外仍只在原已允许工具适用。

旧结果结构和大正文既有投影方式保留，不为统一形状另包装一层 data。没有全局一致快照的承诺；已有游标校验、失效与并发变化行为不得在改名时重写。

“不可互换”是分页使用合同，不代表所有旧 cursor 都有加密的范围绑定：当前成员 cursor 是排序位置，ROOT 范围由独立过滤保证；Work cursor 的位置在指定 Agent 内解析。错误范围的 cursor 不得让查询返回范围外对象，但本轮不新增 cursor token、签名、注册表或统一的失效错误协议。

## 6. Process 查询与控制

### 6.1 exec_process

每次启动新 shell，工作目录和环境修改只作用于该次调用。command 仍是 shell 命令文本，支持已有 shell 语法；不是 argv 新协议。workdir 解析、默认 workspace、TTY、进程组、输出 artifact、取消和物理 join 均复用 TerminalManager。

沿用现有边界：command 最长 1,048,576 字符、workdir 最长 4096 字符，yield_time_ms 为 0–30000，max_output_chars 为 512–32000。这些是现有单次输入／观察界限，不是命令寿命。本轮不新增 shell、超时或命令安全策略。

### 6.2 query_processes

- 集合请求只允许 include_running／include_finished，默认均 true。沿当前 Host 的进程列表和开始时间排序，保留完整 command、cwd、status、exit_code、timed_out、stdin_closed、duration_seconds 及已有输出游标元数据。
- 精确请求只允许 PROCESS target，返回同一字段集合。继续复用 TerminalManager 的归属检查与 `_info`，允许其现有 `output.snapshot(maximum_chars=1)` 只读快照。产品边界是不返回日志正文、不消费输出，而非禁止内部读取；不为此另建元数据读取机制，也不通过 poll 或其他进程 manager 实现查询。
- 集合成功形状为 `{"status":"success","terminal_process_action":"list","processes":[...]}`，保留现有列表 carrier 字段；精确成功形状为 `{"status":"success","process":{...}}`。每项以 target 代替等价裸 process_id。
- 查询元数据可以携带当前 output_cursor／retained_from_cursor，但不得消费日志、改变其保留范围或隐式调用 poll。
- 当前进程列表是受现有物理 owner 容量／回收规则约束的完整即时列表。本轮**不增加列表分页参数**，不把输出 since_cursor 借作列表 cursor。过大结果沿既有 artifact／coverage 路径可读，不静默少列进程，也不添加总进程数限制。
- 已回收或外部进程 ID 沿原 owner 返回不可用／拒绝，不扫描系统进程表补全。

### 6.3 manage_process

动作参数按 §2.2；list 从 action 枚举删除。poll 即时取得状态与输出，wait 等物理结束或本次观察超时，不以超时杀进程。since_cursor 保留“省略／null 读取保留输出”的行为，并仍以对应 PROCESS target 限定输出流。

输出游标越过本次已观察区间，包括因响应大小省略的文本；它不是保证逐页读完所有日志的通用分页游标。保留 gap_before_output、source_coverage、truncated_by_response_bound、artifact 读取提示，不能把省略日志伪装为已完整交付。

write 原样写入 data；submit **无条件额外追加一个 LF**，不增加去重换行行为。close_stdin 沿现有 pipe 关闭／PTY EOT 行为。kill 终止命令及子进程并等待实际退出，沿既有失败和副作用结算，不把发送信号当作已经结束。

保持 wait 的 timeout_seconds=1–30、stdin 操作 yield_time_ms=0–30000 等现有边界。返回的执行／观察字段继续沿当前终端 carrier，包括 `terminal_process_action`；本轮改工具入口，不顺带重命名已有业务结果字段或内部 DTO。

## 7. Monitor 查询与控制

### 7.1 query_monitors

集合 `{}` 返回当前可见的未关闭登记，沿现有 monitor ID 排序；`{"target":MONITOR}` 返回其中一个登记的详情。两分支不接收 cursor 或分页参数，本轮沿现有物理登记容量，不创造 Monitor 历史列表。

集合保留 `status=INVENTORY`、`monitors`，每项含 target、process_target、state、observation_ordinal、pending。精确详情形状为 `{"monitor":{...}}`，在上述字段外增加实际生效配置：

| 字段 | 内容与来源 |
|---|---|
| conditions.completion | true，注册均含完成监听 |
| conditions.output | 未启用时 null；否则 min_new_output_chars、quiet_period_ms |
| conditions.heartbeat_interval_seconds | 实际配置值或 null |
| delivery | max_output_chars、minimum_progress_observation_interval_seconds |
| lifetime | maximum_duration_seconds，表示注册时配置，不是查询重置后的寿命 |

从现有 `_Registration.policy` 和登记事实生成，不能读历史注册参数冒充当前配置，也不创建 policy 副本表。查询不得重新注册、刷新过期时间、采样并消费输出或结算 pending 通知。取消／到期／关闭后不再列为可操作对象；精确查询沿 owner 返回不可用。

### 7.2 manage_monitor

只保留 register／cancel。默认完成监听、输出与 heartbeat 条件、投递间距、现有最长监控寿命均保持。register 使用 PROCESS target，结果返回 MONITOR target 与 process_target；cancel 使用 MONITOR target，不取消进程。

ROOT 与 Subagent 都有原生入口，但范围不同：ROOT 沿既有全局管理可见范围；Subagent 仅当前真实 Work 绑定的登记。注册者、接收 session、Work 与权限来自调用上下文，不允许模型自报。query 的详情分支与列表、cancel 使用同一可见性规则，不能用已知 MONITOR target 绕过范围。

Code Mode 不获得 Monitor 工具。沿原登记的准备、提交／放弃、事件投递与关闭 owner；不将监控升级成持久 job。

## 8. Cell 执行、查询与控制

### 8.1 exec_cell 与 manage_cell

exec_cell 的 code／path 恰一、yield_time_ms 及实际执行语义沿原 exec。path 使用文件 owner 解析并读取一次；Hook 修改后重新校验，随后冻结实际源码。VM、Promise、未捕获错误、在途调用、正式输出、观察超时与资源边界全部保持。

manage_cell 仅两个封闭分支：

| action | 必需字段 | 可选字段 | 结果与结算 |
|---|---|---|---|
| wait | target=CELL | yield_time_ms，默认 1000，范围 0–30000 | 原 wait_exec；一次输出观察，成功交付后确认消费 |
| stop | target=CELL | 无 | 原 stop_exec；停止后续执行／提交，保留在途作用与已接纳 Work 的真实结算 |

exec_cell／manage_cell 的业务结果以 `target:CELL` 代替原裸 cell_id；不同时输出同一身份的两个镜像。state、output、has_more、pending_calls、error 等沿原分支：wait／exec 返回输出页，stop 返回停止结果及尚待读／待结算状态，不偷偷读取输出。

同步更新现有输出页大小计算与最终载荷：Host 的 `_public_page` 和 `CodeModeObservation.public_value()` 必须使用同一 CELL target 形状，按包含 target、状态、输出及其余字段的完整实际 envelope 计算 UTF-8 字节数，不能按旧裸 cell_id 格式选取输出前缀后再增加包装。复用现有页面预算、预留、观察与交付结算链，不增加预算、确认机制或第二套分页器。输出观察仅在完整成功交付后按原 owner 确认消费；报价、序列化或交付失败均不得确认该页消费。

内部 Node IPC、`CodeModeInvocation.cell_id`、数据库来源字段可以继续叫 cell_id；值须使用同一 canonical ID，不能为 native、JS、UI 再造映射 ID。公开 tools 调用使用 target，内部 owner 可继续接收校验后的字符串。

### 8.2 query_cells 参数矩阵

| 分支 | 字段 | 精确规则 |
|---|---|---|
| 集合 | cursor?、max_items? | 无 target；max_items 为正整数，默认 50；cursor 是上页 next_cursor |
| 详情／源码页 | target=CELL、offset_chars?、max_chars? | 不接受列表 cursor／max_items；offset_chars 默认 0，max_chars 默认 20000、范围 1–32000 |

Cell 集合默认 50 是一次观察的默认请求量，不是总 Cell 数或强制最大页数。无新增 max_items 硬上限；实际返回数量受已有模型结果字节边界控制。源码字符页参数复用现有 read_artifact 的单位与请求范围，最终仍按完整 UTF-8 JSON envelope 校验字节大小，不能把字符数当作字节数。

### 8.3 集合内容与分页算法

集合只列当前调用方 ROOT 的现有 CodeModeHost 持有的对象：queued／running，以及因未交付输出、pending 调用或清理尚未完成而保留的 terminal Cell。不查询全局 Host，不枚举已释放历史，不把未接纳脚本构造成对象。

每项只含：

```json
{
  "target": {"kind":"CELL","cell_id":"cell:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "state": "running",
  "has_unread_output": false,
  "pending_calls": 1
}
```

示例 ID 仅展示 canonical 形状。生产结果由 owner 生成。

响应为 `{"cells":[...],"has_more":false,"next_cursor":null}`。按完整 canonical cell_id 的字符串顺序排列，cursor 表示排他的排序下界；续页选当前 Host 内 `cell_id > cursor` 的条目。next_cursor 为最后实际返回的 Cell ID；没有后续项时为 null。不得把未返回条目的 ID 当游标。

复用 Host 当前 inventory，只捕获本次必要的不可变查询字段；不得为了分页保存历史集合。max_items 与完整结果字节预算共同决定装入的最大完整行前缀。已有预算无法容纳一行时返回明确资源错误，不能返回空页加永不前进的 cursor。

cursor 仅是位置，不授予对象访问权，也不要求对应 Cell 仍存在；其语法按 cell canonical ID 校验。已释放边界仍能继续按字符串位置查询。真正 target 查询必须通过当前 Host 的存活与归属检查。cursor 不携带其他 Host 的对象访问能力；查询始终限定本次 Host。

这是弱一致的实时分页，不是历史快照：翻页期间新建的、排序落在边界之前的 Cell 可能要从第一页重查；被回收的 Cell 不再出现。库存静止时可完整遍历；不声称并发变化期间返回所有曾经存在的对象。无 cursor registry、generation、租约或集合指纹。

### 8.4 精确详情与实际源码

详情返回 `cell` 和 `source` 两部分：

- cell：target、state、has_unread_output、pending_calls，以及已有可用 error。只读当前已捕获状态，不调用 observe／acknowledge，不占输出消费 reservation。
- source：实际冻结源码的一页，或明确的不可用原因。只读取该源码文档，不额外展开执行输出队列、Promise 内部值、完整工具目录、权限快照或凭据配置。源码保真要求与既有凭据边界分别遵循原 owner，不引入新的内容改写规则。

成功源码页形状：

```json
{
  "cell": {
    "target": {"kind":"CELL","cell_id":"cell:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
    "state": "running",
    "has_unread_output": false,
    "pending_calls": 1
  },
  "source": {
    "status": "AVAILABLE",
    "text": "console.log('example');",
    "offset_chars": 0,
    "returned_chars": 23,
    "total_chars": 23,
    "has_more": false,
    "next_offset_chars": null
  }
}
```

位置与计数以 Unicode 字符为单位，与既有 artifact 读取一致；无行号前缀、不改换行、不修改代码。offset 等于或超过总字符数时返回空末页；负数拒绝。续页重复同一 CELL target 并复制 next_offset_chars；max_chars 可以调整。

源码是同一个不可变文档，metadata 则每次重新观察。按完整结果预算缩短本次返回的字符前缀时，必须返回精确 next_offset_chars；不能把已经缩短的内容报告成全文。source.status=UNAVAILABLE 时包含明确 reason，不返回伪造 text 或把原调用参数降级冒充源码。

读取链路必须是：

```text
当前 Host 的 CELL → source_exec_attempt_id → 该 session/attempt 已冻结的 code_source CanonicalContent
```

CodeModeHost 只负责证明本次有这个 Cell 并给出内部源执行关联；repository／既有 canonical content reader 负责按真实 session／attempt 读取 inline 或 blob、验证原有完整性。模型不提交 attempt_id，不能用伪造 CELL 直接读取任意历史记录。

冻结源码已有唯一 owner：`freeze_code_mode_source` 在实际执行前保存 Hook 后／path 读取后的文本。不得重新读取 path，不解析历史模型参数来猜源码，不新建 code history 表，也不把源码哈希用作 Cell 身份。缺少或损坏时明确不可用，保留 Cell 当前状态的可观察性。

查询开始时先从 live owner 捕获必要的只读事实；随后 Cell 可以按原生命周期释放。查询可以返回已捕获的观察及其冻结源码，但不能借此延长 Cell 寿命或保证下一次 wait 一定成功。若下一次源码续页时 Cell 已释放，明确 unavailable，不回退到无归属约束的历史检索；历史记录继续由原会话内容工具读取。

错误分层保持清楚：target 格式／分支错误在参数校验阶段拒绝；Host 不可用或目标不在当前 Host 时沿现有 `CodeModeError("unavailable", ...)` 返回整次查询失败，不泄露它是否属于别的 Host；已捕获 Cell 但源码无法读取时返回 cell 元数据和 `source={"status":"UNAVAILABLE","reason":...}`。reason 来自实际读取失败，不伪造空源码成功；资源边界沿既有 typed 错误路径，不新增一套通用错误框架。

### 8.5 生命周期保持

完成或停止不保证可以立即释放；terminal 输出成功交付、pending 清零、输出排空且 VM 已释放后，沿现有回收条件删除进程内记录。query 不设置 terminal_delivered，也不阻止回收。

观察超时不是脚本超时；新消息、普通新 turn、ROOT final、压缩不重启脚本。进入 Plan 或设置变化不追溯修改旧 Cell 的源执行快照。当前非 bypass turn 仍不提供 Cell 工具；用户 UI 的既有查看／停止能力不由模型工具可见性决定。

Host 关闭或崩溃后旧 Cell 无法恢复。本文不因为 query 能读源码就提供重放、重新执行、自动找回或持久控制句柄。

## 9. 定时计划查询与管理

query_schedules 的集合分支允许 cursor、status、session；详情只允许 SCHEDULE target。列表 status 省略表示不过滤状态；显式值只允许 ACTIVE／PAUSED，拒绝 null。session 省略／null 在**模型工具层**仍表示当前 session，不改成全用户范围。明确 SESSION target 继续按已有授权选择会话。服务内部的 None 参数不等于模型可以传入 status:null；服务／HTTP 的无 session 全局查询与用户级页面保持原有 owner，不受模型默认值影响。

列表保留当前 `tasks` 数组和 next_cursor；详情保留 model_task_payload 的完整业务值、revision、session 和诊断。不能以统一 query 为由把列表从完整值改成摘要，也不无故改 target.task_id 或数据库字段。

分页仍由 scheduling repository 按 schedule ID 排序，页条目与字节界限沿 STAGE2_LIMITS。cursor 使用 next_cursor；续页重复同一 status／session；首屏省略 status 时续页也省略，不以 null 替代。详情禁止 cursor、status、session。过滤变化须从第一页重查，不把 session-content 的自包含 cursor 规则搬过来。

manage_schedule 的 values、schedule 规则、时区、permission_mode、expected_revision、replacement 分支沿当前 requests／service。create 的 status 省略／null 默认 PAUSED，与列表的 null 合同分开；不将创建默认值套用到查询。保留模型不得保存高于当前权限的未来计划、原子 switch 和实际事务失败路径。

本轮不添加模型 run-once action，不改变 UI 立即运行、一次性计划出队删除、周期计划状态及失败不自动重建等合同。

## 10. Artifact、Memory 与 Session 的边界

| 工具 | 输入与分页 | 必须保留 |
|---|---|---|
| read_artifact | target；offset_chars 默认 0；max_chars 默认 20000、1–32000 | next_offset_chars／has_more、returned_chars、source_coverage；页面可小于请求量 |
| export_artifact | target、目的 path | 无需先 read；创建父目录，不覆盖已有文件；导出保存正文，不扩充 retained coverage |
| get_memory | MEMORY target | 已知精确 target 可直读；语句、kind、context、时间、生命周期、直接关系与用户注释 |
| search_memory | query；kind?；limit 默认 5、1–50 | kind 是偏好，可按原召回放宽；保留 retrieval_summary、filter_match、relation_warnings；不是全库分页列表 |
| search_sessions | 首次 query?／lifecycle?／limit?；续页 cursor／limit | 空 query 按活动度列会话；游标已绑定范围，不重传筛选 |
| search_session_content | 首次 SESSION、query、include_tools? 等；续页 cursor／limit | 搜索原有会话正文及按选择公开的工具内容 |
| read_session_content | 首次 SESSION／SESSION_ENTRY、direction、include_tools、limit／max_chars；续页 cursor 与页预算 | 不把自包含会话 cursor 改成需要重复 target 的列表 cursor |

Session 工具三项参数的“可省略 target 时使用当前 session”等已有默认值全部保留，以上概括不收窄原 schema。搜索与读取继续在授权范围内分页，不升级为任意执行对象全局查询。

## 11. 分页与结果交付总矩阵

| 查询／读取分支 | 翻页位置 | 续页必须带什么 | 一致性／失效语义 |
|---|---|---|---|
| query_capabilities 集合 | next_offset | 原 filters／parent + offset；limit 可调 | 当前观察排序，变化可影响页边界；保留 completeness |
| query_capabilities 详情 | 无 | target | 非分页对象详情；MCP schema FULL，普通大详情沿 artifact |
| query_agents 成员 | next_cursor（session 位置） | cursor、可调 max_items | 当前 ROOT 及调用方查询范围，session 排序；不是 Work cursor |
| query_agents AGENT | next_cursor（queue 位置） | 同一 AGENT target、cursor、可调 max_items | 同 Agent 的 queue_sequence，归属检查不省略 |
| query_agents WORK | 无 | WORK target | 单份工作；大结果沿原输出投影 |
| query_processes | 无列表分页 | filters 或 target 二选一 | 当前 Host 的已有 retained inventory，不扫描 OS 全局 |
| query_monitors | 无列表分页 | `{}` 或 target | 当前可见未关闭登记；详情不延长登记寿命 |
| query_cells 集合 | next_cursor（cell ID 排序边界） | cursor、可调 max_items | 当前 Host 弱一致页；不要求 cursor 对象仍存活 |
| query_cells 源码 | next_offset_chars | 同一 target、offset_chars、可调 max_chars | 冻结文本；Cell 回收后后续页明确不可用 |
| query_schedules 集合 | next_cursor（schedule ID） | 原 session／status + cursor；无状态过滤仍省略 status，禁止 status:null | 原 owner 的当前页面，无新快照存储 |
| query_schedules 详情 | 无 | SCHEDULE target | 完整值与 revision |
| read_artifact | next_offset_chars | 同一 ARTIFACT target、offset_chars | 已保存正文，coverage 仍可能仅 retained |
| 会话三工具 | 自包含 next_cursor | cursor + 允许的页预算 | 不重复 target、query、lifecycle、include_tools 等首请求条件 |
| manage_process poll／wait | output_cursor | 同一 PROCESS target、since_cursor | 输出观察位置，不是对象列表分页；保留 gap／省略语义 |
| exec_cell／manage_cell.wait | 无模型侧分页 cursor | 同一 CELL target继续 wait | Host 在成功交付时推进输出队列；不通过 query 读取 |

已有页面数字限制不得移植成对象总量限制。Process／Monitor 本轮没有新列表分页，工具结果过大时按既有可恢复 artifact 读取路径处理；若既有结果投影无法提供可恢复读取，实施必须暴露明确资源错误并修正同一投影链，不能静默丢行或新设总量常量。

现有 exec／wait_exec／stop_exec 成功结果的 `CODE_MODE_OUTPUT_PAGE`／FULL_REQUIRED 分类分别映射到 exec_cell、manage_cell.wait、manage_cell.stop，保留原交付要求。exec_cell 与 manage_cell.wait 的输出观察必须完整交付后才确认消费；manage_cell.stop 保持停止结果语义，不因 FULL 分类而读取或消费输出。

query_cells 的列表和源码页新增 FULL_REQUIRED 交付需求，使用既有页报价与交付机制，目的只为保证后续 cursor／offset 与实际交付一致。其新增枚举仅属于既有结果投影分类，不是 durable event／receipt。页面在接受前不能适配实际预算时明确 typed 失败，不在接受后再把页面降级为不完整预览。

## 12. 调用方、权限与 Code Mode 矩阵

下表表示工具可见范围；各 port 必须已绑定，且每次调用仍经过现有授权。Subagent 的运行前提仍是已接纳的 bypass Work。

| 目标入口 | ROOT 原生 | Subagent 原生 | Code Mode `tools.*` |
|---|---|---|---|
| query_capabilities | 原范围，非仅 bypass | 是，沿共同来源查询；MCP 运行目录仍按分配范围 | 是，ROOT＋bypass |
| query_agents | 仅 bypass | 是，bypass，限自身及已知成员 | 是，ROOT＋bypass |
| exec_process／query_processes／manage_process | 原终端权限 | 原终端权限与 Host 范围 | 是，ROOT＋bypass |
| query_monitors／manage_monitor | 原可见与管理范围 | 仅当前真实 Work 的 Monitor 范围 | 否 |
| exec_cell／query_cells／manage_cell | 仅 bypass | 否 | **否，包括 query_cells** |
| query_schedules／manage_schedule | 原定时权限与会话范围 | 否 | 否 |
| read_artifact／export_artifact | 原 artifact／文件权限 | 原范围 | 是，ROOT＋bypass |
| get_memory／search_memory | 原记忆权限 | 否 | 否 |
| 三个 Session 工具 | 原会话范围 | 否 | 原有 JS 范围，ROOT＋bypass |

query_cells 仅原生是本轮明确选择：不借增加查询扩大 JS 子集，不让一个 Cell 枚举／控制其他 Cell，也不引入递归 cell 执行。Host／UI 管理与模型工具可见性是不同入口，现有用户控制不删除。

### 12.1 逐动作权限

- query_capabilities／query_agents／query_processes／query_monitors／query_schedules／query_cells 是只读查询，但 ROOT-only／bypass／terminal access 等原限制仍有效。
- manage_process 的 poll／wait 保持原只读分类；write／submit／close_stdin／kill 保持原效果与终端授权。
- manage_cell.wait 沿原 wait_exec 的观察权限；stop 沿原 stop_exec。两者工具表面仍仅 ROOT＋bypass。
- manage_monitor register／cancel 保持原非查询授权；注册成功仍由现有 effect settlement 决定。
- manage_schedule 的所有 action 都沿原写权限及 revision 合同；query_schedules 继承原 list／get 的读权限。
- read_artifact 为读，export_artifact 为文件写；get_memory／search_memory 保持 memory_read。

不能用一个 manage 工具的静态只读标志覆盖各 action；复用当前 BuiltinActionPermissionOverride／分类链。查询入口虽然拆出，也不能绕过旧 owner 的归属与 terminal 可用性检查。active Plan 的只读限制继续生效，不因改名或新 query 工具扩大作用。

### 12.2 Native／JS 同源

允许进入 JS 的新工具使用同一 schema、参数分支、默认值、target、公共业务值及错误，无额外 `.data` 层。Code Mode 目录、桥接分派、严格 schema 校验与共享白名单同步替换；`tools.run_agent.batch`、`tools.wait_agent.works` 保持不变。

不提供旧 JS 名字的 alias，不通过 query 回传一个可绕过工具子集的任意执行函数。MCP 的元调用仍只执行所属 runtime 内真实可用的远端能力，不得用重名绕过 Cell 控制禁令。

## 13. Cell ID 统一（独立合同）

### 13.1 编码与生成

1. 在 `identity.IdentityKind` 的封闭类型集合加入 `cell`；复用 `random_id("cell")`。
2. 格式为 `cell:<26 位小写 RFC 4648 Base32>`，payload 来自现有 128-bit CSPRNG nonce。完整校验为 `^cell:[a-z2-7]{25}[aeimquy4]$`。
3. 同一源码执行两次产生两个 Cell；不按源码、路径、turn 或 attempt 派生身份，不新增源码哈希或 `hash → cell` 映射。
4. 入口用现有 `require_id(..., "cell")`，实际对象再由当前 CodeModeHost 检查。格式正确不证明存活、归属或权限。
5. Host、Node Worker IPC、内部调用来源、结果、runtime index、日志与 UI 使用同一完整值，不再生成 `cell_<uuidhex>`，不截短、不去前缀、不修正大小写。

### 13.2 target 与替换范围

新增唯一公共形状 `{"kind":"CELL","cell_id":"cell:…"}`。exec_cell 返回 target；query_cells 结果与 manage_cell 输入沿同一形状。公共模型结果不另加等价裸 cell_id；内部 IPC 保留键名 cell_id 是内部字段而非兼容镜像。

Node 协议把 ID 当 opaque string，不重新生成 ID；所有可能把 ID 插入路径、虚拟文件名、错误栈或 UI 键的地方须验证冒号可用，不能为这些消费者另设替代 ID。展示脚本来源的虚拟文件名不是本地文件路径，不因为 ID 格式变更写入新文件。

无旧值 parser fallback、别名或旧 Host 迁移。发布切换时关闭旧执行 Host，按既有生命周期结算中断；新 Host 生成新格式。旧 canonical 工具历史保存原样，不能为改名重写消息或伪造旧 Cell 仍可操作。

本章不改变 Cell 清理条件，不延长生命周期，不增加跨重启恢复，不允许持有旧 ID 自动重跑代码。

## 14. 所有权、实施落点与完整 hard cut

### 14.1 唯一 owner 与最小适配

本轮以改名、拆分／合并工具入口和调整参数／结果投影为主，沿用已有执行、校验、授权、分页和交付机制。已有能力直接复用，不因统一名称重新实现。新增代码主要集中在 Cell 列表与冻结源码查询；Process 详情复用 `_info`，Monitor 详情投影现有登记及 policy，只补必要的只读入口，不引入新的运行或存储机制。

| 领域 | 复用的真实 owner | 允许的最小新增工作 | 禁止新增 |
|---|---|---|---|
| Capability | CapabilitySourceQuery、MCP runtime／引用 owner | 合并参数分支、列表／详情投影和新名路由 | 第二目录、调用资格缓存、query 通用执行 |
| Agent／Work | CollaborationToolPort 与 session／queue／turn repository | 合并查询工具入口 | Work 状态表、复制结果、查询全局协作图 |
| Process | TerminalManager、当前 Host 身份、原输出流与 `_info` | 复用 `_info` 及其只读快照，补精确查询入口 | OS 进程扫描器、第二进程库存、输出重放、另造元数据读取机制 |
| Monitor | 当前 monitor coordinator 与 `_Registration` | 在相同 visible 规则下投影有效 policy | 登记副本、重新注册式 inspect、持久 job |
| Cell | CodeModeHost、源 attempt、canonical content／blob reader | live 查询快照、有限页面、现有源内容读取 | 持久 Cell 表、源码副本、执行恢复、cursor registry |
| Schedule | ScheduledTaskService、原 repository／接纳事务 | 分开读写入口并保持默认范围 | 第二调度器、自动补建计划、额外执行回执 |
| Artifact／Memory | 原读写端口与 owner | 公共名称替换 | 内容镜像、统一对象数据库 |

能力描述、分页和 target 构造都只是适配，不成为新 authority。新增查询读取 canonical 源码可增加窄 repository read 方法，但必须使用现有内容描述、inline／blob 读取与完整性校验；不能复制整套内容存储实现。

durable event kinds、subject slots、append guards、产品关系与 durable jobs 的允许增量全部为 **0**。不新增数据库表或列；Cell 源码和关联已经存在。若实施发现必须增加这些类别，说明与本文假设冲突，先修正规格，不以“证明查询成功”为由增加记录。

### 14.2 逐层修改矩阵

| 层 | 必须覆盖的内容 |
|---|---|
| builtin catalog／schema | 新名称、闭合查询与 action 分支、调用方、availability、function.description 紧凑分支签名、读写分类、恢复分类 |
| Provider wire | 复用既有 lowering／strict:false；canonical 合法调用不被收窄；Chat／Responses 最终序列化与模型填参验证，见 §3.4／§16.4 |
| 公开参数／target | CELL；其余复用；列表与详情互斥；ID 与分页校验 |
| 执行与权限 | 入口分派、action-aware permission、terminal access、Plan 只读、session／Work 归属 |
| Code Mode | shared tool 白名单、schema 同源、guest callable 名称、禁止递归、元调用引用范围 |
| 结果投影 | 合并后的 capability 分支 FULL 判定；exec_cell／manage_cell.wait/stop 保留既有 FULL 分类；Cell 输出页报价与实际 target 包装一致；query_cells 页面 FULL；artifact 新名与下一步调用提示 |
| 源码／repository | freeze_code_mode_source 对真实 `exec_cell` attempt 的验证、现有 code_source 内容读取；不接受旧 exec 别名 |
| Hook | canonical matcher subject 改为新工具名；现有 Bash 外部兼容 alias 跟随 exec_process；不增加旧 Pulsara 名字 alias |
| SYSTEM／Skills | ROOT 与 Subagent 的能力查询表述；Code Mode 先 query MCP 的规则；所有调用例子、artifact 建议、定时工具名 |
| 当前设计 | 更新两份 Agent／Code Mode 设计及 deferred 范围中的现行接口表、数量、CELL target 和命名；历史归档不重写 |
| UI／诊断 | 既有工具结果 renderer、控制动作、read_with、错误提示、类型和 runtime adapter；不建设 deferred 新页面 |
| 测试／dogfood | 新名、新参数、原生与 JS、负向权限、目标归属、分页、交付、清理、前缀连续性；废弃形状测试按现行语义迁移 |

Hook 用户配置中匹配旧 Pulsara 工具名的表达式需要使用新名称；本文不静默修改保存设置、不保留旧名匹配别名。规范已有的外部 Hook alias（如 Bash）继续保持其既有外部兼容含义，不能误当旧 Pulsara API 兼容而删去。远端 MCP 匹配 subject 沿原远端身份，不套用 builtin 改名表。

所有 tool_name 字符串消费者必须完成清点，尤其 SQL 中对源 exec 的检查、投影 FULL 分支、监控／终端反馈、artifact 读取建议、Code Mode 错误提示和测试 fixture。不得仅替换 descriptor 后让旧名执行分支继续存在。

### 14.3 Provider 前缀与发布

工具 schema 和 SYSTEM 只在新 cold epoch 或显式采用的 compaction successor 安装。活跃 epoch 内保持 SYSTEM／tools 字节相同，messages 只追加；不得以“统一工具名称”为新的热重建边界。

本轮按开发 hard cut 部署，不支持同一进程中旧／新工具并行服务或热升级旧 Cell。新版运行时由正常启动路径建立新 Host／新 cold 输入；不能在旧 provider tool array 下偷偷把旧调用路由到新接口。已有 canonical 历史作为历史保留，结果读取不改写原工具名。

## 15. 压缩后的运行时摘要边界

压缩 runtime handoff 的状态筛选、完整 target、PMCA 3,200-token 预算、独立完整 TODO、空 source、预算省略说明、普通环境 Cell 索引删除与 Monitor／Process 资源退役，统一由 [runtime handoff 与资源退役规格](PULSARA_RUNTIME_HANDOFF_AND_RESOURCE_RETIREMENT_IMPLEMENTATION_SPEC.zh.md) 定义。本文的查询、权限和 target 合同仍保持，query 不要求 handoff 完整列出所有对象，也不恢复已回收对象。

子代理与 Cell 侧边栏及 `open_in_pulsara` 展示接入按 [侧边栏规格](PULSARA_SUBAGENT_CELL_SIDEBAR_DESIGN_RESEARCH.zh.md) 处理。

## 16. 实施顺序与验收

### 16.1 实施顺序

1. 固定本规格的矩阵、canonical schema 分支、function.description 签名和既有 wire 投影路径；阅读兼容性报告并建立当前 durable oracle 与 prefix 测试基线，不记录代码／文档哈希。
2. 增加 CELL canonical kind 与公开 target；完成 Host／IPC／native 消费者一致切换。
3. 在既有 owner 上实现窄查询能力：Process metadata、Monitor policy、Cell inventory／源码页；不先做泛型查询框架。
4. 原生 query／manage／exec 入口及权限、投影一次性替换；同步 JS 目录、MCP 查询引用与 Hook 名称。
5. 同步 SYSTEM、bundled Skills、当前设计及现有 UI 消费者；删除被替代的公共入口和兼容分支。
6. 执行下列验收；若有具体失败再扩测，摘要变更仍遵循其独立规格。

### 16.2 逐组测试矩阵

| 测试组 | 必须证明的行为 |
|---|---|
| 名称与 schema | §2 的每个旧入口／action均有唯一新去向；旧名不可调用；互斥字段／错误target／额外键被拒绝；没有旧 alias |
| Provider schema／填参 | §3.4 的 canonical／wire 分层；新增分支合法调用可表达、跨分支字段本地拒绝；按 §16.4 聚焦验证合并后的描述与填参 |
| Capability | 空概览、父过滤、精确详情、来源不可用、MCP 未发现、分页重复筛选；native DIRECT／META 和 JS 冻结目录变化；schema 与调用引用完整交付 |
| Agent | ROOT 成员分页、AGENT Work 分页、WORK 无分页；不串 cursor、scope或Agent；已完成和队列中对象可查；已知target无需先枚举 |
| Process | 新 shell、TTY、输入与观察默认值；列表／详情复用 `_info`，允许其只读快照但不返回或消费日志；列表保留 terminal_process_action；poll/wait游标与coverage；write/submit/EOF/kill及取消结算不变 |
| Monitor | ROOT与当前Subagent Work隔离；详情准确投影effective policy；query不注册、不改过期、不消费pending；cancel不停止进程；关闭后不可操作 |
| Cell ID | canonical格式及padding bits；错误kind/旧hex/大小写/截短拒绝；同脚本两次不同ID；Host／Node／输出／UI同值，无映射表 |
| Cell 输出交付 | 新 target 包装计入完整页预算；近字节边界页报价与实际序列化一致；exec_cell／manage_cell.wait/stop 保留既有 FULL 分类；输出页完整成功交付才确认消费，失败不确认；stop不消费输出 |
| Cell 列表 | 超过默认50条仍可续页；字节预算缩页不漏游标；静止库存完整遍历；边界对象回收后可续；并发新增的弱一致语义；只见当前Host |
| Cell 源码 | inline与blob；path运行后文件修改仍返回冻结源码；Hook后实际代码；Unicode及跨页重组准确；查询不消费输出；缺失／损坏明确不可用 |
| Cell 生命周期 | query与wait并存不抢消费owner；终态但pending/输出未清仍可查；回收后unavailable；stop不取消已接纳Work；没有JS递归入口 |
| Schedule | 模型默认当前session、显式session、list/get完整值、分页filter；列表status:null拒绝，create的status省略／null默认PAUSED；修订冲突、原子switch、无权限升级；不增加模型run-once |
| Artifact／Memory／Session | 改名后原分页／coverage／导出写权限、搜索放宽与精确get保持；Session自包含cursor规则不受影响 |
| 权限与前缀 | §12的原生／JS／Subagent矩阵；active Plan写入仍拒绝；冷epoch／successor新定义；已有epoch不改前缀；Cell沿原快照 |
| Hook／投影／UI | 新canonical tool subject及Bash alias；不执行旧名；真实输出完整性；现有renderer和控制可用，无新增页面 |
| Subtraction | durable oracle各类增量0；无新寿命总cap、Cell表、源码库、registry、hash字段或恢复链 |

测试必须覆盖真实 dispatch、schema／permission 分类、结果交付及 owner，不只断言字符串包含新名称。按现行 API 迁移旧测试，保留其有效语义；不添加 skip／xfail，不降低断言以获得通过。

### 16.3 实际集成与 dogfood

先跑上述变更对应的聚焦测试；原有 Code Mode、协作、终端、能力、压缩前缀与 Plan 相关回归按实际触及范围运行。新增源码查询要覆盖大内容分页、晚到结果和释放竞争，不要求新增持久故障记录。

发布激活前至少完成一次 ROOT＋bypass 的真实 provider 连贯场景：模型使用新 capability／agent query，执行可跨观察等待的 Cell 和 Process，按target查询真实作用、继续等待，再停止一个尚在运行的对象；检查新入口选择、实际请求／结果和无重复执行。MCP场景只使用已配置且适合只读验证的连接；缺少必要配置应明确报告该项未验证，不伪造连接或填充凭据。

真实 provider 使用保存的生产配置，经 LocalSettingsStore 与 require_pulsara_home 读取，不更改设置、不导出秘密。需要隔离时复用只读设置注入和经确认的可丢弃本地数据库。证据保留实际提示、响应、工具参数与结果，只去除真实凭据。未完成该场景时不宣称 provider 侧接口激活已验证。

本轮已完成本规格要求的实现与验收核对，实际回归、dogfood 结果及未测边界以 [实施记录](output/tool-query-cell-unification-20261030/IMPLEMENTATION.zh.md) 及其证据链接为准。

### 16.4 Provider schema 专项验收

沿用已有 target 合同、兼容性报告和 target 实验的设计依据，验收聚焦本轮新增差异：查询入口合并后的分支说明与参数边界、新增详情查询、Cell target 及结果交付。没有改变的对象身份、管理动作和 provider 适配不重新组织全套模型实验；已有回归按受影响链路继续运行。旧证据也不被表述为新合并接口已经通过验证。

| 验收层 | 必须检查的内容 |
|---|---|
| 本地分支与业务校验 | 新增／合并分支的必填、额外键、target 类型、null、数值类型和默认值符合本章合同；canonical 与 parser 在这些结构要求上一致。既有 ID 格式、路径、归属、权限和业务规则校验可严于 schema，不要求二者接受集合完全相同 |
| 离线 wire | 受影响定义通过现有 Chat／Responses 序列化；合法 canonical 样例仍通过 wire；保持 strict:false、内联 target 和既有 lowering，不增加 wrapper 或别名 |
| 聚焦模型填参 | 使用最终 function.description 和 wire 定义，检查列表／详情切换、Capability parent／target、AGENT／WORK 分页和 Cell 列表／源码页参数。可采用模拟工具避免管理副作用；不要求每个旧动作、对象类型、模型与协议组成全组合矩阵 |
| 代表性实际链路 | 与 §16.3 的连贯 dogfood 合并安排，使用真实可见工具和本地校验，检查新 target 的查询与复用、结果交付及受影响 owner；不另设一套完整工具集逐分支实验 |
| Prefix／成本 | 按原前缀回归核对新定义安装边界；整串计量受影响工具定义。真实 usage 随实际验证记录，不以工具数下降推断 token 下降，也不增加固定 token 门槛 |

聚焦用例覆盖新增参数边界即可：详情不得夹带列表过滤；WORK 不得带 cursor／max_items；Cell 源码页必须带 CELL target 且不得混入列表分页字段；Schedule 列表拒绝 status:null，而 session:null 和 create.status:null 保持原含义。默认值只按选定分支应用，不把列表默认字段补进详情请求。对非法组合，校验返回可修正错误，不静默丢字段执行。

真实模型验证使用可用的保存连接与实际协议；历史 Qwen／DeepSeek 结果作为背景基线，不要求原实验全部重跑。先确定代表性意图和预期，再验证实际参数生成与结果使用；只有具体失败或未解决的差异才需要增加样例或对照目标。记录实际模型、协议、工具定义及请求设置，未测范围不宣称已覆盖。

分别记录接口接受、参数校验与任务结果，保留原始失败和后续修正。模型先查列表再按 target 查详情，是允许的正常路径；偶发误查或修正不作为接口设计失败，也不设零错误门槛。若持续出现同一种混参，优先核对函数描述与实际 schema 是否一致；只有具体不可用问题才触发进一步设计调整。

HTTP 200 + response.failed 不能记为成功，预算耗尽或 thinking／tool_choice 冲突不混算为 schema 拒绝。保持已有配置与原始证据，不通过隐式切换模式、放宽 parser 或覆盖失败获得通过。本轮不重跑内嵌 request 等替代方案选型；保存配置、凭据与证据处理沿 §16.3。

## 17. 本轮代码核查依据

下表是实施导航，不是额外 authority，也不是内容哈希清单。实现时以目标合同与实际 owner 对齐；行号变化不改变要求。

| 领域 | 当前代码入口 |
|---|---|
| 全部descriptor、权限与可见性 | [builtin_catalog.py](src/pulsara_agent/capability/builtin_catalog.py)、[tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py) |
| Provider schema 与兼容性证据 | [function_tools.py](src/pulsara_agent/llm/adapters/openai/function_tools.py)、[参数与默认值合同测试](tests/test_builtin_default_null_contracts.py)、[兼容性报告](output/tool-schema-compatibility-20261026/REPORT.zh.md) |
| Capability查询与分页 | [source_query.py](src/pulsara_agent/capability/source_query.py)、[capability_query.py](src/pulsara_agent/conversation_kernel/capability_query.py) |
| Agent／Work查询 | [collaboration_contracts.py](src/pulsara_agent/conversation_kernel/subagents/collaboration_contracts.py)、[collaboration_api.py](src/pulsara_agent/conversation_kernel/subagents/collaboration_api.py)、[subagents repository](src/pulsara_agent/conversation_kernel/_repository/subagents.py) |
| Process与Monitor | [terminal.py](src/pulsara_agent/ports/terminal.py)、[manager.py](src/pulsara_agent/terminal_process/manager.py)、[models.py](src/pulsara_agent/terminal_process/models.py)、[monitor.py](src/pulsara_agent/terminal_process/monitor.py) |
| Cell与源码 | [host.py](src/pulsara_agent/code_mode/host.py)、[contracts.py](src/pulsara_agent/code_mode/contracts.py)、[tool_execution.py](src/pulsara_agent/conversation_kernel/tool_execution.py)、[tools repository](src/pulsara_agent/conversation_kernel/_repository/tools.py)、[reader.py](src/pulsara_agent/conversation_kernel/reader.py) |
| 统一身份 | [identity.py](src/pulsara_agent/identity.py) |
| JS目录与结果 | [tool_surface.py](src/pulsara_agent/conversation_kernel/tool_surface.py)、[tool_result_projection.py](src/pulsara_agent/primitives/tool_result_projection.py) |
| 定时计划 | [requests.py](src/pulsara_agent/scheduling/requests.py)、[service.py](src/pulsara_agent/scheduling/service.py)、[scheduling repository](src/pulsara_agent/conversation_kernel/_repository/scheduling.py) |
| 会话内容 | [session_content.py](src/pulsara_agent/ports/session_content.py) |
| Hook与使用指引 | [matcher.py](src/pulsara_agent/hooks/matcher.py)、[system_prompt.py](src/pulsara_agent/ports/system_prompt.py)、[pulsara-code-mode](src/pulsara_agent/bundled_skills/pulsara-code-mode/SKILL.md) |
| Deferred摘要 | [runtime_handoff.py](src/pulsara_agent/conversation_kernel/compaction/runtime_handoff.py)、[context_sources.py](src/pulsara_agent/conversation_kernel/context_sources.py) |
