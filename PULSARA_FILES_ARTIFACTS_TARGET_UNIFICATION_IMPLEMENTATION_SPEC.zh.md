# 文件、Artifact 与展示工具目标统一实施规格

日期：2026-10-28。状态：生产 hard cut 已实施，公开请求、结果及消费者采用本规格。验证与范围见[实施报告](output/target-hardcut-implementation-20261028/files/REPORT.zh.md)；整体结果见[汇总](output/target-hardcut-implementation-20261028/root/REPORT.zh.md)。

## 1. 推荐与范围

推荐将现有九个入口的对象身份统一到闭合 target：`read_file`、`write_file`、`edit_file`、`find_files`、`search_content`、`view_image`、`artifact_read`、`artifact_export`、`open_in_pulsara`。保留工具名与功能边界，保留内容、查询条件、分页、revision、export 目的路径独立。既有库分别负责路径解析、文件修改、ripgrep 查询、图片验证/截图、canonical artifact 读取与 UI 展示；公共解析只取出其需要的值，不建立通用 target 执行器、注册表、存储、指纹或权限凭证。

`PATH` 同时可定位现有文件、查询目录与拟创建路径；它不声明文件类型或存在性。`ARTIFACT` 是会话/工作区绑定的已保存工具输出，`IMAGE_REF` 是已保存图片引用，二者不等同于文件路径。不让任意工具接受全部 target。对象身份统一的收益是跨结果复制；成本是更长的 schema、调用和逐项结果，不承诺 token 节省。

明确排除整个 subagent 编排、profile、权限、消息、记忆暴露、monitor 开放和历史继承链路。`open_in_pulsara` 的 `{type:"task",task_id}` 请求、成功结果和实际导航分支逐值保留；不能以本次统一为由新增 `kind:TASK`、移除 task 展示或改写 task owner。共享文件、artifact、图片基础工具的新公开格式不构成 subagent 编排重构。`PANEL name=tasks` 仍只打开现有 subagent 概览，不扩展其行为。

新定义仅在 cold epoch 或明确采用的 compaction successor 安装。已安装 SYSTEM/tools 不改字节、messages 只追加；不因公开参数改名重写已提交工具调用、旧输出或历史图片消息。不提供旧参数 aliases、双 parser 或迁移期重放。

### SUPERSEDES

本规格仅替代以下文档的模型公共参数、对象引用结果和相应使用示例；原生文件/图片/展示/导出功能、权限、预算与副作用语义仍以本规格所追当前owner为准：

- [本地IO与终端工具清晰化规格](archived_docs/PULSARA_LOCAL_IO_TERMINAL_TOOL_CLARITY_IMPLEMENTATION_SPEC.zh.md) 的本组九工具相关公开形状；terminal本身由终端target规格负责。
- [Artifact导出规格](archived_docs/PULSARA_ARTIFACT_EXPORT_IMPLEMENTATION_SPEC.zh.md) 的artifact_id/path公开交接。
- [图片引用重读规格](archived_docs/PULSARA_IMAGE_REFERENCE_REREAD_IMPLEMENTATION_SPEC.zh.md) 与[本地图片工具规格](archived_docs/PULSARA_LOCAL_IMAGE_TOOL_IMPLEMENTATION_SPEC.zh.md) 的view_image请求及模型引用label。
- [文件展示计划](archived_docs/PULSARA_OPEN_IN_PULSARA_FILE_IMPLEMENTATION_PLAN.zh.md) 的非task公开展示目标；type:task分支不在替代范围。

[Capability统一规格](PULSARA_CAPABILITY_TARGET_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) 不由本组原地修改；其MCP artifact指引和Skill文件读取例子由总规格明确联动，其他管理/查询合同保留。

## 2. 证据与 owner

| 证据 | 当前事实及实施责任 |
|---|---|
| `capability/builtin_catalog.py:400–457,549–617,715–945` | 九工具 descriptor/schema/description；文件与 artifact 权限分类；展示六种分支。 |
| `tools/builtins/filesystem.py:247–430` | 图片 source 精确 XOR parser；读文件、完整输出可见性与 edit observation。 |
| `tools/builtins/filesystem.py:433–662` | 搜索参数闭合、三输出模式、排序/分页、单文件 glob、宽根拒绝与重复查询策略。 |
| `tools/builtins/filesystem.py:674–853,984–1280` | create-only write、revision/seen-range edit、五种 operation、NO_OP、重叠检查。 |
| `tools/builtins/workspace.py:49–140` | 相对路径、`~`、`${PULSARA_HOME}`、实际 read/write scope owner。 |
| `tool_permission.py:418–434`；`conversation_kernel/tool_runtime.py:4291–4318` | 写权限按目的路径 resolve 判断；确认/bypass 才选择 HOST_LOCAL。这是必须同步的安全消费者。 |
| `tools/builtins/artifact.py:43–398` | artifact parser、分页 FULL 预算、export no-clobber、错误/不确定副作用结算。 |
| `conversation_kernel/tool_artifacts.py:116–206,390–580` | artifact 生产、完整/截断 footer、session/workspace scoped 读取、blob 完整性验证。 |
| `conversation_kernel/tool_runtime.py:1885–1905,3428–3700` | ROOT 展示门禁；图片 source 执行、模型 modality/resource allowance、真实引用读取与截图。 |
| `conversation_kernel/tool_execution.py:707`；`runner.py:3393–3428`；`model_input/compiler.py:532–560` | 图片 source parser 的其他入口、来源 carrier 与预算上界，不能仅改工具 execute。 |
| `tools/builtins/presentation.py:20–76`；`tool_runtime.py:4537–4557` | 逐项规范化；外层 schema 失败才拒绝整批，item 错误继续；展示只是 advisory 请求。 |
| `model_input/lowering.py:123–149,877–901`；`llm/input.py:188–195` | 当前图片 label、历史图片占位、artifact retrieval 指引的真正 producer。 |
| `conversation_kernel/compaction/retained_skill.py:185–215,399–450` | 从 read_file 请求 path 和严格结果 payload 识别 FULL Skill；参数与结果一起同步。 |
| `ports/system_prompt.py:39–42`；bundled data-analysis Skill | Skill 正文读取指引；当前 HTML/图片检查说明。 |
| `frontend/lib/presentation-targets.ts`、`components/presentation-provider.tsx` | UI 解析、Host 精确绑定、逐项预备/展示、取消、最后选择与手动重开。 |
| `frontend/lib/builtin-tool-summary.ts:130–185`、`runtime-adapter.ts:5528–5535`、`components/workbench-view.tsx:338–353` | 参数/结果 path、files 数量、Skill 追踪、diff 展示消费者。 |

上述相对路径均以 `src/pulsara_agent/` 为 Python 文件前缀。证据只陈述当前代码，不把历史实现当保留理由。必读的 capability 统一规格、已实施工具清晰化规格及 190 请求报告支持继续使用现有 lowerer、strict:false 和精确本地 parser；不证明本候选已通过 provider。

## 3. 精确 target 合同

以下对象均 `additionalProperties:false`；身份字段必填且非空字符串，null 不表示缺省。ID 的现有 native 校验继续由原 owner 执行，不凭 schema 的宽字符串接受性授权。

| target | 形状 | 消费入口 |
|---|---|---|
| PATH | `{kind:"PATH",path:"…"}` | 五个文件/查询工具、view_image、open_in_pulsara |
| ARTIFACT | `{kind:"ARTIFACT",artifact_id:"artifact:…"}` | artifact_read、artifact_export |
| IMAGE_REF | `{kind:"IMAGE_REF",image_ref:"image:…"}` | view_image |
| URL | `{kind:"URL",url:"https://…"}` | open_in_pulsara |
| PROCESS | `{kind:"PROCESS",process_id:"…"}` | open_in_pulsara；与 terminal 组共享公开形状 |
| MCP_SERVER | `{kind:"MCP_SERVER",runtime_server_id:"…"}` | open_in_pulsara；复用 capability runtime 目标 |
| PANEL | `{kind:"PANEL",name:"capabilities|tasks|background|browser"}` | open_in_pulsara |

PATH 不加 file/directory/new_path 子类型：同一路径的真实状态由执行当时的 owner 判断，子类型既增加模型选择又可能过时。仅写路径引用也不保证可读/可写。相对路径继续以调用工具已绑定 workspace 为基准；结果沿现行 `_relpath` 规则返回 workspace 内相对、外部绝对路径；展示规范化依旧得到不提前 resolve symlink 的绝对访问路径。

IMAGE_REF 的命名明确指向既存 canonical 引用。它不是 PATH 的 alternate 拼写，也不表示一个可修改的媒体文件。模型不得从路径、哈希或 URL 自造 image_ref；引用仍只能在既有 workspace/session read owner 的资格下读取。IMAGE_REF 不带 session、workspace、mime、尺寸或资源预算。

PROCESS 不带 host_session_id；真正路由仍由调用 Host 注入，不能从 active UI 猜 Host。MCP_SERVER 只表示运行时实例；不接受 MCP_CONNECTION 安装目标或其 scope/source_kind/server_id 字段，也不接受远端工具目标。URL 的 http(s)、凭据拒绝和 PANEL enum 继续由原生 normalize 校验。

### 3.1 不属于 target 的参数

- `base_revision` 是乐观并发 guard；`content_revision` 是结果观察。两者不能嵌入 PATH target，也不能给 target 新增 digest。
- read 的 offset/limit 是行页；search 的 offset/limit 是结果页；artifact 的 offset_chars/max_chars 是保存文本字符页，三套不可互换。
- pattern/glob/file_glob/output_mode 是查询参数；operations/content 是本次修改内容。
- `artifact_export.path` 是本次副作用的目的路径，主 target 仍是源 ARTIFACT；不改成 PATH target、不建立 source_target/destination_target 双重通用包装。成功后目的文件会产出新的 PATH target。
- `write_file.target` 指拟创建的位置；没有已有对象不妨碍路径成为选择对象。这与按 artifact 身份导出到另一个路径的双角色不同。

### 3.2 缺省与 null

read_file.target、write_file.target、edit_file.target、view_image.target、artifact_read.target、artifact_export.target 必填。find_files/search_content 的 target 可省略，唯一含义仍为 workspace 根 `.`；一旦提供，kind/path 必须齐全，`target:null`、`target:{}`、省略 target.path 都拒绝。不使用 `default:{kind:PATH,path:"."}` 让 provider 自己补值；由 parser 处理既有缺省。

read_file offset=1、limit=2000，limit 上限2000；search offset=0、limit=50，上限1000，output_mode=content；artifact offset_chars=0、max_chars=20000，上限32000。所有这些省略保持默认，显式 null、bool 冒充整数保持非法。不新增总文件数、任务数、历史长度、调用次数或时间上限。现有 read 100000 字符、搜索单操作 timeout/重复查询行为和图片物理边界原样保留。

## 4. 全分支前后调用与行为

以下 `R` 代表从实际 read_file 复制的完整 revision，`A`、`I` 为实际产出 ID；示例不声称这些占位标识可执行。完整最小正例见 [examples.json](output/tool-target-hardcut-20261028/files/examples.json)。

| 工具/分支 | 旧调用 → 新调用 | 必须保留 |
|---|---|---|
| read_file 行页 | `{path:"a.py",offset:2,limit:3}` → `{target:{kind:PATH,path:"a.py"},offset:2,limit:3}` | UTF-8/BOM、新行、N\|text、完整行 observation、越界页、truncated hint；过大的完整结果不授予 seen-range。 |
| write_file 新路径 | `{path:"new.py",content:""}` → `{target:{kind:PATH,path:"new.py"},content:""}` | 空内容合法；父目录创建；已有文件/目录/leaf symlink 不替换；原子 no-clobber；publication 不确定时不谎报未写。 |
| edit replace_lines | `{path:"a.py",base_revision:R,operations:[{kind:replace_lines,start_line:2,end_line:3,lines:["x"]}]}` → 只将 path 包为 PATH target | 原 revision 的闭区间；已读范围；lines 非空，每项不得有 CR/LF/NUL。 |
| edit delete_lines | 同上，operation 为 `{kind:delete_lines,start_line:2,end_line:3}` | 删除原范围，不接受 lines 字段。 |
| edit insert_before | operation `{kind:insert_before,line:2,lines:["x"]}`，path → PATH target | 锚点行已读；原 revision 第2行前 gap。 |
| edit insert_after | operation `{kind:insert_after,line:2,lines:["x"]}`，path → PATH target | 同上，行后 gap。 |
| edit replace_file | operation `{kind:replace_file,content:"new\n"}`，path → PATH target | 必须唯一 operation；需要当前 revision 和 read observation，但不要求全文显示；混合换行仅此分支可修改。 |
| find_files 默认根 | `{glob:"*.py"}` → 原样 | target 不必为了格式统一强制出现；保留 basename/relative glob 语义。 |
| find_files 文件/目录 | `{path:"src",glob:"**/*.py",offset:50}` → `{target:{kind:PATH,path:"src"},glob:"**/*.py",offset:50}` | 单文件也过 glob；路径排序，继续相同查询与 offset；宽外部根拒绝。 |
| search content | `{path:"src",pattern:"TODO",output_mode:"content"}` → path 包为 PATH target | 返回 matching lines、line/content；排序 path,line；截取单行500字符仍不授予编辑观察。 |
| search files_only | `{pattern:"TODO",output_mode:"files_only"}` → 原样；可选 target | 返回每文件 target；offset 计文件。 |
| search count | `{pattern:"TODO",output_mode:"count",offset:50}` → 原样；可选 target | count 是匹配行数；total_count 仍是全部匹配行总数；offset/分页仍按文件，不能改成行数。 |
| view PATH raster | `{path:"plot.png"}` → `{target:{kind:PATH,path:"plot.png"}}` | PNG/JPEG/static WebP；regular file、大小、decode、modality 与 allowance；不负责打开 UI。 |
| view PATH SVG | `{path:"plot.svg"}` → PATH target | 原尺寸/viewBox 比例、不裁剪、脚本和外部资源禁用、截图 owner。 |
| view PATH HTML | `{path:"report.html"}` → PATH target | HTML冻结资源、1200×800/light/reduced-motion/network-off，当前静态截图，不冒充交互验证。 |
| view IMAGE_REF | `{image_ref:I}` → `{target:{kind:IMAGE_REF,image_ref:I}}` | workspace/session reference read； unavailable/resource_exceeded 真实区分；不回读原路径或抓网络。 |
| artifact_read 初页/后页 | `{artifact_id:A,offset_chars:8000,max_chars:8000}` → `{target:{kind:ARTIFACT,artifact_id:A},offset_chars:8000,max_chars:8000}` | 页可能因 FULL 字节预算缩短；按 returned next_offset_chars 续页；越尾空页；保留来源覆盖、memory provenance。 |
| artifact_export | `{artifact_id:A,path:"saved/output.txt"}` → `{target:{kind:ARTIFACT,artifact_id:A},path:"saved/output.txt"}` | 无需先read；导出准确保存 UTF-8 body；非preview拼接；terminal是保存的plain output；不存在则创建、不覆盖。 |

edit 的多 operation 仍整体校验后一次修改：range 重叠、重复 gap、插入落在替换内部、未读行、revision 已变均拒绝；它不是逐操作 partial success。阶段前后再次比对 bytes、原子替换、写后校验、changed_windows 授予的新观察均保留。write_file 返回新 revision 不自动授予可编辑观察，仍清除旧 observation。

read/search/write/edit 现有错误码包括 FILE_NOT_FOUND、READ_OUTPUT_TOO_LARGE、SEARCH_ROOT_TOO_BROAD、CONTENT_REVISION_MISMATCH、READ_OBSERVATION_REQUIRED、OVERLAPPING_OPERATIONS、INVALID_LINE_RANGE、NO_OP、FILE_ALREADY_EXISTS、ATOMIC_NO_CLOBBER_UNAVAILABLE、FILE_PUBLICATION_UNCONFIRMED；输入包装变更不合并这些具体结果。artifact not_found 与 content_error（缺blob/descriptor/size/digest/codec失败）保持不同；保存源为 RETAINED_SNAPSHOT 时不能从翻页/导出恢复已经丢失的原始输出。

### 4.1 展示的全部分支与 partial success

| 旧 targets item | 新 targets item | normalize/真实展示语义 |
|---|---|---|
| `{type:file,path:"a.md"}` | `{kind:PATH,path:"a.md"}` | 接受本地路径，拒绝URI；不检查存在、不读文件、不提前解析symlink；UI既有preview owner实际读取并报告结果。目录仍由既有preview行为决定，不新增文件树产品。 |
| `{type:browser,url:"https://…"}` | `{kind:URL,url:"https://…"}` | trim、完整http(s)、合法port、无userinfo/反斜线/host空白；拒绝错误URL不回显其中可能有的凭据。UI浏览器owner保留独立策略。 |
| `{type:terminal,process_id:"p1"}` | `{kind:PROCESS,process_id:"p1"}` | 本地Host绑定；normalize不承诺进程仍存在，UI按真实状态显示。 |
| `{type:mcp,runtime_server_id:"r1"}` | `{kind:MCP_SERVER,runtime_server_id:"r1"}` | 本地Host绑定；不能把安装server_id当runtime ID。 |
| `{type:panel,name:"capabilities"}` | `{kind:PANEL,name:"capabilities"}` | 原概览入口。 |
| `{type:panel,name:"background"}` | `{kind:PANEL,name:"background"}` | 原后台入口。 |
| `{type:panel,name:"browser"}` | `{kind:PANEL,name:"browser"}` | 空白tab；同批重复复用一个tab，不同调用新建。 |
| `{type:panel,name:"tasks"}` | `{kind:PANEL,name:"tasks"}` | 仅改变公开panel标记；原subagent概览行为不变。 |
| `{type:task,task_id:"task:…"}` | **原样保留** | exact task ID校验、组导航与选择、原失败行为均不改；不增加kind:TASK分支。 |

外层仅 `{targets:[…]}`，非空数组，无总长新增上限。ROOT门禁不变。保留 `_validate_builtin_call_schema` 对 targets 元素错误的特例：空数组、未知顶层字段、错误数组类型拒绝整调用；某项为null、字段不合、错误路径/URL等只记录 `{index,message}`，其余按原顺序继续。全部失败仍是成功执行的规范化请求结果、targets为空且有errors，不改成系统错误。最后接受目标选中，重复/取消/后到请求不能抢夺较新用户交互。

公共成功结果中非task项采用 `{target:<纯目标>,host_session_id?:<现有绑定>}`；只有 PROCESS/MCP_SERVER 有host字段。task项完整保留旧 `{type:"task",task_id}`。例如：

```json
{"targets":[{"target":{"kind":"PATH","path":"/project/a.md"}},{"target":{"kind":"PROCESS","process_id":"p1"},"host_session_id":"host:existing"},{"type":"task","task_id":"task:existing"}],"errors":[{"index":1,"message":"请输入不含凭据的完整 HTTP(S) 地址"}]}
```

模型复制非task项的 `target`；不能把host字段送回请求。前端 `parsePresentationResult` 在边界转换新非task结果到现有内部 PresentationTarget DTO，task项按原规则接收；`PresentationProvider` 的task分支与任务服务不改。这个适配是公共请求与现有UI owner的职责边界，不是兼容旧非task公开结果的双读。保留原始工具消息可读，不承诺旧非task结果的交互重开。

## 5. 结果 producer 与可复制链

### 5.1 文件和搜索

read/write/edit成功及已明确路径的结构化错误，把原 `path` 替换为 `target:{kind:PATH,path:<原公开path>}`，不并列保留path镜像。metadata内部path、diff中的a/b路径、真实副作用 `files_modified` 不因统一外观机械改名；后者仍是副作用报告，而非第二个当前对象。content_revision/base_revision、access_scope/workspace_relative、统计、changed_windows、hint都独立。

- find_files和search.files_only：旧 `files:["a.py"]` → `targets:[{kind:"PATH",path:"a.py"}]`。不再给每个纯目标套 `{target:…}` 空壳。
- search.content：旧 `matches:[{path,line,content}]` → `matches:[{target:{kind:PATH,path},line,content}]`。
- search.count：旧以路径为key的 `counts:{"a.py":2}` → `counts:[{target:{kind:PATH,path:"a.py"},count:2}]`，保持路径排序、每文件统计和total_count含义。不能把count或line塞进target。
- 搜索根若需结果指明，复用请求或内部metadata；不为了输出完整性新添一个冗余root target。零匹配保持空列表。

复制链：`find_files → result.targets[0] → read_file.target → result.target → edit_file.target`；只把读出的content_revision另放base_revision。`write_file → result.target → open_in_pulsara.targets[0]`。相同 target 可交给适用工具，但不因此绕过文件类型、read observation或权限。

### 5.2 Artifact

`tool_artifacts._complete_footer/_omission_marker` 与 `model_input/lowering._saved_tool_output_guidance` 将裸 `artifact_id=…` 改为一次可复制 JSON：`target={"kind":"ARTIFACT","artifact_id":"artifact:…"}`。完整、head-tail、ARTIFACT_REF、omitted四类投影均使用同一公开形状；仅确实保存成功才给target，不能用预期ID冒充可读artifact。完整/retained覆盖和offset指引仍独立。

artifact_read 成功/not_found/content_error/resource_boundary 以及其他已有明确ID的结果，将顶层artifact_id替换为target；不会把分页内容包在target。export成功返回 `target:ARTIFACT`（本次源）与 `file_target:PATH`（已确认导出文件）替代原artifact_id/path，不保留镜像；bytes_written/source_coverage不变。这两个对象有不同真实角色，不是无用双ID。尚未确认publication的错误不能给一个看似已生成的file_target，保留“检查请求目的路径再决定重试”。

```json
{"status":"success","target":{"kind":"ARTIFACT","artifact_id":"artifact:existing"},"file_target":{"kind":"PATH","path":"/project/saved/output.txt"},"bytes_written":123,"source_coverage":"COMPLETE","source_coverage_reason":null}
```

复制 `target` 继续artifact_read，复制 `file_target` 给read_file/search_content/open_in_pulsara。artifact范围仍由PostgresToolArtifactReadPort的当前session/workspace join决定，数据库artifact_id/blob_id列、内部ToolArtifactRecordView和引用派生ID不重命名、不新增对象。

artifact_read 的二分截短、export副作用前 `_export_payload_fits` 必须使用新实际payload；FULL预算和canonical preview硬界不能沿旧路径长度估算。若新包装使metadata本身超限，沿现有typed resource_boundary返回，export不得先创建文件。禁止靠增加hard bound或自动递归artifact归档解决包装成本。

### 5.3 图片与历史引用

当前图片由 `image_reference_id_part` 给模型的标签改为：

```json
{"pulsara_image":{"target":{"kind":"IMAGE_REF","image_ref":"image:existing"}}}
```

后续调用直接为 `{"target":{"kind":"IMAGE_REF","image_ref":"image:existing"}}`。`image_reference_text` 的历史占位也明确给同一个target，保留mime/宽高以及“此处未包含图像内容”。`session_text.prompt_history_text` 和相关session读取复用该renderer；跨workspace占位仍不得暴露可复用引用。与session组同步read_session_content说明和测试，不能只改view_image参数。

`tool_image_source` carrier 的 source改为 `{tool_call_id,target:<请求PATH或IMAGE_REF>}`；这是来源身份，`pulsara_image.target` 是产出的图片身份，二者不混为一个target。其真实producer在`model_input/compiler`；`runner`预留上界与`ViewImageSource.provider_value`同时改；quote owner要计新增包装的logical/wire/token。内部LLMImagePart.image_ref、图片descriptor、DB、protobuf和媒体读取route保持原值。view_image success依旧是带图片的FrozenPromptContent，不为了返回JSON重新实现图片结果。

compaction说明要求保存已见完整IMAGE_REF target而非构造ID；已安装旧消息不改。保留的旧裸image_ref只是历史值，模型可以在新调用中使用明确kind包装，不新增旧请求兼容parser。当前候选尚未跑图片真实provider验证。

### 5.4 Skill 内容读取

capability的 `{kind:SKILL,skill_path}` 表示能力对象，不是PATH；保持read_file仅接受PATH。SYSTEM及inspect说明应明确“读Skill内容时，以其skill_path作为PATH target.path”。不添加重复file_target到每个capability结果，不让edit/write无条件接受SKILL。list/inspect/manage仍原样复用SKILL target；跨到文件内容时这一次语义明确的转换有必要。

`retained_skill` 从 `call.target.path` 与 `result.target.path` 取值，保留 FULL 安装、请求结果关联、offset=1、完整行数和历史目录资格检查。严格结果白名单接受实际 read_file 返回的 content_revision，并校验既有 revision 格式。真实 ReadFileTool 结果到 retained Skill 的链路已验证；不重算 digest、不增加激活资格。

## 6. 权限、硬切和消费者实施点

1. builtin_catalog内只改九工具公开schema/description，内联各工具所需闭合target子集。继续生产lowerer、strict:false；view_image将原schema无法表达的path XOR image_ref改为target内union，parser依旧精确验证。没有root request包装或$ref路线。
2. filesystem原owner读取target.path；read/write也拒绝旧path与未知字段，search保留省略target默认根；edit现有严格keys改为target/base_revision/operations。解析不能通过构造旧公共JSON再交旧parser形成双路径；内部`Path`和原生operation DTO保持。
3. 权限 `_filesystem_write_access` 必须按工具分支取有效写路径：write/edit取target.path，artifact_export仍取顶层path。不能拿ARTIFACT当写路径或看到target就统一放行；原resolve、workspace/outside policy、READ_ONLY/ASK/BYPASS与执行write_scope保持。
4. image parser、physical reader、tool_execution、tool_runtime、runner预算、compiler来源carrier、lowering/历史占位及quote owner一起硬切。不要仅更新descriptor导致资源admission拿不到来源。
5. artifact read/export原native port签名保持；只改公开解析/结果renderer、footers和retrieval指引；现有FULL预算必须按新实际序列化计算。
6. presentation逐项parser支持新非task形状及唯一原task豁免；不得保留旧file/browser/terminal/mcp/panel别名。结果parser生成原UI DTO，UI Host校验不能从当前会话补齐，advisory展示不得新增durable delivery/retry。
7. 前端builtin-tool-summary和runtime-adapter从result.target.path/args.target.path读取文件身份，image从args.target.kind区分；search按output_mode/新targets列表显示计数。workbench traceSkill读取target.path；diff、file preview、原生file-reference和用户拖拽协议保留。
8. Hook通用tool_input随新公开参数输出，不补旧path别名；matcher工具名及原Hook执行语义不变。`ports/system_prompt.py`的Skill正文指引、`compaction/prompt.py`图片引用文字、bundled Skill HTML检查例子、相关dogfood脚本与测试调用同步。capability spec中read_mcp_resource的“preview supplies artifact_id”需由ROOT联动改成“preview supplies an ARTIFACT target”，文件组不修改该文档。
9. 更新当前有效规格与必要clean-v0测试输入，不改DB schema。历史工具参数/结果不重写，不加入legacy reader、转换注册器或兼容执行入口。全程不增加durable events、subjects、guards、relations、jobs、总量caps或新fingerprint。

## 7. 离线成本与认知取舍

已用仓库 `.venv`、生产descriptor、现行lowerer和 `PulsaraHeuristicTokenEstimatorV3` 离线生成 [canonical](output/tool-target-hardcut-20261028/files/after.canonical.json) 与 [完整Responses定义](output/tool-target-hardcut-20261028/files/after.responses.json)。[复算脚本](output/tool-target-hardcut-20261028/files/design_schema.py)、[机器计量](output/tool-target-hardcut-20261028/files/measurement.json) 和正反例均保存；无生产执行/真实provider调用。

| 工具 | 原Responses tools数组 | 只改结构 | 结构+候选正文 | 最终差值 |
|---|---:|---:|---:|---:|
| read_file | 222 | 257 | 248 | +26 |
| write_file | 146 | 182 | 188 | +42 |
| edit_file | 621 | 657 | 643 | +22 |
| find_files | 209 | 253 | 280 | +71 |
| search_content | 279 | 323 | 345 | +66 |
| view_image | 228 | 277 | 262 | +34 |
| artifact_read | 218 | 252 | 250 | +32 |
| artifact_export | 238 | 272 | 231 | −7 |
| open_in_pulsara | 548 | 568 | 581 | +33 |
| **九工具整数组** | **2706** | **3037** | **3023** | **+317** |

只改结构多331；候选正文合计收回14。单项舍入不能代替整数组。不是全部catalog、SYSTEM+messages，更不是provider实收usage。结构候选和文本候选分开量化，不能把export文案压缩的收益冒充target本身节省。

请求例 `{path:"src/example.py"}` 估算7→13 tokens、25→50 UTF-8 bytes。50个文件结果从 `{files:[字符串…]}` 到 `{targets:[PATH…]}` 估算216→504 tokens、861→2013 bytes，额外约288 tokens/页。最初 `{files:[{target:PATH}…]}` 的无用逐项空壳已去掉；仍必须承担每个PATH的kind/path结构。search.count/content的结果也会增加，生产预算不得漏算。

收益假设：文件结果可直接复制给读/写后展示；artifact footer可直接复制给读取/导出；terminal PROCESS和capability runtime MCP_SERVER直接展示；图片引用与路径不会靠两个可选字段进行XOR。仍保留的必要认知：五种edit operation、三套分页单位、revision之外read observation、source与export目的地、advisory展示不代表完成，以及task范围豁免。暂无首次成功率、推理token或端到端延迟的实测改善。

**明确取舍：接受本组+317定义成本及逐文件结果成本，优先实现可复制身份；保留search省略target的便捷性，拒绝给无对象查询或目的路径机械包装。** 若只做参数包装而不改搜索结果、artifact/footer和图片占位，收益不足以支持此成本，不推荐这种半成品。

## 8. 必要验证与完成条件

本轮仅27个正例通过候选canonical与真实lowerer后的wire；70个反例被canonical拒绝。open_in_pulsara item反例的schema拒绝不是整批执行拒绝，实际partial admission须独立验证。上述离线样例不证明 native 资格或 provider 行为；生产 parser 和消费者的实际验证见实施报告。

实施时按已有测试owner验证：

- 各工具必填/省略/null/未知字段/旧flat参数/错误kind；read/search分页、文件与目录、外部宽根、Unicode与相对路径；不为测试放宽断言。检验find/search每种结果target可直接用于read；counts分页单位不变。
- 五种edit operation，原revision/重复gap/重叠/未见范围/换行/BOM/空文件/NO_OP/同时变更；write已有路径/symlink/race no-clobber/不确定publication；真实read结果驱动retained Skill而非手写不一致fixture。
- 写权限workspace/外部路径/确认/bypass/read-only矩阵，artifact_export权限始终检验目的path；metadata/path解析和执行同一目标。
- artifact完整/retained/不可用/损坏/not_found，char页和FULL字节页缩短、越尾空页，export准确字节/不覆盖/副作用前预算/不确定结果；新footer和artifact target复制链。
- view PATH raster/SVG/HTML/IMAGE_REF，引用workspace资格、模态不支持、图片错误、resource allowance、来源carrier和quote不小于真实输出；图片label/历史占位能直接复制。
- open六类形状及四panel值、旧task保留、混合合法非法item、零合法项、外层拒绝、ROOT门禁、host绑定、取消/用户交互/最后目标/空白tab重复、UI读取/展示失败不冒充执行失败。
- frontend摘要、Skill trace、文件结果展示、presentation parser及task原行为；native file-preview/export route不随公共工具改名。
- 已安装prefix不可改；只在合法epoch安装新定义；新当前消息和必要compaction successor采用新引用文本，旧已安装消息原样留存。

优先复用 `test_search_tool_split.py`、现有文件编辑测试、`test_artifact_export.py`、`test_round1_tool_output_artifact.py`、`test_round7_1_provider_visible_tool_result_projection.py`、`test_local_image_tool_executor.py`、`test_kernel_image_input_k*.py`、`test_image_history_recovery.py`、`test_open_in_pulsara.py` 与frontend presentation/builtin摘要测试。验收不冻结整份英文文案、token数字或JSON文本快照；检验实际参数、效果与预算。

冻结后再通过已有模拟工具harness对两个真实Responses连接做针对性填参复核：复制PATH、IMAGE_REF、ARTIFACT、PROCESS/MCP_SERVER；export目的path；search省略target；edit guard；混合展示item与task豁免。仅模拟工具、无真实文件副作用，沿现有保存配置与凭据边界。本轮未跑，不以190请求历史结果替代新shape验证。

## 9. 精简英文正文候选

完整逐工具文本与schema一致，见 [descriptions.json](output/tool-target-hardcut-20261028/files/descriptions.json)；不能只在parameters.description说明分支。以下列出关键签名便于审阅，细节保留在附件：

```text
read_file: PATH target; optional offset and limit. Returns content_revision for edit_file.
write_file: PATH target and content. Create only; the path may not exist yet.
edit_file: PATH target, base_revision and operations. Keep revision and operations outside target.
find_files: glob; optional PATH target, offset and limit. Omit target for the workspace root.
search_content: pattern; optional PATH target, file_glob, output_mode, offset and limit.
view_image: one PATH or IMAGE_REF target. Pixels are for the model; open_in_pulsara shows files to the user.
artifact_read: ARTIFACT target; optional offset_chars and max_chars. Continue with next_offset_chars.
artifact_export: ARTIFACT target and destination path. Export the saved body to a new file.
open_in_pulsara: targets containing PATH, URL, PROCESS, runtime MCP_SERVER or PANEL; Use {type:"task",task_id} for a subagent task. Invalid items fail independently.
```

原生衔接、预算、权限与展示已完成聚焦行为验证。此次真实 provider 小样本未覆盖文件与图片工具，不据此宣称全部工具生成成功率。
