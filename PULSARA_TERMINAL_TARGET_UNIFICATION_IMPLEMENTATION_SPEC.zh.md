# Terminal 工具目标统一实施调研规格

日期：2026-10-28。状态：生产 hard cut 已实施，公开请求、结果及消费者采用本规格。验证与范围见[实施报告](output/target-hardcut-implementation-20261028/terminal/REPORT.zh.md)；整体结果见[汇总](output/target-hardcut-implementation-20261028/root/REPORT.zh.md)。

## 1. 决策与边界

模型持有同一 `{kind:"PROCESS",process_id}`，即可继续 poll、wait、write、submit、close_stdin、kill、register，以及通过 `open_in_pulsara.targets` 展示进程。监视注册有独立 `{kind:"MONITOR",monitor_id}`，用于 cancel。操作参数、输出游标、等待预算和监视策略留在 target 外。

保留 `terminal`、`terminal_process`、`terminal_monitor` 三工具及全部动作。启动命令尚无既有对象，`terminal` 请求不加 target；两个 list 是集合查询，也不加 target。monitor register 的 target 指被监视的进程，不指尚未生成的 monitor。

这只是公共调用与对象结果的 hard cut。进程注册表、进程组、stdin、输出 owner、monitor coordinator、权限与副作用结算继续归现有 owner。无全局 target 注册器、对象别名、额外查找工具、持久化恢复、凭据、指纹、租约或新资源上限。内部 DTO、事件、数据库列、protobuf 与 UI 原生控制仍可用 process_id/monitor_id。

明确排除 subagent 编排工具、profile、历史、权限/记忆范围与监视开放：terminal_monitor 仍 ROOT-only，SUBAGENT 不因此获得 monitor。公共 terminal 结果和压缩中的 terminal 对象使用一个新格式，不为 subagent 制造第二套格式；不改其过滤/归属条件。人类控制与桌面后台进程 API 不是本次模型参数统一的改名对象。

仅新 cold epoch 或明确采用的 compaction successor 可以安装新定义。已安装 epoch 的 SYSTEM/tools 字节不变、messages 仅追加；不引入热升级兼容或新的前缀重建边界。旧请求字段无 alias/双读。历史消息不批量改写；开发 hard cut 使用干净状态，不添加跨部署兼容。

### SUPERSEDES

本规格仅替代以下文档中的终端公共扁平身份请求、相应模型结果和调用指引：

- [本地文件、呈现与终端工具清晰化规格](archived_docs/PULSARA_LOCAL_IO_TERMINAL_TOOL_CLARITY_IMPLEMENTATION_SPEC.zh.md) 的terminal三工具及展示PROCESS身份部分。
- [终端认知减法规格](archived_docs/PULSARA_TERMINAL_MODEL_CALL_COGNITIVE_SUBTRACTION_SPEC.zh.md) 中process_id/monitor_id作为模型调用入口的表述；每次独立shell、原动作和运行语义保留。
- [Terminal公共API拆分规格](archived_docs/PULSARA_TERMINAL_PUBLIC_TOOL_API_SPLIT_HARD_CUT_IMPLEMENTATION.zh.md) 中仍适用于当前实现的身份传递部分；不恢复该历史文档已淘汰的log等动作。

其余行为以当前实现为准，不据历史方案恢复持久shell、notification reducer或执行恢复。本次已读根AGENTS.md、共同BRIEF、capability target规格、能力/定时清晰化历史规格、190次实验报告及本地工具清晰化规格。

## 2. 代码证据与适配边界

下表路径相对 `src/pulsara_agent/`，前端路径另标。

| 现有 owner | 已核对事实 / 必要实施位置 |
|---|---|
| `ports/terminal.py` | 三份英文 description；严格 Pydantic action union；默认/null/上下界；process parser 与 monitor canonical ID validator；inline schema 导出与 input contract binding |
| `capability/builtin_catalog.py:947–975`、`_ACTION_PERMISSION_OVERRIDE_SPECS`、`_catalog_shape` | 工具注册、artifact mode、权限 override、七/三动作的闭合目录。动作/分类不变 |
| `conversation_kernel/tool_runtime.py::_DirectTerminalTool.execute` | terminal input 到原生 TerminalRequest，permission mode 到 cwd scope；无需新增 target |
| 同文件 `_DirectTerminalProcessTool.execute`（约 529–606） | 七动作派发；直接从 typed target 取 process_id 给现有 manager；list 输出须单独投影 |
| 同文件 `_invoke_terminal_monitor`（约 3821–3913） | ROOT门禁；list/cancel/register；注册结果先生成 process-local settlement token，待工具结果提交后激活 |
| 同文件 `_terminal_execution_result`（约 4417）、`remote_identity`提取（约3774） | 模型结果process_id替换；内部 remote_identity 必须同步读 target.process_id，不能遗漏 |
| `terminal_process/manager.py::ProcessRegistry._owned,poll,wait,write,close_stdin,kill,list_processes`（约779–990） | 当前 Host owner 验证、ID验证、真实进程组/输入/完成边界；保留原生签名 |
| `terminal_process/output.py::_snapshot_locked,_range_locked`（约651–713） | 游标绑定 owner_epoch/process_id/stream_id/UTF-8 offset；tail窗口、gap及截断语义不变 |
| `terminal_process/monitor.py::prepare_registration,settle_registration,list_current,cancel,_evaluate,settle_installation` | 原注册/观察/取消/到期与竞态；内部 ID 和 coordinator 不必改名 |
| `model_input/lowering.py::_project_terminal_observation`（约721；调用约243/319） | 已有通知provider投影；普通lower与retained request共用，公开两个身份键换target结构 |
| `ports/terminal_observation.py::canonical_mapping`、`conversation_kernel/reader.py::_validate_terminal_observation_content` | 内部canonical及严格闭合校验保持原格式，不为公共target改存储/reader |
| `ports/user_control_feedback.py::project_user_control_feedback_for_provider` | 已有的人类停止进程通知投影；只改变该投影的身份表示即可，原native canonical不必改 |
| `conversation_kernel/compaction/runtime_handoff.py::_payload`、`tool_runtime.py::freeze_compaction_runtime_handoff` | 输出中进程/monitor目标可复制；typed frozen事实、ROOT-only monitor选择和预算降级不变 |
| `tool_permission.py::_terminal_process_input`、`tool_execution.py` Hook payload producer | write/submit 的data仍顶层，hardline判断继续；Hooks收到新的原生公共参数/结果，无反向展平 |
| `tools/builtins/presentation.py::normalize_target` | 文件/展示组负责 PROCESS 输入转原生 `{type:"terminal",process_id,host_session_id}`；Host ID由既有owner注入 |
| `frontend/lib/builtin-tool-summary.ts`、`frontend/components/workbench-view.tsx::terminalOutputText` | 摘要基于action/status/exit_code/output，与身份包装正交；覆盖新结果回归 |
| `frontend/lib/runtime-adapter.ts`、`frontend/components/background-terminal-panel.tsx`、`frontend/lib/presentation-targets.ts` | 原生后台进程及human-control protobuf路径保持；不是把所有process_id字符串替换成target |
| `bundled_skills/pulsara-plugin-installer/references/pulsara-hook-extension.md` | 当前无terminal裸ID调用样例；补充公共目标位置说明，保留匹配别名、cwd与真实输入规则 |

不复刻 shell、PTY、游标 codec、monitor scheduler 或 SDK；本次依赖边界没有变化。唯一适配是：公共 typed target → 现有原生 owner 的 ID；原生事实 → 可复制的公共 target。

## 3. 目标形状

```json
{"kind":"PROCESS","process_id":"<existing process ID>"}
{"kind":"MONITOR","monitor_id":"<existing monitor ID>"}
```

两种对象都封闭，kind固定为大写；只允许上述键。字符串保持当前 minLength=1 的 schema，不新增最大长度、格式发明或trim；process owner继续 `require_id(...,"process")` 和当前Host归属检查，monitor cancel的 typed validator继续 `require_id(...,"monitor")`。schema通过不代表目标存在、仍存活、属于调用方或已授权；此处不提前改变既有错误阶段。

PROCESS 无 host_session_id、session_id、pid、status、cursor、cwd、origin 或 owner epoch。当前 terminal environment 已由工具owner绑定；公开目标不接受调用方自行改路由。跨Host/重启的旧目标不会复活进程。open_in_pulsara 仍是当前conversation的展示请求，由已有展示归一化补内部host_session_id，不把该字段回灌成模型必填身份。

MONITOR 无 process_id；监视对象的PROCESS另放结果的 `process_target`。因此将monitor取消对象和被监视进程对象明确分开，不能把MONITOR交给kill。

不把 since_cursor 放target：同一进程多个观察游标都合法，游标是本次读取位置；错误/旧游标有自己的语义，不能改变对象身份。conditions/delivery/lifetime是本次注册策略，注册之后不变成target的一部分。

## 4. 所有请求分支与前后样例

下列P/M分别代表上节完整对象，不是生产协议支持的变量或缩写。

| 工具/动作 | 旧调用 | 新调用（除目标包装外其余原样） |
|---|---|---|
| terminal | `{"command":"printf hello"}` | 不变；不允许target |
| process list | `{"action":"list"}` | 不变；可选include_running/include_finished |
| process poll | `{"action":"poll","process_id":"p"}` | `{"action":"poll","target":{"kind":"PROCESS","process_id":"p"}}` |
| process wait | `{"action":"wait","process_id":"p","since_cursor":"c","timeout_seconds":10}` | `{"action":"wait","target":{"kind":"PROCESS","process_id":"p"},"since_cursor":"c","timeout_seconds":10}` |
| process write | `{"action":"write","process_id":"p","data":"abc","yield_time_ms":0}` | `{"action":"write","target":{"kind":"PROCESS","process_id":"p"},"data":"abc","yield_time_ms":0}` |
| process submit | `{"action":"submit","process_id":"p","data":"abc"}` | `{"action":"submit","target":{"kind":"PROCESS","process_id":"p"},"data":"abc"}` |
| process close_stdin | `{"action":"close_stdin","process_id":"p"}` | `{"action":"close_stdin","target":{"kind":"PROCESS","process_id":"p"}}` |
| process kill | `{"action":"kill","process_id":"p"}` | `{"action":"kill","target":{"kind":"PROCESS","process_id":"p"}}` |
| monitor register | `{"action":"register","process_id":"p"}` | `{"action":"register","target":{"kind":"PROCESS","process_id":"p"}}`；仍可选conditions/delivery/lifetime |
| monitor list | `{"action":"list"}` | 不变；没有其他字段 |
| monitor cancel | `{"action":"cancel","monitor_id":"m"}` | `{"action":"cancel","target":{"kind":"MONITOR","monitor_id":"m"}}` |
| open_in_pulsara | `{"targets":[{"type":"terminal","process_id":"p"}]}` | `{"targets":[{"kind":"PROCESS","process_id":"p"}]}`；文件/展示组负责完整工具 |

表中的p/m/c仅用于展示位置，不声称通过真实ID/游标校验。保存的机器样例同样只验证schema，不启动进程。

### 4.1 terminal：启动参数保持

| 字段 | 省略值与允许值 |
|---|---|
| command | 必填string，长度1–1,048,576；原生TerminalRequest仍拒绝全空白命令 |
| workdir | 可省略或null，均按workspace root；非null字符串1–4096，现行支持workspace-relative、absolute和~，必须实际存在；READ_ONLY的workspace范围门禁保持 |
| yield_time_ms | 省略10,000，整数0–30,000；0立即返回；不是命令生命周期 |
| tty | 省略false；严格bool，不接受null/数字代替 |
| max_output_chars | 省略32,000，整数512–32,000；只限返回字符 |

保持每调用新shell及独立cwd/environment。底层TerminalRequest虽有max_lifetime_seconds，本模型请求没有该字段，本次不暴露。既有进程物理容量、执行决策deadline、foreground/background副作用归属不变。不把一次初始yield超时当命令timeout，不新增总运行时长或累计调用限制。

### 4.2 terminal_process：分支与真实效果

- list：include_running/include_finished各自省略true，严格bool且不接受null；两个false返回空集合。按同一Host owner选择，按started_at_monotonic排序。当前没有模型分页/limit/cursor；本次不增加或用target限制列表。
- poll：只接收action、PROCESS target、可选since_cursor/max_output_chars；即时观察。max_output_chars省略32,000，范围512–32,000。
- wait：同poll，另可选timeout_seconds，省略30，整数1–30。等待physical_completion最多该时长，超时返回当时状态；不杀进程、不把仍running误报为完成。流式订阅在finally注销。
- since_cursor只用于poll/wait：省略和null都读当前保留输出；非null必须是该进程已有output_cursor。当前schema允许空字符串，但codec拒绝；不借包装改变这一阶段。owner epoch、process、stream、future offset不匹配时报INVALID_CURSOR；落在保留区之前则GAP并返回保留内容，不变成INVALID_CURSOR。output_cursor越过本次已观察范围，包括响应窗口省略的内容；不能把它当逐页“未读日志”游标。
- write/submit：data必填string，可为空；yield_time_ms省略1000，整数0–30,000。write按UTF-8发送原值；submit**无条件额外添加一个LF**，不trim、不自动去掉已有LF；文案继续建议data不带末尾换行。flush之后等待physical_completion，0立即返回；echo/输出不证明完成。两个动作不接受since_cursor/max_output_chars，内部沿原值32,000返回快照。
- close_stdin：仅action/PROCESS target；PIPE关闭输入；PTY先发送原有EOT再关可写副本。已关闭走当前幂等路径；真实close/write错误保持，不吞错成成功。
- kill：仅action/PROCESS target；原生terminate_if_running终止进程组并进行物理join，已终态返回已存在快照；PHYSICAL_SETTLEMENT_INCOMPLETE仍抛原错误，不能仅看target存在而宣称进程及子进程已退出。调用后快照预算仍32,000。
- 未找到/foreign owner/无效ID、stdin closed、输入flush失败、物理join失败等，沿现有执行错误结算。INVALID_CURSOR仍返回原错误结构及省略cursor读取的指引；不要求每个错误都携带target，不添加新的错误catalog。

进程状态和工具调用状态不同：现行 `_terminal_execution_result` 对非start的有效快照返回tool success，即使进程exit_code非零；start的killed为interrupted，其他失败为error，success/running为success。该区分、error在非start为null、trusted duration仅start/poll/wait、yielded_to_background条件均保持。

### 4.3 terminal_monitor：默认、null与生命周期

register只接收action、PROCESS target及三个可选闭合策略对象。三个对象省略会使用现行默认对象；显式null都非法；空对象合法。conditions内部output/heartbeat可以null，不能把这种nullable扩展到外层对象。

| 字段 | 默认与范围 |
|---|---|
| conditions.output | 省略/null均关闭output进度触发；`{}`启用并使用子字段默认 |
| output.min_new_output_chars | 200；整数1–65,536 |
| output.quiet_period_ms | 500；整数0–10,000 |
| conditions.heartbeat_interval_seconds | 省略/null关闭heartbeat；非null整数5–1800 |
| delivery.max_output_chars | 4000；整数512–32,000 |
| delivery.minimum_progress_observation_interval_seconds | 5；整数5–1800 |
| lifetime.maximum_duration_seconds | 36,000；整数1–36,000；是监视时长，不杀命令 |

省略conditions表示completion-only；completion总是启用。输出阈值、quiet period、progress最小间隔及heartbeat现有触发算法保持，不能重新解释成统一的“每隔X秒”。prepare先观察并订阅运行进程；拒绝OWNER_CLOSED、CAPACITY_EXHAUSTED、DUPLICATE_PROCESS_MONITOR、PROCESS_NOT_FOUND、PROCESS_ALREADY_TERMINAL。现行重复注册不是更新策略；target统一不增加update或replace。

保留DORMANT→工具结果提交后ACTIVE、未提交ORIGIN_DISCARDED；baseline_cursor来自注册原子观察，注册前输出不当新进度。completion与注册/订阅竞态、draft合并、immutable in-flight/successor、FULL/NONE/UNKNOWN结算沿当前owner，不因包装重放注册或新建回执。

list仅当前未CLOSED的注册，按monitor_id排序；可含DORMANT，输出state/observation_ordinal/pending不删。不是进程状态查询；没有分页或追加预算。

cancel只停止监视。现行结果：`cancelled`→status CANCELLED；`not_found`或`already_terminal`→status REJECTED；cancellation_outcome仍原字符串，工具调用本身仍success。已经冻结的in-flight观察可能继续按原有结算边界处理，取消不撤回已接受消息，不阻止进程继续。cancel与人类停止进程的cancel_for_process不是同一个接口，不合并两套结果枚举。

原有8活跃注册、119进度观察、10分钟窗口60次进度、12次自主续接、36,000秒监视寿命和28,000输出字节边界是现行受限监视合同，本次全部保持，不扩展到命令/会话总寿命，也不额外引入“防御性”上限。HOST_CLOSE/EXPIRED/PROCESS_TERMINAL/DELIVERY_BUDGET_EXHAUSTED等原关闭路径与弱恢复语义不变。

## 5. 结果、通知和后续调用

### 5.1 命令结果与列表

有真实process_id的 `_terminal_execution_result` 将原顶层process_id替换为target，其他所有结果字段原样保留。已结束进程也返回同一target；目标不保证仍运行或将来仍保留。

```json
{"status":"running","terminal_process_action":"start","target":{"kind":"PROCESS","process_id":"p"},"output":"working","exit_code":-1,"cwd":"/project","timed_out":false,"truncated":false,"error":null,"reason":null,"yielded_to_background":true,"output_disposition":"CURRENT_SNAPSHOT","output_cursor":"c","retained_from_cursor":"c0","gap_before_output":false,"truncated_by_response_bound":false,"source_coverage":"COMPLETE"}
```

preflight失败、容量拒绝等没有生成真实process_id时省略target，不返回null/空PROCESS，不从调用参数捏造进程。错误/原因正文、exit_code=-1、capacity原因保留。

process list的每一项：旧`process_id`→`target`，command/cwd/status/exit_code/timed_out/stdin_closed/duration_seconds/output_cursor/retained_from_cursor不变。列表外层仍status/terminal_process_action/processes。不要机械修改 `TerminalProcessInfo.to_payload()` 给原生日志和后台API强加模型形状；可在tool_runtime现有模型结果producer内用小投影替换身份字段，既有原生DTO保持。

输出artifact仍存原输出文本，artifact模式/coverage与可信时间补充不变；artifact target和hint由文件组负责。本组不在PROCESS里附artifact，不把“更多日志”变成重启命令。

### 5.2 monitor结果

register成功：

```json
{"status":"REGISTERED","target":{"kind":"MONITOR","monitor_id":"m"},"process_target":{"kind":"PROCESS","process_id":"p"},"baseline_cursor":"c","expires_at":"2026-10-28T12:00:00+00:00","policy":{"completion":true,"output":false,"heartbeat_interval_seconds":null}}
```

原monitor_id/process_id不再双写。target是新注册对象，process_target是请求中原样PROCESS。并不表示注册已越过原本的结果提交激活边界。

list的monitors每项同样用target/process_target替换两个裸ID，其余state/observation_ordinal/pending不变；内部coordinator.list_current可保持native ID，由model结果producer投影，不影响内部hand-off消费者。

cancel结果：

```json
{"status":"CANCELLED","target":{"kind":"MONITOR","monitor_id":"m"},"cancellation_outcome":"cancelled"}
```

REJECTED/not_found也返回请求MONITOR target，表示本次指向对象，不声称其存在。register/ROOT等拒绝仍为原status/reason，不制造monitor target。

### 5.3 异步通知与压缩

`model_input/lowering.py::_project_terminal_observation`已经负责将终端canonical观察投影成模型正文。仅修改此既有投影：公开monitor_id→target(MONITOR)、process_id→process_target(PROCESS)。不增加renderer，保留 `pulsara_terminal_observation` 包装、status/display_kind/gap/delivery_coverage/output/observation_kind等当前公开字段及running且exit_code为null时的省略规则。内部canonical的observation_id、ordinal、所有字节计数、schema_version、host_scoped等原事实及其可见性策略不变，不把现行投影已隐藏的字段重新暴露。

该helper同时用于普通 `lower_canonical_item`（约243）及 `lower_retained_request_content`（约319），两条路径必须统一采用新目标，并在实际模型可见字节/快照quote中计算包装成本。只更新tool结果而遗漏异步/retained正文会留下两种模型身份习惯。

`TerminalObservationContentV1`、canonical_mapping、32,000 canonical byte硬界、28,000输出字节界、reader闭合重建与唯一编码验证、in-flight digest保持原样；wrapper增加的是provider投影字节，不是canonical bytes。原生正文到公开target的已有投影是职责边界，不是旧新模型协议兼容或双写。通知安装/FULL/NONE/UNKNOWN结算、事件/subject/guard/relation不动。

`ProcessControlFeedbackContent`已有 `project_user_control_feedback_for_provider`，因此只在该投影将provider_value.process_id替换为target(PROCESS)，将monitor_result.monitor_id替换为monitor_result.target(MONITOR)。native canonical、严格validator、command/session/host/origin、控制结果以及UI protobuf保持原样；没有必要连同人类控制存储一起改名。投影仍保持“requested stop”和“stopped”的实际结果差别，monitor_result可null，已冻结观察ID与其他结果字段保留。

压缩runtime handoff的terminal_processes项使用target；terminal_monitors项使用target和process_target。frozen DTO、当前进程选择、scope过滤、ROOT-only monitor、命令/目录preview、排序、max bytes及full→compact降级保持。新JSON长度进入现有预算检查；不足时走当前降级，不截断target或添加“身份遗漏恢复”机制。subagent task board内容与历史选择不动。

### 5.4 UI、Hook与提示词

`remote_identity`是工具执行内部观察归属，必须由新结果target.process_id提取；target替换若漏此处会导致结果看似成功但内部关联丢失。不可为修复它保留旧顶层process_id镜像。

terminal摘要与output渲染当前不读取process_id，身份包装后继续依据action/status/exit_code/output；增加行为回归即可，不无关改造组件。后台进程列表、日志、停止控制、live monitor protobuf payload继续使用原生ID；模型target与UI routing DTO在现有展示适配处连接。open_in_pulsara仍ROOT-only、按项报错和异步展示，不能从已显示tab推断命令状态。

PreToolUse/PermissionRequest使用新公共输入；PostToolUse使用新公共结果，tool_name/action/data不改。terminal命令workdir仍顶层；terminal_process/monitor的对象路径为tool_input.target。更新Hook扩展说明；不为旧Hook脚本反向展开process_id，matcher别名不变。内建Skill搜索未发现终端裸ID调用示例，实施时复查新出现的调用样例；不为了“同步”给无关Skill加target教程。

## 6. schema、parser与权限实施

`ports/terminal.py`中新增两个本地闭合target输入类型（或同等精确typed定义），复用PROCESS于process六分支和monitor register；MONITOR只用于cancel。_ProcessInput从process_id改target，monitor cancel的canonical_monitor_id validator移动到MONITOR的ID字段；旧顶层字段被extra=forbid拒绝。无需改ProcessRegistry或建立全局target基类。

导出仍精确root action union，7个process分支、3个monitor分支；每个target内联闭合，不使用$defs/$ref请求压缩，不加request外包。保持当前lowerer和strict:false；provider wire是superset，无法表达所有action与target配对，只用function.description的动作行加精确本地schema/parser控制。监视wire即使接受register+MONITOR，canonical/parser也必须拒绝；poll携timeout_seconds仍拒绝。

descriptor、输入binding、parser、authorization schema及执行器必须一起采用新合同；不把模型参数临时展平回旧公共parser。使用原typed target的process_id调用native owner是合法小适配，不是公共兼容层。

权限分类保持：terminal是terminal effect；process list/poll/wait具现行只读override，write/submit/close_stdin/kill沿原terminal权限；monitor仅list具只读override，register/cancel保留现行权限规则。target不授予执行资格。hardline继续检查command或write/submit的顶层data；动作闭合集合、physical effect class、trusted observation binding不改。

终端physical-call deadline/caller cancellation/side-effect settlement、监视process-local registration token、真实运行资源和错误语义保持。不得因target包装把已发生写入、kill或register重放；不得把“本次工具超时”替换成“进程终止”。

## 7. 英文 function.description 候选

完整可复算文本在本组证据目录的三个 `.description.txt`。下列动作签名为唯一公开调用习惯，不复制到parameters根description。

`terminal`：

```text
Run one local shell command. Each call starts a new shell, so its directory and environment changes stay within that call. A running command returns a PROCESS target for terminal_process to inspect or interact with it. In the ROOT conversation, terminal_monitor can resume work on completion or configured progress. Response size and initial wait settings do not limit the command's lifetime. Use artifact_read for omitted output or artifact_export for extensive processing.
```

`terminal_process`：

```text
Control commands started by terminal. Copy a PROCESS target from terminal or the process list; reuse it across actions. Set action to the operation below. Fields listed for an action are required unless marked optional; use only that action's fields.
list: optional include_running, include_finished.
poll: target (PROCESS); optional since_cursor, max_output_chars.
wait: target (PROCESS); optional since_cursor, timeout_seconds, max_output_chars.
write: target (PROCESS), data; optional yield_time_ms.
submit: target (PROCESS), data; optional yield_time_ms.
close_stdin: target (PROCESS).
kill: target (PROCESS).
poll reads now; wait waits up to timeout_seconds. write sends data unchanged; submit adds one newline to data supplied without its final newline. close_stdin closes input; kill stops the command and its children and waits for exit. Omitted optional fields use their defaults; only since_cursor accepts null. status and exit_code describe process completion; output or echo alone does not establish completion. Use terminal_monitor in the ROOT conversation for later updates, artifact_read for omitted logs, and artifact_export for extensive retrieval.
```

`terminal_monitor`：

```text
ROOT only. Resume the conversation when a running command completes or meets configured progress conditions. Set action as follows; fields are required unless marked optional, and each action uses only its listed fields.
register: target (PROCESS); optional conditions, delivery, lifetime.
list: no other fields.
cancel: target (MONITOR).
Copy a PROCESS target from terminal/process results and a MONITOR target from register/list. register returns immediately; the current turn may end while monitoring continues. Omitting conditions requests completion only; configured progress updates also resume the conversation. list shows active monitors, not process completion. cancel stops updates while the command continues. Monitoring also ends on expiry or when its terminal environment closes. Use terminal_process poll for current output or status, and kill to stop a command.
```

## 8. 离线计量与证据

使用当前生产catalog、`lower_openai_function_parameters`、`PulsaraHeuristicTokenEstimatorV3`与仓库`.venv`。每行是完整单工具Responses tools数组，不是provider usage或整个SYSTEM/tools；组计量直接对数组计算，局部舍入不可相加替代。

| 工具 | 当前 | 候选 | 差值 |
|---|---:|---:|---:|
| terminal | 328 | 330 | +2 |
| terminal_process | 562 | 625 | +63 |
| terminal_monitor | 695 | 794 | +99 |
| 三工具数组 | 1585 | 1747 | +162 |

本方案为复制习惯支付额外字节，不宣称减少tokens或提高首次正确率。计量不含open_in_pulsara（由文件组计）、工具结果、异步通知和压缩文本；那些结构也会增加字节，必须由实际生产预算覆盖。

19个正例通过候选canonical和当前lowerer wire；53个反例被canonical拒绝。脚本将target包装逆变换后与生产schema精确相等（恢复原ID annotation），证明除身份结构外的所有分支/default/null/range当前未变；不是生产parser或真实进程行为证明。示例ID为形状占位，没有冒充native ID验证。

产物：

- `output/tool-target-hardcut-20261028/terminal/design_schema.py`：复算脚本，仅写该目录。
- `before.canonical.json` / `after.canonical.json`：三工具精确schema。
- `before.responses.json` / `after.responses.json`：现有lowerer生成的完整工具定义。
- `examples.json` / `negative_examples.json`：正反例。
- `notification.examples.json`：PROGRESS/COMPLETION/EXPIRY的当前真实投影与候选身份包装；canonical不变，保留running的exit_code省略。
- `measurements.json`：机器结果，provider_requests=0。

复算：`PYTHONPATH=src .venv/bin/python output/tool-target-hardcut-20261028/terminal/design_schema.py`。

已有190请求证据支持继续lowerer/strict:false和function.description动作行；它不验证本次PROCESS/MONITOR定义。不得引用旧成功率宣称候选通过provider。

## 9. 实施验证与完成条件

下列行为由生产 parser、真实终端进程与既有集成测试验证，实际运行范围见实施报告。

1. 七process/三monitor分支、terminal无target、两个list无target；缺字段、错kind、混合字段、旧flat参数、额外身份键及null拒绝；所有原默认/上下界保持，monitor的ID语义validator不丢失。typed target→native ID准确且无trim。
2. 启动结果/list/monitor注册或通知取得的PROCESS原样复用到全部六process动作、monitor register及展示；MONITOR只交cancel。不同Host、失效process、错误ID拒绝仍由现有owner处理，不借target增加跨Host调用。
3. PIPE与PTY的write/submit实际字节：空data、原末尾LF、yield=0/默认、flush失败/closed stdin；close EOT、kill子进程/物理join失败和already terminal。验证真实状态，不只断言对象包装。
4. poll/wait正常、null cursor、foreign process/stream/owner/future cursor、GAP、响应tail与输出artifact；wait超时仍running，初始yield与command lifetime分离，可信duration/action绑定不变。更新现有 `test_builtin_default_null_contracts`、`test_terminal_cognitive_subtraction`、`test_round2_terminal_output`、`test_terminal_output_settlement` 等直接相关用例。
5. monitor所有策略默认和nested null；注册DORMANT结算、完成竞态、重复与容量拒绝、cancel三个真实outcome、到期/Host关闭、不杀进程、in-flight取消边界与自主续接保留。沿现有 `test_round2_terminal_monitor`、safe-point/host/Postgres集成用例验证，不新增监视框架或放宽assertion。
6. 通知普通lower与retained request都输出target/process_target且无公开裸ID，状态/省略/输出字段保留，实际final input与snapshot quote计新provider bytes；native canonical/reader唯一编码、digest和32KB预算保持。人类控制只改既有provider投影，原native控制反馈仍通过validator。压缩terminal目标和full/compact预算降级、ROOT-only monitor选择不变；沿现有structured model input compiler用例验证，不写固定字节/token快照。
7. 权限矩阵/hardline/Hook输入输出完整新shape；target不绕过权限；tool result remote_identity仍正确；sidebar/background UI原生API不因模型改名被破坏；展示适配与文件组联合验证。
8. 复用现有prefix冻结验证：只在合法epoch安装新schema，不因monitor、运行进程、refresh或本次schema发布重建root。不要为开发hard cut新增旧参数兼容层。
9. 实施后可按统一activation安排，在已保存生产连接上用既有实验harness做小型模拟工具Responses复核：两provider均覆盖poll与wait字段区分、write与submit、register PROCESS/cancel MONITOR及默认/null。只看结构与任务语义，不执行真实命令；真实进程行为由上述本地测试/既有终端dogfood覆盖。本次统一 provider 小样本已覆盖 poll/list，其他终端动作由本地行为测试验证；未另外重复完整 provider 批次。

## 10. 未决与明确建议

无未闭合的身份或owner选择：PROCESS和展示组已协调一致；MONITOR有独立身份；列表/创建例外明确；所有输入/结果/通知/压缩消费路径均有实施位置。建议按本规格单次hard cut，内部原生ID与已有状态机保持。

生产 parser、执行器、通知和压缩投影已完成聚焦验证。真实 provider 小样本覆盖终端 poll/list；完整动作链由本地真实进程验证，不以小样本推断长期生成成功率。
