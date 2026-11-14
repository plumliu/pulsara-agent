# Pulsara MCP 统一 Meta 调用架构草案

状态：讨论草案，未实施。本文件记录统一 meta 的架构决定，与 MCP 接入、能力查询、能力管理、统一交付、数据库及原生工具索引共同适用。现阶段只修改文档，不修改生产代码；能力变动 runtime observation 继续留待讨论。

## 1. 目标与范围

模型通过 `call_mcp_tool` 调用所有远端 MCP 工具，原生调用和 Code Mode 使用同一个入口。现有 `use_new_mcp_tool` 改名为 `call_mcp_tool`，参数和已经确定的返回语义保持不变。

不再向模型提供 DIRECT／META 两种调用方式。远端 MCP 工具不进入模型请求的原生 `tools` 数组，也不成为 Cell 的独立 `tools[远端工具名]` 方法。`call_mcp_tool` 本身仍是 Pulsara 原生工具，并可通过 JS API 使用。

完整删除 MCP direct 专属安装、兼容判断、绑定及调用路径，不保留闲置实现。内置工具的原生安装、provider 序列化和前缀冻结机制继续保留；MCP 目录发现、连接和执行器继续供 meta 使用，删除范围见第 5 章。

这样未使用的 MCP 工具无需常驻完整 schema，模型也不必判断远端工具是否进入当前冻结目录。代价是：首次使用不熟悉的工具时，需要先发现工具并阅读其接口说明，可能产生多次往返。本方案接受这个取舍，不以“脚本运行时查到 schema”代替模型实际取得说明。

本轮不改变 `read_mcp_resource`、用户通过 slash 使用 MCP Prompt 的产品语义，也不重新设计连接管理、凭据或执行框架。

## 2. 编写业务 Cell 前的信息准备

### 2.1 模型先看到接口，再编写调用

编写实际业务 Cell 前，模型应已取得所需工具的参数、含义及必要的返回结构说明：

1. 根据已有环境信息选择相关服务；信息不足时使用能力查询。
2. 查询相关工具目录和精确详情，将结果实际交付给模型。
3. 模型阅读接口后，再编写调用、字段访问和多工具组合代码。

查询可以通过原生 `query_capabilities` 完成，也可以在准备用 Cell 中查询并显式 `console.log`／`return`，交付后再编写业务 Cell。多个已经确定的工具详情可以一起获取；尚不知道具体工具的发现步骤仍有先后依赖。

模型获得足够说明后也可直接原生调用 call_mcp_tool，不要求使用 Cell；准备 Cell 应输出完整详情，包含调用引用和实际存在的返回 schema，不只打印参数说明。

脚本内存中的查询结果不会自动成为模型上下文。禁止在教程中用一段脚本“先查询未知接口，再立即写出其业务参数”来暗示模型可以在执行途中理解新 schema 并补写代码。

已经掌握且仍适用的接口不要求每次重新向模型展示。这里是信息准备与教程规则，不新增 runtime 的“已读资格”、schema 阅读登记或强制准备 Cell。

详情查询结果完整保存、接纳后，调用引用即可使用，不再等待详情以 FULL 形式交付给模型或完整返回 JS 才激活。原生详情按 12k／NEXT 顺序分页；即便当前页只展示部分说明，模型认为信息足够就可以调用，自行决定是否续读。runtime 不累计检查是否读完 schema；实际参数、权限与工具有效性仍在调用时校验。NEXT 只补读冻结详情，不签发、刷新或重新激活引用。

### 2.2 返回结构说明的边界

精确详情保留完整 `input_schema`、实际描述及服务声明的可选 `output_schema`。共同返回包装由 MCP 工具教程说明：`meta.status`、有序 `content` 和可选 `structuredContent`。

`output_schema` 描述成功的 `structuredContent`，不描述整个 Pulsara 返回包，也不约束失败业务值。没有输出 schema 时，仍可使用已知的共同包装、组合 `content` 或输出完整结果，不要求额外探测调用。只有代码需要访问尚未知晓的业务字段时，才先观察真实结果，再编写依赖它的后续代码；不编造字段，也不为了解返回形状而重复执行有副作用的操作。

统一 meta 不会自动补齐服务未声明的返回类型，也不新增自动接口推断机制。

### 2.3 同一 session 共用调用引用

`MCP_TOOL` 用于定位和查询工具；详情中的 `invocation.target` 为实际执行所用的 `MCP_TOOL_REF`。同一 session 内，原生调用和各个 Cell 共用调用引用：原生查询取得的引用可以直接传入业务 Cell，Cell 查询取得的引用也可以用于该 session 的原生调用或其他 Cell，不必为了更换调用入口再次查询。

引用不因切换 Cell、Cell 结束或模型 epoch 更换而单独失效。原生查询详情并交付给模型后，模型即可按已知接口编写业务 Cell，直接使用该引用；准备 Cell 的结果仍须显式输出，才成为模型编写后续代码时可见的信息。

沿现有进程内引用 owner 调整绑定及清理规则，不新增数据库引用表或跨重启恢复承诺。引用有效性按该工具的实际定义、执行策略、目标身份及调用范围判断，具体变化规则见第 4 章；普通连接暂不可用不等于定义改变。ROOT 与子代理是不同 session，不因共用物理 owner 而互通引用或扩大调用范围。

必须删除现有每个模型 epoch 1,024 个调用引用上限、对应容量拒绝分支及专属测试；不得将它平移为 session 总上限，不另设替代数量。继续复用有效引用、清理失效引用；不以新增计数或预算机制恢复旧门槛。

已确认接口变化使引用失效时，模型重新查询；连接暂不可用等其他错误说明实际原因，不一律引导重新取得引用。需要理解新接口时，先把新详情交给模型，再编写后续调用。不自动重做结果不确定的业务操作。

## 3. 唯一调用入口与结果

### 3.1 参数

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| target | MCP_TOOL_REF Target | 是，首位 | 当前 session 中查询取得的有效调用引用，原生与各个 Cell 共用 |
| arguments | JSON 对象 | 是 | 符合该工具 input_schema 的参数；无参数时填写 {} |

```typescript
type McpToolRefTarget = {kind: "MCP_TOOL_REF"; tool_ref: string};
type McpJsonValue = string | number | boolean | null | McpJsonValue[] | {[key: string]: McpJsonValue};

interface CallMcpToolArgs {
  target: McpToolRefTarget;
  arguments: {[key: string]: McpJsonValue};
}
```

原生调用示意；前提是模型已取得足以构造 `query` 参数的接口说明，并取得当前 session 的有效引用；同一引用也可用于 JS 调用：

```text
工具：call_mcp_tool
参数：
{
  "target": {"kind": "MCP_TOOL_REF", "tool_ref": "mcpref:<查询返回的ID>"},
  "arguments": {"query": "分页设计"}
}
```

JS 使用 `tools.call_mcp_tool({target, arguments})`。本轮不改变参数校验，不增加服务名、工具名、scope、schema 或调用模式参数；实施改名后不保留旧工具名作为公开别名。

### 3.2 保持已确定的返回语义

- 业务值为 `meta`、`content` 和可选 `structuredContent`；meta 只含 `status`，不返回业务顶层 Target，不另套 result。
- `structuredContent` 原样保留，包括缺失与显式 null 的区别，不递归猜测其中的图片或 Target。
- 正式 MCP content 按已定 audience 规则过滤，再转换成有序 text／image；图片逐项验证，坏图在原位置保留原因，其他内容继续保留。没有真实资产时不伪造 IMAGE_REF。
- 原生结果完整保存后按 12k 顺序分页，带公共时间信息、tool_result 和 NEXT；NEXT 只读取冻结结果，不重新调用远端工具。
- JS 取得完整类型化业务值，不做展示分页；只有显式 console.log／return 的组合结果沿独立 steer 交付，首版不额外设置 steer 预算。
- 状态、完整失败业务值 `err.result`、图片接入失败及模型能力投影，沿已经确定的统一交付语义，不因改名或改入口改变。

这里只收敛调用方式；完整结果合同由 [MCP 接入草案](native_tools/mcp.md)与[统一交付草案](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)共同承担。

### 3.3 错误说明与已发生效果

公开状态继续使用 SUCCESS／ERROR／CANCELLED，具体原因及已知执行事实放 content，不新增“引用错误／连接错误”等状态字段。ERROR 本身不等于引用过期，也不等于远端没有执行：

| 情况 | 返回说明与后续 |
| --- | --- |
| 本地发现旧引用失效，尚未派发 | ERROR，明确“工具定义已更新，本次调用未发送，请重新查询工具详情”等实际原因；工具已删除则说明已不可用，不承诺重查一定取得新引用 |
| 参数不符合当前已取得的 schema | ERROR，说明具体参数问题，不伪称工具定义变化 |
| 连接或目录暂不可用 | ERROR，说明实际连接／刷新情况及已知派发事实，不将未知目录说成工具已删除，不要求反复查询来修复连接 |
| 服务返回业务失败 | ERROR，保留完整失败业务值及适用 err.result，不自动判定引用失效 |
| 调用已发送，响应丢失或超时 | 按现有结算说明已知效果或结果未确认，不声称“未执行”，不自动重新调用 |
| 已收到响应，但输出 schema 验证或图片接入失败 | 如实说明收到响应后的失败，保留已有业务内容；不能改称本地未派发或据此重做业务 |

重新查询只取得当前调用信息，不证明之前操作没有发生，也不授权自动重放。新旧引用的退休只影响后续新调用的准入，不撤销已经接纳的调用或抹去其真实结果。

## 4. 能力发现与模型上下文

### 4.1 查询与固定入口

继续使用已确定的服务列表、服务内工具目录、工具精确详情三层查询和最小 Target。服务记录保留实际提供的 instructions；不默认交付所有远端工具的完整输入输出 schema，不新增全量接口注入或自动探索流程。

工具精确详情删除 DIRECT 分支及其 `tool_name`。可调用时返回既有 meta 调用引用；不可用时说明实际原因，不生成假引用。暂沿现有 META／UNAVAILABLE 表达，不在本轮另行设计查询结果包装。查询与管理入口中的权限、启停、信任、ROOT／子代理范围保持各自已确定的合同。

独立及 Plugin MCP 连接默认允许 ROOT 和子代理使用，用户可在能力页关闭子代理范围，模型不填范围参数。ROOT 与子代理均使用原生 call_mcp_tool，ROOT 适用 Code Mode 时也可使用 JS API；各 session 的调用引用不互通，不向子代理开放 Code Mode 或能力管理入口。

取消按远端工具数量、原生工具 token／字节预算选择 direct 或 meta 的分流，也不再按“原先已安装”或“后来新发现”区分调用入口。所有符合实际权限和连接条件的远端工具统一走 meta。远端 schema 是否适合安装进 provider 原生 tools，不再决定其 meta 调用资格；详情过长走冻结分页，不因装不进一页而判定工具不可用。实际协议、参数有效性及执行资源边界仍由相应 owner 处理。

新的工具发现、连接恢复和能力变更只影响当前查询与实际调用资格，不向已安装的 provider `tools` 或现有 Cell 注入远端工具方法。既有 epoch 的 SYSTEM 与 tools 保持不变，messages 继续只追加。实施后的新会话从当前合同开始，不为旧会话构造兼容路径。

### 4.2 复用现有发现与刷新

继续复用 SDK 通知、本地连接 owner 的初次发现、通知后的完整目录刷新、已配置的周期刷新及重连发现。收到 list_changed 只表示目录可能变化；完整取得并接纳目录后，才能确认具体工具新增、删除或定义变化。目录分页失败或观察不可用不等于工具删除。

保留现有通知后暂挡新准入、排空已接纳操作、重新 listing 并采用目录的路径。SDK 的 listing 会更新输出 schema 和参数 header 映射缓存，因此这条边界在 meta-only 下仍有用途；不随原生兼容分类器一起删除，也不另建一套缓存或刷新协调器。

原生与 JS 的动态 MCP 查询和新调用引用解析，都使用当前已接纳、当前调用范围内的目录与执行器，不继续依附 provider 冻结工具视图或旧 borrow 的 MCP 快照。provider 前缀冻结与动态目录选择分别由原 owner 承担；已经接受的查询结果及 NEXT 仍读取其冻结快照，不随实时目录变化改写。

服务可能没有通知、漏发通知，或在观察之后才更新。runtime 只对已观察事实作判断，不承诺调用前一定知道所有服务端变更，不新增每次调用前强制 listing。实际调用的参数拒绝、isError 或输出不匹配本身不足以证明引用已过期，继续按真实结果处理。

### 4.3 按工具变化决定引用是否失效

删除原生基线与当前定义的兼容分类、旧新契约双重参数校验、单向降级锁、回滚后的原生恢复限制和原生例外占位。meta 引用只判断实际依据是否仍一致，不再计算输入／输出变化是否兼容。

| 当前已确认事实 | 引用与调用规则 |
| --- | --- |
| 新增工具 B，工具 A 未变 | B 可按既有目录查询；A 的有效引用继续使用 |
| 删除 A，或 A 改名 | 旧 A 不再接受新调用；改名按删除旧工具、增加新工具处理，不建立自动别名 |
| A 的 description、input_schema 或 output_schema 改变 | A 的旧引用失效，重新查询完整详情；即使变化可能兼容，也不保留旧原生契约或建立宽松引用规则 |
| A 的实际执行策略或可用范围变化 | 按当前策略和范围检查；依赖已变化依据的旧引用不继续使用，不影响依据未变的其他工具 |
| 目录正在刷新或连接暂不可用 | 暂不接纳不具备实际执行条件的新调用，不仅凭暂不可用事实永久判定全部工具已删除／引用已过期 |
| 同一实际目标普通重连，完成发现后 A 的定义、策略及调用范围仍一致 | A 的逻辑引用可以继续使用，新调用解析当前执行器并取得当前物理连接的许可 |
| endpoint、执行账户或安装来源等实际目标身份改变 | 受影响引用失效，不能仅凭相同 server_id、工具名或 schema 迁移到新目标，也不能继承旧批准 |

工具定义不包括仅为原生安装产生的 provider 工具别名。不得仅因整个目录、Host runtime generation 或其他工具发生变化而使未变工具引用失效。实际目标与该工具策略沿既有配置、连接和账户 owner 核对，使用其已有完整事实；整份 resolved_config_identity 还包含显示名称、其他工具设置和刷新间隔，不能直接拿它的整体变化代替该工具或实际目标变化。不新增版本字段、fingerprint 或身份注册表。

已准备、等待审批或已获许可的调用继续持有原执行器和冻结参数，不在中途重新解析引用并转给新目标。尚未获得执行许可的调用仍受当前准入检查约束；已经接纳的调用，包括正在等待物理执行通道的调用，按原许可结算，刷新不能静默迁移、取消重做或丢弃其结果。

### 4.4 能力变动通知另行讨论

能力变动的 runtime observation 留待下一步单独讨论，本轮不确定其字段、目录粒度或预算，也不将全量远端 schema 改为从 observation 注入。

## 5. 完整删除 MCP direct，保留公共安装与 meta 执行链

删除将远端 MCP 工具安装进 provider 原生 tools 数组及 Cell 独立方法目录的整条专属路径。MCP 服务发现后将工具目录、执行策略和执行器接入 runtime 的过程继续保留，供能力查询与 meta 执行使用；不能仅因代码名称包含 installation、direct 或 native 就整块删除。

| 层次 | 本次要求 |
| --- | --- |
| MCP SDK、发现目录、连接、执行器 | 继续复用，供查询和 meta 执行使用 |
| MCP direct 选择与预算 | 删除数量、token／字节预算分流，以及已安装／后来发现工具的入口区别 |
| MCP 专属原生投影与安装 | 删除远端 schema 的 provider 原生安装投影、直接执行绑定及 Cell 独立方法收录；保留查询与实际参数校验需要的原始 schema |
| MCP 原生定义变化处理 | 删除新旧原生 schema 兼容分类及矩阵、旧新契约双重参数校验、降级锁、direct 转 meta、原生恢复限制、冻结名字对应的不可用占位入口及专属诊断；保留第 4 章的实际目录刷新与单工具有效性判断 |
| MCP direct 授权与执行入口 | 删除专属分支，统一通过现有 meta 授权和执行链 |
| Pulsara 内置工具原生安装 | 保留，包括 call_mcp_tool；公共 provider 序列化、工具投影和前缀冻结继续使用 |
| 能力查询 | 删除 DIRECT 分支；可调用工具统一给 meta 引用 |
| meta 调用失败 | 报告实际失败，不保留 direct 回退入口 |

`direct_tool_surface`、`direct_projection_set` 等现有公共结构也承载内置工具，实施时只移除其中 MCP direct 的字段、分支与约束；仍服务内置工具和 provider 前缀连续性的部分保留。MCP 增减不再改变原生 tools 数组，但不能因此取消 SYSTEM／tools 不变、messages 只追加的 epoch 规则。不新增模式开关、备用入口、兼容层或静默回退。

MCP 权限判断保持现有 meta 语义，使用当前 turn 冻结的权限模式和工具实际执行策略：

| 权限模式 | READ_ONLY 工具 | EXTERNAL_EFFECT 工具 |
| --- | --- | --- |
| READ_ONLY | 进入正常执行准入 | 拒绝 |
| ASK_PERMISSIONS | 进入正常执行准入 | 进入现有权限确认流程 |
| ACCEPT_EDITS | 进入正常执行准入 | 进入现有权限确认流程 |
| BYPASS_PERMISSIONS | 进入正常执行准入 | 进入正常执行准入 |

效果分类仍按单工具配置覆盖、服务默认配置的优先顺序确定；没有明确配置时按 EXTERNAL_EFFECT，不仅凭服务的 readOnlyHint 自动放行。实际参数校验、引用及执行器有效性、权限 Hook／用户确认继续复用现有 meta 链路。共用调用引用不授予执行权限，也不改变启用设置、ROOT／子代理范围、Hook 信任、凭据或 OAuth 的职责。

连接建立、更新、关闭和凭据处理继续沿现有 owner；取消、已经接纳或派发的调用、已发生副作用及结果保存照常结算，不因删除 direct 而重写或丢弃。复用原有 meta 引用 owner，按第 2 章调整 session 绑定与接纳后生效，不重写一套 MCP 执行链。

删除只验证 MCP direct 安装、兼容及分流行为的专属测试；混合用例中的权限、连接、取消、结算与前缀连续性断言按 meta-only 合同保留并验证，不因测试文件含 direct 名称而整份删除。能力变动 runtime observation 的具体修订仍按第 4 章留待讨论。

## 6. 当前代码接点与关联文档

当前代码仍是混合入口，本草案不宣称已经实施：

- [tool_runtime_surface.py](src/pulsara_agent/conversation_kernel/tool_runtime_surface.py) 删除 MCP direct 绑定、不可用占位及 `freeze_code_mode_tool_directory` 中收录远端独立方法的分支，保留内置工具的安装与绑定。
- [tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py) 的 `_inspect_mcp_tool_result` 目前按冻结目录返回 DIRECT 或 meta 引用；统一后查询使用 meta 路径。
- [planner.py](src/pulsara_agent/capability/planner.py) 删除 MCP direct 选择、预算分流及详情 FULL 展示门槛；[mcp_compatibility.py](src/pulsara_agent/capability/mcp_compatibility.py) 的 MCP 原生定义兼容判断随调用方一起删除。
- [tool_runtime_authorization.py](src/pulsara_agent/conversation_kernel/tool_runtime_authorization.py) 与 tool_runtime 删除 direct 专属授权和执行分支，保留 meta 权限矩阵、确认、执行准入及共同结算路径。
- [provider_dispatch.py](src/pulsara_agent/conversation_kernel/provider_dispatch.py)、[能力合同](src/pulsara_agent/capability/contracts.py)及[前缀连续性](src/pulsara_agent/model_input/continuity.py)中的公共原生工具投影继续服务内置工具；删除 MCP direct 专属依赖，不拆除公共前缀核对。
- [mcp/meta.py](src/pulsara_agent/conversation_kernel/mcp/meta.py) 及 tool_runtime 中的消费者绑定、结果接纳和清理接点，改为同一 session 共用引用、查询结果完整接纳后可调用；删除原生 FULL 安装和 Cell 交付回调对引用激活的依赖，以及 MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH、McpToolRefCapacityExceeded 和 MCP_REF_CAPACITY_EXCEEDED 对应的旧上限路径及专属测试，不影响其他有独立用途的交付处理。
- [supervisor.py](src/pulsara_agent/conversation_kernel/mcp/supervisor.py)、[slots.py](src/pulsara_agent/conversation_kernel/mcp/slots.py)和[SDK facade](src/pulsara_agent/conversation_kernel/mcp/sdk_facade.py)继续负责通知、目录刷新、SDK 缓存更新、当前连接及物理准入。将当前已接纳目录用于原生／JS 查询与新调用解析；从逻辑引用有效性中移除全局 runtime_generation 相等要求，保留执行许可与实际物理 owner 的核对。
- 同一文件已有 meta 调用接纳与执行路径；[builtin_catalog.py](src/pulsara_agent/capability/builtin_catalog.py) 定义当前元工具参数。改名须覆盖原生目录、JS 目录、查询说明和教程，不改变远端参数语义。

以下文档共同约束实施，职责如下：

| 文档 | 同步内容 |
| --- | --- |
| [MCP 接入](native_tools/mcp.md) | call_mcp_tool 原生／JS 用法、共同返回；移除远端 direct 调用教程，明确先取得接口再编写业务 Cell |
| [能力查询](native_tools/capabilities.md) | 删除 DIRECT 和直接工具名，统一参数／返回说明；引用同 session 共用，完整接纳后可调用，不要求详情全部交付 |
| [能力管理](native_tools/manage_capability.md) | ROOT／子代理默认范围与统一 meta 调用；区分调用引用和物理使用引用，保留安装、启停、信任、授权等底层合同 |
| [统一交付](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md) | 删除混合入口表述，保持唯一结果、原生分页、JS 完整值和独立 steer |
| [数据库](PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md) | 详情与结果的唯一存储路径；调用引用仍由进程内 owner 管理，观察范围不承担激活职责 |
| [原生工具索引](PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md) | 工具改名及覆盖范围 |

实施时核对：MCP direct 专属路径及测试已删除，内置工具原生安装和前缀冻结仍有效；未使用的远端 schema 不常驻工具面；ROOT／子代理原生调用和 ROOT JS 统一 meta，可跨同一 session 的调用入口复用引用；删除每 epoch 1,024 个引用上限及专属测试，不新增 session 总上限；模型自行决定接口信息是否足够，未读完详情不阻止调用；NEXT 只补读冻结结果；动态发现不改写冻结目录或 provider 前缀；除明确调整的引用作用域、激活门槛及 direct 删除外，返回值、权限、连接、错误及副作用结算沿已确定合同。无需为本次架构决定新增数据库表、永久注册表、内部版本标签或资源 cap。

工具变化验收覆盖：单工具定义变更使其旧引用在新准入前被拒绝；无关工具增删及目录重建不连坐；同目标重连可复用引用；换 endpoint／执行账户不能沿用旧引用或批准；刷新失败不误报删除；未通知变更按实际调用结果处理；原生／JS 读取当前已接纳目录，NEXT 保持旧快照；旧许可调用正常结算，未派发错误与已发送结果未确认准确区分，不自动重做业务。删除仅验证原生兼容与降级的测试，保留这些实际行为验证。
