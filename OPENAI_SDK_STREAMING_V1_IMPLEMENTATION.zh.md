# OpenAI SDK Streaming V1 实施计划

## 0. 当前基线

这份文档只规划 `src/pulsara_agent/llm/adapters/openai/` 的下一步改造，不包含 Terminal tool 真流式改造。

当前仓库里的事实：

1. `LLMRuntime.stream()` 已经是 `AsyncIterator[AgentEvent]` 形态，并在 transport 外层包裹 `ReplyStartEvent` / `ReplyEndEvent`。
2. `LLMTransport.stream()` 已经是 provider-neutral 的流式协议，适合直接承接 SDK streaming。
3. `OpenAIResponsesTransport` 现在不是真流式：没有 `_mock_events` 时通过 `urllib.request.urlopen(...).read()` 拿完整 JSON，再用 `response_to_agent_events()` 合成事件。
4. Responses 的事件翻译、payload 构造、tool call id 映射已有测试覆盖，尤其是 `response.output_text.delta`、`response.function_call_arguments.delta`、`response.output_item.done`。
5. `_AgentEventBuilder` 目前藏在 `responses.py`，但它其实是 OpenAI 两种协议共享的 Pulsara event builder。
6. `build_llm_runtime()` 目前只注册 `OpenAIResponsesTransport`。
7. `pyproject.toml` 目前没有 `openai` 依赖。
8. Terminal backend 虽然内部增量读取 stdout/stderr，但 `ToolExecutor.execute()` 仍只在工具完成后发一次最终 `ToolResultTextDeltaEvent`。这是 tool execution protocol 问题，不应塞进 OpenAI adapter PR。

官方 API 形态需要在实现时用 OpenAI SDK 实测/fixture 固定。Responses streaming 官方事件包含 `response.output_text.delta`、`response.function_call_arguments.delta`、`response.reasoning_summary_text.delta`、`response.completed` 等；Chat Completions streaming 使用 `chat.completion.chunk`，核心增量在 `choices[*].delta.content` 和 `choices[*].delta.tool_calls`，usage 通常依赖 `stream_options={"include_usage": true}` 的最终 chunk。

参考：

- OpenAI Responses streaming events: https://platform.openai.com/docs/api-reference/responses-streaming
- OpenAI Chat Completions streaming events: https://platform.openai.com/docs/api-reference/chat-streaming

## 1. 目标

V1 的目标是把 OpenAI provider 从“兼容 Responses 的一次性 HTTP 请求”升级为“官方 OpenAI Python SDK + 两个真实流式 adapter”：

```text
src/pulsara_agent/llm/adapters/openai/
  client.py
  events.py
  responses.py
  chat_completions.py
```

核心原则：

1. Responses API 和 Chat Completions API 是两个 adapter，不做一个巨大的协议转换器。
2. 两个 adapter 共享 SDK client、异常翻译、event builder、usage extraction 辅助逻辑。
3. `ModelProfile.api` 继续作为协议选择开关：
   - `openai_responses`
   - `openai_chat_completions`
4. Pulsara 内部事件契约不变：继续输出 `ModelCallStartEvent` / `TextBlock*` / `ThinkingBlock*` / `ToolCall*` / `ModelCallEndEvent` / `RunErrorEvent`。
5. 真流式的含义是：SDK stream 每收到 provider delta，就尽快翻译为 Pulsara `AgentEvent`，不等完整 response 结束。
6. V1 不做 delta coalescing。先把 SDK delta 忠实翻译并完整落库，收集真实事件密度、chunk 形态和 UI/event log 压力；后续再基于数据决定是否在 projection/UI 或存储层合并。

## 2. 非目标

V1 不做这些事：

1. 不实现 ccswitch 式 proxy/converter。Pulsara 自己控制 harness，不需要把 Responses 伪装成 Chat Completions 或反过来。
2. 不重写 `LLMRuntime` 的 provider-neutral 协议，除非发现现有 `LLMTransport.stream()` 无法表达 SDK streaming。
3. 不做 Terminal tool 真流式。Terminal 需要改变 tool execution/event 协议，另起设计。
4. 不引入多 provider 抽象大重构。先把 OpenAI 两个 wire format 落稳。
5. 不在 V1 强行支持全部 OpenAI streaming event 类型。先覆盖文本、reasoning summary、function/tool call、usage、error；其余 event 要么忽略，要么作为 metadata 记录，不能破坏流。

## 3. 文件职责

### 3.1 `client.py`

职责：创建和配置官方 OpenAI SDK client。

建议内容：

```python
OPENAI_RESPONSES_API = "openai_responses"
OPENAI_CHAT_COMPLETIONS_API = "openai_chat_completions"

def build_async_openai_client(*, api_key: str, base_url: str, timeout_seconds: float) -> AsyncOpenAI:
    ...

class OpenAIAdapterError(Exception):
    ...
```

设计要求：

1. 使用 `AsyncOpenAI`，不要再用 `urllib`。
2. 尊重 `ModelProfile.base_url`，保留兼容 OpenAI-compatible endpoint 的能力。
3. transport 可接收预构造 client，便于测试注入 fake SDK stream。
4. provider 异常统一翻译成 `RunErrorEvent(code="openai_error")` 或协议专属 code。
5. 不在 client 层做 request payload 组装；payload 仍归各协议 adapter。

### 3.2 `events.py`

职责：承载 OpenAI 两个 adapter 共享的 Pulsara event 构造和小工具。

从 `responses.py` 移出：

1. `_AgentEventBuilder`
2. `_arguments_to_json_string`
3. `_usage(...)` 的共享部分，可按 provider input shape 做两个 wrapper

需要新增/整理：

1. SDK event 到 dict 的轻量 helper：优先 `event.model_dump()`，否则兼容 dict。
2. tool call accumulator：
   - Responses 以 `item_id` / `call_id` 映射为主。
   - Chat Completions 以 `choices[*].delta.tool_calls[*].index` 作为流内 key，最终稳定成 OpenAI tool call id。
3. close helper 必须幂等：stream 正常完成、异常中断、SDK event 缺失 done 时，都不能遗留未关闭的 text/thinking/tool block。

### 3.3 `responses.py`

职责：只处理 Responses API 的 payload 和 streaming event 翻译。

保留/迁移：

1. `build_responses_payload()`
2. `translate_responses_event()`
3. `response_to_agent_events()` 可作为非流式 fixture/helper 保留，但不再是主路径。

主路径应变成：

```python
async with client.responses.stream(...) as stream:
    async for event in stream:
        for agent_event in translate_responses_event(event, builder=builder):
            yield agent_event
```

如果 SDK 当前推荐形态不是 `client.responses.stream(...)`，实现时以官方 SDK 的实际 async streaming API 为准，但约束不变：不能退回完整 body 合成。

必须覆盖的 Responses event：

1. `response.output_text.delta` -> `TextBlockDeltaEvent`
2. `response.reasoning_summary_text.delta` / 合理的 reasoning summary delta -> `ThinkingBlockDeltaEvent`
3. `response.output_item.added` function call -> `ToolCallStartEvent`
4. `response.function_call_arguments.delta` -> `ToolCallDeltaEvent`
5. `response.output_item.done` function call -> `ToolCallEndEvent`
6. `response.completed` -> close active blocks + `ModelCallEndEvent`
7. `response.failed` / SDK exception -> close as needed + `RunErrorEvent`

### 3.4 `chat_completions.py`

职责：新增 Chat Completions API adapter。

需要实现：

1. `OPENAI_CHAT_COMPLETIONS_API = "openai_chat_completions"`，并注册到 factory。
2. `build_chat_completions_payload(model, context, options)`。
3. `translate_chat_completion_chunk(chunk, builder, accumulator)`。
4. `OpenAIChatCompletionsTransport.stream()`。

Payload 映射：

1. `context.system_prompt` -> 第一条 `{"role": "system", "content": ...}`，除非已有 system message 需要合并策略。
2. `LLMMessage.user/assistant/system` -> Chat messages。
3. `LLMMessage.tool_call` -> assistant message with `tool_calls`。
4. `LLMMessage.tool_result` -> `{"role": "tool", "tool_call_id": ..., "content": ...}`。
5. `ToolSpec` -> `tools=[{"type":"function","function": {...}}]`。
6. `LLMOptions.max_output_tokens` -> Chat API 对应输出 token 参数。实现时确认当前 SDK/API 名称，不要凭旧参数硬写。
7. `LLMOptions.reasoning_effort` 仅在目标模型/API 支持时传入；不支持时应 fail closed 或显式跳过并测试。
8. `stream=True`，并尽量请求 `stream_options={"include_usage": True}`，以便最终 `ModelCallEndEvent` 带 token usage。

Chunk 映射：

1. `choices[*].delta.content` -> text delta。
2. `choices[*].delta.tool_calls[*].index` -> 流内 tool key。
3. tool call id/name/arguments 可能分多个 chunk 到达：必须用 accumulator 记录。
4. `finish_reason == "tool_calls"` 或 stream 结束时关闭 active tool calls。
5. usage 可能只在最终 chunk 出现，也可能因为中断缺失；缺失时 `Usage()`，不要报错。

## 4. 实施顺序

### PR1：依赖与共享骨架

改动：

1. `pyproject.toml` 增加 `openai` 依赖，并更新 `uv.lock`。
2. 新建 `client.py`，提供 SDK client factory 和 adapter error。
3. 新建 `events.py`，把 `_AgentEventBuilder` 从 `responses.py` 移出。
4. `responses.py` 改 import，但行为保持不变。

退出条件：

1. 现有 `tests/test_llm_runtime.py` 全绿。
2. `transport_builder_for_test()` 改从 `events.py` import builder。
3. 没有 provider 行为变化。

### PR2：Responses 真流式

改动：

1. `OpenAIResponsesTransport` 改为使用 `AsyncOpenAI` streaming。
2. 删除 `_post_responses()` 和 `urllib` 路径。
3. 保留 `_mock_events` 或改成 fake SDK stream fixture，确保测试不打真实网络。
4. `response_to_agent_events()` 保留为 fixture/helper，不作为生产主路径。

退出条件：

1. mock raw events 测试继续覆盖 event translator。
2. 新增 fake SDK stream 测试：证明第一个文本 delta 在 completed event 前已经 yield。
3. SDK exception 会产出 `RunErrorEvent`，并且不吞掉已关闭 block。
4. `uv run pytest tests/test_llm_runtime.py -q` 通过。

### PR3：Chat Completions adapter

改动：

1. 新建 `chat_completions.py`。
2. `factory.py` 同时注册 Responses 和 Chat Completions transport。
3. `LLMConfig(api="openai_chat_completions")` 可选择新 adapter。
4. 新增 payload 和 stream translator 测试。

退出条件：

1. 文本 streaming：chunk content -> `TextBlockStart/Delta/End`。
2. tool call streaming：多 chunk arguments 拼接时，每个 delta 都发出，最终 tool call end。
3. usage final chunk -> `ModelCallEndEvent` token 字段。
4. `finish_reason="tool_calls"` 不会提前结束 reply，reply lifecycle 仍由 runtime 统一包裹。

### PR4：真实 SDK smoke

新增 opt-in 测试，默认跳过：

```text
PULSARA_RUN_REAL_LLM=1
PULSARA_API_KEY=...
PULSARA_PRO_MODEL=...
PULSARA_FLASH_MODEL=...
```

建议覆盖：

1. Responses：真实文本流至少产生两个 `TextBlockDeltaEvent` 或证明 SDK stream 逐 event 到达。
2. Responses：真实 function call 流可产生 `ToolCallStart/Delta/End`。
3. Chat Completions：真实文本流。
4. Chat Completions：真实 function tool call 流。

真实测试只验证 adapter 协议，不验证模型智能。

### PR5：delta 保存策略复盘（V1 后置）

真流式落地后，`TextBlockDeltaEvent`、`ThinkingBlockDeltaEvent`、`ToolCallDeltaEvent` 会明显增多。

V1 的明确选择是：不合并，不抽样，不丢弃。每个 provider delta 都作为 Pulsara delta event 进入原始 event log。这样做的价值是保留完整证据，避免在尚不知道真实流式形态前过早优化。

V1 后再用真实 trace 回答三个问题：

1. event log 是否真的被 delta 数量压垮。
2. UI 是否需要展示层合并，还是原始 delta 已足够可用。
3. tool-call arguments 是否需要按 JSON chunk、时间窗或读取 projection 合并。

若后续需要优化，优先考虑 projection/UI 层合并展示，或引入明确的 event-log 保存策略；不要让 provider adapter 偷偷吞并语义 delta。原始 delta 是事实层，合并后的文本/参数是视图层。

## 5. 测试矩阵

必测：

1. Responses payload 不回退：system prompt、messages、tools、reasoning、max tokens 映射保持原有语义。
2. Responses text delta：`response.output_text.delta` 产生 start + delta，completed 产生 end + model end。
3. Responses reasoning summary delta：进入 thinking block。
4. Responses tool call：`output_item.added` + `function_call_arguments.delta` + `output_item.done` 顺序正确。
5. Responses item id/call id：arguments delta 用 item id 时能映射到 call id。
6. Responses error：SDK exception 和 `response.failed` 都产出 `RunErrorEvent`。
7. Chat payload：tool specs、assistant tool call transcript、tool result transcript 映射正确。
8. Chat text delta：`choices[*].delta.content` 进入 text block。
9. Chat tool call delta：同一个 `index` 的 id/name/arguments 分片被稳定追踪。
10. Chat usage：final usage chunk 进入 `ModelCallEndEvent`；usage 缺失时不失败。
11. Registry：`openai_responses` 和 `openai_chat_completions` 都可由 `ModelProfile.api` 选中。
12. No network unit tests：单测全部使用 fake client/fake stream。

## 6. 风险与约束

### 6.1 SDK event shape 漂移

不要让 production 逻辑依赖某个 SDK class 的私有属性。translator 接收 dict-like payload，入口统一做 `model_dump()` / `dict` 归一化。fixture 中保留官方 event type 字符串。

### 6.2 Chat tool call id 晚到

Chat Completions 的 tool call delta 以 `index` 聚合，id/name/arguments 可能分块到达。builder 不能要求第一块就有最终 id。若 id 晚到，先用稳定临时 id，拿到正式 id 后必须保证后续 delta/end 使用同一个 Pulsara tool_call_id。V1 更推荐等 id 出现再发 start；如果 arguments 先于 id 到达，则缓存到 accumulator，直到可发 start。

### 6.3 Reasoning/thinking 不同协议能力不一致

Responses 有 reasoning summary streaming；Chat Completions 未必有等价字段。Chat adapter 不应伪造 thinking block。没有 provider delta 就不发 `ThinkingBlock*`。

### 6.4 参数名差异

Responses 与 Chat Completions 的 token、reasoning、tool 参数名不完全一致。实现时以当前 OpenAI SDK/API 为准，不要把 Responses payload 直接复用给 Chat。

### 6.5 事件量放大

真流式会放大 event log 和 UI 压力。V1 接受这个成本，先完整保存 delta，用真实 trace 量化压力。不能为了减少事件量而在 adapter 层提前合并、抽样或丢弃 tool-call argument 的可见进度。

## 7. 验收命令

常规：

```bash
uv run ruff check src tests evals
uv run pytest -q
```

只跑 LLM 单元测试：

```bash
uv run pytest tests/test_llm_runtime.py -q
```

真实 LLM smoke（显式 opt-in）：

```bash
PULSARA_RUN_REAL_LLM=1 uv run pytest tests/test_real_llm_integration.py -q
```

## 8. 建议先后结论

先做 OpenAI SDK + Responses 真流式，再做 Chat Completions adapter。理由是 Responses 已有 payload 和 translator 测试，替换底层 transport 的风险最小；Chat Completions 是新增协议，应该站在共享 `client.py` / `events.py` 稳定之后落地。

Terminal 真流式排在这之后。它不是 provider adapter 问题，而是 tool executor 从“完成后返回一个 `TerminalResult`”升级为“运行中持续发 tool result delta”的协议问题。
