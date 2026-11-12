# Pulsara 有序内容统一存储数据库 hardcut 草案

日期：2026-11-12。

状态：优先实施的结构草案，未实施。本轮只修改文档。本文确定单一存储路径、归属及删除边界；完整工具交付合同与[统一工具结果交付与分页草案](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)共同适用。物理承载及尚未讨论工具的完整值映射必须在实施中逐项闭合，不能以旧路径兜底宣称完成。

## 1. 目标与实施范围

数据库直接承载有序、交错的文字与图片，以及必要的结构化内容。用户输入、排队消息、steer、批注、助手正文与 commentary、工具结果、Cell 显式输出复用共同内容结构。前端、历史检索、原生工具消息、JS 完整业务值及导出从它投影，不各自保存一份正文。

数据库设计和存储实施优先，允许先停止 Pulsara 的正常使用、重建为新库，再继续讨论全部 native tool 合同并逐步实现消费者；不要求停用期间维持应用可用。具体顺序及恢复条件见第 10 节。[会话工具草案](native_tools/session_history.md)已确定的 search_sessions、QA／QAQ 摘录、Q₁ Target 和可选原文展开保留；read_session_content 的新合同在数据库基础明确后继续讨论，不必等全部消费者实现才恢复讨论。blob 阈值实验单列，视频独立采样、画布和 motion 产品修订不纳入。

本次覆盖当前 clean-v0、存储生产者、读取者、前端实时／历史接续、模型投影、fork、检索投影、导出及 GC。存储统一保持既有工具权限、workspace 边界和历史范围；本次明确增加的图片合法来源是已消费并进入会话的 Cell steer 图片，具体资格见第 5.2 节。不得据此开放其他来源，也不替尚未讨论的工具猜测新参数和业务字段。

不建立独立正文实体、正文 ID、图片注册表、分页实体、图片绑定标记、交付回执、恢复任务或内部版本标签。不保留旧会话兼容、双写、双读或旧正文格式的兜底解析。

## 2. 唯一内容与业务归属

### 2.1 表的职责

| 表 | 实施后的职责 |
| --- | --- |
| transcript_entries | entry 身份、session/workspace、turn、顺序、来源和必要控制关系；不再保存另一份正文或块清单 |
| prompt_queue_items | 排队、投递方式、来源、消费／取消／拒绝状态及 consumed_entry_id；不另存正文 |
| content_parts | 共同有序内容；由现有 assistant_message_blocks 扩展并替换，不保留平行旧表 |
| tool_results | 工具调用与执行关联、观察事实、已声明的非 content 业务字段和必要失败事实 |
| blobs | 不可变字节，包括图片资产及选择外置的较大内容；复用现有存储 owner |
| tool_invocations / tool_execution_attempts | 原调用、执行、权限和效果结算；不被正文表替代 |
| provider_assistant_replay_fragments | 必要的 provider 精确回放及思考载荷，保持独立协议职责 |

完整业务值可以由业务行和有序内容行共同组成，不要求所有字段塞进一个 JSON。禁止额外保存同值的整份业务 JSON、拼接全文、模型预览或 JS 专用副本。临时编码和投影可以在内存中产生，不成为第二份正文权威。

不重复存储针对同一次内容被多个权威位置重复维护，不要求不同业务发生只要文字相同就合并。用户再次发送同一句话、脚本显式输出选定文字、不同会话接受相同材料，都可以是独立的内容出现；图片和适用 blob 可复用字节，不能因此合并来源或顺序。

查询返回的 QA 摘录、文件匹配行或历史阅读正文，属于本次工具结果，即使文字与原会话或文件相同，也按本次业务范围完整保存，不强行改为对原文位置的引用。禁止的是同一结果同时维护整份 JSON、全文和预览等平行正文；第 6.3 节 NEXT 对原已保存结果的补页规则是特定交付路径，不扩展为所有查询结果去重。

exit_plan 提交的方案保留数据库调用参数，并自动尝试另存工作区 .pulsara/plans/ Markdown，正常情况下为两份，具体合同见 [Plan 工具草案](native_tools/plan.md#33-数据库与-markdown-两份保存)。文件按可选 summary 命名，默认方案.md，重名用 (2)、(3) 顺延；写入失败不阻止审批，原因进入 content，仅文件完整保存成功时返回 PATH Target。审批和执行使用本次调用参数，不另存第三份数据库方案正文，也不以文件变化替换已提交内容；提交状态未确认时如实说明，不冒充提交失败或自动重做。

exit_plan 提交方案后阻塞等待审批，打开卡片时保留 RUNNING 原轮及待决调用，不产生“已提交审批”的工具结果。修订为 SUCCESS，取消本次审批及十分钟超时为 CANCELLED；三者均将唯一最终结果交给当前只读轮继续，工作流保持 ACTIVE，不恢复权限、不新建修订后继轮。只有批准或无方案退出才结束规划及原轮、恢复保存的权限并自动续接，具体按 Plan 合同处理。

数据库约束及触发器同步收敛：OPEN 的 DRAFT_REVIEW 可无 control_tool_result_id；结算为 APPROVED、REVISION_REQUESTED 或 CANCELLED 时允许该字段由空值关联唯一最终结果。删除 DRAFT_REVIEW 一律要求原轮 COMPLETED 的限制，等待及修订／取消／超时后均允许原轮 RUNNING；删除 REVISION_REQUESTED 必须填写 decision_continuation_entry_id 的限制，修订／取消／超时均保持该关联为空，批准仍关联实际执行续接。保留会话、工作区、调用和结果的精确归属及结算不可改写约束，实施时同步更新当前合同测试；不预存成功结果再改写，不增加第二份工具结果、等待实体或新字段。

### 2.2 共同存储不等于共同披露

共同读取入口按既有作用域解析内容，隐藏 inline/blob 差异，保留顺序和真实来源。历史对话消费者在其上选择符合条件的内容；调用方不再分别解析各种 entry 的旧正文布局。

entry 的来源信息仍由 transcript_entries 等现有业务 owner 保存，不因正文进入共同表而丢失。Subagent 的主指令使用 input_origin=AGENT_INSTRUCTION，各份材料使用 AGENT_MATERIAL；两者可以同为 USER_MESSAGE，但来源不同。content_parts.type 只区分 text/image 等内容形状，不承担消息来源分类；不向每个内容项重复复制 entry 来源，也不为这次排序增加一组 entry_kind 或来源枚举。材料的具体 Work 来源及依赖关系仍沿第 4.2 节的原业务 owner 管理。

| 内容 | 历史用户／助手对话正文 |
| --- | --- |
| 用户输入、用户轮内补充 | 保留适用正文及引用／批注身份 |
| 助手正文和 commentary | 保留，即使同一 entry 内还有工具调用 |
| 正文中的代码、JSON、公式 | 保留，不判断它是否“像自然语言” |
| 工具名称、参数和结果 | 不纳入，不因值是自然语言而视为用户或助手发言 |
| 思考、公开思考摘要、不透明 provider 回放 | 不纳入；原存储、UI 展示和协议回放职责保留 |
| Cell 输出及 runtime 材料 | 按真实来源处理，不因投影 role=user 就归类为真人输入 |

没有适用正文的 entry 返回空内容，不伪造文字。授权、冻结可读边界、来源资格及 QA／QAQ 选择规则保持。工具结果有自身可读正文，不表示它属于历史对话正文。

## 3. content_parts 的结构

### 3.1 字段

以下是内部生产表的结构要求，不是模型可见字段。复用 shared ID 层的 block 身份，不新增正文实体 ID 种类。

| 字段 | 形状与职责 |
| --- | --- |
| id | block:<26 位 payload>，内容出现项的身份 |
| session_id、workspace_id | 所属作用域，必须与 owner 及 blob 一致 |
| entry_id | 已进入会话时指向 transcript_entries；与 queue_item_id 二选一 |
| queue_item_id | 队列持有时指向 prompt_queue_items；与 entry_id 二选一 |
| ordinal | owner 内非负顺序值，不是模型可见的分页位置 |
| type | text、image、target、object，以及内部 annotation、tool_call、data |
| inline_value | 可空 bytea；text 为原 UTF-8 字节，target/object/annotation 为当前规范 JSON 编码，data 为原载荷 |
| blob_id | 可空；图片或外置载荷的真实 blob 外键，不是任意 JSON 中的伪引用 |
| tool_call_id、tool_name、tool_arguments | 仅 tool_call 行使用；沿用现有调用关联和参数，不同时写入 inline_value |
| data_media_type | 仅 inline 的内部 data 行记录实际媒体含义；外置时从 blob 取得，不重复记录；不将 DATA 误解为文本或图片 |

公共内容形状仍是 type/value；inline_value 是物理字节，不要求 JS 理解它。按 type 解码，image 的 value 从 blob_id 生成 IMAGE_REF Target。编码不加入 schema_version、固定合同字符串或旧格式探测。

### 3.2 类型与载荷

| type | 唯一载荷 | 消费形状或职责 |
| --- | --- | --- |
| text | inline_value 或 blob_id 二选一，原文字和换行 | {type:"text", value:字符串} |
| image | 仅 blob_id，引用已验证的 image 身份 | {type:"image", value:IMAGE_REF Target} |
| target | inline_value 或 blob_id 二选一，按既定 Target 合同验证 | {type:"target", value:Target}，不自动打开目标 |
| object | inline_value 或 blob_id 二选一，完整 JSON 对象 | {type:"object", value:对象} |
| annotation | inline_value 或 blob_id 二选一，quote/source/comment | 用户批注；不是工具 content 的新增公开类型 |
| tool_call | 仅调用专用字段 | 执行和协议消费者使用，不纳入对话正文 |
| data | inline_value 或 blob_id 二选一，保留原媒体含义 | 原适用消费者使用，不自动升级为对话或视觉输入 |

同一助手消息的 TEXT 与 TOOL_CALL 保留完整先后顺序。空正文可以没有内容行；纯工具调用 entry 可以只有 tool_call 行，不强制每条 entry 有 text。

### 3.3 约束与完整性

1. entry_id 与 queue_item_id 恰有一个非空；使用真实 owner 外键及作用域约束，不用无外键的 owner_kind/owner_id 字符串代替。
2. 分别保证同一 entry、同一 queue 的 ordinal 唯一。不新增顺序清单、连续性回执或内容行上的分页位置列；原生实际交付范围归第 6.3 节的既有观察／投影记录。
3. type 只允许自己的载荷列。禁止同时存 inline 与 blob，禁止 tool_call 再保存文字参数副本；blob 引用保持 workspace 一致。
4. 保留 tool_call_id 在所属助手 entry 内的唯一性；调用、attempt、result 外键改指共同表的实际调用项，保留原关联和结算。
5. 图片字节、MIME、身份和尺寸由现有图片验证 owner 校验，不能只检查 type=image 就接受任意 blob。
6. blob 摘要、大小及不可变字节校验继续在真实内容边界生效。内容行不机械复制整组 blob 描述或另存正文总摘要；Plan 确认、provider 前缀等仍有效的验证沿原边界保留。
7. 沿现有事务 owner 发布完整内容。可以分批准备大结果，但未完整接受的片段不能作为正常完整结果或正常首屏。

type 不决定全部权限：同样的 text 可能属于人类消息或工具结果，仍须结合 owner 来源。不增加 is_dialogue/is_readable 等重复布尔值。

session_id/workspace_id 表达内容记录、owner 和 blob 的归属，不是业务 Target 的新访问限制。PATH 仍按文件工具合同支持工作区内相对路径、工作区外绝对路径；历史会话搜索仍按既定 memory domain 范围跨工作区查询。object 或 target 项中保存的业务引用不要求目标本身属于该内容记录的 workspace；实际文件、会话和图片访问分别沿各自 owner 检查，不因共同表统一收窄或扩大范围。

共同表仍须保留内容类型与所属来源的实际约束：tool_call 只能属于合法的助手调用 entry，不能出现在用户、结果或队列 owner 下；其他类型也按各接入合同校验，不能因共用表就自动开放全部类型。原调用、attempt、result 的关联校验随外键一起切换，不能只改引用表名而丢掉父项资格检查。

载荷发布及归属更新分别限定：已接受内容的 id、ordinal、type 和载荷保持不变；允许的转移是原队列到实际 entry，或按第 4.1 节转到实际替代队列，不允许把已进入会话的内容改回队列、任意转移或改变 session/workspace。目标 DDL 和 runtime grants 必须支持既有事务中的受控归属更新，不能照搬旧表只允许 INSERT 的权限，也不能因此开放整行任意修改。复用现有事务和权限机制，不新增 sealed 标志、版本、归属转移回执或新状态机。

## 4. 队列、steer 与 Cell 生命周期

排队时内容行归 queue_item_id 所有。消费在现有事务内完成：

```text
创建实际 transcript entry
→ 同时清空内容行的 queue_item_id、将 entry_id 设为该 entry
→ 更新 consumed_entry_id 和队列状态
→ 提交事务
```

转移前后 id、ordinal、type、载荷不变。失败回滚到原队列状态，不能留下无归属、半份正文或两个正文 owner。并发消费沿现有事务和状态条件处理，不新增回执、锁服务或恢复任务。

只转移原队列内容。初始上下文、agent 材料、Plan 交接等额外运行时输入若属于不同 entry，继续归它们自己的 entry，不混入用户正文、不复制原文给每个 entry。需要包裹提示时由来源信息投影，不篡改原载荷。

已消费队列经 consumed_entry_id 展示正文；所有读取者同步切换，不能继续依赖原队列正文位置。未消费即取消／拒绝的内容仍归队列，按原生命周期清理；存储存在不授予模型图片读取资格。

消费后的现有确认路径也同步切换：PENDING 时核对队列持有的内容，CONSUMED 时经 consumed_entry_id 核对实际 entry 的同一份内容。不能继续要求“队列正文与 entry 正文各自存在且相等”，更不能为满足旧确认逻辑恢复正文副本。保留现有提交结果确认和并发结算语义，不新增回执。

### 4.1 排队消息改为 steer

普通排队输入改为当前轮 steer 时，在同一现有事务内创建实际接收的 steer 队列、将原内容行转归新队列，并将旧队列标记为已转为引导。复用现有 CANCELLED 状态及 USER_REDIRECTED_TO_STEER 原因，不增加“已转向”状态枚举。内容项身份、顺序及载荷不变，不复制正文或图片引用。

旧队列保留原身份、必要来源和“已转为引导”的状态事实，不再独立保留或展示原正文；不用 consumed_entry_id 冒充指向新队列，也不新增通用重定向链。普通取消／拒绝且没有转移内容的队列仍按上一节处理。

Plan 审阅期间将队列消息明确用作修订反馈时，仍提交 REVISE 决定；普通 steer 不因此变成审批决定。完整有序文字、图片、批注／引用沿既有输入路径进入当前只读轮，支持纯图片反馈；原内容行转给实际接收的队列或 entry，保留来源及图片读取资格，不复制正文。工具结果仅简短说明用户要求修订，先交付该结果，再在安全点按已有输入顺序处理反馈及已接纳补充；不新建修订后继轮或重排上下文。Plan 控制事实仍由原 owner 保存和投影，不把原文又塞进控制 JSON。失败回滚保持原队列及其内容；不能只修改普通 steer 路径。

### 4.2 subagent 的上下文和依赖材料

当前 prompt_queue_items.initial_context 保存文字数组，work_inputs[].body 保存已取得的材料／依赖正文，消费时还会生成独立 AGENT_MATERIAL entry。这两处纳入正文统一，不能仅删除顶层 inline_content/blob_id 后把嵌套正文留作另一份权威。

本次 Work 的初始业务输入统一按以下顺序组装并消费：

```text
ROOT 分配的主指令                  AGENT_INSTRUCTION
→ ROOT 选入的历史材料              AGENT_MATERIAL
→ depends_on 的依赖结果            AGENT_MATERIAL
→ 其他显式 materials               AGENT_MATERIAL
→ 助手执行及后续正常交互
```

主指令是本次初始输入组的第一条 entry，turn.initial_entry_id 与原队列的 consumed_entry_id 都指向它，不指向材料。历史材料内部保持所选顺序；依赖结果和其他材料分别按声明顺序，不按完成时间或入库到达时间重排。同一份 Work 已按原规则去除的重复材料不重新插入。不存在的材料类别直接跳过，空材料与未满足依赖仍按下文区分。

上述顺序用于本次新增输入组，完整追加在该 Subagent 的已有有效上下文之后；新建时接在其已安装的系统及运行时上下文之后，不把 ROOT 选入的历史材料恢复为该 Subagent 过去发生的交互。ROOT 历史投影、主指令和各份 Work 材料保持真实来源，材料不自动成为新任务要求。复用 Agent 不重新灌入 ROOT 历史，不重排已有 entry、摘要、SYSTEM/tools 或已安装 provider messages。

保持依赖就绪条件：全部成功依赖已满足、材料已按原规则取得后，才沿现有启动事务接受完整初始输入组并发起首次模型调用；依赖失败仍按既有拒绝／结算规则处理。初始输入的计量、首次调用前的压缩和提交确认覆盖主指令及全部材料，不先调用模型再补依赖，不只保留主指令而遗漏后续输入。

材料正文进入共同内容路径，排队期间只保留将主指令和各份材料分别转入 entry 所需的分界与顺序。queue_item_id + ordinal 只能表达内容顺序，建库前须在现有队列材料结构中明确这些必要分界；不把多份材料拼成一条用户正文，不恢复嵌套 JSON 正文，也不新增通用正文实体或注册表。

既有 Work 材料／依赖关系仍由原业务 owner 保存：source_work_id、材料种类和顺序用于依赖调度、取得来源结果、协作查询及前端关系展示。依赖是否满足必须明确；已满足但正文为空可以没有内容行，不能与尚未满足的依赖混同。删除 body/null 正文表达时同步承接这个区别，不因正文统一删除实际调度语义。

消费时各份内容在同一事务内转归实际 entry，之后普通正文读取按 entry 及其来源类别处理。主指令的 consumed_entry_id 仍只指主指令；不为按原材料反查每条已消费 entry 新增永久追溯映射，不将 source_work_id 等 Work 关系复制到每个内容项。排队分界用于正确分配内容，不扩展为永久逐项来源链；现有 Work 关系、提交确认和 entry 正文各自保留职责。

具体例子：一个队列项携带主指令“修复导出”、ROOT 选入的历史材料 A“优先检查测试”，以及来源 Work B 的依赖材料。消费时依次创建主指令、A、B 各自的 entry；主指令的来源为 AGENT_INSTRUCTION，A 和 B 为 AGENT_MATERIAL，initial_entry_id 指向第一条主指令。因此目标 DDL 要能回答排队期间“哪些内容项是主指令、哪些是 A、哪些是 B”，不能把全部内容都转给主指令；这是队列内容的必要分界，不是 entry 进入会话后无法辨认来源。B 尚未完成与 B 已完成但没有文字都可能没有内容行，是否可消费须由现有依赖满足状态判断，不能用空正文判断。具体列和约束属于实施设计，不要求用户决定 SQL 字段名。

### 4.3 Cell 输出

Cell 显式 console.log/return 沿现有接纳链路形成交给 ROOT 的 steer，使用共同内容。已消费并进入会话的 Cell steer 中，真实 image 内容项的 IMAGE_REF 可再交给 view_image 读取，沿原 workspace 边界；具体来源资格见第 5.2 节。Cell steer 本身不提供 read_file 读取、续读或导出入口，保存为会话 entry 不会把 steer 变成文件或工具结果。不得新增“入队后立即有可读 SESSION_ENTRY”、ACK 或跨重启恢复承诺。JS 中间结果不因保存而自动进入 ROOT 上下文。

## 5. blob、图片出现与读取资格

### 5.1 字节与出现项

保留 PostgreSQL blobs 及现有发布、校验、读取 owner。blob 不是模型 artifact，也不是终端日志暂存文件。相同图片可复用资产；多次出现仍有多个 content_parts 行。

```text
观察 1 的 object → image A
观察 2 的 object → image B
观察 3 的 object → image A
```

A 的字节可只有一份，三次观察全部保留。图片 MIME、字节数及验证后的 width/height 归资产；在 blobs 补充可空图片尺寸列，成对有效并由图片验证 owner 写入，普通 blob 不伪造尺寸。时间、展示区间、格子位置归本次观察的 object，不写进 blob。

assistant_visualizations 中同一图片的 width/height 改从 blob 取得，删除重复的图片固有尺寸存储及其写入／校验路径；可视化的来源、状态和失败信息保留。若其他媒体存在独立的布局或尺寸语义，按实际用途保留，不能仅因列名相同就误删，也不把它们混成图片固有尺寸。

image 内容行直接承担 canonical_image_refs 的顺序和引用职责，不再保存独立图片清单。GC 检查共同内容行及其他有效 blob 引用者，不新增手工引用计数。

### 5.2 外键不授予读取资格

图片读取 owner 继续校验调用方 session/workspace、合法来源和状态。当前存在同 workspace 合法来源查找，不能因共同表有 session_id 就收窄成仅本 session，也不能扩大到任意 workspace。当前已接受消息、工具结果、READY 可视化等实际资格逐项映射到新表或保留表；新调用路径的披露限制继续适用。

- 仅由未消费、取消或拒绝队列持有的图片，不因 blob 存在而新增模型读取资格。
- 同图还有其他合法已接受来源时，按该来源判断，不被另一队列状态反向禁用。
- JSON 中的 IMAGE_REF 字符串不创建图片出现项或授予访问权；object/target 不自动加载图片。
- JS 内部结果、Cell 显式输出、原生交付分别遵循授权与披露路径，不能用共同存储绕过 ROOT 披露边界。
- 新读图查询直接读取合法内容行及 blob，不恢复旧 prompt JSON 与图片清单双重核对路径。

已消费并进入会话的 Cell steer 图片内容项，是本次明确增加的 view_image(IMAGE_REF) 再读合法来源，沿现有 workspace 边界处理，不收窄成只允许同一 session；不能因为 entry_kind 为 TERMINAL_OBSERVATION 就漏掉它。当前生产读图查询尚未包含这一来源，实施须显式接通并验证，不将其称为原有资格保持。只在队列接受、尚未进入会话的 Cell 输出不获得该资格；未消费即取消或拒绝的独有图片也不获得资格。这仅开放 steer 中图片引用的 view_image 再读，Cell steer 本身不提供 read_file 入口，不把 Cell 材料改称人类发言，也不要求模型先前实际看过图片才允许读取。

首次图文投递沿该输出已经获准的接纳和投影路径加载图片，不要求图片事先成为公开可查的历史来源。公开 IMAGE_REF 再读则按上述已接受来源资格检查。两者复用图片资产和验证 owner，但不能互相作为循环前提；不新增持久“已投递／可读”标记。

### 5.3 现有 blob 承载与后续实验

首版继续使用现有 PostgreSQL blobs 的 bytea 路径，删除应用写入／读取校验及数据库约束中对应的旧人为 16 MiB 单 blob 和完整结果总量上限，不以扩大常量或更名后的上限替代。保留大小非负、实际字节完整性等有效性校验；独立的单次 I/O、图片解码或 provider 资源边界按真实用途处理，不因数字相同一并删除。

很多小内容项可以组成大结果，单个超过旧上限的 text/object 也仍是一个完整内容项，必要时外置为一个 blob。正常接受时保持完整值、内容项边界和顺序，不截断、不拆成公开 JSON 片段、不改变 JS 返回类型。真实数据库、驱动或内存无法承载时明确失败，不伪造已保存结果，不自动重做业务。

inline 与 blob 的选择阈值只决定字节放在哪里，不能决定超过多少就不保存。64 KiB 选择阈值、峰值内存和实际 I/O 承载后续实验，本轮不选新数值。首版不建设分块表或另一套存储系统；只有后续实验确认现有承载不足时再讨论内部流式或分块能力，不将其作为本轮建库前置条件，也不新增总历史、总内容项或总图片数限制。存储方式不改变 JS 完整业务值进入内存、不分页的要求。

## 6. 工具完整业务值

### 6.1 其他业务字段与 content

tool_results 增加 public_fields JSON 对象，仅存各工具已声明的非 content 业务字段。它不包含共同 content、原生页、Pulsara 为展示生成的 next／完整结果 Target 或返回值副本。外部业务本身名为 next 等同名字段保持原义，不按字段名过滤，不把它们当作 Pulsara 续读控制。执行状态、观察时间等已有权威列可确定的值从原列投影，不再重复保存。

字段合同由原生工具修订文档或工具已有明确返回类型约束，不作为任意字段兜底。保留缺失与显式 null、JSON 数值类型、数组顺序、失败业务值、MCP structuredContent 等适用语义。当前返回值不是 meta/content 对象的工具，实施前必须明确无损映射，不能为套表擅自改变返回类型。

定时任务的内部 revision 属于必要执行事实，沿现有 tool_results.result_facts 按用途收敛保留，不进入 public_fields、共同正文、模型 schema／提示词／教程摘录或导出。每次查询的公开配置与对应 revision 从同一次业务读取一起冻结；列表保留各计划身份与对应值，管理成功保留实际变更后的值，switch 同时保留双方的值，不复制整份配置到执行事实。runtime 在本 session 已保存结果中为同一计划选取最大的 revision；NEXT 只能读取已保存的冻结结果，不重新查询或刷新其中的配置与内部执行事实，不产生新的计划快照。不能另读当前业务行冒充旧值，不新增快照表或注册表。用户在定时任务页面实际修改权限等配置时，同一事务内推进计划 revision；模型随后使用旧结果管理时仍按原子核对返回冲突，查询新配置后再决定。此处是执行事实与公开业务值的边界，不增加模型字段或第二份正文权威。

例如 read_file：

```text
工具结果行：执行与观察事实；public_fields 中的 target、文件属性等
结果 entry 的 content_parts：原文件正文 text
```

共同结果读取器组装该工具的完整业务值，JS 直接获得它；原生和导出从它投影。文件 text 保存原文，不存行号。

终端的 process_status、exit_code、cwd 及 query_processes 记录中的 duration_seconds 等，保存本次观察时实际取得的快照，不在补页、导出或组装 JS 值时重新查询实时进程。进程已退出或已被回收，不改变已保存结果中的 RUNNING/null 等原事实；工具调用状态继续由本次调用结算产生，不与进程状态混同。内部观察位置在完整保存本次输出后才推进，读取 NEXT 不推进它。

公开 meta.status 为 SUCCESS/ERROR/CANCELLED，按真实结算及各工具语义映射；不覆盖内部仍有独立用途的执行分类，不把终端非零退出误判为工具调用失败。原因在 content，适用 JS 异常的 err.result 保留完整失败值，不另加 meta.error。调用观察时间和微秒耗时按既定顶层位置投影，不移入 content 或重复存入 public_fields。

### 6.2 接受与失败

业务行、有序内容和有效图片引用共同接受。预备的孤立 blob 不构成可读结果，由现有 GC 处理。完整接受后才正常返回 JS 值或原生首屏；效果已经发生时，保存失败不得触发重做业务。

业务失败已有完整值时，仍保存全部文字、图片、结构化字段。保存失败／未确认时说明已知效果及不可用性，不返回假完整 Target，不用零条内容冒充合法空结果。保存成功但当前页交付失败时保留完整值，按统一交付草案给出实际 Target 和未交付位置。

必要保存失败事实仍由原结算 owner 记录。不能机械删除 output_unavailability_reason，导致无法区分空结果与未保存结果。删除旧总量 cap 所导致的 BODY_TOO_LARGE 分支及旧展示含义；真实失败在现有记录中收敛，不新增恢复状态机。

### 6.3 原生交付范围与 NEXT 读取

原生首屏和 read_file(NEXT) 续页都从已保存完整结果产生，不重新保存原全文、当前页面或剩余后缀作为第二份正文。正常解析并读取原结果时，tool_result 指向该原完整结果，不改指续读调用的新观察 entry；本次 read_file 的状态与观察事实仍属于本次读取，不覆盖原操作事实。

每次原生交付复用既有调用／观察及投影记录，保留已解析的原结果身份和当时实际交付的起止范围。范围定位同一保存结果的确定正文投影，能表达内容项之间及文字／object 投影内部的停读处；图片不可拆，不预设或向模型暴露内部位置字段。首屏同样记录实际范围。不能仅记起点、以后按新的预算重新分页，导致历史工具消息包含当时未交付的内容；实际交付及失败仍沿既有回调处理，不增加 ACK 或第二套已读集合。

例如原生续读当时展示原结果 A 的第 101–180 行，内部记录 A 与该范围即可，不再保存这 80 行正文。恢复已接受的这条观察时按记录范围取内容，不重新执行 NEXT、原业务或重新验证可能失效的临时引用。再次调用同一 NEXT 是另一次观察，可以因当时准入条件不同而得到不同页长；同一已接受观察的范围及返回 NEXT 不因此改写。来源访问、必要存活引用和删除行为沿既有 owner 闭合，不建立分页实体、通用正文注册表或跨重启可调用 NEXT 的新承诺。

交付范围不是文件行号。例如保存结果 R 的文字是 ABCDEFGHIJ，一次只交付 ABCD，则记录这次读取 R 中哪一段；恢复这条观察仍得到 ABCD，不能因后来预算变大而改成 ABCDEF。这里只需保留来源及实际范围，不另存 ABCD 正文，也不要求内容表增加 line_number。文件原生行号从原请求、该次保存的 total_lines 和正文中的 LF 推导，具体规则见文件系统草案第 1.4 节。

JS 原工具调用直接取得完整类型化业务值进入内存，不分页、不产生展示 NEXT，也不记录一份虚构的原生页面。console.log/return 只按脚本显式组合整条交付。显式把原生 NEXT 传入 JS 的跨入口读取沿 unified 的 read_file 合同处理，不将该少见用法作为 JS 常规流程，也不因此增加 JS 分页或保存后缀副本。

NEXT 错误直接沿普通失败规则处理，不新增状态或错误枚举：

| 情况 | 返回与保存 |
| --- | --- |
| 原结果已解析且可定位，但本页或图片无法交付 | 报告交付失败，保留有效原完整结果 Target 和实际未交付位置的 NEXT；不将失败页记为已读，不跳过图片 |
| NEXT 已失效或无法解析，无法定位可读原结果 | 报告读取失败，不伪造原结果 Target；确实产生并保存的失败业务值可以有自己的完整结果 Target，正常交付完该错误结果时 next:null 不表示原结果读完 |
| 参数校验等更早失败，未产生业务值 | 沿既有运行时错误通道报告，不伪造业务值或结果 Target |

失败业务值仍用 meta.status:ERROR，简短原因放 content，例如“无法读取这个 NEXT：引用已失效。”新产生的错误说明是本次失败内容，不是原结果正文的复制；其保存或交付再失败时继续沿第 6.2 节处理，不增加重试票据、恢复任务或自动重做业务。

## 7. 图文生产者、视频 object 与顺序分页

### 7.1 内容组织

| 入口 | 共同内容 |
| --- | --- |
| 用户输入、排队、steer | 原顺序 text/image/annotation |
| 助手正文、commentary | 原正文内容，保留内部调用顺序 |
| view_image | 简短说明 text → image |
| overview | 范围说明；逐张拼图的局部索引 object → image |
| detail | 范围说明；逐个实际观察的时间 object → image |
| motion | 完整请求范围内的全部索引 object → 拼图 image，保存完成后分页 |
| MCP | audience 过滤及适配后的有序内容；公开业务字段保留在其业务位置 |
| Cell 显式输出 | 脚本实际选择的内容和顺序，不自动输出中间工具结果 |

视频 t、visible_range、必要间隔及格子 n/t/cell_rect/representative_range 等业务索引用已有 object 保存一次，供 JS/UI 访问；原生和导出按共同 JSON 规则投影。不另存同内容的说明字符串。独立范围说明可用 text，但不复制整份索引。字段适用性沿视频既有规则及统一交付草案，不增加 mode 或采样。

```json
[
  {"type":"object","value":{"t":10.0,"visible_range":[10.0,10.04]}},
  {"type":"image","value":{"kind":"IMAGE_REF","image_ref":"image:<A>"}},
  {"type":"object","value":{"t":11.0,"visible_range":[11.0,11.04]}},
  {"type":"image","value":{"kind":"IMAGE_REF","image_ref":"image:<B>"}}
]
```

ID 为示意占位，真实 ID 继续由 shared ID 层生成和校验。重复资产不合并出现项；detail 同页载荷共享及跨页实际重投图只影响交付，不删除观察项。

### 7.2 无绑定标记，只保证顺序

所有原生结果交付入口采用顺序分页；JS 完整业务值及 Cell 显式输出不套用该分页。不存图文绑定用的 bind/group/caption 关系字段，不推断 MCP 图文关联，不按工具名形成不可拆分组。

- text 和 object 的确定文字投影可切分；保存的 object 和 JS 值始终完整。
- 图片不可拆；独立 target 内容项按统一交付规则完整装页。
- 说明或视频索引可以在上一页，图片在下一页；next 不重复、不遗漏、不跳过。
- 不回退已经交付的说明，不跨图装入后续文字，不复制上一页说明使每页自足。
- 超大单图独占页，例外只覆盖该图及必要投影标记，不豁免说明、object 索引或其他正文。必要状态和 Target 仍准确投影。

按上述顺序装页：文字取可容纳的连续前缀；下一张图片放不下剩余预算时结束当前页，NEXT 留在图片之前；空页遇到超过 12k 的单图才采用独占页。正常页的 NEXT 指第一个尚未交付的位置，实际起止范围按第 6.3 节记录。例如 8k 文字 → 5k 图片 → 2k 文字，第一页只交付前 8k 文字，第二页交付图片和后 2k 文字，不为填满第一页重排内容。实际 provider 仍不能接受最小单图页时明确失败，保留未交付位置，不以图片引用冒充已交付图片。

第一页只放得下 t=10 索引、下一页从图片 A 开始，是合法结果。跨页理解依赖连续阅读，不承诺每页独立包含全部图片上下文。JS 显式重排只按脚本顺序交付，不继承隐含关系。

### 7.3 MCP 和批注

MCP 复用 SDK 正式类型解析。audience 缺失/null/空数组保留；非空且明确不含 assistant 的项在图片解码及入库前丢弃。保留项移除 audience，不保留原始响应副本；其他适用标注在原所属位置转为可读内容。普通字符串、URL、Base64 或 JSON 不自动升级为图片。

批注继续为 quote/source/comment，comment 是文字，来源位置保留现有单位和身份。允许“文字 → 批注 → 图片 → 文字”，相邻图片不自动成为批注内部附件。选中 Markdown 图片文字形成引用不等于批注内保存图片字节，本次不新增嵌套图文批注。

## 8. 消费与 provider 投影

| 消费入口 | 行为 |
| --- | --- |
| JS 工具调用 | 完整业务值进入内存，图片为 IMAGE_REF，不强制复制图片字节；不分页、不自动披露 ROOT |
| 原生结果及 NEXT | 按保存内容 12k 启发式预算顺序分页，不重执行业务 |
| console.log / 顶层 return | 按显式组合整条交付，首版不另设预算或分页 |
| 导出 | 从完整值产生确定正文及图片引用，不用第一页冒充全文 |
| 历史与检索 | 选择符合来源和范围规则的对话正文；索引可重建 |
| 前端 | 按真实来源和顺序显示，保留流式草稿接续及批注定位 |

原生图文页投影为：

```text
tool 消息：本页文字、object 的 JSON 文字、原图片位置的 IMAGE_REF、必要状态和 Target
关联 user 消息：现有 tool_call_id、对应 IMAGE_REF 标记、真实图片
```

附属 user 消息不是新人类 entry、turn 或权限来源，不在数据库预先保存另一份消息正文。通过调用和图片身份关联，不依赖 provider 猜测；不为图片补说明而自动重发上一页索引。图片和文本共同完成该逻辑页，引用不能冒充已看图。

数据库原文及业务对象是分页计量真源，原生行号、封装和控制 Target 属于投影。数据库不预存原生页面正文，实际交付范围按第 6.3 节记录；NEXT 内部定位对模型透明。

同 epoch 的 SYSTEM/tools 字节一致、messages 后缀追加合同保持，不因存储统一重建已安装前缀；接口只在允许的新 cold epoch 或明确采用的 compaction successor 安装。精确协议回放仍由原 owner 处理，不从公开正文猜回协议。

## 9. 旧结构替代与保留边界

| 旧结构 | 唯一替代或保留理由 | 删除／切换要求 |
| --- | --- | --- |
| assistant_message_blocks | content_parts 承担正文、DATA 及调用顺序 | 替换表、生产者、查询和外键，不留助手专用副本 |
| transcript_entries.inline_content/blob_id/content_digest/content_size/content_media_type/content_codec | 正文进内容行；身份、来源及控制信息归 entry 或原控制 owner | 删除正文载荷组及助手 manifest；逐项承接 draft/Plan/运行时控制事实，不按前缀盲删 |
| prompt_queue_items 的同组正文载荷 | 队列内容行，消费后经 consumed_entry_id 读 | 删除独立正文及复制正文／图片清单路径 |
| prompt_queue_items.initial_context、work_inputs[].body 中的正文 | 共同内容、排队期间必要分界及消费后的 entry 归属 | 删除重复正文；现有 Work 依赖关系和满足状态留在业务 owner，不复制到各内容项、不新增消费后永久追溯映射 |
| 排队改 steer 的正文复制和旧双份消费确认 | 同一内容行转移到实际新 owner | 旧队列只留已转向事实；已消费确认经实际 entry 读取，覆盖普通及 Plan 路径 |
| canonical_image_refs 及专用索引 | image 内容行承担顺序、作用域、合法来源查找和外键 | 全表删除，同步替换读图资格、fork、GC，不只换插入位置 |
| tool_results.output_body_*、entry 中结果预览 | 业务字段 + 共同内容行 | 删除全文／预览双轨、旧 16 MiB 逻辑上限、展示标记 output_omitted |
| NEXT 调用产生的重复全文、页面或后缀 | 原已保存结果 + 既有调用／观察及实际交付范围 | 正常读取不重新存正文；无法定位原结果按普通失败处理，不伪造原结果 Target；不建立分页实体 |
| output_unavailability_reason、output_source_coverage*、result_facts | 按真实失败、业务诊断及执行消费者逐项归位 | 纯展示概念删；保留保存失败、缺口、已知效果和授权事实，不整组复制进 public_fields |
| prompt 专用整份图文 JSON 及图片描述清单 | 内容行 + blob 固有属性 | 删除持久权威和旧 codec；临时 provider 编码不存回正文 |
| 旧正文大小／摘要的派生使用 | 从当前内容取必要计量；真实验证沿原 owner | 不为方便恢复 manifest、正文总摘要或重复 blob 描述 |
| tool_invocations / attempts / 调用结果关系 | 执行和结算 | 保留语义、转接外键，不混入历史对话 |
| provider_assistant_replay_fragments | 精确协议回放及思考的独立职责 | 保留必要内容，允许协议字段与正文重合，但不作为第二正文权威 |
| assistant_visualizations | HTML、音视频、图片展示及失败状态等独立能力 | 不整表删，不强制变成模型图片；同图 width/height 从 blob 读，删除重复尺寸，保留范围和资格 |
| context_snapshots、代码源等其他 blob 引用者 | 现行独立职责 | 不按 content/blob 字段名清理，GC 继续保护 |
| history_search_documents 等 | 派生检索索引 | 从共同正文重建，不作为读取失败时的正文替代源 |

实施逐项核对旧字段的写入方、读取方及目标位置。没有承接真实语义前不能删除；仅被旧代码依赖不构成保留理由。核对属于实施清单，不新增持久映射注册表。

当前合同下的 fork 复制必要内容出现项并保留来源语义，为目标 session 生成正常身份，按原规则复用 blob；不能移动父会话行或借共享 blob 扩权。分页 Target 不自动跨 fork 继承。

## 10. 数据库优先与停用期间的分步实施

### 10.1 “准备”的具体产物

准备不是继续等待全部工具讨论，也不只是在旧代码外加一层接口。先完成以下可检查产物：

1. 当前 clean-v0 的目标 DDL：共同内容表、blob 属性、外键、合法载荷和所属来源约束、受控归属更新权限及旧结构删除清单；不增加内部版本字段。
2. 第 9 节字段及消费者替代关系，特别闭合排队材料的必要分界及既有依赖状态、队列转向、原生首屏／NEXT 的原结果引用与实际交付范围和真实控制字段落点；不新增材料永久追溯映射。
3. 最小存储读写实现及数据库级验证：有序内容发布／读取、完整值组装、队列事务转移、图片引用与读取资格、GC。最终模型工具参数尚未确定时，用明确测试输入验证存储行为，不先猜工具新合同。
4. 沿现有 PostgreSQL bytea 路径落实 blob 读写及数据库约束中的旧人为 16 MiB 上限删除，验证单个超旧上限的 text/object 和多项大结果。保留完整值及内容项边界，真实资源失败如实报告；inline 选择阈值和峰值内存等实验另列，不把建设分块表作为准备条件。

结构可先在验证用数据库建立并迭代；完成本阶段不等于 Pulsara 已能正常运行，也不要求所有应用消费者已经接通。

### 10.2 可以先重建新库，期间停止正常使用

允许在目标结构闭合后，先将已核实的本机可丢弃开发数据库按新 clean-v0 重建，再在新库上逐步实现。这里的“迁移”是停用期间的开发 hardcut，不保留或搬运旧会话，不建设线上迁移链、双库切流、新旧格式协商或兼容读写。

实际重建前读取并核实有效 Pulsara home 和具体本机数据库目标，沿现有关闭路径停止会访问该库的应用／Host、后台调度和相关工作进程，完成必要的在途效果结算。保存的连接、凭据和用户配置仍作为只读输入，不因重建数据库覆写。

新库建立后，在消费者未接通期间禁止正常启动和使用 Pulsara，包括用户会话、后台调度及会按旧结构写库的进程；只运行针对已实现范围的受控测试和开发验证。通过停止现有应用及后台入口保持停用，不为本次开发停用新增数据库维护状态、持久迁移任务或运行时版本开关。

允许此过程跨多个工作阶段和提交，允许中间应用不可运行。必要时可再次重建同一已核实可丢弃开发库，不保留中间测试会话、不为中间结构写兼容迁移。不能让旧二进制访问新库，也不能为了临时恢复使用重新添回旧正文权威。当前文档修改不执行停用或重建。

### 10.3 在新存储基础上讨论工具并完成消费者

数据库基础明确后，继续逐个讨论剩余 native tool，包括历史会话工具。确定完整返回值、JS 类型和原生投影，再检查共同结构是否需要必要调整；已确定的工具合同保留，未确定的合同不因建库被默认定案。

随后逐步接通用户接入、队列、steer、助手提交、工具结果、Cell、图片加载、前端、历史、检索、fork、导出和 GC；按统一交付合同实现原生 12k、JS 完整值、Cell 图文输出和模型类型声明。消费者可以分步接通和测试，但正常使用持续关闭，不要求中间阶段具备产品可用性。

旧生产路径在相应替换中删除，最终不能留下双写、双读或旧结构兼容。hardcut 要求最终只有一套现行合同，不要求所有代码在同一提交完成，也不要求整个开发过程保持可用。blob 阈值及其他资源实验仍单列；实际承载和资源失败必须在恢复使用前闭合。

### 10.4 恢复正常使用的条件

恢复前完成本文和 unified 文档中本次采用合同的必要消费者、正常／失败路径及第 11 节验证；使用新合同会话验证真实 provider 输入和图片交付。数据库能存图、单个工具测试通过或应用能启动，都不能单独作为完成依据。

删除旧实现及专门锁定旧结构的测试，不新增“字段已删除”的断言。保留并更新权限、事务、顺序、完整性、结算和前缀行为测试。报告已完成及仍缺失的消费者，未完成的必需链路不能通过临时兼容路径开放正常使用。

## 11. 验收要求

以下为未来实施验收要求，本次未执行这些测试或 provider 实验。

| 场景 | 必须证明的行为 |
| --- | --- |
| 用户图文 | text/image/annotation 顺序、原换行、身份完整读回；inline/blob 消费等价 |
| 队列和 steer | 原内容原子转移，失败仍归队列；成功经 entry 展示；并发不重复消费 |
| 类型与更新权限 | tool_call 不能挂到用户／结果／队列；允许既定队列归属转移但不改身份、顺序、载荷和 session/workspace，不任意重挂已接受内容 |
| 排队改 steer | 普通及 Plan 转向不复制正文；旧队列只留状态，转移失败原状回滚；消费确认不再要求双份正文 |
| Plan 审批 | OPEN 审批保留 RUNNING 原轮且无预先成功结果；决定或超时只关联一次最终结果；修订／取消审批／超时在同一只读轮继续，批准才建立执行续接；直接及队列转向的图文／纯图片反馈按原顺序接受一次，来源、图片资格和回滚正确 |
| 运行时材料 | 本次输入按主指令、ROOT 历史材料、依赖结果、其他材料顺序接受，initial_entry_id/consumed_entry_id 指向第一条主指令；材料内部顺序确定，既有上下文不重排；来源、依赖就绪及整组提交／计量／压缩正确，空材料和未满足依赖可区分，队列不留第二份正文 |
| commentary + tool_call | 历史保留文字排除调用；执行和协议仍取得完整调用顺序和参数 |
| 思考与回放 | 对话不返回摘要／密文，适用 UI 显示及精确回放仍正确 |
| 完整业务值 | JS/原生/导出同源，缺失/null、类型、顺序及失败值保持，无预览权威 |
| 原生范围与 NEXT | 首屏及正常续读记录原结果与实际起止范围，不另存页面／后缀；恢复既有观察不按新预算重切，不重跑 NEXT 或依赖临时引用有效性；再次调用允许不同页长；读取状态不覆盖原业务事实，外部业务 next 不被删除或执行 |
| NEXT 失败 | 已定位原结果但交付失败时保留其 Target 和未交付位置；无法定位时不伪造原结果 Target，已保存失败值按普通错误结果处理；未产生业务值时不伪造结果 |
| 大结果与资源失败 | 现有 blob 路径完整读回单个超旧 16 MiB 上限的 text/object 及多项大结果；不改变公开项边界或 JS 类型；真实数据库、驱动或内存失败不伪造结果、不重做效果、不截断 JS |
| 视频 | object 可由 JS 访问，原生确定 JSON 投影，观察顺序保持，补页不重解码 |
| 说明跨页 | 索引在上一页，下一页从图开始；无重复、跳图、回退或绑定分组 |
| 超大单图 | 图独占，长说明及 object 仍分页，不借例外超额；真实准入失败准确报告 |
| 图片资格 | A→B→A 三次出现；未消费／取消／拒绝队列独有图片不可再读；已消费进入会话的 Cell steer 图片可由 view_image 按原 workspace 边界再读；首次投递不循环依赖历史资格 |
| 图片属性 | 同图尺寸从 blob 取得，不在可视化记录重复维护；原可视化状态、失败及其他媒体语义保持 |
| GC | 转移后 blob 受保护；删一个出现不误删其他内容／可视化仍引用的 blob |
| MCP | 正式交错、audience 各分支、structuredContent、携图错误及损坏图片；无 Base64 第二正文 |
| Cell | 显式 steer 整条交付，已进入会话的图片引用可再读；steer 本身无 read_file 读取／续读／导出入口，无接纳回执或自动 ROOT 披露 |
| 前端及批注 | 草稿到 entry 接续，引用／评论身份及文字定位保持，不擅自嵌图 |
| fork、检索 | 新合同 fork 来源与图片资格正确；索引可重建，QA／QAQ 保留混合助手项文字 |
| 实际 provider | Chat Completions/Responses 的 tool 文本和关联 user 图片正确；同 epoch SYSTEM/tools 不变、messages 只追加 |
| 停用与恢复 | 新库实施期间不允许旧应用／调度正常运行；必要消费者和端到端验证完成后才恢复，无新旧兼容路径 |

## 12. 当前生产接点

- [数据库 baseline](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)
- [用户图文 codec](src/pulsara_agent/conversation_kernel/prompt_content.py)
- [图片引用和资格](src/pulsara_agent/conversation_kernel/prompt_storage.py)
- [队列和 steer](src/pulsara_agent/conversation_kernel/_repository/prompts.py)
- [助手块写入](src/pulsara_agent/conversation_kernel/_repository/kernel.py)
- [助手接受、可视化及回放写入](src/pulsara_agent/conversation_kernel/_repository/conversation.py)
- [工具全文和预览](src/pulsara_agent/conversation_kernel/tool_outputs.py)
- [历史文字解码](src/pulsara_agent/conversation_kernel/session_text.py)
- [历史阅读](src/pulsara_agent/conversation_kernel/session_content.py)
- [QA／QAQ](src/pulsara_agent/conversation_kernel/history_projection.py)
- [fork 历史](src/pulsara_agent/conversation_kernel/fork_history.py)
- [blob 与 GC](src/pulsara_agent/conversation_kernel/blob.py)
- [provider 回放与思考投影](src/pulsara_agent/llm/provider_replay.py)
- [批注类型](src/pulsara_agent/llm/input.py)

真实样本参考 [reasoning entry 实验](output/reasoning-entry-probe-20261112/README.zh.md)。它是本地证据，不作为生产协议或旧会话兼容依据。
