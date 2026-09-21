# Pulsara 项目、会话与记忆解耦 Hard-cut 数据库设计

状态：增量设计稿，待实现前冻结  
适用范围：clean-v0 PostgreSQL canonical schema、repository 归属、记忆来源投影  
不在本稿范围：用户会话永久删除 API／UI、批量清理、工作目录物理删除、会话归档与保留策略

## 1. 背景与目标

当前 schema 将项目元数据重复保存在每条 `sessions` 记录中，并让 `memory_facts`、`memory_relations` 通过包含 `session_id` 的组合外键永久依赖创建它们的 ToolResult。这个形状在会话永不物理删除时可以工作，但混淆了三个不同概念：

1. 项目或全局 context 决定记忆**属于哪里**；
2. 成功的记忆变更 ToolResult 记录记忆或关系**最初由哪次 canonical 工具结算创建**；
3. ToolResult 所属 session 只是仍可打开时的**来源导航位置**。

项目与会话是一对多，项目与项目记忆也是一对多。会话不是项目记忆的生命周期 owner。用户将来永久删除某条会话时，已正式保存的 advisory 记忆及其关系图默认应继续存在，只失去返回原会话查看创建位置的能力。

本 hard-cut 的目标是先把数据库真源整理为下列结构，再单独设计会话永久删除功能：

```text
memory domain
├── global memory context
│   └── memory facts
└── project workspace
    ├── sessions
    └── project memory context
        └── memory facts

memory fact / relation
└── optional created_by ToolResult
    └── session
        └── optional physical ToolAttempt
```

其中 `optional` 不是写入时可省略，而是为其来源会话被用户永久删除后保留的 canonical 状态。

## 2. 权威边界

本稿在实现并冻结后，取代 `PULSARA_DIRECT_ADVISORY_MEMORY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 中以下窄边界：

- `memory_facts` 以 `(source_session_id, source_tool_result_id)` 永久归属创建结果；
- `memory_relations` 以 `(owner_session_id, owner_tool_result_id)` 永久归属关系结果；
- 记忆页项目元数据必须从仍存在的 project session 推导。

其余直接写入式记忆合同继续成立，尤其包括：

- 原始观察 ToolResult 不成为记忆的持久证据；
- 模型以自然语言把值得保存的观察转写为 `FACT`；
- 语义不同且真实依赖该 FACT 的后续记忆才使用 `BASED_ON`；
- `remember`、`mark_memory_relation` 与各自 canonical ToolResult 同事务结算；
- 用户正文编辑不重写原始会话或创建来源，不自动重判关系；
- 记忆和关系始终是 advisory 数据，而非执行权威或事实保证。

在本设计尚未实现前，当前生产代码和 clean-v0 baseline 仍是运行真源；不得先在会话删除路径中加入兼容补丁、可空旧字段或双读逻辑。

## 3. 核心语义决定

### 3.1 归属与来源必须分离

记忆事实的生命周期归属于 `memory_context`：

- 全局记忆归属于该 memory domain 的 `GLOBAL` context；
- 项目记忆归属于绑定 canonical project workspace 的 `PROJECT` context。

记忆事实可以保留一个创建来源：成功创建它的 `remember` ToolResult。关系可以保留一个创建来源：成功创建该关系的 `remember` 或 `mark_memory_relation` ToolResult。

来源引用不授予 ToolResult 对记忆的生命周期所有权。删除来源不能删除、恢复、改写或重新分类记忆，也不能删除关系。

### 3.2 原始观察 ToolResult 不持久关联记忆

合法流程示例：

```text
工具 A -> ToolResult a
    模型阅读并转写
remember #1 -> ToolResult r1 + FACT b
    模型形成语义不同且依赖 b 的决定
remember #2 -> ToolResult r2 + DECISION c + (c BASED_ON b)
```

数据库只建立：

```text
b.created_by_tool_result_id = r1
c.created_by_tool_result_id = r2
(c BASED_ON b).created_by_tool_result_id = r2
```

数据库绝不建立：

```text
b -> ToolResult a
```

`remember` ToolResult 返回的至多三条相关旧记忆只是当次只读候选。候选出现在结果正文或 `model_visible_memory_fact_ids` 中，不代表候选由该 ToolResult 创建，不自动产生 `BASED_ON`、`SUPERSEDES` 或 `CONTRADICTS`。

### 3.3 ToolResult 是 canonical 创建边界，ToolAttempt 不是

`tool_execution_attempts` 表示一次物理执行尝试。一次逻辑工具调用可能经历失败、取消、ACK 丢失、重试或 exact confirmation；因此 Attempt 不是稳定的产品创建身份。

`tool_results` 表示已经被会话接受的 canonical 工具结算。记忆及关系只引用成功的 canonical ToolResult。ToolResult 自身已有可空 `attempt_id`，需要诊断时仍可沿 `ToolResult -> ToolAttempt` 查询，不需要从记忆增加 Attempt 外键。

不得增加：

- `created_by_tool_attempt_id`；
- fact／relation 到 Tool Call 的第二条 owner 边；
- 为记忆另建 write receipt、provenance tombstone 或 attempt 映射表。

### 3.4 `NULL` 是单一、明确的删除语义

`created_by_tool_result_id IS NULL` 的唯一产品含义是：

> 这条记忆或关系最初由合法的 canonical ToolResult 创建，但该 ToolResult 已随用户永久删除源会话而消失。

它不表示：

- 创建来源未知；
- 写入时没有 ToolResult；
- 导入或迁移遗漏；
- 用户手工清空来源；
- ToolResult 内容不可信；
- 记忆由用户编辑过。

因此所有新 fact／relation 写入时 `created_by_tool_result_id` 必须非空。空值是为未来会话永久删除保留的 canonical 状态，不是普通 writer 可选择的输入。

### 3.5 不保留会话墓碑或来源副本

源会话被永久删除后，不把会话标题、消息摘要、ToolResult 正文、ToolAttempt、entry ID 或 session ID 复制到另一张 provenance 表。否则会话名义上删除，内容实际上仍被另一套来源记录保留。

保留下来的只有独立产品数据：

- 记忆正文、kind、context、生命周期和时间；
- 记忆关系图；
- 用户编辑状态；
- 项目 context 所需的项目身份与显示元数据；
- `created_by_tool_result_id = NULL` 所表达的“最初保存位置已删除”。

## 4. Canonical 实体与关系

### 4.1 `workspaces`

项目不能继续只是每条 session 中重复的一组字符串。新增 canonical `workspaces`，统一持有 project 和 transient 工作空间身份：

```sql
CREATE TABLE pulsara_v3.workspaces (
    memory_domain_id text NOT NULL,
    id text NOT NULL,
    workspace_kind text NOT NULL
        CHECK (workspace_kind IN ('project', 'transient')),
    workspace_root text NOT NULL CHECK (workspace_root <> ''),
    workspace_label text NOT NULL CHECK (workspace_label <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (memory_domain_id, id),
    UNIQUE (memory_domain_id, workspace_kind, workspace_root)
);
```

设计约束：

- project workspace 的 `id` 继续使用现有稳定 project context ID；
- transient workspace 继续使用现有基于规范化 root 的稳定 workspace key；
- `workspace_kind`、规范化 root 和 identity 创建后不可漂移；
- label 的未来重命名不属于本稿，首版保持现有 exact identity 行为；
- workspace 不是文件系统存在性证明。项目目录后来被物理删除，canonical workspace 仍可因正式项目记忆而存在。

### 4.2 `sessions`

`sessions` 继续是会话 aggregate root，但不再重复持有 workspace kind／root／label：

```sql
CREATE TABLE pulsara_v3.sessions (
    id text PRIMARY KEY,
    memory_domain_id text NOT NULL,
    workspace_id text NOT NULL,
    model_call_binding jsonb,
    lifecycle text NOT NULL CHECK (lifecycle IN ('OPEN', 'CLOSED')),
    writer_generation bigint NOT NULL CHECK (writer_generation >= 1),
    writer_lease_owner_id text,
    writer_lease_expires_at timestamptz,
    latest_entry_sequence bigint NOT NULL DEFAULT 0,
    latest_event_sequence bigint NOT NULL DEFAULT 0,
    latest_prompt_queue_sequence bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, workspace_id),
    UNIQUE (id, workspace_id, memory_domain_id),
    FOREIGN KEY (memory_domain_id, workspace_id)
        REFERENCES pulsara_v3.workspaces (memory_domain_id, id)
        ON DELETE RESTRICT
);
```

现有 session 子表仍可携带 `workspace_id` 并通过 session composite FK exact-join；它们不各自重复 join `workspaces`。session 读取标题、路径和 kind 时由 repository join canonical workspace。

一个 project workspace 可以拥有零至多个 session。未来删除最后一条 session 时，如果项目记忆仍引用该 workspace，workspace 必须保留。

### 4.3 `memory_contexts`

新增 `memory_contexts` 作为记忆生命周期 scope：

```sql
CREATE TABLE pulsara_v3.memory_contexts (
    memory_domain_id text NOT NULL,
    id text NOT NULL,
    context_kind text NOT NULL CHECK (context_kind IN ('GLOBAL', 'PROJECT')),
    workspace_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (memory_domain_id, id),
    UNIQUE (memory_domain_id, workspace_id),
    FOREIGN KEY (memory_domain_id, workspace_id)
        REFERENCES pulsara_v3.workspaces (memory_domain_id, id)
        ON DELETE RESTRICT,
    CHECK (
        (context_kind = 'GLOBAL' AND id = 'ctx:global' AND workspace_id IS NULL)
        OR
        (context_kind = 'PROJECT' AND workspace_id IS NOT NULL AND id = workspace_id)
    )
);
```

标准 FK 无法单独表达“PROJECT context 只能引用 `workspace_kind='project'`”。clean-v0 必须用窄 constraint trigger 校验所引 workspace 的 kind，writer 同事务重复 typed 校验。不得让 transient workspace 获得项目记忆 context。

GLOBAL context 在该 memory domain 首次写入全局记忆时按 canonical identity 幂等建立。PROJECT context 在该项目首次保存项目记忆时，与 fact 同事务建立。项目 context 的 root／label 只从 `workspaces` 投影，不在 `memory_contexts` 或 fact 上复制。

项目记忆页的项目列表从 `memory_contexts JOIN workspaces` 并按仍存在的 fact 查询，不再扫描 `sessions`。删除最后一条项目记忆后，既有记忆删除事务应移除空 PROJECT context；workspace 是否仍保留由 session／context 实际引用决定。

### 4.4 `memory_facts`

目标来源列只有一个：

```sql
created_by_tool_result_id text
```

核心形状：

```sql
CREATE TABLE pulsara_v3.memory_facts (
    id text PRIMARY KEY,
    memory_domain_id text NOT NULL,
    context_id text NOT NULL,
    created_by_tool_result_id text,
    lifecycle text NOT NULL CHECK (lifecycle IN ('ACTIVE', 'SUPERSEDED')),
    fact_kind text NOT NULL CHECK (fact_kind IN (
        'USER_PROFILE', 'RESPONSE_PREFERENCE', 'FACT', 'DECISION'
    )),
    statement text NOT NULL,
    fact_semantic_digest text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    user_edited_at timestamptz,
    ...,
    FOREIGN KEY (memory_domain_id, context_id)
        REFERENCES pulsara_v3.memory_contexts (memory_domain_id, id)
        ON DELETE RESTRICT,
    FOREIGN KEY (created_by_tool_result_id)
        REFERENCES pulsara_v3.tool_results (id)
        ON DELETE SET NULL
);
```

新增部分唯一索引：

```sql
CREATE UNIQUE INDEX uq_memory_fact_created_by_result
ON pulsara_v3.memory_facts (created_by_tool_result_id)
WHERE created_by_tool_result_id IS NOT NULL;
```

一次成功的 `remember` ToolResult 最多创建一条新 fact。`ALREADY_PRESENT` ToolResult 不创建 fact，不改绑既有 fact，也不改变既有创建来源。

从表中 hard-cut 删除：

- `source_session_id`；
- `source_tool_result_id`；
- `(id, source_session_id, source_tool_result_id)` unique；
- `(source_session_id, source_tool_result_id)` unique；
- 指向 `(tool_results.session_id, tool_results.id)` 的组合 FK。

### 4.5 `memory_relations`

目标来源列同样只有一个：

```sql
created_by_tool_result_id text
```

```sql
CREATE TABLE pulsara_v3.memory_relations (
    id text PRIMARY KEY,
    memory_domain_id text NOT NULL,
    created_by_tool_result_id text,
    source_context_id text NOT NULL,
    source_fact_id text NOT NULL,
    source_fact_kind text NOT NULL,
    relation_kind text NOT NULL CHECK (relation_kind IN (
        'BASED_ON', 'SUPERSEDES', 'CONTRADICTS'
    )),
    target_context_id text NOT NULL,
    target_fact_id text NOT NULL,
    target_fact_kind text NOT NULL,
    supersede_mode text,
    ordinal integer,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (created_by_tool_result_id)
        REFERENCES pulsara_v3.tool_results (id)
        ON DELETE SET NULL,
    ...
);
```

关系的端点 FK、context／kind exact identity、无向冲突唯一性、取代方向与删除图索引继续保留。`created_by_tool_result_id` 不唯一，因为一次 `remember` 可以为新 fact 建立最多八条 `BASED_ON`。

来源归属规则：

- `BASED_ON` 由创建 source fact 的同一个成功 `remember` ToolResult 创建；
- 新 `SUPERSEDES`／`CONTRADICTS` 由成功的 `mark_memory_relation` ToolResult 创建；
- `ALREADY_PRESENT` 不改绑原关系；
- related-memory 候选不创建或认领关系。

从表中 hard-cut 删除：

- `owner_session_id`；
- `owner_tool_result_id`；
- 指向 `(tool_results.session_id, tool_results.id)` 的组合 FK。

## 5. 来源约束与状态转换

### 5.1 fact 写入

INSERT 时 `created_by_tool_result_id` 必须非空，constraint trigger 和 canonical writer 都必须验证：

1. ToolResult 存在且 `result_record_kind='EXECUTED'`；
2. `result_state='SUCCESS'`；
3. 关联 tool-call block 的工具名是 `remember`；
4. ToolResult 所属 session 的 `memory_domain_id` 与 fact 相同；
5. `remember` 冻结输入的 kind、statement、context 与实际 fact exact-join；
6. PROJECT context 绑定 ToolResult session 的同一个 project workspace；
7. `BASED_ON` 输入与同事务创建的关系集合 exact-join。

### 5.2 relation 写入

INSERT 时 `created_by_tool_result_id` 必须非空：

- `BASED_ON` 验证 owner ToolResult 与 source fact 的创建 ToolResult 相同，并验证输入中的 `based_on_memory_ids`；
- `SUPERSEDES`／`CONTRADICTS` 验证 owner 工具为 `mark_memory_relation`，参数、端点、方向、domain、context、kind 和生命周期 exact-join。

### 5.3 来源字段不可改绑

普通 fact 编辑只允许修改现有用户正文编辑合同列。来源字段必须满足状态机：

```text
non-null original ToolResult ID
             |
             | source session permanently deleted
             v
            NULL
```

禁止：

```text
NULL -> ToolResult ID
ToolResult A -> ToolResult B
ToolResult ID -> NULL（来源仍存在）
```

clean-v0 约束触发器应允许用户编辑一条来源已删除的 fact，而不再尝试 join 已不存在的 ToolResult；但不能因此放宽 INSERT 或改绑验证。实现可用 deferred constraint trigger 在事务末确认 `non-null -> NULL` 时旧 ToolResult 确已消失。不得增加 `origin_status`、`source_deleted_at` 或墓碑行来重复表达同一状态。

### 5.4 ToolResult 删除边界

产品层不提供单独删除 ToolResult 的 API。未来唯一能够让记忆来源变为 NULL 的正常路径是永久删除整个 session aggregate。

数据库管理员执行 clean-v0 重置会同时删除会话和记忆，不产生保留 fact＋空来源的产品状态。测试夹具若直接删除 ToolResult 以验证 FK，只是 schema 测试，不代表产品支持单条结果删除。

## 6. 读取与 UI 投影

### 6.1 创建来源

来源投影由 `memory_fact.created_by_tool_result_id` 或 `memory_relation.created_by_tool_result_id` 开始，依次 join ToolResult、tool-call entry、session 和 workspace：

```text
created_by_tool_result_id non-null且链路存在
    -> AVAILABLE／CLOSED，按现有同源规则决定是否给跳转定位

created_by_tool_result_id NULL
    -> DELETED，显示“最初保存位置已删除”
```

不得把 NULL 投影为“未知来源”“无来源”或数据库错误。非空 ID 却无法 exact-join 是完整性错误，不得降级伪装成 DELETED。

用户编辑正文后，来源仍表示“最初保存位置”，不能声称原对话证明当前改写后的文本；UI 和 `memory_explain` 继续明确 `user_edited_at`。

### 6.2 项目列表

记忆页项目列表只依赖：

```text
memory_contexts(PROJECT)
JOIN workspaces(project)
WHERE EXISTS memory_facts
```

它不接受会话页当前 session，不要求项目存在 OPEN session，也不因为最后一条 session 被永久删除而隐藏仍存在的项目记忆。

项目目录从文件系统消失不自动删除项目记忆。未来若提供删除项目记忆或清理 workspace，必须另行定义用户确认和记忆删除图影响。

## 7. 会话删除的预留边界

本稿不实现会话永久删除，但 schema 必须使未来路径能够在一笔事务中获得以下结果：

```text
删除 session aggregate
├── 删除 transcript／turn／tool／task／event／visualization 等会话数据
├── 删除该 session 的 ToolResults
│   ├── memory_facts.created_by_tool_result_id -> NULL
│   └── memory_relations.created_by_tool_result_id -> NULL
├── 保留 memory facts
├── 保留 memory relations
├── 保留 project memory context
└── project workspace 在仍有 session 或项目记忆时继续存在
```

未来删除规格仍需另行冻结：

- Host／browser bridge 的 quiesce 与不确定结算；
- session aggregate 内部 FK 的 CASCADE／deferred NO ACTION 形状；
- 共享 blob 的精确回收；
- 当前会话删除后的前端选择；
- transient 工作目录是否另给显式物理删除选项；
- 是否以及如何提供“同时删除该会话产生的记忆”。

这些内容不得提前塞进本轮 memory decoupling repository。

## 8. Hard-cut 删除清单

实现本设计时必须一次性删除旧路径：

1. 删除 `sessions.workspace_kind`、`workspace_root`、`workspace_label`，由 canonical `workspaces` 接管；
2. session 创建、恢复、fork、列表和 workspace 校验统一 join `workspaces`；
3. 新增 `memory_contexts`，fact context 改为真实 FK；
4. 删除记忆项目列表从 `sessions` 聚合项目元数据的 `_PROJECTS` 查询；
5. `memory_facts` 删除 `source_session_id/source_tool_result_id`，改为单列 `created_by_tool_result_id`；
6. `memory_relations` 删除 `owner_session_id/owner_tool_result_id`，改为单列 `created_by_tool_result_id`；
7. 所有 lineage trigger、writer、memory detail、`memory_explain`、删除预览和测试改用新列；
8. 不保留旧列、可空别名、兼容 view、双写、回填 migration、旧新查询 fallback 或 session-derived project projection；
9. 重建 clean-v0，并按 `AGENTS.md` 核验后重置本地 disposable 生产数据库。

## 9. 实施顺序

1. 修订 baseline：`workspaces`、`memory_contexts`、session workspace FK、fact／relation 单列创建来源及约束触发器；
2. 改 session authority、fork、summary 和 workspace reader，使 workspace 成为 canonical 父实体；
3. 改 `remember`／`mark_memory_relation` canonical transaction 和 lineage 校验；
4. 改记忆查询、项目列表、来源投影、用户编辑与删除预览；
5. 删除旧 schema／DTO／SQL／测试形状；
6. 重建 clean-v0，运行 schema oracle、Python／前端全量回归及真实 provider 记忆 dogfood；
7. 只有本设计验收通过后，另立会话永久删除 hard-cut 实施规格。

## 10. 验收条件

### 10.1 Schema

- `workspaces` 是 workspace kind／root／label 的唯一 canonical owner；
- project workspace 可同时被多条 session 和一个 PROJECT memory context 使用；
- fact 必须引用合法 `memory_contexts`；
- fact／relation 不含任何 session 来源列；
- fact／relation 只含单个可空 `created_by_tool_result_id`；
- ToolResult ID 继续使用现有全局主键，不新增组合来源列；
- 不存在 provenance tombstone、source copy、write receipt 或 ToolAttempt owner。

### 10.2 写入

- 无 ToolResult、失败 ToolResult、错误工具名、错误 domain／project context 均不能创建 fact／relation；
- 一次 `remember` 的 SAVED fact 与其 ToolResult exact-join；
- `ALREADY_PRESENT` 不改绑来源；
- related-memory 候选不产生 owner 或关系；
- `BASED_ON` 与创建 source fact 的同一 `remember` ToolResult exact-join；
- 后补关系与成功 `mark_memory_relation` ToolResult exact-join；
- 记忆不直接引用 ToolAttempt 或原始观察 ToolResult。

### 10.3 生命周期与读取

- 来源存在时，fact／relation 可经 ToolResult 间接定位 session；
- 来源 ID 为 NULL 时，只投影“最初保存位置已删除”；
- 来源已删除的 fact 仍可检索、解释、编辑和参与关系图；
- 项目没有任何 session 但仍有项目 fact 时，项目仍出现在记忆页；
- 当前会话切换不改变项目记忆列表；
- 删除最后一条项目 fact 后，空 PROJECT context 退出记忆页项目列表。

### 10.4 连续性与范围

- schema hard-cut 不改变 provider `SYSTEM`／tools 的同 epoch 前缀连续性；
- 本轮不新增 durable event、subject slot、append guard、live event 或 durable job；
- 本轮新增的 `workspaces` 与 `memory_contexts` 是 canonical 产品实体，不是执行证明、回执或兼容投影；
- 本轮不提供会话删除 API／UI，也不物理删除任何用户工作目录。

## 11. 仍需在实施稿中精确化的事项

本设计冻结产品方向，但进入代码前仍需在实施稿中根据当前 FK 图逐项列明：

1. `workspaces` identity／root／label 的 exact normalization 与现有 `ResolvedWorkspace` 对齐方式；
2. global／project `memory_contexts` 的幂等创建与空 context 清理 SQL；
3. deferred constraint trigger 如何只允许合法的来源 `non-null -> NULL` 转换；
4. user edit、memory deletion 与来源已删除状态并发时的锁顺序；
5. 现有 schema relation oracle 的净增减及 baseline 重建断言；
6. 真实 provider dogfood 如何证明原始 ToolResult、related candidates 与创建 ToolResult 三者未被混淆。

若这些事项暴露新的产品语义冲突，应先修订本设计；不得用兼容字段、隐式 fallback 或会话删除特例绕过。
