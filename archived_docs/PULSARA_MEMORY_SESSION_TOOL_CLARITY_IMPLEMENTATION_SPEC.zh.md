# Memory 与 saved-session 工具说明精简实施规格

日期：2026-10-26。状态：**已实施并通过聚焦验证**。本文件负责 `remember`、`memory_search`、`memory_get`、`mark_memory_relation`、`search_sessions`、`search_session_content`、`read_session_content` 七个现有工具。本组七个工具保持原名、原参数、原 dispatch；全目录基线 40 个，合入 capability 组无参数全刷新方案后为 39 个；本轮已修改生产工具定义及 SYSTEM 定点文案；未修改数据库、配置或运行中会话，也没有调用模型 API。

## 1. 目的与决定

让模型更容易完成三个判断：该选哪个工具、该传什么、拿到结果以后如何继续。减少同一概念在 SYSTEM、工具正文、schema 根和字段说明中的重复，同时保留决定输入与后续动作的事实。不是删除可执行语义来争取更低 token 数，也不把工具说明整理变成 memory 知识政策重设计。

采用现有平面参数与普通英文说明。四个 memory 工具各有独立用途，不合并、不拆分；三个 session 工具保留现有 first-page/continuation canonical union，把两种调用的字段关系写在函数正文末尾。字段中保留具体值的含义，类型、enum、required、default、范围、nullable 与 additionalProperties 原样保留。数字边界不在正文重复朗读；schema 的字符 maxLength 无法表达 UTF-8 byte admission，因此 statement/query 的字节边界仍用短文字说明。

本组没有 root discriminator。删除 session schema 根的重复说明、保留两个 oneOf 分支，经过**未经修改**的 `lower_openai_function_parameters` 即得到附件 proposed Responses JSON。没有 wire 生成后再删字，没有通用 adapter 新选项，也没有碰 MCP schema。

新说明只在 cold epoch 或明确采纳的 compaction successor 安装；既有 epoch 的 SYSTEM/tools 必须保持 byte-identical，messages 继续 append-only。没有按轮筛工具、延迟暴露说明、通用 dispatcher、新工具名或新增持久化类别。

## 2. 阅读依据与实际 owner

本次完整阅读了 `output/tool-schema-compatibility-20261026/REPORT.zh.md`、`EXPERIMENT_PLAN.zh.md`、`COMPRESSION_DISCUSSION.zh.md`、`COMPRESSION_CRITIC.zh.md`。190 次 Qwen/DeepSeek Responses 实验说明 root union 的兼容与填参差异确实存在；展平仍是有意的 superset，poll 混入 timeout_seconds 是具体关系丢失实例。root parameters.description 与 function.description 的 0/2 对 2/2 小样本只支持谨慎选择说明位置，不支持宣称模型已验证了本方案。nested oneOf 可接受、Qwen refs 不可靠等结果不构成本组重做 adapter 的理由。实验史与第三方差异均只留在规格与证据中，不进入下面的英文工具文本。

本规格编写前，仓库根没有独立的活动 memory/session 规格；相关旧设计位于 `archived_docs/`，不作为本变更的现行权威。遵循本次用户要求与根 `AGENTS.md`，以以下当前生产 owner 交叉核对行为，而不是恢复旧归档契约。

| 现有 owner | 已核对内容；实施触点 |
|---|---|
| `src/pulsara_agent/capability/builtin_catalog.py::_remember_parameters`、`_MEMORY_SEARCH_PARAMETERS`、`_MEMORY_GET_PARAMETERS`、`_MARK_MEMORY_RELATION_PARAMETERS`、`_BUILTIN_DESCRIPTORS` | 七工具正文及四 memory schema annotation 的实际声明处。改文案，不改权限分类、artifact mode 或 binding。 |
| `src/pulsara_agent/memory/product_contract.py` | 四 kind、scope、cohesive unit、authoring、relation comparison 目前用长常量合成字段。以下替换文字是模型可见目标；把对应常量精简为目标文字，去掉不再使用的拼接段，不保留重复旧句。`memory_search.kind` 不再拼接完整 kind guide。 |
| `src/pulsara_agent/ports/session_content.py::session_query_input_schema` | 两个封闭分支、first-page query 要求、cursor-only continuation、limit/max_chars。逐分支字段 annotations 采用附录路径；保持 branch membership 和所有约束。 |
| `src/pulsara_agent/conversation_kernel/memory_tools.py::KernelMemoryToolPort._remember/_resolve_basis/_search/_get/_mark_memory_relation` | kind/scope、exact ID、UTF-8 query、dependency 范围；读取结果的真实 owner。 |
| `src/pulsara_agent/conversation_kernel/memory/writes.py::PreparedRememberWrite/PreparedMemoryRelationWrite` | statement normalization、按 kind 的 byte limit、依赖数量及重号、两个不同关系端点。 |
| `src/pulsara_agent/conversation_kernel/_repository/memory.py::_settle_remember/_settle_memory_relation`（所属 repository mixin） | 保存结果与关系事务：ALREADY_PRESENT 复用现有记录；同 context、active 验证；CONTRADICTS 同 kind；SUPERSEDES source 保持、target 变 inactive，支持现有 taxonomy correction。 |
| `src/pulsara_agent/conversation_kernel/session_content.py::SessionContentQuery.invoke/_sessions/_search_content_bm25/_content/_document/_read_page` | 参数解析、授权、当前 session compaction range、游标、内容分页、历史 tool output 与图片占位符。 |
| `src/pulsara_agent/conversation_kernel/session_search.py::search_session_page` | query.strip 后的关键词/活动列表、当前会话正文范围和跨会话发现。 |
| `src/pulsara_agent/ports/system_prompt.py::DEFAULT_SYSTEM_PROMPT` | 已有 memory advisory/opt-out/自然使用/缺失信息搜索与 saved-conversation 指导；已执行 §6 的定点删除。 |
| `src/pulsara_agent/llm/adapters/openai/function_tools.py::lower_openai_function_parameters` | 现行 non-strict wire lowering，仅调用，不修改。 |

## 3. 信息如何收敛

| 原内容 | 本方案的唯一位置或删除理由 |
|---|---|
| 搜索、point read、保存、关系标记的用途 | 各工具正文第一句；`memory_get` 和 `memory_search` 保留互相发现入口。 |
| 四 kind 的长定义复制在 search 和 remember | 完整但简短的选择定义只在 `remember.kind`。搜索 `kind` 只解释软过滤与 filter_match；不要求搜索者先重新学习四类政策。 |
| memory 都是 advisory、可能陈旧、不得覆盖指令 | 已有 SYSTEM Memory 首条和实际 read result 的 `advisory`/`may_be_stale_or_incomplete`。从各工具长正文删去重复，SYSTEM 保持；不新增确认或取证步骤。 |
| 搜索触发条件、自动 recall 为空不等于无记忆 | 已有 SYSTEM Memory 第二条；删工具正文重复。可主动搜索、可用轻量推断保存的行为不变。 |
| 记忆“只在改善回答时使用”、每次读取都重查代码 | 不重新引入前者；后者按用户既有取舍在 §6 精确删除。没有在字段里改写成隐含同义规则。 |
| statement 一条完整信息、归属与时间等 | `remember.statement` 一次表达；允许自然属于同一信息的多个分句，仍保留建议/计划/采纳/完成区别。 |
| scope 是可见范围而非命题真假范围 | `remember.context_target` 保留。任何 kind 都可选任一 scope，不暗加类型/范围映射。 |
| based_on 与 related 的区别、跨 scope 方向、级联删除 | `remember.based_on_memory_ids` 与其 `items.description`；这影响写入后果，调用前保留。maxItems=8 原样保留。 |
| related_memories 只是候选 | `mark_memory_relation` 正文一次表达；remember 结果现有 `relationship_labels_not_inferred: true` 继续存在。删除“至多三个”工具说明不改变实际 preview 数量。 |
| 关系标记前要比较同主体条件、CONTRADICTS/SUPERSEDES 区别 | 集中在 `mark_memory_relation` 正文；SUPERSEDES 直接说明 source 是获得支持的替代，包含 statement 或 kind 纠正，不用保存时间警告重复同一判断；已采纳最终 critic 对 taxonomy correction 的补充。ID 字段仅描述来源及方向。删除重复“询问用户”“Memory 页删除”句，不增加审批步骤。 |
| 保存时机、立即生效、ALREADY_PRESENT 不改 origin/dependencies、Memory 页删除 | `remember` 正文保留实际写入效果各一次；删除工具调用本来就在最终回复之前的冗余命令。删去“稍后修正不会撤回”的重复，立即生效已明确；删逐工具调用存 FACT 的泛化警告，保存标准仍是 reusable natural-language information。 |
| 三 session 工具的首次/继续字段关系 | 每个正文的 First page / Continue 两行。根 description 删掉；cursor 字段只说 exact next_cursor。continuation 不带原 query/filter/anchor，即使它们没变。 |
| 当前会话只查 compaction 移出上下文的早期内容 | 三工具各以一句直述，保证独立选中任一工具也能知道范围。其他会话只读当前允许的已存 ROOT conversation history。没有暴露内部 cut/carrier 术语。 |
| read anchor/direction 默认值 | `direction` 字段一次完整列出。有 anchor 先读 anchor；无 anchor 的 older/newer 起点与现行 parser 完全相同。 |
| 已有信息充分时无需查询/图片重读 | SYSTEM Saved conversations 已有，不在 read 工具再加一条。 |

### 删除与现有结果的关系

本方案**不新增或修改结果文案/字段**，不存在“先删说明，以后再造结果提示”的依赖。下面说明为何保留必要的短解释，以及哪些事实已由具体结果表达。

| 条件与真实 result owner | 已有输出与处理；本方案的取舍 |
|---|---|
| `_search` 调用 `_retrieval_summary`，包含不完整检索/软过滤/关系检查不可用 | 已返回 `retrieval_summary.status/match/expanded_filters/ranking/relation_check`、逐项 `filter_match`、`relation_warnings`。保留一条字段导航，删除较长重复政策，不另建 freshness/receipt 体系。 |
| `_get` 发现 `user_edited_at` 非空 | 已返回 `user_edit.note`: “The user edited this memory's text. Existing relations were not re-evaluated.” 正文只说 any user-edit note；不把此条件提示改成每次调用前的警告。 |
| `_settle_remember` 新增或命中既有 item | 已返回 SAVED/ALREADY_PRESENT、memory_id、related_memories、retrieval_summary、relationship_labels_not_inferred。正文仍解释两个 status 的实际写入区别，避免依赖结果字段名推测副作用。 |
| `_search_content_bm25` 截短问答文本 | 现有 messages text 保留 `[truncated]`；正文保留这个标记及 exact message entry_id 的 read 入口，不重写问答布局。 |
| `_document` 遇到非 COMPLETE source/display 或 INCOMPLETE/UNAVAILABLE artifact | 现有条件性 `source_coverage`、`display_kind`、`artifact_disposition` 与原因字段；保留“分页不能找回未保存内容”一句。普通完整结果不新增解释。 |
| `_read_page` 一条长消息超过页预算 | 已输出 `partial: true` 和 continuation。正文解释 slice 与先完成本 entry 再前进，`max_chars` 说明 whole-page budget；不造新 offset/cursor。 |
| `_decode`、`_check_range` 或 `_search_content_bm25` 发现 invalid/range changed/expired cursor | 现有 `SessionQueryError` message 已明确 restart query；这些错误处理无需加入每个工具常驻正文。仍保留全部 parser 与错误结果。 |
| `_document` 渲染图片占位符 | 现有 same-workspace image_ref 与 cross-workspace 省略保持。正文说明占位符和充足 max_chars；不加入图片读取授权或新 metadata。 |

## 4. 精确参数合同与实现方式

以附件 `canonical_proposed.json` 作为完整的七工具 canonical schema，`annotation_changes.json` 作为逐 JSON path 的删除/替换清单；§8 同时逐项列出完整英文，读者无需运行脚本才能实施。未列出的 annotation 与所有非 annotation 值原样保留。

实现时在原有 owner 改常量或字段字面量；不引入运行时 patch registry，不把离线 `FIELDS`/`DESCRIPTIONS` 测量字典搬入生产当框架。session helper 的 shared include_tools 当前两工具共用一句，改为在现有按 name 分支里选择 search/read 各自的短说明。first/continuation 的同一字段文案相同；root oneOf 原封不动。

没有新增字段、改名、增加/删除 required、改变 enum、default 或 null。`include_tools` 仍 omission=false，lifecycle 仍 omission=ALL；它们此前没有 schema default，本方案也不补 default，只把已知默认行为写在对应字段。read direction 的条件默认留在文字中，不能伪造统一 default。

尤其保留：memory query/statement UTF-8 admission、RESPONSE_PREFERENCE 的 2048 byte 特例；GLOBAL basis 只能 GLOBAL，project basis 可 GLOBAL 或当前 project；依赖删除仍级联；CONTRADICTS 两个同 kind 活跃 item，SUPERSEDES 可跨 kind 作现有 taxonomy correction，不添加 same-kind 限制。session query 的非空白要求与 cursor 只能配页预算保持；所有 query/cursor/session_id/entry_id 均用原值，不缩短、不编码成新标识。

## 5. 静态测量与取舍

复现：在仓库根执行 `.venv/bin/python output/tool-clarity-specs-20261026/memory_session/measure.py`。脚本自行将 repo `src` 加入 import path，使用仓库现有 `.venv` 的 V3 estimator；不加载设置、不连数据库、不访问 provider。完整结果是同目录 `measurements.json`。

统计单位为 `PulsaraHeuristicTokenEstimatorV3.estimate_json(Responses tools array)`。每行单工具都使用单元素数组，组行用七元素数组；不要把独立四舍五入的单工具数相加后当组数组。40 工具总量只替换本组，其他代理的方案不包含在本表。

| 工具/集合 | 现状 | 提案 | 减少 | 减少比例 |
|---|---:|---:|---:|---:|
| remember | 939 | 658 | 281 | 29.93% |
| memory_search | 639 | 275 | 364 | 56.96% |
| memory_get | 199 | 116 | 83 | 41.71% |
| mark_memory_relation | 303 | 266 | 37 | 12.21% |
| search_sessions | 290 | 256 | 34 | 11.72% |
| search_session_content | 477 | 381 | 96 | 20.13% |
| read_session_content | 584 | 490 | 94 | 16.10% |
| 七工具数组 | **3427** | **2439** | **988** | **28.83%** |
| 全部 40 工具，只替本组 | **22824** | **21836** | **988** | **4.33%** |

削减主要来自 kind 定义、advisory/搜索触发条件重复。关系与 saved-session 工具压缩幅度较小，因为 source/target 方向、当前会话范围、continuation、direction 起点和截断含义直接影响使用；不能为追逐统一百分比把它们删掉。

把 first-page/continuation 关系放在 function.description、减少重新拼接散落规则，是可解释的可读性设计判断。此处只证明 annotation 体积下降与结构合同保留，**没有证明模型首次合法率、任务完成率、推理 token 或修正次数改善**。没有将 190 次旧实验计为本七工具候选的验证，没有要求为本轮文档再做付费实验。

## 6. SYSTEM 的唯一改动

删除 `DEFAULT_SYSTEM_PROMPT` Memory 节最后的完整行：

```text
- Verify recalled coding facts against the current workspace.
```

对应精确 Python 字符串是 `"\n- Verify recalled coding facts against the current workspace."`；不删除此前“field and result guidance.”，也不在其他字段恢复这条统一重读规则。这遵循用户已经明确的 prompt 清理方向，不是根据工具节省量重新设计 memory 政策。Saved conversations 两条和 Memory 的其余三条原样保留：它们覆盖已有自动注入的 memory/history，不只显式七工具结果，所以有独立用途。常驻 tool 内的重复从 tool 端删去即可。

单独使用 V3 `estimate_text`：Memory 节（不含标题）202→186；完整 SYSTEM 2862→2847。独立文本的 rounding 不同，不把 16 和 15 混为同一数，也不把 SYSTEM 节省并入 tools 表。`system_proposed.txt` 只用于审阅完整目标文字，不是新 production owner；已由本组在生产中定点删除，其余 SYSTEM 改动由主代理协调。

## 7. 验证与交付边界

实施后已执行测量脚本，并确认七工具真实生产 canonical/schema lowering 输出逐值等于完整候选；七 canonical schema 均通过 `Draft202012Validator.check_schema`。脚本在**只去 schema 节点 description/title**后逐工具比较现有/提案 canonical 与 lowered schema，均完全相等。没有修改 title；该比较仅把 annotation 与合同分开，不递归误删业务属性同名键。strict=false 保持；本组独立替换仍为 40 个工具，跨组全刷新合并后的目录为 39 个。

已执行 `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_memory_tool_surface.py tests/test_direct_memory_prompt_contract.py tests/test_session_content_query.py::test_native_schemas_support_cursor_only_and_tools_are_read_only_root_only`，结果 **3 passed in 0.81s**。本测量脚本也通过；未新增重复字面量的测试。前期22个离线 schema 代表实例通过，覆盖：三 session 工具 cursor-only 合法，cursor+原 query/filter/anchor 不合法；content 初次缺 query 或纯空白不合法；read 空对象及两个 direction 合法；memory 四个工具使用原 enum/required。无需新增模仿字面量的测试或数据库重置。

现成语义用例可在实际出现 regression 时定位 owner：`test_full_history_uses_zero_not_genesis_and_explicit_self_cannot_bypass`、`test_snapshot_range_excludes_carried_active_request_and_not_retained_summary`、`test_anchor_direction_shows_later_revision_without_matching_original_keyword`、`test_long_unicode_anchor_stays_one_entry_cursor_only_then_follows_direction` 与 `test_cross_session_tool_public_metadata_and_full_artifact_are_one_stable_view`。这些功能没有被本方案改变，不为了文字修改额外扩成新 activation gate、警告清单或长时程 dogfood 项目。

已按 name 取本组核对真实导出的七工具 Responses JSON 与附件值一致。前缀连续性沿用已有 owner/test，不新建证明机制；ROOT 负责最终跨组聚合和必要整体验证。

交付文件：

- `PULSARA_MEMORY_SESSION_TOOL_CLARITY_IMPLEMENTATION_SPEC.zh.md`：本规格与全部精确文案。
- `output/tool-clarity-specs-20261026/memory_session/responses_proposed.json`：完整七工具最终提案 wire 定义。
- 同目录 `responses_before.json`、`canonical_proposed.json`、`annotation_changes.json`：现状、完整 canonical 与逐路径替换。
- 同目录 `measure.py`、`measurements.json`、`system_proposed.txt`：可复现离线计量和单独 SYSTEM 已接受改动；`full_catalog_before.json`、`canonical_before.json`、`system_before.txt` 冻结实施前基线，脚本重跑不会用已修改生产覆盖基线。

无待用户决策的接口问题；用户已授权并完成本组生产实施，运行中已安装前缀未改。最终 critic 的 SUPERSEDES statement/kind 纠正补句已同步到生产、完整附件和下文精确文案。

## 8. 可直接实施的完整英文与逐路径 annotation

下文路径相对单个 `BuiltinToolDescriptor`：`$.description` 是 provider 函数正文；`$.input_schema` 是 canonical schema（Responses wire 对应 `$.parameters`）。session 的 `[0]` 是 first-page 分支，`[1]` 是 continuation 分支。每个 replace/add 均给出**完整目标字符串**，不是省略号或示例；delete 只删除该 annotation 槽。

### remember

`$.description` — replace

```text
Save reusable information for future conversations. Source-faithful lightweight inferences from conversation, behavior, tool choice or planning are welcome; self-report, repetition and high confidence are not required. Save natural-language context, including attributed observations from tool results. Exclude secrets, credentials, raw tool dumps, detailed executable procedures and permission overrides. Saving takes effect immediately and creates no task, reminder or execution authority. SAVED returns the new memory_id; ALREADY_PRESENT returns the existing item without changing its origin or dependencies. Users can delete saved items on the Memory page.
```

`$.input_schema.properties.based_on_memory_ids.description` — replace

```text
Saved items this statement depends on, rather than merely related items, paraphrases or wider-scope copies. GLOBAL may depend only on GLOBAL; CURRENT_PROJECT may depend on GLOBAL or this project's memories. Deleting any basis also deletes this dependent item.
```

`$.input_schema.properties.based_on_memory_ids.items.description` — replace

```text
Exact memory_id from a memory result.
```

`$.input_schema.properties.context_target.description` — replace

```text
GLOBAL is available across conversations sharing this memory store; CURRENT_PROJECT only within this project. Either scope accepts any kind. Availability does not broaden the claim: keep its actual subject, date and conditions in statement.
```

`$.input_schema.properties.kind.description` — replace

```text
USER_PROFILE: who the user is, likes, tends to do, aims for or may prefer. RESPONSE_PREFERENCE: a soft default for answer style, explanation or formatting. FACT: attributed declarative context, including explicit retention of uncertain information. DECISION: a source-supported choice, inclination, plan, goal, commitment or adopted high-level method. Choose FACT when uncertain.
```

`$.input_schema.properties.statement.description` — replace

```text
One self-contained idea that can be read and deleted as a whole; separate unrelated claims. Preserve the subject, attribution, project, time, conditions, quantities, negations and uncertainty that matter. Distinguish suggestions, tentative plans, adopted choices and completed actions. Resolve pronouns and relative dates only when unambiguous. Use natural prose without template labels or invented details. Maximum 8192 UTF-8 bytes; 2048 for RESPONSE_PREFERENCE.
```

### memory_search

`$.description` — replace

```text
Find saved user or project information by meaning or keywords across readable memory contexts. Use memory_get when you already have the memory_id. retrieval_summary reports search completeness, filter expansion, ranking and relation-check availability; filter_match identifies relaxed matches, and relation_warnings identifies active contradictions.
```

`$.input_schema.properties.kind.description` — replace

```text
Preferred kind; omit unless the request requires one kind. Results may include other kinds when exact matches are insufficient; see filter_match.
```

`$.input_schema.properties.limit.description` — delete

`$.input_schema.properties.query.description` — replace

```text
Describe the needed subject using natural language or keywords, with known project and distinguishing details. Resolve vague references from available context. Maximum 32768 UTF-8 bytes.
```

### memory_get

`$.description` — replace

```text
Read a saved memory by its exact ID, including its statement, kind, context, recorded time, lifecycle, direct relations and any user-edit note. Use memory_search to discover an ID.
```

`$.input_schema.properties.memory_id.description` — replace

```text
Exact memory_id from a memory result or reference.
```

### mark_memory_relation

`$.description` — replace

```text
Relate two inspected saved memories about the same subject and conditions. Both must be active and in the same context. Related memories returned by remember are suggestions to inspect, not established relations.
CONTRADICTS: keep two incompatible memories of the same kind active when neither is the supported replacement.
SUPERSEDES: the source is the supported replacement, including a corrected statement or kind; make the target inactive without deleting it.
```

`$.input_schema.properties.relation_kind.description` — delete

`$.input_schema.properties.source_memory_id.description` — replace

```text
Exact memory_id from a memory result; for SUPERSEDES, the replacement that remains active.
```

`$.input_schema.properties.target_memory_id.description` — replace

```text
Exact memory_id from a memory result; for SUPERSEDES, the item made inactive.
```

### search_sessions

`$.description` — replace

```text
Find saved conversations by title or message keywords; omit query to list by activity. This session's message hits cover only earlier content removed by compaction; other accessible sessions use saved ROOT conversation history. Body hits provide session_id and entry_id for read_session_content; title-only hits have no entry.
First page: optional query, lifecycle, limit.
Continue: cursor; optional limit. Send no other fields on continuation. Omit unused fields; null is not accepted.
```

`$.input_schema.description` — delete

`$.input_schema.oneOf[0].properties.lifecycle.description` — add

```text
Omit for ALL.
```

`$.input_schema.oneOf[0].properties.query.description` — replace

```text
Title or message keywords; omission or blank text lists by activity.
```

`$.input_schema.oneOf[1].properties.cursor.description` — replace

```text
Exact next_cursor returned by this tool.
```

### search_session_content

`$.description` — replace

```text
Find saved questions and their answer process by keyword relevance. Omit session_id for this conversation's earlier messages removed by compaction; messages still in context are excluded. Other accessible conversations use saved ROOT conversation history. Results preserve speakers, times and historical quotes/comments; [truncated] marks omitted text. Each item's entry_id anchors its question; use a message's entry_id with read_session_content to expand that message. Images are presence markers; generated markers and image metadata are not search terms.
First page: query; optional session_id, include_tools, limit.
Continue: cursor; optional limit. Send no other fields on continuation. Omit unused fields; null is not accepted.
```

`$.input_schema.description` — delete

`$.input_schema.oneOf[0].properties.include_tools.description` — replace

```text
Also search saved tool names, arguments and results, excluding the three session-query tools' own calls/results. Omit for false.
```

`$.input_schema.oneOf[0].properties.query.description` — replace

```text
Nonblank natural-language or technical-identifier query; Chinese and English are supported.
```

`$.input_schema.oneOf[0].properties.session_id.description` — replace

```text
Exact session_id; omit for this conversation.
```

`$.input_schema.oneOf[1].properties.cursor.description` — replace

```text
Exact next_cursor returned by this tool.
```

### read_session_content

`$.description` — replace

```text
Read saved messages directly or expand a search hit; searching first is optional. Omit session_id for this conversation's earlier messages removed by compaction. Other accessible conversations use saved ROOT conversation history. An entry_id anchor is read first. Long messages start at the beginning and continue across pages before adjacent messages; partial marks a slice. Tool output may have missing saved content as shown by source_coverage, display_kind and artifact_disposition; pagination cannot recover it. Images are text placeholders; only same-workspace placeholders include image_ref. Increase max_chars if a placeholder is incomplete.
First page: optional session_id, entry_id, direction, include_tools, limit, max_chars.
Continue: cursor; optional limit, max_chars. Send no other fields on continuation. Omit unused fields; null is not accepted.
```

`$.input_schema.description` — delete

`$.input_schema.oneOf[0].properties.direction.description` — replace

```text
With entry_id, default newer; older reads preceding messages. Without entry_id, default older starts at latest; newer starts at earliest.
```

`$.input_schema.oneOf[0].properties.entry_id.description` — replace

```text
Exact message entry_id in the selected session.
```

`$.input_schema.oneOf[0].properties.include_tools.description` — replace

```text
Include historical tool names, arguments, public results and retained output. Results and retained output are labeled separately. Omit for false.
```

`$.input_schema.oneOf[0].properties.max_chars.description` — replace

```text
Total text characters across this page's entries.
```

`$.input_schema.oneOf[0].properties.session_id.description` — replace

```text
Exact session_id; omit for this conversation.
```

`$.input_schema.oneOf[1].properties.cursor.description` — replace

```text
Exact next_cursor returned by this tool.
```

`$.input_schema.oneOf[1].properties.max_chars.description` — replace

```text
Total text characters across this page's entries.
```
