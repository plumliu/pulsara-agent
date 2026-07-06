# Tool result adaptive preview 与 artifact 预算实施文档

_Created: 2026-07-04_

## 0. 结论

本轮要解决的问题不是 artifact 是否应该存在，而是“什么时候给模型较完整 preview，什么时候快速降级成极小 head+tail preview”。

推荐 v1：

- 保留完整输出归档阈值：`archive_threshold_bytes = 8_000`。
- 引入 adaptive preview：
  - `<= 32_000 chars`：尽量给完整 inline preview，减少无意义 `artifact_read`。
  - `> 32_000 chars`：只给 head+tail preview，总预算默认 `8_000 chars`。
  - 可选 huge tier：`> 200_000 chars` 时总预算降为 `4_000 chars`。
- preview 必须携带 durable artifact ref、原始字符数/字节数、preview policy、omitted middle chars、建议 continuation read 参数。
- terminal / terminal_process 的 preview 数据源必须是完整 retained output（`full_output_text` / primary artifact candidate），不能只读 `result.output`，因为 `result.output` 往往已经是工具内部裁剪后的 JSON preview。
- streaming terminal 不能只靠 artifact service 后处理；它需要在 terminal streaming builder / runtime 中输出同一套 preview envelope，否则已发出的 `ToolResultTextDeltaEvent` 不会被改写。
- streaming live 阶段必须使用保守固定 head cap，而不是最终 policy；最终中等输出在 finish 补齐，最终巨大输出只补 marker + tail。
- `artifact_read` 仍是完整真值读取入口，但模型默认不应从 0 重读已经看过的 preview。

这让 Pulsara 保留三层存储的审计优势，同时避免中等输出总是多一次 `artifact_read`，也避免巨大输出吃掉上下文。

---

## 1. 我们是怎么发现这个问题的

真实 REPL trajectory 中，用户询问成都天气。模型使用 terminal/firecrawl/curl 获取信息：

```text
firecrawl scrape "nmc.cn/.../chengdu.html"
artifact_read ...
firecrawl scrape "weather.com.cn/..."
curl wttr.in/Chengdu?lang=zh
```

第六步 `firecrawl scrape` 产生较长输出后，Pulsara 返回了截断 preview 和 artifact。模型又调用 `artifact_read` 读取完整 retained output。

这带来两个体验问题：

1. 相比常见 agent 产品，Pulsara 更容易多一次工具调用；
2. 如果单纯提高 preview 阈值，又会导致更大的上下文浪费。

用户指出了关键公式：

```text
delta = 真实输出 - 工具输出阈值
总上下文成本 ≈ 工具输出阈值 + artifact_read 全量读取
```

如果 preview 很大但仍不够，模型可能先吃一遍大 preview，再从 artifact 读一遍全量或大块内容，实际成本接近重复。

因此问题不是“阈值大还是小”，而是需要分层：

- 中等输出：一次给够；
- 巨大输出：立刻给很小但信息密度高的 head+tail，并引导按需读取 artifact。

---

## 2. 当前代码语义

### 2.1 terminal preview 上限

代码入口：

- `src/pulsara_agent/tools/builtins/schemas.py`
- `src/pulsara_agent/tools/builtins/terminal.py`
- `src/pulsara_agent/tools/builtins/terminal_process.py`

当前默认：

```python
DEFAULT_MAX_OUTPUT_CHARS = 32_000
MIN_TERMINAL_OUTPUT_CHARS = 512
```

`terminal` 和 `terminal_process` 的 `max_output_chars` 参数最大值也是 `DEFAULT_MAX_OUTPUT_CHARS`，也就是模型无法请求超过 32k 的 terminal preview。过小的正整数会按 schema clamp 到 `MIN_TERMINAL_OUTPUT_CHARS=512`；artifact service 必须复用同一解释，不能用原始参数重新计算 preview policy。

### 2.2 artifact 归档与 inline preview

代码入口：

- `src/pulsara_agent/runtime/tool_artifacts.py`

当前默认：

```python
DEFAULT_TOOL_ARTIFACT_THRESHOLD_CHARS = 8_000
DEFAULT_TOOL_ARTIFACT_PREVIEW_CHARS = 8_000
DEFAULT_TOOL_RESULT_CONTEXT_CHARS = 8_000
```

语义：

- tool result 超过 8k bytes 会被归档；
- 如果归档产生 refs，inline result 会被裁到 8k；
- 后续上下文组装也按 `LoopBudget.tool_result_context_chars = 8_000` 控制 tool result 可见预算。

因此即使 terminal 工具内部可以生成 20k preview，进入模型上下文时稳定可见预算仍可能只有 8k。

需要特别注意两处当前实现细节：

1. 对普通工具，`ToolExecutionResult.output` 通常就是完整模型可见文本；但对 terminal / terminal_process，它通常已经是工具内部按 `max_output_chars` 裁剪后的 JSON payload，完整输出在 `artifact_candidates` 的 `full_output_text` / `combined_output` candidate 中。
2. streaming terminal 会先发 `ToolResultTextDeltaEvent`，随后 executor 因 `streamed_output_complete=True` 不再发最终 `result.output` delta。因此只在 `ToolResultArtifactService.process_result()` 里改 output，无法改变 streaming terminal 的模型可见 preview。

### 2.3 artifact_read

代码入口：

- `src/pulsara_agent/tools/builtins/artifact.py`

当前默认：

```python
DEFAULT_ARTIFACT_READ_CHARS = 20_000
MAX_ARTIFACT_READ_CHARS = 100_000
```

`artifact_read` 支持：

- `offset_chars`
- `max_chars`
- `has_more`

这说明代码已经具备分页读取能力，但 tool result envelope 没有充分引导模型“从 preview 后继续读”，模型容易从 `offset_chars=0` 重读。

### 2.4 契约现状

`contracts/TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md` 已经定义三层：

1. Streaming Preview；
2. Tool Result Artifact；
3. Terminal Completion Event。

当前契约强调：

- preview 不是完整事实；
- artifact 是完整输出权威；
- artifact_read 是显式读取入口；
- threshold 已统一到 8KB。

本设计不是推翻三层契约，而是在 Layer 1 preview 上增加 adaptive policy。

本设计还需要修正一处命名漂移：当前部分字段叫 `*_chars`，但归档判断实际用的是 UTF-8 bytes；`artifact_read.offset_chars` 和 preview 裁剪则是字符 offset。v1 必须把这两个单位显式拆开，避免中文网页、emoji、日志中的多字节字符造成边界测试漂移。

---

## 3. 目标语义

### 3.1 阈值与单位

推荐默认值：

```python
archive_threshold_bytes = 8_000
complete_preview_body_chars = 32_000
large_preview_chars = 8_000
huge_output_chars = 200_000
huge_preview_chars = 4_000
streaming_live_head_cap_chars = min(
    configured_streaming_live_head_cap_chars,
    huge_policy_visible_head_chars_after_notice,
)
tool_result_message_context_chars = 36_000
```

单位约定：

- `archive_threshold_bytes`：只用于决定是否归档完整输出，按 UTF-8 bytes 计算；
- `*_preview_chars` / `*_output_chars` / `complete_preview_body_chars`：只用于模型可见 preview 和 `artifact_read.offset_chars`，按 Python `str` 字符数计算；
- `streaming_live_head_cap_chars`：streaming 阶段已发 delta 的保守上限，必须在不知道最终输出大小时也安全；
- `tool_result_message_context_chars`：最终 tool result message 的 aggregate 预算，必须覆盖 preview body + JSON/envelope 开销；
- artifact record 继续保存 `size_bytes`，preview metadata 额外保存 `original_chars`，两者都要暴露给 inspector / model payload。

含义：

| 输出大小 | 是否归档 | inline preview |
| --- | --- | --- |
| `<= 8k` | 默认不归档 | 全量 |
| `8k - 32k` | 归档 | 全量或近似全量 |
| `> 32k` | 归档 | head+tail，总 8k |
| `> 200k` | 归档 | head+tail，总 4k |

注意：`archive_threshold_bytes` 不应升高。它是保真阈值，不是上下文预算阈值。降低或保持它比升高更安全。

### 3.2 preview policy

新增 preview policy 概念：

```text
full
head_tail
head_tail_huge
```

`full`：

- 输出未超过 complete preview；
- inline output 尽量完整；
- 若已归档，仍给 artifact ref。

`head_tail`：

- 输出超过 complete preview；
- inline output = head + omission marker + tail；
- 总长度约等于 `large_preview_chars`。

`head_tail_huge`：

- 输出超过 huge threshold；
- inline output 更短；
- 避免超大网页、测试日志、crawl 结果撑爆上下文。

### 3.3 head/tail 比例

推荐默认：

```text
head_ratio = 0.65
tail_ratio = 0.35
```

理由：

- head 通常有命令、页面标题、参数、schema、开头上下文；
- tail 通常有最终错误、统计、摘要、exit status；
- 中间缺失可以通过 artifact_read 精准分页。

裁剪 marker 示例：

```text

[OUTPUT TRUNCATED / PREVIEW: omitted 120542 chars from the middle.
Full retained output is available via artifact_read. Prefer reading from offset_chars=4096
if you need content after the visible head.]

```

### 3.4 durable preview metadata

preview metadata 必须落在 durable message/event fact 中，不能只由 context renderer 临时拼接。v1 冻结为：

- 新增 `ToolResultPreviewMetadata`（或等价 Pydantic model）；
- `ToolResultArtifactRef` 增加 `preview: ToolResultPreviewMetadata | None`；
- `ToolResultArtifactRef.to_model_payload()` 输出 `preview`；
- `ToolResultEndEvent.artifacts`、assembler、context renderer、compaction renderer、inspector 都只从 `ToolResultArtifactRef.preview` 读取；
- `ToolResultArtifactRecord.metadata` 可以冗余保存同一份 preview metadata，供 SQL inspector / 后续索引使用，但不作为唯一事实源。
- 多 artifact 结果中，只有作为 preview source 的 primary text artifact ref 挂 `preview`；其他 artifact ref 保持 `preview=None`，除非它们各自有独立 preview。不要把同一份 preview 复制到所有 artifact 上。

不要选择“只塞进 `ToolExecutionResult.metadata`”或“只在 context renderer 包装”的方案，因为：

- `ToolExecutionResult.metadata` 不一定进入 durable event；
- context renderer 临时包装会在 compaction / resume / inspect 中丢失；
- streaming terminal 的最终模型可见 block 是由事件 assembler 重建，必须依赖 event 中的 artifact ref。

推荐字段至少包含：

```json
{
  "preview_policy": "head_tail",
  "preview_chars": 8192,
  "original_chars": 128734,
  "original_bytes": 180245,
  "omitted_middle_chars": 120542,
  "visible_head_chars": 5324,
  "visible_tail_chars": 2868,
  "read_more": {
    "tool": "artifact_read",
    "artifact_id": "...",
    "suggested_offset_chars": 5324,
    "suggested_max_chars": 20000
  }
}
```

关键点：

- 不引导模型从 0 重读；
- 告诉模型 preview 已经覆盖了哪些区域；
- 如果用户问题已经能由 preview 回答，就不需要 artifact_read；
- 如果需要中间细节，从 `suggested_offset_chars` 或更具体 offset 继续。

生成顺序必须钉死：`read_more.artifact_id` 只有在 `_archive_candidate()` 生成 artifact id 后才知道。因此 v1 应该先选定 preview source / preview seed，再归档得到 artifact id，最后构造 final `ToolResultPreviewMetadata` 并同时写入：

1. primary `ToolResultArtifactRef.preview`；
2. 对应 `ToolResultArtifactRecord.metadata["preview"]`；
3. model payload / inspector 投影。

如果实现为 `_archive_candidate(..., preview_seed=...)`，该函数必须返回已经带 final preview 的 ref，并把同一份 final preview 写入 SQL index metadata。禁止出现 event ref 有 preview、SQL index metadata 没 preview，或 preview 里的 artifact_id 为空的中间态。

---

## 4. 代码落脚点

### 4.1 `ToolResultArtifactOptions`

当前：

```python
archive_threshold_chars: int = 8_000
inline_preview_chars: int = 8_000
tool_result_context_chars: int = 8_000
```

推荐改为：

```python
archive_threshold_bytes: int = 8_000
complete_preview_body_chars: int = 32_000
large_preview_chars: int = 8_000
huge_output_chars: int = 200_000
huge_preview_chars: int = 4_000
streaming_live_head_cap_chars: int = 2_600  # huge_preview_chars * 0.65
tool_result_message_context_chars: int = 36_000
```

兼容字段：

- `archive_threshold_chars` 当前名字与实际 bytes 语义不一致，本实现 hard cut 为 `archive_threshold_bytes`；
- `inline_preview_chars` 不再保留；新版只使用 `complete_preview_body_chars` / `large_preview_chars` / `huge_preview_chars`；
- `tool_result_context_chars` 仍是 `LoopBudget` 的运行时上下文字段名，但 artifact options 中不再使用旧名，改为 `tool_result_message_context_chars`。

### 4.2 新增 adaptive preview helper

建议新增：

```python
@dataclass(frozen=True)
class AdaptivePreview:
    text: str
    policy: str
    original_chars: int
    original_bytes: int
    preview_chars: int
    visible_head_chars: int
    visible_tail_chars: int
    omitted_middle_chars: int
```

函数：

```python
def build_adaptive_preview(text: str, options: ToolResultArtifactOptions) -> AdaptivePreview:
    ...
```

落点：

- `src/pulsara_agent/runtime/tool_artifacts.py`
- 或新增 `src/pulsara_agent/runtime/tool_preview.py`

### 4.3 完整输出数据源选择

新增 adaptive preview 时必须先解决“用哪份文本构造 preview”：

```python
def select_preview_source(
    result: ToolExecutionResult,
    archived_candidates: tuple[ToolResultArtifactCandidate, ...],
) -> PreviewSource:
    ...
```

推荐优先级：

1. 若存在已归档的 text candidate，且 role 是 terminal 的 `combined_output` / 通用 `output`，使用 candidate.text 作为完整 preview source；
2. 否则使用 `result.output`；
3. structured JSON 工具若 candidate.text 是完整 JSON，可继续走 JSON-aware policy；否则不要用 head+tail 破坏 JSON。

这条规则是 terminal 正确性的核心：terminal / terminal_process 的 `result.output` 通常是已经裁剪后的 JSON payload，完整输出在 `artifact_candidates` 的 `full_output_text` / `combined_output` candidate 中。普通工具则通常可以直接使用 `result.output`。

### 4.4 `ToolResultArtifactService.process_result()`

当前逻辑是：

```python
if refs and len(processed_output) > inline_preview_chars:
    processed_output = finalize_output(processed_output, max_chars=inline_preview_chars).text
```

推荐替换为：

```python
if refs:
    source = select_preview_source(result, archived_candidates)
    preview = build_adaptive_preview(source.text, self.options)
    processed_output = rewrite_result_output_with_preview(result, preview)
    attach preview metadata to ToolResultArtifactRef.preview
```

注意：

- 对 `artifact_mode=NEVER` 的工具不处理；
- 对 structured JSON 工具要谨慎，head+tail 可能破坏 JSON。`STRUCTURED_JSON` 可继续使用 old clipping 或单独 schema summary，不在本轮扩大。
- terminal large output 是优先应用对象。
- 对 terminal / terminal_process，`rewrite_result_output_with_preview()` 不应把整个 tool result 替换成裸文本；它应解析原 JSON payload，替换其中的 `output` 字段，并加入 `preview_policy` / `output_preview_chars` / `output_original_chars` 等字段，保持原有 `exit_code`、`cwd`、`process_id` 等结构。
- preview metadata 的 final 版本必须在 artifact id 可用后构造。推荐流程是：
  1. 选出 primary preview source candidate；
  2. 用 source text 构造不含 artifact id 的 preview seed；
  3. 归档 primary candidate，获得 `artifact_id`；
  4. 把 `artifact_id` 写入 preview 的 `read_more`；
  5. 将 final preview 同时挂到 primary `ToolResultArtifactRef.preview` 和 `ToolResultArtifactRecord.metadata["preview"]`。
- 多 artifact tool result 只给 primary preview source 对应的 artifact ref 挂 preview；其他 refs 不共享这份 preview。

### 4.5 streaming terminal 专门落点

streaming terminal 不能只靠 `ToolResultArtifactService.process_result()`，原因是 `ToolResultTextDeltaEvent` 已经在工具执行期间发出，executor 最终又因为 `streamed_output_complete=True` 不再发最终 `result.output` delta。

v1 推荐：

- 把 `_StreamingTerminalJsonBuilder` 升级为使用同一套 `build_adaptive_preview()` 参数；
- streaming 期间仍然实时发 head preview，保持交互体验；
- 但 streaming live head cap 不能来自“最终 policy 的 `visible_head_chars`”，因为最终 policy 要到 finish 才知道，而已经发出的 `ToolResultTextDeltaEvent` 不能撤回；
- live 阶段必须使用固定保守 cap：它不是简单的 `huge_preview_chars * head_ratio`，而是用同一套 `build_adaptive_preview()` 在 huge tier 下计算出的 `visible_head_chars`，也就是扣除 truncation notice 之后的 head budget。默认约 `2_466 chars`。这保证即使最终输出属于 huge tier，早期 streaming delta 也不会超过 durable preview metadata 声明的可见正文 head；
- builder 需要持续累计完整 redacted output，或从 terminal result 的 `full_output_text` 读取完整文本，在 finish 时决定最终 policy：
  - `full`：如果输出最终属于中等输出，finish 时补齐 live head 之后尚未发出的正文，使模型看到完整/近似完整输出；
  - `head_tail` / `head_tail_huge`：finish 时只补 truncation marker + tail preview + preview metadata，不补齐中间正文；
- 如果输出超过 complete preview，finish 时在 JSON suffix 中补充 `tail_preview`、`preview_policy`、`original_chars`、`omitted_middle_chars`、`visible_head_chars`、`visible_tail_chars`；
- `output` 字段可以保持“streamed head + truncation notice”，但模型必须能在同一个 tool result JSON 中看到 tail；
- streaming suffix 中的 preview metadata 是 non-durable / display metadata：它可以包含 `preview_policy`、`original_chars`、`omitted_middle_chars`、head/tail chars，但不能要求包含 final `artifact_id`；
- 带 `artifact_id` 的 durable preview 只以 executor finalize 后的 `ToolResultArtifactRef.preview` 为准，因为 artifact service 归档发生在 builder finish 之后。

这样 streaming terminal 的模型可见语义与 non-streaming terminal 一致：中等输出尽量完整，巨大输出给 head+tail 和 continuation hint。

### 4.6 context budget

当前：

```python
LoopBudget.tool_result_context_chars = 8_000
```

如果 tool result 自身已经 adaptive preview，后续 context budget 不应再把 32k medium preview 裁掉。推荐提升为：

```python
complete_preview_body_chars = 32_000
tool_result_message_context_chars = 36_000
```

同时必须修正预算语义：当前 `msg_to_llm_messages()` 在每个 message / render 调用内重置 `remaining_tool_chars`，所以它不是“整个 context 的 aggregate budget”。v1 要把它改成真正的 per-context aggregate budget：

- `msg_to_llm_messages()` 创建一个共享的 tool-result message render budget；
- `_assistant_messages()`、`_tool_result_messages()`、`_textual_parts()` 都消耗同一个 budget；
- 多个 tool result 的最终 model-facing message 累计超过 `tool_result_message_context_chars` 时，后续结果继续被裁剪/省略；
- `complete_preview_body_chars=32_000` 是 preview body 预算，不是最终 tool result message 的总长度；
- terminal JSON payload 和 context renderer 的 `{"output_preview": ..., "artifacts": ...}` envelope 都会额外消耗字符，因此 `tool_result_message_context_chars` 必须略高于 `complete_preview_body_chars`，默认建议 `36_000`；
- renderer 不能先按整段 JSON/envelope 粗暴裁剪掉 output 字段；推荐对 tool result payload 进行 JSON-aware 渲染：优先保护 `output` / `output_preview` body 到 body budget，再在 envelope 层保留 artifacts 和 preview metadata。

如果实现阶段暂时不做 aggregate budget，文档和验收必须降级为“per message budget”。但推荐不要降级，因为用户最担心的是多个工具输出累计把上下文吃穿。

### 4.7 terminal `max_output_chars`

当前 terminal schema 最大值是 32k。

有两种选择：

1. 继续保持 20k，但 terminal runtime / artifact service 必须从完整 artifact candidate 生成 final preview；
2. 提升 `DEFAULT_MAX_OUTPUT_CHARS` 到 32k，与 complete preview 对齐。

推荐 v1 选择 2：

```python
DEFAULT_MAX_OUTPUT_CHARS = 32_000
```

但注意：

- foreground terminal 的 `artifact_candidates` 存的是 `full_output_text`，所以即使 terminal payload preview 是 32k，artifact 仍保存完整输出；
- 对 streaming terminal，`DEFAULT_MAX_OUTPUT_CHARS=32_000` 只能表示“中等输出最终可补齐到 32k”的上限，不能作为 live streaming head cap；live head cap 必须使用扣除 notice 后的固定保守 `streaming_live_head_cap_chars`。

### 4.8 `artifact_read` prompt / descriptor

更新 `artifact_read` 描述：

- 当 tool result 已提供足够 preview 时，不要读取 artifact；
- 如果需要被省略的中间部分，优先使用 result 提供的 `suggested_offset_chars`；
- 不要默认从 0 重读已可见 preview。

### 4.9 推荐实施顺序

不要先改阈值。推荐顺序：

1. **Preview metadata schema**：新增 `ToolResultPreviewMetadata`，扩展 `ToolResultArtifactRef.preview`、event assembler、model payload、compaction、inspector。
2. **Adaptive preview helper + 单位拆分**：新增 `build_adaptive_preview()`；把归档阈值改为 bytes 语义，preview/read offset 保持 chars 语义。
3. **JSON-aware context renderer + aggregate budget**：把 `tool_result_message_context_chars` 改成真正 per-context aggregate budget，并保留 `complete_preview_body_chars=32k` 的 body 语义，防止多工具输出累计失控，同时确保 artifact refs / preview metadata 不因 envelope 开销丢失。
4. **非 streaming 普通工具 / terminal**：在 artifact service 中选择完整 preview source；terminal JSON payload 走 JSON-aware rewrite。
5. **Streaming terminal**：升级 `_StreamingTerminalJsonBuilder`，finish suffix 补 tail preview 与 metadata，避免 streaming 路径绕过 adaptive policy。
6. **Prompt / descriptor**：更新 `artifact_read` 和 terminal 工具描述，引导模型不要默认从 0 重读。

这个顺序能避免“先放大到 32k，但 terminal 仍只给旧 preview / 多工具输出吃穿上下文”的中间危险态。

---

## 5. 测试矩阵

### 5.1 preview policy

- `<= 8k` 输出：
  - 若 UTF-8 bytes `<= 8_000`，不产生 artifact 或不强制归档；
  - inline 全量；
  - policy 为 `full` 或无 policy。
- `8k - 32k` 输出：
  - 若 UTF-8 bytes `> 8_000`，产生 artifact；
  - inline 仍完整；
  - policy 为 `full`;
  - context renderer 不裁掉它。
- `> 32k` 输出：
  - 产生 artifact；
  - inline 为 head+tail；
  - 总 preview 约 8k；
  - metadata 包含 omitted middle 与 suggested offset。
- `> 200k` 输出：
  - 产生 artifact；
  - inline 为更短 head+tail；
  - 总 preview 约 4k。

### 5.2 artifact_read continuation

- `ToolResultArtifactRef.preview` / model payload 暴露 suggested offset。
- `artifact_read(offset_chars=suggested_offset)` 返回中间后续内容。
- 不要求模型必须读 artifact 才能回答 preview 内的问题。

### 5.3 context assembly

- `complete_preview_body_chars=32k` 下，单个中等输出的 preview body 独占预算时不被二次裁到 8k。
- `tool_result_message_context_chars` 略高于 32k，能容纳 preview body 加 JSON/envelope/artifact metadata 开销。
- 多个 tool results 最终 model-facing message 总量超过 aggregate budget 时，后续结果按稳定顺序裁剪/省略；对 artifact tool result，超预算时仍保留极简 artifact ref / read-more 指针，但不能继续无上限塞入完整 envelope 与 preview metadata。
- 预算不在每个 message 内重置；同一次主模型 context build 内所有 tool result 共享一个 budget。
- renderer 对 terminal JSON / artifact envelope 做 JSON-aware budgeting，避免为了保 envelope 而裁掉 body，或为了 body 而丢 artifact refs。
- artifact refs 不因 preview 裁剪而丢失。

### 5.4 terminal integration

- foreground terminal 输出 12k：
  - artifact 存在；
  - inline 接近完整；
  - 模型不需要 artifact_read 即可看到全部。
- foreground terminal 输出 60k：
  - artifact 存在；
  - inline head+tail；
  - payload 明确 truncated / preview_policy。
- foreground terminal 输出 60k 且 `result.output` 只有 20k JSON preview：
  - adaptive preview 仍基于 `artifact_candidates[combined_output].text`；
  - 不能基于已经裁剪过的 `result.output` 误判为中等输出。
- streaming terminal overflow：
  - streaming live head 不超过固定 `streaming_live_head_cap_chars`，不能先发 32k；
  - 中等输出在 finish 时补齐 live head 之后的剩余正文；
  - 巨大输出在 finish 时只补 truncation marker + tail preview，不补中间正文；
  - finish suffix 中出现 tail preview 与 display preview metadata，但不要求包含 final artifact id；
  - artifact ref 上有带 artifact id 的 durable preview metadata。

### 5.5 durable metadata / compaction / inspect

- `ToolResultArtifactRef.preview` 进入 `ToolResultEndEvent`，被 assembler 重建到 `ToolResultBlock.artifacts`。
- 旧 event log 中 artifact refs 没有 `preview` 字段时，replay / inspect / compact 仍成功，且按 legacy artifact payload 展示。
- 多 artifact tool result 中，只有 primary text artifact ref 有 preview；其他 refs 的 `preview is None`。
- streaming suffix 的 display metadata 可以没有 `artifact_id`；测试应断言模型 payload 最终以 `ToolResultArtifactRef.preview.read_more.artifact_id` 为 durable truth。
- final preview 的 `read_more.artifact_id` 非空，且与 primary ref 的 `artifact_id` 一致。
- SQL/index `ToolResultArtifactRecord.metadata["preview"]` 与 event ref preview 一致。
- context renderer 的 model payload 包含 `preview`。
- compaction renderer 渲染 tool result 时保留 artifact id、size bytes、preview policy、visible head/tail chars、suggested offset。
- inspector 能解释某次 tool result 为什么只显示 head+tail，以及完整输出在哪里读。

### 5.6 字符 / 字节边界

- 中文网页或 emoji 文本：UTF-8 bytes 超过 8k 但 chars 小于 8k 时，按 bytes 归档，但 preview/read offset 仍按 chars 正确工作。
- `original_bytes` 与 artifact `size_bytes` 一致。
- `original_chars`、`visible_head_chars`、`visible_tail_chars`、`suggested_offset_chars` 按字符数计算。

### 5.7 real trajectory

复现天气 / Firecrawl scrape 场景：

1. `firecrawl scrape` 输出中等长度页面；
2. 模型收到足够 preview；
3. 若问题可以直接回答，不再额外调用 `artifact_read`；
4. 对真正巨大页面，模型看到 head+tail 和 artifact hint，并按需读取中间。

---

## 6. 验收标准

- 中等长 tool output 不再频繁触发“多一次 artifact_read”。
- 巨大 output 不会先 streaming 32k head，再叠加 tail / artifact_read 全量形成双倍成本。
- 中等 output 的 32k preview body 不会被 JSON/envelope 开销二次裁掉。
- artifact 仍是完整输出权威。
- preview 明确标注自己不是完整事实。
- `artifact_read` 支持 continuation 读取的 affordance 在模型可见结果中明确呈现。
- 三层契约仍成立，没有新增第四条 output storage 侧路。

---

## 7. 非目标

本设计不做：

- 不取消 artifact_read；
- 不取消 artifact 归档；
- 不把完整 artifact 默认塞进上下文；
- 不为 terminal 单独建立另一套 output storage；
- 不解决 read_file workspace 限制问题；那是另一份文档的主题。
