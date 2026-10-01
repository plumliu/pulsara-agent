# Pulsara 能力来源与统一查询 hard cut 实施规范

状态：2026-10-01 统一来源查询、模型字段减法与旧入口删除已完整落地；非真实会话检查、同一 GPT-6.1 Sol / xhigh critic 代码复审及真实 GUI dogfood 已通过。真实会话暴露的首次禁用 Plugin 安装过度预留与清单摘要误报问题已修复、测试并交叉审核，无本轮阻塞项。既有独立资源预估待修事项及验收范围见第 13 节。第 12 节四项运行修订已在前一轮实现和审核。

本文以根目录 [AGENTS.md](AGENTS.md) 为约束，衔接 [应用与能力管理边界](PULSARA_APP_AND_CAPABILITY_MANAGEMENT_BOUNDARY_DESIGN.zh.md)、[搜索拆分](PULSARA_SEARCH_TOOL_SPLIT_IMPLEMENTATION_SPEC.zh.md) 和 [Hook 输入与审阅](PULSARA_HOOK_WEAK_CODEX_SUPPORT_AND_REVIEW_DESIGN.zh.md)。本文获准实施后，只在来源查询、公开位置与对应模型入口的主题内取代旧合同；不会重做安装布局、执行 owner 或 Hook v2。

## 1. 产品目标与范围

模型先用一个列表入口回答“有哪些能力、来自哪里、是否存在使用障碍”，需要位置、调用参数或具体问题时，再通过一个详情入口查看确切目标。Plugin 是能力的分发与管理来源，不作为能力列表条目。模型无需从 cwd、目录名字或 manifest 拼出安装位置，也无需记住不同能力的多套清单工具。降低模型认知负担是字段选择的前提：能由 owner 内部判断的事实，不要求模型理解其判断机制。

完整 Plugin 清单、空包盘点与无法产生能力的损坏包排障由 GUI 负责；模型查询入口不提供全量 Plugin 枚举，也不提供等效的全量来源摘要。模型仍可使用安装结果、能力来源或用户提供的确切 scope + plugin_id 查看和管理该 Plugin，不要求它先枚举所有安装包。

本轮一起完成用户认可的两部分：

1. Skill、独立 MCP 与 Plugin 的安装／配置结果返回本次操作的实际落点；确切目标详情按相同字段含义返回位置。能力列表只返回选择目标所需的来源和状态，不逐行展开安装布局或完整配置。
2. 新增 `list_capabilities`、`inspect_capability`，统一来源观察及 MCP 查询分支，删除被替代的模型查询入口。

`manage_capability` 保留变更与 HITL；远端调用保留独立入口。统一查询不连接服务器、不执行 Plugin／Hook、不渲染远端 prompt、不读取远端 resource 正文，不以“发现”授予执行许可。内部 `CapabilityKind` 仍只有 TOOL／SKILL；本规范中的 MCP server、Hook source 等是查询记录种类，不是新增运行时 leaf。PLUGIN 仅作为来源类型和确切来源详情 target，不能作为 list 的 kind。

本轮不包含移动现有目录、向 `.agents` 新增安装写路径、统一 CLI 命令树、通用 invoke、搜索市场、自动启用／授权／重连、索引数据库、安装回执或恢复机制。独立 CLI 和 GUI 原生管理继续使用相同 owners。

## 2. 实施前现状与 ownership

生产代码的当前事实：

- `capability/local_skill_publisher.py` 按 scope 选择真实安装根；`capability_management_execution.py` 安装结果把 Skill 目录放在 `current.path`，管理观察的 item.path 则是 SKILL.md，两者含义不同。
- `capability/mcp_management.py` 的 `path(LocalMcpTarget)` 拥有独立 MCP 配置文件位置；模型配置结果没有直接返回这个路径。
- `plugins/package_store.py` 的 PluginStoreLayout 拥有 package/state/data 位置。USER 与 WORKSPACE 实例都存放在有效 Pulsara home，WORKSPACE 使用既有 workspace_state_key 分区。
- `INSTALL_PLUGIN` 已返回 `identity.plugin_id`，但没有向模型返回 package_root。PluginInspectionService 已有安装实例、package_root、data_root 和组件观察。
- `list_mcp_servers` 同时列 server 与某 server 的工具，其他清单工具列 resources／templates／prompts；MCP directory 是本地观察，不触碰 transport。
- `inspect_new_mcp_tool` 公开 schema 并沿既有 owner 准备临时 tool_ref；ref 的发布仍在工具结果提交后结算。`use_new_mcp_tool` 负责远端执行。
- `manage_capability` 中 `INSPECT_LOOSE_SKILLS`、`INSPECT_HOOK_SOURCES` 是已有只读观察动作；安装、启停、授权和信任仍由各自原生服务执行。

依赖／owner 分工：SDK 拥有 MCP 协议与 transport；现有 source producers、resolver、Plugin store/inspection、Hook provider/trust、MCP supervisor、frozen surface 和 ref owner 拥有各自事实与执行资格。新增适配只负责从它们读取公开事实、组合来源关系、按查询过滤、分页和渲染。没有第二套 inventory、正则、目录扫描器、解析器或权限系统。本文出现的 owner、provenance、lease、generation 等是实施约束，不能因其出现在规范里就变成模型 schema 或必读 Skill 术语。

确切 Plugin 查询存在一个需要补齐的原生 extension point：当前 PluginInspectionService.inspect 先观察全量 state aggregate，再遍历包；任一包观察失败可以使整体 instances 为空。全量观察后筛选无法实现确切目标隔离，因此在同一 inspection/store owner 内增加确切 identity 观察分支，复用 layout、read_state、当前 package anchor、PluginSourceObserver、状态一致性与凭据检查。先选择目标再观察，不调用全量 aggregate 后过滤；GUI 全量观察路径保持原职责。这是既有 owner 的最小扩展，不建立第二套 package parser、store、resolver 或查询注册表。

第 12 节四项修订后，freeze_enabled_plugin_instance 和 EnabledPluginViewOwner.observe 只投影声明；PluginInspectionService 与管理／CLI 观察不创建 data_root。Host 使用显式 observe_for_runtime 在本地 stdio／Hook 运行物化前准备目录。default list、确切 inspect 与 Plugin parent 必须复用纯观察，不因数据目录缺失抹掉声明。effective／采用只引用可精确关联的既有 frozen/live 事实，否则 UNKNOWN；不另外实现 resolver。GUI 保留完整清单与诊断。


模型查询还需要两处原生窄入口，避免为了详情复用运行物化：独立 MCP 的唯一配置 parser 先生成经过相同校验、但不解析秘密或计算运行配置身份的声明，运行 owner 再由该声明冻结实际配置；查询仅通过 typed provenance 关联 runtime，当前磁盘配置采用情况无法证明时为未知。Plugin MCP 的同一纯连接组合应用声明、当前实例 overlay 与作者输入默认值，查询取非秘密连接目标摘要，运行 owner 才继续物化秘密和执行身份。不能为判断采用重新读取秘密存储或以 fresh resolved identity 证明采用；实际秘密值过滤仍沿既有 ProcessCredentialBoundary。

Skill target 只有 skill_path。ManagedPluginStore 的原生布局 owner 可以把当前 home 的 USER／当前 GUI WORKSPACE 包内路径定位到确切实例，并核对当前 state 的 install ID，再复用 inspect_exact；查询 adapter 不拆目录名字，也不全量解析其他包的 states。这个入口只定位当前安装副本，不新增 registry 或旧版本查询合同。健康 Plugin 返回的 Skill target 必须能往返，不受无关损坏包阻断；选择事实仍单独为 UNKNOWN。确切 Hook source 的存在、空定义与拒绝诊断也不能取决于它是否有资格形成默认列表行。

## 3. 真实位置与作用域

### 3.1 位置不是 terminal cwd

USER 使用 Host 已冻结的 Pulsara home／user home resolution；WORKSPACE 使用 GUI 会话绑定的 canonical 工作目录。查询和安装不接收任意 workspace_root 或 pulsara_home。terminal 的 cd/workdir、源码下载目录和 app 启动 cwd 不影响这些目标。

loose Skill 的四个发现根保持原优先级：工作目录 `.pulsara/skills`、工作目录 `.agents/skills`、有效 Pulsara home 的 skills、用户 home 的 `.agents/skills`。安装仍只写 Pulsara owned root。Plugin Skill 与 bundled Skill 保持自己的来源，不物化成 loose 副本。

独立 MCP 使用有效 home 的 mcp.yaml 或会话工作目录的 `.pulsara/mcp.yaml`。Plugin 的 MCP 声明仍在 package 的 mcp.json；实例 overlay 与私密凭据继续属于既有 owner，不能误报为项目的 mcp.yaml。Plugin 的 WORKSPACE scope 不表示 package 位于项目目录。

### 3.2 公开路径合同：操作结果与按需详情

| 对象／结果 | 字段 | 精确含义 |
| --- | --- | --- |
| Skill 安装、详情 | `skill_root`、`skill_path` | 安装／来源目录与其中 SKILL.md 的绝对路径；不再用一个 path 同时代表两者。列表只在 target 中携带定位所必需的 skill_path |
| 独立 MCP 增删改、详情 | `config_path` | 本次实际读写或观察的配置文件绝对路径；删除后仍表示被修改的位置，不表示该 server 仍存在 |
| Plugin 安装成功、确切 Plugin 详情 | `package_root` | 此次安装版本或当前观察版本的托管包绝对路径；能力列表的 Plugin 来源只携带其 target |
| Plugin MCP server 详情 | `config_path` | package 中实际 mcp.json 声明位置；不混为独立 mcp.yaml 或实例 state／overlay JSON。包位置通过 source.target 查询，不在每个子项重复 package_root |
| Hook source 详情 | `config_path` | 原生来源的 hooks.json 绝对路径，不是脚本程序路径 |

路径返回遵循任务需要，不遵循内部 DTO 的层级：安装结果给本次落点，列表给可复制目标，详情给该目标的定义位置。远端 MCP tool／resource／prompt 只关联来源 server，不重复 server 的配置路径或 Plugin 包路径。data_root 和实例 state／overlay 文件位置继续由原生 owners 使用，不作为常规模型查询字段；不新增 include_internal 或另一个内部详情工具。检查确切 MCP／Hook 时，与具体失败有关的 cwd／参数／非秘密环境仍沿原 owner 如实说明；不因隐藏高级配置而丢掉故障事实，也不把完整配置当作默认详情。路径可见不授予直接改写 managed state 或读取秘密的资格，变更仍走原生管理 owner。远端 URI 不是 filesystem path。

位置均由负责该操作的既有 owner 输出；使用 SuccessfulPluginInstallOutcome 的真实 identity/package_install_id 和唯一 PluginStoreLayout，不在结果 formatter 重拼第二套路径规则。成功位置来自本次已结算的操作，不能事后随手重读“最新版本”冒充本次结果；失败、冲突、未知结果不声称成功安装，也不输出猜测的 package_root。

`INSTALL_LOOSE_SKILL.current.path` 在本轮删除，改为 `current.skill_root`／`current.skill_path`；安装成功补齐 `identity.skill_path` 供后续原生管理直接使用。只读观察不赋予 content_revision／seen-line 编辑 authority。既有 Skill 管理输入 `skill_path` 保持不变。

目录可见性、managed source race 检查和实际凭据过滤保持现有合同。只过滤真实秘密值；完整配置路径和非秘密来源字段可展示。无法安全公开的目标沿现有 unavailable/diagnostic 表达，不能返回一个被替换字符串却要求模型拿它当真实路径。

## 4. 两个统一查询入口

### 4.1 `list_capabilities`

公开参数只有 `kind`、`scope`、`source_kind`、`parent`、`limit`、`offset`，都是可选。未知字段、错误类型、无效组合明确拒绝。

- kind：SKILL、HOOK_SOURCE、MCP_SERVER、MCP_TOOL、MCP_RESOURCE、MCP_RESOURCE_TEMPLATE、MCP_PROMPT。PLUGIN 明确拒绝，不作为旧形状 alias。
- scope：USER／WORKSPACE。省略表示观察当前 Host 可管理／可见的来源，不增加 AUTO 或新的持久作用域；bundled 来源没有 USER/WORKSPACE 安装 scope。
- source_kind：LOCAL／PLUGIN／BUNDLED。省略不过滤；四个 loose Skill 发现根由内部 root_kind 与 resolver 识别，模型详情通过实际 skill_path 区分，不再公开第二套 root_kind 枚举。
- parent：只接受确切 PLUGIN 来源 target 或 MCP_SERVER target。PLUGIN 形状是按该来源筛选其 Skill／MCP server／Hook source；MCP_SERVER 形状用于列当前 runtime 的直接子项。Plugin target 可从能力行 source.target、安装结果中的 identity 或用户提供的确切身份取得，不需要 Plugin 列表行。不得填写任意目录、通配 plugin_id、省略身份或要求搜索包名。
- limit：默认 50，1–200，沿已有 MCP directory 页条目边界；offset：默认 0，非负整数，bool 不作整数。

ROOT 无 kind／parent 时直接返回跨 local／Plugin／bundled 来源的 Skill、MCP server、Hook source，并补充无法关联到当前安装副本的可见 runtime MCP server。Plugin 提供的能力与独立能力并列，每条保留 source，不要求模型先展开包。选择 kind 后可以跨来源列该类型；source_kind=PLUGIN 仍只返回能力，不返回包记录。指定 Plugin parent 只列该确切包当前声明的 Skill、MCP server、Hook source；指定 MCP server parent 只列当前已观察的 tools/resources/templates/prompts。kind 与 parent 不相容时返回参数错误。

默认清单包含实际观察到的已禁用、被覆盖、不完整或不可用的能力记录，不把 effective winner catalog 当成完整能力声明清单。没有连接的 MCP 可以显示已配置的 server 记录，但不能造出远端工具目录。普通 built-in tool 不作为安装来源记录逐项列出；实际直接调用工具表仍由已冻结 provider surface 提供。

Plugin 提供的能力沿 PluginInspectionService 的实例 summary／原生组件观察读取，disabled 实例也能列出已声明的组件；effective_skill_names／effective_mcp_server_ids／effective_hook 只说明选择或采用状态，不用来决定“包里有什么”。组件损坏或无法观察时保留能力查询的 completeness；查询确切来源时返回该来源诊断，不能用空 effective 集合断言包内没有组件。

有效空包不产生能力条目；损坏且没有可信组件观察的包也不产生伪能力条目。Skill／MCP 行必须对应实际观察的组件，Plugin HOOK_SOURCE 行必须有实际观察到的 Hook 定义；不能按每个 Plugin 自动合成空 Hook source 来代替包清单。默认结果不附带全量 Plugin sources、空包／坏包名单、Plugin 总数或包级分页；完整盘点仍在 GUI。已安装却没有能力行的 Plugin 不必为了让模型发现而额外暴露。能力清单的空页仅说明符合查询且已观察的能力为空，不表示未安装任何 Plugin。

读取范围始终限制到 Host 绑定的 home／项目／当前调用者可观察的来源。quick/transient 的项目观察沿现有 workspace kind；显式不支持的 WORKSPACE 查询明确失败，不能用 scratch 或 cwd 伪造项目。

两个新入口在 ROOT 与 SUBAGENT 中均可见，整体不标为 REQUIRES_MCP_PORT：ROOT 在没有 MCP port 时仍能查询本地来源，只有 MCP runtime 分支要求原有 MCP port。SUBAGENT 只观察既有 scoped MCP runtime catalog；默认清单只列该范围的 runtime MCP_SERVER，也可按 kind 或 runtime server parent 查询它的工具／resources／templates／prompts。子 agent 不调用 ROOT 的管理 preparation 或安装来源扫描。显式 scope／source_kind 筛选、SKILL／HOOK_SOURCE 查询、Plugin parent、确切 Plugin inspect 或安装配置 target 返回带原因的 ROOT_ONLY，不返回成功空清单。list kind=PLUGIN 对所有调用者都是参数错误。其远端范围、ref 与结果提交沿原有 caller scope policy，不能取得 ROOT-only 目录或 ref。

### 4.2 `inspect_capability`

参数只有必填 `target`。target 是闭合的类型化选择对象，可以来自能力行／source.target、安装结果或用户提供的确切身份；它不是执行 permit，不添加新 capability ID、opaque registry 或授权 token。不要求先前调用产生一张“已见身份”回执或会话白名单；仍沿既有观察范围、managed source 校验与权限执行确切查询。

| target.kind | 选择现有目标的字段 |
| --- | --- |
| PLUGIN | 必填 scope、plugin_id，仅查询这个确切来源；不支持省略身份、模式匹配、名称搜索或列出其他 Plugin |
| SKILL | skill_path；必须是当前原生来源观察中的确切副本 |
| HOOK_SOURCE | scope、source_kind=LOCAL/PLUGIN；PLUGIN 另带 plugin_id |
| MCP_SERVER | 安装配置目标使用 scope、source_kind=LOCAL/PLUGIN、server_id；Plugin 另带 plugin_id，server_id 为 local_server_id。runtime 目标只使用实际 runtime_server_id；两个形状互斥，不自动补 scope／source_kind。SUBAGENT 只接受 runtime 形状 |
| MCP_TOOL | 当前 catalog 的 server_id、完整 provider tool_name |
| MCP_RESOURCE | 当前 catalog 的 server_id、uri |
| MCP_RESOURCE_TEMPLATE | 当前 catalog 的 server_id、uri_template |
| MCP_PROMPT | 当前 catalog 的 server_id、name |

输出返回该目标的最新可核实详情及第 3.2 节适用的位置字段。Plugin 成功详情的业务字段闭合为 target、version、description、package_root、enabled、components、diagnostics，具体语义按第 4.4 节；不内嵌所有组件／schema，也不返回其他包、候选包名或全局 Plugin 数量。确切目标不存在／损坏时返回该目标的公开原因，不建议或枚举替代包。Skill 详情返回必要作者 metadata、skill_root／skill_path、选择状态与管理资格；正文沿普通 read_file 渐进读取，不新增 load_skill。Hook 详情返回触发条件、实际执行内容、针对当前观察定义的授权状态及影响执行的具体问题；旧版 trusted_at 或 enabled 不能证明当前定义已授权。不直接序列化完整内部审阅 DTO，不自动信任或执行。

确切 Plugin inspect 与 Plugin parent 来源筛选均走第 2 节的原生确切观察分支：健康包 A 的声明、路径与本地状态不因无关坏包 B 不可读而失败，也不能把全量观察的失败归因到 A。目标缺失、损坏、取消、race 等结果来自该目标的真实观察。Plugin 成功详情不计算整包 adoption／effective。parent 返回的各能力行或对应能力详情如依赖全局 composition，而该观察不可用，选择／effective 状态单独返回 UNKNOWN 与相应完整性；保留目标已观察的声明，不自造 winner 或另写 resolver。MCP runtime 采用继续按既有 provenance 判断。查询使用本次确切 state／package 观察及原有 lifetime/revalidation，不新增 state digest 或持久快照。

ROOT 的安装配置 MCP server 详情返回连接目标摘要、来源、适用的 config_path、当前安装启用选择、所关联的连接／目录事实及具体诊断；不返回 config_to_entry 的完整配置。连接目标摘要只含 transport 类型及 HTTP endpoint 或 stdio command／args／cwd；环境值、headers、认证配置与高级策略不默认展开。ROOT 的 runtime target 只有在既有 owner 提供该 candidate 精确冻结的配置时才给对应摘要，不能取最新同名安装配置冒充；没有可核实摘要时省略并说明原因，不新增配置快照或证明机制。SUBAGENT runtime 详情仅展示现有 scoped catalog metadata、目录／调用资格与公开失败，不返回 config_path、进程启动参数、连接 endpoint、安装 enabled 或安装来源 target；不通过 slot.client.config 扩大配置观察面。

安装 server 与 runtime server 只能由既有规范化配置／provenance 精确关联：复用 LocalConfiguredMcpRuntimeSource 的 USER／WORKSPACE 来源，以及 ManagedPackageMcpRuntimeSource 的 scope key、package owner、package_install_id；不能从名称拆出 Plugin 身份或由目录猜测。HOST_OVERRIDE 或仍被当前 epoch 持有、但不属于当前安装副本的旧 Plugin 版本用 runtime target 展示，不伪造配置路径。上述关联键和配置身份只在内部使用，不进入模型来源对象。

关联同一来源不等于当前磁盘配置已被 runtime 采用。来源对象只回答“来自哪里”；详情在需要区分已保存配置与当前会话事实时说明采用情况，无法核实就明确说明未知。只能用既有 owner 的版本／配置身份判断，不能向模型展示比对链或要求其判定。查询不得重物化秘密、启动连接或新增 digest 来证明关联。ROOT 通过安装 target 可以取得所关联的 runtime target，后续目录查询仍受当前 caller 的 runtime scope 限制；SUBAGENT 不由 runtime target 反查安装管理清单。

配置形状的 MCP parent 只列精确关联的当前 runtime 子项。未关联或目录尚未发现时，返回明确原因与相应 completeness；即使 items 为空，也不能声称远端没有工具／resources／templates／prompts。

MCP resource/template/prompt 详情只展示已经发现的 metadata；实际读取／渲染仍分别调用 read_mcp_resource、get_mcp_prompt。MCP tool 的 schema 和调用模式按第 6 节，不把静态声明当成当前 callable schema。

### 4.3 模型的最短使用路径

模型通常只需 list → 复制 target → inspect，再按结果选择 manage 或执行入口。下列 JSON 只展示请求形状；example-plugin 使用能力来源、安装结果或用户提供的确切身份，example-server 使用当前 runtime 查询返回的身份，不能根据目录或显示名称猜测。已知确切 Plugin 身份可以直接 inspect 或按来源筛选，无须先 list。

ROOT 第一次观察只需调用 `list_capabilities`：

```json
{}
```

只看来自 Plugin 的能力，仍使用同一入口：

```json
{"source_kind": "PLUGIN"}
```

按一个确切 Plugin 来源筛选能力，parent 原样复制能力行的 source.target；也可以使用安装结果或用户给出的确切身份：

```json
{"parent": {"kind": "PLUGIN", "scope": "WORKSPACE", "plugin_id": "example-plugin"}}
```

查看当前 runtime server 的工具，两类调用者都使用同一形状：

```json
{"kind": "MCP_TOOL", "parent": {"kind": "MCP_SERVER", "runtime_server_id": "example-server"}}
```

查看详情时只把目标行的完整 target 放进 `inspect_capability.target`。MCP_TOOL 返回 DIRECT 时使用原生工具；返回 META 时复制 tool_ref 给 use_new_mcp_tool；UNAVAILABLE 时根据公开原因处理。后续页复制 next_offset，并保持原筛选参数。模型不需要选择目录布局、生成 ID、了解 cursor 签名或手工拼接调用工具名。

#### 4.3.1 管理工具的模型说明合同

`manage_capability` 的共同 description、实际 action 分支与参数 description 共同构成首屏可用说明；模型无需先读取多个安装 Skill 或逐个试调用，才能判断操作归属。总描述只讲统一选路、作用域、权限／表单、秘密与结果；15 个现有动作各自在实际 provider schema 的 `oneOf.properties.action.description` 中说明用途与限制，不在总描述重复整张动作表。安装 Skill 保留来源获取与作者流程，不替代核心工具边界。

- 独立 Skill 的安装、启停、删除与独立 MCP 的增删改，不能管理 Plugin 内声明；bundled Skill 只读。Skill 安装不覆盖已有副本，用户要求替换时先准备新来源，再按 inspect 的操作资格删除确切旧副本并安装。启停资格与删除资格不同，不能把可发现当成可删除。
- Plugin 按整包安装／替换、启停、删除；安装和替换后禁用。已有 Plugin MCP 使用 `CONFIGURE_PLUGIN_MCP_CONNECTION`，身份为 scope、plugin_id 与包内 server_id，不能改包的 command／args／cwd／transport kind。Hook 信任与来源启停分别控制，既不修改 Hook 定义，也不代替 Plugin 启用。
- USER／WORKSPACE 绑定当前 Host home／GUI 工作目录，terminal cwd 不改变目标。source_path 是绝对来源目录；skill_path 是确切安装副本的绝对 SKILL.md 路径。管理使用安装身份，不把 runtime server ID、工具名、ref 或整个 query target 当成管理参数。
- 省略 config／overlay 会打开相应预填连接编辑器；必需 action、scope、身份和路径仍须填写。完整候选也可能因权限、凭据或必要审阅进入用户表单。config／overlay 对象是完整替换，不是 patch；overlay:null 清除实例覆盖并恢复包／输入默认值，不删除组件；expected_overlay:null 只断言当前无覆盖。高级对象未知时直接使用编辑器，不要求模型读取私密配置或仓库实现来构造参数。
- 工具在各权限模式均可见，所需确认由 runtime 收集；READ_ONLY 的修改交由用户表单明确提交，不要求切换模式。Plugin 启用与 Hook 信任始终审阅当前内容；模型不能提交接受标记或秘密。删除／替换须有用户请求，不能通过 terminal 或直接编辑受管理文件绕过原生 owners。
- 变更结果与 adoption 分开报告，APPLIED 不说明是否出现表单；不重放已完成变更，不例行二次 reload。配置保存、来源启用、OAuth 授权、Hook 信任、会话采用与实际调用成功不能互相替代。

本节只完善现有说明，不新增 action、参数、权限或执行路径，也不重建运行中 epoch 的 provider prefix；新说明随新的合法 provider 输入根采用。

### 4.4 规范／SDK 对照与模型字段减法

2026-10-01 实际核对：本仓库 pyproject／uv.lock 与根目录 .venv 均使用 MCP Python SDK 2.1.0；[SDK 官方说明](https://github.com/modelcontextprotocol/python-sdk) 的 v2 是 SDK 发布线，[MCP 官方最新规范](https://modelcontextprotocol.io/specification/2026-07-28) 使用日期标识协议版本，两者不是同一个版本号。这里只审计字段语义，不扩大为协议升级。

本地 SDK `mcp/client/stdio.py::StdioServerParameters` 的 command、args、env、cwd 是客户端启动子进程的配置；SDK 不定义 Pulsara Plugin 的 data_root 或 PLUGIN_DATA。Pulsara plugins/mcp_adapter 注入 PLUGIN_DATA，并按 Plugin 声明解析 cwd／参数；plugins/hook_adapter 将同一变量交给 Hook。package_store 拥有目录安全创建，view 的显式 Host 运行观察在本地 stdio／Hook 物化前调用；启用和检查不准备目录。Pulsara 不自动在其中生成缓存、日志或业务文件；是否写入由具体 MCP／Hook 程序决定。本轮从常规模型详情中减去目录字段，并按第 12.3 节将只读观察与本地运行准备分开。

这也不是 MCP roots：[官方 roots 说明](https://modelcontextprotocol.io/specification/2026-07-28/client/roots) 的 Root 是向 server 提供工作文件／目录提示的 uri 与可选 name，在该协议版本已 deprecated；不是插件数据目录，也不提供 filesystem 访问授权。不会因为删去模型 DTO 字段而添加 roots 接线。

字段只在能帮助选择目标、理解能力、定位定义、决定下一步或诚实报告失败时进入模型结果。现有内部／GUI DTO 不直接序列化成模型接口；本节裁剪的是查询投影，不删除其原生消费点。

| 字段／概念 | 已核实来源与含义 | 模型合同 |
| --- | --- | --- |
| target 中 kind／scope／plugin_id | 既有实例身份，scope 绑定 Host 当前 home／项目 | 保留一个可复制 target；不再重复 name、scope、plugin_id 或管理 identity。当前 manifest.name 与 plugin_id 一致，不凭空增加展示名 |
| version、description | PluginManifest 的可选作者元数据 | 原样保留，缺失为 null；version 不是 package_install_id，也不证明实际采用 |
| package_root | PluginStoreLayout 当前确切安装副本 | 保留，满足用户实际位置合同；不能推导为项目 cwd、MCP cwd 或可写数据目录 |
| data_root、instance_config_path | Plugin 的可写实例目录与 state／overlay 文件位置 | 从常规模型 list／inspect 中删除；原生运行、管理和 GUI 继续使用 |
| enabled | 当前实例 state 中的启用选择 | 保留；不等同于当前会话采用、MCP 连接成功或 Hook 已信任 |
| package_in_use | package_store 检查版本锁是否被其他持有者占用；观察锚点等也可能持锁 | 留在内部／GUI；不向模型描述为“模型正在使用”，也不充当 adopted 或 callable 状态 |
| package_valid 总布尔 | 没有一个布尔能同时表达包可观察及所有组件有效 | 不新增；包级失败沿原结果状态／诊断，组件问题逐项说明 |
| 整包 adoption／effective | Skill 选择、MCP 安装调用面与 Hook 执行资格由不同 owners 决定 | Plugin 详情不增加 aggregate 状态；需要时查询具体能力，保留其现有状态及 UNKNOWN 语义 |
| components.*.count | 原生 summary 中解析所得 Skill／server／Hook 定义数量 | 保留；不是原始声明总数、effective 数量或 callable 数量。完整缺失为 0，解析无可信结果为 null |
| components.*.observation | 原生组件 disposition，COMPLETE 仍可附单项无效诊断 | 不直接复制这个枚举字段。用 count 与相关 diagnostics 保留结果语义，不教模型第二套观察状态机 |
| diagnostics | 原生目标／组件错误的 code、message、必要 component/path | 保留具体可操作问题。没有问题为空；不得省略无效单项诊断以暗示全部组件有效 |
| next_actions、额外 management identity | 可从已有 target 和固定入口推导 | 不新增输出字段；统一工具说明解释 target 可复制到 inspect.target／list.parent，管理使用其 scope + plugin_id |
| 能力行 source 与 source.target | 原生来源归属及对应查询目标 | source 只保留 kind、适用的 scope 与 Plugin 来源 target；scope 已在该行 target 或来源 target 内时不重复。Plugin 包路径／enabled 去确切来源详情查看；不追加包索引。安装副本精确关联仍由内部 typed values 拥有，不新加公共 digest／generation |
| root_kind、package_install_id、workspace_state_key、package_owner_key、store_scope_key | 原生发现、存储分区与版本关联事实 | 仅内部使用；模型复制已有 target，不理解或拼接这些身份 |
| Skill 两路径、MCP／Hook config_path | 实际来源定义的路径 | 仅按第 3.2 节用于操作结果及适用详情；列表不重复展开路径。query target 必需的 skill_path 仍保留 |
| MCP 完整 config、scope_policy、exposure_policy、effect_policy | 原生配置、Host 可见范围、筛选与授权策略 | 不直接输出；模型看已裁剪的能力及既有调用资格。某策略确实造成失败时，诊断说明该限制，不教模型自行重算权限或手改完整配置 |
| stateless_http_asserted、supports_parallel_tool_calls、并发容量、刷新间隔、各类 timeout、network_policy | Host 接入与调度设置 | 常规 list／inspect／管理成功结果不携带；诊断只公开造成当前问题的相关设置与事实，默认变更由预填 GUI 完成 |
| auth、secret_env、secret references、public_headers、完整 env | 原生认证与进程配置 | 不默认展开。诊断需要时保留具体缺失项名称／相关非秘密事实，实际凭据值始终过滤；不把秘密存储身份或整套 header/env 当作模型必须理解的配置 |
| Hook digest、trust binding、matcher 编译结果、supported_tool_names／unsupported_tool_names、origin DTO | 信任核对、匹配实现与内部审阅材料 | 不直接输出。详情保留原始触发条件、命令／参数／cwd、授权事实与具体不匹配原因；需要说明工具名兼容时给本定义的简短结论，不给整张内部工具映射表 |
| MCP server／tool 身份、schema、invocation.mode、tool_ref | scoped runtime、已安装 descriptor 与既有 ref owner | 保留调用所必需字段；target 仅定位，tool_ref 才按既有精确绑定进入 META 调用。完整 schema 不截断，DIRECT 不发 ref |
| limit／offset、total_count／next_offset／completeness | 本次分页观察与结果预算 | 保留现有第 5 节页合同；模型只需按 next_offset 翻页，PARTIAL 不作不存在断言，不需要了解签名、版本锁或内部 cut |

组件计数直接按唯一原生 summary 投影：MISSING 对应 0；COMPLETE 对应解析所得 tuple 长度，且完整保留该组件的无效单项诊断；INVALID／UNAVAILABLE 对应 null 与真实诊断。尤其 COMPLETE 不表示每个输入声明都有效，不能用 count 掩盖被拒绝的定义。整体 owner 无可信结果时沿原失败结果处理，不拼凑成功详情。

COMPLETE 的 count=0 但有单项无效诊断，只表示没有解析所得项，不表示包没有声明这类组件；实施验收必须覆盖所有子项无效的情况。

下例只模拟成功详情的业务字段，通用结果 envelope 沿原 owner；不表示该包实际安装，也不新增执行保证：

```json
{
  "target": {"kind": "PLUGIN", "scope": "WORKSPACE", "plugin_id": "review-kit"},
  "version": "1.2.0",
  "description": "提供代码审查 Skill、MCP 与检查 Hook。",
  "package_root": "/Users/demo/.pulsara/plugins/packages/workspace/demo-project/review-kit/pkg_00000000000000000000000000000001",
  "enabled": true,
  "components": {
    "skills": {"count": 2},
    "mcp_servers": {"count": 1},
    "hook_definitions": {"count": 2}
  },
  "diagnostics": []
}
```

组件不可解析时，相应 count 为 null，诊断保留该组件的具体问题；不能填 0。作者版本缺失时 version 为 null，仅表示未声明版本，不增加 UNKNOWN 状态或猜测安装版本。

### 4.5 列表、详情、诊断与变更结果的展示边界

模型接口使用显式字段投影，不把 dataclass、config_to_entry、PluginInstanceInspection 或 GUI review 对象整体转为 JSON。内部字段有生产消费点，只能证明它对 owner 有用，不能证明它应该对模型公开。

| 结果层级 | 模型需要的信息 | 不默认展开的信息 |
| --- | --- | --- |
| list 能力行 | kind、name、简短 description（存在时）、target、source、status | 目录布局、作者完整 metadata、schema、完整配置、组件数量、连接／授权状态机及身份比对过程 |
| Skill 详情 | 来源、作者名称／描述等必要 metadata、两路径、选择／管理事实及诊断 | resolver 候选次序、root_kind、content revision、编辑 authority |
| MCP server 详情 | ROOT 安装 target 的连接目标摘要与定义位置、启用及连接／目录事实、必要 runtime target 和诊断；runtime target 按第 4.2 节精确配置及 caller 范围裁剪 | 完整原生 config、策略组合、SDK 协商记录、lease／generation、指纹及凭据引用对象；SUBAGENT 不获取 Host 配置摘要或安装来源 target |
| MCP tool 详情 | 完整 callable schema、实际调用名称、DIRECT／META／UNAVAILABLE 与 META 所需 tool_ref | descriptor 身份、ref 绑定／容量／签名、执行策略 DTO。schema 不因减负裁剪 |
| MCP resource／template／prompt 详情 | 已发现的名称／描述及 URI／参数等实际使用 metadata | 来源 server 的重复配置、resource 正文、渲染后的 prompt |
| Hook 详情 | 当前定义的触发条件、执行命令／参数／cwd、针对该定义的用户授权事实、影响执行的限制 | Hook 内部身份、信任校验材料、编译缓存与全量兼容工具清单；旧信任记录不冒充当前授权 |
| 确切 Plugin 详情 | 第 4.4 节闭合字段 | 包内全部定义、内部组件观察枚举、整包运行状态 |
| 安装／配置／启停结果 | 已有本次动作结果、后续定位所必需的 identity、适用的实际落点及待处理问题 | 完整配置回显、存储身份和内部状态快照；成功不宣称当前会话已经采用 |

list 的 status 是从原生事实生成的简短说明，供模型阅读，不是新的状态机、permit 或管理输入。正常情况也必须明确是“当前已选择”“已配置，尚未连接”“已禁用”还是“当前采用情况未知”等已核实事实，不能写模糊的“可用”掩盖目录或执行资格。存在多个障碍时保留各个影响下一步的事实，例如“来源已禁用；该 Hook 尚未获授权”，但不逐行复制所有 owner 枚举或列出常态内部阶段。详情保留与该类型有关的分别核实事实；不能为了减少字段将启用、已连接、已信任、已采用合并成一个 available。状态文案只渲染事实，不建立第二套 resolver／权限判断。

diagnostics 保留原 code、具体 message、必要 component/path；相关高级配置只在其解释当前问题确有必要时附在诊断中，复用当前 owner 的错误上下文，不新增配置诊断注册表。例如启动失败需要给出实际 command／cwd，缺失凭据需要指出待填写项，Host 禁止远端访问需要说明具体限制；都不顺带回显其余配置。减少默认字段不是扩大凭据过滤，也不能把真实失败改成泛化的“不可用”。即使环境中可通过 terminal 读取非秘密原生配置，也不要求模型为了完成常规操作先学习整份配置。

UPDATE_LOCAL_MCP 的 config 仍是完整替换。精简详情不能拿来当完整配置模板；通常省略 config，由 owner 打开已预填当前完整配置的 GUI。只有任务确实要求直接提交完整配置时才沿已有原生合同处理，不暗加 patch／merge 或通过 inspect 输出高级字段全集。MCP 管理成功结果当前回显的完整 config 在本轮一并删除，保留操作身份、config_path 与实际 cleanup_attention；内部 mutation outcome、确认表单与配置保存不裁剪。

例如一个独立 MCP 的列表行只需如下内容（仅模拟业务行，不展示分页 envelope）：

```json
{
  "kind": "MCP_SERVER",
  "name": "project-docs",
  "target": {"kind": "MCP_SERVER", "scope": "WORKSPACE", "source_kind": "LOCAL", "server_id": "project-docs"},
  "source": {"kind": "LOCAL"},
  "status": "已配置，尚未连接；远端目录尚未发现。"
}
```

询问“配置在哪里”时，复制 target 调用 inspect，获得 config_path；询问“有哪些工具”时，只有已关联且目录已发现的 runtime 才能返回真实子项。列表不先给一份完整 transport／auth／policy 供模型自行分析。

## 5. 来源、状态、分页与可见失败

每个简短列表行仅按第 4.5 节返回 kind、name、可选 description、target、source、status。source.kind 为 LOCAL／PLUGIN／BUNDLED；LOCAL 必要时携带 USER／WORKSPACE scope，但该行 target 已含 scope 时不重复；PLUGIN 用 source.target 携带确切 scope／plugin_id，BUNDLED 无安装 scope。子项沿已核实且当前调用者可观察的来源投影；来源无法核实时 source 为 null，并在 status 说明未知，不增加 UNKNOWN 来源枚举或伪造 LOCAL／Plugin 身份。SUBAGENT 不反查安装来源，既有 scoped catalog 无来源事实时 source 为 null，只说明此调用者范围内未提供来源信息，不能据此断言 Host 来源异常。来源本身不包含 package_root、root_kind 或一套重复状态，实际位置与来源启用选择由确切详情获得。bundled 不冒充用户安装。source 只描述该行的来源，不附带同目录其他包或全量来源索引。后续用法由统一工具说明教授，不逐行增加 next_actions。内部 ordinal、digest、nonce、guard 与凭据不作为常规清单正文。

状态不能合并成单个 available：

- 安装／声明观察、enabled、Skill selection/shadowed/invalid、Hook trust 在内部及相关详情中分别核实；列表 status 仅说明当前选择和实际障碍，不输出各套内部枚举。disabled Plugin 的组件仍可观察，但不能声称已激活。
- MCP 配置保存、连接、目录发现、当前调用路由、来源采用彼此独立。配置已变但旧 epoch descriptor 仍在时分别说明磁盘事实与实际已安装调用面。
- 来源不完整、解析失败、权限不足、远端目录尚未发现都不能变成“没有任何项”。响应保留按 owner／能力观察范围定位的 completeness 与诊断。某来源不可用时可以分页展示其他已观察能力，但整体标 PARTIAL，不能作全量不存在断言。无确切 Plugin 筛选时，诊断说明能力观察不足，不附带失败 Plugin 的逐项身份／路径名单或包数；确切 Plugin 查询则公开该目标的具体失败详情。安装／管理结果仍报告本次目标的实际结果，GUI 仍保留完整包诊断。

native owner 整体 UNAVAILABLE 且没有可信实例清单时，只按上述查询范围保留其诊断及其他来源的 PARTIAL 结果；不能猜测实例或制造“完整 invalid 清单”。只有 owner 实际观察到的无效能力条目才能作为 invalid 记录展示，坏包本身不能伪装成无效能力。

这是本次调用的公开组合观察，不承诺跨 producer 的原子快照，也不保存历史 inventory。沿现有只读观察和调用 deadline 检查 cancellation；不能为了 list 启动连接、刷新服务器、验证下载脚本或启用 Plugin。

统一列表采用 offset 页：按 kind、source kind、scope、现有稳定身份／路径的大小写敏感顺序排序，再按 offset 取页。结果包含 items、returned_count、total_count、truncated、next_offset；total_count 只计本次已观察且符合过滤的能力条目，不统计 Plugin 安装包，并带 completeness；next_offset 为 offset 加实际返回项数，末页为 null。offset 越界成功空页。源变化可能导致页间移动，没有一致性 cursor 承诺，模型必要时从 0 重查。

理由：当前 MCP 有自己的单 cut cursor，新增磁盘／Plugin／Skill／Hook 观察没有一个共同版本 owner；不为统一目录创建 aggregate fingerprint、签名快照、cursor registry 或物化 inventory。删除被替代模型 list 的旧 cursor 接口与仅服务该接口的 signer，但复用现有 directory 的候选、scope/collision、行渲染和结果字节边界。不能将旧页解码后再筛选导致漏项，也不保留旧工具 wrapper。

保留 MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES 的现有完整结果预算；一页可少于 limit，仍按实际项数前进。单项超预算沿可见 typed overbound 处理，不能返回空页加不前进的 next_offset。详情正文沿既有 artifact/result owner 分页；MCP callable schema 必须完整且沿现有 descriptor overbound 边界，不能截断后当成可调用定义。没有新的总条目、总文件、会话或生命周期 cap。

## 6. MCP 查询整合与执行连续性

生产删除以下 provider-visible 查询工具及其旧参数／公开转发：list_mcp_servers、list_mcp_resources、list_mcp_resource_templates、list_mcp_prompts、inspect_new_mcp_tool。MCP 下的对应 list/inspect 进入新入口，不保留 deprecated aliases。

`manage_capability` 删除 INSPECT_LOOSE_SKILLS、INSPECT_HOOK_SOURCES 两个只读动作与其 schema 分支；底层 inspection services 留作唯一 owner，并供统一查询与 GUI 调用。manage_capability 剩余动作保持现有明确 USER／WORKSPACE、权限与私密表单。

保留 use_new_mcp_tool、read_mcp_resource、get_mcp_prompt 和已安装的直接 MCP descriptor。use_new_mcp_tool 仅更新说明／接线引用新的 inspect_capability，保持 tool_ref、权限、精确 binding、physical settlement 与 replay 路径；本轮不顺带重命名调用入口。

MCP tool 详情遵循当前借用的 route 与安装 surface：

1. DIRECT：返回该已安装 descriptor 的确切 schema 和实际 provider tool_name，invocation.mode=DIRECT；不发布 tool_ref。即使磁盘／最新发现变了，也不能把新 schema 描述成旧 descriptor 的直接调用输入。
2. NEW_MCP_META_ONLY：复用当前 inspect 的 route/binding 检查、结果大小检查、既有 ref prepare 与结果提交 settlement；invocation.mode=META，tool_ref 随成功结果返回，随后交 use_new_mcp_tool。没有新 ref 类型或授权缓存。
3. UNAVAILABLE：返回观察到的公开不可用原因，不发 ref，不伪称 callable；来源旧 ref 不会因统一入口重新有效。

ref 属于同一 epoch／调用者、精确语义与执行策略，仍在结果提交后生效。取消或结果未提交要结算 prepared ref；目录查询不能绕过 ref capacity、stale、owner 或策略检查。ROOT/ref 不传给子 agent。统一 target／列表行本身不作为远端调用参数或 execution permit。

read_mcp_resource/get_mcp_prompt 的输入、网络权限与返回 owner 保持现行合同，只从新查询结果复制确切 server_id 与 URI/name。get 不塞进只读 inspect 里执行。MCP server instructions／descriptions／prompt metadata 继续作为不可信来源文本，不能提升为 SYSTEM。

## 7. Hard cut、前缀与其他文档

新查询入口、旧入口删除、安装位置结果、管理 schema 减法及 bundled 说明作为一次完整变更发布。更新 builtin catalog/executor 闭合、scope 分类、前端工具摘要和活跃 examples/dogfood；不修改 archived_docs 记载的旧事实。

新的 SYSTEM/provider tools 只在既有 cold epoch 或明确采用的 compaction successor 建立。部署停止旧 runtime，使用新进程／新冷会话验收；不在线替换旧工具根，不重写历史查询结果／messages。能力变化及查询不会触发自动 compaction、rebase 或模型工具注入。

Hook 的 stdin、闭合别名、输出控制和 v2 trust 未改变，不切换到 v3，不批量改写或撤销信任。没有新增 Hook 别名；名称 matcher 自然按新实际 builtin 名称观察。GUI Hook review 继续复用生产 matcher，只因当前工具清单变化更新 advisory 展示，不使目录观察成为定义 digest。

不增加 canonical schema、durable/live event、subject、append guard、relation、job、receipt、checkpoint、generation 或 inventory 权威。现有 oracle 中 builtin 数量随明确的五删二增调整，durability 分类保持不变；管理 intent 的两项减法也按新闭合合同更新测试，不能用改 oracle 掩盖漏删。

实施时同步应用／管理边界第 5–7 节与验收中旧查询接线；搜索／Hook 规格中涉及当前工具表面的示例按新真相更新，已完成的历史验收记录保留。位置说明进入四项 bundled capability Skills：主说明只教 list → inspect → manage/use 及复制返回 target/identity，完整 schema 与错误细节仍渐进读取参考文件。

## 8. 实施清单

1. capability/builtin_catalog 与管理 intent：新查询闭合 schema，删除五个旧查询 descriptor 与两个管理 inspect 动作，保留各执行工具。
2. capability_management_execution／原生 services：Skill 两路径字段、MCP config_path、Plugin 成功版本 package_root；按第 3.2／4.4／4.5 节分层构造显式最小投影，列表只给定位所必需的 target，详情按需给位置，MCP 管理成功结果删除完整 config 回显。不得导出内部／GUI DTO 的全部字段，保持 mutation／adoption 分开，原生存储／确认表单不裁剪。
3. tool_runtime／Host：两个 descriptor 在 both caller scopes 可见，本地查询不依赖 MCP port；ROOT 原生来源观察与 SUBAGENT scoped runtime 查询分别接线到既有 owners，再复用过滤／渲染。PluginInspectionService/store 补确切 identity 观察分支，确切 Plugin inspect／parent 不先扫描全量包再过滤；default list／exact inspect／parent 声明观察不调用带数据目录准备副作用的 materializer。保持取消、source race、scope、collision 与完整性。
4. mcp/directory、MCP projection/meta ref：复用候选及结果预算，移除被删除 list 的旧 cursor 公开路径；统一 inspect 的 DIRECT／META／UNAVAILABLE 分支，不复制协议或 ref state machine。
5. bundled instructions、frontend 工具摘要、GUI 共用观察与活跃文档：模型清单直接列能力、Plugin 只作来源，不添加全量包枚举入口；GUI 保留完整 Plugin 清单与诊断。统一字段含义与最少使用步骤，不把查询成功当成启用、信任、连接或实际使用成功。
6. 清理旧模型名称、恢复分类、测试 fixture、主动提示与诊断建议；旧输入明确拒绝，不保留 wrapper 或 dual read。

## 9. 验收与冻结流程

本文先与同一 GPT-6.1 Sol / xhigh critic 讨论、修订，再交用户审阅；此阶段不实施运行代码或启动真实模型会话。

获准实施后先完成非真实会话检查，再交同一 critic 代码复审，修订无阻塞才做真实 GUI dogfood：

- 位置：默认／自定义 PULSARA_HOME、USER／WORKSPACE、四个 loose roots、Plugin 工作区中央存储、source 与安装副本分离、terminal cd/workdir 不改变目标；成功、替换、冲突、删除、取消／未知结果不串版本或伪造路径。
- 来源：bundled／local／Plugin，disabled、shadowed、invalid、不完整／不可管理项；Plugin 包内子项不伪装成 loose，不因观察启用／信任或启动进程；跨 scope 同名项按确切来源区分。
- 查询：ROOT 默认跨来源能力、SUBAGENT 默认 scoped runtime、无 MCP port 的本地查询、kind 跨来源、确切 Plugin 来源筛选与 MCP parent、过滤组合、闭合 target、未知字段／错误类型、空页与真实下一页；ROOT_ONLY 保留原因，不可观察不作不存在结论，行字节预算截断必须继续前进。SUBAGENT server 详情不反查 slot 配置／安装来源，不泄漏路径、endpoint、启动参数与安装启用选择；ROOT runtime 摘要不能用最新同名配置替代当前 candidate 的配置。
- Plugin 边界：list kind=PLUGIN 明确拒绝；默认页和 source_kind=PLUGIN 都仅返回能力；空包无能力行，空 Hook source 不作包清单替身，坏包无伪行，total_count 只统计能力。全量来源摘要、包名单／包数、模糊包名查询、错误中的替代包列表均不存在；确切来源 target 可来自能力、安装或用户身份而不依赖“已见 ID”状态。健康 A 与无关坏 B 并存时，确切 inspect／parent 仍观察 A，B 的全局失败不归到 A；全局选择不可知时声明保留而 effective 单独 UNKNOWN，缺失／损坏／取消／race 的确切目标结果和 anchor 清理正确。GUI 完整 inventory、确切 Plugin 诊断与管理能力保持。
- 字段减法：list 不展开安装路径／schema／配置／root_kind／内部状态枚举；Skill target 必需的 skill_path 保留。位置只按第 3.2 节输出，Plugin 子项通过 source.target 查包位置。MCP 详情和管理成功结果均无完整 config，诊断只带相关设置，完整替换默认仍走预填 GUI。Hook 详情无内部信任／编译／全量别名 DTO，原触发条件及执行内容保留。Plugin 成功详情只包含第 4.4 节闭合业务字段，无 data_root／instance_config_path／package_in_use／总 valid／aggregate adoption／重复 identity／next_actions。MISSING 为 0，COMPLETE 计解析所得项且保留无效单项诊断，INVALID／UNAVAILABLE 为 null；所有子项无效时 COMPLETE count=0、诊断非空，不误报没有声明。缺失版本为 null。MCP server 的作者 metadata 不冒充 Plugin manifest，配置路径／包路径／stdio cwd 不混淆，原 MCP／Hook 运行数据目录及 GUI 字段仍有效。混合状态与未知采用必须如实说明，不用“可用”或空清单遮蔽；DIRECT／META 的完整 schema 与调用资格保持。
- 只读观察：已启用且 data_root 缺失／不可准备的本地 MCP、HTTP-only 与 Hook Plugin，default list／exact inspect／Plugin parent 均不创建目录、不调用运行 preparation、不将有效声明改成 unavailable；运行采用无法精确关联时单独 UNKNOWN。实际启用／运行准备按第 12.3 节；查询取消／失败的 anchor 清理保持。
- MCP：tools/resources/templates/prompts 无 transport list 请求；配置与 runtime 来源精确关联，无法关联时公开未知；DIRECT 只描述已安装定义，META 才发 ref，UNAVAILABLE 不发 ref；collision、ROOT/subagent scope、stale ref、ref 提交/取消 settlement、大 schema 可见失败全部保留。
- 权限：新查询是只读，可在 READ_ONLY 下进行获准观察；schema/ref 公布不执行，read/get/use 仍走原 owner 和权限；管理只保留变更动作与原 HITL。
- 前缀／持续运行：旧 epoch exact SYSTEM/tools 与 messages suffix-only；新冷根没有旧五个查询名称和两个管理 inspect 动作；分页、多轮来源变化没有新总量上限。
- 静态：focused Python、受影响 PostgreSQL/架构闭合、前端 summary/runtime-adapter、TypeScript、Ruff、协议生成及正常构建；只更新有产品理由的 oracle，不弱化断言。
- 真实 GUI：生产保存配置只读，桌面 test1 下隔离项目，GPT-6 Luna / xhigh；自然语言询问能力及其来源／位置，直接看到 disabled Plugin 声明的能力，安装一个 loose Skill／小测试 Plugin 后直接使用返回身份与位置筛选／查询／管理；确切空包仍可查看和管理，完整包盘点留在 GUI。查询 MCP schema 再做一项已授权代表调用；观察模型不用旧工具、猜路径、传播 home 或误把 list 当远端调用。实际需用户确认的表单仍走 GUI，只报告实际完成的验证。

## 10. 审阅记录

2026-10-01 模型负担追加审阅：收紧此前“所有视图沿用位置字段”和“非秘密配置”两处过宽承诺。列表仅承担发现与选择，位置进入操作结果／确切详情；内部发现根枚举、存储关联键、高级 MCP 策略及 Hook 审阅 DTO 不直接进入模型结果。新增第 4.5 节规定显式投影、按问题提供诊断上下文与管理结果减法，保留完整调用 schema、实际失败和原生 GUI 完整配置。同一 critic 认可字段减法，指出 server 摘要不能扩大 SUBAGENT 原 scoped catalog 范围；已明确 ROOT 安装／精确 runtime 配置与 SUBAGENT 目录观察的区别，并将 Hook 授权限定到当前定义。critic 认为以上收口后可冻结。本次仅修订文档，不实施统一查询或其他生产代码；文稿待用户审阅。

2026-10-01：主 agent 根据当前生产 owner 形成初稿，覆盖两条用户批注。同一 GPT-6.1 Sol / xhigh critic 核对现有 scope、Plugin summary/state layout、MCP source identity 与查询/ref owners 后复审通过，无阻塞项。讨论后的修订包括 ROOT／SUBAGENT 范围、本地查询无 MCP port 依赖、disabled 组件观察、runtime 来源与采用分开、未关联 MCP 子项的完整性，以及 owner 整体不可用时的公开失败。主 agent 完成修订并冻结，待用户审阅。

2026-10-01 用户审阅后修订：取消模型全量 Plugin 枚举。list 直接列能力，Plugin 仅在 source 中出现；确切 Plugin 查询／筛选／管理保留，完整包 inventory 与空包／坏包排障留在 GUI。同步修改参数、默认页、示例、路径字段适用对象、诊断与验收，不用空 Hook source 或诊断名单替代包枚举。critic 指出确切查询不能在全量 inspection 后筛选；主 agent 将同一 owner 内的确切 identity 观察扩展、声明与全局 effective 分开、健康 A 与坏 B 的验收写入规范，并统一示例身份来源。同一 critic 收口复审通过，无剩余阻塞；此版重新冻结。

2026-10-01 字段审计：依据 MCP 官方 2026-07-28 规范、本地 SDK 2.1.0 的 stdio／mcp-types，以及生产 Plugin manifest、summary、store lock、MCP／Hook adapter 消费点，明确协议、进程配置与 Plugin store 三层。按第 4.4 节精简模型字段；先前会话模拟中的额外展示名、data_root、总 package_valid、package_in_use 与 aggregate adoption 不构成最终公开合同。同一 critic 文稿复审通过，无阻塞，补充所有子项无效而 COMPLETE count=0 的验收。本阶段仍无运行代码变更；第 11 节是主 agent 独立检查的配置设计事实与后续建议，未据此实施配置／运行合同变更。

2026-10-01 自定义字段审计补充：主 agent 核对第 11 节配置消费链，并用无 I/O 的 _effect 函数 probe 复现 AUTO／无 override 时缺失 openWorldHint 的分类差异；未连接服务器或启动模型会话。critic 核对现有 inspection 的目录副作用，同意将无运行准备的声明观察写入本次查询必要边界；默认查询与确切查询均保留声明，不通过 prepare 生成 liveness／effective。critic 同时核实四项代码事实及其适用范围，无剩余模型投影／只读接线阻塞；来源查询规范冻结，独立 runtime／权限建议仍待决定。

以上是四项运行修订获准前的文档／纯函数审计记录。统一来源查询入口及其第 9 节验收仍待整体实施；已授权并完成的四项运行修订及验证见第 12 节。

## 11. 独立 MCP 自定义字段设计审计

本节按用户新增问题检查生产配置及消费点，区分“不是 MCP wire 字段”与“没有合理用途”。不是协议字段并不意味着应删除：Host 启动程序、提供凭据、决定权限、分配物理资源本来就是客户端职责。本节记录实施前审计事实；用户已授权四项一起修订。第 12 节取代本节各项中的“后续／本轮不改变”范围声明。保留并发额度、外部旧端协议兼容及 provider prefix 连续性。

### 11.1 有实际用途的客户端／Host 字段

| 分组 | 生产消费点与产品用途 | 判断与模型边界 |
| --- | --- | --- |
| command／args／cwd／env、HTTP endpoint | SDK StdioServerParameters 有对应进程启动参数；sdk_facade 负责获准 cwd／环境及 HTTP 接入，SDK 负责协议交互 | 合理的连接配置；模型只在安装／修改／定位问题时接触，不作为远端工具调用参数。cwd 是进程工作目录，不是 Plugin scope 或数据目录 |
| secret_env、auth、public_headers、OAuth 配置 | mcp_credentials、原生管理与 SDK OAuth／HTTP owners 绑定凭据并提供请求配置 | 合理；常规安装使用现有 GUI／私密表单，模型不填凭据值或理解 store 引用格式。OAuth 字段属于认证配置，不冒充 MCP 工具能力 |
| enabled、required | supervisor.start 跳过 disabled，required 会使首次 provider dispatch 等待服务器启动或失败 | 有效的 Host 产品控制；required 表示启动依赖，不表示 server 宣称自己必需。Plugin adapter 固定 required=false，不能由包作者授予阻断 Host 的权限 |
| scope_policy | discovery、scoped catalog、dispatch 决定 ROOT／SUBAGENT 可见与调用范围 | 合理的 Host 权限；不是安装 USER／WORKSPACE scope，不把两种 scope 混用。模型查询已按调用者裁剪，不要求它自行应用 policy |
| exposure_policy | supervisor.discover 的 include／exclude 及 invalid schema 处理 | 合理的工具选择／失败策略；不是服务器目录本身，也不意味着被过滤项不存在。保持现有校验，按需在配置 GUI 查看 |
| effect_policy | supervisor._effect 产生权限分类，tool_runtime 据此拒绝 READ_ONLY 下的 EXTERNAL_EFFECT 或申请确认 | 有真实权限用途。AUTO 采用保守 EXTERNAL_EFFECT，只有 Host override 授予只读；服务器 hints 仅是说明，只读也不自动意味着并发安全 |
| network_policy、allow_http_localhost | sdk_facade HTTP 接入与 OAuth 网络检查执行公网／私网及本机明文例外 | 合理的 Host 接入策略；SDK 协议支持不替代此产品边界，不让模型对常规安装手填全部网络字段 |
| supports_parallel_tool_calls、stateless_http_max_in_flight | 前者进入执行策略；supervisor 根据 client 的物理模式设置同一 slot 的并发 lane | 应保留应用并行许可和物理资源边界这两种需求，但不称为标准 MCP server capabilities；具体命名及 stateless assertion 见后文 |
| default_tool_timeout_ms、per_tool_timeout_ms | discovery execution policy 和 operation deadline 使用 | 合理的每次操作边界，不是会话／工具总次数限制。默认与 override 由 owner 执行，常规查询不要求模型调参 |
| runtime_source、physical_lifetime_anchor、配置身份／secret resolver | supervisor 精确关联来源／配置、维护在用包的物理 lifetime，ref／执行策略沿既有身份绑定 | 内部真实边界；不直接放进模型结果，也不把普通 target 当成 permit。不要因为公开字段减法而删掉实际 owner 检查 |

没有发现以上组只是定义而完全没有生产消费点。它们不应作为模型常规步骤中的必填术语；保留 native 高级配置并不要求模型了解内部 DTO。

### 11.2 四项问题与本轮修订决定

1. **无状态声明不再叫证明。** 原 proved_stateless 从 2026-08-14 的 edbe7aea 首次实现就存在；e0053068 与 90ee7439 后增的是 SDK 1.x 旧端握手、implicit-complete 和限定 HTTP 400 兼容。字段 hard cut 为 stateless_http_asserted，保留双 Host 配置及 session observation 的保守并发门槛。SDK 发布版本与实际日期协议分开；[旧版 HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) 的 session 约束仍保留，[现代 HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) 无协议级 session 不授予业务并行许可。首次目录完成后才开放 bounded lane，晚到 session ID 沿既有 failure owner fence。详见第 12.1 节。

2. **目录刷新真正刷新目录。** 审计前周期及 listChanged 都调用 _start_connect，可能重启 stdio。改为 dirty-fence 后让已接纳调用完整结算，再独占同连接 relist；周期值、列表通知与既有断连重试职责不变。不重建 provider prefix。SDK 完整分页派生缓存由其自身 absorber 收口，详见第 12.2 节。

3. **data_root 按本地运行需要准备。** 移除启用与 inspection 的 mkdir；只有 Host 显式运行观察才为 valid stdio／Hook 准备 private 目录。Skill／HTTP-only 包不创建。失败仅阻止本地进程，保留远端 HTTP、Skill、原声明及优先级 claims；诊断沿原 owner，详见第 12.3 节。

4. **服务器 hints 不授予只读权限。** 审计前 AUTO 将缺失 openWorldHint 当 false，与 SDK／[官方 tools 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) 的默认 true 不符；且服务器 hints 本身不是可信授权。改为 AUTO 保守 EXTERNAL_EFFECT／HOST_DEFAULT，不再以任何 hint 组合授予 READ_ONLY；只有既有 Host tool／server override 可以指定只读。保留 SDK 原始 metadata，不新增 trust 设置；详见第 12.4 节。

### 11.3 高级配置与模型任务的边界

config_to_entry 是原生完整配置投影，包含上述高级字段，不应直接当成日常能力详情。模型通常只需知道连接目标、启用状态、实际定义位置、当前可调用模式与具体失败；诊断确实涉及某个高级设置时，再由该配置／运行 owner 如实提供相关事实。

现有 manage_capability 的 UPDATE_LOCAL_MCP.config 是完整替换，不是 patch。若模型手写“只改 endpoint”的不完整对象，会把未携带的高级设置恢复成默认值。因此现有最短安全路径是省略 config，让原生 owner 打开从当前真相预填的 GUI；原工具说明已经支持这条路径。不能为了少显示字段而教模型用部分配置覆盖整条记录，也不在本轮暗加 merge／patch API 或配置私有字段回读。

默认查询不用暴露高级配置，不表示配置字段全都无效；本轮四项运行修订仅按用户授权及第 12 节执行，不增加模型高级设置负担。


## 12. 四项配置／运行问题的本轮修订合同

本节由用户追加授权；只实施以下四项及其直接消费者、测试与说明，不先行增加 list_capabilities／inspect_capability。第 11 节保留问题背景，执行行为以本节为准。

### 12.1 无状态声明与并行许可

将原生 transport.proved_stateless 完整 hard cut 为 stateless_http_asserted，默认 false；旧键拒绝，不添加兼容读取。它只表示 Host 操作者按无状态 HTTP 配置服务，不声称已证明业务无状态。supports_parallel_tool_calls 仍是独立的 Host 调用并行许可，两个选择都显式开启且 Streamable HTTP 未观察到 session ID 才允许既有 bounded lane；首次完整目录后再确认该门槛，stdio 与 SSE 保持串行。开放 bounded lane 后若 HTTP header 又出现 session ID，立即通过原 slot failure owner 以协议矛盾、不可自动重试的状态 fence；已接纳请求继续按实际响应结算，不在有 permit／lease 时替换 Semaphore。SDK 继续拥有 discovery／initialize 与实际日期版本协商，保留旧端结果归一化及限定 HTTP 400 适配。新版协议无 session 不能自动替代应用并行许可；不新增协议事实 DTO、声明回执或信任登记。GUI 将两个选择解释为操作者配置，并保留既有物理容量；模型默认查询不曝光这些高级选项。

### 12.2 目录刷新复用当前连接

SDK 拥有 tools/resources/templates/prompts 的协议列表，supervisor 拥有完整目录候选、现有 slot fencing、物理 lane、Host byte reservation 与 safe-point 安装。定期刷新及 listChanged 合并后调用同一当前 slot 的完整 relist；不再创建新 client、不再 initialize，不重启 stdio。保持 catalog_refresh_interval_ms 名称、默认、已有合法区间与 DISABLED。

通知立即 mark_dirty，旧 lease 不重新变成 current，已接纳调用仍正常结算；同连接 relist 先等待 pre-fence 已接纳 dispatch 完整结算，再独占原 slot lane 和 Host lane 进行整轮列表；不等待旧 lease 全部释放，也不以连接超时截断这段调用结算等待。实际列表 I/O 保持既有每次操作超时。SDK 的 list_tools 会立即更新结果 schema／header 派生缓存，call_tool 在响应后消费该缓存，故仅共享可并发 Semaphore 不够；必须保证调用和列表不重叠。周期刷新也通过相同 fencing／relist 入口。并发通知仅沿现有 refresh_generation 合并；relist 期间又 dirty 则丢弃候选并继续下一次完整 relist，无任意次数上限。只有 slot 身份、配置身份、attempt_generation、refresh_generation 和 dirty_generation 仍精确匹配时，才能重新打开 slot 并发布当前代的新 lease；老 lease 的 generation 永不改写。safe-point 替换同 slot 的候选只释放旧候选 lease，不能退休或关闭该 slot。多服务器中若 A 的 installed slot 仍 dirty／等待调用结算而 B 已完成 relist，本次 safe point 返回 None 并保留 B 的 pending 和原目录；A 完成后再发布完整运行面，不能抛 lease mint 错误、丢弃 B 或重开 A 旧 lease。

SDK 2.1.0 的 list_tools 只对无 cursor 的单页完整结果清除已删除工具缓存，分页逐页只追加；没有公开“整轮分页完成”或 cache invalidation 接口。因此 facade 在全部分页、目录校验与预算检查成功后，调用同一个 SDK _absorb_tool_listing(ListToolsResult(all_tools), complete=True) 收口派生缓存。该窄 adapter 受 pyproject／uv.lock 的 SDK 2.1.0 版本约束；SDK 继续拥有 header 解析、输出 schema 及 validator，不直接操作其缓存字典、不复制 parser。失败／取消不宣告完整，slot 保持 fence 后沿原故障 owner 处理。已删除项与无限次刷新不留下累计工具缓存，不引入总次数／总历史 cap。

初始启动、显式重连、配置替换及既有 transport failure/retry owner 继续拥有连接重建。周期刷新不绕过 FAILED_TERMINAL，也不产生额外重连重试轨；无 slot 或连接尚未就绪时让既有连接 owner 推进。relist 的协议／目录错误通过现有 slot failure owner 可见，沿现有 retryable 分类处理，不保留伪成功新目录。关闭与配置替换取消并等待 relist，取消不能被记成新的连接故障。目录更新只产生既有候选，不重建当前 epoch 的 SYSTEM/provider tools。

### 12.3 Plugin 数据目录只在本地运行准备中创建

set_enabled 只提交 enabled 状态与既有连接 review；不创建 data_root。freeze_enabled_plugin_instance 只投影已观察声明，inspection 不准备目录。EnabledPluginViewOwner.observe 是纯观察，Host 两处使用显式 observe_for_runtime；CLI／Skill 管理观察继续使用 observe。Host 运行物化在该纯观察后，为 valid stdio MCP 或 Hook 进程准备既有 private data_root；只有 Skill、HTTP／SSE 的包不准备该目录。不新增 store 或延迟任务，不删除已有目录及用户文件，不由 HTTP server 在本机创建业务文件。

目录准备失败是本地运行资格失败：保留 Skill、HTTP／SSE 与声明 summary；阻止受影响 stdio 配置和 Hook 进入运行面，并报告 DATA_ROOT_UNAVAILABLE。MCP declared_server_ids 继续保留，不能因本地准备失败使低优先级同名 server 意外生效。单项 stdio 剔除由原 parsed component 投影表达，HTTP 子项仍可用；不追加另一套 resolver。目录权限、nofollow、取消及失败均沿原 store owner。

### 12.4 服务器提示不授予 Host 权限

默认 AUTO 采用保守 EXTERNAL_EFFECT，classification source 为 HOST_DEFAULT。tool_effect_overrides 优先于显式 server default_effect；只有这些既有 Host 配置可以授予 READ_ONLY。服务器 ToolAnnotations 继续作为 SDK metadata／工具描述供理解使用，缺失值沿规范含义解释；任何 readOnlyHint／destructiveHint／openWorldHint 组合都不升级执行权限。删除 SERVER_ANNOTATIONS 授权分类路径；不新增 trusted_annotations 开关或认证／信任机制。Plugin 默认 AUTO 也同样保守。GUI 的 AUTO 文案改为“保守默认（需要授权）”，明确 READ_ONLY 是用户指定的分类。

验收覆盖现代／旧端与 session ID 矛盾的并发 gate、旧配置键拒绝、周期与通知不重启 client、同 slot lane 互斥、刷新竞态与 old lease stale、断连恢复／关闭、Skill-only／HTTP-only 启用与检查无 mkdir、本地运行 private data 及目录失败的组件隔离、AUTO 缺失／true／false／伪只读 hints 均不授权，以及显式 tool/server override 优先级。运行原 MCP／Plugin 与前端回归、架构约束检查。无需真实模型会话才能验证这四项协议和 owner 行为；本节未激活来源查询的完整第 9 节 dogfood。


2026-10-01 第 12 节实施验收：四项运行修订完成；同一 GPT-6.1 Sol / xhigh critic 实现交叉审核通过，无剩余阻塞。审核发现的 CLI parser flag 未同步和双 server safe-point dirty lease mint 问题均修复，新增 owner 回归 38 项通过。全仓库非 PostgreSQL／非 retrieval_live 测试 2079 项通过（404 项依标记未选择），包含架构约束检查；前端 587 项通过，生产静态包重建与针对变更的 Ruff 检查通过。广回归中旧 Host 测试替身入口已同步为 observe_for_runtime，未改断言；前端一次提示补全时序失败在单独及全量复测均通过，未修改该组件。未修改生产配置或数据库，未运行真实模型会话。统一来源查询工具尚未实施，不将这次通过记录当作第 9 节完整激活证据。


## 13. 本轮代码验收与真实会话进度

2026-10-01 的实现保留原生 GUI／安装／执行 owners；新增两个模型查询工具，删除被替代的五个 MCP 查询入口及两个只读管理 action。位置字段、来源、目标及模型详情按第 3–6 节投影，内置 capability Skills 和前端工具摘要同步更新。没有新增持久状态、事件、关系、任务、身份注册表或 provider prefix 重建边界。

代码复审已修复并验证：健康 Plugin Skill target 与无关坏包隔离；Plugin MCP 摘要应用当前 overlay 和输入默认值；查询不为来源采用重新物化凭据；确切无效 Hook 保留真实诊断；普通大型详情沿既有 OUTPUT artifact 分页；精确查询区分原生来源失败、runtime 未安装／目录未发现、完整观察中的目标缺失，并保留 caller scope 隐藏边界。MCP_TOOL schema 仍 FULL-required，DIRECT／META／UNAVAILABLE 与既有 ref 结算不变。

非真实会话验收：广泛非 PostgreSQL／非 retrieval_live 测试 2117 项通过；随后补充的来源查询／应用边界 75 项通过；相关 PostgreSQL 测试 29 项通过，另有大型 Hook 详情的真实 PostgreSQL artifact 分页往返 1 项通过。前端 35 个文件、583 项测试通过，TypeScript 类型检查与本地静态资源构建通过；修改文件 Ruff、编译、协议生成检查和 diff whitespace 检查通过。测试计数按各次运行记录，不累加重叠用例。

同一 GPT-6.1 Sol / xhigh critic 独立复审通过：查询／结果投影 68 项及最终失败／范围边界 8 项独立验证通过，确认无剩余阻塞并准许进入真实 GUI dogfood。

真实会话准备使用 saved LocalSettingsStore 配置的只读注入，临时 Pulsara home 与已验证的本机 disposable PostgreSQL database。原用户 MCP 文件还包含上一轮删除的 proved_stateless；保留原文件，通过隔离 home 避免把旧配置兼容路径加回来。测试源位于 Desktop/test1/capability_source_query_20261001，demo Plugin 原生校验所得 1 Skill／1 MCP／1 Hook，空 Plugin 为 0／0／0，均无解析诊断。用户通过 GUI 选择该测试目录后，使用保存的 OpenRouter GPT-6 Luna / xhigh 连接完成真实验收；没有注入伪造模型回复或工具结果。


真实 GUI 首轮在安装前被 FOLLOWUP_INPUT_TOKENS 中断。既有 runner 把首次 INSTALL_PLUGIN 也按可能改变 Skill/MCP 目录收费，但 native store 首次发布 disabled，enabled-view owner 在观察组件前跳过该实例，因此该分支不可能改变两个运行目录。本轮只删除 `INSTALL_PLUGIN` 且 `replace != true` 的目录变化预留；替换、启停、删除、reload 及 MCP 变化预留保持。没有扩大模型预算、削弱资源边界或重建 provider prefix。重启同一隔离测试环境后，从普通 GUI 继续实际任务。资源测试文件 61 项通过，critic 独立相关分支 8 项通过。

前端同时修复两个可见误报：完整 JSON 清单携带原生 artifact reread footer 时，显示层先分离标准 footer 再解析正文；缺少有效 items/total_count 的等待或失败结果不再冒充“0 项能力”。实际默认页 29 项能正确显示。前端应用文件回归 143 项通过，TypeScript 检查、本地静态资源构建、修改文件 Ruff/编译及 diff 检查通过。critic 核对 footer 精确协议与首次安装分支，无阻塞。

实际完成的三轮结果：

- 安装与来源：内置管理成功安装 WORKSPACE loose Skill 与 demo/empty 两个禁用 Plugin。demo 确切详情为 1 Skill／1 MCP server／1 Hook definition，empty 为 0／0／0；按 demo 返回身份分页 offset 0→1→2，next_offset 最后为 null，三个返回 target 均能确切检查实际定义位置。禁用／未信任障碍如实返回。独立 Skill 沿返回 skill_path 读取已安装正文，实际回复 SOURCE_QUERY_SKILL_OK。
- MCP：内置 ADD_LOCAL_MCP 写入项目 `.pulsara/mcp.yaml`，返回实际 config_path。目录发现前返回 PARTIAL 与 MCP_CATALOG_NOT_DISCOVERED，随后普通查询所得完整清单为一个工具；inspect 返回完整输入/输出 schema、META 与已提交 tool_ref。一次 use_new_mcp_tool 实际返回 SOURCE_QUERY_ECHO:roundtrip，isError=false。服务端只读 hints 没有授予权限，未配置 Host override 时仍为 EXTERNAL_EFFECT；本次代表调用在完全访问下完成。没有出现 HITL 表单，因此不声称实测了确认表单分支。
- 只读：GUI 选择只读后，canonical turn 的 requested/effective permission 均为 read-only，轮次 COMPLETED。实际仅执行 list_capabilities/inspect_capability，两个 Plugin 仍禁用，Hook 为 UNTRUSTED/selected=false，空 Plugin 计数仍为零，MCP 完整 schema 与 META 查询成功，没有执行或修改。

真实 trace 没有旧五个查询入口、terminal CLI 安装或直接改写 managed state；Plugin 数据目录在安装和查询完成后仍不存在。安装及 MCP 变更的原生采用结果报告 same_epoch_prefix=UNCHANGED；逐字 prefix 连续性由相关静态／PostgreSQL 回归验证，本次 GUI trace 不冒充原始 provider wire 逐字比对。模型曾从已知 server_id 构造 runtime parent；后续确切工具 target 与 tool_ref 来自真实查询结果。本轮验证的是可工作流程，不把单次模型行为当作“永不猜目标”的保证。

两个既有资源预估问题单独记录，不混入本轮查询 hard cut：loose Skill 会在后续 dispatch 重新观察，但其变化尚未纳入后响应目录变化预留；已 JSON 串行化的目录仍使用通用控制字符最大展开上界，预留偏粗。进一步修改需要按各自 owner 格式证明和独立资源合同收口，不随手缩小 Hook 上界或扩大模型预算。这两项没有阻塞上述实际验收，也不记作已经修复。


收尾：已停止隔离 runtime，并核实临时 home 与本机 dogfood database 均已删除；项目内测试源与 loose Skill 保留。全局 `uv tool` editable 环境原先缺少上一轮搜索拆分新增的 wcmatch，已使用仓库 uv.lock 导出的约束离线同步依赖。随后从 Desktop/test1（非源码 cwd）通过全局 pulsara app 正常启动普通生产 listener，bootstrap/model settings 和 sessions API 正常；数据库 status 为 up_to_date。普通 GUI 的能力读取及已有会话恢复仍被用户级 mcp.yaml 的旧 proved_stateless 键阻断，原生声明 parser 确认报错 MCP HTTP transport contains unknown fields: proved_stateless。没有改写该生产文件、重置生产数据库或增加旧字段兼容；这个配置阻塞与本轮隔离功能验收通过分开报告。


2026-10-01 后续配置收尾：用户明确授权移除旧键后，仅删除有效 Pulsara home 的 mcp.yaml 中一处 proved_stateless:false，校验其余 YAML 值完全保留。原生声明解析通过，普通 GUI 能力 API 返回 200，刷新后已有生产会话正常恢复。未重置生产数据库、未修改 local-settings.yaml，前述生产配置阻塞已解除。

### 13.1 Ponytail 生产 dogfood 暴露的清单预留修订（2026-10-01）

首次安装保持 disabled 并成功；启用调用被 FOLLOWUP_INPUT_TOKENS 拦截。目录来源预留使用原始控制字符 6x 展开，但 Skill render_catalog_prompt 与 MCP _bounded_mcp_catalog_provider_body 均先通过 canonical_json_bytes 生成紧凑 JSON，随后才按既有 UTF-8 上限检查。合法 JSON 正文没有字面控制字符，剩余字符在 observation JSON 内至多 2x；因此目录预留使用相同字节上限的全反斜线正文表示编码上界。后续 provider adapter 的二次转义和 token estimator 仍实际计费，不缩小目录上限、不扩大模型预算，也不改写 provider prefix。原始 Hook context 保留控制字符上界，不受此修订影响。回归测试覆盖 JSON 反斜线、引号、控制字符转义、Unicode 和两类 wire adapter，并保留目录变化分支及 canonical/epoch/wire/token 边界测试。此项收口只解决已序列化目录的上界问题；loose Skill 变化的预留归属仍是独立待修项。

进一步实际重试确认：仅修正转义仍会把“所有清单都达到最大容量”的假设当成当前模型的输入，导致正常启用仍中断。目录变化预留继续计入 epoch 物理字节上界，但该尚未发生的最大目录不进入后响应 wire/token 预拒绝；下一 safe point 由原有 source collector 冻结实际目录，原有 dispatch planner 按真实 wire/模型预算接纳，必要时沿既有 compaction 边界压缩。工具结果、Hook、图片、规范行与 epoch 字节边界保持；不允许超预算 provider dispatch，也不承诺任意能力修改后无需压缩即可立即调用模型。没有新 owner、事件、字段、durability 或 prefix 重建例外。

验证状态：资源测试 82 项通过；与本轮 Hook／Plugin／能力查询和架构检查合并运行 240 项通过，Ruff 与 diff whitespace 检查通过。真实生产 GUI 重试完成 Ponytail 的 USER 安装、启用和 TRUSTED 审阅，随后独立冷会话 4f80aa1c 验证 SessionStart／SubagentStart 规则注入，以及 UserPromptSubmit 的 full→ultra→off→full 模式切换。主会话与真实子任务均完成，无再次资源中断；具体输入与运行边界见 Hook 规范文末。


### 13.2 实际清单最终 wire 选择与迟到结算上界（2026-10-01）

本节收口资源审阅中的 P1、P2，取代第 13.1 节对“现有 compaction 即可处理实际清单超预算”的不完整推断。最大 Skill/MCP 清单不重新进入模型窗口的后响应预拒绝；JSON 清单 2x 编码上界继续有效。普通工具结果按最大 FULL wire 预留的行为保持，留待后续单独商议。

P1：compiler 继续唯一拥有合法来源变体、降级顺序、不可丢内容与 append-only 编译；provider_dispatch 继续唯一拥有实际 Chat/Responses wire 计量与接纳。在同一个已经冻结的 canonical/source/target/planning cut 上，最终 wire 不 fit 时仅推进尚未安装的 Skill/MCP 清单合法变体，并按既有 compiler 重编、重新实际计量。清单 source 真值、原 request 与其他已选来源/工具结果展示不被重抓或升级；选择下界是一次本地规划的参数，不是新 DTO fingerprint、来源语义或 durable 状态。有限变体自然终止并沿现有局部 deadline，不能添加总尝试数或生命周期 cap。

普通工具后续、冷输入、prospective 输入与 compaction 的 dry/post successor 采用同一最终 wire winner；保留原 exact-join、一次性 Hook reservation 和 cut/borrow authority。summary、repair 或仅有 semantic projection 的候选没有完整编译 basis，不能通过此路径改写已经冻结的 summary 输入。只有冷 epoch 或已采用 compaction 允许重建输入根；同 epoch 的 SYSTEM/tools/旧 messages 保持原字节，仅编译新 suffix。Skill 清单无法完整展示时使用现有 UNAVAILABLE_MINIMAL 明确观察，不能冒充空清单或不存在；MCP 沿 FULL/COMPACT/REF_ONLY。最小合法输入仍超预算时保留 typed failure；历史压缩不保证能够修复来源自身超预算。

P2：ordinary preview 允许原始纯文本，reader 会将其再次放入 late outcome JSON；该 canonical 预留按原始控制字符 6x 构造上界。已序列化目录的 2x 证明不能套用到 plain-text preview。保持原有 canonical bytes/items、epoch、wire 与图片额度边界，真实 preview→closure/late→canonical 计量及临界 gate 必须覆盖；typed 图片沿既有 frozen prompt owner 增量计费，不复制图存储协议。

验收包含两类 wire API、同 epoch 前缀、最小历史冷 successor、MCP 合法变体、能力变更与图片混合后续、最小仍 overbound 的明确失败，以及真实 late preview 和 typed 图片结算上界。测试通过不代替实际最终 wire winner 的 owner/消费者一致性检查。

2026-10-01 收口：GPT-6 Astra / xhigh critic 交叉复审通过，无剩余阻塞。复审补齐冷 assembly 与 candidate 的完整 request/planning/result/call/read 校验，以及 floor 选择或重编异常时尚未消费的 wire measurement 恰好释放一次；没有新增 authority、持久状态或恢复机制。聚焦回归 189 项通过；全仓库非 PostgreSQL／非 retrieval_live 回归 2205 项通过（404 项依标记未选择）；PostgreSQL Host/runner/图片核心集成 147 项通过。上述运行包含重叠测试，不累加为总数。修改文件 Ruff 与 diff whitespace 检查通过。

随后使用保存的 OpenRouter GPT-6 Luna / xhigh 配置，通过生产 KernelHostCore/ModelRuntime owners、只读设置注入、临时 Pulsara home 和已验证的本机 disposable PostgreSQL database 跑窄范围真实 provider dogfood。两轮、六次实际模型调用完成：manage_capability 安装 USER loose Skill 后继续读取文件与图片；后续 list_capabilities 确认新 Skill。全部实际 wire quote 在模型预算内，实际 HTTP 返回 200；临时 home/database 已清理，生产配置与数据库未改。首次临时脚本未解析 macOS 符号链接目录且使用相对读取路径，产生 STAGING_UNAVAILABLE/FILE_NOT_FOUND；修正测试路径后通过，保留首轮失败记录，没有放宽生产路径校验。这次真实运行验证正常能力变更与图片续接；100k 清单超预算降级、两类 wire API 的逐字前缀、迟到控制字符极限及异常清理由受控真实 compiler/adapter/preview 回归证明，不将窄 dogfood 描述为这些极限分支的真实 provider 实测。

### 13.3 普通工具结果的最低反馈预留与最终展示选择

本节取代第 13.2 节中普通结果仍按最大 FULL wire 预留的保留项。canonical bytes/items、epoch logical bytes 继续使用原最大结算上界，包括 late 原始文本 6x；只有 ordinary BEST_AVAILABLE 的执行前 wire/token 预留改为既有 OMITTED_BODY 的合法上界。物理最大值与模型展示下界必须分开计算。最小表示仍无法容纳时，保持 effects/assistant settlement 前的 OutputResourceInterruption，不新增当场 compaction/retry owner。

lowering 唯一拥有结果变体与最低正文 renderer，复用既有 metadata/outer envelope；runner 不复制字段或发明新的模型结果格式。上界保留闭合状态、coverage、artifact 可用性及原生固定格式 ID、全部合法 timing、既有 canonical JSON 8 KiB/50 条 memory provenance 边界。必须覆盖正常结果与 closure+late 分支，不能承诺 COMPACT/REF_ONLY 一定存在或存在完整 artifact。上界仅用于本地规划，不发送或持久化。

执行前使用既有 classify_tool_result_delivery 的成功分支及实际 public arguments；可能有图片的调用沿既有图片解析/绑定 owner 判断。成功的目录页、MCP schema、artifact read/export 与图片保留最大 FULL 预留，本节不优化它们的收费。SKILL_ACTIVATION 当前没有实际分类消费，不根据 read_file 路径另加完整交付规则。Plan batch 保留专门预留；实际 timing origin 为 PLAN_CONTROL 的结果不进入新增 wire 降级。其既有省略正文误投影为 REJECTED/error 的问题不记作本节已修复。

实际结果先尝试最丰富的合法展示；最终 wire 超预算时，compiler 在第 13.2 节同一反馈入口按既有 tool/source degradation key 推进普通结果与 Skill/MCP 清单的实际可用变体。不得假设每一级 wire 单调减少；有限变体自然终止并沿既有 deadline，不新增重试次数 cap。变体扫描通过既有 KernelSessionIO worker 执行，复用 compiler deadline 在逐项扫描和 lowering 前后检查；异常时未消费的 measurement 恰好释放一次。原 request/canonical/source/target/planning 完整值保持，其他已经降级的选择不升级。same epoch 只选择未安装 suffix；cold/adopted successor 可重选合法历史展示。已有 SYSTEM/tools/messages、实际 assistant/native replay、FULL_REQUIRED 和图片不削减。

图片混合批次的 base suffix/base quote 同步使用普通结果最低上界；释放的 wire/token 空间可分给图片，图片增量仍实际计量，canonical/epoch 最大保护不变。dry/post、prospective、Hook 与普通后续继续采用同一实际 wire winner。模型不新增预算参数或手动降级操作。

合同保证规范结算与冻结基础上的最低反馈可表示，实际 provider 输入始终经过最终 gate；不保证未来追加 mandatory 输入必定容纳、compaction 必定恢复或任意结果完整内联，不因后续模型调用失败重做副作用。验收覆盖两 API、正常/late、极限 provenance/timing、缺失 COMPACT/REF、FULL/Plan 排除、混合多图片、native replay、逐字同 epoch 前缀，以及最低仍不 fit 时未执行工具。代码测试后由同一 Resource reservation critic 交叉审核。

2026-10-01 收口：本节已完成代码 hard cut，compiler 合同升级至 v16-tool-result-final-wire，原 catalog-only 反馈入口已删除。Resource reservation critic（GPT-6 Astra / xhigh）完成代码交叉复审，无剩余阻塞；复审发现的新变体扫描 deadline／事件循环接缝已按既有 worker 和 deadline owner 修订，未增加调度或持久机制。最终全仓库非 PostgreSQL／非 retrieval_live 回归 2242 项通过（406 项依标记未选择）；PostgreSQL Host/runner/图片核心集成 147 项及新增完整 runner gate 回归 2 项通过。critic 独立聚焦回归 82 项通过，包含新增两项 PostgreSQL 测试，与上述集合有重叠，不累加为总数。修改文件 Ruff 与 diff whitespace 检查通过。

新增 gate 回归使用 scripted 模型和真实 PostgreSQL runner：64k 窗口接受三次普通工具并完成后续（旧最大 FULL 后缀自身约 119k），32k 窗口因最低反馈不足在 assistant settlement／effects 前拒绝，执行尝试与 assistant／工具结果记录均为零。两 API 的多图片与大 native replay 使用真实 quote owner／adapter 受控验证 same-base 增量和完整 replay 字节收费；正常／late 上界、来源极值、FULL／Plan 排除和同 epoch 字节连续性均有受控回归。本节未将这些极限分支描述为真实 provider dogfood；第 13.2 节真实运行记录仍只证明其当时的窄范围流程。
