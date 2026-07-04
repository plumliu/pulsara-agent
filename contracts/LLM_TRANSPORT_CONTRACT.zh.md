# LLM Runtime / Transport Contract

_Created: 2026-07-04_

本文档定义 Pulsara LLM runtime、provider-neutral request、transport adapter、retry 与 usage 的长期契约。

相关代码：

- [src/pulsara_agent/llm/runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/runtime.py)
- [src/pulsara_agent/llm/config.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/config.py)
- [src/pulsara_agent/llm/request.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/request.py)
- [src/pulsara_agent/llm/transport.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/transport.py)
- [src/pulsara_agent/llm/adapters/openai/responses.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/adapters/openai/responses.py)
- [src/pulsara_agent/llm/adapters/openai/chat_completions.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/adapters/openai/chat_completions.py)
- [src/pulsara_agent/llm/adapters/openai/events.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/adapters/openai/events.py)
- [tests/test_llm_runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_llm_runtime.py)

---

## 1. 核心立场

Pulsara runtime 内部不直接依赖某个厂商 wire protocol。

统一边界是：

```text
LLMContext + LLMOptions + ModelRole
  -> LLMRuntime
  -> ModelProfile
  -> LLMTransport(api)
  -> AgentEvent stream
```

所有 provider adapter 必须把外部 stream 翻译成 Pulsara typed `AgentEvent`，不得把 SDK 原始事件泄露给 agent loop。

---

## 2. Model roles

用户配置一组 credential/base-url/provider profile，并提供两个 model slot：

- `pro_model`：主推理模型；
- `flash_model`：便宜/快速模型，用于 memory reflection、compaction、governance 等支线。

`LLMRuntime.stream(role=...)` 必须通过 `LLMConfig.model_for(role)` 选择 `ModelProfile`。Agent runtime 不应硬编码具体模型名。

---

## 3. Transport registry

`LLMTransportRegistry` 按 wire API 名称注册 transport。

V1 默认注册：

- `openai_responses`
- `openai_chat_completions`

规则：

- 同名 API transport 重复注册必须失败。
- 未注册 API 调用必须失败。
- Provider/model 选择由 `ModelProfile.api` 决定，不由 runtime 分支判断。

---

## 4. Provider-neutral request

`LLMContext` 是 transport 的唯一 request 输入：

- `messages`
- `tools`
- `system_prompt`

`LLMOptions` 只表达 provider-neutral knobs：

- temperature；
- max output tokens；
- reasoning effort；
- reasoning summary。

Provider-specific 参数必须走 `ProviderProfile.request_defaults` / `request_extra_body` / thinking profile，不得塞进 runtime loop。

---

## 5. Reply envelope

`LLMRuntime` 必须包裹每次 transport stream：

```text
REPLY_START
  MODEL_CALL_START
  provider-translated block events...
  MODEL_CALL_END or RUN_ERROR
REPLY_END
```

Transport 负责 `MODEL_CALL_START` / `MODEL_CALL_END` / `RUN_ERROR` 以及 text/thinking/tool-call blocks。

`LLMRuntime` 负责 `REPLY_START` / `REPLY_END`。即使 transport 产生 `RUN_ERROR`，reply envelope 仍由 runtime 结束；但具体 agent loop 可据 `RUN_ERROR` 标记 run failed。

---

## 6. Event translation

Provider adapter 必须产出以下 typed events，而不是 provider-native chunks：

- `MODEL_CALL_START`
- `MODEL_CALL_END`
- `RUN_ERROR`
- `TEXT_BLOCK_START/DELTA/END`
- `THINKING_BLOCK_START/DELTA/END`
- `TOOL_CALL_START/DELTA/END`

OpenAI Responses 与 Chat Completions 的差异只能存在于 adapter 内部。Agent loop 只能看到统一事件。

Tool call id 规则：

- Responses adapter 必须正确处理 provider item id 与 call id 的映射。
- Chat Completions adapter 必须缓存 arguments，直到 tool call id/name 到齐。
- Tool call arguments 必须以 JSON string delta 形式进入 `TOOL_CALL_DELTA`。

---

## 7. Usage

`ModelCallEndEvent` 是 usage 的 runtime event 边界。

Usage 字段：

- `input_tokens`
- `output_tokens`
- `total_tokens`

Responses usage (`input_tokens` / `output_tokens`) 与 Chat usage (`prompt_tokens` / `completion_tokens`) 必须归一化到同一个 `Usage` shape。

缺失 usage 时必须写 0，而不是省略事件。

---

## 8. Retry

Provider transport 可以重试 transient LLM failure，但必须满足：

- retry config 由 `LLMRetryConfig` 控制；
- 只在尚未产生 semantic output 前重试；
- semantic output 包括 text、thinking、tool-call delta、run error；
- retry attempt 必须产生 `CustomEvent(name="llm.retry")`；
- 最终失败必须产生 `RUN_ERROR`，metadata 中包含 provider error data 与 retry trace；
- 若已有 semantic output 后失败，不得重试，必须以 `skipped_reason="semantic_output_started"` 解释。

这条规则防止同一回复前半部分已经给模型/用户看见后，transport 静默重放导致重复或矛盾输出。

---

## 9. Client lifecycle

Transport 可以使用 injected client（测试/高级场景）或自行构造 SDK client。

规则：

- transport 自行构造的 client 必须在 stream 结束后关闭；
- injected client 不由 transport 关闭；
- SDK 内建 retry 次数由 `openai_sdk_max_retries` / retry config 控制，不能与 Pulsara retry 形成不可解释的双重重试。

---

## 10. Provider profiles

Provider quirks 必须通过 `ProviderProfile` 表达：

- request defaults；
- extra body；
- thinking delta fields；
- thinking replay policy；
- when-thinking omitted params；
- supports tools；
- supports reasoning。

示例：OpenAI Chat Completions 兼容的 DeepSeek-like provider 可以默认启用 thinking body，并在 thinking 时省略不支持的参数。该逻辑不得散落在 agent loop 或 tool loop。

---

## 11. Compaction / side LLM usage

Context compaction、memory reflection、governance 等支线同样使用 `LLMRuntime.stream()`。

支线调用必须：

- 使用合适的 `ModelRole`（通常 flash）；
- 明确传入不含 tools 的 `LLMContext`，如果该支线契约禁止工具；
- 把 `RUN_ERROR` 视为失败，不得只收集 text delta 后忽略 error。

---

## 12. 禁止事项

- 不允许 agent loop 直接调用 OpenAI SDK。
- 不允许 transport 泄露 SDK chunks 给 runtime。
- 不允许 provider adapter 跳过 typed `MODEL_CALL_START/END`。
- 不允许已有 semantic output 后自动 retry。
- 不允许 provider-specific request 参数散落到 runtime loop。
- 不允许缺 usage 时不发 `MODEL_CALL_END`。
- 不允许 compaction summarizer 忽略 `RUN_ERROR`。

---

## 13. 测试守护

最低测试门槛：

- pro/flash role 选择正确模型。
- default runtime 注册 Responses 与 Chat Completions transport。
- Responses payload 使用 internal context/messages/tools/system prompt。
- Chat payload 使用 internal context/messages/tools/system prompt。
- Responses event stream 翻译 text/tool/usage/error。
- Chat Completions event stream 翻译 text/thinking/tool/usage/error。
- tool call id / arguments streaming 边界正确。
- pre-output failure 可 retry，并 emit `llm.retry`。
- post-semantic-output failure 不 retry，并解释 skipped reason。
- retry exhausted metadata 包含 trace。
- owned SDK client stream 后关闭一次。
- compaction / side LLM 遇 `RUN_ERROR` fail closed。
