# Pulsara 内部版本标签与校验减法 Hard-Cut 草案

状态：实施规格，待实施验收。日期：2026-11-11。

本文覆盖本轮扫描确认的生产代码、数据库和配套测试。按用户授权在文档基线提交后实施；验收前不声明完成，不修改用户保存的生产设置。

## 1. 目标与最终语义

Pulsara 当前内部数据只使用当前实现规定的一种结构。内部对象不再携带用于声明“我是第几版结构／实现／规则”的标签，也不再先检查固定版本字符串才解释对象。

删除不是把 `schema_version` 改名为 `contract`、`generation`、`schema_id` 或固定 fingerprint。字段生产、存储、传递、消费、校验，以及只验证这些标签的测试一起删除。只剩标签和标签证明的 DTO、注册表或包装层也一起删除。

保留业务数据本身及其必要约束：字段类型、有效枚举、图文顺序、引用与实体身份、权限、取消、幂等、实际内容完整性、并发控制、provider 输入连续性和真实外部协议要求。**删除版本标签不等于不验证数据。**

本次仅删除无独立用途的标签、重复证明、失效透传和专属测试，不修改产品算法、预算口径、业务分支、权限规则或输入连续性语义。不新增字段、机制、owner 或替代证明；必要的关联和一致性判断使用已有实际值。

本次不承担旧会话、旧数据库结构或历史内部载荷的读取兼容；不增加迁移链、版本选择器、双写、别名、回退或兼容测试。

## 2. 删除边界

### 2.1 完整删除的对象

以下名称只是检索入口，不是封闭名单：

- `schema_version`、声明内部载荷版本的 `schema`。
- `contract_version`、`request_schema_version`、`request_contract_version`、`source_contract_version`、`implementation_contract_version`、`transport_contract_version`。
- `builder_version`、`semantics_builder_version`、`renderer_version`、`rollup_renderer_version`、`normalization_contract_versions`。
- `estimator_version`、固定的 `image_wire_accounting_contract` 及没有选择／查找用途的固定 `estimator_id`。
- 实际只装固定版本标签的 `contract_id`、`compiler_contract`、`prompt_contract`、`model_contract`、`policy_id`、分类器标记等。
- 只用于证明上述固定标签的默认值、`Literal`、非空检查、常量相等检查、组合键、固定摘要、SQL 列／CHECK／触发器分支及错误类型。

执行时沿生产者到最终消费者闭合删除，不保留空字段、占位默认值、失效属性、只为凑旧构造参数的对象，或“已删除版本”的专用证明层。

### 2.2 不能按名称删除的对象

| 对象 | 当前独立用途与处理 |
| --- | --- |
| 文件 `content_revision`、任务／工作流 `revision`、`expected_revision` | 标识真实内容状态或检测并发修改，保留。 |
| writer／attachment generation、live revision／epoch、事件与记录序号 | 参与当前所有权、连接生命周期、排序或连续性，不属于格式版本；本次不改。 |
| MIME、codec、`kind`、`type`、`wire_api` | 选择实际编码、业务变体或传输，不因固定值而自动删除。 |
| MCP 协商版本、OAuth 协议要求、外部插件 schema URL | 由外部协议拥有，保留必要处理；不删除第三方载荷里的版本键。 |
| PostgreSQL 大版本、扩展版本、WSL 版本、包／应用／SDK 发布版本 | 代表实际二进制、数据目录或外部 API 兼容事实，不是内部 DTO 标签。 |
| 内容 digest、MAC、稳定 ID 的派生域 | 按真实边界判断；不机械删除其中的 `v1` 字样。 |
| provider prefix／source lineage／replay compatibility 的实际依据 | 保留真实兼容性检查；不能以删标签为由允许拼接不兼容输入。 |

上述边界不自动豁免其附近的字段。即使标签被放进真实边界使用的 hash，标签仍须有独立用途；实际发布版本也不构成在数据库复制保存一份无人消费字段的理由。

模块名、数据库命名空间 `pulsara_v3`、协议包名、历史文档名和文件名中的 `vN` 不是待删字段。本次不开展全仓库的名称改写。资源确需随唯一当前入口更名时，不保留旧名别名。

## 3. 生产代码删除清单

路径是扫描定位入口；每项均包含其调用点、序列化和消费端，不能只删除表中这一处。

### 3.1 Cell 输出和有序图文正文

| 范围 | 删除内容 | 保留内容与主要入口 |
| --- | --- | --- |
| Cell output | `schema_version: "cell_output.v1"`、必填键、固定相等检查、去除该键的转换代码，以及前端对应拒绝分支 | `ports/terminal_observation.py`、`conversation_kernel/_repository/prompts.py`、`conversation_kernel/reader.py`、`conversation_kernel/tool_outputs.py`、`frontend/lib/runtime-adapter.ts`。保留实际 record／parts、类型、身份和顺序。 |
| Canonical prompt body | `schema: "pulsara.prompt/v2"`、`PROMPT_BODY_SCHEMA`、编码和解码的版本键检查 | `conversation_kernel/prompt_content.py`、`llm/input.py`、`conversation_kernel/prompt_storage.py`、`frontend/lib/prompt-content.ts`。保留实际 parts 编码、必要 canonical bytes 校验、媒体类型与 codec 分派。 |
| 工具 closure／late outcome | `provider_tool_result_closure.v1`、`late_tool_outcome_observation.v1` 对应的 `schema_version` | `conversation_kernel/runner.py`、`conversation_kernel/reader.py`。保留 tool_call 关联、disposition、真实结果状态和正文；上界估算同步使用新的实际正文。 |

Cell 和工具结果的图文表示、完整业务值、原生分页、JS steer 输出语义由[统一工具结果交付与分页草案](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)规定。本文只删除冗余标签，不改变其 12k 原生分页、JS 完整返回或图文输出决策，也不推进 motion 修订。

### 3.2 本机设置与定时规则

| 范围 | 删除内容 | 入口与保留行为 |
| --- | --- | --- |
| Local settings | `schema: "pulsara-local-settings:v5"`、`LOCAL_SETTINGS_SCHEMA`、版本不支持异常及只为其存在的转换提示／分支 | `settings.py`。读写双方改用同一当前结构；保留配置字段检查、凭据保护、原子写入和真实保存失败处理。 |
| Scheduled rule | `contract: "scheduled-rule:v1"`、类型／schema 中的 const、前后端生产值和解析时的固定检查 | `scheduling/contracts.py`、`scheduling/service.py`、`frontend/lib/scheduled-tasks-api.ts`、`frontend/components/scheduled-tasks-view.tsx`。保留规则 kind、时间、时区及业务合法性。 |

删除设置标签不授权重置用户配置，更不能将设置解析问题处理成空配置覆盖。后续实施若需要调整现存设置文件，只能保留实际配置值并精确处理该标签，不读取旧版业务结构、不建立版本迁移器、不输出秘密。本草案编写阶段不读写实际设置。

### 3.3 命令、Plan 与权限

| 范围 | 删除内容 | 入口与保留行为 |
| --- | --- | --- |
| Session commands | `request_schema_version`、候选对象 `schema_version` 属性、写入和比较的 `submit_prompt.v3`、`queue_prompt.v3`、`run_scheduled_task.v1`、`queued_prompt_action.v1`、`compact_context.v1`、`resolve_tool_interaction.v1`、各 Plan 命令的固定版本标签 | `conversation_kernel/steer.py` 与 `_repository/conversation.py`、`prompts.py`、`scheduling.py`、`tools.py`、`plans.py`。保留 command_kind、命令身份、真实参数、semantic digest 和事务／ACK-unknown 确认语义。 |
| Plan binding | `contract_version`／`request_contract_version`、只复述当前工具的固定 contract id／fingerprint 三元组，以及靠它识别当前工具的历史 decoder 注册表 | `primitives/plan_workflow.py`、`ports/tool_registry.py`、`capability/builtin_catalog.py`、`conversation_kernel/plan_runtime.py`、`_repository/plans.py`、`reader.py`、`model_input/contracts.py`、`terminal_protocol/canonical_v3.py`。直接按已有真实交互种类与当前工具处理，保留独立有用的实际工具定义与身份。 |
| 权限 preset | `permission_contract_id = pulsara.permission-presets.v2` 和只用于证明固定 preset 的 `permission_contract_fingerprint`，包括默认参数、等值检查与快照中的重复传递 | `primitives/permission.py`、`primitives/run_permission.py`、对应 repository 与 SQL。保留 requested／effective mode、admission source、Plan overlay、继承来源、工作流身份和实际 revision。 |

Plan 的不同工具各自标为 v1／v2，不构成同一工具多版本执行需求。删除其版本注册表后，不另建一套“当前 decoder”注册表。

`contract_id` 或 fingerprint 若只复述已知工具种类、固定版本与完整 frozen 参数，同步删除冗余部分；实际工具定义、可用性、权限、冻结输入、交互身份和响应关联仍须验证。不能删除整套权限判断来完成字段减法。

权限 snapshot 的实际语义摘要与固定 preset 标签分开处理：固定标签及固定证明删除；确有跨事务确认用途的实际权限事实摘要保留其用途，更新其输入，不能让摘要缺失被解释为权限放行。

### 3.4 模型调用、传输、错误与 replay

| 范围 | 删除内容 | 入口与保留行为 |
| --- | --- | --- |
| Resolved model DTO | `resolved-model-target:v9`、`resolved-model-call:v2` 的 `contract_version` 字段 | `primitives/model_call.py`、`llm/resolution.py`。保留冻结目标、连接、模型、wire API 和真实调用参数。 |
| Transport 固定标签 | 两种 OpenAI adapter 的固定 v8 标签、`transport_contract_version` 及其跨层复制、非空／相等检查 | `ports/provider_stream.py`、`llm/adapters/openai/{chat_completions,responses}.py`、`normalized_transport.py`、`route_wires.py`、`model_target.py`、`frozen_target.py`、`resolution.py`、`validation.py`、`request.py`。保留实际 binding、端点、wire API、冻结目标与当前执行 owner 的一致性。 |
| Provider 错误 DTO | `provider_error_sanitization_contract.v1`、`provider_retry_summary.v1`、`provider_sanitized_error.v2` 和 sanitizer 固定 v3 标签 | `primitives/model_call.py`、`llm/provider_sanitization.py`。保留实际错误分类、重试事实、脱敏规则和可观察业务事实。 |
| Replay compatibility DTO 外壳 | `compatibility_contract_version = pulsara.provider-replay-target-compatibility.v1`、固定检查／传递，以及 `_replay_target_fingerprint` 中的 `compatibility_contract` 标签成员 | `llm/provider_replay.py`。保留 codec、目标身份及真正的 replay compatibility 判断。 |
| Replay 实现宣言 | `RESPONSES_COMPLETED_OUTPUT_SOURCE_CONTRACT` 的固定 v7，hash payload 中 `public_projection=ordered_text_then_tool_calls:v2`、`canonical_array=pulsara.canonical-json.v1`、`public_order=optional_single_message_before_function_calls:v1`、`completed_output_source` 成员 | `llm/provider_replay.py`。删除常量、成员、导出，不改成无版本宣言；保留实际字段／item／排除字段集合、排序与完成事件合并行为。 |
| Native function-tool 版本证明 | `OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION`、仅 hash `wire_api + 固定版本` 的 `openai_native_function_tool_contract_fingerprint()`，及整条 `native_function_tool_wire_contract_fingerprint` 透传、比较和 aggregate 成员 | `llm/adapters/openai/function_tools.py`、`capability/{contracts,planner,registry}.py`、`llm/frozen_target.py`、`conversation_kernel/direct_model.py`。保留实际 wire API、冻结 cohort、工具内容及物化校验。 |
| Replay contract 失效透传 | `RouteWireContract.assistant_replay_contract`、`FrozenProviderProjectionStrategy.assistant_replay_contract`，以及 route 中只为其预先计算、复制的摘要与非空检查 | `llm/model_target.py`、`llm/route_wires.py`、`llm/frozen_target.py`、`conversation_kernel/direct_model.py`。必要 replay 判断仍由现有实际 codec／API 边界负责。 |

真实 replay contract 摘要只保留实际规则值及必要的 target／manifest／public projection 匹配，不保留仅声明当前实现版本的成员，不新增规则 descriptor 替代标签。

Chat 与 Responses 的实际 wire projection 仍不能混用。planner 当前输入没有冻结 target／`wire_api`，不为此增加字段。在原复用判断处使用已有证据：当前 eligibility quote 的 `wire_tool_fingerprint`／字节数，与 predecessor 完整冻结 `wire_tool` 按现有实际 wire 摘要算法比较；仅全部 installed 工具的实际 wire 一致时复用，否则沿现有物化路径重新投影同一 cohort。已有 `retained_direct_inputs` 保留安装时 schema，late tool 不得进入已安装 surface。完整 wire 值、实际字节数、wire digest 和 token quote 的物化一致性校验继续保留。

### 3.5 工具语义、rollup 与分类器

删除以下固定描述层：

- `tool-result-semantics-builder-contract:v1` 及其 `schema_version`。
- `builder_version`、`semantics_builder_version`、`normalization_contract_versions`。
- 只装固定 `schema:*:vN` 字符串的输入／输出 schema 标签及其数量、非空、匹配证明。
- `observation_rollup_renderer_contract.v1`、`renderer_version`、`rollup_renderer_version` 与固定 rollup schema 标签。
- `pulsara.tool-result-full-delivery-classifier.v2` 的固定分类器标签和检查。
- 无真实消费者的 `TOOL_RESULT_LOGICAL_PROJECTION_CONTRACT`、`CANONICAL_TOOL_RESULT_PREVIEW_CONTRACT` 等标签常量。

入口：`primitives/tool_result.py`、`primitives/long_horizon.py`、`primitives/tool_result_projection.py`、`capability/result_contracts.py`、`conversation_kernel/tool_outputs.py`、`model_input/contracts.py`。

不同 builder 分别使用版本 2／2／3，不意味着同一 builder 有多版本读取。业务分支仍按真实工具／结果种类处理。保留实际允许状态、requirement／reason、结果规范化和 rollup 行为；如果某个对象仅剩固定标签与固定标签的摘要，删除整个对象及其工厂，不留下空壳。

### 3.6 Context source、compiler 与 compaction

| 范围 | 删除内容 | 入口与保留行为 |
| --- | --- | --- |
| Source／observation | `contract_version`、`implementation_contract_version`、`source_contract_version`，以及当前 policy 版本串的复制和 drift 检查 | `conversation_kernel/context_sources.py`、`model_input/{contracts,compiler,lowering,continuity}.py`、`conversation_kernel/memory/dispatch.py`。保留 source kind、channel、trust、presence／absence、正文、实际放置与生命周期规则。 |
| Compiler 报告 | `COMPILER_CONTRACT_VERSION`、`compiler_contract_version` 及报告／诊断透传 | `model_input/compiler.py`、`contracts.py`、`diagnostics.py`。保留真实编译结果和有用诊断。 |
| Compaction snapshot | `compiler_contract`、`prompt_contract`、`model_contract` 三个固定标签及 DTO、写入、比较、fork 复制 | `conversation_kernel/compaction/{contracts,coordinator}.py`、`_repository/conversation.py`、`_repository/fork.py`。保留摘要正文、retained entries、来源范围、lineage、codec、真实摘要与合法 successor 采用。 |
| Provider message lowering | `PROVIDER_MESSAGE_LOWERING_CONTRACT` 的固定 v12，以及 FULL_HISTORY／snapshot context-base hash payload 中的 `lowering` 标签成员 | `model_input/continuity.py`、`conversation_kernel/reader.py`、`conversation_kernel/compaction/planner.py`。删除常量、导入／导出和重复标签成员；保留实际 snapshot ID、内容摘要、来源范围与 prefix 判断。 |

仅因标签进入 aggregate fingerprint，不构成保留理由；删除标签及仅证明它的摘要层。真实 provider prefix、source lineage 与 stable identity 的边界按第 6 节处理。

在当前 epoch 内，仍要求 SYSTEM／tools 字节一致、messages 仅追加后缀。本次 hardcut 使用当前契约的新会话，不通过重写存量 prefix 适配新编码。

### 3.7 Embedding、查询、Skills 与其他固定标记

| 范围 | 删除内容 | 入口与保留行为 |
| --- | --- | --- |
| Embedding | `EmbeddingSemanticContract.contract_version = 1`、SQL `embedding_contract_version`、过滤／写入／更新参数 | `retrieval/config.py`、`conversation_kernel/_repository/memory.py`、`conversation_kernel/memory/recall.py`。保留由真实 endpoint、model、shape、dimensions 和文本处理语义得到的 contract_id，防止不兼容向量混用。 |
| Memory hint／dense policy | 只作标签的固定 `contract_id`／`policy_id` | `conversation_kernel/memory/hints.py`、`retrieval/config.py`。保留 opt-out 识别、规则和实际阈值。 |
| 查询诊断 | `inspect_kind.v3`、`stage2_runtime_limits.v1`、`kernel_execution_watchdogs.v1`、`stage2_structural_budgets.v1` 等固定标签 | `conversation_kernel/query.py`。保留查询类别和实际诊断数据，不以删标签为由改动预算或 watchdog 语义。 |
| 会话搜索 cursor | `search_session_content.bm25.v3`、operation 后拼接的 `.v4` 这类格式版本后缀 | `conversation_kernel/session_content.py`。保留真实 operation 区分、位置、范围及 token 真实性检查，不把不同用途 cursor 混用。 |
| Skills | 无独立用途的固定 core／placement 标签 | `capability/local_skills.py`。保留真实 frontmatter、内容与放置规则；不删除外部 manifest 自己的发布版本。 |
| Plugin 本地实例状态 | `PLUGIN_INSTANCE_STATE_CONTRACT_ID`、`PluginInstanceState.contract_id`、本地 state JSON 的 `contract_id=pulsara.plugin-instance-state.v1`、必填键与固定比较 | `plugins/contracts.py`、`plugins/package_store.py`。保留 enabled 类型、实际 plugin／scope／package／overlay／路径身份与原子保存；不重置启用状态或连接配置。外部插件 schema URL 不属于这项内部标签。 |
| Windows 自有 helper | `pulsara-interop 1` 固定版本输出及构建／运行时 stdout 精确相等检查 | `desktop/native/windows-interop-helper/Program.cs`、`desktop/scripts/prepare_windows_resources.py`、`desktop/src/wsl-setup.ts`。保留既有无副作用 `--version` 分支成功退出、构建端 `check=True`、启动端 `execute()` 及退出状态检查，继续在原时点发现不可加载／不可运行的 helper；不新增命令、字段或替代探测。参数名不属于本次字段清理。 |

### 3.8 自有 terminal 的格式身份握手

`terminal_protocol/schema/terminal_kernel_v3.proto` 的 `TerminalKernelBootstrapCarrier.carrier_version` 未发现手写生产读写消费者，列入删除；当前生成产物从唯一 schema 重新生成，不手工维护两套定义。

当前生产 client 直接接收本应用创建的 server 对象，从同一 gateway 导入 `protocol_identity()`；没有独立发布客户端或多版本协商路径。`schema_fingerprint` 虽由实际 `.proto` 字节产生，并有现存工具验证，但在该握手中没有独立用途，不能只删 major／minor 再保留一套格式身份声明。

整体删除：

- `ProtocolIdentity` message，包括 `major`、`minor`、`schema_fingerprint`。
- `HelloRequest.protocol`、`HelloAccepted.protocol` 的生产、交换和检查。
- `protocol_identity()`、`PROTOCOL_MAJOR`、`PROTOCOL_MINOR`、`PROTOCOL_SCHEMA_FINGERPRINT` 及失去用途的导入／导出。
- 格式身份不一致分支及 `PROTOCOL_V4_REQUIRED`；不新增另一种版本／schema 不匹配错误替代。
- `tools/generate_terminal_protocol_contract.py` 中要求 gateway 包含源码 SHA 的检查，以及 wire fixture 的顶层 `schema_fingerprint` 和专属一致性检查。

保留现存 protoc 生成产物一致性验证、fixture 中真实 snapshot 内容摘要校验、launch capability、角色、实体身份、帧解析、连接生命周期和实际资源边界。只移除格式身份子步骤，不取消 hello 中实际认证与 attachment 建立行为，不改变 wire 业务消息的含义。

入口：`terminal_protocol/v3_gateway.py`、唯一 proto 及生成产物、`desktop_app/protocol_client.py`、`desktop_app/application.py`、`tools/generate_terminal_protocol_contract.py` 和当前 wire fixture。更新唯一当前定义，不新增探测、协商矩阵或旧客户端兼容入口。

### 3.9 Token 估计器的固定版本与合同标签

删除 `TokenEstimatorFact.estimator_version`、固定 `estimator_id="pulsara_heuristic"`、固定 `image_wire_accounting_contract="formal_user_image_parts:v1-payload-elided-per-item-rounding"`；同时删除字段约束、生产参数、hash payload 中对应版本／身份／图片计费宣言及专属测试。没有按这些标签选择实现的消费者。

入口：`primitives/model_call.py`、`llm/estimator.py`、`llm/resolution.py`、`llm/frozen_target.py`、`model_input/contracts.py` 及 compile binding／quote／steer／compaction 中的相关依赖。标签不通过 `estimator_fingerprint` 换一种形式继续传递；完整事实已在场的重复证明按第 6 节减去，不新增估计器注册表、策略 ID 或兼容层。

算法和产品预算口径保持当前实现：

- CJK／宽标点计数、其他文本 UTF-8/4、逐项向上取整。
- 图片 28 像素网格、7/8 缩放、最低 256 tokens，以及图片 payload 不被当作文本重复计数。
- 消息封装计费、图文语义源对齐、最终 wire 固定上下文与有序项的估算。
- 当前冻结实现与实际参数、同一估计口径的报价关联、usage anchor 的真实 target／wire 一致性，以及 embedding 现有 token admission。

`estimator_fingerprint` 含实际参数和规则，不因字段名而整条机械删除；其中内部标签和仅证明标签的重复成员必须去掉。`budget_source` 的 heuristic／reported anchor 区别是真实预算来源，保留。`unicodedata.unidata_version` 是实际依赖数据事实，不是 Pulsara 自造格式标签。

未发现上述估计器标签的生产入库路径，不新增 SQL 列或持久缓存适配。类名 `PulsaraHeuristicTokenEstimatorV3` 是名称，本次不要求更名；文档中的“v3 估计器”指当前实际算法，不构成保留运行时版本字段的理由。12k 原生分页预算不变，不开展阈值实验。

## 4. 数据库 hardcut

### 4.1 业务表与 JSONB

直接修改 `src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql` 及当前 repository／DTO／reader，删除：

| 表／载荷 | 删除项 |
| --- | --- |
| `session_commands` | `request_schema_version` 列及所有查询、插入、比较。 |
| Plan interaction | `request_contract_id`／`request_contract_version`／`request_contract_fingerprint` 的固定三元组、对应列、kind 唯一决定的固定 CHECK、immutability trigger 中对应比较。保留实际交互 kind、参数与身份，不留下 id＋固定 hash 作为版本替身。 |
| `context_snapshots` | `compiler_contract`、`prompt_contract`、`model_contract` 三列及 fork／候选确认中的透传与比较。 |
| turns、prompt queue、plan workflows 的权限载荷 | 固定 `permission_contract_id` 与固定 `permission_contract_fingerprint`；关联 nullability 组合和 immutable 比较同步删除对应成员。 |
| Memory embedding | `embedding_contract_version` 列、CHECK、查询过滤及写入更新参数。 |
| `scheduled_tasks.schedule` JSONB | 生产和解析中的固定 `contract` 成员；真实规则字段继续保存和校验。 |
| Cell、prompt、model、错误等已保存正文／JSONB | 第 3 节列出的标签从唯一当前写入结构和读取结构一起删除。 |

SQL 触发器内某个标签比较被删除，不等于删除整个触发器；其中仍约束真实不可变业务事实的分支保留。外键、唯一性、状态与权限约束不在删除范围。

### 4.2 仅有 clean baseline 的版本外壳

当前 `storage/migrations` 只注册 version 0 baseline，不存在逐版升级执行需求。清理以下自造版本层：

- `UNIVERSE_ID` 中固定内部版本身份、`UNIVERSE_GENERATION = 1`、`BASELINE_VERSION = 0`、固定 head version。
- `PostgresMigrationLedgerRowFact`、registry、verified binding 中对应字段、比较、复制和只为这些值存在的注册层。
- `PostgresMigrationReport.previous_head_version`、`migration_head_version`、`applied_versions`，以及 report、CLI、HTTP、desktop diagnostics 中对应 universe／head／prefix 输出和专属比较。
- `build_postgres_schema_manifest(_version=0)`、grants 的 `_through_version=0` 等不参与实际行为的版本参数，以及调用点为了传递该值而存在的分支。
- `public.pulsara_schema_migrations` 中 `universe_id`、`universe_generation`、`version` 及其固定检查／版本主键用途。
- ledger 的 `application_version` 列、DTO 字段、runner 注入参数／实例字段、写入与读回链及专用 `__version__` import。当前只有持久化再读回，没有展示、分派或验证消费者；不为潜在诊断用途保留副本。
- baseline／universe hash payload 中仅声明自身格式的 `schema_version`、固定 version／generation；只为重复包装这些常量存在的聚合证明同步减去。
- `universe_fingerprint`、`genesis_registry_prefix_fingerprint`、`registry_fingerprint`、`registry_prefix_fingerprint`、`durable_registry_prefix_fingerprint` 等对相同单 baseline 信息的重复包装；ledger 中对应 universe／prefix 证明列及重复的 `migration_contract_fingerprint`，以及 `baseline_contract_fingerprint` 等重复聚合。
- verified binding／verification result 中仅重复证明完整事实的 `verification_contract_fingerprint`、`binding_fingerprint` 及 result 聚合摘要层。没有 registry 历史，不保留 prefix 外壳；实际资源完整性／当前安装事实不因删除这些重复包装而消失。
- `schema_verification_service.py` 进程内 verification cache key 中对应的 registry fingerprint 成员；保留现有实际连接目标和有效性检查，不新增替代版本键。
- packaged expected catalog、runtime grants JSON 的 `schema_version`；从真实内容重新产生必要的当前资源／schema 验证数据。

入口：`storage/migrations/{contracts,registry,runner,verifier,manifest,grants}.py`、`storage/schema_contract.py`、`storage/schema_verification_service.py`、`storage/postgres_connection_provider.py`、`desktop_runtime.py`、`desktop_app/http_server.py`、`cli.py`、baseline SQL 和 `storage/migrations/resources/`。

最终只保留当前 baseline 已正确安装的必要持久事实，以及对**实际数据库结构、资源、权限、连接目标**的检查。真实资源校验与跨重启安装确认仍是实际边界；不能因删版本字段把未知数据库直接视作合格，也不能把旧版本注册表换成新 fingerprint 注册表。

删除版本主键后，不为凑表结构新增一个“版本对象 ID”、generation、字段或通用注册表。沿现有 baseline 安装／验证 owner 直接收敛必要记录与唯一性；不增加第二份安装回执或恢复任务。包本身的真实发布版本、实际资源定位与外部依赖兼容判断保留，不以此保留无人消费的 ledger 副本。诊断继续报告真实安装／验证结果和实际数据库、runtime role，不保留恒定 0 的 head 或 applied 集合。

### 4.3 开发状态处理

使用新的 clean baseline，不建立 ALTER 迁移链来保留旧开发会话。后续实施需要重建数据库时，只能在确认目标确为本机可丢弃开发库后执行；不得影响未核实或远端数据库。本文编写不连接或重置数据库。

## 5. 测试与测试支持代码也一起做减法

### 5.1 直接删除，不另写替代测试

以下测试没有独立业务意义，直接删除相应用例；文件若只剩这些用例和专用 helper，文件一起删除：

- 正确版本常量可构造、缺少版本不可构造、错误／未知版本被拒绝。
- 默认版本值、版本字段 round-trip、版本字符串出现在快照中。
- 同一固定版本在 DTO、前端、SQL、注册表中一致。
- 固定版本／固定 fingerprint 三元组能选择所谓历史 decoder。
- 为将来内部版本升级保留的兼容矩阵、旧会话／旧内部正文读取。
- 仅检查版本号 bump、固定标签 hash 或标记集合数量的用例。
- 只验证估计器固定 id／v3／图片合同标签、lowering 固定实现版本、native contract hash、terminal 格式身份握手、数据库 universe／head／prefix／application_version 副本的用例。
- Plugin 本地实例状态固定合同标签的 round-trip／错误版本拒绝用例，以及 Hook trust v1／v2→v3 的版本升级失效用例。

不将它们改写成一批 `assert "schema_version" not in ...`、扫描源码禁止出现 version 单词、或证明“新无版本包装正确”的测试。删除没有业务意义的校验，也删除只服务它的测试。

只服务标签的 golden 字段、构造 helper、反查 hash 猜版本／API 的 helper 和 mock 属性一起删除。生成产物一致性、实际业务正文和内容摘要用的 fixture 不因同文件含标签而整份删除。

### 5.2 混合用例保留有意义的行为

混合测试移除废弃构造参数、fixture 字段、版本断言及专用 mock 属性，保留其中实际在验证的行为。不能为了删标签而削弱权限、幂等、编辑冲突、图文顺序、fork 内容、跨事务确认、数据库漂移或 provider continuity 断言。

当前明确会受影响的入口包括：

- `tests/test_agent_session_host.py` 和 `tests/frontend/runtime-adapter.test.ts` 的 Cell 编码／解析 fixture。
- `tests/test_round4_plan_workflow.py`、`test_round4_plan_postgres.py`、`test_stage2_conversation_runner.py`、`tests/support/round3.py` 的 Plan binding 构造与版本断言。
- `tests/test_round3_structured_model_input_compiler.py`、`test_round3_1_provider_input_prefix_continuity.py`、`test_runtime_handoff_pca.py` 等 source／compiler fixture。
- compaction、fork、canonical reader、request start 相关测试里的三项 compaction 标签。
- `tests/test_session_permanent_deletion.py`、`test_direct_advisory_memory.py` 的 embedding SQL fixture。
- scheduled tasks、模型 transport mock、数据库 baseline／catalog／grants 的当前结构 fixture。
- `tests/test_llm_text_estimator.py` 的固定 v3 断言直接删除；保留实际 Unicode、JSON、framing 与 token 数值断言。
- `tests/test_kernel_image_input_k3.py` 中固定 estimator id／version／图片合同断言删除；保留实际参数、图片计费、图文源与 payload 漂移验证。compaction 的 fake estimator fixture 去掉版本属性，保留真实不同实现／事实不得混用报价的用例。
- `tests/test_round9_unified_capability_semantics.py` 中反查 contract hash 猜 wire API 的 helper 和 hash 不等断言删除；保留真实 Chat→Responses 切换、同一 installed cohort 重新投影、late tool 不进入 native surface 的行为验证。`tests/support/model_config.py`、`tests/test_stage2_kernel_composition.py` 中对应透传一起删除。
- terminal 协议测试与 `tests/fixtures/stage2_protocol_v3_wire.json` 删除格式身份、固定版本／schema fingerprint 断言和只服务它们的 fixture 字段；保留真实认证、角色、帧处理及 snapshot 内容摘要验证。
- `tests/frontend/history-paging-review.test.ts`、`history-continuity.test.ts`、`runtime-adapter.test.ts` 的 prompt schema fixture 去掉标签；保留实际历史分页连续性和图文 owner 关联验证。
- 数据库 report、verification cache、CLI／HTTP／desktop diagnostics 的专属版本断言、固定 proof fixture 和已删除参数一起移除；保留实际安装、schema 漂移、权限及连接目标验证。
- Plugin state fixture 删除内部 contract_id；保留启用状态、作用域、当前安装包、MCP overlay、路径归属和原子读写测试。Windows helper fixture 删除固定 stdout 标签期待，保留进程加载／退出失败在原检查点被报告的行为验证。
- `tests/test_kernel_image_input_k1.py` 等精确正文测试保留重复键、未知字段和非 canonical JSON 拒绝；重复 schema 键 fixture 改用当前真实字段的重复键，不能删除整个结构校验用例。
- `tests/test_round9_2_hook_subsystem.py` 删除只验证旧版本 digest 自动失效的升级用例；保留实际定义内容、来源／环境、用户授权和修改后变为 MODIFIED 的行为验证。

这是受影响文件定位，不是整文件删除名单。例如某个测试验证外部工具正文里的 `{"schema_version":"external.api.v7"}` 被原样保留，它验证的是**外部正文不被改写**，仍有独立意义，应保留。工具返回的任意业务数据不归本次内部标签清理所有。

`tests/test_responses_completed_field_union.py` 等用例验证真实完成事件合并，应保留；其中 provider 返回正文的 `format: "openai-responses-v1"` 不属于内部标签，不改写。durable replay 相关测试去掉内部标签依赖，继续验证 endpoint／model／binding／API 隔离、payload exactness、opaque reasoning 和 public projection 一致性。

活跃 dogfood／fixture 若依赖已删除生产字段，去掉这些依赖；不伪造运行结果。历史报告、归档规格和无关实验产物不做批量重写；不为清零文本搜索结果删除历史证据。

## 6. 摘要、稳定身份和真实兼容性

遵循已激活的 [Fingerprint 减法契约](archived_docs/PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。

- 标签只被另一层 hash 使用，不是保留标签的理由。完整值已传递时，不再叠加版本证明或 DTO fingerprint。
- 字段位于 prefix／replay／跨进程／数据库边界，不等于字段有独立用途；不以“兼容性”统称保留固定实现宣言及其 hash 包装。只删这些冗余结构，不修改边界实际保证的产品行为。
- 真正用于 durable semantic confirmation、内容完整性、MAC、provider prefix／source／replay 或 canonical stable identity 的摘要，按其独立用途处理。
- 如果已公开的稳定 ID 或必须保持的错误身份曾将固定标签作为输入，在其**唯一 ID／摘要构造点**局部保留原派生常量以保持必要输出；不继续保存、接收、校验或跨层传递版本字段，也不新增旧／新 ID 双轨。
- Hook trust 的 `pulsara.hook-definition-trust.v3` 属于该真实边界：只在 `hooks/trust.py` 的唯一 `normalized_definition_digest()` 构造点参与实际定义摘要，用户确认后持久保存的是定义 digest，并在后续读取时决定 TRUSTED／MODIFIED。保留该局部派生常量以免未变的已授权 Hook 失效；不新增或保存独立版本字段。测试 golden、普通 DTO hash 和进程内 compile binding 不构成同等保留理由。
- `identity.py` 的 `pulsara-id-v1` 等真实派生域不改。数据库当前资源发生 hardcut 后所需的真实资源摘要按当前资源产生，不保留旧数据库兼容。
- 不为删标签重新设计 provider epoch、replay、权限 owner 或数据库验证 owner；实际兼容性判断必须在原本拥有它的边界完成。

## 7. 实施与验收边界

实施时按同一 hardcut 更新生产者、消费者、数据库 clean baseline、必要生成产物和当前测试支持代码，删除失去用途的 import、export、异常、工厂、注册项及文档说明。不使用 feature flag 或过渡版本。

验收依据是保留的真实行为仍成立，以及列入删除的字段链路已完整消失，不是新增“版本删除框架”。使用现有有意义的测试验证配置读写、业务解析、图文保存／读取、Plan 与权限、命令幂等、compaction／fork、实际数据库 schema／权限验证、embedding 隔离和 provider prefix／replay。必要生成产物由当前工具生成；测试运行范围按实际改动决定。

实施范围仅为本文定义的内部标签与重复证明减法；另外两份根目录产品草案随文档基线保存，不据此实施统一分页或 motion 修订。测试与必要的已核实本机 disposable 数据库重建按仓库规则执行。不得通过跳过失败、降低业务断言或重写 dogfood 证据获得通过。
