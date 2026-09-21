# Pulsara 直接写入式 Advisory Memory：Hard-cut 实施规格

状态：**直接写入与“移除真实 ToolResult 引用”语义 hard-cut 均已实施并验收（2026-09-21）**。本稿描述唯一现行合同；仓库根目录 `AGENTS.md` 的前缀连续性、小 durability、长程可用性和 hard-cut 约束始终适用。历史记忆文档不是兼容目标。

## 1. 目标与非目标

原治理路径曾让 `remember` 只提交待审 candidate，由 Host-local governor 等来源 turn terminal 后再决定是否成为正式记忆；直接写入 hard-cut 已移除该路径。保留的产品核心是 ROOT 主模型决定何时记录及记录什么，`remember` 的 canonical ToolResult 与正式记忆在同一事务提交，返回成功时即可读取。本轮进一步切断对原始观察 ToolResult 的持久引用。用户在记忆页事后查看与删除。记忆始终是可能错误、过期或未召回的 advisory 数据，不是权限、项目真源、执行器或完成保证。

本次不保留 candidate、governance、terminal claim、辅助审核模型、待审状态或旧数据库兼容路径；不增加 durable job、关系修复器、事件回放、额外回执或后台全库冲突扫描。可重建经核验的本地 disposable clean-v0 数据库，不迁移旧 candidate。

工具输出仍作为普通会话 ToolResult 供模型当下阅读，但**不能作为记忆的持久支持引用**。模型认为某段观察值得留存时，自行以自然语言调用一次 `remember(kind="FACT", statement=...)`，写下有主体、必要项目／时间条件和不确定性的 advisory 摘要；不得把原始输出、任意工具结果 ID 或整个工具调用自动转存为记忆。只有在另有**语义不同且确实依赖该 FACT** 的结论、决定或偏好时，模型才再调用 `remember(based_on_memory_ids=[...])` 建立记忆到记忆的依赖；同义改写或单纯换成更广的检索范围，不构成必须另存一条的依据。原始工具结果是否仍可在会话中打开，与这条模型撰写的 FACT 是否存在、如何删除分属两个合同。

## 2. 已定产品语义

1. 只有 ROOT 可调用 `remember` 和 `mark_memory_relation`；现有 subagent 读取能力不因此收窄。用户当轮禁用记忆、scope、权限及记忆依据可见性继续适用。工具面与提示词只在新冷 epoch 或已采用的 compaction successor 中更新；不能为了让新工具生效而重写运行中 epoch 的前缀。
2. `remember` 不等 turn terminal，也不等未来 steer。正式记忆一经提交，同一 turn 的后续精确／稀疏读取即可看见；下一次 provider dispatch 依第 8 节查询当时的 canonical 偏好并刷新已变化的偏好 source，包含记忆页删除、恢复及其他 Host 写入。其它 kind 不因此强制自动注入。之后的 steer 不能撤销已提交结果。用户要删除时，ROOT 不能假称已删除或用 `SUPERSEDES` 代删，应指向记忆页。
3. `remember` 必填最终 `kind`，且只能为 `USER_PROFILE`、`RESPONSE_PREFERENCE`、`FACT`、`DECISION`。删除 `kind_hint/AUTO` 和二次分类；ROOT 拿不准可选 `FACT`。类别仍决定检索、回答偏好投影和精确重复身份，但不授予更高指令权威。
4. 新事实正常提交返回 `SAVED` 和真实 `memory_id`。同一 memory domain、精确 context、最终 kind、规范化 statement 已有 ACTIVE 事实时，返回 `ALREADY_PRESENT` 与既有 ID；这次调用不改变旧来源、不新增 `BASED_ON` 或其它关系。不同 kind 的同文不算精确重复；语义近似重复允许共存。
5. `BASED_ON` 是 ROOT 在 `remember` 参数中直接声明的**既有正式记忆**依据，与新事实同事务写入；它表示新记忆对旧记忆的真实语义依赖，不是原始工具结果的 provenance 标签，也不是为重复／扩范围副本自动生成的关系。删除依据会按第 6 节级联删除依赖项。`CONTRADICTS`／`SUPERSEDES` 则是在新事实保存后，ROOT 看见 `remember` 结果中的相关旧记忆，或以后实际召回旧记忆时，间接通过 `mark_memory_relation` 显式标定。相关性结果本身不自动建立关系，也不要求模型必须标定。
6. 用户记忆管理 GUI 只在记忆页：浏览、来源、关系、删除和删除影响预览都在那里；会话页不加记忆管理入口、按钮或关系操作。首版不新增人工关系标定按钮；若将来需要，另立产品路径。会话仍可照常显示模型消息及工具结果，但不承担记忆管理。
7. 主模型撰写和关系判断不再有第二模型的语义拒绝保证。从工具输出转写 FACT 时，source fidelity、时效性、prompt injection／记忆回声、秘密／流程取舍、类别判断和未标冲突都是接受的 advisory 风险；确定性验证只声称验证权限、形状、记忆依据身份和事务，不能伪称核实模型摘要与某个原始工具结果一致，或识别所有敏感与错误语义。记忆投影继续明确低权威，使用前应核对可读的当前项目真源。

## 3. `remember` 的工具与结果合同

唯一输入为 `statement`、`context_target`（`GLOBAL`／`CURRENT_PROJECT`）、必填最终 `kind`，以及可选的 `based_on_memory_ids`。`cited_tool_result_handles`、工具结果 citation handle 或任何替代的原始 ToolResult 证明参数均不存在；不得保留 `kind_hint/AUTO` 的旧形状。保留现有单条正文、最多八个记忆依据和调用资源边界；`RESPONSE_PREFERENCE` 仍受单条 2 KiB 正文限制，其它事实仍受单条 8 KiB 限制。`FACT` 回退不能绕过正文、记忆依据或权限校验。`remember` 的工具描述应教会模型：工具输出可以启发一条模型撰写的 FACT，但不是精确验证过的记忆来源；不要机械地为每次工具调用建 FACT，也不要为同义扩范围副本建依赖。

先按现有入口验证 ROOT 身份、当轮 memory opt-out、有效权限、context 和每个提供的记忆依据。`BASED_ON` 目标必须在可见 memory domain 中仍为 ACTIVE，且满足现有 global／project 层级：global 只能依赖 global；project 可依赖 global 或自身 project。本轮不借移除工具引用之名悄悄放宽跨 context 的 `BASED_ON`；若以后允许 global 依赖 project，需另行冻结可见性、跨项目来源遮蔽及删除级联。这些只读验证是预检；canonical writer 在同一提交事务中仍须按稳定顺序锁定并重验依据的 domain、context 和 ACTIVE 生命周期，不允许预检后发生的删除／取代穿透。即使将返回 `ALREADY_PRESENT`，无效依据也不能绕过验证。工具结果的两个成功状态如下：

| 状态 | canonical 结果 | 事实／依据副作用 |
| --- | --- | --- |
| `SAVED` | 新事实的 `memory_id`、最多三条相关旧记忆和检索覆盖信息 | 新事实、其直接来源及所声明的 `BASED_ON` 同事务提交 |
| `ALREADY_PRESENT` | 已有 ACTIVE 事实的 `memory_id`、最多三条相关旧记忆和检索覆盖信息 | 不改已有事实、来源、依据或关系 |

应用错误、权限拒绝、写事务失败、过期 Host writer、取消或无法确认的结果均不得对模型／UI 宣称 `SAVED`。沿用现有 exact ToolResult acceptance、writer guard、取消及模糊 ACK confirmation；同一工具调用重放返回其原 canonical 结果，不再执行一次写入。成功状态只能在数据库已确认的 canonical 工具结果中出现。对这两个动态结算的工具，writer 在事务内生成最终 canonical 正文与 ToolResult occurrence，不能把事务前猜测的正文用于 exact confirmation。模糊 ACK 时按既有 result／entry／call／attempt 身份读取历史 canonical ToolResult，核验它与冻结输入及允许的结果形状 exact-join；若确有赢家，就返回其已提交的 `SAVED`／`ALREADY_PRESENT` 正文；确认不存在才按原冻结输入重试，冲突或仍不确定则返回原有不确定结局，不宣称成功。确认不得依赖相关 memory fact 或 relation **此刻仍存在**，因为用户可能已在记忆页删除它们。live 成功正文只在提交或 exact confirmation 后发布。

### 3.1 默认相关旧记忆

每次有效 `remember` 都尽力做一次写入前只读检索，以 statement 为 query，复用现有 sparse＋可选 embedding＋可选 reranker 实现。关系候选只在**与新事实完全相同的 context**中找 ACTIVE 事实，可跨 kind 供 `SUPERSEDES` 的 taxonomy correction 判断；不可沿用普通记忆搜索的 context 放宽或自动召回排除回答偏好的过滤。按现有有界候选池和 canonical refetch，最终返回最多三条；该上限只约束单次模型可见工具结果，不限制记忆总数、搜索能力或将来的关系标定。每条给精确 ID、原文、kind、context、时间及允许展示的来源状态。精确重复时，把赢家自身从这三条中排除。

检索只提供线索。复用现有检索阶段及降级摘要，明确区分 `COMPLETE`、`PARTIAL`、`UNAVAILABLE`；这里的 `COMPLETE` 仅指本次有界检索按计划执行，不表示穷尽全部记忆或证实没有冲突。零命中、embedding／reranker 故障、部分结果或检索超时均不阻止正式保存；先保留可用的 sparse 结果，失败时如实标记降级。可选远程步骤受现有工具／检索 deadline 约束，不得持有 canonical 写事务等待远程模型，也不得耗尽写入所需的事务预算。之后 ROOT 仍可用常规 `memory_search`／`memory_get` 检查三条以外的记忆。

只读检索完成后，既有 canonical ToolResult acceptance owner 在**同一写事务**中决定 `SAVED` 或并发精确重复的 `ALREADY_PRESENT`、写入事实（若有）、记忆依据和最终工具结果；不写工具结果支持引用。以现有 active semantic partial unique index 为仲裁：在当前 READ COMMITTED writer transaction 内用无污染的冲突插入取得新事实，或等并发提交后重新读取并锁定 exact domain／context／kind／规范化正文的 ACTIVE 赢家，再生成 `ALREADY_PRESENT`；若赢家同时被删除而重读为空，就在同一事务的 deadline 内重新仲裁，不能另开第二个 canonical 结算事务，也不能把唯一冲突直接报成虚假写入失败。只读候选不是去重权威。

不能先把 provisional `SAVED` 作为 live tool result 发给模型／前端，再发现事务失败；需对这两个记忆变更工具采取窄的提交后发布路径，不新增 durable receipt 或第二事务。候选快照可能在检索后变旧，故后续关系工具始终重验精确 ID、scope、kind 和生命周期；工具结果不得称这些候选已经构成冲突或取代。新保存／已存在的 `memory_id` **和**至多三条候选 ID 仍可作为普通模型可见记忆 ID 返回，但不发行 ToolResult citation handle、不建立工具结果证据类别，后续 `remember` 若真正依赖其中的正式记忆，使用精确 `memory_id` 建立 `BASED_ON`；仅看见某个结果不自动生成依据或独立观察来源。

## 4. `mark_memory_relation` 的工具合同

新增 ROOT-only 受控写工具；输入为 `source_memory_id`、`target_memory_id`、`relation_kind`（仅 `CONTRADICTS`／`SUPERSEDES`）。工具可接受 `remember` 返回的三个 ID，也可接受以后从正式搜索／精确读取获得的 ID；不得要求候选必须来自最近一次 `remember`，不增加候选 token、临时许可或持久配对记录。工具入口沿用记忆写权限和当轮 opt-out。

先按精确端点、关系类型和方向查询已有关系：如果该关系已提交、其原始 owner 与当前关系行仍 exact-join、`SUPERSEDES` 的 target 仍体现 `SUPERSEDED` 历史效果，则返回关系的 `ALREADY_PRESENT`，不要求两个端点此刻仍 ACTIVE，也不覆盖原来源；若历史关系与生命周期不一致，报告完整性冲突而非伪造成功。只有**新建**关系才执行以下 ACTIVE 校验。`CONTRADICTS` 的两个端点同 domain、同精确 context、同 kind，均为 ACTIVE；双方保持 ACTIVE，无赢家，反向重复按同一无向关系处理。`SUPERSEDES` 有方向：source 为保留的新事实，target 为将退出活跃召回的旧事实；新关系的两端同 domain、同精确 context，均为 ACTIVE。same-kind replacement／taxonomy correction 由实际 kind 是否相同确定，不让模型传入矛盾模式。关系和 target 的 `SUPERSEDED` 更新与工具结果同事务。自关联、非法跨域／context、失效 ID、重复或并发漂移得到明确结果，不回滚先前已成功保存的 source 事实。

模型可在当前用户明确纠正且能定位旧记忆时标记取代；只看到相关性高或两条旧记忆方向不明时，应保持未标或询问用户。冲突和取代是模型的语义判断，reranker 分数不是授权。该工具只建立关系，不删除记忆；用户删除只能在记忆页。工具预检后的端点须在 canonical 写事务中按稳定顺序锁定、重验，重复关系与并发删除依上述先查既存关系、再判断新建的顺序结算。

会话处理过程中的工具卡片以“标记记忆关系”显示 `mark_memory_relation`，并根据实际结算结果简述取代／冲突关系是新建还是已存在；进行中、失败或未确认时不得显示成功文案。卡片摘要不展示记忆或关系 ID，原始工具结果的可读性仍遵循现有工具结果展示设置。

## 5. Canonical schema 与来源归属

以 PostgreSQL 的 `memory_facts`／`memory_relations` 为语义真源；保留既有 ACTIVE／SUPERSEDED 生命周期、四种 kind、同 context 的 active semantic unique index、search terms／document 及 embedding 附属数据。`memory_fact_semantic_digest` 继续用于现有 canonical 精确身份、唯一约束和 embedding join，不另建 DTO fingerprint。

clean-v0 保留 `memory_facts` 到**创建该事实的 canonical `remember` ToolResult**的 `(source_session_id, source_tool_result_id)`：这是写入 owner 与事务真实性，不是指向产生正文的原始观察 ToolResult，更不是模型摘要的事实核验。工具结果自身可追到 workspace、assistant entry、tool call，不在 fact 上重复存一套冗余来源字段。组合 FK／唯一约束保证创建结果存在、一条新事实只对应一个创建结果；它们**单独不能证明**该结果是成功的 `remember`，也不能证明 memory domain 或 context。现有窄约束触发器仍须核对已提交 ToolResult 的 `EXECUTED`／`SUCCESS`、其 tool-call block 的确切工具名、session 所属 memory domain，以及与 fact context 对应的 workspace；writer 同事务内以冻结的 typed `remember` 输入 exact-join 正文、kind、context 与 `BASED_ON` 依据。不得新增模型转写的原始证据 owner、摘要校验哈希或来源复制行。

从 clean-v0 删除 `memory_fact_tool_result_refs`、它的 FK／索引／约束及 fact→原始 ToolResult 的所有支持引用列或投影；不以可空旧字段、另一张 evidence 表、模型摘要哈希或兼容读取保留这条边。`BASED_ON` 仍使用 `memory_relations`，owner 必须是创建 source fact 的同一个成功 `remember` ToolResult；后补 `CONTRADICTS`／`SUPERSEDES` 的 owner 必须是相应成功 `mark_memory_relation` ToolResult。现有直接 `(owner_session_id, owner_tool_result_id)` FK 和约束触发器继续核对工具名、结果状态、两端 domain/context、`BASED_ON` source-fact owner 与 relation owner 一致，以及关系工具参数与实际端点／方向一致。writer 在同一事务执行相同的 typed 校验；不能只靠任意 ToolResult FK 冒充合法关系。保留当前关系端点 domain/context/kind、`SUPERSEDES` mode、无向冲突唯一性及删除所需索引，不保留 candidate 壳或第二套 relation 表。关系的 UI 文案由类型、端点及时间确定性生成，不存治理模型的 `decision_public_summary`。

`memory_explain` 和记忆页只从 fact 的**创建 `remember` 结果**和关系 owner 投影写入来源；不返回原始观察 ToolResult 的 ID、handle、`tool_result_citation_ids` 或“由工具验证”的结论。模型在会话中调用的 `memory_explain` 仍须同源 workspace 才能看到 session／turn／entry／ToolResult 详情；跨源返回 `CROSS_ORIGIN_REDACTED`，GLOBAL 可被召回不意味着模型能读取另一个 workspace 的原始对话。用户直接操作的记忆页则由本地服务按服务端确定的 memory domain 授权，与会话页当前选中的会话／workspace 无关；只要创建来源会话仍可打开，就可从该 domain 的记忆详情导航到保存时的对话，不用当前会话 workspace 决定来源链接的可见性。该链接不宣称定位或证明模型所参考的具体工具输出。记忆正文、kind、时间与关系仍按既有记忆可读范围显示。以上来源字段是产品解释与删除关系的必要数据，不是治理回执。

记忆详情应分开呈现创建 fact 的 `remember` 来源，以及每条 `BASED_ON`／`CONTRADICTS`／`SUPERSEDES` 关系的 owner（`remember` 保存时建立或 `mark_memory_relation` 后续标定）；不得显示已废弃的“保存时引用”“先前记忆结果／工具观察”支持列表，也不得把后补关系说成创建 fact 的独立来源。详情界面保留正文、类别／范围／时间；“在对话中查看”与“删除记忆”同处元信息下方靠右的操作行，不重复渲染正文中已有的适用条件或保存方式说明；关系区以当前事实为第一层节点、关联事实为下一层可点击节点，关系建立来源仍可单独查看。两个层级卡片之间统一使用向右箭头作为阅读引导，**不以箭头表达数据库方向或因果方向**；由当前节点到关联节点的文案分别为 `is based on`、`is the basis of`、`supersedes`、`is superseded by`、`contradicts`，冲突从任一端查看均为 `contradicts`。来源会话 `OPEN` 时可导航到相应写入处；已关闭时保留归属说明但不提供不可打开的定位链接。这只是现有 canonical 写入与关系 owner 的读投影，不新增 durable 来源副本或治理状态。

点击关联事实卡片应同时切换右侧详情与左侧目录选中项：使用该事实的 global／项目 context，类别归为“全部”，清空搜索，并按其 ACTIVE／SUPERSEDED 生命周期切到“正在使用”／“已被替代”。即使目标不在当前目录第一页，也须在当前筛选下显示并选中该事实，不能只更换右侧详情；用户主动更换筛选或关闭详情后取消这一临时定位。项目选择、搜索与状态选择保留键盘可见焦点，但不使用突兀的橙黄色焦点框。

## 6. 删除、恢复与记忆页

保留当前删除确认的可观察语义，而不是只删一行：删除 root 后沿**入边 `BASED_ON`**递归找全部依赖项；删除所有触及待删事实的关系；若某旧事实的所有 superseder 都在本次待删集合中，按现有 surviving supersede ancestry 规则考虑恢复。恢复若与仍 ACTIVE 的事实或彼此发生 exact semantic collision，预览要求用户继续选择一并删除的事实；没有解决前不执行。`CONTRADICTS` 不选择赢家。删除 superseder 后不再因旧的 16 条／7 KiB 回答偏好存储检查阻止恢复。

计划和执行继续在记忆管理 owner 中复用同一纯图算法、精确预览确认与事务内重验／锁定，防止并发 `remember`、关系标定或删除使确认漂移。删除执行先移除触及待删事实的关系和 embedding，再删事实、恢复合法旧事实；不再规划、锁定或删除 ToolResult 支持引用。过去的 ToolResult／会话历史不回写也不删除。原 candidate normalization、candidate_deletes 和相关 FK 冲突处理完全移除；幸存 fact 失去指向已删端点的关系后仍是同一个 fact，其创建来源不变。UI 仍展示级联删除、关系移除、恢复项、冲突与额外选择。删除预览使用独立、内部滚动的模态弹窗，不拉长记忆详情页；先展示所选事实和删除／恢复／关系变化数量，再分别列出连带删除及恢复项，关系变化可展开核对。若预览未就绪，明确禁止确认删除，并在弹窗内说明冲突及额外选择、重新计算的步骤；重算期间或失败时保留弹窗，但不得继续使用旧预览确认删除。并发漂移返回的新预览仍在同一弹窗中展示。取消或关闭不执行删除。

记忆页继续提供“全局记忆”／“项目记忆”范围、类别、生命周期筛选、分页、详情、允许展示的来源、关系及删除预览。项目选择列表由本 memory domain 中**当前仍存在**的项目 context 正式 fact 派生，ACTIVE 和 SUPERSEDED 均计入；只有会话、没有项目记忆的目录不出现，删除最后一条项目记忆后目录退出列表。项目元数据来自对应的 canonical project session。列表与详情读取均不接受会话页当前会话作为筛选依据；删除后刷新列表，若所选项目已无记忆则清空选择。普通仍为 `OPEN` 的 Web 会话下，有记忆的项目自然属于会话项目集合；但 CLI 可以正常关闭会话并留下正式项目记忆，而当前 Web 会话列表排除 `CLOSED` 会话，因此“记忆项目始终是会话页可见项目的子集”尚非现有代码可保证的不变量。记忆页不能仅为凑这个子集而隐藏仍存在的项目记忆；若产品要让关闭的会话也出现在会话页，应另定会话页合同。首版用户 GUI 不新增手动关系标定；会话页不加记忆管理控件。可按正式 fact 时间显示最近保存项，不添加 durable 通知。用户删除不能撤回先前发给模型的记忆内容。

## 7. 召回、偏好投影与 embedding

普通 `memory_search`／`memory_get` 的 sparse、dense、rerank、canonical refetch、精确可见性与 truthful fallback 保留；关系候选只复用这套检索 owner 的阶段，不复制一套治理专用排序器。`remember` 一提交，稀疏索引与精确 ID 读取即可使用；embedding 未就绪只降低 dense 完整性，不改变写入成功状态。

取消回答偏好**存储总容量拒绝**：同 context 超过 16 条或完整 JSON 超过 7 KiB 仍可写入，删除恢复也不做此项阻断。单次 `MEMORY_RESPONSE_PREFERENCE_HEAD` 仍需有界。首版用查询独立的稳定选择：每个可读 context 中，先排除与任何仍 ACTIVE 的同 context 偏好有显式 `CONTRADICTS` 关系的双方，再按 `accepted_at DESC, id DESC` 取最新前缀，分别遵守现有每 context 最多 16 条／7 KiB 和整体 source 16 KiB 的投影资源边界；current-project 与 global 各自选择，不以 project 内容无声挤掉 global。用 SQL `EXISTS`／有界多取一条判定是否还有未入选偏好，不读取或序列化无限完整集合；若因条数、字节或显式冲突而省略，投影明示 `selection_incomplete`／`conflicts_omitted`，不把缺席说成不存在。若库读取失败则用现有 `UNAVAILABLE` absence，真正无偏好才是 `EXPLICIT_EMPTY`。未标定的语义冲突仍可能同时入选，模型按 advisory 数据处理。此规则只选择一次 provider 输入，不是新入库上限或长期记忆淘汰策略。

将 governor 当前附带的 embedding 扫描／写入原地搬到独立的 Host-local `MemoryEmbeddingMaintainer`（具体类名可随代码风格调整）：维护范围沿用该 Host 可读的 global／当前 project contexts，Host 启动时唤醒，`SAVED` 提交后 best-effort 唤醒；单次维持现有批量、deadline 和资源边界。若本批确有写入进展且有下一页未嵌入 ACTIVE 事实，可继续排队一次 process-local 自唤醒；整批失败或无进展时不热循环，等待下一次写入／Host 启动等普通唤醒。重启后重新扫描未嵌入事实，不设全库或工作生命周期上限；崩溃丢失一次 wake 可以接受，不引入 durable job、lease 或完成回执。没有 embedding 服务时 sparse／精确读取仍可用，降级应可观察。

## 8. Provider 输入连续性与权限

记忆保存、关系标定、删除、embedding 补齐和偏好选择都不能修改已安装 epoch 的 `SYSTEM` 或 provider `tools` 字节，也不能改写已有 `messages`。新记忆及偏好快照只走既有后续 source snapshot／message append；冷 epoch 和明确采用的 compaction successor 才能重建 provider input root。新工具目录或系统说明只能进入合法新 epoch，不能因运行时能力变化偷换前缀。模型切换从 canonical rows 重新投影且遵守既有目标兼容及上下文预算边界。

现有同 turn `NoNewTriggerAnchor` 工具续跑沿用上一次激活快照，**不会天然重新冻结偏好 source**。因此，每次后续 provider dispatch 在继承既有 memory-use policy、权限及激活快照的前提下，做一次第 7 节所述的有界 canonical 偏好查询，并以完整 typed source 值与已安装的最新 `MEMORY_RESPONSE_PREFERENCE_HEAD` 比较；现有 process-local source head 只有 presence／fingerprint，实施时须由 dispatch owner 保留上一份完整 typed 偏好 source 供比较，不建 fingerprint→object registry。只有变化时才经现有 `SNAPSHOT_ON_CHANGE`／invalidation reservation 追加新的偏好 source 后缀。查询覆盖本 Host 的 `remember`／关系标定、记忆页删除或恢复以及其他 Host 的同域写入，不依赖 process-local dirty 标记或跨 Host 通知；不增加 durable 事件／job。若变为真正空集，也须追加明确空态，不能继续沿用旧偏好。库读取失败则按既有 `UNAVAILABLE` 语义处理，不能把旧快照伪装为当前真源。该刷新不是新的 human activation，不重算 Skill／MCP／工具目录，不重新触发普通 memory recall，也不改写旧 source 或 message；旧消息中的偏好可能仍可见，新后缀应明确作为当前偏好快照而非声称已抹除历史。读被当轮禁用则仍输出现有明确空态。若下一次没有 provider dispatch，就无需额外事件；Host 崩溃后冷启动从 canonical rows 读取最新偏好。其他 kind 保存后仍可同 turn 精确／稀疏读取，但不承诺自动注入。

代码 hard-cut 时，若恢复中的旧 epoch 仍装着 `remember(kind_hint)` 描述符，新工具实现不得把旧参数偷偷解释为新 `kind`，也不得就地替换已安装 provider tool。该调用以真实的不可用／参数不匹配结果结束，不产生记忆副作用；合法新冷 epoch 或已采用 compaction successor 安装新描述符后才能使用新工具。这是切断旧路径，不是保留兼容 shim。

ROOT-only mutation、当轮 memory opt-out、permission mode、记忆依据的同 domain/context 可见性，以及模型工具同源／用户记忆页同 domain 的写入来源披露边界，分别在工具、repository 与 UI 的合法入口验证。删除原始 ToolResult 引用不改变普通工具执行权限，也不让工具输出成为记忆写入权威。记忆始终以低权威数据进入模型，不可覆盖当前用户指令、权限、安全要求或实时项目真源；`FACT` 回退也不改变这些规则。

## 9. Hard-cut 施工顺序与删除清单

1. 在 clean-v0 schema 保留 fact 创建 `remember` 结果和 relation owner 的精确归属，删除 `memory_fact_tool_result_refs` 及所有原始 ToolResult 支持引用的 FK／索引／读写投影；`memory_explain`、记忆页详情和图删除只读写 fact 与关系 owner，不能留空壳 evidence 表或兼容字段。
2. 在现有 canonical ToolResult writer transaction 内结算 `remember` 和 `mark_memory_relation`，由事务产生动态最终正文，按历史 canonical ToolResult exact-confirm 模糊 ACK，并把成功 live 正文改为提交／确认后可发布；保留原 writer guard、取消和副作用确认，不另开写事务。
3. `remember` 只接受正文、目标 context、必选最终 `kind` 和可选正式记忆依据；保留检索 pipeline 的最多三条有覆盖说明的相关旧记忆与 ROOT-only 关系工具。删除 `cited_tool_result_handles` 参数、输入解析／校验、`PreparedRememberWrite` 的 handle／reference 字段、工具结果 evidence kind／visibility 的记忆引用分支，以及工具描述中“复制 citation_handle”的提示；额外工具入参不得被旧形状兼容接受。
4. 将回答偏好投影切到有界选择，移除治理和删除中的存储容量拒绝；在同 turn 工具续跑接入窄 memory-source refresh；拆出 Host-local embedding 维护 owner 和正确的启动／提交后 wake。
5. 从 provider ToolResult 逻辑消息中删除 `citation_handle` 字段，删除为它生成／注入／回放的 `memory_citation_handles`、process-local handle 表、注册／resolve DTO 和相关 compiler／lowering 路径；`FrozenModelVisibleMemoryProvenance` 及只为引用 handle 汇聚 ID 的 model-call 字段一并删除。普通 ToolResult 内容、真实 ToolResult ID、返回的 `memory_id`，以及 ToolResult 中为完整／压缩输出呈现真实记忆 ID 的 `model_visible_memory_fact_ids`／`model_visible_memory_ids` 保留，但这些 ID 绝不用于签发引用权威。调用时的 memory-use policy 必须保留为窄 typed 值，不得因拆除 citation owner 而丢失。删除旧校验和测试断言，改测新 wire 形状；不允许保留恒为 `null` 的字段、接受旧输入的 parser 或双形状回放。按现有边界在冷 epoch／已采用 compaction successor 装入新工具面，不就地改写旧消息。
6. 删除 governor、治理 prompt／auxiliary model binding、candidate claim／PROCESSING／SKIP／ABANDON、terminal governance wake、candidate-only DTO／SQL／测试及 UI 待审文案。更新 tool description、系统记忆指导、schema 基线、记忆页与活跃文档。模型可见说明尽量用业务语言而非 canonical row、ToolResult owner、epoch 等内部术语，讲清即时保存、精确重复、工具观察由模型转写 FACT、真正不同的后续结论才可引用既有记忆、删除依据会级联、最多三条相关结果只供判断、后补冲突／取代与用户在记忆页删除的区别。工具说明给一组短的正反例：会议在周二的 FACT 与因而决定周一准备材料的 DECISION 可以建立依据；把“会议在周二”换种说法再存一条，即使扩大范围，也不能以 `BASED_ON` 连接。例子必须明确是演示，不是待存内容。不要保留旧新双路径、兼容别名或空壳候选。

实施时记录事件／subject／guard／relation／job oracle 的前后差异：本功能不申请新的 durable event、subject slot、append guard 或 durable job；`memory_relations` 是既有产品关系的重接，不是新增证明图。若实现确需增加这些类别，必须先修订本规格并说明独立产品理由、owner、事务与故障路径。

本轮目标 oracle：committed event、subject slot、append guard、live event 和 durable job 均不增加；产品 relation 在现行直接写入基线的 28 张中再减去 `memory_fact_tool_result_refs` 一张，目标为 27 张，具体以重建 clean-v0 后的仓库 oracle 核对。`memory_relations` 本身未新增，继续由 canonical ToolResult 直接归属。删去 process-local handle owner 不应新增 durable 证明机制。

## 10. 验收与 dogfood

- 工具：必填四选一 `kind`，省略／`AUTO` 拒绝；ROOT-only 和 memory opt-out 生效。`SAVED`／`ALREADY_PRESENT` 与 canonical ToolResult 精确一致；重复调用不改来源和 `BASED_ON`。并发写入撞上 active semantic unique index 时只返回唯一赢家；预检后 `BASED_ON` 被删除／取代时，writer 拒绝失效引用，不因精确重复而绕过。失败、取消和模糊 ACK 不产生半成品或虚假成功 live 输出；已提交后用户删掉 fact，历史 ToolResult 仍可按原调用 exact-confirm，不重复写入。
- 工具观察转写：真实工具结果可启发模型用 `remember(kind="FACT")` 写一条有主体、scope、时间与必要不确定性的自然语言观察摘要；新 FACT 不保存原始结果 ID／handle，也不声称经过工具自动核验。只有语义不同且确有依赖的第二条记忆才用 `based_on_memory_ids`，不得为每个工具调用、同义改写或单纯扩大检索范围机械建记忆／关系。删除该 FACT 时，真正依赖它的后续记忆按现有 `BASED_ON` 图级联删除，原始会话 ToolResult 不受影响。`remember` 的旧 handle 入参明确拒绝；provider ToolResult wrapper 不再发行 `citation_handle`，模型在同一 turn 后续使用记忆 ID 仍可建立合法 `BASED_ON`。
- 检索：每次 `remember` 尽力提供零至三条同精确 context 的 ACTIVE 旧记忆，包含回答偏好且排除重复赢家；sparse-only、embedding／reranker 故障、部分覆盖和过期目标均有真实结果，不阻碍正式保存，也不把零命中解释成无冲突。
- 关系：`BASED_ON` 只随新事实同事务建立；后补 `CONTRADICTS` 保留两者 ACTIVE，`SUPERSEDES` 同事务让 target 退出活跃召回。重复提交已存在 `SUPERSEDES` 时，即使 target 已是 `SUPERSEDED` 仍返回 `ALREADY_PRESENT`，但历史效果不一致要报告冲突；反向冲突重复、非法 context／kind／状态、自关联、并发删除与迟到标记有确定结果。关系失败不撤销先前 `SAVED`。
- 管理：依据级联、删除取代项后的恢复、surviving ancestry、active／恢复间精确冲突及额外选择与既有产品行为一致；不再有 candidate normalize、支持 ToolResult 引用或偏好存储容量阻断。模型的 `memory_explain` 仅投影 fact 创建结果与关系 owner，来源同源可读、跨源遮蔽，不返回 `tool_result_citation_ids`；用户记忆页按 domain 读写入来源和关系 owner，不显示“保存时引用”工具结果列表，切换当前会话不改变项目列表或来源链接。只有当前仍有 ACTIVE／SUPERSEDED fact 的项目出现在选择器中，删掉最后一条后消失；会话页无记忆管理控件，记忆页能完成浏览与删除预览。
- 投影与维护：超过旧 16 条／7 KiB 后仍可写偏好；每次 source 有界、稳定、明确不完整，并在冲突另一端未入选时仍不把入选端当成无冲突。同 turn `NoNewTriggerAnchor` 的每次续跑都做有界 canonical 偏好查询；本 Host 保存／标定、记忆页删除／恢复、其他 Host 写入所造成的变化，均只追加已变化的偏好 source，不新建 activation、重跑普通召回或改写前缀；删除到空集、库不可用也有真实的后缀状态。Host 启动与新写入可补 embedding；有进展且仍有积压时可 process-local 续扫，无进展不热循环；服务故障时 sparse／精确读取可用。
- 来源与连续性：任意成功 ToolResult、错误工具名、错误 workspace／context 都不能伪装 fact／relation owner；`remember` 返回的新 ID 和三条候选 ID 只是记忆 ID，不成为原始工具观察的证据。不存在可被模型拿来引用的真实 ToolResult handle、持久支持引用或旧 wire 中的可空 `citation_handle`。旧 epoch 的 `kind_hint`／`cited_tool_result_handles` 工具形状不能被兼容解析或就地换 tools。正常同 epoch 多轮、steer、冷启动、已采用 compaction、模型切换下，`SYSTEM`／`tools` 字节不变且旧 messages 不重写。
- 实测：实际模型 dogfood 覆盖读取真实工具结果后由模型转写项目 FACT、在确有不同结论时另存一条 `BASED_ON` 记忆、三条相关结果、关系标定、重启后读取、记忆页删除级联及偏好投影降级；核对 provider ToolResult 不含 `citation_handle`，记忆详情不伪称原始工具来源。使用已有已保存生产配置，按 `AGENTS.md` 保护凭据且只重置核验过的本地 disposable 数据库。

实际验收（2026-09-21）：重建 clean-v0 后核对产品 relation 为 27 张，`memory_fact_tool_result_refs` 不存在；经核验的本地生产 PostgreSQL 库也已按授权重置并重建，旧会话数为 0。Python 全量回归 2011 项通过，前端 368 项通过；另有最终提示词与 provider ToolResult wire 形状的定向 28 项通过，TypeScript、Ruff 和 diff 检查通过。使用已保存的 DeepSeek Chat 连接及最终提示词，实际模型从文件工具结果转写项目 FACT，再以其真实 `memory_id` 保存语义不同的 DECISION 和 `BASED_ON`，未建立同义依据；三条相关记忆、后补 `SUPERSEDES`、重启后的召回，以及记忆页删除依据时的级联预览与执行均通过。模型还识别同义内容跨范围保存后仍有语义重叠，但未据此误建依赖或冲突。运行中的 App 已冷重启并返回 HTTP 200。测试用的独立数据库在脚本结束后删除，未把真实工具输出引用恢复进事实或模型 wire。另一条已保存的 gpt-5.5 Responses 连接曾完整通过同一业务链路，但后续两次复测在任何记忆工具调用前返回 `transport_protocol_error`（thinking 完成内容与流增量不一致）；这是单独的 provider 流协议诊断，不计入最终提示词的成功验收。若未来发现不能闭合的产品边界，应先修订本规格，不能暗留兼容路径。
