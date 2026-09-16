# Pulsara 本地图片读取工具实施规范

状态：实施设计，尚未实现。日期：2026-09-16。

本稿以当前工作区生产代码为基线；Git 基点为 `9c9c66a1`。工作区另有尚未提交的数据库重置与设置页修改，本任务不修改、提交或回滚那些内容。本文中的拟新增符号是实施约定，不代表代码中已经存在。

## 1. 目标、权威与范围

本任务允许模型调用 `view_image(path)` 查看本地静态图片，接通真实文件读取、工具结果落库、provider 输入、历史/压缩/fork 和浏览器左侧展示。

权威顺序：用户本轮确认的产品行为 → [AGENTS.md](AGENTS.md) → 本实施规范 → 已冻结的 [图片输入设计](PULSARA_KERNEL_IMAGE_INPUT_AND_OPENAI_WIRE_ADAPTER_DESIGN.zh.md) 中仍适用的 D1–D4、K1–K4、U1/U2 → 当前代码。代码用于识别现状与修改落点；本文明确要求改变的旧限制不能反过来阻止本次实施。没有明确改变的既有合同继续成立。

本稿明确扩展旧图片设计的“工具结果只有文本”范围，不改变 D1 公式、D2 格式/像素/metadata 限制、G 的常数、原效果前 R 检查原则，以及三级模型交接策略。新增图片工具的 R 必须有可执行的计算方式，见第 7 节。

### 1.1 用户已经确认的边界

1. 新增独立 `view_image`，读取一个本地文件；模型可在同一响应中调用多次。
2. 图片在 canonical 中属于对应工具结果，与 Chat Completions / Responses 无关。
3. 两套协议均使用相同的 provider-neutral 投影：原工具文本结果，随后追加一条 `user` role 的图片附件消息。Responses 不另走原生图片 function output 路径。
4. 同一批正常完成的工具调用只生成一条这样的派生消息，其中按原调用顺序放入成功图片与来源说明。
5. 用户不感知上述 wire role。图片在聊天区左侧归属于工具结果，不能生成右侧用户气泡。
6. 工具可并发执行；并发完成顺序不能改变 canonical 关联、provider 图片顺序或 Figure 编号。

### 1.2 首版范围

- 每次 `view_image` 一个 `path`，成功返回一张图片。多图通过多个工具调用表达。
- PNG、JPEG、静态 WebP；使用已经冻结的原始图片字节，不缩放、转码、纠正 EXIF 方向或截取动画首帧。
- 不新增 URL 下载、PDF/视频/GIF 输入、OCR 服务、缩略图数据库、图片编辑、浏览器文件系统直读或图片专用后台任务。
- 不顺带将所有 MCP 工具输出改成多模态；首版只有本地 `view_image` 生产图片工具结果。
- 原 `read_file` 的 UTF-8 文本、行号和 `content_revision` 合同不变。读图片不为 `edit_file` 建立“已经读过文本行”的事实。

## 2. 当前代码真源与实际缺口

下表是本轮读取代码后确认的现状。链接给出文件，符号名用于代码移动后定位；不要把行号或文件 SHA 做成验收证据协议。

| 当前 owner / 符号 | 已有行为 | 本次必要修改 |
|---|---|---|
| [builtin_catalog.py](src/pulsara_agent/capability/builtin_catalog.py)：`_BUILTIN_DESCRIPTORS`、`_FILESYSTEM`、`_recovery_contract` | `read_file` 是只读、并发安全、`filesystem_read`；catalog 同时驱动权限、工具族和 recovery | 加入 `view_image` 的完整 catalog 事实，不能只加 JSON schema |
| [filesystem.py](src/pulsara_agent/tools/builtins/filesystem.py)：`ReadFileTool.execute` | 只读文本；追踪行窗口和文件 revision | 新增图片执行实现，复用路径规则，不混入文本 revision 状态 |
| [workspace.py](src/pulsara_agent/tools/builtins/workspace.py)：`_resolve_read_path` | 工作区相对路径、本机绝对路径、`~`、`${PULSARA_HOME}`；相对路径不得逃逸工作区 | 直接复用，不照搬其他产品的 external-directory 审批体系 |
| [tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py)：`DirectKernelToolPort`、`_physical_io` | 构造 builtin、执行权限和物理调用；普通结果经 `result.output.encode('utf-8')` 转成 kernel 结果 | 注入既有图片验证 owner，增加明确的 typed 图片结果分支 |
| [tool_execution.py](src/pulsara_agent/conversation_kernel/tool_execution.py)：`ToolBatchExecutor.execute` | 主循环是 `for call_ordinal, call in enumerate(calls)`，实际逐个执行 | 在该 owner 内支持连续读图调用的并发窗口；没有现成的通用并行批次可以直接宣称复用 |
| [io.py](src/pulsara_agent/conversation_kernel/io.py)：`KernelSessionIO` | 物理 I/O semaphore、取消后 drain、Host 关闭 drain | 继续使用；当前 [limits.py](src/pulsara_agent/conversation_kernel/limits.py) 的 I/O 硬并发是 8，不另设图片线程池 |
| [input.py](src/pulsara_agent/llm/input.py)：`LLMImagePart`、`FrozenPromptContent` | 图片完整值包含 MIME、尺寸、immutable bytes；图片内容摘要属于 immutable content 边界 | 复用图片 part，不另造 provider 专用 Image DTO |
| [image_validation.py](src/pulsara_agent/conversation_kernel/image_validation.py)：`HostPromptImageValidator` | Pillow spawn worker、`verify()` 后重新打开 `load()`、全 Host 单验证槽、取消清理 | 本地工具取得文件后走同一验证实现；不得另起不受该槽约束的解码路径 |
| [tool_contracts.py](src/pulsara_agent/conversation_kernel/tool_contracts.py)：`KernelToolResult` | `content: bytes`，构造时要求 UTF-8 | 增加互斥的 typed 图片结果载体，移除图片路径上的隐式 `.decode()` 假设 |
| [tool_artifacts.py](src/pulsara_agent/conversation_kernel/tool_artifacts.py)：`ToolOutputArtifactProcessor.prepare` | 文本 OUTPUT artifact、inline preview、HEAD_TAIL | 图片不能被转换成文本 artifact、base64 preview 或省略标记 |
| [_repository/contracts.py](src/pulsara_agent/conversation_kernel/_repository/contracts.py)：`PreparedToolResultAcceptance` | 完整工具结果候选；canonical preview 必须 inline；已有跨提交确认身份 | 增加与正文 exact-join 的图片 publication 输入，复用现有确认身份 |
| [_repository/tools.py](src/pulsara_agent/conversation_kernel/_repository/tools.py)：`accept_tool_result`、`read_accepted_tool_result` | entry、tool_results、原 committed occurrence 在既有事务中接纳/确认 | 在同一事务发布图片并插入原 refs；确认覆盖完整正文和有序 refs |
| [prompt_content.py](src/pulsara_agent/conversation_kernel/prompt_content.py)、[prompt_storage.py](src/pulsara_agent/conversation_kernel/prompt_storage.py) | `pulsara.prompt/v1`、descriptor、图片 occurrence、事务 publication/hydration/exact-confirm | 复用其 Text/Image body codec 和 storage join；名称中的 prompt 不决定 entry 来源 |
| [clean-v0 SQL](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)：`canonical_image_refs` | refs 已支持 `transcript_entry_id`；tool_results 要求结果 entry 为 inline | 本次沿用该 owner，无需新表、新 ref owner 列；图片字节在 blob 中，inline 正文只放 descriptor |
| [reader.py](src/pulsara_agent/conversation_kernel/reader.py) | 工具结果走文本 decode；late outcome 再包装为文本；物理预检的图片 entry 集仅选 USER_MESSAGE/USER_STEER | ordinary/late 均需 hydrate typed 工具结果；补齐 metadata 预检，不能只改主读取分支 |
| [model_input/contracts.py](src/pulsara_agent/model_input/contracts.py)：`FrozenProviderInputItem` | 只允许特定 HUMAN USER 与 snapshot 携带图片；工具 body 是纯文本 | 放开 TOOL_RESULT / LATE_TOOL_OUTCOME 的合法 typed 内容，保持原工具归属 |
| [lowering.py](src/pulsara_agent/model_input/lowering.py)、[compiler.py](src/pulsara_agent/model_input/compiler.py) | 单 item lowering、工具 FULL/COMPACT/REF_ONLY/OMITTED 决策；cold 与 append 编译分支 | 共同使用批次图片展开；成功图片不可由降级选择器静默丢弃 |
| [runner.py](src/pulsara_agent/conversation_kernel/runner.py)：`_quote_post_response_resources` | 在 assistant 接纳和工具效果前，按实际调用批次计算文本结果/closure/late 的 R 和 U_W/U_T | 保留原检查位置；为新图片结果分配有限、可验证的资源额度 |
| [compaction/contracts.py](src/pulsara_agent/conversation_kernel/compaction/contracts.py)：`provider_input_item_canonical_expanded_bytes` | 非用户图片会被拒绝；已有完整 tool group、protected tail 和 safe cut | 工具图片进入相同 C 计量与完整组边界 |
| [_repository/fork.py](src/pulsara_agent/conversation_kernel/_repository/fork.py) | 复制每个 transcript entry 后调用 `copy_canonical_prompt_refs` | 复用该机制，核实工具 body、call/result 映射和 imported guard，不另拷原路径 |
| [canonical_v3.py](src/pulsara_agent/terminal_protocol/canonical_v3.py)：`read_content_reference` | `image_ref_ordinal` 仅允许 USER_MESSAGE/USER_STEER | 允许合法 TOOL_RESULT 图片 owner，仍经原 session/scope 与 entry 校验 |
| [runtime-adapter.ts](frontend/lib/runtime-adapter.ts)、[pulsara-types.ts](frontend/lib/pulsara-types.ts) | 工具结果映射到 `ToolTrace.resultText`；历史/活动/实时 reconciliation 多个入口 | 添加工具结果 typed content 的可丢弃投影，不能只修改一种历史入口 |
| [workbench-view.tsx](frontend/components/workbench-view.tsx)：`TraceCard`；[prompt-content-view.tsx](frontend/components/prompt-content-view.tsx) | 前者展示左侧工具文本；后者已有缩略图、Figure、Lightbox 和图片读取生命周期 | 工具图片复用现有图片组件与读取机制，显示于左侧工具结果 |

这些限制说明：只注册工具，或只让 HTTP 返回 data URL，都不能完成此任务。

## 3. 工具合同与读取边界

### 3.1 模型可见接口

```json
{
  "name": "view_image",
  "parameters": {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ["path"],
    "additionalProperties": false
  }
}
```

描述应明确：读取一个本地 PNG/JPEG/静态 WebP 并让模型看到内容；相对路径以当前 workspace 为基准；多张图片使用多个调用；不处理 URL、目录、PDF 或动画。不提供 `detail`、resize、任意 MIME、页码等未闭合参数。

catalog：`is_read_only=True`、`is_concurrency_safe=True`、`permission_category='filesystem_read'`，纳入 filesystem family、read-only recovery 和 evidence acquisition 分类。工具集合、schema、描述只随正常冷 epoch 或已采用 successor 安装；不能在现存 epoch 临时增删工具。纯文本目标若调用已安装的工具，返回明确的不支持图片错误，不热改 tools。

权限沿 `PreparedResolvedToolInvocation` → PreToolUse → 原 policy/confirmation → 原 attempt admission。真正读取前检查当前调用绑定的 target input modalities，不能只查 UI 配置或猜 model/provider 名字。不得为此新增秘密文件扫描器、路径 allowlist 或另一套许可协议。

### 3.2 文件取得

1. 复用 `_resolve_read_path`；路径字符串作为工具参数/来源标签，不作为后续发送的可变数据源。
2. 权限通过后打开普通文件。拒绝目录、设备、FIFO、socket 等不具备有限文件读取语义的对象。使用维护中的 Python 文件系统 API，检查实际打开句柄的 `fstat`，不能仅依赖打开前 `stat`；避免 FIFO 在检查前阻塞。
3. 读取前检查当前调用的字节额度；读取时最多取得该额度加 1 byte，用额外一字节检测增长/超限。禁止无界 `read_bytes()` 后才判定太大。
4. 对本次取得的 immutable bytes 使用 Pillow 的实际格式识别。扩展名、调用方标签、4KB sniff 均不能替代完整验证。复用验证 worker，允许它为该入口从实际内容得出 MIME；原用户上传路径仍必须核对其声明 MIME。
5. 验证字节一次冻结，之后 publication、retry、cold rebuild、fork、UI 查看均读取 blob。原文件改名、修改、删除均不改变已接纳结果。

文件可能在读取过程中被外部进程修改；本产品承诺保存并验证“本次实际读到的完整字节”，不承诺文件系统快照或创建新的文件锁/版本证明。若这些字节无法完整解码，返回工具错误。

### 3.3 验证与错误

继续使用静态 PNG/JPEG/WebP、`W×H <= 16_777_216`、PNG 单块及累计文本各 1 MiB、完整 verify/load、全 Host 单解码槽。保持原图片字节。不得照搬 Codex/OpenCode 的 resize、GIF 或更大尺寸常数。

参数非法用原 `INVALID_ARGUMENTS`；路径/格式/像素/本次额度不满足时用原 `APPLICATION_ERROR`，正文给出稳定的具体原因，例如 `IMAGE_FORMAT_UNSUPPORTED`、`IMAGE_DECODE_FAILED`、`IMAGE_RESOURCE_EXCEEDED`、`MODEL_IMAGE_INPUT_UNSUPPORTED`。这是工具正文错误码，不新增 committed event kind。错误不可伪装成 SUCCESS + “[图片已省略]”。普通失败不附图片；权限拒绝和取消沿原状态合同。

### 3.4 同步文件读取与异步验证的衔接

当前 `_execute_tool_call` 经物理 I/O owner 在线程内执行，`HostPromptImageValidator.freeze` 则是异步入口。实施时不能在文件线程内另建事件循环或自行调用 Pillow 来绕过验证 owner。最小衔接由 `DirectKernelToolPort.invoke` 负责：

1. 在原权限与 attempt 已成立后，经 `KernelSessionIO.run_tool_invocation` 执行有界文件读取。此阶段返回的原始 bytes 只是内部读取产物，不是可接纳的 SUCCESS 工具结果。
2. 在原异步 runtime 中调用注入的 Host 验证 owner；共享本次 `NONTERMINAL_TOOL_INVOCATION` 的绝对 deadline，排队、读取、验证之间不重新获得一整段超时。
3. 验证完成并通过本次资源额度后才组装 typed `ToolExecutionResult` / `KernelToolResult`，进入共同 settlement 路径。无需给所有 builtin 增加通用 async 执行框架。

整个调用的结束时间、取消状态和是否迟到必须覆盖读取与验证全过程，不能直接沿用“文件线程已返回”的 ON_TIME 就宣称工具完成。读取阶段使用原 physical drain；验证阶段使用原 worker 终止/回收。仅取得未验证 bytes 时发生取消/超时，不得发布图片成功结果；完整结果已形成时继续遵守原 known-result settlement。Host 对验证器保留原生命周期所有权，单个调用不得 close 共享验证器。这一衔接的异常、超时与清理必须有针对性测试。

## 4. 一个工具结果、一份 canonical 真值

### 4.1 进程内内容

首版采用小范围闭合 union，避免为了单个图片工具重写所有文本工具的正文格式：

- `ToolExecutionResult.output`：现有 `str` 文本分支，或明确的 `FrozenPromptContent` 图片分支。
- `KernelToolResult.content`：现有 UTF-8 `bytes` 文本分支，或同一 `FrozenPromptContent` 图片分支。
- 图片分支只接受已验证的内容，首版至少一个 Text 和恰好一个 `LLMImagePart`，且只能用于 `view_image` 的 SUCCESS。纯文本结果继续走现有文字处理。

这两个分支表达不同内容种类，不是旧/新实现的兼容回退。每个结果只有一个正文值：不得同时保留可变 `output_text`、`image_list`、metadata 内 base64、另一份 provider payload。所有现有盲目 `.encode()` / `.decode()` 消费点必须穷尽分支；不得捕获 TypeError 后回退为 `str(image)`。

复用 `FrozenPromptContent` 是复用已验证 Text/Image 内容类型，不将工具变成 prompt 命令。权限、来源、状态、时间、工具关联仍由原 tool result owner 提供。

### 4.2 持久化形状

纯文本工具保持现有 inline preview 与 OUTPUT artifact 行为。图片工具的 inline 正文复用 `pulsara.prompt/v1`，媒体类型为既有 `application/vnd.pulsara.prompt+json`；仅存 Text 和图片 descriptor：

```text
transcript_entries：entry_kind=TOOL_RESULT，inline body=Text + Image descriptor
tool_results：原 call/result/attempt/state/timing/permission 等字段
canonical_image_refs：transcript_entry_id=result_entry_id，ref_ordinal=0
blobs：真实图片字节
```

不保存 provider role/API 名、不建立派生 user entry、不新增“图片消息”关系、图片专用 subject、额外 committed event 或 delivery receipt。正文中的 digest/宽高/字节数由原 codec 生成；图片引用按 occurrence 计数，内容相同可以复用 blob，但不能合并两个调用的结果身份。

结果 body 仍服从既有 64 KiB canonical preview 硬边界，图片编码字节不塞进 body。首版一个调用一个图片 descriptor 和短状态文字足够 inline；完整路径来自已存在的调用参数，provider/UI 可按来源取值，不需要在多个正文中反复复制。正文必须先通过 codec/inline 大小验证，不能放宽 SQL 的 inline 工具结果约束。图片本身另外计入 D2 的 C/L/W/T。

### 4.3 Publication 与 FULL 确认

工具完成取得/验证/资源检查后，再准备冻结的 `PreparedToolResultAcceptance`。图片分支携带 codec 生成的 inline 正文及与其 exact-join 的 immutable image occurrences；这和现有 `FrozenCanonicalPrompt` 的 body/publication 输入关系一致，不新增独立图片 authority。

在原 `accept_tool_result` writer transaction 内：验证原 fence/attempt → 通过现有 blob publisher 发布/精确复用图片 → 写 TOOL_RESULT entry 与 tool_results → 写原 `canonical_image_refs` → 写原 `TOOL_RESULT_ACCEPTED` occurrence → commit。解码、文件读取、base64 序列化不在 SQL 事务里发生。

结果提交不明时只走原 `read_accepted_tool_result` 确认；FULL 要比对完整正文、有序 refs、blob 身份与元数据，图片 payload 在发布及正常 hydration 时校验。不因为确认重读原路径，不创建另一张确认表；也不重跑 `view_image` 来“恢复”一个结果。

结果仍是独立事务，不将整批工具改成一个大事务。任一调用失败不回滚已经按既有合同接纳的其他结果。GC 复用 `canonical_image_refs` 的可达性；不得先发布无引用 blob 再在后台补关系。

### 4.4 Hook、artifact 与 live

- PreToolUse/PermissionRequest 继续看真实工具名和参数。
- PostToolUse 复用原文本公共投影，只含短结果说明及必要图片元数据；不暴露 base64，不伪称 hook 看过图像像素。图片正文保持原完整值。
- 图片分支不进入文本 HEAD_TAIL、REF_ONLY 或 `artifact_read` 文本分页。原文本 artifact 机制不变。
- 原 live 事件只发送进度和短文字；图片在 committed result 后按原 entry/ref 读取显示。无需新的 live image chunk/event，断线重连由 canonical 恢复即可。

## 5. 并发调用与批次定义

### 5.1 批次身份

一批是同一 `ASSISTANT_TOOL_REQUEST` entry 内，按原 `CompletedToolCallBlock` / canonical block ordinal 排序的 calls。不是时间窗口，不是整个 turn，也不是“刚好同时结束的图片”。不新增 durable batch ID，使用已有 assistant entry identity。

### 5.2 并发的最小落点

在 `ToolBatchExecutor` 内提取可复用的单调用 prepare/authorize/invoke/settle 操作，再让连续、可独立执行的 `view_image` 调用共享该操作。禁止复制整套 permission、Hook、attempt、settlement 代码作为第二套图片执行器。

首版只并发连续的 `view_image` 段。其他工具、终端、写文件、plan control、subagent report 等是原顺序屏障；例如 `[view A, write X, view B]` 不得为了合并图片把 view B 提前到 write X 之前。provider 合并仍按整条 assistant 的 calls 归组，与物理并发段无关。

为保持原 Hook/人工确认时序，存在匹配 Pre/Post/Permission Hook 或需要交互确认的调用作为执行屏障，沿原串行步骤处理。未涉及这些顺序效果的读图段才并发；这是明确的调度语义，不是另一条数据/协议回退路径。

物理读取复用 `KernelSessionIO` 的现有并发额度；Pillow 解码仍共享单验证槽。调用可以并发在途、等待槽位，不意味着同时解码 8 张图片。不要无限创建带大字节缓冲的任务；使用原物理容量形成有限窗口，未调度调用保留在当前 calls 序列中等待，不因并发槽满而拒绝逻辑工作。

### 5.3 确定性与取消

- 每个调用的 attempt、结果和事件仍独立；call ordinal 在发起时确定。
- 物理完成结果可暂存在当前批次内，canonical settlement 按原调用顺序推进，避免后完成的调用占走先调用的资源。等待中的结果总字节受第 7 节额度约束。
- 一个调用的已知失败变成该调用的工具错误，不取消其他已授权只读调用。
- Host/turn 取消停止未启动调用，drain 已启动物理工作及验证 worker。已取得的准确结果按原 settlement/late 合同处理；不能因为 gather 被取消而丢掉已完成结果或误称“没有执行”。
- 各调用 own 的 live sink、attempt、settlement token 必须分别持有，不能把当前串行实现的单个局部变量直接共享给多个任务。
- Host 关闭等待当前 owner 清理，不留下 detached worker、额外队列服务或跨重启执行恢复。

## 6. 两种 API 共用的图片投影

### 6.1 统一展开

输入 canonical 仍是一组 assistant calls 与各自工具结果。公共 compiler/lowering 输出：

```text
assistant：calls [A, B, C]
tool A：原文本 observation envelope + 短图片读取结果
tool B：原文本 observation envelope + 错误（若失败）
tool C：原文本 observation envelope + 短图片读取结果
user：
  Text(说明：以下内容是工具读取的图片附件)
  Text(来源：call A，path A)
  Image(A)
  Text(来源：call C，path C)
  Image(C)
```

一次正常完整批次恰好 0 或 1 条派生 user 消息：没有成功图片就是 0；否则是 1。每张图附准确 call_id，路径标签采用原已冻结调用参数中的 path，明确它是请求路径；实际解析仍按第 3 节执行。这样来源标签在效果前已知，能进入准确报价，不增加一份规范化路径状态。用既有规范 JSON 文本编码标签，避免文件名中的换行、引号或标记改变关联。图片和标签都是工具来源数据，不赋予 system 指令权限。

Chat 将派生 user 图片编码为 `image_url`；Responses 编码为 `input_image`。两者的工具结果仍是文本。不能在 adapter 中重新读取文件、决定分组，或者让 Responses 另发一份 native image output。

### 6.2 Compiler 与连续性

单条 `lower_canonical_item()` 不足以独自完成整批合并；批次展开应在公共 compiler 的有序组装层完成，复用同一纯函数用于 cold、append、summary source、prospective quote 和最终 materialization。不要新增第二个平行 compiler。

扩展 `FrozenProviderInputItem`，允许 TOOL_RESULT / LATE_TOOL_OUTCOME 保存 typed 图片内容；工具 request/result 关联字段继续存在。`tool_result_body_text` 如保留，只是同一完整内容的派生文本视图并校验一致，不是另一份可变正文。`provider_input_item_text()` 对图片继续 fail closed；只让明确的工具图片 lowering 处理它。

`LoweredToolResultVariant` 的现有 FULL 限制 `MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES = 40_000` 继续约束工具文字载体；它与 canonical preview 的 65,536 bytes 是两个边界，不能混用。图片是本组不可静默省略的附件，不把数 MiB 图像塞进这个 text variant 限制。成功图片结果新增一个明确的 FULL-required 原因，覆盖“文字结果 + 对应附件”的整个投影；失败的纯文本结果仍走原规则。

一组图片消息可对应多个 canonical source entry。扩展现有 semantic group/placement 的映射以容纳一对多/多对一，使用完整有序来源 identity 和内容；禁止假定“一个 entry 永远对应一个 provider message”。不添加 fingerprint→object registry，不伪造 canonical USER kind 来迁就旧索引。

先收齐正常批次的 result/closure，后输出附件，不能插在仍未闭合的 tool call/result 中间。选出的合并消息、原工具消息和顺序只安装一次；之后 retry 原样复用，新的结果只能 suffix append。用户新消息、Hook source 和 runtime observation 继续遵循原 compiler placement，不由 adapter 随意移动。

### 6.3 中断和迟到结果的边界

“同批一条”是一次正常批次投影的规则，不是要求改写已安装历史：

1. 未安装过本批次的结果，完整闭合后按 6.1 合并。
2. 批次已经以原 closure 安装，随后出现既有 `LATE_TOOL_OUTCOME`：只能在新的 suffix 中发送迟到结果及其附件，不回填之前那条 user 消息。
3. 同一个新 suffix 内、同一原 assistant group 的多个 late 图片可合并一条；跨 group 不合并。调用顺序从原 call ordinal 得到，不能借用结果完成时间。
4. 不为将来可能出现的图片预先安装空消息，不等待所有“可能迟到”结果无限阻塞；不引入新 rebase 边界。

这沿用当前 reader 的 closure+late 语义，是 append-only 的必要结果。必须在测试中明确普通批次与迟到批次的区别。

### 6.4 Final wire 与图片来源配对

既有 [estimator.py](src/pulsara_agent/llm/estimator.py) 使用完整 frozen semantic content 与实际 wire part 配对核验 MIME、尺寸和图片 bytes。新增派生消息也必须进入同一份完整 semantic plan：图片的 canonical 来源是 tool result，wire 坐标则是派生 user 的具体 part，二者由 compiler 的有序 placement 对应。不得仅凭“第几张图片”配尺寸，也不得为工具图片额外维护一份宽高表或重新解码。

generic wire、native replay replacement 后的 final wire、direct traversal 都必须保留该配对；原 assistant replay replacement 不能吞掉紧随其后的工具图片 carrier。两套 adapter 只消费已经冻结的 user Text/Image 序列，继续走原编码与 final materialization 校验。模型输入来源、图像 token 计量和前端消息归属不能从这个 user role 反推为 HUMAN_MESSAGE。

## 7. D1/D2 与工具输出资源闭合

### 7.1 保持冻结参数

```text
D1(image) = max(256, ceil(7 × ceil(width/28) × ceil(height/28) / 8))
C hard = 16 MiB；L hard = 64 MiB；W hard = 64 MiB；N hard = 4096
G_C = G_L = 4 MiB；G_N = 296
```

图片逐 occurrence 计费；相同 blob 被两个调用引用，C/L/W/T 均按两次使用计。原始文件 bytes、canonical body bytes、wire base64 bytes 与解码像素内存仍分开计量。base64 长度仍为 `4 × ceil(E/3)`。既有工具文字 envelope、来源标签、额外 user framing 和真实 native replay 均不能漏计。

不能把旧的单个文本 result 上界当成带图片结果的上界，也不能为每个 `view_image` 机械预留整个 16 MiB，导致任何非空上下文都无法调用工具。

### 7.2 本实施稿选择的资源策略：从剩余额度导出逐调用额度

为保持效果前 R 检查，同时不提前读取尚未获授权的文件，首版使用确定性的当前批次额度。它是当前执行窗口的资源参数，不是会话图片数、工具数或文件大小的永久产品上限。

计算位置仍在 `_quote_post_response_resources`，早于 assistant canonical settlement、Stop/Post Hook 和任何本批工具授权/物理执行：

1. 计算原 actual assistant/native replay、所有调用的既有文本结果或 closure+late 上界，以及共存 Hook/continuation/source 的 `R_base`。为图片来源说明和附件消息结构另外计入保守 framing；正常/late 的互斥路径取最大，共存路径相加。
2. `R_base` 已不能通过原 C/L/N/U_W/U_T/replay resident 检查时，沿原 output resource interruption 全批失败，未授权、未执行，不重放 provider 响应。
3. 对可解析为当前 builtin `view_image` 的 n 个调用，分别求 C/L/W/T 的剩余硬额度 `H_j = hard_j - base_upper_j`。这里 `base_upper` 包含已有输入、actual assistant 和第 1 步必需增量；不能只从 hard 中减本批 R。通过冻结的 resolved builtin binding 识别调用，不仅凭字符串同名猜测；不通过读取文件猜测大小，不依据 provider 名识别工具。n=0 时完全沿原报价，不能执行除法或创建图片额度。
4. 在每个维度将 `H_j` 按原调用顺序均分：`b[i,j] = floor(H_j/n) + (i < H_j mod n ? 1 : 0)`。不在结果完成时抢占共享额度，不回收失败调用的份额再改其他已发起调用的合同。
5. 原 R 检查证明 `base_upper_j + sum_i b[i,j] <= hard_j`。T 的 hard 是原 target 给出的 effective input budget；不另扣一次输出预留。派生消息不增 canonical N，但必须计入实际 epoch/wire item 结构及相关物理边界。
6. 原工具 execution context 接收本次不可变额度；只在进程内存在，不写 row、event、receipt 或新 permit registry。图片为 SUCCESS 的必要条件是实际结果的完整增量在各维度都不超过该调用额度。

这里的均分是本稿为简单、确定性的并发接纳所选择的实现政策，不冒称已由旧 D2 规定。它可能使一张大图在多图批次中报资源错误，而单独调用时可以成功；错误应说明“本批分配额度不足”，不能称文件格式非法。模型可以在后续调用中重新请求；系统不自动删图、拆调用、重试或转码。以后若要借用其他调用的剩余额度，应另做明确改动，不在首版加入动态竞价/再分配调度器。

### 7.3 执行期间的实际检查

文件取得上界不大于本次 C/L/W 可推导的编码字节额度及既有 M 初筛界，W 使用 base64 与必要包装的保守逆算。先检查 `fstat`，再有界读取；尺寸和 D1 只能由同一图片验证得出。额度为零或连最小图片/包装都容纳不了时直接给工具资源错误，不启动解码。

验证成功后，通过与 compiler 相同的投影得到图片结果资源增量 q；同时覆盖该调用正常结果与可能 closure+late 的最大路径，检查 `q_j <= b[i,j]` 后才可冻结 SUCCESS/publication 候选。q 表示在 R_base 已覆盖部分之外仍需增加的非负费用，不能从已经覆盖的文字额度再次扣减而低估；逐项对应必须由统一 quote helper 表达。无法证明可安全扣除的共享费用宁可完整计入 q。超额度返回文本 `APPLICATION_ERROR`，不发布图片；原文本失败结果已由 R_base 预留。

U_W/U_T 必须是真上界：已知前缀及 assistant 用原实际物化；新图片用精确 MIME、E、尺寸与标签，完整计入标准 wire 编码和 D1。合并 user 消息的 framing 可以用“逐调用独立 carrier 的和”保守覆盖，但必须针对现有估算器/JSON escaping 证明该不等式，不能仅用 `L×4/3` 代替整个 wire。

不要为了预算反复物化完整 base64；沿既有标准编码/quote 入口计量，并由最终 materialization 精确确认。验证与结果缓冲使用同一 immutable bytes；并发窗口内 Σ编码缓冲受 Σ逐调用额度限制，decoded pixels 仍只有原单验证槽。旧/新 plan 同存和 compiler working set 继续由原 owner 检查，不能把 64 MiB 逻辑界宣称为 Python RSS 上限。

### 7.4 Safe point 与接纳后的保证

open tool batch 中不压缩、不切换 target、不重新分配已冻结资源。完成批次后回原 provider safe point；如完整输入不满足 G，沿原 ordinary compaction，再尝试下一次调用。成功保存的图片不能为了勉强编译被 REF_ONLY/OMITTED 静默删除。

初次工具结果本身不合规时返回明确工具错误；已经接纳后发现 quote/实际物化不一致属于实现错误，必须报资源/完整性失败并修复，不能把 SUCCESS 改成“图片省略”掩盖错误。

## 8. Reader、压缩、模型交接与 fork

### 8.1 Reader 和 cold replay

正文媒体类型决定解码方式：纯文本 TOOL_RESULT 保留现有读取；typed 图片 body 使用原 codec/ref metadata/hydration，不检测任意文本 JSON 恰好有哪些 key。reader 在 payload 读取前将工具图片 refs 纳入预检；尤其修改 `_preflight_physical_bytes` 的 owner entry 集，不能漏掉 TOOL_RESULT。

ordinary 与 late 都从相同完整结果恢复图片。late 文本 envelope 只能包装文本部分，不能将 descriptor stringify 后当成图像已传递。`provider_input_item_canonical_expanded_bytes`、source fingerprint/identity 输入和物理范围计量均覆盖完整 typed 内容；descriptor 结构费与图片 E 不混淆。

canonical 缺图、摘要不匹配、refs 多/少或归属错误，沿现有 integrity/continuity 错误失败，不读原路径补图，不从 UI cache 恢复，不请求远程 URL。

### 8.2 普通压缩

- 原 summary 模型看到已安装 source prefix 中的工具图片，不只看 `[图片]` 标签。普通同模型压缩仍按 [compaction/model_call.py](src/pulsara_agent/conversation_kernel/compaction/model_call.py) 在原 prefix 后追加 summary request，并通过原 wire prefix 校验；不能借“重新合并图片”重编已经安装的前缀。公共展开函数用于构造合法新 plan/候选，不能赋予 summary 额外 rebase 权限，也不补回此前已退出有效上下文的历史图像。
- 原完整 tool group 是不可拆边界；safe cut 不得位于“tool 文字已保留而附件丢在另一侧”的中间。
- protected tail 保留一组时，保留该组全部已接纳图片并计 C/L/W/T；原最多三组、2 MiB 文本/结构 tail 政策不变，E 单独进入 D2。
- 已被 summary 覆盖的旧工具图片可随旧组从新 provider context 移除，不要求永久再注入原图。canonical 历史与 blob 可达性依旧存在，用户历史查看不受 provider 压缩影响。
- 工具派生 user 消息不进入 recent human selector、active request 或用户原话保留数。必须依据 canonical kind/origin 选择，而不是 `LLMMessage.role`。

### 8.3 模型切换

冻结的三级规则扩展覆盖工具图片：Tier 1 检测有效输入任何来源的 Image；纯文本目标遇到工具图片也不能直接安装。Tier 2 使用原多模态模型总结完整 source；最近三条真实用户输入按原规则选择，工具图片本身不使 recent 用户原话计数变化。Tier 3 原模型不可用时，P 将 source 中的工具 Image 也替换为普通 Text("[图片已省略]")，再由新文本模型总结；保持原纯文本交接 recent=0 和覆盖证明。

P 只作用于明确的交接/summary 候选，不修改原 canonical 工具结果、历史 UI、已安装 epoch 或重试 payload。用于临时投影的 typed 值须明确允许“图片经 P 后为纯文本”的合法形状，不能重新恢复原图；这不是新持久化省略类型。

### 8.4 Fork 与子代理

fork 沿原 imported call/result 映射复制 typed body 与 refs；blob 可复用，owner 和 call identity 归属子会话。无需在 clone 时重新读文件。已有 per-entry `copy_canonical_prompt_refs` 可复用，但 imported 工具结果、late、被 snapshot 覆盖/保留的组合必须测试。

ROOT 和 SUBAGENT_TASK 共享同一工具/结果链；不能只给 ROOT 支持图片。原父上下文文本 advisory 仍按冻结设计执行，不新增把全部工具图片跨代理重注入的机制。

## 9. Protocol-v3 与前端产品语义

### 9.1 读取接口

继续使用原内容读取请求的 `entry_id + image_ref_ordinal`，无需新 endpoint 或任意本地路径读取 API。`canonical_v3.py` 放开合法 TOOL_RESULT owner 后，仍验证 session、scope、workspace、entry、body codec、ref ordinal、blob MIME/大小/digest。仅“知道某个 blob_id”不能获得图片访问权限。

history/snapshot/活动记录只传 descriptor，图片按需通过原 chunked read 获取。live 不传大图；工具结果 commit 后用原观察/补读路径取得正文与 refs。不要新增专门的图片完成事件或可靠投递 ACK。

### 9.2 左侧展示

`ToolTrace` 增加可选的 canonical typed result content，`resultEntryId` 仍是图片 owner；文字摘要从同一内容派生。覆盖常规 history、bootstrap snapshot、subagent activity、实时结果 commit 后 reconciliation 和断线重连各入口。

工具完成后，在聊天区左侧的对应结果区域显示紧凑缩略图行和可点击的 `[Figure x]`。主要图片结果应可直接看到，不能必须展开原始 JSON 才发现图片；长诊断与工具参数仍可折叠。复用现有 `PromptContentView` / Lightbox 的加载、错误、object URL 回收和 stale-owner 校验。

Figure 编号只在当前工具结果内部从 1 起计算；本版一个调用一张图，多个工具结果各有自己的 Figure 1，这是独立结果的局部编号。provider 合并消息使用 call_id 关联，不依赖这些展示编号。若以后支持单工具多图，按该结果的 occurrence 顺序自然扩展，不持久化 Figure 数字。

不同工具结果不因 wire 合并而合成一个 UI 大气泡。不得展示“Pulsara 自动生成 user role”或把协议来源说明渲染成用户发言。用户侧只需知道哪个工具读了哪张图、是否成功，以及点击查看图片。

不得复用用户消息的编辑/重发/队列操作到工具结果上；read image 仅为历史附件查看，不再次触发模型工具或读取原本地文件。

## 10. 失败和一致性矩阵

| 情形 | 必须发生的行为 |
|---|---|
| 当前 target 不支持 image | 明确工具错误；不读文件、不热改工具前缀 |
| 权限拒绝/需要确认 | 原 decision/confirmation 路径；确认前不读图 |
| 不存在、目录、设备、FIFO、非法路径 | 有界文本错误；不阻塞等待无限输入 |
| MIME 伪装、损坏、动画、像素/metadata 超界 | 原验证 owner 拒绝；无图片 blob/refs |
| R_base 超硬界 | assistant 未接纳，工具未授权/执行，原 output interruption |
| 本批单调用图片额度不足 | 该调用资源错误；其他成功结果仍按顺序处理；无静默图像降级 |
| 多图逆序完成 | canonical/派生图片仍按原 call ordinal；UI 按工具身份归属 |
| 全部读图失败 | 只有原工具错误消息，不额外派生 user 图片消息 |
| 数据库写入失败 | 原事务回滚或 exact-confirm；不后台补 refs、不重读原路径 |
| cancellation/晚到 | drain 物理操作并沿原 known-result/closure+late 结算；不改旧前缀 |
| 同图被多次读取 | 多个独立结果/occurrence；blob 可去重；资源计多次 |
| 原文件在成功后删除/修改 | provider retry、历史、fork 仍使用已保存图像 |
| blob 损坏或引用缺失 | integrity failure；UI 明确附件读取失败，模型路径不静默省略 |
| 浏览器切会话期间图片返回 | 沿现有 owner 校验丢弃旧请求的展示结果，释放 object URL |
| 压缩/文本模型交接 | 仅在原合法 successor/P 边界改变有效输入，canonical 原图保留 |

## 11. 变更纪律与版本边界

1. 正常工具结果、原 accepted event、entry subject、canonical_image_refs、blob GC 都复用；预期 committed/live event kind、subject slot、append guard、product relation、durable job **类别数量不增加**。新增工具 catalog 项和现有 delivery reason 枚举值不等于新增 durable 类别。
2. 保留现有 tool_results inline SQL 约束；图片 bytes 放 blob、正文只放 descriptor。本稿不要求新增 SQL 列或 migration。如果实现发现必须改变 schema，先把具体理由和单一 clean-v0 变更写回本稿，不能偷偷加在线 migration 或兼容双读。
3. codec 内容摘要、现有跨重启工具结果确认身份、provider source/replay 兼容性摘要可以扩展覆盖新内容；不新增 DTO 指纹、文档 SHA、文件 SHA 或额外确认记录。
4. 修改会影响源兼容性的 compiler/tool-result 投影版本时，在原契约版本处 hard cut，并更新相应 golden、replay/source identity 验收。不要为了继续使用旧 epoch 保留两种投影。只在原 cold/adopted successor 边界重新建立输入。
5. 文本工具、用户图文输入、模型配置、队列、原图片 D1/D2 算法不借机重构；新类型 union 的消费者必须同步改完，不留 `try new -> except old`。

## 12. 实施分批与交付门槛

分批用于代码组织与审阅，生产入口只在链路完整后开放；不建立运行时 feature negotiation 或两套实现。

| 批次 | 交付内容 | 完成门槛 |
|---|---|---|
| L1 内容与存储 | typed 工具图片载体；验证复用；事务 publication/refs；FULL confirm；reader ordinary/late 与 C 预检 | PostgreSQL 真路径成功/回滚/确认/损坏/fork 测试通过；普通文本输出不变 |
| L2 投影与资源 | 公共分组展开；cold/append/summary/prospective 共用；FULL 图片交付；R 额度与 U_W/U_T；模型交接 | 两种 API 一组一条附件消息；actual 不超过 upper；prefix/retry/late/P 测试通过 |
| L3 工具与并发 | 完整 builtin 注册、权限、文件有界取得；原 executor 内并发窗口；取消/关闭 settlement | 真实文件并行读取、逆序完成、Hook/写工具屏障、物理 drain 通过；此时方可开放模型入口 |
| L4 浏览器与验收 | 原读取 API 支持工具 owner；左侧缩略图/Figure/Lightbox；历史/活动/reconnect | 浏览器完整链路 + 两种正式 adapter 真实 provider dogfood，通过后记录验收 |

实施者每批先说明本批改变的 owner、现有能力如何复用、未改变哪些产品合同；不要另建 approval registry。所有验证与命令使用仓库 `.venv`/uv，frontend 使用现有 npm scripts。

## 13. 必须通过的验收

### 13.1 有限、针对性的自动化测试

| 编号 | 场景及断言 |
|---|---|
| L01 | catalog/schema 只有 path；read-only、concurrency、family、permission/recovery 分类一致；旧 epoch tools 不变 |
| L02 | 工作区相对路径、绝对路径、`~`、`${PULSARA_HOME}` 和相对逃逸行为与原 read owner 一致 |
| L03 | 文件不存在、目录、FIFO/device、空文件、扩展名伪装、损坏、动画、多帧、像素/PNG metadata 超界，均无成功图片 publication |
| L04 | 读取期间增长超过额度被 bounded read 拒绝；不通过先整文件读取再判超限实现 |
| L05 | 成功图片 bytes/digest/MIME/尺寸从文件验证贯穿 canonical 与实际 wire；原图字节无 resize/EXIF 修改 |
| L06 | 同一个文件被多个调用读取：独立 result/ref，多 occurrence 计量，blob 可复用 |
| L07 | 同批单图、多图、混合失败、同图重复、文字工具混合：只要有成功图片均为 1 条派生 user 图片消息；全部失败为 0 条 |
| L08 | Chat 与 Responses 使用相同 provider-neutral 消息；没有图片 tool output，也没有额外 canonical USER entry |
| L09 | 逆序物理完成仍按 call ordinal 展示/投影；不是按 result sequence 或完成时间拼图 |
| L10 | `[view, write, view]` 保持屏障；匹配 Hook/人工确认保持原时序；并发只作用于许可的读图段 |
| L11 | 并发超过现有物理容量只等待；单验证槽可观测；取消与 Host close 后无后台物理读/解码遗留 |
| L12 | successful result body、blob、refs、tool row、原 event 同事务；故障注入后无 partial FULL；提交不明使用原 exact-confirm |
| L13 | reader 在读 blob 前计工具图片 E；metadata quote 覆盖 ordinary/late/base+suffix，不绕过 16 MiB |
| L14 | 图片成功结果不可被 FULL/COMPACT/REF_ONLY/OMITTED 候选链静默丢图；文本工具原降级仍可用 |
| L15 | R_base 原失败：没有 accepted assistant/工具授权/效果；图片额度失败：准确单调用错误，不伪称 SUCCESS |
| L16 | C/L/W/T 各自边界两侧；均分余数确定、错误调用额度不漂移；所有实际 normal/closure+late wire 与 D1 <= 冻结上界 |
| L17 | 极端长宽比、小字节大像素、大编码文件和包含 JSON 特殊字符的路径，不能破坏 D1/U_W/U_T 的证明 |
| L18 | retry byte-identical；读原文件后修改/删除，重放仍使用 blob；增量安装不改 SYSTEM/tools/历史 messages |
| L19 | 已安装 closure 后 late 图片只追加新后缀；不修改旧合并 user；多 late 的分组/去重按真实 source cut |
| L20 | 普通 compact 摘要真正看到被选 source 图像；完整 tool group 切分；保留尾组的图片不丢；无图用户 recent 计数不受影响 |
| L21 | 纯文本目标 Tier 1 拒绝图片，Tier 2 原模型总结，Tier 3 P 覆盖工具图并维持已有 recent/coverage 规则 |
| L22 | cold rebuild/fork/imported results/重复 compact/ROOT 与 subagent 均可恢复工具图片，且不会再次调用原路径 |
| L23 | TOOL_RESULT 的 entry/ref 读取走原权限；跨 session、scope、错误 ordinal、伪造 digest/blob owner 均不能读取 |
| L24 | 普通历史、bootstrap、live commit、subagent activity、重连均显示左侧工具图片，不出现右侧伪用户气泡 |
| L25 | Figure 在结果内编号、点击 Lightbox、关闭/切会话回收资源、加载失败不显示成另一会话图片 |
| L26 | 原用户图文输入、队列编辑、模型切换、文本 read/edit、artifact 分页和工具 timing/citation 回归不变 |

现有相邻测试入口包括：`tests/test_kernel_image_input_k1.py`、`test_kernel_image_input_k2.py`、`test_kernel_image_input_k2_postgres.py`、`test_kernel_image_input_k3.py`、`test_kernel_image_input_k4_lifecycle.py`、`test_round7_1_provider_visible_tool_result_projection.py`、`test_round3_1_provider_input_prefix_continuity.py`、`test_round1_tool_output_artifact.py`、`test_conversation_fork.py`、`test_round5b_long_horizon_context_compaction.py`，以及 frontend 的 prompt-content-view、workbench-view、runtime-adapter/app 相关测试。

在这些 owner 附近补有行为意义的测试；不要为每个 getter/DTO 镜像写测试。迭代只跑相邻用例，最终做与实际改动面相称的验证。不得弱化断言、增加 skip/xfail 或改写旧实验结果来获得绿色。

### 13.2 真实 provider 与浏览器验收

通过 `LocalSettingsStore` 和 `require_pulsara_home()` 读取用户已有配置，选择可用的支持图像的 Chat 与 Responses 各一组。冻结测试 target，实际请求使用正式 compiler、wire materialization、transport 和工具入口，不能手拼请求替代生产链。

使用生成的普通测试图与可验证内容，不使用私人聊天缓存作为默认素材。要求模型真正调用本地 `view_image`；覆盖单图、同轮两个以上并行调用、一个成功一个失败、下一轮追问已读图、原文件删除后历史查看，以及压缩/交接关键路径。记录实际工具 calls、结果归属、完整 provider 输入、usage、canonical/ref 数量与 prefix 比较；只遮蔽真实凭据。

browser 验收经正常 UI 发起，让工具生成左侧图片结果，检查 Figure、放大、刷新/重连和错误态。可另用一次性数据库做 destructive/fork/取消组合；只覆盖内存注入的 PostgreSQL target，保存配置只读。不为验收修改用户模型配置、导出 API key 到环境、重置未核实数据库或创建长期后台服务。

验收记录必须区分“结构正确”“provider 接受”“模型确实使用视觉内容”。模型误读图片不自动归因于协议；provider 接受一个 HTTP 请求也不等于已证明它看到了正确图像。

## 14. 实施完成定义

以下条件全部满足才算完成：模型可调用并发本地读图；每个结果独立、完整、可重放地保存；两种 API 共用一套确定性分组投影；已安装前缀不变；D1/D2 与效果前资源保证覆盖图片；取消/late/compact/文本模型交接/fork 有确定行为；UI 始终左侧按工具结果展示；相邻测试与正式 provider/browser 验收通过。

最后更新原图片设计的实施状态与验收入口，保留历史实验为历史，不宣称旧 K4/U2 自动覆盖本功能。文档中所有“已实现/通过”必须来自新的实际代码与验证，不能用本文的设计条目代替。

## 附录 A：其他项目的借鉴边界

前序两个 Luna max 子代理只读核验了本地仓库。Codex 的 [view_image handler](../codex/codex-rs/core/src/tools/handlers/view_image.rs) 使用独立工具并返回图像 function output；OpenCode 的 [read](../opencode/packages/opencode/src/tool/read.ts) 复用读取工具，自有 [Chat adapter](../opencode/packages/llm/src/protocols/openai-chat.ts) 将 tool media 转成后续 user 图片消息，[Responses adapter](../opencode/packages/llm/src/protocols/openai-responses.ts) 则可直接返回多模态 function output。

这些代码只提供实现参考，不是 Pulsara 的数据/权限/资源权威。本稿采用用户明确选择的跨协议统一 user 附件投影，不照抄其他项目的 resize、错误占位降级、持久化 data URL、压缩丢图或 provider-family 分支。
