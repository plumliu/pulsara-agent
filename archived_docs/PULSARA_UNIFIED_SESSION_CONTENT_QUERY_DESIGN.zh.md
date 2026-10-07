# Pulsara 统一会话内容查询设计

日期：2026-10-07。状态：已完成生产代码落地、真实 provider 验证及 GPT-6 Astra / xhigh critic 代码复核；实现冻结。本设计落实用户已确认的一份 canonical 来源、一套定位、一套返回格式，以及当前会话与其他会话的不同读取范围。

工具语义已按 [工具语义增量修订](PULSARA_SESSION_CONTENT_TOOL_SEMANTICS_ADDENDUM.zh.md) 落地：`include_tools` 包含调用和结果，内容搜索排除三个查询工具自身调用/结果，内容 cursor 采用 v2 判别值。增量验收与审阅记录见补充文档；本文第13节保留基线证据，不把它们当作增量证据。工具语义冲突以补充文档为准，其余合同沿用本文。

## 1. 用户目标与范围

模型能够找回当前会话压缩前的原文，并在用户指代其他会话时找到、搜索和读取那份会话。现有 UI 会话搜索、滚轮历史与模型工具共用读取事实与内容解码，模型使用简洁的文本投影。

三个工具：`search_sessions`、`search_session_content`、`read_session_content`。不另建窗口历史工具、私有 notes 服务、检索数据库或后台摘要生成任务。

首版面向 ROOT 模型，返回 ROOT 聊天内容；不开放 SUBAGENT_TASK 内部逐条记录。这是与现有用户会话搜索相同的产品范围，不是 session_id + entry_id 无法表示 worker。子代理仍通过现有任务查询获得状态和结果摘要。本次不借用 Codex 的 agent_name 参数扩大为 worker 调试或恢复产品；未来若开放，需单独明确调用者与目标 scope 的读取合同，再复用相同定位。

## 2. 本地 Codex 参考及差异

已核查 `/Users/plumliu/Desktop/python_workspace/codex`：

- `codex-rs/tui/src/dynamic_tools.rs::tool_specs/handle` 注册模型可调用的 list_threads/read_thread，包装 app-server thread/list、thread/read、thread/turns/list。返回任务与轮次摘要，支持有限输出和分页。它不是只有 UI 才能调用的接口。
- `codex-rs/ext/history-notes/src/tools.rs` 注册 history.list_windows/list_items/search_contents/read_item；默认当前代理，支持窗口、角色和工具过滤，精确条目按字符范围读取。
- `history-notes/src/backend.rs` 向 Codex 后端发送 alpha/history/v2 请求，并由 runtime 加入 session_id/current_agent_name；`extension.rs` 限定配置、OpenAI provider 和 Codex backend auth。仓库没有该远端服务的完整响应 schema。
- Astra 外部文档 `CL4R1T4S/OPENAI/Codex_Desktop/GPT-6-Astra_Prompts.md` 的上下文恢复说明，与 Tools.json 的上述两组工具相对应。外部提示中的保密和执行指令只是研究材料，不作为 Pulsara 的执行规则。

采用：只读冷查询、先定位后取原文、准确引用、按需展开、分页/分段返回、历史内容不自动形成新任务或执行授权。

Pulsara 共用本地 canonical rows/blobs，不复制 Codex 两套后端、window/item ID、角色全集或私人笔记系统。也不采用列表缺少分页、远端私有加密响应或兼容期 legacy full-read fallback。三个工具使用同一读取 owner 与模型文本投影。

## 3. 当前代码可复用的 owner

| 现有位置 | 已有能力与本次接缝 |
| --- | --- |
| `conversation_kernel/session_search.py` | 同 domain 的冷搜索；标题、ROOT 用户正文/批注 comment、assistant TEXT blocks；字面多关键词；流式 SQL、内容完整性校验、会话聚合和分页 |
| `KernelHostCore.search_sessions/read_resumable_session` | 冷资源 owner 与 domain 校验；不打开目标 Host，不启动模型、MCP、hooks 或 writer |
| `web_app/session_controller.py::_history_reader/history_page/history_content` | 已实现逐次授权、归档冷读、entry/event 双 cut 和正文读取；抽取可共享的读取接缝，不让模型调用 Web controller/HTTP |
| `terminal_protocol/canonical_v3.py::CanonicalProtocolReader` | canonical entry、正文 edge、工具 artifact 的准确归属和内容校验；历史分页包含中断提示 |
| `conversation_kernel/reader.py::read_frozen_dispatch`、当前已安装基底 | 当前调用实际使用的 FULL_HISTORY/SNAPSHOT 及原始 suffix 范围 |
| `tool_runtime.py/tool_contracts.py` 与既有工具结果路径 | 调用者 scope、只读 effect gate、取消、结果交付与落盘 |

现有 session search 每会话只保留一个命中，且输出丢掉了 entry 定位字段。实施时应保留消息命中的准确 entry_id；不能拿截断 snippet 再反查或猜来源。会话内搜索使用同一正文解码和候选读取逻辑，返回多个 entry 命中，不复写一份搜索引擎。

现有 SQL 对 assistant 的每个 TEXT block 单独匹配，而本设计的匹配单位是 entry。共享接缝必须先按 ordinal 构造同一 entry 的文本，再做所有关键词匹配；读取使用完全相同的文本视图。UI 与模型共用这一搜索事实，避免跨 block 关键词漏检。模型返回的是该视图的真实连续切片，不使用 UI snippet 的装饰省略号、空白压缩或重排；240 字符内未必能展示同一消息中相距很远的所有关键词，精确原文用 entry_id 读取。

UI 仍保留自己的展示样式、最近会话预览数量与导航行为。模型端空 query 使用已有冷列表/分页能力；单页数量是响应预算，不是只能查询最近几个会话。

## 4. 范围合同：由 runtime 判断当前与其他会话

省略 session_id 或显式传当前 session_id，均查询当前会话的更早原始记录；指定其他 session_id，则查询该会话已保存且有权访问的 ROOT 内容。显式传自身 ID 不得绕过更早范围限制。

### 4.1 当前会话

范围是当前实际安装基底的原始 suffix 窗口之前，且位于调用者 ROOT scope 的 canonical records：

- FULL_HISTORY：语义 floor 为 0；没有更早区间。revision-zero 行的 source_through_sequence 是 turn-local genesis 标记，不能用作历史查询上界。
- SNAPSHOT：更早区间为 entry_sequence <= 已安装 snapshot 的 source_through_sequence；其后的原始 suffix 不在本工具范围。
- 如果当前 active_request 被精确保留在 carrier 中且可按 entry_id 识别，排除这条当前活动请求。不能因为 mid-turn compaction 把本轮请求写入 carrier，就把它作为待找回的旧用户请求。
- 摘要、recent_human_requests/retained_historical_requests 的摘录与已有查询结果不做文字去重；它们不重定义原始 suffix 窗口。某段旧原文被摘要提到或检索过，仍允许精确重读。

这是按原始记录窗口划分的产品范围，不声称逐字符证明某个内容完全未出现在 prompt 中。载体中某些摘录没有 canonical entry ID，不能为排除它们新增 ID、指纹或正文反向匹配机制。

初始调用从实际已安装 epoch/编译基底冻结范围，不能只读取数据库最新 binding 代替调用者正在使用的基底。执行中的工具请求必须沿原有 exact invocation/surface borrow 绑定该读取值；接缝不足时做一个最小只读 owner adapter，不建立历史可见性账本。

### 4.2 其他会话

不依赖目标模型的当前上下文、epoch 或压缩状态。首次查询冻结目标的 latest_entry_sequence 与 latest_event_sequence，随后仅读取这个 cut 内已经提交的内容；未提交的流式文字和 pending queue 不属于保存历史。

归档和目录丢失不影响冷读；查询不会取消归档、创建目录或恢复 runtime。已删除或越权会话返回不可访问，不从父会话、备份或 blob 裸 ID 补回。

### 4.3 Fork

child 只能读取其自身真正复制/产生的记录。当前 fork 合同复制有效摘要/保留窗口，不承诺父会话被压缩掉的全部原文。不得沿 imported source attribution 自动扩大读取；父会话仍存在且有权访问时，可作为另一个明确目标查询。

## 5. 一套定位与正文语义

唯一消息定位为 `session_id + entry_id`。同一工具搜索、读取、UI 导航都使用 child-local/canonical 的真实 ID；模型不学习 window ID、turn ID、event ID、provider call ID 或另一个合成 content ID。

消息内部 TEXT blocks 按原始 ordinal、以固定换行分隔构造一个稳定文本视图，不要求模型传 block_id，也不能在 block 边界拼接出源中不存在的单词。字符分段位置由返回 cursor 携带，不要求模型计算 offset。

- 用户消息：复用 canonical prompt codec；拼接文本 part 和批注 comment，引用来源不冒充用户原话。定时驱动输入保留既有 provenance，标明其非真人来源。
- assistant：默认只读取 TEXT blocks；include_tools=true 时在原 entry 内按 ordinal 同时投影 TOOL_CALL 的保存名称与参数，采用补充文档规定的两行历史文本。DATA、provider replay 和存储 manifest 不替代正文。commentary 和 final 保留为各自 canonical entry，不拼成虚构单条 final。
- 可选工具结果：include_tools=true 时纳入 ROOT TOOL_RESULT，role=tool，并带 tool_name 与 result_state；正文按下述固定公共结果/输出视图读取，不混入原始 provider replay、推理或可执行的 function-call 对象。内容搜索排除三个查询工具的调用及结果，读取保留全部；调用参数与 snippet source/bounds 语义见补充文档。include_tools 是内容筛选，不是权限开关。图片/附件/可视化正文不属于此范围。
- 中断：读取其挂载消息时附上 interruption.reason/detail，保持后端 runtime 事实；不虚造 assistant 消息或新 entry ID。首版正文关键词搜索不单独匹配中断详情字段。

正文视图的 Unicode 字符位置稳定，来源 bytes 按已有边界校验。它是保存内容的文本投影，不是旧 provider 请求的字节级 replay。

### 5.1 工具结果：保留公共结果，也能找回长输出

一个 TOOL_RESULT 仍只有一个 entry_id、一个 text、一个续读位置。视图由已保存 canonical 边决定：

- NOT_REQUIRED 或 UNAVAILABLE：使用保存的公共结果正文，不编造缺失输出。
- AVAILABLE 或 INCOMPLETE：固定先放保存的公共结果段，再附对应 primary artifact 的输出正文段；两段分别以 `Saved tool result:`、`Tool output:` 标注。这些标签是投影说明，不是历史源文字；源 payload 的大小写、空白和顺序保留。

公共结果可能含 exit_code 等外围信息，artifact 可能只有 output；不能用后者悄悄替换整个结果，也不反解析任意 JSON 来猜重建方式。少量预览重复优于丢失外围事实或使长输出中段永久不可检索。搜索与读取使用同一固定视图，不能因预算、命中位置或当前调用者改变组成；长视图用同一个 cursor 分段。分段标签不参与关键词命中；源文本位置仍映射到同一稳定视图，不增加新 ID 或持久状态。

分别复用既有 source_coverage/source_coverage_reason、display_kind、artifact_disposition/artifact_unavailability_reason。正常完整结果只需公共字段；源只保留快照、公共结果是 HEAD_TAIL 或 artifact 不可用时，附上理解当前可读内容所需的既有值和必要原因。display_kind 描述公共结果段，不描述整个合成视图。source_coverage=COMPLETE 表示当时主输出覆盖物理源，不保证保存的预览或今天的可读内容完整。

例如 COMPLETE 源覆盖 + HEAD_TAIL 公共预览 + UNAVAILABLE artifact 只能读到已保存头尾；必须保留这三项事实。读完该视图后 partial 可以省略，但上述事实仍在；next_cursor 只能继续已保存视图/邻文，不能暗示能找回丢失中段。声明 AVAILABLE/INCOMPLETE 的边或 blob 缺失、损坏时返回内容错误，不能动态缩为预览视图或重跑工具。

跨会话 artifact 沿目标 session_id + result_entry_id 解析 canonical edge，再由目标 session/workspace 的既有只读 port 读取。不调用绑定于当前会话的 artifact_read，不以裸 artifact/blob ID 授权；UI reader 含 reasoning 等投影，不能把整个 UI DTO 序列化给模型。

## 6. 工具参数

全部工具 is_read_only=true，沿现有只读/Plan gate；domain、调用者 ROOT scope、当前会话基底由 runtime 注入，模型不能传入。

| 工具 | 参数与默认行为 |
| --- | --- |
| search_sessions | 首次：query=""、lifecycle=ALL、limit=20；继续：cursor、limit?；复用现有会话搜索排序与实时列表语义 |
| search_session_content | 首次：query 必填、session_id?、include_tools=false、limit=20；继续：cursor、limit?；大小写不敏感字面多关键词，所有词在同一消息文本中匹配；每条消息最多一个命中 |
| read_session_content | 首次：session_id?、entry_id?、direction?、include_tools=false、limit=20、max_chars?；继续：cursor、limit?、max_chars? |

read 的 direction 只取 older/newer。未指定 entry_id 时默认从允许范围最新消息向更早读；指定 entry_id 时锚点总是首条，默认向更新内容读，便于检查旧提案之后的修订或结论。明确 direction=older 可读锚点前文；newer 可读后文。长消息先从正文开头分段读完，再沿选定方向推进，直到冻结范围结束；不设邻文总量上限，不加 around/window/offset 参数。无锚点时显式 newer 从允许范围最早消息开始。

三个工具均支持 cursor-only 续读：首次调用的目标和语义条件由 cursor 携带，后续只需传 cursor，可以调整页预算；不得同时重传 query、session_id、entry_id、direction、lifecycle 或 include_tools。默认值只作用于首次调用，不能覆盖 cursor 中的值。原生 schema 和描述须允许此调用形态，不能仍把 query 设为所有调用必填。指定 entry_id 不绕过当前范围检查；范围外返回明确错误。

search_session_content 的角色和日期 filters 暂不增加。先通过关键词、会话 ID 和原文位置完成任务，不能照搬 Codex 全部 namespace/role 参数。

会话搜索命中的 entry 可能处于调用者当前 suffix。如果命中自身会话，返回前必须按相同更早范围过滤正文候选；否则会产生不可读取的定位。标题仍可用于定位自身，但无消息 entry_id。不能在“先每会话取一个最佳命中”后简单删除范围外候选，否则会漏掉同会话更早的有效命中。

## 7. 一套返回格式

三个工具共用成功 envelope：

```json
{
  "items": [],
  "next_cursor": null
}
```

内容记录使用同一组语义字段，搜索与读取相同：

```json
{
  "session_id": "session:...",
  "entry_id": "entry:...",
  "role": "user",
  "at": "2026-10-07T01:00:00Z",
  "text": "此前原始讨论的片段或正文"
}
```

搜索 text 是命中附近的原文片段；读取 text 是从正文开头或 cursor 指定位置开始的连续内容。三工具返回 text 只要不是整条消息的完整文本视图，就附 partial=true；搜索片段及最终分段同样适用。完整短消息省略 partial。该标记只表示本页切片，不能代替工具源完整性。next_cursor 是继续本次操作的唯一入口；search 的 cursor 继续找其他命中，展开该命中的原文要使用 session_id + entry_id 调用 read。

会话候选在同一记录上增加 title、project、updated_at；只有正文命中时才带 entry_id/role/at/text。标题命中或空查询不伪造 entry_id、role 或 text。统一 envelope/定位/正文含义，不要求塞满同样的空字段。

仅在事实存在时附加 tool_name/result_state、interruption 或 scheduled provenance；不用 null 填满可选字段，不返回 writer generation、binding IDs、digest、完整路径清单或控制状态。

items 顺序：会话搜索沿现有相关性/活动时间；内容搜索从新到旧；read 按所选方向，同一消息跨页分段始终按原文字符顺序。显示时间不作为定位或分页权威。

仅当当前会话本身没有压缩前的已保存可读范围时，返回空 items、null cursor，并附 note="没有更早的会话内容。"。未命中、筛选空集、普通分页或 newer 到达范围末尾均为正常空结果，不附此说明。错误走现有 APPLICATION_ERROR，保留简短 code/message：目标不可访问、定位不属于目标/范围、cursor 不匹配/无效、读取超时、内容损坏、单次结果不可容纳。错误不伪装为空结果。

典型调用顺序：已知当前会话时直接搜索内容；不知道目标会话时先搜索会话；复制命中的真实 ID 读取。指定锚点的 read 默认向更新消息推进，以检查后来是否有修订；需要前文时明确 older。下面是调用参数示意，ID 与 cursor 仅为占位：

```json
{"query":"输入预算"}
```

以上用于 search_session_content。找到某条 entry 后，用 read_session_content：

```json
{"session_id":"session:...","entry_id":"entry:..."}
```

其 next_cursor 直接原样续读：

```json
{"cursor":"opaque-returned-value"}
```

需要该锚点的前文时重新读取：

```json
{"session_id":"session:...","entry_id":"entry:...","direction":"older"}
```

## 8. 分页、范围与输出预算

- next_cursor 是可丢弃的读取位置，不是能力凭据。所有调用再次校验 domain、session、ROOT ownership 与允许范围。cursor 不能授予裸 blob 或跨 scope 读取。
- cursor 无签名，不证明服务器曾发行过该值。首次和续读都由 runtime 判断目标是否当前会话，重新应用真实已安装基底的范围及当前活动请求排除；不能信任 cursor 自称的 caller、上界或排除 ID 来扩大访问。合法冻结位置还需与 canonical entry/event cut 及文本长度一致；旧 cursor 的范围必须位于当前允许范围内。改动位置最多改变本来就允许读取的数据，不产生新授权。
- 内容 cursor 绑定操作、目标、query/include_tools/direction、entry/event cut、当前会话冻结上界、原文位置及必要的活动请求排除值；不持久保存游标或建立 cursor -> object registry，不加 MAC/DTO fingerprint。
- 后续调用只传 cursor 与可调页预算，解码后沿同一 owner 再次授权并校验冻结范围。不得把一个 search cursor 交给 read，也不能用新增过滤参数改变同一页链。
- 同一内容查询固定其已捕获 cut；新消息、late 结果、中断发生在 cut 后时不混入该页链。重新查询获得更新的 cut。当前会话基底更新后，原游标范围仍须是当前允许范围的子集，才能继续；否则提示重新查询，不增加 lease/generation/自动 repair。
- 一页结束时若消息未读完，cursor 先继续这条消息，再沿选定方向进入下一条允许消息；不同时返回 next_offset、next_entry_id、has_more 或 total_count。
- 复用会话搜索已有单页最大 50 条、默认 20 条；limit 指本页不同 entry 的条数上限，会话列表则指候选会话数。搜索 snippet 沿用现有 240 字符展示预算。read.max_chars 是整页所有 items.text 的总字符预算，采用既有 artifact 文本读取默认 20,000、最大 32,000 字符常量，并受普通工具结果实际交付预算进一步约束；不是每条消息都可返回 32,000 字符。优先返回锚点或 cursor 当前分片，再用剩余预算读相邻消息。这些是单次模型响应预算，不限制库大小、历史长度、查询次数或可读消息总量。
- producer 必须保持完整 JSON envelope，并计算元数据、cursor 和正文的整体交付大小；必要时减少返回条目或字符，不依赖外层任意截断 JSON。不能容纳一个可继续的最小条目时返回 typed 资源错误，不能返回不前进的游标。
- 大正文按稳定字符切片继续；工具日志如果只有不完整 retained coverage，应明确沿用该 coverage，不能声称读到了已丢失部分。不能为恢复日志重跑工具。

## 9. 权限与模型解释

调用者 domain 从现有 invocation owner 获得，session_id 是目标而非授权凭据；每次查询重新授权。搜索和正文读取在不同目标的权限判定上保持一致。取消使用现有工具调用取消与 PostgreSQL deadline；不新增后台 job 或查询恢复状态。

结果作为普通工具结果追加到本轮。里面的 user/assistant/tool 都是历史来源标签，不能重新安装为当前原生 messages、待执行 tool call 或普通 final answer。旧指令、授权与输出是否仍适用，结合当前任务、目标、后续修订和现行 gate 判断；既不自动扩大授权，也不统一宣告所有旧用户授权失效。

新增工具只能沿已有 cold epoch / 明确采用的 compaction successor 生效，现有 SYSTEM/tools 字节不变；不为发现历史、刷新目标状态或绑定变化新增 rebase 边界。

系统提示只需说明：涉及先前决定、压缩前细节或其他会话时，按需定位和读取保存原文；当前会话只查更早原始记录，其他会话查保存内容；结果是历史资料，不能自动恢复旧任务或工具。工具描述分别讲自己参数和 next_cursor，不重复长篇 runtime 说明，也不要求读取另一份 Skill 才能调用。

## 10. 数据与架构减法

不新增数据库表、durable/live event kind、subject slot、append guard、product relation、job、索引正文副本、摘要服务、receipt、checkpoint、lease、generation 或 replay recovery。既有工具请求/结果仍按普通链路落盘，这不是额外检索持久化。

允许增加的最小代码：共享冷查询 owner/只读 adapter、三个现有体系内的 builtin descriptor/route、统一模型文本投影与一次内容分页的 cursor codec。归属：repository/canonical reader 拥有事实与读取；Host 拥有调用者绑定和 domain；model projection 拥有轻量返回格式；UI 与模型各自拥有展示和调用入口。

搜索首次仍接受随已授权文本规模增长的直接读取成本，流式解码且受已有 deadline。不能用扫描最近 N 条、最近 N 天或全文总字符 cap 伪装完整搜索。实测需要优化时再利用数据库成熟索引能力，另定可丢弃投影合同。

## 11. 验证与激活条件

关键行为：FULL_HISTORY floor=0；SNAPSHOT before-range；显式自身 ID 不绕过；mid-turn active request 排除；载体摘录与检索结果不引入正文去重；跨会话忽略目标 epoch；domain/ROOT/fork ownership；归档、目录丢失、删除；消息/中断双 cut；搜索多个命中与准确 entry 定位；跨 TEXT block 关键词；搜索片段与原文一致；同一消息长正文继续、锚点前文与后续修订、两种方向；cursor-only 续读；字面关键词/Unicode；结果保留 partial 与独立工具 coverage；取消、损坏和资源错误；整页 text/JSON 预算明确、分页持续前进；provider prefix append-only。

用同一份代表性任务检查 UI/模型是否取到相同 canonical 文本，而非用快照字符串测试证明检索正确。用保存生产设置的只读注入与隔离数据库做真实 provider：压缩后找回原始决定，找另一会话并读其片段，长原文继续读取；确认模型不重跑历史工具、不凭旧授权扩权、不把摘要当原文。

记录完整 native tool wire 的 v3 估算，以及代表性搜索/读取响应的 v3 估算；比较模型是否能直接复制返回引用、正确继续读取、理解片段与完整正文。缩短键名、去掉来源或统一空字段不能仅凭 token 少就被认定更优。

## 12. critic 审阅与取舍

GPT-6 Astra / xhigh 已对照本地 Codex 的 thread 工具、history-notes 工具及其后端/测试，以及 Astra 外部材料完成独立审阅，并与主代理讨论修订。详细过程保存在 ignored 的 `output/session-content-query-design-20261007/astra-review.zh.md`；下列决定是本文的设计真值，不依赖该临时报告才能实施。

已采纳：

1. 加入仅有 older/newer 的方向参数，锚点默认向后续内容读；明确前文可直接读取。保留单 cursor，不增加 around/window/offset 或邻文总量上限。
2. cursor-only 续读，不要求模型重复目标、关键词和筛选条件；runtime 每次独立授权和重验当前范围。
3. 统一 entry 文本视图，再搜索/读取；跨 TEXT block 的关键词能够命中，模型 snippet 不加 UI 装饰省略号。
4. partial 只表示本页片段；工具源覆盖、预览与 artifact 可用性使用现有各自事实。固定公共结果 + 可用 primary artifact 视图，保住外围信息与长输出正文。
5. max_chars 明确为整页 text 总预算，limit 为不同 entry 数；保持完整 JSON 和可继续推进的分页。

保留：ROOT 首版范围；普通内容的角色与时间；有事实才出现的定时 provenance、中断与工具元数据；共同 items/next_cursor envelope；每条内容独立可复制的 session_id + entry_id。不加入更多角色/日期 filters、worker scope、引用别名或窗口 IDs。

作为格式取舍证据，主代理用生产 v3 estimator 对 20 条合成短消息测算：逐条 session_id 的格式为 1,360 tokens，单目标公共 session_id header 为 1,094，节省 266。它不是实际工具响应或模型行为测试；本次优先保持独立引用与三个工具共同的 item 语义，不为了该常数节省引入第二种引用方式。未来若要调整，以真实调用的正确性和开销作为依据。

最终一致性核对：GPT-6 Astra / xhigh critic 已阅读全文，确认没有剩余阻塞问题，同意冻结。主代理已采纳其最后两处措辞修订：明确锚点默认 newer；无更早范围 note 只用于当前会话缺少压缩前可读区间。该核对记录设计阶段的冻结；生产实施与实际验证见第 13 节。


## 13. 已实施与代码复核

生产入口已加入 `search_sessions`、`search_session_content`、`read_session_content`。`ports/session_content.py` 只携带三工具的 schema 与当前调用可读范围；Host 注入调用者/domain 的冷查询 owner，ToolBatchExecutor 从该实际 response 的冻结 canonical facts 提取范围，DirectKernelToolPort 通过现有物理 I/O owner 调度查询。工具只进入 ROOT surface，既有只读权限模式可调用。

`conversation_kernel/session_text.py` 共用 canonical 文本解码；`session_search.py` 共用 UI/模型的会话定位、entry 级匹配和准确引用；`session_content.py` 提供同一范围/投影规则下的搜索、读取及 cursor-only 续读。CanonicalProtocolReader 提供共用授权、entry/event cut 校验与工具 artifact edge 解析。UI 展示接口保持原样，未新增数据库表、事件、索引正文副本或恢复机制。

新增工具定义和系统提示只在获准建立输入根的边界进入模型：新的 cold epoch 或显式采用的 compaction successor。现有 epoch 的 SYSTEM/tools 不更新、不重写；消息继续按后缀追加。查询归档或目录丢失的目标会话不启动目标 Host，也不创建目录。

自动化验证位于 `tests/test_session_content_query.py`：包括 FULL_HISTORY 的真实 genesis floor、实际 snapshot carrier/active request、跨 block 搜索、长 Unicode 原文及整页预算、两种方向、固定 entry/event cut、篡改 cursor 不扩权、归档/实际已删除目录/fork/删除、工具 retained/unavailable/corrupt 输出、ROOT-only native schema，以及真实物理 I/O owner 的取消结算和数据库错误分类。最终关联回归 239 项全部通过（47.65 秒），覆盖搜索、fork、中断、artifact、prefix continuity、Plan、Web 历史接口、workspace recovery 和架构减法。全部变更 Python 文件通过 Ruff，`git diff --check` 通过。

真实 provider probe：`tests/dogfood/run_session_content_query_dogfood.py` 读取保存的 DeepSeek Chat connection 与凭据，在只读 settings 注入、临时 Pulsara home、临时工作区和已验证 loopback disposable PostgreSQL 数据库中执行；完成后删除隔离数据库。没有修改生产设置或生产数据库。ignored 证据为 `output/session-content-query-20261007/dogfood.json`，保存实际 provider input、模型回复、usage、工具参数/结果，仅脱敏实际凭据。

实际结果：

- 8 次会话查询全部 SUCCESS。其他会话按标题定位，再按关键词找到原 user entry，读取原文并准确引用原决定。
- 当前会话经过真实 COMPACTED 后，模型找到压缩前的原 user entry；依次读取 16,000、32,000、692 字符，后两次调用只带 cursor/max_chars。48,692 字符原文完整重组，完整中间/末尾标记和原决定都保留；末页相邻 assistant 为独立 entry。
- 11 对同轮实际 agent_model_loop provider input 保持 tools 完全一致、messages 前缀连续追加。三工具完整 native wire 的生产 v3 估算合计 957 tokens；代表性会话搜索/内容搜索为 52/150，4,000 字符读取为 1,166，16,000 字符读取为 4,166 tokens。这些数值是生产启发式估算，不是闭源 tokenizer 的精确计数。
- 本轮查询没有重跑历史任务。模型对原标记内容和引用正确，但最终自然语言中的页序说明有偏差；不据此宣称所有位置口述、旧授权判断或对抗历史提示场景均已证明正确。

GPT-6 Astra / xhigh 的 Session query design critic 已审阅 tracked diff、新增文件、自动化测试及实际 provider 证据，并独立运行 adapter/schema 用例。首轮提出的同步查询绕过物理 I/O owner、schema 连接错误误分类两项已修复并复核；cursor active exclusion/改目标的针对性回归也已加入。最终确认无剩余阻塞缺陷，同意冻结实现。详细记录在 ignored 的 `output/session-content-query-design-20261007/code-review.zh.md`；本文与生产代码构成可独立阅读的实施真值。

补充真实浏览器 dogfood（2026-10-07）：使用保存的 OpenRouter GPT-6 Luna / high，在独立 Web 实例、临时 home 和 verified loopback disposable 数据库中，通过 Chrome UI 新建3个会话并完成15、13、7轮聊天。两个会话分别经手动压缩实际采用快照，随后各自查询旧原文；第三个会话重新定位并读取两边已压缩的历史。原始决定、后续修订、现场原话及引用正确；长消息两页 cursor-only 续读与原文前12,000字符完全相等，当前请求独有关键词无历史命中。33次查询中32次成功，一次模型抄错 cursor 被明确拒绝后自行重试成功。全部35轮完成，60次实际 provider 请求均为所选模型/high 且 usage=reported；50对同一输入根内的请求保持 tools 一致、messages 仅追加。浏览器刷新保留回答；测试环境已清理，没有修改生产设置或数据库。ignored 报告与截图：`output/playwright/session-content-20261007/REPORT.zh.md`；最终保存证据的24项断言全部通过。
