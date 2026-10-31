# Pulsara 自有 ID 遗漏收口 hard-cut 实施规格

状态：**实施已落地；worker 请求身份与 ID-16 公开投影的后续收口均已实现。各批次验收范围和限制见报告及证据。**日期：2026-11-01。源码调查基线：`8996d6bb`。

本文覆盖本次 7 位代理全仓扫描和 ROOT 复核的发现，包括确定的生成遗漏、伪身份、接受边界缺口，以及经核实不属于对象 ID 的值。Node worker 的 prepare/call request_id 已纳入共享 R(request)，准备结果通过 request_id 复用 prepare 请求身份；sequence、握手与原生命周期保持。固定 wire key 与实际秘密重叠时，按 ID-16 的“先投影动态数据，再组装固定协议”合同处理。

## 1. 目标与不可改变的产品合同

Pulsara 自己分配、派生并用来识别对象、请求、实例或可操作投影的 ID，统一通过共享 identity 层生成和校验，格式为 `<kind>:<26 位小写 Base32>`。不能以“内部”“易失”“只在前端”“名字叫 token/fingerprint”为理由保留第二套身份编码；也不能把历史文档的例外直接当作当前保留依据。

本次只改变身份表示及其必要的生成、传递、查询、校验方式，不改变产品语义：

1. 同一语义对象何时创建、何时复用、何时失效，以及当前 owner、作用域和并发归属保持。
2. 排序、分组、去重、命令幂等、请求与响应关联、重试、取消、Stop、晚到结果结算保持。分页的完整逻辑序列、冻结 cut、无遗漏／重复和权限保持；按实际字节装页的条数允许随新编码长度自然变化，见 §2.3。能力优先级／选择的逻辑规则不得因改用 typed 身份字典序而改变。
3. ROOT／Subagent turn、Work、队列、steer、Cell 输出、定时触发和权限继承合同保持。不得借本轮恢复 monitor、Work final 自动广播或任何新轮唤醒路径。
4. MCP DIRECT／META、原始工具名、provider-qualified 工具名、配置来源及覆盖优先级、连接代次、物理调用与副作用结算保持。
5. UI 的标签打开和复用、选中状态、关闭、历史滚动、折叠、异步导入、图片和批注编辑、undo/redo 保持；不增加面向用户的新操作。
6. 文件预览权限、读取根、凭据强度、过期／撤销、Hook trust、OAuth、权限和 Plan 确认保持；身份字符串不授予执行权。
7. 同一现有 provider epoch 内，SYSTEM 和 tools 逐字节不变，messages 只能追加后缀；只有现有 cold epoch 或明确采用的 compaction successor 可安装新 schema／根输入。身份收口不是新增 rebase 边界。
8. 不增加业务表、事件种类、subject、持久 job、恢复系统、映射注册表、墓碑或生命周期／累计调用上限。只扩展确实已有对象需要的 kind 词表，更新已有 owner、字段和约束。

旧数据、旧内部协议和旧编码不保留在线兼容。实施按 clean-v0 重建已核实的本地可丢弃数据库。不得因此修改未核实或远程数据库、用户保存的凭据／网络配置。

## 2. 统一层与身份配方

### 2.1 唯一格式

复用 [Python identity owner](src/pulsara_agent/identity.py) 和现有 [TypeScript identity 入口](frontend/lib/identity.ts)。不得在领域 owner、Electron、UI 组件或测试支持代码再复制 Base32／摘要编码算法。

- 后缀承载 128 bit，正则为 `[a-z2-7]{25}[aeimquy4]`，严格检查零 padding bits、允许的 kind 和完整字符串。
- `R(kind)` 代表共享 `random_id`／`randomIdentity`，使用系统 CSPRNG；不是时间戳、计数器、Math.random、UUID 的二次手工拼装或十六进制编码。
- `D(kind, role, parts...)` 代表共享 `derive_id` 的既有字符串数组、UTF-8、SHA-256 前 16 字节和 Base32 配方。数字用既有规范十进制表示，布尔值在下表中固定为 `0`／`1`，不擅自做 trim、大小写或 Unicode 归一化。
- 已分配身份不在 render、重试、回调或投影刷新时重建。D 的 role／parts 是闭合配方；不能开放任意 UI 文本充当身份配方。
- 未发布的 R 若在原 owner 已有且可枚举的占用集合／唯一约束中发现碰撞，就在原创建流程重抽；已经发布的 ID 不换号。对跨 owner、已退休对象或只有闭包的 presentation 不新建全局注册表／墓碑，沿既有 128-bit 剩余概率合同。D 命中必须仍由原 owner 核对其持有的完整语义或真实跨重启验证事实，冲突明确失败，不能加盐回退。
- 保留原 PK／UNIQUE、live collection、对象身份和私有 authority 检查，不另建“短 ID → 原 ID”表。有限 128-bit 空间的剩余碰撞风险沿既有统一 ID 合同，不承诺删除后数学上的零复现。

JS 随机身份、kind 词表、编码与校验由 [identity-core.mjs](desktop/src/identity-core.mjs) 单一依赖无关源码拥有；desktop identity.ts 保留现有 SHA 派生入口并复用该 core，renderer 从同一入口引用。Electron 编译将 core 纳入 dist；现有 prepare_code_mode.py 将同一源码原样复制到离线 Code Mode 资源，worker 从随 Python 包提供的该产物导入，不读取开发目录、不下载依赖、不保留第二份手写算法或路径 fallback。mise 的真实 sources/outputs 跟踪 core，Hatch 要求已准备资源并放入 wheel。Python 与 JS 使用同一 kind／编码合同和共享测试向量，不建立跨语言身份服务。

### 2.2 本轮闭合配方表

新增 kind 只命名已经存在的对象，不创建新实体；Python 与 TypeScript 的适用词表保持对应，现有 kind 继续复用。

| 现有对象／字段 | 目标配方 | 分配／复用边界 |
|---|---|---|
| 原生网页 tab、文件预览 tab | `R(tab)` | 原生 tab 在原创建/adopt 分支一次分配；文件 tab 在 prepare、预览 I/O 前分配，adopt 命中旧对象时释放新资源并沿用旧 ID；不因重绘／导航换号 |
| Electron 原生网页实例 | `R(browser-instance)` | 现有 BrowserOwner.create 时；renderer tab 与 native instance 仍是不同对象 |
| 原生网页 presentation | `R(presentation)` | 每次现有 presentBrowser display-slot owner 创建时；失效、hide 和晚到 layout 匹配保持 |
| Code Mode prepare/call request_id | `R(request)` | 每个新内部请求独立分配，按原 Cell pending/握手关联；sequence 仍只对实际 call 递增 |
| 准备结果 request_id／正式调用 preparation | 复用对应 prepare 请求的 `request:` ID | 不另造 preparation ID；正式 call 自己的 request_id 独立，finally 仍释放同一准备记录 |
| retain reservation 关联请求 | `R(request)` | 每次现有 retain 请求；复用同一请求等待 future，不把它与工具调用 request／sequence 合并 |
| inspect-quit、open-oauth-browser 控制请求 | `R(request)` | 每次现有控制请求；两方向各自 pending owner 和结果相关性保持 |
| controller／observer 的 client_instance_id | `D(transport, client-instance, connection_id, suffix)` | suffix 精确取原分支 `controller`／`observer`；不从 ID 反推出或改变 attachment role |
| MCP supervisor owner_epoch | `R(epoch)` | supervisor 实例构造一次；它不是 connection_generation／runtime_generation 计数器 |
| MCP 远端工具 remote_identity | `D(mcp-tool, remote-tool, server_id, connection_generation, remote_tool_name)` | 同一现有 server／连接代次／工具组合稳定；完整原始工具名参与 |
| MCP 标准操作 remote_identity | `D(mcp-tool, standard-operation, server_id, connection_generation, tool_name)` | 标准操作与远端工具 role 分开；取消前返回与常规返回使用同一既有组合 |
| reasoning projection block_id | `D(reasoning-block, provider-reasoning, entry_id, ordinal)` | 与当前 entry 和 reasoning ordinal 一一对应；与持久 block: 命名空间分离，不新增持久行；读取显式带原 ordinal |
| 历史初始 segment_id | `D(segment, history-initial, owner_kind, owner_id)` | 现有 turn／imported-history owner 的初始段 |
| 历史事件边界 segment_id | `D(segment, history-boundary, owner_kind, owner_id, segment_event_id)` | 保留原事件边界，不能把不同事件合并 |
| 导入中断后 segment_id | `D(segment, history-interruption, owner_kind, owner_id)` | 只替换已有中断段身份，不新增 turn／分段 |
| TODO live draft_identity 与 block_id | 两字段均为 `D(block, todo-snapshot, todo_run_id, todo_revision)` | 保留当前两字段相等关系；一份同 revision 快照仍是一次 advisory 观察 |
| history_search_documents.id | `D(search-document, history-projection, session_id, q1_entry_id, variant, include_tools)` | variant 保留 `QA`／`QAQ`，include_tools 为 `0`／`1`；复用原投影行主键和 BM25 key |
| renderer BrowserHistoryPage.id | `R(page)` | 现有 page 对象真正新建时；命中并复用既有 page 时沿用 ID |
| 草稿批注节点 draftId | `R(draft-node)` | 保留同一编辑节点跨编辑、重复引用和 undo 的身份；不写入最终 prompt |
| 草稿导入 DraftImport.id | `R(draft-import)` | 原异步导入对象创建时；重试仍属于原对象，不误建第二份 |
| 草稿图片 DraftAsset.id | `R(draft-asset)` | 原 asset 新建／恢复时；既有节点引用、ready promise 和 object URL 生命周期保持 |
| workspace Skill UI 引用 | `D(skill, ui-target, root, name)` | root 精确为原 agents／pulsara；仍在原 workspace 库存精确解析，不为 user path-based 库存新增 ID |
| session 只读 Skill winner 投影 | `D(skill, ui-target, source, name)` | source 精确为原 workspace／user／plugin／bundled；保持只读与原相等维度 |
| 无真实对象 ID 的临时 UI 视图 | `R(view)` | 只在原临时对象首次分配身份处生成并随原对象复用；不作为 canonical entry、工具调用或执行权 |
| MCP_SERVER.runtime_server_id target | `D(mcp-server, runtime-target, runtime_source_key)` | runtime_source_key 为现有唯一 runtime 查找键；不加入原来没有的物理 generation／安装实例维度 |

`tab`、`browser-instance`、`presentation`、`mcp-tool`、`reasoning-block`、`search-document`、`draft-node`、`draft-import`、`draft-asset`、`skill`、`mcp-server`、`view` 是本轮为既有身份新增的 kind；其余使用已有 kind。不得复用 `replay-reservation` 冒充 retain 请求。


### 2.3 字节边界、分页与激活

人类已明确决定：**继续按实际字节分页，允许新 ID 编码长度导致每页自然装入不同条数。** 保持物理上限、逻辑顺序、冻结 cut、权限、全量内容和续页无重复／遗漏；不要求旧新逐页成员、cursor 字节或临界 RESOURCE_EXHAUSTED 结果相同。不保留旧 ID 的虚拟长度计量、旧编码 admission 或新旧协商。snapshot、history page/window/context、metadata/schema／工具结果均按各原 owner 的实际 wire／JSON／protobuf 字节重新计量；不得放宽上限或静默丢弃内容来抵消表示变化。

这不授权改变能力优先级、排序、去重或语义选择。ID-14 中真正的 prefix／framing 协议所保留的完整 digest 仍按其固定真实表示计费，不能把“SHA 查找键换成 typed 值”误实现为不同排序／不同协议。实际物理内存／payload 上限照常核验，共享冻结对象引用，不在各 DTO 复制长名字和全量结构。原 replay carrier 16 MiB 边界、分页和无限逻辑段合同保持，不增加 reasoning 数量或累计历史上限。

这是 hard cut 的安装约束：不得在已暴露旧 target 的活动 epoch 中热换 resolver 或校验规则，使 messages 中仍合法的旧引用突然失效。部署停用旧进程后，使用既定新进程 cold epoch 和 clean-v0；现有支持的 successor 路径只在原明确采用边界安装。不是保留两个版本的解码器，也不新增 epoch 边界；已运行旧二进制的 epoch 只能由该原二进制按原合同结束。

## 3. 逐项生产遗漏与语义保护

以下源码位置以调查基线为准，实施时定位实际函数，不把行号当接口。每项必须同时修改生产者和所有实际消费者；只换字符串前缀不算完成。

### ID-01：浏览器对象和 Electron／renderer 共享层

位置：[browser-tabs.ts](frontend/lib/browser-tabs.ts:75)、[browser-owner.ts](desktop/src/browser-owner.ts:75)、[browser-presentation.ts](frontend/lib/browser-presentation.ts:9)。

三种身份跨 IPC 进入 records/pages、BrowserState、activate/layout/hide/close 和显示所有权匹配，不是 DOM key。按 §2 分配，并在 IPC 接受处使用统一 kind 校验。

必须保留 tab 去重、批次 adopt/start、关闭中页面、原生资源收尾、过期 presentation 无法控制新显示 owner、弹窗和导航的全部现有关系。合法但已过期的 instance/presentation 在原 matches/layout/hide 路径继续 no-op，不因加校验升级为异常；activate 先隐藏旧页面再采用新显示 owner 的顺序保持。不得把 tabId、instanceId、presentationId 合成一个 ID。

### ID-02：Code Mode 请求、准备结果与 retain reservation

位置：[host.py](src/pulsara_agent/code_mode/host.py:498)、[sidecar.mjs](src/pulsara_agent/code_mode/node/sidecar.mjs)。retain 请求存入 cell.reservations，sidecar 原样回传后完成原 future。

改用 `R(request)`；字段 reservation、bytes、accepted 与现有 retain 协议不因命名统一变成工具请求。保留 cell 内并发关联、资源预算、关闭时 future 结算、异常/finally 释放，不增加 ACK、重试、保留期或恢复协议。


worker 的 prepare／call 在每次原 newRequest 处调用共享 `randomIdentity("request")`；只对原 pending/preparationCharges/awaitingResume 持有的 ID 检查占用并重抽，不加总次数上限、全局 registry 或墓碑。Python Host 及 worker 的请求／结果／delivered／resume／discard 接受点使用共享 request-kind 校验，不接受旧计数值、错误 kind 或非法 padding。

prepare 返回内容精确为 `{request_id, peers}`，request_id 与对应 prepare 请求相同；guest 的正式 batch 调用和 finally/discard 都读取 prepared.request_id。删除 prepared.id 的产生和读取，不保留双字段／缺省回退。正式 call 的 request_id 是另一独立请求，preparation 字段引用前述 prepare request_id；保留现有成员身份验证和一次性取用，不再分配第二个 preparation 实体。

实际工具调用 sequence 原样保留 BigInt 计数、call 时递增、十进制 wire 和 Python 整数语义，prepare 不递增。result→delivered→resume、delivery owner 提交后才继续 guest、prepared 生命周期、原数据预算/finally、Stop 和晚到已接纳工具结果均保持。验证使用真实 QuickJS 的并发批次／普通工具乱序结果、prepare resume 门禁、旧请求／旧准备字段拒绝以及隔离安装 wheel 后的 worker；仅表示增加的字节按原实际物理上限计费。

### ID-03：桌面宿主控制 requestId

位置：[runtime.ts](desktop/src/runtime.ts:186)、[desktop_runtime.py](src/pulsara_agent/desktop_runtime.py:213)。

两方向都改用 `R(request)`，发送与回显必须是同一个值，接受处验证 request kind。保留 inspect-quit 现有超时、preparing 时不排入真实查询、宿主退出／管道断开时 pending 请求失败，以及 OAuth 浏览器 opened 结果语义。不要将该 requestId 改成 OAuth state 或认证秘密，也不改变 OAuth 会话。

### ID-04：HELLO 的 client_instance_id

位置：[protocol_client.py](src/pulsara_agent/desktop_app/protocol_client.py:301)、[terminal_kernel_v3.proto](src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto:21)。

按 §2 派生，审清 schema、serialize、parser 及外部测试 caller。目前未找到服务端用此字段授权或定位的证据，不能借统一 ID 新增授权、去重或 controller 互斥语义；现有角色和附件所有权仍由原字段和 owner 决定。

### ID-05：MCP supervisor incarnation

位置：[supervisor.py](src/pulsara_agent/conversation_kernel/mcp/supervisor.py:1364)、[mcp/contracts.py](src/pulsara_agent/conversation_kernel/mcp/contracts.py)。`_epoch` 用来判别 candidate 属于哪个 supervisor。

使用 `R(epoch)` 并同步相关 DTO 的类型和校验。只改变该随机 incarnation；connection_generation、runtime_generation、配置 revision 等原递增整数不改成对象 ID。supervisor 重建时失效、slot/lease/candidate/permit 的归属与当前完整事实比较保持。

### ID-06：MCP remote_identity 两条路径

位置：[supervisor.py](src/pulsara_agent/conversation_kernel/mcp/supervisor.py:3703)、[标准操作](src/pulsara_agent/conversation_kernel/mcp/supervisor.py:1086)、[tool_execution.py](src/pulsara_agent/conversation_kernel/tool_execution.py:2172)、[_repository/tools.py](src/pulsara_agent/conversation_kernel/_repository/tools.py:501)。

远端工具和标准操作进入 McpKnownToolResult 和持久 attempt 的 remote_identity，按 §2 的两个闭合 role 派生；同一现有组合在所有成功、失败、取消前分支一致生成。保留原始 server/name/generation typed 值供路由和诊断，短身份不替代完整确认事实，也不伪称由 MCP 服务端签发。

持久确认不能只比较截成 128-bit 的 MCP remote_identity。现有 attempt 行、prepared publication 和同一次既有事件 payload 同步携带独立的 `remote_identity_verification` 完整验证事实：远端工具保留当前 `_mcp_remote_identity` 的原 domain `pulsara:mcp-remote-tool-identity:v1`、原 JSON 字段 `{server_id, connection_generation, remote_tool_name}` 和原 canonical 编码产生的完整 SHA-256；标准操作在原发布边界计算完整 SHA-256，domain 固定为 `pulsara:mcp-standard-operation-identity:v1`，JSON 为 `{server_id, connection_generation, tool_name}`。两类短 ID 仍按 §2 对应 role 生成；verifier 是完整验证值，不使用 identity 层截短，也不作为对象 ID 公开调用。
完整 verifier 在物理调用前由原 lease/permit 对应的冻结事实确定，贯穿结果、prepared、attempt 持久写和 event payload；publish/confirm 在原事务内同时精确比较短 ID、完整 verifier、session/attempt、原事件，保持 FULL/NONE/CONFLICT 以及 commit 成功但 ACK 丢失的原语义。只在原 attempt 增加必要的 verifier 字段和原事件 payload 值，不新增表、事件、注册表、job 或第二次提交；这是原跨重启确认的验证事实，不是另一对象 ID／可调用别名。原始长名字无需复制到有 4 KiB 等原实际上限的发布字段。注入短 ID 相同而完整 verifier 不同的冲突必须拒绝，不得复用已发布结果。


共享 publication 还服务于 exec_process/manage_process，必须保留原 closed 分支：MCP 两类 `mcp-tool:` 非空时 verifier 必填并精确比较；terminal `process:` 继续按原完整 process ID 确认，verifier 为 NULL；没有 remote identity 时两字段均 NULL，不新增 publication。原 remote_identity／published_at 的成对空值及事件发生条件保持，process 的原事件 payload 不新增 verifier 键／不改变原字节语义。clean-v0 约束只在对应 MCP 分支要求 verifier，prepared/result DTO 和 repository 一致校验；不得让终端工具承担 MCP verifier、不得把没有 process 的结果伪造成 remote 对象。

完整长工具名仍能结算，不恢复 4 KiB 名称截断或拼接歧义；物理调用次数、失败诊断、未知副作用和 late settlement 保持。MCP SDK call/request/session IDs、远端工具名和 URI 原样传输。

### ID-07：reasoning block 可读定位符

位置：[canonical_v3.py](src/pulsara_agent/terminal_protocol/canonical_v3.py:1933) 和 `_resolve_reasoning_content`，以及 [runtime-adapter.ts](frontend/lib/runtime-adapter.ts:2619)。

projection block_id 使用独立 `reasoning-block:` kind（§2），避免与持久 `block:` 命名空间混同。ReadContentRequest／renderer selector 同步增加有 presence 的非负 `reasoning_ordinal`，值来自原 CanonicalReasoningBlock.ordinal；它只是已有 ordinal 的显式编码，不授予读取资格。该字段只在 reasoning-block selector 分支合法；普通 block／entry／queue／image／visualization selector 不允许混入它，保留原互斥选择规则，更新 proto、生成文件和实际 parser。

读取顺序必须明确：先由原 session/entry/attachment 授权；普通持久 block 查询仍走原精确分支。reasoning 分支在读取 replay 前，用 entry_id、显式 ordinal 按 §2 重算并核对 block_id；不是该 entry 的 ID、缺少／非法 ordinal 或不匹配的 ID 按原 content-reference-missing／参数错误载体返回，不因为一个无关 ID 解析损坏 replay。匹配后才沿原有 entry/replay 读取及损坏处理，检查真实 ordinal 范围并返回同一正文。投影时与已装载的同 entry reasoning 集合检查重复派生身份；直接 read 即使未先执行投影，也必须在 selector 已匹配、replay 已按原路径加载后，在同一个已加载 reasoning 集合检查请求 ID 是否只属于该 ordinal。两个不同 ordinal 同号在投影和直接读取都明确冲突，不能返回任意正文或加盐；不为无关 selector 提前加载 replay。真实 block 与 reasoning 的 namespace 已分离，不新增查询／永久碰撞表。

删除旧字符串反解及旧 selector 接受路径。不得为反解短 ID 扫描所有 entry、盲解任意 replay 或增加映射表。保持原 reasoning 顺序、类型、懒加载、权限和可用性；缺失、损坏、无权读取、inline/fragment 阈值沿原 owner 的实际合同。provider 原生 block/item ID 不改写。按 §2.3 用真实新表示计量分页；不新增 reasoning 持久副本或假 block 行。

### ID-08：历史 segment_id

位置：[canonical_v3.py](src/pulsara_agent/terminal_protocol/canonical_v3.py:1069) 的 entry_contexts，消费者为 [runtime-adapter.ts](frontend/lib/runtime-adapter.ts:2663) 的 historyGroupId。

三种分段精确按 §2 替换，更新线上投影 DTO、分页边界、renderer 与测试；不根据新 ID 的字典序排列，不增加或合并分段。跨页、冻结 cut、导入中断前后、steer 前后仍按原 entry/event sequence 分组，同一分段在重复读取和相邻页中仍相等。

### ID-09：TODO live 快照身份

位置：[tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py:4175)、[live.py](src/pulsara_agent/conversation_kernel/live.py)、[v3_gateway.py](src/pulsara_agent/terminal_protocol/v3_gateway.py:1598)。

使用 §2 的同一 block ID。generation_id 的既有配方不改，todo_run_id、todo_revision、计数和有序 items 不改。继续只作现有快照失效／advisory 观察，真正 TODO 内容由原当前 owner 快照提供；不写入 canonical、不唤醒模型、不增加 durable occurrence，也不使丢失的 live 观察获得可靠交付承诺。

### ID-10：历史搜索投影主键

位置：[history_publication.py](src/pulsara_agent/conversation_kernel/history_publication.py:167)、[history_search.py](src/pulsara_agent/conversation_kernel/history_search.py:114)、[clean-v0](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql:2424)。

该 id 虽可丢弃，但已是现有投影表主键和 BM25 key，纳入统一生成。替换它不新增 canonical 文档实体；原 session/q1/variant/include_tools 事实和索引职责保持。保持 QA／QAQ 互斥布局、同 cut 替换、stale publish 防回退、检索排序和分页。不允许因为新 id 字典序改变 tie-break、遗漏／重复结果；遇到以旧主键作 tie-break 的地方必须显式保留原语义排序元组。

### ID-11：renderer page、文件 tab、草稿对象

位置：[runtime-adapter.ts](frontend/lib/runtime-adapter.ts:2339)、[file-browser.tsx](frontend/components/file-browser.tsx:200)、[prompt-draft.ts](frontend/lib/prompt-draft.ts:153)。

这些是现有可操作对象的本地身份，按 §2 统一；不因仅在 renderer 中就继续使用计数器／UUID。page 重用、history window/aperture、加载方向与 cut 保持；文件 tab 导航、dedupe、关闭和异步失效保持；导入重试、图片 ready promise、批注节点编辑和 undo 继续关联原对象。

批注／导入／图片草稿 ID 仍不写入最终 prompt；本地 draft-asset 不冒充后端 image/blob。真实图片发布的 image: ID 和 workspace/content 绑定不变。不把 object URL、DOM/SVG ID、React 局部 key 或文件临时名强制变成业务对象。

### ID-12：MCP doctor 的虚构 session 参数

位置：[cli.py](src/pulsara_agent/cli.py:1475)、McpHostSupervisor.session_id。

诊断不凭空建会话或写库；supervisor 不保留无实际用途的 session 参数／存储字段；诊断状态仍由原 owner 管理。若复查发现生产消费者，必须先补全该真实依赖并在现有 caller session 上传递原 canonical session，不能擅自创建一个随机“诊断 session”。

真实 Session／Work／Host 不因删去冗余参数而合并或改变归属。

## 4. 复合引用和 fingerprint：明确用途后收口

### ID-13：Skill UI 引用和 MCP runtime server target

位置：[session_controller.py](src/pulsara_agent/desktop_app/session_controller.py:3155)、[只读 Skill](src/pulsara_agent/desktop_app/session_controller.py:3674)、[mcp_adapter.py](src/pulsara_agent/plugins/mcp_adapter.py:188)、[capability_query.py](src/pulsara_agent/conversation_kernel/capability_query.py:248)。

workspace Skill 的 root:name 已被 UI 回传用于精确查询和启停；source:name 是 editable=false 的 session 只读 winner。user inventory 没有这个 id 字段，仍使用原 name/path/root 和 path-based 操作。MCP 的 runtime_server_id 是可操作 runtime target。这些引用按 §2 统一，不保留两套可接受输入。

同时保留真正的自然名称和路由：Skill name、skill_path、MCP 配置 server_id、插件声明 local_server_id、plugin 名、原 framed runtime_source_key 及 provider-qualified 名称仍是原事实，不能因 target 改用短 ID 而改名、改变覆盖／冲突规则或改变 provider 工具顺序。framed_plugin_mcp_server_id 的结果若仍用于原 namespacing，应明确作为 runtime_source_key，不继续冒充公开 canonical target ID。

生成与反向精确解析均复用当前 owner 的库存和原自然键事实，不加 alias registry 或持久映射。`runtime_server_id` 匹配当前 session 库存的同一原始 runtime_source_key；不能把它升级为某一物理连接 incarnation 的永久引用。workspace Skill 的 root 只取当前 agents/pulsara 字面值，只读 winner source 只取 workspace/user/plugin/bundled；不加入更细 root_kind、plugin ID、路径、workspace ID 等新维度。user path-based 操作不变；workspace 请求边界和“恰好一个精确候选”规则保持。

MCP 字段合同固定如下：

| 目标／owner | 字段含义与处理 |
|---|---|
| MCP_SERVER target | runtime_server_id 是新 canonical mcp-server ID；在原授权 session inventory 内逐项重算精确匹配唯一 runtime_source_key |
| MCP_TOOL、MCP_RESOURCE、MCP_RESOURCE_TEMPLATE、MCP_PROMPT target | server_id 仍是原自然 runtime_source_key；tool_name/uri/uri_template/name 原样保持，不传新 mcp-server ID 进入这些字段 |
| MCP_CONNECTION／安装来源 | scope、source_kind、plugin_id、配置 server_id 仍按原安装定位与 provenance；未采用的安装不伪造 live server |
| 标准 MCP operation／supervisor／candidate／lease／executor | 使用原 server_id／runtime_source_key 和原物理 generation，不改变 SDK 参数、连接选择或调用次数 |
| parent-child/source 关联 | 先把 parent canonical target 精确解析为原 runtime_source_key，再按现有 CHILD_KINDS、scope、plugin/local 事实 join；禁止把两种字符串直接等值关联 |
| 错误／诊断／presentation 中发布的 MCP_SERVER target | 同一边界生成 canonical target；自然 server 字段仍仅作原诊断事实 |

同步 directory、capability_query 的详情、parent/child、runtime_target、diagnostic 分支、tool_runtime 错误 target 和 UI/模型 schema。只对改型的 runtime_server_id 拒绝旧 composite 形式，不能拒绝仍为名称的管理／远端 server_id。不同 scope 覆盖、plugin/local 同名、provider 名冲突、重连和 unassociated 状态的选择及展示保持。

### ID-14：capability/source identity fingerprint

位置：[capability/contracts.py](src/pulsara_agent/capability/contracts.py:65)、[registry.py](src/pulsara_agent/capability/registry.py:91)、[planner.py](src/pulsara_agent/capability/planner.py:237)、[tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py:2209)。

直接使用完整冻结值：CapabilitySourceRef 的 source kind＋stable_source_id，CapabilityIdentity 的 kind＋source kind＋stable_source_id＋stable_name。移除只为查找／DTO 自校验携带的上述重复字段，不新增 capability/source 对象、registry 或短码。

不能假设消费者已携带完整值。ToolCapabilityVersionRef、已安装 predecessor/route/gate、NewMcpToolRef、prepare/allowed/retire/resolve 及 executor 关联都要在原 owner 生命周期中透传同一完整 frozen identity，直接精确比较。不能凭 provider_name、当前库存或新短 ID 反向猜测旧身份；保留 semantic/schema、policy、route、consumer、session/scope/epoch、runtime generation、私有 authority、FULL 交付激活和物理 permit 全部原校验。immutable 值按既有结构共享引用，不新增跨层序列化副本／长期保留 owner。

真实跨边界 digest 保持逐字节原协议：在唯一原排序、framing／聚合／source-lineage／prefix 边界，从上述 typed 字段按原 domain 标签、字段名、嵌套结构、规范 JSON 与完整 SHA 字符串即时重算。具体包括 capability-source-identity:v1 的 `(kind, stable_source_id)`，capability-identity:v1 的 `{kind, source_kind, stable_source_id, stable_name}`，以及调用它们的原 aggregate 规范输入。不是仅保证“语义大致相同”，不把 typed tuple 直接替换进旧 JSON。

snapshot/fact/native projection 排序仍使用原 `(kind, identity digest, fact semantic digest)` 等精确排序键，适配器 function_tools 也保持原顺序；不能对新的 typed 元组改排序。registry planner framing 对该真实完整摘要表示仍按原字节计费，原边界和逻辑选择保持；实际 wire/内存的物理限制按 §2.3 核验。原 digest 只在必要边界即时存在，不作为另一 ID、别名或重复 DTO 字段复活。验证需包含 typed 名称顺序与原摘要顺序相反、多来源和临界预算、重连 stale ref、完整 tools 字节及 source digest 固定向量。

此项不是全仓 fingerprint 清理授权，只处理查明的身份查找重复值；所有真正的 policy／replay／permission／durable confirmation verifier 不截短。

### ID-15：context source 固定槽位和授权来源包装

位置：[context_sources.py](src/pulsara_agent/conversation_kernel/context_sources.py:1198)、[model_input/contracts.py](src/pulsara_agent/model_input/contracts.py:639)、[interaction.py](src/pulsara_agent/conversation_kernel/interaction.py:555)、[_repository/tools.py](src/pulsara_agent/conversation_kernel/_repository/tools.py:1245)。

`context-source:<kind>` 与闭合 source kind 一一对应，用于候选唯一性、选择和排序，也进入普通、memory、compaction 三个 context-source-candidate:v1 的 semantic payload 和独立校验。删除这份冗余字符串 instance ID，直接使用现有 typed source kind 作为同一槽位键；保留“一种 kind 至多一份候选”、原降级／预算选择和稳定 tie-break。这些真实边界和独立 verifier 从 typed kind 局部重建完全相同的 context-source:<kind.value.lower()> 值，以及原 JSON 字段名、嵌套和序列化，所得完整 digest 必须逐字节一致；不再把该字符串携带为 DTO 实例 ID。compiler 的原排序先比较 kind，且 kind 唯一，因此移除末级冗余键不能改变顺序；不得借删字段重建安装中的 epoch。

人工确认授权分支（authorization_kind = 'human'）直接携带和比较同一个 canonical decision: ID；工具策略的权限分类／原因仍使用原 closed typed 值，不为一个原因创建 permission 对象。重试冻结授权、批准对象、scope、nonce、失效和跨请求不复用授权的规则保持，不增加重新 gate。

## 5. 伪身份和展示层包装

### ID-16：插件预校验／脱敏的全零 package ID

位置：[package_core.py](src/pulsara_agent/plugins/package_core.py:90)、[management.py](src/pulsara_agent/plugins/management.py:1411)、[plugins/contracts.py](src/pulsara_agent/plugins/contracts.py)。

预校验不注入全零或随机假 package ID。提取同一套无授权的声明解析规则，输出纯声明验证结果；没有真实安装实例时不构造可执行 PluginHookSourceIdentity/provenance、不授予 trust。真实安装仍按原 package_store 路径保留受控 source descriptor/重校验、复制到 stage、重新打开 stage、用真实 package/scope/lifetime anchor 绑定并重新解析 Hook，进行原语义摘要对照、paired bytes 和 race 检查；不能把预校验输出的临时 provenance 变为运行定义。native/imported 两条入口、所有 Hook 声明规则、路径限制、环境替换、信任和原失败诊断均保持。声明解析不能少一条规则，也不能提前读取／执行本来只在绑定阶段发生的内容。

脱敏不再把假 package ID 装回 canonical 领域 DTO。raw domain DTO 始终保留真实 ID／原 None、原 disposition/cut/诊断，由原 owner 完成 settle/CAS；仅在原公开 CLI／HTTP／UI／日志／模型工具结果／交互表单等输出边界投影为闭合 PublicPackageId：

```text
{visibility: VISIBLE, value: package:<canonical payload>}
| {visibility: WITHHELD}
| null
```

公开输出先脱敏动态数据，再组装固定协议。字段名和闭合枚举由各业务 owner 的代码生成，保持原样；即使实际秘密恰好是 `package`、`visibility`、`WITHHELD` 或 `APPLIED`，也不改写这些固定协议。ID、名称、路径、正文和其他动态值仍按捕获的完整实际秘密处理。它们不会因为与某个协议字段或枚举同名而取得豁免。公开 package ID 继续仅由 `public_package_id` 投影。

各 owner 显式投影自己的闭合结构；嵌套结构交给对应 owner，任意外部 JSON 则整块检查键和值。env、header、工具 schema 等外部数据里的 `package_install_id`、`status` 等名字仍是动态键；不建立通用键名白名单。外部键被脱敏改变或发生冲突时，只省略该数据块，并在直接所属记录的 `withheld_fields` 中列出代码定义的字段名。省略不等于空对象或原领域的 null，不向模型／编辑器伪造替代内容；其余可展示字段及已完成操作的 disposition、cut、诊断、凭据写入和结算均保留。编辑器不得将未展示的配置当作空配置保存或测试。

组装完成的协议不再经过整包 JSON 或整段序列化文本脱敏，不保留旧路径作为兼容／兜底。CLI 在原输出门禁内观察最新凭据、合并该操作已观察的轮换前值，投影动态数据后直接格式化并打印。模型表单保留 raw 冻结观察与原 guard；Hook 审阅在准备时保留原 scrub_set，展示时再合并当前观察。查询、操作结果、OAuth、用户库存和诊断日志使用同一边界，不引入持久化结构或新的授权入口。

VISIBLE 只可携带真实且可展示的原 ID；WITHHELD 表示该字段存在但秘密规则禁止展示；null 仅对应原领域字段允许且实际为 None，不表示 withheld。wire 使用同名小写 visibility 字段和上列大写值，禁止将 union 对象 String()、设空串或塞入原强类型成功结果重建。公开动态值不得包含已捕获的完整非空实际秘密值；代码生成的固定协议按上一段保持原样；当完整秘密等于 package ID 或是 ID 的子串时标为 WITHHELD。仅与秘密共享部分字符不扩大脱敏，其他字段沿原 scrub_set 规则；不重新分配已提交对象 ID。

公开投影覆盖全部出口，不仅是 CLI／HTTP snapshot：install、already-present、ACK_UNKNOWN、enablement/STALE、removal、inspection、version、GC，以及模型 query_capabilities/manage_capability 工具结果、用户 capability interaction 表单的 plugin/expected_current、OAuth authorization 结果／事件中的所有 package-ID 字段。每种原结果的存在性、未知事实、disposition、cut、实际写入与诊断保持。

CLI、HTTP、模型输出序列化、renderer、capability-interaction-editor、mcp-authorization-result/parser 和 OAuth 状态消费者都显式处理 PublicPackageId；VISIBLE.value 是原真实字符串，可由模型复制到原字符串型工具输入，工具输入不能接受输出 union 对象。WITHHELD 不作为字符串、可操作 target 或 caller guard，也不能用于完成匹配另一个安装实例；但不能因显示引用 withheld 丢弃原成功结果、授权结果本身或已有 interaction，原内部真实身份／guard 仍由原可信 owner 管理，公开 projection 不能反向成为 authority。

分别保留各入口原有 guard 合同，不增加必填参数：

- 原 UI／CLI／API 要求 caller 显式提供 expected_package_install_id 的路径，仍要求同一真实可见 guard；WITHHELD 不可转空串、假值或通过省略变成无条件请求。
- 原 manage_capability 等允许省略 expected_package_install_id 的模型入口仍可省略，由原同 scope owner 进行原检查／观察，在原 prepared/confirmation 中冻结真实 current ID／expected_current，走原用户确认、权限和精确 CAS；不要求模型先 query 到 VISIBLE ID，不新增 gate 或后端 override。已显式提供的 stale guard 继续按原条件拒绝。
- 可选公共参数与内部冻结 guard 分开，REMOVE_PLUGIN 的 instance observation、其他 action 的 component/inspection、Hook trust、配置和 OAuth 各自保持原事实／scope／lifetime 比较。公开脱敏投影不再被内部管理路径回读为 current identity；不改成“所有 withheld 成功”或“所有 withheld 拒绝”。普通 action 的原状态机和提交结果必须逐 action 验证，不以偶然伪 ID 或 DTO 构造错误建立新产品合同。
- 用户确认表单仍通过原 interaction/prepared 关联执行，内部 raw expected_current 不泄露到公开表单；OAuth 自动状态展示只在原关联条件具备并可安全展示时使用 VISIBLE.value 做字符串匹配，WITHHELD 保留原授权事实和普通状态查询入口，不冒认另一个 package。需要精确 caller guard 的后续操作与内部已冻结操作按各自上两条处理，不增加可操作别名、授权 fallback 或新的自动行为。

普通可见路径的全部行为不变；脱敏路径保持原秘密保护和不能凭假 guard 变更真实实例的边界。覆盖真实 ID 等于秘密、秘密为 ID 子串、一般完整 key 等情况，确保提交成功不因输出 DTO 构造变成异常、ACK_UNKNOWN/STALE 不丢事实、后续 CAS/OAuth 不放宽，也不泄露秘密。普通可见、实际秘密命中、可选 guard 省略、显式 current、显式 stale、所有 plugin action／表单和授权结果分别有端到端断言，不把一个 action 的成功／失败推及所有 action。

### ID-17：live 和 process 的 UI 包装／缺字段 fallback

位置：[runtime-adapter.ts](frontend/lib/runtime-adapter.ts:3933)、project/applyProjectedLiveResults，以及 [workbench-view.tsx](frontend/components/workbench-view.tsx:1362)。本项不改变现有 best-effort live 行为，不新增 gap、清空、重连、恢复或通知。

1. Message 的视图身份与 canonical 操作目标分开。view key 使用 closed view role＋typed source identity；canonical entry 只有实际存在时才进入 sourceEntryId 等操作目标。live:原ID、live-result:原ID、entry:…:process 不再作为可操作 message/entry ID。纯 React/DOM key 可稳定序列化结构键；不让它反向授权或冒充 entry。
2. 普通/final/process 共享实际 canonical entry 作为来源，但保留不同 view role，所以同一 entry 的 final 与 process 不合并。live draft 使用原 draft/turn/generation 事实关联；detached result 可能尚无 canonical entry，创建原 LiveToolResult 时分配 R(view) 并随原对象复用，保留 attempt/proposedEntry/draft/generation/block/assistantEntry/providerToolCall 的原多字段匹配、后到合并和文本结算。不凭空造 entry，不新增 registry；已有 LiveToolResult Map 的键切换不重分配 view ID。
3. role-aware 改造所有把 Message.id 当 source 或 key 的消费者：最新 aperture 判定、canonical context/hydration、sourceEntryIds、assistant＋tool 匹配、pending/committed 交接、HistoryMessageHost、onFork、历史定位/anchor、disclosure choices、orderedHosts 和 compaction 插槽。已有 source 时按原范围操作，无 source 时保留原不可操作占位；不把“局部视图 ID”发回 canonical API。
4. 正常 producer→assembler→live payload→v3 serializer→TS 解码的不变量须有证据：provider tool_call/block 标识原样保留，本地 block/entry/generation 由原 owner 提供。合法 DELTA/END 即使 START 已不在 advisory ring 中，仍按真实关联身份维持原显示和累计内容，不能要求 START 必须先到。
5. 原 crypto.randomUUID／reasoning:N／revision:N fallback 分支不改成错误或 gap。保持原触发条件（含 null/undefined 与空字符串差异）、局部位置／revision 相关性、原累计正文和原推进游标：用 typed 槽位／数值作为内部关联事实，需要实际分配临时显示对象 ID 时在原对象创建处调用 R(view)／R(block)，并由原数组／Map 对象保留。删掉私有 UUID／伪 ID 编码，不删除原可显示的 fallback 内容。不要把缺字段事件升级为 server LIVE_RING_OVERFLOW。
6. 同批合法前后帧、canonical 提交和 settlement 按原顺序继续应用，已匹配 draft/result 和未提交 bytes 不清空。真正由服务端报告的 LIVE_GAP/LIVE_RING_OVERFLOW 仍沿原处理，不能声称 canonical snapshot 能恢复丢失的未提交流。覆盖 initial live hello 与后续 observe，以及 partial/late/ring overflow 区别。
7. TODO item 的 run＋ordinal 是原有序列表项位置，保持 typed 位置／局部渲染 key；不新增任务实体、持久行或交付合同。

## 6. 接受、schema、数据库和示例的剩余缺口

### ID-18：统一校验及模型 schema

位置：[ui-preferences.ts](desktop/src/ui-preferences.ts:6)、[presentation-targets.ts](frontend/lib/presentation-targets.ts:39)、[memory/targets.py](src/pulsara_agent/memory/targets.py:22)、[artifact 工具](src/pulsara_agent/tools/builtins/artifact.py:295)、[builtin_catalog.py](src/pulsara_agent/capability/builtin_catalog.py:455)。

- 删除领域代码自写的 session/cell 宽松或重复正则，调用适用平台共享 validator；保持原 preference key、文件位置和可丢弃性质，不因 key 含有 session: 就给 preference 另造 ID。
- PRESENTATION 与 open_in_pulsara 的各字段按实际 kind 统一：PROCESS.process_id=process，MCP_SERVER.runtime_server_id=mcp-server，AGENT.agent_id 与 root_session_id=session，CELL.cell_id=cell，host_session_id=host；PANEL 及条件性 root/host 字段沿原分支验证。保持原整批 target 格式失败与原逐条资源不可用／URL 错误隔离，不能改成逐条过滤或新增 authority。
- memory/artifact schema 使用统一 pattern 和允许 kind，执行入口在解引用前严格验证；read/export 在非法格式、错 kind 时都沿原参数错误载体结算，不向用户冒出未处理 ValueError。
- 不增加 prefix lookup、自动修正尾位、旧值转换、跨 owner 搜索或默认放行；合法但不存在／不可见的 ID 仍走原不可用合同，不泄露其他 scope。
- 新 schema 仅在既定 epoch 安装边界生效，不能在当前流式请求中改写已冻结 tools。

### ID-19：tool_invocations 和 output_artifact 的 clean-v0 约束

位置：[ToolInvocationSource](src/pulsara_agent/conversation_kernel/tool_invocations.py:29)、[_repository/tools.py](src/pulsara_agent/conversation_kernel/_repository/tools.py:179)、[baseline.sql](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql:632)。

正常 invocation 已由 D(call, …) 生成，正常 artifact 已由 D(artifact, …) 生成；问题是 DTO／SQL 没有完整守住格式。对接受边界的 invocation_id／关联 turn 等本地身份使用共享验证，tool_invocations.id 添加严格 call: CHECK；tool_results.output_artifact_id 在原非空情况下添加 artifact: CHECK。保留原 NULL、AVAILABLE/INCOMPLETE/UNAVAILABLE、PK/FK/UNIQUE 和所有结算关系。

不校验或改写外部 provider tool_call_id 的 canonical 形态；它与 Pulsara 的 invocation_id 是不同职责。同步 clean-v0、实际约束清单与相关测试，不创建在线迁移链或冗余关系。

### ID-20：随附 Skill 示例与相关文档

位置：[workflows.md](src/pulsara_agent/bundled_skills/pulsara-code-mode/references/agent-orchestration/workflows.md:69)。全 b session 和全 c queue 的末位不满足 padding bits 规则。

示例通过共享层的确定测试向量产生合法值，并明确实际调用复制工具返回的目标；不能教模型自行计算、补齐、截短或按名称猜测 ID。更新本轮活跃规格和当前提示词／Skill 的旧编码说明，清理当前活跃指导中要求使用旧 MCP ref HMAC／分配 monitor 身份的陈旧说明；明确要求删除 monitor 的现行条款保留，历史验收证据不改写为新实验。

已完成的 mcpref 保持 R(mcpref)、精确有效绑定复用、FULL 交付后激活、退役后重新分配与原 scope 校验；不得回退 HMAC／旧下划线格式。

## 7. 经核实应保留的非对象身份值

以下保留理由是当前实际用途，不是沿用历史例外。不得把这一表扩展成新的 ID 豁免名单；新发现必须给出生成者、验证者、消费路径及独立职责。

| 值 | 已核实职责 | 本轮边界 |
|---|---|---|
| 文件预览 read_token | 32 随机字节 bearer credential；GET 持有 token 即可读取活跃预览内容／资源 | 保留强度、URL 验证和撤销；不改为 128-bit ID，不新建 preview ID／token 双层注册表 |
| OAuth state、access/refresh token、terminal launch capability | 认证、授权或协议秘密 | 由原安全 owner 生成／核验；不改成普通对象 ID |
| MCP secret-generation HMAC、Hook trust digest、permission／Plan 确认摘要 | 变化检测、审批承诺和完整确认 | 完整验证边界保持；不因 MCP ref 去 HMAC 就一并删除 |
| provider/MCP SDK 原生 call/item/response/session/request ID | 来自外部协议的关联与 replay | 原值保持，不加 Pulsara 前缀；Pulsara 自合成部分不借此豁免 |
| content digest、revision:52位、source/prefix/replay digest | 内容完整性或真实兼容边界 | 不截短、不改为对象 ID；唯一真实 owner 计算 |
| server/plugin/tool/model 名、skill_path、URI、workspace 路径 | 配置／外部声明自然键或定位事实 | 保留原名字与规范化；公开的自有可操作引用按 ID-13 处理 |
| memory_domain_id 的既有配置值、ctx:global | 宿主提供的分区域和全局作用域常量，当前没有独立域对象分配 | 不建 domain 表／随机 ID，不改隔离键、跨库语义或全局 memory 作用域 |
| builtin descriptor 的 provider_kind:name、route-wire:<wire_api> | 已核查为固定目录标签／compatibility 输入，未发现独立对象 lookup 或调用 | 保留原标签和完整兼容摘要，不把它们注册成对象；若找到真实可操作消费者必须另补本轮条目 |
| context source kind、revision／ordinal／sequence | 闭合槽位或已有数值顺序 | 保持原类型；冗余 instance ID 按 ID-15 删除 |
| 分页 cursor | 冻结过滤条件、排序锚点及原认证状态 | 不改成短 ID，不建 cursor registry，不改每页授权、失效和分页 cut |
| blob storage identity、Electron partition 名、临时目录／staging 文件名、object URL | 平台／存储路径命名 | 不冒充业务 ID、不加对象表；平台 owner 和生命周期保持 |
| 纯 DOM/SVG/Mermaid/toast key | 局部渲染标识 | 不进入业务协议；可操作文件 tab、草稿资源、历史 page 不属于此行 |

## 8. 文件所有权与实施顺序

后续实施先确认唯一 owner；共享核心文件不并发修改。建议按以下依赖顺序落地，最终是一条新路径而非阶段兼容：

1. 共享身份模块与 closed kind／测试向量 owner：Python identity、两套 TS 构建、严格校验。只提供编码和校验，不接管领域对象分配。
2. 领域 owner：BrowserOwner/renderer、CodeModeHost、desktop runtime、MCP supervisor、历史投影／读取、TODO live。各自负责原生命周期和相关性，统一层不授予权限。
3. Capability owner：保持实际名称、库存、工具排序和 provider prefix；收口 runtime target、Skill 引用和内部 typed 身份查找。
4. Storage／protocol owner：同步已受影响的字段、clean-v0、序列化、精确读取、取消／错误结果和固定路由；不存在的对象不补行。
5. UI／提示词 owner：消费同一身份，显式区分 view role，清理伪引用／示例；不增加产品说明、按钮或新流程。
6. 集成 owner：按 §9 证明新格式和语义不变，完成所有生产／测试／当前文档的旧路径删除，再交叉审阅。

不要以“先上线新生成器，旧读取以后删”作为步骤；以上是依赖分工，不是允许双读双写。身份 kind 的增加不改变事件／subject／guard／relation／job 数量。

## 9. 验证与验收

### 9.1 必须有的行为证明

| 场景 | 必须证明的结果 |
|---|---|
| Python／TS／Electron 编码 | 共享 canonical 向量、Unicode D 输入、末位／大小写／长度／错 kind 拒绝；平台之间不复制独立算法 |
| R 碰撞、D 冲突 | 原 owner 可枚举占用集合内未发布 R 冲突重抽；已发布不换号；跨 owner／退休值不新增注册表；D 完整事实冲突明确失败 |
| 多 tab／多 presentation | 原标签复用、两个 native 实例隔离、过期 layout/hide/close 不影响新 owner；原资源释放完整 |
| HELLO 与 live 投影 | client_instance_id 不新增认证／去重／互斥；初始hello及observe、正常／缺字段fallback、无START的合法DELTA/END、late settlement及真实ring overflow保持原显示／游标／通知 |
| worker 请求与准备结果 | 并发 prepare/call 全为独立 request: ID；返回 request_id 复用 prepare，旧 id 不接受；sequence 只按 call 递增；resume 之前不派活，原请求与回包精确关联；prepared/finally 与资源释放保持 |
| retain 并发／关闭 | 各 future 按原请求准确完成；Cell 关闭后资源和 pending 全部原样结算，不影响已接纳副作用 |
| inspect-quit／OAuth 交错 | 两方向请求相关性、原超时和管道断开行为不变；OAuth state 不被替换或误关联 |
| MCP 重连／supervisor 重建 | 原 candidate/slot/permit 不跨 owner 接受；connection generation 不被改成随机 ID |
| MCP 工具与标准操作 | process、MCP、无身份三分支及process原事件字节；短 ID＋完整 verifier 的原事务 FULL/NONE/CONFLICT、提交成功ACK丢失、同短ID不同完整事实冲突；两条 remote_identity 路径、超长名字、取消前／常规／错误结果全覆盖；原参数、调用次数、工具名和副作用结算相同 |
| reasoning selector 与碰撞 | 新 reasoning-block kind、显式 ordinal、普通块／reasoning 分支互斥；无关 ID＋损坏 replay 不额外解析；投影与未先投影的直接读取都拒绝两 ordinal 同号，缺失／损坏结果保持 |
| 实际字节边界 | snapshot/history/context 的阈值前后按真实新 wire 大小计量；允许每页条数/临界 exhausted 随表示变化，逻辑 cut／顺序／完整性不变；不保留旧编码虚拟计量 |
| reasoning 完整读取 | 重复读取、跨页、导入／fork、真实 provider reasoning 类型与 ordinal 保持；错误 block 不被反解回退或跨 entry 读取 |
| 历史分段与搜索 | 相同 cut/输入对应相同 segment／document ID；QA/QAQ、顺序、相邻页、tie-break、stale publish 和无重复／遗漏保持 |
| TODO live | 同 run/revision 同 ID、下一 revision 不同；两身份字段相等、generation 不变；仍是 advisory，无 canonical 写入或模型唤醒 |
| page／草稿／文件 tab | 复用不换号；异步晚到、关闭、重试、undo/redo、重复批注和图片导入不混对象；最终 prompt 不带草稿 ID |
| Skill／runtime server target | 对同一已有自然键精确命中原对象；scope、覆盖优先级和 provider-qualified 名称不变；旧 composite target 不再接受 |
| Capability/source 收口 | version/predecessor/ref/gate 全量身份透传，原 digest 标签／JSON／字节向量、反序多源与临界 framing；完整事实比较、DIRECT/META 选择、预算选择和确定顺序不变；已安装 SYSTEM/tools 字节一致，messages append-only |
| 插件预校验／脱敏 | native/imported、source/stage重解析/paired bytes及替换竞态；install/already-present/ACK_UNKNOWN/STALE/inspection/version/GC、CLI/UI显式guard、模型原optional guard省略／显式current／stale及OAuth表单和工具结果；完整秘密相等／子串case；全部原声明规则继续验证；不产生伪安装实例、额外 trust 或执行权；结果不泄露秘密、不返回假的合法 package ID |
| 严格接受和 clean-v0 | DTO、schema、实际 SQL 约束和错误载体一致；合法无权／不存在仍按原 unavailable；非法 export 无未处理异常 |
| 被保留的非 ID | 预览 token 原认证／撤销路径、外部协议原值、摘要验证、cursor 分页、权限 gate 与真实副作用保持 |

### 9.2 验证执行边界

使用仓库根 uv 管理的 `.venv`。按改动运行聚焦测试、真实 PostgreSQL clean-v0 集成、renderer/Electron 类型检查、构建和打包检查；尤其确认共享 TS identity 源文件确实进入两个产物。数据库重建前通过现有配置 owner 核实本地可丢弃目标。

测试要保护上述产品语义，不只检查正则，也不把外部 provider ID 硬改成统一 ID 来刷绿。旧编码断言删除或替换为新合同；仍保护排序、权限、副作用、生命周期的失败不能按“旧产品”删除。

身份和 provider 边界使用实际生产 owner／适配器及受控输入验证，保留输入、输出、模型工具 schema、旧新排序元组和失败详情。需要真实配置／provider 验证时仅通过 require_pulsara_home() 与 LocalSettingsStore 读取已保存配置，遵守只读、局部覆盖和实际秘密脱敏边界；不添加环境 key 回退，不修改用户模型／网络配置。纯身份收口不新增模型选择或额外产品配额。

本轮不要求视觉验收／Playwright，也不以 Gallery fixture 代替生产协议证明。实施验收还应搜索源代码、schema、提示词、Skill、活跃文档和测试中的 UUID/hex/计数器／旧复合 ID，但不得把纯文本正文和历史证据做全文正则替换。

### 9.3 完成条件

- ID-01 至 ID-20 都有生产修改或按本条明确的非对象化删除结果；没有遗漏生产／消费分支、旧格式接受、legacy alias、影子 registry 或迁移兼容。
- §2 配方和共享校验在实际构建中使用，非对象值按 §7 的真实职责保留。
- §1 产品合同及 §9.1 行为矩阵通过，不以“测试全绿”替代 ROOT 对实际模型输入、UI 关联、权限和结算路径的审阅。
- 既有无关修改保留，配置／秘密未改变；只提交任务相关文件。

## 10. 文档审阅闭环

本稿由本次全仓 ID 扫描的 7 位 GPT‑6.1 Terra、xhigh 代理分别独立审阅，ROOT 逐项复核其意见并修改。审阅必须检查遗漏、错误归类、配方碰撞／相等维度、原生命周期、provider prefix、强凭据、排序分页、构建共享和可执行验证，不只核对“是否 26 位”。

任何真实产品歧义都不能用新增兼容、fallback、身份注册表或改变产品行为自行绕过。文档修订后送原 7 位审阅者逐一复核；每人明确给出可冻结结论，且全部阻塞意见关闭后才标记“冻结，尚未实施”。缺席、无答复或尚未关闭的意见不视为同意。不能把审阅通过写成代码实现或实验通过。


### 10.1 定稿审阅结论

以下均为第三轮全文／负责链路复核后的明确结论，审阅模型均为 GPT‑6.1 Terra、xhigh；所有阻塞意见已纳入正文并关闭。ROOT 已逐项复核并同意定稿。

| 原扫描审阅者 | 重点复核范围 | 最终结论 |
|---|---|---|
| id_audit_runtime | 运行时、协作、队列、授权、source 槽位与 live | 可冻结，无剩余阻塞 |
| id_audit_capability_config | Capability、MCP、配置、插件及完整确认 | 可冻结，无剩余阻塞 |
| id_audit_storage | 持久确认、clean-v0、历史／reasoning、查询与约束 | 可冻结，无剩余阻塞 |
| id_audit_frontend_api | renderer／Electron、API、view role、草稿及凭据 | 可冻结，无剩余阻塞 |
| id_audit_protocol_model | 协议、provider prefix/replay、live 和物理边界 | 可冻结，无剩余阻塞 |
| id_audit_scripts_docs | 诊断／脚本、插件预校验、公开结果与示例 | 可冻结，无剩余阻塞 |
| id_audit_crosscut | 全仓交叉、所有权、确认、排序与产品语义 | 可冻结，无剩余阻塞 |

上述结论仅证明规格审阅通过；实施验证仍须满足 §9，不得将其作为代码已通过验收的证据。
