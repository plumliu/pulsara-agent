# Pulsara 记忆一级页面与级联删除 Hard-cut 实施规范

状态：实施前权威规范

适用仓库：pulsara_agent

强制前置：
`PULSARA_MEMORY_GOVERNANCE_TERMINAL_CLAIM_SOURCE_SEMANTICS_AND_PROMPT_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`
与
`PULSARA_MEMORY_TAXONOMY_AND_SCOPE_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`
必须已经完整实施、验证并通过审查。本规范只从两项前置完成后的 clean-v0 基线继续；不得在管理
页面或删除实现中重新定义 candidate、governance、来源、四类 taxonomy、context、公开形成摘要或
relation 语义。发生冲突时，后者对 taxonomy、context、structured shape、recorded_at 与 reflector
减法的定义优先。

本规范的权威顺序：

1. 当前用户要求；
2. 仓库根目录 AGENTS.md；
3. 上述两项前置规范对 candidate/governance/source/taxonomy/context/relation 语义的契约；
4. 本规范对管理读取、产品投影和删除语义的契约；
5. 两份规范引用的当前生产代码；
6. archived_docs 下的历史设计仅作背景，不能覆盖以上契约。

本规范冻结“记忆”一级页面、完整管理读取、关系产品投影和用户发起的物理删除语义。实施必须是一次 clean-v0 hard cut：允许重置已核验的本地可丢弃 PostgreSQL 数据库，只修改当前基线，不提供旧库迁移、双读写、兼容别名、feature flag、软删除过渡或修复任务。

---

## 1. 目标结果

Pulsara 增加一个与“会话”“能力”同级的“记忆”页面。它提供两个 placement 主视角：

- “跨对话”：展示当前 memory_domain_id 下 `ctx:global` 的四类记忆；GLOBAL 只表示跨对话可读，
  不表示正文在所有情境都适用，也不表示内容都“关于用户”；
- “项目”：从曾经以目录方式打开过的 project workspace 中选择一个，只展示该目录的 exact
  project context。四类均可出现，同目录的所有会话看到同一份项目记忆。

“关于你”保留为 USER_PROFILE 的类别名称和筛选视图，不能再充当 global placement 的同义词。

快速开始目录属于 transient workspace，不具备项目记忆作用域，绝不出现在项目选择器中。

页面默认展示 ACTIVE 记忆，产品文案为“正在使用”；用户可以切换到 SUPERSEDED 历史，产品文案为“已更新”。页面提供搜索、类别筛选、keyset 分页、页面内详情面板和用户确认后的删除。

删除由用户直接授权，不能交给模型工具或模型决策。删除采用物理 hard delete：

- 删除 memory_facts 中被确认的事实；
- 按本规范计算 BASED_ON 依赖闭包；
- 删除所有相关 relation、candidate 内容和引用；
- 由 memory_embeddings 的既有 ON DELETE CASCADE 删除派生向量；
- 精确恢复失去 superseder 且仍可安全激活的旧事实；
- 绝不删除原始 transcript、assistant block、tool result、turn 或 session；
- 不新增 DELETED lifecycle、tombstone、删除事件、删除 job、删除 checkpoint、计划表或第二套图。

---

## 2. 当前代码真源

实施前必须重新完整阅读下列文件；符号发生移动时按当前等价真源追踪：

- AGENTS.md
- PULSARA_MEMORY_GOVERNANCE_TERMINAL_CLAIM_SOURCE_SEMANTICS_AND_PROMPT_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
- PULSARA_MEMORY_TAXONOMY_AND_SCOPE_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
- src/pulsara_agent/memory/scope.py
- src/pulsara_agent/conversation_kernel/memory/contracts.py
- src/pulsara_agent/conversation_kernel/memory/recall.py
- src/pulsara_agent/conversation_kernel/memory/dispatch.py
- src/pulsara_agent/conversation_kernel/memory_tools.py
- src/pulsara_agent/conversation_kernel/memory/governor.py
- src/pulsara_agent/conversation_kernel/_repository/memory.py
- src/pulsara_agent/conversation_kernel/host.py
- src/pulsara_agent/storage/postgres_connection_provider.py
- src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql
- src/pulsara_agent/storage/migrations/manifest.py
- src/pulsara_agent/storage/migrations/grants.py
- src/pulsara_agent/storage/migrations/resources/0000_conversation_kernel_expected_catalog_v1.json
- src/pulsara_agent/storage/migrations/resources/0000_conversation_kernel_runtime_grants_v1.json
- src/pulsara_agent/web_app/application.py
- src/pulsara_agent/web_app/session_controller.py
- src/pulsara_agent/web_app/http_server.py
- frontend/lib/pulsara-types.ts
- frontend/lib/runtime-adapter.ts
- frontend/components/activity-rail.tsx
- frontend/app/pulsara-app.tsx
- frontend/app/globals.css 及其拆分样式
- tests/test_round8_advisory_memory.py
- tests/test_stage2_conversation_kernel_postgres.py
- tests/test_stage5_clean_migration.py
- tests/test_local_web_http_surface.py
- frontend/lib/runtime-adapter.test.ts
- frontend/app/pulsara-app.test.tsx

### 2.1 已确认的数据真相

当前记忆类别为：

- USER_PROFILE
- RESPONSE_PREFERENCE
- FACT
- DECISION

当前关系为：

- BASED_ON
- SUPERSEDES
- CONTRADICTS

当前 lifecycle 只有 ACTIVE 和 SUPERSEDED。

CONTRADICTS 在产品语义上无向。memory_relation_id 对端点进行无序规范化，数据库也用 least / greatest 唯一索引防止反向重复。数据库行仍保留 source_fact_id 和 target_fact_id，但这只是关系存储形状，不是 UI 主语。

四类都可位于 global 或 exact current-project context，不存在 kind/context 合法性矩阵。项目视角必须
允许 USER_PROFILE；“关于你”筛选只表示 kind，不表示 placement。

项目 Host 的 readable contexts 恰为 `ctx:global` 加 exact project context；transient Host 只有
`ctx:global`。Project context 来自 canonical project path 的稳定 identity。同一目录的 session 共享
workspace_id 和 exact memory context。

当前 LocalSessionController.list_sessions 明确使用 include_closed=False，因此不能作为“曾经打开的项目”选择器真源。项目目录表必须由 kernel 独立读取所有 OPEN 和 CLOSED canonical sessions，并按 workspace_id 聚合。

### 2.2 已确认的数据库真相

当前 clean-v0 包含：

- memory_candidates
- memory_candidate_tool_result_refs
- memory_candidate_basis_refs
- memory_facts
- memory_relations
- memory_embeddings

当前没有记忆 event、job、checkpoint 或 management registry。本功能不得增加它们。

关键约束：

- memory_candidates.accepted_fact_id 与 memory_facts.source_candidate_id 形成双向、deferred、当前为 ON DELETE RESTRICT 的 accepted pair；
- candidate 的 related_target_fact_id、duplicate_winner_fact_id 和 applied_existing_fact_id 都 FK 到 memory_facts；
- basis ref 的 target FK 到 memory_facts；
- relation 的 decision candidate、source fact、target fact 都是 RESTRICT；
- 只有 memory_embeddings.fact_id 使用 ON DELETE CASCADE；
- ACTIVE semantic 在同 domain、context_id、fact_semantic_digest 下唯一；
- lineage constraint trigger 要求 accepted candidate 与 fact exact join；
- APPLIED_TO_EXISTING candidate 必须且只能拥有一条 SUPERSEDES 或 CONTRADICTS relation；
- 非 accepting candidate 不得拥有 canonical fact 或 relation。

因此，直接执行 DELETE FROM memory_facts 不是合法实现；它既会被 FK 阻止，也无法表达 dependent cascade、candidate 归一化和 superseded target 恢复。

### 2.3 已确认的写入与召回真相

当前治理写入在一个事务中：

- 锁住候选；
- 锁住 relation target 和 basis facts；
- 插入 accepted fact；
- 插入 relation；
- 对 SUPERSEDES target 执行 ACTIVE 到 SUPERSEDED；
- 最后结算 candidate。

exact-semantic duplicate source 可以通过 APPLIED_TO_EXISTING candidate 给既有 source 增加一条 supersede 或 contradiction。一个既有 source 因而可以通过多个不同 APPLIED_TO_EXISTING candidate supersede 多个 target。

当前 PostgresMemoryQuery 是面向模型的 bounded recall：

- search 最多返回 bounded top-k；
- direct_relations 最大 100；
- response preference snapshot 有独立的物理上界；
- ordinary recall 只选择 ACTIVE；
- get 可以读取 ACTIVE 或 SUPERSEDED；
- provenance 按 exact workspace fence 决定是否公开 locator。

这个 API 不能被记忆管理页面复用为“完整目录”。管理目录和详情必须是独立查询面，不能把 bounded model recall 的截断结果包装成完整清单。

回答偏好只在新的 ROOT_HUMAN_PROMPT 边界冻结。已开始的 turn 可能已经冻结旧 memory source；删除不能回写或重建已安装 provider input prefix。提交删除后，下一个符合条件的 ROOT human prompt 必须重新采集并通过现有 append-only source invalidation 观察新真相。

---

## 3. 不可妥协的 hard-cut 边界

1. 不新增模型可调用的删除工具。
2. 不让模型批准、排序或选择删除结果。
3. 不新增 soft-delete 列、DELETED lifecycle 或 tombstone。
4. 不新增 memory deletion event、job、outbox、receipt、checkpoint、generation、plan registry 或 replay registry。
5. 不增加第二套 memory graph；memory_facts 和 memory_relations 仍是唯一 canonical semantic truth。
6. 不用数据库宽泛 CASCADE 替代产品语义。embedding 的既有 CASCADE 保留，其他删除由一个显式事务 executor 完成。
7. 不新增 quote fingerprint、plan fingerprint 或 fingerprint-to-plan map。完整 frozen typed plan 本身就是比较对象。
8. 不复用 PostgresMemoryQuery.search、direct_relations 或 memory tools 作为管理目录。
9. 不调用 embedding、rerank、provider token count 或任意 provider API。
10. 不按 provider 名称增加任何分支。
11. 不删除或改写 canonical transcript、provider replay、tool result、turn、session、event。
12. 不因删除即时重建当前 epoch 的 provider input root。
13. 不设图深度、事实总数、关系总数或重试次数的任意 hard cap。
14. 目录和详情采用分页；删除 planning 是有数据库事务/HTTP 边界的局部操作，可以有一个局部 deadline，但超时必须零变更。
15. 数据库只修改 clean-v0 baseline 和对应资源；不新增 0001，不兼容旧 schema，不双路径运行。实施验证时重置已核验的本地可丢弃数据库。
16. deletion confirmation 不得受 http_server 当前 8 MiB client_max_size 形成的 aggregate inventory cap；使用逐 record 有界、总 record 数无固定上限的流式 transport。
17. 不借记忆删除顺手强化全库关系模型。本任务不修补 relation 缓存 fact_kind 的一般性一致性缺口，不新增 incoming superseder 唯一索引，也不新增或扩大 lifecycle/lineage constraint trigger；这些若成为独立产品或完整性需求，另立规范并单独证明。

---

## 4. 产品信息架构

### 4.1 一级入口

AppView 增加 memory。ActivityRail 在“会话”和“能力”附近增加记忆入口，使用与现有线性 icon、尺寸、stroke 和 hover 语言一致的图标。

一级页面使用 MemoryView，不嵌入会话 inspector，也不借用会话右栏。

### 4.2 两个 context 主视角

“跨对话”：

- context_id 固定 `ctx:global`；
- 表示可跨当前 owner 的对话读取，不承诺 universal applicability；
- 可显示四类，不需要项目选择器；
- 不自行放宽当前 provenance workspace fence。

“项目”：

- 必须先选择一个由后端 project catalog 返回的 workspace_id；
- context_id 精确等于所选 workspace 的 canonical project context；
- 可显示四类，包括 project-context USER_PROFILE；
- 同目录所有会话共享；
- 目录当前不存在、session 已 CLOSED，仍可出现在选择器；
- transient / quick workspace 永远不出现。

项目选择器按最近打开时间降序、workspace_id 稳定打破平局。显示 workspace_label 和 workspace_root；若同一 workspace_id 有多条 session，使用 updated_at 最新行的 label/root。

### 4.3 搜索、筛选和生命周期

筛选项：

- 全部
- 事实
- 关于你
- 回答偏好
- 决策

“关于你”只对应 USER_PROFILE 类别，类别 chip 的 aria-label 使用“类别：关于你”。global 与 project
两个视角都显示该 chip；不得把它解释成旧 USER scope，也不得因 project placement 隐藏。

默认 lifecycle 是 ACTIVE，标签“正在使用”。切换项“已更新”对应 SUPERSEDED。

搜索仅在所选 exact context、lifecycle、kind 内执行本地 PostgreSQL 文本过滤。它不是语义召回：

- 不生成 embedding；
- 不调用 rerank；
- 不做 context relaxation；
- 不按 provider 行为变化；
- 不修改 model recall 排序；
- 结果继续按 updated_at DESC、id DESC 做 keyset 分页。

每个物理页面大小为 1 至 100；默认 40。100 是单次 JSON 序列化和前端渲染边界，不是库存总量上限。只要 next_cursor 存在，客户端可以继续读取。

### 4.4 行展示

每行展示：

- statement 正文，列表中可视觉截断但可访问文本保留；
- 产品类别；
- 可读位置：“跨对话”或项目 label；
- recorded_at，并可另显示 updated_at；
- lifecycle/status；
- 使用提示。

状态与使用提示优先级：

1. lifecycle 为 SUPERSEDED：显示“已更新”；
2. lifecycle 为 ACTIVE 且存在另一 ACTIVE companion 的 CONTRADICTS：显示“需要确认”；
3. ACTIVE RESPONSE_PREFERENCE：显示“通常随新一轮提供”；
4. 其他 ACTIVE：显示“相关时使用”。

ACTIVE RESPONSE_PREFERENCE 一旦处于活动 contradiction，补充说明“冲突解决前暂不作为回答偏好使用”，与 freeze_response_preference_source 当前排除冲突端点的行为一致。

不能把 SUPERSEDED 文案写成“已删除”；历史仍然存在，直到用户确认物理删除。

### 4.5 空状态

- global context 无事实：“还没有跨对话记忆”；
- USER_PROFILE 筛选无事实：“还没有关于你的记忆”；
- 尚无 project workspace：“从一个目录开始会话后，项目记忆会显示在这里”；
- 项目无事实：“这个项目还没有共享记忆”；
- 筛选无结果：“没有符合当前筛选的记忆”；
- 管理读取不可用：显示可重试错误，不能把错误伪装成空清单。

---

## 5. 详情面板与关系相对投影

点击一行后，在 MemoryView 内部打开 detail aside；窄屏使用同一信息结构的 drawer。不能打开会话 inspector。

详情包含：

- 完整 statement；
- 类别、context 产品文案、recorded_at、lifecycle、updated_at；
- 使用状态；
- 完整直接关系，按 keyset 分页；
- 可公开的形成方式；
- decision_public_summary 的产品化整理摘要；
- provenance 契约允许且前端确实能定位时，显示“在对话中查看”；
- 删除入口。

不存在的编辑、修改、合并按钮不渲染；不能显示 disabled placeholder。

列表、详情、关系 companion 和删除 confirmation 都把 cohesive memory 当作完整 record，只展示最终
自然 statement，不拆 clause，不提供 partial edit/delete，也不渲染 `What:` / `Why:` 等 5W2H 字段或
标签。转写后的 statement 不能伪装成用户逐字原话；来源契约允许时，“在对话中查看”仍指向真实来源。
管理面不得重写前置规范的简单做法/Skill authority、task/goal/calendar/commitment 宽松归类或
coding-source precedence。

### 5.1 关系必须以当前详情事实为主语

管理 API 不把 raw source/target 方向交给前端自行猜测。它返回 selected_fact_id、companion fact 和 relative_role。产品投影如下：

| 数据库关系 | 当前详情是 source | 当前详情是 target |
| --- | --- | --- |
| BASED_ON | “依据 companion” | “被 companion 作为依据” |
| SUPERSEDES | “更新了 companion” | “已由 companion 更新” |
| CONTRADICTS | “与 companion 存在冲突” | “与 companion 存在冲突” |

例：数据库行为 a CONTRADICTS b：

- 打开 a，companion 是 b，显示“与 b 存在冲突”；
- 打开 b，companion 是 a，显示“与 a 存在冲突”。

CONTRADICTS 的 relative_role 只有 CONFLICTS_WITH，不存在 SOURCE_CONFLICT 或 TARGET_CONFLICT。数据库 least/greatest、source/target 和 relation id 构造方向都不能泄漏为产品语义。

建议 typed projection：

    MemoryManagementRelationProjection(
        relation_id,
        relative_role,  # BASED_ON / BASIS_FOR / UPDATES / UPDATED_BY / CONFLICTS_WITH
        companion,
        public_summary,
        accepted_at,
    )

前端只翻译 relative_role，不接收 decision_kind、reason_code、supersede_mode 或 raw context ID 作为直接文案。
relation projection 中的 `public_summary` 若存在，也只是前置规范冻结的 target-independent formation
summary；“更新了 / 已由…更新 / 与…存在冲突”只能来自 relative_role + companion，不能从 summary
猜 relation。

### 5.2 关系完整性

详情关系查询以 relation_kind、accepted_at、id 的稳定 keyset 排序。每页最多 100，但没有总关系数上限。next_cursor 为 null 才能宣称“已加载全部”。

当前 PostgresMemoryQuery.direct_relations 的 LIMIT 100 仍只服务模型工具；MemoryView 不得调用它，也不能在第 100 条后静默停止。

### 5.3 来源可见性

形成原因只使用现有公开字段和 provenance contract：

- MAIN_AGENT_REMEMBER：产品文案“在对话中记住”；
- decision_public_summary：作为“整理摘要”展示；
- raw decision kind、reason code 不显示。当前唯一 producer 是 Main Agent 的 `remember` tool call；
  `MEMORY_WRITE_HINT` 不是 candidate source，不能产生另一种形成方式。

项目视角可以用所选 workspace 形成 exact read binding。只有 provenance disposition 为 SAME_ORIGIN，且 session/turn/entry locator 完整且仍可由前端打开时，才显示“在对话中查看”。

global 视角没有可据以放宽 fence 的虚构 host workspace。跨 origin locator 继续隐藏；只显示允许公开的
形成方式与摘要。后续若要 global 公开跨项目 locator，必须另写权限/来源契约，本任务不推断该例外。

---

## 6. 独立的管理读取面

新增 provider-neutral memory management contracts 和 service。推荐位置：

- src/pulsara_agent/conversation_kernel/memory/management.py：frozen DTO、产品投影纯函数和 management query 协调；
- src/pulsara_agent/conversation_kernel/_repository/memory.py：canonical SQL、事务 planner/executor；
- KernelHostCore 暴露少量 async facade；
- web controller 只调用 core，不直接持有 SQL。

### 6.1 Project catalog

project catalog 从 pulsara_v3.sessions 读取：

- memory_domain_id 必须由当前 WebApplication 服务器侧设置；
- workspace_kind = 'project'；
- 包含 OPEN 和 CLOSED；
- 按 workspace_id 聚合；
- 取每组 updated_at 最新 session 的 workspace_root 和 workspace_label；
- transient 永久排除；
- keyset 为 last_opened_at DESC、workspace_id DESC；
- 不复用 /api/sessions 的 include_closed=False 结果。

目录行不存在并不删除 project identity，也不隐藏已有 memory。

### 6.2 Catalog query

输入：

- server-owned memory_domain_id；
- exact `ctx:global` 或 exact catalog project context_id；
- lifecycle；
- 可选 fact_kind；
- 可选 normalized local search text；
- page_size；
- typed keyset cursor。

输出：

- 当前页事实；
- ACTIVE contradiction badge 所需的存在性投影；
- project label projection；
- next_cursor。

事实页与其 conflict badge 必须来自一个 REPEATABLE READ cut。badge 只在 selected fact 和 companion 都为 ACTIVE 时成立。

cursor 是 base64url 编码的完整 typed keyset 值与精确筛选条件，不是 fingerprint，也不映射到 registry。服务端必须验证 cursor 的 context、lifecycle、kind、search 与本次请求完全相同；不匹配返回 400。

“完整”表示不存在静默 top-k 截断，用户可以沿 next_cursor 遍历静态库存；它不承诺跨多个 HTTP 请求保存一个 durable snapshot。并发 mutation 可能令后续页减少，客户端在自身或 409 可见 mutation 后从第一页重新读取。不得为了跨请求 snapshot 增加 registry、receipt 或 checkpoint。

### 6.3 Detail query

detail 以 server-owned domain、exact context_id 和 fact id 三者查找，不得只凭全局 fact id。

事实本体、source candidate 的公开 provenance、第一页 relation projection 和 active conflict status 来自一个 REPEATABLE READ cut。后续 relation page 继续 exact fact/context fence，并使用 typed keyset cursor。

管理读取失败不能 fallback 到 model recall，也不能用旧缓存伪装成功。

---

## 7. 删除关系代数

记 C 为最终物理删除的 fact 集合。

### 7.1 BASED_ON

a BASED_ON b 表示 b 是 a 形成、理解或选择时值得保留的重要依据、背景、动机或依赖。它不要求
形式逻辑上的必要条件；普通但有意义的 rationale 也可以建立关系。这里沿用前置规范已经选择的
共同删除命运，不反向把 governance 门槛收紧为严格依赖证明。

- 删除 a：删除 a 和该 relation；b 不受影响。
- 删除 b：a 必须加入 C；随后继续处理所有依赖 a 的事实。

闭包定义：

1. C 从用户选择的 root fact 和用户明确选择的一并删除 fact 开始；
2. 若存在 source BASED_ON target，且 target 在 C，则 source 加入 C；
3. 重复到最小不动点。

实现必须 cycle-safe，可以使用 recursive CTE 的 UNION 去重或等价集合算法。禁止 MAX_DEPTH、MAX_HOPS、MAX_PREFIX_TRIALS 一类任意截止。菱形依赖只删除每个 fact 一次。

context 后果对四类 source 完全相同：

- global source 只能依赖 global target；
- project source 可以依赖 global target或同一 exact project target；
- 因此删除一个 global basis 可能级联到多个项目中的任意四类 dependent；
- 删除一个 project basis 不会跨到其他项目。

确认 UI 必须按“所有项目”及各项目 label 分组展示完整 cascade，不能只显示数量。

### 7.2 SUPERSEDES

a SUPERSEDES b 表示 a 更新了 b，b 为 SUPERSEDED。

- 删除 a：删除 relation；若 b 不在 C 且没有其他合法 incoming superseder，b 成为 restoration candidate。
- 删除 b：删除 relation；a 不加入 C，继续存活。
- 若 a、b 都在 C：删除 relation，不恢复。

链例：

- a supersedes b，b supersedes c；
- 删除 a 后，b 恢复 ACTIVE，b 到 c 的 relation 保留，c 仍为 SUPERSEDED；
- 删除 b 后，a 保留，a 到 b 被移除；b 到 c 被移除，c 进入恢复判断。

一个既有 source 可以通过多个 APPLIED_TO_EXISTING candidates supersede 多个 target。删除 source 时，每个存活 target 都独立进入恢复集合；不能只恢复第一条。

当前生产 writer 通过 target row lock 和 ACTIVE lifecycle 检查阻止第二个 incoming superseder，但删除 executor 不把“最多一个”当成数据库结构前提。它必须读取并计数每个 target 的全部存活 incoming SUPERSEDES；只有删除后计数为零时，该 target 才进入恢复判断。

### 7.3 CONTRADICTS

a CONTRADICTS b 是无向冲突：

- 删除任一端：删除 relation；
- 另一端不加入 C；
- 另一端保持原 lifecycle；
- 如果它不再有其他 ACTIVE contradiction，移除“需要确认”状态。

删除不能把任一端自动判定为赢家，也不产生新的 relation。

### 7.4 混合图

闭包传播只沿“target 被删时反向追 incoming BASED_ON source”。SUPERSEDES 和 CONTRADICTS 不传播删除。

完成 BASED_ON 闭包后：

- 删除 C 的所有 incident relations；
- 对 incident SUPERSEDES 的存活 target 做恢复规划；
- 对 incident SUPERSEDES / CONTRADICTS 的存活 source 做 candidate cleanup 或归一化；
- 如果用户把某个不可恢复旧事实加入“一并删除”，以它为新 seed 重新计算整个 BASED_ON 闭包和恢复集合。

不能在旧闭包上局部打补丁。

---

## 8. 恢复 admission 与用户显式消歧

失去 superseder 不等于必然可执行恢复。恢复 SUPERSEDED 到 ACTIVE 前必须按最终事务状态做 hard admission。

### 8.1 Active semantic 唯一性

对每个 restoration candidate，检查删除 C 后保留的 ACTIVE facts 和同批拟恢复 facts：

- key 为 memory_domain_id、context_id、fact_semantic_digest；
- 一个 key 最终最多一个 ACTIVE；
- 如果与现存 ACTIVE winner 冲突，该旧事实不能恢复；
- 如果多个旧事实互相冲突，后端不能选择 winner。

### 8.2 回答偏好容量

恢复 RESPONSE_PREFERENCE 必须复用治理写入的同一套：

- 生产代码中的 per-context active count bound（当前为 16）；
- 生产代码中的 per-context canonical projection byte bound（当前为 7 KiB）；
- memory_response_preference_item_payload；
- canonical_json_bytes；
- exact per-context advisory transaction lock。

不能复制常量、近似字节数或另写 codec。执行最终检查时，对每个 context 计算：

- 删除 C 后留下的 ACTIVE preferences；
- 用户选择保留并拟恢复的 preferences；
- exact final canonical projection。

count 或 bytes 任一超界，计划不可执行。

### 8.3 不伪造 winner

preview 有两种状态：

- READY：所有存活 restoration candidates 可以同时通过 exact hard admission；
- NEEDS_RESOLUTION：至少一个 restoration candidate 与当前 ACTIVE 真相、同批恢复或 preference capacity 冲突。

NEEDS_RESOLUTION 不签发 execution authority。UI 显示：

- 哪些旧记忆原本会恢复；
- 为什么不能同时恢复，使用产品文案；
- 用户可以勾选哪些旧事实“一并永久删除”，再请求一次 preview。

additional_delete_fact_ids 是用户的显式删除授权，不是模型选择。它们成为新 seeds，服务器从头重算完整 plan。对同一 semantic collision group，用户可以全部删除或明确保留至多一个；对 preference capacity group，用户可以保留任意能通过 exact count/bytes admission 的子集。后端不提供隐藏默认 winner，不按时间、id、context 或模型分数自行获胜。

如果用户不选择足以消除冲突的 additional deletions，preview 保持 NEEDS_RESOLUTION，删除按钮不签发。

当前 ACTIVE facts 不会被删除流程自动牺牲来给旧事实腾位。用户若想删它们，应另行从其详情发起删除；不能把本 root 的隐式冲突解决扩大成未确认删除。

### 8.4 计划收敛

每次追加 deletion seed 都可能：

- 新增 BASED_ON dependent；
- 移除新的 outgoing SUPERSEDES；
- 暴露新的 restoration candidates。

因此每次 preview 都从 seed set 重新求最小不动点并重新 admission。无图深度或轮数 cap；有限 canonical 行集合保证集合单调扩张时终止。若局部 planning deadline 到期，返回 typed timeout，零 mutation，用户可重试。

---

## 9. Candidate、引用和来源清理

物理删除不能只处理 facts/relations。memory_candidates 也存 statement 和治理内容，必须按所有权清理。

### 9.1 必须删除的 candidates

以下 candidate 连同其 tool result refs 和 basis refs 一起删除：

1. source fact 位于 C 的 ACCEPTED candidate；
2. relation 被删除且 candidate 为 APPLIED_TO_EXISTING 的 owner；
3. duplicate_winner_fact_id 指向 C 的 SKIPPED candidate；
4. SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT 且任一 endpoint 位于 C 的 candidate；
5. applied_existing_fact_id 指向 C 的 candidate；
6. PENDING 或 PROCESSING candidate 的 basis ref 指向 C；
7. 任何其他通过 related_target_fact_id、duplicate_winner_fact_id、applied_existing_fact_id 或 candidate basis FK 引用 C，且不能按下一节合法归一化的 candidate。

如果 governor 已在进程内持有某个随后被删除的 PROCESSING candidate，后续 settlement 必须因 canonical candidate missing / target drift 而安全停止，不得重建候选或事实。

### 9.2 存活 accepted source candidate 的归一化

若 relation 因 target 被删除而移除，但 source fact 存活：

- relation owner 是 source fact 自己的 ACCEPTED candidate：
  - ACCEPT_AND_SUPERSEDE 或 ACCEPT_AND_CONTRADICT 改为 ACCEPT；
  - related_target_fact_id 清为 null；
  - decision_public_summary 必须保持原 exact 非空值；前置 governance hard cut 已保证它只描述
    target-independent source formation，不编码已删除的 relation/target；
  - status、final_kind、accepted_fact_id、accepted_fact_at 保持；
  - candidate 继续 exact join source fact。
- relation owner 是 APPLIED_TO_EXISTING candidate：
  - 删除该 candidate 及 refs；
  - source fact 的原始 accepted candidate 不变。

BASED_ON target 被删时 source 已进入 C，因此不存在“移除 basis 但保留 dependent memory”的归一化分支。

### 9.3 可以保留的 opaque occurrence

其他 candidate 的 model_visible_memory_fact_ids 数组或旧 tool result 文本可能含已删除 fact id 的历史 occurrence。它们不持有 canonical memory content 或执行 authority，不为本功能重写。

删除语义是移除 memory materialization、candidate 内容 owner、关系和搜索/embedding 投影，不是遗忘原始对话。详情确认必须明确提示：

“这会删除记忆及依赖关系，不会删除原始对话或工具结果。”

---

## 10. Internal plan 与 exact typed user confirmation

repository preview 产生完整 FrozenMemoryDeletionPlan；它是一次 planning call 内的 process-local value，不跨请求保存在服务器。

建议 internal DTO：

    FrozenMemoryDeletionPlan
      root_fact
      explicit_additional_roots
      facts_to_delete
      relations_to_delete
      candidates_to_delete
      candidate_refs_to_delete
      candidates_to_normalize
      facts_to_restore
      surviving_relation_endpoint_effects
      disposition
      restoration_conflicts

internal expectation 携带事务执行所需的完整 canonical 值：

- fact：id、context_id、kind、lifecycle、fact_semantic_digest、recorded_at、updated_at；
- relation：id、decision_candidate_id、kind、source/target exact identity、supersede_mode、ordinal、accepted_at；
- candidate action：id、status、decision_kind、decision_reason_code、decision_public_summary、related/duplicate/applied ids、accepted_fact_id、processing_started_at、decided_at；
- candidate refs：candidate_id、ordinal 及 exact basis target 或 tool-result identity；
- restore：expected SUPERSEDED identity 与 planned ACTIVE；
- normalize：expected old decision fields 与 exact new decision fields。

浏览器不需要批准 candidate cleanup 等不可见数据库动作。management service 从 internal plan 纯投影出 FrozenMemoryDeletionConfirmation，作为用户确认边界：

    FrozenMemoryDeletionConfirmation
      root_fact
      explicit_additional_roots
      facts_to_delete_with_product_text
      relative_relation_effects
      facts_to_restore
      restoration_conflicts
      disposition

confirmation 中每个 fact/relation/restore 仍携带 id、exact context_id、lifecycle、kind、fact_semantic_digest、recorded_at、updated_at 和关系端点等防漂移值；它不是只含数量的摘要。candidate status、candidate refs 和内部 reason code 不跨产品边界。

不可变 statement 不需要为了真实性再造 fingerprint；fact_semantic_digest 是现有 canonical semantic boundary，可作为完整 fact expectation 的一个真实字段，但不能代替 lifecycle、relations 或 ownership exact compare。

internal plan 与 user confirmation 都有 canonical sequence 顺序：

- facts 以 memory_domain_id、id；
- relations 以 id；
- restorations 以 context_id、accepted_at、id；
- internal candidates 以 id、refs 以 candidate_id/ordinal。

executor 不信任客户端序列来决定删除动作，而是：

1. 从 server-owned domain、root id、explicit additional roots fresh 规划 internal plan；
2. 在锁内再规划得到 transaction-owned exact internal plan；
3. 从该 internal plan 纯投影新的 FrozenMemoryDeletionConfirmation；
4. 与客户端确认的完整 canonical confirmation sequence 做 exact equality；
5. 只有 equality 且 disposition READY 才执行 transaction-owned internal plan。

会改变用户可见删除闭包、关系效果、恢复集合、lifecycle、updated_at 或 restoration admission 的漂移都返回 409 MEMORY_DELETION_PLAN_DRIFTED，并附新的 preview。仅 PENDING 到 PROCESSING 而最终 internal cleanup 和用户可见 impact 完全相同，不需要伪造一个用户不可见的 drift；executor 仍必须使用锁内当前 candidate 真相，并通过既有 deferred constraints 与本规范的 affected-subgraph final validation。

禁止把 plan/confirmation 序列化后哈希成 quote fingerprint，也禁止服务端 registry。旧 confirmation 不得被拓宽、修补或继续执行。

### 10.1 无固定库存上限的 transport materialization

src/pulsara_agent/web_app/http_server.py 当前 Application 使用 client_max_size = 8 MiB。普通 JSON API 保留该物理边界，但 deletion preview/execute 不能把它变成 fact/relation 数量的隐式上限。

preview request、preview response、confirmed DELETE request 和大结果采用 application/x-ndjson 的 canonical typed record stream：

- HEADER：view、project、root、disposition；
- ADDITIONAL_ROOT：每个用户 seed 一条；
- FACT_DELETE、RELATION_EFFECT、FACT_RESTORE、RESTORATION_CONFLICT：每个产品 expectation 一条；
- END：exact record counts 和流终止。

一条 record 的最大字节数由现有 memory statement、context、时间和 ID 字段上界机械推导；这是单
canonical row 的物理协议边界。record 总数和总 body bytes 没有固定 cap。

这两个 request handler 禁止调用 request.json() 或 request.read()。它们直接消费 request.content 的 chunk stream，用增量 NDJSON parser 校验，并写入 operation-local TemporaryFile/SpooledTemporaryFile。临时文件：

- 只是一请求内 transport buffer，不是 semantic authority；
- 没有 plan id，不能跨请求 lookup；
- 在 DB transaction 开始前完整接收并校验；
- close/异常时只删除一次；
- 不提交数据库、不写仓库、不形成 checkpoint。

完成 END 校验后才打开 repository transaction，避免慢上传占用 row lock。executor 从临时文件按 canonical 顺序流式读取 expected confirmation，与 fresh plan 的 confirmation iterator 逐条 exact compare。

handler 必须通过已验证的 route-specific raw-stream path 绕过 aiohttp Application 的 8 MiB aggregate read check；不能简单把全局 client_max_size 调大或设为 unlimited。transport 使用 connect/write/read-idle watchdog，不增加 total upload lifetime cap。单 record 非法返回 400；本地临时存储耗尽是 typed physical-resource failure、零 DB mutation。

preview 在只读 planning 完成后先释放 DB transaction/borrow，再从 operation-local buffer 流式响应，不能因慢浏览器长期占用 repeatable-read snapshot。

---

## 11. PostgreSQL clean-v0 hard cut

### 11.1 不新增表或列

product relation 集合保持不变。不得增加：

- memory_deletions
- memory_deletion_plans
- memory_tombstones
- memory_heads
- memory_generations
- memory_management_events
- memory_repair_jobs

### 11.2 accepted pair 的窄化 FK 调整

当前 accepted candidate 和 fact 的双向 FK 使用 ON DELETE RESTRICT，即使 constraint 声明 deferred，也不适合在一个事务内删除互相引用的 pair。

executor 的固定合法顺序是先删除 candidate，再删除 fact。因此 clean-v0 只修改阻止第二步的这一条 FK：

- memory_facts(source_candidate_id, id) 到 memory_candidates(id, accepted_fact_id)：从 ON DELETE RESTRICT 改为 ON DELETE NO ACTION，继续 DEFERRABLE INITIALLY DEFERRED。

反向 FK 保持原样：

- memory_candidates(id, accepted_fact_id) 到 memory_facts(source_candidate_id, id)：继续 ON DELETE RESTRICT、DEFERRABLE INITIALLY DEFERRED。

candidate 先删除后，反向引用行已经不存在，不会阻止随后删除 fact；保留反向 RESTRICT 还会拒绝错误的 fact-first 顺序。修改后的 source FK 在事务结束时检查“二者都已删除”，仍然拒绝 candidate 单边删除。不要把任一方向改成 CASCADE，也不要把第二条 FK 一并改成 NO ACTION。

其他 candidate target、basis、relation FK 继续 RESTRICT；executor 必须显式清理或归一化。这样意外遗漏在数据库层失败，而不是偷偷扩大删除。

### 11.3 不新增数据库完整性强化

本功能不新增 UNIQUE、partial unique index、composite FK、CHECK、constraint trigger 或约束函数，也不扩大现有 trigger 的触发表或事件范围。

memory_relations 的缓存 source_fact_kind / target_fact_kind 没有通过 composite FK 与端点 fact_kind exact join，这是当前 schema 的一般性完整性缺口，不是删除功能成立所必需的变化。本任务明确不顺手修补；若未来要求数据库独立防御任意绕过 repository 的错误写入，应作为单独 schema-integrity hard cut 评估现有数据、写入路径和完整测试面。

当前生产 writer 已在 `_lock_governance_target` 中对 target 执行 FOR UPDATE，并要求其 lifecycle 为 ACTIVE，足以让正常 writer 串行化同一 target 的 supersede。删除 executor 仍不假设 incoming SUPERSEDES 唯一：它对全部存活 incoming rows 做 exact count，只有计数为零才规划恢复。

ACTIVE semantic 的既有 partial unique index 继续保持；它负责 restoration 后真正需要的最终 ACTIVE 冲突防线，不扩展为其他图不变量。

### 11.4 executor-local affected-subgraph final validation

删除事务不新增全局 lifecycle 或 lineage trigger。executor 在 commit 前只对本次受影响子图做一次 exact final validation：

- closure C 中的 facts 全部不存在；
- candidates_to_delete、其 tool/basis refs 和所有应删除的 incident relations 全部不存在；
- 每个归一化后存活的 accepted candidate 都是 decision_kind = ACCEPT、related_target_fact_id = NULL、
  decision_public_summary 与归一化前 exact byte-identical 且非空，并仍 exact join 自己的 accepted fact；
- 对每个因删除 incident SUPERSEDES 而受影响、且自身仍存活的 target，重新计算全部存活 incoming SUPERSEDES 并做穷尽分区：计数为零时，它必须在 facts_to_restore 中且最终为 ACTIVE；计数大于零时，它必须不在 facts_to_restore 中且最终为 SUPERSEDED；
- 被删 facts 没有残留 embedding；
- response preference 的最终 count、canonical bytes 和既有 ACTIVE semantic uniqueness 都仍通过最终 admission。

validation 必须从数据库最终事务视图重新查询，不以先前 plan、删除行数或 Python 对象别名代替。任何不一致都 rollback 并作为实现错误或已枚举的并发重规划结果返回；禁止在 validation 中自动修复。

它不得扫描或断言全库的 ACTIVE/SUPERSEDED 等价关系，也不得把历史上与本次操作无关的异常变成本次删除 blocker。现有 lineage triggers、CHECK、FK 和 unique constraints 原样保留并照常执行。

### 11.5 Runtime grants

当前 runtime role 对多数 memory tables 没有 DELETE。hard cut 更新：

- manifest.py 的 CONVERSATION_KERNEL_RUNTIME_PRIVILEGES；
- 0000_conversation_kernel_runtime_grants_v1.json；
- grants/verifier 生成结果；
- expected catalog fingerprint/resource。

最小新增 DELETE 权限：

- memory_candidates
- memory_candidate_tool_result_refs
- memory_candidate_basis_refs
- memory_facts
- memory_relations

memory_embeddings 已有 DELETE，且 fact FK 的既有 CASCADE 保留。不能给 runtime role 增加 session、transcript、tool result 或 agent event 的 DELETE。

### 11.6 资源与重置

只编辑 0000_conversation_kernel_baseline.sql，不创建增量 migration。使用仓库已有 clean catalog/grant 生成与验证流程更新：

- baseline SQL checksum；
- migration universe identity；
- expected catalog fingerprint；
- runtime grants resource。

在测试前解析并核验本地 DSN 确实是 disposable local test/development database，然后 reset 到全新 v0。禁止对未核验或远端数据库 reset。

### 11.7 精确数据库 delta 与最小性证明

以下 delta 的比较起点必须是前置 governance hard cut 已完成、且已经包含
`INSUFFICIENT_SOURCE_SUPPORT` CHECK value 的 clean-v0 catalog。不得拿前置实施前的 catalog
作为本规范的直接 before snapshot，也不得把前置规范的 reason vocabulary 变化误算为 deletion
功能新增的第四类数据库变化。

从该前置基线起，本任务允许的数据库差异封闭为三类：

| 类别 | 精确变化 | 必要性 |
|---|---|---|
| FK action | 仅 `memory_facts(source_candidate_id, id) -> memory_candidates(id, accepted_fact_id)` 从 `ON DELETE RESTRICT` 改为 deferred `ON DELETE NO ACTION` | 原来的双向 RESTRICT 不存在合法的 accepted pair 删除顺序；该单向变化允许 candidate-first，同事务结束仍要求 pair 不得单边残留 |
| runtime privilege | 仅给 runtime role 增加 `memory_candidates`、两张 candidate ref 表、`memory_facts`、`memory_relations` 的 DELETE | executor 必须显式执行这些表的删除；`memory_embeddings` 已有 DELETE，其他产品表不在授权范围 |
| 派生资源 | 按现有 generator 更新 baseline checksum、migration universe identity、catalog/grants resources 中确实由前两项引起的值 | 这是现有 clean-v0 verifier 的派生证据，不是新机制或额外约束 |

除此之外，表、列、enum、index、UNIQUE、CHECK、FK 列集合、trigger、function、role 和未列出的既有 grant 都不得变化。实现前后用 catalog/schema diff 证明此封闭集合；若 generator 显示额外差异，先视为越界或工具漂移调查，不能把它直接纳入本功能。

---

## 12. Planner 与 executor 事务算法

### 12.1 Preview

preview 是只读、可重试操作：

1. 绑定 server-owned memory_domain_id；
2. 验证 root 和 additional roots 属于该 domain；
3. 在 REPEATABLE READ 或 SERIALIZABLE cut 中读取 seeds；
4. 求 incoming BASED_ON 最小闭包 C；
5. 读取 C 的所有 incident relations；
6. 计算 candidate delete/normalize actions；
7. 计算存活 SUPERSEDES targets 的 restoration candidates；
8. 读取最终 admission 所需的 exact active semantic winners 和 preference scopes；
9. 构造 READY 或 NEEDS_RESOLUTION internal plan，并把 product confirmation canonical records 写入 operation-local transport buffer；
10. rollback/结束只读事务并释放 DB borrow；只由 response writer 线性接管临时 buffer，不保留 registry entry。

preview 不能占用 summary lane、Host writer guard 或 session lease。它使用现有 MEMORY_MAINTENANCE/只读适配 lane 的局部 borrow，结束即释放。

### 12.2 Execute

execute 使用 SERIALIZABLE transaction 和 MEMORY_MAINTENANCE lane：

1. 创建一个 operation-local monotonic deadline；
2. 绑定 server-owned domain；
3. 重新规划；
4. 将 root 包含在 closure/restoration/admission fact 集中，按 memory_domain_id、fact id 的全局稳定顺序一次锁取；不能先锁任意 root 再逆序锁更小 id；
5. 重新求闭包，若同一 snapshot 中发现新增行，则扩展集合并从头按全局顺序重新取得一轮 transaction；不能在已持有较大 id 时逆序补锁；
6. 重复 fresh transaction/replan，直到 locked set 与 plan 一致；
7. 按 id 锁 incident relations；
8. 按 id 锁受影响 candidates 和 refs；
9. 在锁内再次重新规划；
10. 从锁内 internal plan 投影 canonical confirmation iterator，与客户端 streamed confirmation 逐条 exact equality；
11. 不是 READY 或发生 drift：rollback，返回 typed 409；
12. 为所有受影响 response-preference scopes 按稳定 key 获取现有 advisory transaction locks；
13. 在锁内再次做 exact semantic 与 preference capacity admission；
14. 删除 candidate tool refs；
15. 删除 candidate basis refs；
16. 删除 incident relations；
17. 归一化存活 accepted candidates；
18. 删除所有 candidates_to_delete，包括 APPLIED、duplicate、processing 以及 C 中 facts 的 accepted source candidates；fact 到 source candidate 的 deferred NO ACTION FK 允许按 candidate-first 顺序让 accepted pair 在同一事务共同消失；
19. 删除 C facts；此时所有 candidate target/basis 引用都已清除，embedding 由既有 FK cascade；
20. 将 approved restoration facts 更新为 ACTIVE、updated_at 为同一 operation timestamp；
21. 执行 executor-local affected-subgraph final validation，并强制既有 deferred constraints 在 commit 前检查；
22. commit；
23. 返回 deleted/restored/normalized 的产品 projection。

实现可以在满足 FK 顺序的前提下调整 14 至 20 的局部顺序，但必须先移除所有指向 C 的 RESTRICT 引用，再删除 C facts；最终状态、锁序和单事务边界不得改变。任何异常 rollback 全部操作。

### 12.3 并发治理

现有 governor 通常先锁 candidate 再锁 target/basis；删除需要锁 facts 和多个 candidates，存在真实 deadlock 可能。

处理原则：

- 不添加持久 lease、graph generation 或 deletion mutex row；
- PostgreSQL SERIALIZABLE、row FK、既有 ACTIVE semantic unique index 和 deterministic ordering 是真相；
- deadlock_detected、serialization_failure、lock timeout，以及明确命中本规范 memory FK/active-semantic unique constraint 的并发引用冲突，在同一个 operation deadline 内从头重试；
- 只有已枚举的 memory constraint name 可以归类为并发重规划；其他 ForeignKeyViolation / UniqueViolation 是实现错误，不能无限吞掉；
- 不设固定 retry count；
- 每次 retry 都重新 capture/replan，不复用旧 measurement；
- deadline 到期返回 typed timeout，保证零 mutation；
- 不给 provider stream、turn、task 或 worker 增加 wall-clock lifetime cap。

若并发 governance 先提交：

- 新 BASED_ON dependent 必须进入 fresh closure；
- 新 intake 的 candidate basis ref 即使在旧 SERIALIZABLE snapshot 中不可见，也必须由 fact FK 阻止旧计划删除；下一事务 fresh replan 后返回 409；
- 新 supersede/contradict relation 必须进入 fresh candidate action；
- 新 internal plan 的用户 confirmation projection 与客户端不等则 409。

若删除先提交：

- governor 对 missing candidate、missing target 或 reference drift 安全终止；
- 不重插被删 candidate/fact；
- 不把本地 prepared object 当 canonical authority。

embedding worker 对已删除 fact 的 upsert 由 exact fact FK/refetch 拒绝或返回 false，不能复活 fact。

### 12.4 所有权

- HTTP request stream 只表达用户 intent 和 confirmed typed product confirmation；
- Web controller 不持有 SQL transaction；
- kernel management service 持有一次 provider borrow；
- repository planner/executor 持有一次 transaction；
- internal plan 没有跨请求服务器 owner；
- transport TemporaryFile、connection、borrow、cursor 均有唯一 owner，并在 success/error/cancel 后释放一次；
- 不 alias HostWriterGuard，不借用任意 live session 作为 USER 删除 authority。

---

## 13. Web API

建议单一路由集合：

### 13.1 GET /api/memories/projects

查询：

- page_size
- cursor

返回 project catalog page。domain 只来自 WebApplication 当前 workspace_input.memory_domain_id。

### 13.2 GET /api/memories

查询：

- view = global | project
- project_id，仅 project 必填
- lifecycle = active | updated
- kind，可选产品值
- search，可选
- page_size
- cursor

后端将产品值映射到 server-owned exact context_id；客户端不能提交 raw context_id。project_id 必须 exact join 当前 project catalog 中 workspace_kind='project' 的行。

### 13.3 GET /api/memories/{fact_id}

查询参数与 catalog 的 view/project fence 相同，另有：

- relation_page_size
- relation_cursor

返回 detail 和一页 relative relations。

### 13.4 POST /api/memories/{fact_id}/deletion-preview

request content-type 为 application/x-ndjson。第一条 HEADER 携 view/project/root，随后每个 additional_delete_fact_id 使用一条 ADDITIONAL_ROOT，最后必须有 END。不得把所有 additional roots 拼回一个受 8 MiB 限制的 JSON array。

response 以 NDJSON 流返回 READY 或 NEEDS_RESOLUTION 的 FrozenMemoryDeletionConfirmation product records。runtime adapter 增量 decode，MemoryView 可以逐步虚拟化展示，但只有 END 校验完成后该 confirmation 才可确认。additional roots 可以跨项目，因为 global-basis dependency cascade 本身可能跨项目；每一个 ID 必须属于 server-owned domain，并在 impact 中完整显示。

### 13.5 DELETE /api/memories/{fact_id}

request content-type 为 application/x-ndjson：

- HEADER 携 view/project/root；
- ADDITIONAL_ROOT 逐条表达用户额外删除授权；
- 其余 records 是 preview 已完整显示并确认的 canonical FrozenMemoryDeletionConfirmation；
- END 结束。

响应：

- 200：提交后的 deletion result；结果较大时同样使用 NDJSON stream；
- 400：非法 filter/cursor/typed plan；
- 404：domain/context fence 内不存在 root；
- 409 MEMORY_DELETION_PLAN_DRIFTED：未执行，携 fresh preview；
- 409 MEMORY_DELETION_NEEDS_RESOLUTION：未执行；
- 408 MEMORY_DELETION_CONFIRMATION_READ_IDLE：stream 未完整到达，未执行；
- 504 MEMORY_DELETION_PLANNING_TIMEOUT：未执行；
- 507 MEMORY_DELETION_TRANSPORT_STORAGE_EXHAUSTED：operation-local buffer 不可用，未执行；
- 503：数据库管理面不可用。

DELETE 缺失完整 confirmed confirmation sequence 或 END 时不能执行，不能把一次旧 preview 当长期 authority。http_server 必须证明超过 8 MiB aggregate 的合法 stream 能到达 executor；全局 client_max_size 不能先返回 413。

### 13.6 安全与文案

- 不接受 memory_domain_id；
- 不回传 raw SQL、candidate internal status 或 reason code；
- 所有 confirmation impact、error 和 detail 都使用产品 DTO 与产品文案；
- 不把 404 与 empty 混淆；
- 不记录 PULSARA_API_KEY；
- memory statement 本身不是 secret redaction target，错误诊断可保留真实内容；
- 不新增 cookie/localStorage deletion authority；
- HTTP controller 只投影产品 DTO。

---

## 14. 前端交互

### 14.1 页面状态

MemoryView 独立维护：

- active context tab；
- project catalog/current project；
- search text；
- kind filter；
- lifecycle tab；
- catalog page/cursors；
- selected fact/detail；
- relation pages；
- deletion preview；
- deletion preview stream completion/record counts；
- additional deletion selections；
- request error/loading。

切换 context/project/lifecycle/filter/search 时：

- 取消或忽略旧 request 的结果；
- 清空不再匹配的 selected detail；
- 从第一页重新加载；
- 不把一个 context 的 cursor 用到另一个 context。

### 14.2 删除确认

详情中的“删除记忆”打开页面内确认层。至少显示：

- root 完整正文；
- 直接删除和 BASED_ON cascade 的完整事实清单；
- 按所有项目/项目名分组；
- 将移除的冲突/更新关系的产品影响；
- 将恢复为“正在使用”的旧事实；
- 不删除原始对话的说明。

NEEDS_RESOLUTION 时：

- 列出不可同时恢复的旧事实；
- 显示“已有相同内容正在使用”或“恢复后回答偏好将超出可用容量”等产品文案；
- 提供“一并永久删除”勾选；
- 勾选变化后重新 preview；
- 只有新 preview READY 才启用最终删除按钮。

最终按钮文案包含实际数量，例如“永久删除 4 条记忆”。不得用候选数、关系数等内部行数误导用户。

runtime adapter 必须用 ReadableStream 增量解码 preview NDJSON，并按 canonical record sequence 保存 FrozenMemoryDeletionConfirmation。可以在流到达时渐进展示/虚拟化 impact，但在 HEADER、全部 records、END count 三者未 exact join 前：

- confirmation 不可执行；
- 最终按钮禁用；
- cancel/error 必须丢弃未完成 sequence；
- 不得只保留前 N 条后仍允许删除。

最终 DELETE 用 request ReadableStream 逐条编码 confirmation，不先 JSON.stringify 为一个大字符串。浏览器/handler 的 read-idle watchdog 可以中止失活 transport，但不能添加 total stream lifetime cap。

点击后不做 optimistic hard delete。等待 200 后关闭 detail、清理 selection 并重新加载当前 catalog。409 时保留页面，展示“记忆已发生变化，请重新确认”及 fresh impact。

### 14.3 视觉

沿用现有 Pulsara 的浅灰边界、细线 icon、紧凑间距和 surface 层级。relation 使用轻量文字行，不画数据库有向图。CONTRADICTS 两端都用同一种中性“存在冲突”视觉，不用红色胜负方向。

删除按钮只在详情内出现；危险操作确认使用现有 destructive color token，但页面主体不因可删除而变成危险色。

### 14.4 可访问性

- context/lifecycle 使用 tablist；
- kind filter 使用可读 aria-label；
- relation 文案包含 companion statement 或可访问名称；
- aside/drawer 有 heading 和关闭按钮；
- 删除确认焦点被约束，Escape 在未提交时关闭；
- loading、error、success 使用 aria-live；
- 不仅用颜色表达 ACTIVE、UPDATED、CONFLICT。

---

## 15. Provider input 与运行时生效时点

删除事务 commit 即代表：

- canonical memory rows、relations、candidate content 和 embedding 已不存在；
- 后续 management read/recall 看不到被删事实；
- 恢复成功的事实变为 ACTIVE；
- 冲突 badge 按剩余关系重新投影。

但 commit 不代表：

- 取消已发出的 provider request；
- 改写当前 turn 已冻结的 memory source；
- 重建 SYSTEM、tools 或 messages prefix；
- 删除包含旧 memory 文本的 transcript/tool result；
- 重播当前 epoch。

下一次符合条件的 ROOT_HUMAN_PROMPT 必须通过 memory/dispatch.py 的现有采集边界 fresh freeze：

- deleted facts 不再进入 automatic recall；
- deleted/冲突变化的 response preference head 不再沿用；
- restored facts 可按正常规则进入；
- source identity 变化走现有 append-only invalidation，而非 prefix rebase。

active turn 与 idle session 的删除数据库语义完全相同。差异只在于 active turn 可能已经冻结旧 source；UI 可提示“正在进行的回复可能仍使用删除前的记忆，下一轮起生效”。

---

## 16. 文件级实施范围

允许按实际模块边界微调新文件名，但职责不可混淆。

### 16.1 生产代码

必须修改：

- src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql
- src/pulsara_agent/storage/migrations/manifest.py
- src/pulsara_agent/storage/migrations/resources/0000_conversation_kernel_expected_catalog_v1.json
- src/pulsara_agent/storage/migrations/resources/0000_conversation_kernel_runtime_grants_v1.json
- src/pulsara_agent/conversation_kernel/_repository/memory.py
- src/pulsara_agent/conversation_kernel/host.py
- src/pulsara_agent/web_app/application.py
- src/pulsara_agent/web_app/http_server.py
- frontend/lib/pulsara-types.ts
- frontend/lib/runtime-adapter.ts
- frontend/components/activity-rail.tsx
- frontend/app/pulsara-app.tsx
- frontend/app/globals.css 或对应拆分样式入口

建议新增：

- src/pulsara_agent/conversation_kernel/memory/management.py
- src/pulsara_agent/web_app/memory_controller.py
- frontend/components/memory-view.tsx
- frontend/app/styles/memory.css

按 verifier 真源需要修改：

- src/pulsara_agent/storage/migrations/grants.py
- src/pulsara_agent/storage/migrations/contracts.py
- 相关资源生成脚本。

只有当测试证明下一轮 invalidation 现有实现存在缺口时，才修改：

- src/pulsara_agent/conversation_kernel/memory/dispatch.py
- src/pulsara_agent/conversation_kernel/memory_tools.py

不能为了 UI 改写 model recall。

### 16.2 测试

建议新增：

- tests/test_memory_management_contracts.py
- tests/test_memory_management_postgres.py
- tests/test_local_web_memory_management.py
- frontend/components/memory-view.test.tsx

并更新：

- tests/test_stage5_clean_migration.py
- tests/test_round8_advisory_memory.py
- tests/test_local_web_http_surface.py
- frontend/lib/runtime-adapter.test.ts
- frontend/app/pulsara-app.test.tsx

---

## 17. 严格实施顺序

0. 完成 governance 与 taxonomy/context subtraction 两项前置 hard cut 的全部 DoD、PostgreSQL 和
   real-provider semantic dogfood；确认唯一 clean-v0 baseline 已切换到四类/exact-context 完成态。
1. 冻结 management DTO、relative relation role、error/disposition；先写纯 contract tests。
2. 只修改 clean-v0 的 fact 到 source candidate FK action 与五张 memory 表的 runtime DELETE grants，并同步 catalog resources；先用 schema diff 证明没有新增 index、UNIQUE、composite FK、function 或 trigger。
3. reset 已核验的本地 disposable database，先证明 clean-v0 与现有 memory governance 全绿。
4. 实现 project catalog、memory catalog 和 paginated detail；证明不复用 bounded recall。
5. 实现纯 deletion closure/planning 投影；覆盖所有图代数和 restoration conflict。
6. 实现 SERIALIZABLE transactional executor、exact plan compare、candidate cleanup 和 retry-under-deadline。
7. 暴露 KernelHostCore facade；不得让 Web controller 直接查询数据库。
8. 实现 memory controller、五个 HTTP routes，以及 preview/DELETE 独立 NDJSON raw-stream admission；先用 >8 MiB integration test 证明没有 aggregate cap。
9. 实现 runtime adapter 的增量 stream typed decode/encode，拒绝 raw enum、truncated END 和 count 漂移。
10. 实现 ActivityRail memory 入口、MemoryView、detail aside/drawer。
11. 实现 deletion preview、resolution 和最终 confirmation。
12. 跑 focused tests，修复回归。
13. 跑完整 PostgreSQL、clean migration 和 frontend test/build。
14. 用真实本地浏览器完成页面 dogfood：global、project、history、双向 contradiction 文案、cascade preview、409 drift 和成功删除。

不得先做一个只读 UI 再长期保留兼容路径；本任务合入时必须是单一管理读取和单一删除执行路径。

---

## 18. 必测矩阵

### 18.1 Context 与目录

- global 只读 `ctx:global`；
- project 只读 selected exact project context；
- 两个视角均允许四类，GLOBAL 不承诺 universal applicability；
- 同目录多 session 聚合为一个 project；
- CLOSED-only project 仍出现；
- transient/quick 永不出现；
- project path 当前不存在仍出现；
- 不同 memory_domain 隔离；
- client 伪造 context/domain/project id 被拒绝。

### 18.2 Catalog/detail

- ACTIVE 默认与 SUPERSEDED history；
- 四类 kind 映射，ACTION_RULE 与兼容别名不存在；
- global/project 均显示 USER_PROFILE filter，“关于你”不等于 global placement；
- search 不跨 context/lifecycle；
- catalog/detail/relations 的 recorded_at 均来自 accepted_at canonical projection；
- updated_at/id keyset 无静态数据重复或遗漏；
- cursor/filter 不一致 400；
- 多于 100 条 catalog 可继续翻页；
- 多于 100 条 direct relations 可继续翻页；
- model recall 的 bounded 结果不影响管理完整性；
- 同一 page cut 的 conflict badge 与 facts 一致。

### 18.3 相对关系投影

- a BASED_ON b：a 显示“依据 b”，b 显示“被 a 作为依据”；
- a SUPERSEDES b：a 显示“更新了 b”，b 显示“已由 a 更新”；
- a CONTRADICTS b：a 显示“与 b 存在冲突”；
- 同一 CONTRADICTS 行打开 b：显示“与 a 存在冲突”；
- 反向插入/least-greatest 规范化不改变两端文案；
- raw source/target、decision_kind、reason_code 不渲染；
- active conflict 与 superseded status 优先级正确。

### 18.4 单边删除

- BASED_ON 删除 source：target 保留；
- BASED_ON 删除 target：source 级联删除；
- SUPERSEDES 删除 source：target 恢复；
- SUPERSEDES 删除 target：source 保留且 owner 正确归一化/删除；
- CONTRADICTS 删除任一端：另一端保留且 conflict badge 消失。

### 18.5 复杂图

- BASED_ON 长链；
- BASED_ON 菱形；
- BASED_ON cycle-safe 固定点；
- supersede chain 删除头/中/尾；
- 一个 existing source supersede 多 target；
- relation owner 为 source ACCEPTED candidate；
- relation owner 为 APPLIED_TO_EXISTING candidate；
- BASED_ON 与 supersede 混合图；
- BASED_ON 与 contradiction 混合图；
- 删除 global basis 可跨多个项目级联；
- 删除 project basis 不跨项目；
- 同一 fact 由多条路径到达只删除一次。

### 18.6 Candidate 与引用

- accepted fact/candidate pair 同事务共同删除；
- candidate 单边删除在 deferred source FK 检查时失败，fact 单边/先删 fact 被保留的反向 RESTRICT FK 拒绝；
- tool result refs 删除但 tool_results 保留；
- basis refs 删除；
- pending candidate 引用被删 basis 后删除；
- processing candidate 引用被删 basis 后删除；
- governor 持有 stale prepared candidate 不复活；
- skipped duplicate winner 指向被删 fact 时删除；
- skipped duplicate relation candidate cleanup；
- accepted relation candidate 归一化为 ACCEPT，target-independent formation summary exact 保留；
- APPLIED_TO_EXISTING candidate 随 relation 删除；
- unrelated model_visible_memory_fact_ids occurrence 不触发 transcript 重写；
- embedding 被 cascade 删除且 worker 不复活 fact。

### 18.7 恢复 admission

- 无冲突 restoration READY；
- 与现存 ACTIVE semantic collision；
- 多个 restoration candidates 互相 semantic collision；
- user additional deletion 解决 collision；
- 后端不按时间/id 选择 winner；
- response preference count 正好 16 通过、17 拒绝；
- exact canonical bytes 正好 7 KiB 通过、超 1 byte 拒绝；
- 多个 context 各自独立 admission；
- additional root 触发新 dependent 和新 restoration 后完整重算；
- NEEDS_RESOLUTION 不能 execute。

### 18.8 数据库约束

- clean-v0 schema diff 只出现 fact 到 source candidate 的 FK action 变化和授权/resource 变化，没有新增 index、UNIQUE、composite FK、function 或 trigger；
- fact 到 source candidate 的 deferred NO ACTION 允许 candidate-first accepted pair 同事务删除；
- 反向 candidate 到 fact 的 RESTRICT 保持，fact-first 和任一单边删除仍失败；
- 遗漏 candidate target、basis ref 或 relation owner 的清理仍被保留的 RESTRICT FK 拒绝；
- executor final validation 分别捕获遗漏 BASED_ON dependent、遗漏 candidate 归一化、incoming 已为零却遗漏 restoration、错误 restoration 分区和 orphan embedding，并使整个事务 rollback；
- 正常 writer 对同一 target 的并发 supersede 仍由 target row lock 与 ACTIVE lifecycle check 串行化；
- 即使测试夹具构造多个 incoming SUPERSEDES，executor 也逐条计数，并且只在全部 incoming 都将消失时规划恢复；
- relation delete 与 target restore、relation delete 与 target delete 的合法事务都可提交；
- runtime role 只获得所需 memory DELETE；
- runtime role 仍不能 DELETE transcript/session/tool result/event；
- clean-v0 expected catalog/grants/checksum 全部一致。

### 18.9 并发与漂移

- preview 后新增 incoming BASED_ON，execute 返回 409 fresh preview；
- preview 后新增 contradiction/supersede，409；
- preview 后 candidate PROCESSING 到 terminal 且产生新 canonical fact/relation，409；
- PENDING 到 PROCESSING 但用户 confirmation impact 完全相同，可使用锁内 fresh internal cleanup，不伪造用户不可见 drift；
- preview 后 lifecycle/updated_at 变化，409；
- concurrent governance 与 deletion 无 partial state；
- deadlock/serialization retry 在同一 deadline 内 fresh replan；
- deadline 到期零 mutation；
- concurrent embedding upsert 不留下 orphan embedding；
- 两个 delete request 同 root：最多一个提交，另一个 404/409；
- confirmed confirmation stream 不发生 alias/double execution。

### 18.10 Web 与前端

- 五个 routes 和 method/body validation；
- deletion preview/DELETE 使用 canonical NDJSON，不调用 request.json/read；
- 合法 confirmed confirmation stream 总量超过 8 MiB 仍到达 executor，不被 aiohttp client_max_size 预先 413；
- 超过 8 MiB 且发生 plan drift 返回业务 409，证明 transport 没有成为 inventory cap；
- 单 record 超出由字段契约推导的边界返回 400，已读临时文件释放；
- truncated/missing END、count mismatch、cancel、read-idle timeout 均零 DB mutation 且唯一关闭 transport owner；
- preview 释放 DB borrow 后再向慢客户端流式响应；
- runtime adapter 不把 streamed confirmation 聚合成单一 JSON request body；
- domain 不能由 client 提交；
- 400/404/408/409/503/504/507 映射为不同产品状态；
- AppView memory 可从 rail 与 command palette 到达；
- global/project tabs、selector、filters、history、pagination；
- detail 在页面内部，不打开 session inspector；
- 来源 link 只在 permitted locator 存在时渲染；
- 无 edit/disabled fake actions；
- cascade preview 按项目分组；
- resolution checkbox 重新 preview；
- 成功前不 optimistic 删除；
- 409 要求重新确认；
- keyboard/focus/aria；
- responsive drawer。

### 18.11 Provider prefix continuity

- active turn 已冻结 memory 后删除：已安装 provider input 不被改写；
- idle session 删除：下一个 ROOT prompt fresh capture；
- 下一轮 deleted response preference 不再出现；
- restored preference 在下一轮按正常规则出现；
- contradiction 删除后 warning/effective set 更新；
- SYSTEM/tools byte-identical，messages 只追加 suffix；
- 不增加 provider API 调用。

---

## 19. 验证命令与证据

迭代优先使用仓库根 .venv 和 uv 环境。实施者必须根据实际新增文件运行等价的完整命令，最终报告每条命令和结果。

最低 focused Python：

    .venv/bin/python -m pytest -q tests/test_memory_management_contracts.py tests/test_memory_management_postgres.py tests/test_local_web_memory_management.py tests/test_memory_governance_semantics.py tests/test_round8_advisory_memory.py

clean-v0 与 PostgreSQL：

    .venv/bin/python -m pytest -q tests/test_stage5_clean_migration.py tests/test_stage2_conversation_kernel_postgres.py tests/test_memory_governance_semantics.py tests/test_memory_management_postgres.py

Web：

    .venv/bin/python -m pytest -q tests/test_local_web_http_surface.py tests/test_local_web_memory_management.py

Frontend：

    cd frontend
    npm test -- --run
    npm run build

若仓库 package script 的实际名称不同，使用 package.json 中的真实等价命令并记录。不得通过 skip、xfail、弱化断言或改证据换绿。

浏览器 dogfood 至少记录：

1. global context 列表、USER_PROFILE“关于你”筛选与详情；
2. project selector 包含 closed project、排除 quick；
3. a/b 两端 contradiction 相对文案；
4. BASED_ON cascade preview；
5. restoration conflict 的 NEEDS_RESOLUTION；
6. 人工制造 drift 后 409；
7. READY 删除成功、页面刷新、数据库 canonical 行核验；
8. 原始对话仍可打开。

本功能本身不要求调用真实 LLM provider。若实现者额外运行真实 provider，绝不能输出 PULSARA_API_KEY；provider usage 也不参与删除或记忆状态判断。

---

## 20. 禁止实现清单

以下任一出现都视为 hard-cut 失败：

- DELETE memory_facts 后捕获 FK 异常再逐项猜测；
- 为删除加 DELETED lifecycle；
- 用 deleted_at 模拟 hard delete但仍可被 recall/detail 读取；
- ON DELETE CASCADE 从 fact 广泛传播到 relation/candidate 并绕过恢复；
- 把 BASED_ON、SUPERSEDES、CONTRADICTS 都当同一种 cascade；
- 删除 superseder 后无条件恢复 target；
- restoration 冲突时按 newest、oldest、id 或模型分数选 winner；
- 把 SUPERSEDED orphan 留在库中；
- 关系超过 100 条时宣称完整；
- 用 memory_search 结果渲染管理目录；
- 让前端提交 memory_domain_id 或 raw context_id；
- 让模型调用 delete_memory；
- 新增 plan fingerprint、registry 或 durable deletion job；
- 将完整 confirmation JSON.stringify 后交给 8 MiB aggregate request limit；
- 为绕过 deletion stream 而把整个 WebApplication 的 client_max_size 改成 unlimited；
- 为本功能新增 incoming SUPERSEDES partial unique index、relation fact_kind composite FK 或全局 lifecycle/lineage constraint trigger；
- 用全库 lifecycle 扫描代替本次 affected-subgraph final validation；
- 修改当前 provider prefix；
- 删除 transcript/tool result 来伪造“彻底遗忘”；
- 0001 migration、兼容旧 schema、v1/v2 双读写；
- feature flag；
- 任意 graph hop、retry count 或总库存 cap。

---

## 21. 完成定义

只有同时满足以下条件才算完成：

0. governance 与 taxonomy/context subtraction 两项前置 hard cut 已完成全部 DoD；terminal claim、
   source-aware prompt、四类 taxonomy、exact context、recorded_at 与 clean-v0 基线没有被本功能回退或旁路。
1. “记忆”是可用一级页面，不是静态 mock。
2. global 与 project context 精确，quick 被排除；“关于你”只表示 USER_PROFILE kind。
3. catalog/detail 是独立、分页、完整的管理读取面。
4. CONTRADICTS 两端都以当前详情对象为主语投影 companion。
5. relation target 删除后 surviving accepted candidate 的 formation summary 保持非空且 byte-identical，
   不重跑 governance 或生成 replacement 文案。
6. 删除 algebra、candidate cleanup、restoration admission 在一个 exact transaction 内实现。
7. 只有 READY exact plan 能执行，drift 必须 409。
8. 用户而非模型解决 restoration 冲突。
9. clean-v0 只包含单向 FK action 与最小 grants 变化；schema diff 证明没有新增 index、constraint trigger、function 或 composite FK，executor-local final validation 经 PostgreSQL 证明。
10. 没有新增 durable relation/event/job/checkpoint/registry 或兼容路径。
11. provider prefix continuity 与下一 ROOT prompt 生效时点有测试。
12. 超过 8 MiB 的 confirmed confirmation stream 能进入 executor，且没有 total inventory cap、plan registry 或全局 unlimited body。
13. Python、PostgreSQL、Web、Frontend 和浏览器 dogfood 全部通过并保留具体证据。
14. 最终 git diff 只包含本功能及必要 clean-v0 资源更新，没有覆盖并发任务或未跟踪用户文件。
