# Pulsara 本地文件、呈现与终端工具说明修订实施规格

作者：ROOT agent（GPT-6 Astra）。日期：2026-10-26。状态：已实施，2026-10-27 完成代码与验证。本规格与另外三份 TOOL_CLARITY_IMPLEMENTATION_SPEC 合起来覆盖修改前 40 个内置工具，最终合并为 39 个；本文件负责其中 12 个。

## 1. 产品目标与选择

模型使用这些工具，需要识别操作对象、填写该操作的参数、理解结果接下来能做什么。本次让这些信息就近、明确地出现一次。类型与值域由 schema 表达；动作对应关系、数据含义与观察范围用短句表达。工具名称、动作、字段、默认值、参数合法集合、执行效果与权限保持现有契约。

本组保留现有工具数量和划分，不拆分 terminal_process，不把动作重新命名，不增加查询/分发工具。采用当前展平协议，压缩正文和 schema annotation；这不是内嵌 oneOf 或共享字段约束方案的实施。模型可见英文只描述工具本身，不包含实验来源、供应商观察、历史修订理由。

与前一版 619-token terminal_process 草案相比，本版进一步删除通用行为告诫：诸如不因用户停止而重启、不要猜测 handle、不应为了日志重跑等，不再写成重复的工具说明。保留真正决定调用的事实：write/submit 字节差异、cursor 来源与范围、输入后的等待、何时有未来更新、覆盖文件需要哪个工具。出现错误时沿用现有具体结果，不新增“提醒层”。

## 2. 本组工具与计量

用仓库 `.venv`、当前生产 catalog、原 OpenAI function lowerer 和 `PulsaraHeuristicTokenEstimatorV3.estimate_json`，计算 Responses tools 数组。表内每行是一个单工具数组；分组/全目录独立计数，不使用逐行舍入数相加。

| 工具 | 修改前 | 已实施 | 减少 |
|---|---:|---:|---:|
| artifact_export | 460 | 238 | 222 |
| artifact_read | 562 | 218 | 344 |
| read_file | 471 | 222 | 249 |
| find_files | 235 | 209 | 26 |
| search_content | 312 | 279 | 33 |
| write_file | 276 | 146 | 130 |
| edit_file | 926 | 621 | 305 |
| view_image | 408 | 228 | 180 |
| open_in_pulsara | 771 | 548 | 223 |
| terminal | 779 | 328 | 451 |
| terminal_process | 1154 | 562 | 592 |
| terminal_monitor | 1232 | 695 | 537 |

本组：**7580 → 4290**。仅替换本组、其余 28 工具保持原文时，全目录：**22824 → 19533**。本轮无新增 provider 调用；静态体积不能证明模型更容易理解。认知目标是少做关系拼接、少在重复叙述中寻找有效字段，同时保留用户任务所需功能。

## 3. 实施位置与单一所有者

| 范围 | 现有 owner | 本次动作 |
|---|---|---|
| 文件、artifact、view_image 的工具正文与字段 annotation | `src/pulsara_agent/capability/builtin_catalog.py` 的对应 `_BUILTIN_DESCRIPTORS` 条目 | 按 §6 的正文与逐路径表替换 |
| edit_file 的嵌套操作 annotation | 同文件 `_edit_operation_schema()` | 将共同的原始行锚点和 literal lines 含义集中到函数正文/operations；删除分支重复描述 |
| open_in_pulsara | 同文件 `OPEN_IN_PULSARA_DESCRIPTION`、`OPEN_IN_PULSARA_SCHEMA` | 替换正文和六种 target 字段说明；task_id 来源使用当前真实工具名 |
| 三个 terminal 工具正文 | `src/pulsara_agent/ports/terminal.py` 的三个 `*_TOOL_DESCRIPTION` 常量 | 替换为 §6 英文 |
| terminal 字段语义 | 同文件 `TerminalInput`、`_ProcessInput`、各 process/monitor input model 的 `Field(description=...)` | 依据导出 JSON Pointer 对应到现有字段；共享字段只维护一个短定义 |
| terminal schema 导出 | 同文件 `_inline_schema_references()` | 保留引用内联和 object 根；删除 root allowed-fields 自动说明生成；移除导出根 discriminator annotation；按 schema 位置去除 title |
| 本地执行和参数校验 | `tools/builtins/filesystem.py`、`tools/builtins/artifact.py`、`conversation_kernel/tool_runtime.py`、`terminal_process/manager.py` | 行为不改；读取这些 owner 用来确定语义 |

terminal 的 `Field(discriminator="action")`、TypeAdapter 和分支 required/additionalProperties 继续存在。删除的是导出 JSON 的 discriminator annotation，与 Pydantic 执行分派不同。原 adapter 只有见到该导出 annotation 才追加根 required-fields 文字；因此在现有 builtin 导出 owner 去掉它，就能让完整动作行只出现在 function.description，无需改动通用 lowerer，也不影响 MCP 的 server-owned schema。

只在 schema 节点处理 annotation：properties/$defs 等映射中的 key 是字段名；const/default/examples 中的对象是数据。不能把所有 dict 中名为 title/description/discriminator 的键一律删除。既有通用 lowerer 的同名属性问题不借此扩成另一个改造。

`src/pulsara_agent/conversation_kernel/tool_runtime.py` 现有 builtin snapshot 从 descriptor 生成 FrozenToolSpec，本规格的 canonical 导出就走这条路径。无需第二套展示定义、名称匹配注册表或额外 wire 后处理。Python input 类可保留开发期 title；移除的是上述现有导出函数产生的 terminal schema title。其他内置工具的手写 schema 按指定 annotation 操作修改。

## 4. 为什么这些删改不让模型多猜一步

- 文件读写：content_revision、原始坐标、读过的行/相邻空隙、whole-file replacement 的区别是调用条件，直接保留。SHA 算法、原子写入实现过程、每种错误的预先清单不影响模型构造当前调用，删除。read_file 的 N|text 只解释一次，不重复两个反例。
- 文件搜索：Rust regex、glob 深度规则、不同分页单位都决定查询含义，保留。路径归属放 path 字段一次；数值默认和上限留 schema。
- artifact：读取保存的观察、保留片段、字符偏移与 continuation 是产品含义，保留。读取/导出用法各有简短入口；新文件写入和留存语义保留。不再对每个 handle 写猜测、探测、重跑的泛化告诫。
- terminal_process：七个动作的允许/必填字段用普通英文行描述。默认值、nullable 和有实际差别的值语义保留，不发明方括号 DSL。只在 process_id/since_cursor 等字段解释来源。动作说明放 function.description，root schema 不再复制它。
- terminal_monitor：注册异步返回、未来更新、list/cancel 与进程状态的区别均保留；记录缺失、具体 failure code 的处理沿用结果。不预先把每个可能生命周期变故变成动作禁令。
- 图像与显示：view_image 的单选输入、支持的媒介、实际渲染条件，open_in_pulsara 的批量目标与异步展示回执，都是解释输出所需事实。删除与当前参数无关的长串 unsupported/禁止事项。

对比前一稿额外删掉的告诫不以“后续会新增结果提示”为前提。现有结果本来就包含具体状态：artifact.py `_base_payload()` 有 source_coverage 等字段；filesystem.py `_application_error_result()` 有错误与 read hint；tool_runtime 的 HTML 结果已有 screenshot 条件说明。这里没有新增字段、说明通道、数据库状态或重复结果手册。

实际代价：展平仍把不同动作的字段合在一起，英文动作行仍需模型遵循。本规格没有宣称它获得严格分支解码。保持本地 canonical/parser 精确校验，避免为压缩文字扩大实际允许参数集合。此前嵌套结构的结果是比较依据，不是本规格更改接口的理由。

## 5. 合入与必要核对

按现有 owner 替换文本并清理指定 annotation，同一变更删除被替代的旧正文/自动说明，不保留两版说明选择机制。现有已安装 epoch 的 SYSTEM/tools 保持冻结；新的定义由既有 cold epoch 或显式 compaction successor 安装路径采用。

复现：从仓库根运行 `.venv/bin/python output/tool-clarity-specs-20261026/local_io_terminal/propose.py`。脚本将最终 canonical 与 Git 基线导出的 canonical 去掉 annotation 后作结构相等核对，再直接调用未修改的 lowerer 得到最终 Responses JSON。类型、范围、default、enum/const、required、封闭分支均保留。本轮已落地生产定义并运行相关测试；没有新增模型 API 调用。完整验证见 §9。

实施后的核对聚焦实际差异：确认七动作行与原 canonical 对应；确认嵌套 lines 的 literal/空行含义仍覆盖；确认 view_image 单选、open_in_pulsara target 字段以及 terminal monitor 参数路径没有丢失。现有 parser 与工具例行检查可以直接复用，不新增通用评测框架。若随后做真实调用比较，使用既有实验脚本比较首次合法与任务语义正确率，同时计入必要修正 token；不以更短文本直接宣称更好行为。

## 6. 完整英文正文与逐字段改动

以下 `function.description` 为完整替换文本。每张表的路径均相对于该工具 canonical `parameters`；`REMOVE` 表示删除 annotation，参数节点本身保留。所有表外 schema 键保持原值。完整 before/after JSON、导出的最终 wire 定义及可复现脚本位于 `output/tool-clarity-specs-20261026/local_io_terminal/`。

### artifact_export

```text
Export a saved tool-result artifact to a new UTF-8 file for searching, processing or extensive retrieval. artifact_read provides paged excerpts instead; a prior page read is unnecessary. The file contains the saved body itself, including plain output text for terminal artifacts. Its coverage is the original observation or its retained portion, as reported by source_coverage. Parent directories are created; an existing path is left unchanged. The exported file remains independently of the conversation.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/artifact_id/description` | artifact_id returned by a tool result in this conversation and project. |
| `/properties/path/description` | New file path, relative to the workspace or absolute; ~ is supported. |

### artifact_read

```text
Read a page of saved tool output using its artifact_id. Continue with next_offset_chars while has_more is true; pages may be shorter than max_chars. source_coverage identifies whether the body covers the original observation or only its retained portion; offsets address that saved body. Use artifact_export for extensive retrieval or processing.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/artifact_id/description` | artifact_id returned by a tool result in this conversation and project. |
| `/properties/max_chars/description` | REMOVE |
| `/properties/offset_chars/description` | Zero-based character offset; copy next_offset_chars to continue. |

### edit_file

```text
Edit an existing UTF-8 file using the content_revision from read_file. All line anchors are 1-based in that original revision; ranges are inclusive and operations use distinct, non-overlapping ranges or gaps. Line edits require the affected lines or adjacent gaps to have been read. replace_file replaces the whole file as the sole operation and needs a current revision, without requiring the whole file to be displayed. Results provide a diff, the new revision and changed line windows; NO_OP means no bytes changed. Use write_file for a new path.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/base_revision/description` | Exact content_revision returned by read_file for this path. |
| `/properties/operations/description` | Replacement text is literal file content without read_file line labels. Each lines item contains one line without CR, LF or NUL; an empty string is a blank line. |
| `/properties/operations/items/oneOf/0/properties/end_line/description` | REMOVE |
| `/properties/operations/items/oneOf/0/properties/lines/description` | REMOVE |
| `/properties/operations/items/oneOf/0/properties/lines/items/description` | REMOVE |
| `/properties/operations/items/oneOf/0/properties/start_line/description` | REMOVE |
| `/properties/operations/items/oneOf/1/properties/end_line/description` | REMOVE |
| `/properties/operations/items/oneOf/1/properties/start_line/description` | REMOVE |
| `/properties/operations/items/oneOf/2/properties/line/description` | REMOVE |
| `/properties/operations/items/oneOf/2/properties/lines/description` | REMOVE |
| `/properties/operations/items/oneOf/2/properties/lines/items/description` | REMOVE |
| `/properties/operations/items/oneOf/3/properties/line/description` | REMOVE |
| `/properties/operations/items/oneOf/3/properties/lines/description` | REMOVE |
| `/properties/operations/items/oneOf/3/properties/lines/items/description` | REMOVE |
| `/properties/operations/items/oneOf/4/properties/content/description` | Complete replacement text; an empty string empties the file. |
| `/properties/path/description` | Existing file path, relative to the workspace or absolute; ~ is supported. |

### find_files

```text
Find paths by basename or relative-path glob without reading file contents. *.py matches all depths; src/*.py matches one level; src/**/*.py is recursive. Use *name* for a name fragment. Results are sorted by path; continue with the returned offset hint.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/offset/description` | Zero-based file offset; use the returned continuation hint. |
| `/properties/path/description` | File or directory; relative to the workspace or an explicit external path. Broad external roots are rejected. |

### open_in_pulsara

```text
ROOT only. Show files, webpages, subagent tasks, existing terminal processes, MCP connections or overview panels in this conversation's tabs. Group related targets in one call; the last accepted target is selected. Invalid targets are reported by zero-based input index while valid targets proceed. The result acknowledges display requests; display occurs afterward and does not return webpage contents. The tasks panel shows subagents. A browser panel target creates a blank tab; duplicates share one tab within a call and later calls create new tabs.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/targets/description` | REMOVE |
| `/properties/targets/items/anyOf/0/properties/path/description` | File path; workspace-relative, absolute, ~ and ${PULSARA_HOME} paths are supported. |
| `/properties/targets/items/anyOf/1/properties/url/description` | HTTP(S) URL without embedded credentials. |
| `/properties/targets/items/anyOf/2/properties/process_id/description` | process_id from terminal or terminal_process. |
| `/properties/targets/items/anyOf/3/properties/task_id/description` | task_id from spawn_agent, create_agent_tasks or list_agents; opens its group and selects the task. |
| `/properties/targets/items/anyOf/4/properties/runtime_server_id/description` | runtime_server_id from inspect_capability. |
| `/properties/targets/items/anyOf/5/properties/name/description` | REMOVE |

### read_file

```text
Read lines from a local UTF-8 file and obtain its current content_revision for edit_file. Lines are displayed as N|text; N| is a line label, while text is the file content. Read the lines or adjacent gaps needed for line edits; a whole-file replacement needs the current revision but not a full display. Follow the returned offset hint for truncated output.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/limit/description` | REMOVE |
| `/properties/offset/description` | First line to return, numbered from 1. |
| `/properties/path/description` | Local text path; relative paths start at the workspace. ../, absolute paths, ~ and cataloged ${PULSARA_HOME}/ paths are supported. |

### search_content

```text
Search text with a Rust regular expression. content returns matching lines; files_only returns paths; count returns matching-line counts per file. Results are sorted by path and line and paginate in every mode. read_file provides the revision and line context needed for edits.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/file_glob/description` | Basename or relative-path glob, also applied to a single-file query. |
| `/properties/offset/description` | Zero-based result offset; count mode pages files. Use the returned continuation hint. |
| `/properties/path/description` | File or directory; relative to the workspace or an explicit external path. Broad external roots are rejected. |

### terminal

```text
Run one local shell command. Each call starts a new shell, so its directory and environment changes stay within that call. A running command returns process_id for terminal_process to inspect or interact with it. In the ROOT conversation, terminal_monitor can resume work on completion or configured progress. Response size and initial wait settings do not limit the command's lifetime. Use artifact_read for omitted output or artifact_export for extensive processing.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/title` | REMOVE |
| `/properties/command/description` | REMOVE |
| `/properties/command/title` | REMOVE |
| `/properties/max_output_chars/description` | REMOVE |
| `/properties/max_output_chars/title` | REMOVE |
| `/properties/tty/description` | Enable terminal interaction for programs needing line editing or a full-screen interface. |
| `/properties/tty/title` | REMOVE |
| `/properties/workdir/description` | Existing directory for this call; workspace-relative, absolute and ~ paths are supported. Omit for the workspace root. |
| `/properties/workdir/title` | REMOVE |
| `/properties/yield_time_ms/description` | Initial observation wait; 0 returns immediately. |
| `/properties/yield_time_ms/title` | REMOVE |

### terminal_monitor

```text
ROOT only. Resume the conversation when a running command completes or meets configured progress conditions. Set action as follows; fields are required unless marked optional, and each action uses only its listed fields.
register: process_id; optional conditions, delivery, lifetime.
list: no other fields.
cancel: monitor_id.
register returns immediately; the current turn may end while monitoring continues. Omitting conditions requests completion only; configured progress updates also resume the conversation. list shows active monitors, not process completion. cancel stops updates while the command continues. Monitoring also ends on expiry or when its terminal environment closes. Use terminal_process poll for current output or status, and kill to stop a command.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/description` | REMOVE |
| `/discriminator` | REMOVE |
| `/oneOf/0/title` | REMOVE |
| `/oneOf/0/properties/action/title` | REMOVE |
| `/oneOf/0/properties/conditions/description` | Progress triggers in addition to completion; omit for completion only. |
| `/oneOf/0/properties/conditions/title` | REMOVE |
| `/oneOf/0/properties/conditions/properties/heartbeat_interval_seconds/description` | Progress interval even when there is little new output. |
| `/oneOf/0/properties/conditions/properties/heartbeat_interval_seconds/title` | REMOVE |
| `/oneOf/0/properties/conditions/properties/output/description` | REMOVE |
| `/oneOf/0/properties/conditions/properties/output/anyOf/0/title` | REMOVE |
| `/oneOf/0/properties/conditions/properties/output/anyOf/0/properties/min_new_output_chars/description` | New-output threshold for a progress update. |
| `/oneOf/0/properties/conditions/properties/output/anyOf/0/properties/min_new_output_chars/title` | REMOVE |
| `/oneOf/0/properties/conditions/properties/output/anyOf/0/properties/quiet_period_ms/description` | Required pause in output before a progress update. |
| `/oneOf/0/properties/conditions/properties/output/anyOf/0/properties/quiet_period_ms/title` | REMOVE |
| `/oneOf/0/properties/delivery/description` | REMOVE |
| `/oneOf/0/properties/delivery/title` | REMOVE |
| `/oneOf/0/properties/delivery/properties/max_output_chars/description` | Maximum command-output characters in each update. |
| `/oneOf/0/properties/delivery/properties/max_output_chars/title` | REMOVE |
| `/oneOf/0/properties/delivery/properties/minimum_progress_observation_interval_seconds/description` | Minimum spacing between progress updates. |
| `/oneOf/0/properties/delivery/properties/minimum_progress_observation_interval_seconds/title` | REMOVE |
| `/oneOf/0/properties/lifetime/description` | REMOVE |
| `/oneOf/0/properties/lifetime/title` | REMOVE |
| `/oneOf/0/properties/lifetime/properties/maximum_duration_seconds/description` | Monitoring duration; expiry does not stop the command. |
| `/oneOf/0/properties/lifetime/properties/maximum_duration_seconds/title` | REMOVE |
| `/oneOf/0/properties/process_id/description` | process_id from a running terminal command. |
| `/oneOf/0/properties/process_id/title` | REMOVE |
| `/oneOf/1/title` | REMOVE |
| `/oneOf/1/properties/action/title` | REMOVE |
| `/oneOf/2/title` | REMOVE |
| `/oneOf/2/properties/action/title` | REMOVE |
| `/oneOf/2/properties/monitor_id/description` | monitor_id returned by register. |
| `/oneOf/2/properties/monitor_id/title` | REMOVE |

### terminal_process

```text
Control commands started by terminal. Set action to the operation below. Fields listed for an action are required unless marked optional; use only that action's fields.
list: optional include_running, include_finished.
poll: process_id; optional since_cursor, max_output_chars.
wait: process_id; optional since_cursor, timeout_seconds, max_output_chars.
write: process_id, data; optional yield_time_ms.
submit: process_id, data; optional yield_time_ms.
close_stdin: process_id.
kill: process_id.
poll reads now; wait waits up to timeout_seconds. write sends data unchanged; submit adds one newline to data supplied without its final newline. close_stdin closes input; kill stops the command and its children and waits for exit. Omitted optional fields use their defaults; only since_cursor accepts null. status and exit_code describe process completion; output or echo alone does not establish completion. Use terminal_monitor in the ROOT conversation for later updates, artifact_read for omitted logs, and artifact_export for extensive retrieval.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/description` | REMOVE |
| `/discriminator` | REMOVE |
| `/oneOf/0/title` | REMOVE |
| `/oneOf/0/properties/action/title` | REMOVE |
| `/oneOf/0/properties/include_finished/description` | REMOVE |
| `/oneOf/0/properties/include_finished/title` | REMOVE |
| `/oneOf/0/properties/include_running/description` | REMOVE |
| `/oneOf/0/properties/include_running/title` | REMOVE |
| `/oneOf/1/title` | REMOVE |
| `/oneOf/1/properties/action/title` | REMOVE |
| `/oneOf/1/properties/max_output_chars/description` | Maximum returned output characters; does not limit the command. |
| `/oneOf/1/properties/max_output_chars/title` | REMOVE |
| `/oneOf/1/properties/process_id/description` | Exact process_id from this terminal environment. |
| `/oneOf/1/properties/process_id/title` | REMOVE |
| `/oneOf/1/properties/since_cursor/description` | Exact output_cursor from this process, or omit for retained output. It advances past observed text, including omitted output. |
| `/oneOf/1/properties/since_cursor/title` | REMOVE |
| `/oneOf/2/title` | REMOVE |
| `/oneOf/2/properties/action/title` | REMOVE |
| `/oneOf/2/properties/max_output_chars/description` | Maximum returned output characters; does not limit the command. |
| `/oneOf/2/properties/max_output_chars/title` | REMOVE |
| `/oneOf/2/properties/process_id/description` | Exact process_id from this terminal environment. |
| `/oneOf/2/properties/process_id/title` | REMOVE |
| `/oneOf/2/properties/since_cursor/description` | Exact output_cursor from this process, or omit for retained output. It advances past observed text, including omitted output. |
| `/oneOf/2/properties/since_cursor/title` | REMOVE |
| `/oneOf/2/properties/timeout_seconds/description` | REMOVE |
| `/oneOf/2/properties/timeout_seconds/title` | REMOVE |
| `/oneOf/3/title` | REMOVE |
| `/oneOf/3/properties/action/title` | REMOVE |
| `/oneOf/3/properties/data/description` | Exact UTF-8 text; an empty string is allowed. |
| `/oneOf/3/properties/data/title` | REMOVE |
| `/oneOf/3/properties/process_id/description` | Exact process_id from this terminal environment. |
| `/oneOf/3/properties/process_id/title` | REMOVE |
| `/oneOf/3/properties/yield_time_ms/description` | Wait for process exit after input is flushed; 0 returns immediately. |
| `/oneOf/3/properties/yield_time_ms/title` | REMOVE |
| `/oneOf/4/title` | REMOVE |
| `/oneOf/4/properties/action/title` | REMOVE |
| `/oneOf/4/properties/data/description` | Exact UTF-8 text; an empty string is allowed. |
| `/oneOf/4/properties/data/title` | REMOVE |
| `/oneOf/4/properties/process_id/description` | Exact process_id from this terminal environment. |
| `/oneOf/4/properties/process_id/title` | REMOVE |
| `/oneOf/4/properties/yield_time_ms/description` | Wait for process exit after input is flushed; 0 returns immediately. |
| `/oneOf/4/properties/yield_time_ms/title` | REMOVE |
| `/oneOf/5/title` | REMOVE |
| `/oneOf/5/properties/action/title` | REMOVE |
| `/oneOf/5/properties/process_id/description` | Exact process_id from this terminal environment. |
| `/oneOf/5/properties/process_id/title` | REMOVE |
| `/oneOf/6/title` | REMOVE |
| `/oneOf/6/properties/action/title` | REMOVE |
| `/oneOf/6/properties/process_id/description` | Exact process_id from this terminal environment. |
| `/oneOf/6/properties/process_id/title` | REMOVE |

### view_image

```text
Inspect a local PNG, JPEG, static WebP or SVG, or capture a local HTML file as an image for the model. Supply exactly one of path or image_ref. SVG is rendered without cropping, with scripts and external resources disabled. HTML uses a 1200×800 light-theme viewport with reduced motion and network access disabled, capturing the content root or viewport. The image is a still view of the current file. Use open_in_pulsara to show content to the user.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/image_ref/description` | Exact image: reference from an earlier image or historical image placeholder in this workspace. |
| `/properties/path/description` | Local image or HTML path; workspace-relative, ../, absolute and ~ paths are supported. |

### write_file

```text
Create a new UTF-8 text file, creating parent directories as needed. Existing paths are left unchanged. Use read_file followed by edit_file to change or replace an existing file.
```

| canonical JSON Pointer | 操作/最终值 |
|---|---|
| `/properties/content/description` | Complete file text; an empty string creates an empty file. |
| `/properties/path/description` | New file path, relative to the workspace or absolute; ~ is supported. |

## 7. 证据位置与其他三份规格的边界

- 当前 190 次真实实验与压缩讨论：`output/tool-schema-compatibility-20261026/REPORT.zh.md`、`COMPRESSION_DISCUSSION.zh.md`。只作为设计背景；§6 英文不含实验信息。
- 本组 exact canonical：`output/tool-clarity-specs-20261026/local_io_terminal/proposed_canonical.json`。
- 最终 provider JSON：同目录 `proposed_tools.json`；完整 annotation diff：`annotation_changes.json`；计量：`measurements.json`。
- 能力与调度、子代理与计划、记忆与会话由另外三份根目录规格分别拥有；本文件不重复改它们的正文，不改 SYSTEM，不改 bundled Skill。

共同合入时应按工具名合并 39 个最终定义一次计量，而不是把四份“仅替换本组后的全目录”相加。跨规格的合并计量与覆盖清单写入 `output/tool-clarity-specs-20261026/combined_measurements.json`。

## 8. 四组最终合并核对

三位 GPT-6 Astra / high 子代理与 ROOT 按原分工完成实施。无参数 `reload_capabilities` 每次全刷新，独立模型入口 `reload_hooks` 删除；内部刷新 helper 保留。

| 范围 | 工具数（修改前→修改后） | 修改前 tokens | 修改后 tokens |
|---|---:|---:|---:|
| local_io_terminal | 12→12 | 7580 | 4290 |
| capability_schedule | 9→8 | 6423 | 5192 |
| orchestration | 12→12 | 5396 | 4040 |
| memory_session | 7→7 | 3427 | 2439 |

完整 Responses 工具数组：**22824 → 15958，减少 6866（30.08%）**。生产导出与四组最终候选逐值一致。

SYSTEM 单独为 2862→2847；按原口径将 SYSTEM 文本与 Responses tools 数组的估计相加，**25686→18805（减少 26.79%）**。按需加载的 subagent Skill 单独为 1916→1830，不计入常驻工具总量。

## 9. 最终审阅落实与验证

终审四项均已落实：SUPERSEDES 明确内容或分类纠正；artifact_read 删除重复的 unavailable 告诫；ask_plan_question.allow_free_text 删除 custom-answer 重复尾句；bundled subagent Skill 删除任务结束即无法继续的假想告诫。

本组联合核对：173 项 Python 测试通过，覆盖文件 artifact、终端、动作参数、展示和 provider 前缀连续性。另有全刷新 Hook 集成用例通过；其他三组的测试记录见各规格。前端 57 项摘要测试及桌面 build 通过；修改的 Python 文件 Ruff 检查与 git diff --check 通过。

终端 schema 导出仅在 schema 节点去掉 title，保留同名业务字段及 default 数据；测试覆盖这个区别。生产解析器、字段合法集合和原有默认执行行为保留。源码与基线的精确核对、39 个实际定义及完整计量见 `output/tool-clarity-specs-20261026/implementation/`；基线来自提交 `370917e0825e98221a7272d09171998f7ca8ccfe`。
