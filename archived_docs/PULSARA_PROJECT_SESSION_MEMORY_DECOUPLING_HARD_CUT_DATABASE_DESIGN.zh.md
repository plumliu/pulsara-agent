# Pulsara 项目、会话与记忆解耦 Hard-cut 数据库实施规格

状态：已冻结（2026-09-22），可直接实施
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
├── global memory facts (`context_id = ctx:global`)
└── project workspace
    ├── sessions
    └── project memory facts (`context_id = workspace.id`)

memory fact / relation
└── optional created_by ToolResult
    └── session
        └── optional physical ToolAttempt
```

其中 `optional` 不是写入时可省略，而是为其 owner ToolResult 实际消失后保留的 canonical 状态。产品层只授权未来的整会话永久删除路径移除这类 ToolResult；数据库不把服务层授权范围伪装成仅靠 FK 就能证明的不变量。

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

记忆事实的生命周期归属于 context：

- 全局记忆以 `context_id='ctx:global'` 归属于该 memory domain；
- 项目记忆以 `context_id=workspace.id` 直接归属于 canonical project workspace。

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

`created_by_tool_result_id IS NULL` 的唯一产品投影含义是：

> 这条记忆或关系最初由合法的 canonical ToolResult 创建，但该 ToolResult 已随用户永久删除源会话而消失。

它不表示：

- 创建来源未知；
- 写入时没有 ToolResult；
- 导入或迁移遗漏；
- 用户手工清空来源；
- ToolResult 内容不可信；
- 记忆由用户编辑过。

因此所有新 fact／relation 写入时 `created_by_tool_result_id` 必须非空。空值不是普通 writer 可选择的输入。

数据库本身能够可靠证明的较窄不变量是：

> `A -> NULL` 发生时，事务提交前 owner ToolResult A 已实际不存在。

仅凭 fact 到 ToolResult 的 FK，PostgreSQL 无法证明 A 一定是随整条 session 删除，而不是被另一条直接 SQL 删除。产品层必须把 ToolResult 删除权限收口到未来的 session aggregate 删除 owner；不增加 tombstone、receipt 或删除授权表来伪造这一证明。若非授权代码单独删除 ToolResult，它违反 repository authority，即使最终数据库形状同样表现为 NULL。

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
    memory_domain_id text NOT NULL CHECK (
        memory_domain_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'
    ),
    id text NOT NULL,
    workspace_kind text NOT NULL
        CHECK (workspace_kind IN ('project', 'transient')),
    workspace_root text NOT NULL CHECK (workspace_root <> ''),
    workspace_label text NOT NULL CHECK (workspace_label <> ''),
    PRIMARY KEY (memory_domain_id, id),
    UNIQUE (memory_domain_id, workspace_kind, workspace_root),
    CHECK (
        (workspace_kind = 'project'
            AND id ~ '^ctx:workspace/[a-z0-9][a-z0-9._-]{0,127}$')
        OR
        (workspace_kind = 'transient'
            AND id ~ '^transient:[0-9a-f]{64}$')
    )
);
```

设计约束：

- project workspace 的 `id` 继续使用现有稳定 project context ID，并保留 `ctx:workspace/` 命名空间；
- transient workspace 继续使用现有基于规范化 root 的稳定 workspace key，并保留 `transient:` 命名空间；两种 ID 命名空间必须由 CHECK 保持不相交；
- `workspace_kind`、规范化 root、identity 和 label 创建后不可漂移，因此本表不保留无 owner 的 `updated_at`；
- project label 统一由规范化 root 的 basename（为空时用完整 root）确定；`HostWorkspaceInput` 仍为 transient 保留 `display_label`，但 project 输入只要显式提供 `display_label` 就在 workspace resolution 阶段拒绝，不能静默忽略、比较或采用；CLI 的 project `--display-label` 路径同步删除；
- transient label 由首次创建该 canonical transient workspace 的输入冻结；后续采用同一 root 时必须 exact-match，否则返回 typed workspace metadata conflict；
- 两个并发创建者先按 `(memory_domain_id, id)` 取得统一的 transaction-local canonical identity advisory lock，再执行 `INSERT ... ON CONFLICT DO NOTHING` 并 exact-compare kind／root／label；相同值共享赢家，不同值只有一个 canonical 赢家，另一方得到 typed conflict，不允许 `DO UPDATE` 覆盖；`DO NOTHING` 自身不视为行锁；
- workspace 不是文件系统存在性证明。项目目录后来被物理删除，canonical workspace 仍可因正式项目记忆而存在。
- workspace 不保存 `created_at`：当前没有产品读取该时间，项目排序以 fact 的 `MAX(updated_at)` 为准；orphan workspace 被删除后重建还会让该时间失去稳定含义，保留它只会增加无人消费的持久审计值。

runtime schema grant 精确冻结为 `workspaces: SELECT, INSERT, DELETE`，不授予 `UPDATE`。INSERT 只由 session create／fork workspace adoption owner 使用；DELETE 只由记忆 orphan cleanup 和未来另行冻结的 session-delete owner 使用。kind／root／label 不可变性因此不需要再增加 UPDATE trigger；repository 仍须将 INSERT／DELETE 收口到上述产品 owner，grant 本身不是调用授权。

本稿中对 immutable `workspaces`／`memory_relations` 的“锁定”精确定义为 repository 统一取得的 transaction-local canonical identity advisory lock：锁键使用带实体 namespace 的 `(memory_domain_id, canonical_id)`，并通过 `pg_advisory_xact_lock(hashtextextended(...))` 映射为当前数据库事务锁。它不持久化 hash、不充当对象身份或完整性证明；极小概率 hash 碰撞只会让无关操作额外串行，不能放过冲突操作。采用该机制是因为 PostgreSQL 的 locking `SELECT` 需要 UPDATE 权限，而上述两张 immutable 表按冻结 grant 明确不能获得 UPDATE。`memory_facts` 仍使用 `SELECT ... FOR UPDATE`，session writer 仍使用既有 session row lock。所有 repository 创建、引用、清理 owner 必须使用同一 namespace 和锁键；普通 `INSERT ... ON CONFLICT DO NOTHING` 不能替代该锁。

### 4.2 `sessions`

`sessions` 继续是会话 aggregate root，但不再重复持有 workspace kind／root／label：

```sql
CREATE TABLE pulsara_v3.sessions (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    memory_domain_id text NOT NULL CHECK (
        memory_domain_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'
    ),
    model_call_binding jsonb,
    lifecycle text NOT NULL CHECK (lifecycle IN ('OPEN', 'ARCHIVED')),
    writer_generation bigint NOT NULL CHECK (writer_generation >= 1),
    writer_lease_owner_id text,
    writer_lease_expires_at timestamptz,
    latest_entry_sequence bigint NOT NULL DEFAULT 0 CHECK (latest_entry_sequence >= 0),
    latest_event_sequence bigint NOT NULL DEFAULT 0 CHECK (latest_event_sequence >= 0),
    latest_prompt_queue_sequence bigint NOT NULL DEFAULT 0
        CHECK (latest_prompt_queue_sequence >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, workspace_id),
    CHECK ((writer_lease_owner_id IS NULL) = (writer_lease_expires_at IS NULL)),
    CHECK (
        model_call_binding IS NULL OR (
            jsonb_typeof(model_call_binding) = 'object'
            AND model_call_binding ? 'connection_id'
            AND model_call_binding ? 'reasoning'
            AND model_call_binding - ARRAY['connection_id', 'reasoning']::text[] = '{}'::jsonb
            AND jsonb_typeof(model_call_binding->'connection_id') = 'string'
        )
    ),
    FOREIGN KEY (memory_domain_id, workspace_id)
        REFERENCES pulsara_v3.workspaces (memory_domain_id, id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_pulsara_v3_sessions_workspace_fk
ON pulsara_v3.sessions (memory_domain_id, workspace_id);
```

现有 session 子表仍可携带 `workspace_id` 并通过 session composite FK exact-join；它们不各自重复 join `workspaces`。session 读取标题、路径和 kind 时由 repository join canonical workspace。

`sessions.id` 已是全局 primary key；`UNIQUE (id, workspace_id)` 继续作为现有 session 子表 composite FK 的 exact-join 目标。没有任何下游 FK 引用 `(id, workspace_id, memory_domain_id)`，因此不保留这个被 primary key 完全覆盖的三列 unique；domain／workspace 归属由 session 自身的 `(memory_domain_id, workspace_id) -> workspaces` FK 冻结。

一个 project workspace 可以拥有零至多个 session。未来删除最后一条 session 时，如果项目记忆仍引用该 workspace，workspace 必须保留。

### 4.3 `memory_facts`

目标来源列只有一个：

```sql
created_by_tool_result_id text
```

核心形状：

```sql
CREATE TABLE pulsara_v3.memory_facts (
    id text PRIMARY KEY,
    memory_domain_id text NOT NULL,
    context_id text NOT NULL CHECK (
        context_id = 'ctx:global'
        OR context_id ~ '^ctx:workspace/[a-z0-9][a-z0-9._-]{0,127}$'
    ),
    project_workspace_id text GENERATED ALWAYS AS (
        CASE WHEN context_id = 'ctx:global' THEN NULL ELSE context_id END
    ) STORED,
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
    FOREIGN KEY (memory_domain_id, project_workspace_id)
        REFERENCES pulsara_v3.workspaces (memory_domain_id, id)
        ON DELETE RESTRICT,
    FOREIGN KEY (created_by_tool_result_id)
        REFERENCES pulsara_v3.tool_results (id)
        ON DELETE SET NULL
        DEFERRABLE INITIALLY DEFERRED
);
```

`project_workspace_id` 是纯数据库派生列，不进入 DTO、工具参数或事实身份。GLOBAL fact 得到 NULL，PROJECT fact 得到其现有 `context_id`。由于 `workspaces` 用 CHECK 保持 project／transient ID 命名空间不相交，而 project memory context 只接受 `ctx:workspace/`，该 FK 只能落到 project workspace，不需要额外的 `memory_contexts` 表或 workspace-kind trigger。

项目 root／label 只从 `workspaces` 投影，不在 fact 上复制。项目记忆页从 `workspaces JOIN memory_facts ON fact.memory_domain_id=workspace.memory_domain_id AND fact.project_workspace_id=workspace.id` 查询，不再扫描 `sessions`。没有 fact 的 workspace 不出现在记忆项目列表中，因此不需要创建、清理或修复派生的 context 行。

新增索引：

```sql
CREATE UNIQUE INDEX uq_memory_fact_created_by_result
ON pulsara_v3.memory_facts (created_by_tool_result_id)
WHERE created_by_tool_result_id IS NOT NULL;

CREATE INDEX idx_pulsara_v3_memory_fact_project_workspace_fk
ON pulsara_v3.memory_facts (memory_domain_id, project_workspace_id)
WHERE project_workspace_id IS NOT NULL;
```

一次成功的 `remember` ToolResult 最多创建一条新 fact。`ALREADY_PRESENT` ToolResult 不创建 fact，不改绑既有 fact，也不改变既有创建来源。owner unique partial index 同时服务 ToolResult 删除时的反向 `SET NULL` 查找；project-workspace partial index 同时服务 workspace RESTRICT／orphan cleanup 和记忆页项目查询。

从表中 hard-cut 删除：

- `source_session_id`；
- `source_tool_result_id`；
- `(id, source_session_id, source_tool_result_id)` unique；
- `(source_session_id, source_tool_result_id)` unique；
- 指向 `(tool_results.session_id, tool_results.id)` 的组合 FK。

### 4.4 `memory_relations`

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
        ON DELETE SET NULL
        DEFERRABLE INITIALLY DEFERRED,
    ...
);

CREATE INDEX idx_pulsara_v3_memory_relation_created_by_result_fk
ON pulsara_v3.memory_relations (created_by_tool_result_id)
WHERE created_by_tool_result_id IS NOT NULL;
```

关系的端点 FK、context／kind exact identity、无向冲突唯一性、取代方向与删除图索引继续保留。source 方向查询与 FK 检查复用关系 canonical unique 的 `(memory_domain_id, source_context_id, source_fact_id, relation_kind, ...)` 前缀，不再维护一棵完全重复的 outgoing B-tree；target 方向仍保留独立 incoming index。`created_by_tool_result_id` 不唯一，因为一次 `remember` 可以为新 fact 建立最多八条 `BASED_ON`；其 partial index 是 ToolResult 删除执行反向 `SET NULL` 的必要访问路径。

`memory_relations.id` 已是全局 canonical primary key，且没有下游 composite FK 以 `(memory_domain_id, id)` 引用 relation；因此本表不再保留重复的 `UNIQUE (memory_domain_id, id)`。这与 `memory_facts` 不同：fact 的 `(memory_domain_id, id)` 仍是 `memory_embeddings` 的 exact-identity FK 目标，不能删除。

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

INSERT 时 `created_by_tool_result_id` 必须非空。数据库 deferred constraint trigger 验证可稳定由 canonical rows 表达的条件：

1. ToolResult 存在且 `result_record_kind='EXECUTED'`；
2. `result_state='SUCCESS'`；
3. 关联 tool-call block 的工具名是 `remember`；
4. ToolResult 所属 session 的 `memory_domain_id` 与 fact 相同；
5. tool-call 参数的最终 kind、context target 与 fact exact-join；
6. PROJECT context 绑定 ToolResult session 的同一个 project workspace。

canonical writer 在同一事务额外负责冻结 typed 输入与规范化 statement、semantic digest、search terms 以及 `BASED_ON` 集合的 exact-join。正文 NFC、换行归一化与 Python `strip()` 不在 PostgreSQL 中复制一套近似实现；数据库只执行字节边界、稳定身份和 owner 检查。FK 与 lineage trigger 必须 `DEFERRABLE INITIALLY DEFERRED`，因为当前 canonical transaction 先仲裁／写 fact 和关系，再插入动态最终 ToolResult；不得为迎合立即 FK 拆成第二事务。

### 5.2 relation 写入

INSERT 时 `created_by_tool_result_id` 必须非空：

- `BASED_ON` 验证 owner ToolResult 与 source fact 的创建 ToolResult 相同，并验证输入中的 `based_on_memory_ids`；
- `SUPERSEDES`／`CONTRADICTS` 验证 owner 工具为 `mark_memory_relation`，参数、端点、domain、context、kind 和生命周期 exact-join；`SUPERSEDES` 保留工具参数方向，`CONTRADICTS` 则把工具参数的无序端点集合 exact-join 到 canonical row。

PROJECT relation 还必须在 writer 与 deferred lineage trigger 中同时验证：新关系 owner session 的 `memory_domain_id` 等于 relation domain，且 `owner_session.workspace_id = source_context_id`；`SUPERSEDES`／`CONTRADICTS` 的现有形状进一步要求 `source_context_id = target_context_id`，PROJECT `BASED_ON` 则允许 target 为同 project 或 GLOBAL。GLOBAL relation 要求两端均为 GLOBAL，owner 可以来自同 memory domain 的任意 workspace。仅知道同 domain 的其它项目 memory ID 不授予关系写权限；项目 A 的 session 不能给项目 B 的 facts 建关系。重复／`ALREADY_PRESENT` 预检也先用**当前调用 session**执行相同的 domain／workspace 授权，不能因原 owner 已存在或已删除而绕过；该当前调用只用于授权，不改绑既有 owner。

重复关系的 settlement 规则：owner 非空时继续 exact-join 原 ToolResult；owner 已为 NULL 时，既有 relation 视为先前已由 schema seal 的 canonical 关系，重新验证端点 identity、关系类型以及 `SUPERSEDES` 的有向端点与历史生命周期效果后返回 `ALREADY_PRESENT`，不要求恢复已删除 owner，也不把关系改绑到本次 ToolResult。`CONTRADICTS` 始终按 `(context_id, fact_id)` 对无序端点 canonicalize；首次 INSERT row、canonical ToolResult 正文及反向 `ALREADY_PRESENT` 都返回同一 canonical source／target，原 owner 工具参数允许以任一方向表达该无序端点集合。owner 非空但无法 join 是完整性错误，不能伪装为 DELETED。

### 5.3 来源字段不可改绑

普通 fact 编辑只允许修改现有用户正文编辑合同列。来源字段必须满足状态机：

```text
non-null original ToolResult ID
             |
             | owner ToolResult actually disappears
             v
            NULL
```

禁止：

```text
NULL -> ToolResult ID
ToolResult A -> ToolResult B
ToolResult ID -> NULL（来源仍存在）
```

来源 trigger 事件矩阵冻结为：

| 事件 | 数据库行为 |
|---|---|
| fact／relation INSERT | `created_by_tool_result_id` 必须非空；deferred 校验完整 owner identity |
| ordinary fact UPDATE，owner 未变 | 不重新要求当前 statement 等于原 `remember` 参数；继续由 fact immutability／用户编辑 trigger 约束允许列 |
| 任一 owner `A -> B` | 拒绝 |
| 任一 owner `NULL -> B` | 拒绝 |
| 任一 owner `A -> NULL` | deferred 到事务末；仅当 ToolResult A 已不存在时允许 |
| fact owner `NULL -> NULL` | 允许合法的用户正文编辑、生命周期恢复及其它既有管理更新 |

这使来源已删除的 fact 仍可编辑、恢复、删除和参与关系图，同时不放宽 INSERT。不得增加 `origin_status`、`source_deleted_at` 或墓碑行重复表达 NULL。

relation 行没有合法的普通 UPDATE 路径。新增 BEFORE UPDATE canonical seal：只允许其余列字节不变的非空 `created_by_tool_result_id: A -> NULL` 形状；`id`、domain、两端 context／fact／kind、relation kind、mode、ordinal、accepted time 及其它全部列不可改变，owner 已为 NULL 时同样不能修改关系身份。数据库不声称 BEFORE trigger 能识别 UPDATE 是否由 FK referential action 发起；fact 与 relation 的 deferred lineage constraint trigger 均监听 `AFTER INSERT OR UPDATE`，由它在 `A -> NULL` 时证明 OLD owner 在提交前已实际消失，runtime grant 与 repository authority 再保证正常产品路径只由 FK 产生该转换。INSERT 执行完整 lineage 校验，owner 未变的合法 fact UPDATE 直接保留既有 lineage。runtime grant 对 `memory_relations` 固定为 `SELECT, INSERT, DELETE`，删除现有 `UPDATE`；PostgreSQL 内部执行 `ON DELETE SET NULL` 不要求把普通 UPDATE 权限授予应用角色。

### 5.4 ToolResult 删除边界

产品层不提供单独删除 ToolResult 的 API。repository authority 只允许未来的 session aggregate 永久删除 owner 移除 ToolResult；普通工具、记忆管理和维护任务均无此写入口。数据库保证的是 owner 实际消失，不声称仅靠 FK 就能鉴别调用者授权。

数据库管理员执行 clean-v0 重置会同时删除会话和记忆，不产生保留 fact＋空来源的产品状态。测试夹具若直接删除 ToolResult 以验证 FK，只是 schema 测试，不代表产品支持单条结果删除。

## 6. 读取与 UI 投影

### 6.1 创建来源

来源投影由 `memory_fact.created_by_tool_result_id` 或 `memory_relation.created_by_tool_result_id` 开始，使用 `LEFT JOIN` 依次读取 ToolResult、tool-call entry、session 和 workspace。底层 join 行可复用，但**模型工具**和**用户记忆页**是两个不同的授权投影，不能共用“相对当前 workspace 是否同源”的可见性决定。

共同的 availability 只有：

```text
availability = OPEN | ARCHIVED | DELETED

owner 非空且 exact-join OPEN session   -> OPEN
owner 非空且 exact-join ARCHIVED session -> ARCHIVED
owner 为 NULL                          -> DELETED
```

非空 owner 却无法 exact-join 是完整性错误，不得降级伪装成 DELETED。fact 和 relation owner 都使用同一 availability 状态机；不能因 relation owner 消失而用 inner join 静默漏掉关系。

`memory_explain` 是模型工具，额外计算相对于调用会话 workspace 的 visibility：

```text
visibility   = SAME_ORIGIN | CROSS_ORIGIN_REDACTED | null

OPEN + SAME_ORIGIN
    -> 返回允许的 session／turn／entry locator

ARCHIVED + SAME_ORIGIN
    -> 说明来源已归档，locator=null

OPEN/ARCHIVED + CROSS_ORIGIN_REDACTED
    -> 保留 availability，但隐藏 session／turn／entry locator

DELETED
    -> visibility=null，locator=null，显示“最初保存位置已删除”
```

用户记忆页由已认证的本地服务按 memory domain 授权，不接受当前会话或当前 workspace 作为来源可见性输入，也不返回上述模型用 visibility 字段。其规则固定为：

```text
OPEN    -> 返回可导航 locator
ARCHIVED  -> 显示来源已归档，locator=null
DELETED -> 显示“最初保存位置已删除”，locator=null
```

因此同一条跨 workspace 来源可以在 `memory_explain` 中是 `CROSS_ORIGIN_REDACTED`，同时在用户记忆页中因同 domain 授权而可导航；两者不是矛盾，也不得通过把 domain 授权伪称为 `SAME_ORIGIN` 来复用 wire。

来源工具名不依赖仍存在的 ToolResult 墓碑。fact 的创建工具确定性为 `remember`；relation 的 `write_tool` 由 relation kind 无损派生：`BASED_ON -> remember`，`SUPERSEDES/CONTRADICTS -> mark_memory_relation`。owner 删除后仍返回该派生值，不输出 null、不删除字段，也不复制工具结果内容。

不得把 NULL 投影为“未知来源”“无来源”或数据库错误。

用户编辑正文后，来源仍表示“最初保存位置”，不能声称原对话证明当前改写后的文本；UI 和 `memory_explain` 继续明确 `user_edited_at`。

### 6.2 项目列表

记忆页项目列表只依赖：

```text
workspaces(project)
JOIN memory_facts
  ON fact.memory_domain_id = workspace.memory_domain_id
 AND fact.project_workspace_id = workspace.id
```

它不接受会话页当前 session，不要求项目存在 OPEN session，也不因为最后一条 session 被永久删除而隐藏仍存在的项目记忆。

项目 `last_activity_at` 固定为该项目仍存在的 ACTIVE／SUPERSEDED facts 中 `MAX(updated_at)`；分页稳定排序为 `last_activity_at DESC, workspace.id DESC`，游标冻结这两个完整 typed 值。它不再引用 transcript 或最后一条 session 的活动时间。用户正文编辑、取代／恢复等确实更新 fact `updated_at` 的记忆变化会更新项目排序；单纯打开页面不会。

项目目录从文件系统消失不自动删除项目记忆。未来若提供删除项目记忆或清理 workspace，必须另行定义用户确认和记忆删除图影响。

所有可能首次建立或最终清理 workspace 引用的事务使用统一集合锁序：

```text
session row（既有 Host writer／session control 事务涉及 session 时）
-> 所有受影响 project workspace canonical identity advisory locks（按 memory_domain_id, id 排序）
-> memory facts rows（按 id 排序，`FOR UPDATE`）
-> memory relation canonical identity advisory locks（按 id 排序）
```

现有 Host writer 已先锁定 session，因此 PROJECT `remember` 在其后锁单个 workspace；session 创建／fork 尚无既有 session row，先 UPSERT／锁 workspace 再插 session。

记忆删除没有 session owner，并且一个 global basis 的删除图可能经入边 `BASED_ON` 同时覆盖多个 project workspace，因此执行固定为两阶段重算：

1. 无锁计算预览删除图；
2. 从所有待删、待恢复、参与冲突检查及关系变化的非 global facts 派生完整 `(memory_domain_id, workspace_id)` 集合；
3. 按序锁定集合中的全部 workspace；
4. 按序锁定计划涉及的 facts 和 relations；
5. 在锁内从 canonical rows 重算删除图，并重新派生完整有序 `workspace_ids`、`fact_ids`、`relation_ids`；任一集合发生增减或 identity 变化都回滚本次事务并以新集合重新开始，不能在已锁部分 workspace／facts／relations 后追加锁；
6. 执行删除／恢复后，对全部受影响 workspace 分别执行 `DELETE workspaces ... WHERE NOT EXISTS(session) AND NOT EXISTS(memory_fact)`。

删除执行事务使用 PostgreSQL `READ COMMITTED`。第一次无锁 plan 只用于取得待锁集合；等待锁后的第二次 plan 必须使用新的 statement snapshot，才能观察等待期间已经提交的 fact／relation／session／workspace 变化并触发整事务重试。不得改成 `REPEATABLE READ`／`SERIALIZABLE` 后仍把同一事务里的第二次查询称为 fresh replan；SSI 或最终 FK 错误只能作为失败保护，不能替代集合比较。

这既覆盖从一个 global fact 级联到多个项目，也覆盖纯 GLOBAL 图和同一 project workspace 内在预览后新增 dependent fact／relation 的竞态，并避免 `fact -> workspace` 与 `workspace -> fact` 反序。并发 `remember`／关系标定／新 session 由同一组 workspace、fact 和 relation 锁串行化；序列化失败、死锁、任一锁集合变化或 FK 竞争沿现有有界单次操作 deadline 做确定重算，不引入 retry-count 上限或 durable cleanup job。

## 7. 会话删除边界（由后续冻结规格接管）

会话永久删除现由 `PULSARA_SESSION_PERMANENT_DELETION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 负责。该规格扩展本稿的 schema，在一笔事务中获得以下结果：

```text
删除 session aggregate
├── 删除 transcript／turn／tool／task／event／visualization 等会话数据
├── 删除该 session 的 ToolResults
│   ├── memory_facts.created_by_tool_result_id -> NULL
│   └── memory_relations.created_by_tool_result_id -> NULL
├── 保留 memory facts
├── 保留 memory relations
└── project workspace 在仍有 session 或项目记忆时继续存在
```

后续删除规格已冻结以下责任：

- Host／browser bridge 的 quiesce 与不确定结算；
- session aggregate 内部 FK 的 CASCADE／deferred NO ACTION 形状；
- 共享 blob 的精确回收；
- 当前会话删除后的前端选择；
- 所有工作目录均保留，不提供物理目录删除选项；
- 已保存记忆一律保留，不提供同时删除记忆选项。

这些责任由会话删除 owner 承担，不扩展记忆删除／编辑的职责。

## 8. Hard-cut 删除清单

实现本设计时必须一次性删除旧路径：

1. 删除 `sessions.workspace_kind`、`workspace_root`、`workspace_label`，由 canonical `workspaces` 接管；
2. session 创建、恢复、fork、列表和 workspace 校验统一 join `workspaces`；
3. fact 保留现有 `context_id`，新增仅供 FK 的 generated `project_workspace_id`；不新增 `memory_contexts`；
4. 删除记忆项目列表从 `sessions` 聚合项目元数据的 `_PROJECTS` 查询；
5. `memory_facts` 删除 `source_session_id/source_tool_result_id`，改为单列 `created_by_tool_result_id`；
6. `memory_relations` 删除 `owner_session_id/owner_tool_result_id`，改为单列 `created_by_tool_result_id`；
7. 所有 lineage trigger、writer、memory detail、`memory_explain`、删除预览和测试改用新列；
8. 来源查询 hard-cut 为 LEFT JOIN；共享 availability，但分别实现模型工具的同源 visibility 与用户记忆页的 domain 授权投影，不保留用 `null source` 混合 ARCHIVED／遮蔽／删除的旧 wire；
9. 更新 `storage/migrations/manifest.py`、expected catalog 与 runtime grants：新增 `workspaces: SELECT, INSERT, DELETE`，并把 `memory_relations` 收窄为 `SELECT, INSERT, DELETE`；同步更新 schema relation oracle 与 27-table 固定断言；
10. 不保留旧列、可空别名、兼容 view、双写、回填 migration、旧新查询 fallback 或 session-derived project projection；
11. 重建 clean-v0，并按 `AGENTS.md` 核验后重置本地 disposable 生产数据库。

## 9. 实施顺序

1. 修订 baseline：`workspaces`、session workspace FK、fact 的 generated project workspace FK、fact／relation 单列创建来源及 deferred 约束触发器；
2. 改 session authority、fork、summary 和 workspace reader，使 workspace 成为 canonical 父实体；
3. 改 `remember`／`mark_memory_relation` canonical transaction 和 lineage 校验；
4. 改记忆查询、项目列表、来源投影、用户编辑与删除预览；
5. 删除旧 schema／DTO／SQL／测试形状；
6. 重建 clean-v0，运行 schema oracle、Python／前端全量回归及真实 provider 记忆 dogfood；
7. 只有本设计验收通过后，另立会话永久删除 hard-cut 实施规格。

product-relation oracle 由当前 27 张净增 `workspaces` 一张，目标固定为 **28 张**。`memory_contexts` 不进入 schema；`project_workspace_id` 是 `memory_facts` 的 generated column，不计为 relation。公共 migration registry 仍是独立基础设施表，不计入 product relation oracle。本轮不新增 event、subject、guard、live event 或 durable job。

## 10. 验收条件

### 10.1 Schema

- `workspaces` 是 workspace kind／root／label 的唯一 canonical owner；
- project workspace 可同时被多条 session 和多条项目 fact 使用；
- PROJECT fact 的 generated workspace FK 必须引用同 domain 的 canonical project workspace；GLOBAL fact 不产生 workspace FK；
- fact／relation 不含任何 session 来源列；
- fact／relation 只含单个可空 `created_by_tool_result_id`；
- ToolResult ID 继续使用现有全局主键，不新增组合来源列；
- workspace 两条 child FK 与 relation ToolResult child FK 均有按 FK 前导列排列的显式反向索引；fact ToolResult child FK 由其 owner unique partial index 覆盖；
- 不存在 provenance tombstone、source copy、write receipt 或 ToolAttempt owner。
- product relation oracle 精确为 28，唯一净增 relation 是 `workspaces`。

### 10.2 写入

- 无 ToolResult、失败 ToolResult、错误工具名、错误 domain／project context 均不能创建 fact／relation；
- 一次 `remember` 的 SAVED fact 与其 ToolResult exact-join；
- `ALREADY_PRESENT` 不改绑来源；
- related-memory 候选不产生 owner 或关系；
- `BASED_ON` 与创建 source fact 的同一 `remember` ToolResult exact-join；
- 后补关系与成功 `mark_memory_relation` ToolResult exact-join；
- PROJECT relation 的 owner session 必须属于端点 project context；同 domain 的其它项目 session 也不能越权建关系；
- 记忆不直接引用 ToolAttempt 或原始观察 ToolResult。
- fact／relation 可以在 ToolResult 尚未插入的同一 canonical transaction 中先写入，deferred FK／lineage 在 commit 前由最终 ToolResult 闭合。
- 用户编辑已保存正文不会因重新比对原 `remember` statement 而被拒绝。
- relation 除 owner 随 ToolResult 删除执行 `A -> NULL` 外完全不可 UPDATE，runtime 不持有 relation UPDATE 权限。

### 10.3 生命周期与读取

- 来源存在时，fact／relation 可经 ToolResult 间接定位 session；
- 来源 ID 为 NULL 时，只投影“最初保存位置已删除”；
- `memory_explain` 的 availability 与同源 visibility 独立表达；跨 workspace 遮蔽不能伪装成 ARCHIVED 或 DELETED；
- 用户记忆页只按 memory domain 授权：OPEN 来源可导航，ARCHIVED／DELETED 无 locator，不依赖当前会话 workspace；
- fact／relation owner 删除后，`write_tool` 仍由 fact／relation kind 确定性派生，不需要来源墓碑；
- 来源已删除的 fact 仍可检索、解释、编辑和参与关系图；
- owner 已删除的既有 relation 再次标定时，在端点及历史效果一致的前提下返回 `ALREADY_PRESENT` 且不改绑；
- 项目没有任何 session 但仍有项目 fact 时，项目仍出现在记忆页；
- 当前会话切换不改变项目记忆列表；
- 项目列表按 `MAX(fact.updated_at) DESC, workspace.id DESC` 稳定分页；
- 删除最后一条项目 fact 后，该 workspace 立即退出记忆页项目列表；若同时无 session，则在同事务清理 orphan workspace。

### 10.4 连续性与范围

- schema hard-cut 不改变 provider `SYSTEM`／tools 的同 epoch 前缀连续性；
- 本轮不新增 durable event、subject slot、append guard、live event 或 durable job；
- 本轮唯一新增 relation `workspaces` 是 canonical 产品实体，不是执行证明、回执或兼容投影；
- 本轮不提供会话删除 API／UI，也不物理删除任何用户工作目录。

## 11. 实施级验证矩阵

进入代码后必须把下列场景写成定向测试，而不是留给实现者自由解释：

1. 两个 session 对同一 project root 并发首建：由 root 确定性派生的 canonical project label 相同则共享 workspace；project 输入显式携带任意 `display_label` 都在 workspace resolution／UPSERT 前直接拒绝，不能成为赢家；仅 transient workspace 的 first-writer label 在后续 metadata 不同采用者处返回 typed conflict；任何路径都不能由最后写入者覆盖；
2. `remember` 先写 fact／relations、后写 ToolResult 的现有事务顺序能在 deferred FK 下成功；缺少最终 ToolResult 时 commit 必须失败；
3. ToolResult 存在时普通 SQL 不能把 owner 从 A 改为 B、从 NULL 改为 B 或直接清空；owner 真正消失后 FK 置 NULL；
4. 用户编辑 ACTIVE／SUPERSEDED fact 时 owner 保持原值或 NULL，statement／digest／terms 按现有合同更新，不重新匹配原调用正文；
5. relation owner 删除后详情仍显示关系；`SUPERSEDES` 按有向端点、`CONTRADICTS` 按无序端点重复标定并返回 `ALREADY_PRESENT`，端点或 `SUPERSEDES` 历史效果漂移则报完整性冲突；owner 非空和 NULL 两种状态下，修改 relation identity 的 SQL 均失败；
6. 仍有项目 fact、但没有任何 session 时，项目列表仍显示 canonical root／label，并按 fact `updated_at` 稳定分页；
7. 一个 global basis 同时被多个 project workspace 的 facts 依赖时，级联删除锁定所有受影响 workspace；纯 GLOBAL 图增长、同一 project workspace 内新增 dependent fact／relation，以及跨多个 project 的增长，均因完整 workspace／fact／relation 锁集合变化而整事务重试；与并发 `remember`／session create 按冻结集合锁序串行化，不丢 fact、不误删或遗留 orphan workspace、不增加 repair job；
8. 同一 OPEN 跨 workspace 来源在 `memory_explain` 中为 `CROSS_ORIGIN_REDACTED`，在同 domain 用户记忆页中仍可导航；两种投影分别覆盖 OPEN／ARCHIVED／DELETED，非空断链必须报完整性错误；
9. schema manifest、expected catalog、runtime grant 与 relation oracle 精确为 28；`workspaces` 有且仅有 SELECT／INSERT／DELETE，`memory_relations` 无 UPDATE；catalog 精确包含 sessions/workspace、project-fact/workspace、relation/ToolResult 三个新增反向索引和 fact owner unique partial index；不存在 `memory_contexts`、旧 session owner 列或兼容 view；
10. 项目 A session 使用项目 B 的端点 ID 标定关系必须失败；GLOBAL 两端在同 domain 下仍按合同可由任意 workspace session 标定；
11. 真实 provider dogfood 证明原始 ToolResult、related candidates 与创建 ToolResult 三者未被混淆。

若实施发现以上任何边界无法闭合，应先修订本稿；不得用兼容字段、隐式 fallback 或会话删除特例绕过。
