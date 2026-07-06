# Pulsara LLM Retry v1 实施计划

_Created: 2026-06-19_

本文规划 Pulsara 下一步 LLM provider retry 机制。相关调研见：`LLM_RETRY_OPEN_SOURCE_SURVEY.zh.md`。

目标不是一次性做完整 model failover / credential rotation，而是先把 OpenAI SDK / OpenAI-compatible streaming 调用的 transient 网络失败变成可分类、可重试、可观测的 transport 层能力。

## 0. 当前基线

当前相关代码：

- `src/pulsara_agent/llm/config.py`
- `src/pulsara_agent/llm/factory.py`
- `src/pulsara_agent/llm/runtime.py`
- `src/pulsara_agent/llm/adapters/openai/client.py`
- `src/pulsara_agent/llm/adapters/openai/events.py`
- `src/pulsara_agent/llm/adapters/openai/responses.py`
- `src/pulsara_agent/llm/adapters/openai/chat_completions.py`
- `src/pulsara_agent/runtime/agent.py`
- `tests/test_llm_runtime.py`
- `tests/test_real_llm_integration.py`

当前事实：

1. `OpenAIResponsesTransport.stream()` 和 `OpenAIChatCompletionsTransport.stream()` 都是 async generator。
2. adapter 进入 provider 前会先 emit `ModelCallStartEvent`。
3. provider stream 中的 text/thinking/tool call delta 会立即 yield 成 Pulsara event，并由 `RuntimeSession.emit()` 写入 event log。
4. provider exception 当前会被 adapter 捕获并转成 `RunErrorEvent`。
5. `provider_error_data()` 已经能记录 exception chain，最近的 `Connection error.` 能看到下层 cause。
6. `AgentRuntime` 有 `max_consecutive_model_failures` 预算，但那是 agent loop 级恢复，不是 provider request retry。
7. `build_async_openai_client()` 当前没有显式配置 OpenAI SDK 的 retry 策略。

## 1. v1 总目标

把 LLM request 从：

```text
create stream once -> emit deltas -> on exception emit RunErrorEvent
```

升级为：

```text
attempt provider request
  classify failure
  if retryable and no semantic output emitted:
    backoff + retry
  else:
    emit structured final error
```

v1 成功标准：

1. Responses API 和 Chat Completions API 共用一套 retry policy。
2. transient connection / timeout / rate-limit / overload / retryable 5xx 可以在安全边界内自动 retry。
3. deterministic bad request / auth / unsupported parameter / context too large 不盲 retry。
4. `Retry-After` 被解析、尊重、cap，并带 positive jitter。
5. 已经 emit 语义 delta 的 stream 不透明重试。
6. 每次 retry 都可观测，最终错误携带 retry trace。
7. 所有行为由单测和 fake client 驱动验证；real LLM 只做 smoke，不依赖真实网络抖动。

## 2. 非目标

v1 不做：

1. 不做跨 provider model failover。
2. 不做 credential rotation。
3. 不做 context compression / 自动缩短 prompt。
4. 不做 mid-output stream replay / event log rollback。
5. 不做 token delta coalescing；全量 delta 是否保存仍按现有 event log 语义。
6. 不重试整个 agent loop。
7. 不重试已经执行过 tool call 的高层 flow。
8. 不为 memory tool 的 invalid candidate retry 做重构；那是 tool argument 修正，不是 provider transport retry。

## 3. 核心原则

### 3.1 Transport retry，不是 agent loop retry

retry 边界必须是单次 provider request attempt：

```text
LLMTransport.stream(...)
```

不能把下面整段重跑：

```text
model reply -> tool calls -> tool execution -> memory hooks -> next turn
```

否则会重复执行工具、重复写 event log、重复触发 memory hooks。

### 3.2 语义输出前才允许透明 retry

`AgentEventBuilder` 应是语义输出状态的单一真相源。新增或等价维护：

```python
builder.has_semantic_output: bool
```

一旦 yield 过以下任何事件，就视为已经有语义输出：

- `TextBlockStartEvent`
- `TextBlockDeltaEvent`
- `ThinkingBlockStartEvent`
- `ThinkingBlockDeltaEvent`
- `ToolCallStartEvent`
- `ToolCallDeltaEvent`
- `ToolCallEndEvent`
- provider-origin `RunErrorEvent`

这个 flag 不应散落在 Responses / Chat adapter 的局部变量里。原因是 `AgentEventBuilder` 已经是所有 text/thinking/tool-call event 的收口点，也已经维护 active block 状态；如果 retry runner 和 builder 各自维护状态，二者会有 drift 风险。

如果此后 provider stream 抛出异常，v1 不做自动 retry。原因：

1. text delta 重放会重复文本。
2. thinking delta 重放会重复 reasoning stream。
3. Chat Completions tool call argument 是增量拼接，重放会污染 JSON。
4. event log 没有撤销已写 delta 的机制。

### 3.3 `ModelCallStartEvent` 不算语义输出

adapter 当前会在 provider request 前先 yield `ModelCallStartEvent`。这是一条 invocation event，不是 provider semantic output。

因此 v1 可以允许这种路径：

```text
ModelCallStartEvent 已发出
provider create 失败
retry
最终成功
```

但必须避免每次 attempt 都重复发 `ModelCallStartEvent`。推荐做法：

1. 外层 transport stream 只创建一次 `AgentEventBuilder` 并只 emit 一次 `model_start()`。
2. retry loop 包住 provider stream creation / consumption。
3. attempt 级事件使用 `CustomEvent(name="llm.retry", ...)` 或 metadata，不再发新的 model start。

### 3.4 分类优先于 backoff

retry 的第一步不是 sleep，而是 classification：

```text
retryable / non_retryable / unknown
reason
retry_after
provider_status
provider_code
```

没有 classifier 的 retry 会把确定性坏请求变成 provider 压力。

### 3.5 SDK retry 不能不透明叠加

Pulsara 应明确拥有 retry envelope，但不能先关闭旧保护再补新保护。OpenAI Python SDK 当前默认会做有限重试；如果先全局把 `max_retries=0` 合入，而 Responses / Chat Completions 的 Pulsara retry 尚未接好，就会制造一个严格比当前更差的窗口。

因此 v1 的规则是：

1. `build_async_openai_client()` 只增加可选 `max_retries` plumbing，不改变默认行为。
2. 某个 transport 只有在同一个 PR 已经接入 Pulsara safe retry 后，才为该 transport 设置 SDK `max_retries=0`。
3. Responses 和 Chat Completions 分别在自己的 safe retry PR 中切换，不能由共享 client helper 一刀切。

完成接入后的目标状态仍是由 Pulsara retry envelope 接管：

```python
AsyncOpenAI(..., max_retries=0)
```

这样：

1. attempt 次数由 Pulsara `RetryConfig` 控制。
2. retry trace 完整进入 Pulsara logs/events。
3. `Connection error.` 不会被 SDK 多层包装后才浮出。

如果不关闭 SDK retry，也必须在文档和 metadata 里明确：底层 SDK 可能已经做过不可见 retry，Pulsara 的 retry 是第二层。v1 推荐关闭 SDK retry，减少双层 retry 的不可预测等待。

### 3.5.1 Client 生命周期必须跨 retry loop 固定

当前 transport 通过 `_client` 支持测试注入，也会在未注入时自建 `AsyncOpenAI` client。retry loop 接入后必须钉死生命周期：

1. 注入的 `_client` 永不由 transport close；测试和宿主拥有它。
2. transport 自建的 owned client 在整个 retry loop 外创建，并在所有 attempts 完成后 close 一次。
3. v1 owned client 默认跨 attempts 复用；这保留连接池，但也承认 connection reset 后可能复用到坏连接池的风险。
4. 如果未来要按特定连接错误每 attempt 新建 client，必须显式实现并测试每个 attempt 的 close，不能让 `finally` 在 loop 中提前 close 后又复用同一 client。

### 3.6 Retry-After 是下限契约，但必须有上限

`Retry-After` / `retry-after-ms` / provider body 中的 retry hint 表示「不要早于这个时间重试」。因此 jitter 应是 positive jitter，不能早于 hint。

但长时间等待会让 agent 像卡死。v1 应有 `max_retry_after_seconds`，超过上限后：

1. 不在 transport 层长睡。
2. 发出最终 `RunErrorEvent`，metadata 标记 `retry_after_exceeded=true`。
3. 未来可交给 failover / 用户确认。

### 3.7 可取消 sleep

v1 的 retry sleep 应使用 async sleep，后续可接 cancellation。实现时不要用阻塞 `time.sleep()`。

若当前 runtime 没有统一 cancellation token，至少要保证 task cancellation 能打断 retry wait。

## 4. 建议文件结构

```text
src/pulsara_agent/llm/
  retry.py                 # provider-neutral retry config/classifier/backoff/runner

src/pulsara_agent/llm/adapters/openai/
  errors.py                # OpenAI-compatible error extraction/classification helpers
  client.py                # AsyncOpenAI construction, max_retries config
  responses.py             # Responses stream retry integration
  chat_completions.py      # Chat Completions stream retry integration

tests/
  test_llm_retry.py        # classifier/backoff/retry runner unit tests
  test_llm_runtime.py      # adapter fake-client stream retry tests
```

不建议把 retry 逻辑放进 `runtime/agent.py`。那里只能看到最终 `RunErrorEvent`，已经太晚，无法区分 provider request attempt 与 agent recovery turn。

## 5. Config

新增 dataclass：

```python
@dataclass(frozen=True, slots=True)
class LLMRetryConfig:
    enabled: bool = True
    attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.2
    max_retry_after_seconds: float = 30.0
```

`attempts` 表示总 attempt 数，包含第一次请求。因此 `attempts=3` 是 initial + 2 retries。

建议 env：

```text
PULSARA_LLM_RETRY_ENABLED=true
PULSARA_LLM_RETRY_ATTEMPTS=3
PULSARA_LLM_RETRY_BASE_DELAY_SECONDS=0.5
PULSARA_LLM_RETRY_MAX_DELAY_SECONDS=8.0
PULSARA_LLM_RETRY_JITTER=0.2
PULSARA_LLM_RETRY_MAX_RETRY_AFTER_SECONDS=30
# PULSARA_OPENAI_SDK_MAX_RETRIES intentionally unset during plumbing PR
```

默认值选择理由：

1. DeepSeek/OpenAI-compatible 的偶发网络错误通常短暂，0.5s/1s/2s 足够覆盖。
2. 30s 以上等待应交给更高层状态/用户可见 UI，而不是悄悄卡住。
3. v1 还没有 failover，不宜做 2 分钟级等待。

`LLMConfig` 增加字段：

```python
retry: LLMRetryConfig = field(default_factory=LLMRetryConfig)
openai_sdk_max_retries: int | None = None
```

`openai_sdk_max_retries=None` 表示不覆盖 SDK 默认行为。transport 在自己已经接入 Pulsara safe retry 后，可以把 `None` 解析为该 transport 的 owned default `0`；未接入 Pulsara retry 的 transport 必须继续不覆盖 SDK 默认 retry，避免中间 PR 倒退。

config 校验规则需要在 PR1 拍板：

1. `attempts` clamp 到 `>= 1`；禁用 retry 用 `enabled=False`，不用 `attempts=0`。
2. `base_delay_seconds <= 0` 或 `max_delay_seconds <= 0` 直接 `ValueError`。
3. `max_delay_seconds < base_delay_seconds` 直接 `ValueError`。
4. `jitter_ratio` clamp 到 `[0.0, 1.0]`。
5. `max_retry_after_seconds <= 0` 直接 `ValueError`。

`build_llm_runtime(config)` 必须把 retry config 注入两个 OpenAI transport，不能让 transport 自己从 env 读取，避免配置源分裂。

## 6. Error classification

新增：

```python
class RetryDecisionKind(StrEnum):
    RETRY = "retry"
    DO_NOT_RETRY = "do_not_retry"

@dataclass(frozen=True, slots=True)
class LLMRetryDecision:
    kind: RetryDecisionKind
    reason: str
    status_code: int | None = None
    retry_after_seconds: float | None = None
    retry_after_exceeded: bool = False
    provider_code: str | None = None
```

### 6.1 Retryable

以下默认 retryable：

- OpenAI SDK `APIConnectionError`
- OpenAI SDK `APITimeoutError`
- Python / httpx 常见连接错误：
  - `TimeoutError`
  - `ConnectionError`
  - `ConnectionResetError`
  - `ConnectionAbortedError`
  - `BrokenPipeError`
  - `ReadTimeout`
  - `ConnectTimeout`
  - `PoolTimeout`
  - `ConnectError`
  - `ReadError`
  - `RemoteProtocolError`
  - SSL/TLS transient errors
- HTTP `408`
- HTTP `409`
- HTTP `429`
- HTTP `500`
- HTTP `502`
- HTTP `503`
- HTTP `504`
- HTTP `529`

### 6.2 Non-retryable

以下默认 non-retryable：

- HTTP `400`，除非 provider 明确给出 transient code。
- HTTP `401`
- HTTP `403`
- HTTP `404` model not found / endpoint not found。
- HTTP `413` payload too large / context too long。
- `invalid_request_error`
- `unsupported_parameter`
- `unknown_parameter`
- `invalid_schema`
- `tool schema invalid`
- `authentication`
- `insufficient_quota` / billing failure。

### 6.3 5xx 中的 deterministic bad request

部分 OpenAI-compatible gateway 会用 `500/502` 包装 deterministic request validation error。classifier 不能只看 status。

即使 status 是 5xx，只要 message/body/code 包含以下信号，也应 fail fast：

```text
unknown parameter
unsupported parameter
invalid request
invalid_request_error
schema validation
tool schema
model not found
does not support
unsupported value
```

这条来自 Hermes 的教训：否则一次坏参数会被重复打到 provider。

### 6.4 Unknown

unknown error 的 v1 默认建议：

1. 如果 exception chain 中有明确 transport cause，按 retryable transport。
2. 如果只有普通 `RuntimeError("Connection error.")`，但 cause chain 指向 `OSError`/connection reset/remote protocol，则 retryable。
3. 如果完全无法识别，do not retry，但在 metadata 中记录 `classification="unknown_non_retryable"`。

这个默认偏保守，避免把业务错误打成 retry flood。

## 7. Retry-After 解析

解析来源：

1. response headers：
   - `retry-after-ms`
   - `Retry-After`
   - `retry-after`
2. OpenAI SDK exception fields：
   - `response.headers`
   - `body`
3. provider body/message：
   - `retry_after`
   - `retryAfter`
   - `retry_after_ms`
   - message 中的 `try again in 1.5s` / `retry after 500ms`

支持格式：

1. seconds number。
2. milliseconds number。
3. HTTP-date（可作为后续增强；v1 可以先解析失败后忽略）。

规则：

1. `Retry-After` 是下限，计算 backoff 时使用 positive jitter。
2. 若 `retry_after_seconds > max_retry_after_seconds`，v1 不 sleep，直接最终失败并标记 exceeded。
3. 若没有 retry-after，使用 exponential backoff + symmetric jitter。

## 8. Backoff

建议函数：

```python
def compute_retry_delay(
    *,
    attempt_index: int,
    config: LLMRetryConfig,
    retry_after_seconds: float | None,
    rng: random.Random | None = None,
) -> float:
    ...
```

语义：

- `attempt_index` 从 1 开始，表示第几次失败后准备下一次 attempt。
- 没有 Retry-After：
  - `base * 2 ** (attempt_index - 1)`
  - cap 到 `max_delay_seconds`
  - symmetric jitter：`delay +/- delay * jitter_ratio`
- 有 Retry-After：
  - `delay = retry_after_seconds`
  - positive jitter：`delay + random(0, delay * jitter_ratio)`
  - cap 前先检查 `max_retry_after_seconds`

测试需要 patch random 或传 deterministic rng，避免 flaky。

## 9. Stream retry runner

建议 provider-neutral runner：

```python
@dataclass(slots=True)
class RetryAttemptTrace:
    attempt: int
    max_attempts: int
    error_type: str
    error_message: str
    reason: str
    delay_seconds: float | None
    status_code: int | None = None
    retry_after_seconds: float | None = None

async def retry_provider_stream(
    *,
    config: LLMRetryConfig,
    operation: Callable[[], Awaitable[AsyncIterator[Any]]],
    classify: Callable[[BaseException], LLMRetryDecision],
    on_retry: Callable[[RetryAttemptTrace], Awaitable[AgentEvent | None] | None],
    has_semantic_output: Callable[[], bool],
) -> AsyncIterator[Any]:
    ...
```

更实际的 v1 可以不抽象到完全 provider-neutral，但 Responses 和 Chat Completions 必须共享同一个 classifier/backoff 代码。

### 9.1 Responses integration

当前结构：

```python
stream = await client.responses.create(**payload, stream=True)
async for raw_event in stream:
    events = translate_responses_event(...)
    for event in events:
        yield event
```

v1 改为：

1. `yield builder.model_start()` 只发生一次。
2. 初始化 `retry_trace=[]`。
3. 对 attempt loop：
   - create stream。
   - consume stream。
   - translate events。
   - 每次 builder 生成 text/thinking/tool-call/run-error event 时，由 builder 自己标记 `has_semantic_output=True`。
4. exception：
   - close stream/client。
   - classify。
   - 如果可 retry 且 `not builder.has_semantic_output` 且 attempts 未耗尽：
     - yield optional `CustomEvent(name="llm.retry", value=trace)`。
     - sleep。
     - continue。
   - 否则 close active blocks，yield final `RunErrorEvent`。

### 9.2 Chat Completions integration

Chat Completions 有额外风险：

1. `ChatToolCallAccumulator` 会把 tool call argument delta 手动拼接。
2. 如果 mid-output retry，accumulator 状态会和新 attempt 混在一起。
3. 因此只允许 semantic output 前 retry。

每个 retry attempt 如果发生在 semantic output 前，必须重建：

- `ChatToolCallAccumulator`
- provider stream iterator

但不重建 builder 的已发 `ModelCallStartEvent`。由于还没有 semantic output，builder 应没有 active text/thinking/tool blocks；如果有，则说明 retry gate 错了，应 fail closed。

### 9.3 Test seam

retry adapter 测试必须使用 `_client` 注入 fake client，而不是扩展 `_mock_events` / `_mock_chunks`。

原因：

1. `_mock_events` 和 `_mock_chunks` 在 provider client 调用之前就分流返回，完全绕过 `client.responses.create()` / `client.chat.completions.create()`。
2. retry 的关键路径是「第一次 provider call raise，第二次 provider call 返回 async iterator」。
3. fake client 应支持 per-call script，例如 `raise connection error -> return stream chunks`，并记录调用次数、close 次数、是否复用了注入 client。

### 9.4 Client lifecycle in retry tests

retry 测试必须覆盖 client 生命周期：

1. 注入 `_client` 时，transport 不调用 `close()`。
2. owned client 时，整个 retry loop 结束后只 close 一次。
3. pre-output retry 后成功时，client 没有在第一次 attempt 后被提前 close。

## 10. Observability

### 10.1 Retry attempt event

v1 推荐使用现有 `CustomEvent`：

```python
CustomEvent(
    **event_context.event_fields(),
    name="llm.retry",
    value={
        "api": "openai_chat_completions",
        "provider": model.provider,
        "model": model.id,
        "base_url": model.base_url,
        "attempt": 1,
        "max_attempts": 3,
        "reason": "connection_error",
        "delay_seconds": 0.73,
        "status_code": None,
        "retry_after_seconds": None,
        "has_semantic_output": False,
    },
)
```

`CustomEvent` 是过渡方案。若 UI 需要专门渲染 retry，可在 v2 新增 `LLMRetryAttemptEvent`。

### 10.2 Final error metadata

最终 `RunErrorEvent.metadata["provider_data"]` 应包含：

```json
{
  "type": "...",
  "message": "...",
  "causes": [...],
  "retry": {
    "enabled": true,
    "attempts": 3,
    "exhausted": true,
    "has_semantic_output": false,
    "traces": [...]
  }
}
```

如果不 retry，metadata 也应说明原因：

```json
{
  "retry": {
    "enabled": true,
    "attempts": 1,
    "skipped_reason": "non_retryable_invalid_request"
  }
}
```

### 10.3 Logging

日志至少包含：

- provider
- api
- model
- base_url host（避免泄漏完整敏感 query）
- attempt / max_attempts
- delay
- status_code
- provider code
- exception type
- root cause type/message
- retry_after source
- has_semantic_output

不要在日志里打印 API key 或完整 request body。

## 11. PR 分解

### PR1：Retry config + classifier + backoff

新增：

- `src/pulsara_agent/llm/retry.py`
- `src/pulsara_agent/llm/adapters/openai/errors.py`
- `LLMRetryConfig`
- env parsing
- `parse_retry_after_seconds()`
- `classify_llm_error()`
- `compute_retry_delay()`

测试：

1. connection / timeout exception retryable。
2. OpenAI SDK-like `status_code=429` retryable。
3. `retry-after-ms` / `Retry-After` 解析。
4. retry-after 超 cap 后不 sleep。
5. 400 invalid request non-retryable。
6. 500/502 deterministic invalid parameter non-retryable。
7. 503/529 retryable。
8. 413 non-retryable。
9. jitter/backoff deterministic。

PR1 不改 adapter 行为，只打基础。

### PR2：OpenAI SDK client retry plumbing（不改变行为）

改：

- `build_async_openai_client(..., max_retries: int | None = None)`
- `LLMConfig.openai_sdk_max_retries: int | None`
- `build_llm_runtime(config)` 注入 retry config 和 SDK retry config。
- 默认 `None` 不传给 `AsyncOpenAI`，保持 SDK 当前默认 retry 行为。

测试：

1. 默认 client construction 不传 `max_retries`，不改变现有行为。
2. env 显式设置时能传入 `max_retries`。
3. 两个 transport 接收到同一份 retry config。
4. PR2 不关闭任何 transport 的 SDK retry，避免倒退窗口。

### PR3：Responses adapter safe retry

改：

- `OpenAIResponsesTransport`
- 只 emit 一次 `ModelCallStartEvent`。
- create/consume stream 中 safe failure 自动 retry。
- retry attempt emit `CustomEvent(name="llm.retry")`。
- final `RunErrorEvent` 带 retry trace。
- Responses transport 在同一 PR 内接管 retry，并为 Responses client 设置 SDK `max_retries=0`（除非用户显式覆盖）。

测试：

1. create stream 前 connection error，第二次成功。
2. stream 首个 raw event 前 read timeout，第二次成功。
3. text delta 后 read timeout，不 retry，发 final error。
4. provider `response.failed` / `RunErrorEvent` 后不 retry。
5. retry exhausted 后只发一个 final `RunErrorEvent`。
6. `ModelCallStartEvent` 只出现一次。
7. active blocks 在 final error 前关闭。
8. 用 `_client` fake 脚本测试 fail-then-succeed，不使用 `_mock_events`。
9. 注入 `_client` 不被 close；owned client retry loop 后只 close 一次。

### PR4：Chat Completions adapter safe retry

改：

- `OpenAIChatCompletionsTransport`
- 与 Responses 共用 retry helper。
- semantic output 前失败可 retry。
- tool call delta 后失败不 retry。
- Chat transport 在同一 PR 内接管 retry，并为 Chat client 设置 SDK `max_retries=0`（除非用户显式覆盖）。

测试：

1. create stream 前 connection error，第二次成功。
2. thinking delta 前失败 retry。
3. text delta 后失败不 retry。
4. tool call delta 后失败不 retry。
5. retry 后 accumulator 状态不泄漏到新 attempt。
6. final error metadata 带 retry trace。
7. 用 `_client` fake 脚本测试 fail-then-succeed，不使用 `_mock_chunks`。
8. 注入 `_client` 不被 close；owned client retry loop 后只 close 一次。

### PR5：Runtime/UI 可观测收口

改：

- 确认 `CustomEvent(name="llm.retry")` 能被 event log、timeline、UI projection 合理保留。
- 若 timeline 当前忽略 `CustomEvent`，先不强行入模型上下文，但 UI/event log 应能看到。

测试：

1. retry event 按 sequence 保存。
2. final `RunErrorEvent` 包含 retry summary。
3. agent loop 的 `reply_had_run_error` 仍只在最终失败时触发；中间 retry event 不触发 recovery turn。

## 12. 测试矩阵

### 12.1 Unit tests

`tests/test_llm_retry.py`：

1. `RetryConfig` env parsing。
2. invalid attempts/base/max/jitter 按 §5 的固定规则 clamp 或 raise。
3. classifier status code。
4. classifier exception type。
5. classifier exception chain。
6. deterministic 5xx bad request fail fast。
7. retry-after headers/body/message parsing。
8. positive jitter 不低于 retry-after。
9. symmetric jitter 在 expected range。

### 12.2 Adapter fake-client tests

`tests/test_llm_runtime.py`：

1. Responses pre-output retry success。
2. Responses post-text failure no retry。
3. Responses retry exhausted final error。
4. Chat pre-output retry success。
5. Chat post-tool-delta failure no retry。
6. Chat retry exhausted final error。
7. retry attempt event order。
8. retry trace metadata。

### 12.3 Runtime integration tests

1. 中间 retry 成功时，agent loop 不进入 recovery mode。
2. 最终失败时，agent loop 仍按 `max_consecutive_model_failures` 预算恢复。
3. `CustomEvent(name="llm.retry")` 不污染 assistant replay 文本。

### 12.4 Real LLM smoke

Real LLM 不应作为 retry 可靠性的主要证明，因为真实网络抖动不可控。

保留两个 smoke：

1. Responses happy path：确认 retry config 不破坏真实 Responses。
2. Chat Completions happy path：确认 DeepSeek/OpenAI-compatible streaming 仍能消费 text/thinking/tool call delta。

如果真实 provider 偶发 `Connection error.`，用日志确认：

1. classifier 是否命中。
2. attempt trace 是否记录。
3. 最终成功/失败路径是否符合 safe retry gate。

## 13. 风险与取舍

### 13.1 Mid-stream retry 被 v1 禁止

这意味着某些真实网络中断仍会失败，即使 provider request 理论上重跑可能成功。

这是有意识的取舍：Pulsara event log 是 append-only，v1 没有撤销 delta 或去重 replay 的能力。宁可少 retry，也不要制造重复 tool call / 重复文本。

### 13.2 Thinking delta 也算语义输出

即使 thinking 不一定进入最终用户回答，它仍是 provider 输出并会进入 event log。mid-thinking retry 也可能重复或污染调试轨迹，因此 v1 视为不安全 retry。

### 13.3 SDK retry 关闭可能暴露更多短错误

关闭 SDK retry 后，Pulsara 会更早看到 transient error。这是预期行为，因为 Pulsara 自己会做可观测 retry。

如果某 provider SDK 内置 retry 对特殊 header 处理更好，可以通过 `PULSARA_OPENAI_SDK_MAX_RETRIES` 暂时打开，但默认不建议双层 retry。

### 13.4 Retry-After cap 可能早于 provider 建议失败

如果 provider 要求等待 120 秒，而 Pulsara cap 是 30 秒，v1 会失败并暴露 metadata，而不是等待。未来 model failover/credential rotation 接入后，可在这里转入 fallback。

### 13.5 Classifier 会逐步扩展

OpenAI-compatible 供应商错误格式不统一。v1 classifier 应先覆盖 DeepSeek/OpenAI 常见路径，并保留 provider_data，方便后续根据真实错误继续补规则。

## 14. 实施顺序建议

推荐顺序：

1. PR1 classifier/backoff/config。
2. PR2 SDK retry plumbing（不改变行为）。
3. PR3 Responses safe retry。
4. PR4 Chat Completions safe retry。
5. PR5 observability/timeline 收口。

顺序不变量：关闭某个 transport 的 SDK retry 必须和该 transport 的 Pulsara safe retry 同 PR 落地。不能先全局设置 `max_retries=0`，再分 PR 补 Responses/Chat retry；那会在中间状态制造比现状更差的网络抖动窗口。

不要先在 adapter 里手写局部 retry。否则 Responses 和 Chat Completions 会各自长出一套不同分类逻辑，后续更难维护。

## 15. v1 完成后的判断标准

完成后，遇到 DeepSeek 这类 `Connection error.` 应能回答四个问题：

1. 这是哪个 provider/model/base_url 的哪一次 attempt？
2. root cause 是 connection reset、timeout、rate limit，还是 unknown？
3. 系统为什么 retry / 为什么不 retry？
4. 如果 retry 了，等了多久，是否因为已经输出 delta 而停止 retry？

当这四个问题都能从 event log / metadata / logs 中回答，Pulsara 的 LLM retry 才算进入可维护状态。
