# 会话内容查询 target 统一实施调研规格

日期：2026-10-28。状态：生产 hard cut 已实施，公开请求、结果及消费者采用本规格。验证与范围见[实施报告](output/target-hardcut-implementation-20261028/sessions/REPORT.zh.md)；整体结果见[汇总](output/target-hardcut-implementation-20261028/root/REPORT.zh.md)。

## 1. 决策与范围

会话使用 `SESSION`，会话内消息使用 `SESSION_ENTRY`。对象身份进入 target；查询、方向、内容过滤、页预算留在外层。`search_sessions` 查的是调用者可访问的会话集合，**不增加 target**。另两工具首屏可省略 target，继续使用当前会话的可读历史。三个工具的续页仍为 `cursor + 可选页预算`，不得同时传 target 或任何首屏过滤条件。

不合并工具，不新增查询前置步骤、不将 cursor 包成 target，不用目标拥有者注册表替换原生授权，不添加 durable event、job、receipt、hash 或总历史/轮次上限。三个入口保持 ROOT-only、read-only、`session_read`、`artifact_mode=NEVER`。subagent 整条链路及其数据、权限、历史、profile、monitor 开放均不在本轮范围。

### SUPERSEDES

本规格仅替代[会话查询设计](archived_docs/PULSARA_UNIFIED_SESSION_CONTENT_QUERY_DESIGN.zh.md)、[会话工具语义补充](archived_docs/PULSARA_SESSION_CONTENT_TOOL_SEMANTICS_ADDENDUM.zh.md)和[本地历史召回规格](archived_docs/PULSARA_LOCAL_HISTORY_RECALL_PG_SEARCH_IMPLEMENTATION_DISCUSSION.zh.md)中**公开身份字段及对应调用示例**；检索、摘录、原文读取、压缩范围与原生存储语义以现有生产实现为准并保持。内部 DTO、数据库列、索引文档和 cursor 私有字段不必改名。已存工具消息不重写，不保留旧公开请求别名或新旧双读双写。

只有新 cold epoch 或明确采用的 compaction successor 安装新定义；已安装 SYSTEM/tools 字节不变、messages 只追加。不能因 target 新格式而让 reload、检索或 UI 刷新重建输入根。

## 2. 已核对的生产 owners

下表路径相对 `src/pulsara_agent/`，函数名为稳定定位依据。

| Owner | 当前责任与本次边界 |
|---|---|
| `ports/session_content.py::session_query_input_schema` | 首屏/续页闭合 union、默认值与预算；改公开身份字段，保留两种请求形态。 |
| `ports/session_content.py::session_content_range` | 从当次实际 installed binding/carrier 取范围与 active-request 排除，不读取 latest compaction。保持。 |
| `capability/builtin_catalog.py:976` 及 `_catalog_shape` | 三工具 descriptor、HOST_MAIN_RUN owner、依赖 session-query port；改说明，保持权限和执行绑定。 |
| `capability/call_classifier.py::classify`、`tool_permission.py::PolicyPermissionGate` | 原生 read-only 分类及权限政策；target 不绕过它们。 |
| `conversation_kernel/tool_execution.py:1487` | 按本次 canonical facts 冻结 session_content_range 到 invocation context。保持。 |
| `conversation_kernel/host.py:743` | 给每个 loaded session 注入绑定 session_id/memory_domain_id 的 SessionContentQuery。保持。 |
| `conversation_kernel/tool_runtime.py:2954` | ROOT/owner/range 验证、物理只读调用、错误投影和结果 bytes。保持；不增加新 dispatcher。 |
| `conversation_kernel/session_content.py::invoke, _sessions, _content, _search_content_bm25, _document, _read_page` | 精确参数检查、授权、三类 cursor、结果、逐字符分页；在该 owner 中解析 target 并改模型结果投影。 |
| `conversation_kernel/session_search.py::search_session_page` | 跨会话标题/正文冷查询、排序、keyset、流式扫描；同时供桌面查询使用，内部 rows 不改。 |
| `conversation_kernel/history_search.py::search_history, refetch_history` | BM25、QA/QAQ、copy folding、canonical refetch 与范围二次检查；不改索引/排序/召回资格。 |
| `conversation_kernel/history_projection.py`、`history_excerpt.py` | 搜索文档、工具自引用排除、带引用/注释的摘录与 `[truncated]`；内部身份不改。 |
| `terminal_protocol/canonical_v3.py::authorized_session, validate_history_cut` | memory domain 授权、entry/event cut 一致性；仍是最终 owner。 |
| `conversation_kernel/memory_tools.py::_context_source` | 模型历史召回 full/compact/省略引用；仅该最终模型投影增加/替换可复制 target。 |
| `desktop_app/session_controller.py::search_sessions`、`http_server.py::_search_sessions`、`frontend/components/session-search-dialog.tsx` | 用户级全局会话搜索是独立 HTTP DTO，继续用户级 owner；不套模型当前压缩区间、不改它的 session/snippet 形状。 |

已全文检索 bundled Skills、前端以及模型输入目录，未发现三工具的 bundled Skill 调用示例或专用前端 target 解包器。`frontend/lib/builtin-tool-summary.ts` 无这三个工具专属分支；现有通用工具参数/结果显示继续可用。不为统一外观新增 UI 专属结构。现有 dogfood 与测试中有大量旧入参和结果字段，实施时必须同步。

## 3. Target 精确合同

```json
{"kind":"SESSION","session_id":"session:..."}
{"kind":"SESSION_ENTRY","session_id":"session:...","entry_id":"entry:..."}
```

- `SESSION` 的 kind、session_id 都必填；不能用 `{kind:SESSION}` 表示当前会话。
- `SESSION_ENTRY` 的 kind、entry_id 必填；**仅输入允许省略 session_id**，表示当前会话。这个变体保留原来只给 entry_id 的便利，不按 entry 全局 ID 反查它的会话，也不能跨会话猜测。所有输出 target 都含完整 session_id。
- target 对象闭合，不允许 null、未知字段、其他 kind；SESSION_ENTRY 不接受 direction/include_tools/cut 等操作或权限信息。search_session_content 仅收 SESSION，read_session_content 收两种。
- 输出 target 是地址，不是存在性、可读性、授权或已公开证明。仍按实际调用范围与数据库 owner 检查 session/entry 配对。
- 沿现有 ID 规则：schema 的非空 string 是形状约束；生产 `_string` 还通过 `is_valid_id` 检查类型前缀与 canonical ID。不要声称二者接受集合相同，不以本轮统一顺带改变 schema/parser 的既有差异。
- 与调度组共用 SESSION 形状；调度父会话字段即使名为 session，也可原样接收此对象，其 null/缺省合同由调度工具自己定义，不能把它的 null 许可传播到本组三工具。

## 4. 逐分支输入变化

以下 `S/E/C` 是便于阅读的占位符，不是可执行 canonical ID。完整机器形状见附件。三个工具不增加 action。

| 工具/分支 | 当前请求 | 新请求 |
|---|---|---|
| search_sessions 列表 | `{}` | 不变 |
| search_sessions 搜索/筛选 | `{"query":"方案","lifecycle":"ARCHIVED","limit":10}` | 不变 |
| search_sessions 续页 | `{"cursor":"C","limit":10}` | 不变，无 target |
| search_session_content 当前 | `{"query":"方案","include_tools":true}` | 不变 |
| search_session_content 指定会话 | `{"session_id":"S","query":"方案","limit":5}` | `{"target":{"kind":"SESSION","session_id":"S"},"query":"方案","limit":5}` |
| search_session_content 续页 | `{"cursor":"C","limit":5}` | 不变；target/query/include_tools 都禁止 |
| read 当前最近历史 | `{}` | 不变 |
| read 当前从最早向后 | `{"direction":"newer"}` | 不变 |
| read 指定会话 | `{"session_id":"S","direction":"older"}` | `{"target":{"kind":"SESSION","session_id":"S"},"direction":"older"}` |
| read 当前锚点 | `{"entry_id":"E","limit":1}` | `{"target":{"kind":"SESSION_ENTRY","entry_id":"E"},"limit":1}` |
| read 指定会话锚点 | `{"session_id":"S","entry_id":"E","include_tools":true}` | `{"target":{"kind":"SESSION_ENTRY","session_id":"S","entry_id":"E"},"include_tools":true}` |
| read 锚点及更早消息 | `{"session_id":"S","entry_id":"E","direction":"older"}` | `{"target":{"kind":"SESSION_ENTRY","session_id":"S","entry_id":"E"},"direction":"older"}` |
| read 续页/长消息续片 | `{"cursor":"C","limit":1,"max_chars":512}` | 不变；不得重复 target/direction/include_tools |

缺省/null/预算均保持：

- search_sessions query 省略/空白按活动列出，lifecycle 省略为 ALL，显式 ALL/OPEN/ARCHIVED；query/lifecycle:null 非法。
- search_session_content query 必填且 strip 后非空，中文/英文/技术标识符走原查询 owner；首屏 include_tools 省略为 false。
- read 无锚点默认 older，从最近可读 entry 开始；指定 newer 则从最早开始。有 SESSION_ENTRY 锚点默认 newer；无论 older/newer，都先读取锚点，再邻接前/后消息。方向不进入 target。
- limit 省略 20，范围 1–50；read max_chars 省略 20000，范围 1–32000，是一页所有文本字符总和。整数拒绝 bool，null 不合法。原单页物理边界保持，不把它改成历史总量上限。
- include_tools、direction、cursor 显式 null 都非法。cursor 必须非空且属于相应工具。首屏和续页互斥；wire lowerer 可能放过混合组合，精确本地 parser 必须拒绝。

## 5. 可读范围与 continuation 的保持条件

### 5.1 范围不随 target 改变

`session_content_range` 在 FULL_HISTORY 下返回 through_sequence=0；否则从实际 installed CompactionSnapshotCarrier 取 source_through_sequence，并在 active_request 位于 SNAPSHOT_EXACT 时排除该 entry。不是“最近一次压缩时间”，也不是当前 DB head。

当前 session 的 search/read 仅可读 `entry_sequence <= through_sequence` 且非 excluded entry 的 ROOT 记录；当前上下文仍在的 suffix 与 active request 不可读。**显式 SESSION 指向当前 session 与省略 target 的范围完全一致**，SESSION_ENTRY 也不能越过它。当前没有更早内容时 read 保持空 items/null cursor/现有 note；无权锚点仍报 ENTRY_NOT_READABLE，不能悄悄退回列表。

其他可访问会话读取保存的 ROOT 历史，按 caller memory_domain_id 授权。无需运行该会话、不要求目录存在、不把 archived 或项目不同误当不可读。entry 必须在所选 session 内、符合 ROOT/可读内容类型与 include_tools；错误配对不自动改选会话。fork 后仍读选中会话自己的 canonical rows，不递归补来源。

### 5.2 三种 cursor 不能统一成一个假合同

| 工具 | 现有 continuation | 必须保持 |
|---|---|---|
| search_sessions | base64 JSON 包装 caller_session_id、query/lifecycle、当前范围与 inner keyset | 绑定 caller；底层为 live discovery，按 rank/updated_at/id，不宣称全局快照。每页重验范围，query/filter 不再由调用者重复提供。 |
| search_session_content | `.bm25.v3`、query/selected session/include_tools/range/excluded、consumed_page | `_HistoryConsumedPage` 属于 loaded session 进程 owner，只保存已消费 canonical question IDs 的不可变链。每页重算未消费 BM25 候选并 refetch；不是旧 score/offset 快照。restart/dispose 后 CURSOR_EXPIRED，可重新查询，不增加 durable registry、lease 或历史总 cap。 |
| read_session_content | `.v4`、selected session、entry/event cut、range/excluded、position/offset、include_tools/query=None/direction | 冻结首屏内容/事件 cut，后续新增内容和中断结尾不混进本次分页。长 entry 从文本开头开始跨页读完，之后才切邻接 entry。每页重验 session 与 cut/range；不用 target 再绑定第二遍。 |

公开 opaque cursor 原样复制；内部当前是可解码的 JSON，不能写成签名授权令牌。read cursor 当前没有独立 caller_session_id 字段；本轮不擅自添加 caller/revision/nonce 绑定或安全机制。existing owner 与每次实际授权/范围校验是执行依据。保留 cursor 操作版本和字段即可，因为内部语义没变；不添加旧请求兼容意味着旧 flat 首屏拒绝，不意味着无理由废弃仍有效的同合同 cursor。

### 5.3 检索与读取内容不混淆

search_sessions 仍为标题优先的关键词查找，仅搜用户文本/assistant TEXT；正文命中在本会话范围过滤之后选最佳项。它不是 BM25 内容工具，也不新增 include_tools。

search_session_content 保留 pg_search 的 QA/QAQ 互斥、问答过程、copy folding、中文/英文/技术字段、分数、canonical refetch、引用/评论屏障与 `[truncated]`。item 锚点是 Q1，不一定是实际命中消息；消息自己的 target 必须来自自己的 entry_id。不能将整个问答单位只留一个 target 而丢失逐消息展开能力。

include_tools=false 保留普通对话；true 对内容搜索加入历史工具名/参数/结果，但排除这三个查询工具自身的 calls/results，避免检索证据递归污染。普通文本恰好提到工具名仍能命中。read include_tools=true 则可直接读这三个查询工具的历史调用/结果，两者不同，不共享错误的排除逻辑。

read 对已保存公共工具结果与 retained output 保持 `Saved tool result` / `Tool output` 区分，source_coverage/display_kind/artifact_disposition 及原因、interruption、origin=scheduled、partial 保持。未保存内容不能靠翻页恢复。图片/visualization 仍是原子文本占位，只有同 workspace 提供可用图片引用；按文件组统一方案，公开占位提供 `{kind:IMAGE_REF,image_ref:...}` target 复制给 view_image。实际 `llm/input.py::image_reference_text` 与当前图片 label producer 由文件组统一修改，session_text 共享调用；内部 image_ref DTO/DB/protobuf 不变。跨 workspace 不泄露可用引用，不把生成标记/图片元数据变成搜索词。页预算放不下完整占位仍给可操作预算错误，不拆坏它。

## 6. 结果与直接消费者 hard cut

### 6.1 search_sessions

每项始终有 SESSION target；正文命中作为 hit 对象，含 SESSION_ENTRY target 与原 role/at/text/partial/origin；title/project/updated_at 保留。title-only 或活动列表没有 hit。不同时保留顶层 session_id/entry_id 镜像。

```json
{"items":[{"target":{"kind":"SESSION","session_id":"S"},"title":"方案讨论","project":"project","updated_at":"...","hit":{"target":{"kind":"SESSION_ENTRY","session_id":"S","entry_id":"E"},"role":"user","at":"...","text":"采用方案A","partial":true}}],"next_cursor":null}
```

item.target 支持继续查该会话；hit.target 支持精确展开命中。这是两个不同对象的必要交接，不以 ENTRY 偷换 SESSION；把旧 hit 字段集中后没有无用途 ID 镜像。内部 session_search row 与桌面 DTO 保持原样，只在模型 `_sessions` renderer 改形状。

### 6.2 search_session_content / read_session_content

内容搜索 item 的 session_id+entry_id 改为完整 Q1 SESSION_ENTRY target；messages 每条 entry_id 改为完整自身 SESSION_ENTRY target。title/score/messages 内容全保留。

```json
{"items":[{"target":{"kind":"SESSION_ENTRY","session_id":"S","entry_id":"Q1"},"title":"方案讨论","score":3.2,"messages":[{"target":{"kind":"SESSION_ENTRY","session_id":"S","entry_id":"Q1"},"role":"user","label":"question","at":"...","text":"选择哪个方案？"},{"target":{"kind":"SESSION_ENTRY","session_id":"S","entry_id":"A1"},"role":"assistant","label":"answer","at":"...","text":"采用方案A"}]}],"next_cursor":null}
```

上例 label/time/score 仅示意，真实值继续由原生 excerpt owner 提供。item 与 message target 可能都指向 Q1，但各自保留“整个问答单位入口”和“逐条消息”的作用；不另外添加 session_target/id。read 每项 session_id+entry_id 改 target，其他字段不变；同一 entry 的续片返回相同 target，继续片段必须用 cursor，重新用 target 会从 entry 起点重读。

```json
{"items":[{"target":{"kind":"SESSION_ENTRY","session_id":"S","entry_id":"A1"},"role":"assistant","at":"...","text":"第一段","partial":true}],"next_cursor":"C"}
```

### 6.3 自动历史召回

`memory_tools.py::_context_source` 的 `omitted_conversations` 现有 `{session_id,entry_id,read_with}` 改 `{target:完整SESSION_ENTRY,read_with:"read_session_content"}`。full/compact 的已展示历史项目前缺少身份，给最终模型历史项增加 Q1 target、messages 增加完整自身 target，使其同样能直接展开；source=session_history、title、role/label/at/text 继续保留。与 MEMORY 身份统一组已划分：本组负责历史项与 omitted_conversations，记忆组负责 memory item/refs/relations。

**仅改最终模型投影**。`HistoryCandidate.projection()` 还被 `_rerank_document_projection` 用于远端 rerank；不得直接修改它而把无关 ID 带进 rerank、改变 4000 token 文档准入或排名。最小做法是在 `_context_source` 的现有历史分支构造模型结果时，使用 candidate 原生 session_id/q1_entry_id/messages.entry_id 添加 target；保持共享 candidate/projection 和 rerank 输入不变。自动召回的范围与 Decision/refetch 资格、memory IDs 暴露 metadata 不变。

新增 target 的实际 JSON 成本必须纳入现有 context source full/compact/references 渲染与预算；预算紧时仍按原有降级 owner 选择，而不是少计 target、截断 ID 或提升预算。历史正文摘录预算与可见 projection 包装预算不是一回事，不为了新包装删掉原文本内容规则。

## 7. 实施步骤、错误及验证

1. 在现有 session schema owner 构造本工具需要的两个闭合 target 子集；内联，不增加全局 target registry 或 `$ref`。保留 search_sessions 原 schema；另两工具第一分支替换身份字段，第二分支保持 cursor+预算。
2. `SessionContentQuery.invoke` 精确检查新字段与 target kind/keys/null，提取本次选中 session/anchor 给原有内部执行方法；不通过调用旧 public flat parser 做兼容转换。保留 native `_string`、授权、range、cut 与所有内容分支。
3. 同步 `_sessions`、`_search_content_bm25`、`_document` 的最终模型 renderer；`_fits` 必须计新完整 payload/cursor，保留拆页、原子占位与 RESULT_BUDGET_EXCEEDED。不能在旧 `_fits` 之后追加 target。
4. 同步 builtin function.description、history recall 最终模型投影、测试与 dogfood。内部 SQL/native DTO、桌面 API、索引不重构。现有 canonical history 测试不能因换外形弱化。
5. 所有 read errors 仍走 tool_runtime 原有转换：INVALID_REQUEST、INVALID_ID、CURSOR_CONFLICT/INVALID/RANGE_CHANGED/EXPIRED、SESSION_NOT_ACCESSIBLE、ENTRY_NOT_READABLE、RESULT_BUDGET_EXCEEDED、SEARCH_UNAVAILABLE；超时 QUERY_TIMEOUT、存储不可用 QUERY_UNAVAILABLE、内容不可验证 CONTENT_CORRUPT。只读查询仍通过 physical I/O owner 记录 timing/cancellation，不添加副作用结算机制。

必要验证以行为为主：

- 新输入所有首屏/续页，旧 flat 拒绝；target 型别错、缺键、多键、null、canonical ID 错误；SESSION_ENTRY 缺省 session_id 不查全域；cursor+target/query/filters 都拒绝；所有原预算与默认值保留。
- search_sessions 的 SESSION、hit 的 ENTRY、BM25 Q1/messages、read item、自动召回 full/compact/omitted 引用可原样用于合法下一调用，不要依赖先查询才能操作已知 target。
- 当前 FULL_HISTORY 零范围、压缩前 range、active request exclusion、显式当前 target、错误 session/entry 配对、不同 domain、archived、已删除会话、目录不存在、fork 自己记录。
- 三类 cursor 的各自语义：live keyset、BM25 rerank/copy folding/restart expiry、read entry/event cuts 与长消息多页；预算改变可续页，target/filter 改变不可续页；不隐加生命周期上限。
- include_tools 两种模式、查询工具自引用搜索排除与显式读取、普通文本工具名可命中、保留结果/retained output及不完整元数据、interruption、图片/visualization 原子占位与跨 workspace image_ref 限制。
- 结果 `_fits` 与 context 紧预算降级计完整 target，无截断 ID、无丢消息身份；rerank 输入仍是原投影；前端通用工具显示与用户级全局查询无回归。
- 按实际改动运行既有 `tests/test_session_content_query.py`、`tests/test_history_recall.py`、`tests/test_history_projection_cut.py`、`tests/test_builtin_default_null_contracts.py` 相关项；图片相关运行 `tests/test_image_history_recovery.py` / `tests/test_unified_visualization.py` / kernel image integration 对应项。不要为外形增加冻结文案/token 数测试。
- 授权实施阶段更新并运行已有 session-content query/semantics dogfood、default-null 与 image-history/unified-reference 对应场景；保存生产配置只读，沿原 owners。新 provider shape 验证需覆盖两目标 Responses 的 target-copy、entry 省略、cursor-only 与原长消息功能；本轮没有跑这些，不能用静态形状替代。

## 8. 离线证据与体积

执行 `PYTHONPATH=src .venv/bin/python output/tool-target-hardcut-20261028/sessions/design_schema.py`。脚本只 import 生产 descriptor/lowerer/V3 estimator，生成候选文件，无 DB/provider 操作；16 个正例同时通过 canonical 与现行 lowerer wire，43 个反例被 canonical 拒绝。占位 ID 仅验证形状，不假称通过 production `_string` 或执行 owner。

| 完整 Responses 单工具 tools 数组 | 生产 | 候选 | 差值 |
|---|---:|---:|---:|
| search_sessions | 256 | 261 | +5 |
| search_session_content | 381 | 402 | +21 |
| read_session_content | 490 | 614 | +124 |
| 三工具整数组 | 1126 | 1276 | +150 |

启发式计量独立舍入，单行加总不代替整数组。不是 provider 实收 usage，不是完整 SYSTEM+tools，不含结果/自动召回实际文本的动态增量。统一 target 增加 schema 包装与必需类型，不能宣传节省 tokens；收益假设是减少跨工具拆字段和 session/entry 误配，尚未真实测量。

附件： [脚本](output/tool-target-hardcut-20261028/sessions/design_schema.py)、[当前 canonical](output/tool-target-hardcut-20261028/sessions/before.canonical.json)、[候选 canonical](output/tool-target-hardcut-20261028/sessions/after.canonical.json)、[当前完整 wire](output/tool-target-hardcut-20261028/sessions/before.responses.json)、[候选完整 wire](output/tool-target-hardcut-20261028/sessions/after.responses.json)、[正反例](output/tool-target-hardcut-20261028/sessions/examples.json)、[计量](output/tool-target-hardcut-20261028/sessions/measurements.json)。无 SHA/fingerprint 附件。

继续采用当前 lowerer + strict:false，本地精确 parser 为执行权威。根首屏/续页 union 会展平，read target 的内联 union 由当前 lowerer 处理；不增加 request 包装、不改 lowerer、不以 wire 接受代替授权。既有 190 请求实验只支持此路线的已知边界，不证明本次 SESSION/SESSION_ENTRY 候选已被真实接口接受。

## 9. 英文 function.description 候选

完整机器同值见 [descriptions.json](output/tool-target-hardcut-20261028/sessions/descriptions.json)。字段 schema 保留 include_tools、query、方向与页预算的必要说明。

`search_sessions`：

```text
Find saved conversations by title or message keywords; omit query to list by activity. This session's body hits cover only earlier content removed by compaction; other accessible sessions use saved ROOT history. Copy item.target (SESSION) to search/read a conversation, or hit.target (SESSION_ENTRY) to read a body hit. Title-only hits have no hit.
First page: optional query, lifecycle, limit.
Continue: cursor; optional limit. Send no other fields on continuation. Omit unused fields; null is not accepted.
```

`search_session_content`：

```text
Find saved questions and their answer process by keyword relevance. First page: query; optional target (SESSION), include_tools, limit. Omit target for this conversation's earlier content removed by compaction; messages still in context are excluded. Other accessible conversations use saved ROOT history. Results preserve speakers, times and historical quotes/comments; [truncated] marks omitted text. Each item.target anchors its question; copy a message.target to read_session_content to expand that message. Images are presence markers; generated markers and image metadata are not search terms.
Continue: cursor; optional limit. Send no other fields on continuation. Omit unused fields; null is not accepted.
```

`read_session_content`：

```text
Read saved messages directly or expand a search hit; searching first is optional. First page: optional target (SESSION or SESSION_ENTRY), direction, include_tools, limit, max_chars. Omit target for this conversation's earlier content removed by compaction; SESSION_ENTRY may omit session_id for the same range. Other accessible conversations use saved ROOT history. A SESSION_ENTRY anchor is read first. Long messages start at the beginning and continue across pages before adjacent messages; partial marks a slice. Tool output may have missing saved content as shown by source_coverage, display_kind and artifact_disposition; pagination cannot recover it. Images are text placeholders; same-workspace placeholders provide an IMAGE_REF target to copy into view_image. Increase max_chars if a placeholder cannot fit.
Continue: cursor; optional limit, max_chars. Send no other fields on continuation. Omit unused fields; null is not accepted.
```

## 10. 明确建议与未验证项

建议采用本规格：SESSION_ENTRY 输入允许当前会话省略形式、输出完整，cursor-only continuation 保持，search_sessions 的 SESSION 与 hit ENTRY 分清，自动召回只在最终模型结果加目标。无需新增产品前置确认或持久结构。

当前无待外部信息才能决定的产品阻塞。新 parser、结果复制／预算、范围与分页已通过本组聚焦行为验证；两个真实 provider 的填参行为及长程 dogfood 仍需整体实施验证。统一 target 的收益和实际调用成功率不得从 +150 静态 token 结果推导。
