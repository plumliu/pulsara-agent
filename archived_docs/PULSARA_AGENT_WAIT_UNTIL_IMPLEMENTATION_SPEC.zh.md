# Pulsara Agent 等待条件实施规格

日期：2026-11-09。状态：已实施，已完成并行实现、交叉复审与相关回归验证。

## 1. 产品目标与适用范围

ROOT 需要两种不同的等待条件：及时观察某个 Agent 当前任务结束，以及等待该 Agent 处理完手上的全部工作。由 Code Mode 的同一个 `tools.wait_agent` 通过 `until` 参数表达，不为准确 Work 身份额外保留一个等待工具。

`tools.wait_agent` 仅接受 Agent targets。Work 身份继续用于安排任务、依赖、停止和准确结果查询；ROOT 通过 `query_agents` 读取结果。已确定的先后依赖继续交给 `depends_on` 和现有调度器，不通过等待脚本重复实现调度。

本规格替代 [子代理来信等待规格](PULSARA_SUBAGENT_INBOX_WAIT_IMPLEMENTATION_SPEC.zh.md) 中 Code Mode `wait_agent`／`wait_work` 的合同。原生 `wait_message`、subagent final answer 提示词和其余有效合同继续适用。删除脚本 `wait_work`，不保留别名或旧参数分支。

## 2. 工具职责

原生 `wait_message` 负责 subagent 暂停自己等待任意合法 Agent 来信。ROOT Code Mode 的 `wait_agent` 负责指定 Agent 的有界状态观察；准确 Work 结果由 `query_agents` 读取。

工具合同为：

- `wait_agent` 增加 `until: "work_end" | "idle"`，默认 `"idle"`。
- 两种模式均采用现有有限观察超时，超时只结束此次观察。
- `work_end` 只绑定调用时正在执行的 Work，不使用历史最后一次 Work 替代当前任务。
- `idle` 不绑定 Work，观察目标是否仍有正在执行或排队的 Work。
- 删除 `wait_work` 的实现入口、schema、工具说明与当前使用示例。

## 3. 调用范围与参数

仅 ROOT 在 bypass-permissions 模式下通过 Code Mode 调用。原生 ROOT 工具与 subagent 工具均不暴露 `wait_agent`；subagent 继续使用原生 `wait_message`。

```js
// run_agent 的返回数组按分配位置对齐，同一个 Agent 可以重复出现。
const agents = [...new Map(
  result.agents.map(target => [target.agent_id, target])
).values()];
const observation = await tools.wait_agent({
  agents,
  until: "idle",
  settle: "all",
  timeout_seconds: 30
});
```

调用方显式按 `agent_id` 去重；运行时仍拒绝重复目标，不静默修复参数。保留原始 `result.works`，用于准确查询各次分配的结果。等待返回后先判断 `state` 与 observations；消息或超时均不保证任务已结束，不能无条件推进后续业务。

| 参数 | 必填 | 合同 |
| --- | --- | --- |
| `agents` | 是 | 非空、无重复的当前协作 subagent AGENT target 数组 |
| `until` | 否 | `"work_end"` 或 `"idle"`，默认 `"idle"` |
| `settle` | 否 | `"all"` 或 `"first"`，默认 `"all"` |
| `timeout_seconds` | 否 | 有限数值 0 至 300 秒，默认 30 秒；0 只做首次检查 |

参数为封闭对象，拒绝 `works`、`target` 和未知字段。timeout 拒绝布尔值、NaN、无穷及越界值。300 秒沿用已有单次观察边界，不是任务、Agent 或多次观察的累计时长上限。

只观察本次请求明确列出的 subagents，不自动扩展到后来创建的 Agent。拒绝 ROOT alias 和 ROOT 自身的 session target：此入口观察下属 Agent 的执行状态，不提供 ROOT 等待自身的特殊消息模式。ROOT target 在发送消息等其他工具中的现有意义保持。

## 4. 两种等待条件

### 4.1 work_end

在首次读取当前状态时绑定每个目标正在执行的 Work，之后只判断该 Work 是否进入终态。绑定使用现有 canonical Work 身份，不创建订阅身份或持久记录。

| 首次读取时的状态 | 行为 |
| --- | --- |
| W1 正在执行，W2 排队 | 绑定 W1；W1 结束时满足条件，返回 W1 target |
| W1 已结束，W2 正在执行 | 绑定 W2；W2 结束时满足条件，返回 W2 target |
| 没有正在执行的 Work，但有排队任务 | 标记 `not_started`；不擅自绑定队首任务或等待它启动 |
| 没有正在执行或排队的 Work | 已满足，标记 `idle`；不把历史 Work 当成本次完成 |

Work 的 completed 或 stopped 都属于终态；等待满足不等于任务成功。绑定的 Work 结束后，即使后续 Work 已经开始，本次条件仍然满足。返回的是被绑定 Work 的 target，ROOT 必须查询它的实际结果。

首次已 idle 的目标在本次观察中保持已满足，不因后来收到任务而重新变为未满足；首次 `not_started` 的目标保持无法绑定，不因后来启动任务而补绑。其余目标固定观察首次绑定的 Work。这里的“不再看已满足者”只指不再追踪其后继 Work，不移除原始发送方集合中的消息观察。

“调用时”以 owner 首次状态读取的 canonical 观察点为准，不承诺以 JavaScript 提交时刻冻结远端状态。Work 在首次读取前已结束时，不能声称本次观察捕获了它的结束。

### 4.2 idle

Agent 在一次当前状态读取中同时满足以下条件，才算 idle：

1. 没有正在执行的 Work／active turn。
2. 没有仍待执行的 Work，包括排队及等待依赖的任务。

执行槽未分配、等待来信、等待工具、任务交接间隙都不单独构成 idle。W1 结束而 W2 仍排队时继续观察；W2 开始或结束期间都不改变本次观察对象。

等待期间新接纳的 Work 计入实际队列；不冻结初始 Work 集合。任务失败后，等待依赖的后续任务仍由既有调度器结算；在后续任务仍待处理时，不提前报告 idle。

idle 是观察时刻的事实，不是承诺 Agent 此后不再接收任务。返回后新安排的 Work 不撤回既有结果。

## 5. 多 Agent 与 settle

`until` 决定单个目标何时满足条件，`settle` 决定几个目标满足后返回。四种组合都是条件等待，不要求调用后必须发生一个新事件。单目标时，同一种 until 下的 first 与 all 等价。

### 5.1 条件矩阵

| until 与 settle | 满足条件与无法继续观察的边界 |
| --- | --- |
| `work_end / first` | 首次任一目标已 idle 则立即满足；否则，只要存在绑定的当前 Work，就等待任一个绑定 Work 终态，即使其他目标为 not_started。只有全部目标都无法绑定时才 unavailable。 |
| `work_end / all` | 首次 idle 的目标保持已满足，等待其余全部绑定 Work 终态；任一目标首次 not_started 则 unavailable；全部首次 idle 则立即满足。 |
| `idle / first` | 每轮任一目标当前没有正在执行或排队的任务，即满足；首次已有 idle 也立即满足。 |
| `idle / all` | 原始目标集合在同一当前快照中全部没有正在执行或排队的任务，才满足；不累计历史 idle。 |

`work_end` 的首次满足判定与绑定 Work 的终态都保持有效。`idle` 每轮重新判断：A 曾经 idle、后来又接到任务，而 B 此时才 idle，不能据历史观察声称两者现在全部 idle。

多目标的首次状态及后续每轮当前状态，均由现有关系库 owner 使用单条批量 SELECT 读取，取得共同观察快照。不能依赖 READ COMMITTED 事务内的多条 SELECT 提供同一快照，也不通过跨次缓存拼接一个并不存在的全体 idle 状态。每轮读取后结束数据库操作，不跨挂起保留事务，不增加写锁、代际或证明记录。

### 5.2 边界场景矩阵

下表仅说明条件判断；来信、中断和超时仍按第 6 节提前结束观察。

| 场景 | work_end / first | work_end / all | idle / first | idle / all |
| --- | --- | --- | --- | --- |
| A 首次 idle，B 正在执行 W1 | 立即满足 | 等 W1 终态 | 立即满足 | 等两者共同 idle |
| A 正在执行 W1，B 仅排队 | 等 A 的 W1 终态；B 不补绑 | unavailable | 等任一目标当前 idle | 等两者共同 idle |
| A 首次 idle，B 仅排队 | 立即满足；B 仍未满足 | unavailable | 立即满足 | 等 B 清空队列，且当时 A 仍 idle |
| 全部仅排队，尚未启动 | unavailable | unavailable | 继续观察 | 继续观察 |
| 全部首次 idle | 立即满足 | 立即满足 | 立即满足 | 立即满足 |
| A 的绑定 W1 结束，后继 W2 已启动 | A 已满足；可据 first 返回 | A 已满足；只等其他尚未满足者 | A 仍忙；看其他目标或继续等 | A 仍忙，继续等 |
| A 首次 idle 后开始 W2，B 的 W1 随后结束 | 首次已返回，不撤回 | 满足；A 的 W2 不影响本次条件 | 首次已返回，不撤回 | A 仍忙则不能满足 |
| A 等待中曾 idle，随后反忙，B 才 idle | 已结束的绑定 Work 不反悔 | 已结束的绑定 Work 不反悔 | 依实际观察到的当前快照判断 | 不累计 A 过去的 idle；继续等共同 idle |

等待来信、等待工具、等待物理槽位或等待依赖不等于任务结束；idle 下仍有待执行任务就算忙。绑定任务无论 completed 或 stopped，都满足 work_end；任务结果是否成功须另行查询。

首次已 idle 的目标使 first 立即返回是正常行为。调用方应处理结果、安排任务、选择仍需观察的目标或改用 all；不为防止反复立即返回而增加隐式记忆、强制延迟或累计等待上限。

不能通过反复使用 `idle / first` 的 pending_targets 缩小集合，来证明原集合后来同时全部 idle；被移除的 Agent 可能再次变忙。要求原集合共同 idle 时，必须以原集合使用 `idle / all`。

## 6. 返回合同与优先级

顶层 `state` 使用 `settled`、`message_available`、`unavailable`、`timeout`、`interrupted`。idle 是目标的观察结果；顶层 `settled` 表明 `settle` 条件满足，不表示所有任务成功。

每次结果包含：

| 字段 | 内容 |
| --- | --- |
| `state` | 本次观察的返回原因 |
| `until` | 本次采用的模式 |
| `observations` | 按输入顺序返回各目标的 `target`、`condition`；`work_end` 绑定了 Work 时附带 `work` target |
| `pending_targets` | 本次条件尚未满足的 AGENT targets，可用于新的独立观察；新观察重新确定状态与绑定，不延续本次绑定 |

目标 `condition` 为 `pending`、`work_ended`、`idle` 或 `not_started`。`idle` 模式不返回 Work 绑定；`work_end` 的 `work` 字段在等待期间保持同一对象。

`work_end` 中，首次 idle 与 not_started 描述本次绑定阶段的判定，不声明返回时 Agent 的最新忙闲状态；已满足者可能已经在做后继 Work。该模式的 pending_targets 包含尚未满足的绑定目标及无法绑定的目标，不等于当前实际忙碌者。`idle` 模式的 observations 与 pending_targets 则描述该轮共同快照中的当前忙闲状态。

例如 W1 结束、W2 已经开始时：

```js
// A 和 W1 表示从真实结果中取得的完整 Target，不是显示名称。
({
  state: "settled",
  until: "work_end",
  observations: [{ target: A, condition: "work_ended", work: W1 }],
  pending_targets: []
});
```

`message_available` 另带现有 `message_sources`；`unavailable` 另带 `unavailable_targets`，理由为 `not_started`。等待结果不包含完整 Work 正文、指令或消息正文；使用现有 query／普通输入路径读取，不重复存储。

同次读取的判定顺序为：

1. 调用方控制中断：`interrupted`。
2. 任一指定发送方存在可观察来信：`message_available`。
3. 满足 `settle`：`settled`。
4. `work_end / all` 存在任一首次 `not_started` 目标，或 `work_end / first` 全部目标都为首次 `not_started`、没有任何可继续观察的绑定 Work：`unavailable`。
5. deadline 到达：`timeout`；否则继续观察。

因此 `work_end / first` 下 A 已 idle、B 尚未启动时返回 settled；A 正在执行、B 尚未启动时仍可等 A；`work_end / all` 下 B 无法绑定则 unavailable。均不等待或绑定 B 的未来 Work。

消息提前唤醒不代表所选 until 条件满足，也不代表条件必然未满足。state 表示返回原因，observations 表示条件结果；`message_available` 与空 pending_targets 可以同时成立。此优先级针对该轮已经观察到的事实，不承诺 deadline 边界上的严格时刻裁决。

无效、不属于当前协作或已失去授权的目标沿现有调用失败路径拒绝，不伪装为 idle。目标生命周期变化沿现有 owner 验证，不能把目标消失视为任务成功。

## 7. 来信、超时与物理执行

两种模式均保留现有 Code Mode 的指定发送方过滤与 Cell 自有消息游标。原始 agents 中任一发送方的可观察来信都可提前唤醒，包括 work_end 中已满足或无法绑定的目标；不能因不再追踪其后继 Work 而忽略它的消息。列表外来信保持正常入队，不结束此次指定观察。不新增普通 Work 完成消息或中间进展自动投递。

等待只观察 Agent／Work 状态，不消费任务结果，不驱动队列执行。任务启动、依赖结算和停止沿现有调度器执行。当前消息的 FIFO、预算、资源拒绝与 provider prefix 合同保持。

deadline 在调用开始时固定，状态变化不延期。先取得变化 revision，再读状态与来信；首次已满足、按矩阵无法继续观察或 timeout 为 0 时直接按第 6 节顺序判定，不释放／重抢物理槽。0 秒不是强制 timeout，也可返回 settled、message_available 或 unavailable。确需挂起时沿用原 revision，防止状态读取与挂起之间丢失通知；后续通知只触发重查，不直接证明条件满足。

挂起与恢复复用现有执行槽 owner。观察超时或 Cell 中断不停止任何目标 Work，不取消排队任务；后续执行由 ROOT 根据结果决定。ROOT 可以继续原生查询、发送消息和停止工作。

不得通过内部反复重置 timeout 实现无限观察，不增加累计等待次数、任务总时长或 Agent 生命周期上限。若状态查询或结果组装遇到现有资源边界，沿当前明确失败规则结束，不对无限目标列表承诺一次调用必定成功。

## 8. 实施落点与 hard cut

1. `subagents/collaboration_contracts.py`：增加 `until`，按当前角色与封闭 schema 校验；删除 `wait_work` schema、参数校验及工具词汇。原生 `wait_message` 保持。
2. `capability/builtin_catalog.py`：仅保留脚本 `wait_agent`，更新默认模式、超时与返回说明，删除 `wait_work` 描述符与绑定。
3. `subagents/collaboration_api.py`：两种模式按本规格实现；删除 `_wait_works` 及入口，移除以 latest historical Work 补作绑定和 ROOT 自身等待分支。采用本规格的统一结果形态，不保留旧结果双字段。
4. `_repository/subagents.py` 与 `subagent.py`：复用 canonical Agent／queue／turn／Work 状态和已有变化通知，以最小查询适配完成批量状态观察；不复制调度器或构造新的权威缓存。
5. `tool_surface.py`、`tool_runtime.py`：删除 `wait_work` 目录与实际入口，维持 `wait_agent` 仅 ROOT Code Mode、`wait_message` 仅原生 subagent。
6. ROOT system prompt、bundled Code Mode Skill 及 references：将“等待全部工作”示例改为 `wait_agent({agents, until: "idle"})`，当前任务结果示例采用 `work_end`。从分配回执取 agents 时按 agent_id 显式去重，保留原 Work targets 查询；说明消息／超时后先检查状态再决定下一步，不把新调用当作旧绑定的恢复。删除 `wait_work` 与 `wait_agent({works: ...})` 指导；不改已确认的 subagent final answer 措辞。
7. 前端工具文案、当前合同测试和现行 dogfood：移除 `wait_work`，区分两种 until 的含义；idle 结果不显示为“任务成功”。

不保留旧会话描述符、旧 schema、旧工具别名、旧结果兼容字段或 fallback。忽略旧会话数据，按当前 clean baseline 验证。无需为本次设计增加数据库表、event、subject、guard、relation、job、消息回执、持久订阅或恢复机制。

已有 epoch 的 SYSTEM／tools 保持字节一致，messages 保持 suffix 追加；工具合同只在正常新 epoch 安装，不为热更新重建已安装前缀。

## 9. 验收要求

- 参数默认 idle／all／30 秒，合法 until、settle 与 0 至 300 秒均通过；非法值、布尔 timeout、旧 works、未知字段与 ROOT 自身 target 拒绝。
- 三个真实时序：W1 正在运行且 W2 排队、W1 已结束且 W2 正在执行、只有排队任务尚未启动；分别验证两种 until 的正确对象和返回原因。
- `work_end` 不读取历史 Work 作为本次新完成，不随 W2 启动重绑定；停止或失败的绑定 Work 正确视为终态但不标成功。
- `idle` 在 W1／W2 交接间隙、依赖等待、工具等待、来信等待、物理槽未分配时不误判；队列清空后满足；等待期间新安排的任务计入忙碌。
- 按第 5 节完整覆盖四格与边界矩阵；特别验证 work_end / first 的运行中加 not_started 组合仍可等待、全部 not_started 才 unavailable，all 遇 not_started 则 unavailable。
- work_end 中首次 idle 后收到新任务仍保持本次已满足；idle / all 则使用单条批量 SELECT 读取共同快照，不累计历史 idle，也不以逐次删除 pending_targets 代替原集合条件。
- 同 Agent 多 Work 分配后，调用方去重 Agent targets、等待 idle、逐一查询原始 Work 结果；运行时仍拒绝重复参数。
- 指定发送方消息提前唤醒并保留来源，已满足／无法绑定者仍在消息观察集合内；列表外消息不提前结束观察；中间 Work 完成不产生新自动消息。结果状态优先级与 observations、pending_targets 一致，覆盖 message_available 与空 pending_targets 同时成立。
- 两种模式均有限超时、0 秒不挂起、无关变化不延期；覆盖状态变化发生在读取前、读取与挂起之间、挂起后，且不漏唤醒或热循环。
- 真实 Host／PostgreSQL／Code Mode 验证 canonical 观察与队列推进，Cell 停止只结束观察，真实 Work 仍按原生命周期运行；原生 `wait_message` 与 final answer 回归保持。
- 旧 `wait_work` 在目录、调用边界及当前指导中完整删除；无兼容入口或新增持久类别；同 epoch provider 输入连续。

使用仓库 `.venv`，运行聚焦 schema／协作 API、真实隔离 PostgreSQL、Host／Cell 等待控制、工具目录与 bundled 指导回归。并行实现如经授权，完成后交叉审阅实际分支与竞争测试；规格未完成全部验收前不标记已实施。

## 10. 验收结果

ROOT 与 GPT-6.1-Sol high 并行实现并交叉复审，当前无未关闭问题。共同状态读取使用单条批量 SELECT，绑定 Work 状态沿现有 request execution 投影；当前 Work 查询明确只选择 AGENT_INSTRUCTION，避免把同 turn 已消费的 Agent 消息当成任务。

| 验证批次 | 结果 |
| --- | --- |
| 协作 API、until 矩阵与竞争、参数、真实 PostgreSQL、Cell 等待控制、until Host、原生收件 Host、入口权限、统一输入队列、压缩上下文载体 | 168 项测试、15 项子测试通过 |
| 参数合同、入口矩阵、能力目录、默认空值、Code Mode runtime、bundled Skill 与示例行为 | 366 项通过 |
| Code Mode 执行、Agent Host、canonical 上下文、Cell 观察、MCP readiness、Code Mode dogfood 合同 | 81 项通过 |
| 前端 runtime adapter | 167 项通过 |

Python 批次有重叠，不合计为独立测试总数。真实数据库测试从保存配置只读取得本地 PostgreSQL 目标，再创建隔离临时库；未重置主库或修改保存配置。Host 场景以实际等待挂起点同步，验证 W1 结束、W2 继续执行时 work_end 返回原 W1 target，而 idle 继续等到队列清空；原始各 Work 结果仍可准确查询。原生来信交付后的运行 turn 验证当前 Work 不被消息混淆。

矩阵测试覆盖首次 idle／running／queued 的组合、first 的可绑定加不可绑定目标、idle 反忙、新任务入队、不补绑、历史任务不重放、消息优先级、已满足目标继续来信、结果资源失败不推进消息游标，以及检查与挂起之间的状态变化。真实 Cell 中断不停止目标 Work；原生 wait_message、已确认 final answer 措辞与 provider prefix 连续性回归通过。Ruff F 与差异格式检查通过。
