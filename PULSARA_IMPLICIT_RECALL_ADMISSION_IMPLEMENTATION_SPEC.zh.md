# 隐式召回：Decision 准入与中英文停用词

日期：2026-10-12。范围：本次新请求的隐式召回与共享词法查询；不重写已安装的 provider 输入前缀。

状态：代码已落地，已通过真实 pg_search 与保存的 Decision 配置复算。

初版验证：相关检索、历史、记忆和协议测试共 **212 项通过**，Ruff 与 diff 检查通过。Decision 准入已重启生效；本节停用词已按后续讨论改为自主维护集合，验证见下文。生产设置、规范记忆与会话数据未修改。

自主词表验证：**36 项相关真实 pg_search 回归通过**，覆盖新增语气词、英文冠词/系动词、跨字段过滤及否定/条件/数量/人称保留；Ruff 与 diff 检查通过。外部词表及对应许可证文件已移除。

## 1. Decision 准入

当前检索仍先取各路最多 20 条，普通记忆融合后与历史按 15＋5 弹性配额组成最多 20 条候选池。保持一次远程请求，不新增查询生成模型或额外上下文。

隐式召回使用独立的 `automatic_recall` 判断目的：条目是否对本次任务有具体用途，包括适用的事实、偏好、约束、已有决定、可复用成果或必要检查。共同词语、相似主题、可选灵感不够；不能自行假设用户在延续另一个项目或会话。开放式写作或测试请求不因此需要旧题材，但适用的用户偏好仍可通过。

仅当本次稳定配置是 Decision 时，以返回的肯定概率 **p ≥ 0.7** 准入。先核验全部候选响应完整、唯一且概率合法，再筛选、降序排序，最多选择 5 条；相同分数按原候选顺序。普通记忆与历史使用相同阈值。允许空结果，不为凑满五条补入被拒绝或未判别的候选。0.7 是用户选定的初始产品边界，不是跨模型校准保证，不新增设置字段或前端控件。

阈值只作用于隐式召回；显式 memory_search、remember 的 related_memory 检查保持排序行为。普通 Reranker 的数值不是肯定概率，不应用此阈值。重排关闭时保持既有 4＋1 弹性选择；普通 Reranker 失败时保持既有降级。

Decision 配置存在但不可用、请求失败、超时、投影不能发送、响应不完整或配置切换导致结果丢弃时，历史候选不绕过准入；普通记忆保持本地顺序降级，最多五条。取消继续由原所有者结算，不吞取消异常。选中历史仍需规范回读，失效后不补位。

## 2. 中英文停用词

Pulsara 在共享词法 owner `retrieval/pg_search.py` 直接维护 **56 个中英文停用词**，不导入外部词表，也不再维护“上游词表减去保留词”的逻辑。

中文 35 个：`的 地 了 着 著 啊 呀 呢 吧 嘛 吗 麼 么 哦 喔 嗯 呃 哎 唉 啦 呗 嘞 喽 噢 哟 唷 嘿 呵 哇 和 与 與 及 而 是`。

英文 21 个：`a an the of and to in for by as on with is are am be been being was were please`。

English stemming 路同时过滤 `please` 的词干 `pleas`，避免词干化后绕过过滤；它是同一词的分析形式，不是新增语义停用词。

另保留既有标点/空白过滤。否定、情态、条件、数量、时间、人称及指代词不加入停用集合，例如“不、没有、可以、当前、之前、只有、必须、你、我、都、得、就”及 `not no can if all one or we you it this then from which have has`。该集合是保守起点，不追求词表大小或完整性；新增词应有具体噪声案例及相关查询回归，不新增词表管理系统。Jieba 仅作为现有 pg_search 中文 tokenizer 继续使用，不引入 Python Jieba 分词路径。

停用词在 **pg_search 0.26.1 的查询分析** 中处理。共享词法 owner 使用公开 `pdb.match(value, tokenizer => jsonb)`，分别保留已有 Jieba search-mode、English stemming、技术 regex；三个字段均应用同一停用词，避免通过另一个 OR 分支重新命中。记忆检索与会话搜索共用此路径，排序、范围、分页、Q-A-Q、截取和批注规则不变。

查询分析不改变规范原文、派生索引正文、Embedding 输入或 Decision 输入。评分和 snippet_positions 使用同一查询配置，位置仍是原索引正文 UTF-8 范围；不在 Python 中删字或重建位置。纯停用词查询允许无词法结果，不退回未过滤查询。

pg_search owns tokenization, stemming, term matching, BM25 and byte positions. Pulsara owns the compact vocabulary policy, candidate qualification and implicit admission. Index analyzers keep their existing configuration; query-time filtering needs no schema change, re-embedding, index rebuild or production database reset. No new durable events, jobs or projection authorities are introduced.

## 3. 验证

词表扩至 56 词后的聚焦真实 pg_search 回归共 **38 项通过**，覆盖新增语气词、`please`/大写形式、混合查询、原文命中位置、实义词保留及分页；Ruff 与 diff 检查通过。

- p=0.7 通过、p<0.7 拒绝、全拒绝、通过超过五条、普通记忆/历史统一准入；每次最多一次判别。
- Decision 不可用/失败/坏响应时历史不注入，普通记忆继续；普通 Reranker、显式检索和相关记忆检查不套阈值。
- 真实 pg_search 验证中文/英文纯停用词为空、混合查询不因停用词匹配无关行，否定/条件/时间/数字保留，英文词干与路径/API_key 可查。
- 命中位置与原文一致，历史显式工具与隐式候选共用分析，稳定分页与可读范围回归。
- 用保存的 Decision 配置复算此前通用写脚本/压缩请求及有具体历史需求的对照请求，记录实际分数和通过集合；生产设置只读，不导出密钥。

### 实际 Decision 复算

保存的模型：`perplexity/pplx-decider-v1.1-27b`，System One。按当前语料重建会话 `5e910ea3` 的原始压缩前可读范围；不是当时 HTTP 请求的捕获，也不撤回既有前缀。

| 请求 | 最高肯定概率 | 通过数 | 单次 Decision 时间 |
| --- | --- | --- | --- |
| 随便写东西以测试压缩 | 0.0317 | 0 | 1.46 秒 |
| 随便写 Python 脚本并运行 | 0.0145 | 0 | 1.31 秒 |
| 再随便输出以测试压缩 | 0.0305 | 0 | 1.58 秒 |
| 明确询问之前出游预算的 A/B 费用 | 0.9955 | 2 | 1.64 秒 |

四次请求均成功，每次一轮判别。对照请求第二条通过项为 0.9942；另一个 0.5331 分的候选未通过。证据保存在忽略目录 `output/recall-admission-20261012/live-results.json`；原诊断证据保留。该样本说明此次问题得到拦截，不表示完成准确性或跨模型阈值校准评测。

上表是首次 Decision 准入实现时的复算记录；后续自主词表替换没有修改 Decision 问题或阈值，没有重复该远程实验。
