# Pulsara MCP 工具混合检索实施规范

原生工具与 Code Mode 共用 `query_capabilities(kind="MCP", query=...)`。模型配置中有完整的 Embedding 连接时，Pulsara 将 BM25 与向量检索合并，从当前可见目录返回实际 schema 和调用引用，供模型通过 `call_mcp_tool` 执行。PostgreSQL 在应用重启后复用已有工具向量。真实模型的召回质量与准入阈值留待后续评测。

## 1. 各层负责什么

| 问题 | 由谁回答 | 本次改动 |
| --- | --- | --- |
| 哪些服务和工具对本次查询可见？目录是否完整？ | MCP supervisor 的已接纳目录、来源声明、ROOT／子代理范围和查询参数 | 沿用 `CapabilitySourceQuery._mcp()` 筛出的工具与服务。先定范围，再检索。 |
| 哪些工具匹配查询？ | 现有 bm25s、共享 embedding 客户端、PostgreSQL/pgvector | 工具按 BM25 与余弦相似度分别召回，有 dense 命中时用加权 RRF 合并。服务匹配沿用 BM25。 |
| 模型何时知道目录有变化？ | context source collector 比较本次冻结事实与已安装的 observation 快照；continuity owner 安装输入 | 沿用 `MCP_CATALOG` 和 `MCP_CATALOG_CHANGES`。检索后端状态只影响本次搜索。 |
| 接口、调用引用和实际执行是否仍然有效？ | 查询结果构造、会话内 MCP 引用 owner、执行前授权与 MCP executor | 根据当前接口生成结果、核验引用和参数。缓存只提供排序数据。 |

改动遵循根目录 [AGENTS.md](AGENTS.md)。持久化增量是一张可丢弃的派生表，产品理由是跨进程复用同一编码结果，免除重复远程请求。产品关系、事件、subject、append guard、持久任务及其 oracle 维持现有集合。表的写入边界、失败和丢弃语义见第 4 节。现有查询、引用和执行职责见 [capability_query.py](src/pulsara_agent/conversation_kernel/capability_query.py)、[tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py) 和 [tool_runtime_authorization.py](src/pulsara_agent/conversation_kernel/tool_runtime_authorization.py)。

## 2. MCP runtime observation 的现行合同

### 模型何时看见变化

- **冷 epoch 或明确采用的压缩 successor。** 在 Skill 目录之后、已有历史或压缩摘要之前，放一条纯文本 `CONTEXT_MESSAGE / MCP_CATALOG`。只列本作用域内已启用、具有公开连接 Target 的服务名；插件服务带来源。启用状态取自配置，连接尚未 READY 也可列出。初始快照不可得时说明未知；成功取得空清单时才说明没有已启用服务。
- **同一 epoch。** 在符合条件的新 turn 或本 turn 被采用的真人输入／steer，collector 对比当前冻结快照和上次安装的快照。有净变化时，以 `RUNTIME_OBSERVATION / MCP_CATALOG_CHANGES` 追加后缀。普通工具续跑、同 turn 的 Cell／Agent 结果和目录刷新本身都不单独触发提醒。原 SYSTEM、provider tools 逐字节稳定，旧 messages 是新 messages 的完整前缀。
- **比较基线。** 注册、预览、测量、被丢弃的候选和失败的安装均不推进基线。A → B → A 在两次有效观察之间抵消时没有净变化。新上下文采用当前目录建立自己的基线。初始未知后首次取得可靠快照时报告“服务目录已恢复”；同 epoch 本次拿不到 owner 时保留上次基线。
- **非 READY 的服务。** 比较快照为仍在目录中、暂未 READY 的服务保留先前可靠的工具事实。可用状态可以产生服务级提醒；恢复 READY 后才用当前工具集合比对。当服务确实移除时才产生移除提醒。新增、更新服务的提醒可携带当前连接 Target，已移除服务只给说明。

这些行为由 [context_sources.py](src/pulsara_agent/conversation_kernel/context_sources.py)、[input_continuity.py](src/pulsara_agent/conversation_kernel/input_continuity.py) 和[现有前缀与快照测试](tests/test_hardcut_runtime_observations.py)约束。SDK 的目录变化通知由现有 supervisor 负责重新发现并在既有边界采用；搜索直接消费查询 owner 的目录，不能依赖某次提醒已送达模型。

### 同一次变化，对观察、向量与调用各意味着什么

| 已接纳事实的变化 | 在上述观察时机的模型提示 | 工具文档编码 | 查询和调用 |
| --- | --- | --- | --- |
| 新增、确认移除、改名 | 已知 READY 服务按工具名称集合报告“工具目录已更新”；新服务按服务添加规则提示 | 新名字查缓存，缺失才算；移除无需计算 | 当前集合决定候选。旧名字不会由历史缓存复活。 |
| 同名工具的公开 description 变化 | 服务级“工具使用说明”变化 | 用新全文查缓存，缺失才算 | 从当前 executor 取得说明和引用。 |
| 仅 input schema 或 output schema 变化，包括参数说明、必填项、枚举 | 分别报告“工具输入参数定义”或“工具声明的返回结构”变化 | **零次文档重算** | 完整 schema 取当前值；旧绑定失效，重新查询。 |
| ROOT／子代理可见范围、effect kind、parallel safe 或 timeout 变化 | 按现有比较报告服务可见范围或“工具使用条件或可见范围”变化 | 文本相同则复用 | 每次按当前范围筛选，执行时核验策略、权限及引用。 |
| 服务显示名、使用说明、启停、可用状态、连接配置或实际目标变化 | 按现有字段报告对应服务级变化 | 工具名与描述相同则复用；服务名与服务说明参与原有服务 BM25 | 目录未知继续报告不完整；保留的已知目录可用于发现，执行连接暂不可用时结果按既有规则为 UNAVAILABLE。 |
| 仅 embedding 连接、冷／热缓存、补算成功或失败变化 | 无 MCP 目录净变化 | 按第 3–4 节处理 | 搜索排序可变化；这些事实不签发调用引用，也不唤醒模型。 |

观察比较的字段与调用引用绑定的字段分别沿用各自 owner。例如账号或认证事实参与引用校验，不能据此推断一定有对应文案。调用前仍检查当前绑定；目录暂时失联也不能推出工具永久删除。详见 [executor_reference_basis](src/pulsara_agent/conversation_kernel/mcp/meta.py) 和[真实 SDK 刷新测试](tests/test_mcp_tool_evolution_integration.py)。

## 3. 共享 embedding 连接与请求额度

1. **设置。** 模型配置是唯一入口。MCP 直接读取 `LocalSettingsStore` 中的 `memory_retrieval.embedding`，连接完整时自动按需使用，未配置、配置不完整或已清除时使用 BM25。`memory_retrieval.embedding_enabled` 只控制记忆检索。MCP 关键词查询会把查询词、当前范围内的工具公开名和描述发送到已配置的端点。
2. **客户端。** 复用 `build_embedding_provider`、异步 `embed`／`embed_batch`、凭据边界、请求限制和响应校验。把当前在 `KernelMemoryToolPort` 构造的 embedding semaphore 提升到组装两个消费者的 Host，注入记忆和 MCP 路径。同一 Host 内两者共用现有 5 个在途请求额度，每请求至多 10 项，向量 1024 维。批量处理可分批排队，这些物理限制不构成工具总数限制。记忆的事实维护器、查询阈值、Decision／rerank 仍归记忆功能。
3. **发送与变更。** 每次查询冻结当时生效的连接；只向启用的连接提交查询词和下面定义的工具文本，沿现有凭据边界防止实际密钥进入请求正文或证据。切换或清除连接后停止对旧配置的新准入；旧响应按其原空间归属，发布结果前复核当前连接。使用 API key 的连接更换端点时，需重新填写密钥。同端点轮换 API key 且编码空间相同时，已有向量可复用。若服务商在相同端点和模型名下静默替换模型，Pulsara 无法自动识别；必要时清空派生表重建。

接入位置见 [host.py](src/pulsara_agent/conversation_kernel/host.py)、[memory_tools.py](src/pulsara_agent/conversation_kernel/memory_tools.py)、[embedding 客户端](src/pulsara_agent/retrieval/embedding/openai_compatible.py)、[设置写入](src/pulsara_agent/settings.py)和[现有连接编辑界面](frontend/components/memory-retrieval-settings.tsx)。

## 4. PostgreSQL 缓存与按需生成

**编码输入。** 单一构造函数生成 `remote_tool_name + "\n" + (description or "")`。BM25 工具文档与 dense 共享这段原文。描述摘录仅供结果展示。服务 ID、schema、参数值、调用结果、执行策略和会话标识都不参与编码；仅写在 schema 中的词因此不会进入这两种工具检索。超过现有 embedding 输入物理边界的单项保留 BM25；批次组装时隔离该项，避免拖累同批合法文本。

**存储。** 在 clean-v0 baseline 增加下表，同步 expected catalog、runtime grants 和相应结构校验。文档与查询向量在转换为 pgvector 的单精度表示前归一化，以保留余弦方向并避免极端数值造成溢出或下溢。

```sql
CREATE TABLE pulsara_v3.mcp_tool_embeddings (
    cache_key bytea PRIMARY KEY CHECK (octet_length(cache_key) = 32),
    embedding public.vector(1024) NOT NULL
);
```

`cache_key` 为固定顺序数组 `[规范化 embedding 端点, model_id, shape, dimensions, 实际编码文本]` 经现有 `canonical_json_bytes` 编码后的完整 SHA-256 字节。端点取共享连接已有的规范化规则；这里的用途是跨重启确认同一计算输入。API key、内部版本标签和记忆专用的 `embedding_contract.contract_id` 均不进入键。该键只在派生表内寻址，公开的服务身份、工具身份与 `MCP_TOOL_REF` 沿用各自 owner。相同文本可复用同一向量，来自不同连接的同名工具在结果中仍各占一项。

**一次关键词查询的时序。**

1. 按当前查询范围取得已接纳目录；BM25 随即可以搜索。仅列服务、列指定服务工具、读精确详情或执行工具时，无需文档补算或查询词编码。
2. 共享连接完整时，只对本次可见工具计算 key，按主键批量读取缓存。存在可复用向量时编码一次本次查询词，在这些 key 对应的行上用 `embedding <=> query_vector` 排序；完全冷缓存时，本次先返回 BM25，连查询词请求也可省去。精确名称和 BM25 命中始终保留各自的现有接口信息。
3. 把缺失且可编码的文本交给 Host 管理的进程内异步任务按批补算。本次查询不等待目录全量建好；后续查询使用已提交的行。相同 Host 内合并同时在途的同一连接与输入，任务结束释放占用；其他 Host 偶发重复计算用数据库唯一约束收敛。采用有界在途批次，避免大目录一次排出所有 HTTP 请求、阻塞前台查询或记忆请求。连接预算、取消和关闭沿用宿主生命周期，进程退出允许丢失未完成工作。
4. 网络调用在数据库事务外完成；通过向量校验后，以短事务批量 `INSERT ... ON CONFLICT (cache_key) DO NOTHING`。读失败、超时或服务错误时，本次仍可用 BM25；写失败仅损失缓存。用户取消保留原查询的取消结果，不转换为一次成功降级。缓存不保存查询词、排序、调用引用或“目录完整”标志。旧目录中已不再使用的行可保留到数据库重置；清空整表只导致下次按需重算。
5. 远程等待期间目录可能发生变化。构造命中详情前沿原有 scope／来源／已接纳目录再次解析候选和当前 executor。描述已变时舍弃过期 dense 分数，关键词结果按当前文本核对；仅 schema 变化时可复用分数并读取新 schema。目录变化连续发生时按当前可确认的结果和既有不完整语义结束，不循环等待稳定目录。调用引用在原查询结果接纳流程中生效。每次安装新目录时，已经确认移除或绑定发生变化的工具会失去旧引用，即使在下次查询前恢复为原样也需要重新获取引用。

数据库接入复用 [verified PostgreSQL connection provider](src/pulsara_agent/storage/postgres_connection_provider.py) 和[现有 schema 基线](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)。缓存命中不证明工具已安装、在线、对调用者可见或可执行。

## 5. 双路召回与 RRF

- **BM25。** 保留 [mcp_search.py](src/pulsara_agent/capability/mcp_search.py) 现有工具和服务的联合语料、分词、正分匹配及服务展示。取出工具命中后，从 1 开始计算工具榜内名次；服务匹配仍放在工具之后。
- **Dense。** 余弦相似度为 `1 - distance`。只使用本次可见、已缓存、与查询词在同一编码空间的工具。先按相关性准入，再按相似度降序；同分使用当前目录稳定次序。密集向量对无关查询也会产生最近邻。当前暂以余弦相似度 ≥ 0.40 纳入 dense 榜。此值尚未校准，第 7 节列出后续验证方法。
- **合并。** 使用查询 owner 当前的工具身份 `(server_id, remote_tool_name)` 合并两张榜。有工具通过 dense 准入时，分数为 `0.1/(60 + BM25 名次) + 1.0/(60 + dense 名次)`，缺席一路时该项计 0。没有工具通过 dense 准入时沿用 BM25 工具顺序。查询串精确匹配完整工具名的结果作为首组；组内沿用稳定目录顺序。其余工具按 RRF 分数降序，同分回到稳定目录顺序。服务仍按原有 BM25 次序追加。向量不可用时保留词法搜索及精确名称优先行为。
- **交付。** RRF 为最终排序。沿现有结果构造取得工具描述、完整 input schema、可选 output schema、实际可用时的 invocation target；dense 独有命中也提供可读描述摘录。沿用现有结果分页与 Cell 接口。混合检索中，仅进 BM25 榜的第 1 名得 `0.1/61`，仅进 dense 榜的第 1 名得 `1.0/61`，两榜均为第 3 的工具得 `1.1/63`。同义工具进入候选后，最终名次仍需实测，无法从公式保证它排第一。

## 6. 实现入口

1. 模型配置通过现有 Embedding 连接编辑器保存或清除连接。Host 在每次关键词查询时读取当前配置。
2. [缓存适配器](src/pulsara_agent/capability/mcp_embedding_cache.py)负责按 key 查找、写入和余弦评分。[Host 的补算任务](src/pulsara_agent/conversation_kernel/mcp/tool_embeddings.py)负责请求准入、批处理和关闭。
3. [关键词查询](src/pulsara_agent/conversation_kernel/capability_query.py)先按当前作用域取目录，远程等待后再核对工具。[工具排序](src/pulsara_agent/capability/mcp_search.py)按第 5 节合并两路结果。

## 7. 行为合同与后续质量评测

| 场景 | 可观察的验收结果 |
| --- | --- |
| 冷启动、重启、并发查询 | 用仓库的 65 个 Notion 工具检查每个不同输入的编码次数。冷库首查可先给 BM25；补齐后关闭并重新打开宿主，连接和文本相同，工具文档编码数为 0。新查询词请求另计。并发、取消、停机后已提交行可用，未完成项可再次按需计算。 |
| 变化与执行 | 单改描述仅新文本缺失时编码；单改输入／输出 schema 为 0 次文档编码，旧引用拒绝派发、新引用按新 schema 调用；另一项未变的引用继续可用。改名、删除、相同公开名的多来源、ROOT／子代理范围缩小、列表刷新中暂不可用及恢复均通过实际查询入口覆盖。 |
| observation 与前缀 | 冷上下文、采用压缩、同 turn 续跑、真人 steer、未知恢复、A→B→A、候选安装失败按第 2 节比较模型可见消息和实际安装状态。只变更 Embedding 连接或补齐缓存时无目录净变化；单改 schema 时既有类别提示照常追加。逐字节比较既有 SYSTEM／provider tools 及完整旧消息前缀，覆盖原生与 Cell 查询后的继续执行。 |
| 可用性与降级 | 缓存缺失、PG 缓存操作出错、远程 embedding 超时、超长描述和查询取消分别验证。已有可用的词法结果与引用继续返回；若另有已启用服务的目录不可可靠读取，保留原有部分结果及不完整说明；已知空目录和完全无关查询可返回无匹配。 |
| 后续评测：排名与真实成本 | 固定真实工具目录，比较 BM25 单路和融合结果。含精确名字、同义英文、同对象不同动作及无合适工具的查询；例如 `call off a meeting`、`reschedule a meeting`、`make a copy of this document`。在校准样本选择 dense 准入值，再用独立查询核对目标名次、前 5 召回率、无匹配误报与冷／热耗时。前 5 是评估窗口。若改写召回未改善或无关查询大量携带完整 schema，应回到样本检查两路名次及准入值。 |

实施时复用[目录完整性测试](tests/test_mcp_search_completeness.py)、[目录刷新集成测试](tests/test_mcp_tool_evolution_integration.py)、[输入连续性测试](tests/test_hardcut_runtime_observations.py)和 [Notion 工具样本](tests/fixtures/notion_native_tools_20261020.json)。后续真实 embedding 评测经 `require_pulsara_home()` 与 `LocalSettingsStore` 读取用户保存的连接，使用只读配置注入和已核实的本地可丢弃数据库。记录查询、候选名次、缓存冷／热状态及真实失败原因，排除实际凭据；缺少所需连接时明确标注未验证的部分。
