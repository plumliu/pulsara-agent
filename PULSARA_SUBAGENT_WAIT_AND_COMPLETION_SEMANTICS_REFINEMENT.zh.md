# Pulsara 子任务等待、完成交付与可见性：产品与内核修订方案

> 状态：**IMPLEMENTED, REACTIVATION PENDING — 2026-09-12 修正任务清单合并时的 ROOT 来源关系投影；原 activation 保留于第 16 节，修订后验证见第 17 节**
>
> 日期：2026-09-11
>
> 本文整理两份用户截图、Luna max 对本地 Codex 的只读调研、后续独立源码审查和产品讨论，并在同一文档中冻结、实施和验收 subagent wait/completion hard cut。第 16 节是原 activation 记录，不代替第 17 节修订后的验收。
>
> 2026-09-11 已完成生产代码、clean-v0 schema/协议、前端、测试、真实 provider/browser 与 isolated wheel 的同次 hard cut；没有保留旧字段、旧 outcome 或兼容路径。

## 0. 结论与修订范围

Pulsara 已经具备异步子任务执行、process-local completion inbox、可被 steer 唤醒的异步 wait、批量接纳和 ROOT 结束前的 completion seal。问题不是缺少一套调度器，而是现有等待条件、完成来源交接与界面表达还不够一致。

本轮收敛到以下方向：

1. `spawn_agent` / `create_agent_tasks` 创建任务，不隐含 join。ROOT 有独立工作时继续工作；本次回答依赖子任务结果时，由模型显式等待。
2. `wait_agent(task_ids, settle=first|all)` 使用严格目标谓词。部分成功、部分失败和无关 completion 均不结束 `all`；不暗藏 fail-fast。
3. 等待条件满足与结果运输分开。成功返回 join 前，必须通过 `KernelSubagentManager` 的 turn-bound readiness 方法接管来源到现有 inbox；wait 工具闭合后，才由下一合法 safe point 接纳新的 completion entry。不存在 safe-point 直接持有另一套 ready 来源的替代路径。
4. 子任务完成、当前 owner 排队、canonical 接纳、exact provider request 纳入是独立事实，不合成单一“已收到”状态。**本轮展示止于 canonical 接纳，不实现 subagent provider-included badge 或其 process-local projection。**
5. UI 在原任务组/节点更新完成事实；后来接纳的 canonical 消息仍位于实际接纳轮次，不重写已结束 ROOT 的历史。
6. 当前 ROOT 的 steer 可以中断 wait；下一轮排队输入不能。ROOT STOP、Host close 与取消按现有身份和结算规则处理。
7. idle session 不因普通 completion 自动创建 ROOT；当前 Host 保留的 completion 可以在下一次人工输入时接纳。保留现有“用这份结果继续”入口，不扩张其控制语义。
8. 复用现有 owner、safe point、结果存储、工具结算、权限及 provider continuity。不新增 durable scheduler、delivery queue、receipt、恢复图或 fingerprint。
9. 同次将 `completion_delivered` / `completionDelivered` hard-cut 为 `completion_accepted` / `completionAccepted`；wait 成功结果仅保留 §6.5 的五种 exact outcome，删除泛化 `input_available`。

## 1. 证据、基线与文档权威

### 1.1 证据等级

全文使用三种判断，实施与复盘时也应保留这种区分：

- **源码事实**：已从当前生产路径直接核验；不等于已经跑过本次验收。
- **合理推断／待复现风险**：源码允许某个交错或行为解释，但未证明它就是用户案例的根因。
- **设计决定／建议**：要改变或补齐的目标合同，不能倒写成现有实现。

证据优先级：当前源码与 schema、可定位的执行证据、当前有效规格、外部调研及截图推断。模型或调研报告的结论不能覆盖代码事实。

### 1.2 当前基线

- Pulsara：本地仓库 `pulsara_agent`，Git HEAD `094e5063`；审查时存在其他工作产生的未提交修改，因此本文描述的是**当时工作树**，不把全部事实归给该 commit。
- Codex：本地同级仓库 `codex`，此次复核 HEAD `e7637306bc`。Git 标识只用于代码版本定位，不生成文件、文档或证据摘要。
- 未重新查询案例数据库，因此 18:13:01、18:13:05、18:28 的精确时刻来自用户提供的时间线。
- 未据此断言截图中的 Codex 应用二进制与本地 checkout 完全一致。
- 本文初稿编写阶段没有执行 pytest、前端测试或真实 provider 调用；该陈述只描述 Phase A 基线，不覆盖第 16 节的实施后验收记录。

### 1.3 与现有规格的关系

相关文档：

- [子代理异步完成交付与 Late-Join 现行规格](PULSARA_SUBAGENT_ASYNC_COMPLETION_HARD_CUT_RESEARCH_AND_IMPLEMENTATION_SPEC.zh.md)
- [PR03 用户停止与后台命令产品合同](PULSARA_BROWSER_DOGFOOD_PR03_KERNEL_USER_STOP_AND_BACKGROUND_COMMAND_PRODUCT_CONTRACT.zh)
- [Round 10 子任务编排规格](ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md)
- [浏览器复盘与修复索引](PULSARA_BROWSER_DOGFOOD_REVIEW_AND_FIX_PLAN_2026-09-08.zh.md)

本文现为已激活的生产合同；下表差异已在本次 hard cut 中同次收敛：

| 现有条款 | 需要收敛的差异 |
| --- | --- |
| 异步完成规格 §10.4 | 删除带 targets 时“任意 pending completion 都立即返回”的规则；明确严格 first/all 和 exact steer |
| 异步完成规格 §8、§11 | 补齐显式 join 的 terminal→completion-ready 交接，保留工具闭合、safe point 和 seal |
| 异步完成规格 §12 | 删除“已收到／已用于对话”对请求纳入的含混表达；补充当前／上一／更早 ROOT 的来源关系；同步 `completion_accepted` 命名，本轮不做请求纳入展示 |
| PR03 §17.2、§18.5 | 保留既有结果继续入口与任务图；仅同步完成状态、来源和文案，不删除控制能力 |
| 复盘索引 | 增加本次修订入口与真实状态；不重置已完成修复，也不提前宣称激活 |

PR03 关于 idle 人工终止后台命令的 `UI-only、不自动补投` 规则保持不变；它不是普通 subagent completion 的规则。

## 2. 两份截图揭示的问题

### 2.1 图 1：旧 reader 的完成与新一轮输入混在一起

用户提供的时间线：

| 时刻 | 事实或陈述来源 | 应如何理解 |
| --- | --- | --- |
| 18:13:01 | 用户给出的运行记录 | 原 ROOT 正常结束 |
| 18:13:05 | 用户给出的运行记录 | reader 子任务后来完成 |
| 18:28 | 用户消息及截图 | 新输入要求创建五个子任务；旧 reader completion 随新 ROOT 接纳 |
| 18:28 的紫色提示 | 截图与当前渲染实现 | 提示出现的时刻对应 entry 接纳，不等于 reader 刚完成 |

截图文案为“Pulsara 已收到子任务进展，主任务会结合这项工作的结果继续处理”。它没有显示来源任务，位置又紧邻新建五个子任务的用户输入，容易让用户误判完成时间和所属任务组。

**判断：** 旧 completion 在新 ROOT 接纳可以符合当前合同；把该接纳表现成来源不明的“刚收到进展／已经用于处理”是展示问题。不能为了修 UI，把 18:28 的 canonical entry 插回 18:13。

原始附件：[图 1](</var/folders/2y/j7tf9q5s4fg4q5sfkdp_ktc00000gn/T/codex-clipboard-04a542f7-e415-4526-850d-8cded517787b.png>)。附件位于用户机器临时目录，可能失效；以上文字保留必要案例，不依赖附件持续可访问。

### 2.2 图 2：真正等待不等于主模型持续输出

截图及用户描述中，Codex 主任务调用 `wait_agent` 后保持运行，子代理继续执行；用户新输入到达后，wait 返回“被新输入打断”，主任务继续。

需要借鉴的体验是：

- 当前逻辑运行保持活动，工具 future 异步挂起；
- session 输入处理仍可工作，子代理不被挂起；
- 等待期间不靠反复请求模型维持“运行中”；
- exact steer 可以恢复同一逻辑回合。

静态截图本身不能证明线程调度、数据库顺序或所有中断路径；内部机制需要源码和执行证据支持。

原始附件：[图 2](</var/folders/2y/j7tf9q5s4fg4q5sfkdp_ktc00000gn/T/codex-clipboard-f5791ff2-ecbe-42bd-a3f5-0091a824b2f4.png>)，同样是可能失效的临时文件。

## 3. 当前 Pulsara 源码事实与问题分类

### 3.1 已有机制，不重做

| 事实 | 当前真源／定位符号 |
| --- | --- |
| wait 是 Condition/revision 驱动的异步等待，不是定时忙轮询 | [subagent.py](src/pulsara_agent/conversation_kernel/subagent.py)：`KernelSubagentManager._wait` |
| 已有 first/all、可选 targets、默认 30 秒和单次最大 300 秒 | [builtin_catalog.py](src/pulsara_agent/capability/builtin_catalog.py)：`wait_agent` descriptor |
| ROOT completion inbox 为 task-id-only FIFO＋去重 set，属于当前 HostSession | [subagent.py](src/pulsara_agent/conversation_kernel/subagent.py)：`offer_subagent_completion`、`snapshot_pending_root_completions` |
| completion 在普通 provider preparation 批量接纳，不由 wait 返回正文 | [provider_dispatch.py](src/pulsara_agent/conversation_kernel/provider_dispatch.py)：`_drain_root_completion_suffix` |
| ROOT final 前已有 offer/seal/settlement 协调 | [runner.py](src/pulsara_agent/conversation_kernel/runner.py)：`seal_root_completion_delivery` 调用；[subagent.py](src/pulsara_agent/conversation_kernel/subagent.py)：对应 phase 方法 |
| canonical completion entry 带来源 task ID，接纳与模型实际使用并非一个事实 | [completions.py](src/pulsara_agent/conversation_kernel/_repository/completions.py)：`accept_subagent_completion_into_root` |
| 普通结束清理 exact ROOT slot，不等于关闭整个子任务 owner | [host.py](src/pulsara_agent/conversation_kernel/host.py)：`_settle_active_root_task`、`_clear_active_root_locked` |
| Host close 可丢弃 inbox；task/result 仍由 canonical rows 查询 | [subagent.py](src/pulsara_agent/conversation_kernel/subagent.py)：`aclose`；[subagents.py](src/pulsara_agent/conversation_kernel/_repository/subagents.py)：任务查询 |

### 3.2 已确认：严格 join 被通用输入条件打断

当前 `_wait` 在目标谓词未满足时，只要 `_root_completion_queue` 非空就返回 `input_available`。该 completion 可以来自某个已完成目标，也可以来自完全无关的任务。

因此 `all(A,B,C)` 可能在 A 完成时返回，模型之后反复 wait。调长 timeout 不能解决这种提前返回。现行规格同时规定“join 谓词”和“任意 pending completion 立即返回”，不能只要求 coding agent 修代码而不修合同。

### 3.3 已确认：接纳事实被展示成已用于处理

[runtime-adapter.ts](frontend/lib/runtime-adapter.ts) 的 `projectEntries` 将带 source task 的 `INTER_AGENT_MESSAGE` 转为 `subagent-completion`，时间取 `accepted_at_utc`；[workbench-view.tsx](frontend/components/workbench-view.tsx) 的 `UserMessage` 使用泛化紫色提示及“已把这项工作的进展用于当前处理”的 tooltip。

当前 `completion_delivered` 主要由 `accepted_root_entry_id` 是否存在推导。这证明 canonical 接纳，不证明某个最终请求包含了该 entry，更不证明模型读懂或使用了结果。

### 3.4 待精确复现：terminal→inbox 窗口

源码允许以下交错：

1. child canonical task/result 已提交；
2. ROOT 的 targeted wait 查询到 terminal，返回谓词满足；
3. child 尚未走完结果确认和 `offer_subagent_completion`；
4. ROOT 下一 preparation 暂时没有该来源。

定位：[subagent.py](src/pulsara_agent/conversation_kernel/subagent.py) 的 `_run_child`、`finish_completion`、`_wait`，以及 [provider_dispatch.py](src/pulsara_agent/conversation_kernel/provider_dispatch.py) 的 inbox-only drain。

可能后果是空转一次请求；是否会进一步跨过 ROOT 结束边界，需要暂停 offer 的确定性测试。**本文不把该窗口写成图 1 的已确认根因。**

### 3.5 不应误判为内核缺陷的行为

- 未显式 wait 的 ROOT 在 child 之前结束，本身不违反 create-only/no-join 合同。
- 已结束 ROOT 不自动复活，是当前产品边界，不等于结果丢失。
- 若用户要求“等全部结果后汇总”，模型却未 wait 就 final，是需要修指导与验证的编排行为失败；不能通过“只要有任何活跃 child 就不许结束”普遍兜底。
- 连续 wait 并非一律错误：first 后处理结果、用户 steer 后重新选择目标、合法 timeout 后重新决策都合理。需要消除的是部分 completion 无意义打断 all、短轮询和结果交接遗漏造成的额外模型回合。

## 4. Luna 调研与 Codex 的可借鉴边界

### 4.1 已独立核验的核心结论

Luna max 报告是证据输入，不是 Pulsara 的实现规格。以下结论经本地关键路径核验：

| 结论 | Codex 本地来源 |
| --- | --- |
| V2 wait 等 mailbox/steer activity 或 timeout，返回摘要而非子任务正文 | `codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs`：`wait_for_activity`、`WaitAgentResult` |
| 订阅时先建立 watch receiver，再检查已经 pending 的 steer/mailbox | `codex-rs/core/src/session/input_queue.rs`：`subscribe_activity` |
| child 完成的 UI activity 可携带原 parent turn，模型 communication 使用 `trigger_turn=false` | `codex-rs/core/src/session/mod.rs`：完成 activity 与 `InterAgentCommunication::new` 路径 |
| regular task 与 session 输入处理分离，等待不要求持续模型调用 | `codex-rs/core/src/tasks/regular.rs`、`session/handlers.rs` |
| idle 自动启动受 trigger-turn 或独立 sleep 标记约束，不是任何 completion 都启动 | `codex-rs/core/src/tasks/mod.rs`：`maybe_start_turn_for_pending_work_with_sub_id` |

路径相对于 `/Users/plumliu/Desktop/python_workspace/codex`。Luna 报告中其余测试、提交历史等描述未在本次全部重新验证，不应把报告的每条内容标成此次审查结论。

### 4.2 不照搬的两点

**V2 activity wait 不是 group/all。** A、B、C 分别在 5、20、40 秒结束，任意 mailbox 更新就返回会让模型在 5 秒醒来再决定是否等待；严格 all 则可继续挂起到全部终态。内部 condition 被通知、重新检查，与工具完成并触发下一模型请求是两回事。

**普通异步 wait 不需要 durable sleep。** Codex 的 `has_outstanding_durable_sleep` 检查扩展 `SleepItem`，允许某些 idle session 被 queue-only 消息启动新 turn。这是额外的唤醒资格，不是延长普通 wait，也不能仅凭名称推断所有部署具有同样的崩溃恢复保证。

Pulsara 本轮只需要“活动 ROOT 挂起工具并恢复”，不引入 idle 自动续跑、durable sleep、代理复活、多层任意消息或新持久调度器。V1 target wait 与 V2 mailbox wait 的差异也不能混写。

## 5. 独立事实模型与 owner

### 5.1 ROOT 与 session

```text
ROOT A：RUNNING
          ├─ 正常生成／准备请求／执行工具
          ├─ WAITING_TOOL（派生运行状态）
          └─ 停止与结算中（仍未确认终态）
       → TERMINAL

Session：当前有活动 ROOT／当前 idle
         idle 不等于 session 关闭，也不等于历史 ROOT 可复活
         新人工输入或既有显式继续入口 → 新 ROOT B
```

这些是概念层次，不要求新增数据库枚举。STOP 接纳不等于工具和 effect 已结算；不能提前把 ROOT 标终态。

### 5.2 子任务保持现有完整状态

保留 `PENDING_START`、`WAITING_DEPENDENCY`、`ACTIVE`，以及 terminal 的 `COMPLETED`、`FAILED`、`CANCELLED`、`INTERRUPTED`、`BLOCKED_DEPENDENCY_FAILED`。

不为图示把排队／依赖等待都压成 RUNNING，也不把 Host interruption 改成用户取消。终态由 [SubagentTaskStatus](src/pulsara_agent/conversation_kernel/subagents/contracts.py) 判断；all 是全部终态，不是全部成功。

### 5.3 完成相关信息不是一条必经状态链

| 独立事实 | 真源与必要身份 | 丢失或变化边界 |
| --- | --- | --- |
| 终态结果／说明已存在 | canonical task/result，task ID，真实 terminal time | 不依赖 UI 或 inbox 存在 |
| 当前 owner 持有待接纳项 | HostSession-local inbox 与 exact owner/session | 可退休、可在 Host 丢失时消失；未知不等于未完成 |
| 已接纳到主对话 | source task 对应的 canonical entry、目标 turn、sequence、接纳时间 | 保留为历史事实，不因 ROOT 结束或 transport 失败删除 |
| 某请求实际包含对应来源 | 概念上必须由 exact compiled-request 内容／placement 与已有请求身份证明 | **仅保留语义边界；本轮不新增 subagent 的运行时投影或 UI** |

`READY → QUEUED → ACCEPTED → INCLUDED` 只能表示常见发生顺序，不能成为唯一状态机。历史结果可以经过手动入口接纳，当前 owner 队列可以丢失。本轮只实现／维护前三类事实的展示；第四类用于约束措辞，不转化为新的 completion request observation 需求。

对外不必同时显示四行；内部必须分清它们。当前 task 完成也不等于它启动的所有后台进程都停止，不承诺回滚或消除外部副作用。

### 5.4 复用的 owner 边界

| Owner | 本轮职责 | 不承担的职责 |
| --- | --- | --- |
| `KernelSubagentManager` | 目标谓词、唯一 turn-bound readiness 接管、现有 FIFO/set、condition/revision、offer/seal | 新 durable mailbox、全历史扫描、独立调度器 |
| `HostSession` | exact ROOT/session/owner、steer ingress、STOP、生命周期、原手动继续入口 | 第二份 task truth、前端猜测目标 |
| runner／tool settlement | wait future 与工具闭合、取消和 effect 结算、ROOT 收尾 | wait 内偷偷写 completion entry |
| provider dispatch／safe point | 只从 manager 现有 inbox 批量接纳、固定输入顺序、预算与 compaction、最终输入 | 第二份 ready 来源、join 专属投递器、未闭合工具组中的旁路 writer |
| repository | task/result、来源唯一的 completion entry、现有事务与确认 | 新 receipt、重启 delivery replay |
| UI adapter／任务图 | 来源分组、终态时间、当前 owner 排队与 canonical 接纳投影 | provider-included 展示或推断、根据文案／工具 JSON 猜执行状态 |

“分开建模”表示所有权和事实分开，不要求按每个概念新建类、表或 registry。

## 6. 严格等待与输入中断合同

### 6.1 创建与等待

- `create_agent_tasks` 只创建明确 batch，并复用现有依赖调度；`spawn_agent` 是同一底层语义的单任务入口。
- 创建返回稳定身份，不隐含 join、不自动安装 waiter。
- ROOT 有可做的独立工作时继续；只有当前答复确实依赖未完成目标时才调用 wait。
- 内核不能通过“是否有任何活跃 child”自动阻止 ROOT 正常结束。已有 pending completion 的封口协调不是等待所有活跃 child。

### 6.2 保留单一工具表面

沿用 `wait_agent`，不恢复 `wait_agent_tasks`，不并存 `first` 与 `any` 同义别名：

```json
{
  "task_ids": ["task:A", "task:B", "task:C"],
  "settle": "all",
  "timeout_seconds": 120
}
```

示例 ID 仅为说明，实际调用必须使用创建返回的 exact IDs。保留现有 1..16 targets、去重／session 校验、单次 0..300 秒、默认 30 秒；这些不是任务寿命或累计等待上限。本轮不因“长任务”自行扩大或新增总上限。

不传 targets 的 wait 是当前 ROOT 可处理输入的 activity wait；没有 pending 输入且没有 nonterminal child 时，保留立即返回 `nothing_pending` 的能力。不是无限等未来可能创建的工作。

### 6.3 目标谓词

- `first`：指定集合中任意任务 terminal；已经 terminal 的目标也满足，不能要求必须在订阅之后新完成。
- `all`：指定集合全部 terminal；某项失败、取消或依赖失败不单独结束等待。
- `first` 返回当次判定已观察到的 satisfied/pending IDs，不把“first”解释成精确墙钟上最早完成的唯一任务。
- 再次对同一已终态目标调用 first 会立即满足，这是 level-triggered 合同。指导模型调整待等集合或使用 all，不新增“曾见完成”持久游标来掩盖错误编排。
- 待等目标冻结于本次调用；未来新任务和无关组不自动加入。现有 batch IDs 列表已足以表达 group/all，本轮不必新增 group selector。
- future 收到 condition 通知后可以重新检查再挂起；不能把每次内部 wake 都变成 ToolResult 或 provider 请求。

### 6.4 中断矩阵

| 事件 | 带 targets 的 first/all | 无 targets 的普通 wait |
| --- | --- | --- |
| 已接纳、指向当前 exact ROOT 的 steer | 中断等待，优先处理 steer | 唤醒 |
| `NEW_TURN` 下一轮排队输入 | 不打断，保留原队列身份 | 不打断 |
| 无关子任务 completion | 不打断 | 当前可处理时唤醒 |
| 指定目标部分成功 | first 满足；all 继续 | 可处理 completion 唤醒 |
| 指定目标部分失败／取消／中断／依赖失败 | first 满足；all 继续 | 可处理 completion 唤醒 |
| 全部目标 terminal | all 满足，执行来源交接 | 按 pending 输入判断 |
| 当前 ROOT STOP／当前等待执行被取消 | 按现有取消与工具结算退出，不伪装谓词成功 | 同左 |
| Host close／owner 丢失 | 取消／不可用的真实结论，不改投新 owner | 同左 |
| timeout | 结束本次等待，不取消 child | 同左 |

“取消目标 child”与“取消 ROOT/等待”不同。前者只改变目标终态，不能成为 all 的隐藏 fail-fast。

F03 的 command、queue item、delivery mode、target turn 身份必须保留。不能把任意文本输入都解释成 steer，不能把迟到 STOP 或唤醒投给新 ROOT。

当前 ordinary completion 通道是 terminal outcome；本轮不把“可处理 mailbox”扩张成任意 child 进度、日志或任意事件。terminal monitor 与人工控制反馈仍遵守各自既有通道，不因本矩阵而悄悄改其唤醒规则。

### 6.5 返回与同时到达

成功完成 wait 工具调用的正文统一为以下结构，不运输结果正文：

```json
{
  "outcome": "predicate_satisfied",
  "satisfied_task_ids": ["task:A", "task:B"],
  "pending_task_ids": []
}
```

`outcome` 的封闭取值现在冻结，不留给 Phase A 二次选择：

| outcome | 合法条件 |
| --- | --- |
| `predicate_satisfied` | 仅带 targets；first/all 谓词满足，且 §7 readiness 接管返回 READY |
| `steer_available` | 当前 exact ROOT 存在可处理的已接纳 steer；两种 wait 模式均适用 |
| `completion_available` | **仅无 targets**；现有 inbox 有当前 ROOT 可处理的 completion |
| `nothing_pending` | **仅无 targets**；没有 pending 输入，也没有 nonterminal child |
| `timeout` | 本次 deadline 已到，最终判定没有选中更高优先级的合法返回条件 |

- targeted wait 的两个 ID 数组按原调用 targets 顺序分区，去重且覆盖原集合；它们只表达本次已观察到的 terminal/nonterminal，不代表 provider 纳入。无 targets 时两个数组均为空。
- 工具外层 SUCCESS 表示同步操作正常返回，不表示全部 child 成功；timeout 和 steer 也不是 child 失败。
- 删除旧 `input_available`，不保留别名、双读或新旧 outcome 协商。descriptor、返回类型与测试同次更新；存量 canonical 原文不为消除旧单词而重写。
- 先遵守现有权限、参数与 owner 校验、真实取消边界，再在正常返回中采用：exact steer → targeted 谓词＋READY／无 targets completion → 无 targets nothing_pending → timeout 的优先级。
- STOP/Host close 走既有取消与结算，不伪造成 SUCCESS＋普通输入；没有真实取消意图时，不能仅因 turn 已 seal 就声称“用户取消”。
- 最后判定时若已观察到 exact steer 与谓词同时满足，优先报告 steer 中断；可以同时保留真实的 satisfied/pending 信息。更晚到达的 steer 仍由原 safe point 和 canonical settlement 接管，不承诺 wait 能预测未来输入。
- deadline 到达后做有限的最终判定，不续期、不无界重查。timeout 只陈述判定边界未观察到满足，不将旧缓存状态称为“此刻仍在运行”。读取失败按既有错误处理，不冒充 timeout 快照。
- 参数无效、权限拒绝、目标不存在、目标 turn 不再开放和 owner 不可用是操作错误，不是“目标已完成”；不增加第六个成功 outcome 来承载这些错误。§7.2 的 readiness 不可用结论走现有工具错误 envelope，真实取消仍保留原取消通道。

### 6.6 attention 明确不在本轮

本轮不实现任何隐藏的“任一失败提前返回 all”。未来若需要 fail-fast／attention，必须单独规定触发范围、重复事实处理、优先级、取消效果与模型指导，不能仅给 all 多加一个分支。

严格 all 可能等到其他目标结束或 timeout 才让模型处理首个失败，这是明确的产品取舍；失败状态仍应及时显示给用户。

## 7. join 完成来源交接与工具闭合

### 7.1 必须成立的不变量

> `wait(first/all)` 以 `predicate_satisfied` 返回时，必须先由当前 `KernelSubagentManager` 的 turn-bound readiness 方法确认 exact ROOT 仍可接纳；本次 satisfied 集合中尚未 canonical accepted 的 terminal outcome 必须已进入该 manager 的现有 `_root_completion_queue` / `_root_completion_set`。后续 safe point 只消费这一个 inbox，不能再依赖 child 将来执行一次 offer 回调。

“数据库里存在结果，所以理论上可构造”不满足该不变量。删除“由 safe-point owner 另持可构造来源”的替代实现；既有 `accepted_root_entry_id` 已证明接纳的目标不用重复入队，但也必须通过本次 turn/owner 校验才可宣称 join 成功。

### 7.2 唯一 readiness 方法与接管算法

在 `KernelSubagentManager` 内新增窄方法 `ensure_root_completion_ready`，只由带 targets 的 `_wait` 在谓词满足分支调用。方法输入冻结为 `session_id`、`root_turn_id`、本次 satisfied 的 `task_ids`；身份来自本次已授权 invocation，而不是重新挑选“当前最新 ROOT”。方法由绑定现有 `HostWriterGuard` 的 manager 执行，复用已有 owner epoch/guard/cancellation 检查，不引入新 token 或 generation。

实现算法：

1. 在 manager 原条件锁下校验 session、原 owner 可用性、`_root_completion_turn_id == root_turn_id`、`_root_completion_delivery_open` 与关闭状态；不得把 B 当成已结束的 A。
2. 通过既有 repository/IO owner 对输入的 exact task IDs 确认 canonical terminal outcome 与 `accepted_root_entry_id`；不信任模型传来的 status/result。所有输入必须属于同一 session；不要求 task 的 origin ROOT 等于本次 ROOT，以保留跨轮次等待。
3. 数据库读取不持有 manager async lock。读取完成后，重新取得同一条件锁，复核原 owner、exact turn 和 delivery-open；检测到 seal、关闭或 takeover 时，不执行本次队列追加，不返回 READY。
4. 已确认 canonical accepted 的目标跳过；尚未接纳且已在 set 中的目标保留原位置；其余 terminal 目标按本次输入顺序追加到**原有** FIFO/set。该追加与 phase 复核处于同一临界区，期间不 await。
5. 必要时推进现有 revision 并通知；返回 READY。不创建新 ready 容器、不复制结果正文、不在此写 canonical entry。

readiness 内部结果固定为三类：

| 内部结果 | 含义与 wait 处理 |
| --- | --- |
| `READY` | 本次 owner/turn 校验通过；未接纳 terminal 目标已由原 inbox 接管，允许正常分支返回 `predicate_satisfied` |
| `TARGET_NOT_OPEN` | exact turn 已 seal、关闭或原 phase 已被替换；经现有错误 envelope 返回目标不可用，不能等未来重新开放后把旧调用算成功 |
| `OWNER_UNAVAILABLE` | manager/Host 已关闭或已发现 guard/owner 不再有效；经现有取消或错误路径退出，不改投新 owner |

无效输入、跨 session/权限冲突、目标缺失、canonical 来源不完整和 IO 失败按现有校验／异常纪律处理，不伪装 READY。外层错误使用已有 `APPLICATION_ERROR`/权限或取消分类；不可用错误正文分别以 `wait_target_not_open`、`wait_owner_unavailable` 作封闭原因，不新增成功 outcome。真实 STOP/cancellation 仍优先执行原工具结算。

这里线性化的是同一 manager 的接管与 seal，不承诺数据库、外部 Host 和进程内锁组成跨进程事务。确认后若 canonical 接纳又发生竞争，原 source-task 唯一约束与 safe-point writer 仍作最终仲裁；不能把陈旧 hint 变成重复 entry。

两类入口共享同一个 FIFO/set，但用途不能混淆：

- 普通 child `offer_subagent_completion(task_id)` 保留 queue-only 语义，允许原 ROOT 已 seal/结束后的 completion 入队等下一 ROOT；其不要求 active turn 是有意边界，不是应一律改掉的漏洞。
- join readiness 必须绑定本次 exact ROOT 并检查 open，**不能**只调用无 turn 检查的普通 offer 后直接返回成功。
- 不在已持有条件锁时再次 await 会获取同一锁的普通 offer；可在原 manager 内复用极小的 locked enqueue 代码，不另设 owner。

其他限制：

- 只针对本次明确目标，不扫描全历史、不回放整个 session 的 undelivered tasks。
- 已经 canonical accepted 的来源不重复写 entry；历史结果仍走现有 reader/compaction 合同，不强行重新注入正文。
- child 后来的重复 offer 不能造成重复 canonical completion；复用现有 source-task 唯一约束与队列去重，不建永久 delivered-ID registry。
- 不复制 result body 建第二真源，不加 DTO hash。
- provider dispatch 仍只用 `snapshot_pending_root_completions` 消费原 inbox，禁止 join 专属 writer、额外 readiness source 参数或第二投递器。

### 7.3 正确事件顺序

```text
child terminal winner FULL 确认
→ task/result 可查询，UI 更新原节点
→ 普通 child offer 或 turn-bound readiness 接管来源到 manager 现有 inbox
→ targeted wait 谓词满足且 ensure_root_completion_ready 返回 READY
→ wait ToolResult 按既有机制闭合
→ 全部相关工具结算完成
→ 下一合法 safe point：先处理 exact steer，再规划 completion suffix
→ canonical INTER_AGENT_MESSAGE 接纳
→ 编译／安装最终请求
→ transport invocation（独立边界，不是“模型已读”）
```

绝不能在 wait 尚未闭合时，为满足 join 的“成功”而先写新的 completion entry。已有历史 entry 可以先存在，但不是本次 pending completion 的先写要求。

### 7.4 readiness 不是无条件纳入承诺

来源已接管不等于下一请求无条件包含全部正文。既有 steer 优先级、工具组结算、compaction successor、资源边界、STOP、Host close 和 provider failure 均保持有效。

需要消除的是**仅因来源交接遗漏而让模型空转**；不是为了减少一次请求而绕过预算、修改冻结 successor，或承诺模型一定理解结果。

## 8. 批量接纳、ROOT 收尾与 race

### 8.1 批量 drain

当前 `_drain_root_completion_suffix` 已有 FIFO 批量接纳，`_ROOT_COMPLETION_SUFFIX_BATCH_ITEMS = 16` 有单次上下文 headroom 的理由。不能把它当成历史上限，也不能为了“全部 drain”直接删掉资源保护。

目标行为：

- 对当前 ready cut 按既有资源 admission 批量接纳；支持多个 ready completion 同次进入请求。
- 内部小批写入不等于必须每条 completion 调一次模型；实现时核验当前预算、prepare 循环和 compaction 的实际边界。
- 新到达的条目进入后续 cut，不无限追赶生产者。
- FIFO、human steer lane 优先级和未选中项保留纪律不变，不偷偷提升 join targets 越过既有顺序。
- 资源不足或不可接纳须保留真实 pending/历史状态，并走既有 typed outcome；不能伪造 INCLUDED 或无理由空转。

一次五目标 join 的问题不能归咎于 16 项 batch bound；必须检查部分 completion 打断和来源 readiness。

### 8.2 completion 与 final 的线性化

复用现有 exact-turn offer/seal/assistant settlement：

1. offer 先进入 owner：seal 看到 pending，ROOT 可以保留开放以处理现有结果；这不是等待所有未完成 child。
2. seal 先完成且 assistant settlement 最终结束该 ROOT：后到 completion 留到下一 ROOT，不重开旧 turn。
3. settlement 因已接纳 steer 或其他既有合法原因未结束：按 exact 结果重新开放原 phase。
4. settlement 失败／取消时沿用原确认与清理纪律，不能先宣称 ROOT 成功结束。

判断顺序依据 owner 的锁与 canonical settlement，不比较两个墙钟时间来猜“谁更早”。terminal commit 与 offer 不是同一物理点；显式 join 需要 §7 的桥接，普通 idle completion 不因此获得额外唤醒授权。

### 8.3 防止 lost wake-up

继续使用 level-triggered 等待：取得 revision、检查 pending facts／目标谓词、在条件锁下复核 revision，无变化才挂起。completion offer 和 FULL/确切确认后的 steer ingress 通知同一活动 owner。

- signal 只是重查提示，不是结果、投递顺序或权限真源。
- 不改成裸 `Event.clear()` 后等待单个边沿。
- 重复 ACK 确认应保持输入身份与现有通知纪律，不重发另一个用户请求。
- owner 关闭通过原取消路径退出，不把目标重新绑定到新 Host。

## 9. idle、跨轮次与现有继续入口

### 9.1 唤醒策略

| ROOT/session 状态 | 普通 completion 的行为 |
| --- | --- |
| active wait | 按严格 first/all 或无目标 activity 条件恢复；不是所有 completion 都完成工具 |
| RUNNING、无 wait | 排队，在下一合法 safe point 批量接纳，不修改当前已发送请求 |
| 原 ROOT 已 TERMINAL，session idle | 不自动创建 ROOT；当前 owner 可保留 inbox，UI 更新原任务组 |
| 新人工输入启动 ROOT B | B 可按现有 safe point 接纳仍由当前 owner 持有的旧 completion；origin 仍是原任务 |
| Host 丢失／重启 | 不保证本地队列存在，不自动 replay 或 rerun；canonical task/result 仍可查询 |

不把 `create_agent_tasks` 解释成用户授权无限等待或以后自动续答。未来主动唤醒 idle 的产品能力必须另行规定授权、触发、停止、权限、费用与 owner/重启边界，本轮不引入。

### 9.2 保留“用这份结果继续”

这是已有生产功能，不是本轮新增提议：

- [task-workspace.tsx](frontend/components/task-workspace.tsx) 的节点详情已有入口。
- [host.py](src/pulsara_agent/conversation_kernel/host.py) 的 `accept_subagent_completion` 已处理 command、ROOT 身份、权限、活动槽位与确认。
- [completions.py](src/pulsara_agent/conversation_kernel/_repository/completions.py) 的同一 writer 处理自动／手动接纳和来源唯一性。

本轮保持其 controller、idle、terminal、未接纳等现有门槛与真实权限提示；重复点击、automatic/manual race、owner 丢失按原合同回归验证。不新增“选择任意结果集合自动继续”或“一键重跑 child”，也不因本次只修展示就移除既有按钮。

原入口是否还存在独立缺陷，需要专项证据；本文只确认它已存在，不声称已完成全路径审计。

### 9.3 不混用其他 idle 策略

PR03 的人工后台命令终止反馈在 idle 时 UI-only，不创建反馈 entry、不自动补投。terminal monitor 有自己的观察与唤醒规则。子任务 completion 的 queue-only/下一人工轮次接纳不能反向改变这两类合同。

## 10. UI：先显示完成事实，再显示真实接纳

### 10.1 原组／原节点与主对话各司其职

- 子任务达到 canonical terminal 后，更新由 `parent_turn_id`、batch/task 身份定位的原组与节点。
- 使用真实 terminal timestamp；不能把浏览器首次加载时间或 completion entry 接纳时间当完成时间。
- 原任务组是可更新的状态投影，不是对旧 provider/canonical messages 的回写。
- 新 canonical completion entry 保留在实际目标 ROOT 和 sequence；显示原任务来源，不重新归属于位置相邻的新建任务组。
- 来源详情尚未加载时按 exact task 身份安全降级，不按角色／正文／相邻 trace 猜测，不把新任务名字填到旧消息上。
- 任务分页、跨页查源、重连、图节点详情和原结果继续入口保持可用；不为此重构整个侧栏或能力页。

### 10.2 文案表

| 可证明的事实 | 建议文案 | 禁止暗示 |
| --- | --- | --- |
| reader canonical 成功完成 | `reader 已完成`，原节点显示完成时间 | 主模型同时知道结果；所有后台进程都已结束 |
| terminal 且尚无主对话 entry | `结果尚未加入主对话` | 一定有活跃自动投递队列 |
| 当前 Host 确认仍持有 pending completion | `结果等待加入主对话` | 保证何时接纳、保证崩溃后投递 |
| source `parent_turn_id == completion entry.turn_id`，已接纳 | `reader 的结果已加入本轮对话` | 明明是当前 ROOT 的结果却标成“上一轮” |
| source ROOT 是 target ROOT 的紧邻上一 ROOT，已接纳 | `上一轮 reader 的结果已加入本轮对话` | reader 刚刚完成、新一组任务的结果 |
| source ROOT 在 target ROOT 之前但不是紧邻上一 ROOT，已接纳 | `此前 reader 的结果已加入本轮对话`，可定位原组 | 所有旧任务一律来自“上一轮” |
| 已接纳但来源关系暂不能完整确认 | `reader 的结果已加入本轮对话`；名称未加载时为 `子任务结果已加入本轮对话` | 根据相邻卡片猜测“上一轮／此前” |
| canonical accepted，不论编译／transport 是否完成或失败 | 展示止于 `已加入对话` | 本轮不提供 subagent provider-included badge，也不推断 INCLUDED/NOT_INCLUDED |

来源关系按 exact 身份判定，不能根据视觉相邻位置、加载顺序或时间差猜测：

1. source 是 task 的 canonical `parent_turn_id`；target 是**该 completion entry 自己的 `turn_id`**，不是浏览器此刻正在运行的最新 ROOT。
2. source 与 target 相同即当前 ROOT。这覆盖最常见的“ROOT A 创建 reader → A wait → reader 完成 → A 接纳”。
3. “上一 ROOT”必须由同一 session 既有 canonical ROOT admission/initial-entry 顺序确认；排除 child turn，不以最近一条可见消息、时间戳或 task 排序代替。沿用已有 canonical 顺序，不新增持久 ROOT ordinal。
4. 翻页、历史回看和新 ROOT 开始不能改变已有 entry 的来源关系。缺少 source/target 顺序事实时使用不带“上一轮／此前”的中性文案，按既有精确查询补齐，不推测。

唯一实现路径：runtime adapter 从完整 canonical entries 提取 ROOT 顺序，将其作为只读、可丢弃的 `canonicalRootTurnIds` 随当前 projection 传递。`mergeRuntimeTaskInventory` 继续使用同一份顺序，不再从 `Message[]` 重建。由 terminal observation 发起、尚无 assistant message 就结束的 ROOT 也必须参与顺序；视觉投影将其 trace 并到其他消息，不会删除这一 canonical 事实。顺序缺失时清除旧的 previous/earlier 推断并显示中性文案；source/target 身份相同仍可直接证明 current。不新增协议字段、数据库 ordinal、receipt、fingerprint 或第二真源。

失败、取消、中断和依赖失败使用真实终态及有用说明，不一律写“已完成”。成功/失败都可以有待接纳的终态说明。

删除“主任务会结合这项工作的结果继续处理”“已经用于当前处理”等超过证据的固定文案；不清洗或改写模型、用户、工具的来源正文。

### 10.3 图 1 的目标呈现

```text
原任务组 reader
  18:13:05 已完成
  结果尚未加入主对话
  （确认当前 owner pending 时：结果等待加入主对话）

18:28 用户：调用五个 subagent……
18:28 此前／上一轮 reader 的结果已加入本轮对话
       └─ 指向原 reader，不能指向新建五个任务
```

新任务创建和旧结果接纳可以发生在同一轮，但来源必须清楚。以上时刻是用户案例，不作为新 dogfood 的伪造时间戳。

### 10.4 本轮明确不实现 provider-included 展示

选择最小范围，取消先前“覆盖足够就顺便显示”的可选项：

- 本轮 subagent UI 一律止于 `已加入对话`，不提供“已纳入本轮模型请求／已发送／模型已收到”的 badge、tooltip 或派生字段。
- 不泛化 runner observer / PR03 control feedback Host 实现到所有 completion，不新增 completion inclusion map、process-local attempt、实时事件或持久 receipt。
- 保留 PR03 控制反馈现有 observation 能力与测试；不因 subagent 不做该展示而删除、改名或降低其他产品已有证据。
- canonical accepted 后即使编译、ROOT 或 transport 失败，仍保留接纳事实。不能由 `accepted_root_entry_id`、`completion_accepted` 或 `through_sequence` 推断请求纳入，更不能自动再投一次。
- 本轮可在测试／dogfood 中检查实际 provider 请求以证明结果交付和 continuity，但这是测试证据，不要求构建运行时 UI projection。编译/transport 失败时只需证明界面不出现过度文案。

未来若单独立项请求纳入展示，才需要冻结 exact entry placement/有效正文与 request 身份、普通路径和 compaction successor、乱序、多个请求及观察丢失边界。它不是本轮的可选实现分支，也不能用持久 receipt 替代真实证据。

### 10.5 `completion_accepted` 字段 hard cut

字段唯一语义是：该 source task 的 terminal outcome 已有 canonical ROOT completion entry。名称与当前真实含义对齐，不增加新的数据库布尔列。

| 表面 | 新名称／规则 |
| --- | --- |
| Python/HTTP/工具结果/协议字段 | `completion_accepted` |
| 前端领域类型及状态 | `completionAccepted` |
| 值的 authority | 仍由 `accepted_root_entry_id != null`／原 source-task entry join 推导 |
| true 的含义 | 已 canonical accepted；不表示 provider delivery、模型阅读或结果使用 |

同次覆盖以下生产面及相应 fixture/test：

- `KernelSubagentManager._list` 的模型可见结果投影。
- `web_app/session_controller.py` 的 task DTO，`terminal_protocol/canonical_v3.py` 及 `terminal_kernel_v3.proto` 的 task projection。
- 协议 generator 管理的 bindings/schema identity/fixtures；该字段保持原布尔含义与既有 protobuf tag，不能平行添加一份旧字段。按既有 generator 更新，不手工改产物冒充生成。
- `frontend/lib/runtime-adapter.ts` 的协议/HTTP类型及映射、`pulsara-types.ts`、`pulsara-app.tsx` 的操作门槛和 optimistic 接纳状态、任务节点与组件测试。
- active specifications、descriptor/result 指导和最终静态 bundle。返回字段变化遵守 provider prefix 合同，既有 epoch 的历史工具结果不得原地重写。

删除生产构造／读取中的 `completion_delivered` / `completionDelivered`，不留别名、fallback、双读或双写；本轮不做“等价 DTO 两种名称”的兼容层。保持原继续入口的门槛和接纳竞争结果，仅替换语义准确的字段名。

`accepted_root_entry_id` 本身保持不变；不对全仓 `delivered` 字符串做机械替换，也不顺手改 terminal process 的真实交付语义、既有事件或 command 名称。历史 canonical 原文、历史文档中的旧名可作为历史保留，不能为了零匹配重写 provider 历史；活动 schema、代码、fixture 和新指导只保留新字段。

## 11. 模型工具描述与提示词同步

更新现有唯一 descriptor／指导源，不另加动态 system 补丁或第二份“subagent 提示词注册表”。需要同步的意思如下：

### 11.1 spawn/create

- 创建后可以继续独立工作，工具不隐含等待。
- 当前答复依赖子任务结果时，使用 exact targets 的显式 wait；不能只说“等结果后我会汇报”然后 final。
- terminal outcome 会通过普通 completion 通道接纳，但正常结束 ROOT 不意味着以后会自动启动新一轮。
- task ID 用于等待、查询和控制，不要求靠 wait/list 获取结果正文。

### 11.2 wait

- first/all 是严格终态谓词；失败和取消也算终态，all 不 fail-fast。
- 不传 targets 等待当前可处理输入；当前 ROOT steer 中断，下一轮排队输入不打断。
- 结果只使用 §6.5 的五种 exact outcome；明确 `steer_available` 与仅无 targets 可返回的 `completion_available`，删除泛化 `input_available`。
- timeout 只限制本次调用；可以按任务预计耗时选择有意义的等待时间，避免短时重复调用。
- wait 不返回完整结果；结果在工具闭合后的合法 safe point 进入上下文。
- 收到 first／steer／timeout 后根据真实 remaining targets 决策，不重复等已经完成的同一 first 目标。

描述、schema、返回 code、测试和文档必须同次 hard cut。SYSTEM/tools 属于 prefix；只能在既有合法 cold boundary 使用新工具合同，不能在运行 epoch 中途动态改描述。

## 12. 明确非目标与删除清单

### 12.1 本轮不做

- 不新增 attention/fail-fast、group selector、durable sleep 或 idle 主动续跑。
- 不新增 subagent provider-included badge、运行时 inclusion projection 或对 PR03 observer 的通用化。
- 不新增 durable delivery queue、waiter job、receipt、replay recovery、tombstone、fingerprint。
- 不扩张到 child 再 spawn child、任意 child→ROOT 进度消息、代理复活或 follow-up 执行模型。
- 不修改既有 compaction handoff 的进程/monitor 清单范围。
- 不重构能力 tab、完整终端仿真、任务图布局或 unrelated PR03 控制实现。
- 不修改已结束轮次正文，不合并“已结束”和“模型已收到”。

### 12.2 实施时移除的旧路径

- targeted first/all 被任意 completion 一律返回的分支。
- wait 旧泛化 `input_available` 及兼容解释。
- 只见 task terminal、却没有把未接纳来源接到实际后续 safe point 的成功路径。
- safe point 另持 ready 来源的候选方案；仅保留 turn-bound manager readiness → 原 inbox。
- 将 completion 接纳渲染成“已用于处理”的固定文案和推断。
- `completion_delivered` / `completionDelivered` 的生产 DTO/schema/读写/类型及对应旧测试期待。
- 忽略 exact delivery mode/target、把任意新消息当 steer 的处理（若实施审查发现）。
- 与新合同冲突的测试期待、descriptor、活跃规格文案。

不删除现有 inbox、result transport 单一路径、first/all 能力、失败终态、手动继续、权限或 effect settlement。没有 feature flag、旧新双路径、兼容别名或过渡 fallback。

## 13. 实施顺序

### Phase A：复现与冻结合同核对

1. 复核当前工作树，不把其他 coding agent 的修改归入本修复。
2. 用确定性 barrier 复现 partial/unrelated completion 打断 all；复现 terminal commit 后暂停 offer 的窗口。
3. 按已经冻结的 §6.5 五种 outcome、§7.2 单一 readiness owner、§10.4 无 inclusion 展示与 §10.5 字段改名建立实现核对表；不重新开放可选方案。核实 UI 原始身份/顺序与 owner pending 的只读投影接入点。
4. 列出新旧规格冲突与准确删除位置；若需要新 durable 类别或改变现有资源边界，停止扩张并另行说明必要性，不能由编码者临时添加。

### Phase B：kernel 单一路径

实现严格谓词与 `ensure_root_completion_ready`；保持 shared activity、exact steer、取消、原 FIFO、safe-point 单一接纳与 final seal。先用 focused 测试覆盖交错，再核验 provider-input 顺序与资源边界。

### Phase C：展示、指导与协议投影

更新当前／上一／更早 ROOT 的来源关系、完成时间、原组状态与 canonical 接纳文案，保留原继续入口；同次 hard-cut `completion_accepted` / `completionAccepted`。不实现 subagent 请求纳入展示或 observer 泛化。同步 descriptor、typed DTO/协议生成和所需的静态产物。

### Phase D：真实验收与文档收口

完成 focused、相邻及完整回归、continuity、架构 subtraction、安装产物和真实 provider/browser 验证。逐项记录证据，再同步旧 active specs、索引和本文件状态；不能仅依据“测试数量很多”激活。

## 14. 测试与真实证据要求

### 14.1 必须覆盖的 focused 矩阵

| 编号 | 场景 | 核心断言 |
| --- | --- | --- |
| W01 | all 的目标错峰成功 | 部分成功更新 UI/inbox，不返回工具、不因此发起 ROOT 请求；全部终态后返回 |
| W02 | 首个目标失败／取消／中断，其他仍运行 | all 继续；无隐藏 fail-fast；first 可满足 |
| W03 | 无关 task completion | targeted wait 不结束；无目标 wait 可唤醒 |
| W04 | first 调用前已有 terminal，或多项已 terminal | 立即满足，返回当次已观察到的集合；不重复正文 transport |
| W05 | 无 targets、无输入、只有历史终态 | `nothing_pending`，不等未来任务 |
| W06 | terminal commit 后暂停普通 offer | `ensure_root_completion_ready` 将未接纳目标置于原 FIFO/set，再返回谓词成功；合法后续请求不因来源遗漏而空转 |
| W07 | W06 后 child 重复 offer／自动手动竞争 | 一份 canonical source-task entry；结果不重复注入 |
| W08 | steer 在订阅前、检查中、挂起后、ACK 确认后到达 | 不丢 wake；exact ROOT 与 command/queue 身份不漂移 |
| W09 | 下一轮输入与当前 steer 同时存在 | 下一轮输入不打断；当前 steer 正常中断并优先接纳 |
| W10 | STOP、Host close、waiter cancellation | 原工具/effect settlement 完成；不误取消无关 child，不伪造谓词成功 |
| W11 | 取消一个目标 child | 该目标成为终态，all 仍等待其他目标 |
| W12 | deadline 与谓词／steer 并发 | 有限最终判定、原因真实、无续期；timeout 不取消 child |
| W13 | 多项 ready 和超过单批预算 | 合法批量接纳，保留 FIFO/预算与剩余项；无每条结果强制一次模型调用 |
| W14 | offer 先于 seal／晚于 seal；settlement 不结束 | 当前/下一轮去向与 owner 顺序一致；原 ROOT 不被复活 |
| W15 | 工具组、steer、completion、冻结 successor | 工具先闭合；不写穿冻结请求；原 prefix 与 compaction 合同保持 |
| W16 | task failure、初始 terminal、依赖级联、ACK unknown | 全部真实终态说明走同一来源通道；不遗漏 dormant task |
| W17 | ROOT 先结束，child 后完成，再发新输入 | 原组先更新；idle 无新请求；新 ROOT 接纳旧来源而非新组 |
| W18 | Host restart／owner takeover | durable 结果可查；本地 pending 不伪装仍存在；无自动重跑或 replay |
| W19 | canonical accepted 后编译／transport 失败 | 仍显示已加入对话，不声称已纳入请求或模型已读 |
| W20 | `completion_accepted` 字段贯穿模型结果、HTTP、协议和前端 | 只保留新 snake/camel 名称与正确布尔含义；接纳前后、失败终态、继续入口和 optimistic 状态一致，无旧字段 fallback |
| W21 | 既有结果继续，重复点击及接纳竞争 | 保留权限和新 ROOT 语义，不重跑 child；未扩张控制入口 |
| W22 | 同 ROOT、紧邻上一 ROOT、更早 ROOT、分页/重连/历史回看 | 依据 source parent_turn_id 与 entry target turn/canonical ROOT 顺序选择文案；不按视觉位置、时间差或最新 active ROOT 猜测；事实缺失则中性降级 |
| W23 | readiness 查询前／查询后 seal、关闭、owner takeover 或换成 ROOT B | 不追加本次来源、不返回 READY/predicate_satisfied，不误投 B；真实取消与 TARGET_NOT_OPEN/OWNER_UNAVAILABLE 不混淆 |
| W24 | 全部已接纳目标、混合已接纳／已入队／尚未入队目标 | 已接纳不重新入队；已排队不改变 FIFO；新增只进原 FIFO/set；所有目标已接纳时也检查 exact turn/owner |
| W25 | 五种 wait outcome 与同时到达优先级 | exact 返回值及数组分区正确；targeted wait 永不返回 completion_available；steer 优先；不存在 input_available 兼容含义 |

W19 必须覆盖成功接纳后编译/transport 失败仍只显示“已加入对话”，并确认本轮 subagent UI 完全没有 provider-included badge。W20 不要求新建或测试 subagent inclusion observer；PR03 已有 observer 的相邻回归照常保留。

W06 是待复现风险对应的必测场景，不把未运行的静态分析写成“已经先红后绿”。测试 barrier 不能依赖第三方 provider 偶然慢来触发 race。

### 14.2 落点与命令

优先扩展现有 owner 测试：

- `tests/test_round10_hierarchical_subagent_orchestration.py`
- `tests/test_stage2_conversation_runner.py`
- `tests/test_stage2_conversation_kernel_postgres.py`
- `tests/test_stage2_subagent_close.py`
- `tests/test_round3_1_provider_input_prefix_continuity.py`
- `tests/test_pr03_control_feedback.py`、`tests/test_pr03_user_controls.py`（保护相邻既有语义）
- `frontend/lib/runtime-adapter.test.ts`
- `frontend/app/pulsara-app.test.tsx`
- `frontend/components/task-workspace.test.tsx`
- `frontend/components/workbench-view.test.tsx`

实施后的命令示例（**本次未执行**），仓库根目录使用 uv 管理的 `.venv`：

```sh
.venv/bin/pytest tests/test_round10_hierarchical_subagent_orchestration.py tests/test_stage2_subagent_close.py -q
.venv/bin/pytest tests/test_stage2_conversation_runner.py tests/test_stage2_conversation_kernel_postgres.py tests/test_round3_1_provider_input_prefix_continuity.py -q
.venv/bin/pytest tests/test_pr03_control_feedback.py tests/test_pr03_user_controls.py tests/test_fingerprint_subtraction_architecture.py -q
.venv/bin/python tools/generate_terminal_protocol_contract.py --check
uv run ruff check .
.venv/bin/pytest -q
git diff --check
```

在 `frontend`：

```sh
npm test -- lib/runtime-adapter.test.ts app/pulsara-app.test.tsx components/task-workspace.test.tsx components/workbench-view.test.tsx
npm test
npm run lint
./node_modules/.bin/tsc --noEmit --incremental false
npm run build:local
```

需要数据库的验证遵守 AGENTS.md 和现有 fixture；先解析并确认真实本地目标。新发现无关失败应如实分类，不弱化断言、增加 skip/xfail 或将全回归失败写成通过。

### 14.3 真实 provider/browser dogfood

使用 `LocalSettingsStore`＋`require_pulsara_home()` 只读加载保存的生产配置；优先可用的已保存 OpenRouter 连接。不要求新环境密钥、不读取旧 `.env` fallback、不覆盖生产设置。

1. **原案例**：让 ROOT 明确完成一项允许后台继续的工作，child 后完成。观察原组即时终态、idle 无额外请求；再发送新的五任务输入，旧完成接纳提示仍精确定位旧任务。
2. **严格 all**：五个错峰目标，在最大 timeout 内可完成。模型显式 all，一项先失败或取消，其他继续；此时任务 UI 变化，但 wait 不结束、不触发 ROOT provider 请求。全部 terminal 后一次恢复并综合可用结果。
3. **first 与无目标 wait**：证明允许的提前返回不是被全部禁掉；同时 ready 结果通过正常通道批量进入。
4. **用户输入**：wait 中分别发送 exact steer 和下一轮排队输入。前者恢复同一 ROOT，后者保持排队；保留 command/turn/request 身份证据。
5. **STOP 与 owner**：明确停止 ROOT，观察工具结算；不把 target child cancellation 和 ROOT STOP 混为一谈。
6. **canonical 接纳文案边界**：正常和编译／传输失败场景，subagent UI 均止于“已加入对话”，不存在 provider-included badge。测试可核对正常真实请求确实收到结果以验证交付，但不要求构建运行时 inclusion projection。
7. **既有继续入口**：idle 点击结果继续，验证权限、新 ROOT、未重跑 child；重连和重复点击行为保持。
8. **同轮与旧轮来源**：ROOT A 创建 reader 并显式等待，由 A 接纳时显示不带“上一轮”的文案；另做紧邻上一 ROOT 与更早 ROOT 来源，翻页／启动新 ROOT 后历史提示的关系仍正确。

“等待中无 provider 请求”限定为等待的 ROOT；child 自己的 provider 调用继续是正常行为。记录实际 wait 参数、wake 原因、ROOT/child/request 身份、工具闭合及接纳顺序、模型实际文本。不要仅凭 UI spinner 或模型说“我在等待”判定成功。

使用真实浏览器操作并肉眼检查原组、节点详情、主时间线及窄屏，不只使用 mocked UI 或 DOM 断言。控制性注入与自然交互证据分别标明；原截图不替代新代码 dogfood。

### 14.4 安装产物与证据纪律

生产静态资源变更后构建最终 bundle；按现有隔离 wheel/launcher 流程从非源码目录启动，确认 import 和静态入口来自安装包，验证上述关键交互。保留命令、版本、真实结果与必要截图，不生成代码／文档／证据 SHA。

只遮蔽真实凭据值，保留有用的 prompts、模型回复、工具结果、错误体和来源身份。截图含有尚未运行测试的示例时，不能当作新证据。

## 15. 完成标准与冻结决定

只有实施阶段全部满足下列条件，才能把本方案标为 ACTIVATED，并同步相关 active specs。下列状态包含第 17 节审查后修订，未重验的安装产物／真实浏览器门槛不沿用原勾选：

- [x] first/all 与无 targets 等待按 §6 单一路径实现，无隐藏 fail-fast；五种 exact outcome 和输入身份矩阵通过，旧 input_available 无兼容路径。
- [x] W06 的 terminal→inbox 窗口已确定性验证并由唯一 turn-bound manager readiness 闭合；wait 成功不依赖未来 offer；W23/W24 的 seal/owner/已接纳检查通过。
- [x] 工具先闭合、safe point 接纳、批量预算与 offer/seal/settlement 顺序均有证据。
- [x] 原任务组完成事实与接纳提示独立，当前／上一／更早 ROOT 来源依据 exact 身份与 canonical 顺序，时间、分页、重连和历史回看正确。
- [x] 本轮 subagent UI 一律止于 canonical 接纳；无 provider-included badge、运行时 inclusion projection、推断或新 receipt；编译/transport 失败时文案不过度。
- [x] completion_accepted / completionAccepted 已贯穿所有生产 DTO/schema/模型结果/类型/读写与入口状态，旧字段无别名、双读或 fallback；生成产物一致。
- [x] 现有继续入口、权限、F03 队列身份、STOP/effect settlement、PR03 相邻语义保持。
- [x] Host 丢失无伪造排队、无 replay 或 child 重跑；不新增持久调度机制或 fingerprint。
- [ ] 修订后 focused、完整回归、continuity、架构 subtraction、协议／静态构建全部完成；本轮自动化范围见第 17 节。
- [ ] 修订后的真实 provider/browser 与最终安装产物验收完成，不复用旧截图冒充本次成功。
- [x] 新旧规格及 descriptor 同步，旧路径删除；修改范围、剩余风险、验证命令和 Git 状态有最终记录。

本次审阅已冻结：§6.5 五种 outcome 和正文形状；§7.2 `ensure_root_completion_ready` 的唯一 owner、输入、队列接管与不可用结论；§10.2 的来源关系判定；§10.4 不做 subagent inclusion 展示；§10.5 字段 hard cut。实施者不得改成“到时候看情况”的可选路径。

Phase A 已按冻结范围完成精确复现，并核对了现有只读事实、错误 envelope 接入点和生成文件范围；没有重新选择 readiness owner、outcome 或 inclusion 范围，也没有增加第二投递器、兼容双路径或持久层。

当前已明确不做的事项不是“等待实现者自行决定”：fail-fast、durable sleep、idle 自动续跑、新继续控制、group selector、handoff inventory 扩展、subagent provider-included UI/projection 均不在本轮范围。

最终目标是：**该等的时候挂起当前 ROOT，该结束的时候允许结束；任务完成及时可见，结果接纳与请求纳入各自如实表达。**

## 16. Activation 记录（2026-09-11）

### 16.1 Phase A：确定性红灯

实施前用生产 owner 路径固定复现了三类旧行为：targeted `all` 被部分／无关 completion 以 `input_available` 提前打断；canonical target 已 terminal 但 child offer 尚未来到时，join 没有确保结果进入唯一 inbox；旧 UI 把 canonical 接纳写成“主任务会结合／已经用于当前处理”。红灯没有通过弱化断言、增加 skip/xfail 或修改 fixture 时序消除。

### 16.2 Phase B–C：单一路径 hard cut

- `KernelSubagentManager` 新增窄的 `ensure_root_completion_ready(session_id, root_turn_id, task_ids)`，在同一 turn-bound owner 下两次核验现有 `HostWriterGuard`，读取 exact canonical task 后仍防止 owner takeover；只向既有 FIFO inbox 补入尚未接纳的 terminal source。
- `_wait` 只保留 `predicate_satisfied`、`steer_available`、`completion_available`、`nothing_pending`、`timeout`。带 targets 的 `first/all` 只按冻结谓词结束；exact steer 优先；NEW_TURN 输入不打断；`completion_available` 只属于无 targets wait。
- wait 工具先正常结算，再由既有 provider preparation safe point 从唯一 inbox 批量接纳；普通 child completion 在 idle 时仍仅排队，不创建 ROOT。
- schema、Python DTO/HTTP/protobuf、前端领域类型与继续入口同次改为 `completion_accepted` / `completionAccepted`；旧 snake/camel 名称与 `input_available` 生产路径均删除，不保留 alias、双读或 fallback。
- UI 用 source task 的 canonical `parent_turn_id`、completion entry 的 target `turn_id` 和既有 ROOT admission 顺序区分本轮、上一轮和此前结果；原任务节点独立展示完成与是否已加入主对话。本轮未增加 provider-included badge、观察投影、receipt 或持久状态。
- descriptor、system prompt、旧异步完成规格、README、协议 fixture/generated artifact 和本索引均同步到新真相。

### 16.3 Phase D：自动化与安装产物

- focused 子任务语义：`25 passed`；相关非 PostgreSQL 组合：`28 passed, 16 deselected`。
- runner/kernel/continuity：`145 passed`；PR03/subtraction 相邻：`32 passed`。
- 协议／bridge：`23 passed`；协议生成 `--check` 通过。
- Python 完整回归：`1743 passed, 19 warnings`；warnings 仅为既有 aiohttp `shutdown_timeout` deprecation。
- 前端完整回归：`15 files, 200 tests passed`；ESLint、TypeScript `--noEmit`、Vite local build 均通过。
- `uv run ruff check .`、`git diff --check` 通过；生产搜索确认没有旧字段或旧 wait outcome 的活动读写路径。
- 最终 wheel 为 `pulsara_agent-0.1.0-py3-none-any.whl`，安装到隔离 venv 后从非源码目录启动；import、静态入口均来自安装包，安装包内 readiness 含两次 host-writer validation。最终 bundle 为 `index-BlAJ2EBl.js` 与 `index-C2h6hHOq.css`。

完整命令、真实结果、身份与截图见 [activation evidence](output/playwright/subagent-wait-refinement/activation-evidence.md)。

### 16.4 真实 provider/browser 结论

使用 `LocalSettingsStore` 和默认 Pulsara home 只读加载保存的 OpenRouter `openai/gpt-5.6-luna` Chat Completions 连接；凭据未输出，数据库使用已核验的本地 disposable target。capacity、dependency graph、context/steer、exact 输入矩阵及无 targets completion 五组报告全部 `status=passed`。严格 all 的五任务场景只有一次 wait tool 调用；无 targets 场景精确返回 `completion_available`；exact steer 场景返回 `steer_available`，同一时刻的 NEW_TURN 保持排队。

最终 wheel 的真实浏览器复核覆盖：本轮、紧邻上一轮、此前 ROOT 三类提示；原任务节点未接纳／已接纳状态和既有“用这份结果继续”入口；继续后未重跑 child；刷新后来源关系稳定；390×844 时 `documentElement.scrollWidth == innerWidth == 390`。新建的自然时序会话 `6a09eabb` 显示 `此前 earlier-child 的结果已加入本轮对话`，不是把接纳时间倒写成完成时间。浏览器 fresh reload 控制台除缺失 favicon 的 404 外无产品错误。

### 16.5 保留边界与剩余风险

- completion inbox 仍是 process-local；Host 丢失后不能伪造“仍在等待加入”，但 canonical child task/result 仍可查询并由既有显式继续入口处理。
- 本轮有意不实现 subagent provider-included UI/projection；“已加入本轮对话”只陈述 canonical 接纳，不承诺模型读取或理解。
- idle ordinary completion 不自动唤醒 provider；这是冻结产品语义，不是遗漏。
- 未新增 scheduler、durable delivery queue、receipt、fingerprint、handoff inventory 或数据库迁移链。

### 16.6 模型指导补强（2026-09-11 follow-up）

激活后的同日复核发现，等待语义虽已准确，但模型可见描述仍出现 `ROOT`、`legal input boundary`、`exact-turn` 等实现词，并且没有把 idle completion 的产品后果说完整。现已在唯一 builtin descriptor 与静态 system prompt 真源中补齐：创建不隐含等待；本次答复依赖结果时应在完成独立工作后进行一次有意义的等待；wait 返回的是结束等待的原因，结果正文在工具调用结束后作为独立对话消息出现；失败、取消和依赖失败也计入 first/all 的“已结束”，不代表成功；子任务在主回复结束后完成不会自动启动新回复，只能由后续用户请求或既有显式继续入口接纳。`list_agents` 不用于轮询，queued 消息不等于已读，取消不回滚既有文件或外部副作用。

模型可见的七个 subagent 工具描述及其参数 schema 已检查，不再出现 `ROOT`、`canonical`、`inbox`、`safe point`、`legal input boundary`、`exact-turn` 等内部术语。该补强只改变合法 cold boundary 使用的新 system/tools 文本；运行中已安装的 provider prefix 仍不重写。新增描述契约先红后绿；subagent/system-prompt/prefix focused 为 `37 passed`，工具目录与 descriptor 编译相邻回归为 `108 passed`，Ruff 与 `git diff --check` 通过。补强后再次使用保存的 OpenRouter Luna 运行 capacity/all 场景：五项任务全部完成，模型只调用一次 `wait_agent`，报告 `status=passed`，见 `output/playwright/subagent-wait-refinement/prompt-followup/real-provider.json`。

### 16.7 同轮身份头收拢（2026-09-11 follow-up）

主时间线按 exact `turnId` 识别一次 assistant loop：同一轮的思考、工具、用户 steer、子任务完成提示和最终回复只在首个 assistant 片段显示一次图标与精确名称 `Pulsara`；下一轮仍重新显示。没有 `turnId` 的临时投影保留基于相邻 assistant 内容的窄 fallback，steer 或子任务提示不会凭空制造新的 assistant 身份。

该调整不改变 canonical rows、消息顺序、provider 输入或子任务接纳语义，只压缩重复的视觉身份。组件测试先复现同一 `turnId` 跨 steer／completion 出现两个身份头的旧行为，再固定为每轮一个。前端全量 `200 passed`，ESLint、TypeScript 和 local build 通过；追加将 steer 标题收紧为单独的“引导”后重新构建，最终 bundle 为 `index-BgRJrTCE.js` 与 `index-C5kA0N-N.css`。真实浏览器在长会话 `b29ba118` 中测得 11 个 assistant 片段、2 个用户轮次、2 个 `Pulsara` 身份头，证据见 `output/playwright/subagent-wait-refinement/single-pulsara-heading-per-loop.png`。

## 17. ROOT 来源顺序审查修正（2026-09-12）

对 commit `4884daa6` 及当前 dirty 工作树复核时，发现 `project()` 已使用 canonical entries 判定来源，但随后 `mergeRuntimeTaskInventory()` 又从展示消息重建顺序。一个由 terminal observation 发起、没有 assistant message 就结束的中间 ROOT，其 trace 可能并入前一条消息；据此重算会把真实的“此前”误写为“上一轮”。这是展示层丢失身份后的错误重算，不是 canonical completion 投递时序变化。

先以真实 adapter snapshot 复现 A→B→C：A 创建 reader，B 只有 terminal observation，C 接纳 A 的结果。修复后 canonical ROOT 顺序作为当前 projection 的只读事实传入清单合并，重复 merge 均保留 `earlier`。同一测试验证 B 不需要独立可见消息；相邻测试覆盖 current/previous/earlier、缺失顺序时清除旧推断并降级中性文案。

删除 `orderedRootTurnIdsFromMessages`，没有保留 fallback；不增加后端／协议字段、数据库 ordinal、哈希或独立查询 owner。任务分页合并仍复用原 machinery。

与 PR04 联合验证：三文件 focused **154 passed**、前端完整回归 **221 passed**、ESLint、TypeScript、协议生成检查、`build:local` 和 `git diff --check` 通过。最终生成入口为 `index-DkOQlHP2.js` / `index-Cryz142G.css`；命令见 [PR04 第 23 节](PULSARA_BROWSER_DOGFOOD_PR04_PROMPT_QUEUE_STEER_COMPOSER_HARD_CUT_IMPLEMENTATION_SPEC.zh.md#23-审查后修正2026-09-12)。本轮只修前端投影／交互和对应测试，不重复 kernel pytest；没有重跑真实 provider/browser 或 isolated wheel，因此本规格保持 IMPLEMENTED、REACTIVATION PENDING。原第 16 节证据不覆盖本轮安装产物，无 stage/commit。
