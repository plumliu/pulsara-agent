# Pulsara 模型可见引用调查与改造建议

日期：2026-10-14。代码基线：Git `5df79ba9`。

状态：第一轮模型可见面调查，已按第二轮查漏修正。当前方向已改为统一业务身份 hard cut，图片可视化延期；本表不能单独充当全仓实施清单。完整范围与尚未闭合项见[第二轮全仓复查](PULSARA_IDENTITY_HARD_CUT_REPOSITORY_AUDIT.zh.md)。没有修改生产代码。

## 1 当前设计方向

具体规则现见[统一身份 hard-cut 实施规格](PULSARA_UNIFIED_IDENTITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)：128-bit业务 ID 采用类型词加26位 Base32；文件完整版本证明、cursor、MAC 和外部身份分别定界。该规格经独立审阅后冻结 v1，现已实施；验证结果见[实施记录](PULSARA_UNIFIED_IDENTITY_HARD_CUT_VALIDATION.zh.md)。下文现状表保留改造前的调查事实。

**用户已选择进一步规划统一业务身份 hard cut，内部业务接口与模型操作使用同一套身份规则。** 本文保留已确认的模型暴露事实，原先优先保留全部 canonical ID、再作模型短引用适配的建议已被替代。

统一范围必须由全仓实体、主外键、生成器、复制与生命周期清单确定。内容完整性摘要、文件版本事实、认证 token、cursor 和外部 provider 身份不能因外形相似而机械截短；无需模型使用的内部元数据仍应通过明确的公开投影减少暴露。

不新增全局别名服务、通用句柄注册表或新旧兼容协议。具体短 ID 的长度、唯一范围、生成与碰撞规则由冻结规格统一规定。图片命名方向保留，但混合展示设计已延期，不再作为本轮实施目标。

后续规格已冻结上述规则，不再采用模型别名方案。第8节中“不转指”的要求针对协议不得主动重解释身份；有限身份空间在完整删除后仍有理论碰撞风险，不能把该要求理解成无墓碑条件下的数学零碰撞保证，详见实施规格第3节。

## 2 调查范围与证据标准

三路覆盖资源与历史、文件与执行、runtime 与 provider 投影，追踪生产生成点、公开 schema、工具结果正文、最终 lowering 与消费入口。只有进入模型 schema、消息、工具结果或 runtime source 的值计为模型可见。

以下长度均为字符数，不代表 tokenizer token 数。普通工具 FULL 结果保留正文；COMPACT、REF_ONLY 等预算选择可能使某个字段在具体请求中不出现。“模型可见”表示存在确认过的生产投影路径，不表示每轮都发送。

本轮没有真实 provider wire 抓包、频率统计或 token 成本测量；不能据此宣称缩短一定改善成功率或给出精确 token 节省。外部 MCP、Hook、用户文件、终端输出和自由文本摘要可自行包含任意哈希，不属于 Pulsara 可任意改写的引用协议。

## 3 模型需要传回的资源与版本引用

| 字段 | 当前格式与长度 | 语义与使用方式 | 必须保留的边界 |
| --- | --- | --- | --- |
| `image_ref` | `sha256:` + 64 hex，71 | 图片 immutable bytes 的内容摘要；图片标签、历史占位提供，传回 `view_image` | 当前 session/workspace 合法已接受消息或 snapshot owner；完整内容校验；支持原有跨压缩、重启读取；不能凭摘要全局取 blob |
| `artifact_id` | `artifact:tool-result:` + 64 hex，85 | 由结果 entry ID 派生的保留输出身份；传回 `artifact_read/export`，不是内容摘要 | 当前 session/workspace、AVAILABLE/INCOMPLETE、完整字节校验；offset 相对于保留正文，不能恢复已丢失的源输出 |
| `memory_id` | `memory:` + 64 hex，71 | 由接受来源派生的事实身份；用于 get、basis 和 relation 操作；编辑 statement 不改 ID | memory domain、可读 context、basis 层级和事务时 ACTIVE 状态；语义去重摘要是独立字段 |
| 文件 `content_revision` / `base_revision` | `sha256:` + 64 hex，71 | 完整原文件 bytes 的版本；read/edit/write 或 stale 错误给出，edit 要求传回 | canonical path 对应的读取 observation、见过行范围、完整版本检查、staging 后逐字节检查、写权限与写后验证 |
| 子任务 `task_id` | `subagent-task:` + 64 hex，78 | 从 attempt 与 ordinal 派生的稳定 canonical 身份；wait/cancel/message、worker history、material 输入使用 | 当前 session 的精确任务归属、状态和依赖；重启、排队及完成后的已有生命周期 |
| 历史 `entry_id` | `entry:` + UUID hex 为38；`entry:` + SHA hex 为70；`steer-entry:` + SHA hex 为76 | 显式 search/read 返回并作为历史展开锚点；不是统一一种哈希 | 指定 session、ROOT eligibility、安装上下文的历史 cut、活动请求排除 |
| `session_id` | `session:` + UUID hex，40 | 显式会话搜索/内容读取的目标；不是哈希 | 当前 memory domain 与 workspace/session 授权，删除后不可读取 |

主要证据：

- 图片：[产生内容摘要](src/pulsara_agent/llm/input.py:86)、[provider 图片标签](src/pulsara_agent/model_input/lowering.py:117)、[owner 精确读取](src/pulsara_agent/conversation_kernel/prompt_storage.py:94)、[历史投影](src/pulsara_agent/conversation_kernel/session_text.py:20)。
- 附件：[ID 生成与读取 owner](src/pulsara_agent/conversation_kernel/tool_artifacts.py:493)、[完整性校验](src/pulsara_agent/conversation_kernel/tool_artifacts.py:546)、[工具结果公开字段](src/pulsara_agent/tools/builtins/artifact.py:320)。
- 记忆：[ID 生成](src/pulsara_agent/conversation_kernel/_repository/memory.py:57)、[公开读取结果](src/pulsara_agent/conversation_kernel/memory_tools.py:540)、[授权读取](src/pulsara_agent/conversation_kernel/memory/recall.py:266)、[basis 校验](src/pulsara_agent/conversation_kernel/memory_tools.py:448)。
- 文件：[版本生成](src/pulsara_agent/tools/builtins/filesystem.py:875)、[编辑检查](src/pulsara_agent/tools/builtins/filesystem.py:714)、[模型参数格式](src/pulsara_agent/capability/builtin_catalog.py:979)。
- 子任务：[ID 派生](src/pulsara_agent/conversation_kernel/subagent.py:870)、[list 公开字段](src/pulsara_agent/conversation_kernel/subagent.py:2338)、[任务精确查询](src/pulsara_agent/conversation_kernel/subagent.py:2422)。
- 历史：[显式读取投影](src/pulsara_agent/conversation_kernel/session_content.py:577)、[自动召回投影](src/pulsara_agent/conversation_kernel/history_excerpt.py:20)。自动 history recall 的投影不携带 session/entry ID，不能把内部 `HistoryCandidate` 字段算作自动暴露。

### 3.1 文件版本不能简单截断

现有 `_FileObservationSlot` 由 canonical workspace root 和 resolved path 下的进程局部 owner 管理，存完整 revision 与见过的行。相同完整版本合并可见行，版本改变则替换 observation；它不是一个独立的每会话 durable registry。

`write_file` 返回新 revision，但清除 observation，不能仅凭这串值直接获得后续 edit 资格。`replace_file` 同样要求当前读取及版本，只是不要求完整文件的每一行都已展示。

若未来使用 `revision:` 短引用，应把引用解析回读取时的完整版本事实后，再执行全部原检查。只把 `current_revision == base_revision` 改成短前缀比较会削弱契约。即使适配借用现有 observation，也必须防止一个旧短引用在 slot 被新版本替换后指向新内容；不能把“当前只有一个 slot”当成没有碰撞问题。

## 4 模型可见但不作为操作参数传回的字段

| 字段 | 当前形式 | 用途 | 建议 |
| --- | --- | --- | --- |
| 工具 `observation.source_turn_ref` | `sha256:` + 64 hex，71 | 工具结果所属回合，与 freshness 提示对照 | 保留来源区分语义，整体设计更简洁的模型表示；不能单独删一端 |
| `TOOL_OBSERVATION_FRESHNESS.current_turn_ref` / `immediate_predecessor_turn_ref` | 同上，前序可空 | 区分当前和前序捕获，避免把旧结果当当前真值 | 与工具结果的来源引用一起修改；不是权限凭证 |
| 子任务 `batch_id` | `subagent-batch:` + 64 hex，79 | 创建结果的批次关联 | 没有确认按该值调用工具的入口；先判断是否需要模型展示 |
| 子任务 `result_id` | `subagent-result:` + 64 hex，80 | 完成结果、依赖材料的来源身份 | 不因“无需传回”就自动删除；先保留模型确需的来源区分，再决定省略或缩短 |
| memory `relation_id` | `memory-relation:` + 64 hex，80 | get 与 relation 操作回包的关系身份 | 当前工具不按该 ID 操作；优先审视展示必要性 |
| `list_agents.tasks[].model_target.token_estimator.estimator_fingerprint` | `sha256:` + 64 hex，71 | 内部估算器身份，由完整 DTO 穿透到工具结果 | 明确的可减少暴露项；采用显式模型投影，内部契约保留 |
| `reload_capabilities.plugin_hook_sources` 的两个 definition digest | 裸64 hex，可空 | Plugin Hook 当前/已批准定义的持久 trust 校验，普通工具 FULL 正文可见 | 第二轮新增；保留内部 trust 检查，精简模型 reload 投影 |

证据：[来源引用派生](src/pulsara_agent/primitives/tool_observation.py:218)、[provider 工具外壳](src/pulsara_agent/primitives/tool_result_projection.py:236)、[freshness source](src/pulsara_agent/conversation_kernel/context_sources.py:2265)、[子任务 list](src/pulsara_agent/conversation_kernel/subagent.py:2338)、[result 派生](src/pulsara_agent/conversation_kernel/subagent.py:3477)、[memory relation 回包](src/pulsara_agent/conversation_kernel/memory_tools.py:599)。

`estimator_fingerprint` 的完整链路是 [TokenEstimatorFact](src/pulsara_agent/primitives/model_call.py:93) → [FrozenSubagentModelTarget](src/pulsara_agent/conversation_kernel/subagents/model_target.py:40) → [全量 model_dump 保存](src/pulsara_agent/conversation_kernel/_repository/subagents.py:635) → [list 原样返回](src/pulsara_agent/conversation_kernel/subagent.py:2353) → 普通工具结果 FULL 正文。这里需要精简的是公开投影，不需要改变估算器身份算法。

补充模型句柄：`list_agent_models.connection_id` 会被模型传回以选择调用连接，也应纳入身份范围；不能因其使用 UUID 而从改造清单中漏掉。`scheduled_tasks` 公开结果还包含关联 queue/turn/entry ID，不能只统计 task_id。

补充排除：`list_agents.material_refs` 是 `{task_id,status,result_id}`，没有额外 image/artifact 指纹；[构造处](src/pulsara_agent/conversation_kernel/_repository/subagents.py:551)定义了闭集。`model_call_binding` 只含 connection ID 和 reasoning，其中 `model-connection:` + UUID hex 是49字符 UUID handle，不是内容摘要。

## 5 分页与能力令牌单独处理

| 字段 | 编码和内容 | 当前生命周期及校验 |
| --- | --- | --- |
| terminal `output_cursor` / `retained_from_cursor` / monitor `baseline_cursor` | base64url JSON，包含 owner epoch、process、stream、offset 和 SHA-256 checksum | 当前 terminal owner/stream；校验归属和 offset，超出保留区间明确 GAP；checksum 无密钥，不是认证令牌 |
| `search_sessions.next_cursor` | base64url JSON，外层查询与范围，内层 keyset | live discovery；校验 caller、query、lifecycle、range；没有全局冻结 corpus snapshot |
| `read_session_content.next_cursor` | base64url JSON，v3，包含 cut、entry/event 位置、offset 和查询范围 | 每页重新授权，可依数据重建；不依赖过程内 cursor registry |
| `search_session_content.next_cursor` | base64url JSON，bm25.v2，含过程内 consumed_page 身份 | binding 不变；消费链丢失明确 CURSOR_EXPIRED，不承诺跨重启恢复 |
| `list_agents.next_cursor` | base64url 的 HMAC32 bytes 与 JSON；现有输入上限512 bytes | coordinator secret、session、排序和分页参数绑定；重建 owner 后旧 token 失效 |
| `scheduled_tasks.next_cursor` | 最后一条 scheduled task 主键，无 base64 包装 | 按 task ID 排序，传回续页；主键变化需同步分页规则 |
| late MCP `tool_ref` | `mcpref_` +43 base64url 字符，共50 | keyed token，绑定 scope、epoch、capability、policy 与 route；由原 owner 精确解析后调用 |

证据：[terminal 编解码](src/pulsara_agent/terminal_process/output.py:768)、[terminal 模型结果](src/pulsara_agent/conversation_kernel/tool_runtime.py:4344)、[历史 cursor 编码](src/pulsara_agent/conversation_kernel/session_content.py:66)、[精确读取续页](src/pulsara_agent/conversation_kernel/session_content.py:425)、[BM25 续页](src/pulsara_agent/conversation_kernel/session_content.py:733)、[子任务 cursor](src/pulsara_agent/conversation_kernel/subagent.py:2185)、[MCP token](src/pulsara_agent/conversation_kernel/mcp/meta.py:100)。

这些值不是可随意截短的长哈希。它们有的携带查询状态，有的具有认证语义。统一增加 `cursor:` 前缀也不会使它们拥有统一生命周期。

把它们改成几位短码通常意味着新增状态映射，可能使原本可重建的读取依赖内存，或者把原本易失的分页升级为持久恢复。两种改变均须独立产品决策。首轮图片引用改造不包含 cursor 或 MAC 截短。

## 6 不应列入此次清理的内容

- **Plan ID**：`workflow_id`、`interaction_id` 确实出现在存储 body，但最终 [PLAN_CONTROL 投影](src/pulsara_agent/primitives/tool_result_projection.py:142)只保留控制状态和问题答案；[lowering](src/pulsara_agent/model_input/lowering.py:658)执行该投影。它们不属于确认的模型复制负担。
- **provider 内部 fingerprints**：SYSTEM/context source、编译、continuity、tools descriptor、wire/source compatibility 等字段未随完整 DTO 自动发送。[runtime 外壳](src/pulsara_agent/model_input/continuity.py:187)有明确字段集。
- **Skills 与部分 Hook 查询**：模型 catalog/active skill、Hook list/inspect 使用显式投影，但这不能推广到全部入口。第二轮确认 `reload_capabilities` 会通过 `plugin_hook_sources` 暴露 `definition_digest` 与 `trusted_definition_digest`，两者为裸64位 hex（可空），同时包含 `pkg_` + UUID32 的 package_install_id。此前“Hook 摘要仅 GUI 可见”的结论不成立。内部 trust 校验应保留，可精简模型 reload 投影。证据：[host reload](src/pulsara_agent/conversation_kernel/host.py:1444)、[共享展示函数](src/pulsara_agent/hooks/presentation.py:14)、[工具结果](src/pulsara_agent/conversation_kernel/tool_runtime.py:3096)。
- **HTML visualization_ref**：当前是 [UI canonical 展示值](src/pulsara_agent/terminal_protocol/canonical_v3.py:1011)，没有已确认的独立模型读回工具；HTML 截图才进入普通图片引用链路。
- **terminal 异步观察的内部 digest**：[异步投影](src/pulsara_agent/model_input/lowering.py:696)过滤它们。普通 terminal ToolResult 的 cursor 则确实暴露，二者不能混算。
- **schedule 与导入文件**：`scheduled:` + UUID hex 为42字符，另有整数 revision；导入文件使用带 UUID 子目录的路径。它们不是需要做 SHA 截短的对象。
- **用户与外部内容**：文件中的 Git SHA、校验和、远程工具返回的标识，以及原始路径不能通过全局正则替换改写。

## 7 方案取舍的当前结论

| 方案 | 当前决定 |
| --- | --- |
| 直接截短全部摘要字符串 | 不采用；会混淆内容校验、身份、状态与认证 |
| 统一业务 canonical 身份 hard cut | 当前设计方向；接受重建已验证的本地开发数据，先闭合全部实体和生命周期 |
| 保留全部长 ID 加模型别名 | 不作为默认方案；避免长期双套身份与映射维护 |
| 精简模型公开字段 | 仍然必要；例如 estimator 和 Hook trust digest 的模型暴露，不需要为其新造短 ID |

模型 schema、输出、工具入口、历史、UI 和内部业务接口应一起切换。保留的完整内容摘要承担内部完整性检查，不作为第二套可操作业务 ID。每一类的最终选择以待冻结的 hard-cut 规格为准。

## 8 短引用需要先闭合的正确性

### 8.1 长度不能代替身份协议

16位十六进制只承载64 bit，4位和6位分别只有16 bit和24 bit。更短意味着更小的标识空间；类别前缀有助于区分资源，但不能消除同类碰撞，也不提供权限。

完整256-bit值可采用更紧凑的无损编码，通常仍会有数十字符；是否真的节省模型 token 必须按实际 tokenizer 测量。若要求十几字符，则必须明确它是有作用域的引用，并对碰撞、生命周期与持久性作出决定。

### 8.2 仅查询当前唯一前缀仍可能误指

例子：A 和 B 具有同一短前缀。最初只有 A 可见，模型记住短引用；后来 B 出现，A 被删除或移出授权范围。此时查询“当前可见的唯一匹配”会得到 B。即使每次都拒绝多条结果，旧引用仍可能被错误重解释。

因此不能只规定“查到两条时报错”。必须保证已发出的引用始终定位原对象或明确失效，永不转指另一对象；权限变化、删除、fork、重启与新增对象均需覆盖。返回更长引用只解决已检测的歧义，不自动解决旧短引用的重新绑定。

### 8.3 最小设计约束

- 解析先绑定资源类型和现有授权 owner；短引用不能成为访问凭据。
- 不以默认取第一项、全局扫 blob、猜测对象或字符串相似匹配恢复引用。
- durable 资源不能仅靠过程内 alias map 支持；过程内 cursor 也不能顺带新增 durable 续传契约。
- 若选择在既有 canonical owner 上增加公开引用字段，须说明独立必要性、唯一约束、分配碰撞、删除后复用及 fork 规则；它不是本文自动批准的改动。
- 若选择复用既有定位信息或无损编码，需展示完整往返与模型实际长度，不能只展示漂亮的短例子。
- 不增加总对象数或历史数量上限来保证“永远不会碰撞”。

这些是调查时的设计约束，具体算法与长度现已由独立审阅后的实施规格确定；不让所有资源为同一个长度目标增加机制。

## 9 下一步

上述设计已在[冻结的统一身份实施规格](PULSARA_UNIFIED_IDENTITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)闭合；后续经用户授权实施时，以该单一路径和验收要求为准。本文和[第二轮全仓复查](PULSARA_IDENTITY_HARD_CUT_REPOSITORY_AUDIT.zh.md)保留为代码基线的调查记录。

图片展示延期。不得继续按原建议只把图片当作模型别名适配试点，也不得为了统一格式顺带重写外部 provider ID、MAC 或分页状态。

## 10 实施验证要求

| 类别 | 验证重点 |
| --- | --- |
| 模型公开面 | 真实 schema、FULL/COMPACT/REF_ONLY、runtime source、历史与摘要路径使用同一格式；内部字段不漏出 |
| 输入往返 | 模型原样传回即可；错误类型、无授权、未知/歧义/过期引用明确失败 |
| 长期身份 | 冷启动、压缩、fork、删除、权限变化和构造碰撞后不发生旧引用转指 |
| 文件版本 | stale 拒绝、可见行约束、staging 竞态与写后验证维持原强度 |
| 分页与能力 | 若另行改动，保持既有 cut/GAP/过期/HMAC/scope/epoch 契约 |
| provider 连续性 | 新 SYSTEM/tools 在冷 epoch 或明确采用的 compaction successor 安装；旧 messages 不回写，新内容按 suffix 追加 |
| 文本保真 | 用户/文件/外部工具中的哈希、路径和自由文本不被全局改写 |

正式改造应比较代表性 provider 输入中的真实 token 数与引用调用成功率，尤其测试长历史里的精确复制。当前调查只证明暴露链路与契约，不充当性能收益证据。

相关文档：[HTML 与图片统一展示规格](PULSARA_UNIFIED_VISUALIZATION_PRESENTATION_SPEC.zh.md)、[仓库执行约束](AGENTS.md)、[已实施的 fingerprint subtraction 规格](archived_docs/PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、[已实施的文件版本编辑规格](archived_docs/PULSARA_CONTENT_REVISION_LINE_EDIT_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。
