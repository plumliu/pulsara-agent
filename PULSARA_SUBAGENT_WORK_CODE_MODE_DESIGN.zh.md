# Pulsara：Subagent／Work 与双入口编排设计

日期：2026-10-28。状态：产品语义与接口草案，供审阅；尚未实施。

本文整理已讨论的产品方向，并给出一套贯通示例的候选接口。AGENT／WORK、ROOT 协调、final 完成工作、原生与 JS 共用操作等是已确定方向；具体工具名称、参数拼写和返回字段是本文建议，不代表生产已经支持。本文不授权修改生产代码。

## 1. 核心语义

只有两个协作领域的一等对象：

| 对象 | target | 职责 |
|---|---|---|
| Agent | `{"kind":"AGENT","agent_id":"…"}` | 执行者的稳定身份与连续对话历史，包含 ROOT 和 Subagent |
| Work | `{"kind":"WORK","work_id":"…"}` | 一次具体请求、执行状态及其 final／失败结果 |

`agent_id:"ROOT"` 是保留标识，指向当前会话的 ROOT；普通 Subagent ID 不使用这个值。统一身份格式不改变各操作的适用对象：消息可以发给 ROOT，派活与委派工作查询仍针对 Subagent。ROOT 不因此成为 `run_agent` 的派活目标。

一次派活自动产生 Work。首次派活可以同时创建 Subagent，不要求先创建空执行者。复用时向同一个 Subagent 追加请求。同一 Subagent 按接纳顺序串行处理工作，后续工作可以排队；同一 batch 内按声明顺序确定位置，不为依赖隐式重排。不同 Subagent 可以并行。

Subagent 的有效历史保留原有内容，接续请求追加其后。已有压缩使用有效摘要与尾部，不恢复压缩前全文，也不因继续派活改写旧消息。profile 作为工作方法进入消息，不拼入通用 SYSTEM。

Subagent 使用 ROOT 的模型与 reasoning 配置，公共接口不提供模型发现和独立模型选择。每份 Work 使用哪一次 ROOT 配置快照、配置变化如何接入既有合法 epoch 边界，需要在实施规格中结合生产链路明确；不允许在已安装 epoch 内直接改写 SYSTEM／tools。

Subagent 输出普通 final 表示本次 Work 完成，由运行时保存并交付结果。完成不是对结论正确性的认证。执行失败、取消、中断分别保留实际状态。无需结果提交工具。

ROOT 负责分工、判断结果、组织讨论和决定用户目标何时完成。脚本执行已经明确的顺序、并行、等待和结果传递。多人协作与 workflow 不要求创建协作组对象，也不引入专门协调 Subagent。

## 2. 两种入口，共用一套操作

原生工具用于直接派活、交流、查看、等待和停止；JS 用于组合这些操作。

原生工具与同名 `tools.<name>(...)` 调用的参数、target、默认值、返回结构、错误以及执行语义一致。JS 专用组合扩展挂在对应操作下：`tools.run_agent.batch(...)` 组合派活，`tools.wait_agent.works(...)` 等待指定工作。batch 可以接收延迟生成参数的函数，最终形成同一底层操作接受的具体参数。业务入口统一使用 `tools` 命名空间，正式输出使用 `console.log`。两种入口进入同一执行 owner，共用权限、Hook、排队、取消和结果保存机制。JS 不通过重新调用模型实现工具执行，也不拥有另一套业务 scheduler。

工具定义维护一份，生成原生 schema 和 JS API 说明。JS 写法统一为：

```javascript
await tools.send_agent_message({
  target: { kind: "AGENT", agent_id: "agent_1" },
  message: "Please also check empty input."
});
```

ROOT／Subagent 的工具权限仍有区别。拥有 code mode 不会自动获得 ROOT 编排权限。ROOT 管理入口沿用当前 bypass-permissions 前提；跨 agent 消息是本设计明确新增的产品能力，其投递接线需要实施时完成。

## 3. 完整工具及 JS 环境清单

本表只列协作与 code mode 入口，不枚举文件、终端、MCP 等其他工具。

### 3.1 原生与 JS 共有：六个协作工具

| 工具 | 原生 | JS | 调用方 | 作用 |
|---|---|---|---|---|
| `run_agent` | 是 | `tools.run_agent` | ROOT | 新建或复用 Subagent，提交一份 Work |
| `list_agents` | 是 | `tools.list_agents` | ROOT | 分页列举当前会话的 Subagent，恢复可用引用 |
| `inspect_agent` | 是 | `tools.inspect_agent` | ROOT | 按 AGENT／WORK target 查询具体信息、工作列表或结果 |
| `send_agent_message` | 是 | `tools.send_agent_message` | ROOT、Subagent | 给指定 Subagent 当前工作发送消息；Subagent 也可给 ROOT 发消息 |
| `wait_agent` | 是 | `tools.wait_agent` | ROOT、Subagent | 只接受 AGENT，等指定 Agent 的消息或本次等待绑定的工作结果 |
| `stop_agent` | 是 | `tools.stop_agent` | ROOT | 停止指定 Work，保留 Subagent 身份和历史 |

`inspect_agent` 的名字用于协作查询，具体查询对象由 target.kind 决定。`stop_agent` 接受 WORK，因为取消的是某次工作，而不是删除执行者。

### 3.2 只有原生入口：三个 code mode 生命周期工具

| 工具 | 参数 | 作用 |
|---|---|---|
| `exec` | `code`／`path` 恰一；可选 `yield_time_ms` | 启动一次 JS 执行 |
| `wait_exec` | `cell_id`；可选 `yield_time_ms` | 继续等待原脚本并取得新输出 |
| `terminate_exec` | `cell_id` | 停止脚本后续执行 |

这三个工具不通过 `tools.*` 嵌套调用。终止脚本不等于取消已接纳的 Work；需要取消工作时调用 `stop_agent`。

`cell_id` 是脚本执行引用，不是第三种协作对象。脚本跨让出／恢复保留原局部变量与 Promise，不重新执行先前操作。其他脚本不自动共享其变量。跨脚本协作使用真实 target 查询已存在的对象。

### 3.3 只有 JS 环境入口：编排与输出

| API | 作用 | 与原生工具的关系 |
|---|---|---|
| `tools.run_agent.batch(specs)` | 按局部名称声明一批工作，先准备身份，再生成包含队友引用的初始请求，最后接纳工作 | ROOT 专用的 JS 组合扩展，复用 `run_agent` 的创建与派活语义，不是新的原生工具 |
| `tools.wait_agent.works({targets, settle?})` | 持续等待一个或多个指定 WORK 结束；默认等全部，也可等任意一个 | ROOT／Subagent 均可用的 JS 专属扩展，复用工作状态观察与结果读取，不接收 AGENT |
| `console.log(...values)` | 将脚本选择的文本或结构化结果作为正式输出返回给调用模型 | 不可丢弃的模型输出通道，不创建工作，不代表业务完成 |
| `Promise.all`／`Promise.allSettled`、变量、条件、循环 | 标准 JS 组合能力 | 不新增工具 schema 或 workflow DSL |

`console.log` 是本 code mode 环境提供的正式模型输出接口。调用内容按顺序进入 `exec`／`wait_exec` 的输出，返回给运行该脚本的模型；ROOT 脚本返回给 ROOT，Subagent 脚本返回给该 Subagent。字符串保留正文，结构化值以可检查的结构展示，不退化为 `[object Object]`。它不属于可丢弃的诊断日志，不采样、不静默丢弃。单次返回容纳不下的输出沿用分批交付或 artifact 机制，并明确提供后续读取入口；若无法接纳输出，必须显式报告错误。正式输出不意味着另建日志持久化或进程重启恢复系统。

`tools.wait_agent.works` 复用工作状态观察机制，暂停脚本时不占用普通工具执行窗口；不通过后台循环调用 `wait_agent` 实现。被恢复时遵循当前合法执行窗口。helper 与 wait_agent 一样向 ROOT 和 Subagent 开放，不授予其他 ROOT 编排权限。

code mode 的图像输出、文件作者入口等通用能力由独立的 code mode 规格定义，不因本表重复创建另一套接口。

### 3.4 记忆、终端监控与完成方式

这些是 Subagent 的公共工具暴露规则，不增加协作对象或另一套 JS 权限。

| 能力 | ROOT | Subagent | 两种入口 |
|---|---|---|---|
| `memory_search`、`memory_get` | 保留 | 不提供 | 原生与 JS 遵循同一调用方规则 |
| `remember`、`mark_memory_relation` | 保留 | 不提供 | 原生与 JS 遵循同一调用方规则 |
| `terminal_monitor` | 保留全局管理能力 | 开放注册、查看、取消当前 Work 的监控 | 原生与 JS 使用同一工具合同 |
| 工作完成 | ROOT 接收并判断结果 | 普通 final 完成本次 Work | 不需要额外结果提交调用 |

Subagent 不新增自动记忆召回或偏好注入。ROOT 提供任务所需背景；已经进入 Agent 有效历史的记忆文字与工具结果照常保留，不因工具暴露变化改写历史。ROOT 的记忆能力与设置规则保持。

monitor 记录注册者 AGENT，投递目标与订阅生命周期绑定注册时的当前 WORK，归属由实际调用上下文确定。模型不填写任意接收 Work。公开参数继续使用已有 PROCESS／MONITOR target；AGENT／WORK 是协作对象范围，不替代终端工具自己的目标类型。

- Subagent 查看、取消自己当前 Work 的 monitor；ROOT 保持同 Host 全部 monitor 的查看、取消能力。
- 进程来源与监控注册者是两回事。注册仍遵循原有进程可访问范围，不额外限制只能监控自己启动的进程。
- 通知在注册 Work 的正常输入准备点追加；模型调用和已冻结输入不被临时改写。压缩交接保留当前 Work 的监控状态。
- 注册 monitor 不阻止 final。Work 真正完成、失败、取消或中断后关闭订阅；既有完成 Hook 要求继续时，仍按活动 Work 处理。
- 关闭订阅不停止被监控进程。未接纳通知不再投递，已接纳通知保留在历史；已在确认中的投递沿原有结算处理，不转投下一份 Work。
- 同一 Agent 开始下一份 Work，只继承有效历史，不接管上一份 Work 的活跃订阅；需要时重新注册。
- Subagent 的 monitor 不自动创建新 Work。ROOT 原有活动轮投递与空闲时开启新轮的行为保持。

例子：agent_1 在 work_1 注册进程监控并接收进展；work_1 final 后订阅结束，进程可以继续运行。agent_1 在 work_2 中能看到已经进入历史的进展，但需要新的订阅才能接收后续通知。

普通 final 的共同完成链路负责保存 Work 结果、交付 ROOT、满足等待和推进成功依赖。`report_agent_result` 完整退役：移除工具定义、原生／JS 暴露、专用执行与结算分支，以及相应 Skill／提示词指导；不保留兼容入口。完成职责收敛到普通 final 链路，仍保留实际失败、取消与完成 Hook 的有效行为。

查询、依赖和 materials 读取确切 Work 的真实完成内容，不能把有界 summary 改名为完整 final。展示预览和完整材料区分；大结果沿已有 artifact／完整读取机制交付，不要求重建报告工具。

## 4. 参数与结果合同

### 4.1 `run_agent`

| 参数 | 含义 |
|---|---|
| `instruction` | 必填，本次工作请求 |
| `target` | 可选 AGENT，指向已有 Subagent；省略时创建新 Subagent，提供时复用；不接受 `agent_id:"ROOT"` |
| `name` | 仅新建时可选的显示名称 |
| `profile` | 可选工作方法，写入本次请求消息；新建默认通用方法，复用默认沿用原方法 |
| `context` | 仅新建时可选的初始 ROOT 上下文选择，沿用已支持的 `none`／`last_n` 语义；省略为 `none` |
| `materials` | 可选 WORK target 数组，引用已结束工作的 final 或公开失败材料 |
| `depends_on` | 可选 WORK target 数组，等待这些前置工作成功后启动，并提供其结果 |

profile 先沿用现有五种工作方法，具体文本另行审阅。每份 Work 在接纳时确定其有效 profile：显式提供时采用该值；省略时继承该 Agent 最近一份已接纳 Work 的有效方法，新 Agent 使用通用方法。同一 batch 按声明顺序解析。后续取消某份 Work 不改变其他已接纳 Work 的有效 profile，也不改写旧消息。模型不用搬运旧 profile 文本。

`materials` 仅提供材料，不等待成功；`depends_on` 表达真实成功依赖，前置失败会阻止该 Work 启动。两者引用同一 Work 时，沿用依赖结果优先的规则。程序也可在取得结果并判断后再派下一份工作；不要求所有流程先构造成完整依赖图。

接纳后立即返回，不隐含“等待到 final”：

```json
{
  "agent": {"kind":"AGENT","agent_id":"agent_1"},
  "work": {"kind":"WORK","work_id":"work_1"},
  "status": "running"
}
```

返回时已完成可以附带 final；调用者仍按实际 status 判断。ROOT 可以继续独立工作，也可以调用等待工具。

### 4.1.1 `tools.run_agent.batch(specs)`：先解析引用，再启动工作

`specs` 是以本批次局部名称为键的对象，每项是一份 `run_agent` 工作说明。返回对象保持相同的键，每项返回普通 `run_agent` 的 agent、work、status。局部名称只用于脚本内引用，不形成协作组、持久成员关系或第三种 target。

```javascript
const { a, b } = await tools.run_agent.batch({
  a: {
    name: "Module A developer",
    instruction: ({ b }) =>
      `Implement module A. Coordinate the interface with the module B developer: ${JSON.stringify(b.agent)}.`
  },
  b: {
    name: "Module B developer",
    instruction: ({ a }) =>
      `Implement module B. Coordinate the interface with the module A developer: ${JSON.stringify(a.agent)}.`
  }
});
```

执行顺序：

1. 读取全部成员说明。省略 target 的项预分配新 AGENT 引用；已有 AGENT target 的项复用执行者。为本批次各次工作预分配 WORK 引用。准备阶段只持有临时参数与引用，不创建持久空 Agent 或 Work。
2. 构造按局部名称索引的引用对象，如 `{a:{agent,work}, b:{agent,work}}`。每项提供将用于接纳的确切引用，不表示新对象已经存在，也不提供尚未产生的结果。
3. 以同一份引用对象求值延迟参数，生成完整初始请求；检查全部参数、成功依赖以及同一 Agent 的队列先后关系。
4. 共同接纳 owner 在一次事务中接纳整批新 Agent／Work 及依赖关系。整批接纳成功后再释放执行；启动遵循实际容量、执行者串行和成功依赖条件。

因此，成员第一轮就能看到 ROOT 明确写入的队友身份、职责与交流目的。ROOT 不必先查询 ID、复制到提示词，再发介绍消息。helper 不自动广播全体成员信息，也不自动给双方添加讨论义务。

只有 instruction 与 depends_on 支持延迟求值；materials 始终是已结束工作的普通引用数组：

| 字段 | batch 支持的值 | 求值结果 |
|---|---|---|
| `instruction` | 字符串，或 `(peers) => string` | 本次请求正文 |
| `materials` | WORK target 数组 | 已结束工作的材料引用；本批次尚未完成的 Work 使用 depends_on 表达结果依赖 |
| `depends_on` | WORK target 数组，或 `(peers) => WorkTarget[]` | 成功前置依赖；可以引用同一批次的其他 Work |

参数函数同步生成参数，不在其中启动工具、等待工作或编排副作用。它们接收的 peers 引用与返回对象使用相同局部名称，但只包含 agent／work。真实执行结果仍由等待或查询取得。普通 `tools.run_agent(...)` 和原生 schema 继续只接收具体字符串与 JSON 值，不接收函数。

队友身份引用不等于成功依赖。两份 instruction 可以相互引用 agent 而并行执行。启动顺序同时受 depends_on 和同一 Agent 的串行队列约束：同一 Agent 按接纳顺序追加，同批按声明顺序确定位置，不因依赖自动调换顺序。将两种先后关系合起来会形成等待环的请求，在接纳前拒绝。

例如同一 Agent 的两份工作，先产出再使用是合法的：

```javascript
// agentX 是已有的真实 AGENT target。
await tools.run_agent.batch({
  produce: { target: agentX, instruction: "Produce the proposal." },
  use: {
    target: agentX,
    depends_on: ({ produce }) => [produce.work],
    instruction: "Review the proposal from the prerequisite."
  }
});
```

下面的顺序不能接纳，因为 use 排在 produce 前，却要求等待 produce；应由 ROOT 调整声明顺序，不由调度器暗中重排历史：

```javascript
await tools.run_agent.batch({
  use: {
    target: agentX,
    depends_on: ({ produce }) => [produce.work],
    instruction: "Review the proposal from the prerequisite."
  },
  produce: { target: agentX, instruction: "Produce the proposal." }
});
```

所有初始参数生成完成后才开始本批次执行，不等于所有成员在同一时刻进入模型调用。排队、前置依赖和工作结束仍会影响消息接收方是否活动；引用存在不承诺对方一直在线。

batch 成功返回表示整批工作已经接纳，不表示 final 已产生。参数函数抛错、校验失败或接纳事务失败时，本批不产生新的持久 Agent／Work，也没有成员开始执行；已存在的 Agent 保持原状。提交结果尚不能确认时沿既有确认链路核实，不能直接断言未接纳并重派。

整批接纳后，各 Work 独立执行：某份工作失败按实际结果结束，成功依赖它的工作按依赖失败结束；不回滚其他已发生的执行。接纳原子性不等于执行原子性，也不要求所有成员同时进入模型调用。

普通 run_agent 和 batch 使用同一个支持整批准备与接纳的 owner；普通调用是一项请求的情况。batch 不能仅实现成逐项调用 run_agent 的 Promise.all，也不向公共 schema 暴露预分配 ID 字段。不新增批次领域对象。

### 4.2 `list_agents` 与 `inspect_agent`

- `list_agents({cursor?, max_items?})`：返回 agent target、显示名称、当前工作与排队概览、`next_cursor`。不默认输出全部历史。
- `inspect_agent({target: AGENT, cursor?, max_items?})`：查看指定 Subagent，以及分页的工作请求、状态和结果概览；本委派查询入口不接受 `agent_id:"ROOT"`。
- `inspect_agent({target: WORK})`：查看一次工作及其实际结果。返回所属 agent、instruction、status，以及完成时的 final 或失败时的诊断。

WORK 示例：

```json
{
  "target": {"kind":"WORK","work_id":"work_1"},
  "agent": {"kind":"AGENT","agent_id":"agent_1"},
  "status": "completed",
  "final": "The parser reads the previous field name. See parser.py:42."
}
```

final 属于这个确切 Work，不是“该 agent 最新的回答”。结果长度与 artifact 引用复用公共工具输出规则，截断展示不冒充完整 final；`materials` 由运行时读取实际结果，不要求模型复制展示预览。

### 4.3 `send_agent_message`

同一参数形状，通过 target.agent_id 指定接收者：

```json
{"target":{"kind":"AGENT","agent_id":"agent_1"},"message":"Please also inspect null input."}
```

```json
{"target":{"kind":"AGENT","agent_id":"ROOT"},"message":"The two approaches change different public behaviors; a decision is needed."}
```

接收者统一由 AGENT target 表达；`agent_id:"ROOT"` 指当前会话的 ROOT。发送者由运行时提供。

指向 Subagent 的 AGENT target 在接纳消息时解析到其当前正在执行的 Work，返回实际接收 work 的 target 和投递状态。已接纳消息不因 agent 开始下一份工作而改投。agent 没有当前活动工作时返回该事实；若工作已排队但尚未启动，结果明确标记目标 `status:"not_started"`，不接纳这条消息、不启动工作、不悄悄发给下一份排队请求。

queued 表示接受投递，不表示模型已阅读。消息在既有输入安全点追加，不重写已安装历史。向 ROOT 发送的消息进入其消息投递链路，不打断正在写出的 provider 请求；ROOT 空闲时是否自动开启新轮属于实施时需要核对的既有通知合同。

直接交流所需的对方 target、职责与问题由 ROOT 提供。成员不会仅因同时运行就自动获得全体通讯录。Subagent 使用原生消息工具即可，不需要为单条消息编写脚本。

### 4.4 AGENT 等待与 JS 专属 WORK 等待

ROOT 与 Subagent 都可以调用原生 `wait_agent`，或在 JS 中使用相同参数的 `tools.wait_agent`，两者的 targets 只接受一个或多个 AGENT。精确等待一个或多个 WORK 使用 JS 专属 `tools.wait_agent.works`。等待保持调用者当前 Work 活动，不输出 final、不创建新 Work、不结束现有 Work。

| 接口 | targets | 等待含义 |
|---|---|---|
| 原生 `wait_agent`／JS `tools.wait_agent` | `AGENT`，普通 Subagent | 对方发来新消息，或本次等待绑定的工作结束 |
| 原生 `wait_agent`／JS `tools.wait_agent` | `AGENT`，`agent_id:"ROOT"` | 等 ROOT 发给调用者的新消息；ROOT 不被虚构成一份可等待的 Work |
| JS `tools.wait_agent.works` | `WORK` | 精确等待指定工作结束并取得结果，不随 agent 后续工作变化 |

等待消息或结果：

```json
{
  "targets": [{"kind":"AGENT","agent_id":"agent_B"}],
  "timeout_seconds": 30
}
```

在 JS 中精确等待一份工作：

```javascript
console.log(await tools.wait_agent.works({
  targets: [{ kind: "WORK", work_id: "work_1" }]
}));
```

等待多份工作中的任意一份结束：

```javascript
const result = await tools.wait_agent.works({
  targets: [
    { kind: "WORK", work_id: "work_1" },
    { kind: "WORK", work_id: "work_2" }
  ],
  settle: "first"
});
console.log(result);
```

**目标绑定。** 开始等待时，将普通 AGENT 绑定到它当前执行中的 Work；若没有活动 Work、工作已排队但尚未启动，立即返回 `state:"unavailable"`，在目标状态中标明 `status:"not_started"`，不挂起等待、不启动对方。若没有未结束工作，则可以返回最近一次已结束 Work 的实际结果。既没有工作也没有待交付消息时，立即返回该 Agent 无工作可等待的状态。已有匹配消息先按新消息条件交付，不因对方已经结束而漏掉。

返回所绑定的 WORK 引用，不在一次等待中追随下一份工作。需要精确等待已排队或指定的工作时，在 JS 中调用 `tools.wait_agent.works`；它可以等该 Work 按正常调度启动并结束，但等待本身不改变其启动条件。ROOT 只作为消息来源，没有 Subagent Work，不套用上述 not_started 判断。

**新消息。** 等待接纳时先检查发给当前调用者、来自所选 Agent、尚未进入模型上下文的消息，再订阅后续到达；发送与等待之间已经到达的回复不能漏掉。收到消息即满足消息条件，不要求 reply_to，也不把消息自动认定为问题已解决。消息正文沿正常消息链路进入当前上下文，工具结果只给出唤醒原因和发送者引用，不再复制正文。不订阅双方所有通信或读取对方私有对话。

**不通知对方。** wait_agent 只观察调用者的收件消息与指定工作状态，不自动向目标发送“有人在等你”、不创建问答请求、不启动对方工作。需要对方做什么由 send_agent_message 明确表达。UI 可以展示等待状态，但不因此增加模型输入。

**AGENT 集合与返回。** wait_agent 的 targets 必填且非空，每项均为 AGENT。来自任一所选 AGENT 的新消息会立即返回 `message_available`，不受 settle 限制。没有匹配消息时，默认 settle=all，只有每个目标都绑定了已结束的 Work 才返回 settled；settle=first 则只需任一绑定 Work 结束。ROOT 只有消息条件，不被排除出 all 的目标列表，也不虚构完成的 Work。没有可等待工作的普通 AGENT 返回 `unavailable` 及该目标，不进入无对象的等待。

例如等待 `[ROOT, AGENT B]`，B 绑定的工作已结束但 ROOT 没有消息：all 继续等待 ROOT 消息或超时，first 可以因 B 的工作结束而返回。纯 ROOT 等待只由 ROOT 新消息、超时或执行中断结束，不因空工作集合自动完成。任何模式下，一条匹配消息都立即唤醒，不要求收齐所有人的消息。

候选 state 为 message_available／settled／timeout／interrupted／unavailable。返回保留所解析的引用和真实结果；示例字段如下：

```json
{
  "state": "settled",
  "bindings": [
    {
      "target": {"kind":"AGENT","agent_id":"agent_B"},
      "work": {"kind":"WORK","work_id":"work_B"}
    }
  ],
  "works": [
    {
      "target": {"kind":"WORK","work_id":"work_B"},
      "agent": {"kind":"AGENT","agent_id":"agent_B"},
      "status": "completed",
      "final": "The interface includes an errors array."
    }
  ],
  "pending_targets": []
}
```

message_available 另给 `message_sources`（匹配的 AGENT target 数组），实际正文由正常消息投递。works 是已绑定工作的当前状态和可用结果；ROOT 不占一个伪造的 work 项。失败、取消、中断、依赖失败等终态满足“工作结束”，但不冒充 completed。pending_targets 列出当前尚未满足的目标；发生消息唤醒后由模型判断是否继续等待。

**等待期间。** 本次等待时间沿用生产支持的边界，0 为立即查询；超时只结束这次等待，不停止当前 Work 或目标 Work，也不重新发送问题。调用者可以再次等待。等待期间不空转模型，不占用模型调用与普通工具执行资源；让出物理执行名额，使需要回复的队友可以推进。调用者仍占据自己 Agent 的当前 Work 串行位置，同一 Agent 的下一份 Work 不得越过它；恢复时重新取得执行名额。不能通过把等待中的 Work 当成已结束来腾出容量。Subagent 调用者的 Work 进入任一终态时，结束归属于它的尚存等待；同一 Agent 的后续 Work 不接管该等待。ROOT 没有这里的 Subagent Work 生命周期，不能据此把 ROOT 的普通 final 当成删除 code mode cell 的指令。

未被选中的普通消息沿正常收件链路排队，不仅因到达就提前满足这次等待；显式用户 steer、取消等控制沿既有行为处理。ROOT 需要在等成员结果时接收其求助，应使用 wait_agent；`tools.wait_agent.works` 只等待指定工作结束，普通消息不满足它的完成条件。

**ROOT 等待指引。** 原生 wait_agent 与 JS tools.wait_agent 的说明共同包含：

> Wait for a known subagent's message or current work result. Do not wait for ROOT unless ROOT has explicitly asked you to wait for its reply.

这是一条提示词使用约定。schema 和执行端不因 agent_id 为 ROOT 而拒绝等待，也不增加“ROOT 已同意”的验证参数或特殊授权分支。向 ROOT 发消息始终与等待分开。

**JS 专属 WORK 等待。** `tools.wait_agent.works({targets, settle?})` 向 ROOT／Subagent 开放，targets 必填且非空，只接受一个或多个 WORK。每个引用始终指向确切的一次派活；已排队、等待依赖的工作也可以等待，启动仍由正常调度与依赖条件决定。等待本身不通知或启动对方。

默认 `settle:"all"`，所有指定工作进入终态后返回；`settle:"first"` 在任意一个指定工作进入终态后返回，其余工作继续运行。成功、失败、取消、中断和依赖失败都算结束，结果保留真实状态与可用 final。已经满足条件时立即返回。普通消息不使这个 Promise 完成。

该接口持续观察工作状态，不设 timeout_seconds，也不在后台循环调用 wait_agent。正常返回 `state:"settled"`、`works`（指定工作的状态与可用结果）及 `pending_targets`（尚未结束的 WORK 引用）。它不解析 AGENT 当前工作，不需要 AGENT bindings 或 message_sources。first 判断的是当前集合状态，不消费完成事件；继续等待剩余工作时，使用返回的非空 pending_targets。

JS 中的 `tools.wait_agent` 匹配到消息时，保存此次唤醒结果并让出当前窗口；先通过正常输入链路把消息交给调用模型，再在合法恢复窗口将保存的结果交给脚本。恢复时不重新按“是否未读”计算已经匹配的结果，也不在交付该消息前重复登记消息等待。这复用消息接纳与模型输入边界，不增加模型阅读回执。

两种 JS 等待均沿用上述调用者生命周期与资源让出规则。code mode 因其他输入处理或窗口关闭让出时，保留待完成的观察与 Promise；普通让出不是工作结束。明确终止脚本、取消调用者执行或观察不可继续时，持续等待拒绝 Promise 并保留实际原因，不自动取消目标工作。

新一次 wait_agent 调用会重新解析 AGENT 当前工作。若要继续精确等待先前绑定的那次工作，可在 JS 中将 bindings 返回的 WORK 引用传给 `tools.wait_agent.works`。来自所选 Agent 的消息一旦进入模型上下文，后续 wait_agent 不再把它当作新消息。

### 4.5 `stop_agent`

```json
{
  "target": {"kind":"WORK","work_id":"work_2"},
  "reason": "The requested comparison is no longer needed."
}
```

取消排队或执行中的确切 Work，返回实际状态。已结束的工作保持其结果。停止不删除 Subagent、不清除历史、不撤销已发生的修改，也不隐含停止同一 agent 的其他工作或后台终端进程。需要取消多份工作时，分别提供其 WORK target，可由 JS 并行调用。

## 5. 单 Subagent：全部使用原生工具

### 5.1 调查

调用 `run_agent`：

```json
{
  "name": "Parser investigator",
  "instruction": "Investigate failures caused by empty input. Report the cause and code locations; do not edit files."
}
```

假设返回 `agent_1` 与 `work_1`。这些 ID 只是示例；实际调用必须复制真实返回值。

中途补充，调用 `send_agent_message`：

```json
{
  "target": {"kind":"AGENT","agent_id":"agent_1"},
  "message": "Also distinguish missing input from an explicit null value."
}
```

调用 `wait_agent`：

```json
{
  "targets": [{"kind":"AGENT","agent_id":"agent_1"}],
  "timeout_seconds": 30
}
```

成员发来消息时 ROOT 处理并回复，然后可再次等待；成员先完成时直接取得相应 Work 的 final。超时后工作继续，ROOT 可以继续其他工作，再等待。

### 5.2 复用原执行者

ROOT 读完结果后调用 `run_agent`：

```json
{
  "target": {"kind":"AGENT","agent_id":"agent_1"},
  "instruction": "Continue from your investigation and check whether the same issue exists in batch input."
}
```

返回同一个 agent target 和新的 work target。原调查历史继续可用，不需要把上次 final 再作为自己的 materials 重复注入。

## 6. 多人协作：初始请求介绍队友，原生工具交流

以下代码放入 `exec.code`；也可保存为普通 JS 文件后使用 `exec.path`。

```javascript
const { a, b } = await tools.run_agent.batch({
  a: {
    name: "Module A developer",
    instruction: ({ b }) =>
      `Implement module A. The module B developer is ${JSON.stringify(b.agent)}. Agree on parse(input) -> Result before integrating the modules.`
  },
  b: {
    name: "Module B developer",
    instruction: ({ a }) =>
      `Implement module B. The module A developer is ${JSON.stringify(a.agent)}. Agree on parse(input) -> Result before integrating the modules.`
  }
});

console.log({ moduleA: a, moduleB: b });
console.log(await tools.wait_agent.works({ targets: [a.work, b.work] }));
```

两名成员在第一次模型输入中就看到对方的真实 target、职责和具体协作问题。后续讨论直接调用原生 `send_agent_message`，无需为发消息编写脚本，也无需 ROOT 再补发介绍。

两个成员没有 depends_on，因此在容量允许时并行工作。ROOT 可用原生工具继续发送指导、查询状态或停止某次工作。

A 需要 B 确认接口时，直接连续使用两个原生工具，不需要自己写脚本。以下 agent_B 表示初始请求中提供的真实目标。

先调用 send_agent_message：

```json
{
  "target": {"kind":"AGENT","agent_id":"agent_B"},
  "message": "Please confirm whether parse returns an errors array. I need this to finish the integration."
}
```

确认发送结果表示消息已接纳（例如 queued）后，再调用 wait_agent：

```json
{
  "targets": [{"kind":"AGENT","agent_id":"agent_B"}],
  "timeout_seconds": 30
}
```

A 的 Work 保持活动。B 回复时，A 收到新消息后继续；B 先完成时，A 读取其 final。超时可以再次等待，不重复发送问题。等待本身不通知 B；前一条消息已接纳意味着问题会沿消息链路交给 B，不代表 B 已经阅读。A 只有完成自己的工作才输出 final。

若 B 尚未开始执行，send_agent_message 返回目标 not_started，消息不接纳；以 B 的 AGENT target 调用 wait_agent 同样立即返回目标 not_started。A 从工具结果就能知道对方尚未启动，不假定问题已经送达，也不靠发消息或等待启动 B。JS 中使用 tools.wait_agent.works 可以等待确切的已排队工作，执行由原有调度和依赖条件决定。

如果 ROOT 明确安排“先等我回复再继续”，Subagent 可以这样使用同一个原生等待工具：

```json
{
  "targets": [{"kind":"AGENT","agent_id":"ROOT"}],
  "timeout_seconds": 30
}
```

这个调用等待 ROOT 发给自己的消息，不把 ROOT 的最终回答当成一份 Subagent 工作结果。没有上述明确安排时，按工具说明不主动等待 ROOT。

## 7. 复杂 workflow：调查、审查、修订、实现、代码审查

示例分为两个批次：调查与方案审查；ROOT 作出选择后的修订、实现与代码审查。每一批通过 `tools.run_agent.batch` 一次写出成员说明与依赖。ROOT 仍负责实质决策，脚本负责执行已经确定的安排。

### 7.1 第一批：并行调查，再由 critic 审查

```javascript
const research = await tools.run_agent.batch({
  schema: {
    name: "Schema researcher",
    profile: "research_worker",
    instruction: ({ runtime }) =>
      `Investigate the parameter schema and parser. Propose a change with code evidence. Coordinate cross-layer questions with ${JSON.stringify(runtime.agent)}, who investigates execution and callers.`
  },
  runtime: {
    name: "Runtime researcher",
    profile: "research_worker",
    instruction: ({ schema }) =>
      `Investigate execution and callers. Identify changes needed to preserve current behavior. Coordinate parameter-contract questions with ${JSON.stringify(schema.agent)}, who investigates the schema and parser.`
  },
  critic: {
    name: "Critic",
    profile: "review_worker",
    depends_on: ({ schema, runtime }) => [schema.work, runtime.work],
    instruction: "Review the two prerequisite investigation results. Identify gaps, tradeoffs and decisions ROOT needs to make. Report your assessment in the final answer."
  }
});

console.log({ research });
console.log(await tools.wait_agent.works({
  targets: [research.schema.work, research.runtime.work, research.critic.work]
}));
```

schema 与 runtime 从第一轮起知道对方是谁，可以用原生消息工具直接核对调查中的问题。critic 在两项调查成功结束后才启动，前置工作的结果由依赖机制提供，不重复填写 materials。

critic 的身份虽然已经存在，但等待依赖时没有活动模型执行；调查者不需要向尚未启动的 critic 发消息。critic 阅读已完成调查的结果，不假设两位调查者仍在线。需要进一步讨论时，ROOT 可以给原调查者追加 Work。

前置调查失败会使 critic 的依赖无法满足；等待结果保留实际状态。ROOT 阅读各项结果及 critic 的 final，决定接下来采用哪种方案。这里没有把执行 completed 当成方案获批。

### 7.2 第二批：修订、实现、原 critic 审查代码

ROOT 已决定统一缺失参数处理、保留显式 null 语义。以下 `research` 使用第一批实际返回的对象；若是另一个 cell，ROOT 将前一段正式输出的该对象作为输入写入脚本。代码示例中的占位 ID 必须替换为真实返回值，不按局部名称猜 ID。这个跨 cell 传入已有对象的步骤不同于批次内部互相填写尚不存在的成员 ID；批次内部仍由 helper 自动解析。

```javascript
const research = {
  schema: {
    agent: { kind: "AGENT", agent_id: "<returned-schema-agent-id>" },
    work: { kind: "WORK", work_id: "<returned-schema-work-id>" }
  },
  runtime: {
    agent: { kind: "AGENT", agent_id: "<returned-runtime-agent-id>" },
    work: { kind: "WORK", work_id: "<returned-runtime-work-id>" }
  },
  critic: {
    agent: { kind: "AGENT", agent_id: "<returned-critic-agent-id>" },
    work: { kind: "WORK", work_id: "<returned-critique-work-id>" }
  }
};

const delivery = await tools.run_agent.batch({
  revision: {
    target: research.schema.agent,
    materials: [research.runtime.work, research.critic.work],
    instruction: "ROOT chose to unify missing-input handling while preserving explicit-null behavior. Continue from your investigation, incorporate the other investigation and critique, and produce the complete implementation plan."
  },
  implementation: {
    name: "Implementer",
    depends_on: ({ revision }) => [revision.work],
    instruction: "Implement the agreed plan supplied by the prerequisite. Report the actual changes and verification results."
  },
  review: {
    target: research.critic.agent,
    depends_on: ({ revision, implementation }) => [revision.work, implementation.work],
    instruction: "Review the actual implementation against the final plan and your earlier findings. Report concrete defects and any remaining decisions for ROOT."
  }
});

console.log({ delivery });
console.log(await tools.wait_agent.works({
  targets: [delivery.revision.work, delivery.implementation.work, delivery.review.work]
}));
```

revision 复用原调查者，review 复用原 critic，各自的旧历史保持原样。implementation 是新 agent。批次内通过局部名称引用 WORK 依赖，helper 准备真实引用；启动顺序为修订成功、实现成功、代码审查。review 同时取得定稿与实现结果。这里不再手写一连串等待与下一步派活。

已接纳的依赖工作由共同调度链路推进，不依赖脚本保持在 await；脚本终止不自动取消它们。ROOT 要停止后续工作时，对相应 WORK target 使用原生停止工具。

ROOT 阅读代码审查的 final，决定完成还是继续修改。若需要单次修订，原生 `run_agent` 即可：target 使用 delivery.implementation.agent，materials 使用 delivery.review.work；无需为了这一条请求再写 batch。

本例中的审查与实现是阶段依赖，不要求它们在运行中互相聊天。需要双向实时讨论的工作应安排为可同时活动的请求，并在 instruction 参数函数中明确介绍对方；不能一边让 B 等 A 完成，一边要求 A 等 B 在线回复。

### 7.3 停止不再需要的工作

ROOT 决定某份工作不再需要时，既可原生调用，也可在 JS 中执行同一个操作：

```javascript
console.log(await tools.stop_agent({
  target: { kind: "WORK", work_id: "<returned-work-id>" },
  reason: "ROOT has enough evidence for this comparison."
}));
```

脚本的明确条件也可以触发停止；条件必须表达实际产品判断，不能把“任意一个成员结束”自动等同于“其他成员都不需要”。

## 8. 提示词与 bundled skill 分工

| 位置 | 内容 |
|---|---|
| 原生工具定义 | 本工具参数、target、结果、派活／消息／等待／停止的必要语义 |
| code mode 环境说明 | exec／wait_exec／terminate_exec、工具代理、输出、持续等待与脚本生命周期 |
| 通用 SYSTEM | 简短指出：简单委派使用原生工具；多人协作或多阶段编排按需读取 bundled subagent skill |
| `pulsara-subagent` bundled skill | 连续复用、结果材料、`tools.run_agent.batch` 的延迟参数与局部引用、`tools.wait_agent.works` 的工作集合等待、多人协作、JS workflow、ROOT 判断节点与完整示例 |

Subagent 发消息不以读取复杂 workflow skill 为前提。模型不需要知道实验过程、历史接口或内部协议名称。这里不新增独立的群组 skill 或 workflow 语言。

## 9. 与现有规格及生产的关系

### SUPERSEDES 范围

本文作为本轮接口讨论基线，替代旧方案中以下产品表述；不宣称对应代码已经变更。

| 来源 | 本文负责的新语义 |
|---|---|
| `PULSARA_TASK_HISTORY_APPEND_IMPLEMENTATION_SPEC.zh.md` | 执行者可持续复用，多份 Work 追加到其有效历史；身份不再由一次旧 task 的 ID 同时承担。保留历史不改写、profile 消息化方向；内置记忆仅 ROOT，monitor 注册者为 AGENT、投递与生命周期绑定当前 WORK，完成统一使用普通 final。 |
| `archived_docs/ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md` 及现有 bundled subagent skill | 本文的六个共享操作、AGENT／WORK 身份、普通 final 完成、原生消息和 ROOT 统一协调。 |
| `experiment/quickjs-code-mode` 分支的 Code Mode 规格 | 本文的原生／JS 暴露矩阵、等待合同及正式模型输出接口；`console.log` 承担不可丢弃的正式输出，不采用原实验的 best-effort 诊断日志语义。 |

记忆工具、monitor 和普通 final 的目标合同见 §3.4。旧文件保留为上下文，不作为新对象与接口的定义来源。

### 进入实施前需要闭合的代码映射

1. 将现有数据库中的执行与历史链路映射到稳定 Subagent 和各次 Work；复用 ROOT 的对话引擎。本文不规定新增数据库表或持久恢复系统。
2. 将普通调用与 batch 接到同一个接纳 owner，落实整批事务接纳后释放执行、同一 Agent 的接纳顺序和接纳时 profile 解析；持续等待接入既有状态观察与窗口恢复。
3. 核对 ROOT 配置快照、现有状态枚举、消息向空闲 ROOT 的投递和最终结果的公共输出表示；落实 AGENT 的等待绑定、未读消息检查、当前 Work 挂起与唤醒，ROOT 等待限制只进入提示词。
4. 将记忆调用方规则同步到原生／JS 暴露，将 monitor 投递、查看、压缩交接及终态关闭绑定当前 Work；普通 final 统一承担完整结果保存与交付。
5. 用现有 schema lowerer 检查这份候选参数设计。工具参数保持精确本地校验，说明保留必要的动作／字段关系；不宣称自然语言示例已经通过真实 provider 验证。

实施仍须保持已有 provider 前缀规则、实际副作用结算和合法的压缩／cold 边界。当前阶段仅形成这份设计文档。
