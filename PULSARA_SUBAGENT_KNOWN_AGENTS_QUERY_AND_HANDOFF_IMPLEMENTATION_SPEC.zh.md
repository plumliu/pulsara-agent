# Subagent 已知对象、查询与压缩交接实施规格

状态：已实施并完成验证；ROOT 完整 diff 审阅与独立一致性审阅完成。结果及范围见 [验收记录](output/known-agents-implementation-20261030/verification.zh.md)。

## 1. 目标与边界

Subagent 在压缩后可以重新找到已被介绍或实际联系过的其他 Subagent。ROOT 不维护联系人参数或完整名单；runtime 根据已接纳的输入自动维护集合。`query_agents` 对 ROOT 与 Subagent 使用相同返回结构和 Work 查询语义，只改变对象范围。压缩交接使用同一范围，在现有预算内展示当前活跃对象。

本规格对以下事项具有优先级：Subagent 的 `query_agents` 可用性、已知对象集合、Agent 查询范围、Agent 压缩快照范围、`ROOT` 别名展示。其他委派、排队、steer、停止、资源退役、工具权限和 provider prefix 语义保持现有契约。

不新增通信权限名单、ROOT 管理联系人接口、Work 可见性集合、关系传播协议、消息确认协议、历史恢复链、额外生命周期上限或 UI 页面。

## 2. 产品语义

### 2.1 已知对象集合

记 `K(A)` 为 Subagent session A 的已知其他 Subagent 集合。

- 只做并集，不因新 Work、停止当前 Work、压缩、内存卸载或重新打开 session 而清空。关系随 session 保存，不仅保存在模型上下文或进程内。
- 只登记同一个 ROOT 下实际存在的 Subagent。自己不作为联系人重复登记；ROOT 使用固定别名，不作为关系行登记。
- 普通 Work instruction、明确选入的初始上下文文本、被接纳的 Agent 消息正文中出现完整 session ID 时，runtime 用字符串匹配提取候选，轻量校验后加入接收者的集合。
- 接纳 A 发给 B 的直接消息时，建立 A→B 与 B→A。正文提到 C 时只建立 B→C，不建立 C→B 或 A→C。
- 明确提供给 A 的 Work 材料／依赖会介绍其来源 Agent B，只建立 A→B。材料正文成为显式输入时，使用同一文本扫描规则。
- 不递归合并他人的集合。A 认识 B、B 认识 C，不推导 A 认识 C。
- 查询、等待、压缩快照本身不扩充集合。所有 tool result 均不在文本扫描范围，包括 `query_agents`、`wait_agent` 和普通工具结果；不得在统一 provider messages 渲染层扫描文本。
- 对一个已被接纳的输入，后续取消、拒绝执行或未被模型消费不撤销已建立的关系。这是 session 已接纳的信息集合，不是模型阅读回执。
- 无效、跨 ROOT、已删除的 ID 被忽略，不因此拒绝有效的 instruction／消息。之后目标才出现，也不回扫旧文本补登记。

### 2.2 通信资格不变

`K(A)` 是查询和快照范围，不是发送、等待或派活的权限名单。知道同一 ROOT 下有效的 Subagent ID，仍可按既有规则尝试发送消息；目标不必先在 `K(A)` 中。

消息只有被活动 turn 接纳后才建立上述关系。`inactive`、`not_started`、事务失败或目标 turn 已结束均不产生关系；不唤醒 idle Agent，不创建隐式 Work，不把消息改投下一 turn。

直接消息中的发送者、接收者身份来自可信调用上下文和接纳参数，不依赖正文是否写出 ID。来自 ROOT 的消息只扫描接收者正文；ROOT 的发现全集原本就覆盖全部成员，无须维护 ROOT 联系人行。

### 2.3 当前可查询范围

```text
ROOT 的普通 Agent 范围 = 同一 ROOT 下当前仍保留的成员 session
Subagent A 的普通 Agent 范围 = ({A} ∪ K(A)) ∩ 同一 ROOT 下当前仍保留的成员 session
双方另外获得固定的 AGENT/ROOT 别名
```

“仍保留”不是“正在运行”。idle、排队和终态后仍保留的 Agent 都可查询；活跃筛选仅用于压缩快照。

自身 A 是隐含已知对象，不存自指关系；列表、指定 Agent、指定 Work 和快照使用相同范围，不因未存自指行而禁止 A 查询自己的 Work。

不维护单独的 Work 集合。一个 Agent 在范围内，它的 Work 列表、instruction、状态、final、停止诊断与依赖失败信息均按现有 ROOT 查询契约返回。`query_agents` 不因此增加完整会话历史读取能力。

## 3. 当前代码链路与复用点

| 链路 | 当前生产位置 | 实施要求 |
|---|---|---|
| Agent/Work 参数与 ID | `subagents/collaboration_contracts.py`、`identity.py` | 复用 target、完整 ID 语法和参数校验；query target 保持实际 Agent／Work ID，ROOT 单独作为通信别名返回 |
| 工具目录 | `capability/builtin_catalog.py` | 将 query_agents 从 ROOT 专属分类移入共享分类；保持 run_agent、stop_agent 的原范围 |
| 工具实际可见性 | `tool_runtime.py::sealed_builtin_capability_snapshot` | 移除 SUBAGENT 对 query_agents 的过滤；不开放其他被过滤工具 |
| 实际工具授权 | `tool_runtime.py::authorize` | 从 `root_tools` 移除 query_agents，保持已有共同权限检查；不能只放开 schema 和 invoke |
| 工具权限分类 | `tool_permission.py` | query_agents 移出 parent family 后仍保留 bypass 门禁；send／wait 沿既有权限规则 |
| 工具执行入口 | `subagents/collaboration_api.py::invoke` | 允许 Subagent query_agents；可信调用者决定范围，公共参数不能指定观察者 |
| Work 接纳 | `_repository/subagents.py::admit_agent_requests` | 在原接纳事务内扫描 instruction、登记明确材料／依赖来源和已冻结材料正文 |
| 初始上下文 | `provider_dispatch.py::_prepare_agent_invocation_snapshot` → `CollaborationToolPort._run` | 复用选定的 `req.initial_context` 公共文本投影，在 Work 接纳事务中扫描；不重新读取 ROOT 历史 |
| 依赖物化 | 同文件 `resolve_agent_dependencies` 及其连接内实现 | 在正文被写入 work_inputs 的同一事务内扫描新正文 |
| 消息接纳 | `host.py::_send_agent_message` → `_repository/subagents.py::enqueue_agent_steer` | 在已确认目标活动 turn 的原事务内建立关系并扫描接收者正文 |
| 初始输入组 | `_repository/prompts.py::_queued_ordered_inputs` | 复用输入来源区分，不合并 entry，不在输入渲染时反复扫描 |
| 查询 | `_repository/subagents.py::{list_agent_sessions,list_agent_works,query_agent_work}` | 在 SQL 范围过滤后分页，复用当前 Work DTO 和内容读取 |
| 压缩采集 | `tool_runtime.py::freeze_compaction_runtime_handoff`、`CollaborationToolPort.freeze_compaction_handoff` | 去掉“Subagent 一律无 Agent 快照”，改成同一范围上的活跃筛选 |
| 快照预算／输出 | `compaction/runtime_handoff.py` | 复用 builder、惰性分页、整项预算、省略提示和 TODO 处理 |
| 会话存储／删除 | `storage/migrations/sql/0000_conversation_kernel_baseline.sql` 中 sessions | 增加最小关系表；session 删除自然级联删除关系 |

上述 `subagents/`、`_repository/`、`host.py` 和 `tool_runtime.py` 均位于 `src/pulsara_agent/conversation_kernel/`；其余生产路径位于 `src/pulsara_agent/`。

当前代码在工具目录、实际 binding 过滤、authorize 与 invoke 四层限制 Subagent 查询；必须同时调整。当前快照采集与 CollaborationToolPort 又各有 ROOT 限制，不能只删其中一处。

`_agent_rows` 同时被发送和停止使用，`_work` 同时被停止、`wait_agent` 与 Code Mode 的等待使用：不能直接把新的查询范围强塞入这些公共内部读取方法或 wait 的 `_bind`，从而意外限制其他操作。应复用 repository 的基础归属检查，并为 query／handoff 提供显式的观察者范围读取入口。

## 4. 集合存储、所有权与事务

### 4.1 最小持久关系

在 clean-v0 增加一张 canonical 关系表 `agent_known_sessions`，只有必要的两个身份字段：

| 字段 | 含义 |
|---|---|
| `observer_session_id` | 持有集合的 Subagent session |
| `known_session_id` | 被认识的 Subagent session |

二者组成主键，均引用 sessions，使用 `ON DELETE CASCADE`；禁止自己指向自己。主键支持按观察者、目标 ID 的范围查询；目标列索引支持删除清理。两个端点必须是同一 ROOT 的 Subagent，由唯一 repository 写入 owner 校验，不向工具开放直接写接口。

选择关系表而非 sessions 内整包 JSON 数组，是为了复用数据库的去重、原子并集、分页和删除，不进行并发读改写整个无总量上限的集合。

这张表的独立产品理由是：已接纳的介绍跨 Work、压缩和 session 卸载后仍应可查询；它直接决定当前查询范围，不是证明消息处理成功的记录。现有队列保存发送者和材料来源，但不能直接表示文本中已验证的第三方介绍；每次查询回扫所有历史也不适合作为当前状态读取路径。

本次 canonical 关系表类别增量为一；不增加 durable event、live event kind、subject、append guard、job 或执行恢复状态。实施时对应更新 durability oracle，并明确记录这一项产品必要性，不能宽泛放松 oracle。

### 4.2 唯一写入 owner

repository 提供一个连接内的集合并入 helper，接收已验证的观察者和候选 ID；批量校验同 ROOT 归属，用 `INSERT ... ON CONFLICT DO NOTHING` 并入。复用现有 ID 解析和 SQL 连接，不新增资源 registry、关系事件或独立提交事务。

维护位置：

1. `admit_agent_requests`：原有请求全部校验通过，在接纳事务内处理 instruction 和 `req.initial_context`。普通调用与 batch 复用同一路径；batch 先建立全部成员身份，再统一处理介绍，避免依赖循环顺序。
2. 显式 `materials`／`depends_on`：已接纳关系足以介绍来源 Agent，不要求等待对方 final；复用当前 Work→Agent 解析。已冻结正文在其接纳时扫描；尚未物化的依赖正文在成功写入 work_inputs 时扫描。
3. `enqueue_agent_steer`：确认活动目标 turn，接纳消息的同一事务内并入双方关系和接收者正文候选。ROOT 完成材料通知仍按其真实输入类型处理，不误当成双方直接通信。

现有 `context:last_n` 已通过 `_prepare_agent_invocation_snapshot` 生成 USER／ASSISTANT 公共文本投影，排除工具结果。扫描实际选定并冻结为 `req.initial_context` 的文本即可，不回读 ROOT 历史、不扫描未选入的单元，也不扩充该投影的内容。显式选择某个 Work 作为材料同样属于新的材料输入。扫描边界由这次接纳的输入来源决定，不因为材料曾经是 assistant final 或曾被工具返回就拒绝扫描；直接的 tool result 仍不扫描。

关系写入成功与原输入接纳同事务提交；数据库故障应回滚原事务，不允许消息显示已接纳而丢失集合更新。重试、重复接纳和重复正文不得产生重复关系。文本候选无效是正常忽略，数据库故障不是正常忽略。

不为对方申请新的 writer lease，也不持有两个 live session 的锁。复用原数据库事务及截止时间，采用以下最小并发处理：

- `admit_agent_requests` 读取既有目标 session 的 `FOR UPDATE` 改成 `FOR NO KEY UPDATE`；同时将其 `_agent_queue_event` → `_repository/kernel.py::_append_events` 的 owned-agent 归属检查改为 `FOR NO KEY UPDATE`，避免后续事件追加又升级同一 session 锁。保留原有 `lock_session_admission`、归属校验、事件类型门禁和接纳事务。这些路径只更新队列／事件序号、idle 时间等非键字段，不修改 session 主键；新锁仍与删除和其他 writer 互斥，但与关系外键所需 `KEY SHARE` 兼容。
- 在关系插入前，收集此次写入涉及的端点，去重、按 session ID 排序，以同 ROOT 条件取得 `FOR KEY SHARE` 行锁；以实际取锁后的存在行作为有效候选。删除先完成则候选被忽略；介绍先取锁则删除等待本次短事务提交，随后正常级联清理。
- 关系行按 `(observer_session_id, known_session_id)` 稳定顺序插入并去重；batch 在全部身份建立后统一并入关系。不能只排序关系行，却保留可能形成锁环的父 session `FOR UPDATE`。
- Work 材料／依赖的有效性与文本中可选候选的有效性分开处理：复用 `_lock_work_input_sources` 和原 Work 校验，不能把必需来源缺失也当作无效字符串忽略。
- 依赖物化可能已持有待处理 queue 行锁，之后为正文新提到的对象取锁；这与删除 session 后级联删除 queue 的顺序仍可能形成数据库死锁，不能宣称仅靠上述排序即可杜绝。扩展现有事务级重规划 wrapper，以中性名称统一处理 `_WorkInputsReplan`、psycopg `DeadlockDetected` 和 `SerializationFailure`，沿用原 `deadline_monotonic`，覆盖现有 Work 接纳、依赖物化、取消及 `enqueue_agent_steer`。
- 重试必须让当前事务完整回滚，再以同一个请求／queue 身份重新执行，不在失败事务内补写，不重复生成身份、不重发 provider 或工具副作用。现有 `_writer_transaction` 仅在提交后发布 committed-event batch；prompt／blob 存储亦使用同一事务，继续复用这些保证。FK 错误、参数错误及程序异常不得被归入可重试异常；截止时间耗尽按现有超时路径失败。

这只是现有同步数据库事务边界的有限异常处理，不增加后台修复、持久重试任务、lease 或通用重试框架；不新增独立重试次数上限。

这些锁只存在于原接纳事务，不改变 idle 起点、保留时间或 session writer 所有权。验收必须验证 batch 与消息交错、双向消息和删除先后两种合法串行结果。

### 4.3 文本扫描

- 从指定输入的原始纯文本提取完整 `session:` ID，复用 `ID_PAYLOAD_PATTERN`／`require_id`，不维护另一套 ID 格式。
- 检查边界，避免把更长字符串中的合法前缀误当完整 ID；代码片段、引号和 Markdown 中的完整 ID 同样可以被识别，不做语义分类。
- 候选去重，批量检查当前归属；不按名称检索，不模糊匹配，不扫描 queue ID 来推导联系人。
- ROOT 实际 session ID 不写入集合，也不在联系人列表中产生重复 ROOT；别名始终由可信 root_session_id 解析。
- 候选数量受已有单次输入的实际大小约束，不另设联系人总数或每条文本任意 ID 数量上限；可按数据库单次参数边界分批执行，但不得静默截断。
- 不扫描模型摘要、runtime 快照、环境提示、所有直接 tool result 或普通 assistant 输出流。只有显式作为新 instruction、已选初始上下文、消息或 Work 材料接纳的文本进入扫描，不按其中引用内容的原作者另设过滤。

### 4.4 生命周期

不修改现有 idle 起点、资源卸载、删除时间和安全门禁。认识某个 Agent、查询它或把它列入快照不延长其保留期。任一端点删除后级联清理关系；重新创建的新 session 是新身份，不继承旧 ID 的关系。不要为旧对象保留 tombstone、重定向或替代 ID。

## 5. query_agents 的共同范围与 ROOT 别名

### 5.1 一个范围 owner

repository 根据可信 `root_session_id` 与调用者 `session_id` 构造唯一查询范围：ROOT 使用成员归属，Subagent 使用成员归属加“目标为自身，或存在 `agent_known_sessions` 关系”的检查。不能从用户参数、cursor 或工具正文中取得观察者身份。

普通列表、指定 Agent、指定 Work 和压缩候选都复用这个范围。过滤必须发生在 SQL 的 LIMIT 前，避免分页空洞、数量泄漏或把 ROOT 页读出后再过滤。指定 Work 按其所属 Agent 检查范围；失败使用正常的目标不可用结果，不返回越界目标的正文或元数据。

保持当前 Agent ID keyset 分页、Work queue_sequence 分页、每页上限和 DTO。并发添加集合成员不引入持久快照游标：现有分页继续，重新从第一页查询可看到此前游标之前新增的对象，不承诺目录遍历的事务快照一致性。

### 5.2 ROOT 的固定返回形式

ROOT 是通信目标别名，不是 Subagent，也没有委派 Work。为了单独提供它且不挤占 Agent 分页名额，无 target 查询统一追加固定 `root` 字段，ROOT 与 Subagent 返回相同结构。以下示例为没有成员的 ROOT；Subagent 的普通列表至少包含自身：

```json
{
  "root": {"kind": "AGENT", "agent_id": "ROOT"},
  "agents": [],
  "next_cursor": null
}
```

`agents` 只包含当前页的普通 Subagent；`root` 在空集合和后续页同样存在，不参与排序、cursor 或 `max_items`。这是在查询结果中单独追加 ROOT 别名，不新增集合关系或数据库 session。

`root` 是用于 `send_agent_message`／`wait_agent` 的固定通信目标；`query_agents` 的 target 继续只接纳实际 Subagent ID 或 Work ID，不增加 `query_agents(ROOT)` 空 Work 特例。不把 ROOT 的普通用户请求伪造成 Work，也不增加 ROOT 的 instruction／历史查询。

`send_agent_message`、`wait_agent` 对 ROOT 的现有解析不变；不因此为 `run_agent`、`stop_agent` 或 Work target 开放 ROOT。读取别名不启动执行。

### 5.3 工具可用性与通信独立

将 `query_agents` 加入 Subagent 的实际原生工具表、参数验证和 invoke 允许集合；维持现有 bypass 条件。ROOT／Subagent 不增加两套工具名或两种返回 DTO。Subagent 不因这次修改获得 Code Mode、创建／停止 Agent 或普通会话检索能力。

查询范围失败不能影响发送消息：例如 A 的集合没有 B，A 已知其有效 ID 时仍可发送；消息被接纳后，A、B 自动建立关系，此后查询可发现对方。

## 6. 压缩交接

- ROOT 的 Agent 候选保持现有成员范围；Subagent 的 Agent 候选使用其查询范围。快照不允许重新从 ROOT 全量名单取数。
- 在共同范围上继续筛选 `current_work_id IS NOT NULL OR queued_count > 0`，沿用现有身份、当前 Work、排队数投影。不是把 query 的完整 instruction／final 塞入快照。
- 已知但 idle 的对象不必列入快照，仍可 query 找回。集合不是快照反序列化得到的，也不被快照内容覆盖。
- ROOT 别名不伪造活跃 Work。无 target query 总能给出它；当前 runtime 身份说明／工具说明应明确可使用 ROOT，不为单独列出别名强行生成一份非空 PMCA。
- Subagent 仍没有 Cell 快照，其 Process、TODO 作用域保持现有语义；只打开按范围读取 Agent 的路径。
- 复用 3,200 V3 启发式 tokens 的 PMCA 预算、惰性分页和整项省略机制；TODO 完整追加、不占 PMCA 预算的规则保持。
- 省略提示应匹配当前作用域的实际工具：Subagent 可被引导使用 query_agents，不提示它使用不可用的 query_cells。同一实际提示须参与 builder 的初始预留、逐项测量、溢出回退和最终正文，不能只在输出后替换文案。无需另造摘要机制或专用恢复工具。
- 保留 coordinator 的实际时序：模型摘要完成后采集 runtime handoff；采用后在既有 safe point 还会重采集当前 runtime source，并做后续调用的实际预算测量。不要把采集提前到摘要前，也不要以“已冻结”为由删除合法的采用后重采集。
- 每次采集仍是分页的弱一致观察，不是全目录的单事务快照。已选入的完整项属于那一次冻结结果；联系人若在采集期间加入且 ID 排在已过 cursor 之前，可以不出现在该次结果中。集合中的身份仍可在后续 query 从第一页找回。
- 摘要执行期间、handoff 分页期间和采用后新接纳的介绍，都只按原接纳事务维护集合；不要求重启模型摘要，不修改已安装的 prefix，不新增持久快照游标或恢复链。

新增工具定义仅在 cold epoch 或明确采用的 compaction successor 安装；已有 epoch 的 SYSTEM/tools 保持字节相同，messages 仅追加。集合变化不是重建 provider prefix 的理由。

## 7. 实施顺序与 hard cut

1. 增加 clean-v0 最小关系表和 repository 并入／范围读取；同步 `storage/migrations/manifest.py::CONVERSATION_KERNEL_RELATIONS`、expected catalog、contracts golden 和精确 schema oracle。当前关系表基线 29 项增加为 30 项，其他类别保持精确值；执行首次和重入 migrate 检查。没有线上迁移、回扫旧数据或双读路径；需要重置时只使用已核实的本地可丢弃数据库。
2. 将集合更新接入现有 Work／消息接纳、依赖物化事务；覆盖 batch 和双方并发消息。不能靠工具调用完成后的进程内回调补写。
3. 调整 query schema、catalog、实际过滤、authorize 和 invoke；复用原查询 DTO，统一三个入口的范围与固定 ROOT 返回。
4. 让 Agent handoff 复用同一范围读取，保持活跃筛选与预算；更新作用域对应的省略提示。
5. 更新相关测试、当前委派契约、runtime handoff 规格和 bundled code-mode 协作参考文档。删除“query_agents 仅 ROOT”和“Subagent 永无 Agent 快照”的旧断言；不保留新旧路径或增加 profile。

不修改侧边栏、open_in_pulsara 或 ROOT 的成员目录范围。现有 `resolve_visible_agent` 等面向 ROOT UI 的能力不能被误改为某个 Subagent 的联系人目录。

## 8. 验收

### 8.1 集合与接纳

- instruction 中重复、带标点和代码格式的合法完整 ID 只登记一次；部分 ID、无效 ID、跨 ROOT、自己和 ROOT 不产生普通关系行。
- 普通调用与 batch 语义一致；同一事务创建的成员可以被正确介绍；拒绝／回滚不留下集合变化。
- A→B 消息被接纳后双方可互查；正文提到 C 只使 B 认识 C。未接纳消息不产生关系。
- 显式材料与依赖来源正确登记；依赖尚无正文时不扫描，正文物化时才扫描。空结果保留现有独立 entry 文案，不额外虚构联系人。
- 所有直接 tool result 不扫描；查询 B 的 Work 中即便包含 C 的 ID，也不自动登记 C。显式选入的 `context:last_n` 公共文本或 Work 材料提供 C 时登记；未选入的 ROOT 历史及被原投影排除的工具结果不登记。
- 用真实 PostgreSQL 的可控事务交错验证双向消息、batch 接纳与消息、依赖物化与级联删除、候选删除先赢／接纳先赢；保留原删除／接纳断言。验证数据库死锁／序列化失败时整个事务回滚重试和截止时间耗尽的正常失败；并发追加不丢成员、重试不重复发布。事务写失败不能出现输入已接纳而关系未保存。
- 停止、换 Work、压缩和 session 卸载／重开后集合保留；删除按原生命周期清理，不因关系续命。

### 8.2 查询

- ROOT 保持全成员范围；Subagent 只列出自身和自己集合中仍存在的成员；没有其他联系人时也能查询自身 Work，并返回 ROOT 别名。
- 双方对同一可查询 Agent／Work 返回相同字段与内容；Subagent 可从 Agent 列表获得 Work ID 并查询 instruction、final 或停止诊断。
- 列表、指定 Agent、指定 Work 都执行相同范围判断；已知范围外 queue ID 不绕过检查。
- ROOT 固定字段不影响普通分页数量和游标；query target 继续拒绝 ROOT 别名，send／wait 继续接纳它，不暴露普通 ROOT 对话。
- 大于一页的集合没有静默截断；数据库范围过滤先于 LIMIT；不存在联系人总数 cap。
- 经实际 native tool_execution → authorize → invoke 链路验证 query 可用，不能只测试 schema 或直接 port.invoke；原本禁止给 Subagent 的 run_agent、stop_agent、Code Mode 等仍未开放。已安装 epoch 的 SYSTEM/tools 字节保持，successor 才获得新工具定义。
- 集合外同 ROOT 有效目标仍可被发送消息，成功后建立双方关系；A 尚不认识 B 时 `wait_agent(B)` 仍按既有活动／终态语义读取 Work，等待本身不扩充集合。不误用 query 的过滤限制通信或 wait。

### 8.3 压缩与真实 provider

- Subagent PMCA 的 Agent 项只来自自身 A 与 K(A) 的共同查询范围，并仅保留活跃项；ROOT 结果保持原成员范围，Subagent 无 Cell 项。
- 超预算省略恢复用确定性测试构造足够候选并触发真实 builder 预算；Subagent 可用 query_agents 分页重新找到，idle 联系人也能找回。ROOT／Subagent 两种省略文案都参与 3,200-token 边界、整项回退和 TODO 独立计量验证。
- 覆盖摘要执行期间及分页 cursor 之前新增联系，验证其最终可 query、不会重启摘要；冻结前缀、首次调用前压缩、turn 内压缩及采用后重采集保持既有语义。
- 复用 `test_agent_requests_persistence.py`、`test_agent_collaboration_api.py`、`test_agent_collaboration_postgres.py`、`test_agent_session_host.py`、`test_runtime_handoff_agent_postgres.py`、`test_runtime_handoff_pmca.py`、owner／retirement 集成测试，以及现有 catalog 测试；更新旧 scope 断言而不削弱预算和归属断言。clean-v0 使用 `test_stage5_clean_migration.py` 和 `test_stage2_conversation_kernel_postgres.py` 的精确 oracle。
- 实施阶段执行真实 provider 最小闭环：ROOT 创建 A/B/C；只向 A 介绍 B；A 实际采用 compaction successor 后，通过原生 query 找回 B、查询 B 的 Work，并给确实处于活动 turn 的 B 发消息；确认 C 未进入 A 的目录。正文只使用真实创建的 ID，保留 COMPACTED／采用记录及实际工具参数与返回。此闭环与确定性超预算场景分开，不假设三个 Agent 就会触发 PMCA 省略。
- 当前 `tests/dogfood/run_compaction_request_start_dogfood.py` 可复用配置和证据收集，但只覆盖 ROOT，不能当作 Subagent 已通过的证据。子代理触发与采用链路参考 `test_compaction_request_start_integration.py::test_subagent_first_summary_uses_full_initial_group_and_current_work` 和 `test_agent_session_host.py::test_child_first_preparation_adopts_real_common_snapshot_for_next_work`，使用现有 owner 与压缩策略，不添加生产捷径或复活旧任务工具。
- provider 验证复用有效 Pulsara home 的只读生产配置，通过 LocalSettingsStore 加载；保留具体工具参数、返回和 provider 证据，排除实际凭据值。不以模型未实际调用工具的自然语言陈述代替验证。

上述实施与验证已完成；实际测试、真实 provider 证据及保留的失败现场见 [验收记录](output/known-agents-implementation-20261030/verification.zh.md)。
