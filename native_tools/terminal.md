# Pulsara 终端工具修订与使用教程草案

共用规则见[原生工具修订草案索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章。

## 1. exec_process：启动命令与首次观察

### 1.1 用途与范围

启动一次新的本地 shell 命令，返回首次观察到的进程事实和实际公开输出。每次调用都是新 shell；本次命令里的 cd、export 等不成为下次调用的目录或环境状态。工作目录、权限、环境及终端处理继续由现有 owner 承担。底层 terminal owner 与物理资源调度继续共享，进程归属及模型工具访问按调用上下文的 session_id 隔离；ROOT 与 subagent session 等价，各自只能查询、观察和控制本 session 的进程。

yield_time_ms 只决定首次最多等待多久，不是命令寿命限制。首次观察返回时进程可以仍在运行；后续通过其 PROCESS Target 继续查询、观察或交互，不重新执行 command。

完整结果指本次观察选定的完整可用输出和进程事实，不要求等待进程结束，也不承诺尚未观察的整个进程生涯都已经入库。不返回 observe 便捷调用字段或公开游标；尚未观察的公开输出按第 1.8 节暂存，不因固定容量阈值被淘汰。

### 1.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| command | string | 必填 | 要启动的 shell 命令，不是 argv 数组，不接受空白命令 |
| workdir | string | 可选，默认工作区根 | 本次命令的现有工作目录，支持工作区相对路径、绝对路径及 `~`，仍检查实际权限 |
| yield_time_ms | integer | 可选，默认 10,000，范围 0–30,000 | 首次观察最多等待的毫秒数；0 立即返回，不限制命令寿命 |
| tty | boolean | 可选，默认 false | 是否使用伪终端，用于需要终端交互或行编辑的程序 |

```typescript
interface ExecProcessArgs {
  command: string;
  workdir?: string;
  yield_time_ms?: number;
  tty?: boolean;
}
```

删除 max_output_chars，不提供另一种字符数预览参数。原生入口统一按 12k 分页，不再由启动参数选择尾部预览长度；JS 返回本次观察的完整业务值。command／workdir 现有长度约束的依据尚需单独核对，本节不为它们选定新的 cap，也不将删除展示参数解释为取消真实执行资源或权限边界。

### 1.3 状态、元数据与正文分工

| 字段 | 含义 |
| --- | --- |
| target | 本次命令的 PROCESS Target，用于后续查询、观察或交互；正常取得已退出结果时仍保留 |
| meta.status | 工具调用状态：SUCCESS／ERROR／CANCELLED，与进程是否成功退出分开 |
| meta.process_status | RUNNING 或 EXITED；退出码非零也属于 EXITED |
| meta.exit_code | RUNNING 时为 null，EXITED 时为真实退出码，不用 -1 充当运行中占位 |
| meta.cwd | 实际解析后的工作目录，使用绝对路径 |
| content | TextPart[]，仅保存本次实际取得的终端公开输出；没有输出为 [] |

观察时间和本次工具观察耗时仍在原生消息顶层，不混进日志或重复放进 meta。命令输出即使恰好是 JSON，也仍为文字项，不自动升级成 object、Target 或图片。

上述进程事实是本次观察取得并保存的快照。补页、导出或返回这次 JS 业务值时不重新查询进程，不将保存时的 RUNNING/null 改成后来取得的 EXITED/退出码；进程记录回收也不改变已保存结果。query_processes 的 duration_seconds 等记录同样固定在本次查询时刻，不随补页继续增长。工具状态与进程状态保持独立，原生分页不承担刷新进程的职责。

```typescript
type ProcessTarget = {
  kind: "PROCESS";
  process_id: string;
};

interface ExecProcessRunning {
  target: ProcessTarget;
  meta: {
    status: "SUCCESS";
    process_status: "RUNNING";
    exit_code: null;
    cwd: string;
  };
  content: TextPart[];
}

interface ExecProcessExited {
  target: ProcessTarget;
  meta: {
    status: "SUCCESS";
    process_status: "EXITED";
    exit_code: number;
    cwd: string;
  };
  content: TextPart[];
}

type ExecProcessSuccess = ExecProcessRunning | ExecProcessExited;

interface ExecProcessFailure {
  target?: ProcessTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: TextPart[];
}
```

正常取得进程结果时返回 ExecProcessSuccess；实际启动、执行或结果处理失败及取消沿既有 owner 结算。确实形成完整失败业务值时使用 ExecProcessFailure：没有创建进程时省略 target，已经创建且 Target 已确定时保留该引用；不伪造 cwd、进程状态或退出码，已知进程事实、已发生效果及不确定性写入 content。

JS 沿异常通道通过 err.result 提供实际存在的完整 ExecProcessFailure；没有业务结果时不伪造 result。原生完整保存的失败值仍按正常结果规则分页，保存或交付本身失败则沿统一草案报告。失败及取消不自动重做启动命令，不把进程非零退出或正常取得的信号退出结果当成工具失败。

不再重复返回 terminal_process_action:start、yielded_to_background 或另一组调用状态；正文没有自动生成的“仍在运行”“命令失败”等总结。命令自身输出的相同文字属于真实日志，正常保留。原生展示不使用 output_preview、显示截断布尔值或旧 read 调用，使用本次完整结果的 tool_result 与 next。

### 1.4 教程：运行中

假设命令已经输出 started，但尚未结束：

```text
工具：exec_process
参数：
  command: "printf 'started\\n'; sleep 30; printf 'done\\n'"
  workdir: "/workspace"
  yield_time_ms: 1000
```

首次输出可以完整装入一页时，原生返回：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

target: {"kind":"PROCESS","process_id":"process:<进程ID>"}

meta:
  status: SUCCESS
  process_status: RUNNING
  exit_code: null
  cwd: /workspace

content:
  started

tool_result: <完整结果的导出 Target>
next: null
```

同一观察的 JS 正常返回值：

```json
{
  "target": {"kind": "PROCESS", "process_id": "process:<进程ID>"},
  "meta": {
    "status": "SUCCESS",
    "process_status": "RUNNING",
    "exit_code": null,
    "cwd": "/workspace"
  },
  "content": [
    {"type": "text", "value": "started\n"}
  ]
}
```

尚无输出时 content:[]，不加入一条状态说明。RUNNING 与 next:null 可以同时成立：本次保存的输出已交付完毕，进程仍可继续运行。

### 1.5 教程：正常退出与非零退出

命令 `printf 'hello\n'` 正常结束时，原生返回示意：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

target: {"kind":"PROCESS","process_id":"process:<进程ID>"}

meta:
  status: SUCCESS
  process_status: EXITED
  exit_code: 0
  cwd: /workspace

content:
  hello

tool_result: <完整结果的导出 Target>
next: null
```

JS 正常返回：

```json
{
  "target": {"kind": "PROCESS", "process_id": "process:<进程ID>"},
  "meta": {
    "status": "SUCCESS",
    "process_status": "EXITED",
    "exit_code": 0,
    "cwd": "/workspace"
  },
  "content": [
    {"type": "text", "value": "hello\n"}
  ]
}
```

命令 `printf 'problem\n' >&2; exit 7` 已结束且本次观察正常取得时，原生返回：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

target: {"kind":"PROCESS","process_id":"process:<进程ID>"}

meta:
  status: SUCCESS
  process_status: EXITED
  exit_code: 7
  cwd: /workspace

content:
  problem

tool_result: <完整结果的导出 Target>
next: null
```

JS 同样正常返回，不抛 ToolError：

```json
{
  "target": {"kind": "PROCESS", "process_id": "process:<进程ID>"},
  "meta": {
    "status": "SUCCESS",
    "process_status": "EXITED",
    "exit_code": 7,
    "cwd": "/workspace"
  },
  "content": [
    {"type": "text", "value": "problem\n"}
  ]
}
```

退出码说明命令结果，不重复生成 error:Error 等字段，也不擅自总结日志里的失败原因。

### 1.6 JS 使用及输出选择

```javascript
const result = await tools.exec_process({
  command: "printf 'hello\\n'",
  workdir: "/workspace",
  yield_time_ms: 1000
});

console.log({content: result.content});
if (result.meta.process_status === "EXITED") {
  console.log(result.meta.exit_code);
}
```

该脚本分别选择输出正文及退出码，不自动附加原生观察外壳、tool_result、next 或其他中间值。非零退出仍能按同一代码路径访问 result；真实工具错误才进入异常通道。后续观察或控制同一进程使用第 2 节的 manage_process。

### 1.7 结果补页与新输出观察

原生先完整保存本次观察结果，再按 12k 交付；JS 正常取得本次完整业务值，不受这项分页限制。tool_result 指向本次保存结果，next 只补读其中未交付部分，不重新执行命令，也不取得进程以后产生的输出。

exec_process 的原生结果和 JS 完整业务值均不返回 output_cursor 或其他公开日志游标。runtime 维护该进程的内部观察位置，先完整接住首次观察结果，再将位置推进到本次观察末尾；模型不需要复制或构造游标，也不增加公开的替代位置字段。

内部观察位置不表示模型已经读完结果。若原生只交付第一页，未交付的旧正文通过 next 补读；读 next 不推进进程观察位置。进程结束也不代表本次结果已经交付完，仍可有非空 next。后续 manage_process 沿第 2 节使用同一内部位置。

### 1.8 待观察日志的透明暂存

正常运行、存储读写成功期间，已经收集但尚未形成完整观察结果的公开输出，不因固定字节阈值而被淘汰。公开输出仍经过既有终端处理，不另存一份未处理原始流绕过原有边界。

内存保持有界，放不下的待观察输出由内部临时文件承接。取消达到单进程 16MiB／Host 合计 128MiB 就淘汰未观察日志的行为；这两个旧值若继续用于内存缓存，只能决定何时转入临时文件，不能成为日志总量上限或超量终止进程的依据。缓存额度的必要性和具体取值后续通过资源实验确定，本节不选定新数值。这里的 16／128MiB 与 Code Mode 的 16／64MiB 暂存额度不是同一项资源。

runtime 按内部观察位置取得本次新增输出，完整保存为工具结果后才推进位置；对应日志可以释放暂存空间，不必另外保留一份完整进程历史。尚未纳入结果的新输出继续暂存，其余清理沿既有进程 owner 生命周期处理，不新增跨重启恢复承诺。内部观察位置不以模型读完分页为条件，也不以 JS 是否显式输出给 ROOT 为条件。

保留现有结束进程回收规则：结束满一小时可回收；共享 owner 下的已结束记录合计超过 32 条时可优先回收旧记录，并继续遵守既有在途读取保护。回收可以包含尚未观察的日志，不因“未读”而无限延长保留；运行中不按这两项规则回收。共享 owner 关闭仍按既有生命周期清理。上述数值暂不调整，后续实验再评估。

进程记录回收后，对应 PROCESS Target 及未转入完整结果的日志可能不可用。已经完整保存的工具结果不依赖进程暂存继续存在，仍按其既有结果生命周期分页和导出；不为解释回收新增墓碑、过期记录或恢复任务。

临时日志对模型透明：原生与 JS 均不返回临时文件路径、内部位置或缓存占用，不要求模型管理暂存或复制游标，不新增暂存 Target、ACK 或可读记录。模型只使用 PROCESS Target 观察进程、NEXT 补读固定结果，以及既定的完整结果导出能力。

例如模型启动构建后，进程新增 40MiB 日志而模型尚未观察；这 40MiB 由内存及临时文件完整承接。下一次观察完整保存为结果 B 后，原生只交付 B 的第一页并提供 NEXT，JS 则取得完整 B，此时可回收 B 对应的暂存。之后进程再新增 10MiB，属于下一次观察的待处理内容；读 B 的 NEXT 不包含或消费这 10MiB。导出 B 会写出 B 的完整 40MiB，即使模型只看过第一页，也不需要继续保留原临时日志。

内部暂存与 write_file 导出不绑定：暂存等待观察，导出读取已经保存的完整工具结果。一次结果的导出不自动包含进程后续输出，也不代表整个进程历史；本节不展开 write_file 的 source 参数分支。

磁盘满、读写失败等真实资源问题沿既有错误通道明确说明原因、实际缺失及已知进程效果，不能把残缺输出当作完整结果成功交付，也不能静默越过缺失或自动重做命令。不原样保留旧的 output_disposition、retained_from_cursor、多组 coverage／gap 字段，不为这些失败新增状态矩阵或恢复机制。

### 1.9 实施核对点

以下是采用已确定合同后的行为验证要求，本轮未实施或运行验证：

- 参数仅 command、workdir、yield_time_ms、tty，不接受 max_output_chars；首次等待与命令寿命分开，不引入累计寿命 cap。
- meta.status 为工具调用状态，process_status／exit_code 为进程事实；RUNNING 使用 null 退出码，EXITED 使用真实退出码。
- 正常取得非零退出的命令结果时返回 SUCCESS，JS 正常返回，不因初次 yield 与后续观察的时点不同改变异常行为。
- 顶层 PROCESS Target 与 meta.cwd 表达实际事实；RUNNING 和 EXITED 的原生结果与 JS 完整业务值都不返回公开日志游标。
- content 仅为实际公开终端输出的 type:text/value 数组，无输出为 []；不自动解析输出 JSON，不追加重复运行或失败说明。
- 原生观察信息保持顶层，结果按统一 12k 交付；JS 返回完整本次观察。runtime 在完整接住首次结果后推进内部观察位置，next 只补读固定结果，不改变该位置。
- 未观察输出超过旧单进程 16MiB 或 Host 合计 128MiB 时，转由临时文件承接而非淘汰或杀进程；本次结果完整保存后才推进内部位置并回收对应暂存，分页和后续新增输出互不混淆。
- 已结束记录仍按一小时／共享 owner 合计 32 条的既有规则回收，未观察日志可随之回收；运行中进程及已保存完整结果不因这些规则被删。底层 owner 共享，进程访问按 session_id 隔离，ROOT 与 subagent session 等价。
- 暂存不暴露临时路径、位置、额度或额外 Target；保存后的完整结果不依赖原暂存继续存在，导出不会混入后续日志。真实资源失败明确说明，不静默截断、不自动重做命令。
- 不保留 observe、动作 start 回显、yielded_to_background、output_preview 或显示截断标记，不增加公开游标的替代字段或旧 coverage／gap 字段组合。

## 2. manage_process：观察与控制已有进程

### 2.1 用途与动作

操作本 session 的 exec_process 已启动的进程，复用其 PROCESS Target，不重新执行启动命令。底层 terminal owner 继续共享，但访问按调用上下文的 session_id 隔离：ROOT 与 subagent session 等价，不能查询、观察或控制其他 session 的进程，即使拿到其 PROCESS Target 也不例外。不新增可由模型填写的 session_id 或跨 session 接管入口。同一 session 内跨 turn、原生与 JS 使用同一进程的内部观察位置，不增加独立读者、重放模式或公开位置参数。

| action | 行为 | 日志与位置 |
| --- | --- | --- |
| wait | 等待进程结束或本次等待时间到，再观察；timeout_seconds=0 时立即观察 | 返回全部待观察增量，完整保存后推进内部位置 |
| write | 将 data 按 UTF-8 原样写入所选输入通道并刷新，再等待至 yield_time_ms 指定的观察时点或进程结束 | 同样返回全部待观察增量，包含发送输入前尚未观察的输出，完整保存后推进位置 |
| close_stdin | 关闭进程输入，沿既有 PIPE／PTY 处理，不保证进程立即退出 | 仅返回控制说明及实际进程事实，不读取日志、不推进位置 |
| kill | 停止命令及其子进程并等待实际收束；无法收束时报告失败 | 仅返回控制说明及实际进程事实，不读取日志、不推进位置 |

wait 的等待条件是进程结束或等待时间到，不因出现新输出、终端回显或已有待观察日志就宣称交互完成。两个等待参数均只限制本次等待，不限制进程寿命。

wait 保持可在只读模式下观察的语义；write、close_stdin、kill 保留现有控制动作的权限和作用域边界。不借删除游标扩大跨会话访问权限。

### 2.2 最简参数表与分支类型

target 在参数表、类型与示例中始终放首位。每次只能填写对应 action 的参数，未使用字段应省略，不通过 null 选择默认行为。

| 参数 | 类型 | 适用动作 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- | --- |
| target | PROCESS Target | 全部 | 必填 | 既有进程的完整 Target |
| action | string 枚举 | 全部 | 必填 | wait、write、close_stdin、kill 四选一 |
| timeout_seconds | integer | wait | 可选，默认 30，范围 0–30 | 本次最多等待多少秒；0 立即观察 |
| data | string | write | 必填，允许空串 | 原样发送的 UTF-8 输入文字，不自动追加换行 |
| yield_time_ms | integer | write | 可选，默认 1,000，范围 0–30,000 | 输入刷新后最多等待的毫秒数；0 立即观察 |

```typescript
type ManageProcessArgs =
  | {
      target: ProcessTarget;
      action: "wait";
      timeout_seconds?: number;
      data?: never;
      yield_time_ms?: never;
    }
  | {
      target: ProcessTarget;
      action: "write";
      data: string;
      yield_time_ms?: number;
      timeout_seconds?: never;
    }
  | {
      target: ProcessTarget;
      action: "close_stdin" | "kill";
      timeout_seconds?: never;
      data?: never;
      yield_time_ms?: never;
    };
```

类型中的 never 仅表达分支互斥，不是新增参数。整数及范围由实际输入校验保证，拒绝额外字段。

poll 合并到 wait，不保留旧动作或别名；原先立即观察改为 timeout_seconds:0。submit 合并到 write，不保留自动追加 LF 的分支；需要发送换行时由模型在 data 中显式填写。删除 since_cursor、max_output_chars，不新增替代游标、预览参数或提交开关。

### 2.3 返回字段与状态

| 字段 | 含义 |
| --- | --- |
| target | 本次操作的 PROCESS Target |
| meta.status | SUCCESS／ERROR／CANCELLED，表达工具调用状态 |
| meta.process_status | 正常取得结果时的 RUNNING／EXITED，与退出码是否为零分开 |
| meta.exit_code | RUNNING 为 null，EXITED 为实际退出码 |
| meta.cwd | 实际绝对工作目录 |
| content | TextPart[]；wait／write 为完整待观察输出，close_stdin／kill 为简短控制说明 |

成功值复用第 1 节已声明的进程事实形状；失败值不伪造进程属性：

```typescript
type ManageProcessSuccess = ExecProcessSuccess;

interface ManageProcessFailure {
  target: ProcessTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: TextPart[];
}
```

wait／write 没有新增输出时 content:[]，不添加“仍在运行”等日志外总结。进程非零退出仍正常返回 SUCCESS，JS 不因此抛错。close_stdin 成功可以返回“已关闭进程输入。”；kill 实际完成终止后可以返回“已停止进程及其子进程。”。如果目标此前已经退出，只说明实际状态，不声称本次执行了终止。

原生观察时间与微秒耗时在顶层并列，另有完整结果 tool_result 和分页 next。JS 直接取得完整业务值，不附带原生时间外壳、tool_result 或 next。主动 kill 成功不等于工具被取消，不增加 KILLED 公开调用状态；目标结束情况仍由实际进程事实及必要说明表达。

不返回 terminal_process_action 回显、yielded_to_background、output_cursor、retained_from_cursor、observe、output_preview、旧 read 指令及显示截断布尔值。不将旧 output_disposition 或多组 coverage／gap 字段原样搬入新合同。真实执行或存储失败的有用事实写入 content，不新增 meta.error、timed_out 或其他错误分类字段；本次等待到时且正常取得结果本身不是错误。

### 2.4 教程：立即观察或等待

原生立即观察：

```text
工具：manage_process
参数：
  target: {"kind":"PROCESS","process_id":"process:<进程ID>"}
  action: wait
  timeout_seconds: 0
```

假设进程新增两行输出且仍在运行，本次内容可完整装入一页：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

target: {"kind":"PROCESS","process_id":"process:<进程ID>"}

meta:
  status: SUCCESS
  process_status: RUNNING
  exit_code: null
  cwd: /workspace

content:
  编译完成
  开始测试

tool_result: <完整结果的导出 Target>
next: null
```

同一观察的 JS 完整返回值：

```json
{
  "target": {"kind": "PROCESS", "process_id": "process:<进程ID>"},
  "meta": {
    "status": "SUCCESS",
    "process_status": "RUNNING",
    "exit_code": null,
    "cwd": "/workspace"
  },
  "content": [
    {"type": "text", "value": "编译完成\n开始测试\n"}
  ]
}
```

JS 等待至多 30 秒并显式选择输出：

```javascript
const result = await tools.manage_process({
  target: {kind: "PROCESS", process_id: "process:<进程ID>"},
  action: "wait",
  timeout_seconds: 30
});

console.log({content: result.content});
if (result.meta.process_status === "EXITED") {
  console.log(result.meta.exit_code);
}
```

上次完整结果保存后，如果没有新增输出，再观察返回空 content，不重复旧日志。正常取得非零退出结果时仍走相同的正常返回路径。

### 2.5 教程：写入与全部待观察增量

write 仅按 data 的实际值发送，不自动补换行、不转换 CR／LF、不根据末尾字符推断提交意图。标准参数解码后按 UTF-8 写入所选通道，不做二次转义解释。PTY 的终端处理与目标程序仍可能进一步解释或转换输入，工具不承诺应用接收到的字符必然未经终端处理。

tty 仍由模型在 exec_process 启动时选择，默认 false。输入什么文字、控制字符或序列，由模型根据先验知识或使用时探索决定；不增加按程序分类的教学、自动模式识别、程序适配表、粘贴动作或提交开关。

假设上次观察后，进程已经输出“警告 A”，但模型尚未观察；下面的示例进程收到一行输入后输出 ACK 并退出：

```text
工具：manage_process
参数：
  target: {"kind":"PROCESS","process_id":"process:<进程ID>"}
  action: write
  data: "hello\n"
  yield_time_ms: 1000
```

原生返回：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

target: {"kind":"PROCESS","process_id":"process:<进程ID>"}

meta:
  status: SUCCESS
  process_status: EXITED
  exit_code: 0
  cwd: /workspace

content:
  警告 A
  ACK:hello

tool_result: <完整结果的导出 Target>
next: null
```

JS 调用：

```javascript
const result = await tools.manage_process({
  target: {kind: "PROCESS", process_id: "process:<进程ID>"},
  action: "write",
  data: "hello\n",
  yield_time_ms: 1000
});

console.log({content: result.content});
```

JS 完整返回值：

```json
{
  "target": {"kind": "PROCESS", "process_id": "process:<进程ID>"},
  "meta": {
    "status": "SUCCESS",
    "process_status": "EXITED",
    "exit_code": 0,
    "cwd": "/workspace"
  },
  "content": [
    {"type": "text", "value": "警告 A\nACK:hello\n"}
  ]
}
```

发送前的“警告 A”不会因 write 而跳过。write 不以输入发送时刻重置观察起点，也不承诺所有返回输出都是这次输入引起的。空 data 不添加字符，但仍检查进程输入是否可用，并按同一规则等待、取得待观察增量；需要纯观察时可直接使用 wait。

### 2.6 教程：关闭输入与停止进程

原生关闭输入：

```text
工具：manage_process
参数：
  target: {"kind":"PROCESS","process_id":"process:<进程ID>"}
  action: close_stdin
```

若关闭成功且进程尚未结束，原生返回：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

target: {"kind":"PROCESS","process_id":"process:<进程ID>"}

meta:
  status: SUCCESS
  process_status: RUNNING
  exit_code: null
  cwd: /workspace

content:
  已关闭进程输入。

tool_result: <完整结果的导出 Target>
next: null
```

对应的 JS 值使用相同业务字段：

```json
{
  "target": {"kind": "PROCESS", "process_id": "process:<进程ID>"},
  "meta": {
    "status": "SUCCESS",
    "process_status": "RUNNING",
    "exit_code": null,
    "cwd": "/workspace"
  },
  "content": [
    {"type": "text", "value": "已关闭进程输入。"}
  ]
}
```

关闭输入后观察进程：

```javascript
const closed = await tools.manage_process({
  target: {kind: "PROCESS", process_id: "process:<进程ID>"},
  action: "close_stdin"
});
console.log({content: closed.content});

const observed = await tools.manage_process({
  target: closed.target,
  action: "wait",
  timeout_seconds: 30
});
console.log({content: observed.content});
```

原生停止进程：

```text
工具：manage_process
参数：
  target: {"kind":"PROCESS","process_id":"process:<进程ID>"}
  action: kill
```

JS 停止并另行观察剩余日志：

```javascript
const stopped = await tools.manage_process({
  target: {kind: "PROCESS", process_id: "process:<进程ID>"},
  action: "kill"
});
console.log({content: stopped.content});

const remaining = await tools.manage_process({
  target: stopped.target,
  action: "wait",
  timeout_seconds: 0
});
console.log({content: remaining.content});
```

例如实际完成终止、退出码为 -15 时，kill 的完整业务值为：

```json
{
  "target": {"kind": "PROCESS", "process_id": "process:<进程ID>"},
  "meta": {
    "status": "SUCCESS",
    "process_status": "EXITED",
    "exit_code": -15,
    "cwd": "/workspace"
  },
  "content": [
    {"type": "text", "value": "已停止进程及其子进程。"}
  ]
}
```

退出码必须来自实际结算，不固定为 -15。原生按相同字段投影，并在顶层附加观察信息、tool_result 和 next。close_stdin／kill 的控制说明不是日志读取；两个动作均不推进内部位置，不因返回控制结果而清空待观察日志。后续 wait 仍可取得对应的剩余输出，沿既有进程 owner 生命周期处理，不新增永久日志归档。

### 2.7 完整观察、失败与取消

exec_process 的首次观察以及后续 wait／write 共用该进程的内部观察位置，原生与 JS 切换时接着读取。runtime 完整保存本次观察结果后才推进位置并回收对应暂存；没有形成完整保存结果时，不将其记为已经观察。不要求模型读完原生分页或脚本已经 console.log，才允许推进内部位置。

并行取得同一进程的增量时，由现有进程 owner 协调取数和位置推进，不让两个成功观察重复消费同一段或跳过尚未保存的输出；不建立持久消费者、独立 reader 身份或 ACK。并发追加的输出按本次观察边界划分，边界后的内容留给下一次观察。

原生先完整保存本次结果再按 12k 分页；JS 取得完整业务值。next 只补读这份固定结果，不重新 wait、不发送 data、不改变进程内部观察位置，也不混入进程后来产生的输出。内部暂存、资源失败与导出继续遵循第 1.8 节。

输入已关闭、进程不可操作、权限不足、输入写入失败或终止无法收束等实际错误，不伪造正常结果。确实形成失败业务值时，只使用请求对象 target、meta.status 和 content；实际已知的进程事实、已发送输入或不确定性直接写入 content，不补假的 cwd、退出码或状态。

例如 write 因输入已关闭而未发送内容：

```json
{
  "target": {"kind": "PROCESS", "process_id": "process:<进程ID>"},
  "meta": {"status": "ERROR"},
  "content": [
    {"type": "text", "value": "无法写入进程 process:<进程ID>：输入已关闭，本次未发送内容。"}
  ]
}
```

JS 失败沿异常通道，通过 err.result 提供实际存在的完整 ManageProcessFailure；没有业务结果时不伪造 result。原生已完整保存的失败值仍按正常结果分页，错误说明中不增加 _hint 或重复错误代码。参数校验与运行时错误沿既有通道表达。

取消与控制副作用沿既有 owner 结算：已发送的输入、已关闭的通道或已发出的终止请求不能因取消或后续保存、交付失败被说成没有发生；如实报告已知效果或不确定性，不自动重做 write、close_stdin 或 kill。删除仅因旧展示回执预算不足而在控制动作前拒绝执行的检查，保留真实权限、输入可用性、执行资源及物理收束边界。

### 2.8 本机输入小实验记录

2026-11-12，使用仓库根 .venv 的 Python，经当前 TerminalManager 的 PIPE／PTY 路径执行逐行输入示例：先输出 READY，再执行 value=input()，最后打印 repr(value)。输入用 write 原样发送，发送后观察 500ms；这只是实验等待时间，不是产品超时或资源阈值。测试关闭 shell 环境快照，使用临时目录，结束后回收全部测试进程；未修改生产配置、代码或数据库。

下表用 Python 字符串字面量表示实际发送值与取得值：

| tty | 发送内容 | 实测结果 |
| --- | --- | --- |
| false | `"hello\n"` | 示例正常结束，取得 `"hello"` |
| false | `"hello\r"` | 500ms 内仍等待；随后关闭输入，示例结束并取得 `"hello\r"` |
| false | `"hello\r\n"` | 示例正常结束，取得 `"hello\r"`，CR 保留在正文中 |
| true，示例未修改默认终端输入模式 | `"hello\r"` | 示例正常结束，取得 `"hello"`；输出观察中还有终端回显 |

另用未带 -u 的 Python 子进程通过 PIPE 输入并关闭输入，确认 CR 和 CRLF 两项取得值同样保留 CR。实验记录说明当前环境中的实际传递行为，不推导所有程序或终端都使用同一规则，不形成程序分类教程，也不授权 runtime 自动转换字符。没有据此验证任意 coding-agent TUI 的端到端兼容性。

### 2.9 实施核对点

以下是拟议合同的行为验证要求，除第 2.8 节记录的当前实现小实验外，本轮未实施或运行新合同验证：

- 公开动作只有 wait、write、close_stdin、kill；wait 的 0–30 秒边界、默认值及立即观察行为明确，删除 poll／submit 及其兼容分支。
- 参数按动作互斥且 target 在先；删除 since_cursor、max_output_chars，不提供公开位置、替代预览或提交开关。
- write 原样发送 data，不自动补 LF，不根据内容判断交互意图；原生与 JS 使用相同输入路径，PTY 及应用自身的解释保持实际语义。
- wait／write 取得全部待观察增量，包含 write 发送前的待观察输出；同一进程共用内部位置，完整保存后才推进并回收对应暂存，不因切换原生／JS 或并发调用而漏读、重复消费。
- 查询、日志观察和控制只接受当前 session_id 归属的进程，ROOT 与 subagent session 等价；跨 turn 及原生／JS 共用本 session 进程位置，不因共享 owner 而越界。
- close_stdin／kill 不读取或消费日志，不推进位置；控制成功不伪造进程已经退出，kill 已退出目标不声称本次执行了终止。
- 返回值遵循 target/meta/content，工具状态与进程状态分开；非零退出、主动终止成功均正常返回 SUCCESS，退出码为真实值。
- 无新增输出为 content:[]；控制结果为简短说明；失败原因及已知效果在 content，JS 仅在有完整失败业务值时提供 err.result，不新增错误字段矩阵。
- 原生观察信息保持顶层，12k 分页和 NEXT 只读取固定结果；JS 始终取得完整本次业务值，主动图文输出仍由脚本选择。
- 待观察日志按第 1.8 节透明暂存，不因旧内存阈值被淘汰，不暴露临时路径或占用；保存、交付和取消不自动重做控制动作。

## 3. query_processes：进程清单与元数据

### 3.1 用途与查询边界

查询共享终端 owner 中归属于当前 session_id、且仍保留的进程记录。省略 target 只列出本 session 的当前记录，填写 PROCESS Target 也必须校验归属本 session；ROOT 与 subagent session 等价，各看各的，ROOT 不因层级关系自动取得其他 session 的模型工具访问权。session_id 来自调用上下文，不新增模型参数或 Target 字段。不查询操作系统全部进程，不承诺完整命令历史，也不恢复已回收的记录。列表保持按启动时间从早到晚排列，进程记录回收按第 1.8 节执行。

query_processes 是只读元数据查询：不取得或交付日志，不推进进程内部观察位置，不消费或释放待观察日志暂存。原生与 JS 使用同一查询边界；查询进程结束状态不意味着模型已经观察了它的日志。

### 3.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PROCESS Target | 可选，省略表示列表查询 | 填写则查询这一个进程；不接受 null |

```typescript
interface QueryProcessesArgs {
  target?: ProcessTarget;
}
```

target 是唯一参数，始终放在参数表、类型和示例首位。删除 include_running、include_finished，不增加新的筛选枚举、limit、公开游标或 continuation。需要筛选时，模型直接阅读状态，或由 JS 处理完整返回值；原生较大结果沿统一 12k 分页交付。

### 3.3 单查与列表共用的记录类型

完整业务值只使用 meta 和 content。每个进程为一个 type:object 内容项，value 中的 target 放首位；不在顶层重复 target，不保留 process／processes 两种包装或另外一份 Target 清单。

| 字段 | 明确含义 |
| --- | --- |
| meta.status | 本次查询调用的 SUCCESS／ERROR／CANCELLED |
| content | 进程 object 记录与必要的文字说明，保持有序 |
| 记录的 target | 供后续查询、观察或控制使用的 PROCESS Target |
| 记录的 command | 启动该进程时的 shell 命令 |
| 记录的 cwd | 实际绝对工作目录 |
| 记录的 process_status | RUNNING／EXITED，不用它表达查询是否成功 |
| 记录的 exit_code | RUNNING 为 null，EXITED 为实际退出码 |
| 记录的 stdin_closed | 标准输入关闭标记；false 不保证进程仍可写，还需考虑进程状态和实际输入通道 |
| 记录的 duration_seconds | 运行中的已持续时间，或结束后的总运行时间，单位为秒；不是本次查询耗时 |

```typescript
type QueryProcessRecord = {
  target: ProcessTarget;
  command: string;
  cwd: string;
  stdin_closed: boolean;
  duration_seconds: number;
} & (
  | {
      process_status: "RUNNING";
      exit_code: null;
    }
  | {
      process_status: "EXITED";
      exit_code: number;
    }
);

interface QueryProcessPart {
  type: "object";
  value: QueryProcessRecord;
}

interface QueryProcessesSuccess {
  meta: {
    status: "SUCCESS";
  };
  content: (QueryProcessPart | TextPart)[];
}

interface QueryProcessesFailure {
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: TextPart[];
}
```

单查命中时包含一条进程 object，列表命中时包含一到多条完全相同形状的进程 object。必要说明可作为相邻 text 项出现，因此不要求成功单查的整个 content 数组永远只有一项。JS 不根据单查或列表切换字段访问路径。

查询到正常结束、非零退出或被终止的进程时，查询本身仍为 SUCCESS，记录使用实际 EXITED 与退出码。删除独立 timed_out 布尔值；确有必要公开的超时等事实放相邻文字项，不增加原因字段矩阵。不返回 terminal_process_action 回显、旧 status 别名、进程内部身份、公开游标或日志完整性字段。

### 3.4 教程：列表查询与原生投影

原生列出当前记录：

```text
工具：query_processes
参数：{}
```

假设本 session 当前有一个运行中进程和一个已非零退出的进程，本次结果可以完整装入一页。每条记录的完整 value 按共同规则投影为两空格缩进 JSON，target 保留在对象内部：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际查询耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "PROCESS",
      "process_id": "process:<进程A的ID>"
    },
    "command": "python worker.py",
    "cwd": "/workspace",
    "process_status": "RUNNING",
    "exit_code": null,
    "stdin_closed": false,
    "duration_seconds": 12.3
  }
  {
    "target": {
      "kind": "PROCESS",
      "process_id": "process:<进程B的ID>"
    },
    "command": "pytest",
    "cwd": "/workspace",
    "process_status": "EXITED",
    "exit_code": 1,
    "stdin_closed": false,
    "duration_seconds": 4.2
  }

tool_result: <完整结果的导出 Target>
next: null
```

原生观察时间与微秒查询耗时保持顶层，不能用记录的 duration_seconds 代替查询耗时。较大结果完整保存后再按 12k 交付；NEXT 补读固定查询结果，不重新查询，不刷新其中的进程状态，也不读取日志。

### 3.5 教程：JS 完整值与筛选

上例对应的 JS 完整业务值：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {"kind": "PROCESS", "process_id": "process:<进程A的ID>"},
        "command": "python worker.py",
        "cwd": "/workspace",
        "process_status": "RUNNING",
        "exit_code": null,
        "stdin_closed": false,
        "duration_seconds": 12.3
      }
    },
    {
      "type": "object",
      "value": {
        "target": {"kind": "PROCESS", "process_id": "process:<进程B的ID>"},
        "command": "pytest",
        "cwd": "/workspace",
        "process_status": "EXITED",
        "exit_code": 1,
        "stdin_closed": false,
        "duration_seconds": 4.2
      }
    }
  ]
}
```

JS 不套用原生观察外壳、tool_result、next 或 12k 页预算。筛选运行中的记录：

```javascript
const result = await tools.query_processes({});

const running = result.content
  .filter(part => part.type === "object")
  .map(part => part.value)
  .filter(process => process.process_status === "RUNNING");

for (const process of running) {
  console.log(process.command, process.target);
}
```

需要把完整查询材料交给 ROOT 时，显式使用 console.log({content: result.content}) 或同形 return；查询及元数据里的 PROCESS Target 不自动触发日志观察。

### 3.6 教程：查询单个进程

原生调用：

```text
工具：query_processes
参数：
  target: {"kind":"PROCESS","process_id":"process:<进程A的ID>"}
```

假设取得的进程事实与前例相同，完整业务值为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {"kind": "PROCESS", "process_id": "process:<进程A的ID>"},
        "command": "python worker.py",
        "cwd": "/workspace",
        "process_status": "RUNNING",
        "exit_code": null,
        "stdin_closed": false,
        "duration_seconds": 12.3
      }
    }
  ]
}
```

原生沿第 3.4 节同一规则投影这一条记录，附加顶层观察信息、tool_result 和 next。不会改成 process 单对象，也不新增顶层进程 target。

JS 单查仍使用相同访问路径：

```javascript
const result = await tools.query_processes({
  target: {kind: "PROCESS", process_id: "process:<进程A的ID>"}
});

for (const part of result.content) {
  if (part.type === "object") {
    console.log(part.value.command, part.value.stdin_closed);
  }
}
console.log({content: result.content});
```

### 3.7 列表无记录、单查未找到与其他错误

两种无记录情况必须分别表达，均用一个 text 项，不返回空 content:[]，不生成伪进程记录：

| 情况 | meta.status | content 中的文字 |
| --- | --- | --- |
| 省略 target 的列表查询正常完成，没有记录 | SUCCESS | 没有可查询的进程。 |
| 查询指定 PROCESS Target，未找到可查询记录 | ERROR | 未找到可查询的进程 process:<进程ID>。 |

列表无记录的完整业务值：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "没有可查询的进程。"}
  ]
}
```

单查未找到的完整失败业务值：

```json
{
  "meta": {"status": "ERROR"},
  "content": [
    {"type": "text", "value": "未找到可查询的进程 process:<进程ID>。"}
  ]
}
```

单查说明必须带请求的进程 ID，不能误用列表为空的说明，也不能无依据断言目标在操作系统中不存在。权限、执行或其他实际失败同样为 ERROR，content 说明实际原因及涉及的请求对象，不伪造 command、cwd、进程状态、退出码或运行时长。

本次查询取消使用 CANCELLED 和简短取消说明，不终止目标进程、不推进观察位置。JS 沿异常通道通过 err.result 提供实际存在的完整 QueryProcessesFailure；参数或运行时失败没有业务结果时不伪造 result。列表为空正常返回；单查未找到进入失败通道，两者不能只换一句文字却保持相同状态。

```javascript
try {
  const result = await tools.query_processes({
    target: {kind: "PROCESS", process_id: "process:<进程ID>"}
  });
  console.log({content: result.content});
} catch (err) {
  if (err.result) {
    console.log({content: err.result.content});
  } else {
    throw err;
  }
}
```

不增加 meta.error、错误枚举、_hint、独立 message 或重复身份字段。原生已完整保存的失败值仍正常分页；next:null 只表示该结果交付完成，不把 ERROR 改成 SUCCESS。保存、交付失败沿统一草案说明，不自动重做查询或控制进程。

### 3.8 实施核对点

以下是采用本合同后的行为验证要求，本轮未实施或运行验证：

- 参数只有可选 target，不接受 null；删除 include_running、include_finished，不新增筛选参数、业务页长或公开游标。
- 底层 owner 与物理调度共享，模型查询及控制按 session_id 隔离；ROOT 与 subagent session 等价，即使知道其他 session 的 PROCESS Target 也不可访问。列表只返回本 session 保留记录，按启动时间从早到晚排列，回收遵循第 1.8 节。
- 单查与列表共用相同 object 记录与 content 访问路径，无 process／processes 分支包装、顶层 target 或重复 Target 清单。
- 每条记录保留 command、cwd、RUNNING／EXITED、实际 exit_code、stdin_closed、duration_seconds；非零退出不改变查询调用的 SUCCESS。
- object 的完整 value 连同 target 一起投影为两空格缩进 JSON，不特判字段；JS 取得完整类型化记录，原生沿统一 12k 分页，不新增进程内容类型。
- 列表无记录为 SUCCESS 和“没有可查询的进程。”；单查未找到为 ERROR，说明带请求进程 ID；两者都返回 text 项，不返回空 content。
- 失败及取消只使用 meta.status 和文字 content；JS 仅在实际有完整失败值时提供 err.result，不伪造进程属性或添加错误字段矩阵。
- 元数据查询、结果分页、导出及 JS 主动输出均不读取日志、不推进进程观察位置、不消费或释放待观察暂存；后续日志读取继续由 manage_process 的 wait／write 承担。

## 4. 当前生产实现参考

- [终端工具当前参数及说明](../src/pulsara_agent/ports/terminal.py)
- [终端结果当前包装与投影](../src/pulsara_agent/conversation_kernel/tool_runtime_terminal.py)
- [进程执行与日志观察当前 owner](../src/pulsara_agent/terminal_process/manager.py)
- [Code Mode 当前调用入口](../src/pulsara_agent/conversation_kernel/tool_execution.py)
- [当前原生工具结果投影](../src/pulsara_agent/primitives/tool_result_projection.py)

这些链接用于核对实施接点，不表示当前代码已经符合本草案。后续 bundled skill 采用教程前，必须与已实施的参数、返回声明和执行值一起更新；不得先让模型按未实施接口调用工具。
