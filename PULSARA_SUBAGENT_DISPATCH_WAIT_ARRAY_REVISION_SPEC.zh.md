# Subagent 派发与等待：数组接口及工作标题修订规范

日期：2026-11-07。状态：实现、自动回归和交叉复审已完成；DeepSeek 完整真实模型验收按用户决定暂缓；本文规定当前工作树的接口。

本文修订 [Code Mode 语义判断与代理工具入口规范](PULSARA_CODE_MODE_DECIDE_IMPLEMENTATION_SPEC.zh.md) 中的代理派发、等待、参数与结果形状、标题归属及相关前端展示。冲突处以本文为准。`decide`、原生控制独立性、权限边界、provider 输入连续性、停止和关闭合同继续有效。

## 1. 目标与确定的取舍

降低模型完成“派发 → 等待 → 查询结果 → 复用代理”所需记忆的规则数量：

- 只有一个派发入口 `tools.run_agent(...)` 和一个等待入口 `tools.wait_agent(...)`。
- 派发集合、返回的引用集合、等待集合统一使用数组；单项也是长度为 1 的数组。
- 不使用具名表，不保留 `.batch`、`.works` 方法，不提供单对象自动解包或整份回执自动转换。
- Agent 和 Work 都使用现有完整 Target。数组下标只对应本次输入位置，不是跨调用、跨 Cell 的身份。
- 将派发参数 `name` 重命名为 `work_title`，表示本次 Work 的展示标题。它不用于寻址、索引、去重、依赖匹配、复用或权限判断。
- Agent 是可复用的执行主体；Work 是一次具体工作。统一调用形式不合并两者的生命周期和等待语义。

模型的最短使用规则为：

> 派发用 `run_agent([工作配置, ...])`。等具体工作用 `wait_agent({works: [...]})`。等代理活动用 `wait_agent({agents: [...]})`。复制实际 Target，标题只供人阅读。

## 2. 统一派发接口

### 2.1 参数和返回

```ts
type AgentTarget = { kind: "AGENT"; agent_id: string };
type WorkTarget = { kind: "WORK"; work_id: string };

type BatchReferences = {
  agents: AgentTarget[];
  works: WorkTarget[];
};

type AgentAssignment = {
  target?: AgentTarget;
  work_title?: string;
  instruction: string | ((refs: BatchReferences) => string);
  context?: { mode: "none" } | { mode: "last_n"; turns: number };
  depends_on?: WorkTarget[] | ((refs: BatchReferences) => WorkTarget[]);
  materials?: WorkTarget[];
};

type DispatchResult = {
  agents: AgentTarget[];
  works: WorkTarget[];
  details: Array<{
    work_title: string | null;
    status: string;
    inspect: { tool: "query_agents"; arguments: { target: WorkTarget } };
  }>;
};

tools.run_agent(assignments: AgentAssignment[]): Promise<DispatchResult>;
```

上述类型中的字符串身份仍按共享 identity 层验证具体 kind；不是任意字符串。Work 状态和 `inspect` 沿用现有合同。

三个返回数组与输入数组等长、同序。`agents[i]` 是第 i 项工作的执行代理，`works[i]` 是该项工作的 Target，`details[i]` 是受理信息。只有一项时也返回这三个数组，不返回另一种单项对象。`status` 是受理时快照，之后的真实状态通过查询取得。

派发返回表示已受理，不表示工作已完成。继续复用现有原子准入和完整结果预算检查；不能因输出容量不足而受理部分成员，也不能在已受理后通过重新派发取得回执。

数组是 JavaScript 公开调用形式。Guest 在第一次 await 前捕获密集数组和普通字段，通过现有准备／bridge 边界传递唯一内部封装：`{members: [{agent_id, work_id, parameters}, ...]}`。同步函数只在 Guest 内按声明顺序各求值一次，不作为 JSON 发给后端。canonical original/resolved 参数分别记录求值后、hook 改写后的实际有序封装及来源。

Host 的 `run_agent` descriptor 校验该对象封装，Guest 的公开签名只接受配置数组；两层复用同一 assignment 字段定义。不能只把内部 descriptor 改成 array 而让 Host 仍要求 dict，也不能保留单项对象直接执行的捷径。预分配记录必须属于当前 Cell、当前准备请求；逐位置核对 Agent／Work 身份及成员数，不能只比较无序 ID 集合。所有单项、多项都经过同一个准备、封装、授权与结算路径。

输入必须是连续、非空的数组。拒绝稀疏数组、`undefined`／`null` 成员、额外数组属性、非配置对象及未知字段，不能以 `Object.entries` 跳过空洞后重新编号。回调输出也须通过字段和 JSON 校验；准备失败或取消后必须释放已有进程内准备资源。

### 2.2 创建、复用与顺序

- 不传 `target`：创建一个 Agent，向它派发本项 Work。
- 传真实 AGENT target：向已有 Agent 追加本项 Work，继续它的原有对话。
- `context` 仍只用于创建 Agent；续派不重建已有代理的上下文。
- `work_title` 在创建和续派时都可填写，每项 Work 独立保存。
- 同一批可以多次向同一个 Agent 续派：相应 `agents` 元素相同，`works` 元素不同，工作按声明顺序进入该 Agent 的 FIFO 队列。
- `target` 仅接受调用前已持有的真实 AGENT Target，不新增 target 回调、批内索引 target 或占位身份。同一批新建后再续派该新 Agent 的需求，应先取得其实际 Target，再发起下一次派发。
- 数组顺序不会使不同 Agent 的工作自动串行。需要成功前置条件时显式传 `depends_on`。
- `materials`、上下文、模型绑定、依赖失败传播及队列语义保留现有合同。

### 2.3 批内引用

保留现有“执行前取得所有成员真实引用”的能力。宿主通过共享 ID 层预分配引用，在任何成员受理启动前，将只读的 `{agents, works}` 数组交给参数函数。

只有 `instruction` 和 `depends_on` 接受同步参数函数。函数不能调用工具、返回 Promise 或等待未来结果。它只根据预分配引用构造文本和依赖数组。引用数组及其 Target 不可由 Guest 改写。

可识别的 async 参数函数在执行前拒绝；普通函数返回 thenable 时，判定为该 Cell 的不可恢复脚本契约错误，禁止该 Cell 后续提交工具，即使脚本捕获错误也不能继续提交。其他 Cell 及已提交工具的结算不受影响。函数须保持纯同步，不得安排 Promise continuation 等异步工作；此限制不是对任意 JavaScript 异步来源的追踪机制。

```js
const jobs = await tools.run_agent([
  {
    work_title: "调查停止链路",
    instruction: "调查停止链路，给出具体问题与证据"
  },
  {
    work_title: "复核调查结论",
    instruction: "检查前置调查结论，报告遗漏和建议",
    depends_on: ({ works }) => [works[0]]
  }
]);

await tools.wait_agent({ works: jobs.works });
console.log(await tools.query_agents({ target: jobs.works[1] }));
```

需要并行成员互相通信时，可在各自 instruction 回调里引用 `agents[i]`；不要等待一个依赖自己结束才能启动的 Work 给自己回信。引用说明身份，不保证对方已经启动或消息可以投递。

`refs.agents[i]`／`refs.works[i]` 与受理后的 `result.agents[i]`／`result.works[i]` 的 Target 字段和身份值相同，不承诺 JavaScript 对象引用 `===`。预分配不是受理；参数计算或校验失败时不得留下部分 Agent／Work。继续使用已有进程内准备机制，不新增可恢复准备记录或持久句柄。

## 3. 统一等待接口

```ts
type WaitArguments =
  | { works: WorkTarget[]; settle?: "all" | "first" }
  | {
      agents: AgentTarget[];
      settle?: "all" | "first";
      timeout_seconds?: number;
    };

tools.wait_agent(arguments: WaitArguments): Promise<WaitResult>;
```

`works` 与 `agents` 必须且只能提供一个，数组非空。拒绝未知字段、错误 Target kind、重复目标及混合分支。`works` 分支拒绝 `timeout_seconds`。不接受旧 `targets` 字段、具名表、完整派发回执或单独一个 Target 的隐式包装。

`WaitResult` 保留现有状态内容，具体如下：

| 字段 | `works` 等待 | `agents` 等待 |
| --- | --- | --- |
| `state` | `settled`／`interrupted` | `message_available`／`settled`／`unavailable`／`timeout`／`interrupted` |
| `works` | 所选工作的状态项数组 | 已绑定工作的状态项数组 |
| `pending_targets` | 未终止的 WORK targets | 绑定工作未终止或未绑定工作的 AGENT targets |
| `bindings` | 不提供 | `{target: AgentTarget, work: WorkTarget \| null}[]` |
| `message_sources` | 不提供 | 消息结果中的 AGENT targets |
| `unavailable_targets` | 不提供 | 不可用结果中的 `{target: AgentTarget, reason}` 项 |

这里返回的 `works` 每项包含 `target`、`agent`、`status`、`inspect` 及适用诊断字段，是状态对象而非 WORK Target；不能直接把 `observed.works` 传回等待。继续等待用本分支的 `pending_targets`，读取正文用状态项的 `inspect` 或 `target`。状态项顺序沿用所选目标顺序；Agent 分支只包含实际绑定的工作，不保证与输入 Agent 数组等长。

### 3.1 `works`：等待明确的一次或多次工作

- 只等待给定 WORK targets；不随 Agent 后续派工改变对象。
- `settle` 默认 `all`；`first` 表示任一所选 Work 到达终态。
- 普通消息不会唤醒这类等待；不增加观察超时或总工作时长限制。
- 工作终态不代表成功。已完成、已停止及其中的失败原因按现有状态和诊断判断。
- 返回继续包含 `state`、`works` 和 `pending_targets` 数组；保留真实状态及诊断引用，不自动附带全部 final 正文。
- `state` 沿用 `settled`／`interrupted`。读取正文继续调用 `query_agents`。

```js
const observed = await tools.wait_agent({
  works: jobs.works,
  settle: "first"
});
console.log(observed);

if (observed.state === "settled" && observed.pending_targets.length) {
  console.log(await tools.wait_agent({ works: observed.pending_targets }));
}
```

### 3.2 `agents`：等待代理活动

保留现有代理等待语义，不借本次接口合并改成纯消息收取：

- 在调用开始时冻结绑定。现有分支为：有当前工作则绑定；无当前工作但有排队工作则报告 `not_started`；否则有最近工作则绑定最近工作；均不存在则报告 `inactive`。不得在等待过程中改绑后续 Work。等待自己的当前 Work 仍拒绝。ROOT 别名没有绑定 Work，只观察消息／超时。
- 任一所选 Agent 有可用消息时返回 `message_available`；否则按绑定工作的终态、不可用原因或观察超时返回。
- `settle: "all" | "first"` 仅控制绑定工作的终态聚合；即使是 `all`，任一所选来源的可用消息仍会唤醒，不表示等待每个 Agent 都发消息。
- 继续保留 `bindings`、`works`、`pending_targets` 以及适用的 `message_sources`、`unavailable_targets` 数组。
- `state` 继续区分 `message_available`、`settled`、`unavailable`、`timeout`、`interrupted`。
- 原有 `ROOT` 通信别名只在已有允许它的 AGENT 通信／等待入口有效；不能据此向 ROOT 派发子代理 Work。
- 消息仍由原有投递、可见性和消费位置 owner 管理。这里是可用性观察，不增加独占邮箱、消息确认或可靠收取承诺。

```js
await tools.wait_agent({
  agents: [jobs.agents[0]],
  timeout_seconds: 30
});
```

同批向一个 Agent 派两项 Work 时，`jobs.agents` 含重复目标。若要等两项工作，直接使用 `jobs.works`；若要观察这个 Agent，明确传一次该 Agent target。继续拒绝重复 Agent，不按数组位置将同一个 Agent 解释为两个独立完成条件，也不静默去重。

### 3.3 中断和边界

等待超时或中断只结束本次观察，不停止目标 Work。停止 Cell 后，已受理 Work 仍按真实状态运行；停止代理工作继续使用 `stop_agent`。

沿用已验证的物理边界：每次派发 1–16 项、每项至多 16 个依赖和 16 个材料、每批至多 64 条显式依赖。等待目标集合不套用派发的 16 项限制，继续按现有调用输入／结果预算处理，不新增目标数量上限。Agent 观察超时默认 30 秒、合法范围 0–300 秒；这些是既有单操作边界，不是会话、Agent、Work 或历史总量上限。不得因改用数组另加累计次数或生命周期限制。

## 4. `work_title` 的语义与所有权

### 4.1 输入规则

- 将派发参数 `name` 替换为 `work_title`，不保留旧字段别名。
- 继承原 `name` 的可选性和现有 256 UTF-8 字节字段边界；传入时必须是非空白字符串，`null` 非法。该边界是既有展示文本边界，不新增另一套截断限制。
- 省略时规范值为 `null`，前端显示“未命名任务”；不自动从 instruction、历史对话或模型输出生成标题，不另发模型请求。
- 创建 Agent 与向已有 Agent 续派均允许此字段。标题不是创建专属参数。
- 标题可以重复，不要求唯一；同 Agent 的不同 Work 可以同名，不同 Agent 的 Work 也可以同名。
- `work_title` 是短摘要，`instruction` 是实际任务说明。标题不承担路由、执行授权或额外指令通道的职责。

### 4.2 保存和读取

标题属于 Work 的 canonical 记录，与该次派发在同一个原子准入事务中保存。现有 Work 由代理指令队列行承载，应在这个 owner 上表达标题；不另建标题注册表、展示实体或事件流。

准备、受理确认应携带并比较每项完整标题值，包括续派。这里的确认仅指相同预分配 Work IDs、相同源 attempt 的提交结果确认，不是用相同标题／参数对不同调用去重。新的独立派发总是新的 Work。不引入标题 fingerprint。

标题在每成员 PreTool hook 的合法参数改写范围内，最终受理值须重新校验并保存。受理后的回执、查询及 UI 都使用最终值，不能使用 hook 改写前的草稿。

本修订不新增改名工具。受理后该 Work 的标题保持不变；续派产生新 Work 和新标题，不能覆盖旧 Work。工作结束后标题继续属于该历史工作。

派发回执、工具查询 WORK 的结果、Agent 的当前工作投影、工作流图和会话任务展示均从这一 canonical 值读取。frontend 的当前 Work 身份、标题和状态在同一个读取快照中关联派生，不成为第二份可独立写入的事实。停止请求尚未结算时不能提前显示下一项标题。

UI 的当前 Work 必须关联真正的 `AGENT_INSTRUCTION` 队列行；Agent 是否正在执行、普通消息队列与 Work 队列数量是不同事实。基线通用 runtime 查询的 `current_work_id`／`queued_count` 还可能覆盖普通消息，不得直接当作标题展示所需的严格 Work 投影。复用现有查询 owner 提供类型明确的 UI 投影；不要为此直接改变 Agent 等待所依赖的通用绑定字段语义。若实现需要统一调整底层字段，必须先补齐消息驱动 turn 的等待语义分析和回归。

不得把 `work_title` 写入 Agent 的长期会话标题，或者拿它寻找／重建 Agent target。本次不新增另一个 `agent_name` 参数；Agent 原有会话身份展示沿用现有 owner，无独立标题时沿用“子代理”及必要的身份区分展示。

标题不自动拼入子代理的 instruction、SYSTEM、context 或 materials，也不参与扫描文本以引入队友可见性的逻辑。ROOT 在正常工具回执／查询中看到标题属于该次真实结果。UI 任务分隔通过现有 Work 关联展示标题，不增加模型消息、transcript entry 或修改原消息正文。

## 5. 前端展示

沿用当前前端的字体、字号层次、色彩、间距和组件风格，实施前阅读实际样式并在渲染后检查。新增标题和状态分别布局，不使用“xx · xx”式文字拼接；不用新的视觉体系替换现有界面。

### 5.1 子代理列表与标签页

Agent 的稳定身份和当前工作的标题分别显示：

```text
子代理（稳定身份短展示）          ● 工作中
当前任务：复审派发与等待接口
另有 2 项工作排队
```

- 子代理列表：保留代理身份主行，增加当前任务副行。
- 标签按钮：保留稳定的代理身份标签，显示状态点；悬停可查看当前任务完整标题。无独立代理名称时，身份短展示仅从真实 Agent ID 派生，完整 ID 可查看，不新增编号身份。标签的定位、去重、复用继续按 Agent ID，不按标题。
- 标签页内部顶部：在代理名称下明确显示“当前任务：xxx”。已经打开的页也应随工作切换更新，不只在首次打开时读取。
- 列表只展示当前任务和排队 Work 数量；各项排队标题在现有分页队列视图中读取，不在每个 AgentRow 中嵌入整个队列。新工作入队时不能覆盖尚在运行的当前任务。
- 无当前 Work 但 Agent 仍在处理消息等输入时显示“处理中”，不误报空闲，也不把消息当成 Work。只有排队 Work 时显示“待开始”或“排队中”；没有活动及待执行工作时显示“空闲”。若展示最近工作，标题写为“最近任务：xxx”，完成／停止状态另行显示。
- 历史消息的代理署名不随当前工作切换而被重写。在各次工作对应的任务分隔位置显示该 Work 自己的标题。

稳定 Agent label 与当前 Work 标题使用独立字段。不能简单把 `tab.title` 改成 `work_title`：基线将该值一路传给会话中所有历史助手署名。页眉的当前任务与用户定位的历史 Work 也必须分开。

可见工作区的标签栏需要轻量 Agent 展示元数据刷新，即使相应 pane 未选中也能更新状态与悬停信息；复用现有读取周期、owner 校验和取消机制，不为每个后台标签挂载完整会话 reader。选中 pane 同 Host 的刷新短路前也更新元数据。更新不重建 tab ID、不重开 reader、不重置滚动、阅读选择或 workLocation。关闭标签、切换根会话后到达的旧响应不得恢复旧标签或覆盖新对象。

标签栏与 pane 的元数据应共用读取／发布 owner，复用现有请求顺序校验，防止同一 Agent 的旧任务响应晚到后覆盖新标题；仅检查根会话和标签仍存在不足以防止同对象乱序。不为此新增 generation 或全局注册表。

### 5.2 工作流图

继续按现有 Agent 分组及 Work 依赖关系展示。每个工作行以自己的 `work_title` 为主要文字，状态独立显示：

```text
子代理（稳定身份短展示）
  修复停止链路          已完成
  复审派发与等待接口    运行中
  补充接口回归测试      排队中
```

工作标题替代当前工作行中直接展示的完整 Work ID；实际 ID 可在详情／悬停中读取。点击、连线、选中与定位继续使用 work_id，不使用标题。旧工作不能显示该 Agent 的新工作标题。

图中某个 Cell 涉及的 Work 不一定是该 Agent 此刻正在处理的 Work。图节点必须显示所选 Work 本身的标题与状态，不能拿 Agent 当前任务副行替代节点事实。相同标题不合并节点。

现有读取接口需提供足够的标题事实：`AgentRow` 的当前工作投影、`WorkFact`、工作详情及任务分隔读取。复用现有读取和刷新 owner，不因展示标题增加持久事件或全局刷新机制。

标题还需贯穿现有队列／canonical entry 元数据、协议序列化及前端投影，不只改 graph API。历史 `AGENT_INSTRUCTION` 按其自身 `input_source.queue_item_id` 关联 Work；`source_work_id` 是来源工作，不能用它找接收方本次任务的标题。读取使用既有分页及批量关联，避免逐消息额外调用 work API。

已消费 Work 在原有任务分隔位置展示标题；未启动就停止的 Work 没有 turn 或 consumed entry，仍从 canonical Work 行读取，并在现有队列／所选终止工作卡片展示。不能为显示标题伪造 transcript 消息，也不能在该卡片显示 Agent 当前或最近的另一项标题。

## 6. Target 与跨 Cell 使用

Cell 的局部数组不自动进入下一个 Cell；Agent 和 Work 的真实身份不依赖这些变量存在。跨 Cell 复用时，使用先前正式输出中的 Target，或查询取得实际 Target。

不得要求按 `work_title`、Agent 显示名称、数组下标或本地变量名找对象。不同查询返回的第 0 项不保证是同一个 Agent；分页及排序变化也不能改变 Target 身份。

```js
const first = await tools.run_agent([
  { work_title: "排查停止链路", instruction: "检查取消传播" }
]);
await tools.wait_agent({ works: first.works });

const next = await tools.run_agent([
  {
    target: first.agents[0],
    work_title: "复审统一接口",
    instruction: "检查数组派发与等待的公开合同"
  }
]);
await tools.wait_agent({ works: next.works });
```

以上两次调用在同一 Cell 内展示复用关系。跨 Cell 时需要显式传递／重新读取实际引用，不能直接引用上一 Cell 的 `first` 变量。同一个 Agent、两项不同 Work 各有各自标题。

## 7. 暴露矩阵、硬切与输入连续性

| 入口 | 派发 | 等待 | 查询／通信／停止 |
| --- | --- | --- | --- |
| ROOT Code Mode | `run_agent(数组)` | `wait_agent({works})` 或 `wait_agent({agents})` | 现有三项操作 |
| ROOT 原生 | 无 | 无 | 保留 `query_agents`、`send_agent_message`、`stop_agent`，不依赖 Cell 名额或 sidecar |
| 子代理原生 | 无 | `wait_agent({agents})`，不开放 `works` 分支 | 保留既有查询、通信；不开放停止其他代理 |

模型可见合同按调用角色独立提供。原生 catalog 不登记 `run_agent` 或 `decide`；两者只在脚本 API 目录中拥有 schema 与宿主执行元数据。原生 `wait_agent` 的调用 owner 仅为子代理，ROOT 的脚本 `wait_agent` 使用独立的 schema 和说明。宿主结算、取消及执行服务继续复用，不把脚本 API 注册成原生 capability 后再隐藏。

ROOT 的系统提示及 bundled Code Mode Skill 只说明 ROOT 可用的调用方式，不介绍子代理的原生等待入口。子代理的提示及原生工具定义只说明其自身调用方式，不教授 ROOT 的脚本 API；子代理的默认 Skill 投影不包含 ROOT 专属的 bundled `pulsara-code-mode` 手册。此处规定主动提供的调用指导，不改变文件读取权限或用户显式共享上下文的语义。


权限范围沿用现有合同：ROOT 的 Code Mode 与协作入口仍限 bypass-permissions；不借本次接口收敛扩大其他权限模式或子代理能力。

一次硬切删除旧单项对象派发、具名 batch、`.batch`／`.works` helper、旧等待 `targets` 字段及派发 `name` 参数。内部批次用有序成员和真实 ID 关联，删除仅为具名表服务的成员 key、peer 名称映射及校验。数组位置是局部关联信息，不是新的 Pulsara 对象 ID。

继续复用唯一协作后端、既有 batch 准备与原子受理机制、工具结算、Code Mode 来源校验、取消与关闭汇合。不得为相同能力保留新旧双路径或输入猜测。

更新 clean-v0 baseline、生产读取、测试、dogfood、系统提示与 bundled Skill。若因内部 Work 字段调整需要清理开发状态，按仓库规则核验后重建可丢弃的本地数据库，不为未发布内部设计引入在线迁移链。

provider 前缀仍只能在新 cold epoch 或显式采用的 compaction successor 边界重建。不能因接口、标题或 UI 变化重写已安装的 SYSTEM/tools 或既有消息。到达宿主的旧参数／原生调用按实际来源和新参数合同正常拒绝；已删除的 JS `.batch`／`.works` 调用可能直接产生现有 Cell 脚本错误，无需也不得为返回工具错误而保留 helper stub。两者均不因这次非法调用创建工作，不异常终止 ROOT；保留此前已受理工作的真实状态，不为旧输入增加兼容执行路径。

### 7.1 Schema 与实际授权

脚本 API 目录的 `wait_agent` schema 包含 `works`／`agents` 两个互斥分支，子代理 native schema 只包含 `agents`。通过共用分支 schema 定义构造各角色独立的入口合同，并为每份实际 descriptor 配对其精确 execution binding；不能仅修改 provider 展示 schema 而破坏现有 specs／binding 对应关系，也不能让 JS 目录偷偷绕过 catalog 校验。

实际 prepare／authorize／invoke 和协作 owner 都须按真实 scope／source 拒绝子代理的 `works` 分支，不能因统一名称 `wait_agent` 已在子代理白名单就放行全部参数。ROOT 原生等待仍拒绝。Agent 重复目标校验须在 `ROOT` 别名解析后再次按真实身份检查，保留原有可见性与消息游标归属。

已有会话按已丢弃处理。开发合同变更不保留旧描述、拒绝 leaf、旧会话迁移或恢复分支；当前合同的 descriptor 不一致直接作为结构错误处理。当前版本新建会话内的前缀连续性仍按正常运行合同验证。

## 8. 实施落点与验收

主要实施位置：

- `conversation_kernel/subagents/collaboration_contracts.py`、`collaboration_api.py`：数组派发、统一等待分支、标题校验及一致回执。
- `code_mode/node/guest.js` 及现有 bridge／准备 owner：统一数组捕获、冻结引用、同步回调、删除具名 helper。
- `capability/builtin_catalog.py`、`tool_runtime.py`、`tool_execution.py`、`tool_surface.py`：schema、源准入、角色矩阵与同一路径结算。
- canonical Work 队列与现有 repository／reader：标题保存、确认、查询和前端投影；不新增独立 owner。
- `terminal_kernel_v3.proto` 的队列／输入来源元数据、`canonical_v3.py` 与前端 ProtocolQueue／ProtocolEntry 到 QueuedPrompt／Message 的投影：按自己的 Work 关联传递展示标题，不改变 provider 输入正文。
- `frontend/lib/agent-cell-api.ts`、`agent-cell-workspace.tsx`、`agent-conversation-pane.tsx`、标签及会话任务展示：读取标题和状态，按真实身份刷新及定位。
- 系统提示、bundled Code Mode Skill、原方案活动说明和测试／dogfood：全部描述数组、Target 和 `work_title` 的新合同。

必须验证：

1. 单项和多项使用完全相同的输入／返回形状；三个返回数组同序、等长，完整保留状态与 inspect 信息。
2. 批内回调拿到真实冻结引用；稀疏数组／undefined、调用后修改数组、异步回调、成员调换、跨 Cell 准备引用均拒绝或被正确隔离。联合 FIFO 的依赖环、参数失败、资源边界在部分受理前拒绝；同 Agent 多次续派维持 FIFO 和不同 Work 身份。
3. 创建与续派均可带 `work_title`；省略、空白、非法类型、字节边界、重复标题及受理确认均正确。标题不参与索引和授权。
4. `works` 与 `agents` 两分支保留各自的消息、终态、all／first、超时、中断与可见性语义；重复目标和非法字段明确拒绝。
5. ROOT 原生 run／wait 仍不可执行；子代理保留 Agent 活动等待，不获得 Work 等待、派发或停止权限。Code Mode 不可用时原生三个控制入口仍可用。
6. Cell Stop、Host 关闭、VM 排队、工具结算、已受理 Work 保留、同 epoch provider 前缀连续性继续通过。
7. 同一 Agent 先后执行不同标题的工作，排队时不抢占当前标题；消息驱动 turn 不误报空闲或 Work 数量；同 Host 标题更新、未选中标签、切根会话后迟到响应、关闭重开标签均正确。阅读状态与历史定位保持，同标题不同 Work 分别定位；冷读历史与未启动即停止的卡片使用自己的标题。
8. 非法调用与已删除 JS helper 不创建工作；当前入口的角色、来源和参数校验正常拒绝误用，不添加旧会话兼容。
9. 用真实 Node／Guest 运行更新后的 Skill 示例；使用保存的真实 provider 配置做必要的单项、批量、依赖和续派验收，核对实际入参、Target、标题、结果与清理。配置只读，证据排除实际凭证值。
10. 模型认知验收覆盖“单项派发、整批等待、选定 Work、消息观察、同 Agent 续派、跨 Cell 查询复用”，检查是否误用旧 helper、具名表、标题寻址或把消息当成任务完成。未经实际验收，不宣称已证明模型误用率下降。
11. 第 0 项及后续成员的 Pre／Post hooks 都运行且不递归；Pre 改写 work_title 后保存／确认／回执一致，身份改写、参数超限或后项拒绝均整批零受理。Post 失败不回滚或重复工作；确认相同 attempt 与新的相同参数派发分别得到正确结果。
12. 直接尝试子代理 `wait_agent({works})` 被实际 owner 拒绝；JS 与 native 各自 descriptor／binding 精确对应；ROOT 别名解析后的重复 Agent 拒绝。不能依靠模型不发送非法参数来证明权限正确。

### 8.1 实现风险速查

| 易错做法 | 必须保持的行为 |
| --- | --- |
| 只改公开 schema 为数组 | Guest 数组与 Host members 对象由同一成员定义校验，单项也走准备链 |
| 用 Object.entries 捕获输入或按 Agent ID 建回执字典 | 不遗漏空槽，不去重同 Agent 的不同 Work，三个数组保持逐位置对应 |
| 将同名 run_agent 的外层与成员 hooks 混为一谈 | 明确成员位置，第 0 项不等于外层，Pre 后整批重验，受理一次 |
| wait_agent 名字在子代理白名单就直接执行 | 真实 scope／source 再限制参数分支，子代理不能等 works |
| 从 Agent title 保存／确认 work_title | 标题只落当前 Work 的 canonical 行，创建与续派一视同仁 |
| 用标题或参数相同判断是旧工作 | 相同源 attempt 与真实预分配身份才能确认提交；新调用产生新 Work |
| UI 改 tab.title 或只在 reader 重建时更新 | 稳定身份和任务标题分离，同 Host 也刷新，历史署名不变 |
| 无 current_work_id 就显示空闲 | 严格 Work 投影与 Agent 活动、普通消息队列分开 |
| 按 source_work_id 或当前 Agent 找历史标题 | 按接收方自身 queue_item_id／Work 读取，未消费工作也有标题 |
| 为展示任务分隔添加模型消息、自动补标题 | 只更新读取元数据，不新增 provider 输入或标题生成请求 |

## 9. 实施验证

Python 使用仓库 `.venv`，PostgreSQL 使用独立临时本地集群，集成测试在 `CI=1` 下执行。保存的生产设置只读，真实模型验收使用独立工作区、Pulsara home 和测试数据库。

### 9.1 自动验证与界面

- 当前入口归属、角色隔离、真实 Host 执行、脚本 schema 与 Skill 的定向验证 105 项通过；正常调用、非法来源、取消及前缀连续性断言保留，不构造旧会话兼容场景。
- bundled Skill 21 项测试通过，包括真实 Node／Guest 执行主文档双成员示例和带 Decision 分支的实现／复审流程；与上述定向验证有重叠，不累计为独立总数。
- 验收记录器相关 3 项测试通过；真实 Host 产生冻结对象、派发及 Work 快照后，阶段记录和关闭记录均可完整保存。68,800,219 字节证据写盘校验内容完全一致，事件循环最大心跳间隔 0.482 秒，序列化与凭证清洗在线程中进行。
- 前端全套 102 个测试文件、1,667 项通过。TypeScript、ESLint、桌面构建与生成协议合同检查通过；ESLint 保留 10 个现有警告，构建保留已有 chunk 提示。
- clean-v0 迁移邻接检查 16 项通过；`test_compatible_vector_and_unrelated_native_extension_are_preserved` 所在环境缺少 PostgreSQL `intagg` 扩展，相关用例未能通过，未改断言或跳过。实际受影响的 schema、catalog、Work 标题保存、不可变性及查询已通过临时数据库验证。
- 使用生产组件的 `/follow-demo.html?agents` 做浏览器渲染，确认项目字体成功加载、列表与当前任务分离、同 Host 工作切换、历史标题保留及工作流各 Work 标题展示。截图保存在 `output/playwright/subagent-array-fonts.png`、`subagent-array-next-work.png`、`subagent-array-graph.png`。

### 9.2 交叉复审

复用原来的八位 GPT-6 Astra subagent。入口与 Guest、hook 与 runtime、协作 API 与 canonical 存储、协议读链与前端元数据 owner 均完成双向复核，另有整链独立复审。发现的准备回执超限资源清理、异步回调返回后的工具提交、权限与测试调用接缝均已闭合。Skill 主示例经过独立复核，明确提供新 Agent 的任务上下文。

### 9.3 真实模型证据

验收入口为 `tests/dogfood/run_subagent_array_dogfood.py`。只向保存的 DeepSeek 发送自然任务，不给模型预写 Cell；记录实际 provider 请求与返回、冻结源码、工具参数和结果、Work 与历史、日志及最终文件。认知结果按实际行为评估，不对调用次数、代码措辞或选定脚本加断言；权限、真实身份、同 epoch 前缀及物理关闭仍作必要验证。

已保存的首轮有效行为证据包含 51 次 DeepSeek 模型调用、2 个 Agent、4 项 Work 和 4 个 Cell。模型通过单元素数组派发，跨 Cell 复用实际 Agent Target，完成 Work 等待及 Agent 消息等待；同 epoch provider 输入连续性检查覆盖 48 对相邻请求。该证据不代表多成员同批与依赖续派的两轮完整验收已通过。

依据首轮实际行为，bundled Skill 主文档补充了提供任务上下文的双成员数组与同步引用回调示例、消息 Target 的完整嵌套形式，以及工作查询不能作为完整通信日志的边界。说明直接教授当前正确用法，错误形式仅作为反例。

验收记录器曾遇到大日志同步写盘阻塞与冻结对象复制失败，已修复并用真实 Host 数据及完整大日志回归验证。其后一次真实运行记录到 DeepSeek HTTP 500，重试流仅返回片段后持续停滞，达到观察超时；该次 Session 物理关闭与独立数据库删除均完成。用户结合社群信息确认服务端故障，决定暂缓后续 DeepSeek 验收，不将其作为本次实现缺陷或完整验收成功。

本地原始证据保存在 `output/subagent-array-dogfood/`：`report.json` 保留首轮模型行为，`first-trial-observations.json` 为其派生观察，`final-report.json` 保留 HTTP 500 与流停滞诊断；`retry-report.json` 和 `responses-report.json` 保留用户要求停止时的运行记录。日志与截图沿用仓库输出目录规则，不纳入源码提交。
