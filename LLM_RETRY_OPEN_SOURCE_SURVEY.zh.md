# LLM Retry 开源实现调研：Hermes / OpenClaw / Codex

_Created: 2026-06-19_

本文记录对三个本地开源项目 retry / provider error handling 的调研结论：

- Hermes: `/Users/plumliu/Desktop/python_workspace/hermes-agent`
- OpenClaw: `/Users/plumliu/Desktop/python_workspace/openclaw`
- Codex CLI / Codex SDK: `/Users/plumliu/Desktop/python_workspace/codex`

调研目的不是照搬某个 retry helper，而是提炼成熟 agent 系统对「provider 网络抖动、rate limit、流式中断」的共同处理方式，并为 Pulsara 下一步 LLM retry 设计提供依据。

## 0. Pulsara 当前基线

Pulsara 目前已经有 OpenAI SDK 流式 adapter：

- `src/pulsara_agent/llm/adapters/openai/client.py`
- `src/pulsara_agent/llm/adapters/openai/responses.py`
- `src/pulsara_agent/llm/adapters/openai/chat_completions.py`
- `src/pulsara_agent/llm/runtime.py`
- `src/pulsara_agent/runtime/agent.py`

当前能力：

1. `LLMConfig.from_env()` 可以选择 `PULSARA_API=openai_responses` 或 `openai_chat_completions`。
2. Responses 与 Chat Completions 都通过 `AsyncOpenAI` SDK 发起流式调用。
3. adapter 能把 Responses / Chat Completions 的 text、thinking、tool call delta 转成 Pulsara event。
4. provider error metadata 已经增强，能记录 exception type、message、repr、status_code、body、response、exception chain。
5. `AgentRuntime` 有连续模型失败预算：模型回复内出现 `RunErrorEvent` 后，会进入 recovery turn，超过预算后失败。

当前缺口：

1. 没有 Pulsara 自己的 LLM transport retry policy。
2. 没有统一的 retryable / non-retryable classifier。
3. 没有 `Retry-After` / `retry-after-ms` 解析和上限。
4. 没有 jittered backoff。
5. 没有 attempt 级可观测事件或结构化日志。
6. 没有区分「stream 创建前失败」「首个语义 delta 前失败」「已经输出 delta 后失败」。
7. 当前 `AgentRuntime` 的 recovery 是 agent loop 层的后续 turn 恢复，不是 provider transport 层的同请求 retry。
8. `AsyncOpenAI` SDK 的内置 retry 行为没有被 Pulsara 显式配置或观测；如果 SDK 先重试并最终抛出 `Connection error.`，Pulsara 只能看到被包装后的末端错误。

这解释了最近 real LLM 中看到的现象：DeepSeek/OpenAI-compatible endpoint 偶发 `Connection error.` 时，Pulsara 能记录 cause chain，但还不会在 adapter 层用清晰策略重试。

## 1. Hermes：错误分类、用户可见 backoff、interrupt-aware sleep

Hermes 的相关文件包括：

- `agent/retry_utils.py`
- `agent/error_classifier.py`
- `agent/conversation_loop.py`
- `agent/codex_runtime.py`
- `tests/test_retry_utils.py`
- `tests/run_agent/test_stream_interrupt_retry.py`
- `tests/run_agent/test_jsondecodeerror_retryable.py`

### 1.1 Jittered exponential backoff

`agent/retry_utils.py` 提供 `jittered_backoff()`：

```text
delay = min(base_delay * 2^(attempt - 1), max_delay) + positive_jitter
```

它有几个值得借鉴的点：

1. jitter 是默认行为，不是可选装饰。
2. jitter 用进程内 monotonic counter 混入随机种子，避免多个 session 同时失败后同步重试。
3. delay 有上限，避免无限退避让用户误以为 agent 卡死。

### 1.2 错误分类比 retry helper 更重要

`agent/error_classifier.py` 维护了大量 transient transport signal：

- `APIConnectionError`
- `APITimeoutError`
- `ReadTimeout`
- `ConnectTimeout`
- `ConnectError`
- `RemoteProtocolError`
- `ConnectionResetError`
- `BrokenPipeError`
- `SSLError`
- `SSLEOFError`
- `ServerDisconnectedError`

同时也按 HTTP status 做判断：

- `429`：rate limit，retryable。
- `500/502`：通常 retryable，但如果 message/code 明确是 unknown parameter、unsupported parameter、invalid request，则 fail fast，避免把确定性坏请求打成 retry flood。
- `503/529`：overloaded，retryable。
- `413`：payload/context too large，进入压缩或 failover，不应裸重试。
- 其他 4xx：通常 non-retryable。

核心启示：retry 的承重部分不是 `sleep()`，而是 classifier。没有 classifier，retry 会把 schema 错误、认证错误、上下文过大都伪装成「网络抖动」。

### 1.3 Retry-After 与 rate-limit 路径

Hermes 在 rate-limit 分支会读取 provider response headers：

```text
Retry-After / retry-after
```

并把等待时间 cap 到 120 秒；没有 header 时回退到 jittered exponential backoff。它还会把等待状态写入用户可见 status buffer，避免长等待期间 UI 看起来冻结。

### 1.4 interrupt-aware sleep

Hermes 的 backoff sleep 不是一次 `sleep(wait_time)`，而是小步循环：

1. 每 0.2 秒检查 interrupt。
2. 大约每 30 秒 touch activity，告诉 gateway / watchdog agent 仍然活着。
3. 用户中断时返回结构化 interrupted result，而不是强行等完整 backoff。

这对 Pulsara 的长期 UI/session 很重要。v1 可以先用 `asyncio.sleep()`，但计划里需要给可取消 sleep 留边界。

### 1.5 流式 retry 被单独处理

Hermes 的 `agent/codex_runtime.py` 对 Responses stream 有单独逻辑：

1. stream create 阶段失败可以重试。
2. stream iteration 中的 transport error 也有限重试。
3. 每次 retry 都记录 attempt、provider context、error。
4. stream 中还维护 first-delta、text-delta、reasoning-delta hook 和 activity touch。

但 Pulsara 不能直接照搬 Hermes 的 mid-stream retry：Pulsara 的 delta 会立刻进入 event log。若已经 emit text/tool delta，再透明重跑同一请求，可能造成重复 token、重复 tool call argument、或同一 tool call 被拼接两遍。Pulsara 需要更保守的 event-sourced 语义。

## 2. OpenClaw：请求级 retry、SDK retry 上限、非幂等保护

OpenClaw 的相关文件包括：

- `docs/concepts/retry.md`
- `src/infra/retry.ts`
- `src/infra/retry-policy.ts`

### 2.1 Retry per HTTP request

OpenClaw 文档把目标写得很明确：

1. retry 每个 HTTP request，而不是 retry 整个多步骤 flow。
2. 只 retry 当前 step，保持 ordering。
3. 避免重复执行非幂等操作。

这对 agent 系统尤其关键。LLM turn 可能包含：

```text
model stream -> tool calls -> tool execution -> next model stream
```

如果把整个 flow 重跑，就可能重复执行工具、重复写文件、重复提交外部请求。正确边界应是 provider request attempt，而不是 agent loop。

### 2.2 Generic retry runner

`src/infra/retry.ts` 提供泛化 `retryAsync()`：

- `attempts`
- `minDelayMs`
- `maxDelayMs`
- `jitter`
- `shouldRetry(err, attempt)`
- `retryAfterMs(err)`
- `onRetry(info)`

这说明成熟系统会把 retry timing 与 retry classification 分开：

```text
classifier 决定能不能 retry
timer 决定何时 retry
observer 记录 retry 发生了什么
```

### 2.3 Retry-After 的正向 jitter

OpenClaw 对 `Retry-After` 有一个很重要的细节：

- 普通 exponential backoff 用 symmetric jitter。
- server-supplied `Retry-After` 是下限契约，不能提前。
- 因此对 `Retry-After` 用 positive jitter，只增加等待，不减少等待。

这比简单 `delay *= random()` 更稳。否则一半客户端会早于 provider 指定时间重试，可能被 rate limiter 继续惩罚。

### 2.4 SDK 内置 retry 与 failover

OpenClaw 文档提到，对于 Stainless-based SDK（Anthropic/OpenAI），SDK 会处理一些短 retry，例如：

```text
408 / 409 / 429 / 5xx
retry-after-ms / retry-after
```

但 OpenClaw 也给 SDK retry 设了策略上限：当 SDK 想 sleep 超过 60 秒时，OpenClaw 注入 `x-should-retry: false`，让错误浮出，以便外层 failover 选择其他 auth profile / fallback model。

启示：不能让 SDK 成为 retry 黑箱。Pulsara 要么显式关闭 SDK retry，由自身 policy 接管；要么明确记录「SDK retry 是底层不透明 retry」，并避免外层再叠一层大 retry。

### 2.5 非幂等 strict retry

OpenClaw 的 channel retry policy 有 `strictShouldRetry`，用于非幂等操作。默认 regex 可以兜底识别 network transient，但在 sendMessage 这类可能重复投递的操作上，必须让 caller 精确决定。

Pulsara 的 LLM streaming 同样有非幂等风险：不是外部副作用，而是 event log 副作用。一旦 token/tool delta 已经写出，透明重试就不再是安全重试。

## 3. Codex：小而硬的 overload retry 与 stream retry 可观测性

Codex 的相关文件包括：

- `sdk/python/src/openai_codex/retry.py`
- `sdk/python/src/openai_codex/errors.py`
- `sdk/python/examples/10_error_handling_and_retry/`
- `codex-rs/core/src/responses_retry.rs`
- `codex-rs/core/src/session/turn.rs`
- `codex-rs/codex-api/src/sse/responses.rs`

### 3.1 Python SDK：typed retryable errors

Codex Python SDK 的 `retry_on_overload()` 很小：

```text
max_attempts = 3
initial_delay = 0.25s
max_delay = 2.0s
jitter_ratio = 0.2
```

它只 retry `is_retryable_error(exc)`，而 `is_retryable_error()` 只认：

- `ServerBusyError`
- `JsonRpcError` data 中包含 server overloaded signal

这体现了另一种成熟取舍：用户面对的 SDK helper 不需要包治百病，它只处理非常明确的 transient overload。

### 3.2 Rust core：stream retry 有 UI warning

`codex-rs/core/src/responses_retry.rs` 的 `handle_retryable_response_stream_error()` 做了几件事：

1. 有 `max_retries`。
2. retryable stream error 后用 backoff 或 provider requested delay。
3. 记录 warning log。
4. 必要时通知前端 `Reconnecting... n/max`。
5. 达到上限后可切换 fallback transport。

这说明 stream retry 不应静默发生。用户看见流式输出停住时，需要知道是正在重连，而不是 agent 死了。

### 3.3 Responses SSE 错误分类

`codex-rs/codex-api/src/sse/responses.rs` 将不同 response error 映射成不同 API error：

- context window error
- quota exceeded
- invalid prompt
- cyber policy
- server overloaded
- retryable error with optional delay

其中 `try_parse_retry_after()` 会从 rate-limit message 中解析 seconds/ms。这个方向与 Hermes/OpenClaw 一致：把 provider 错误结构化，再交给上层策略。

## 4. 跨项目共识

三个项目虽然实现风格不同，但有一些高度一致的原则：

1. **retry 边界应靠近 provider/transport**  
   retry 当前 provider request，而不是重跑整个 agent flow。

2. **先分类，再 retry**  
   connection reset、timeout、429、overload、部分 5xx 可以 retry；auth、invalid request、unsupported parameter、schema error、context too large 不应盲 retry。

3. **jitter 是必要组件**  
   没有 jitter 的指数退避会让并发 session 在同一时间撞回 provider。

4. **Retry-After 要尊重，但要有上限**  
   server hint 是下限契约；但超长 sleep 需要浮出给外层 failover / 用户决策。

5. **流式 retry 与普通 HTTP retry 不同**  
   stream 尚未输出任何语义内容时，retry 较安全；stream 已经输出内容后，透明 retry 可能造成重复或污染。

6. **不要 retry 确定性坏请求**  
   例如 unsupported parameter、unknown model、invalid schema、bad request。部分 provider 会错误地用 502 包装 request validation error，因此 classifier 不能只看 status code。

7. **retry 必须可观测**  
   至少记录 provider、model、base_url、attempt、delay、status/error type、root cause、是否来自 Retry-After、是否达到上限。

8. **SDK retry 不能是黑箱**  
   如果 SDK 已经自动 retry，agent 需要知道这是底层行为；如果要自己实现 retry，最好显式关闭或收窄 SDK retry，避免双层 retry。

9. **非幂等副作用要求更保守**  
   OpenClaw 关注外部消息重复投递；Pulsara 需要关注 event log 中 text/tool deltas 重复写入。

## 5. Pulsara 的设计启示

Pulsara 应该补的不是一个 `try/except: sleep; retry`，而是一小层 LLM retry infrastructure：

```text
llm/retry.py
  RetryConfig
  RetryDecision
  classify_llm_error()
  parse_retry_after()
  compute_backoff()
  retry_stream_attempts()

llm/adapters/openai/errors.py
  extract OpenAI-compatible status/body/header/type metadata
  classify provider-specific wrapped exceptions

llm/adapters/openai/responses.py
llm/adapters/openai/chat_completions.py
  use retry envelope around stream creation / safe stream iteration
```

最关键的 Pulsara 特有原则是：

```text
自动 retry 只允许发生在尚未 emit 语义 delta 之前。
```

这里的「语义 delta」包括：

- `TextBlockDeltaEvent`
- `ThinkingBlockDeltaEvent`
- `ToolCallStartEvent`
- `ToolCallDeltaEvent`
- `ToolCallEndEvent`
- provider `RunErrorEvent`

如果已经向 event log 写出这些事件，v1 不应透明重跑同一 provider request。应该关闭 active blocks，发出结构化 `RunErrorEvent`，由 agent loop 的既有 recovery budget 或后续更高层策略处理。

## 6. Pulsara 当前 gap 清单

按优先级排序：

1. **没有 classifier**  
   `Connection error.`、`APIConnectionError`、`APITimeoutError`、HTTP 429/5xx、invalid request 当前没有统一判断。

2. **没有 Retry-After 解析**  
   OpenAI-compatible provider 可能通过 headers/body/message 给出 retry hint；Pulsara 还不会读取。

3. **没有 adapter-level retry envelope**  
   Responses/Chat Completions 当前失败后直接 emit `RunErrorEvent`。

4. **没有 stream-safe retry 语义**  
   没有 track `semantic_output_started`，所以无法区分 safe retry 与 unsafe retry。

5. **SDK retry 不透明**  
   `build_async_openai_client()` 没有显式设置 SDK retry 行为。若 SDK 默认 retry 后仍失败，Pulsara 只能看到最终 exception。

6. **observability 仍偏最终错误**  
   有最终 provider metadata，但没有 attempt-by-attempt trace。

7. **runtime recovery 与 transport retry 容易混淆**  
   `AgentRuntime._recover_or_fail_model()` 是模型失败后的下一轮恢复，不是对同一次 provider request 的网络 retry。

8. **real LLM 测试无法稳定证明 retry**  
   网络抖动不可控；应以 fake transport/client 单测为主，real LLM smoke 只验证真实 provider 下 diagnostics 和 happy path。

## 7. 建议结论

Pulsara v1 retry 应选择保守但可观测的实现：

1. 在 adapter/transport 层做 request-attempt retry。
2. 默认 3 attempts（initial + 2 retries），带 jitter 和 cap。
3. 尊重 `Retry-After`，但 cap 到可配置上限。
4. 禁止自动 retry 已经输出语义 delta 的 stream。
5. 对 400/401/403/413/unsupported parameter 等确定性错误 fail fast。
6. 用 `CustomEvent(name="llm.retry", value={...})` 或结构化日志记录每次 retry。
7. 最终失败仍发 `RunErrorEvent`，并保留完整 provider metadata 与 retry trace summary。

这会把 DeepSeek/OpenAI-compatible 的偶发 `Connection error.` 从「神秘失败」变成「可分类、可重试、可诊断」的 provider transport 事件。
