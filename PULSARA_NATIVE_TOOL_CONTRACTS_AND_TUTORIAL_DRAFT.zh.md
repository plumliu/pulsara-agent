# Pulsara 原生工具修订与使用教程草案

日期：2026-11-12。

状态：讨论草案，未实施。本文件记录逐个工具已确定的参数、业务返回值、原生交付形状及 Code Mode 用法，也作为后续 bundled skill 的教程底稿。本轮只写文档，不修改工具、数据库、SDK、提示词或 bundled skill。

## 1. 范围与阅读方式

本文件逐步覆盖 Pulsara 原生工具，以及与这些工具对应的 JS API。每个工具应明确读取或操作范围、参数、返回字段、错误含义和可运行的组合方式，并删除不帮助模型理解或继续操作的冗余内容。

当前确定范围：

| 工具与分支 | 本轮覆盖内容 |
| --- | --- |
| read_file，PATH Target | 参数、完整业务值、meta/content 包装、状态、行号投影、原生与 JS 示例 |
| write_file，PATH + content | 创建语义、参数、简短结果、原生与 JS 示例、错误语义 |
| edit_file，PATH Target | 精确唯一替换、整文件替换、空文件规则、diff 返回、原生与 JS 示例、错误语义 |
| find_files，目录 PATH Target | target 在先的最简参数、Target 内容项、无匹配说明、原生与 JS 示例、扫描失败及取消 |
| search_file_content，PATH Target | target 在先的参数、三种输出模式、匹配行及非重叠匹配次数、object 内容项、原生与 JS 示例 |
| exec_process | 四个启动参数、工具状态与进程状态分离、运行／退出结果、实际输出及原生与 JS 示例；删除 observe 和公开游标；待观察日志透明暂存 |
| manage_process | wait、write、close_stdin、kill 四个动作；内部观察位置、完整待观察增量、控制动作、原生与 JS 示例及输入小实验 |
| query_processes | 可选 PROCESS Target、单查与列表共用的 object 记录、原生与 JS 示例，以及列表无记录与单查未找到的不同状态和说明 |
| search_sessions | query、lifecycle 参数、同域跨工作区范围、entry 优先与标题兜底，以及只含会话信息和命中来源的 object 记录；不附具体 entry |
| search_session_content | target、query 参数，单 ID 的 Q₁ Target 与 QA／QAQ 摘录，四种消息标签及原生／JS 完整返回；删除 include_tools 和搜索续查参数 |
| read_session_content | 可选的原文展开步骤，必填 target、正负 limit；SESSION 从 latest 向前读，单 ID 的 SESSION_ENTRY 从锚点按指定方向读；逐消息 object 及原生／JS 示例 |
| enter_plan | 仅原生、空对象参数、meta.status 与简短文字结果；保留模式切换、自动继续、重复进入成功及单独调用行为 |
| ask_plan_question | 仅原生，question 与可选 options，始终允许自由回答；结果只用 meta.status 和文字 content，10 分钟未作答返回 CANCELLED，由模型决定是否调用 exit_plan |
| exit_plan | 仅原生，可选 plan、summary；提交方案阻塞等待审批，只返回一次最终结果，修订／取消审批／十分钟超时均让当前只读轮继续，由模型决定是否退出，完整图文修订反馈另沿用户输入路径交付一次；批准或无方案退出才恢复权限并自动续接；Markdown 按 summary 命名、默认方案.md、重名用 (2) 顺延，写入失败不阻止审批，实际保存成功才返回顶层 PATH Target；无方案退出不等待审批、不创建文件 |
| remember | 仅 ROOT 原生，context_kind、kind、title、条件必填的 statement；创建独立记忆，返回 Target、updated_at、简短说明及最多三条完整相关候选 |
| query_memory | 仅 ROOT 原生，target 精确读取与 query／可选 limit 关键词查询互斥，target 放首位；返回完整记忆对象，原生沿统一 12k 分页 |
| manage_memory | 仅 ROOT 原生，target、action:edit／delete；edit 提供 statement／context_kind，修改成功和无变化返回 Target、updated_at 及简短说明，删除成功只返回简短说明；不推荐候选；并发旧值由 runtime 自动取得，无快照先提示查询 |

`read_file` 的 NEXT Target 分支和 `write_file` 的 source 导出分支暂不展开。续读时只需传入运行时返回的完整 Target，不公开或解释内部定位。其他工具按后续讨论逐项补充，不从本节推导尚未确定的业务字段。

先按[有序内容统一存储 hardcut](PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md#10-数据库优先与停用期间的分步实施)明确数据库结构与存储基础，允许停用 Pulsara 后先重建新库；随后继续包括历史会话工具在内的全部 native tool 合同讨论，再逐步接通消费者。无需等待全部消费者实现才继续讨论；已定语义保留，未定部分不因建库成为实施合同。

本文件与[统一工具结果交付与分页草案](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)配合使用：该草案定义完整保存、原生 12k 分页、JS 完整返回及 Cell 图文 steer；本文件细化工具本身的合同。这里已明确的工具参数、字段、meta/content 形状及状态表达，以本文件为准。

文中的原生调用与原生消息使用纯文本示意；JavaScript 代码块只表示 Code Mode 脚本，TypeScript 代码块只表示拟议 SDK 类型。示例中的尖括号表示说明占位，不是可直接使用的 Target 或观测值。

## 2. 本轮确定的结果表达

### 2.1 业务结果与原生交付

工具的完整业务值使用以下分工：

| 位置 | 职责 |
| --- | --- |
| target | 实际读取或操作的业务对象；文件工具为解析后的文件，find_files 为搜索目录，search_file_content 为搜索文件或目录，exec_process 和 manage_process 的正常进程结果为 PROCESS Target，exit_plan 方案文件实际保存成功时为该文件的 PATH Target |
| meta | 本次调用状态及工具必要的类型化业务信息 |
| content | 最直接的可读内容，使用有序内容项对象数组 |

query_processes 的进程 Target 只放在 content 的每条 object 记录中，单查与列表都不在顶层重复 target。

不再将业务 JSON 序列化为字符串塞入 `body`，也不再叠加 `pulsara_tool_result` 外壳。meta 中的业务字段保留各自类型，不能把它们全部变成需要脚本重新解析的说明文字。

原生工具消息从同一业务结果投影。正文、业务对象 Target 与 meta 直接呈现；原生页面另外提供完整结果的 `tool_result` Target 和未读内容的 `next`。`tool_result` 用于导出完整结果；`next` 仅用于结果分页。它们不混入 JS 的完整业务值，也不作为 Cell steer 的输出包装。

现有观察信息在原生消息顶层并列呈现：`observed_at_utc` 是实际观察时间，`observation_duration_microseconds` 是实际观察耗时，单位为微秒。二者与 target、meta、content、tool_result、next 同层，不放进 meta、content 或嵌套 observation。未测得耗时不得伪造为 0，仍沿既有观察 owner 表达实际可用事实，不为本次修订增加一套计时机制。

JS 的 `tools.read_file`、`tools.write_file`、`tools.edit_file`、`tools.find_files`、`tools.search_file_content`、`tools.exec_process`、`tools.manage_process`、`tools.query_processes` 成功时直接返回各节定义的完整业务值，不套用原生消息的时间外壳、12k 页预算、tool_result 或 next。数据库内容、JS 值和原生投影来自同一份结果，不保存第二份手工维护的业务正文。

content 的每项统一为 type/value：文字项的 value 是字符串，图片项的 value 是 IMAGE_REF Target，Target 项的 value 是 PATH Target，object 项的 value 是 JSON 对象。find_files 将命中文件保存为 Target 内容项，不并列返回 targets 或重复文字清单；search_file_content 的匹配行和计数使用 object 项。原生工具消息及显式 console.log／return 将 Target 项投影为文字引用，将 object 项投影为通用 JSON 文字，不自动打开、读取或加载其中的文件；只有图片项投影真实图片。普通文字项仍不自动增加分隔符。共同类型见第 2.4 节，投影及准入沿统一交付草案。

这些 PATH 工具不公开文件修订标记，也不接受模型提供的修订条件。读取、创建及结果交付不产生后续编辑资格，不维护已读行区间或通过结果窗口回填资格。edit_file 不要求模型先调用 read_file 或通过特定入口读过全文；文件路径权限与真实交付范围仍各自保持原义。

### 2.2 状态只表达一次

公开调用状态只使用 `meta.status`，枚举固定为 SUCCESS、ERROR、CANCELLED。不再并列输出外层 result_state、顶层 status、正文里的 status:ok 或另一组错误分类字段。

| 状态 | 含义 |
| --- | --- |
| SUCCESS | 本次工具调用成功；不表示模型已看完全部结果 |
| ERROR | 本次调用失败；具体原因、涉及路径及有用提示放在 content |
| CANCELLED | 本次调用已取消，包含派发前取消；实际已发生事实仍如实说明 |

公开状态从真实调用结算结果产生：成功为 SUCCESS，取消为 CANCELLED，其余失败为 ERROR。参数校验、权限、业务效果与取消结算由各工具明确的合同及既有 owner 承担，不把内部分类重新作为公开别名携带。参数或运行时失败与已产生业务结果的失败仍按实际事实处理；没有业务结果时，不为了填满本表而伪造一个业务对象。

状态不兼任交付进度字段。原生页的 SUCCESS 可以伴随非空 next；正常交付完成时的 next:null 不改变原业务状态。业务、保存或交付发生错误时，保留已经确定的业务事实，不用新增多组状态字段代替说明。保存或交付失败沿统一交付草案处理，不重新执行业务。

exec_process、manage_process 的进程状态是独立业务事实：meta.status 表达工具调用是否成功，meta.process_status 与 exit_code 表达命令是否结束及怎样退出。正常启动、观察或完成控制动作并取得结果时，即使进程仍在运行或以非零退出码结束，工具调用均为 SUCCESS，JS 正常返回；不能因命令退出码非零而抛 ToolError。主动 kill 成功也为 SUCCESS，不将目标进程被终止等同于工具调用 CANCELLED。

read_file、write_file、edit_file、find_files、search_file_content 的业务错误沿异常通道返回；在 `catch (err)` 中，err 是 JS 异常对象，`err.result` 是它携带的完整失败业务值，不是枚举或错误代码。确实存在该业务值时，也使用 target/meta/content 结构，meta.status 为 ERROR 或 CANCELLED，全部内容保留且不分页。两个搜索工具的失败业务值包含原因及实际已取得的文件或业务记录，不把它截成程序页。query_processes 同样沿异常通道提供实际存在的完整失败业务值，但按[第 3 节](native_tools/terminal.md#3-query_processes进程清单与元数据)只使用 meta/content，不重复顶层 target。参数或运行时失败若没有业务结果，就不伪造 err.result。不把旧的重复结果 ID、读取指令、可用性布尔值或内部错误分类带回新的公开包装。

### 2.3 错误说明与提示统一进入 content

错误展示使用相同的结果结构，不另造错误正文包装：

| 位置 | 职责 |
| --- | --- |
| meta.status | ERROR 或 CANCELLED，不再细分公开错误枚举 |
| content | 用文字项表达简短、具体的错误原因、涉及路径及有用提示；搜索工具还保留实际已取得的 Target 项或 object 记录 |

删除 meta.error，不增加替代它的公开错误代码字段。具体失败原因直接写入 content，例如说明文件不存在、参数无效、权限不足或执行异常，并写明实际涉及的路径；脚本不依赖另一套 FILE_NOT_FOUND 等公开代码枚举判断分支。文件原文或其他工具真实业务数据中的同名字段不因此被扫描删除。

不再使用 `_hint` 字段，也不把它改名成另一个提示字段。原先 `_hint` 中有用的内容归入 content，可与错误说明合成一个文字项，或按顺序形成多个文字项；需要换行或分隔时由文字项自身提供。删除重复或没有实际帮助的劝告，不要求每次错误都附带操作教程。业务结果不再并列保存一份重复的 message；JS 异常自身的既有 message 仍按异常通道表达，不混作第二份业务正文。

本文 PATH 工具的失败业务值统一只保留 target、meta.status 和 content。文件属性只在各工具明确需要的成功值中作为类型化字段返回；失败时实际已知且有用的文件事实、已发生效果或不确定性写入 content，不在 meta 中保留可选文件属性，不伪造业务结果或已读范围。操作已经完成但保存或交付失败时，说明须保留已发生事实，不能统称为操作没有发生。

### 2.4 共同内容项类型

存储结果、JS 返回值、原生投影及显式 console.log／return 使用同一 type/value 内容项。type 决定如何交付，value 保留真实文字、类型化引用或 JSON 对象：

```typescript
type PathTarget = {
  kind: "PATH";
  path: string;
};

type ImageRefTarget = {
  kind: "IMAGE_REF";
  image_ref: string;
};

type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject;
type JsonObject = {[key: string]: JsonValue};

interface TextPart {
  type: "text";
  value: string;
}

interface ImagePart {
  type: "image";
  value: ImageRefTarget;
}

interface TargetPart {
  type: "target";
  value: PathTarget;
}

interface ObjectPart {
  type: "object";
  value: JsonObject;
}

type ResultPart = TextPart | ImagePart | TargetPart | ObjectPart;
```

各工具声明具体的 content 类型：文件正文、创建说明与 diff 使用 TextPart[]；find_files 使用文字项与 Target 项；search_file_content 按模式声明 object 的精确 value 字段或 Target 项。object.value 的最外层是 JSON 对象，内部允许嵌套对象、数组及 JSON 基本值，不只声明为未知对象，也不新增 match 或 count 内容类型。输入参数中的 target、content 或 MCP 业务字段不因内容项统一而重命名。

Target 项展示引用，图片项交付真实图片。object 的 value 整体按确定字段顺序、固定两空格缩进序列化为标准 JSON 文字，并带结束换行；保留花括号、方括号、引号和字段名，字符串及控制字符按 JSON 规则转义。原生、导出及显式 Cell 输出共用这一规则，保存与 JS 返回仍为完整的原对象。

object 中的 target、targets 或更深层字段均作为普通 JSON 数据递归序列化，不拆出引用、不提升为 Target 项。对象内部的图片引用也不触发真实图片交付，不读取路径指向的文件，不根据字段名猜测媒体或工具操作。

object.value 本身是唯一权威业务对象，完整 JSON 文字按需投影，不再保存一份原始 JSON 备份或独立手写正文。原生按其文字投影的连续顺序进行 12k 分页，跨页片段明确属于正文片段，不冒充完整 JSON 对象；内部定位对模型透明，不生成对象字段游标或程序分片。JS 始终取得完整对象，console.log／return 沿既定 Cell 规则整条投影，不套用原生页预算。

## 3. 工具文档索引

各篇工具文档共同适用本文第 1、2 章的规则。

| 文档 | 工具章节 |
| --- | --- |
| [文件系统工具](native_tools/filesystem.md) | 第 1–5 章：read_file、write_file、edit_file、find_files、search_file_content |
| [终端工具](native_tools/terminal.md) | 第 1–3 章：exec_process、manage_process、query_processes |
| [历史会话工具](native_tools/session_history.md) | 第 1–3 章：search_sessions、search_session_content、read_session_content 的参数、返回类型及原生／JS 示例；单 ID 的 entry 定位、四种消息标签与正负 limit |
| [Plan 工具](native_tools/plan.md) | 第 1–3 章：enter_plan、ask_plan_question、exit_plan 的参数、行为、返回值及原生示例；10 分钟交互期限、方案双份保存及顶层 PATH Target；仅原生，无 JS API |
| [记忆工具](native_tools/memory.md) | 第 1–3 章：remember、query_memory、manage_memory 的参数、互斥分支、返回类型、原生示例及失败语义；runtime 自动取得并发条件；仅 ROOT 原生，无 JS API |
