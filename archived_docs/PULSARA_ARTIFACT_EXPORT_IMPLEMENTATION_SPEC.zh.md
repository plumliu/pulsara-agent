# 工具输出 artifact 的直接读取与文件导出

状态：**已实施并通过本轮验证**，2026-09-30。已完成 GPT-6 Astra（xhigh）独立提案、候选批判审阅、实施及最终代码复审；最终无剩余阻塞。验证范围与真实模型样本局限见 [实施验证报告](dogfood_evidence/artifact_export/REPORT.zh.md)。本轮激活不代表普遍效率收益。

## 1. 问题、目标与范围

模型目前通过 `artifact_read(artifact_id, offset_chars?, max_chars?)` 获取大型工具结果的原文。它可以可靠补读小段内容，但面对长日志、大 JSON、筛选、聚合或多份结果关联时，需要把大量无关正文逐页搬进上下文，并自行维护页边界。工具调用、输入 token 和模型手工处理负担因此增加。

本次提供两条互补路径：

- 少量原文补读：保留现有 `artifact_read` 字符分页与成功页 exact FULL 交付。
- 需要本地处理：新增 `artifact_export(artifact_id, path)`，将已保存正文原样创建为一个本地文件；模型自行使用既有 `read_file`、`search_content / find_files` 或 `terminal`，通过现有工具和 Python 等脚本处理。

默认优先 `artifact_read` 分页补读，信息足够即停止；反复分页明显繁琐，或需要复杂提取、聚合、脚本时再选导出。模型可直接导出，不必强制先读取一页。不由 runtime 判定任务“简单／复杂”，不设强制分页次数或导出阈值。已经可见的结果足够时，两者都不必调用。

本版不增加 artifact 搜索工具、行号分页、JSONPath、聚合 DSL、内嵌脚本运行器、自动导出缓存或通用 JSON 结构检测／预览。脚本运行、权限、进程等待、取消与输出保留继续使用现有 terminal 合同。

## 2. 当前事实与复用边界

| 当前 owner／文件 | 已有职责 | 本次连接方式 |
| --- | --- | --- |
| `ports/artifact.py` 与 `PostgresToolArtifactReadPort` | 按 session/workspace 的 canonical ToolResult edge 读取 artifact，核对 blob 字节长度、digest、UTF-8 | 在同一 owner 内增加整份已验证正文读取，供分页与导出共享 |
| `tools/builtins/artifact.py` | 严格参数校验、字符分页、根据 logical FULL 大小缩页 | 保留读取行为，新增窄的导出执行器 |
| `tools/builtins/workspace.py`、`tool_permission.py` | 工作目录相对路径、host-local 写权限 | 新工具使用 `path` 参数并复用相同写权限路径 |
| `tools/builtins/filesystem.py` 与 `local_source_binding.py` | 新文件 no-clobber 原子创建、临时文件清理，以及已有的 no-follow 目录绑定原语 | 为共享 bytes 创建原语补最小的目录 FD 适配；不复制第二套文件发布机制 |
| `primitives/tool_result_projection.py`、`model_input` | 工具结果完整交付要求、provider 输入预算与前缀连续性 | 为导出成功结果声明窄的完整交付合同 |
| `ports/terminal.py` 与 terminal runtime | 通用命令、进程、权限和输出 | 直接处理导出文件，不增加 artifact 专用命令执行协议 |

当前 `ToolOutputSourceFormatHint.JSON` 用于保留工具响应的 JSON 外壳及其中的 `output` 字段，不证明 artifact 正文是 JSON。导出对象始终是 canonical artifact body，不是预览、外壳、首尾拼接或重建的原始远程响应。

不得直接复用 `WriteFileTool.execute(content=...)`：其文本编辑校验会拒绝 NUL 和某些扩展名，而已验证 UTF-8 artifact 正文必须原样导出。复用的是既有路径权限、文件发布和错误语义；不经过模型重写正文，也不复用与编辑产品有关的格式限制。

## 3. 冻结模型 API

### 3.1 `artifact_read`

保持当前参数、默认值、返回形状、字符坐标、大小约束和来源覆盖语义。`max_chars` 是单页请求上限；以准确的 `next_offset_chars` 继续，不能用请求长度推算下一页。成功页完整送达，不允许再次首尾截断、摘要降级或递归生成 artifact。

工具描述增加一条简短选择指引：默认优先本工具分页补读，信息足够即停止；反复分页繁琐或需要复杂提取、聚合、脚本时，可用已提供的 `artifact_export` 创建文件再处理。不要求先读后导出。

### 3.2 `artifact_export`

严格对象 schema，仅接受两个必填字段，不接受额外属性：

```json
{"artifact_id":"从已有工具结果复制的精确 ID","path":"相对或绝对的新文件路径"}
```

- `artifact_id`：与 `artifact_read` 同样的精确句柄规则，不能猜测、拼接、改用 blob ID／工具调用 ID，也不能导出其他会话或工作区的 artifact。
- `path`：新文件路径。相对路径基于 workspace 根目录；绝对路径和 `~` 沿用当前文件写入解析及权限。字段名固定为 `path`，直接复用现有文件权限分类。模型可以选择 `.txt`、`.log`、`.json` 等文件名；扩展名不驱动解析、转换或格式真实性判断。
- 不提供 `overwrite`、`append`、`encoding`、`format`、查询表达式、脚本、自动目录或默认文件名参数。

成功结果固定为小型 JSON，例如：

```json
{
  "status": "success",
  "artifact_id": "原句柄",
  "path": "/absolute/path/to/output.txt",
  "bytes_written": 123456,
  "source_coverage": "RETAINED_SNAPSHOT",
  "source_coverage_reason": "TERMINAL_RETENTION_GAP"
}
```

`path` 必须是当前执行主机上已成功创建的绝对路径，按 JSON 字符串正常转义；不是 shell 命令或 shell 转义串。`bytes_written` 是实际正文 UTF-8 字节数。`source_coverage` 与 reason 从原 artifact record 复制，`COMPLETE` 的 reason 为 `null`。不返回正文预览、自动摘要、额外 digest、内容 revision 或签发式证据 ID。

建议英文描述保持如下语义，可在实施时作等义语言整理：

> Create a new local file containing the exact retained UTF-8 body of a previous tool-result artifact. Copy artifact_id exactly. Relative paths start at the workspace root. Existing paths are never overwritten. Use the returned absolute path with file tools or terminal for searches, filters, aggregation, or scripts. Prefer artifact_read pagination by default; export when repeated paging would be cumbersome or complex extraction, aggregation or scripts are needed. Export preserves the saved source coverage; it does not rerun the original tool or recover output that was never retained.

## 4. 数据真实性与文件合同

1. 数据来源只有通过 canonical session/workspace edge 取得的 immutable artifact body。沿用现有 artifact 的大小、digest、codec 与 UTF-8 校验；`AVAILABLE`／`INCOMPLETE` 可读，其他情况不能发明正文。
2. 输出文件字节与已验证正文完全一致，包括 BOM、CRLF、NUL、多字节字符、空正文和末尾是否换行。不 pretty-print JSON，不解析后重序列化，不追加来源提示、截断标记或工具元数据，不重新运行 sanitizer 改写已保存正文。
3. `COMPLETE` 表示覆盖工具实际观察到的完整输出，`RETAINED_SNAPSHOT` 表示只保存了可取得部分。两者都诚实保存正文；导出完整文件不把后者提升成完整源输出。一次 terminal 的合法增量区间也可为 `COMPLETE`，不能据此声称含有整个进程历史、远端全集或证明后续脚本查询完整。
4. 目标仅能创建为普通文件。已有文件、目录、符号链接（含悬空链接）及并发抢占目标都不覆盖；复用 no-clobber 原子发布。缺失父目录沿当前 `write_file` 规则创建。
5. 导出是普通本地文件写入。授权导出后，副本可见性由本机文件权限决定，不再具有 canonical artifact 的会话隔离或撤销语义；工具按既有临时新文件的私有权限创建，不主动扩大读权限。文件可被后续工具修改、移动或删除，修改后的文件不再代表原 artifact 原文；canonical artifact 不变。重复导出到已有路径，即使内容相同也报已有目标，不暗中认作幂等成功。
6. 文件生命周期由普通文件操作管理。会话结束、Host 重启、compaction、会话归档／删除不自动删除导出的独立副本。不新建副本注册表、TTL、引用计数、后台 GC、自动重建或回执。
7. 路径只承诺当前主机／文件系统视图可访问，不承诺跨机器、容器或不同执行环境可用。共享同一视图的 subagent 可按既有文件权限读取；路径文字不授予新的权限，也不是访问 canonical blob 的旁路。

## 5. 权限、执行及失败

新工具按 `filesystem_write` 分类，非只读；路径权限、确认、hook、物理执行和结算沿用现有文件写工具。直接读取 artifact 的 scope 权限与目标文件写权限同时满足才执行。它可对 ROOT 和具备同一 scoped artifact owner 的 child 提供，不仅因终端暂时不可用而禁用；模型仍可用已有文件工具读取导出文件。READ_ONLY 下 export 按写权限拒绝，`artifact_read` 仍可用；本版不为只读复杂查询开旁路。

不把 host-local terminal 或 Python 当作 artifact 鉴权 owner；脚本只取得获准导出的文件。不给终端数据库凭据、blob 路径或隐式读取任意 artifact 的 CLI。

执行顺序：参数／权限校验 → scoped artifact 读取和完整性校验 → 目标解析及成功响应大小预检 → no-clobber 原子创建 → 确认实际写入结果 → 按现有 tool settlement 发布结果。正文校验失败不得创建目标文件；正文发布前只写入自有临时文件。按普通 `write_file` 合同创建的父目录可能留存，不自动回删共享父目录。发布之后不能因结果提交失败而删除可能已被用户或脚本使用的文件。

| 情形 | 结果与下一步语义 |
| --- | --- |
| 不存在或跨 scope 的 artifact | 与 `artifact_read` 一样返回 `not_found`，不泄露其他 scope 的存在性，不写文件 |
| blob 缺失、digest／长度不符、codec 不可用 | 与现有读取一致的 `content_error`，不重跑源工具、不修补正文、不写文件 |
| 参数非法／权限不允许 | 沿既有参数或权限门返回，不执行导出 |
| 目标已存在或并发出现 | 使用既有 `FILE_ALREADY_EXISTS` 语义；模型选择新路径，不能自动删除或覆盖 |
| 文件系统不支持原子 no-clobber | 沿既有 `ATOMIC_NO_CLOBBER_UNAVAILABLE`，不能退回覆盖式写入 |
| 磁盘满、目录不可写或其他 I/O 失败 | 返回实际文件操作失败；清理未发布的自有临时文件，不声称可用文件已成功创建 |
| 文件已发布但验证／后续结算失败，或取消与发布竞态 | 保留真实副作用事实，沿现有 settlement 表达已创建／可能已创建；不能声称失败就必然没有文件，不自动重试 |
| 调用结果丢失后重试同一路径 | 仍使用普通 no-clobber 规则；不新增 durable 幂等键、恢复 job 或结果回执 |

共享发布原语在异常中保留本次实际是否完成发布的事实，交给现有 settlement 使用。沿现有错误正文的 `error`／`message`／`_hint` 明确“目标未创建”“已发布但未确认成功”或“可能已创建，先检查”，不新增公共 `publication_state` 枚举或副作用状态框架。无法返回工具结果的 Host 崩溃沿既有执行不确定性合同，不虚构可送达的说明。

现有 `_atomic_create_bytes` 使用可变父路径字符串，不能据此声称防住父路径替换。本版的窄适配为：沿现有路径规则确定并授权 canonical 目标，保留原请求叶名检查以拒绝已有符号链接；复用 `local_source_binding.open_or_create_absolute_directory_nofollow` 绑定该 canonical 父目录。临时文件创建、no-clobber 发布、同步、清理及字节／对象验证都基于绑定的目录 FD 与文件 FD，不再次按可变字符串追踪目标。返回前检查公布的绝对路径仍指向所发布对象；如果变化，按已发布／可能已发布的失败结算，不删除不再受本次调用持有的对象。

这是一处共享新文件发布原语的最小适配，保留现有文件工具的模型 API、权限及内容校验，不把整个文件工具系统换成新框架。已存在的父目录别名按原权限解析规则确定实际目标后再绑定；不把包／Skill 来源的词法拒绝策略套到所有用户路径。不宣称能阻止同用户其他进程在调用成功后修改文件，也不宣传为 OS sandbox。

继承现有单 artifact blob 上限、单操作 I/O 边界及真实文件系统资源限制，不新增 artifact 总数、导出总大小、文件寿命、模型调用数或会话时长上限。整份读取不通过循环调用模型分页 API 实现，避免每页重复拉取及校验同一 blob；复用一次有界 blob 读取即可，不为本版新建存储引擎。

## 6. 交付、来源信息与前缀

成功的导出结果必须 exact FULL 交付：路径与来源覆盖共同决定模型下一步，不能压成首尾文本或只留引用。复用现有 `FULL_REQUIRED` 机制，增加局限于此工具成功结果的理由 `ARTIFACT_EXPORT_LOCATION`。如果无法满足 provider 输入预算，沿现有 full-required 路径处理，不以二次截断换取继续调用 provider。失败结果沿既有失败投影语义。

导出响应不含被导出的正文，成功／失败的小型响应都不再生成次级 artifact。将现有纯进程内 `artifact_source_read` 标志一次 hard cut 重命名为 `artifact_inline_result`，语义严格限于 artifact 两工具的有界内联、无 candidate、无附加 footer、非递归结果；不能用它证明“正文已向模型展示”。更新现有 DTO／适配／processor／测试中的同一标志，删除旧名，不并存双字段或加入持久化列。`artifact_read` 保持原行为，export 使用相同非递归路径；FULL 仍由工具身份和结果状态独立分类。源正文绝不当成本次输出再次归档。

发布文件前实际序列化并按既有 renderer 测量完整成功响应及其逻辑交付大小，包含路径控制字符的 JSON 转义、artifact ID 和外层字段，确保满足既有内联正文与 provider-neutral logical FULL 硬界；不能只凭“小元数据”假定可容纳。`artifact_inline_result` 已绕过自动归档阈值，不能把 8,000-byte archive threshold 误作导出响应限制。超界时在写前返回窄的 `resource_boundary`，不截断路径或创建文件后改发摘要；错误正文也须有界，不能原样回显导致超界的长参数。这沿用既有协议大小边界，不新增任意路径长度限制。

上述仅是单结果的写前预检。文件发布后，整次 provider input 仍可能因其他内容碰到预算边界；继续沿既有 full-required 处理，已经创建的文件不因此撤销。

仅因返回路径，不设置源正文的 `model_visible_memory_fact_ids`；保持为空，不声称模型已读过文件或取得了新的记忆可见性。后续文件／terminal 工具沿现行行为，不增加文件 provenance 注册表。

新增工具描述、schema 和新指引只在冷 epoch 或显式采用的 compaction successor 进入工具面。运行中 epoch 不改写 `SYSTEM`、provider `tools` 或历史 `messages`。同 epoch 的新查询／导出结果只作为 suffix 追加；后续文件被删除也不回写历史成功结果。

统一调整 artifact canonical 预览、provider lowering 和 terminal 工具描述中的逐页专用指引：结果足够则继续；默认优先 `artifact_read` 分页；反复分页繁琐或需要复杂提取、聚合、脚本时，可用已提供的 export 导出处理。历史 canonical preview 不重写，已安装旧消息不重写。不能在工具不可用时发出无条件调用指令；用简短的“when available”描述或在当前未安装输入的投影阶段依据既有工具面判定，不引入逐轮重建工具表面。

## 7. 实施落点与存储减法

- `ports/artifact.py`、`conversation_kernel/tool_artifacts.py`：增加最小的整份已验证正文读取接口／值；分页复用同一校验。保留原 scope 查询、canonical edge 和 blob 所有权。
- `tools/builtins/artifact.py`：新增导出工具执行器，严格两个字段，原样 bytes 发布，小型成功结果。
- `tools/builtins/filesystem.py` 与 `workspace.py`：让两种新文件创建共享已有 no-clobber 原语和必要目标检查；不带入文本编辑专属校验或把模型观察窗口误标为已读。
- `capability/builtin_catalog.py`、`conversation_kernel/tool_runtime.py` 与 host 注入：完整接入 descriptor、availability、permission、execution binding、ROOT/child、写 scope、长时工具策略及现有恢复分类。导出按创建本地文件的副作用处理，不按只读 `EVIDENCE_HYDRATION` 处理。
- `primitives/tool_result_projection.py`、`model_input`、`tool_artifacts.py`、`ports/terminal.py`：完整交付分类和一致的调用指引；不增加自动 JSON 推断。
- README 中英文与工具展示：说明两入口、文件保留、来源覆盖、路径可访问边界和已有文件不覆盖；已有通用工具卡片足够时不增加专门 UI。

不增加 schema 表／列、durable event、subject slot、append guard、product relation、durable job、索引服务、内容指纹字段或工具来源证据权威。现有 blob digest 继续只用于真实内容完整性边界。新增工具名、binding 枚举与 delivery reason 仅属于既有闭合目录的必要扩展，不建立新注册框架。

本次保持 `artifact_read` 的产品合同，新增单一 export 路径；不同时保留另一个 `artifact_read(mode=export)`、CLI 导出入口或自动缓存路径。所有新指引与实现一次接通，不能先宣传尚不可执行的工具。

## 8. 验证与激活条件

实现完成前不能把本文状态改为“已实施”。使用仓库根 uv 管理的 `.venv` 做聚焦自动验证，并用保存的真实 provider 配置完成模型使用验证。

### 8.1 必须证明的合同

1. 对普通文本、大 JSON、UTF-8 多字节、CRLF、BOM、NUL、无末尾换行、空正文逐字节导出；正文不混入 preview 或 coverage 提示。JSON 重复键和数字字面值保持原样。
2. `COMPLETE`／`RETAINED_SNAPSHOT` 的来源覆盖不变；未知／跨 session／跨 workspace、已删除源 edge、缺失／损坏 blob 均不产生目标文件。fork 按已有 canonical artifact ownership 规则，不通过路径或 blob ID 猜测扩大 scope。
3. 相对路径基于 workspace；绝对路径、`~`、workspace 外路径在各权限 preset 下与现有写文件一致。拒绝已有文件／目录／符号链接及并发目标，不覆盖；特殊扩展名不触发正文转换。
4. 真实 no-clobber 创建、父路径替换、原请求叶名的悬空 symlink、返回前绝对路径不再指向所发布对象、临时文件清理、磁盘／权限失败及发布后失败路径；取消和结果丢失不得造成自动重复写入或虚假无副作用结论。
5. `artifact_read` 原分页、精确续读及 exact FULL 继续成立；export 成功位置响应完整交付且不递归归档原正文；导出不增加模型已见记忆 ID。
6. ROOT 和 child 工具面、permission gate、catalog 分类、binding、hook、物理执行结算与模型可见提示端到端接通；不存在只加 schema 未接执行的路径。
7. 已安装 epoch 的 SYSTEM/tools 字节相同、messages 前缀保持；新工具仅在允许边界采用。后续导出／读取只追加结果。

### 8.2 模型使用验证

在相同保存模型、相同任务内容下，对照现有分页方案与本版；使用真实工具和已保存 artifact，不在用户提示里指定必须导出或写好查询代码。小型场景矩阵至少覆盖：

| 任务 | 要观察的结果 |
| --- | --- |
| 小段精确补读 | 模型可直接 artifact_read，不被迫创建文件或执行命令 |
| 长日志中定位少量异常并引用上下文 | 自主选导出＋现有工具，找对原文；不重跑源命令 |
| 大 JSON 筛选＋分组统计 | 自主读取结构、使用脚本得出正确结果；不必逐页把全部数据送模型 |
| JSON 工具外壳中的普通日志／无效 JSON | 按导出正文选择处理方式，不把外壳或扩展名当正文格式证明 |
| 多份 artifact 的关联 | 原样导出不同文件，复用一次脚本关联；不引入服务端 join API |
| RETAINED_SNAPSHOT 中搜索零命中 | 回复只对保留正文作结论，不声称整个原始输出均无匹配 |
| 文件已存在／导出无写权限／terminal 不可用 | 识别具体限制，不覆盖、不绕过权限、不无限重复同一调用 |

记录正确性、工具调用数、模型可见正文量／provider usage（区分 cache）、无效参数／重试、路径错误、重跑源工具、覆盖范围表述。小样本只用于发现明显使用障碍，不声称普遍效率或固定节省比例；如果模型仍机械分页，优先修正不一致的指引，不立即扩充新 API。

真实 provider 通过 `LocalSettingsStore` 与有效 Pulsara home 只读加载配置；沿现有 terminal cognitive dogfood 的隔离路径使用经过验证的本地 disposable DB。保留有用的实际输入、输出与错误，只排除真实凭据值，不新增环境凭据 fallback。只在上述合同与真实调用链证据满足后激活。

## 9. 独立审阅与决策记录

用户要求由 GPT-6 Astra、xhigh 先独立提出方案，再获知主线程候选并做批判审阅。本次以无历史上下文的 subagent 执行该流程。阶段一仅提供痛点、当前 API、仓库路径和架构约束；审阅者独立推荐显式 `artifact_export(artifact_id, path)` 加保留分页。随后才披露主线程候选，继续讨论权限、文件路径、交付与失败边界。

独立审阅比较了以下替代路径：

| 方案 | 本次取舍 |
| --- | --- |
| 只加大字符页 | 仅减少翻页次数，复杂处理仍须把大量正文送入模型，不能解决主要痛点 |
| 专门搜索、行号、JSONPath／聚合接口 | 对只读搜索有价值，但需要另外定义匹配分页、解析与查询语义；目前无充分场景要求，暂不增加 |
| 每个 artifact 自动提供文件路径 | 少一次模型调用，但会耦合原工具成功与额外落盘、权限及文件生命周期；本版保留显式导出 |
| 导出工具自动生成默认路径 | 少填一个参数，但要另行冻结目标、审批与重试路径语义；必填 `path` 可直接复用当前文件权限，选择后者 |
| 工具内执行 Python／SQL | 会重复现有执行、权限和取消机制，采用导出后复用 terminal |
| `artifact_read` 增加导出模式 | 混合直接读和文件写入，需按模式分权限；独立导出入口保持职责清楚 |

两轮审阅没有把“独立结论相同”当作验证。审阅实际促成了普通副本可见性／保留、COMPLETE 的观察范围、父目录 FD 绑定、写前真实大小测量、发布后不确定性及非递归标志语义的收敛。最终重读后无未决设计阻塞。实施阶段完成第 8 节的自动验证与真实模型场景验证；各场景直接证明的内容及替代验证边界在实施报告中逐项说明。

冻结的取舍是用一个熟悉的路径参数和一次显式导出调用，换取复用现有文件／terminal 能力及清楚的副作用边界。只读模式仍只有分页；若后续真实任务证明必须提供高效只读搜索，再另行修订产品合同，不能在本版实施时顺手加入。

## 10. 实施与最终代码审查记录

最终实现保留两个入口，未增加 schema、durable event、subject、guard、relation 或 job。较大范围回归 444 项通过；最终修订后的聚焦集 190 项通过，Ruff 与 diff 检查通过，批次数量不累加。

原 Astra xhigh reviewer 对实现进行独立复审，发现最终 FD close 失败可能误报未创建的 P2。已修复并新增故障注入，补充 ROOT/child 实际发布后取消以及 terminal 缺席的执行链路。reviewer 独立重跑最终 export 测试 39 项通过，确认无剩余阻塞。

真实模型同时证明小段可继续直接补读、复杂任务可导出处理；JSON 分组结果更准确，但多份结果关联调用数与耗时更高，因此不把导出写成强制路线或普遍效率保证。完整命令、原始轨迹与局限见 [实施验证报告](dogfood_evidence/artifact_export/REPORT.zh.md)。
