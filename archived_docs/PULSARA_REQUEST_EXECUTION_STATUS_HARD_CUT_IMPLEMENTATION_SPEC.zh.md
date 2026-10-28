# Pulsara：统一请求执行四态实施规格

日期：2026-10-28。状态：已实施并完成聚焦验证。

## 1. 目标与范围

正常 ROOT 的新轮请求统一提供 `queued / running / completed / stopped` 四种执行状态。未来 Subagent 的 WORK 直接引用已接纳的 `queue:`，复用同一个读取合同。调用方无需自行拼接“队列是否已消费”和“对应 turn 是否结束”。

本轮实施正常 ROOT NEW_TURN 的公共读取、命令响应、协议／桌面消费以及定时任务单次执行结果。不实施 Subagent session 化、协作工具重构、依赖调度或 Code Mode；不新增模型调用重试、旧 turn 恢复或脚本重放机制。

采用完整 hard cut：所有受影响的生产消费者同步改用统一合同，不提供旧／新执行状态双字段、别名解析或兼容回退。队列交付、命令操作和 turn 执行各自的权威事实保留，它们不是四态的旧版别名。

## 2. 四态与终态原因

| status | 产品含义 | 是否结束 |
|---|---|---|
| `queued` | 已接纳，尚未开始；等待执行条件或容量 | 否 |
| `running` | 对应 turn 正在执行，包含工具／消息等待和现有临时重试 | 否 |
| `completed` | turn 正常完成 | 是 |
| `stopped` | 未正常完成而结束，包括主动取消、错误、中断和投递前拒绝 | 是 |

正常路径为 `queued → running → completed`；queued 或 running 也可进入 stopped。终态不回到运行态。继续工作使用新请求，不改写已经结束的请求。

`stopped` 保留已有权威 `terminal_reason`，以区分主动停止、配置错误、宿主中断等事实。其他三个状态的 `terminal_reason` 为 null；不要将 queue 的 `CONSUMED` 当作执行结束原因。不增加一套归一化原因枚举，不凭 live 日志或说明文字猜测原因。本轮不扩展错误详情写入，现有详情／历史展示继续由既有链路提供。

### 精确映射

| 当前已提交事实 | execution.status | execution.terminal_reason |
|---|---|---|
| NEW_TURN queue PENDING | queued | null |
| queue CONSUMED，关联 turn RUNNING | running | null |
| queue CONSUMED，关联 turn COMPLETED | completed | null |
| queue CONSUMED，关联 turn INTERRUPTED | stopped | 对应 consumed turn 的 terminal_reason |
| NEW_TURN queue CANCELLED | stopped | queue.terminal_reason |
| NEW_TURN queue REJECTED | stopped | queue.terminal_reason |

接纳前拒绝、命令冲突或不存在的 queue 不是 stopped 请求：它们没有成功接纳的执行对象，继续返回原有操作错误／不存在结果。

### Steer 与 ROOT 的排队转补充

STEER_ACTIVE_TURN 是已有 turn 的补充输入，没有独立执行四态。它保留自己的接纳、交付、拒绝和目标 turn 信息。

ROOT 已有“将排队请求转为 steer”行为保持：原 NEW_TURN 被取消，投影为 `stopped / USER_REDIRECTED_TO_STEER`；新建的替代 steer 使用自己的 queue ID 和交付结果。不能把目标 turn 的运行或完成状态当成 steer 的工作状态。普通队列操作和 Plan 内的同类转换均适用。

## 3. 唯一事实与读取 owner

保留现有队列 PENDING／CONSUMED／CANCELLED／REJECTED、turn RUNNING／COMPLETED／INTERRUPTED 及其原因字段。四态是只读投影，不新增数据库列、Work 表、ID 映射、事件、持久任务或执行恢复记录。

稳定身份使用完整 `queue:`，执行关联为：

```text
prompt_queue_items.id
→ consumed_entry_id
→ transcript entry.turn_id
→ turns
```

在 conversation kernel 公共合同边界定义 `RequestExecutionStatus` 和 `RequestExecution`，由 repository 的共同请求读取路径取得一致快照，再调用唯一的纯映射函数。函数只处理经过验证的 NEW_TURN 事实；不把任意字典、最新 ROOT turn 或进程存活状态当替代来源。

公共值为：

```json
{"status":"running","terminal_reason":null}
```

```json
{"status":"stopped","terminal_reason":"USER_STOPPED"}
```

请求定位、内容和结果读取继续使用现有 queue／entry／turn 字段；不为四态复制 final 正文。提供按 session＋queue ID 的共同读取能力，供命令查询、单次调度执行和未来 WORK 使用。现有批量／聚合查询可以批量取得同样事实后调用同一投影，不退化成逐项数据库往返。

### 一致性要求

- 从同一个数据库快照读取 queue 与 consumed turn；沿现有事务读取模式，不拼接不同时刻的独立查询结果。
- CONSUMED 必须定位同 session 的真实执行 entry 和对应 turn，并核对生产消费合同中的 initial 关联。不回退“最近一个 turn”。
- 生产消费事务同时创建 turn、initial entry 和 queue CONSUMED，正常提交没有“已消费但没有执行记录”的中间状态。缺失或冲突时使用明确的一致性错误，不返回 queued／running 兜底，不增加第五种工作状态。
- 现有 ROOT scope 在本轮按实际 ROOT 身份核对；状态纯映射不依赖旧 Subagent task 枚举。未来新的 session 执行接入相同事实合同。
- 投影用于展示和读取，不代替控制操作的事务条件。停止、取消、调度仍由原 owner 锁定并复核真实阶段。

## 4. 公共响应与消费者

### 4.1 PromptDelivery 与命令查询

在现有 `PromptDelivery` 增加 `execution: RequestExecution | null`：NEW_TURN 必有四态值，STEER_ACTIVE_TURN 为 null。身份和交付字段沿现有结构保留，不在 execution 中重复 queue ID 或生成新的请求 ID。

示意仅列相关字段：

```json
{
  "delivery_mode":"NEW_TURN",
  "queue_status":"CONSUMED",
  "execution":{"status":"running","terminal_reason":null}
}
```

```json
{
  "delivery_mode":"NEW_TURN",
  "queue_status":"CANCELLED",
  "execution":{"status":"stopped","terminal_reason":"USER_REDIRECTED_TO_STEER"}
}
```

```json
{
  "delivery_mode":"STEER_ACTIVE_TURN",
  "queue_status":"CONSUMED",
  "execution":null
}
```

初次提交成功响应反映接纳事务时刻，因此 execution 为 queued；调度可能在响应到达前已经启动。后续 query 读取新的快照，不承诺接纳响应永远是最新状态。请求已经接纳不能因后续状态查询失败被误报为未提交或被自动重派。

`query_command` 对 NEW_TURN 提交命令沿共同投影生成原命令结果：queued／running 对应 PENDING，completed 对应 SUCCEEDED，stopped 对应 REJECTED。这里只保留已有命令结果语义，不再各处重新解释 queue 与 turn。

取消、改投等操作命令仍表达该操作是否成功。例如取消成功的命令是 SUCCEEDED，其所描述原 NEW_TURN 的 execution 是 stopped，两者不冲突。Steer 命令继续使用既有交付语义，execution 为 null，不用目标 turn 四态冒充一份 steer 工作。

### 4.2 协议、桌面与快照

同步更新 Python typed 合同、terminal protobuf 及生成代码、gateway、desktop bridge／HTTP DTO、TypeScript adapter 和实际消费位置。执行请求的状态由服务端给出；前端不再另写 queue→turn 四态推断，也不缺字段时回退旧规则。

队列编辑／取消资格、正文从队列移入历史的位置、改投结果核对仍使用交付事实。`queue_status=CONSUMED` 在这些用途上有效，不替换为 completed。现有命令未知、连接同步或观察超时也不是请求执行状态，不伪造 stopped 或自动重发。

会话 snapshot 继续保留 pending-only 队列和 active-only turn 控制视图，不扩展为无限历史请求目录。若 snapshot 的 NEW_TURN 队列行需要呈现执行状态，复用同一 `RequestExecution`（queued）；steer 行不提供执行四态。session 的整体运行状态和最新 turn 仍保持各自语义，不强行变成单个请求状态。

### 4.3 定时任务

定时任务产生的 NEW_TURN 同样使用共同四态。单次 run 返回使用 `execution`，替换现有混用 PENDING／CONSUMED／RUNNING 的运行状态表达；初次、合并已有执行、幂等确认三条路径均从同一事实合同生成，保留确切 queue 身份和原因。

确认查询补齐 consumed entry／turn 及原因所需事实。不能对已消费的中断执行返回 queue 的 CONSUMED 作为结束原因。

一个定时任务可同时有运行实例和排队实例；任务的 ACTIVE／PAUSED 配置、待执行数量与运行实例概览不是单份请求四态。聚合继续表达这些独立事实；涉及某次执行状态时使用共同投影。防重复运行和接纳检查保留原 session writer 锁内 SQL，不改成“先读展示状态再决定写入”。

### 4.4 未来 Subagent 的接入点

未来 WORK 直接以 queue ID 使用相同四态和原因读取：等前驱属于 queued，调用模型／工具及共同重试属于 running，正常 final 为 completed，未成功结束为 stopped。前驱未成功导致后继不启动也归 stopped，具体原因由将来的接纳事实提供。

本轮不把旧 subagent task 的内部枚举批量替换成四态，也不提前实现依赖／停止工具。公共合同变更实际触达的调用方需同步适配；真正的 session 化实施再删除其旧任务状态机。

## 5. 执行边界：重试、完成、停止与接管

- 临时 provider 错误由现有重试路径继续时，turn 仍 RUNNING，请求为 running；不因某次失败输出、stream 中断或等待时间而提前结束。
- assistant 消息已产生不代表 completed。Stop Hook 要求继续或存在待处理 steer 时，按真实 turn 状态保持 running；只有 canonical 完成提交才 completed。
- 明确取消排队请求，或已经消费后停止 turn，按各自事务赢家生成 stopped 与实际原因。控制操作的发起不等于结算已完成。
- session 关闭、执行错误、host takeover 等沿现有 turn 终止原因投影。接管前持久 turn 尚 RUNNING 时，不凭本地失联猜 stopped；接管提交中断后再反映该事实。
- host takeover 保留原有 PENDING NEW_TURN，仍为 queued；已中断的 consumed 请求为 stopped，不重跑旧 turn、工具或整段 JS。
- 多个请求独立读取。某份失败不回滚其他已完成请求或已发生的文件／外部修改。

## 6. 具体实施落点

| 位置 | 改动 |
|---|---|
| kernel 公共 contracts 与共同请求读取投影 | 四态类型、RequestExecution、唯一纯映射；NEW_TURN 校验与一致性读取 |
| `_repository/conversation.py` query_command 及共同 queue 查询 | 复用 queue→entry→turn 联查，返回投影所需完整事实；正确区分直接目标 turn 与 consumed turn |
| `host.py` 提交响应与 query_command | 使用统一 execution；删除 CONSUMED 缺 turn 时回退 PENDING 的分支；区分提交、操作、steer |
| `_repository/scheduling.py`、`scheduling/service.py` | run 初次／合并／幂等确认使用共同投影，修正 overloaded run status；保持锁内防重检查 |
| terminal schema／生成代码、gateway、desktop bridge／HTTP | 贯通 execution，NEW_TURN 与 steer 分支明确；不加兼容解析 |
| frontend runtime adapter、提交／队列操作消费、scheduled API 与组件 | 执行状态消费统一值，交付／命令／任务聚合继续按自身语义消费 |

必须修正已发现的原因读取问题：当前 query_command SQL 有 `consumed_turn_terminal_reason`，Host 的中断分支却可能读取直接目标 turn 的 `terminal_reason`。统一 owner 以 consumed turn 的真实原因生成 stopped，不退化为笼统 TURN_INTERRUPTED。不得为此新增事件或一套错误详情权威。

当前 `terminal_public_detail` 并非所有路径都会写入，queue 也没有同等详情字段。本规格仅承诺 terminal_reason；不要为凑齐响应新增详情列、反向扫描事件或改造所有中断写入路径。

## 7. 数据库与 hard cut

现有 canonical queue／entry／turn 事实足以实现四态，本轮不需要迁移、baseline 枚举变更或数据库重置。用户允许重置已验证本地可丢弃数据库，这是实施验证权限，不是必须增加 schema 改动的理由。

移除受影响 NEW_TURN 消费位置重复的执行状态推断和错误 fallback；同步更新 protobuf 生成产物、DTO、前端及 dogfood。保留的 queue delivery／command outcome／turn status 是职责不同的现行合同，不为保持旧执行接口而输出第二套执行状态。

## 8. 验证与完成条件

使用现有 PostgreSQL／Host 集成基础验证行为，不新增冻结自然语言、完整 schema 或整份 DTO 字面快照测试。

| 场景 | 必须观察到的结果 |
|---|---|
| 接纳→消费→正常完成 | 同一 queue ID，queued→running→completed，CONSUMED 不提前完成 |
| 启动前取消／拒绝 | stopped，保留 queue 原因，没有空 turn |
| 执行中停止／错误／关闭／接管 | stopped，来自 consumed turn 的精确原因；其他待处理请求保持原有语义 |
| 模型临时重试、Stop Hook 或 steer 续接 | 保持 running，实际 turn 完成后才 completed |
| 普通与 Plan 内 queued→steer | 原请求 stopped／重定向原因；替代 steer 无执行四态 |
| 取消与消费、完成与补充并发 | 读取反映真实事务赢家，不以显示投影驱动控制 |
| command／协议／桌面／scheduled 单次执行 | 同一读取事实得到同一四态和原因；操作结果不误当工作完成 |
| 定时任务同时运行和排队 | 分别保留实例事实，不丢运行实例或排队实例 |
| 不存在、接纳前拒绝、关联不一致 | 明确对应错误，不伪造 stopped 或 queued；不自动重发 |

实施时运行聚焦的既有行为测试与受影响前后端检查。删除仅冻结旧混合状态文案／形状、没有行为价值的测试时，应明确其用途；不能削弱验证身份、原因、竞态或副作用的断言来取得通过。

完成前进行一次小规模真实 Host 生产链 dogfood：使用已保存配置和模型，观察一个执行请求及其后的排队请求，覆盖正常消费完成、一个取消或改投；记录实际 queue ID、四态及原因，不记录凭据。可复用现有 prompt queue dogfood，不另造执行框架。故障、接管与竞态用确定性集成场景验证，不依赖 provider 随机失效，也不反复扩大真实调用量。

## 9. SUPERSEDES 与调研证据

- 本规格统一 NEW_TURN 的公开执行读取合同；不替代 queue 交付、命令操作、ROOT steer 转换或内部 turn 状态职责。
- 替代 [Agent／Work 设计](../PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md) §4.1 的八态公开枚举：未来 WORK 统一四态，等待和结束的细分原因不再作为独立公开状态。本轮不因此实施其余协作重构。
- [调度 Target 规格](PULSARA_SCHEDULING_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) 的调度行为与 queue provenance 保持；其中涉及单次执行结果的公开状态表达以本规格为准，不替代任务启停／聚合合同。
- 本次不改变已有模型重试、Stop Hook、接管和精确事务确认的行为，不新增恢复链路。

静态调查报告（代码行号以调查时工作树为准）：

- [请求事实与写入 owner](../output/unified-request-status-review/owner/REVIEW.zh.md)：接纳、消费、取消、转 steer、终态、约束及一致性读取。
- [消费入口](../output/unified-request-status-review/consumers/report.md)：Host／协议／桌面／前端／scheduled 的区别与硬切列表。
- [Hard-cut 复核](../output/unified-request-status-review/hardcut/terra_queue_status_findings.zh.md)：重试、Stop Hook、竞态、baseline 与最小验证路径。

实施与验证结果见[交付记录](../output/unified-request-status-implementation/root/IMPLEMENTATION.zh.md)，代码审阅见[Astra 复核](../output/unified-request-status-implementation/critic/REVIEW.zh.md)。
