# Pulsara 隐式记忆召回时延实验计划

日期：2026-10-09

状态：计划待执行；本文不代表生产隐式召回已启用重排，也不包含实测结果。

## 1. 本轮要回答的问题

在 500、1000、1500、2000、3000 条记忆下，比较当前无重排隐式召回，以及加入 Qwen reranker、Jev decision、Luna decision 后，用户消息进入 Pulsara 到主回答模型请求发出的耗时。

本轮只评估时延、可用性和实际完成的工作量。暂不评估记忆相关性、Recall@K、排序准确性或回答质量，也不根据时延结果宣布哪种模型的记忆效果最好。候选数和最终注入数固定，记忆库变大时不同时调高这两个参数。

## 2. 计时边界

主指标为 **prompt-to-provider-dispatch**：

- `t0`：Pulsara 服务接收本次用户消息，进入正常提交入口。
- `t1`：主回答 LLM 的请求交给实际 HTTP transport，开始发送。冻结调用对象、组装完提示词或进入 provider adapter 均不能代替这个时点。
- 总耗时：用同一进程的单调时钟计算 `t1 - t0`。

包括正常准入与调度、上下文和能力准备、查询分词及 embedding、稀疏/向量检索、RRF、canonical 行读取、可选重排、关系读取、记忆 source 投影、提示词编译与预算检查、请求序列化等前置步骤。查询 embedding 和 decision 请求属于这一段准备工作；它们的 dispatch 不能误认成 `t1`。

不包括语料生成、记忆 embedding 建库、索引构建、应用进程启动、主回答模型响应/生成或前端显示。并行步骤分别记录 span，总耗时直接测量，不能把各 span 相加。

实际主请求按正常 provider 路径发送；后续回复及必要收尾在下一样本前完成，耗时不计入主指标。不能用“到了假 provider”替代真实 dispatch，也不能留下上一样本的后台请求干扰下一样本。

## 3. 固定的运行条件

应用进程保持运行。每个样本使用独立、无历史消息的会话，保留正常必需的 SYSTEM、tools 与准备流程；不人为构造长上下文，不让重复测量累积成越来越长的会话。

本轮不设置冷启动、长历史、compaction 等独立变量。提示词组装仍计时，是否构成瓶颈由阶段数据判断。会话内遵守既有 provider prefix 连续性，不为切换实验组重写已安装的前缀。

固定主回答模型、embedding 后端、机器、数据库配置、权限、可见 scope、自动召回开关及其他配置。一次只运行一个前台样本；并发负载测试另行开展。回答偏好 source 保持同一空态，不作为规模变量。

## 4. 四个实验组

| 组别 | 隐式记忆路径 | 重排/判别输入 | 最终记忆条数 |
| --- | --- | --- | --- |
| A：当前基线 | 稀疏 + dense → RRF → 当前 canonical 选择 → source | 无重排；沿用当前至多 5 条路径 | 至多 5 |
| B：Qwen | 稀疏 + dense → RRF → canonical 候选 → Qwen → source | 至多 20 条，一次 rerank 请求 | 至多 5 |
| C：Jev | 同 B 的候选准备 → Jev decision → source | 至多 20 条，一次 decision 请求 | 至多 5 |
| D：Luna | 同 B 的候选准备 → Luna decision → source | 至多 20 条，一次 decision 请求 | 至多 5 |

Qwen 使用仓库现有 `qwen3-rerank` 集成。为减少网关差异，首轮 Jev 与 Luna 均经 OpenRouter Decisions 调用，模型分别为 `typesafe/jev-1.13`、`openai/gpt-6-luna-decisions`；执行前验证实际账户可用性并记录准确模型标识。Jev 官方直连若需测量，单列补充实验，不混入 C 组。Luna 官方 API 不在本轮范围内。

B/C/D 在相同稀疏、dense、RRF、scope 和生命周期规则下选出候选。先完成至多 20 条候选的重排，再截取最终至多 5 条；不能先截成 5 条再重排。A 与其他组的配对差值包含扩大 canonical 候选池、远端重排及相关投影的全部增量成本。

每次 decision 请求把当前查询和候选列表放入共享 `state`，为每条候选建立一个具备稳定候选 ID 的 `noul` 问题。统一问题语义为“这条记忆是否有助于处理当前用户任务”，覆盖背景、偏好、已采纳决定与需要核实的前提。Qwen 指令采用相同任务目标。

decision 按返回值降序排列，相同分值保留原 RRF 顺序。首轮不加入额外分数阈值，不给每条候选追加多种判别问题，也不发 20 个串行 HTTP 请求。检查响应完整覆盖请求中的候选且数值有效；不能静默使用缺失或错配的答案。

保留隐式召回现有 kind、可见性、敏感信息、冲突与低权威规则。自动查询没有显式 requested-kind 优先级；FACT、USER_PROFILE、DECISION 在既有条件下参与候选，RESPONSE_PREFERENCE 保持独立 source。模型排序不应被新增 kind 排序覆盖。

## 5. 语料与查询的低成本构造

### 5.1 记忆库

先编写少量自然的母样本与模板，再用确定性 Python 脚本组合主题、项目、对象、条件和句式，扩展为 3000 条主语料。首轮不需要模型逐条生成全部记忆；如果模板重复明显，可只对小批量样本做廉价模型改写，费用单独记录。

- 中文记忆约 70 汉字，英文记忆约 50 words；使用自然长度波动，不逐条强行补齐。
- 覆盖 FACT、USER_PROFILE、DECISION，固定生成种子、ID、语言与 kind 分布，使用虚构个人和项目。
- 构造 500 ⊂ 1000 ⊂ 1500 ⊂ 2000 ⊂ 3000 的嵌套集合；各档语言、主题和 kind 比例近似一致。
- 每档实际装载并索引对应数量的 canonical 记忆；不能始终装载 3000 条，只靠查询过滤假装较小数据库。
- 使用当前真实 embedding 后端 `text-embedding-v4` 和 1024 维向量。主语料 embedding 只生成一次，各档复用；随机向量不能作为主实验。
- 使用既有 clean-v0 schema 与 canonical 写入/建库机制，保留关系约束和向量索引。

查询主题在最小集合中也有配套背景，并在各档安排足够多的主题相近条目，以便实际检索通常能形成 20 条候选。仍保留既有相似度门槛、稀疏匹配和 canonical 过滤；不足 20 条时如实记录，不能复制、填充或绕过过滤来凑数。

这些语料只需满足工作量和文本自然度要求，不标注 gold relevance，不为本轮构造大规模难负例、复杂时序冲突或准确性评分集。保留未来另建准确性语料的空间。

### 5.2 用户查询

准备 10 条中文和 10 条英文查询，分别约 100 汉字和 70 words，覆盖不同主题和任务表达。先检查能走到真实隐式召回路径，避免把未触发召回的短路样本当作重排时延。

记录查询和记忆的实际字数/词数、UTF-8 字节数，以及供应商返回或可可靠取得的输入 token 数。请求开销还包含元数据、共享 state、问题和指令，不能仅用正文长度估算全部输入。

## 6. 样本矩阵与执行顺序

首轮使用 5 个规模 × 4 个实验组 × 20 条查询 × 3 次重复，共 **1200 个计时样本**。其中 B/C/D 合计至多 900 次重排或 decision 请求，另有正常查询 embedding、主模型请求与建库成本。

建库、连接准备和小规模 smoke 单列，不计入这 1200 个样本。正式测量在每档规模内按固定种子交错实验组与查询顺序，避免某个组总在同一时间段运行。样本使用配对查询；记录顺序和实际时间，便于检查远端波动。

仅建库 embedding 可复用。每个正式样本正常执行查询 embedding、数据库检索和对应远端重排；不能读取提前缓存的查询向量、候选或 decision 答案来代表实际链路。模型名称、版本、路由、超时和重试配置在开始前记录，期间保持固定。

每组每种语言在每档只有 30 个样本，尾部估计有限。报告 p50、p90、探索性的 p95、最大值和样本数，不用这一规模宣称稳定 p99。若首轮发现明确瓶颈，再有针对性增加重复次数。

## 7. 分阶段证据和失败统计

至少记录以下 span 与工作量：

| 阶段 | 必要证据 |
| --- | --- |
| 接收与正常准备 | `t0`、准入/调度、准备及主 dispatch 时点 |
| 查询处理 | 分词耗时、embedding 耗时、是否真的请求及是否成功 |
| 数据库召回 | sparse/dense 各自耗时与条数、RRF 合并数、canonical 读取耗时与候选数 |
| 重排/decision | 构造请求、远端往返、响应处理耗时；候选与问题数、模型、状态 |
| source 与主请求 | 关系读取、投影、编译/预算、序列化耗时及最终注入数 |

优先复用 `tests/dogfood/probe_prompt_startup.py` 的 process-local profiling 和现有 provider transport 注入点，补齐接收与真实主 dispatch 边界。独立的“查询 + 20 文档 API 往返”可作辅助诊断，但不能替代主指标。

只做机械验证：实际走过 embedding 和检索、候选 ID 对齐、响应完整且数值有效、最终记忆至多 5 条、主请求按正常路径发送。最终被预算降级为更少条目时记录实际投影形态和条数。

区分正常完整执行、候选不足 20、未触发召回、dense 不可用、API 超时/错误、响应非法和退回 RRF 等状态。远端失败可以按已明确的实验降级路径继续主请求，但不能记成该模型成功；被降级缩短的耗时不能混作成功性能。

汇总同时给出所有尝试的状态分布、成功完成链路的时延、成功且候选满 20 的时延、失败/降级样本耗时。未到达主 dispatch 的样本报告失败及已耗时间，不能伪造 `t1` 或悄悄丢弃。

沿用适用的现有取消与超时机制，记录各供应商实际超时、重试配置及差异。诊断中止条件与产品运行边界分开，不增加任意总 turn 或任务生命周期上限。

## 8. 实施边界与配置

本文记录实验设计；当前交付是计划文件，尚未实施或运行实验。后续实验接入沿用当前 runtime 的召回、canonical、source 与 provider preparation owners，以最小实验注入点接入排序；不复制一套检索、调度或 provider 框架，也不直接改变生产隐式召回策略。

供应商 SDK/transport 负责网络请求与其支持的协议行为；Pulsara 负责候选可见性、身份对应、排序结果应用、最终 source 和取消边界。适配层只转换请求与响应，执行前检查依赖的实际接口及支持的注入点。

从 `LocalSettingsStore` 与 `require_pulsara_home()` 读取保存配置，保持只读；不 source `.env`，不把凭证导出到 shell。Qwen、Jev/OpenRouter、Luna/OpenRouter 所需配置分别明确解析，缺少连接或凭证时报告具体缺项，不能借用不相关的聊天路由或把 fallback 记成已测模型。

使用已验证的本地独立可丢弃数据库和现有 read-only settings injection。读取保存配置后才切换临时 Pulsara home；不修改保存设置或原数据库。记录 Git revision、工作区差异和有效非秘密配置，不引入逐文件 SHA、产品事件、持久化 job 或证明实验运行的数据库机制。

实验请求、候选、响应、错误和阶段数据保存为本地实验产物；只清除实际凭证值，保留可诊断正文。它们不成为生产语义状态。

## 9. 执行步骤和交付物

1. 检查保存配置、供应商接口、计时插入点及隔离数据库路径；先证明 `t0/t1` 对应真实入口和主 transport。
2. 生成主语料与查询，计算一次记忆 embedding，装载首档数据库；记录长度分布、分布比例和 setup 耗时/费用。
3. 每组做少量 smoke，确认真实候选、一次请求的批量形状、最终 source、dispatch 和失败记录。
4. 依次装载五档规模，运行交错的正式样本；每档核实真实行数及索引完成状态。
5. 输出逐样本 JSONL、汇总 CSV 和 Markdown 报告，保留配置、语料/查询及生成脚本，使实验可重跑。

报告按规模、组别、语言列出主时延分位数和失败率，展示阶段分解及配对的总耗时增量，标明样本不足、候选不足和降级影响。setup 时间/费用单列，不并入 prompt-to-provider-dispatch。

本轮最终应能判断：哪个排序后端的前置时延更低、增量主要来自哪里、数千条记忆是否显著增加检索成本、远端尾延迟和失败是否值得关注。是否扩大候选数、增加最终注入条目或选择某模型上线，需要后续准确性实验和产品决策；本轮固定 **候选至多 20、最终至多 5**。

## 10. 接口与代码参考

- 当前隐式召回及候选截取：[memory_tools.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/memory_tools.py)
- 检索与配置：[recall.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/memory/recall.py)、[config.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/retrieval/config.py)
- Qwen adapter：[dashscope.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/retrieval/rerank/dashscope.py)
- 正常 provider 准备：[provider_dispatch.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/provider_dispatch.py)
- timing 与隔离 dogfood：[probe_prompt_startup.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/dogfood/probe_prompt_startup.py)、[run_direct_memory_dogfood.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/dogfood/run_direct_memory_dogfood.py)
- [OpenRouter Decisions 请求协议](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)
- [Jev 模型](https://openrouter.ai/typesafe/jev-1.13)、[Luna Decisions 模型](https://openrouter.ai/openai/gpt-6-luna-decisions)
- [Qwen 文本重排接口](https://help.aliyun.com/en/model-studio/text-rerank-api)
