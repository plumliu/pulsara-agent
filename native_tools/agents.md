# Pulsara Agent 协作工具修订与使用教程草案

共用规则见[原生工具修订草案索引](../archived_docs/PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整保存、原生 12k／NEXT、JS 完整返回及 Cell 输出沿[统一交付草案](../archived_docs/PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)，正文与业务字段存储沿[数据库 hardcut 草案](../archived_docs/PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。

状态：当前生效合同。本文件确定 query_agents、send_agent_message、stop_agent、wait_message、tools.run_agent、tools.wait_agent 的参数、精简返回值及调用示例。query_agents、send_agent_message 支持 ROOT 和 Subagent 原生调用；stop_agent 只供 ROOT 使用，ROOT 的 Code Mode 提供这三个工具的对应 JS API。tools.run_agent、tools.wait_agent 为 ROOT 专用、仅 JS 调用；wait_message 为 Subagent 专用、仅原生调用，Subagent 无 JS API。继续沿现有 bypass-permissions 执行条件，不因查询只读而扩大权限。

## 1. query_agents：查询 Agent、Work 摘要与 Work 详情

### 1.1 用途、范围与顺序

Agent 是可持续分派工作的协作者，Work 是一次分派。一个 Agent 可以先后执行多项 Work；查询某项 Work 不等于读取整个 Agent 会话。

| 参数分支 | 本次查询范围 |
| --- | --- |
| 不填 target | 当前调用者可见、仍保留的全部 Agent 摘要 |
| target 为 AGENT | 该 Agent 的全部可查询 Work 摘要，不展开指令和结果正文 |
| target 为 WORK | 该 Work 的主指令、状态，以及实际已有的最终回答或停止诊断 |

ROOT 查询本次协作中仍保留的成员；Subagent 查询自己及通过已接纳输入或消息获知的协作者。沿现有协作归属、可见范围及保留生命周期读取，不扩展为任意 session 查询，也不因内容已保存就承诺已回收 Agent 仍可查询。ROOT 不作为 AGENT 查询目标，列表不返回单独的 root 字段。

Agent 列表保持按 Agent ID 排列，不将其解释为创建时间；某 Agent 的 Work 保持实际分派队列顺序。WORK 精确查询读取本次分派自身的指令和结果，不误取该 Agent 最新一项 Work 的内容。

查询不启动、继续、停止或等待工作，不消费 Agent 消息，也不将已保存结果的补页视为新的状态观察。

### 1.2 最简参数表与 Target

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | AGENT／WORK Target | 可选，放首位；省略则列 Agent | AGENT 列其 Work 摘要，WORK 读取一项 Work 的完整详情 |

```typescript
type AgentTarget = {kind: "AGENT"; agent_id: string};
type WorkTarget = {kind: "WORK"; work_id: string};

interface QueryAgentsArgs {
  target?: AgentTarget | WorkTarget;
}
```

只接受可选 target，不增加 action、query 或状态筛选。target 填写时须完整，不接受 null、其他 kind、ROOT 别名或额外字段。agent_id 使用实际 session 身份，work_id 使用实际 queue 身份，沿共享 ID 层校验；不增加 Agent／Work 的另一套 ID，不要求模型同时提供两种 ID。

删除 cursor、max_items 和旧重新查询式 next，不保留默认 50／最大 50 条的展示范围。内部可以分批取得记录，但必须完成本次查询范围并保存完整结果后，才交付原生第一页或 JS 业务值。

### 1.3 精简返回字段与类型

三个分支均只返回 meta 和 content，不在业务顶层重复 target、agent、root、agents 或 works。每条记录采用 type:object/value，target 放在记录内部。删除 inspect 调用对象；后续查询直接使用记录中的 Target。

| 记录 | 保留字段 |
| --- | --- |
| Agent 摘要 | target、实际已知的 name、current_work、current_work_title、queued_work_count |
| Work 摘要 | target、agent、work_title、work_status，以及实际存在的 failed_dependency |
| Work 详情 | Work 摘要字段，加完整 instruction，以及实际已有的 final 或 diagnostic |

Agent 的 current_work 只表示当前运行的 Work；没有运行工作时 current_work 和 current_work_title 均为 null。运行中的 Work 也可能没有标题，此时 current_work 非 null，current_work_title 为 null；是否有运行工作以 current_work 判断，不能只凭标题判断。排队情况由 queued_work_count 单独表达，值为非负整数。删除 current_work_status，它与 current_work 的既有含义重复；name 未取得时省略。

Work 没有标题时，work_title 返回 null，不以空字符串代替；Work 摘要与详情使用相同规则。

work_status 保留 queued／running／completed／stopped 四种工作状态，与 meta.status 的本次查询状态分开。查询到 queued 或 stopped Work 仍是 SUCCESS，不表示本次查询失败。

删除公开 terminal_reason 代码及其 null 占位。Work 摘要需要说明停止原因时，以紧邻该记录的简短 text 表达，不展开完整诊断；WORK 精确查询通过 diagnostic 保留实际停止说明，不重复同一原因。存在失败依赖时保留 failed_dependency 的 WORK Target，不在原因字符串中编码身份。

instruction 是本次 Work 的主指令，不自动拼接全部历史材料或依赖正文。final 是对应完成 Work 的完整最终回答，不是整个执行历史，允许真实空字符串。diagnostic 是对应停止 Work 的实际诊断。未完成时不伪造 final，未取得的可选字段省略；不把工具调用、思考或整个 Agent 会话附入结果。

```typescript
type WorkStatus = "queued" | "running" | "completed" | "stopped";

interface AgentSummaryRecord {
  target: AgentTarget;
  name?: string;
  current_work: WorkTarget | null;
  current_work_title: string | null;
  queued_work_count: number;
}

interface WorkSummaryRecord {
  target: WorkTarget;
  agent: AgentTarget;
  work_title: string | null;
  work_status: WorkStatus;
  failed_dependency?: WorkTarget;
}

interface WorkDetailRecord extends WorkSummaryRecord {
  instruction: string;
  final?: string;
  diagnostic?: string;
}

type AgentTextPart = {type: "text"; value: string};
type AgentObjectPart<T> = {type: "object"; value: T};

interface QueryAgentsSuccess<T> {
  meta: {status: "SUCCESS"};
  content: (AgentTextPart | AgentObjectPart<T>)[];
}

interface QueryAgentsFailure<T> {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (AgentTextPart | AgentObjectPart<T>)[];
}

type QueryAgentListResult =
  | QueryAgentsSuccess<AgentSummaryRecord>
  | QueryAgentsFailure<AgentSummaryRecord>;
type QueryAgentWorksResult =
  | QueryAgentsSuccess<WorkSummaryRecord>
  | QueryAgentsFailure<WorkSummaryRecord>;
type QueryWorkResult =
  | QueryAgentsSuccess<WorkDetailRecord>
  | QueryAgentsFailure<WorkDetailRecord>;

declare function query_agents(args: {target?: never}): Promise<QueryAgentsSuccess<AgentSummaryRecord>>;
declare function query_agents(args: {target: AgentTarget}): Promise<QueryAgentsSuccess<WorkSummaryRecord>>;
declare function query_agents(args: {target: WorkTarget}): Promise<QueryAgentsSuccess<WorkDetailRecord>>;
declare function query_agents(
  args: QueryAgentsArgs
): Promise<
  | QueryAgentsSuccess<AgentSummaryRecord>
  | QueryAgentsSuccess<WorkSummaryRecord>
  | QueryAgentsSuccess<WorkDetailRecord>
>;
```

各分支 Result 联合描述保存的完整业务值；JS 正常返回只使用对应 QueryAgentsSuccess，失败或取消通过异常中实际存在的 err.result 提供对应 QueryAgentsFailure。

正常非空 Agent 列表和 Work 列表分别返回对应的 object 项；WORK 精确查询成功返回一个 WorkDetailRecord。text 用于空结果或实际诊断，不增加 partial、meta.error、公开错误枚举或独立提示字段。

### 1.4 原生示例

以下身份为示意。各原生结果的实际 observed_at_utc 和 observation_duration_microseconds 按索引第 2 章在顶层并列；示例省略共同时间信息，不伪造观察值。

列出 Agent：

```text
工具：query_agents
参数：{}

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "AGENT",
      "agent_id": "session:<A>"
    },
    "name": "Ada Lovelace",
    "current_work": {
      "kind": "WORK",
      "work_id": "queue:<W1>"
    },
    "current_work_title": "检查导出逻辑",
    "queued_work_count": 1
  }
  {
    "target": {
      "kind": "AGENT",
      "agent_id": "session:<B>"
    },
    "name": "Alan Turing",
    "current_work": null,
    "current_work_title": null,
    "queued_work_count": 0
  }

tool_result: <本次完整 Agent 列表的导出 Target>
next: null
```

列出某 Agent 的 Work 摘要，第二项没有标题：

```text
工具：query_agents
参数：
{
  "target": {"kind":"AGENT","agent_id":"session:<A>"}
}

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "WORK",
      "work_id": "queue:<W1>"
    },
    "agent": {
      "kind": "AGENT",
      "agent_id": "session:<A>"
    },
    "work_title": "检查导出逻辑",
    "work_status": "completed"
  }
  {
    "target": {
      "kind": "WORK",
      "work_id": "queue:<W2>"
    },
    "agent": {
      "kind": "AGENT",
      "agent_id": "session:<A>"
    },
    "work_title": null,
    "work_status": "queued"
  }

tool_result: <本次完整 Work 列表的导出 Target>
next: null
```

读取一项已完成 Work。其保存业务值为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {"kind": "WORK", "work_id": "queue:<W1>"},
        "agent": {"kind": "AGENT", "agent_id": "session:<A>"},
        "work_title": "检查导出逻辑",
        "work_status": "completed",
        "instruction": "检查导出逻辑，报告具体问题，先不要修改。",
        "final": "发现一处问题。\n空结果未保留列标题。"
      }
    }
  ]
}
```

同一结果的原生调用与交付：

```text
工具：query_agents
参数：
{
  "target": {"kind":"WORK","work_id":"queue:<W1>"}
}

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "WORK",
      "work_id": "queue:<W1>"
    },
    "agent": {
      "kind": "AGENT",
      "agent_id": "session:<A>"
    },
    "work_title": "检查导出逻辑",
    "work_status": "completed",
    "instruction": "检查导出逻辑，报告具体问题，先不要修改。",
    "final": "发现一处问题。\n空结果未保留列标题。"
  }

tool_result: <本次完整 Work 详情的导出 Target>
next: null
```

上述列表与详情可以发生在不同观察时点。原生 object 使用统一的两空格缩进 JSON 投影，记录里的 target、agent 或 failed_dependency 不拆出为另一份 Target 项，也不自动继续查询。

### 1.5 JS 示例

ROOT 先列出可见 Agent；完整结果进入 JS，仅显式输出的内容沿 Cell steer 交付：

```javascript
const result = await tools.query_agents({});
console.log(result);
```

模型取得上次输出中的 Agent Target 后，可以在后续 Cell 查询其全部 Work 摘要：

```javascript
const result = await tools.query_agents({
  target: {kind: "AGENT", agent_id: "session:<A>"}
});
console.log(result);
```

已知 Work Target 时，查询详情并直接访问完整字段：

```javascript
const result = await tools.query_agents({
  target: {kind: "WORK", work_id: "queue:<W1>"}
});
for (const part of result.content) {
  if (part.type === "object") {
    console.log(part.value.final ?? part.value.diagnostic ?? part.value.instruction);
  } else {
    console.log(part.value);
  }
}
```

三个分支也可直接 console.log 完整业务值；JS 不携带原生时间外壳、tool_result 或 next，不返回对象片段。模型写脚本时须已了解这里的参数与返回类型；JS 内存中的查询结果不会自动成为模型可见上下文。console.log／非 undefined 的显式 return 按脚本实际选择的内容整条交付，不套用原生 12k 分页。

实际存在完整失败业务值时，JS 沿既有异常通道通过 err.result 取得它：

```javascript
try {
  console.log(await tools.query_agents({
    target: {kind: "WORK", work_id: "queue:<W1>"}
  }));
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

### 1.6 空结果、失败与取消

| 情况 | 返回语义 |
| --- | --- |
| 正常列 Agent，没有可见记录 | SUCCESS，content 一条 text：“当前没有可查询的 Agent。” |
| AGENT 存在且可查询，但没有 Work | SUCCESS，content 一条 text：“该 Agent 当前没有可查询的 Work。” |
| 指定 AGENT／WORK 不存在、已不可查询或超出原可见范围 | ERROR，content 简短说明目标及实际原因，不伪造记录 |
| Target 不完整、ROOT 别名、额外字段或权限不允许 | 沿既有参数／权限错误通道说明原因，不改变协作范围 |
| 来源读取失败或本次查询未完整完成 | ERROR，说明实际原因并保留已可靠取得的记录，不将其称为完整列表或用空说明冒充无记录 |
| 实际结算为取消 | CANCELLED，说明取消并保留实际已取得的内容，不以取消撤销已有事实 |

失败或取消业务值存在时完整保存，content 先说明实际原因，再保留对应分支的已取得记录；更早失败尚未产生业务值时不伪造 err.result 或 tool_result。错误结果按统一规则交付完毕的 next:null 只表示这份错误结果已读完，不表示查询成功或工作完成。查询到 stopped Work 仍返回 SUCCESS 及真实 work_status／diagnostic。

### 1.7 冻结分页与存储边界

本次请求范围内的完整结果先取得并保存，再向原生交付 12k 第一页或向 JS 返回完整值。原生余下内容通过公共 NEXT Target，由 read_file(target=next) 顺序续读；不返回再次调用 query_agents 的业务 next。

object 的 JSON 投影可跨页，不要求整条 Agent／Work 记录或完整 instruction／final／diagnostic 装入一页。删除仅因记录或详情装不进展示预算而拒绝查询的旧检查，不保留旧 max_items、整记录页或专用字段游标；真实读取、存储或执行资源失败沿共同错误规则报告，不增加替代总量 cap。

状态、当前 Work、排队数量、摘要、指令、最终回答及诊断均冻结为本次查询实际取得的值。工作随后结束、Agent 被回收或又接到新 Work，不改写已保存结果；NEXT 和导出不重新查询、不更新状态、不补入后来生成的 final，也不启动或消费工作。需要最新状态时显式重调 query_agents。

查询结果沿已有工具结果与共同 content_parts 保存：Agent、Work 摘要及详情各为一次 object 内容项，text 保存实际说明；不在 public_fields 重复保存 records／agents／works 或整份返回 JSON。原 Work 的主指令从其队列或已消费 entry 的共同正文取得，最终回答从该 Work 对应的实际 final entry 取得，诊断沿现有结算事实取得，不另建 Work 正文表或分页实体。查询产生的冻结结果是本次工具调用的业务内容，不成为原 Work 正文的另一份权威。

## 2. send_agent_message：向接收方当前活动轮次发送消息

### 2.1 用途与交付语义

向同一协作中的 AGENT 发送文字消息；ROOT 别名表示本次协作的 ROOT。只向接收方当前活动轮次的输入队列接纳消息，不创建 Work，不加载空闲 Agent，也不启动排队工作。协作归属、消息来源、输入处理和结算继续复用现有底层语义。

| 接收方状态 | 本次行为与返回 |
| --- | --- |
| 存在活动轮次 | 消息接纳到该轮输入队列，SUCCESS；在正常输入处理点交付，不保证立即被模型读取或回复 |
| 空闲，没有活动轮次，也没有待开始的 Work | 不发送、不暂存到未来工作，ERROR：“消息未发送：接收方当前没有活动轮次。” |
| Work 已排队，但尚未开始活动轮次 | 不发送、不附加到排队 Work，ERROR：“消息未发送：接收方的工作仍在排队，尚未开始。” |
| 已选中的活动轮次在接纳前结束 | 不发送，ERROR：“消息未发送：接收方当前轮次已结束。” |

接纳时核对捕获的确切轮次，与该轮结束串行化；不会自动转投后继 turn 或另一项 Work。SUCCESS 只表示已加入该轮输入队列，不表示接收方已读、已回复或工作完成。发送工具不等待回复，也不为投递消息而取消接收方正在进行的工具调用。

消息接纳后，若目标轮次在消费前结束，未消费消息按原规则拒绝，不转投后继 turn／Work，也不补发。原发送结果保持冻结，SUCCESS 仍只表示当时接纳成功。

### 2.2 最简参数表与类型

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | AGENT Target | 必填，放首位 | 同一协作中的接收方；agent_id 可使用 ROOT 别名 |
| message | string | 必填 | 实际发送的文字，保留原换行；不接受空字符串或纯空白 |

```typescript
interface SendAgentMessageArgs {
  target: AgentTarget;
  message: string;
}
```

target 只接受第 1.2 节的最小 AGENT 形状：`{kind:"AGENT", agent_id:string}`。agent_id 为实际 session 身份，或本工具支持的 ROOT 别名；不接受 WORK、null 或额外字段。ROOT 别名不扩展到 query_agents 的参数。

保留现行 message 至多 16,384 UTF-8 字节的输入限制，与结果的原生 12k 分页无关。不增加发送者、session、Work、投递模式或等待参数，来源由 runtime 自动取得。

### 2.3 精简返回值与类型

业务结果只保留 meta.status 和文字 content。删除 recipient、work、delivery 及重复 message 字段，不返回顶层业务 Target；实际消息与接收轮次、Work 的内部关联仍由原 owner 保留。

```typescript
interface SendAgentMessageSuccess {
  meta: {status: "SUCCESS"};
  content: AgentTextPart[];
}

interface SendAgentMessageFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: AgentTextPart[];
}

type SendAgentMessageResult = SendAgentMessageSuccess | SendAgentMessageFailure;

declare function send_agent_message(
  args: SendAgentMessageArgs
): Promise<SendAgentMessageSuccess>;
```

正常接纳时，content 为一条 text：“消息已加入接收方当前轮次的输入队列。”空闲、尚未启动和接纳前结束均为 ERROR，原因直接写进 content，不用公开枚举区分。CANCELLED 仅用于本次调用实际结算为取消的情况，不将接收方没有活动轮次解释为取消。

SendAgentMessageResult 描述保存的完整业务值；JS 成功时正常返回 SendAgentMessageSuccess，ERROR／CANCELLED 沿异常通道提供实际存在的完整 err.result。没有业务结果的参数或运行时失败不伪造 err.result。

### 2.4 原生示例

以下省略共同观察时间信息；tool_result 和 next 仍由公共原生交付层提供，不属于业务值。

接收方正在运行，消息已接纳：

```text
工具：send_agent_message
参数：
{
  "target": {"kind":"AGENT","agent_id":"session:<A>"},
  "message": "先检查空结果导出的问题，暂时不要修改文件。"
}

meta:
  status: SUCCESS

content:
  消息已加入接收方当前轮次的输入队列。

tool_result: <本次完整发送结果的导出 Target>
next: null
```

Subagent 向本次协作的 ROOT 发送消息，交付规则相同：

```text
工具：send_agent_message
参数：
{
  "target": {"kind":"AGENT","agent_id":"ROOT"},
  "message": "已定位空结果导出问题，正在核对影响范围。"
}

meta:
  status: SUCCESS

content:
  消息已加入接收方当前轮次的输入队列。

tool_result: <本次完整发送结果的导出 Target>
next: null
```

接收方空闲：

```text
工具：send_agent_message
参数：
{
  "target": {"kind":"AGENT","agent_id":"session:<A>"},
  "message": "请补充检查空结果导出。"
}

meta:
  status: ERROR

content:
  消息未发送：接收方当前没有活动轮次。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

接收方有排队 Work，但尚未开始：

```text
工具：send_agent_message
参数：
{
  "target": {"kind":"AGENT","agent_id":"session:<A>"},
  "message": "开始后请优先检查空结果导出。"
}

meta:
  status: ERROR

content:
  消息未发送：接收方的工作仍在排队，尚未开始。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

### 2.5 JS 示例

ROOT 的 Code Mode 使用相同参数，取得完整业务值后显式输出：

```javascript
const result = await tools.send_agent_message({
  target: {kind: "AGENT", agent_id: "session:<A>"},
  message: "先检查空结果导出的问题，暂时不要修改文件。"
});
console.log(result);
```

成功值为 `{meta:{status:"SUCCESS"}, content:[{type:"text", value:"消息已加入接收方当前轮次的输入队列。"}]}`，不携带原生时间信息、tool_result、next 或业务 target。

处理接收方空闲等失败时，输出实际失败业务值，不自动重发：

```javascript
try {
  console.log(await tools.send_agent_message({
    target: {kind: "AGENT", agent_id: "session:<A>"},
    message: "请补充检查空结果导出。"
  }));
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

console.log／非 undefined 的显式 return 继续沿独立 Cell steer 交付脚本实际输出，不套用原生 12k 分页。发送者的工具结果与接收方收到的消息各走原有路径，接收方回复不并入本次结果。

### 2.6 失败、冻结分页与存储边界

| 情况 | 返回语义 |
| --- | --- |
| 接收方不存在、已不可用或不属于同一协作 | ERROR，说明实际原因，不创建消息或伪造 Target |
| 参数无效、message 超出既有输入限制或权限不允许 | 沿参数／权限错误通道说明原因；确有失败业务值时使用 meta.status 与文字 content |
| 接纳明确失败 | ERROR，说明消息未发送及原因 |
| 接纳或保存状态未确认 | 如实说明状态未确认，不直接声称未发送，也不自动重做 |
| 已接纳后发生结果保存或交付失败 | 保留“消息已加入输入队列”的已知事实，再说明失败；不撤销已接纳消息或自动重发 |
| 本次调用实际结算为取消 | CANCELLED，说明取消；消息已经接纳的事实仍如实保留 |

结果完整保存后，原生按共同 12k／NEXT 规则交付，JS 返回完整业务值。NEXT 仅续读这份冻结结果，不再次发送、不等待接收方读取或回复，也不根据接收方后续状态改写本次结果。失败结果交付完毕的 next:null 只表示该结果读完，不表示消息发送成功。

删除仅因发送回执装不进展示页而阻止接纳的旧检查，展示预算不参与是否发送的业务判断。真实参数、权限、输入队列和存储失败继续由现有 owner 报告，不新增投递回执表、公开投递状态或重试机制。

结果文字沿共同 content_parts 保存，不在 public_fields 再存 recipient、work、delivery 或整份结果 JSON。接收方消息继续通过现有输入队列进入会话；其正文与发送工具的简短结果是各自业务内容，不另建消息正文实体。

## 3. stop_agent：停止指定 Work 或 Agent 当前及排队的工作

### 3.1 最简参数表与范围

仅 ROOT 可原生调用或通过 tools.stop_agent 调用，仍要求 bypass-permissions。停止对象限于本次协作中的 Agent／Work，沿用现有归属与权限核对。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | AGENT／WORK Target | 必填，放首位 | AGENT 停止该 Agent 当前运行及当时已排队的工作；WORK 只停止指定工作 |

```typescript
interface StopAgentArgs {
  target: AgentTarget | WorkTarget;
}
```

Target 复用第 1.2 节的最小形状，不接受 ROOT 别名、null、其他 kind 或额外字段，不增加 action、force 或等待时间参数。AGENT 分支的停止范围在原停止事务中确定，不包含之后新提交的 Work；WORK 分支不转而停止该 Agent 后续或其他无关工作。

### 3.2 对 Work 与已提交工具调用的影响

停止、依赖处理、取消信号及效果结算完全保持现有底层语义，本章调整公开参数、结果与交付合同。

| 对象或状态 | 当前底层行为 |
| --- | --- |
| 本次选中的排队 Work | 取消排队，状态为 stopped，不为它开启 turn |
| 本次选中的运行中 Work | 取消对应活动任务，等待该任务退出，再读取实际 Work 状态；不只发出信号就宣称停止完成 |
| 指定 Work 已 completed／stopped | 保持原结果，正常成功，说明未执行停止 |
| Agent 没有未结束 Work | 正常成功，说明没有需要停止的工作 |
| 同一 Agent 的其他无关排队 Work | WORK 分支保留；AGENT 分支停止本次已选中的排队工作 |
| 尚未开始、依赖被停止 Work 的任务 | 沿现有依赖规则停止，可能涉及其他 Agent；不扩大为停止所有相关 Agent |
| Agent 本身及已有历史 | 保留，之后仍可分派新 Work；不因停止而删除 Agent 或永久禁用它 |

停止与自然完成竞争时，返回实际状态。某项 Work 在停止前已经 completed，就保留 completed，不能改写为 stopped。排队取消、运行中断和依赖未成功等内部事实仍由原 owner 保留，精简公开字段不删除这些事实。

| 工具调用阶段 | 停止 Work 时的处理 |
| --- | --- |
| 尚未实际派发 | 沿现有取消检查阻止后续派发 |
| 已经开始执行 | 由原工具按自身取消、物理执行及结算规则处理，可能仍取得实际结果；不统一承诺撤销或继续执行 |
| 已取得确定结果，正在保存 | 完成原有结果结算，保留实际结果和已发生效果 |
| 已产生文件写入、远端请求等效果 | 不因停止 Work 回滚或丢弃已知事实 |
| 未闭合或迟到的工具结果 | 继续沿原调用闭合及迟到结果路径处理，不复制进 stop_agent 的简短结果 |

等待运行任务退出可能涉及原有工具结算，不承诺停止立即完成，也不新增等待上限、强杀或自动重做机制。

### 3.3 精简返回字段与类型

业务结果保留 meta.status、成功结果的四个 Work 状态计数，以及有序文字／object content。不返回顶层业务 Target；逐项结果中的 target 是 WORK Target，供按需查询实际结果。

| 位置 | 保留内容 |
| --- | --- |
| meta.status | SUCCESS／ERROR／CANCELLED，本次停止调用的实际状态 |
| meta.queued_work_count | 本次所列 Work 中处于 queued 状态的数量 |
| meta.running_work_count | 本次所列 Work 中处于 running 状态的数量 |
| meta.completed_work_count | 本次所列 Work 中处于 completed 状态的数量 |
| meta.stopped_work_count | 本次所列 Work 中处于 stopped 状态的数量 |
| content 的 text | 本次停止、无变化或失败的简短说明 |
| content 的 object | 每项 Work 的 target、work_status |

```typescript
interface StopWorkRecord {
  target: WorkTarget;
  work_status: WorkStatus;
}

type StopAgentContentPart = AgentTextPart | AgentObjectPart<StopWorkRecord>;

interface StopAgentSuccess {
  meta: {
    status: "SUCCESS";
    queued_work_count: number;
    running_work_count: number;
    completed_work_count: number;
    stopped_work_count: number;
  };
  content: StopAgentContentPart[];
}

interface StopAgentFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: StopAgentContentPart[];
}

type StopAgentResult = StopAgentSuccess | StopAgentFailure;

declare function stop_agent(args: StopAgentArgs): Promise<StopAgentSuccess>;
```

StopAgentResult 描述保存的完整业务值；JS 正常返回只使用 StopAgentSuccess，失败或取消通过异常中实际存在的 err.result 提供 StopAgentFailure。

成功结果的四项计数均为非负整数，无对应状态时填 0，总和等于本次 content 中的 Work 记录数；每项 Work 只列一次。它统计本次停止处理结果，不统计 Agent 全部历史，也不包含未列入结果的其他 Agent 依赖联动记录。stopped 表示该 Work 的实际状态，不代表它一定是本次新停止的。

WORK 分支正常返回指定 Work 的一条状态记录，包括原本已经结束的情况。AGENT 分支按原队列顺序返回本次直接选中的 Work，不补入其他已结束历史；没有未结束工作时只返回文字，四项计数均为 0。

失败或取消时，content 说明原因并保留实际已确认的 Work 状态或停止效果，不强行给出完整统计；meta 不提供四个计数字段，不把未知数量填成 0。

删除 stop_requested、stopped 布尔值、affected_count、旧 status_counts、独立 works 数组和重复 message 字段。记录不重复 agent、work_title、inspect 或公开 terminal_reason 代码；有用原因以 text 表达，指令、最终回答或诊断全文按需通过 query_agents 查询，不在停止结果中展开。

### 3.4 原生示例

以下省略共同观察时间信息。object 沿共同两空格缩进 JSON 规则投影；tool_result、next 属于原生公共交付层。

停止一项运行中 Work：

```text
工具：stop_agent
参数：
{
  "target": {"kind":"WORK","work_id":"queue:<W1>"}
}

meta:
  status: SUCCESS
  queued_work_count: 0
  running_work_count: 0
  completed_work_count: 0
  stopped_work_count: 1

content:
  已停止指定工作。
  {
    "target": {
      "kind": "WORK",
      "work_id": "queue:<W1>"
    },
    "work_status": "stopped"
  }

tool_result: <本次完整停止结果的导出 Target>
next: null
```

停止 Agent 的当前运行工作及一项排队工作：

```text
工具：stop_agent
参数：
{
  "target": {"kind":"AGENT","agent_id":"session:<A>"}
}

meta:
  status: SUCCESS
  queued_work_count: 0
  running_work_count: 0
  completed_work_count: 0
  stopped_work_count: 2

content:
  已停止该 Agent 当前运行及已排队的工作。
  {
    "target": {
      "kind": "WORK",
      "work_id": "queue:<W1>"
    },
    "work_status": "stopped"
  }
  {
    "target": {
      "kind": "WORK",
      "work_id": "queue:<W2>"
    },
    "work_status": "stopped"
  }

tool_result: <本次完整停止结果的导出 Target>
next: null
```

若指定 Work 原本已经 completed，同样返回 SUCCESS，一条 text：“该工作已结束，未执行停止。”，以及该 WORK Target 和 work_status:completed 的 object；completed_work_count 为 1，其余三项为 0。指定 Work 原本已 stopped 时，记录为 stopped，stopped_work_count 为 1，不声称本次再次停止了它。

若 AGENT 没有未结束工作，返回 SUCCESS，四项计数均为 0，content 只含一条 text：“该 Agent 当前没有需要停止的工作。”不返回空 Work 对象。

### 3.5 JS 示例

ROOT 在 Code Mode 中停止指定 Work，正常成功时直接取得完整业务值：

```javascript
const result = await tools.stop_agent({
  target: {kind: "WORK", work_id: "queue:<W1>"}
});
console.log(result);
```

停止某 Agent 当前及排队工作，并访问本次完整计数和逐项状态：

```javascript
const result = await tools.stop_agent({
  target: {kind: "AGENT", agent_id: "session:<A>"}
});
console.log({
  queued_work_count: result.meta.queued_work_count,
  running_work_count: result.meta.running_work_count,
  completed_work_count: result.meta.completed_work_count,
  stopped_work_count: result.meta.stopped_work_count
});
for (const part of result.content) {
  console.log(part.value);
}
```

失败或取消沿异常通道提供实际存在的完整 err.result；参数或运行时失败尚未产生业务值时不伪造它：

```javascript
try {
  console.log(await tools.stop_agent({
    target: {kind: "WORK", work_id: "queue:<W1>"}
  }));
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

JS 值不包含原生时间信息、tool_result、next 或业务顶层 target；console.log／非 undefined 的显式 return 沿既定 Cell steer 路径整条交付所选内容，不套用原生 12k 分页。

### 3.6 错误、取消与冻结分页

| 情况 | 返回语义 |
| --- | --- |
| 停止处理正常完成 | SUCCESS，返回实际 Work 状态和对应统计；停止对象变为 stopped 不等于本次调用被取消 |
| Work 已结束或 Agent 无未结束工作 | SUCCESS，简短说明未执行停止，沿第 3.3 节返回记录与计数 |
| 与自然完成竞争 | 保留实际 completed 等状态，用文字说明，不假称本次成功停止了该项 |
| 目标不存在、不可用、不属于本次协作或权限不允许 | ERROR 或沿既有参数／权限错误通道报告，不伪造 Work 记录 |
| 停止或结果读取失败 | ERROR，说明原因、已发生的停止效果和已确认状态，不将未确认的工作声称为已停止 |
| 本次调用实际结算为取消 | CANCELLED，保留已发生效果及已确认状态，不据此宣称所有 Work 或工具调用都已取消 |
| 结果保存或交付失败，或状态未确认 | 沿统一交付规则说明实际效果和不确定性，不自动重复停止，不伪造结果 Target |

本次停止范围、逐项状态和计数在原停止流程取得结果后冻结，完整保存后原生按 12k／NEXT 交付，JS 返回完整值。失败业务值实际存在时同样完整保存并交付；失败结果读完的 next:null 不表示停止成功。

NEXT 由 read_file 顺序续读冻结结果，不再次执行 stop_agent，不扩大停止范围、不重新停止后来排队的 Work，也不重新查询或更新状态、计数、最终回答及诊断。object 的文字投影可以跨页，不要求全部 Work 记录装入第一页。需要当前状态时显式调用 query_agents。

删除仅因停止回执不满足展示页预算而拒绝执行的旧检查，不增加结果条数、Agent 历史或 Work 总量上限。停止效果已经发生后，结果保存或交付失败不能被表述成“未执行停止”。

结果沿现有工具结果与共同 content_parts 保存：text 为说明，object 为 StopWorkRecord；meta 的四个 Work 计数沿已声明的非 content 业务字段保存，不另存 works 数组、原始结果 JSON 或手写清单。实际工作状态、取消原因和工具结算继续由既有业务 owner 管理，不新增停止记录表或另一套工作状态权威。

## 4. wait_message：等待当前轮次的 Agent 消息

### 4.1 调用范围与最简参数

仅 Subagent 原生调用，仍要求 bypass-permissions。ROOT 不能调用，也不提供 tools.wait_message 或其他 JS API。等待 ROOT 或其他 Agent 通过 send_agent_message 发给自己当前 turn 的未读消息，不指定接收方或发送者。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| timeout_seconds | number | 可选，默认 30 | 本次等待期限，单位为秒；0～300，允许小数；0 表示立即检查一次 |

```typescript
interface WaitMessageArgs {
  timeout_seconds?: number;
}
```

只接受可选 timeout_seconds；填写时须为有限数字，不接受 null、布尔值或额外字段。不增加 target、发送者列表、游标或消息数量参数。上述类型仅说明原生合同，不声明 JS 方法。

### 4.2 等待、超时与停止

| 情况 | 行为 |
| --- | --- |
| 调用时已有当前 turn 的未读 Agent 消息 | 立即结束等待，SUCCESS |
| 等待期间有 ROOT 或其他 Agent 的消息到达 | 发现消息后结束等待，SUCCESS，不限定发送者 |
| 本次等待期限到达 | SUCCESS，说明等待超时；只结束这次等待，当前 Work 继续 |
| timeout_seconds 为 0 | 只检查一次，有消息和无消息均正常成功，不进入阻塞等待 |
| 新 Work 排队、其他 Work 完成或其他无关变化 | 不视为收到消息，继续等待；不延长原等待截止时间 |
| ROOT 停止当前 Work，或会话关闭 | 沿现有活动任务取消路径退出等待，不等满本次超时，也不让已停止的模型自动继续 |

阻塞等待期间沿现有调度让出 Subagent 物理执行名额，恢复执行时按原规则重新取得名额。立即检查或消息已经可用时不先让出名额。等待、唤醒、取消及调度完全复用现有底层语义。

本工具只检查是否有消息可处理，不消费消息、不标记已读，也不推进消息观察位置。未读判定继续覆盖本 turn 已接纳、尚未进入当前模型上下文的 Agent 消息；已经进入模型上下文的消息不再算未读。

### 4.3 精简返回值与消息正文

业务结果只保留 meta.status 和文字 content。删除公开 state 及 message_available／timeout 等返回枚举；不返回业务 Target、消息正文、发送者清单、计数或另一组提示字段。

```typescript
interface WaitMessageResult {
  meta: {status: "SUCCESS" | "ERROR" | "CANCELLED"};
  content: AgentTextPart[];
}
```

| 本次结果 | meta.status | content |
| --- | --- | --- |
| 有新消息可处理 | SUCCESS | “有新消息待处理，正文将通过正常输入路径交付。” |
| 等待超时，或立即检查未发现消息 | SUCCESS | “本次等待超时，尚未发现新消息。” |
| 实际读取或等待失败 | ERROR | 实际失败原因 |
| 等待调用实际结算为取消 | CANCELLED | 实际取消说明，沿既有工具结果闭合路径处理 |

消息正文继续走原有链路：send_agent_message 接纳到当前 turn 的输入队列；wait_message 检查到可用消息后结束等待；runtime 在正常输入处理点按原有来源、顺序及实际输入准入交付正文。等待结果不复制正文，也不代表模型已经阅读了消息。

停止时保持原有 turn 中断与工具结算，不将停止包装成普通成功结果并自动续跑；也不承诺已停止的模型还能收到一条额外的取消提示。已经取得的确定结果继续按真实结算保留，不改写已有消息。

### 4.4 原生示例

以下省略共同观察时间信息；tool_result、next 属于原生公共交付层。默认等待 30 秒，期间发现新消息：

```text
工具：wait_message
参数：{}

meta:
  status: SUCCESS

content:
  有新消息待处理，正文将通过正常输入路径交付。

tool_result: <本次完整等待结果的导出 Target>
next: null
```

指定本次等待期限，未发现新消息而超时：

```text
工具：wait_message
参数：
{
  "timeout_seconds": 60
}

meta:
  status: SUCCESS

content:
  本次等待超时，尚未发现新消息。

tool_result: <本次完整等待结果的导出 Target>
next: null
```

立即检查一次：

```text
工具：wait_message
参数：
{
  "timeout_seconds": 0
}
```

立即检查仍按上面两种 SUCCESS 结果说明是否发现消息，不消费消息或停止当前 Work。

### 4.5 错误与冻结分页

参数无效、角色／入口不允许或权限不满足时，沿原有校验与错误通道报告；读取和等待实际失败时，meta.status 为 ERROR，原因放 content。更早失败尚未产生业务值时不伪造业务结果或 tool_result。实际取消沿现有取消与闭合语义，不能把普通等待超时改成 CANCELLED。

本次等待取得的结果完整保存后，原生统一按冻结 12k／NEXT 交付。NEXT 通过 read_file 续读同一保存结果，不重新等待、不检查消息队列、不消费消息或推进已读状态；后来有消息到达也不改写之前的超时结果。消息正文仍由正常输入链路交付，不成为 NEXT 的后续页面。

保存或交付失败沿统一交付规则说明实际情况，不自动重启等待，不因等待结果未交付而重发或重复保存消息正文。结果文字沿共同 content_parts 保存，不另存 state、消息正文副本或专用等待记录表。

## 5. tools.run_agent：ROOT 批量分派工作，仅 JS

### 5.1 用途与参数

仅 ROOT 在 Code Mode 中通过 tools.run_agent 调用，仍要求 bypass-permissions；不注册为模型可直接调用的原生工具，也不向 Subagent 提供。提交一批 Work 后返回本次接纳结果，不等待工作完成。

入口始终接收非空的工作配置数组，只有一项工作也使用一个元素。每项配置的参数如下：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | AGENT Target | 可选，填写时放首位 | 省略则新建 Agent；填写已有 Agent 则向其队列追加一项新 Work，保留其会话上下文 |
| instruction | string 或同步函数 | 必填 | 本次 Work 的主指令；同步函数取得同批引用后返回实际字符串 |
| work_title | string | 可选 | 本次 Work 的显示标题，允许不同 Work 同名；省略时可为无标题，不用标题定位工作 |
| materials | CONTEXT_MATERIAL／WORK Target 数组 | 可选，默认 [] | 本次 Work 的 ROOT 历史材料及已经结束的 Work 结果；新建和复用 Agent 均可提供 |
| depends_on | WORK Target 数组或同步函数 | 可选，默认 [] | 本 Work 启动前必须成功完成的前置 Work；成功结果随后作为材料交付 |

```typescript
type ContextMaterialTarget = {
  kind: "CONTEXT_MATERIAL";
  turns: 1 | 2 | 3;
};

type AgentMaterialTarget = ContextMaterialTarget | WorkTarget;

interface BatchReferences {
  readonly agents: readonly AgentTarget[];
  readonly works: readonly WorkTarget[];
}

interface RunAgentAssignment {
  target?: AgentTarget;
  instruction: string | ((refs: BatchReferences) => string);
  work_title?: string;
  materials?: AgentMaterialTarget[];
  depends_on?: WorkTarget[] | ((refs: BatchReferences) => WorkTarget[]);
}
```

target 只接受最小 AGENT 形状，不接受 ROOT、WORK 或 null。instruction、work_title 沿现有非空校验，保留实际文字；可选字段省略，不使用 null 代替默认值。模型不填写内部成员身份或提交包装，实际 Agent／Work 身份仍由共享 ID 层和现有准备、接纳 owner 取得。

删除顶层 context 及其“仅新建 Agent 可用”校验，不保留 mode:none、mode:last_n 或其他替代别名。CONTEXT_MATERIAL 除 kind 外只接受 turns；它表示选择 ROOT 历史材料，不分配独立材料 ID。

保留当前单批 1～16 项 Work、每项最多 16 个依赖与 16 个 WORK 材料引用、单批最多 64 条显式依赖的单次输入边界。instruction 和 work_title 分别沿现有 65,536／256 UTF-8 字节限制；CONTEXT_MATERIAL 的 turns 必须为 1、2 或 3。这些限制不变成总工作数、Agent 数或任务生涯上限。

### 5.2 材料选择、启动与顺序

materials 可以直接混合两种 Target：

```json
[
  {"kind":"CONTEXT_MATERIAL","turns":1},
  {"kind":"WORK","work_id":"queue:<已结束的W0>"}
]
```

| 材料或依赖 | 选择与交付语义 |
| --- | --- |
| CONTEXT_MATERIAL | 从本次分派对应的已接纳 ROOT 输入快照中，选择最近 turns 组对话正文；新建和复用 Agent 均可显式选择 |
| materials 中的 WORK | 先将与 depends_on 同源的项按依赖处理，只交付一次；其余独立 WORK 材料必须已经 completed 或 stopped，分别提供最终回答或实际停止诊断，作为背景材料 |
| depends_on 中的 WORK | 可以尚未完成，包括同批新 Work；全部成功完成后才启动本 Work，并交付其最终回答；依赖失败沿原规则停止本 Work |

turns 继续使用现行 ROOT 对话正文分组与投影，不等同于数据库 turn 行数，也不复制完整 provider 上下文；不足指定组数时取得实际可用内容。保留所选用户／助手公开正文，工具调用、工具结果、思考及运行时附加说明不进入这份历史；图片位置沿现行文字缺失说明处理，不借此新增实际图片转交能力。

没有 CONTEXT_MATERIAL 项就不带 ROOT 历史，不自动重复注入。选中的历史在本次分派时随 Work 冻结保存，不在排队工作实际启动时重新读取最新 ROOT 对话；每次显式选择产生本次 Work 的材料，不回填或重排该 Agent 已有上下文。

沿数据库草案，开始本次 Work 时追加的输入顺序为：

```text
主指令
→ materials 中的 ROOT 历史材料
→ depends_on 的成功结果
→ materials 中已结束 Work 的结果
→ 助手执行及后续正常交互
```

ROOT 历史材料与 WORK 材料各自保持在 materials 数组中的相对顺序，依赖结果保持声明顺序。主指令独占本次初始输入组的第一条 entry，initial_entry_id／consumed_entry_id 指向它；材料作为各自的 AGENT_MATERIAL entry 交付，不拼进主指令。初始输入整组就绪后才开启首次模型调用。

同一 Agent 的多项 Work 按队列顺序执行，不同 Agent 按物理名额调度。Agent 忙碌时，本次新 Work 及其材料一起排队，在该 Work 开始时交付，不插入当前运行的 Work。队列顺序只等待前项结束，depends_on 才要求前项成功。WORK 材料与依赖的重复校验、同一来源同时出现在两处时只按依赖交付一次，以及包含队列顺序的循环依赖校验继续沿原规则处理。

### 5.3 同批引用与精简返回类型

instruction、depends_on 的同步函数取得本批各输入位置对应的 {agents, works} Target 引用；不是已启动 Agent 或已完成 Work 的结果。函数必须同步返回，不能调用工具、使用 async 或返回 Promise，错误沿现有 Cell 参数函数规则处理。函数只在本地计算实际参数，不能把其内存中的值当成模型已经看到的内容。

这些准备阶段的引用与实际接纳结果身份一致，但准备本身不创建 Agent／Work，也不成为持久执行资格。保留此用途，不要求模型在函数内查询未来结果。

完整业务结果只保留 meta.status 与 content。成功时，每个输入位置对应一个 object，value 使用 agent、work、work_status，不使用通用 target 字段。删除旧 agents／works／details 三组平行结果数组、inspect 和回显标题，不在顶层返回业务 Target、统计或 accepted／created 布尔字段。同步函数参数中的 {agents, works} 仍承担准备引用的独立用途，不是旧结果数组。

```typescript
interface RunAgentWorkRecord {
  agent: AgentTarget;
  work: WorkTarget;
  work_status: WorkStatus;
}

interface RunAgentSuccess {
  meta: {status: "SUCCESS"};
  content: AgentObjectPart<RunAgentWorkRecord>[];
}

interface RunAgentFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (AgentTextPart | AgentObjectPart<RunAgentWorkRecord>)[];
}

type RunAgentResult = RunAgentSuccess | RunAgentFailure;

declare function run_agent(assignments: RunAgentAssignment[]): Promise<RunAgentSuccess>;
```

RunAgentResult 描述保存的完整业务值；正常返回只使用 RunAgentSuccess，失败或取消通过异常中实际存在的 err.result 提供 RunAgentFailure。

成功结果的 object 顺序与输入一致，数量相同；同一 Agent 收到多项 Work 时，每项记录仍保留对应 Agent，但 Work 身份各不相同。work_status 使用本次实际取得的 queued／running／completed／stopped 状态，不保证永远为 queued。

SUCCESS 表示整批接纳成功，不表示已经执行或全部工作最终成功；某项已因失败依赖成为 stopped 时仍如实返回其状态。最终回答与诊断不随接纳结果返回，按需用 query_agents 查询对应 Work。

例如一项工作接纳后的完整 JS 业务值：

```json
{
  "meta": {"status":"SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "agent": {"kind":"AGENT","agent_id":"session:<A>"},
        "work": {"kind":"WORK","work_id":"queue:<W1>"},
        "work_status": "queued"
      }
    }
  ]
}
```

### 5.4 JS 示例

新建 Agent，并显式附带 ROOT 历史材料：

```javascript
const result = await tools.run_agent([
  {
    instruction: "检查空结果导出是否保留列标题，报告问题，先不要修改。",
    work_title: "检查导出逻辑",
    materials: [{kind: "CONTEXT_MATERIAL", turns: 1}]
  }
]);
console.log(result);
```

模型已知实际 Agent 与已结束 Work 的 Target 后，在后续 Cell 复用 Agent；本次仍可附带新选取的 ROOT 历史：

```javascript
const result = await tools.run_agent([
  {
    target: {kind: "AGENT", agent_id: "session:<A>"},
    instruction: "结合最新讨论和已有调查结果，完善修复建议。",
    materials: [
      {kind: "CONTEXT_MATERIAL", turns: 2},
      {kind: "WORK", work_id: "queue:<已结束的W0>"}
    ]
  }
]);
const first = result.content[0].value;
console.log({agent: first.agent, work: first.work, work_status: first.work_status});
```

同批提交调查与依赖调查成功的复审，函数只引用预分配的 Work Target：

```javascript
const result = await tools.run_agent([
  {
    instruction: "调查空结果导出问题，给出修复建议。",
    work_title: "调查问题"
  },
  {
    instruction: "审阅依赖材料中的修复建议，检查遗漏，先不要修改。",
    work_title: "复审建议",
    depends_on: ({works}) => [works[0]]
  }
]);
console.log(result);
```

instruction 同样可以使用同步函数，例如 `instruction: ({agents}) => "与这位协作者核对结论：" + JSON.stringify(agents[1])`；它传递的是已知格式的 Target 引用，不读取尚未发生的回复。跨 Cell 不复用局部变量，使用已输出的实际 Target 或显式查询所得 Target。

失败或取消的实际完整业务值沿异常通道由 err.result 提供：

```javascript
try {
  console.log(await tools.run_agent([
    {instruction: "检查导出逻辑，报告具体问题。"}
  ]));
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

### 5.5 接纳、错误与完整保存

| 情况 | 返回与实际效果 |
| --- | --- |
| 整批正常接纳 | SUCCESS，按输入顺序返回逐项记录；不等待工作完成 |
| 参数、Target、材料、归属或依赖图无效 | ERROR 或沿原有校验通道拒绝；保留原整批事务，不留下半批新 Agent／Work |
| 已接纳工作因依赖未成功而停止 | 接纳结果仍为 SUCCESS，记录真实 stopped 状态；不把 Work 结局误作整批提交失败 |
| 提交结果未确认 | 沿原有确认路径核对原提交；仍未确认时如实说明，不重新分配身份或自动重提 |
| 接纳后结果保存或交付失败 | 保留已接纳事实和实际已知身份、状态，说明失败，不重做分派 |
| 本次调用实际结算为取消 | CANCELLED，保留实际已接纳工作及已知效果；不以调用取消自动撤销 Work |

参数或运行时失败没有业务值时，不伪造 err.result 或 Agent／Work 记录。失败业务值存在时完整保存，content 说明原因及已确定的接纳事实，不新增 partial 状态或公开错误枚举。

本次完整接纳结果保存后直接返回 JS，meta 只有 status；不带原生观察时间外壳、tool_result、next 或 12k 分页，不生成原生调用示例。显式 console.log／非 undefined 的 return 沿现有 Cell 输出路径整条交付所选内容，未输出的普通中间结果不自动进入 ROOT 上下文；共同异常补发边界仍沿 unified 与 Code Mode 草案。

删除仅因接纳回执装不进展示页而拒绝整批提交的旧检查，展示预算不决定接纳范围。结果中的状态与身份冻结为本次取得的值，之后工作完成不改写旧结果。停止 Cell 不自动停止已接纳 Work，需要停止时沿 stop_agent 的原规则处理。

沿数据库草案保存本次主指令、各份材料和独立的接纳结果：工作输入使用现有队列及共同内容归属转移；结果的每项 object 只保存 agent／work／work_status，不再另存三组结果数组或原始返回 JSON。CONTEXT_MATERIAL 是选择参数，不新增材料实体、材料 ID 或永久来源映射；实际历史正文随本次 Work 冻结，按 AGENT_MATERIAL 交付。

## 6. tools.wait_agent：等待指定 Work 或 Agent 空闲，仅 JS

### 6.1 两种互斥参数分支

仅 ROOT 在 Code Mode 中调用，仍要求 bypass-permissions，不提供原生入口或 Subagent JS API。等待只观察目标，不启动工作、不停止工作，也不消费消息正文。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| works | WORK Target 数组 | 与 agents 必须且只能填一种 | 等待这些确切 Work 成为 completed 或 stopped，包括尚在排队的 Work |
| agents | AGENT Target 数组 | 与 works 必须且只能填一种 | 等待这些 Agent 当前没有运行中或排队 Work |
| settle | all／first | 可选，默认 all | all 要求全部满足；first 要求至少一项满足 |
| timeout_seconds | number | 可选，默认 30 | 本次观察期限，0～300 秒，允许小数；0 表示立即观察一次 |

```typescript
interface WaitAgentOptions {
  settle?: "all" | "first";
  timeout_seconds?: number;
}

type WaitWorksArgs = WaitAgentOptions & {
  works: WorkTarget[];
  agents?: never;
};

type WaitAgentsArgs = WaitAgentOptions & {
  agents: AgentTarget[];
  works?: never;
};

type WaitAgentArgs = WaitWorksArgs | WaitAgentsArgs;
```

所选分支的数组必须非空、无重复，目标属于本次 ROOT 协作；不接受 ROOT 别名、null 或额外字段。同一 Agent 的多个不同 Work 可以一起等待，不把它们当成重复目标。timeout_seconds 须为有限数字，不接受布尔值。

删除 until 和通过 AGENT 隐式绑定当前 Work 的分支，不保留 work_end 别名；精确等待统一传 works。queued 是正常可等待状态，不再返回 not_started／unavailable 等专用等待分类。目标不存在、不可访问或不属于本次协作时按错误处理，不把它当成已结束或空闲。

### 6.2 满足条件、完整观察与竞争顺序

| 行为 | works 分支 | agents 分支 |
| --- | --- | --- |
| 满足条件 | 指定 Work 为 completed 或 stopped | Agent 当前没有运行中及排队 Work |
| 开始时已经满足 | 立即计入条件，无需等待新事件 | 同左，已经 idle 就计入条件 |
| 尚未启动 | 排队 Work 继续等待，不需要先启动才能被观察 | 只要有排队 Work 就不算 idle，包括等待依赖或执行名额的工作 |
| 之后又提交新 Work | 不影响本次已指定 Work，不追随该 Agent 的后继工作 | 新 Work 会影响该 Agent 当前是否 idle |
| 普通 Agent 消息 | 不提前结束等待，也不推进消息通知位置 | 所选 Agent 的新消息可提前结束；其他发送者仍走正常输入路径 |
| 超时或取消本次等待 | 不停止目标 Work | 同左 |

first 表示本次观察中至少一项条件已满足，不表示等待下一次新发生的完成事件，也不承诺选出全局最早结束者。已有 completed／stopped Work 或 idle Agent 均可使 first 立即返回。脚本继续等剩余目标时，应从返回记录中选择尚未满足的项，而不是依赖另一个公开游标。

AGENT 的 all 要求同一次当前状态快照中全部 idle，不能累计它们曾经分别 idle；first 同样以当前快照判断。等待工具、消息、依赖或物理名额期间仍可能存在未结束 Work，不因此算 idle。返回的 idle 只表示这次观察，不保证之后没有新 Work。

所有正常成功返回都包含全部输入目标的记录，顺序与输入一致，包含已满足和未满足项；first 只改变退出条件，不裁剪结果。正常超时和因消息提前返回也提供这份完整状态观察。完整观察承诺仅适用于 SUCCESS；失败或取消保留实际已取得的记录，不伪造完整或最新快照。

每轮 Work 状态及 Agent idle 判断使用一次批量状态读取取得的共同快照。WORK 必须直接按确切 Work ID 读取原队列／entry／turn 的状态，不逐项查询后拼成从未同时存在的组合；复用现有批量读取 owner 和 request_execution 投影，不另建工作状态权威。

同次判断保持既有竞争顺序：实际取消已生效 → 所选发送者新消息 → 条件满足 → 超时。WORK 分支跳过消息判断；消息查询沿现有路径，不承诺它与工作状态读取处于同一个数据库事务。wait_reason 表达本次采用的返回原因，不代表其他条件一定不成立；例如因消息返回时，记录中的 Agent 也可能已经全部 idle。

观察期限到达后不再继续阻塞等待，正常返回仍使用实际取得的状态；若该次观察已经满足条件，按上述优先级返回 condition_met，不按未保存的事件时间回溯裁判。期限不是 Work 寿命或硬实时返回承诺，无关变化不延长原期限。0 也先完成一次正常观察，再判断已有消息、条件或超时。

取消后不为取得“取消瞬间最新状态”强制再读数据库，直接沿原工具取消结算退出并保留已知事实。已经取得确定正常结果时，继续原有结果结算；后来停止 Cell 不改写这份结果，也不让已停止的脚本恢复执行。等待与变化通知复用既有 owner，不新增等待任务表、轮询工具调用或强杀下游工作的机制。

### 6.3 消息正文与 Cell 通知位置

AGENT 分支的新消息仍按现有消息可用性及 ROOT 输入前沿判断，只检查所选发送者。返回的 message_sources 是本次触发通知的去重 Agent 集合，按 agents 输入顺序排列；可以用于脚本定向查询或发消息，不包含正文，也不表示消息已经进入模型上下文。

每次等待在开始时读取本 Cell 各发送者已经报告的位置。顺序调用不重复报告已经成功交给该 Cell 的同一批通知；重叠调用允许共同报告同一消息，不保证每条通知只唤醒一次。不同 Cell 独立，不共享排他消费权，也不把这份进程内位置持久化或承诺崩溃恢复。

只有 SUCCESS 且 wait_reason:message_available 的结果实际成功交付 JS 后，才沿已有交付回调，以本次捕获的来源序号推进该 Cell 的通知位置；保存或 JS 交付失败、ERROR／CANCELLED 均不推进。采用已有可等待回调，在事件循环中按最大已报告位置更新，不在线程池中并发读改写共享字典；不新增登记表、消息回执或 ACK。

这只抑制等待通知的重复报告，不消费正文、不标记模型已读。消息正文继续沿正常输入队列交给 ROOT，不并入 wait_agent 结果；WORK 分支不因消息返回，也不更新这份通知位置。

### 6.4 精简返回字段与类型

| 位置 | 保留内容 |
| --- | --- |
| meta.status | SUCCESS／ERROR／CANCELLED，表示本次观察的实际结算 |
| meta.wait_reason | SUCCESS 时为 condition_met、message_available 或 timeout；WORK 分支没有 message_available |
| meta.message_sources | 仅 AGENT 分支因消息返回时存在，非空 AGENT Target 数组 |
| WORK 分支的 object | 复用 RunAgentWorkRecord：agent、work、work_status |
| AGENT 分支的 object | agent、is_idle |
| 失败／取消的 text | 实际原因、已知效果及观察缺口 |

```typescript
interface AgentIdleRecord {
  agent: AgentTarget;
  is_idle: boolean;
}

interface WaitWorksSuccess {
  meta: {
    status: "SUCCESS";
    wait_reason: "condition_met" | "timeout";
  };
  content: AgentObjectPart<RunAgentWorkRecord>[];
}

type WaitAgentsSuccess = {
  meta:
    | {status: "SUCCESS"; wait_reason: "condition_met" | "timeout"}
    | {
        status: "SUCCESS";
        wait_reason: "message_available";
        message_sources: AgentTarget[];
      };
  content: AgentObjectPart<AgentIdleRecord>[];
};

interface WaitAgentFailure<T> {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (AgentTextPart | AgentObjectPart<T>)[];
}

type WaitWorksResult = WaitWorksSuccess | WaitAgentFailure<RunAgentWorkRecord>;
type WaitAgentsResult = WaitAgentsSuccess | WaitAgentFailure<AgentIdleRecord>;

declare function wait_agent(args: WaitWorksArgs): Promise<WaitWorksSuccess>;
declare function wait_agent(args: WaitAgentsArgs): Promise<WaitAgentsSuccess>;
declare function wait_agent(args: WaitAgentArgs): Promise<WaitWorksSuccess | WaitAgentsSuccess>;
```

WaitWorksResult、WaitAgentsResult 描述对应分支保存的完整业务值；正常返回只使用对应 Success 类型，失败或取消通过异常中实际存在的 err.result 提供对应 WaitAgentFailure。

SUCCESS 只表示正常完成一次观察。等待超时、历史终态立即满足、依赖失败导致 Work 为 stopped，均可正常成功；work_status 保留实际工作结局，不将 stopped 改写成工具 ERROR 或 CANCELLED。

删除公开 state、until、bindings、observations、pending_targets、unavailable_targets 及重复统计，不回显 settle。保留类型化 wait_reason，脚本据此区分返回原因；保留 message_sources 供按来源编排，不再添加每条记录的 has_message、satisfied、终止原因码、当前工作或排队数量。指令、最终回答和诊断全文仍按需通过 query_agents 查询。

例如等待两项 Work、settle:all 超时，其中一项已结束，仍完整返回两项：

```json
{
  "meta": {"status":"SUCCESS", "wait_reason":"timeout"},
  "content": [
    {
      "type": "object",
      "value": {
        "agent": {"kind":"AGENT","agent_id":"session:<A>"},
        "work": {"kind":"WORK","work_id":"queue:<W1>"},
        "work_status": "completed"
      }
    },
    {
      "type": "object",
      "value": {
        "agent": {"kind":"AGENT","agent_id":"session:<A>"},
        "work": {"kind":"WORK","work_id":"queue:<W2>"},
        "work_status": "running"
      }
    }
  ]
}
```

例如等待两位 Agent，其中 A 的消息使等待提前返回，B 已空闲：

```json
{
  "meta": {
    "status": "SUCCESS",
    "wait_reason": "message_available",
    "message_sources": [{"kind":"AGENT","agent_id":"session:<A>"}]
  },
  "content": [
    {
      "type": "object",
      "value": {
        "agent": {"kind":"AGENT","agent_id":"session:<A>"},
        "is_idle": false
      }
    },
    {
      "type": "object",
      "value": {
        "agent": {"kind":"AGENT","agent_id":"session:<B>"},
        "is_idle": true
      }
    }
  ]
}
```

### 6.5 JS 示例

模型已知真实 Work Target 后，可以等待它，即使工作仍在排队：

```javascript
const result = await tools.wait_agent({
  works: [{kind: "WORK", work_id: "queue:<W1>"}],
  settle: "all",
  timeout_seconds: 60
});
console.log(result);
```

按 first 逐次观察工作结束。每次完整返回后只保留未结束项，避免已满足的历史项让后续 first 立即重复返回：

```javascript
const dispatched = await tools.run_agent([
  {instruction: "检查导出逻辑，报告问题。"},
  {instruction: "检查导出测试覆盖，报告遗漏。"}
]);
let pending = dispatched.content.map(part => part.value.work);
while (pending.length > 0) {
  const result = await tools.wait_agent({
    works: pending,
    settle: "first",
    timeout_seconds: 60
  });
  console.log(result);
  pending = result.content
    .map(part => part.value)
    .filter(record => record.work_status !== "completed" && record.work_status !== "stopped")
    .map(record => record.work);
}
```

观察指定 Agent 是否全部空闲；正常返回仍需检查原因与实际记录，消息或超时不等于全部空闲：

```javascript
const result = await tools.wait_agent({
  agents: [
    {kind: "AGENT", agent_id: "session:<A>"},
    {kind: "AGENT", agent_id: "session:<B>"}
  ],
  settle: "all",
  timeout_seconds: 60
});
console.log(result);
```

失败或实际取消沿既有 JS 异常通道提供实际存在的 err.result，不要求脚本从正常返回值中查找 ERROR；普通 timeout 则正常返回 SUCCESS：

```javascript
try {
  console.log(await tools.wait_agent({
    works: [{kind: "WORK", work_id: "queue:<W1>"}],
    timeout_seconds: 60
  }));
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

### 6.6 冻结结果与存储边界

正常成功结果先完成本次全部目标状态观察、完整保存，再向 JS 返回完整类型化业务值；不套用原生时间外壳、12k 页预算、tool_result 或 next。显式 console.log／非 undefined 的 return 沿既有 Cell 输出路径整条交付，不自动披露未输出的普通中间值。

ERROR／CANCELLED 只保存和交付实际已有的失败值与已知记录，不为了填满目标数量执行取消后的额外读取，也不把读取失败当成 idle、completed 或零条合法结果。更早失败尚无业务值时不伪造 err.result。保存或交付失败如实说明，不自动重启等待或重做目标工作。

工作状态、idle 判断、返回原因和消息来源均属于本次保存结果，之后 Work 完成、新任务排队、消息进入模型上下文或 Cell 被回收，都不改写这份结果。历史展示和导出读取保存结果，不重新等待、查询当前状态或推进消息通知位置。

wait_reason、实际存在的 message_sources 沿声明的非 content 业务字段保存；逐项记录沿共同 content_parts 的 object 保存，失败文字使用 text。meta.status 从原结算状态投影，不重复保存返回 JSON、目标清单或另一个状态副本。Cell 通知位置和观察期限不持久化，不新增等待实体、消息消费回执或恢复任务。

## 7. 当前生产实现参考

- [原生工具目录、说明及调用角色](../src/pulsara_agent/capability/builtin_catalog.py)
- [协作参数、同批引用及最小 Agent／Work Target](../src/pulsara_agent/conversation_kernel/subagents/collaboration_contracts.py)
- [协作工具分派、消息等待与结果组装](../src/pulsara_agent/conversation_kernel/subagents/collaboration_api.py)
- [可见范围、消息接纳与未读检查、Work 停止及依赖](../src/pulsara_agent/conversation_kernel/_repository/subagents.py)
- [消息等待期间的名额让出与恢复调度](../src/pulsara_agent/conversation_kernel/subagent.py)
- [正常输入队列与消息正文消费](../src/pulsara_agent/conversation_kernel/_repository/prompts.py)
- [活动轮次、消息交付与精确 Work 停止](../src/pulsara_agent/conversation_kernel/host.py)
- [Work 执行状态投影](../src/pulsara_agent/conversation_kernel/request_execution.py)
- [工具派发、取消与确定结果结算](../src/pulsara_agent/conversation_kernel/tool_execution.py)
- [已接纳物理调用的实际结果与取消处理](../src/pulsara_agent/conversation_kernel/io.py)
- [调用入口边界](../src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [ROOT／Subagent 与权限核对](../src/pulsara_agent/conversation_kernel/tool_runtime_authorization.py)
- [ROOT 对话正文材料的冻结选择](../src/pulsara_agent/conversation_kernel/provider_dispatch.py)
- [Cell 中同批引用准备与分派接纳](../src/pulsara_agent/code_mode/host.py)
- [Code Mode 工具调用及显式输出](../src/pulsara_agent/code_mode/node/guest.js)

这些链接用于核对当前生产接点，实际验收状态见实施计划。
