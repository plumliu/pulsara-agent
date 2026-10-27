# 工具目标统一：整体实施方案

日期：2026-10-28。状态：生产 hard cut 已实施，六组生产链路与消费者已同步；8 个 subagent 工具保持原定义。实施验证见[汇总报告](output/target-hardcut-implementation-20261028/root/REPORT.zh.md)。

## 1. 目的与范围

同一个对象在查询、读取、操作和展示之间使用可复制的目标对象，减少模型重组身份字段的工作。工具保留各自职责和现有产品语义；不合并为通用执行工具，不建立目标注册表，不要求每次操作先查询。

本轮采用完整 hard cut。旧会话与旧请求不是兼容目标；可重置已核实的本地开发数据库。本次没有数据库结构变更，也未清空用户数据库。新接口不保留旧参数别名、双读双写或迁移分支。内部数据库列和原生服务 DTO 不因公开参数形状改变而机械改名。运行中已安装 epoch 的输入不因此获得重写许可；定义安装沿现有 cold epoch／compaction successor 边界。

**subagent 整个工具链路排除**：`list_agent_models`、`spawn_agent`、`create_agent_tasks`、`list_agents`、`send_agent_message`、`wait_agent`、`stop_agent`、`report_agent_result`。task/group 产品语义、历史继承、profile、任务间消息、工具可见性和 monitor 开放范围不在本轮。公共工具和公共结果采用单一新格式，不为 ROOT 与 subagent 另造两套接口。

`open_in_pulsara` 是跨类型的展示工具：本轮只修改非 task 分支，原 `{type:"task",task_id}` 请求与对应结果分支保留。这是范围划分，不是旧文件／进程参数兼容入口。

## 2. 文件分工

| 组 | 工具 | 方案 |
|---|---|---|
| Capability / MCP | list/inspect/manage/reload_capabilities、use_new_mcp_tool、read_mcp_resource、get_mcp_prompt | [既有联合规格](PULSARA_CAPABILITY_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) |
| 记忆 | memory_search、memory_get、remember、mark_memory_relation | [记忆规格](PULSARA_MEMORY_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) |
| 会话 | search_sessions、search_session_content、read_session_content | [会话内容规格](PULSARA_SESSION_CONTENT_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) |
| 终端 | terminal、terminal_process、terminal_monitor | [终端规格](PULSARA_TERMINAL_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) |
| 文件／artifact／展示 | read_file、write_file、edit_file、find_files、search_content、view_image、artifact_read、artifact_export、open_in_pulsara | [文件与展示规格](PULSARA_FILES_ARTIFACTS_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) |
| 调度 | scheduled_tasks | [调度规格](PULSARA_SCHEDULING_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) |
| ROOT 复核 | todo、enter_plan、ask_plan_question、exit_plan，以及跨组边界 | 本文 |

各组以当前生产代码为依据，覆盖入口、解析、授权、执行、返回和消费者。实现时按本文共同边界与各组具体合同执行；对旧规格的替代范围分别列在各组 SUPERSEDES 中，不把历史报告当新产品需求。

## 3. 共同接口规则

1. `target` 表示本次选定的对象；多个角色使用 `source`、`based_on`、`session`、`replacement` 等角色名，但对象形状复用。不把所有角色都命名为 target。
2. 操作内容、匹配条件、分页预算、修订条件和导出目的路径仍是操作参数。目标并不证明存在、资格、授权或数据新鲜。
3. 查询返回可操作对象时，身份与内容／统计分开，提供后续工具能直接复制的目标；不同时新增等价的裸 ID 镜像。来源事实、关系元数据、内部 exposure IDs 等仍按实际用途保留。
4. 创建动作只有在调用方确实指定目的地址时使用 target，例如新文件路径、新 MCP 连接地址。由内容决定身份的记忆、安装或进程启动不虚构目标。
5. 工具只接收自己支持的目标子集。共享小型形状构造器不等于共享所有目标的巨型 union，也不把现有 `inspect_capability` 扩展为全对象检查器。
6. 保留省略、null、默认值和游标各自的实际含义；不要为接口整齐收窄或扩大行为。会话内容 cursor 已绑定查询范围，续页不重传 target；进程输出 cursor 仍需 process target；调度列表沿现有要求重复过滤条件。

`SKILL` 与 `PATH` 表示不同对象：前者用于能力查询和管理，后者用于文件内容。读取 Skill 正文时从 `skill_path` 构造 PATH；不扩大所有文件工具的目标集合，也不在每个 Skill 结果中重复返回一份文件地址。本方案减少同一对象的字段搬运，不承诺跨语义对象也完全无需转换。

MCP 身份明确区分：`MCP_CONNECTION` 表示安装配置，包含 scope/source_kind/server_id（Plugin 另含 plugin_id）；`MCP_SERVER` 表示运行时实例，只含 runtime_server_id。配置用于管理，运行时目标用于展示，两者均可检查；详情中的 runtime_target 由已有精确关联产生。目录 `list_capabilities.kind=MCP_SERVER` 仍是原有类别过滤，具体对象身份由返回的 target.kind 区分，不扩大目录或产生重复条目。

## 3.1 主要调用形式

```json
{"target":{"kind":"MEMORY","memory_id":"memory:..."}}
{"target":{"kind":"SESSION_ENTRY","session_id":"session:...","entry_id":"entry:..."}}
{"action":"poll","target":{"kind":"PROCESS","process_id":"process:..."}}
{"action":"cancel","target":{"kind":"MONITOR","monitor_id":"monitor:..."}}
{"target":{"kind":"PATH","path":"src/main.py"}}
{"target":{"kind":"ARTIFACT","artifact_id":"artifact:..."},"path":"output/log.txt"}
```

依次用于 memory_get、read_session_content、terminal_process、terminal_monitor、read_file、artifact_export；ID 是展示占位，实际值沿原生 owner 校验。

定时任务切换明确区分源计划和替换方案：

```json
{
  "action":"switch",
  "target":{"kind":"SCHEDULE","task_id":"schedule-A"},
  "expected_revision":3,
  "replacement":{
    "kind":"existing",
    "target":{"kind":"SCHEDULE","task_id":"schedule-B"},
    "expected_revision":7
  }
}
```

创建替换计划时，replacement 为 `{kind:"create",values:完整配置}`。两个现有计划的版本条件保持独立；源计划暂停与替换计划启用仍由现有原子操作完成，不拆成两次调用。`task_id` 仍是调度原生标识字段，`SCHEDULE` 类型将它与本轮排除的 subagent task 区分。

## 4. 不需要增加 target 的入口

| 工具／动作 | 原因与处理 |
|---|---|
| memory_search、search_sessions | 发现集合，不是对一个已选对象操作；查询参数保留 |
| terminal | 创建进程，command/cwd/env 是启动参数；返回 PROCESS 目标 |
| terminal_process.list、terminal_monitor.list | 查询既定作用域列表；不要求模型指定虚构集合对象 |
| remember | 创建新记忆；context_target 表达存储范围，based_on 表达已有依赖 |
| 两类 capability 安装 | 来源内容确定身份；沿已审规格使用 scope/source_path |
| reload_capabilities | 既有全刷新，无参数 |
| todo | 替换当前执行的完整 checklist，调用方不能选择另一个清单；保留 items |
| enter_plan、ask_plan_question、exit_plan | 操作当前 Plan 流程，调用方不能选择另一个流程；保留 reason/question/plan 等内容参数 |

ROOT 已核对 `capability/builtin_catalog.py` 的四个 workflow descriptor、`tools/builtins/todo.py::parse_todo_replacement`、`conversation_kernel/todo_runtime.py` 的 run owner、`primitives/plan_workflow.py` 的解析与 `conversation_kernel/plan_runtime.py` 的现有流程绑定。它们没有缺失的对象选择步骤，新增 target 只会重复运行时已知的当前范围。

## 5. Schema 与模型说明

沿 [已实施的工具清晰化规格](archived_docs/PULSARA_CAPABILITY_SCHEDULING_TOOL_CLARITY_IMPLEMENTATION_SPEC.zh.md) 和 [190 次真实请求实验](output/tool-schema-compatibility-20261026/REPORT.zh.md) 的结论：

- 保留精确 canonical 分支、本地 parser 和现有 OpenAI lowerer；两种协议继续显式 `strict:false`。
- target 使用内联、闭合的对象形状；不引入 `$defs/$ref`、provider 特判或根 request 包装。
- 必要的 action／字段对应关系放在简短英文 function.description。不能只依赖 parameters 根 description，也不能把展平后的 required 当完整业务条件。
- 生产说明只讲如何使用，不包含实验来源、历史解释或内部重构过程。
- 离线形状检查与 token 计量不等于真实 provider 验证；新候选仍须在实施阶段核验参数生成和实际行为。

## 6. 实施与验证顺序

先统一公开形状与精确 parser，再接入现有 owner，最后同步所有可复制结果、runtime observation、SYSTEM／Skill 示例和 UI 消费者。它们构成一次完整提交范围，不能只改模型入参而保留返回裸 ID 的旧使用说明。

保持原生权限、读取范围、进程生命周期、调度事务、版本冲突和结果结算。针对这些真实行为及旧参数拒绝做测试，不用冻结自然语言或 token 数字证明正确。局部 owner 验证后，再检查查询→复制 target→执行→展示的端到端衔接。模型接口实验使用模拟工具、真实 provider，避免为验证 schema 触发真实管理副作用。

## 7. 计量口径与证据

ROOT 从实施前生产 descriptor 枚举 **39 个工具**，明确排除 8 个 subagent 工具，覆盖其余 31 个；这不是某个运行中会话实际可见工具的数量。

使用当前 `PulsaraHeuristicTokenEstimatorV3`，先通过生产 lowerer，再计量完整 Responses tools 数组。实施前全目录为 **15,958 tokens**，排除 subagent 后为 **12,944 tokens**。不含 SYSTEM/messages/MCP 动态直接工具，不是 provider usage。

- [目录及分组基线](output/tool-target-hardcut-20261028/root/inventory.json)
- [基线复算脚本](output/tool-target-hardcut-20261028/root/inventory.py)
- [生产目录 wire 定义](output/tool-target-hardcut-20261028/root/baseline.responses.json)
- [分组共同调研背景](output/tool-target-hardcut-20261028/BRIEF.zh.md)

各组提供自己的候选定义与计量；整体结果以候选合并后的整串计量为准，不用分别舍入的单项差值代替。

| 定义组 | 实施前 | 已审候选 | 候选差值 |
|---|---:|---:|---:|
| Capability / MCP，含不变的 reload | 3502 | 4027 | +525 |
| 记忆 | 1313 | 1502 | +189 |
| 会话内容 | 1126 | 1276 | +150 |
| 终端 | 1585 | 1747 | +162 |
| 文件／artifact／展示 | 2706 | 3023 | +317 |
| 调度 | 1690 | 1863 | +173 |
| todo 与三个 Plan 工具 | 1026 | 1026 | 0 |
| 排除的八个 subagent 工具 | 3015 | 3015 | 0 |
| **39 工具整目录** | **15958** | **17474** | **+1516** |
| **审查范围内 31 工具（含 5 个不变）** | **12944** | **14460** | **+1516** |

本次新调研五组的 20 工具整数组为 **8417 → 9409（+992）**。其余增量来自既有 capability/MCP 方案及两句跨组衔接说明。实际改了 26 份工具定义，其中 23 份参数结构改变；覆盖 31 个工具不等于 31 个都必须改参数。

总目录静态增幅约 **9.5%**，范围内增幅约 **11.7%**。这些数字尚未包含更多 target 出现在结果和观察中的动态成本。例如文件组 50 个路径的样例从 216 到 504 tokens，增加约 288；记忆组三个引用片段从 40 到 62。它们只是指定样例，不是固定每轮开销。

采用统一对象的方案，同时保留本文列出的无 target 入口与角色字段。最明显的收益在跨工具路径：PROCESS→monitor／展示、artifact→读取／导出、会话搜索命中→指定消息读取、capability查询→管理。memory单 ID 与普通文件单路径包装的局部收益较小，但纳入同一复制习惯有整体价值；这是一项明确的设计取舍，尚无新模型实验可证明净认知成本或总调用费用下降。

- [整合后的全部定义](output/tool-target-hardcut-20261028/root/after.responses.json)
- [整合计量](output/tool-target-hardcut-20261028/root/aggregate.measurements.json)
- [整合复算脚本](output/tool-target-hardcut-20261028/root/aggregate.py)

## 8. 跨组衔接的最终合同

| 衔接 | 唯一公开形式／实施边界 |
|---|---|
| 记忆正文、关系、自动召回与偏好 | MEMORY target；保留现有 model_visible_memory_ids 的 exposure 职责，不把它改成另一套操作接口 |
| 会话命中、逐消息展开与历史召回 | 输出完整 SESSION_ENTRY；输入允许省略 session_id 指向当前会话；只改最终模型投影，不把身份包装加进 rerank 文档 |
| 进程操作、monitor与展示 | PROCESS target相同；monitor独立 MONITOR target；target／process_target分别表示监视注册与被监视进程 |
| monitor异步通知与人类停止反馈 | 复用各自已有 provider 投影；不修改原生 canonical、reader、digest 或控制 protobuf；普通呈现与 retained request 使用同一现有投影 |
| Artifact 全部来源 | 既有保存输出指导文本给 `target={kind:ARTIFACT,artifact_id}`；artifact_read结果给target；export给源target和产出file_target；不另造artifact_target镜像 |
| 图片及历史占位 | 当前标签 `pulsara_image.target` 为 IMAGE_REF；历史占位给同形目标；来源 carrier 的 target 表示原 PATH／IMAGE_REF；原生图片字段保持 |
| MCP配置与运行时 | MCP_CONNECTION用于安装配置，MCP_SERVER用于运行时实例；query/parent/result同步区分，目录类别过滤和精确关联不变 |
| Skill正文 | SKILL.skill_path → PATH.path；SYSTEM、inspect说明与实际read_file消费者同步 |
| 调度与会话 | SCHEDULE用于操作计划，SESSION用于归属／过滤；模型当前会话默认不传播到用户级HTTP列表 |
| 已接收定时指令的头部 | 现有 scheduled_request_parts 输出 SCHEDULE target，普通消息与 retained request 共用；原生 queue provenance/source/due time 不变，目标不保证一次性计划仍存在 |

既有 capability/MCP 参考规格保持独立，本整合版明确覆盖其中两句英文（相应 JSON 已计入上表）：

```text
inspect_capability:
Read Skill bodies with read_file using a PATH target built from skill_path.

read_mcp_resource:
If the saved-output guidance supplies an ARTIFACT target, copy it into artifact_read to page the saved result.
```

分别替换该参考规格的 `Read Skill bodies with read_file.` 与 `If the preview supplies artifact_id, continue through artifact_read: it pages the saved result.`；其他已审定文本保持。这里是规格的准确差异，不是要写入模型说明的修改历史。

`retained_skill._exact_historical_read` 同步读取 PATH target 并验证 read_file 的 content_revision。使用真实 ReadFileTool 输出验证 Skill 保留，保留原有 FULL 安装、目录资格和行数检查。

## 9. 实施与验证

六组公开参数、结果、parser、权限取址、runtime observation、压缩消费者、SYSTEM／Skill 示例与 UI 已同步。生产不依赖 output 中的候选文件。没有旧参数 fallback、alias、双读或新增数据库结构；安装前缀继续沿原 cold epoch／compaction successor 机制。

[真实 provider 实验](output/target-schema-2x2-20261028/REPORT.zh.md)使用5工具、7场景。初轮28请求中27个正常返回，全部通过两层schema；5个ID前缀误读在明确完整ID的8次补测中全部正确。一次传输失败保留原始证据。这是有限样本，未测全部工具或长期成功率。

实施后全目录 **17,495 tokens**，范围内31工具 **14,481 tokens**，均较实施前增加 **1,537**。较已审候选增加21：终端Pydantic自然输出kind的type:string；调度复用SESSION形状中的简短ID说明。结构与允许值未改变；总量独立舍入。最终定义及计量见[生产导出](output/target-hardcut-implementation-20261028/root/implemented.responses.json)、[计量](output/target-hardcut-implementation-20261028/root/measurements.json)。

39 = 8个排除工具 + 5个范围内不变工具 + 3个仅说明调整工具 + 23个参数结构调整工具。8个subagent定义与实施前逐值相等，task/profile/历史继承及monitor可见性不改。

验证保留真实执行、参数拒绝、权限、事务、分页、预算、prefix和UI行为；不冻结新schema全文、说明措辞或token数。完整日志与已执行范围见[实施汇总](output/target-hardcut-implementation-20261028/root/REPORT.zh.md)。
