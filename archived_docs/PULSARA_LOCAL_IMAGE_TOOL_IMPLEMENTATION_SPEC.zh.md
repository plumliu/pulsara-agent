# Pulsara 本地图片读取工具实施规范

状态：已实施并完成本轮验收。日期：2026-09-16。实际验收入口与边界见冻结 [图片输入设计 §17](PULSARA_KERNEL_IMAGE_INPUT_AND_OPENAI_WIRE_ADAPTER_DESIGN.zh.md#17-view_image-本地工具扩展实施状态2026-09-16)。

后续扩展 [已知图片引用重读规格](PULSARA_IMAGE_REFERENCE_REREAD_IMPLEMENTATION_SPEC.zh.md) 已实施：同一 `view_image` 现在接受 path/ref 互斥来源，且不实现历史图片查询。引用读取的新增验收独立记录在该规格 §14；本文原 path 工具验收不反向替代它。

本稿以实施前工作区生产代码为基线。2026-09-16 根据独立 critic 审阅补齐 unknown modalities、逐调用执行额度、组合来源 placement、Tier 3 工具证据、文本公共投影、验证窗口及历史读取权限，随后按本文完成 hard cut；正文中的“当前缺口”“拟新增”保留为实施落点说明，最终状态以上述 §17 验收记录为准。

## 1. 目标、权威与范围

本任务最初允许模型调用 `view_image(path)` 查看本地静态图片，接通真实文件读取、工具结果落库、provider 输入、历史/压缩/fork 和浏览器左侧展示；其后同一工具按已知图片引用重读规格扩展了互斥的 `image_ref` 来源。

权威顺序：用户本轮确认的产品行为 → [AGENTS.md](AGENTS.md) → 本实施规范 → 已冻结的 [图片输入设计](PULSARA_KERNEL_IMAGE_INPUT_AND_OPENAI_WIRE_ADAPTER_DESIGN.zh.md) 中仍适用的 D1–D4、K1–K4、U1/U2 → 当前代码。代码用于识别现状与修改落点；本文明确要求改变的旧限制不能反过来阻止本次实施。没有明确改变的既有合同继续成立。

本稿明确扩展旧图片设计的“工具结果只有文本”范围，不改变 D1 公式、D2 格式/像素/metadata 限制、G 的常数、原效果前 R 检查原则，以及三级模型交接策略。新增图片工具的 R 必须有可执行的计算方式，见第 7 节。

### 1.1 用户已经确认的边界

1. 新增独立 `view_image`，读取一个本地文件；模型可在同一响应中调用多次。
2. 图片在 canonical 中属于对应工具结果，与 Chat Completions / Responses 无关。
3. 两套协议均使用相同的 provider-neutral 投影：原工具文本结果，随后追加一条 `user` role 的图片附件消息。Responses 不另走原生图片 function output 路径。
4. 同一批正常完成的工具调用只生成一条这样的派生消息，其中按原调用顺序放入成功图片与来源说明。
5. 用户不感知上述 wire role。图片在聊天区左侧归属于工具结果，不能生成右侧用户气泡。
6. 工具可并发执行；并发完成顺序不能改变 canonical 关联或 provider 图片顺序。
7. 工具图片的最终 UI 按 2026-09-16 用户修订：收起时仅显示工具摘要，展开后直接显示大图，不显示 Figure 编号、链接或重复的成功文字；点击图片可放大。

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
| [workspace.py](src/pulsara_agent/tools/builtins/workspace.py)：`_resolve_read_path` | 工作区相对路径、本机绝对路径、`~`、`${PULSARA_HOME}`；只读相对路径以工作区为起点且允许通过 `../` 访问外部目标 | 直接复用，不照搬其他产品的 external-directory 审批体系 |
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
| [runner.py](src/pulsara_agent/conversation_kernel/runner.py)：`FrozenPostResponseResourceQuote`；[tool_contracts.py](src/pulsara_agent/conversation_kernel/tool_contracts.py)：`KernelToolInvocationContext` | 前者只有批次聚合报价；后者没有图片额度或 target modalities | 按 §7.5 传递同一冻结的逐调用额度与 target fact；deadline 按单调用实际调度时点建立 |
| [tool_execution.py](src/pulsara_agent/conversation_kernel/tool_execution.py)：`_settle_known_tool_result`、`_dispatch_post_tool` | settlement 无条件解码并做文本 artifact；PostTool 从文本公共投影构造 Hook 输入 | 按 §4.4 分开 canonical 图片接纳与派生文字消费，不把 descriptor JSON 当公共文字 |
| [model_input/contracts.py](src/pulsara_agent/model_input/contracts.py)：`FrozenCompiledMessagePlacement`；[provider_replay.py](src/pulsara_agent/model_input/provider_replay.py) | placement 是单来源；native replay 按来源 entry 分组 | 按 §6.5 增加闭合的工具附件来源值，同步修改全部 placement consumer |
| [compaction/contracts.py](src/pulsara_agent/conversation_kernel/compaction/contracts.py)：`provider_input_item_canonical_expanded_bytes` | 非用户图片会被拒绝；已有完整 tool group、protected tail 和 safe cut | 工具图片进入相同 C 计量与完整组边界 |
| [compaction/planner.py](src/pulsara_agent/conversation_kernel/compaction/planner.py)：`DestinationToolEvidence`、`freeze_destination_dialogue_projection_plan`；[coordinator.py](src/pulsara_agent/conversation_kernel/compaction/coordinator.py) | destination evidence 只保存 `result_body` 文本；后续 P 只能替换 projection 中实际存在的 Image | 按 §8.3 保留 typed 工具证据，在 Tier 3 选择/渲染前执行逐 occurrence P |
| [_repository/fork.py](src/pulsara_agent/conversation_kernel/_repository/fork.py) | 复制每个 transcript entry 后调用 `copy_canonical_prompt_refs` | 复用该机制，核实工具 body、call/result 映射和 imported guard，不另拷原路径 |
| [canonical_v3.py](src/pulsara_agent/terminal_protocol/canonical_v3.py)：`resolve_content_reference` | `image_ref_ordinal` 仅允许 USER_MESSAGE/USER_STEER | 允许合法 TOOL_RESULT 图片 owner，按 §9.1 验证 attachment/session、exact entry/ref owner 及来源 metadata |
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

描述应明确：读取一个本地 PNG/JPEG/静态 WebP 并让模型看到内容；相对路径以当前 workspace 为基准且可用 `../` 访问外部目标，绝对路径与 `~` 也可用；多张图片使用多个调用；不处理 URL、目录、PDF 或动画。不提供 `detail`、resize、任意 MIME、页码等未闭合参数。

catalog：`is_read_only=True`、`is_concurrency_safe=True`、`permission_category='filesystem_read'`，纳入 filesystem family、read-only recovery 和 evidence acquisition 分类。工具集合、schema、描述只随正常冷 epoch 或已采用 successor 安装；不能在现存 epoch 临时增删工具。纯文本目标若调用已安装的工具，返回明确的不支持图片错误，不热改 tools。

权限沿 `PreparedResolvedToolInvocation` → PreToolUse → 原 policy/confirmation → 原 attempt admission。真正读取前检查当前调用绑定的 target input modalities，不能只查 UI 配置或猜 model/provider 名字。不得为此新增秘密文件扫描器、路径 allowlist 或另一套许可协议。

模态判断严格复用 [llm/validation.py](src/pulsara_agent/llm/validation.py) 的谓词：`modalities is not None and "image" not in modalities` 才返回 `MODEL_IMAGE_INPUT_UNSUPPORTED`。已知支持 image 正常执行；`None` 是 unknown，也走普通读取、验证和请求，不新增 probe，不将其改写成 supported 或 unsupported。后续 provider 若拒绝，保留原实际错误，不自动删图重试。

事实来源为当前 `request.prepared_call.call.target.fact`，沿 §7.5 的进程内调用合同传入，执行端只读取其中的 `input_modalities`。不得重新解析模型名称、访问可变 catalog/设置，或把 wire API 类型当成模态能力。

### 3.2 文件取得

1. 复用 `_resolve_read_path`；相对路径以工作区为起点，只读调用允许 `../` 访问工作区外部，与已允许的绝对路径、`~`及 `${PULSARA_HOME}` 语义一致；路径字符串作为工具参数/来源标签，不作为后续发送的可变数据源。
2. 权限通过后打开普通文件。拒绝目录、设备、FIFO、socket 等不具备有限文件读取语义的对象。使用维护中的 Python 文件系统 API，检查实际打开句柄的 `fstat`，不能仅依赖打开前 `stat`；避免 FIFO 在检查前阻塞。
3. 读取前检查当前调用的字节额度；读取时最多取得该额度加 1 byte，用额外一字节检测增长/超限。禁止无界 `read_bytes()` 后才判定太大。
4. 对本次取得的 immutable bytes 使用 Pillow 的实际格式识别。扩展名、调用方标签、4KB sniff 均不能替代完整验证。复用验证 worker，允许它为该入口从实际内容得出 MIME；原用户上传路径仍必须核对其声明 MIME。
5. 验证字节一次冻结，之后 publication、retry、cold rebuild、fork、UI 查看均读取 blob。原文件改名、修改、删除均不改变已接纳结果。

文件可能在读取过程中被外部进程修改；本产品承诺保存并验证“本次实际读到的完整字节”，不承诺文件系统快照或创建新的文件锁/版本证明。若这些字节无法完整解码，返回工具错误。

### 3.3 验证与错误

继续使用静态 PNG/JPEG/WebP、`W×H <= 16_777_216`、PNG 单块及累计文本各 1 MiB、完整 verify/load、全 Host 单解码槽。保持原图片字节。不得照搬 Codex/OpenCode 的 resize、GIF 或更大尺寸常数。

现有 `HostPromptImageValidator.freeze(PromptContent)` 强制接收声明 MIME，不能直接把扩展名推导的 MIME 填进去冒充声明。实施时在**同一 owner** 增加本地 bytes 验证入口：输入有界 immutable bytes 与绝对 deadline，复用同一个 slot、spawn/cleanup 和验证实现，返回实际 MIME/宽高及已验证 `LLMImagePart`。内部 worker 工作项明确区分“有声明 MIME，必须相等”和“本地无声明 MIME，由实际格式确定”；后者只用于本工具入口。现有上传 `freeze()` 继续要求声明，不对上传开放缺失 MIME 的回退。两入口共享 decoder，不复制 Pillow 检查流程，也不新增 validator service。

参数非法用原 `INVALID_ARGUMENTS`；路径/格式/像素/本次额度不满足时用原 `APPLICATION_ERROR`，正文给出稳定的具体原因，例如 `IMAGE_FORMAT_UNSUPPORTED`、`IMAGE_DECODE_FAILED`、`IMAGE_RESOURCE_EXCEEDED`、`MODEL_IMAGE_INPUT_UNSUPPORTED`。这是工具正文错误码，不新增 committed event kind。错误不可伪装成 SUCCESS + “[Image omitted]”。普通失败不附图片；权限拒绝和取消沿原状态合同。

### 3.4 同步文件读取与异步验证的衔接

当前 `_execute_tool_call` 经物理 I/O owner 在线程内执行，`HostPromptImageValidator.freeze` 则是异步入口。实施时不能在文件线程内另建事件循环或自行调用 Pillow 来绕过验证 owner。最小衔接由 `DirectKernelToolPort.invoke` 负责：

1. 在原权限与 attempt 已成立后，经 `KernelSessionIO.run_tool_invocation` 执行有界文件读取。此阶段返回的原始 bytes 只是内部读取产物，不是可接纳的 SUCCESS 工具结果。
2. 在原异步 runtime 中调用注入的 Host 验证 owner；共享本次 `NONTERMINAL_TOOL_INVOCATION` 的绝对 deadline，I/O admission、读取、validator-slot 等待与验证之间不重新获得一整段超时。该 deadline 在本次调用获调度、进入原 invocation 时建立，尚未调度的 calls 队列不提前消耗它；具体传递见 §7.5。
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

必须落到以下现有消费点，不能只修改 `KernelToolResult` 的类型声明：

| 消费点 | 图片结果分支的唯一行为 |
|---|---|
| `_settle_known_tool_result` | 从 typed content 冻结 codec body/occurrences，绕过 `ToolOutputArtifactProcessor.prepare`；不执行 `result.content.decode()` |
| `PreparedToolResultAcceptance` / `accept_tool_result` | 接收完整 codec body 与 exact-join publication 输入；只有这一路持有 canonical 接纳真值 |
| live 与 `FrozenToolResultPublicProjectionInput` | 从同一 typed content 派生短文字；保留原 metadata/delivery，不能把整个 codec JSON 放进 `canonical_body` |
| `AcceptedCanonicalToolResultSettlement.public_projection` | 继续是文本公共投影，不再复制一份图片 authority；其中现名 `canonical_body` 在本分支是派生文字，不能拿来重建 canonical 图片 |
| `_dispatch_post_tool` | 继续从上述短文字构造 Hook 的纯文本输入，这是明确的公共投影；不作为 reader/compiler 的输入替代品 |
| reader / provider lowering | 从 canonical codec + refs 恢复完整值；文字 tool message 使用同一文本投影函数，Image 进入 §6 的附件 carrier |

文本公共投影由一个纯函数从冻结 typed content 生成，覆盖短状态说明和需要公开的图片元数据。prepared/settled 构造点核对它与原内容一致；下游不能独立编辑。这样不引入第二份可变正文，也不要求 Hook 理解图片 descriptor。

## 5. 并发调用与批次定义

### 5.1 批次身份

一批是同一 `ASSISTANT_TOOL_REQUEST` entry 内，按原 `CompletedToolCallBlock` / canonical block ordinal 排序的 calls。不是时间窗口，不是整个 turn，也不是“刚好同时结束的图片”。不新增 durable batch ID，使用已有 assistant entry identity。

### 5.2 并发的最小落点

在 `ToolBatchExecutor` 内提取可复用的单调用 prepare/authorize/invoke/settle 操作，再让连续、可独立执行的 `view_image` 调用共享该操作。禁止复制整套 permission、Hook、attempt、settlement 代码作为第二套图片执行器。

首版只并发连续的 `view_image` 段。其他工具、终端、写文件、plan control、subagent report 等是原顺序屏障；例如 `[view A, write X, view B]` 不得为了合并图片把 view B 提前到 write X 之前。provider 合并仍按整条 assistant 的 calls 归组，与物理并发段无关。

为保持原 Hook/人工确认时序，存在匹配 Pre/Post/Permission Hook 或需要交互确认的调用作为执行屏障，沿原串行步骤处理。未涉及这些顺序效果的读图段才并发；这是明确的调度语义，不是另一条数据/协议回退路径。

物理读取复用 `KernelSessionIO` 的现有并发额度；Pillow 解码仍共享单验证槽。调用可以并发在途、等待槽位，不意味着同时解码 8 张图片。executor 使用既有 `STAGE2_LIMITS.foreground_io_hard_concurrency`（当前 8）作为本批次窗口容量，最多启动这么多个读图调用；窗口覆盖 **read 开始 → 等待验证 → validate 完成或原始 bytes 丢弃**。I/O semaphore 只在同步 read 存活时占用，不能把它释放当成这个窗口已完成。

窗口内某调用形成完整结果或失败、释放原始缓冲后，才从当前 calls 序列补位。验证后的成功结果若等待前序 settlement，可由本批持有同一 immutable bytes，仍计入 §7 的总图片份额；不再另留原始副本。未调度调用继续等待，不因并发槽满被拒绝，也不预先创建携带大缓冲的任务。该窗口只是原 executor 的有界任务集合，不新增跨批次队列服务、线程池、scheduler 或新的并发常数。

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

该扩展的闭合形状和 consumer 修改要求以 §6.5 为准，不能仅给现有 `origin_entry_id` 随意选一个值。

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

### 6.5 组合来源 placement 的闭合合同

当前 `FrozenCompiledMessagePlacement` 的 `origin_entry_id / origin_item_fingerprint / within_origin_ordinal` 表达一个来源，cold/append layout 也使用一 item 一 message 的 zip。实施时在该 owner 内 hard cut 为闭合的来源联合：**原单来源**，或 **工具图片附件组合来源**。原消息保留原单来源语义；附件不得同时保留一个伪造的单来源 entry 字段供旧 consumer 使用。

拟新增的组合来源值只需要：

```text
ToolAttachmentSource
  assistant_entry_id：原工具请求 entry
  members：按原调用顺序排列的非空 tuple
    member.call_ordinal：原请求中的序号
    member.source：已冻结的 TOOL_RESULT / LATE_TOOL_OUTCOME 完整 source item
```

`member.source` 复用当前编译所持的 `FrozenProviderInputItem`，由其中既有字段取得 result entry id/sequence、tool_call_id、tool_request_entry_id、状态及 typed content；不要再复制这些字段、图片尺寸或 byte payload 为第二套 DTO。共享 immutable 值即可，不深拷图片。member 的顺序只由 call ordinal 决定；所有 source 必须属于相同 assistant 请求，并与当前编译范围内的真实结果 exact-join。每个成功图片 occurrence 恰好进入一个本次 carrier，失败和没有图片的结果不进入 members。

组合来源到 wire 的配对由同一个展开函数生成：遍历 member 内有序 content 时，记录 Image 的原 part 坐标及其输出消息 part 坐标；消费完整 source 和输出 message 验证这一对应，不另建图片身份或宽高查找表。首版一调用一图不免除 occurrence 校验。既有 placement/source/replay compatibility digest 的唯一 builder 覆盖有序组合来源及展开规则版本；不给这个进程内来源 DTO 再加独立 fingerprint 字段或 registry。

以下 consumer 必须同一 hard cut 完成：

| Consumer | 必须实现的规则 |
|---|---|
| `compiler.py` cold/append layout、provider-safe delta | 用公共有序组展开产生 messages 与 placements；二者仍逐项平行，但不再与 canonical items 做一对一 zip。正常完整批次的文字结果与 carrier 一次组装、一起通过预算、一起安装 |
| `direct_model.py` replay replacement、`provider_replay.py` manifest selection | 只选择原单来源且实际为 ASSISTANT 的 placement。组合附件始终走 generic user 编码，不能通过其中的 assistant_entry_id 匹配或扩张 replay 替换区间 |
| `compaction/planner.py` safe boundaries / protected tail | 普通批次的 assistant、result/closure 与附件是原完整 tool group 的同一不可拆边界。组合来源关联所有真实结果 sequence，不能把“最后一条 tool 文字之后、carrier 之前”当成合法 cut；suffix-minimum 与 coverage 遍历所有 member，而不是随便选一个 result |
| late source / safe cut | late carrier 与本次参与投影的 late result source 不可拆。原 assistant id 只用于关联，不能把早已安装或已在 lineage floor 之前的 assistant/closure 再计为本次待覆盖内容；不得要求回到旧组起点才能追加 |
| 普通 summary prefix proof | 使用已安装 messages/placements 的原样前缀，保持原 wire-prefix 检查。按新的来源形状判断完整边界，不重新展开并替换 predecessor |
| `provider_dispatch.py` ROOT 父上下文公共投影 | 识别组合来源为工具附件，不能反查成 HUMAN_MESSAGE 或把它当作 assistant replay。继续沿冻结的文本 advisory 规则处理图片缺失，不向父上下文新增图片或将其计入用户原话 |
| source/replay compatibility identity | 原唯一 identity builder 覆盖组合来源的有序成员与现有 source 身份；相关版本、golden 和前缀比较同步更新。禁止给旧 consumer 提供猜测来源的兼容 accessor |
| final-wire semantic source pairing | 每个 generic 附件 wire item 继续携带对应完整 `LLMMessage`，其图片与组合来源 members exact-join；native replacement 不改变附件坐标与内容 |

同一个正常批次来源在同一次编译中只产生一个 carrier。late 的组合范围由 §6.3 的当前 suffix 决定：已经安装的组合值保持不变，后来出现的成员创建新的组合来源，绝不修改旧 members。cold epoch 可以按其当前 source 范围重新展开，这是既有合法 rebuild 边界；不能据此重编现存 epoch。

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
6. 按 §7.5 将本次不可变额度从 quote 传到原工具 execution context；只在进程内存在，不写 row、event、receipt 或新 permit registry。图片为 SUCCESS 的必要条件是实际结果的完整增量在各维度都不超过该调用额度。

这里的均分是本稿为简单、确定性的并发接纳所选择的实现政策，不冒称已由旧 D2 规定。它可能使一张大图在多图批次中报资源错误，而单独调用时可以成功；错误应说明“本批分配额度不足”，不能称文件格式非法。模型可以在后续调用中重新请求；系统不自动删图、拆调用、重试或转码。以后若要借用其他调用的剩余额度，应另做明确改动，不在首版加入动态竞价/再分配调度器。

### 7.3 执行期间的实际检查

文件取得上界不大于本次 C/L/W 可推导的编码字节额度及既有 M 初筛界，W 使用 base64 与必要包装的保守逆算。先检查 `fstat`，再有界读取；尺寸和 D1 只能由同一图片验证得出。额度为零或连最小图片/包装都容纳不了时直接给工具资源错误，不启动解码。

验证成功后，通过与 compiler 相同的投影得到图片结果资源增量 q；同时覆盖该调用正常结果与可能 closure+late 的最大路径，检查 `q_j <= b[i,j]` 后才可冻结 SUCCESS/publication 候选。q 表示在 R_base 已覆盖部分之外仍需增加的非负费用，不能从已经覆盖的文字额度再次扣减而低估；逐项对应必须由统一 quote helper 表达。无法证明可安全扣除的共享费用宁可完整计入 q。超额度返回文本 `APPLICATION_ERROR`，不发布图片；原文本失败结果已由 R_base 预留。

U_W/U_T 必须是真上界：已知前缀及 assistant 用原实际物化；新图片用精确 MIME、E、尺寸与标签，完整计入标准 wire 编码和 D1。合并 user 消息的 framing 可以用“逐调用独立 carrier 的和”保守覆盖，但必须针对现有估算器/JSON escaping 证明该不等式，不能仅用 `L×4/3` 代替整个 wire。

不要为了预算反复物化完整 base64；沿既有标准编码/quote 入口计量，并由最终 materialization 精确确认。验证与结果缓冲使用同一 immutable bytes；并发窗口内 Σ编码缓冲受 Σ逐调用额度限制，decoded pixels 仍只有原单验证槽。旧/新 plan 同存和 compiler working set 继续由原 owner 检查，不能把 64 MiB 逻辑界宣称为 Python RSS 上限。

### 7.4 Safe point 与接纳后的保证

open tool batch 中不压缩、不切换 target、不重新分配已冻结资源。完成批次后回原 provider safe point；如完整输入不满足 G，沿原 ordinary compaction，再尝试下一次调用。成功保存的图片不能为了勉强编译被 REF_ONLY/OMITTED 静默删除。

初次工具结果本身不合规时返回明确工具错误；已经接纳后发现 quote/实际物化不一致属于实现错误，必须报资源/完整性失败并修复，不能把 SUCCESS 改成“图片省略”掩盖错误。

### 7.5 逐调用冻结值、传递链与 deadline

现有 `FrozenPostResponseResourceQuote` 只有聚合报价，不能声称执行端已经能取得 b[i]。本次在该 quote 内增加按原 call ordinal 排序的图片调用额度 tuple。每项是不可变的进程内值，包含：

- 原 `call_ordinal` 和 `tool_call_id`，用于与本条 assistant 的 calls exact-join。
- 当前冻结 tool exposure/borrow 中对应的 resolved builtin binding 值或现有精确身份，用于确认是同一个 `view_image`。它不是权限授权，不在 quote 阶段调用 PreToolUse、confirmation 或 attempt admission。
- 当前 `request.prepared_call.call.target.fact` 的既有冻结值引用。执行端据其 `input_modalities` 判断能力，不另存一个可变的 `supports_image` 或新 target fingerprint。
- 该调用的 C/L/W/T 额度 b[i]，以及统一 quote helper 所需的既有 base 计量信息。不得在执行时按最新剩余空间重新均分。

该 tuple 不保存 deadline、文件 bytes、解码事实、attempt 或未来 result receipt。普通非图片调用不产生图片额度；无法解析为合法 builtin 的调用保持原错误路径及其 R_base 预留，不凭同名伪造份额。

传递链必须明确落地：

```text
runner._quote_post_response_resources：生成聚合上界 + 有序图片调用额度
runner 原 effect-before gate：检查同一个 quote
assistant 正式接纳后：把该 quote 的额度传给 ToolBatchExecutor.execute
executor：与 calls / frozen surface 校验一次，按原 ordinal 取得对应值
KernelToolInvocationContext：携带本调用的同一冻结额度与 target fact
DirectKernelToolPort.invoke：模态检查、有界读取、验证、q <= b[i]
共同 settlement：只接纳已经满足额度的完整结果
```

不得通过全局 `call_id -> budget` map、registry、额外 token/MAC 或数据库找回这些值。沿已有对象传参足够；取消后仍需 settlement 的在途调用继续持有自己的同一个值。工具入口缺少应有额度、binding 或 call identity 不匹配属于内部合同错误，不能回退成无限额读取。

**deadline 的生命周期与上述额度不同。** executor 从有限窗口开始调度某调用，完成原授权步骤并进入 runtime invocation 后，按现有 `_deadlines.deadline(NONTERMINAL_TOOL_INVOCATION)` 建立该调用的绝对 deadline。它由 runtime 本次调用栈持有，传给 physical read 和异步 validator；无需写进 effect-before quote。未调度的逻辑 calls 不消耗这个 invocation watchdog，人工确认仍由原交互 owner 管理。已进入 invocation 后的 I/O admission、read、validator 排队和 decode 共用该值，不在阶段切换时续期。物理结果时间与已知结果结算仍按原 watchdog/drain 合同处理。

验收须证明两件不同的事：同一调用在 read/validate 之间不获得新超时；队列尾部调用不会因为 provider 响应产生得较早，就尚未调度已经耗尽自己的 invocation deadline。不得新增整批工具的 wall-clock 上限。

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

冻结的三级规则扩展覆盖工具图片：Tier 1 检测有效输入任何来源的 Image；纯文本目标遇到工具图片也不能直接安装。Tier 2 使用原多模态模型总结完整 source；最近三条真实用户输入按原规则选择，工具图片本身不使 recent 用户原话计数变化。Tier 3 原模型不可用时，P 将 source 中的工具 Image 也替换为普通 Text("[Image omitted]")，再由新文本模型总结；保持原纯文本交接 recent=0 和覆盖证明。

P 只作用于明确的交接/summary 候选，不修改原 canonical 工具结果、历史 UI、已安装 epoch 或重试 payload。用于临时投影的 typed 值须明确允许“图片经 P 后为纯文本”的合法形状，不能重新恢复原图；这不是新持久化省略类型。

这里的“纯文本目标”严格沿冻结 D4：input modalities 已知包含 text 且不含 image。unknown 不能因切换、provider 错误或工具名被推断为纯文本，不触发图片省略。Tier 2 未形成合法 successor 时也沿原可进入 Tier 3 的分支处理，不只涵盖网络不可用。

**必须补齐 destination evidence 路径，不能只让公共 compiler 支持图片。** 当前 `compaction/planner.py::DestinationToolEvidence` 仅有 `result_body: str | None`，`freeze_destination_dialogue_projection_plan()` 只取 `tool_result_body_text`；renderer 把字符串写成 `retained_result`，而后置的 `project_destination_dialogue_for_text_only_handover()` 只能替换已经存在的 Image。若原工具 Image 在此之前被降成文字，后面再调用 P 也不会产生省略文本。

本次沿原 owner 做以下 hard cut：

1. `DestinationToolEvidence` 的结果正文改为原完整 typed tool content（或没有正文），取自同一 canonical source item；替换文本-only 的结果正文 authority，不并存独立 `result_body` 和图片列表。原 call/result identity、状态、outcome ordinal 保持不变。
2. `freeze_destination_dialogue_projection_plan()` 从 `item.content` 冻结正文，保留全部有序 Text/Image；closure 仍无图片正文。不能从 Hook public projection 或派生 tool message 反向恢复证据。
3. Tier 3 的 coordinator 在调用现有 backbone/evidence 候选选择及渲染**之前**，对该冻结 plan 的工具证据正文逐 part 应用同一个 P：每个 Image 在原位置变成一个普通省略 Text。原 typed plan/canonical read 不被修改。无图工具文字原样保留，call/result 关联与状态不变；P 后的文字视图由同一结果重新派生，不能继续引用未投影的 `tool_result_body_text`。
4. destination renderer 消费上述 typed 正文，保留每条结果内部顺序和来源说明。Tier 3 输出完全是文字；例如两条保留的结果证据各含一张图，就各在所属正文中保留一个省略 Text，不能把整批图片合并成一个总提示。通用 typed renderer 遇到 Image 时输出对应 part，不能 stringify descriptor。
5. `project_destination_dialogue_for_text_only_handover()` 继续覆盖用户/其他 typed parts；已有普通省略 Text 原样保留，不解析标记、不二次重建图片。coordinator 的 quote、实际 summary 和 successor coverage 必须使用同一次 P 后的候选。

原完整 turn suffix、backbone/evidence 预算选择及排序不因此改变，工具 evidence 不被升级为全部强制保留。P 先覆盖候选 plan 的全部工具图片；被原选择器保留的结果必须渲染其对应省略 Text，没被选中的结果沿原省略证据语义处理，不能宣称新模型看过它。测试需强制覆盖“含图片结果证据被保留”和“因原预算选择未保留”两种情况，避免只测用户图片或只验证最终没有 Image。

### 8.4 Fork 与子代理

fork 沿原 imported call/result 映射复制 typed body 与 refs；blob 可复用，owner 和 call identity 归属子会话。无需在 clone 时重新读文件。已有 per-entry `copy_canonical_prompt_refs` 可复用，但 imported 工具结果、late、被 snapshot 覆盖/保留的组合必须测试。

ROOT 和 SUBAGENT_TASK 共享同一工具/结果链；不能只给 ROOT 支持图片。原父上下文文本 advisory 仍按冻结设计执行，不新增把全部工具图片跨代理重注入的机制。

## 9. Protocol-v3 与前端产品语义

### 9.1 读取接口

继续使用原内容读取请求的 `entry_id + image_ref_ordinal`，无需新 endpoint 或任意本地路径读取 API。[ReadContentRequest](src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto) 没有 caller scope 字段；[v3_gateway.py](src/pulsara_agent/terminal_protocol/v3_gateway.py) 从当前 attachment 确定 session。不得声称这里会比较一个请求并未携带的 ROOT/SUBAGENT scope。

`canonical_v3.py` 放开合法 TOOL_RESULT owner 后，校验当前 attachment/session 与 exact entry/ref owner 的归属、该 owner 自身 scope metadata 的自洽、workspace、body codec、ref ordinal 及 blob MIME/大小/digest。scope metadata 是结果本身的来源关系，不是另一个客户端授权参数；不能只凭 blob_id 读取图片，不能把其他 entry 的合法 ref 嫁接到当前结果。

同一 attached session 的 ROOT 页面可以查看其 SUBAGENT_TASK 活动结果图片，这是已有历史/活动产品范围；不能新增“caller scope 必须等于 result scope”的规则把它拒绝。跨 session、错 entry、错 ref owner 或 ordinal 仍失败。本次不扩 proto、不建立新的 caller-scope 许可路径。

history/snapshot/活动记录只传 descriptor，图片按需通过原 chunked read 获取。live 不传大图；工具结果 commit 后用原观察/补读路径取得正文与 refs。不要新增专门的图片完成事件或可靠投递 ACK。

### 9.2 左侧展示

`ToolTrace` 增加可选的 canonical typed result content，`resultEntryId` 仍是图片 owner；文字摘要从同一内容派生。覆盖常规 history、bootstrap snapshot、subagent activity、实时结果 commit 后 reconciliation 和断线重连各入口。

工具结果保留在聊天区左侧。卡片收起时只显示原工具摘要，不显示缩略图；展开后直接显示按容器自适应、保持完整比例的大图。点击大图使用原 Lightbox 放大。图片成功结果不重复展示 `Image loaded.`、原始 JSON 或 Figure 编号/链接，图片和放大弹窗均无编号 caption；失败结果继续使用原文本错误展示。

这替换此前的工具缩略图与 Figure 方案，不改变用户消息或队列原有的 Figure 展示。复用现有 `PromptContentView` 的工具显示 variant、读取与 Lightbox 生命周期；收起时卸载图片视图并回收 object URL，再次展开按相同 owner 读取。provider 合并消息继续使用 call_id 关联，UI 不新增编号或持久化状态。

不同工具结果不因 wire 合并而合成一个 UI 大气泡。不得展示“Pulsara 自动生成 user role”或把协议来源说明渲染成用户发言。用户侧只需知道哪个工具读了哪张图、是否成功，以及点击查看图片。

不得复用用户消息的编辑/重发/队列操作到工具结果上；read image 仅为历史附件查看，不再次触发模型工具或读取原本地文件。

## 10. 失败和一致性矩阵

| 情形 | 必须发生的行为 |
|---|---|
| 当前 target 的已知 modalities 不含 image | 明确工具错误；不读文件、不热改工具前缀 |
| 当前 target 的 modalities 为 None | 按 unknown 走普通请求；不探测、不自动省图，provider 拒绝保留原错误 |
| 权限拒绝/需要确认 | 原 decision/confirmation 路径；确认前不读图 |
| 不存在、目录、设备、FIFO、非法路径 | 有界文本错误；不阻塞等待无限输入 |
| 非法格式、损坏、动画、像素/metadata 超界 | 原验证 owner 拒绝；无图片 blob/refs |
| 扩展名与合法实际图片格式不同 | 以实际格式验证及冻结 MIME，不因扩展名不符而拒绝；上传入口的声明 MIME 核对不变 |
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
| ROOT 页面读取同 session 子代理结果图 | 沿原 attachment/session 与 exact result/ref owner 读取，不以 scope 不同为由拒绝 |
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
| L1 内容与存储 | typed 工具图片载体；验证 owner 的本地无声明 MIME 入口；事务 publication/refs；FULL confirm；reader ordinary/late 与 C 预检；canonical/public projection 分界 | PostgreSQL 真路径成功/回滚/确认/损坏/fork 测试通过；普通文本输出不变 |
| L2 投影与资源 | 组合来源 placement 与全部 consumer；cold/append/summary/prospective 共用；FULL 图片交付；R 额度 carrier 与 U_W/U_T；typed destination evidence/P | 两种 API 一组一条附件消息；actual 不超过 upper；prefix/replay/retry/late/Tier 3 证据测试通过 |
| L3 工具与并发 | 完整 builtin 注册、权限与 unknown 模态判断；冻结份额的执行端接收；文件有界取得；read+validate 窗口与单调用 deadline；取消/关闭 settlement | 真实文件并行读取、逆序完成、Hook/写工具屏障、物理 drain 通过；此时方可开放模型入口 |
| L4 浏览器与验收 | 原读取 API 支持工具 owner；左侧卡片展开大图/Lightbox、无 Figure；历史/活动/reconnect | 浏览器完整链路 + 两种正式 adapter 真实 provider dogfood，通过后记录验收 |

实施者每批先说明本批改变的 owner、现有能力如何复用、未改变哪些产品合同；不要另建 approval registry。所有验证与命令使用仓库 `.venv`/uv，frontend 使用现有 npm scripts。

## 13. 必须通过的验收

### 13.1 有限、针对性的自动化测试

| 编号 | 场景及断言 |
|---|---|
| L01 | 原 catalog/schema 只有 path；已由后续已知引用重读规格 hard cut 为 path/image_ref 恰有一个；read-only、concurrency、family、permission/recovery 分类一致；旧 epoch tools 不变 |
| L02 | 工作区相对路径、允许 `../` 的外部只读目标、绝对路径、`~`、`${PULSARA_HOME}` 行为与原 read owner 一致 |
| L03 | 文件不存在、目录、FIFO/device、空文件、伪装成图片的非法内容、损坏、动画、多帧、像素/PNG metadata 超界，均无成功图片 publication |
| L04 | 读取期间增长超过额度被 bounded read 拒绝；不通过先整文件读取再判超限实现 |
| L05 | 成功图片 bytes/digest/MIME/尺寸从文件验证贯穿 canonical 与实际 wire；原图字节无 resize/EXIF 修改 |
| L06 | 同一个文件被多个调用读取：独立 result/ref，多 occurrence 计量，blob 可复用 |
| L07 | 同批单图、多图、混合失败、同图重复、文字工具混合：只要有成功图片均为 1 条派生 user 图片消息；全部失败为 0 条 |
| L08 | Chat 与 Responses 使用相同 provider-neutral 消息；没有图片 tool output，也没有额外 canonical USER entry |
| L09 | 逆序物理完成仍按 call ordinal 展示/投影；不是按 result sequence 或完成时间拼图 |
| L10 | `[view, write, view]` 保持屏障；匹配 Hook/人工确认保持原时序；并发只作用于许可的读图段；已知不支持、已知支持、unknown 三态分别正确，执行期间修改设置不改变本次冻结判断 |
| L11 | 暂停 validator 后观察窗口：已读但未验证的调用不随 calls 总量无限增长；队列尾部只等待、不提前耗尽 invocation deadline；同次 read/validate 共用 deadline；取消与 Host close 后无后台物理读/解码遗留 |
| L12 | successful result body、blob、refs、tool row、原 event 同事务；故障注入后无 partial FULL；提交不明使用原 exact-confirm |
| L13 | reader 在读 blob 前计工具图片 E；metadata quote 覆盖 ordinary/late/base+suffix，不绕过 16 MiB |
| L14 | 图片成功结果不可被 FULL/COMPACT/REF_ONLY/OMITTED 候选链静默丢图；文本工具原降级仍可用 |
| L15 | R_base 原失败：没有 accepted assistant/工具授权/效果；图片额度失败：准确单调用错误，不伪称 SUCCESS |
| L16 | C/L/W/T 各自边界两侧；从 runner 到 runtime 传递同一有序份额，call/binding 错配不能执行；均分余数确定、错误调用额度不漂移；所有实际 normal/closure+late wire 与 D1 <= 冻结上界 |
| L17 | 极端长宽比、小字节大像素、大编码文件和包含 JSON 特殊字符的路径，不能破坏 D1/U_W/U_T 的证明 |
| L18 | retry byte-identical；读原文件后修改/删除，重放仍使用 blob；native replay 只替换 ASSISTANT 单来源，合并附件仍完整；组合 placement 与 final-wire 来源逐 occurrence 对应；增量安装不改 SYSTEM/tools/历史 messages |
| L19 | 已安装 closure 后 late 图片只追加新后缀；不修改旧合并 user；多 late 的分组/去重按真实 source cut |
| L20 | 普通 compact 摘要真正看到被选 source 图像；组合附件与 result 之间不是合法 cut，suffix coverage 遍历全部成员；保留尾组的图片不丢；ROOT 文本 advisory 不把附件认成用户原话；无图用户 recent 计数不受影响 |
| L21 | 已知纯文本目标 Tier 1 拒绝直交，Tier 2 原模型总结；Tier 3 用 typed DestinationToolEvidence 先 P 再选/渲染，保留结果中的每个 Image 对应一个省略 Text，未选结果沿原证据选择；recent=0、coverage 与 canonical 原图正确；unknown 不触发省图 |
| L22 | cold rebuild/fork/imported results/重复 compact/ROOT 与 subagent 均可恢复工具图片，且不会再次调用原路径 |
| L23 | TOOL_RESULT 的 entry/ref 读取走原 attachment/session 权限；跨 session、伪造 result entry、错误 ordinal/ref owner、owner metadata 不自洽均不能读取；同 session 的 ROOT 页面能读取子代理活动图片，无 caller-scope 参数或新许可路径 |
| L24 | 普通历史、bootstrap、live commit、subagent activity、重连均显示左侧工具图片，不出现右侧伪用户气泡 |
| L25 | 收起时仅工具摘要，展开后显示大图且无 Figure/重复成功文字；点击 Lightbox 无编号 caption；收起/切会话回收资源，加载失败不显示成另一会话图片；用户消息和队列 Figure 不变 |
| L26 | 原用户图文输入、队列编辑、模型切换、文本 read/edit、artifact 分页和工具 timing/citation 回归不变 |
| L27 | 本地无声明 MIME 由共享 validator 得出实际格式，合法图片扩展名不符也能读取；上传声明 MIME 不符仍拒绝；两入口共享单 slot/cleanup，均不按扩展名猜 MIME |
| L28 | 图片 settlement 绕过文本 artifact；canonical 保存完整 codec/refs；Hook/live/public projection 只含确定的短文字，不能消费 descriptor JSON 或反向覆盖 canonical |

现有相邻测试入口包括：`tests/test_kernel_image_input_k1.py`、`test_kernel_image_input_k2.py`、`test_kernel_image_input_k2_postgres.py`、`test_kernel_image_input_k3.py`、`test_kernel_image_input_k4_lifecycle.py`、`test_round7_1_provider_visible_tool_result_projection.py`、`test_round3_1_provider_input_prefix_continuity.py`、`test_round1_tool_output_artifact.py`、`test_conversation_fork.py`、`test_round5b_long_horizon_context_compaction.py`，以及 frontend 的 prompt-content-view、workbench-view、runtime-adapter/app 相关测试。

在这些 owner 附近补有行为意义的测试；不要为每个 getter/DTO 镜像写测试。迭代只跑相邻用例，最终做与实际改动面相称的验证。不得弱化断言、增加 skip/xfail 或改写旧实验结果来获得绿色。

### 13.2 真实 provider 与浏览器验收

通过 `LocalSettingsStore` 和 `require_pulsara_home()` 读取用户已有配置，选择可用的支持图像的 Chat 与 Responses 各一组。冻结测试 target，实际请求使用正式 compiler、wire materialization、transport 和工具入口，不能手拼请求替代生产链。

使用生成的普通测试图与可验证内容，不使用私人聊天缓存作为默认素材。要求模型真正调用本地 `view_image`；覆盖单图、同轮两个以上并行调用、一个成功一个失败、下一轮追问已读图、原文件删除后历史查看，以及压缩/交接关键路径。记录实际工具 calls、结果归属、完整 provider 输入、usage、canonical/ref 数量与 prefix 比较；只遮蔽真实凭据。

browser 验收经正常 UI 发起，让工具生成左侧图片结果，检查收起/展开的大图、无 Figure、放大、刷新/重连和错误态。可另用一次性数据库做 destructive/fork/取消组合；只覆盖内存注入的 PostgreSQL target，保存配置只读。不为验收修改用户模型配置、导出 API key 到环境、重置未核实数据库或创建长期后台服务。

验收记录必须区分“结构正确”“provider 接受”“模型确实使用视觉内容”。模型误读图片不自动归因于协议；provider 接受一个 HTTP 请求也不等于已证明它看到了正确图像。

2026-09-16 的真实 PostgreSQL、Chat、DeepSeek Responses 与浏览器结果记录在冻结图片设计 §17。以下边界本轮只由确定性自动化测试证明，没有宣称为真实 provider 端到端实验：调用中途取消或 Host close 时的物理解码回收、真实网络断链造成的 commit-ACK ambiguity、late 图片实际送达 provider、Tier 2/Tier 3 真实模型切换、protected tail 工具图片的真实 provider 保留，以及 ROOT/subagent 跨会话浏览器读取。真实 compact 场景中的旧工具组已被 summary 合法覆盖，因此 successor 无需永久再注入这些图片；相邻合同测试单独证明 protected tail 不可拆且保留整组图片。

## 14. 实施完成定义

以下条件全部满足才算完成：模型可调用并发本地读图；每个结果独立、完整、可重放地保存；两种 API 共用一套确定性分组投影；已安装前缀不变；D1/D2 与效果前资源保证覆盖图片；取消/late/compact/文本模型交接/fork 有确定行为；UI 始终左侧按工具结果展示；相邻测试与正式 provider/browser 验收通过。

最后更新原图片设计的实施状态与验收入口，保留历史实验为历史，不宣称旧 K4/U2 自动覆盖本功能。文档中所有“已实现/通过”必须来自新的实际代码与验证，不能用本文的设计条目代替。

## 附录 A：其他项目的借鉴边界

前序两个 Luna max 子代理只读核验了本地仓库。Codex 的 [view_image handler](../codex/codex-rs/core/src/tools/handlers/view_image.rs) 使用独立工具并返回图像 function output；OpenCode 的 [read](../opencode/packages/opencode/src/tool/read.ts) 复用读取工具，自有 [Chat adapter](../opencode/packages/llm/src/protocols/openai-chat.ts) 将 tool media 转成后续 user 图片消息，[Responses adapter](../opencode/packages/llm/src/protocols/openai-responses.ts) 则可直接返回多模态 function output。

这些代码只提供实现参考，不是 Pulsara 的数据/权限/资源权威。本稿采用用户明确选择的跨协议统一 user 附件投影，不照抄其他项目的 resize、错误占位降级、持久化 data URL、压缩丢图或 provider-family 分支。
