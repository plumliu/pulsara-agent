# Pulsara 能力与定时任务工具清晰化实施规格

状态：**已实施并完成本组聚焦验证**。日期：2026-10-26。八工具说明、schema annotation 与无参数全刷新 hard cut 已落地；未请求真实模型、执行真实 MCP 或改动保存设置。以下保留规格正文及实施前计量口径，末节记录实际验证。

## 1. 本组决策与边界

依照用户最终决定，将 `reload_hooks` 合并进无参数 `reload_capabilities`，每次请求全量刷新，不增加 target 参数或别名。最终保留 `list_capabilities`、`inspect_capability`、`manage_capability`、`reload_capabilities`、`read_mcp_resource`、`get_mcp_prompt`、`use_new_mcp_tool`、`scheduled_tasks` 八个工具；本组 9→8，完整 builtin 目录 40→39。除这项明确授权的合并外，其余修改为**说明与 annotation 整理，继续使用当前 lowerer**；不引入结构候选、工具拆分或新的 dispatcher。

模型先要回答“找什么、观察还是变更、用哪个目标”，然后才需要动作字段与值的含义。说明据此分工：function.description 给入口、动作允许字段和决定操作的事实；schema 给类型、required、nullable、范围、默认值和完整嵌套结构；已有结果给本次实际状态。删除的是重复解释和无法帮助当前选择的泛化告诫，不是少用动作。

具体保留：manage 的全部 15 个动作、scheduled_tasks 的全部 8 个动作与 5 种时间规则、所有 source/target 身份形状、DIRECT/META 路由、用户表单与现有权限判断、原结果数据。保留工具的字段名、枚举、required、additionalProperties、默认值、null 语义及 parser 均不变。`scope`、`source_kind`、管理 ID、运行时 ID 是不同的既有概念，不能为省字合并。

现行 schema 原始 union 由当前 `lower_openai_function_parameters` 转成 provider superset；本地精确 schema/parser 继续是调用真值。说明不能代替条件约束。英文动作行是减少拼接信息的候选，不声称保证生成合法参数。

本规格遵循根 AGENTS.md。只允许新 cold epoch 或明确采用的 compaction successor 安装新定义；已有 epoch 的 SYSTEM/tools 字节不变，messages 只追加。reload、MCP 更新与本次说明发布均不新增输入根重建边界，不按轮次筛选或修改工具。

## 2. 已读证据与现行 authority

完整阅读 `output/tool-schema-compatibility-20261026/REPORT.zh.md`、`EXPERIMENT_PLAN.zh.md`、`COMPRESSION_DISCUSSION.zh.md`、`COMPRESSION_CRITIC.zh.md`。190 次既有真实请求支持的范围是：两个目标对原始根 union 的表现不同；展平实际放过过 poll 的 timeout_seconds；当前 Qwen 根 description 位置探针 0/2、function.description 2/2；内联嵌套 union 可作为另一个结构候选但成本更高。它们不证明本组八份新文本已获 provider 验证，也不说明第三方服务内部怎样拼提示词。

本规格初拟时仓库根没有现行功能实现规格；相关旧规格均位于 `archived_docs/`。已核对历史 `PULSARA_CAPABILITY_SOURCE_QUERY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 的管理说明与查询合同、`PULSARA_SCHEDULED_TASKS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 的时间与切换合同。它们仅解释设计来源；与现行实现冲突时以本任务、AGENTS.md 和下表实际 owners 为准，不恢复旧字段或旧流程。

| 当前 owner | 已核对内容／实施位置 |
|---|---|
| `src/pulsara_agent/capability/builtin_catalog.py::_BUILTIN_DESCRIPTORS` | 八个最终 function.description 的安装入口；删除 reload_hooks descriptor、目录绑定与 recovery 名称项。read/get/meta 三工具的字段 annotation 也在此编辑。保留工具的 permission_category、artifact_mode、可见性与 binding 不变。 |
| `capability/source_query.py::list_input_schema, inspect_input_schema, target_schema, parse_query` | 查询参数与完整 target union、子代理范围、过滤相容性；本规格不改这些 schema。 |
| `conversation_kernel/capability_query.py::CapabilitySourceQuery.execute` | 源观察、详情、completeness、分页和 diagnostics。 |
| `capability/management_intent.py::_FIELDS, capability_management_input_schema, parse_capability_management_intent` | 15 动作闭合字段、Hook plugin_id 条件、OAuth guard 条件和 overlay null；编辑 descriptions，保留全部约束及 parser。 |
| `conversation_kernel/capability_management.py::CapabilityManagementPreparation.prepare` | exact identity、安装与配置 preparation、既有编辑器与 review。 |
| `conversation_kernel/capability_management_execution.py::CapabilityManagementCall.execute, _hook, _result` | 用户提交、原生 mutation、status/current/adoption 结果。 |
| `conversation_kernel/tool_runtime.py` 的授权与调用分支、`_query_capabilities_result`、`_mcp_unavailable_direct_message` | ROOT/bypass reload 条件、MCP 路由、adoption、旧 DIRECT 入口的具体恢复指引。 |
| `conversation_kernel/host.py::reload_hooks, reload_capabilities` | reload_capabilities 已调用 reload_hooks，复用该全量刷新入口；保留必要内部 reload_hooks helper，保持已安装前缀。 |
| `conversation_kernel/mcp/meta.py::ProcessLocalNewMcpToolRefOwner.resolve_callable` | tool_ref 在当前 agent/conversation/epoch 的绑定，保留。 |
| `conversation_kernel/mcp/supervisor.py::admit_standard_operation, invoke_standard, _invoke_standard_remote_result` | 目录 exact join、resource URI、prompt 所需且已声明的字符串 arguments、外部结果信任标记和实际 SDK 调用。 |
| `scheduling/requests.py::_FIELDS, action_schema, validate_action` | 8 动作、values、switch.target 和 schedule 的闭合形状；编辑 annotation，不改其值域。 |
| `scheduling/contracts.py::validate_rule, next_occurrence` | UTC 整秒、ISO 日期、HH:mm、weekday 1–7、DST 与 next occurrence。 |
| `scheduling/service.py::ScheduledTaskService.invoke, get, mutate, switch` | model 入口当前 session 默认、权限覆盖、完整更新、实际结果。 |
| `conversation_kernel/_repository/scheduling.py::create_scheduled_task, mutate_scheduled_task, switch_scheduled_task` | 每 session 单 ACTIVE、expected_revision、原子 switch、pending 取消与运行中工作保持。 |
| `llm/adapters/openai/function_tools.py::lower_openai_function_parameters, _merge_property_schema` | lowerer 保持当前算法与 strict:false；它会自然合并移去不同说明后的相同 action enum。 |

表中省略 `src/pulsara_agent/` 前缀的路径均相对此目录。没有 dependency 所有权转移；SDK、原生管理、队列和定时器机制全部沿现有 owner。

## 3. 每个工具的最终 function.description

以下英文逐字作为最终值；没有实验历史、provider 特例、研究引用或自创缩写。值域和 defaults 不在正文逐项复述。`manage_capability` 的共同 action/scope 与每行额外字段合起来是完整调用形状；`scheduled_tasks` 的 action 每行列出除 action 外的字段。

### list_capabilities

```text
Find Skills, Hooks, MCP servers, tools, resources, templates and prompts. Use {} for an overview, or filter the list. Copy an item's target into inspect_capability; use source.target as parent to find a Plugin's components. Continue with next_offset and the same filters. Results describe the observed catalog and its completeness. SUBAGENT callers can query only their assigned MCP runtime.
```

### inspect_capability

```text
Inspect a known capability target; list_capabilities supplies copyable targets. Read Skill bodies with read_file. For MCP tools, follow invocation.mode: DIRECT uses the listed named tool; META uses the returned input_schema and tool_ref with use_new_mcp_tool, including when an older named entry remains listed; UNAVAILABLE gives the current obstacle. This reads metadata. Use read_mcp_resource for resource content and get_mcp_prompt for rendered prompts.
```

### manage_capability

```text
Install and manage capabilities requested by the user. ROOT agent only; available in every permission mode. action and scope are required. Send only the additional fields listed for that action; fields are required unless marked optional.
INSTALL_LOOSE_SKILL: source_path; optional name, description. Install an independent Skill into a new destination.
SET_LOOSE_SKILL_ENABLED: skill_path, enabled. Toggle an eligible independent Skill.
REMOVE_LOOSE_SKILL: skill_path. Remove an independent Skill directory after inspecting removal eligibility.
TRUST_HOOK_SOURCE: source_kind; optional plugin_id. Review and trust current Hook definitions.
REVOKE_HOOK_TRUST: source_kind; optional plugin_id. Remove trust, keeping definitions.
SET_HOOK_SOURCE_ENABLED: source_kind, enabled; optional plugin_id. Toggle the Hook source.
ADD_LOCAL_MCP: server_id; optional config. Add an independent connection.
UPDATE_LOCAL_MCP: server_id; optional config, expected_identity. Replace an independent connection's settings.
REMOVE_LOCAL_MCP: server_id; optional expected_identity. Remove the entry and dedicated local credentials, keeping the server program.
INSTALL_PLUGIN: source_path; optional replace, source_format. Install the whole Plugin disabled; enable separately.
SET_PLUGIN_ENABLED: plugin_id, enabled; optional expected_package_install_id. Toggle the whole Plugin, with component review on enable.
REMOVE_PLUGIN: plugin_id; optional expected_package_install_id. Remove the Plugin and dedicated credentials, preserving its data and original source.
CONFIGURE_PLUGIN_MCP_CONNECTION: plugin_id, server_id; optional overlay, expected_overlay, expected_package_install_id. Configure a Plugin connection.
AUTHORIZE_MCP: server_id; optional plugin_id, expected_identity, expected_package_install_id. Authorize an already configured OAuth connection.
CLEAR_MCP_AUTHORIZATION: server_id; optional plugin_id, expected_identity, expected_package_install_id. Clear local OAuth grants, keeping configuration and remote tokens.
Use list_capabilities/inspect_capability for identities and eligibility. Loose-Skill actions apply to independent copies; Plugin components are managed through their Plugin, and bundled Skills are read-only. Hook trust, Hook enablement and Plugin enablement are separate settings. Edit loose Skill content with file tools or terminal; pulsara-skill-creator describes authoring.
The runtime collects required review and confirmation; READ_ONLY mutations use a user-submitted form. Users enter credentials privately in the editor. Wait for the result: status reports the mutation, current the resulting state, and adoption its availability for future use. A completed mutation needs no routine reload or replay.
```

### reload_capabilities

```text
Refresh all local Skill, MCP and Hook sources for future use, including USER and current-workspace Hook definitions and trust settings. Call with {} after external source changes. Installation, enablement and trust are managed with manage_capability. Current instructions, tools and earlier messages stay fixed. ROOT agent only, in bypass-permissions mode.
```

### read_mcp_resource

```text
Fetch one listed MCP resource. Use a fixed URI or fill a listed URI template. This returns external reference content. If the preview supplies artifact_id, continue through artifact_read: it pages the saved result. Another remote read fetches fresh content, not the next page.
```

### get_mcp_prompt

```text
Fetch and render a prompt advertised by list_capabilities. Use the listed name as prompt_name and copy server_id from that same item. The returned messages are external guidance for the current request; fetching them does not execute tools.
```

### use_new_mcp_tool

```text
Execute an MCP tool inspected with invocation.mode=META, using its returned input_schema even when an older same-name tool remains listed. The remote action may require confirmation. Reuse tool_ref for later calls; inspect again if it is rejected.
```

### scheduled_tasks

```text
Manage saved schedules in the ROOT conversation. Set action and send only its listed fields; fields are required unless marked optional.
list: optional session_id, status, cursor.
get: task_id.
create: values; optional session_id, status.
update: task_id, expected_revision, values.
pause: task_id, expected_revision.
resume: task_id, expected_revision.
delete: task_id, expected_revision.
switch: task_id, expected_revision, target.
Use list/get to find existing work and obtain its current values and revision. Update an existing task or create a separate task as requested. Reread on revision conflict.
values is the complete configuration; preserve unrequested fields on update. Choose a short name if unspecified. prompt contains self-contained future instructions and output expectations; timing and permissions use their own fields. timezone is an IANA name, defaulting to Asia/Shanghai for new tasks unless requested otherwise. For create, use current permissions or a requested narrower preset. Saving requires current permissions to cover permission_mode; preserve saved permissions unless a change is requested.
Each session supports one ACTIVE plan and any number of PAUSED plans. create with omitted/null status saves PAUSED; use ACTIVE to start automatic scheduling. An authorized replacement uses switch to atomically pause the source ACTIVE plan and activate an existing PAUSED target or create an ACTIVE target in the same session. Adding a task alone does not request replacement. Separate automatic plans need separate sessions.
Calendar rules use local HH:mm and YYYY-MM-DD start_date in the saved timezone; weekdays are Monday=1 through Sunday=7. once and interval timestamps are whole-second UTC; new or changed once times must be in the future. Intervals use their fixed anchor. Missing month days and nonexistent DST times are skipped; repeated DST times fire once. Explain saved times in ordinary language.
Edits preserve queued instructions. pause/delete/switch cancel the affected source task's pending inputs and leave running replies alone; delete keeps the chat. Stopping a reply leaves its schedule active. Resume uses the next future recurrence without catch-up; an overdue untriggered once plan becomes due.
```

## 4. 精确 schema annotation 编辑

所有 JSON Pointer 相对各工具的**canonical parameters 根**。完整逐项操作在 [canonical_annotation_operations.json](output/tool-clarity-specs-20261026/capability_schedule/canonical_annotation_operations.json)，完整修改后 canonical 值在 [final.canonical_parameters.json](output/tool-clarity-specs-20261026/capability_schedule/final.canonical_parameters.json)。对于八个保留工具，仅删除/替换/新增下表 annotation，其他键和值逐字保留；这些是源 schema 的编辑，不是绕过 canonical 的临时 wire patch。

`manage_capability` 的 `/oneOf/0` 至 `/oneOf/14` 顺序依次为 INSTALL_LOOSE_SKILL、SET_LOOSE_SKILL_ENABLED、REMOVE_LOOSE_SKILL、TRUST_HOOK_SOURCE、REVOKE_HOOK_TRUST、SET_HOOK_SOURCE_ENABLED、ADD_LOCAL_MCP、UPDATE_LOCAL_MCP、REMOVE_LOCAL_MCP、INSTALL_PLUGIN、SET_PLUGIN_ENABLED、REMOVE_PLUGIN、CONFIGURE_PLUGIN_MCP_CONNECTION、AUTHORIZE_MCP、CLEAR_MCP_AUTHORIZATION。

`scheduled_tasks` 的 `/oneOf/0` 至 `/oneOf/7` 顺序为 list、get、create、update、pause、resume、delete、switch。switch 的 `/properties/target/oneOf/0` 为 existing，`/oneOf/1` 为 create。嵌套 schedule 的 once/interval/daily/weekly/monthly 分支完全保留。

本组没有可删的 title，故不为零收益增加全局 annotation 清洗器。`list_capabilities`、`inspect_capability`、`reload_capabilities` 的 parameters 原样保留；`reload_hooks` 整个模型可见定义删除，见 [tool_operations.json](output/tool-clarity-specs-20261026/capability_schedule/tool_operations.json)。`manage_capability` 的真实业务字段 `/properties/description` 必须保留；删除的是 schema 节点的 description annotation，不能递归删除所有同名字典键。const/default/example 里的普通数据也不能当 schema 走访。

管理动作的说明已移到 function.description：在 `capability_management_input_schema` 中移去 action.description 拼接，并删除仅为这段说明服务的 `_ACTION_DESCRIPTIONS` 常量；保留 `_FIELDS` 原值。schedule 的根 description 已移到 function.description：从 action_schema 删除该根键，不改变分支生成。

### 4.1 字段 description 的精确最终文本

下表所有值只应用到相应工具内具有该字段名的 property schema 根。完整出现位置见下一节的逐路径表；任何未列出的旧 description 都按逐路径表删除。scheduled values/name/prompt/timezone/permission_mode 的语义统一移至 function.description，避免在顶层 values 与 switch.target.values 各复述一次。

#### manage_capability

| 字段 | 最终 description |
|---|---|
| `scope` | USER is the Host home; WORKSPACE is the GUI project directory. Copy an existing target's scope; choose the requested scope for installation. |
| `server_id` | New ID for ADD_LOCAL_MCP; otherwise copy the installation target's independent or Plugin-local server_id, not runtime_server_id. |
| `plugin_id` | Plugin ID from the source target or install result. For OAuth actions, include only for a Plugin connection. |
| `source_path` | Absolute local source directory containing SKILL.md or the Plugin distribution root; download it first. |
| `skill_path` | Exact absolute installed SKILL.md path from list/inspect. |
| `name` | Override the installed loose Skill name; omission keeps its source name. |
| `description` | Override the installed loose Skill description; omission keeps its source description. |
| `expected_identity` | Optional observed independent-MCP guard; omit for automatic inspection. OAuth allows it only without plugin_id. |
| `expected_package_install_id` | Optional observed Plugin-install guard; omit for automatic inspection. OAuth allows it only with plugin_id. |
| `source_kind` | Hook source owner from its target: PLUGIN requires plugin_id; LOCAL omits it. |
| `replace` | Omitted means false; true replaces the Plugin instance when requested by the user. |
| `source_format` | Plugin distribution format; omitted means native. |
| `config` | Omit to open the connection editor, prefilled for UPDATE. An object replaces the complete native entry. Public fields are documented in pulsara-mcp-installer; use the editor for credentials or unknown settings. |
| `overlay` | Omit for the prefilled Plugin connection editor; null restores package/input defaults. An object replaces the complete native overlay. It configures endpoint, public headers/environment and authentication; package-owned command, args, cwd and transport stay fixed. Use the editor for credentials or unknown fields. |
| `expected_overlay` | Optional observed prior overlay guard; omit for automatic inspection. null asserts that no override exists. |

#### read_mcp_resource

| 字段 | 最终 description |
|---|---|
| `server_id` | Copy from the resource or template listing that supplied the URI. |
| `uri` | Exact listed resource URI, or a concrete URI with all listed template variables filled. |

#### get_mcp_prompt

| 字段 | 最终 description |
|---|---|
| `arguments` | Use declared argument names, including every required name, with string values. Omit or use {} when none are needed. |

#### use_new_mcp_tool

| 字段 | 最终 description |
|---|---|
| `tool_ref` | Exact inspected tool_ref, scoped to this agent and conversation. |
| `arguments` | Arguments matching the inspected input_schema; use {} when no inputs are needed. |

#### scheduled_tasks

| 字段 | 最终 description |
|---|---|
| `session_id` | Omit or use null for this session; otherwise copy the target session ID. |
| `cursor` | Copy next_cursor and repeat the same status/session_id filters. |

### 4.2 完整逐路径操作

replace 的值均由上一节同名字段给出；remove 仅移除该 annotation。这里无未展开通配符。

#### get_mcp_prompt

| op | JSON Pointer | 新值来源 |
|---|---|---|
| replace | `/properties/arguments/description` | 4.1 `get_mcp_prompt.arguments` |
| remove | `/properties/prompt_name/description` | — |
| remove | `/properties/server_id/description` | — |

#### manage_capability

| op | JSON Pointer | 新值来源 |
|---|---|---|
| remove | `/oneOf/0/properties/action/description` | — |
| replace | `/oneOf/0/properties/description/description` | 4.1 `manage_capability.description` |
| replace | `/oneOf/0/properties/name/description` | 4.1 `manage_capability.name` |
| replace | `/oneOf/0/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/0/properties/source_path/description` | 4.1 `manage_capability.source_path` |
| remove | `/oneOf/1/properties/action/description` | — |
| remove | `/oneOf/1/properties/enabled/description` | — |
| replace | `/oneOf/1/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/1/properties/skill_path/description` | 4.1 `manage_capability.skill_path` |
| remove | `/oneOf/2/properties/action/description` | — |
| replace | `/oneOf/2/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/2/properties/skill_path/description` | 4.1 `manage_capability.skill_path` |
| remove | `/oneOf/3/properties/action/description` | — |
| replace | `/oneOf/3/properties/plugin_id/description` | 4.1 `manage_capability.plugin_id` |
| replace | `/oneOf/3/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/3/properties/source_kind/description` | 4.1 `manage_capability.source_kind` |
| remove | `/oneOf/4/properties/action/description` | — |
| replace | `/oneOf/4/properties/plugin_id/description` | 4.1 `manage_capability.plugin_id` |
| replace | `/oneOf/4/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/4/properties/source_kind/description` | 4.1 `manage_capability.source_kind` |
| remove | `/oneOf/5/properties/action/description` | — |
| remove | `/oneOf/5/properties/enabled/description` | — |
| replace | `/oneOf/5/properties/plugin_id/description` | 4.1 `manage_capability.plugin_id` |
| replace | `/oneOf/5/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/5/properties/source_kind/description` | 4.1 `manage_capability.source_kind` |
| remove | `/oneOf/6/properties/action/description` | — |
| replace | `/oneOf/6/properties/config/description` | 4.1 `manage_capability.config` |
| replace | `/oneOf/6/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/6/properties/server_id/description` | 4.1 `manage_capability.server_id` |
| remove | `/oneOf/7/properties/action/description` | — |
| replace | `/oneOf/7/properties/config/description` | 4.1 `manage_capability.config` |
| replace | `/oneOf/7/properties/expected_identity/description` | 4.1 `manage_capability.expected_identity` |
| replace | `/oneOf/7/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/7/properties/server_id/description` | 4.1 `manage_capability.server_id` |
| remove | `/oneOf/8/properties/action/description` | — |
| replace | `/oneOf/8/properties/expected_identity/description` | 4.1 `manage_capability.expected_identity` |
| replace | `/oneOf/8/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/8/properties/server_id/description` | 4.1 `manage_capability.server_id` |
| remove | `/oneOf/9/properties/action/description` | — |
| replace | `/oneOf/9/properties/replace/description` | 4.1 `manage_capability.replace` |
| replace | `/oneOf/9/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/9/properties/source_format/description` | 4.1 `manage_capability.source_format` |
| replace | `/oneOf/9/properties/source_path/description` | 4.1 `manage_capability.source_path` |
| remove | `/oneOf/10/properties/action/description` | — |
| remove | `/oneOf/10/properties/enabled/description` | — |
| replace | `/oneOf/10/properties/expected_package_install_id/description` | 4.1 `manage_capability.expected_package_install_id` |
| replace | `/oneOf/10/properties/plugin_id/description` | 4.1 `manage_capability.plugin_id` |
| replace | `/oneOf/10/properties/scope/description` | 4.1 `manage_capability.scope` |
| remove | `/oneOf/11/properties/action/description` | — |
| replace | `/oneOf/11/properties/expected_package_install_id/description` | 4.1 `manage_capability.expected_package_install_id` |
| replace | `/oneOf/11/properties/plugin_id/description` | 4.1 `manage_capability.plugin_id` |
| replace | `/oneOf/11/properties/scope/description` | 4.1 `manage_capability.scope` |
| remove | `/oneOf/12/properties/action/description` | — |
| replace | `/oneOf/12/properties/expected_overlay/description` | 4.1 `manage_capability.expected_overlay` |
| replace | `/oneOf/12/properties/expected_package_install_id/description` | 4.1 `manage_capability.expected_package_install_id` |
| replace | `/oneOf/12/properties/overlay/description` | 4.1 `manage_capability.overlay` |
| replace | `/oneOf/12/properties/plugin_id/description` | 4.1 `manage_capability.plugin_id` |
| replace | `/oneOf/12/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/12/properties/server_id/description` | 4.1 `manage_capability.server_id` |
| remove | `/oneOf/13/properties/action/description` | — |
| replace | `/oneOf/13/properties/expected_identity/description` | 4.1 `manage_capability.expected_identity` |
| replace | `/oneOf/13/properties/expected_package_install_id/description` | 4.1 `manage_capability.expected_package_install_id` |
| replace | `/oneOf/13/properties/plugin_id/description` | 4.1 `manage_capability.plugin_id` |
| replace | `/oneOf/13/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/13/properties/server_id/description` | 4.1 `manage_capability.server_id` |
| remove | `/oneOf/14/properties/action/description` | — |
| replace | `/oneOf/14/properties/expected_identity/description` | 4.1 `manage_capability.expected_identity` |
| replace | `/oneOf/14/properties/expected_package_install_id/description` | 4.1 `manage_capability.expected_package_install_id` |
| replace | `/oneOf/14/properties/plugin_id/description` | 4.1 `manage_capability.plugin_id` |
| replace | `/oneOf/14/properties/scope/description` | 4.1 `manage_capability.scope` |
| replace | `/oneOf/14/properties/server_id/description` | 4.1 `manage_capability.server_id` |

#### read_mcp_resource

| op | JSON Pointer | 新值来源 |
|---|---|---|
| replace | `/properties/server_id/description` | 4.1 `read_mcp_resource.server_id` |
| replace | `/properties/uri/description` | 4.1 `read_mcp_resource.uri` |

#### scheduled_tasks

| op | JSON Pointer | 新值来源 |
|---|---|---|
| remove | `/description` | — |
| replace | `/oneOf/0/properties/session_id/description` | 4.1 `scheduled_tasks.session_id` |
| remove | `/oneOf/0/properties/status/description` | — |
| replace | `/oneOf/2/properties/session_id/description` | 4.1 `scheduled_tasks.session_id` |
| remove | `/oneOf/2/properties/status/description` | — |
| remove | `/oneOf/2/properties/values/properties/name/description` | — |
| remove | `/oneOf/2/properties/values/properties/permission_mode/description` | — |
| remove | `/oneOf/2/properties/values/properties/prompt/description` | — |
| remove | `/oneOf/2/properties/values/properties/timezone/description` | — |
| remove | `/oneOf/3/properties/values/properties/name/description` | — |
| remove | `/oneOf/3/properties/values/properties/permission_mode/description` | — |
| remove | `/oneOf/3/properties/values/properties/prompt/description` | — |
| remove | `/oneOf/3/properties/values/properties/timezone/description` | — |
| remove | `/oneOf/7/properties/target/oneOf/1/properties/values/properties/name/description` | — |
| remove | `/oneOf/7/properties/target/oneOf/1/properties/values/properties/permission_mode/description` | — |
| remove | `/oneOf/7/properties/target/oneOf/1/properties/values/properties/prompt/description` | — |
| remove | `/oneOf/7/properties/target/oneOf/1/properties/values/properties/timezone/description` | — |

#### use_new_mcp_tool

| op | JSON Pointer | 新值来源 |
|---|---|---|
| replace | `/properties/arguments/description` | 4.1 `use_new_mcp_tool.arguments` |
| replace | `/properties/tool_ref/description` | 4.1 `use_new_mcp_tool.tool_ref` |

### 4.3 lowerer 之后的完整定义

[final.responses_tools.json](output/tool-clarity-specs-20261026/capability_schedule/final.responses_tools.json) 是八个**完整**最终 Responses function 定义，不是仅描述或 patch；[final.full_catalog.responses_tools.json](output/tool-clarity-specs-20261026/capability_schedule/final.full_catalog.responses_tools.json) 是替换本组后的完整 39 工具数组，其余 31 个来自当次生产导出原值。

通过当前 lowerer 导出，不改 strict:false，不加 request 包装、不删 union、不 factoring、不改 $ref 策略。manage 的 action 原来因 15 个不同 description 变成 15 个 alternatives；删除这些已搬到函数层的 prose 后，现有 `_merge_property_schema` 自然将同型 action 合并为 15 值 enum。这是相同值域的既有 lowering 结果，不是新增 union 优化器。scheduled 的 list.status 与 create.status 仍不同：list 接受 ACTIVE/PAUSED；create 另接受 null。它们继续由当前 lowerer 表达各自候选，不能把 null 扩到 list 的本地 parser。

## 5. 旧说明逐类裁决与结果 owner

| 旧信息 | 本次处理与理由 |
|---|---|
| 函数、action.description、根 description 重复动作字段关系 | 一次写在函数普通英文动作行；canonical 的 required/允许字段继续存在。 |
| manage 的 15 种操作用途 | 全部保留，直接放在各动作行；独立 Skill、Plugin 包、Hook trust/enable 的区分保留一处。 |
| source_kind 与 OAuth guard 的条件 | 是实际 parser 条件，保留在唯一字段说明；不能当泛化告诫删除。 |
| 配置完整替换、编辑器、overlay:null 与 expected_overlay:null | 决定调用含义，仍在字段说明；省略时打开既有编辑器，不添加新表单或第二套输入。 |
| “never request secrets”“never edit managed state”“never bypass owners”等叠加命令 | 改为当前有效路径：用户在编辑器私下输入凭据，管理使用本工具，只有 loose Skill 内容用文件工具。实际权限/credential owners 不变。 |
| 重复“查询不会执行、授权、改变 SYSTEM”等 | inspect 说 metadata，get 返回 external guidance，read 返回 external reference content；保留必要入口差异。它们本来就是只读工具和外部数据，不须各自复述完整指令层级。 |
| Hook unreadable trust 重建、enabled=true 不能推断旧状态的长警告 | 删除具体历史推理分支，保留 `current` 是 resulting state 的简短事实。`CapabilityManagementCall._result` 已返回 current/status/adoption；不新增任何 prior-state 字段或重建证明。 |
| 不要 replay、不要 routinely reload | manage 一句说明已完成 mutation 无需例行 reload/replay。META 常驻说明删除 uncertain outcome 的笼统检查要求；`supervisor.py::_mcp_invocation_failure` 已在真实失败结果返回 `remote_outcome:UNKNOWN`、`retry_performed:false` 与区分只读/副作用的 guidance。它报告观察失败但远程操作可能已执行，实际状态出现时再解释即可，不新增预检门槛或结果文字。 |
| META 拒绝、旧 DIRECT 条目 | 调用前保留“ref rejected → inspect”，实际 `_mcp_unavailable_direct_message` 已在未派发/入口被替换时给确切 target 和 invocation.mode 指引；不复制整段恢复手册到常驻说明。 |
| resource 的远程读取与 artifact 分页 | 保留 artifact_read 入口和 fresh read/next page 区别；分页参数不存在是接口事实。既有 `ToolOutputArtifactCandidate` 与结果投影给实际 artifact_id，不新增结果消息。 |
| list 的部分目录/未观察来源 | 以 observed catalog/completeness 正向解释。`CapabilitySourceQuery.execute` 和 `source_query.render_page` 已输出 completeness/diagnostics/next_offset；原结果不裁剪。 |
| 每次 create 前必须完整 list 并 get 候选 | 现行 create/service/repository 不要求先执行目录读取；这是旧 prose 的固定工作流，本次删除。改为按需要用 list/get 取得既有任务和当前 values/revision，再按用户意图更新或另建；expected_revision 与原子 switch 条件原样保留。 |
| schedule 重复 values 元素说明 | 完整 values、prompt、timezone 与 permissions 统一放函数正文；schema 类型和 required 原样保留。 |
| 单 ACTIVE、switch 原子替换、pending 与 running 区分 | 是实际效果与选择依据，保留。不存在“为精简而拆两个调用”的替代路径。原子性意味着失败不提交部分切换，无需重复一段失败恐吓。 |
| 时间规则边缘与默认值 | 保留本地时间/UTC、weekday 对应、DST 与跳过规则、PAUSED 默认、session 默认、过期 once resume，避免靠失败推导。新增 weekday 文字来自现行 `next_occurrence` 的 `d-1`，没有新增功能。 |

**本次没有把尚不存在的指引“移到未来结果”。** 以上结果均已存在，保持原样；删除无需调用前理解的叙述不以新结果机制为前提。资源与 prompt 的 `UNTRUSTED_OBSERVATION` 标签由 `McpInstalledRuntimeGeneration.invoke_standard` 的现行结果包装承担；不新增所有结果都要携带的安全说明，不扩展持久事件、任务或结果大小。

## 6. 静态量化与复现

使用当前 `.venv`、当前生产 import 与 `PulsaraHeuristicTokenEstimatorV3.estimate_json(Responses tools array)`。无 provider 调用，以下不是服务实收 usage、reasoning 用量或认知测量。保留工具按 `[完整 Responses function]` 计；已删除 reload_hooks 的 after=0 表示其定义不再占位，不是对空数组单独计量；group/catalog 按整个数组另算，启发式舍入使各行 delta 相加及 group/catalog 的 delta 可能相差 1。

| 工具 | 当前 | 提案 | delta |
|---|---:|---:|---:|
| `get_mcp_prompt` | 321 | 190 | -131 |
| `inspect_capability` | 810 | 811 | 1 |
| `list_capabilities` | 511 | 495 | -16 |
| `manage_capability` | 1973 | 1504 | -469 |
| `read_mcp_resource` | 292 | 191 | -101 |
| `reload_capabilities` | 117 | 132 | 15 |
| `reload_hooks` | 123 | 0 | -123 |
| `scheduled_tasks` | 2046 | 1690 | -356 |
| `use_new_mcp_tool` | 234 | 182 | -52 |
| **本组工具数组（9→8）** | **6423** | **5192** | **−1231（−19.17%）** |
| **完整目录（40→39），仅替换本组** | **22824** | **21592** | **−1232（−5.40%）** |

复现：

```bash
PYTHONPATH=src .venv/bin/python output/tool-clarity-specs-20261026/capability_schedule/measure.py
```

实施前的 baseline、最终候选、annotation 操作与 measurement 作为已记录证据保留。当前 measure.py 默认从生产导出，核对八份定义与获批候选逐值相等、相对实施前 canonical 的非 annotation 部分不变，输出 implementation_verification.json 与 implemented.responses_tools.json；它不覆盖旧 before。上表完整目录为仅替换本组时的历史比较，当前全目录包含其他组并行修改，另记于验证文件。没有 SHA/指纹产物。当前完整 catalog 文件仅含本组变更，不能覆盖其他工具组并行形成的候选；总规格按工具名合并后应再次计整个数组。

## 7. 可检验的认知假设与取舍

已测得的是体积与静态约束保持。待验证的认知收益是：管理动作从“先读泛化说明，再在 action alternatives 中找字段与用途”变为逐行直接配对；schedule 的顶层动作关系从 parameters.description 移到 function.description；共用 values 含义只读一次；查询、管理、刷新、远程读取的入口更直接。

代价是 manage/scheduled 的 function.description 仍较长，模型仍需将动作行与 schema 的属性并集结合；当前 superset 仍会接受跨动作字段。inspect 增加 1 token，list 节省很少，因为主要成本是完整目标形状；保持它们比盲目消除身份结构更符合“不损失使用”的目标。不能因为本组节省 19.17% 就宣称首次成功率、推理成本或端到端延迟改善。

若后续授权真实比较，应只围绕实际歧义比较同一任务：独立 MCP 与 Plugin 连接、Hook trust 与 enable、META 遇旧 DIRECT、resource artifact 分页、schedule list 与 create 的 null/status 区别、完整 update 与原子 switch。观察工具/动作选择、首轮合法参数、任务语义与需要的修正轮；本次未运行这种比较，不新增付费实验或 activation 门禁。

## 8. reload 合并的完整 hard cut 与核对

用户明确选择一个无参数入口。最终 `reload_capabilities.parameters` 精确保留现有值：

```json
{"type":"object","properties":{},"required":[],"additionalProperties":false}
```

每次调用都走既有全量 owner。`host.py::reload_capabilities` 先经 `_reload_capabilities_serialized` 刷新本地 MCP、Plugin 及其 Skill/Hook 来源，再调用内部 `reload_hooks` 刷新 USER/current-workspace Hook 定义与 trust。其已有 `status`、`local_hooks`、`local_hook_sources` 与 interruption/partial 结果继续如实表达本次执行；全量是请求范围，不改变既有取消和部分完成语义，也不添加新的扫描器或原子事务。

实施删除清单：

- `capability/builtin_catalog.py`：删除 `_BUILTIN_DESCRIPTORS["reload_hooks"]`、`_catalog_shape` 的专属 binding 分支和 `_recovery_contract` 名称项；仅由它使用的 HOOK_CONTROL binding 枚举等在核对无其他使用后删除。保留 Hook 管理动作的权限分类。
- `conversation_kernel/tool_runtime.py`：删除 `_DirectCapabilityControlTool("reload_hooks")` 注册、SUBAGENT 排除清单中的旧名称、授权分支中的旧名称及独立 dispatch/result 分支。`KernelCapabilityReloadPort.reload_hooks` 只为该模型 dispatch 服务，删除该 public port 方法；保留 `reload_capabilities` 和 `adopt_capability_management_change`。
- `conversation_kernel/host.py`：保留现有内部 `reload_hooks` helper 与 `reload_capabilities` 对它的调用，保留必要内部单元测试。不要因删除模型工具而删除 Hook 扫描、trust 采用或内部故障测试。
- `cli.py`：现有 `running_host_refresh` 的 `reload_hooks or restart required` 与 `notice` 的 `running Hosts require reload_hooks or restart` 分别改为 `reload_capabilities or restart required`、`running Hosts require reload_capabilities or restart`。其他当前用户说明同步到唯一入口；历史证据不改。
- 相关目录/注册/dispatch 测试改为最终 39 名工具及无参数唯一刷新入口，移除只覆盖已删除模型入口的用例；保留覆盖内部 helper 和全量入口调用链的用例。不要添加 legacy alias、转发旧 tool name、target 参数、双入口或 compatibility wrapper。

该 hard cut 通过通常的新构建/进程采用，新的 provider 定义仍只在 cold epoch 或显式 compaction successor 安装；不通过 reload 重写任何已安装 epoch 的 SYSTEM/tools/messages。没有新的前缀边界。

### 8.1 实施顺序与比例相称的核对

1. 完成下述 reload 合并 hard cut，在 builtin_catalog 安装八个最终 description；在原 schema builders 应用第 4 节 annotation 编辑，删除已无用途的管理动作说明常量。不要新增全局 description 重写层、运行时 wrapper 或 fallback。
2. 用现有 lowerer 重新导出。核对八个保留工具的原始与提案 canonical 去掉 schema annotation 后完全相等，完整 JSON Schema 自检通过；对业务字段名 `description` 保持属性定义。这些检查已由本次 measure.py 执行，证据见 measurement.json。
3. 实施时运行与实际修改直接相关的既有 `tests/test_capability_management_intent.py`、`tests/test_capability_source_query.py` 以及 `tests/test_builtin_default_null_contracts.py` 中 scheduled 相关 schema/dispatch 测试。若有旧文案字符串断言，只更新到本规格的精确新文案；条件字段、nullable、权限和结果断言保持原强度。既有真实接口实验不在本次实施中重跑。
4. 复用已有 prefix 冻结测试确认新描述只随合法新 epoch 安装；代码路径若未涉及 epoch，不为本次文案新建生命周期框架或长程门禁。交付清楚区分“静态 schema 约束保持”与“尚未测模型行为”。

静态约束检查覆盖：八个保留工具的 canonical 非 annotation 部分逐值相等、原生 lowerer 成功、最终 JSON Schema 自检、annotation patch 重放精确一致、同名属性和 literal data 保持、完整定义计量、本组9→8/目录40→39及reload参数仍为空。具体运行验证见下一节。无已知未解决的产品/字段边界问题；认知收益与第三方 provider 对完整八份文本的采用仍是尚未验证的假设。

## 9. 实施记录

已在原 owner 中替换八工具的正文与字段 annotation，删除管理 action 重复说明常量、scheduled 根动作说明；canonical 条件字段/default/null/permissions 不变。reload_hooks 模型 descriptor、binding 枚举、注册、dispatch、public port、CLI 提示以及前端摘要分支已删除；host 内部 helper 与相关测试保持。旧模型入口的 Hook 集成用例改为全刷新 reload_capabilities，沿用原视图与前缀断言。未增加别名或 target 参数。

验证：

- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_capability_management_intent.py tests/test_capability_source_query.py tests/test_builtin_default_null_contracts.py tests/test_app_capability_boundary.py tests/test_capability_change_observation.py -k 'not terminal_flattening'`：**232 passed，2 deselected**。排除的是 ROOT 同时负责更新的两个 terminal 旧文案位置断言；本组 scheduled 断言已改到 function.description，继续精确核对每动作 required/allowed 集合。
- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_round9_2_hook_integration_postgres.py::test_round9_2_reload_uses_exact_predecessor_for_own_pre_and_post`：**1 passed**。本地隔离 PostgreSQL、脚本模型；证明刷新自身 pre/post 使用前驱 Hook view，后续工具使用新 view，SYSTEM/tools 固定且 messages 保持前缀。无 provider 调用。
- `PYTHONPATH=src .venv/bin/python output/tool-clarity-specs-20261026/capability_schedule/measure.py`：八个生产定义和获批候选逐值相等、保留 canonical 验证合同，唯一 reload 输入保持 `{}`，模型入口计数 39；结果保存于 implementation_verification.json。

前端摘要测试行已改为全刷新入口的实际成功文案，前端测试/build 与其他组的最终全量计量由 ROOT 统一执行，避免重复写入构建产物。
