# Tool result adaptive preview 与 artifact 预算实施文档

_Created: 2026-07-04_

## 0. 结论

本轮要解决的问题不是 artifact 是否应该存在，而是“什么时候给模型较完整 preview，什么时候快速降级成极小 head+tail preview”。

推荐 v1：

- 保留完整输出归档阈值：`archive_threshold_chars = 8_000`。
- 引入 adaptive preview：
  - `<= 32_000 chars`：尽量给完整 inline preview，减少无意义 `artifact_read`。
  - `> 32_000 chars`：只给 head+tail preview，总预算默认 `8_000 chars`。
  - 可选 huge tier：`> 200_000 chars` 时总预算降为 `4_000 chars`。
- preview 必须携带 artifact ref、原始大小、preview policy、omitted middle size、建议 continuation read 参数。
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
DEFAULT_MAX_OUTPUT_CHARS = 20_000
MIN_TERMINAL_OUTPUT_CHARS = 512
```

`terminal` 和 `terminal_process` 的 `max_output_chars` 参数最大值也是 `DEFAULT_MAX_OUTPUT_CHARS`，也就是模型无法请求超过 20k 的 terminal preview。

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

- tool result 超过 8k 会被归档；
- 如果归档产生 refs，inline result 会被裁到 8k；
- 后续上下文组装也按 `LoopBudget.tool_result_context_chars = 8_000` 控制 tool result 总预算。

因此即使 terminal 工具内部可以生成 20k preview，进入模型上下文时稳定可见预算仍可能只有 8k。

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

---

## 3. 目标语义

### 3.1 三个阈值

推荐默认值：

```python
archive_threshold_chars = 8_000
complete_preview_chars = 32_000
large_preview_chars = 8_000
huge_output_chars = 200_000
huge_preview_chars = 4_000
```

含义：

| 输出大小 | 是否归档 | inline preview |
| --- | --- | --- |
| `<= 8k` | 默认不归档 | 全量 |
| `8k - 32k` | 归档 | 全量或近似全量 |
| `> 32k` | 归档 | head+tail，总 8k |
| `> 200k` | 归档 | head+tail，总 4k |

注意：`archive_threshold_chars` 不应升高。它是保真阈值，不是上下文预算阈值。降低或保持它比升高更安全。

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

[OUTPUT PREVIEW TRUNCATED: omitted 120542 chars from the middle.
Full retained output is available via artifact_read. Prefer reading from offset_chars=4096
if you need content after the visible head.]

```

### 3.4 artifact envelope

tool result 的 artifacts ref 附近应该暴露 preview metadata。可以放在 `ToolResultArtifactRef.metadata`、tool result payload、或 context renderer 包装层中。推荐至少包含：

```json
{
  "preview_policy": "head_tail",
  "preview_chars": 8192,
  "original_chars": 128734,
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
archive_threshold_chars: int = 8_000
complete_preview_chars: int = 32_000
large_preview_chars: int = 8_000
huge_output_chars: int = 200_000
huge_preview_chars: int = 4_000
tool_result_context_chars: int = 32_000
```

兼容字段：

- `inline_preview_chars` 可以保留一轮并映射到 `large_preview_chars`，但 hard cut 更清晰；
- 如果保留，必须避免名字误导，因为它不再是唯一 inline preview budget。

### 4.2 新增 adaptive preview helper

建议新增：

```python
@dataclass(frozen=True)
class AdaptivePreview:
    text: str
    policy: str
    original_chars: int
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

### 4.3 `ToolResultArtifactService.process_result()`

当前逻辑是：

```python
if refs and len(processed_output) > inline_preview_chars:
    processed_output = finalize_output(processed_output, max_chars=inline_preview_chars).text
```

推荐替换为：

```python
if refs:
    preview = build_adaptive_preview(result.output, self.options)
    processed_output = preview.text
    attach preview metadata to artifact refs / result metadata
```

注意：

- 对 `artifact_mode=NEVER` 的工具不处理；
- 对 structured JSON 工具要谨慎，head+tail 可能破坏 JSON。`STRUCTURED_JSON` 可继续使用 old clipping 或单独 schema summary，不在本轮扩大。
- terminal large output 是优先应用对象。

### 4.4 context budget

当前：

```python
LoopBudget.tool_result_context_chars = 8_000
```

如果 tool result 自身已经 adaptive preview，后续 context budget 不应再把 32k medium preview 裁掉。推荐提升为：

```python
tool_result_context_chars = 32_000
```

但这只是“每轮所有 tool result 的总上下文预算”，不是无限制。巨大输出已经被 adaptive preview 降到 8k/4k，所以不会因为提升预算而吃穿上下文。

### 4.5 terminal `max_output_chars`

当前 terminal schema 最大值是 20k。

有两种选择：

1. 继续保持 20k，让 artifact service 决定最终 preview；
2. 提升 `DEFAULT_MAX_OUTPUT_CHARS` 到 32k，与 complete preview 对齐。

推荐 v1 选择 2：

```python
DEFAULT_MAX_OUTPUT_CHARS = 32_000
```

但注意 foreground terminal 的 `artifact_candidates` 存的是 `full_output_text`，所以即使 terminal payload preview 是 32k，artifact 仍保存完整输出。

### 4.6 `artifact_read` prompt / descriptor

更新 `artifact_read` 描述：

- 当 tool result 已提供足够 preview 时，不要读取 artifact；
- 如果需要被省略的中间部分，优先使用 result 提供的 `suggested_offset_chars`；
- 不要默认从 0 重读已可见 preview。

---

## 5. 测试矩阵

### 5.1 preview policy

- `<= 8k` 输出：
  - 不产生 artifact 或不强制归档；
  - inline 全量；
  - policy 为 `full` 或无 policy。
- `8k - 32k` 输出：
  - 产生 artifact；
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

- artifact ref / payload 暴露 suggested offset。
- `artifact_read(offset_chars=suggested_offset)` 返回中间后续内容。
- 不要求模型必须读 artifact 才能回答 preview 内的问题。

### 5.3 context assembly

- `LoopBudget.tool_result_context_chars=32k` 下，中等输出不被二次裁到 8k。
- 多个 tool results 总量超过预算时仍按现有顺序裁剪。
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
- streaming terminal overflow 的 truncation notice 与 adaptive preview metadata 不冲突。

### 5.5 real trajectory

复现天气 / Firecrawl scrape 场景：

1. `firecrawl scrape` 输出中等长度页面；
2. 模型收到足够 preview；
3. 若问题可以直接回答，不再额外调用 `artifact_read`；
4. 对真正巨大页面，模型看到 head+tail 和 artifact hint，并按需读取中间。

---

## 6. 验收标准

- 中等长 tool output 不再频繁触发“多一次 artifact_read”。
- 巨大 output 不会把 32k preview 和 artifact_read 全量叠加成双倍成本。
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

