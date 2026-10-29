# Pulsara Code Mode：ROOT＋bypass、工具子集与执行合同

日期：2026-10-29。状态：本轮运行时已实施；实际验收与边界见 §10，暂缓表面仍按配套范围执行。

提示词、profile 文案与前端暂缓范围，以及 bundled Skill 和 Code Mode 工具 schema 要求，见 [暂缓事项与实施范围](PULSARA_AGENT_CODE_MODE_DEFERRED_SURFACES.zh.md)。

本文负责 JS 接入、工具范围、结果、资源与生命周期。协作对象、工具业务语义、Subagent 能力范围与恢复方式以 [Agent／Work 设计](PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md) 为准；本文定义它们的 JS 接入，不另设业务合同。两篇共同构成实施基线，工具业务实现只有一套。

当前工具名、查询分支与 CELL 身份以 [工具查询、命名与 Cell 身份统一规格](PULSARA_TOOL_QUERY_AND_CELL_ID_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) 为准。本文接口表与示例采用该合同；所链接的既往实验与实施记录保留其原验证范围，不作为新接口已验证的证据。

## 1. 产品范围与所有权

Code mode 让 ROOT 用 JavaScript 组合已有工具、处理结果和编排工作。简单调用仍可直接使用原生工具。

| 调用方与本次有效模式 | 原生工具 | Code mode |
|---|---|---|
| ROOT，bypass-permissions | 按共同目录与可用性提供 | 提供本文子集 |
| ROOT，其他模式 | 按原有规则提供 | 不提供 |
| Subagent | 按其原生范围提供 | 不提供 |

权限按 turn 固定。cell 从已接纳 exec_cell 冻结 ROOT 身份、权限、模型配置、工作区和父上下文基准；后来设置变化、新 turn 或压缩不更换这些事实。新 exec_cell 使用它自己所属 turn 的快照。实际工具可用性在每次调用时检查。

**脚本持续执行。exec_cell／manage_cell.wait 的超时只结束这次观察；manage_cell.wait 不恢复或重新执行代码。压缩只整理模型上下文，不暂停 JS。**

| Owner | 责任 |
|---|---|
| QuickJS SDK | JS 语义、Promise、微任务、内存／栈限制、interrupt 回调和 handle 生命周期 |
| 每 cell 独立 Node Worker | VM、计时、值转换、工具 Promise 与正式输出，不执行 Pulsara 业务 |
| 本地 Node sidecar | Worker 隔离、活动计算段的外部 watchdog、有限 IPC 转发 |
| Python HostSession | cell 的进程内接纳／控制、JS bridge、输出与关闭；工具调用交既有共同执行链 |
| 共同请求／turn 引擎 | ROOT 与 Subagent 的接纳、排队、执行、历史、压缩、final 和取消；WORK 直接引用已接纳请求 |
| 现有工具／MCP／artifact owner | 业务执行、权限、Hook、容量、取消、结果及已发生副作用的结算 |

固定使用 `quickjs-wasi@3.6.2`。Node 是宿主实现，不向 guest 开放 Node 模块、文件系统、网络、进程、任意 npm/import 或定时器。guest 有标准 ECMAScript、Promise、`tools` 和 `console.log`。源码是普通 JavaScript，不引入 TypeScript 编译链或独立 workflow DSL。

JS 只增加程序执行和结果桥接。派活进入接收 session 的共同请求队列；`work_id` 就是该请求 ID，状态从请求队列及其关联 turn 读取，final 从 turn 对应的 entry 读取。未启动请求没有 turn。cell 不另存工作状态、结果或调度队列；脚本结束不改变已接纳请求的生命周期。

## 2. 工具范围与一致性

### 2.1 原生与 JS 共有

| 类别 | 工具名（JS 加 `tools.`） |
|---|---|
| 文件 | `read_file`、`write_file`、`edit_file`、`find_files`、`search_content` |
| 进程 | `exec_process`、`query_processes`、`manage_process` |
| Artifact | `read_artifact`、`export_artifact` |
| 会话内容 | `search_sessions`、`search_session_content`、`read_session_content` |
| 能力查询 | `query_capabilities` |
| MCP | `use_new_mcp_tool`、`read_mcp_resource`、`get_mcp_prompt`；cell 创建时可用的直接 MCP 工具 |
| Agent | `run_agent`、`query_agents`、`send_agent_message`、`wait_agent`、`stop_agent` |

这是 17 个现有普通内置工具、5 个 Agent 工具，加动态 MCP 直接入口。Agent 已按配套文档 hard cut；当前接口及实际验收见 §10。

同名操作共用参数、target、默认值、校验、Hook、业务结果与执行 owner。原生把结果呈现给模型，JS 得到同一个公共业务值；不加通用 `.data` 封套：

```javascript
const a = await tools.run_agent({ instruction: "Inspect the parser." });
console.log({ agent: a.agent, work: a.work });
```

工具定义维护一份。provider schema 与 JS 代理同源，严格本地校验保留；第三方 provider 对 JSON Schema 的实现程度不构成参数正确性保证。模型已经能看到的原生工具定义不再完整复制进 SYSTEM；环境说明只列范围、调用方式和 JS 扩展，普通工具组合与复杂编排示例放 `pulsara-code-mode` bundled Skill。

不能作标识符的名称使用 `tools[toolName](args)`。这仍只访问该 cell 目录，不是绕过子集的通用入口。缓存函数只缓存名字／签名，不缓存权限、连接或执行 lease。

### 2.2 仅原生

| 工具 | 定位 |
|---|---|
| `enter_plan`、`ask_plan_question`、`exit_plan` | runner 接管对话阶段与用户交互 |
| `exec_cell`、`query_cells`、`manage_cell` | 从脚本外控制 cell，仅 ROOT＋bypass |
| `view_image` | 通过后续 user message 给模型看图 |
| `open_in_pulsara` | 为用户打开文件、网页或面板 |
| `todo` | 模型直接维护当前进度 |
| `search_memory`、`get_memory`、`remember`、`mark_memory_relation` | 四项均仅供 ROOT |
| `manage_capability`、`reload_capabilities` | ROOT 直接管理配置与刷新 |
| `query_schedules`、`manage_schedule` | ROOT 直接管理未来工作 |
| `query_monitors`、`manage_monitor` | ROOT 或 Subagent 原生查询与管理监控 |

“仅原生”不扩大工具权限。Subagent 的 monitor 绑定注册时当前 WORK，细节见配套文档。Subagent 不获得 JS 或自动记忆召回；已有历史不因工具范围变化而改写。

### 2.3 仅 JS

| API | 合同 |
|---|---|
| `tools.run_agent.batch(specs)` | 准备 AGENT 与请求 ID（WORK 引用），生成参数，在共同事务接纳整批请求及依赖后调度 |
| `tools.wait_agent.works({targets, settle?})` | 观察指定请求／turn 的终态；非空 WORK 集合默认 all，也可 first，无消息唤醒、无观察超时 |
| `console.log(...values)` | 按顺序交给 ROOT 的正式输出 |

原生 `wait_agent` 与 JS `tools.wait_agent` 只等 AGENT；Subagent 使用原生入口。JS 扩展均由 ROOT 调用。Promise.all、条件与循环是语言能力，不增加工具。

## 3. 生命周期 API

| 工具 | 参数 | 作用 |
|---|---|---|
| `exec_cell` | `code`／`path` 恰一；可选 `yield_time_ms` | 接纳 cell，观察新输出与状态 |
| `query_cells` | 无 target：可选 `cursor`、`max_items`；CELL target：可选 `offset_chars`、`max_chars` | 列存活 Cell，或读取状态与冻结源码；不消费执行输出 |
| `manage_cell` | wait：`action:"wait"`、CELL target，可选 `yield_time_ms`；stop：`action:"stop"`、CELL target | 等待尚未交付输出，或停止后续 JS／工具提交 |

Cell target 为 `{"kind":"CELL","cell_id":"cell:…"}`，沿统一身份 owner 生成完整 canonical ID，不另建身份映射。公共结果只携带 target，内部 IPC 与运行时索引继续使用 `cell_id` 字段。

`yield_time_ms` 默认 1,000，范围 0–30,000，单位毫秒；0 立即查询。这是一次观察的调度粒度，不是脚本、工具、Work 或会话寿命。接纳结果等宿主开销不承诺精确截止；重复 wait 没有总次数限制。

code／path 内容作为异步函数体，可顶层 await。path 由既有 Python 文件 owner 解析，读取一次后冻结源码；之后编辑文件不改变 live cell。必要的实际源码关联源 exec_cell attempt 的 CanonicalContent，沿现有 inline／blob 与 retention 读取，不加代码 hash、重放记录。Hook 改写参数后重新校验、读取与冻结。

每次 exec_cell 有独立 VM，无共享全局变量或隐式 REPL，源码只求值一次。非 undefined 的 return 作为最后一条正式结果输出；undefined 不产生内容记录。正常完成还须结算已提交工具并排空后续微任务；不能因作者漏写 await 就丢掉已发操作。未捕获错误禁止新提交，在途已接纳调用照实际结算。

Promise 拒绝使用 SDK onUnhandledRejection 追踪，在完整微任务排空后仍无处理者才判 failed；同段随后加上的 catch 不误杀。root 拒绝单独处理；root 已 fulfilled 也不能掩盖未 await 工具的未处理失败。追踪只保留仍待判断的活 Promise，以公开 identity 比较并明确释放 dup handles，不建历史拒绝集合。

```json
{
  "target": {"kind":"CELL","cell_id":"cell:aaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "state": "running",
  "output": [{"kind":"log","text":"Started the investigation."}],
  "has_more": false,
  "pending_calls": 1
}
```

state 为 `queued`、`running`、`completed`、`failed`、`terminated`。queued 表示等待 VM 容量、源码尚未执行。has_more 只表示已有输出待读；pending_calls 表示尚待结算的内部调用。terminated 证明脚本停止，不证明已派发作用撤销。接纳前失败没有 live cell，按普通工具错误返回。

VM 不再执行后即可释放；待读输出与在途结算由 Host 接管。终态、结算完成且输出消费完毕后释放进程内记录；旧 Host／已释放句柄返回明确 unavailable，不恢复执行。同 cell 只有一个输出消费 owner，同时读取返回 busy，但不妨碍 manage_cell.stop。外层结果 canonical 接纳确认后才推进读取位置；不确定接纳沿现有精确确认，不重跑脚本或提前丢页。

## 4. 计算、等待与资源

### 4.1 连续计算段

SDK 提供 interruptHandler，没有自动扣除异步等待的 CPU 预算。Pulsara 用小适配包住每次最外层 VM 驱动：初始 eval，或工具结果 resolve/reject、callFunction 及后续 executePendingJobs 的整段。同步重入不重复计时，微任务之间不清零。

真正返回宿主、等待外部工具时不计时。下次外部完成开始新段；不把短段累加成 cell 总 CPU 寿命。`await Promise.resolve()` 不能重置预算。中断一旦触发就结束 cell，不重新使用失败 VM。

计量是 VM 同步占用的单调时间，含桥接／同步宿主回调，不是精确 CPU 计费。已测 while、无限微任务与重入可中断；编译、JSON.parse 或同步宿主阻塞未必及时调用 interrupt。因此 VM 位于独立 Worker，sidecar 对同一活动段设置更宽的外部 watchdog，能终止卡住的 Worker；异步等待期间不运行这个 timer。

Worker 进入段前公布活动序号与开始状态，离开后清除；过时 timer 不能杀下一段。只需进程内共享状态槽，不需要持久心跳／租约。段返回时也检查经过时间，避免未触发 callback 的长内建操作被误报成功。manage_cell.stop 从外部控制，不排队到死循环之后。

### 4.2 初始物理配置

这些是可配置的首次接入 profile，不是 SDK 的普适保证。仅约束连续执行或当前占用，不限制累计工作量。

| 边界 | 初始值 | 目的／超界行为 |
|---|---:|---|
| 连续 VM 段 | 5 秒 interrupt，10 秒外部 watchdog | 错误脚本不能长期占线程；异步工具等待不计入 |
| 每 VM guest heap／栈 | 64 MiB／256 KiB | 使用 SDK memoryLimit／maxStackSize；明确 OOM／栈溢出 |
| 单次源码或 bridge JSON 值 | 4 MiB UTF-8 | 限制 Python／Node／WASM 单次复制；超大工具响应保留真实结果／已有 artifact，再报告传输资源错误 |
| 每 cell 未交付／待转发数据 | 16 MiB；每 Host 合计 64 MiB | 限制 IPC、排队参数及输出 collector 当前占用；释放已消费数据，不在另一数组藏无界副本 |
| 同 Host 存活 VM | 4 | 限制 WASM 实例与线程占用；额外工作排队 |

等待中的 VM 仍占内存槽，不占计算预算、普通工具槽或 Agent 模型槽。VM 容量与 Subagent 并发独立，一个 cell 可编排许多 Agent。排队源码也计当前数据预算；真实资源不足明确返回边界，不伪造已接纳／已执行。

数据计量包含编码后的字段、记录边界和排队条目开销，不只计算非空正文。4 MiB 单值小于 64 MiB guest heap，给解码对象与跨边界副本留出空间；16／64 MiB 分别约束局部和聚合存量。它们是本稿提出的初始工程取值，尚未完成生产并发内存标定；SDK 实测只覆盖 §10.2 列出的负载。启动时验证配置自洽，失败如实报告，不用临时常数改写总寿命。

输出满不是脚本暂停窗口。同步 console.log 无法接纳下一条时明确失败，已接纳记录保留；可存档的大记录沿既有 artifact 读取。有限错误控制信息保留固定空间，不能因输出满无声消失。不建透明磁盘日志或重启恢复系统。

SDK handle 明确释放。3.6.2 单侧 newPromise resolver 留存已实测，采用持有 promise／resolve／reject 三 handle 的小 deferred 工厂，结算后统一 dispose，不复制 Promise runtime。所有进入 VM 的路径均经计时 adapter，不漏 host Promise 自动转换触发的 jobs。

本轮正常约 1.5 MB JSON 筛选排序最长段约 44 ms；100 个并发模拟工具 Promise 最长段约 1 ms。5 秒初值给小型编排留出余量。集成仍须测实际机器占用、IPC 与聚合额度；不能宣称所有合法脚本都小于这些值。

## 5. 真实调用来源与共同执行

### 5.1 来源与持久内容

生产 decision／attempt 的请求归属要求真实 assistant TOOL_CALL 和 RUNNING turn；结果接纳已经支持 turn 结束后的真实结算。Code mode 需要补足程序调用来源，继续使用共同执行与结果链，不伪造模型输出。

最小 canonical `tool_invocations` 关系只保存调用来源，供现有 decision／attempt／result／Hook 引用：

| 来源 | 唯一事实 | 接纳条件 |
|---|---|---|
| MODEL_TOOL_CALL | 指真实 assistant entry／tool call，名称与原参数从该块读 | 保持 RUNNING turn 与现有身份／权限条件 |
| CODE_MODE_CALL | 源 exec_cell attempt＋cell 内调用序号、工具名与原参数 | 当前 Host 的存活 cell owner 接纳，使用源 exec_cell 快照 |

MODEL 不复制原块中的名称和参数。CODE 唯一性为源 exec_cell attempt＋子调用序号，在副作用前固定真实来源；来源行随第一次共同 decision／attempt／result 接纳事务建立，不留下需要另行修复的空调用对象。来源不保存运行状态、队列、结果正文、交付标志或恢复信息。当前 writer 与存活 cell owner 提供执行权，旧 ID、数据库中的 exec_cell 或导入历史本身不授权执行。

参数、Hook 或权限拒绝也有真实调用与结果，但没有物理 attempt；因此不能仅给 attempt 加一个 CODE 字段，或伪造一次 attempt 来承载来源。这份来源关系解决归属，不承担另一套工具生命周期。

两个 JS 扩展有封闭的内部 descriptor：`run_agent.batch` 接收按局部 key 索引的最终具体参数，`wait_agent.works` 接收 WORK 数组与 settle。它们只接收 CODE 来源，不进入 provider tools，不将函数持久化，也不把 WORK 参数送给普通 AGENT 等待校验。descriptor 复用底层参数类型与业务 owner，不推广成全工具 channel 注册体系。

batch 只有一个真实 invocation。逐项 PreTool／PostTool 使用 run_agent matcher、该项普通参数及“batch invocation＋member key”的因果身份；不额外跑一遍批量 Hook，不伪造多个模型调用。最终具体参数与委派请求来源都能定位到对应 member key。每项目标身份必须仍等于预分配引用；Hook 改变身份则整批在接纳前拒绝，不重跑参数函数或留下错指的队友。成功提交后 PostTool 出错只附真实诊断，不把已接纳请求说成未执行。

两种来源的结果都写入现有 `tool_results` 和真实 `TOOL_RESULT` entry。正文、blob、artifact、coverage、状态、耗时、接受与精确确认使用同一套 owner；结果 entry 继续承载已有结果事件和保留关系。结果归于源调用的 turn，turn 已结束不阻止实际结算。JS 获得本次公共业务值不需要另建结果库、空 entry 分支或专用正文 resolver。

调用来源决定投影：MODEL 结果进入真实模型 call/result 历史；CODE 结果交付 JS，UI 在父 exec_cell 下展示，只有 console、return 和本文规定的未交付结果输出经 exec_cell／wait 进入模型。共享 canonical 存储不等于把全部内部结果交给模型。

provider reader、预算／headroom、原生结果配对与迟到观察、压缩来源、模型会话读取／搜索、历史召回及 fork 共用这条来源选择，先过滤再分页和报价。CODE 不能变成孤立 provider tool message、原生未完成调用或模型已读材料。UI、诊断、artifact／blob 保留和未结算调用检查仍包含真实 CODE 记录；UI 使用已有工具卡片与内容读取，只按来源关联到父 exec_cell，不重复显示为对话气泡。

cell 仍进程内；不新增持久 cell 调度或重启队列。现有 event／subject／guard／job 类别保持，调用来源引用及约束 hard cut。Plan 只接受 MODEL 来源，统一归属不扩大其调用范围。

### 5.2 共同执行与并发

每次调用：最终参数校验→PreTool→共同权限／可用性→attempt 接纳→物理执行→真实结果与必要副作用结算→PostTool→交付 JS。外层 exec_cell 与内层业务各有真实 Hook 身份；内层自身 Hook 不重复跑。ROOT cell 的 Hook 上下文走正常 ROOT 输入点，不扩大权限；用户配置的 ROOT／Subagent 适用范围与真实输入来源遵循配套设计 §1.5。

bypass 不吞掉 Hook 拒绝、策略故障或真实不可用。当前策略异常可能退为 REQUIRE_CONFIRMATION；CODE 来源遇到这种无法获得授权的情况，返回明确的未派发 ToolError，ROOT 可通过原生入口处理，不把后台 cell 接到新的用户确认窗口。原生既有确认行为保持；远端 MCP 自身的 input-required／失败按实际业务结果交付。

将现有 ToolBatchExecutor 的单 active-batch 槽改为逐调用的进程内执行／取消上下文，原生批次与 JS 进入同一叶子执行路径、Hook dispatcher 和结果结算 owner。原生批次维持原顺序。JS 的 Promise.all 提交并发请求：接纳／PreTool 按提交顺序处理，物理操作在各 owner 容量／资源协调下并行；PostTool 随实际完成的结算顺序处理，共同 context 写入串行，Promise 按实际结算完成。不能让一个长等待卡住别的已完成结果，也不要求 PostTool 等待尚未结束的早期调用。

同 PROCESS 的 stdin／kill／close 要由 terminal owner 裁决；文件操作的观察、写入和相同资源竞争在共同文件 owner 处理。不能用覆盖整个 await 的 Host 大锁，也不另建“工具纯度”注册表。需要先后关系的脚本使用 await，Promise.all 不承诺相互依赖的写操作按数组顺序完成。

当前 read_file／edit_file／write_file 确有 workspace 内的同路径锁；export_artifact 的 atomic no-clobber、terminal／外部程序写入不在这把锁内，不能把它说成跨进程文件事务。edit 继续保留 revision 与实际文件复核。

文件观察复用现有 workspace 状态，支持原生 read→JS edit、JS read→原生 edit。当前 read_file 用原生 40,000-byte 完整展示界限决定可观察行；JS 改为以本次完整业务值实际交付为准，仍遵守原有读行／字符边界。交付成功、该工具 Promise continuation 运行前，文件 owner 在同路径锁内复验读取 revision，匹配才合并真实范围；迟到旧 revision 不覆盖更新观察。锁不跨 await，其他 JS 不需暂停。传输失败／只有预览不扩大观察；不伪造 provider 已读，不另建 cell 观察权限库。原生路径及 replace_file 既有 revision 规则保持。

持续 WORK／消息等待仅登记共同请求／turn 与收件状态的进程内观察，不占普通工具槽、数据库事务或前置 Hook 阶段。batch 使用从 ROOT 推广的共同请求接纳 owner，后续由共同队列调度，不依赖 JS 存活；细节见配套文档。

### 5.3 压缩与资源解耦

cell 保留调用签名和来源，不借用旧 provider 前缀。每次调用从真实 capability owner 取得当前可用 binding；接纳后只持该次物理执行所需 executor／MCP permit，结算后释放。

具体需要拆开 `KernelToolInvocationContext.surface_borrow` 和 PostTool 对 current_view.epoch 的强依赖：物理执行资源不是 provider prefix lease；Hook 因果由真实 invocation／result 提供，ROOT final 后无 current view 不能导致工具结算失败。

ROOT 可以独立压缩。JS 结果交付不安装 provider input；正式外层输出和工具自有的模型通知才进入正常 suffix。SYSTEM／tools 仅在 cold 或 adopted successor 边界重建，后台进展不改写已安装前缀。

## 6. MCP 的查询与调用

cell 创建时冻结 JS 目录的名称和精确签名，包括当时直接 MCP。每次调用复核当前 server、schema、policy 和 executor。新工具／不匹配原签名的工具 inspect 后走 META，不让 cell 目录或 provider tools 原地长大。

当前 META ref 只有完整 inspect schema 进入 provider 后才激活，需在同一个进程内 ref owner 区分 MODEL_EPOCH 与 CODE_CELL 消费者：

- 原生仍由模型实际获得完整 schema 后激活。
- JS 在完整结果成功交给该 cell 时激活；交付失败不激活。
- JS DIRECT／META 以 cell 固定直接签名为基准；当前事实取 Host MCP owner，不依赖活动 provider view。
- ref 绑定真实 Host、会话、调用方、工具事实和消费者。纯 ROOT compaction／cold 不销毁 cell ref；generation、schema／policy、范围或可用性实际改变时拒绝未派发调用，必要时重 inspect。
- cell 结束释放 ref；已接纳物理调用沿自己的 permit 结算。

不新增持久 token、连接管理、旧接口别名或自动副作用重试。响应丢失不能当作未执行。

当前 MCP 的 image／audio 块作为 JSON 业务数据返回，保留其真实 data／引用；本轮保持该语义，不自动追加图片 user message。给模型看图仍用原生 view_image，不顺带扩张图片／音频协议。

## 7. 业务结果、异常与输出

### 7.1 JS 值

成功 resolve 公共 JSON 值或文本字符串。来源 owner 明确 JSON／TEXT 类型，同一 typed 公共结果生成原生正文和 JS 值；不按“看起来能 JSON.parse”猜类型，也不维持两套业务 DTO。

“完整”是工具本次约定范围内、未经模型展示预算剪裁的响应，不是完整文件／进程全历史。原有分页、truncated、cursor、gap 保持真实含义。

exec_process／manage_process 已有有限 tail 和独立完整范围 artifact，后者目前只在模型 footer。共同公共响应增加可选 `output_artifact`，给真实 ARTIFACT target、disposition、source coverage；原生和 JS 一起用，不覆写原 output。示意：

```json
{
  "output": "selected tail…",
  "truncated": true,
  "output_artifact": {
    "disposition": "AVAILABLE",
    "target": {"kind":"ARTIFACT","artifact_id":"returned-id"},
    "source_coverage": "COMPLETE"
  }
}
```

只有实际可读归档才给 target；retention gap、不可用 disposition／reason 保留。COMPLETE 指所选范围完整，不表示命令启动以来全部输出。

工具失败 reject ToolError，含 code、message、可用公共失败 result 与 artifact 入口；Promise.all／allSettled 遵循标准语义。查询 `stopped` Work 本身是成功的查询，不因目标工作停止而 reject。业务已执行后才发生传输资源错误，要保留真实效果与结果入口，不误报未执行。

参数跨 bridge 是严格 JSON：拒绝 undefined、非有限数、BigInt、函数、Symbol、循环或非 JSON 对象，不借 stringify 静默丢字段。JSON 整数超 JS 安全整数范围明确报错，结果侧提供原始文本／已有 artifact，不暗中舍入或改字符串。文本中的数字仍原样。

### 7.2 console／return

一次 console.log 一条有序记录。字符串原样，其他参数标准 JSON.stringify，顶层 undefined 显示 undefined，多参数空格连接；BigInt、循环等不能序列化的值报错。Error 的 message／stack 由脚本显式选择。getter／toJSON 在受计时的 guest 段执行。

console.log 同步接纳正式输出，不采样、不静默丢弃，也不代表模型已阅读。非 undefined return 用同一规则，在正常完成时排在所有日志后；cell 已失败不再发出成功 return 记录。未捕获异常保留 name／message／可用 stack，ToolError 另保留真实失败状态及结果入口，不能按普通 Error 的 JSON.stringify 得到空对象后冒充完整诊断。此前输出保留，行列取冻结源码与 SDK 实际信息，不能可靠给出的不猜。

exec_cell／wait 按序交付容纳得下的完整记录前缀，剩余用 has_more＋后续 wait 读取。单条过大走现有 artifact；失败明确报告。整页控制字段与已选记录经现有 FULL_REQUIRED／资源报价保持完整，不在接受后又降成截断预览。沿用现有 40,000-byte 模型工具结果边界及上下文预算，不另增 provider 上限。

各 cell 内有序，跨 cell 按各自读取，不虚构全局时间。内部结果不全灌模型，由 console／return 选择。工具自有 Hook、Agent 消息与完成投递保留自身通道，不被吞掉，也不新加阅读回执。

cell 已 failed／terminated，而在途结果尚未成功 resolve/reject 给 JS 时，真实结算不能只令 pending_calls 减一：exec_cell／wait 的正式输出追加 `kind:"tool_result"` 记录，给原子调用序号、tool_name、真实结果状态及公共结果／已有 artifact。按实际结算顺序从 accepted result 惰性读取，展示为 preview 时保留 coverage／读取入口，不复制新日志或制造 provider tool message。VM 已释放仍可 wait 这些结算；轻量记录在 pending_calls 清零且输出消费完成后释放。已经交给 JS、只是脚本未打印的普通结果不自动输出。这里判断的是实际 Promise 交付，不是模型或脚本阅读回执。

## 8. 生命周期与控制

| 触发 | cell | 已接纳工作／作用 |
|---|---|---|
| exec_cell／wait 观察超时 | 持续运行 | 持续运行 |
| 压缩、普通新消息／steer、ROOT final | 持续运行，新消息由 ROOT 处理 | 保留原执行 |
| 新 turn、权限变化、进入 Plan | 旧 cell 仍沿源 exec_cell 快照 | 后台工作不被改成新 turn 权限 |
| 用户 Stop 当前 ROOT turn | 终止该 turn 创建的 cell；仅被该轮 wait 的旧 cell 不受影响 | 在途作用按 owner 取消／结算，已派 Work 不自动取消 |
| manage_cell.stop／用户精确停止 cell | 停后续 JS，取消观察与未派发提交 | 不等于 stop_agent 或杀已转交终端 owner 的进程 |
| JS 异常、OOM、计算超界、Worker 故障 | failed，不自动重新求值 | 已接纳作用真实结算 |
| HostSession／工作区关闭 | 停接纳、终止全部所属 cell，再释放资源 | 在途调用按共同 owner 取消／结算；活动 turn 中断，未启动请求沿共同队列保留 |
| 宿主崩溃 | VM 丢失，旧句柄不可继续 | 共同 Host 接管如实结算中断；不重放已运行 turn／工具，未启动请求按队列语义处理 |

普通 steer 仍可取消前台原生批次尚未 dispatch 的步骤；独立后台 cell 不继承该前台 batch 的取消信号。新消息不是终止脚本；明确停止走上表。

停止和子调用接纳在同一个 cell owner 原子裁决：停止先赢则未派发；调用先接纳则按实际能力结算。释放 VM 不能抛弃线程／远端正在完成的结果。

UI 提供 cell 状态、输出和精确停止，复用 conversation_kernel/user_control.py 的进程内精确目标控制 owner，不增加持久命令日志。当前新 turn 非 bypass 时，模型仍无 code mode，但用户能查看／停止旧 cell。Plan 约束本轮，不表示先前后台工作已停止；呈现真实状态，不另加确认流程。

完成不自动创建 ROOT turn。正常输入准备及压缩交接提供简短存活／未读 cell 索引（引用、状态、是否有待读输出），不重复日志。Host 的空闲／archive／reopen 判断纳入运行及结算 cell，不能因 ROOT final 误关 owner。

没有总运行时长、总调用次数或历史量上限。根 Promise 未完成且 guest jobs、宿主请求都为空时，在这个没有 timer／外部事件的环境中已无进展来源，返回 stalled；真实长工具等待不满足此条件。执行只在进程内连续，不承诺跨重启恢复。

## 9. 调用例子

```javascript
const { research, review } = await tools.run_agent.batch({
  research: {
    name: "Researcher",
    instruction: "Investigate the implementation and report a concrete proposal."
  },
  review: {
    name: "Reviewer",
    depends_on: ({ research }) => [research.work],
    instruction: "Review the prerequisite proposal and report defects and tradeoffs."
  }
});
console.log({ research, review });
console.log(await tools.wait_agent.works({ targets: [research.work, review.work] }));
```

exec_cell 在 1 秒后返回 running，ROOT 随后压缩，research／review 仍按依赖执行；Promise 满足后第二条日志自然产生。ROOT 后来 manage_cell.wait 取回它，不会再派活。若先 manage_cell.stop，已经接纳的 Work 仍独立运行，可用原生 inspect／wait／stop 管理。

单 Agent、初始队友引用以及调查→审查→修订→实现→原 critic 代码审查的完整例子见配套文档。

## 10. 实施、证据与验收

### 10.1 同步落点

| 位置 | 必须落实 |
|---|---|
| catalogue／surface | ROOT＋bypass 子集、原生保留、同源 SDK 代理 |
| tool_contracts／execution／repository／clean-v0 | 最小调用来源；共用 decision／attempt／TOOL_RESULT entry 与逐调用结算，不另建 JS 结果或执行状态 |
| 共同请求／turn／协作入口 | WORK 直接引用已接纳请求；原生与 JS 派活共用请求队列、turn 与 final 链路，按目标 session 调度；依赖与 batch 原子接纳遵循配套设计 |
| ToolOutput／artifact／projection | typed 值、终端 artifact 入口、完整输出页，不以 preview 冒充实际值 |
| MCP ref／runtime | 双消费者、当前真实 binding、完整 inspect 激活 |
| runner／compaction／Host／Hook | 持续 cell、实际 Hook 来源、状态索引、准确 stop／close／reopen |
| Node／QuickJS 打包 | 可复现 Node／WASM 依赖、隔离、计时、handle、有限 IPC；运行时不联网下载依赖 |
| reader／history／fork／UI／用户控制 | 模型来源过滤在分页／预算前完成；UI 使用同一真实结果并归组到父 exec_cell；状态、输出、精确停止复用既有 owner |
| SYSTEM／工具／bundled skill | 短环境合同＋原生 schema；复杂编排按需读取既有 skill，不写实验过程 |

本轮实施与验证总览见 [实施记录](output/agent-code-mode-implementation-20261029/integration/IMPLEMENTATION.zh.md)，逐项行为、证据层级和未覆盖边界见 [验收矩阵](output/agent-code-mode-implementation-20261029/integration/ACCEPTANCE_MATRIX.zh.md)。

### 10.2 本轮证据

- [QuickJS 初探](output/quickjs-compute-budget-20261028/REVIEW.zh.md)：排除异步等待的计时。
- [SDK 边界实测](output/code-mode-freeze-20261028/quickjs_limits/REVIEW.zh.md)：循环／微任务、编译盲区、Worker 中断、正常计算、未 await 调用及 handle。
- [实际结果实验](output/code-mode-freeze-20261028/output_contract/REVIEW.zh.md)：terminal tail／artifact、MCP JSON 图片、序列化。
- [ROOT 汇总](output/code-mode-freeze-20261028/root/REVIEW.zh.md)：专项代码证据与最终选择。
- [共同引擎复用审阅](output/code-mode-root-reuse-20261028/root/REVIEW.zh.md)：请求／turn、上下文、消息／monitor、工具结果的唯一归属与必要适配。

以上前置实验不作为生产端到端通过证据。旧 fake compaction 实验仅给模拟窗口换标签，没有执行 Pulsara 真实压缩。[SDK 官方接口](https://github.com/vercel-labs/quickjs-wasi#interrupt-handler) 用于核对公开能力，结论以固定发布版本实测为准。

### 10.3 实施验收

1. 原生与 JS 使用同一结果 entry、artifact 和业务值；CODE 过滤在分页／报价前完成，模型读取、压缩、搜索与 fork 不混入内部结果，UI 可从父 exec_cell 查看真实调用。
2. 原生批次／两个 cell／慢快调用并存，取消、Hook、结果归属不串线。
3. cell 等真实 Work 时 ROOT 完成生产压缩，结束后下一条工具只执行一次；ROOT final 后也能继续。
4. JS inspect→META 不经模型可调用；纯压缩不坏 ref，真实 server／schema 变化仍约束调用。
5. 循环、微任务、编译卡住、OOM、输出满能结束；长外部等待和多段短计算不被总寿命限制杀掉。
6. 多页／大单条输出、terminal gap、artifact 不可用、unsafe 数字与失败不静默丢失或假成功。
7. Stop、steer、Plan、manage_cell.stop、Host close、进程死亡按表工作；未 await 的已提交操作正确结算。
8. 配套 Agent 的 batch／history／message／wait／monitor 验收；WORK ID 从队列到 turn 保持不变，启动／取消只结算一个实际阶段，final 读原 entry；已接纳请求不依赖 cell 存活。

只测可观察行为，不冻结自然语言或整份 schema。本轮已用保存 provider 配置验证两种 API 的实际 prefix、调用、结果与生产压缩；真实 SDK MCP 的同 cell／ref 跨压缩也已验证。完整输入、canonical 核对与覆盖层级见 [验收矩阵](output/agent-code-mode-implementation-20261029/integration/ACCEPTANCE_MATRIX.zh.md)，不把文档例子当通过证据。

### SUPERSEDES

本文与配套文档替代实验分支 `PULSARA_CODE_MODE_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 的范围、生命周期、工具入口和结果合同。旧实验是证据，不是必须保留的设计。生产完整 hard cut，不留旧／新兼容模式。
