# Pulsara 会话内容查询：工具语义增量修订

日期：2026-10-07。状态：增量生产代码、137项关联回归与真实 provider 增量验收已完成；原 Session query design critic（GPT-6 Astra / xhigh）代码审阅及最终证据复核通过，实现冻结。

本文补充并修订 [统一会话内容查询设计](PULSARA_UNIFIED_SESSION_CONTENT_QUERY_DESIGN.zh.md)。冲突时，以本文对工具调用、工具结果和搜索候选的规定为准；未涉及的权限、范围、定位、分页、错误、冷读取和 provider prefix 合同继续沿用原设计，不重新开放原设计已经冻结的其他产品范围。

## 1. 用户确认的合同

保留三个工具及现有 `include_tools` 参数，不新增工具、开关、定位标识或返回 envelope。

| 工具 | 普通正文 | 工具调用 | 工具结果 |
| --- | --- | --- | --- |
| `search_sessions` | 搜索标题、ROOT 用户正文、assistant TEXT 正文 | 不搜索 | 不搜索 |
| `search_session_content`，默认 `include_tools=false` | 搜索 ROOT 用户/assistant 正文 | 不搜索 | 不搜索 |
| `search_session_content`，`include_tools=true` | 保留普通正文搜索 | 搜索工具名与保存的参数；排除本节指定的三个查询工具 | 搜索保存的公共结果和已有规则允许的 retained 输出；排除本节指定的三个查询工具 |
| `read_session_content`，默认 `include_tools=false` | 读取 ROOT 用户/assistant 正文 | 不读取 | 不读取 |
| `read_session_content`，`include_tools=true` | 保留普通正文读取 | 读取保存的工具名与参数 | 读取保存的公共结果和已有规则允许的 retained 输出 |

唯一的搜索排除集合是 `search_sessions`、`search_session_content`、`read_session_content` 自身。只对 `search_session_content` 的工具调用与工具结果候选应用，不影响普通正文，不应用于读取。复用既有 `SESSION_QUERY_TOOL_NAMES`，不另建分类注册表。

读取无需为这三个工具设置特殊允许规则：`include_tools=true` 的正常行为已经包含所有工具调用与工具结果。`search_sessions` 无 `include_tools` 参数；不为了与内容工具形式一致而扩大定位工具的搜索范围。

## 2. 修订范围与现状

增量之前的基线只投影 assistant TEXT blocks，`include_tools=true` 只增加 TOOL_RESULT，没有投影 TOOL_CALL，也没有排除查询工具自己的结果。这条路径已被以下统一语义替换；`search_sessions` 的用户文本和 assistant TEXT 来源保持不变。

本次实施已经：

1. 把两个内容工具的 `include_tools=true` 扩展为同时纳入 TOOL_CALL 和 TOOL_RESULT。
2. 让内容搜索排除三个会话查询工具自身的调用及结果。
3. 沿同一 canonical owner 返回保存事实，保持消息粒度和已有 envelope。

原设计第 5 节“assistant 只读取 TEXT”“工具参数搜索不属于首版”，以及第 6 节 `include_tools` 仅包括结果的解释，由本文替换。原设计的工具结果公共正文/artifact 规则保持不变。默认值仍为 false；不采用讨论途中提出的“默认搜索所有工具结果”。

## 3. 消息定位与混合内容

工具调用属于原来的 assistant entry，工具结果属于原来的 TOOL_RESULT entry。二者沿用各自已有 `session_id + entry_id`；不把调用和结果合并成新消息，也不为单个调用或 block 新建模型定位。

一条 assistant entry 可以同时包含 TEXT、DATA 和多个 TOOL_CALL：

- `include_tools=false`：与当前实现一样，只返回和匹配 TEXT。
- `include_tools=true`：在同一 entry 内按已有 block ordinal 投影 TEXT 和 TOOL_CALL；DATA、reasoning、provider replay 仍不进入正文。
- 搜索时只从匹配候选中移除三个查询工具的调用部分，不能因为消息含一个查询调用，就丢掉同条消息的 TEXT 或其他工具调用。
- 普通 TEXT 中提及这三个工具名称或复述某段结果，仍属于普通正文。不能按文字内容、工具名前缀或“是否只读”进行黑名单过滤。

同一 entry 的多个符合条件的段落可以共同满足多个关键词；不同 entry 不能共同凑成一个命中。一条 entry 仍最多返回一个搜索命中，`limit` 仍计不同 entry，而不是调用数量。

## 4. 历史工具调用的文本投影

不增加 `tool_calls` 数组等第二套模型返回形状。继续使用 `items`、`next_cursor`，以及内容 item 的 `session_id`、`entry_id`、`role`、`at`、`text`。工具调用随 assistant 消息返回，`role=assistant`；结果仍为 `role=tool`，保留既有 `tool_name`、`result_state` 等事实。

assistant 视图按 block ordinal 生成，纳入的 block 之间用一个固定换行分隔。TEXT 保持已解码原文；每个 TOOL_CALL 采用以下文本片段：

```text
Tool call: terminal
Arguments: {"command":"rg -n needle src"}
```

这只是 `item.text` 内的历史文本，不是向 provider 安装的原生 function call。

投影规则：

- 工具名和参数来自该 entry 的 canonical TOOL_CALL block，不从工具结果、assistant 口述或当前工具目录反推。
- 参数是保存的 JSON 值；直接复用 `primitives.context.canonical_json_bytes`，不另写序列化器。其对象键排序、Unicode 和紧凑编码规则决定稳定文本。保留保存值的类型、字符和内容，不声称保留 provider 原始参数字符串的空白或键顺序。
- `Tool call:`、`Arguments:` 及 block 分隔符是生成标签，不参与关键词匹配。调用的实际名称及参数 JSON（包括参数本身的键和值）参与匹配。
- 不增加 tool_call_id、attempt_id、block_id 或其他模型必须掌握的定位；需要核对结果时，沿相邻消息读取已有独立 TOOL_RESULT。
- 调用记录不证明曾获批准、开始物理执行或执行成功。没有结果的已保存调用也可读取、可按搜索规则命中；不能因为缺少结果或当前工具已经移除而隐藏它。

这份投影只拥有展示，不拥有执行。沿用 canonical typed rows 的真实性，不为参数新增 digest、指纹或 durable 正文副本。

## 5. 搜索排除：判断归属，不递归去重

`search_session_content` 在 `include_tools=true` 时：

1. TOOL_CALL 根据该 canonical block 的 `tool_name` 判断是否属于排除集合。
2. TOOL_RESULT 先根据既有 canonical result → assistant TOOL_CALL 边取得真实所属工具名，判断是否排除，再对保留的候选读取正文/artifact。不能信任结果正文自称的工具名或角色；归属边缺失或损坏时沿既有 `CONTENT_CORRUPT` 错误处理，不能猜测、静默跳过或当作普通结果放行。
3. 被排除的 TOOL_RESULT 整条跳过；被排除的 TOOL_CALL 只移除其搜索段，保留同条消息其他符合条件的内容。
4. 其他工具的保存调用、结果正常纳入，包括错误、拒绝、取消等保存结果；本次不增加状态过滤器。

过滤发生在候选匹配和页面计数之前。查询结果没有命中，不应占用 `limit`、制造空白占位或让分页停止；跳过它之后继续按既有范围扫描，不设置扫描总量上限。

不解析工具结果内部的 `items` 来递归展开来源，不沿引用自动读取别的会话，也不做正文相似度、摘要或跨 entry 去重。执行工具输出中如果出现这些查询工具的名称或类似 JSON，仍按其真实所属执行工具处理。

## 6. 搜索片段与读取正文的关系

每个 entry 在给定 `include_tools` 值下只有一份确定的可读文本视图。读取包含该视图允许的所有调用与结果；搜索在同一视图上使用符合条件的 source 段进行匹配，不另造一个缩短后的消息身份或位置体系。

`include_tools=true` 时，任何含 TOOL_CALL 的 assistant 视图（即使没有 TEXT）都应用以下规则：查询工具调用的 source 段不匹配；每个纳入视图的 block 同时提供完整展示跨度及可匹配 source 跨度。这些只是原视图中的 process-local 字符区间，不增加 ID。搜索 snippet 必须是该可读视图中的真实连续切片，把命中前后的上下文裁到命中的允许 block 展示跨度内，最多沿用现有240字符预算；不能只过滤匹配段，再对整条消息截取 hit±上下文，否则可能把被排除的查询调用夹带回 snippet。生成标签可以作为该 block 的展示上下文，但不能制造命中。

上述 block 边界规则仅用于开启工具且含调用的 assistant 视图。纯 TEXT 消息以及 `include_tools=false` 的 TEXT-only 视图，沿用现有按 ordinal 合并 TEXT 的行为，保留跨 TEXT block 的关键词匹配与片段规则。普通 TOOL_RESULT 沿用原设计公共正文 + artifact 的固定视图和片段规则，不套用 assistant block 限制；被排除的查询工具 TOOL_RESULT 在投影前整条跳过。

不同符合条件的片段共同满足关键词时，仍可命中同一 entry；单个 snippet 未展示全部关键词不代表匹配错误。展开时用返回的真实 entry 定位读取全文；读取中出现搜索没有纳入的查询调用是正常的，不是搜索误归属。

两个内容工具统一使用修改后的 source view builder。`search_sessions` 继续使用已有 TEXT-only 路径，不误带入调用参数，也不因此改变 UI 会话搜索。

## 7. 读取、长消息与旧阅读证据

读取不增加工具类别限制。`include_tools=true` 纳入全部保存 TOOL_CALL 与 TOOL_RESULT；`include_tools=false` 排除调用部分和结果 entry。明确锚定一条只有调用的 assistant entry 或 TOOL_RESULT，同时关闭开关时，返回已有 `ENTRY_NOT_READABLE`。混合 entry 有 TEXT 时，关闭开关仍可读取 TEXT。

调用参数过长时，和其他正文一样按稳定字符位置跨页切片。同一个 entry 保持同一定位，先读完这条消息，再沿 older/newer 推进；整页字符预算、条目预算、JSON 交付预算、`partial` 和 cursor-only 合同不变。`text` 内的参数 JSON 可以在字符分页处被切开；必须保持完整的是外层工具响应 JSON，不要求每页历史片段单独构成可执行参数对象。

工具结果继续沿原设计使用公共结果正文 + 可读 primary artifact 的固定视图。覆盖、HEAD_TAIL、INCOMPLETE、UNAVAILABLE 与损坏错误的区分不变。标为可读的 artifact 损坏时，不缩回公共预览；不会重跑历史工具。

例如 A 会话曾通过 `read_session_content` 读过 B：这次返回值仍是 A 的一条独立 TOOL_RESULT。A 的内容搜索跳过它；A 的历史读取开启工具后可展示当时保存的返回正文。正文中的 B 定位不变，但不自动展开，也不把其中的 role 重装为 A 的原生消息。要查询 B 的原文，显式查询 B，重新通过既有授权与范围校验。

## 8. 不变的范围、错误与输入根

- `include_tools` 是内容选择，不是权限开关。当前会话只查实际已安装基底之前的 ROOT 原始记录；FULL_HISTORY floor=0，SNAPSHOT 使用已安装 snapshot 上界，并保留 active request 排除。
- 显式传当前 session_id 不扩大范围；其他会话沿既有 domain 授权读取已提交 ROOT 内容，不依赖目标 epoch。
- 归档、目录丢失、删除、fork、entry/event 双 cut、取消、deadline、资源错误、消息中断与定时来源等原合同保持不变。
- 新的工具描述与系统提示只在新 cold epoch 或显式采用的 compaction successor 进入输入根。既有 epoch 的 SYSTEM/tools 保持 byte-identical，messages 按 suffix 追加。
- 不为本次投影修订增加数据库表、事件、索引副本、job、lease、generation、fingerprint 或运行恢复机制；已有历史事实直接可用，不迁移 canonical 消息。

### 8.1 内容 cursor 的一次 hard cut

`include_tools=true` 的文本视图会增加历史调用，旧 offset 可能指向不同文字；内容搜索候选也会变化。不能把旧页链静默解释成新语义。

沿现有 cursor 的 `operation` 判别字段执行一次 hard cut：内容搜索使用 `search_session_content.v2`，内容读取使用 `read_session_content.v2`。这是 opaque cursor 内部的判别值，公开工具名称不变，不新增参数或返回字段。`search_sessions` 的 cursor 不变。

新 decoder 只接受新内容判别值。原内容 cursor 使用既有 `CURSOR_INVALID` 明确提示重新查询，不提供旧投影、双读、自动转换或 repair。继续使用原参数约束、cut、上界、排除值和位置校验，不能把判别值当授权或真实性证明。模型只需原样复制新的 `next_cursor`，无需理解其内部格式。

## 9. 实施接缝与模型描述

复用已有 owner，而不是建立工具历史子系统：

- `conversation_kernel/session_content.py` 拥有 entry 选择、调用/结果的 canonical 文本投影、匹配段、排除集合应用和分页。当前 `_document` 接缝扩展到 TEXT/TOOL_CALL，保持 TOOL_RESULT artifact 路径。
- `conversation_kernel/session_text.py` 的现有 decoder、关键词和匹配语义继续复用；必要的 source 段适配只是展示数据，不成为新 authority。
- `ports/session_content.py` 与 `capability/builtin_catalog.py` 说明默认 false，以及开启后同时包含调用与结果；内容搜索描述一句三个查询工具自身的调用/结果不参与搜索即可。
- 读取描述只说明一般的工具调用/结果读取、参数分段和 cursor；不为三个查询工具重复写“特别允许读取”。系统提示仍按原合同说明历史用途，不重复整份工具协议。
- `conversation_kernel/session_search.py` 保持标题、用户正文和 assistant TEXT 的范围；Host、调用者范围绑定、物理 I/O owner、Web controller 与 canonical storage 不新增权威。

改动应作为完整 hard cut 落地，删除被替换的“include_tools 只表示结果”解释和选择路径。已有保存事实不是 legacy；不保留新旧语义运行开关。

## 10. 验收与证据

实施时在既有测试与 dogfood 基础上证明以下新增合同：

| 场景 | 必须得到的行为 |
| --- | --- |
| 三工具默认参数 | 定位只查文本；内容工具仍不纳入工具调用/结果 |
| 普通工具只有 TOOL_CALL、尚无结果 | 开启后可搜索/读取，名称与参数正确；关闭后不可按此锚点读取 |
| TEXT + 普通调用 + 查询调用 + DATA | 搜索保留 TEXT 和普通调用，查询调用不匹配、不进入 snippet，DATA 不显示；读取开启后保留全部调用，entry_id 不变 |
| 无 TEXT 的普通调用 + 查询调用 | 普通调用可命中，snippet 不越界带入相邻查询调用；只含查询调用时没有搜索命中；读取开启后两者可读 |
| 多个普通调用 | 按 ordinal 展示，同 entry 多关键词可命中一次；参数名和值都可查 |
| 三个查询工具各自的结果/调用 | 内容搜索开启后仍排除；普通读取开启后均可读，不需要额外开关 |
| 正文提到查询工具、其他工具输出相似 JSON | 正常命中，不按文字黑名单或 JSON 内容冒充所属工具 |
| 只存在工具参数/结果的关键词 | `search_sessions` 无正文命中；内容搜索开启后按其候选规则命中 |
| 错误/拒绝/取消结果 | 如已保存且属于普通工具，可搜索/读取，保留真实状态 |
| 长 Unicode 参数与多条调用 | 小预算连续续读可重组完整投影，单 entry 稳定，外层 JSON 完整，分页持续推进 |
| SOURCE/preview/artifact | 现有 retained、HEAD_TAIL、UNAVAILABLE、损坏行为不回退、不伪造完整性 |
| 新旧内容 cursor | 新 cursor-only 可继续；旧内容判别值拒绝；跨 operation/目标/范围攻击仍被既有校验拒绝；定位 cursor 不变 |
| 会话与调用者边界 | 当前实际基底、active request、跨会话、ROOT、fork、domain、归档/目录丢失/删除规则不变 |

补充一次使用保存配置的实际 provider 验证：普通工具调用名称/参数和结果可在压缩后找回；会话查询结果不污染搜索；已知其定位时能够读取旧查询证据；长参数可 cursor-only 续读；同一输入根内 SYSTEM/tools 一致且 messages 仅追加。可复用此前的 OpenRouter GPT-6 Luna / high，保存实际参数、正文、返回状态和引用，使用只读配置注入与 verified disposable DB，结束后清理。

已有 DeepSeek/API 与35轮 OpenRouter/浏览器证据仅证明基线实现；本次新增工具调用和搜索排除语义使用独立增量证据。

### 10.1 本次增量实施结果

生产代码使用一条投影路径：assistant 的 TEXT/TOOL_CALL 按 ordinal 进入同一文本视图；搜索使用真实 source 段与 block 展示边界；工具结果先沿 canonical 边确定归属，再决定是否读取正文/artifact。两个内容 cursor 已切换到 `.v2`，原内容判别值返回 `CURSOR_INVALID`；定位工具和其 cursor 保持不变。工具描述与 schema 已同步，没有新增参数、返回分支或持久化结构。

仓库 `.venv` 执行以下验证：

```text
python -m pytest -q tests/test_session_content_query.py tests/test_session_search.py \
  tests/test_round1_tool_output_artifact.py tests/test_conversation_fork.py \
  tests/test_turn_interruption_history.py tests/test_system_prompt.py
126 passed
python -m pytest -q tests/test_round3_1_provider_input_prefix_continuity.py
11 passed
```

新增合同的20项定向回归也由 critic 独立运行通过；它们已包含在126项中，不重复累计。覆盖查询调用/结果排除、混合及 call-only entry、片段边界、实际工具名/参数匹配、标签不匹配、长 Unicode 参数重组、旧内容 cursor 拒绝、普通与查询工具的四类保存结果，以及归属边缺失不能被排除规则掩盖。原 artifact、权限、ROOT/fork、范围、删除/归档/目录丢失、中断与页预算回归保持通过。改动文件 `ruff check`、`git diff --check` 通过。

增量真实验证入口是 `tests/dogfood/run_session_content_tool_semantics_dogfood.py`。默认选择保存的 OpenRouter GPT-6 Luna Chat/high；`--model deepseek-flash` 选择保存的 DeepSeek Chat/high。两者使用同一输入、断言和生产 owner，不改生产配置或为诊断改变产品限制。

实际通过记录使用 DeepSeek：2个真实会话、4个完成的 ROOT 轮次、13次 provider 请求（12次 agent loop、1次实际 compaction summary），usage 均为 reported、推理均为 high；10次工具观测全部 SUCCESS。普通 terminal 完整执行给定长 Unicode 参数命令，跨会话读取返回原文，随后实际采用 compaction snapshot。压缩后的8次内容查询证明：

- 开启工具可搜索到原 terminal 调用；按同一个 entry 以512字符四页续读，其中后三页只传 cursor 和页预算。原调用的512/512/512/342字符片段共1878字符，拼接结果与 canonical 名称/参数投影完全相等；末页按既有预算继续读取相邻结果，不改变原调用定位。
- 普通工具输出 `OUTPUT-ORIGINAL-BLUE` 命中原 TOOL_RESULT。
- 先前查询复制来的 `REFERENCE-ORIGINAL-GREEN` 在当前会话内容搜索中零命中；显式锚定旧查询结果仍能读取当时的原文证据。
- 恢复阶段实际只调用会话查询工具，没有重跑历史执行任务。9对同一输入根内的请求（含跨 ROOT 轮次）保序序列化 tools 相等，messages 仅追加；SYSTEM 位于保持不变的消息前缀中。

通过证据为 ignored `output/session-content-query-tool-addendum-20261007/dogfood.json`，包含完整实际输入、输出、参数、结果与 usage，仅脱敏实际凭据。首次 OpenRouter 记录独立保留在 `dogfood-openrouter-timeout.json`：reference 轮完成；长输入 execute 请求在600秒诊断期限内没有完整 provider 响应，未执行工具。这是未完成的诊断尝试，不计为通过，也不推断为工具语义失败；没有削弱断言或覆盖该记录。两次测试均使用只读保存设置注入、临时 home/工作区和 verified loopback disposable 数据库；结束后数据库已删除，并另行确认两库均不再存在。生产设置和生产数据库未修改。

## 11. 审阅记录

原 Session query design critic（GPT-6 Astra / xhigh）已审阅基础设计、本文和必要的当前 canonical 接缝，与主代理完成修订及最终复核。详细记录保存在 ignored `output/session-content-query-tool-addendum-20261007/critic-review.zh.md`，本文可独立作为实施真值，不依赖临时审阅文件。

已采纳：

1. 调用投影简化为两行 `Tool call` / `Arguments`，参数复用现有 `canonical_json_bytes` owner；不增加结构化返回分支或自写编码器。
2. 搜索 snippet 明确按允许 block 展示跨度裁剪，防止只过滤匹配段后夹带查询调用；普通 TOOL_RESULT 沿用已有固定视图。
3. 裁剪规则覆盖任何含 TOOL_CALL 的 assistant 视图，包括没有 TEXT 的普通/查询混合调用；验收包含该情形。
4. 查询结果先沿 canonical 边判定所属，再投影正文/artifact；缺失边沿既有损坏错误处理。

双方确认内容 cursor 的 v2 operation hard cut 必要且足够；它只承担格式/文本位置合同的区分，不承担授权或真实性。公开工具名、参数与返回字段不增加，不保留旧投影兼容路径。

规格及实现最终审阅结论：无剩余阻塞问题，主代理与原 critic 同意冻结本次增量实现。critic 独立复核长参数原文重组、8次恢复查询的真实返回、查询结果搜索排除与正常读取、9对跨轮/同根 prefix 连续性，以及无历史执行任务重跑。增量代码审阅记录为 ignored `output/session-content-query-tool-addendum-20261007/code-review.zh.md`；137项主任务回归、critic 的20项独立定向回归、通过与未完成的实际 provider 记录按各自范围保留，不以基线实证替代增量验证。
