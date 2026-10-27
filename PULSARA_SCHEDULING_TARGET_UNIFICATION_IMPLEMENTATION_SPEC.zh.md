# 定时任务目标统一实施调研规格

日期：2026-10-28。状态：生产 hard cut 已实施，公开请求、结果及消费者采用本规格。验证与范围见[实施报告](output/target-hardcut-implementation-20261028/scheduling/REPORT.zh.md)；整体结果见[汇总](output/target-hardcut-implementation-20261028/root/REPORT.zh.md)。

## 1. 决策、范围与非范围

保留 `scheduled_tasks` 的八个动作、switch 两个分支和五种时间规则。模型对既有计划使用 `target={kind:"SCHEDULE",task_id}`，结果原样提供可复制目标；revision 是操作 guard，留在目标外。已知确切目标可直接调用，不新增先 list/get 的强制流程。

`switch` 根 `target` 只表示被暂停的源 ACTIVE 计划；现有名为 `target` 的替换配置改名 **`replacement`**：

- `{kind:"existing",target:SCHEDULE,expected_revision}`：启用同一 session 的既有 PAUSED 计划。
- `{kind:"create",values}`：在源 session 创建并启用新计划；没有新对象 ID，也没有可改写 session/status 的位置。

replacement 的 `kind=existing/create` 是现有操作分支标签，保持小写；对象 target 的 `kind=SCHEDULE/SESSION` 大写。不要把 replacement 本身变成另一种全局 target。

`list/create` 不操作一个既有计划，没有主对象 target。使用独立的 `session: SESSION|null`，分别表示查询过滤和新计划归属。SESSION 形状与会话检索组一致：`{kind:"SESSION",session_id}`，对象内 ID 必填；整个 session 省略或 null 在模型入口表示当前会话。`status/cursor/values/expected_revision` 仍在各自原有职责位置，不放入 target。查询游标保持原有 schedule ID 标量，不把它误包装成要执行的目标。

### SUPERSEDES

本规格只替代 [此前清晰化规格](archived_docs/PULSARA_CAPABILITY_SCHEDULING_TOOL_CLARITY_IMPLEMENTATION_SPEC.zh.md) 的 scheduled 模型身份字段、switch 参数名称及相应结果交接。其清晰化依据、时间语义、调度执行和无参数 reload 合并均保持。与 [capability 目标规格](PULSARA_CAPABILITY_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) 共用 target 习惯和 provider 路线，不共用执行器或管理目标注册表。

不添加 cron、后台持续运行、模型 run-now、新日历规则、新计划状态、全用户模型列表、新建会话工具、耐久任务/回执/恢复图或总量上限。UI-only run-once、原生 relational rows、queue provenance 和已有事务/模型选择/权限 owners 保留。subagent 整条链路不在范围内。

只接受新模型输入，不保留旧顶层 task_id/session_id 或旧 switch.target 别名。内部 row.id/task_id/session_id 可以保留。已有历史工具消息不重写、不为旧参数增加兼容解析。新 schema 仅在 cold epoch 或明确采用的 compaction successor 安装；运行中 epoch 的 SYSTEM/tools 字节不变、messages 只追加。

## 2. 已核对的生产链路

完整阅读 BRIEF、AGENTS、capability 目标规格、完整历史 capability/scheduling 清晰化规格及 190 次实验报告；当前事实以下列生产 owners 为准。

| Owner（路径相对仓库） | 当前事实与此次责任 |
|---|---|
| `src/pulsara_agent/capability/builtin_catalog.py:81,1047,1971` | list/get action 权限覆盖；八动作说明、action_schema；只绑定 HOST_MAIN_RUN 且需 scheduler port。替换公开说明/schema，不新增工具或可见范围。 |
| `src/pulsara_agent/scheduling/requests.py:8–218` | `_FIELDS`、`creation_status`、`validate_action`、`action_schema`。当前 root 八分支，switch 内 existing/create，values 内五种 rule；所有分支闭合。 |
| `src/pulsara_agent/scheduling/service.py:35–215` | 原生 payload、分页、create/mutate/switch；invoke 在 validate 前填模型 session 默认；create/update/switch 对新配置/目的配置做 permission coverage。 |
| `src/pulsara_agent/scheduling/contracts.py:26–175` | 整秒 UTC、IANA timezone、闭合 rule、dateutil rrule、DST/求下一次；保持原机制和库所有权。 |
| `src/pulsara_agent/conversation_kernel/_repository/scheduling.py:45–199,234–609` | memory_domain 隔离、OPEN session、分页、writer/session锁、版本与单 ACTIVE、switch 原子事务、pending 取消；不迁移身份列或另建目标表。 |
| `src/pulsara_agent/conversation_kernel/_repository/scheduling.py:626–652` | once 在输入入队后删除计划；循环规则推进 next_run_at，不把任务完成与计划移除混淆。 |
| `src/pulsara_agent/conversation_kernel/tool_runtime.py:940–960,1277–1286,1876–1885,3070–3096,4536–4551` | SUBAGENT 不暴露；prepare/authorize均按canonical schema前置验证；ROOT执行、应用错误、取消等待management operation结算、实际结果保留。 |
| `src/pulsara_agent/capability/call_classifier.py:95–141`、`src/pulsara_agent/tool_permission.py:276–375` | action 分类维持 list/get 只读；READ_ONLY 拒绝其他 schedule 动作。没有 capability 管理那种 READ_ONLY 用户表单例外。 |
| `src/pulsara_agent/desktop_app/scheduled_controller.py` | HTTP routes 与模型共用 request validator，却有不同 session 默认；UI run-now 独立 owner。共享 parser 改造不能破坏 HTTP。 |
| `frontend/lib/scheduled-tasks-api.ts`、`frontend/components/scheduled-tasks-view.tsx` | 原生 UI payload 使用 id/session_id；switch UI 已有 existing 调用，API type 同时允许 create。列表是用户级，显式 session filter；不依赖当前聊天的默认。 |
| `src/pulsara_agent/ports/system_prompt.py:74–92` | 未来工作、当前 turn 立即执行、UI run-once、once 退休和 memory retrieval 指引；无旧字段示例，不需要改文案。 |
| `src/pulsara_agent/model_input/lowering.py:219,290–310` | 普通USER与retained USER request共用`scheduled_request_parts`，当前Saved task头输出裸task_id；此既有最终provider投影同步输出SCHEDULE target，native provenance保持。 |
| `src/pulsara_agent/bundled_skills/`、`frontend/lib/builtin-tool-summary.ts` | 搜索未发现 scheduled 专属 Skill 入参示例或 summary 分支；不要为本次结构调整新增 Skill/摘要机制。 |

这里的行号用于定位当前实现，不作为未来冻结断言；内部 task 命名不代表 subagent task，schedule ID 仍要求现有 `schedule:` canonical identity。

## 3. 精确形状及逐动作前后

两个目标均闭合，拒绝额外字段：

```json
{"kind":"SCHEDULE","task_id":"schedule:…"}
{"kind":"SESSION","session_id":"session:…"}
```

SCHEDULE 无 session_id/revision，因 task_id 在现有存储中独立定位计划，真实归属由 repository 读出；传一份 caller 声称的 session 并不能替代归属验证。`expected_revision` 必须是真正 int、>=1；bool 不可混用。ID 沿 `identity.is_valid_id` 校验，不引入新的长度/总量限制，不 trim 或重写 ID。

下表 S、D 是源/目的 SCHEDULE 对象，P 是 SESSION 对象，V 是完整 values；这是对照缩写，不是实际可发送 JSON。完整可提交形状在 [examples.json](output/tool-target-hardcut-20261028/scheduling/examples.json)。

| 动作/分支 | 当前请求 | 候选请求 | 保持 |
|---|---|---|---|
| list | `{action:"list",session_id:s,status:"PAUSED",cursor:c}` | `{action:"list",session:P,status:"PAUSED",cursor:c}` | 三字段可省；cursor续页重带同过滤。 |
| get | `{action:"get",task_id:t}` | `{action:"get",target:S}` | 不要求 revision；已知确切对象可读。 |
| create | `{action:"create",session_id:s,values:V,status:"ACTIVE"}` | `{action:"create",session:P,values:V,status:"ACTIVE"}` | session/status可省；由服务分配新ID。 |
| update | `{action:"update",task_id:t,expected_revision:3,values:V}` | `{action:"update",target:S,expected_revision:3,values:V}` | 完整替换values，无session/status字段。 |
| pause | `{action:"pause",task_id:t,expected_revision:3}` | `{action:"pause",target:S,expected_revision:3}` | 取消该计划pending，running不动。 |
| resume | `{action:"resume",task_id:t,expected_revision:3}` | `{action:"resume",target:S,expected_revision:3}` | 当前单ACTIVE约束、下一未来周期/过期once原规则。 |
| delete | `{action:"delete",task_id:t,expected_revision:3}` | `{action:"delete",target:S,expected_revision:3}` | 删计划与pending，留聊天与running。 |
| switch existing | `{action:"switch",task_id:t,expected_revision:3,target:{kind:"existing",task_id:d,expected_revision:8}}` | `{action:"switch",target:S,expected_revision:3,replacement:{kind:"existing",target:D,expected_revision:8}}` | 两个独立guard；同session，源ACTIVE、目的PAUSED。 |
| switch create | `{action:"switch",task_id:t,expected_revision:3,target:{kind:"create",values:V}}` | `{action:"switch",target:S,expected_revision:3,replacement:{kind:"create",values:V}}` | 新对象无需guard/ID；自动同session且ACTIVE。 |

switch existing 的真实 JSON 示例（省略号仅表示 ID 展示缩略，附件包含格式合法 ID）：

```json
{"action":"switch","target":{"kind":"SCHEDULE","task_id":"schedule:source…"},"expected_revision":3,"replacement":{"kind":"existing","target":{"kind":"SCHEDULE","task_id":"schedule:destination…"},"expected_revision":8}}
```

### 3.1 省略、null、范围和分页

- 模型 list/create：当前 `invoke` 先将省略/null session_id 替换为 default_session_id，然后 validate。因此两者今天均可用；不是仅依据 description 推测。候选同样仅在这两个动作对整个 session 省略/null做当前会话解析，再进入精确身份验证。`session:{kind:"SESSION",session_id:null}` 非法。
- 直接调用现有 `validate_action({action:"list",session_id:null})` 会拒绝；现有测试 `test_scheduled_service_none_keeps_its_unfiltered_meaning` 明确覆盖。模型默认适配与原生 HTTP/service 校验是不同边界，不把默认注入 repository 或全部 service.list 调用。
- HTTP/service list 的 `session_id=None` 表示所有可见 OPEN sessions；模型 list 省略/null只能当前session。HTTP create必填合法session_id，不接受null；这些原生合同保留。
- create status省略/null→PAUSED；ACTIVE/PAUSED合法。list status省略→两状态，显式null在模型canonical中非法。`tool_runtime` 的prepare（1279）与authorize（1878）均调用 `_validate_builtin_call_schema`，因此真实模型入口已经拒绝list.status:null；其唯一逐项例外是open_in_pulsara，与scheduled无关。现有 `validate_action` 不独自检查list status，repo的None表示不过滤，这是内部直接调用语义；候选保持模型canonical gate，不把内部宽松度当模型新能力，也不因共享parser误改HTTP/service None。其他动作不允许status/session，显式null也不得被提前删除从而绕过闭合检查。
- cursor仅list可带，必须是现有schedule ID格式，null非法；按ID严格大于游标继续，保持status/session过滤。无用户limit，沿 `history_page_default_entries=128`、`history_page_hard_bytes=7 MiB` 既有每页预算；首条大对象的当前处理不改变。没有任务总数/历史/轮次上限；cursor不绑定过滤签名，不新增游标对象、快照或hash。
- read/get的可见性沿memory_domain；list另外只列OPEN session。get已知对象不能成为跨domain访问路径。session关闭、目标不存在、目的跨session等均按当前owner失败，不自动创建/移归属/换session重试。

## 4. 五种时间规则与 values 语义

`values` 始终闭合且完整：name、prompt、schedule、timezone、permission_mode 五字段必填。name/prompt 要求非空白并使用现有 canonical prompt storage/admission 大小边界；不为模型友好增加小cap。timezone 不是 parser 自动默认，模型说明提示未指定时填写 Asia/Shanghai；缺字段仍拒绝。

schedule 的 `contract:"scheduled-rule:v1"` 保留，kind为既有小写，五分支结构此次完全不改：

| kind | 必填业务字段及样例 | 保持行为 |
|---|---|---|
| once | `run_at_utc:"2030-11-01T01:00:00Z"` | 整秒UTC；new/create未来；修改时间到过去拒绝。未修改既有时间的纯文字更新不新增未来检查。 |
| interval | `anchor_at_utc:"2030-11-01T01:00:00Z",seconds:3600` | anchor固定，seconds正整数且非bool；UTC，不靠更新时钟重锚。 |
| daily | `start_date:"2030-11-01",time:"09:00"` | IANA本地日历，ISO日期和HH:mm。 |
| weekly | 上述日期/时间与 `weekdays:[1,5]` | 非空、不重复、1–7，周一=1；原parser排序。 |
| monthly | 上述日期/时间与 `day:31` | day整数1–31，不存在的月日跳过。 |

保留 dateutil 枚举当前日历窗口、ZoneInfo 时区和DST检查：不存在当地时间跳过，重复当地时间选择fold=0一次；不补跑错过的循环时点。datetime可表示范围耗尽沿当前typed错误/自动PAUSED行为，不能替换为人为查询年数/次数cap。

update保留status；对schedule改变或calendar规则的timezone改变重算ACTIVE next_run_at，once/interval仅改timezone不改变UTC序列。原子switch/create也用数据库clock决定下一次；create无论PAUSED/ACTIVE都要求未来有效时点并验证会话模型binding。resume与switch existing对过期但尚未触发的once把原时间变为due；switch create不接受过去once。

once 在 TIMER 或 UI RUN_ONCE 输入被接受入queue时删除计划，queue和历史仍存在；删除不表示执行成功，不因失败恢复计划。循环run-once不改变自动日程。保存文字与正在排队的冻结instruction分开，编辑不重写已排队输入。

## 5. 权限、事务和错误保持

target 是身份，不是授权、存在性证明或此前用户选择的凭据。当前 ROOT-only/port availability、list/get只读覆盖、其余动作 permission gate 不变；READ_ONLY不能通过换target结构获得调度写权限。

模型invoke对create/update/switch检查当前preset覆盖保存的permission_mode。switch existing检查的是目的计划保存权限，switch create检查replacement.values；不能错误检查源计划而放行更宽目的权限。resume当前没有相同的保存配置检查；本次不借重构增加或删除该条件。其他既有gate仍执行，历史工具结果并不授权新变更。

`expected_revision` 是刚观察的行版本，目标可长期复制但guard须取新revision；不存在自动读新revision并重放变更的路径。同ID源/目的在parser拒绝；现有同session row lookup同时保障目的归属。源必须ACTIVE且revision匹配；目的必须PAUSED且revision匹配，否则冲突。源暂停、源pending取消、目的激活/创建都在同一个session writer事务内；任何校验、模型配置、时点或后续写入失败整体回滚，不改为pause+resume两次工具调用。

一个session只允许一个ACTIVE，任意多个PAUSED；保留session锁与部分unique索引。无效新ACTIVE创建/恢复报ACTIVE_TASK_CONFLICT，不自动替换既有计划。实际无变化的update或已处在所需状态的pause/resume沿当前返回、不凭空递增revision；pause即使已PAUSED仍执行已有pending取消步骤。

pause/delete/switch只取消源计划PENDING inputs，运行中回复继续。保留当前queue cancellation events与已经存在的writer守卫，不新增取消回执或新guard。工具调用被取消时沿 `_await_mcp_operation` 等待已接受的管理操作结算，返回实际结果；不把调用方取消报告成事务回滚。

现有应用错误格式 `{error, message}` 保留：INVALID_ACTION/INVALID_REQUEST/INVALID_ID、PERMISSION_ESCALATION、REVISION_CONFLICT、ACTIVE_TASK_CONFLICT、INVALID_SCHEDULE/INVALID_TIMEZONE/TIME_RANGE_EXHAUSTED、SESSION_ARCHIVED及模型配置异常沿原owner。KeyError/ValueError当前由tool_runtime包装为APPLICATION_ERROR/INVALID_REQUEST；不凭规格把不存在目标虚构成新稳定错误码。native writer/resource失败的既有路径不变，不以target解析失败后的fallback隐匿真实错误。

## 6. 结果与所有消费者

### 6.1 模型结果投影

使用一个小的模型结果渲染函数，在 `ScheduledTaskService.invoke` 的返回边界将现有原生task payload转换为新合同；不修改repository row，不使 `get` 内部权限/运行检查面对投影而丢失原生字段。每个模型task对象：

- 删除原 `id`，替换为 `target:{kind:"SCHEDULE",task_id:row.id}`。
- 删除原 `session_id`，替换为 `session:{kind:"SESSION",session_id:row.session_id}`；归属可直接复制到list/create，也可交给会话检索工具的SESSION位置。
- 保留name/prompt/schedule/timezone/permission_mode、revision、status、next_run_at、created_at/updated_at，以及当前存在的session_title、pending_queue_item_id、last_turn_status/last_turn_id/last_entry_id、prepare_error等实际信息。当前write结果未带某些join字段时不新增强制补查。

不能同时保留model row.id和target.task_id，或session_id和session两个镜像；target的存在不表示计划当前还存在。关联turn/entry/queue scalar仍有独立provenance用途，本次不强行把所有ID套成target。

list保持`{tasks:[…],next_cursor}`；get/create/update/pause/resume返回投影对象；switch保持`{paused:…,active:…}`且两者各自携带target与revision；delete仍返回JSON null，错误保留原error对象。不为已经删除的对象新增成功receipt，只为能返回的真实对象提供可复制target。

示意（仅列相关字段）：

```json
{"target":{"kind":"SCHEDULE","task_id":"schedule:…"},"session":{"kind":"SESSION","session_id":"session:…"},"revision":4,"status":"PAUSED","name":"Daily review","next_run_at":null}
```

### 6.2 HTTP/UI 与同一 request owner

原生service/HTTP task payload的id/session_id仍服务页面、run-now、deep link、active-plan定位、refresh与编辑；它们不是旧模型参数别名。目标统一不要求改数据库列或全UI行类型。模型投影仅在invoke，不从active session推导页面读/刷新/授权。

HTTP现有URL `{task_id}`、list query session_id、create body session_id保持原生路由职责；controller在构建共享的新请求validator输入时显式构造SCHEDULE/SESSION，再调用现有native service方法。create不得通过构造null SESSION获得模型默认；HTTP列表未指定session不能注入当前会话。不要保留旧模型validate_action作兼容层。

**switch的旧参数名在公开HTTP body也改为replacement**：`{expected_revision,replacement}`，existing里的目标采用新SCHEDULE对象，create分支保持values。controller把路由task_id构成根source target做同一精确验证，源ID无需在body重复。service/repository切换参数建议同步改名replacement，并按其分支读取目的身份；native task_id/session_id仍可保留。具体内部typed值是否展开由唯一request owner决定，不能新增接受旧公共JSON的备用parser。

`ScheduledSwitchTarget`改名`ScheduledReplacement`并更新两分支type；`LocalScheduledTasksApi.switch`参数/body改名；`ScheduledTasksView.act` existing调用从task.id构造SCHEDULE。API虽当前页面只触发existing，仍必须保留create分支。其它UI表单、五种rule、preview/local-once和run-now request不改。前端源/目的的revision、替换后刷新和冲突重新读取须维持。

`system_prompt.py`当前没有旧形状，可原文保留；bundled Skills无scheduled示例，不创建新Skill。三个现有scheduled dogfood脚本与测试中模型trace断言读取新target/session/replacement；原生service调用/SQL断言不机械替换。通用trace展示真实新arguments/results，历史消息保留原文，不新增双读兼容。

### 6.3 已接收定时指令的 provider 头

`model_input/lowering.py::scheduled_request_parts` 当前把source、due time和裸task_id放在 `Saved task (...)` 头中；`lower_canonical_item` 的普通USER分支与 `lower_retained_request_content` 的retained USER分支都调用它。这个既有producer也必须同步，避免工具结果已可复制而实际定时输入仍要求模型重新拼目标。

只在该helper的最终provider文本投影中，将 `task_id: <id>` 替换为 `target: {"kind":"SCHEDULE","task_id":"<id>"}`；复用现有canonical JSON序列化，让目标是完整、可复制的JSON对象，不再同时保留另一个裸ID镜像。例如：

```text
Saved task (Automatic timer; requested at 2030-11-01T01:00:00+00:00; target: {"kind":"SCHEDULE","task_id":"schedule:…"}). Execute the saved instruction:
```

真实输出使用完整canonical ID，示例中的省略号仅用于展示。`Automatic timer`/`User selected Run once` 的trigger_source区别、真实due_at_utc、随后实际保存prompt以及当前消息角色/信任与snapshot引用方式均保持；不改queue provenance、ScheduledProvenance的native task_id/task_revision、接受输入或执行语义。目标仅从已冻结provenance.task_id派生，不能把provenance.task_revision变成当前expected_revision，更不能查询新状态后改写旧来源。

once计划可能已在入队时退休；此target标识来源计划，不保证对象仍存在，也不要求先get再执行保存指令。保留system_prompt现有的once退休说明，不新增恢复、重建或反复查询流程。普通USER与retained分支复用同一个helper；不修改subagent请求、profile/history或其消息producer。

本项仍受已安装epoch连续性约束：不能重渲染已安装messages来替换旧头；新投影随合法新epoch/明确采用的compaction successor使用。下节工具schema token表不计这个按实际scheduled输入出现的头部变化，不把它合并宣称为完整provider输入成本。

## 7. Hard cut 实施顺序

1. requests原owner定义闭合SCHEDULE/SESSION子形状、八动作新schema和switch两分支；保留完整values/rule。parser分清身份、guard、creation parent，拒绝旧位置和错类型；模型默认只在list/create进入精确身份验证之前解析。
2. service.invoke读取新参数，在唯一边界接入现有native方法；保留保存权限检查，switch全链路改replacement，增加唯一模型task payload投影。原生owner读取id/session_id、DB schema、队列与日历算法不变。
3. controller共享parser构造、HTTP switch body、frontend switch API/type/调用者同批改；没有old/new协商、双写或alias。
4. builtin正文替换为附件英文，`scheduled_request_parts`按§6.3同步既有provider头；更新新合同的样例、tests与dogfood，不修改历史实验。现有active规格中若引用旧scheduled形状须同步到新真值；本规格无需数据库迁移，本轮不执行任何重置。
5. 在既有合法epoch边界采用新定义；不借reload安装、更换运行中工具或删改旧messages。

## 8. Provider 路线、离线证据与 token

保留当前lowerer、strict:false、内联闭合嵌套target/switch/rule union；没有$ref、request外层、provider分支或新的schema框架。canonical八根分支经lowerer形成superset，function.description保留动作字段行。本地parser必须精确判定，wire合法不是执行授权。

证据目录：[scheduling](output/tool-target-hardcut-20261028/scheduling/)。[生成脚本](output/tool-target-hardcut-20261028/scheduling/design_schema.py)只读取生产descriptor、用真实lowerer生成候选，V3估算，JSON Schema检查；[canonical](output/tool-target-hardcut-20261028/scheduling/after.canonical.json)、[完整Responses定义](output/tool-target-hardcut-20261028/scheduling/after.responses.json)、[英文正文](output/tool-target-hardcut-20261028/scheduling/description.txt)、[计量](output/tool-target-hardcut-20261028/scheduling/measurements.json)可复查。

`description.txt` 是本规格**完整、精确的英文 function.description 候选正文**，包含八动作签名、switch两分支、session角色与现有时间/权限/队列语义；实施时在builtin_catalog安装该正文，不另拼一份与附件不一致的短说明。它是设计附件，不是生产运行时读取依赖或文案快照测试。

```bash
PYTHONPATH=src .venv/bin/python output/tool-target-hardcut-20261028/scheduling/design_schema.py
```

| V3口径 | 当前生产 | 候选 | 差值 |
|---|---:|---:|---:|
| canonical parameters | 1880 | 2200 | +320 |
| 当前lowerer后的parameters | 1104 | 1217 | +113 |
| Responses单工具tools数组 | 1690 | 1863 | +173 |
| Chat单工具tools数组 | 1693 | 1866 | +173 |

这不是provider实收usage、整体SYSTEM/tools或准确tokenizer计数。结构增加了常驻字节；收益假设是直接复制schedule/session身份并明确switch源与replacement，不宣称省token或已证实减少思考。

18个正例同时通过候选canonical和wire；21个形状反例均被canonical拒绝，其中4个仍被wire接受，正是lowerer superset边界。create/update/switch-create的完整values与五规则schema已逐值确认不变，五个规则样例也通过当前原生validate_rule。这些数字记录设计阶段的离线形状和规则检查；生产 parser、执行器和 PostgreSQL 事务验证见实施报告。ID格式、同源目的、revision状态、permission、真实时间与事务均需原生parser/owner验证，JSON Schema不能独立证明。

## 9. 必要验证与完成条件

实施时用既有tests拓展行为验证，不冻结英文全文、token数字或整份schema快照：

1. 八动作和switch两分支，目标/字段错位、旧flat输入、kind大小写、extra字段、缺省/null、bool revision、错ID、同源目的；values/rule schema约束与原生产逐值保持。现有 `tests/test_builtin_default_null_contracts.py` 更新为新contract，并保留其模型默认与native全用户列表分离断言。
2. list/get/create/update/pause/resume/switch结果target可直接用于后续动作；revision变化必须重新读；session可直接复制到会话检索及scheduled list/create。delete为null，错误无伪造target，原生UI payload不受模型projection影响。
3. `tests/test_scheduled_tasks_postgres.py` 保持单ACTIVE/任意PAUSED、revision/no-op、跨domain/跨session拒绝、switch source/destination检查及所有失败回滚、pending取消与消费竞争、分页预算无总数cap、archive删除、once退休。只改因public参数更名需要的fixture。
4. `tests/test_scheduled_tasks_time.py` 原五规则、DST gap/fold、长期seek与越界行为保持。timezone在说明中的选择默认不变成自动填缺字段；新/改once未来、过期once resume及existing switch、新replacement未来规则分别验证。
5. 权限gate ROOT/READ_ONLY和invoke配置coverage；switch检查目的而非源；取消在事务中保留实际结算。`tests/test_scheduled_tasks_host.py` 现有queue、hook后二次admission、compaction provenance、restart pending与不自动memory recall验证不减弱。对§6.3核对普通USER/retained USER中的同一个完整SCHEDULE target、TIMER/RUN_ONCE区别、due time及实际prompt不变；once退休后来源target仍可展示但不视为存活或当前revision，已安装epoch消息前缀不重写。
6. controller/API和 `tests/frontend/scheduled-tasks-view.test.tsx` 验证新replacement existing/create、错误刷新、用户级列表/会话filter、无当前会话仍可读取页面、run-now未改变。不是本任务要求的视觉变更，不新增页面流程。
7. 实施完成后复用保存配置和现有dogfood harness，按新公共shape覆盖当前scope默认、完整update、switch两分支、保留排队输入和立即执行saved task的既有语义。真实provider schema/填参复核用Qwen3.8 Flash与DeepSeek Flash Responses的模拟工具，覆盖switch根target与replacement的关键混淆，不执行真实管理副作用；不把历史190请求当候选验证。若产品激活已有dogfood要求，继续遵守，不在此杜撰长期门禁或额外总量。

## 10. 未决与明确建议

建议采用上述完整合同：SCHEDULE目标、独立SESSION角色字段、replacement双分支、模型专用无ID镜像投影；两组SESSION形状已对齐。没有需要新增产品机制才能解决的未决问题。

仍未验证的是新形状在真实provider上的首次正确率和实际调用成本，及新production parser/adapter的端到端实现。这是后续验证工作，不应通过保留旧参数fallback或把guard移进target回避。HTTP/default与model/default分离、switch两个revision和同session原子性是实施审阅的重点。

跨组critic已完整审阅：源target/replacement、双guard、目的权限、同session原子性、模型/HTTP默认分离、SESSION互通及共享validator适配均无阻塞；生产实现与行为验证已完成；候选 switch 分支也已在两模型、两协议的小样本中调用。
