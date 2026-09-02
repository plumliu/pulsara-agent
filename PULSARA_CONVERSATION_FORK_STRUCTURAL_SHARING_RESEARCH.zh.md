# Pulsara 会话 Fork 结构共享轻量调研

> 状态：讨论材料，不是实施规范，不授权修改生产代码或数据库。
>
> 调研日期：2026-09-01
>
> Pulsara 基线：`c828720a246abf1fd57003120c256f6df4a87712`
>
> Codex 参考基线：`fb0781b9eee6d2da741b984bed9dde95834d909d`

## 1. 技术结论

如果 Pulsara 要提供语义严格的会话 Fork，当前最合理的候选方向是：

- 不复制完整 canonical graph；
- 让父子会话逻辑共享一个不可变 canonical history prefix；
- 用精确 fork cut 标识共同前缀的终点；
- 父子从该 cut 之后分别拥有独立 suffix；
- fork cut 因而自然成为两个分支的最近公共祖先（lowest common ancestor，LCA）；
- 历史事实可以被多个逻辑会话读取，但每条物理 canonical row 仍只有一个 owner；
- writer、turn、queue、interaction、subagent、reservation 与 tool execution authority 不随历史共享。

Codex 当前的持久化 Paginated 会话已经采用这一模式。旧 Legacy 路径仍然复制历史，但现代持久会话会保存精确 `history_base`，让 child rollout 只持久化自己的 local suffix。

对 Pulsara 而言，这不是小功能：一次性工程成本预计为中高，主要成本来自把现有单 `session_id` reader 改造成 lineage-aware reader，以及保持 compaction binding、provider replay、pagination 和 ownership 的精确性。fork 创建本身和长期存储成本则会较低。

本文仅固定调研结论、证据和待讨论问题；目前不建议开始实现。

## 2. 调研范围与术语

本文讨论的 Fork 是“从既有会话的精确历史边界创建一个可独立继续的新会话”，不是：

- 用一段自然语言摘要启动普通新会话；
- 复制或创建 Git branch/worktree；
- subagent context injection；
- 将父会话当前的 live execution 转移给 child。

本文使用以下术语：

- **物理 owner**：canonical row 实际所属的 session。
- **逻辑历史**：一次会话读取时，由一个或多个 owner 的有界 segment 组合出的有序历史。
- **fork cut**：父会话中最后一个被 child 继承的精确 canonical 边界。
- **prefix**：截至 fork cut 的不可变历史。
- **suffix**：fork cut 之后由某个分支独立追加的历史。
- **LCA**：两个分支最后共同拥有的历史位置；对于直接父子 fork，就是 fork cut。

## 3. Codex 的实际实现

### 3.1 Paginated 与 Legacy 是两条不同路径

Codex 的 `ThreadHistoryMode` 同时保留 `Legacy` 和 `Paginated`。TUI 对非 ephemeral 持久会话明确请求 `Paginated`。

`thread/fork` 根据源会话模式分流：

- Paginated 源会话调用 `prepare_fork`，生成 reference-backed fork；
- Legacy 源会话加载、截断完整 history，使用 `ForkPersistence::Copied`。

因此，Codex 文档中笼统的“copy history”不能用于判断现代 Paginated 路径的物理存储行为。

### 3.2 共享边界是精确物理位置

Paginated fork 的 `HistoryPosition` 包含：

- source rollout ID；
- exclusive end ordinal；
- 最后一条已继承 JSONL record 后的 byte offset。

这不是摘要、内容 hash 或模糊 turn 数量，而是指向 immutable rollout prefix 的精确物理 cut。`prepare_fork` 在签发该 cut 前会持有 source lifecycle reservation、持久化当前 source、解析完整 lineage，并物化 fork boundary 所需的投影。

### 3.3 Child 只写 suffix，reader 组合 lineage

Reference-backed child 会在 session metadata 中写入 `history_base`。初始化模型上下文时，Codex 可以读取完整逻辑历史；但真正持久化 child rollout 前，会移除 inherited items，只保留 child-local settings、必要的 synthetic boundary，以及之后产生的新记录。

SQLite 中的 `thread_turns` 和 `thread_items` 以 rollout ID 为 owner。child reader 先解析有序 lineage segments，再按每个 segment 的 rollout ID 和 ordinal range 查询。因此：

- ancestor rows 不会被重新插入为 child rows；
- child 的逻辑历史仍能分页读取 ancestor rows；
- nested fork 继续形成 segment lineage；
- 新 segment 可以遮蔽更早 segment 中同一逻辑 turn 的旧投影。

Codex 测试直接验证：child rollout 文件不包含父会话消息，但 child 的下一次 provider request 仍包含该消息。

### 3.4 引用带来保留约束

Codex 在 hard delete 前扫描 rollout reference index。如果仍有外部 fork 引用源 rollout，删除会被拒绝。该约束是结构共享的必要组成，而不是可选清理策略。

## 4. Pulsara 候选模型

### 4.1 Fork cut 作为最近公共祖先

一个最小的逻辑模型如下：

```text
S0: [shared prefix 1..C]
                    ├── S0 suffix: [C+1 .. A]
                    └── S1 suffix: [C+1 .. B]

S0 与 S1 的 LCA = cut C
logical_history(S1) = prefix(S0, C) + local_suffix(S1)
```

如果 `S1` 在 `D` 再 fork 出 `S2`，`S2` 可以引用 `S1@D`；reader 递归解析后得到有序、无重叠的物理 segments。实现不应引入任意 lineage depth cap，而应使用分页、环检测和确定性的 segment 解析。

概念上，每个 fork 至少需要记录：

- child session identity；
- immediate parent session identity；
- 精确 source entry cut；
- 与该 cut 对齐的 terminal turn/binding identity；
- 创建时所需的 workspace 与 lineage 一致性事实。

这些字段只是讨论模型，不是最终 DDL。是否保存在 `sessions`、独立单行 relation，或使用更一般的 history segment relation，需要在实施规范中决定。

### 4.2 共享与隔离边界

| 类别 | 候选行为 |
|---|---|
| 已完成 transcript entries 与 terminal turns | 逻辑共享，保留原物理 owner |
| assistant blocks 与兼容的 exact replay fragments | 随 inherited entry 读取，不复制 |
| 已结算 tool results | 可作为历史事实读取，但不转移 execution authority |
| adopted compaction snapshot/binding | 只能继承 cut 处已证明的 immutable binding；表示方式待定 |
| child 新 canonical entries | 只写 child，首个逻辑 sequence 为 `cut + 1` |
| session row、writer generation 与 lease | child 独立创建 |
| active turn、prompt queue、interaction 与 plan runtime | 不继承 |
| subagent task、reservation、borrow、handle | 不继承 |
| tool attempt 与 remote idempotency authority | 不继承；历史事实与重试授权必须分离 |
| agent events 与 session commands | 不复制为 child history |
| blobs | 可以继续复用已有 immutable workspace content identity |

### 4.3 Fork 是新的 cold epoch

Fork 创建新 session，因此可以被定义为新的 cold epoch，在 fork genesis 重建一次 provider-input root。该例外不应扩散到 epoch 内部：child 安装首个 provider prefix 后，仍必须保持现有 prefix continuity。

Inherited provider replay fragment 仍须通过完整 target/profile/wire API/replay compatibility join。Fork 不能把“新 cold epoch”解释为可以无条件复用不兼容的 provider-native payload。

## 5. 当前 Pulsara 的适配成本

当前 schema 和 reader 以单 session owner 为基本假设：

- `transcript_entries`、`turns`、`assistant_message_blocks`、provider replay 与 tool result 关系均携带 session-scoped FK；
- canonical page reader 使用单一 `WHERE e.session_id = %s`；
- context snapshots 和 turn context binding 也绑定具体 session；
- session allocator 假设本 session 的 `latest_entry_sequence` 与物理 rows 同步增长。

因此结构共享的主要工作不是创建 fork row，而是建立唯一的 lineage resolution/read path，并让以下消费者统一使用它：

1. canonical transcript 与 turn pagination；
2. provider-input canonical compile/read；
3. replay selection 与 exact hydration；
4. compaction source、successor 与 adopted snapshot/binding；
5. UI 会话恢复和历史分页；
6. tool-result closure 与历史展示；
7. fork、archive 和 hard-delete 的事务约束；
8. nested fork 的 cursor、ordering 与 ownership 测试。

实现成本为中高，但运行特征较好：fork 创建不随历史长度复制整张 canonical graph，新增存储主要等于 child suffix；读取成本随 lineage segment 数量增加，可通过统一的 segment planner 和分页控制，而不必设置隐蔽的 lifetime cap。

## 6. 可降低首版成本的产品边界

如果后续决定实施，首版可以先限定为：

- 只能从 completed 或 interrupted 的 root turn terminal boundary fork；
- active turn 只能 fork 到其开始之前，暂不支持 mid-turn freeze；
- parent 与 child 必须位于相同 workspace 和 memory domain；
- 不继承 prompt queue、interaction、plan runtime、subagent runtime 或任何 live authority；
- 默认继承 fork cut 处有效的 provider/config 选择，显式 override 仍需通过 compatibility admission；
- source 可 archive，但有 descendant reference 时不可 hard delete；
- 不提供 detach/materialize history；
- 不提供跨 workspace fork；
- 所有读取必须经同一个 typed lineage resolver，禁止调用方自行拼接 SQL。

这些边界可以减少竞态和 ownership 面积，同时保留结构共享方案最重要的长期性质。

## 7. 未决问题

在写实施规范前，至少还需要讨论：

1. UI 允许从 assistant terminal message、任意 terminal turn，还是只从当前最新 terminal turn fork？
2. source 正在运行时，是拒绝、截到 active turn 之前，还是像 Codex 一样生成 child-local interrupted boundary？
3. child 是否必须继承 parent 的 provider target/profile，允许哪些显式 override？
4. cut 处 adopted compaction snapshot/binding 是直接跨 owner 引用，还是创建 child-local immutable alias？
5. child 的 title、project/workspace metadata、permission defaults 和 memory domain 如何继承？
6. inherited canonical row ID 在 child API 中是否保持原 ID，客户端是否已经假设 ID 必然属于当前 session？
7. pagination cursor 应编码 logical session 与 lineage position，还是物理 owner segment 与 ordinal？
8. archive、delete、export 和未来 detach 的完整引用生命周期是什么？
9. fork-of-fork 是否始终引用 immediate parent，还是在写入时规范化为更少的 physical segments？
10. 是否需要在 fork UI 中显式展示 parent、fork point 和分支关系？

## 8. 备选方案与当前判断

### 完整复制 canonical graph

短期 reader 改动可能较少，但需要复制并重映射 turn、entry、block、replay、tool result、snapshot/binding 等关系，同时产生两份可漂移的 canonical truth。当前不推荐。

### 摘要启动新会话

成本最低，但不能保持 exact transcript、provider replay 和 canonical identity，因此只能作为“从摘要继续”的独立产品能力，不能称为严格 Fork。

### 共享 immutable prefix

一次性 reader 与 schema 改造较大，但 fork 创建、长期存储、canonical ownership 和多分支一致性最好。当前将其记录为首选候选方案，等待进一步产品与架构讨论。

## 9. 建议的下一步

目前保持不实施。下一轮讨论应优先回答第 7 节中的 boundary、compaction binding、provider compatibility 和 deletion 四组问题。

只有这些问题形成明确产品决定后，才应编写权威 hard-cut implementation spec、列出完整文件清单与测试矩阵，并开始 schema 和生产代码修改。

## 10. 证据索引

Codex 本地源码基线 `fb0781b9eee6d2da741b984bed9dde95834d909d`：

- `codex-rs/tui/src/app_server_session.rs`：持久 TUI 会话选择 Paginated history。
- `codex-rs/app-server/src/request_processors/thread_processor.rs`：Paginated 与 Legacy fork 分流。
- `codex-rs/thread-store/src/local/paginated_fork.rs`：source reservation、lineage materialization 与 exact boundary。
- `codex-rs/protocol/src/protocol.rs`：`HistoryPosition` 精确位置定义。
- `codex-rs/core/src/thread_manager.rs`：`Copied` 与 `Referenced` persistence。
- `codex-rs/core/src/session/session.rs`、`codex-rs/core/src/session/mod.rs`：写入 `history_base` 并删除 inherited persistence items。
- `codex-rs/thread-store/src/local/rollout_lineage.rs`：有界 immutable lineage segments。
- `codex-rs/thread-store/src/local/thread_history/segment_paging.rs`：跨 segment relational projection reads。
- `codex-rs/thread-store/src/local/delete_thread.rs`：被引用 source 的 hard-delete guard。
- `codex-rs/app-server/tests/suite/v2/thread_fork.rs`：child file 不复制父消息而 provider input 保留父消息。

Pulsara 本地证据：

- [`0000_conversation_kernel_baseline.sql`](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)：当前 session-scoped canonical schema 与 FK。
- [`query.py`](src/pulsara_agent/conversation_kernel/query.py)：当前单 session canonical pagination。
- [`reader.py`](src/pulsara_agent/conversation_kernel/reader.py)：canonical provider-context rematerialization。
- [`contracts.py`](src/pulsara_agent/conversation_kernel/contracts.py)：现有 exact canonical session snapshot cuts。

## 11. 方法与局限

本报告基于上述两个本地 checkout 的静态源码、schema 和测试审阅，没有执行性能 benchmark，也没有对 fork 方案进行生产实现或 PostgreSQL prototype。工程成本判断是架构范围判断，不是排期估算。

Codex 的 durable rollout 与 Pulsara 的 canonical PostgreSQL relations 并非同一种存储模型；本文借鉴的是 immutable prefix reference、exact cut、lineage reads 和 deletion guard，而不是照搬 Codex 的 JSONL/SQLite 结构。
