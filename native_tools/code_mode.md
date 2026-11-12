# Pulsara Code Mode 控制工具修订与使用教程草案

共用规则见[原生工具修订草案索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整结果保存、原生 12k 分页及 NEXT 沿[统一交付草案](../PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)。

状态：讨论草案，未实施。本族包括 exec_cell、query_cells、manage_cell，均为 ROOT 原生控制工具，不提供 JS API，继续沿现有 bypass_permissions 权限规则开放。本文件确定三个工具的参数、精简返回值、原生示例及错误边界。TypeScript 只描述合同，不表示存在脚本入口。本轮只改文档。

## 1. exec_cell：启动独立 JavaScript 脚本

### 1.1 用途与行为

提交一个独立 JavaScript 脚本，等待执行环境接纳后返回 CELL Target，不等待脚本完成。支持顶层 await 和 return；每次调用使用独立脚本环境，不共享上一次 Cell 的 JS 变量，也不是向同一 REPL 追加代码。

保留现有执行、工具调用、输出及生命周期语义。本次只调整参数和启动工具结果，不将脚本输出搬入启动结果，不改变 steer 的独立交付路径。

### 1.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 与 code 二选一 | 从本地 UTF-8 文件读取完整源码，冻结后作为本次脚本执行 |
| code | string | 与 target 二选一 | 直接提交 JavaScript 源码，允许空字符串 |

target 在参数表和文件分支中放首位，只接受 {kind:"PATH",path:string}，path 为非空字符串；路径解析和访问沿文件读取 owner 的现有规则，支持工作区相对路径、绝对路径及 ~。删除旧的顶层 path 字符串参数，不保留别名。

两种分支严格互斥，恰好提供一项，不接受 null、混填、其他 Target kind 或额外字段。不增加标题、超时、权限、返回值选择或展示预算参数。

```typescript
type ExecCellPathTarget = {kind: "PATH"; path: string};

type ExecCellArgs =
  | {target: ExecCellPathTarget; code?: never}
  | {target?: never; code: string};
```

文件分支读取的是源码本身，不调用带行号的原生 read_file 投影。保留启动前冻结实际执行源码的既有路径；冻结后文件变化不改变本次已提交源码。工具目录、执行上下文和权限仍由既有 owner 取得，不由模型填写或在结果中回显。

### 1.3 精简返回值与状态

| 字段 | 含义 |
| --- | --- |
| target | 已接纳脚本的 CELL Target，用于后续查询或控制 |
| meta.status | SUCCESS／ERROR／CANCELLED，表示本次启动调用的实际结算 |
| content | type:text、value:string 的有序文字项数组，表达简短接纳确认或实际失败原因 |

SUCCESS 的说明为“已接纳脚本执行。”，只确认接纳，不表示脚本已运行完成或 ROOT 已收到其输出。删除 accepted 布尔值；启动结果不增加 cell_status，不回传源码、工具目录、权限快照或内部执行身份。

```typescript
interface ExecCellTarget {
  kind: "CELL";
  cell_id: string;
}

interface ExecCellTextPart {
  type: "text";
  value: string;
}

interface ExecCellSuccess {
  target: ExecCellTarget;
  meta: {status: "SUCCESS"};
  content: ExecCellTextPart[];
}

interface ExecCellFailure {
  target?: ExecCellTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: ExecCellTextPart[];
}

type ExecCellResult = ExecCellSuccess | ExecCellFailure;
```

失败或取消时只有确实存在且已确认的 Cell 才可返回其 Target；尚未创建或身份未确认时不伪造 Target。具体原因、涉及路径及已知效果放 content，不返回 meta.error、错误代码枚举或 _hint。更早失败尚未产生业务值时，沿既有错误通道报告，不伪造业务结果。

### 1.4 原生示例

直接提交脚本：

```text
工具：exec_cell
参数：
{
  "code": "console.log('开始检查'); return '检查完成';"
}
```

从文件提交脚本：

```text
工具：exec_cell
参数：
{
  "target": {"kind":"PATH","path":"scripts/check.js"}
}
```

假设脚本已成功接纳，保存的启动业务值为：

```json
{
  "target": {"kind": "CELL", "cell_id": "cell:<ID>"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "已接纳脚本执行。"}
  ]
}
```

原生交付：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际调用耗时>

target: {"kind":"CELL","cell_id":"cell:<ID>"}

meta:
  status: SUCCESS

content:
  已接纳脚本执行。

tool_result: <本次完整启动结果的导出 Target>
next: null
```

上面的直接提交示例另产生“开始检查”和“检查完成”两条 Cell 输出，沿现有 steer 路径独立交付，不拼入这份启动结果。此处不承诺它们与启动结果之间额外的跨链路显示顺序。

文件读取失败，且错误结果已保存并完整交付时：

```text
meta:
  status: ERROR

content:
  无法读取脚本 scripts/check.js：文件不存在。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

示例中的身份和观察值为说明占位；实际观察时间及微秒耗时顶层并列，未测得耗时不伪造为 0。

### 1.5 启动结果、steer 与错误边界

exec_cell 只返回本次启动结果。脚本内部工具返回值保留在 JS 中；console.log 以及非 undefined 的顶层 return 继续沿既有 Cell 正式输出路径主动交给 ROOT，不作为 exec_cell 的结果正文或续页。图文组合、完整业务值及资源边界按 unified 已确定规则处理，本节不新设输出预算或分页。

保留现有异常诊断及晚到结果补发路径：非显式停止时，脚本异常结束等原因使已提交工具的结果未能交付 JS，仍沿原 Cell 输出链路将相应结果交给 ROOT，便于继续处理已发生的业务。这是既有异常路径，不扩展为正常调用自动输出全部中间值，不把晚到结果伪装为脚本成功 return。显式 stop 和 Host 关闭仍按现有规则抑制补发，不新增恢复任务、工具重做或输出读取入口。

脚本接纳后的语法错误、运行异常或未处理的工具调用异常，由后续 Cell 状态及诊断表达，不反向改写已成功接纳的启动结果。脚本捕获并处理的工具异常不因其曾发生就自动变为整段脚本失败。调用取消及执行效果结算仍沿现有 owner，不因精简公开字段改变实际停止或清理语义。

删除启动前因接纳确认放不进展示页预算而拒绝启动的旧检查。真实源码读取、运行环境或资源失败仍如实报告；既定 4 MiB 单值限制的删除及 16/64 MiB 暂存额度实验沿 unified，不在本工具另定数值。

本次启动结果完整保存后按原生 12k 规则交付，NEXT 只读取这份冻结结果，不重新读取脚本文件、启动或继续执行 Cell，也不读取 Cell steer。保存或交付失败时如实说明已知效果，不自动重提脚本；只有完整结果实际保存且可读时才提供 tool_result 及适用的 NEXT，不提供假引用。

## 2. query_cells：查询 Cell 状态及冻结源码

### 2.1 用途与范围

不传 target 时，列出当前 Host 仍持有的全部 Cell；传入 CELL Target 时，取得该 Cell 的状态、待结清工具调用数量及完整冻结源码。查询不执行脚本、不等待脚本完成，也不消费或重新交付 Cell steer。

本工具查询当前仍持有的 Cell，不新增历史 Cell 查询或保留承诺。完成且相关调用、输出和资源已经结清的 Cell 仍按现有生命周期回收；源码已保存不代表已回收的 CELL Target 仍可查询。列表保持现有按 Cell ID 排序的规则，不把此顺序解释为创建时间。

### 2.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | CELL Target | 可选；省略则查询列表 | 填写则查询指定 Cell 的状态和完整冻结源码 |

只接受 target；填写时只允许 {kind:"CELL",cell_id:string}，身份沿共享 ID 层校验，不接受 null、其他 Target kind 或额外字段。

删除 cursor、max_items、offset_chars、max_chars，不保留列表条数或源码字符数的展示参数。请求范围内的完整结果先保存，再按统一 12k 规则交付；不以另一组参数或默认值继续限制列表及源码长度。

```typescript
interface QueryCellsArgs {
  target?: ExecCellTarget;
}
```

### 2.3 精简返回值与类型

列表以每个 Cell 的 object 记录为主体，单查以完整源码为主体；不保留 cells、cell 或 source 包装。

| 位置 | 列表查询 | 指定 Cell 查询 |
| --- | --- | --- |
| 顶层 target | 不返回；Target 在每条 object 内 | 返回实际查到的 CELL Target |
| meta.status | SUCCESS／ERROR／CANCELLED | SUCCESS／ERROR／CANCELLED |
| meta.cell_status | 不返回；状态在各记录内 | 成功时为本次查询取得的 Cell 状态 |
| meta.pending_calls | 不返回；数量在各记录内 | 成功时为本次查询取得的待结清工具调用数量 |
| content | object 记录数组；无记录或失败时使用文字说明 | 完整源码文字项；有实际诊断或查询失败时使用文字说明 |

列表每条 object 只保留 target、state、pending_calls。pending_calls 是当前尚未结清的脚本工具调用数量，为非负整数，不是所有历史调用总数。删除公开的 has_unread_output；它反映 Host 内部输出移交情况，不用于判断 ROOT 是否收到或阅读了 steer。内部输出缓冲、移交状态及取消信号仍沿现有 owner 保留，不因公开字段精简而删除或改义。

```typescript
type QueryCellState =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "terminated";

interface QueryCellRecord {
  target: ExecCellTarget;
  state: QueryCellState;
  pending_calls: number;
}

interface QueryCellObjectPart {
  type: "object";
  value: QueryCellRecord;
}

interface QueryCellsListSuccess {
  meta: {status: "SUCCESS"};
  content: (QueryCellObjectPart | ExecCellTextPart)[];
}

interface QueryCellSuccess {
  target: ExecCellTarget;
  meta: {
    status: "SUCCESS";
    cell_status: QueryCellState;
    pending_calls: number;
  };
  content: ExecCellTextPart[];
}

interface QueryCellsFailure {
  target?: ExecCellTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: ExecCellTextPart[];
}

type QueryCellsResult =
  | QueryCellsListSuccess
  | QueryCellSuccess
  | QueryCellsFailure;
```

单查的源码保存为文字项，保留原字符串及换行，不添加行号，不序列化为带转义的 source.text 字段。源码为空时为 content:[]；若有实际诊断，则仍保留相应文字说明。诊断放 content，不增加结构化 error 外壳、meta.error 或公开错误代码。

没有诊断时只放源码原文。有诊断时，先放文字项“执行诊断：<实际诊断>\n\n执行源码：\n”，再放保持原样的源码文字项；前一项自行包含必要换行，runtime 不自动补分隔符。这样区分诊断和源码，不增加字段，也不把说明拼进源码原字符串；源码为空时可以只保留带上述标记的诊断项。

meta.status 只表达查询是否成功，Cell 状态是独立业务事实。成功取得状态及完整源码时，即使 cell_status 是 failed 或 terminated，仍为 SUCCESS；原脚本的诊断不冒充本次查询失败。

### 2.4 原生示例

列出当前仍持有的 Cell：

```text
工具：query_cells
参数：{}
```

保存的列表业务值示例：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {"kind": "CELL", "cell_id": "cell:<A>"},
        "state": "running",
        "pending_calls": 2
      }
    },
    {
      "type": "object",
      "value": {
        "target": {"kind": "CELL", "cell_id": "cell:<B>"},
        "state": "queued",
        "pending_calls": 0
      }
    }
  ]
}
```

原生交付中的 object 沿共同规则投影为两空格缩进 JSON：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际调用耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "CELL",
      "cell_id": "cell:<A>"
    },
    "state": "running",
    "pending_calls": 2
  }
  {
    "target": {
      "kind": "CELL",
      "cell_id": "cell:<B>"
    },
    "state": "queued",
    "pending_calls": 0
  }

tool_result: <本次完整列表结果的导出 Target>
next: null
```

查看指定 Cell：

```text
工具：query_cells
参数：
{
  "target": {"kind":"CELL","cell_id":"cell:<A>"}
}
```

假设冻结源码是 console.log('开始检查'); 加一个结束换行，保存的单查业务值为：

```json
{
  "target": {"kind": "CELL", "cell_id": "cell:<A>"},
  "meta": {
    "status": "SUCCESS",
    "cell_status": "running",
    "pending_calls": 1
  },
  "content": [
    {"type": "text", "value": "console.log('开始检查');\n"}
  ]
}
```

原生交付：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际调用耗时>

target: {"kind":"CELL","cell_id":"cell:<A>"}

meta:
  status: SUCCESS
  cell_status: running
  pending_calls: 1

content:
  console.log('开始检查');

tool_result: <本次完整单查结果的导出 Target>
next: null
```

源码是读取到的实际执行源码，不重新打开 exec_cell 的原 PATH。上述例子假设一页可完整交付；超出时 next 为 NEXT Target，而不是重新调用 query_cells 的参数。观察信息和身份占位沿第 1 章的示例约定。

### 2.5 空结果、错误及取消

| 情况 | meta.status | content 与其他字段 |
| --- | --- | --- |
| 正常列表查询，没有记录 | SUCCESS | 一条文字：“当前没有可查询的 Cell。”；无顶层 target |
| 指定 Cell 不存在、已回收或当前 Host 不可用 | ERROR | 简短实际原因；不伪造 Target、Cell 状态或数量 |
| Cell 已查到，但冻结源码读取失败 | ERROR | 返回已确认的 CELL Target；文字说明源码读取失败及实际原因，不以空源码冒充成功，不另设 source.status |
| 参数无效或其他查询失败 | ERROR | 具体原因；已经取得且有用的事实放文字，不伪造成功记录 |
| 调用取消 | CANCELLED | 说明实际取消情况；不因查询取消停止被查询的 Cell |

列表无记录与单查不可用必须区分；Host 不可用不能冒充空列表。错误及取消时不提供可选的 cell_status、pending_calls 字段组合，必要已知事实直接写入 content；确实查到 Cell 时才可返回其 Target。

错误结果同样沿完整保存和原生分页规则交付。结果已保存且正常交付完整时可以提供 tool_result 和 next:null；这只表示错误正文已读完。保存或交付失败沿共同规则报告，不提供假引用，也不重新执行原查询或脚本。

### 2.6 冻结结果与统一分页

列表一次取得当前完整记录，单查一次取得该 Cell 的状态、调用数量及完整冻结源码，形成并保存本次结果后再分页。状态和数量是查询时的快照，不在补页时重新读取 live Cell；源码也不依赖后来磁盘文件的内容。

原生按统一 12k 顺序交付；列表 object 的 JSON 文字投影及源码文字均允许跨页，不增加整条记录不可拆的限制。NEXT 只定位已保存冻结结果的未交付内容，模型原样传给 read_file；不公开内部位置，不生成查询游标、源码偏移或完整后继调用参数。

删除 source.offset_chars、returned_chars、total_chars，以及旧业务正文内的 next、has_more、next_offset_chars 等分页说明。不因一条记录无法整条装页而返回 OUTPUT_PAGE_BOUNDARY。已保存结果不因 Cell 随后完成或被回收而改变；若要观察新状态，另行调用 query_cells，不通过 NEXT 刷新。

查询和补页均不消费 steer，不重新交付 console.log／return，也不开放通过 read_file 读取整份 Cell 输出的入口。本次查询结果本身仍按普通工具结果保存、分页和导出。

## 3. manage_cell：等待或停止 Cell

### 3.1 用途与最简参数

wait 等待指定 Cell 进入终态，或在本次等待期限到达时返回实际状态；stop 停止该 Cell 的后续 JS 执行和工具提交。两种动作都不读取源码，不消费或重发 steer。

底层执行、停止、取消信号、已发生效果结算、输出移交及资源回收完全保持当前语义。本次调整参数表达及公开结果，不把停止 Cell 扩大为撤销所有工具效果、取消全部 Agent Work 或撤回已入队输出。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | CELL Target | 必填 | 要等待或停止的 Cell |
| action | wait／stop | 必填 | 等待状态或停止脚本 |
| yield_time_ms | integer | 仅 wait 可填；默认 1,000，范围 0～30,000 | 本次最多等待的毫秒数；0 立即观察，不限制脚本寿命 |

target 放首位，只接受 {kind:"CELL",cell_id:string}，身份沿共享 ID 层校验。stop 只接受 target、action；不接受 yield_time_ms。两种分支均拒绝 null、额外字段及其他 Target kind，不增加游标、输出读取、超时停止或连带取消参数。

```typescript
type ManageCellArgs =
  | {target: ExecCellTarget; action: "wait"; yield_time_ms?: number}
  | {target: ExecCellTarget; action: "stop"; yield_time_ms?: never};
```

### 3.2 wait 与 stop 的行为

| 行为 | wait | stop |
| --- | --- | --- |
| Cell 未结束 | 等待终态或本次期限到达 | 阻止后续 JS 执行和工具提交，等待取得实际终态 |
| Cell 已结束且仍可访问 | 立即返回实际状态 | 保留 completed／failed／terminated，不强制改成 terminated |
| Cell 已回收或不可用 | ERROR | ERROR，不重建或重新执行 Cell |
| 收到 console.log 输出 | 不因此结束状态等待，输出继续独立交付 | 按第 3.4 节区分输出阶段 |
| 本次等待到期但 Cell 未结束 | SUCCESS，返回 queued 或 running，不停止 Cell | 无本次等待期限参数 |

stop 等待的是 Cell 的终态，不等待所有已派发工具都完成业务结算。停止成功时 pending_calls 可以仍大于 0；不能将其当作所有工具效果已经取消或全部结算完成的确认。自然完成、失败与停止发生竞争时，如实返回实际终态。

### 3.3 停止对工具调用的影响

| 调用所处阶段 | 保持的当前语义 |
| --- | --- |
| JS 尚未执行到调用 | 不再执行 |
| 请求已经产生，但尚未实际派发 | 沿现有取消检查阻止继续派发 |
| 工具已经开始执行 | 收到 Cell 取消信号，由原工具按自身取消及效果结算规则处理；不统一承诺继续或撤销 |
| 写入、发送等业务效果已经发生 | 不回滚，保留实际调用及结果事实 |
| 已成功提交 Agent Work | 不自动取消该 Work；需要停止时另用 stop_agent |

显式停止后，不再把晚到工具结果作为新的 Cell 输出补发给 ROOT，也不让已停止 JS 恢复执行。已发生效果继续沿原调用 owner 结算和保存，不因停止丢弃业务事实，不新增恢复脚本、工具重放或连带停止机制。

例如脚本正在等待写文件时停止，文件是否仍被创建由写入的实际阶段决定；已经创建的文件保留。随后尚未执行的 console.log 和下一次工具调用不再运行，不能把停止成功说明成“文件写入已撤销”。

### 3.4 停止对各阶段 steer 输出的影响

| 输出所处阶段 | 保持的当前语义 |
| --- | --- |
| 尚未执行的 console.log 或尚未产生的顶层 return 输出 | 不再产生 |
| 输出已产生，仍在 Cell 缓冲中，尚未进入 ROOT 输入队列 | 丢弃，不再交付 |
| 正在移交队列，是否入队成功尚待确认 | 沿既有接纳确认路径按真实结果结算；未确认前不能声称已撤回，不因 stop 提前销毁确认所需内容 |
| 已成功进入 ROOT 输入队列 | stop 不撤回，继续沿既有队列消费、取消或拒绝规则处理 |
| 已进入会话或已经交给模型 | 保留，不改写已有上下文 |

console.log 返回不等于输出已经进入 ROOT 队列，更不等于模型已阅读。stop 只关闭该 Cell 的后续输出路径，不增加按 Cell 批量取消已入队 steer 的操作；不会为了报告停止再生成一条 Cell steer。原生 manage_cell 的工具结果仍正常返回。

### 3.5 精简返回值与状态

wait 和 stop 共用结果字段，正常取得结果时均保留 pending_calls：

| 字段 | 明确含义 |
| --- | --- |
| target | 实际等待或停止的 CELL Target |
| meta.status | 本次控制工具调用的 SUCCESS／ERROR／CANCELLED |
| meta.cell_status | 本次实际取得的 Cell 状态；wait 可为任意现有状态，stop 成功时为实际终态 |
| meta.pending_calls | 本次观察时尚未结清的脚本工具调用数量，非负整数 |
| content | TextPart[]；wait 通常为空，有实际诊断时保留文字；stop 返回简短说明及必要诊断 |

```typescript
type ManageCellTerminalState = "completed" | "failed" | "terminated";

interface ManageCellWaitSuccess {
  target: ExecCellTarget;
  meta: {
    status: "SUCCESS";
    cell_status: QueryCellState;
    pending_calls: number;
  };
  content: ExecCellTextPart[];
}

interface ManageCellStopSuccess {
  target: ExecCellTarget;
  meta: {
    status: "SUCCESS";
    cell_status: ManageCellTerminalState;
    pending_calls: number;
  };
  content: ExecCellTextPart[];
}

interface ManageCellFailure {
  target?: ExecCellTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: ExecCellTextPart[];
}

type ManageCellResult =
  | ManageCellWaitSuccess
  | ManageCellStopSuccess
  | ManageCellFailure;
```

成功观察到 failed 不等于 wait 失败，停止成功也不等于本次工具调用被取消，均返回 SUCCESS。CANCELLED 只用于本次调用自身的取消结算；不能因这次控制调用被取消，就宣称 Cell、已提交工具或队列输出全部已经取消。

参数无效、Cell 不可用或实际控制失败使用 ERROR。错误和取消时只返回 meta.status 和简短 content，确有已确认对象时才可保留 target；已知状态、控制效果及不确定性写入 content，不伪造状态或数量，也不构造可选元数据组合。更早失败没有业务值时沿既有错误通道报告，不伪造结果。

删除 wait 的公开 error_code，实际诊断放 content；内部错误事实、输出缓冲和取消信号继续用于原有停止与结算，不因公开字段删除而丢弃。不增加 timeout、stopped、has_unread_output、输出撤回数量或另一组状态／错误枚举。正常停止到 terminated 时说明“已停止脚本执行。”；若实际已经 completed 或 failed，则说明“脚本已结束。”，有用诊断另放文字，不假称本次终止了已完成的脚本。

### 3.6 原生示例

等待状态：

```text
工具：manage_cell
参数：
{
  "target": {"kind":"CELL","cell_id":"cell:<A>"},
  "action": "wait",
  "yield_time_ms": 1000
}
```

假设等待到期，脚本仍运行且没有诊断：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际调用耗时>

target: {"kind":"CELL","cell_id":"cell:<A>"}

meta:
  status: SUCCESS
  cell_status: running
  pending_calls: 2

content: []

tool_result: <本次完整等待结果的导出 Target>
next: null
```

若等待取得脚本失败状态，有实际诊断时仍为正常观察结果：

```text
target: {"kind":"CELL","cell_id":"cell:<A>"}

meta:
  status: SUCCESS
  cell_status: failed
  pending_calls: 0

content:
  脚本执行失败：变量 result 未定义。

tool_result: <本次完整等待结果的导出 Target>
next: null
```

停止脚本：

```text
工具：manage_cell
参数：
{
  "target": {"kind":"CELL","cell_id":"cell:<A>"},
  "action": "stop"
}
```

假设脚本已停止，但一个工具调用仍在结算：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际调用耗时>

target: {"kind":"CELL","cell_id":"cell:<A>"}

meta:
  status: SUCCESS
  cell_status: terminated
  pending_calls: 1

content:
  已停止脚本执行。

tool_result: <本次完整停止结果的导出 Target>
next: null
```

Cell 已回收或不可用，且错误结果已正常保存交付时：

```text
meta:
  status: ERROR

content:
  无法操作 Cell cell:<A>：当前 Host 中该 Cell 已不可用。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

本工具的诊断及说明均使用 type:text、value:string 内容项；正常 wait 无诊断时为 content:[]。示例假设结果一页可完整交付，实际观察信息沿共同规则顶层并列，不伪造耗时。

### 3.7 冻结结果与实施边界

状态、pending_calls 和诊断在本次观察或控制结算时冻结，完整保存后原生按 12k 分页。NEXT 只补读该冻结结果，不重复等待、停止、重新查询 Cell 状态或读取 steer；补页时 pending_calls 不随工具后续结算更新。

删除停止前因结果不满足展示页预算而拒绝执行 stop 的旧检查；这沿共同的业务执行与展示分离规则处理，不改变底层停止、工具取消及输出归属。保存或交付失败如实保留已知效果，不因模型没收到停止说明就重做操作，不提供假 tool_result 或 NEXT。

实施只需接通本章的参数／结果合同及共同交付路径，不重写现有停止或队列机制，不增加撤回已入队 steer、回滚工具效果、连带取消 Work、等待全部调用结算或跨重启恢复的能力。重复停止只在 Cell 仍可访问时按实际状态返回；回收后的引用不因曾成功停止而恢复可用。

## 4. 当前生产实现参考

- [控制工具参数、Cell 类型及资源配置](../src/pulsara_agent/code_mode/contracts.py)
- [原生工具目录与描述](../src/pulsara_agent/capability/builtin_catalog.py)
- [ROOT 与权限检查](../src/pulsara_agent/conversation_kernel/tool_runtime_authorization.py)
- [源码读取、冻结及原生调用接入](../src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [本地文件源码读取](../src/pulsara_agent/tools/builtins/filesystem.py)
- [Cell 接纳、状态及输出管理](../src/pulsara_agent/code_mode/host.py)
- [内部工具调用、取消检查及效果结算](../src/pulsara_agent/conversation_kernel/tool_execution.py)
- [Cell 输出接纳及队列移交](../src/pulsara_agent/conversation_kernel/host_prompts.py)
- [Cell 输出队列存储](../src/pulsara_agent/conversation_kernel/_repository/prompts.py)
- [脚本控制台输出](../src/pulsara_agent/code_mode/node/guest.js)
- [脚本执行与顶层 return](../src/pulsara_agent/code_mode/node/worker.mjs)

这些链接用于核对实施接点，不表示当前代码已经符合本草案。
