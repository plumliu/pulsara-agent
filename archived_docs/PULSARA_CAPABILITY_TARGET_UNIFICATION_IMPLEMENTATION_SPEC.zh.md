# Capability 管理目标统一实施规格

日期：2026-10-28。状态：生产 hard cut 已实施，公开请求、结果及消费者采用本规格。验证与范围见[实施报告](../output/target-hardcut-implementation-20261028/capability/REPORT.zh.md)；整体结果见[汇总](../output/target-hardcut-implementation-20261028/root/REPORT.zh.md)。

## 1. 产品决策与范围

模型在 `list_capabilities`、`inspect_capability`、`manage_capability` 之间使用同一种 `target` 表达能力身份。列表中的目标可以直接用于查看或该目标支持的管理动作；已知确切身份时可直接操作，无需先走一次查询。

MCP 的 META 工具调用、资源读取（含模板）和 prompt 获取也采用 target：读取或获取直接使用对应对象目标，META 调用使用 inspect 返回的 `invocation.target`。各工具只接受其职责对应的目标类型，不把所有 target 塞进一个通用执行器。直接暴露的 MCP 工具仍使用自己的业务参数。

保留三个工具、15 个管理动作名称及其现有业务语义。管理参数采用 `action + target + 操作参数`。只有安装独立 Skill 和安装 Plugin 两类由来源内容决定新身份的操作，使用 `scope + source_path` 等安装参数，不提供 target。`ADD_LOCAL_MCP` 使用 LOCAL MCP_CONNECTION target 表示拟创建的连接地址，server_id 由调用方指定；target 不表示对象已经存在。

目标统一不合并安装身份与运行时身份，不把配置、版本条件或凭据放入 target。Skill、Hook、Plugin、MCP 的原生服务仍各自负责身份观察、资格判断、变更和副作用结算。没有新的目标注册表、持久化对象、管理许可或生命周期。

本轮只实施 capability 目标合同。task/subagent 的工具、数据模型、历史继承、profile、消息和 monitor 链路暂不变。未来可以复用“列出／查看／管理”的习惯，本规格不提前冻结协作工具名称和参数。

### SUPERSEDES

本规格实施时，仅在以下主题内替代旧规格：

- 能力来源查询规格中安装配置与运行时共用 target.kind=MCP_SERVER 的合同：安装配置采用 MCP_CONNECTION，运行时保留 MCP_SERVER；目录类别过滤不变。

- [能力与定时任务工具清晰化规格](PULSARA_CAPABILITY_SCHEDULING_TOOL_CLARITY_IMPLEMENTATION_SPEC.zh.md) 中 manage_capability 的扁平身份入参、统一顶层 scope、相应正文与字段说明；保留其他工具、原生行为和 adapter 路线。
- [能力来源查询规格](PULSARA_CAPABILITY_SOURCE_QUERY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md) 中管理结果使用 identity 作为后续目标来源的叙述。既有查询 target、parent、调用方范围和运行时关联合同保留。
- 能力与定时任务工具清晰化规格中的 read_mcp_resource/get_mcp_prompt 扁平请求、use_new_mcp_tool 顶层 tool_ref，以及对应 inspect META 返回路径和调用示例，由本规格第 11 节替代。原生 SDK 操作、引用绑定、DIRECT/META 路由、授权、结果和生命周期保留。

这是一次新请求/结果合同的 hard cut：不接受旧管理调用别名，不双写 scope/identity 与 target。已提交的历史工具消息不是待迁移的请求，不重写。已安装 epoch 的 SYSTEM/tools 保持原值、messages 只追加；新定义仅在既有 cold epoch 或明确采用的 compaction successor 边界安装。

## 2. 实验依据与 schema 写法

依据 [190 次 Responses 实验报告](../output/tool-schema-compatibility-20261026/REPORT.zh.md)、[压缩讨论](../output/tool-schema-compatibility-20261026/COMPRESSION_DISCUSSION.zh.md) 和已实施的工具清晰化规格：

1. 两个真实目标是 DashScope qwen3.8-flash、DeepSeek deepseek-flash，均为 Responses、strict:false。不能用 OpenAI 文档替代第三方接口的实际表现。
2. 原始生产根 union 在 Qwen 失败；展平可用，但观察到跨 action 字段混用。继续使用现有 lowerer，不把精确根 union 直接发送给 provider。
3. 必要 action/字段关系写在 function.description；不能只依赖 parameters.description。schema 保留类型、枚举、required、范围、null 和闭合对象；本地 parser 精确判断动作与目标组合。
4. 内联嵌套 union 在旧实验中是有实证的候选，不能据此声称本次 target 定义已经过 provider 验证。不引入 `$defs/$ref` 压缩、provider 特判、fallback 或新的 request 包装层。

本次精确 canonical schema 有 20 个根分支：15 动作，三个 Hook 动作各细分 LOCAL/PLUGIN，两个 OAuth 动作各细分 LOCAL/PLUGIN。每个分支只包含该动作允许字段，additionalProperties=false；各 target 也闭合。复用现有 target 形状生成代码，不在序列化 schema 中使用引用。

现有 lowerer 展平根 action 分支，target 内联分支转成 anyOf。最终 wire 的 required 不再表达完整动作关系；函数说明中的动作行和本地精确 parser 一起承担这部分合同。不得仅因 wire 接受某个组合就执行它。

冻结附件：

- [精确 canonical schema](../output/capability-target-unification-20261028/root/after.canonical.json)
- [通过当前生产 lowerer 的 wire schema](../output/capability-target-unification-20261028/root/after.wire.json)
- [全部 20 分支的最小调用示例](../output/capability-target-unification-20261028/root/examples.json)
- [可复算生成脚本](../output/capability-target-unification-20261028/root/design_schema.py)

查询 target/parent 的六个正例及旧安装名称拒绝检查见 [查询示例](../output/capability-target-unification-20261028/root/query.examples.json)。

这些是本次设计交付物，不是生产导入依赖或冻结文本断言测试。实施后由生产定义生成 schema；若实施中需要改变合同，应先同步规格。

## 3. Target 的精确形状

| 目标 | 必填字段（除 kind 外） | 使用范围 |
|---|---|---|
| `SKILL` | skill_path（绝对 SKILL.md 路径） | 查询；符合原生资格的独立 Skill 启停/删除 |
| `PLUGIN` | scope、plugin_id | 确切 Plugin 查看、整体启停/删除 |
| `HOOK_SOURCE`，LOCAL | scope、source_kind=LOCAL | 本地 Hook 来源的信任/撤信/启停 |
| `HOOK_SOURCE`，PLUGIN | scope、source_kind=PLUGIN、plugin_id | Plugin Hook 来源的信任/撤信/启停 |
| `MCP_CONNECTION`，LOCAL | scope、source_kind=LOCAL、server_id | 独立连接创建/更新/删除/OAuth |
| `MCP_CONNECTION`，PLUGIN | scope、source_kind=PLUGIN、plugin_id、server_id | Plugin 内连接配置/OAuth，server_id 是包内 ID |

scope 的值仍为 USER、WORKSPACE。SKILL、PLUGIN、HOOK_SOURCE 复用现有形状；MCP 安装配置统一为 MCP_CONNECTION，运行时实例使用 MCP_SERVER。管理仅接受安装配置目标，不接受运行时实例目标。

### 3.1 MCP 配置与运行时身份

```json
{"kind":"MCP_CONNECTION","scope":"USER","source_kind":"LOCAL","server_id":"docs"}
{"kind":"MCP_CONNECTION","scope":"WORKSPACE","source_kind":"PLUGIN","plugin_id":"demo","server_id":"docs"}
{"kind":"MCP_SERVER","runtime_server_id":"runtime-id"}
```

MCP_CONNECTION 指已配置或拟创建的安装地址；MCP_SERVER 指当前调用范围中的运行时实例。LOCAL/PLUGIN 仍由 source_kind 表示，不另建两种连接类型。两种目标均可 inspect，也可作为现有远端目录查询的 parent；只有 MCP_CONNECTION 可用于连接管理，只有 MCP_SERVER 可用于 open_in_pulsara 的 MCP 展示。安装详情的 runtime_target 仅在原有精确关联成立时给出 MCP_SERVER，不能由 server_id 字符串猜测。

`list_capabilities.kind=MCP_SERVER` 仍是 MCP 服务器目录的类别过滤，包含当前实现可观察的安装配置和运行时信息；它不是 target 的判别器。本次不拆列表、不增加发现范围。目录行 kind 保持该类别，行 target.kind 明确选定对象：安装行为 MCP_CONNECTION，运行时行为 MCP_SERVER。无过滤概览、排序、分页、去重和 completeness 保持现有语义；不能为了名称拆分再多发一份原本已合并的 runtime 行。

query 的 target/parent、管理入参和结果、source.target、目录 detail 与表单/OAuth 消费者都采用这一身份区分。精确安装 lookup、Plugin parent 与子项查找、runtime_ids 关联使用新的完整目标键；`runtime_details[("MCP_SERVER", runtime_id)]` 等原生运行时索引不改。管理/查询 parser 按 kind 判断安装或运行时，再检查对应闭合字段；安装形状的 kind=MCP_SERVER 不作为别名接受。SUBAGENT 仍只能访问自己的 scoped runtime；这项公共类型改名不扩展其工具链或安装源访问。

其他查询目标继续存在，但不能传给 manage：runtime MCP_SERVER `{kind,runtime_server_id}`，以及 MCP_TOOL、MCP_RESOURCE、MCP_RESOURCE_TEMPLATE、MCP_PROMPT。不从运行时 ID 拆字符串猜安装位置，不用当前同名配置替代仍被旧 epoch 持有的连接。ROOT 需要管理连接时，从安装来源目录取得相应安装 target。SUBAGENT 查询范围保持不变。

PLUGIN 仍不是 list 的 kind。Plugin 目标来自 `source.target`、成功安装结果或已知确切身份；不新增全量 Plugin 盘点接口。Plugin Skill 的 target 可用于 inspect，但不因此获得独立管理资格；管理整个 Plugin 时使用它的 `source.target`。bundled Skill 保持只读。

## 4. 全动作与分支矩阵

以下“其他参数”不含 action/target；标注可选的字段可以省略，其余必填。每行均适用于原实现允许的 USER/WORKSPACE 范围，具体路径条件见第 5 节。

| 动作 | target / 安装输入 | 其他参数 | 必须保留的路径与效果 |
|---|---|---|---|
| INSTALL_LOOSE_SKILL | 无 target；scope、source_path | 可选 name、description | 原生验证与规范化；发布到新目标目录，不覆盖既有目录；成功后返回实际 SKILL target |
| SET_LOOSE_SKILL_ENABLED | SKILL | enabled | 按真实路径解析作用域，再调用原生启停观察与提交；不要求 Skill 当前是同名 winner |
| REMOVE_LOOSE_SKILL | SKILL | 无 | 原生目录删除资格；无/坏 SKILL.md 但受管理目录存在时仍可删除；保留清理结果 |
| TRUST_HOOK_SOURCE | HOOK_SOURCE LOCAL | 无 | 观察当前本地定义，用户审阅后信任该观察；不是启用开关 |
| TRUST_HOOK_SOURCE | HOOK_SOURCE PLUGIN | 无 | 观察确切 Plugin 的当前 Hook 定义；不信任其他包，也不代替 Plugin 启用 |
| REVOKE_HOOK_TRUST | HOOK_SOURCE LOCAL/PLUGIN（两分支） | 无 | 撤销该来源信任，定义保留；分别走原有本地/Plugin owner |
| SET_HOOK_SOURCE_ENABLED | HOOK_SOURCE LOCAL/PLUGIN（两分支） | enabled | 只改来源启用选择；信任、来源开关、Plugin 开关保持独立 |
| ADD_LOCAL_MCP | LOCAL MCP_CONNECTION（拟创建） | 可选 config | 目标必须是新连接；config 省略开编辑器，对象是完整原生配置；不是更新 |
| UPDATE_LOCAL_MCP | LOCAL MCP_CONNECTION | 可选 config、expected_identity | 目标须存在；省略 config 开预填编辑器，对象完整替换；保留配置冲突与凭据目标确认 |
| REMOVE_LOCAL_MCP | LOCAL MCP_CONNECTION | 可选 expected_identity | 删除配置条目及专属本地凭据，不卸载服务器程序；保留清理状态 |
| INSTALL_PLUGIN | 无 target；scope、source_path | 可选 replace、source_format | replace 缺省 false；格式 native/claude/codex/cursor，缺省 native；来源决定身份；安装后禁用，replace 保留原生替换审阅与效果判断 |
| SET_PLUGIN_ENABLED | PLUGIN | enabled；可选 expected_package_install_id | true 时保留组件审阅、凭据/外部进程接受路径；false 走既有关闭语义 |
| REMOVE_PLUGIN | PLUGIN | 可选 expected_package_install_id | 以已安装实例观察为依据；内容损坏不阻止移除；删除专属凭据，保留数据及原始来源 |
| CONFIGURE_PLUGIN_MCP_CONNECTION | PLUGIN MCP_CONNECTION | 可选 overlay、expected_overlay、expected_package_install_id | 组件必须属于该 Plugin；overlay 省略/null/对象三分支见下；保留包拥有的执行字段 |
| AUTHORIZE_MCP | LOCAL MCP_CONNECTION | 可选 expected_identity | 连接已配置 OAuth；浏览器授权、取消及真实结果结算沿现有流程 |
| AUTHORIZE_MCP | PLUGIN MCP_CONNECTION | 可选 expected_package_install_id | 确切包内连接已配置 OAuth；保留包实例身份检查和 credential owner |
| CLEAR_MCP_AUTHORIZATION | LOCAL MCP_CONNECTION | 可选 expected_identity | 清除现有本地 OAuth 授权；配置与远端 token 保留；沿当前 owner 的 auth 资格处理 |
| CLEAR_MCP_AUTHORIZATION | PLUGIN MCP_CONNECTION | 可选 expected_package_install_id | 同上，归属确切 Plugin 连接；不允许 local 配置 guard |

本次不把这 15 个动作改名为通用 SET_ENABLED/REMOVE/INSTALL。它们的操作对象和副作用有实际差异；统一身份格式已经消除了搬字段的主要负担，无需同时改变动作词汇。

## 5. 不能藏在 target 里的语义

### 5.1 Skill 的作用域与动作资格

SKILL target 保持 `{kind,skill_path}`。作用域由原生 Skill owner 根据当前受支持根与确切路径解析，模型无需额外提供 scope。pure parser 只检查形状和绝对路径，不读取文件系统、不猜作用域。

| 来源根 | 启停 | 删除 |
|---|---|---|
| USER_PULSARA | 按现有定义/配置可用性 | 按原生目录资格 |
| USER_AGENTS | 按现有定义/配置可用性 | 按原生目录资格 |
| WORKSPACE_PULSARA | 按现有定义/配置可用性 | 按原生目录资格 |
| WORKSPACE_AGENTS | 按现有定义/配置可用性 | 不支持 |
| Plugin / bundled / 非受支持根 | 不支持独立启停 | 不支持独立删除 |

启停必须覆盖 WORKSPACE_AGENTS，不能借用只检查 removal 范围的 `inspect_loose_skill_directory` 作为所有动作的解析器。最小实现是在 `LocalSkillManagementService` 内抽取按现有 root policy 定位 scope 的小 helper，启停复用其候选/配置观察，删除复用 `_removal_target` 与目录观察；不得把路径字符串前缀当原生资格。配置不可用、定义无效、目录不在支持根等错误仍按各原生操作处理；不能通过失败后换 scope 重试掩盖错误。

解析后的 scope 放在当前 prepared invocation 中，workspace_root 由该 scope 与服务现有 workspace 绑定推导，供 effects、form、执行、再次 preparation 和 adoption 共用。它是当前调用派生值，不是新的持久身份。public intent 的序列化仍是新 target 格式，不把旧顶层身份字段注回请求。

解析发生在当前 preparation/权限 effects 判断之前；不能推迟到 execute。启停保留 producer 的根别名/观察不可用判断；删除不新增全目录扫描或启停配置门禁。若确切路径在支持根中实际对应两个不同的管理 scope，报告目标归属不明确，不按 USER 优先或最长字符串前缀选一个配置文件。

### 5.2 Scope 不改变当前可操作范围

保留现有 preparation 的具体 workspace 条件，不把查询接口的 GUI-project 限制机械套到所有管理动作。当前 Skill/Hook/Plugin 安装、启停、删除和 Plugin 连接配置有相应 project 检查；独立 MCP 与 OAuth 并未使用完全相同的门禁。目标重构本身不扩大或收缩这些产品路径。

scope 代表当前 Host 的 USER 或既有 WORKSPACE 所有者；不增加任意 workspace 路径入参。解析使用本次服务绑定的 workspace，不扫描其他项目。安装动作顶层 scope 表示目的作用域，其他动作的作用域来自 target（SKILL 由 owner 解析）。两套位置不能同时提交。

### 5.3 配置与版本条件

- config 省略：打开原生编辑器，UPDATE 预填当前值；config 对象：完整替换配置。config:null 非法，不能解释成清空。
- overlay 省略：预填 Plugin 连接编辑器；overlay:null：恢复 package/input 默认；overlay 对象：完整替换 overlay。对象里的组件 ID 必须与 target.server_id 一致，继续由原生 overlay parser 校验。包拥有的 command/args/cwd/transport 不变。
- expected_overlay 省略：preparation 观察当前 overlay；null：明确要求当前无 override；对象：匹配该已观察 overlay。不得把 null 当省略。
- expected_identity 与 expected_package_install_id 仍为可选操作条件，省略时按现有 preparation 观察；显式提供则检查。两者不进入 target，也不能互换。Plugin OAuth 只接受后者，LOCAL OAuth 只接受前者。
- AUTHORIZE 要求当前 auth 为 OAuth；CLEAR 不新增这个前提，仍可按现有 owner 清理授权。不能为了共享目标解析把两个动作的资格收紧成同一条件。
- name/description 是 Skill 安装覆盖值；replace/source_format 是 Plugin 安装选项；enabled 是本次开关值。它们均不是身份。
- 凭据仍由用户在私有编辑器提交。模型参数不增加 secret 字段；公开 config/overlay、secret_changes、credential owner、目标变更确认和原生校验边界保持。

### 5.4 权限、审阅与生效

管理仍 ROOT-only，所有 permission mode 的现有 preparation/表单入口保持。READ_ONLY 通过现有用户提交表单；其他模式继续依据原生解析 effects 决策。target 不是权限或此前已观察凭证；已知确切 target 可直接调用，但仍做当前原生观察。

复用查询 target schema 不等于直接用 JSON Schema validator 替换管理 parser。管理身份字符串沿原规则要求 strip 后非空，Skill/source 路径为绝对路径；不静默 trim/改写 ID。config/overlay 的公开对象继续交对应 native parser，不能由 target schema 推断其业务合法性。

Hook 信任审阅、Plugin 启用审阅、需要编辑配置/补凭据、OAuth 浏览器等待都沿既有路径。取消、冲突、部分结果和已发生副作用结算不改变。APPLIED 后使用现有 adopt_capability_management_change；adoption 部分失败不得将已应用变更重放。

TRUST_HOOK_SOURCE 保留完整、非空定义与可用 trust assessment 的前提；REVOKE_HOOK_TRUST 不新增相同前提。Plugin Hook 执行仍比较当前安装、启用状态和定义 snapshot，不因公开 target 形状相同而共用错误的资格条件。

## 6. 查询、结果与桌面消费者

### 6.1 查询

查询的 target/parent 中，安装配置使用 MCP_CONNECTION；运行时实例保持 MCP_SERVER。分页、发现范围、ROOT/SUBAGENT 可见范围与精确关联保持。list 的说明改为：

```text
Find Skills, Hooks, MCP servers, tools, resources, templates and prompts. Use {} for an overview, or filter the list. Copy an item's target into inspect_capability or a supported manage_capability action; use source.target as parent to find a Plugin's components. Continue with next_offset and the same filters. Results describe the observed catalog and its completeness. SUBAGENT callers can query only their assigned MCP runtime.
```

inspect 仍只读 metadata；Skill 正文、MCP resource/prompt、DIRECT/META 执行入口各自保持。其正文中 `returned input_schema and tool_ref` 改为 `returned input_schema and invocation.target`，其余保留；完整六工具候选见第 12 节附件。

### 6.2 管理结果

成功或已知目标的管理结果返回 target，替代原顶层 scope + identity。status/action/current/adoption、诊断及真实 OAuth authorization 内容保持。target 只回答“操作哪个对象”，不证明对象当前存在。

- 对 target 动作，返回原请求的确切 target，包括 ADD 的目标及 REMOVE 后已不存在的目标。
- Skill 安装成功，从原生 outcome.destination_path 生成实际 SKILL target。
- Plugin 安装成功，从原生 outcome.identity 生成实际 PLUGIN target；不得用来源目录名推测。
- 两个安装动作失败且没有已确认安装目标时省略 target，不生成空 target 或从预期值冒充已安装对象。原有无目标的取消/异常结果可以保持无目标。

例如：

```json
{"status":"APPLIED","action":"SET_LOOSE_SKILL_ENABLED","target":{"kind":"SKILL","skill_path":"/example/skills/demo/SKILL.md"},"current":{"path":"/example/skills/demo/SKILL.md","enabled":true},"adoption":"NOT_APPLICABLE"}
```

这里 adoption 仅展示结果形状；真实执行后仍由原有 runtime adoption owner 写入实际值。不得为了删除结构重复而删掉 current 中具有操作结果意义的字段。

### 6.3 表单与 OAuth UI

`public_prefill` 采用新请求形状，可另外带现有 editor 所需公开观察。form.scope 仍是 preparation 已解析的显示/路由信息，不是要求模型再次提供 scope。UI 从 prefill.target 取 server_id/plugin_id/skill_path，从安装 prefill 取 source_path；原生 connection、plugin、credential_owner、expected_current 结构按现有职责保留。

表单提交仅能修改既有可编辑 config/overlay、私有凭据及确认选择；不能替换 target/action/安装scope/source。重备与 `_same_target` 明确比较原公共 target（安装动作比较 scope/source）、prepared 的 resolved_scope 及其推导的 workspace_root，并继续比较现有原生观察。只比较 Skill inode/removal observation 不能替代新解析作用域的一致性。这里比较已有调用值，不新增 guard 或注册表；不得将表单数据展平后交回已删除的旧模型请求 parser。

`frontend/lib/mcp-authorization-result.ts` 改从结果 target.scope 读取作用域，要求 MCP_CONNECTION 安装目标；仍保留完整结果、APPLIED、current.authorized、credential owner、config/display identity、package_install_id 的原有校验。解析给现有组件的内部 `{scope,authorization}` 可保留；不需要重构整个 OAuth UI。LOCAL/PLUGIN target 与返回 credential owner 的服务器/包身份应一致。

## 7. 实施文件与责任边界

| Owner / 文件 | 必要修改 |
|---|---|
| capability/source_query.py | 抽取可复用的单种 target schema 构造；安装配置目标为 MCP_CONNECTION、运行时目标为 MCP_SERVER；更新 query/parent parser，管理只取需要的类型 |
| capability/management_intent.py | 新公共 intent 的 target/安装scope 分支，20 分支 schema、精确 parser；保留专用参数和 null 规则；移除旧扁平模型入参 |
| capability/local_skill_management.py | 在原生 root policy 内解析 Skill 动作作用域，保持启停与删除资格差异 |
| conversation_kernel/capability_query.py | 安装 target/source.target 改为 MCP_CONNECTION；同步精确 lookup/parent 和 runtime_ids 关联；目录类别过滤、原生 runtime 索引与合并去重保持 |
| conversation_kernel/capability_management.py | 读取新 intent；prepared 保存解析 scope/root；用现有 native request/target 调用原生 owner，不建立旧请求兼容层 |
| conversation_kernel/capability_management_execution.py | form/prefill/reprepare、动作字段读取、结果 target、Skill/Plugin 安装实际身份、OAuth result；保留副作用结算 |
| capability/builtin_catalog.py | manage 正文、list 指引；不改变 owner/permission/可见性 |
| frontend/components/capability-interaction-editor.tsx | 从 target 取身份，保留表单已解析作用域及现有编辑器 |
| frontend/lib/mcp-authorization-result.ts | 新结果目标解析，保留授权结果的实际证明字段 |
| bundled_skills/pulsara-{skill,mcp,plugin}-installer | 管理及 MCP 调用示例和对应 references 使用新请求；安装两类 scope 及其专用参数保留 |
| 现有管理/查询/权限/前端测试 | 更新新合同输入输出，验证行为；不写冻结文本或 token 数断言 |

内部 `LocalMcpTarget`、Plugin request/identity、Hook snapshot、Skill observation 等类型可继续使用各原生 owner 的字段。统一的是模型公共目标和查询/管理交接，不要求把所有内部 SDK/request 都改成同一个 JSON DTO。

公共 intent 的逻辑组成固定为 action、可选 target、仅用于两个安装动作的可选 scope、该动作其余 fields；target 与安装 scope 互斥。target 使用已验证的闭合值，不新增通用目标框架。prepared invocation 持有必有的 resolved_scope；服务已绑定的 workspace_root 按该 scope 取用，不另存第二套可独立变化的工作区配置。intent.to_dict() 输出公共请求，form.scope 输出 prepared 的已解析值。

调用入口只接受新公共格式。intent.to_dict()、表单重备和执行不应双向转换旧公共 JSON；如需要 native 参数，只在调用对应原生 owner 时读取 typed target。不要以“兼容内部”为名保留旧扁平 parser。

现有 tool_runtime 的权限、adoption、runner 的 action→catalog 选择、Hook 输入和真实工具日志也要核对；action 名称未变通常不需要改其判断。desktop 普通安装/管理原生入口与 CLI 的原生请求格式不属于模型接口重构，不为统一外观修改它们。

## 8. Token 与认知取舍

计量使用仓库 `PulsaraHeuristicTokenEstimatorV3`、当前生产 lowerer、单个 manage_capability 的完整 tools 数组。不是 provider 实收 usage，不是整个 SYSTEM+tools。

| 计量项 | 当前 | 候选 | 差值 |
|---|---:|---:|---:|
| canonical parameters | 3124 | 2914 | −210 |
| 实际 lowering 后 parameters | 797 | 994 | +197 |
| function.description | 679 | 784 | +105 |
| Responses 单工具 tools 数组 | 1504 | 1806 | +302 |
| Chat Completions 单工具 tools 数组 | 1507 | 1809 | +302 |

统一 target 在此候选中增加常驻输入，不能宣传成节省 token。减少的是调用时从查询结果拆字段、补作用域、区分同名身份的工作；target 自身仍必须表达不同对象的必要身份，因此其嵌套结构有成本。canonical 较小也不代表 wire 较小。

本方案选择保留这笔 +302 的明确成本，以换取跨查询/管理可复制的目标。它没有以缩写、模糊 union 或删掉条件换取表面压缩。list 正文约 98→108（+10）；list/inspect/manage 三工具合计 Responses tools 数组 **2809→3125（+316）**。这仍不是整个 builtin 目录。完整数字见[测量结果](../output/capability-target-unification-20261028/root/measurements.json)。

## 9. 验证与完成标准

本次已做设计离线检查：20 个分支最小正例通过 proposed canonical 与当前 lowerer 输出；34 个错位/多余字段/错误目标/null 等反例被 proposed canonical 拒绝；9 个 overlay 与 expected_overlay 三态组合通过形状检查，其中对象占位不声称通过 native overlay parser。这仅证明设计 schema 的这些样例，不证明新 production parser、执行器或真实 provider 已通过。没有修改/运行生产功能测试，也没有调用 provider。

实施时验证以下行为：

1. 20 分支的合法调用、必填遗漏、跨动作字段、目标类型错误、LOCAL/PLUGIN guard 互串、绝对路径、布尔类型、null/缺省。schema 负责形状，parser 保留既有附加语义：例如只含空白的 ID、相对 skill_path 可能通过复用的 query schema，却必须被管理 parser 拒绝；不宣称二者接受集合完全相同。旧扁平管理格式拒绝。
2. list/inspect 的原样目标可以用于相应 manage；已知目标无需先 list；runtime-only/远端对象不能管理。MCP_CONNECTION 与 MCP_SERVER 的 query/parent 及结果互相衔接；旧安装 kind 拒绝，安装详情只按精确关联提供 runtime_target；目录分类与去重不变。SOURCE/PLUGIN ID 与 runtime ID 不混用；Plugin 全量盘点与 SUBAGENT 范围不扩大。
3. Skill 四类根的启停/删除矩阵，坏或缺 SKILL.md 的可删目录、无资格路径、配置不可用、重复名称但不同路径。结果 target 是实际安装目录。
4. 本地/Plugin Hook 信任、撤信、启停各自的状态和审阅；Plugin 启用/关闭、损坏包删除、安装 replace/format；scope 现有路径不变。
5. MCP 创建/更新/删除；config 省略/对象/null；overlay 三态、expected_overlay 三态、组件 ID 一致、guards 冲突；凭据编辑与目标确认不变。
6. LOCAL/PLUGIN OAuth 授权与清除、非 OAuth 配置、取消、前端读取新结果；private credentials 不进入模型结果。表单不得修改目标；stale preparation 不执行错误对象。
7. APPLIED、CONFLICT、PARTIAL、REJECTED、CANCELLED 与 adoption 路径；作用域 effects 和 permission mode；已发生副作用保留，不重放。
8. 两种 OpenAI 协议都经同一现有 lowerer，strict:false；已有 epoch 前缀连续性不受定义发布影响。

实现完成后，用既有保存连接对 Qwen 3.8 Flash 与 DeepSeek Flash 做一次有针对性的 Responses schema/填参复核，覆盖六种管理 target、两个安装例外、ADD新身份及关键 guard/null 差异，以及第 11 节的调用引用、具体资源、资源模板和 prompt 四种执行目标。只提供模拟工具、不执行真实管理或远端副作用，沿既有实验 harness 保存真实请求/输出；独立请求并行。它是本次新公共 shape 的验证，不扩张为通用兼容性平台，不通过增加付费轮次追求零错误。若需要改 schema 路线，更新本规格，而非暗加 provider 分支。

验收关注行为与真实参数，不以英文描述逐字、固定 token 数或整份 JSON 快照断言取代行为验证。规格未冻结前，生产代码保持不变。

## 10. 模型可见 manage_capability 正文

以下英文正文与附件 `manage_description.txt` 一致；实验来源和内部解析过程不写入模型说明。

```text
Install and manage capabilities requested by the user. ROOT agent only; available in every permission mode.
Set action and send only its listed fields; fields are required unless marked optional. Copy targets from list/inspect or a management result; a known exact target can be used directly. For ADD_LOCAL_MCP, construct a LOCAL MCP_CONNECTION target with the destination scope and new server_id. Installation targets use source-local IDs, not runtime_server_id; runtime and MCP tool/resource/prompt targets are for inspection, not management.
INSTALL_LOOSE_SKILL: scope, source_path; optional name, description. Install an independent Skill into a new destination.
SET_LOOSE_SKILL_ENABLED: target (SKILL), enabled. Toggle an eligible independent Skill.
REMOVE_LOOSE_SKILL: target (SKILL). Remove an eligible independent Skill directory.
TRUST_HOOK_SOURCE: target (HOOK_SOURCE). Review and trust current Hook definitions.
REVOKE_HOOK_TRUST: target (HOOK_SOURCE). Remove trust, keeping definitions.
SET_HOOK_SOURCE_ENABLED: target (HOOK_SOURCE), enabled. Toggle the Hook source.
ADD_LOCAL_MCP: target (LOCAL MCP_CONNECTION); optional config. Add an independent connection.
UPDATE_LOCAL_MCP: target (LOCAL MCP_CONNECTION); optional config, expected_identity. Replace an independent connection's settings.
REMOVE_LOCAL_MCP: target (LOCAL MCP_CONNECTION); optional expected_identity. Remove the entry and dedicated local credentials, keeping the server program.
INSTALL_PLUGIN: scope, source_path; optional replace, source_format. Install the whole Plugin disabled; enable separately.
SET_PLUGIN_ENABLED: target (PLUGIN), enabled; optional expected_package_install_id. Toggle the whole Plugin, with component review on enable.
REMOVE_PLUGIN: target (PLUGIN); optional expected_package_install_id. Remove the Plugin and dedicated credentials, preserving its data and original source.
CONFIGURE_PLUGIN_MCP_CONNECTION: target (PLUGIN MCP_CONNECTION); optional overlay, expected_overlay, expected_package_install_id. Configure a Plugin connection.
AUTHORIZE_MCP: target (LOCAL or PLUGIN MCP_CONNECTION). Authorize an already configured OAuth connection.
CLEAR_MCP_AUTHORIZATION: target (LOCAL or PLUGIN MCP_CONNECTION). Clear local OAuth grants, keeping configuration and remote tokens.
For both OAuth actions, optional expected_identity applies only to LOCAL targets; optional expected_package_install_id applies only to PLUGIN targets. For MCP_CONNECTION and HOOK_SOURCE targets, LOCAL/PLUGIN selects source_kind.
Use inspect_capability for eligibility. Plugin Skills are managed through their source Plugin; bundled Skills are read-only. Hook trust, Hook enablement and Plugin enablement are separate settings. Edit loose Skill content with file tools or terminal; pulsara-skill-creator describes authoring.
The runtime collects required review and confirmation; READ_ONLY mutations use a user-submitted form. Users enter credentials privately in the editor. Wait for the result: status reports the mutation, current the resulting state, and adoption its availability for future use. A completed mutation needs no routine reload or replay.
```

## 11. MCP 执行目标统一

### 11.1 对象目标与调用目标

target 表示该操作选定的对象，可以是按名称/URI定位的对象，也可以是已解析的调用引用。target 的类型由消费工具定义，不要求每类 target 都能交给 inspect 或 manage。

| 入口 | target 类型 | 其他请求参数 |
|---|---|---|
| inspect_capability | MCP_CONNECTION、MCP_SERVER、MCP_TOOL、MCP_RESOURCE、MCP_RESOURCE_TEMPLATE、MCP_PROMPT 等查询目标 | 无新增参数 |
| use_new_mcp_tool | MCP_TOOL_REF：tool_ref | arguments，必填对象，无参数时 `{}` |
| read_mcp_resource | MCP_RESOURCE：server_id、uri | 无 |
| read_mcp_resource | MCP_RESOURCE_TEMPLATE：server_id、uri_template | uri，必填具体 URI |
| get_mcp_prompt | MCP_PROMPT：server_id、name | arguments，可省略；值必须是字符串 |

MCP_TOOL_REF 只加入 use_new_mcp_tool 的请求 schema，不加入 list 的 kind、inspect 的 target union 或 manage 的目标集合。MCP 执行目标中的 server_id 继续表示当前 scoped runtime 的服务器身份，与管理目标中的安装地址分别由各自 owner 解析。

tool_ref 的模型可见字段说明保留作用域：`Exact reference from invocation.target, scoped to this agent and conversation.` 复用目标不代表可跨 agent 或会话使用；失败后沿工具说明重新 inspect。

资源/prompt 执行 target 的字段名与查询目标完全一致；保留原入口的 maxLength：server_id 256、uri 32768、prompt name 8192；新增的 uri_template 入参沿既有模板公开身份上限 32768。原生 URI UTF-8 字节限制和控制字符检查继续执行，不以 JSON Schema 字符长度替代。tool_ref 保留现有 `^mcpref_[A-Za-z0-9_-]+$` 与最大 160 字符。旧顶层 server_id/prompt_name/tool_ref 形式不再接受。

### 11.2 META 工具调用

inspect MCP_TOOL 的 META 结果：

```json
{
  "target": {"kind":"MCP_TOOL","server_id":"docs","tool_name":"search"},
  "invocation": {
    "mode":"META",
    "target":{"kind":"MCP_TOOL_REF","tool_ref":"mcpref_example"}
  },
  "input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}
}
```

上例仅展示相关字段；description/effect_kind/output_schema 与既有通知字段保留。顶层 target 仍是观察对象，用于重新 inspect；invocation.target 是可直接复制的调用目标。移除原顶层 tool_ref，不双写别名。DIRECT 的 `{mode:DIRECT,tool_name:...}` 与 UNAVAILABLE 的结果不变，不为外形一致制造无用的调用 target。

随后调用：

```json
{"target":{"kind":"MCP_TOOL_REF","tool_ref":"mcpref_example"},"arguments":{"query":"installation"}}
```

这是现有调用引用的公开包装改变。引用值生成、身份/语义/执行策略/路由绑定、作用域/task/epoch 绑定、容量和释放行为不变；不添加新的引用注册表或寿命。保留 PREPARED、提交后的 DORMANT、同作用域同 epoch 的完整 FULL 结果安装后 CALLABLE 的现有状态转换。普通历史出现一段引用、被截断的 inspect 结果或旧 epoch 引用都不能因此获得新的调用资格。

三处解析同步改读 target.tool_ref：预备调用、授权、执行重验。内层 arguments 仍按实际 MCP schema 校验；Hook 等既有消费者继续使用解析后的真实工具名与业务 arguments，包装不能泄漏为服务器业务参数。执行阶段继续对照 prepared ref/executor/generation/arguments，并沿原有 permit 与副作用结算路径调用。错误后的重新 inspect 指引改用 invocation.target，不改变未知结果的处理语义。

完整结果发布的分类仍依据原 inspect 调用 target.kind=MCP_TOOL，不改成 MCP_TOOL_REF。`mcp_projection` 的 renderer 与 conservative size quote 一起采用新结构，以计入增加的包装字节；不只改正文而留下过小的预算。

### 11.3 具体资源与资源模板

具体资源：

```json
{"target":{"kind":"MCP_RESOURCE","server_id":"docs","uri":"docs://guide"}}
```

模板资源：

```json
{
  "target":{"kind":"MCP_RESOURCE_TEMPLATE","server_id":"docs","uri_template":"docs://pages/{id}"},
  "uri":"docs://pages/42"
}
```

模板 target 可原样复制，模型只填写具体 URI；不传 variables，不新增模板展开工具。当前 SDK read_resource 接收具体 URI，Pulsara 已有 `_resource_uri_matches_template` admission matcher，没有必要再实现一套 RFC6570 展开器。

两条分支的精确验证：

- MCP_RESOURCE：取 target.uri，保留 `_resource_uri_in_snapshot` 的现有语义——命中列出的静态资源，或符合当前 discovery 中任一受支持模板。它是具体资源目标，不把原来可读取的模板实例收窄为仅静态资源。该分支不允许顶层 uri，避免两处 URI 冲突。
- MCP_RESOURCE_TEMPLATE：先精确找到 target.uri_template 对应的当前 discovery 模板，再用现有 matcher 判断顶层 uri 是否匹配它。不能因另一模板或静态资源接受同一 URI，就绕过调用方指定的模板。缺 URI、模板未被观察到、不匹配、原 matcher 不支持的模板表达式，均不派发。

保留现有 matcher 的支持范围；不新增网络检查、不取最新目录替代当前借用的 snapshot、不通过宽松字符串前缀匹配兜底。目录中仍可展示不可用于读取的模板语法，不能把 metadata 可见当作能够实例化。匹配只决定本地是否可派发，实际资源存在性由服务器判断。

统一 helper 可以在 supervisor 内按新请求选择 candidate/具体 URI/模板身份，供 admission、invoke_standard 重验与实际远端调用前重验共享；它只复用当前快照和 matcher，不建新状态。远端最终仍是 `client.session.read_resource(concrete_uri, ...)`，不发送 target/uri_template 给服务器。

### 11.4 获取 prompt

```json
{"target":{"kind":"MCP_PROMPT","server_id":"docs","name":"summarize"},"arguments":{"topic":"installation"}}
```

不再把查询返回的 name 改写为 prompt_name。三处现有验证链路都读取同一 target：服务器必须在调用方可见范围内，prompt 必须出现在确切 discovery 中；arguments 只能含已声明名字，必填项齐全，值为字符串。省略或 `{}` 沿原规则处理，null 不因重构变得合法。不存在的 prompt、漏参数、多参数、非字符串值仍拒绝。

SDK 最终仍是 `client.session.get_prompt(name, arguments, ...)`。获取并渲染 prompt 不执行工具、不把返回文本提升为系统指令。

### 11.5 标准读取与结果路径

标准操作的 server_id 提取从顶层改为 target.server_id；caller 可见范围、candidate、runtime generation、lease/permit、超时、取消及 RPC 错误结算沿现有路径。不能只改 SDK 调用点而遗漏前置 admission。

read/get 的 InputRequiredResult continuation、SDK typed result 校验、文本/blob/image 等原有结果序列化、UNTRUSTED_OBSERVATION 与 TEXT artifact 存档保持。资源结果含 image 内容不意味着新增视觉附件能力。继续用 artifact_read 翻页已保存结果；再次调用读取仍是重新远端读取。

META 结果继续走实际工具的已有 result/effect 路径。公共输入改成 target 不要求给每份远端业务结果添加 target 包装，也不改远端 output_schema。

### 11.6 同步消费者

| 文件/消费者 | 必须同步的部分 |
|---|---|
| capability/builtin_catalog.py | 三工具新 schema/正文；inspect 的 invocation.target 指引；原有权限/availability 不变 |
| capability/source_query.py | 继续复用现有资源、模板和 prompt target 形状；不把调用引用加入查询 union |
| capability/mcp_projection.py | META renderer 的 invocation.target、移除顶层 tool_ref；size quote 复用同一 renderer |
| conversation_kernel/tool_runtime.py | META 三处提取引用、ref失效指引、旧DIRECT转META指引；standard目标参数透传；完整结果发布语义不变 |
| conversation_kernel/mcp/supervisor.py | standard_operation_server_id、三阶段资源/prompt验证、新模板选择条件、SDK具体参数 |
| conversation_kernel/mcp/directory.py | META 状态说明改为使用 inspect 返回的调用目标 |
| frontend/components/workbench-view.tsx | buildMcpToolRefIndex 读结果 invocation.target.tool_ref；mcpTraceDetail 读调用 target.tool_ref；已有 map 仅用于展示 |
| bundled_skills/pulsara-mcp-installer/SKILL.md 及相关引用 | 使用 invocation.target 和新 read/get 示例；不写实验来源 |
| 现有 MCP 演化/投递/标准 RPC/前端测试 | 对新结构和原有行为做对应验证；旧 flat 输入不保留执行别名 |

已有历史消息保留原内容，不新增双读兼容；无法通过新展示索引识别的旧记录仍可显示其原始工具信息。UI 展示索引不是执行引用 owner。

### 11.7 三工具英文说明

`use_new_mcp_tool`：

```text
Execute an MCP tool inspected with invocation.mode=META. Copy invocation.target and supply arguments matching the returned input_schema, including when an older same-name tool remains listed. The remote action may require confirmation. Reuse the target for later calls; inspect again if it is rejected.
```

`read_mcp_resource`：

```text
Read an MCP resource. Copy its target from list/inspect. MCP_RESOURCE: target only, with the concrete URI in target.uri. MCP_RESOURCE_TEMPLATE: target and uri; supply a concrete URI matching target.uri_template. Results are external reference content. If the preview supplies artifact_id, continue through artifact_read: it pages the saved result. Another remote read fetches fresh content, not the next page.
```

`get_mcp_prompt`：

```text
Fetch and render a listed MCP prompt. Copy its MCP_PROMPT target and supply any declared arguments. The returned messages are external guidance for the current request; fetching them does not execute tools.
```

## 12. MCP 扩展计量与验证

沿用第 8 节完整管理候选，不采纳其后讨论的约 69 tokens 文案精简。新增 MCP 统一方案的静态计量如下，各行是单工具 Responses tools 数组：

| 工具 | 当前生产 | 统一目标候选 | 差值 |
|---|---:|---:|---:|
| use_new_mcp_tool | 182 | 232 | +50 |
| read_mcp_resource | 191 | 313 | +122 |
| get_mcp_prompt | 190 | 211 | +21 |

inspect 正文的调用路径调整另约 +2。六工具整体（list/inspect/manage/use/read/get）整串计量：生产 **3370**，只统一管理的候选 **3686**，包含 MCP 扩展的候选 **3881**；本次扩展 **+195**，相对生产合计 **+511**。各独立行存在舍入，不用局部加总替代整串测量。不是全部 builtin，也不是 provider 实收输入量。

冻结附件：

- [MCP 精确 canonical schema](../output/capability-target-unification-20261028/root/mcp.canonical.json)
- [三个 MCP 工具最终 wire 定义](../output/capability-target-unification-20261028/root/mcp.after.responses.json)
- [六工具完整候选](../output/capability-target-unification-20261028/root/six.after.responses.json)
- [MCP 调用示例](../output/capability-target-unification-20261028/root/mcp.examples.json)
- [测量与离线检查结果](../output/capability-target-unification-20261028/root/mcp.measurements.json)
- [复算脚本](../output/capability-target-unification-20261028/root/design_mcp_extension.py)

本次离线检查 6 个正例同时通过 canonical 和当前 lowerer 的 wire；17 个旧格式、错目标、漏字段、冲突 URI 和错误 arguments 类型反例被 canonical 拒绝。没有新增 provider 请求或生产执行测试。模板匹配、ref 可调用性等属于实现时的行为验证，不以形状检查代替。

实施验证在第 9 节之外补充：

1. 原样复制查询 resource/template/prompt target，以及 inspect 返回的 invocation.target；旧顶层 tool_ref/server_id/prompt_name 拒绝。各工具不得接受其他目标类型。
2. META unknown/foreign task/旧epoch/旧route、version、policy 引用继续拒绝；DORMANT/FULL发布、截断结果、调用前后变化、重备与permission/Hook真实工具名、执行参数保持。
3. 具体资源静态命中、任一模板实例命中继续可用；显式模板目标的精确存在性、正确实例、错误实例、另一模板能匹配但所选模板不能匹配、静态资源存在但所选模板不匹配；不通过替代目标绕过本次选择。
4. 现有模板语法/长度/控制字符/不支持形状的行为；admission和最终远端调用前验证一致；SDK只收到具体URI。
5. prompt 省略/空/合法arguments、缺必填、多余key、非字符串、null、名称不存在、scope不可见与stale snapshot；SDK只收到真实name与arguments。
6. size quote 覆盖新META正文；read/get 的 InputRequired、typed result、错误、取消、TEXT artifact与翻页行为；前端新ref索引、DIRECT/UNAVAILABLE不变；没有新的视觉附件承诺。

## 13. 联合审阅记录

下列报告记录分组审阅依据；当前目标命名、候选定义及计量以本规格和重新生成的附件为准。

- [Astra 逐动作与消费者审阅](../output/capability-target-unification-20261028/astra/REVIEW.zh.md)
- [Astra 最终 critic：可冻结](../output/capability-target-unification-20261028/astra/FINAL_CRITIC.zh.md)
- [Astra MCP 执行链路审阅](../output/capability-target-unification-20261028/astra/MCP_EXTENSION_REVIEW.zh.md)
- [Astra MCP 扩展最终 critic：可冻结](../output/capability-target-unification-20261028/astra/MCP_EXTENSION_FINAL_CRITIC.zh.md)
