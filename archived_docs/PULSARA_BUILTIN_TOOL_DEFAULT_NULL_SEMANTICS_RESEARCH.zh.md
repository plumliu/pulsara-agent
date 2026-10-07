# Pulsara 内置工具的省略、默认值与 null 语义调研

- 调研日期：2026-10-07
- 调研基线：`main`，`997a5281`
- 状态：第 8.2 节获批缺口已实施，本地回归和 Chat 真实调用通过；Responses 严格复验仍有模型填参失败，详见第 9.3 节。启发式 token 前后统计见第 9.2 节。
- 范围：当前 canonical catalog 的全部 41 个内置工具，包括相关嵌套参数、执行解析和 provider 投影。
- 不在范围内：第三方 MCP 工具的参数契约。三个内置 MCP 包装入口仍纳入检查，但不改写其承载的第三方参数。

## 1. 起因与结论

实际会话中，模型调用 `scheduled_tasks` 的 `list` action 时，将可省略的 `session_id` 显式填写为 `null`，被参数校验拒绝；省略该字段后调用成功。

用户提出：默认行为是否应该通过显式 `null` 表达，并检查其他内置工具是否存在同类问题。随后明确要求先完整调研、再商议，不直接修改代码。

调研阶段由三个 GPT-6.1 Sol / high 子代理分批核查 14、14、13 个工具，主代理补查通用校验与 provider 投影。当时没有修改生产代码、配置或数据库，没有调用有副作用的工具。仓库根 `.venv` 用于只读代码检查和纯 schema/解析探针；后续获批实施另记于第 9 节。

结论不能压缩成“optional 全部 nullable”：

1. 默认行为优先通过省略字段表达；有具体收益时，可以选择性支持 `null` 别名，不强制模型填满所有字段。
2. 必填身份、内容、完整值对象，仍应要求具体值。
3. 未选 action/kind/mode 的字段，应省略；不能因 provider 展平后出现了该字段就填写 `null`。
4. 现有 `null`、空字符串、空数组、空对象有时表达不同操作，必须保留。
5. schema、实际解析、工具说明之间存在缺口，需要与默认语义一起讨论。

第 3–6 节保留 `997a5281` 的调研基线，描述修复前的行为与证据，不作为实施后契约。第 9 节列明本轮已实施的变化；其余工具与参数保持基线契约。

后续 GPT-6 Astra / xhigh 独立审阅建议将初步提案收窄为上述方向：模型统一优先省略默认字段，只在有具体收益时选择性支持 `null` 别名，不将所有纯默认字段统一 nullable 作为实施目标。主代理接受这一方向，用户随后批准第 8.2 节全部缺口。其他“可讨论 null”的候选不在本轮范围内。

## 2. 判断依据与术语

### 2.1 以实际生产调用契约为准

生产工具调用先经 canonical JSON Schema 校验，再进入具体 owner。因此，某个孤立 Python parser 接受 `None`，不代表模型可以合法填写 JSON `null`。

- 通用 schema gate：[tool_runtime.py](../src/pulsara_agent/conversation_kernel/tool_runtime.py)，约 1295、1867 行。
- 通用数字/布尔解析：[schemas.py](../src/pulsara_agent/tools/builtins/schemas.py)，约 38、59 行。`args.get(name, default)` 只处理字段缺失，显式 `None` 仍会触发类型错误。
- 内置目录：[builtin_catalog.py](../src/pulsara_agent/capability/builtin_catalog.py)。

### 2.2 四种经常被混淆的情况

| 情况 | 例子 | 不能推导出的结论 |
|---|---|---|
| 缺失时采用固定默认 | `read_file.limit` 省略后为 2000 | 不代表当前允许 `limit:null` |
| 缺失时没有附加条件 | 搜索省略 `file_glob` | 不代表存在一个默认 glob |
| 缺失时选择另一条产品路径 | 省略 MCP `config` 打开编辑器 | 不代表 null 只是默认占位 |
| null 有独立意义 | `overlay:null` 清除覆盖 | 不能把 null 删除后继续执行 |

“可选字段”也可能在特定 action、kind 或查询阶段下必填。例如内容搜索首页必须有 `query`，但游标续页不允许带它。

### 2.3 模型填写指导不是执行默认

定时任务的“新任务使用 Asia/Shanghai”、权限采用当前 preset、名称由模型拟定，属于模型填写指导。create/update 的完整 `values` 仍要求这些字段出现，runtime 不会因缺失或 null 自动补齐。

## 3. 调研基线：全部 41 个工具的语义

表中未注明允许 null 的声明字段，当前均不能显式填 null。结构化数据内部的任意 JSON null、native 配置 owner 自己声明的 nullable 字段，不受此概括影响。

### 3.1 文件、产物、检索与能力查询：14 个

| 工具 | 当前省略/default | 当前 null 与不可省略边界 | 建议方向，尚未实施 |
|---|---|---|---|
| `artifact_export` | 无默认参数 | `artifact_id,path` 必填非空；省略/null 都拒绝；新建目标，不覆盖已有文件 | 保持完整输入 |
| `artifact_read` | `offset_chars=0`；`max_chars=20000` | `artifact_id` 必填；max_chars 为 1–32000；null 拒绝 | 分页预算可讨论 null=省略 |
| `read_file` | `offset=1`；`limit=2000` | `path` 必填；limit 为 1–2000；null 拒绝 | 分页参数可讨论 null=省略 |
| `find_files` | `path="."`；`limit=50`；`offset=0` | `glob` 必填非空；null 拒绝；offset 续页需保持相同查询 | 默认目录和分页可讨论 null=省略 |
| `search_content` | `path="."`；`limit=50`；`offset=0`；`output_mode=content`；省略 file_glob 不加文件过滤 | `pattern` 必填非空；null 拒绝；分页保持查询字段 | 区分固定默认和不过滤，两者可讨论接受 null |
| `edit_file` | 无默认参数 | `path,base_revision,operations` 必填；操作数组非空；所有操作字段按精确分支校验 | 不默认化身份、内容或操作；保留空字符串语义 |
| `write_file` | 无默认参数 | `path,content` 必填；content="" 合法，content:null 非法；只创建新文件 | 保持完整输入 |
| `view_image` | 无默认来源 | 必须且只能提供有效 `path` 或 `image_ref`；另一字段当前必须省略，不能 null | 不强制填写未选来源；若讨论另一来源 null=缺席，需单独规定 XOR |
| `visualization_render` | `review=false` | 必须且只能选 path/visualization_ref；生产 schema 拒绝全部字段的 null | review 可讨论 null=默认；来源边界单独处理 |
| `search_sessions` | `query=""`；`lifecycle=ALL`；`limit=20`；无 cursor 为新查询 | null 拒绝；续页仅 cursor 和可选 limit，不能重带 query/lifecycle | 默认、无关键词和新查询可讨论 null；游标规则必须同时明确 |
| `search_session_content` | 当前会话；`include_tools=false`；`limit=20`；无 cursor 为新查询 | 首页 query 实际必填且非空白；null 拒绝；续页仅 cursor/limit | 默认会话、开关、预算可讨论 null；首页 query 不默认化 |
| `read_session_content` | 当前会话；include_tools=false；limit=20；max_chars=20000；无 entry_id 表示不设锚点 | null 拒绝；direction 根据有效 entry_id 条件推导；续页仅 cursor/limit/max_chars | 保留条件默认与游标冻结；不机械替换字段存在性检查 |
| `list_capabilities` | limit=50；offset=0；未提供过滤器按当前可观察范围列出 | kind/scope/source_kind/parent 有组合约束；null 拒绝 | 分页可讨论 null；过滤器需明确无筛选及 parent 依赖语义 |
| `inspect_capability` | 无默认参数 | target 必填封闭分支对象；本分支身份字段必填；其他分支字段禁止；null 拒绝 | 保持精确身份输入 |

补充边界：

- `artifact_read` 使用 artifact_id + offset_chars 续读，不是 cursor-only。
- `find_files/search_content` 使用 offset 分页，后续调用保持查询条件。
- `search_sessions.query` 可以为空字符串；内容搜索首页 query 不可以为空白。
- `read_session_content.direction`：有效 entry_id 存在时省略 direction 得到 newer；没有锚点时得到 older，从最新消息开始。
- 三个会话查询工具有有效 cursor 时，其他查询字段即使与旧值相同也不允许重带。若未来允许 null=缺席，必须明确 null 占位是否算“携带条件”。
- visualization 的孤立 source parser 可以接受一个有效来源配另一个 null，但生产 schema 先拒绝；不能将孤立 parser 的宽松当作公开契约。

`edit_file.operations[]` 的精确分支：

| kind | 必填字段 | 其他边界 |
|---|---|---|
| replace_lines | kind/start_line/end_line/lines | lines 非空；元素可以是空字符串空行，但不能含 CR/LF/NUL |
| delete_lines | kind/start_line/end_line | 不允许附 lines |
| insert_before、insert_after | kind/line/lines | 不允许带其他分支行号字段 |
| replace_file | kind/content | 必须是唯一操作；content="" 合法清空 |

本分支必填字段不能省略或 null，其他分支字段不能出现。区间、冲突和已阅读观察约束继续由执行 owner 校验。

能力 parent/target 的身份分支：

- PLUGIN：kind/scope/plugin_id。
- 配置 MCP_SERVER LOCAL：kind/scope/source_kind/server_id；PLUGIN 再需 plugin_id。
- runtime MCP_SERVER：kind/runtime_server_id，不能混配置身份字段。
- inspect HOOK_SOURCE：LOCAL 为 kind/scope/source_kind；PLUGIN 再需 plugin_id。
- inspect SKILL：kind/skill_path，实际要求绝对路径。
- inspect MCP_TOOL/MCP_RESOURCE/MCP_RESOURCE_TEMPLATE/MCP_PROMPT：kind/server_id，加 tool_name/uri/uri_template/name 对应字段。
- 每个身份分支的字段完整且非 null，其他分支字段省略。list parent 与额外 kind/scope/source_kind 必须一致。

依据：[artifact.py](../src/pulsara_agent/tools/builtins/artifact.py)、[filesystem.py](../src/pulsara_agent/tools/builtins/filesystem.py)、[visualization_source.py](../src/pulsara_agent/model_input/visualization_source.py)、[session_content.py](../src/pulsara_agent/ports/session_content.py)、[查询执行](../src/pulsara_agent/conversation_kernel/session_content.py)、[source_query.py](../src/pulsara_agent/capability/source_query.py)、[capability_query.py](../src/pulsara_agent/conversation_kernel/capability_query.py)。

### 3.2 规划、子代理与终端：14 个

| 工具 | 当前省略/default | 当前 null 与不可省略边界 | 建议方向，尚未实施 |
|---|---|---|---|
| `ask_plan_question` | options=[]；选项 description=""、recommended=false | question/allow_free_text 必填；option.label 必填；null 拒绝；无选项时必须 allow_free_text=true | 可讨论普通可选字段 null；保留问题和选项约束 |
| `enter_plan` | reason="" | reason:null 拒绝；无必填参数 | 可讨论 reason:null=无说明 |
| `exit_plan` | 无 summary | plan 必填非空；summary:null 生产拒绝，孤立 owner 接受为无摘要 | 可讨论 summary:null=缺席 |
| `todo` | 无可省略字段 | items 必填；items=[] 清空；每项 text/status 必填；null 拒绝 | 不将 null 用作清空 |
| `spawn_agent` | task_name 无值；profile=general_worker；context=none；model 继承父目标；material_task_ids=[] | task 必填；全部声明字段当前非 nullable；嵌套选择见下文 | 普通可选标签/材料可讨论 null；模型和上下文分支单独处理 |
| `create_agent_tasks` | 每项 task_key/label/display_role 无值；profile/context/model 同单个 spawn；depends_on/material_task_ids=[] | tasks 必填且非空；每项 task 必填；声明字段非 nullable | 保留批次完整输入；不将无依赖误当更新清空 |
| `list_agents` | max_items=50；include_dependencies=true；无 cursor 为首页 | null 生产拒绝；孤立 owner 接受 cursor:null；续页保持分页配置 | 可讨论默认分页和开关 null |
| `list_agent_models` | 输入 {} | 额外字段禁止 | 无需改 |
| `send_agent_message` | 无默认 | task_id/message 必填非空，null 拒绝 | 保持完整输入 |
| `stop_agent` | 无 reason | task_id 必填；reason:null 生产拒绝；reason 当前被校验但未用于改变取消执行 | 可讨论 reason:null=无说明，不附加新取消语义 |
| `wait_agent` | 无指定目标；timeout_seconds=30；有目标时默认 settle=all | task_ids=[] 非法；无目标时不能带 settle；null 生产拒绝 | 目标与 settle 条件必须明确，不能只放宽 schema |
| `report_agent_result` | 无 data/output_preview；diagnostics=[] | summary 必填；顶层 null 拒绝；data={} 是显式结构化结果；对象内部 JSON null 合法 | 可讨论预览/诊断缺席；保留 data 的 presence 语义 |
| `terminal` | workdir=工作目录；yield_time_ms=10000；tty=false；max_output_chars=32000 | command 必填非空；workdir:null 已合法且等同省略；其他默认字段 null 拒绝 | 保留已有 nullable；数字和开关可讨论 null=默认 |
| `terminal_process` | 依 action 默认 | 只有 poll/wait 的 since_cursor:null 已合法；其他不适用字段即使 null 也拒绝 | 保留闭合 action，不强制全字段占位 |

`terminal_process` 各 action：

| action | 必填 | 可省略/default |
|---|---|---|
| list | action | include_running=true；include_finished=true |
| poll | action/process_id | since_cursor=null；max_output_chars=32000 |
| wait | action/process_id | since_cursor=null；timeout_seconds=30；max_output_chars=32000 |
| write、submit | action/process_id/data | yield_time_ms=1000 |
| close_stdin、kill | action/process_id | 无 |

write/submit 的 data="" 合法；submit 仍会发送换行。data:null 和省略不合法。list 不接受 process_id，poll 不接受 timeout/data 等其他分支字段，即使其值为 null。

子代理嵌套参数：

1. context 出现时必须有 mode。none 不需要 turns/task_id；last_n 必须有 turns；worker_history 必须有 task_id。当前 schema 未完整表达这些条件，owner 会补充拒绝错误组合。
2. model 出现时 connection_id 必填。model 整体省略表示继承父目标；指定 connection 后省略 reasoning，表示采用新目标的默认 reasoning，二者是不同默认路径。
3. reasoning 出现时，真实 codec 要求精确变体：effort 为 kind/value；toggle 为 kind/enabled；budget_tokens 为 kind/tokens。其他变体字段禁止。当前 schema 没有完整表达条件必填及互斥。
4. 显式指定 model.connection_id 时，省略 reasoning 才走该目标的默认 reconcile；model 整体省略仍继承父目标，不走这一默认选择路径。reasoning 显式 null 当前被公开 schema 拒绝；在底层表示没有选择并走精确 validate，对 selectable 模型可能失败，不能作为默认别名。
5. effort.value=null 是可能的真实档位：模型清单可能返回它，codec 和目标校验支持它；但 spawn/batch schema 的 value 只允许 string。属于独立的 schema 可达性缺口，不是默认值问题。

空数组必须保留不同含义：todo.items=[] 清空；batch.tasks=[] 非法；wait.task_ids=[] 非法；depends_on/material_task_ids/diagnostics 的 [] 与省略同义。

依据：[plan_workflow.py](../src/pulsara_agent/primitives/plan_workflow.py)、[todo.py](../src/pulsara_agent/tools/builtins/todo.py)、[subagent.py](../src/pulsara_agent/conversation_kernel/subagent.py)、[launch.py](../src/pulsara_agent/conversation_kernel/subagents/launch.py)、[model_connections.py](../src/pulsara_agent/llm/model_connections.py)、[model_target.py](../src/pulsara_agent/llm/model_target.py)、[terminal.py](../src/pulsara_agent/ports/terminal.py)。

### 3.3 管理、记忆、监控及 MCP 包装：13 个

| 工具 | 当前省略/default | 当前 null 与不可省略边界 | 建议方向，尚未实施 |
|---|---|---|---|
| `manage_capability` | 依 action 打开编辑器、沿用源元数据、补检查 guard，或用安装默认值 | action/scope 必填；仅 overlay/expected_overlay 顶层公开 nullable 且有独立语义；不适用字段禁止 | 保留业务 null，不做通用删除 |
| `reload_capabilities` | 输入 {} | 额外字段禁止 | 无需改 |
| `reload_hooks` | 输入 {} | 额外字段禁止 | 无需改 |
| `scheduled_tasks` | list/create/run_now 默认当前会话；list 无 cursor 为第一页，无 status 不筛选 | 全部字段当前非 nullable；values 完整；按 action 条件必填/禁止 | 默认字段可讨论 null，但必须在工具边界解释，不能直接透传 |
| `remember` | based_on_memory_ids=[] | statement/context_target/kind 必填；依赖列表 null 生产拒绝，孤立 owner 接受为空 | 可讨论依赖列表 null；必填类型/范围不默认化 |
| `memory_search` | kind 无偏好；limit=5 | query 必填；kind/limit:null 生产拒绝；孤立 owner 仅对 kind 接受 None | 可讨论无偏好与预算 null，并同步唯一解析边界 |
| `memory_get` | 无默认 | memory_id 必填非空 | 无需改 |
| `memory_explain` | 无默认 | memory_id 必填非空 | 无需改 |
| `mark_memory_relation` | 无默认 | source_memory_id/target_memory_id/relation_kind 必填；关系明确为 CONTRADICTS 或 SUPERSEDES | 保持完整输入 |
| `terminal_monitor` | register 默认仅完成通知，采用默认 delivery/lifetime | 外层三个对象不能 null；内部 output/heartbeat nullable 表示禁用 | 内部 null 保留；外层对象和普通默认数字可另行讨论 |
| `get_mcp_prompt` | 不传 arguments；或显式 {} | server_id/prompt_name 必填；arguments:null 生产拒绝，孤立 owner 传 SDK None | 可讨论包装层 arguments:null=缺席；不改 prompt 内部声明 |
| `read_mcp_resource` | 无默认 | server_id/uri 必填；URI 必须匹配冻结 discovery | 无需改 |
| `use_new_mcp_tool` | 无默认 | tool_ref/arguments 必填；无参数工具也使用 {}；内部交给第三方 schema | 不改写第三方 arguments 中的 null |

## 4. 调研基线：管理工具的完整边界

### 4.1 manage_capability 的 15 个 action

以下均额外要求 action/scope；列出的可省略字段不自动等于 nullable。

| action | 必填 | 可省略及约束 |
|---|---|---|
| INSTALL_LOOSE_SKILL | source_path | name/description 沿用源元数据 |
| SET_LOOSE_SKILL_ENABLED | skill_path/enabled | 无 |
| REMOVE_LOOSE_SKILL | skill_path | 无 |
| TRUST_HOOK_SOURCE、REVOKE_HOOK_TRUST | source_kind | PLUGIN 时 plugin_id 条件必填；LOCAL 时禁止它 |
| SET_HOOK_SOURCE_ENABLED | source_kind/enabled | 同上 |
| ADD_LOCAL_MCP | server_id | config 省略打开编辑器 |
| UPDATE_LOCAL_MCP | server_id | config 省略打开预填编辑器；expected_identity 可由检查补全 |
| REMOVE_LOCAL_MCP | server_id | expected_identity 可省略 |
| INSTALL_PLUGIN | source_path | replace=false；source_format=native |
| SET_PLUGIN_ENABLED | plugin_id/enabled | expected_package_install_id 可省略 |
| REMOVE_PLUGIN | plugin_id | expected_package_install_id 可省略 |
| CONFIGURE_PLUGIN_MCP_CONNECTION | plugin_id/server_id | overlay/expected_overlay/expected_package_install_id 可省略 |
| AUTHORIZE_MCP、CLEAR_MCP_AUTHORIZATION | server_id | 无 plugin_id 为独立 MCP；有 plugin_id 为插件 MCP；相应 guard 不能混用 |

| 字段 | 省略 | 显式 null |
|---|---|---|
| overlay | 打开预填编辑器 | 清除实例覆盖，恢复包/输入默认 |
| expected_overlay | 由当前 inspection 冻结 guard | 断言没有现有覆盖 |

对其他 guard，省略让 owner 补全，不代表可以填 null。任何 action 不适用的字段，即使 null，也应保持拒绝。

### 4.2 native MCP 配置与插件 overlay

它们是 Pulsara 自己的配置契约，并非第三方 tool arguments，但也不能套用递归 null 归一。

- config 对象替换完整 native entry，不是 patch；外层 config 省略选择编辑器。
- native entry 对显示名称、enabled、required、scope policy、并发能力、超时、目录刷新等有各自默认。大部分字段显式 null 仍是类型错误，不能因有默认就假定 nullable。
- stdio 的 args/env/secret_env 可省略为空集合；cwd 省略/null 使用默认目录。
- auth 省略/null 表示 NoAuth；OAuth 的若干可选字段本来允许无值，redirect_uri 有自己的默认 callback。
- 插件 overlay object 要求 7 个键完整出现：local_server_id/transport_kind/endpoint/public_headers/environment/secret_environment/auth。
- overlay.endpoint:null 表示沿用连接输入默认 endpoint（若存在），否则沿用包 endpoint。overlay.auth:null 在解析层得到 NoAuth；但插件若声明了连接输入默认值，后续组合会沿用默认认证，因此不能直接称为最终连接无认证。空 overlay 对象不等于清除覆盖。
- stdio/HTTP 各有字段互斥约束，不因填 null 自动满足。

后续若纳入配置内部的默认语义整理，须由现有 native config 和 overlay owner 逐字段定义，不由 generic tool adapter 递归删值。

依据：[management_intent.py](../src/pulsara_agent/capability/management_intent.py)，约 58、166、242、289 行；[capability_management.py](../src/pulsara_agent/conversation_kernel/capability_management.py)；[mcp_config.py](../src/pulsara_agent/mcp_config.py)，约 728、941 行；[mcp_connection.py](../src/pulsara_agent/plugins/mcp_connection.py)，约 208 行。

### 4.3 scheduled_tasks 的 8 个 action

| action | 必填字段，除 action 外 | 可省略字段 |
|---|---|---|
| list | 无 | cursor/status/session_id |
| get | task_id | 无 |
| create | values | session_id |
| update | task_id/expected_revision/values | 无 |
| pause、resume、delete | task_id/expected_revision | 无 |
| run_now | task_id/expected_revision/client_command_id/request_at_utc | session_id |

所有非本分支字段禁止；当前全部字段非 nullable。重试 run_now 要保留原 command identity 和 request instant。

values 必须完整提供 name/prompt/timezone/permission_mode/schedule；没有 patch 或 runtime 填写默认。

schedule 必须有 contract=`scheduled-rule:v1` 与 kind，并完整提供对应字段：

- once：run_at_utc。
- interval：anchor_at_utc/seconds。
- daily：start_date/time。
- weekly：start_date/time/weekdays。
- monthly：start_date/time/day。

其他 kind 的字段禁止，必填字段不接受 null。

**放宽校验后若直接透传 null，会改变查询范围。** 只放宽 schema 时，现有 `validate_action` 仍拒绝 session_id:null，不会立即扩大查询范围。若这层校验也被放宽，当前 invoke 的 `setdefault("session_id", default_session_id)` 不会补全显式 null，而底层列表把 session_id=None 解释为不按会话过滤，扩大到调用者 domain 内全部会话。因此若采用 null=当前会话，必须在工具调用边界解释，不能改变底层/HTTP已有的 None 范围语义。

**续页须重复原筛选条件。** `scheduled_tasks.list` 的 cursor 只是最后一条任务 ID，不包含 status/session_id；继续分页时须保持原筛选条件。省略 status 会失去状态筛选，省略原来指定的其他会话 session_id 会回到当前会话。它不同于三个会话查询工具的 cursor-only 契约，不能套用同一条分页说明。

依据：[requests.py](../src/pulsara_agent/scheduling/requests.py)、[service.py](../src/pulsara_agent/scheduling/service.py)，约 154–172 行；[scheduling repository](../src/pulsara_agent/conversation_kernel/_repository/scheduling.py)，约 110–130、235 行；[contracts.py](../src/pulsara_agent/scheduling/contracts.py)。

### 4.4 terminal_monitor 的嵌套默认

- register 必填 process_id；list 只需 action；cancel 必填 monitor_id。
- register 的 conditions/delivery/lifetime 可省略或给 {}，对象本身不能 null。
- conditions 默认 output:null、heartbeat_interval_seconds:null，仅完成通知。
- output:null 禁用输出进度条件；output={} 启用默认 min_new_output_chars=200、quiet_period_ms=500，二者不同。
- delivery 默认 max_output_chars=4000、minimum_progress_observation_interval_seconds=5。
- lifetime 默认 maximum_duration_seconds=36000。
- 普通默认数字目前不可 null；list/cancel 不能携带 register 字段，即使 null。

依据：[terminal.py](../src/pulsara_agent/ports/terminal.py)，约 260–365 行。

## 5. 调研基线：Provider 投影与模型可见性的发现

Chat Completions 和 Responses 的 function tool 都显式使用 `strict:false`。不存在当前协议要求“把所有可选字段填满”的必要性。

根级 action union 在 provider schema 中会展平为字段并集，required 只保留共同要求；canonical local schema/owner 保留准确执行契约。不能因展平后的字段看起来 optional，就认为所有 action 都能携带它。

- terminal_process/terminal_monitor 有 discriminator，投影会补每个 action 的条件必填说明，但不会自动补齐允许/禁止字段说明。例如 poll 携带 timeout_seconds、monitor list 携带 conditions 的输入可能通过 provider schema，却仍被 canonical schema 拒绝。这说明分支指导仍有缺口，不代表 provider 投影必须与本地 schema 完全相同。
- manage_capability 没有同样的 discriminator，但 action 自身描述携带 Required/Optional 和行为说明，保留了模型可见的分支指导。
- scheduled_tasks 的分支 action 只有 const，没有对应完整签名说明。展平后仅 action 必填，现有工具描述不足以完整恢复各 action 的 required/allowed 字段。
- 子代理 context/reasoning、会话搜索首页/续页也存在条件要求，需要准确表达，不能仅依赖调用失败后的反馈。

依据：[function_tools.py](../src/pulsara_agent/llm/adapters/openai/function_tools.py)，约 146、272、322、352 行；[投影测试](../tests/test_round5a1_provider_output_termination.py)，约 128、211 行。

## 6. 验证范围与证据限制

本轮做的是只读调研，不是修改后的回归验收：

- 阅读 41 个工具的 canonical schema、相关执行解析和现有针对性测试。
- 使用根 .venv 做纯 JSON Schema/Pydantic/解析探针，没有调用文件写入、进程启动、任务管理或其他有副作用的工具。
- 文件/查询组验证了 51 个顶层声明字段的局部 schema，均拒绝 null；另核对生产 gate，避免误用孤立 parser 的宽松行为。
- 验证 scheduled_tasks/read_file/search_session_content 的代表性 null 调用被 schema 拒绝；terminal.workdir:null 被接受。
- 验证子代理 context/reasoning 的若干 schema/owner 分歧、terminal_process 不适用字段拒绝，以及 monitor 外层对象和内部 null 的差异。
- 阅读而非重跑全部既有测试；不能据此声称完整回归或真实 provider dogfood 已通过。

主要现有测试：

| 领域 | 证据 |
|---|---|
| 文件/产物 | [test_artifact_export.py](../tests/test_artifact_export.py)、[test_round1_tool_output_artifact.py](../tests/test_round1_tool_output_artifact.py)、[test_content_revision_line_edit.py](../tests/test_content_revision_line_edit.py)、[test_search_tool_split.py](../tests/test_search_tool_split.py) |
| 查询/能力 | [test_session_content_query.py](../tests/test_session_content_query.py)、[test_capability_source_query.py](../tests/test_capability_source_query.py) |
| 图片/可视化 | [test_local_image_tool_executor.py](../tests/test_local_image_tool_executor.py)、[test_visualization_subscription.py](../tests/test_visualization_subscription.py) |
| Plan/TODO | [test_round4_plan_workflow.py](../tests/test_round4_plan_workflow.py)、[test_lightweight_todo_refinement.py](../tests/test_lightweight_todo_refinement.py) |
| 子代理/reasoning | [test_round10_hierarchical_subagent_orchestration.py](../tests/test_round10_hierarchical_subagent_orchestration.py)、[test_llm_model_connections.py](../tests/test_llm_model_connections.py)、[test_llm_model_target.py](../tests/test_llm_model_target.py) |
| 终端 | [test_round2_terminal_architecture.py](../tests/test_round2_terminal_architecture.py)、[test_terminal_cognitive_subtraction.py](../tests/test_terminal_cognitive_subtraction.py) |
| 管理/记忆 | [test_capability_management_intent.py](../tests/test_capability_management_intent.py)、[test_capability_management_preparation.py](../tests/test_capability_management_preparation.py)、[test_memory_tool_result_rejection.py](../tests/test_memory_tool_result_rejection.py)、[test_direct_advisory_memory.py](../tests/test_direct_advisory_memory.py) |
| 定时/MCP | [test_scheduled_tasks_postgres.py](../tests/test_scheduled_tasks_postgres.py)、[test_scheduled_tasks_host.py](../tests/test_scheduled_tasks_host.py)、[test_round6_mcp_production.py](../tests/test_round6_mcp_production.py) |

已有测试覆盖不少默认/分支/边界，但没有看到覆盖全部工具的成组省略/null 对照矩阵。若后续实施，应优先证明范围、互斥、游标、精确选择和空集合操作语义，而非为每个普通数字默认重复编写相同测试。

## 7. 审阅后的提案与实施边界

以下边界已按第 8 节的独立审阅建议修订，适用于用户批准的第 8.2 节实施范围。初步提案中“纯默认/无附加信息字段普遍允许 null”的建议已收窄为选择性容错，不强制所有工具采用“全字段必填、默认填 null”：

1. 默认行为：模型说明和示例优先省略字段；仅在有具体收益、且字段语义明确时选择性接受 null 别名，不据此统一放宽所有纯默认字段。
2. 必填字段不得省略；是否允许 null 由字段的值域独立决定。必填身份、内容等非 nullable 字段仍拒绝 null；effort 分支的 value 则应保持必填，并允许目标模型明确提供的 null 档位。
3. 不适用分支字段：省略；保留闭合 action/kind/mode，不用 null 填满并集字段。
4. 有业务含义的 null：维持明确操作或精确选择，不能归一成缺席。
5. 空字符串/空数组/空对象：保留已有区别，不改成 null 的别名。
6. 条件契约缺口：本轮修复 scheduled action 签名、query 首页/续页、context/reasoning 精确分支，以及 effort.value:null 的可达性，完整清单见第 8.2 节。
7. 实施边界：schema、唯一解析 owner、工具描述和相关内置 Skill 示例须一致；不得只放宽 schema，也不得在通用 provider adapter 递归删除 null。

后续工具契约改动仍须遵守仓库的 provider-input prefix 连续性：不能为应用新 schema 静默改写运行中 epoch 的冻结 SYSTEM/tools。本文不引入新的 rebase 边界。

本轮限于第 8.2 节所涉的 9 个内置工具，不扩展其他候选参数。外部 MCP 参数继续遵守第三方原始契约。

## 8. GPT-6 Astra / xhigh 独立审阅建议

审阅任务：**Pulsara null 语义调研 critic**，2026-10-07。审阅者阅读本文及仓库约束，按需检查工具 schema、执行 owner、provider 投影和相关测试，并运行无副作用的解析探针。没有修改生产代码，也未运行完整回归或真实 provider dogfood。

### 8.1 推荐给模型的统一规则

> 使用默认行为时省略字段；只填写当前 action/kind/查询阶段适用的字段；只有字段明确说明接受 null 时，才按其说明填写。

`optional` 表示可以不提供键，`nullable` 表示提供键时允许其值为 null，两者独立。必填也不等于非 nullable：例如 effort 分支的 value 应保持必填，但目标模型若提供 null 档位，该值就应可达；省略 value 仍应拒绝。当前 provider 使用 strict:false，没有协议要求将全部可选参数填满。与增加大量 null 别名相比，一条一致的填写规则和准确的分支说明更能减少模型负担。

主代理同意该方向：不把“所有纯默认字段接受 null”作为统一目标。逐工具表中的“可讨论 null”保留为候选；本轮仅第 8.2 节所列缺口获批实施。

### 8.2 优先处理真实契约缺口

用户已明确批准下表全部缺口并要求直接实施；优先级保留为审阅时的排序，不扩大表外范围。

| 优先级 | 建议 | 原因与边界 |
|---|---|---|
| P1 | 修复 spawn/batch 的 context/reasoning 分支 schema，以及 effort.value:null 可达性 | 分支必填和禁止字段与既有 owner 一致，例如 last_n 必须有 turns、none 禁止 turns/task_id、effort 必须有 value 且禁止其他变体字段。value 的 null 是模型清单与目标校验已经支持的实际档位；动态档位合法性继续由既有目标校验负责，不能接受目标模型未提供的档位，也不能将 reasoning 整体 null 改成默认。复用既有 context 解析和 reasoning codec，不新增平行解析器。 |
| P1 | 补齐 scheduled_tasks 各 action 的必填、可选和禁止字段说明，以及续页筛选规则 | provider 展平后的并集不能完整指导合法调用；cursor 不保存筛选条件。 |
| P1 | 选择性允许 list/create/run_now 的 session_id:null 表示当前会话 | 有本次真实误用证据；须同时更新公开 schema 和工具调用边界解析，保持底层/HTTP 的 None 语义，其余 action 仍禁止该字段。模型示例仍优先省略，不作为其他 optional 字段自动 nullable 的先例。 |
| P2 | 补齐会话查询首页/续页、wait_agent 目标/settle 的条件 schema 与模型可见说明 | schema 表达既有 owner 执行的分支必填和禁止字段，不仅修补文案；不让模型用 null 填充当前阶段不适用的字段，保留游标冻结和条件默认。 |
| P2 | 补齐 terminal_process/terminal_monitor 的分支允许字段指导 | discriminator 自动补充的 required 说明不能表达完整分支约束。 |

canonical schema 应准确表达可表达的分支形状；provider 投影可保留现有架构允许的超集，但须保留足够的模型可见分支指导，不要求两者完全等价。运行时身份、动态模型档位和其他需实际状态才能判断的约束，继续由既有 owner 校验。

暂缓统一放宽普通数字、布尔、对象、数组和过滤器的 null，以及为来源互斥字段增加 null 占位。孤立 parser 接受 None 本身不足以证明公开契约应扩张。

### 8.3 保留具有操作含义的值

- overlay 省略打开编辑器，null 清除覆盖；expected_overlay:null 表示断言没有覆盖，不能删除这些 null。插件认证还须区分解析值与最终继承连接输入默认值后的结果。
- 省略 model 继承父目标；显式指定 model.connection_id 时，省略 reasoning 才走该目标的默认 reconcile。这是 Pulsara 的目标默认策略，不一定等于 provider 不发送 reasoning 参数。精确选择继续由既有校验约束，不能将整体 reasoning:null 当作默认。
- content=""、todo.items=[]、report.data={}、monitor.output={} 有各自实际含义。false 与合法的 0 也是具体值，不能被 truthiness 默认化；tasks=[]、wait.task_ids=[] 继续按当前契约拒绝。
- 外部 MCP 参数遵守第三方契约，不能由通用 adapter 递归删 null 或补默认。

### 8.4 实施验证应聚焦的边界

对 canonical schema、provider 投影及可见描述、执行 owner 做成组对照，覆盖合法输入可达性和非法分支组合的拒绝：定时任务当前/其他会话及多页筛选、context/reasoning 的条件必填和互斥、模型真实 nullable 档位、overlay presence/guard/最终 endpoint 与认证继承、查询游标与条件方向、wait_agent 目标/settle、空集合与清空操作。对 provider 投影允许的超集，应验证模型可见指导保留了准确分支规则、本地校验仍拒绝非法组合，而不是要求投影与 canonical schema 接受完全相同的输入。不能仅凭 schema 探针声称模型能正确使用；相应真实 provider 调用应在实施验收中验证。

用户批准后的实施记录见第 9 节。新工具契约仍只能在已批准的 epoch 边界安装，不新增 durable 状态或 provider prefix 重建路径。

## 9. 第 8.2 节实施与验收记录

### 9.1 已实施契约与 owner

- `spawn_agent/create_agent_tasks`：共用 catalog 中的 context/model schema 构造，删除两份宽松的嵌套形状。context 使用封闭 none/last_n/worker_history 分支；reasoning 使用封闭 effort/toggle/budget_tokens 分支，effort.value 必填且允许 string/null。既有 context parser、reasoning codec、目标校验继续负责执行和动态合法性；整体 reasoning:null 仍拒绝。
- `scheduled_tasks`：从同一 action 字段定义生成 Required/Optional 签名，明确其余字段禁止；cursor 说明要求重复原 status/session_id。公开 schema 仅在 list/create/run_now 分支允许 session_id:null，工具 invoke 在调用既有 validator 前将省略/null 解析为当前会话。不适用分支字段不删除，仍拒绝；底层 service/HTTP 的 None 查询范围不变。
- `search_sessions/search_session_content/read_session_content`：canonical schema 分成首页与 cursor-only 续页。内容搜索首页 query 必填且包含非空白字符；续页仅允许 cursor 与对应预算。provider 根 union 展平后，保留首页条件和续页允许字段说明。查询 owner、授权范围、游标内容与方向默认不变。
- `wait_agent`：canonical schema 区分无目标与有目标；无目标只能提供 timeout_seconds，有目标必须提供非空 task_ids，settle 仍默认 all。provider 说明明确条件，既有等待 owner 和时间/目标边界不变。
- `terminal_process/terminal_monitor`：继续由现有 Pydantic 输入类型生成 schema；terminal schema owner 从分支属性生成允许字段说明，补足 provider 展平损失的信息。不修改通用 provider adapter，不扩展第三方 MCP 契约。
- 更新内置 `pulsara-subagent` Skill 的 reasoning 与 wait 调用指导。没有数据库 schema 或存储变更，clean-v0、event/subject/guard/relation/job 类别不变；没有新增上限、哈希或 epoch 重建边界。

### 9.2 Pulsara 启发式 token 前后对照

使用仓库自己的 `PulsaraHeuristicTokenEstimatorV3`，不使用 provider usage 或外部 tokenizer。基线在生产代码修改前实测；前后均计入工具名称、完整描述和参数 schema，不包含 Skill 正文、SYSTEM 或消息。

- **Canonical ToolSpec**：`estimate_tool_spec(ToolSpec(...))`，包含 Pulsara 的 ToolSpec framing，与其 semantic tool_tokens 口径一致。
- **投影后 function JSON**：`estimate_json(openai_function_definition(tool))`，包含 strict:false 与根 union 展平后的描述；Chat/Responses 共用该 function definition。不包含 API 外层 wrapper 和请求 envelope，不代表整次请求的实际 token。
- 表内合计为逐工具整数估算相加。共检查 41 个工具；仅以下 9 个定义变化，其余 32 个 canonical 与 function JSON 均保持原值。

| 工具 | Canonical 修改前 | 修改后 | 差值 | Function JSON 修改前 | 修改后 | 差值 |
|---|---:|---:|---:|---:|---:|---:|
| spawn_agent | 877 | 960 | +83 | 873 | 957 | +84 |
| create_agent_tasks | 1123 | 1215 | +92 | 1119 | 1212 | +93 |
| scheduled_tasks | 3094 | 2577 | -517 | 1410 | 1581 | +171 |
| search_sessions | 237 | 330 | +93 | 232 | 282 | +50 |
| search_session_content | 326 | 437 | +111 | 322 | 387 | +65 |
| read_session_content | 442 | 575 | +133 | 438 | 490 | +52 |
| wait_agent | 404 | 456 | +52 | 400 | 385 | -15 |
| terminal_process | 1704 | 1798 | +94 | 1059 | 1149 | +90 |
| terminal_monitor | 1305 | 1350 | +45 | 1186 | 1227 | +41 |
| **合计** | **9512** | **9698** | **+186（1.96%）** | **7039** | **7670** | **+631（8.96%）** |

scheduled_tasks 的 canonical 降低来自去掉根级重复 properties；准确 action 签名使其投影后 JSON 增长。两种口径必须分开呈现，不能用 canonical 降低推断 provider 可见定义变短。

复测脚本：[measure_builtin_tool_tokens.py](../tools/measure_builtin_tool_tokens.py)。原始前后定义与估算保存在 `output/builtin-tool-default-null-20261007/tokens-before.json` 和 `tokens-after.json`，不记录代码/文档内容哈希。

```sh
.venv/bin/python tools/measure_builtin_tool_tokens.py --output output/builtin-tool-default-null-20261007/tokens-after.json
```

### 9.3 验证记录与边界

相关 14 个测试模块合跑 **485 passed**，覆盖新分支契约、定时任务 PostgreSQL/host、会话查询、子代理编排、provider prefix 连续性、provider 输出终止、模型连接/目标、terminal 和能力管理/插件输入。随后将 scheduled_tasks 签名移至根 description，相关 **309 项测试再次通过**。修改文件的 Ruff 检查和 `git diff --check` 通过。数据库测试使用已核实的本机配置创建独立 disposable 数据库，保存的生产设置未改写。

nullable effort 已通过 schema → 既有 codec → 精确目标校验的测试：目标列出 null 时接受，未列出时拒绝，缺失 value 与整个 reasoning:null 仍拒绝。此次保存的真实连接未提供 nullable effort 档位，因此不声称完成该档位的真实 provider 调用；真实子代理调用使用已保存目标及其默认 reasoning。

真实调用脚本：[run_builtin_default_null_dogfood.py](../tests/dogfood/run_builtin_default_null_dogfood.py)。读取保存的生产连接，在独立临时 home/数据库内运行定时任务、会话查询、terminal、单个/批量子代理与两种 wait；不启动定时调度器。报告保留实际 provider 输入、输出、工具参数和结果，仅脱敏实际凭据值。最终结果如下：

| 保存的目标 / wire API | 最终复验结果 | 证据文件（位于下述 output 目录） |
|---|---|---|
| openai/gpt-6-luna / Chat | **通过**：9 个受影响工具均调用成功；共 31 次模型调用，3 个 worker 均 COMPLETED，实际结果分别为 SINGLE_DONE、BATCH_A_DONE、BATCH_B_DONE；17 组主会话阶段内相邻请求保持 tools 相同、messages 追加 | `dogfood-chat-verified-workers.json` |
| deepseek-flash / Responses | **未完整通过**：第一轮给 scheduled_tasks.get 多填 session_id:null，被 canonical gate 拒绝；复验误抄 search_sessions 游标，出现 3 次 APPLICATION_ERROR。模型随后修正，但脚本保留“每次调用都成功”的严格断言，分别止于定时任务/历史阶段，不能作为完整 9 工具或 worker 成功证据 | `dogfood-responses-verified-workers.json`、`dogfood-responses-verified-workers-retry.json` |

另对以上保存的请求复查了 Responses 的 instructions 不变、tools 相同与 input 追加：两次失败运行分别覆盖 8、21 组阶段内相邻请求，均通过。这是 prefix 连续性证据，不改变其工具调用验收失败的结论。描述和 schema 修正不能保证模型永不误填；canonical gate 和游标契约仍按原有执行边界拒绝非法输入。

探索性运行暴露了模型误抄游标、给不适用 action 填入 session_id:null 或其他分支字段的情况；本地 gate 均拒绝，模型随后可修正。早期报告还暴露测试记录器漏传 frozen_target_fact，导致工具接纳成功但 worker 启动失败。已修复记录器，并补充对三个 worker 的数据库最终状态及实际返回 marker 的断言。原始失败报告及旧版仅检查工具成功的报告均保留于 `output/builtin-tool-default-null-20261007/`，后者不能作为子代理执行成功证据。
