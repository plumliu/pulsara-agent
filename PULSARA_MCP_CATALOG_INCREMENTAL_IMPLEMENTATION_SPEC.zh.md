# Pulsara MCP 初始服务目录增量实施规范

状态：待实施。本轮只编写实施文档，不修改代码、生产配置或数据库，不构建、安装或操作运行中的 Pulsara。

本文件补充 [MCP 统一 Meta 架构](PULSARA_MCP_META_ONLY_CALLING_ARCHITECTURE_DRAFT.zh.md)和 [Runtime Observation 修订草案](PULSARA_RUNTIME_OBSERVATION_REVISION_DRAFT.zh.md)：新上下文提供一份轻量的已启用 MCP 服务目录，后续继续发送既定净变化。涉及初始服务目录的口径以本文件为准；查询、调用引用、权限、工具结果和变化检测沿现有合同。

## 1. 目标与完成范围

让模型在开始处理任务前知道当前已经启用了哪些 MCP 服务，遇到联网搜索、网页读取或外部服务需求时，能够主动发现合适工具。

完整路径是：

```text
新上下文中的服务名称清单
  → 按任务关键词 query_capabilities(kind:"MCP", query:...)
  → 模型收到匹配工具的接口说明和调用引用
  → 原生 call_mcp_tool，或在已了解接口后编写业务 Cell
```

搜索命中的工具详情已有完整 input_schema 和可用时的 invocation.target，不要求先列服务、再列工具、再逐个精确查询。仅命中服务时，沿现有连接 Target 查询该服务工具。模型已有足够说明和有效引用时可以直接调用。

本次完成初始目录、上下文位置、比较快照衔接、发现引导和验收。保留 MCP meta-only：远端工具不进入 provider tools 数组，也不成为 Cell 的独立方法。

## 2. 模型看到什么

### 2.1 正常目录

初始目录使用纯文本，正文包含 server_id 列表和一段固定查询指引：

```text
当前已启用的 MCP 服务：
- firecrawl
- github

需要外部能力时，可通过 query_capabilities 的 MCP 分支，按任务关键词搜索工具，或查询服务列表。使用工具前，取得相应接口说明和调用引用。
```

“已启用”描述当前有效配置，不表示连接已经成功、工具目录已经发现或每个工具都可以执行。具体情况由查询和调用结果说明。

目录不显示 Target、调用引用、工具名称、工具数量、资源数量、schema、完整 instructions、端点、命令、凭据、配置路径或内部生命周期字段。实际定位及调用信息通过现有查询取得。

没有工具用途简介、自动摘要或额外推理请求；首版只显示服务标识。固定查询指引不硬编码 Firecrawl 等具体服务，也不教模型根据服务名猜测工具参数。

### 2.2 Plugin 来源

沿既有通知的轻量来源说明，在名称后补充插件名称：

```text
当前已启用的 MCP 服务：
- firecrawl
- docs（来自插件 knowledge-tools）

需要外部能力时，可通过 query_capabilities 的 MCP 分支，按任务关键词搜索工具，或查询服务列表。使用工具前，取得相应接口说明和调用引用。
```

服务名称使用现有连接 Target 中的公开 server_id。Plugin MCP 使用包内的本地服务键，不直接展示 supervisor 为聚合生成的 framed server_id；公开键已经由既有 Target 投影 owner 解析，无需新增拆解逻辑。插件名称沿当前通知已有的来源信息取得，不为目录额外读取插件包或建立名称注册表。

相同公开 server_id 可能属于不同连接。保留实际不同连接的条目；需要明确身份时模型查询 Target，不把这里的名称当作唯一定位参数。不因纯文本名称相同而合并不同连接。

### 2.3 空与失败

| 实际观察 | 初始目录正文 |
| --- | --- |
| 成功观察，当前作用域没有已启用服务 | `当前没有已启用的 MCP 服务。` |
| 服务配置已启用，连接中、目录尚未发现或连接失败 | 仍列服务名称，不把单个服务未就绪当作服务目录读取失败 |
| 无法可靠取得当前服务清单 | `当前无法获取 MCP 服务目录；这不表示没有已安装的服务。需要时可通过 query_capabilities 的 MCP 分支查询。` |

空清单只能来自成功观察的当前有效配置。不能把缺失的 owner、读取异常或未知状态转换为空集。

不为这份提示新增通用错误恢复机制。若既有必需能力冻结、注册或输入准备已经失败，继续沿原错误路径结束准备，不能为了显示“目录不可用”吞掉该失败；上述失败正文适用于既有上下文准备仍能继续、但清单无法可靠取得的情况。

## 3. 条目来源与筛选

### 3.1 复用的所有权边界

| 参与方 | 负责什么 |
| --- | --- |
| 配置及 Plugin owner | 当前有效安装、启停、归属、合并与使用范围 |
| MCP supervisor | 现有发现、连接、刷新，以及同次锁内冻结的目录和工具事实 |
| context source collector | 从冻结事实生成名称清单，并生成相应初始比较状态 |
| model input compiler | 将两份初始能力目录放到合法的新上下文位置，完成真实输入计量 |
| provider input continuity owner | 成功安装候选后接受消息及该候选携带的比较状态 |
| 能力查询、引用及调用 owner | 后续实际接口、Target、引用、权限和执行结算 |

当前 `PreparedMcpCapabilitySourceSnapshotSet` 已携带 `catalog_snapshot` 和 `inspection_inputs`。生产路径由 supervisor 在同一次锁内冻结它们，本次直接复用，不增加目录采集器、持久索引、通知队列或另一套服务状态。

### 3.2 具体筛选

从本次冻结的、对应当前 conversation scope 的服务目录中选择：

1. 有有效配置声明，且 `configuration.enabled` 为 true。
2. 既有公开连接 Target 可定位；HOST_OVERRIDE 保持内部入口，不进入产品目录。
3. 服务已通过现有 ROOT／Subagent 范围筛选。Subagent 不列 ROOT_ONLY 连接，不从 ROOT 的文本目录复制后自行过滤。

以配置中的 enabled 判断启用状态，不用 READY、可用工具数量或是否取得远端 schema 代替。连接未就绪仍可有已知的有效配置；已禁用服务不进入初始已启用列表，但继续保留在现有相关状态比较所需的内部快照中。

目录使用 owner 已解析的当前有效来源，不单独扫描用户目录、项目目录或 Plugin 文件来补齐，不修改既有来源优先级和同名配置解析。

按公开 server_id 稳定排序；同名时沿既有来源身份稳定排列。对重复的同一实际连接只保留一项。用于内部排序或区分的身份不进入公开正文，也不新分配目录条目 ID。

## 4. 发送时机和上下文位置

### 4.1 发送表

| 场景 | 完整初始 MCP 目录 | 后续变化通知 |
| --- | --- | --- |
| 新 session 的冷上下文首次安装 | 一次 | 不把当前全部服务伪称为刚新增 |
| 同一 epoch 的普通工具续跑 | 不发送 | 沿现有规则，不因续跑本身发送 |
| 同一 epoch 的普通新 turn，包括真人、定时任务、Agent 或 Plan 自动续接 | 不重复 | 与上次已告知快照比较，有相关净变化才发送 |
| 同一 turn 本次采用的用户主动 steer | 不重复 | 有相关净变化才发送 |
| 同一 turn 的 Cell／Agent 输出 | 不发送 | 不因该输入本身发送 |
| 活动 turn 压缩并明确采用 successor | 在新上下文前部重建一次 | 不补报压缩前尚未通知的变化历史 |
| 已结束 turn 的空闲压缩 | 新上下文实际建立时提供目录 | 不提前发送活动 turn 的恢复说明，也不唤醒模型 |
| 上下文预览、编译测量、未采用的压缩候选 | 可在该投影中展示候选目录 | 不更新已安装上下文的比较状态 |

冷启动以既有 continuity owner 判定的实际新 cold epoch 为准。进程内尚无已安装 epoch、需要按现有规则打开冷上下文时也适用；不额外设计跨重启目录恢复。

能力新增、重连、启停、权限变更或显示需求不产生新的 rebase 边界。普通新 turn 与新 epoch 是不同边界，不能因 turn_id 变化重发完整目录。

### 4.2 排列

两份初始目录均属于 `CONTEXT_MESSAGE`，按现有 Skill 初始目录方式投影为纯文本消息。新增 `ContextSourceKind.MCP_CATALOG`，仅用于这份初始服务说明；`MCP_CATALOG_CHANGES` 继续使用统一 runtime observation 外壳及 text／可选 target 的 body 数组。

普通冷上下文：

```text
system：固定系统提示词
tools：固定 Pulsara 原生工具定义

messages：
  当前完整 Skill 目录
  当前已启用 MCP 服务目录
  既有历史／用户输入及其适用的 runtime observation
```

活动 turn 压缩后的新上下文：

```text
messages：
  当前完整 Skill 目录
  当前已启用 MCP 服务目录
  模型生成的压缩交接手册
  TODO／PCA Runtime 交接
  必须优先用 read_file 重读的 Skill 清单
  TURN_CONTEXT 及适用的 Plan／前轮中断 observation
  保留的近期原始消息与必要完整工具交互
  尚待处理的新输入及其适用的附加说明
```

空闲压缩不提前附加 TODO／PCA、当前 turn 的权限、规划状态或获批执行要求；两份初始能力目录按其自身规则建立。

MCP 初始目录不放入 TODO／PCA，也不扩充 runtime observation 已确定的三种发送入口。目录正文不再套 `pulsara_runtime_observation`、presence 或 type/value。

同一 epoch 的 SYSTEM、provider tools 保持不变，messages 只追加。两份完整目录只在已允许的新上下文根建立；变化通知追加到后续输入，不重写开头目录。

## 5. 初始目录与净变化共用一次冻结

### 5.1 成功初始化

每个新上下文候选使用一次 MCP owner 冻结：

```text
冻结当前作用域的 MCP owner 快照 S
  ├─ 从 S 选择已启用服务，生成初始目录文本
  └─ 从 S 生成初始内部比较快照

编译、计量、准备候选
  → 既有 provider 输入安装成功
  → 接受该候选的消息及比较快照
```

内部比较状态继续使用 `InstalledObservationState.mcp_catalog` 和既有 `InstalledMcpObservationSnapshot`，包括当前相关服务状态、工具定义和策略。公开名称清单只是该快照的轻量投影，不能用它替代完整比较事实；内部保存完整事实也不表示模型已读过工具接口。

新上下文从 S 初始化，不带入已退出上下文的未发送 diff。初始清单已经列出 firecrawl 时，不同时追加“firecrawl 刚新增”的通知。成功观察的空目录也必须接受其比较快照，避免下次把仍为空误作读取失败。

候选准备、预览、队列采用、编译成功和 provider 回复都不是独立的快照更新点。沿现有输入安装成功边界接受候选中已经冻结的状态，不在安装时重新读取当前目录。

### 5.2 后续更新

每个新 turn 的首次模型输入或本次采用的用户主动 steer，按既定规则临时计算最新净 diff：

```text
上次安装成功的比较快照 S
  + 本次冻结的最新相关事实 T
  → 临时 diff(S,T)
  → 有相关变化时生成 MCP_CATALOG_CHANGES
  → 输入安装成功后，用本次生成通知所依据的快照替换 S
```

无相关净变化不发通知。A→B→A 不报告中间操作。消息准备后又发生的变化留待下次合格入口比较，不让安装时的新读取覆盖实际已汇报快照。

同次准备新目录和新用户输入时，初始目录负责建立比较点，不重复发送针对该初始状态的全量“新增”变化。普通同 epoch 的新 turn 则必须先比较，不能提前初始化成当前状态而吞掉变化。

### 5.3 初次清单失败后的恢复

新上下文只能提供“目录暂不可用”时，将该上下文的 MCP 比较起点保留为未知；不沿用退出上下文的旧状态，也不记成空集。

在下一个合格通知入口首次成功观察时：

- 通过 `MCP_CATALOG_CHANGES` 追加“服务目录已恢复；当前已启用服务为……”及当前名称清单。
- 成功观察但没有已启用服务时，明确说明当前没有已启用服务。
- 不将这些既有服务称为新安装或新启用，不回写初始目录位置。
- 输入安装成功后接受本次完整比较快照，之后恢复正常净变化规则。

直接用既有比较状态的未知值区分这一分支，不新增初始化登记表、永久重试任务或通知回执。当前 `_mcp_changes(None, current)` 会将条目视为新增，实施时须在未知比较起点的恢复分支中先生成恢复说明，不能机械沿用该文案。

### 5.4 时序例子

| 时序 | 结果 |
| --- | --- |
| 准备目录时 firecrawl 开启，安装前被关闭 | 本次目录及接受的比较点仍对应开启；下次合格入口报告关闭 |
| 压缩前未通知新增服务，压缩后目录已包含它 | 使用新完整目录建立比较点，不再补报压缩前新增 |
| 压缩候选包含新服务，但候选未采用 | 旧上下文及其比较快照保持不变；之后仍按旧比较点计算 |
| 初次有服务名称，远端工具目录尚未知，后来发现成功 | 沿现有状态及工具事实规则通知已确认的相关变化；不重新发送初始目录 |
| 查询或调用已经发现服务不可用，尚未到通知入口 | 工具结果立即说明实际情况；调用准入不等待 observation |

## 6. 生产接线位置

以下接点按本轮读取的当前代码列出；实施前按函数定位，不依赖行号保持不变。

| 文件／接点 | 当前事实 | 实施动作 |
| --- | --- | --- |
| [MCP supervisor](src/pulsara_agent/conversation_kernel/mcp/supervisor.py)，`freeze_capability_source_snapshot_set`、`_catalog_from_candidates_locked` | 同一次锁内冻结配置目录及工具事实；目录包含配置声明和公开连接 Target | 直接复用。通常不需修改发现、连接或 supervisor；确认 scope、enabled 和公开连接筛选 |
| [能力冻结载体](src/pulsara_agent/conversation_kernel/capability_composition.py)，`PreparedMcpCapabilitySourceSnapshotSet` | 已携带目录及 inspection inputs | 继续携带完整值，不加目录副本、DTO fingerprint 或版本字段 |
| [Dispatch 准备](src/pulsara_agent/conversation_kernel/provider_dispatch.py)，MCP owner 冻结及 `new_context` 传递 | 已将同次 MCP owner 和上下文边界传给 collector | 沿现有冻结与边界传递，禁止为目录另调一次查询或重新读取配置 |
| [来源定义](src/pulsara_agent/model_input/contracts.py)，`ContextSourceKind` | 当前只有 `MCP_CATALOG_CHANGES` | 增加初始说明的 `MCP_CATALOG`；使用 `CONTEXT_MESSAGE` 和既有 MUST_KEEP 类别 |
| [来源采集](src/pulsara_agent/conversation_kernel/context_sources.py)，`complete_frozen_sources` | `new_context` 分支只生成 Skill 目录，同时初始化 MCP 比较状态 | 同时生成独立 MCP 初始目录候选，placement 紧随 Skill；从同一 MCP 快照设置比较候选状态。MCP 目录不依赖 Skill 采集成功 |
| 同文件的 `_mcp_comparison_snapshot`、`_mcp_changes` | 已负责当前比较事实和变化正文 | 保留既有定义、策略及暂不可用处理；补齐未知起点恢复文案 |
| [输入编译](src/pulsara_agent/model_input/compiler.py)，`catalogue`／`remaining` 分区 | 只将 `SKILL_CATALOG` 提前到新上下文前部 | 将两种初始目录一并分区，保持 Skill 后接 MCP 的顺序；已有 epoch 出现任何初始目录时继续拒绝前缀冲突 |
| [消息编码及状态](src/pulsara_agent/model_input/continuity.py)，`encode_runtime_observation`、`InstalledObservationState` | 初始 Skill 目录不使用 observation 编码；MCP 比较状态已有落点 | 将初始 MCP 目录纳入同样的编码边界；复用已有状态，不新增单独“目录已发送”状态 |
| [输入安装](src/pulsara_agent/conversation_kernel/input_continuity.py)，`install` | 候选消息和 `resulting_observation_state` 一起进入 epoch view | 保持现有接受边界，验证失败候选和未采用 successor 不改变已安装状态 |
| [冷上下文装配](src/pulsara_agent/conversation_kernel/cold_epoch.py)及 [压缩链路](src/pulsara_agent/conversation_kernel/compaction/) | 共用输入编译和候选采用 | 通过公共来源及编译接线得到目录，不新增第二份压缩专用目录拼接 |
| [默认提示词](src/pulsara_agent/ports/system_prompt.py)，ROOT／Subagent | 当前只泛指相关时发现能力 | 按第 7 节替换发现引导，保留当前调用及权限规则 |

初始目录的采集逻辑可放在 `context_sources.py` 的小型纯投影函数中。无需新建通用目录框架、额外 service owner 或注册表。

### 6.1 需要特别验证的现有分支

1. 当前 `complete_frozen_sources` 的 MCP 初始化写在 Skill 正常／失败的两个分支内。本次应使 MCP 目录与其比较状态独立依据自己的冻结事实生成；Skill 失败不等于 MCP 失败，反之亦然。
2. compiler 当前只识别 Skill 初始目录。只增加 source 而不修改分区，会把 MCP 目录放到输入附近，无法满足压缩后位于交接手册前的合同。
3. 新上下文目录与后续通知分别处理，不能让已有 `eligible` 新 turn／真人入口判断在同次初始化之后再重复发新增通知。
4. 上下文预览和 compaction 测量使用同一投影，但只有真正安装的候选状态可接受；不能在 projector 内修改 session owner。

## 7. 模型发现引导

目录让模型知道已安装服务，默认提示词负责把任务需求与按需发现连接起来。对 ROOT 和 Subagent 的现有 MCP 发现句作局部替换，保留其他工具、Skill 和权限说明，不重写整份提示词。

建议表达：

```text
For web search, page reading, or external-service tasks, discover suitable installed MCP tools with query_capabilities using kind:"MCP" and query before constructing your own requests. Reuse known suitable interfaces directly; use another in-scope approach when no suitable tool is available.
```

其余调用引导继续说明：复制查询返回的 invocation.target，按 input_schema 填 arguments，通过 call_mcp_tool 调用；资源用 read_mcp_resource。用户明确指定方式时遵循用户要求，不禁止 curl，也不强制每次联网都重新查询。目录为空且已有足够事实、工具接口已经了解或任务只需普通下载时，由模型按任务选择合适路径。

`query_capabilities` 的当前描述已经包含 MCP 搜索、命中结果的 schema 与 invocation，不新增参数。原生搜索结果直接交给模型；准备 Cell 的查询结果须显式输出后模型才看到。模型收到足够说明后自行选择原生调用或业务 Cell，目录不增加强制 Cell 或“已读资格”门槛。

固定说明避免大幅增加系统提示词，不把任何具体服务器或完整工具目录写入静态 SYSTEM。默认提示词修改在新会话／合法新上下文生效，不热改已安装 epoch 的 SYSTEM。

## 8. 资源、存储与运行边界

- 本次只消费已有冻结值，不新增 initialize、tools/list、resources/list、远端探测、连接等待或自动重试。既有冷启动 readiness 及当前发现／刷新语义保持；目录不得额外拉长其等待条件。
- 目录按实际完整正文参与 provider 输入估算和实际封装计量。没有独立数量、字符、token 或总量 cap，不恢复 FULL／COMPACT／REF_ONLY、工具名摘要预算或按目录大小分流。
- 这是一条上下文来源，不是工具结果，不使用 12k／NEXT。实际 provider 输入无法容纳时沿现有真实准入／压缩规则处理，不静默截断服务名或假称清单完整。
- 复用进程内冻结快照和输入安装状态，不新增数据库表、事件、消息登记、永久日志、指纹索引、代次或内部版本标签。现有 provider 前缀校验边界保持。
- 保留查询权限、ROOT／Subagent 范围、Hook 控制、引用失效、真实派发及取消结算。初始目录不授予任何新调用资格。
- 前端能力页继续使用用户级 owner；这次模型输入目录不成为前端数据源，也不反向依赖当前活动会话页面。

## 9. 实施时同步的合同

本轮只新增本文件。实施变更时同步下列现有文档的具体口径，避免继续同时保留相反要求：

| 文档 | 同步范围 |
| --- | --- |
| [Runtime Observation 草案](PULSARA_RUNTIME_OBSERVATION_REVISION_DRAFT.zh.md) §1.3、§6.4、§7.4 | 初始能力区域包含轻量 MCP 服务目录；同次冻结初始化比较点；失败恢复按本规范；观察入口和后续净 diff 不变 |
| [Meta-only 架构](PULSARA_MCP_META_ONLY_CALLING_ARCHITECTURE_DRAFT.zh.md) §4.1、§4.4 | 接入轻量初始服务目录；保留按需接口发现、不注入全量工具 schema；压缩新目录与新比较点一起采用 |
| [能力查询](native_tools/capabilities.md) MCP 章节 | 说明名称目录用于发现提示，实际 Target、工具说明及引用仍来自既定查询；参数与返回类型不变 |
| [MCP 工具](native_tools/mcp.md) | 发现入口可由初始目录引导；调用和资源合同不变 |
| [Plan 草案](native_tools/plan.md)涉及压缩排列的段落 | 如列出完整初始上下文顺序，将 MCP 服务目录与 Skill 目录并列；Plan 新 turn 本身不重发完整目录 |
| [工具索引](PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)及 [实施计划](PULSARA_HARD_CUT_IMPLEMENTATION_PLAN.zh.md) | 加入本规范依据和实际验收结果；不改变工具数量或 API 边界 |

统一交付和数据库 hardcut 的正文结构、分页及业务存储不因本项改变；复审其中初始上下文的描述，若有排斥轻量服务名称目录的措辞则一并同步。

## 10. 实施顺序

1. 增加初始来源 kind，完成基于现有 MCP owner 的纯文本投影；明确空集、未知、Plugin 公共键和 scope 筛选。
2. 在 collector 的新上下文分支接入目录，并从同一冻结值准备比较状态；补齐未知起点恢复。
3. 将 compiler 的初始目录分区扩为 Skill 和 MCP 两种，接通冷启动、压缩、空闲压缩及只读预览；保持安装边界不变。
4. 局部更新 ROOT／Subagent 默认发现提示，同步第 9 节文档。
5. 完成第 11 节行为验收和真实 provider 观察，再进入用户另行授权的构建／安装流程。

草稿、实验和报告可放在 `output/hardcut-20261116/mcp-catalog/`。报告记录当前输入和实际行为，凭据只按已有实际 secret 值脱敏；不保存配置原文或为报告计算代码／文档 SHA。

## 11. 验收条件

### 11.1 有意义的合同验证

优先扩展已有测试的行为场景，避免新建只镜像字段、常量或 CSS 的测试。

| 场景 | 必须证明的行为 |
| --- | --- |
| ROOT 冷启动存在已启用和已禁用服务 | 清单仅列当前作用域已启用服务；未就绪但配置已知的服务仍出现；查询仍反映真实状态 |
| Plugin 与本地同名连接 | 使用公开本地 server_id 和轻量 Plugin 来源说明；名称不被当作唯一身份，实际查询 Target 仍可区分 |
| Subagent 冷启动 | ROOT_ONLY 服务不泄露；允许子代理使用的连接可发现并通过其自身 session 引用调用 |
| 初始目录与后续修改 | 下一次合格输入仅发相关净变化；普通工具续跑不重复完整目录；A→B→A 无中间通知 |
| 活动 turn 压缩 | 两份初始目录位于交接手册前；新比较点来自目录同次冻结；不补报压缩前变化；原 Plan／TODO／PCA 语义不变 |
| 空闲压缩 | 有新上下文初始目录，没有旧 turn 的运行交接要求；预览／未采用候选不改变活动 epoch |
| 目录未知、恢复为空或恢复为非空 | 不把失败当空；恢复说明不伪称新增；安装成功后继续正常比较 |
| 准备后修改、安装失败或压缩候选放弃 | 比较点对应实际采用的候选，不取后来状态，也不提前推进 |
| 同一 epoch 的后续调用 | SYSTEM 和 tools 不变，messages 只追加；能力变化不重建根、不触发 MCP direct 安装 |

可复用的测试接点：

- [runtime observation 当前合同](tests/test_hardcut_runtime_observations.py)
- [provider 前缀连续性](tests/test_round3_1_provider_input_prefix_continuity.py)
- [压缩首调用来源](tests/test_compaction_first_call_source_boundary.py)
- [压缩后续输入边界](tests/test_compaction_suffix_boundary_integration.py)
- [ROOT MCP 初次就绪](tests/test_mcp_first_request_readiness.py)和 [Subagent 初次就绪](tests/test_agent_mcp_first_readiness.py)
- [meta-only 集成](tests/test_mcp_meta_only_integration.py)

### 11.2 真实模型验证

使用保存的本地 DeepSeek 配置，分别验证 chat-completions 与 responses。沿 `LocalSettingsStore` 和 `require_pulsara_home()` 读取，使用现有只读配置注入及隔离测试环境，不修改生产设置或导出 secret 到 shell。

给模型一条普通搜索／网页任务，不在用户输入中点名 MCP 或 Firecrawl。检查真实 provider 请求与后续调用：

1. 初始 messages 中出现当前轻量 MCP 服务目录，provider tools 仍只有既定固定入口。
2. 模型通过原生能力查询或明确输出的准备 Cell 取得相关接口；不凭目录名称编造参数或调用引用。
3. 有合适已安装工具时，能够发现并调用它；无需为了脚本调用另取一次同 session 引用。
4. 不存在合适工具时，可以如实选择其他在权限内的方法；没有“每次都必须查询”或硬禁终端的额外门控。
5. 压缩后的重复场景仍能发现工具，真实输入前缀及变化通知保持合同。

用例应观察实际模型选择，而不只断言提示词里含某个工具名。发现模型绕开合适 MCP 时，先核对目录是否交付、搜索是否命中和接口说明是否足够，再局部修订发现文案，不用强制自动查询或隐藏路由替代模型选择。

记录模型输入、查询词、结果与调用链，以及是否增加了远端发现请求或启动等待。服务不可用、保存配置缺失等真实限制如实报告，不用假成功记录替代实测。

完成本规范后，模型在每个合法新上下文中都能看到当前已启用服务的轻量概览；现有按需查询、meta 调用和后续净变化形成同一条路径。
