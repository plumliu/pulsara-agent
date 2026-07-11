# Pulsara Resolved Model Target / Call 与模型上下文预算单一真源 Hard Cut 实施规格

> 状态：Implemented and verified（2026-07-11）
>
> 适用范围：模型槽位配置、LLM resolution、ContextCompiler、AgentRuntime、transport、context compaction、memory governance/reflection、typed events、inspector/replay
>
> 依赖文档：ARCHITECTURE_DEBT_AUDIT.zh.md 第 2、13、14 节
>
> 实施策略：开发期 hard cut；不保留旧 model limits、旧 stream(role/options) API、旧 event payload 或静态 compaction threshold 的生产兼容路径

> 最终验证（2026-07-11）：`uv run ruff check src tests evals`、`git diff --check`
> 与 `python -m compileall` 通过；默认测试集 `1681 passed, 68 skipped`；启用真实
> DeepSeek provider 的完整 `real_llm` 集合 `53 passed, 14 skipped`。第 20 节列出的
> 201 个精确测试名全部存在并包含在通过的测试集中；PostgreSQL model-call event
> round-trip、Inspector join/usage/identity、alias/snapshot 默认接受、`exact` 拒绝与
> secret-safe JSON payload 另有定向通过记录。

## 0. 文档目标

本文把架构债务审计中的 “ResolvedModelCall / model context limits 单一真源” 收敛成可以直接拆 PR、写代码、迁移数据并验收的工程规格。

这不是给 ModelProfile 补两个可选数字，也不是把 compiler 中的 256000 改成另一个常量。真正目标是冻结一次模型执行从配置到 provider request 的完整合约：

1. 模型槽位必须提供可验证的窗口和输出限制；
2. preflight/manual compaction 可以解析模型 target，但不能伪造一次模型 call；
3. 每次真实模型调用只解析一次 ResolvedModelCall；
4. compiler、最终 payload validator、LLMRuntime 和 transport 消费同一个 call；
5. context retry、mid-turn compaction retry 和 transport network retry 不得重新解析模型；
6. typed events 保存同一个 event-safe fact identity；
7. compaction 明确区分 target model limits 与 summarizer model limits；
8. governance、reflection 等不经过主 ContextCompiler 的 side call 也必须在 provider 前 fail closed。

完成后，Pulsara 的模型执行主线只有一条：

~~~text
required model slot config
        |
        v
LLMRuntime.resolve_target(role, requested_options)
        |
        +----> preflight/manual compaction target contract
        |
        v
LLMRuntime.resolve_call(target, purpose)
        |
        +----> ContextCompiler allocation
        +----> ContextCompiledEvent
        +----> final LLMContext validation
        +----> ModelCallStartEvent
        +----> LLMTransport
        +----> ModelCallEndEvent
~~~

## 1. 冻结结论

### 1.1 Target 与 Call 是两层不同身份

V1 必须同时存在 ResolvedModelTarget 与 ResolvedModelCall。

ResolvedModelTarget 表示：

- 选中了哪个模型槽位和模型；
- 绑定哪个 transport；
- effective options 是什么；
- 该模型的窗口、输入、输出限制是什么；
- 使用哪个 token estimator；
- 最终有效输入预算是多少；
- 这些字段的稳定 fingerprint 是什么。

ResolvedModelCall 表示：

- 这一次可能真正发起的模型调用；
- 它引用哪个 target；
- 它服务于哪一种 call purpose；
- 它使用唯一、随机的 resolved_model_call_id。

Target 没有随机 call identity，可以用于：

- HostSession preflight compaction；
- 用户手动 compaction；
- compaction threshold 计算；
- run 启动前的模型合约冻结；
- recovery 时验证当前 runtime binding 是否仍与旧 run 合约一致。

Call 只用于一次可能发起的模型执行。不得为了 manual compaction 或 preflight compaction 伪造主模型 call ID。

### 1.2 一个 Agent run 冻结一个主模型 target

HostSession 在本轮 user run 建立前解析一次主模型 target：

~~~text
resolve run target
    -> preflight compaction 使用该 target
    -> create LoopState / RunStartEvent
    -> AgentRuntime 首次模型调用复用该 target
    -> tool follow-up 模型调用继续复用该 target
~~~

同一 run 内：

- target 不变；
- 每个模型 loop step 创建新的 ResolvedModelCall；
- compile retry 复用当前 call；
- network retry 复用当前 call；
- tool follow-up 创建新 call；
- permission resume 后的下一次 follow-up 创建新 call，但继续使用原 run target。

V1 禁止同一 run 内 transport 自行 fallback 到另一个模型。未来若支持模型切换，必须回到 coordinator，解析新 target、写 typed fact，并重新 compile。

### 1.3 RunStartEvent 是该 run 的模型 target contract

RunStartEvent 新增 required 字段 model_target，值为 ResolvedModelTargetFact。

这使 run-bound permission contract 与 run-bound model target contract 对齐：

- run permission snapshot 不从 session 当前 default 猜；
- run model target 也不从恢复时当前 LLMConfig 猜；
- active/suspended run 恢复时，从 RunStartEvent 读取 target fact；
- runtime 只能 rebind 一个 fingerprint 完全一致的 target；
- 当前配置无法重建相同 target 时，resume fail closed；
- 不静默换模型、不静默换输出预算、不静默换 tokenizer/estimator。

### 1.4 Compiler 与 transport 必须消费同一个 Call

禁止继续存在：

~~~python
build_compiled_context(model_role=role, ...)
llm_runtime.stream(role=role, options=options, ...)
~~~

生产 API hard cut 为：

~~~python
target = llm_runtime.resolve_target(
    role=ModelRole.PRO,
    requested_options=options,
)
call = llm_runtime.resolve_call(
    target=target,
    purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
)
compiled = build_compiled_context(
    resolved_call=call,
    ...,
)
async for event in llm_runtime.stream(
    call=call,
    context=compiled.llm_context,
    event_context=event_context,
):
    ...
~~~

不得保留 stream(role=..., options=...) compatibility overload。

### 1.5 输出限制只有一个生产真源

生产 runtime 的 LLMOptions 只保留唯一的 per-call option：

~~~python
@dataclass(frozen=True, slots=True)
class LLMOptions:
    reasoning_effort: str | None = None
~~~

temperature 与 reasoning_summary 永不由 Pulsara 发送。max_output_tokens 不属于 LLMOptions；resolve_target() 必须从 model slot 生成非空 effective max output：

~~~text
effective_output_tokens = ModelContextLimits.default_output_tokens
~~~

强 invariant：

~~~text
1 <= effective_output_tokens <= ModelContextLimits.max_output_tokens
~~~

超过限制是 ModelContextLimits / ModelSlotConfig 配置错误，不静默 clamp。

provider_profile.request_defaults、request_extra_body、thinking omit rules 不得再次提供或删除输出 token cap。以下已知 key 在生产配置中必须被 validator 拒绝：

- max_output_tokens；
- max_completion_tokens；
- max_tokens；
- 以及 adapter 明确登记为 output-budget alias 的 provider key。

transport payload 必须始终从 ResolvedModelContextBudgetFact.effective_output_tokens 显式写入 output cap，不能依赖 provider default。

### 1.6 最终输入预算公式冻结

ModelContextLimits 至少包含：

~~~python
class ModelContextLimits:
    total_context_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    default_output_tokens: int
    input_safety_margin_tokens: int
~~~

一次 resolved target 的预算计算为：

~~~text
pre_margin_input_tokens =
    min(
        max_input_tokens,
        total_context_tokens - effective_output_tokens,
    )

input_budget_tokens =
    pre_margin_input_tokens - input_safety_margin_tokens
~~~

要求 input_budget_tokens >= 1，否则 target resolution 失败。

input_safety_margin_tokens 是模型槽位的 required Pulsara policy，不在 compiler 中按窗口百分比临时计算。

### 1.7 最终 provider payload 前必须再次校验

ContextCompiler 的 allocation 成功不是 provider send 的充分条件。

LLMRuntime.stream() 必须在 ModelCallStartEvent 之前，使用同一个 target.token_estimator 对最终 LLMContext 重新估算：

- system prompt；
- 全部 messages；
- tool schemas；
- provider-neutral envelope overhead；
- V1 estimator 固定的 conservative framing overhead。

若 estimate 超过 call.target.context_budget.input_budget_tokens：

- 不发起 provider call；
- 不写 ModelCallStartEvent；
- 抛 ModelInputBudgetExceeded；
- 主 Agent 路径写 ModelCallRejectedEvent；
- compaction/reflection 由各自 terminal fact 记录 failure；
- governance 由 MemoryGovernanceRunResult 记录 failure 与 call fact，durable governance audit 等后续 UOW 大章统一落地；
- 不允许 transport 自行发送后让 provider 返回 overflow。

### 1.8 Compaction 永远有两组 limits

compaction 同时消费：

1. target_model_target：
   - 主模型下一轮的输入预算；
   - 决定是否 compact；
   - 决定 threshold 和 post-compaction target；
   - 不是一次主模型 call。
2. summarizer_call：
   - flash summarizer 的真实调用；
   - 决定 compaction prompt/input 是否可进入 flash window；
   - 决定 summary 最大输出；
   - 有真实 resolved_model_call_id。

禁止把两者合并成一组 context_window_tokens。

### 1.9 Hard cut，不做兼容推断

V1 明确不做：

- 根据模型名猜窗口；
- 缺 limits 时回退 256000；
- 缺 output limit 时回退 8000 或 8192；
- 从旧 ContextCompactionPolicy 读取窗口；
- 从 provider response error 反推窗口；
- 为旧 event log 补默认 resolved facts；
- 对旧 active run 使用当前 config 猜 target；
- 保留旧 stream(role/options) 生产路径。

旧 event log、旧 manifest、旧测试 fixture 和缺少 limits 的环境配置必须迁移或重置。

### 1.10 Model context budget 不等于 execution/rollout budget

本章中的 budget 只表示一次 resolved model call 的 provider context feasibility：

- 该模型一次请求最多能接收多少 input；
- 为本次请求保留多少 output；
- estimator/safety margin 后最终可发送多少 model-visible context；
- 最终 provider payload 是否必须在发送前 fail closed。

它不表示：

- 一个 run/session/goal 最多允许多少次 model follow-up；
- 最多允许多少次 tool call；
- 是否因为连续搜索、轮询或低信息增益而要求模型停止；
- 是否进入 soft pressure、forced finalization 或 wall-clock deadline；
- root agent 与 subagent 如何共享累计推理资源。

这些属于未来独立的 RunExecutionBudgetPolicy / RolloutBudgetCoordinator，由 AgentRuntime/Host coordinator 持有，不进入 ResolvedModelTarget 或 ResolvedModelCall。

强边界：

- ResolvedModelTarget 只持有 immutable context limits/context budget；
- ResolvedModelCall 只持有一次调用身份、purpose、target 与 context mode；
- mutable remaining budget、pressure level、deadline、no-progress count 不得进入 target/call fact 或 fingerprint；
- context compaction 只能恢复 model-visible context capacity，不能重置未来累计 rollout usage；
- terminal/MCP/background process 的外部等待时间不等同于新的 model call，也不能仅因 wall-clock 较长被误判为递归工具循环；
- 每个成功收口的 ModelCallEndEvent 提供未来 rollout accounting 可消费的 usage_status 与可选 usage fact；missing 不是零，但本章不根据累计 usage 改变 AgentRuntime continuation policy。

## 2. 当前代码真相与具体债务

### 2.1 ModelProfile 的 limits 可空

当前 [llm/models.py](src/pulsara_agent/llm/models.py) 中：

~~~python
context_window: int | None = None
max_output_tokens: int | None = None
~~~

这两个字段：

- 语义不足以表达 total/input/output/default 的区别；
- 允许 production model 没有窗口；
- 不能表达 Pulsara safety margin；
- 没有 validator；
- 不能形成有效 input budget。

### 2.2 LLMConfig 根本不解析 limits

当前 [llm/config.py](src/pulsara_agent/llm/config.py) 的 LLMConfig.from_env() 只解析：

- API key；
- base URL；
- pro/flash model name；
- provider profile；
- retry。

model_for(role) 构造 ModelProfile 时没有设置 context_window 或 max_output_tokens。

因此即使 ModelProfile 有可选字段，默认生产路径也永远没有真实 limits。

### 2.3 Compiler 使用固定窗口和输出保留

当前 [context_engine/compiler.py](src/pulsara_agent/runtime/context_engine/compiler.py)：

~~~python
def _context_window_tokens(request):
    return 256_000

def _reserved_output_tokens(request):
    return 8_000
~~~

并且 safety margin 固定为 context window 的 25%。

在 hard cut 之前，这会导致：

- 小窗口模型被错误放行；
- 大窗口模型被过早降级；
- per-call output override 与 compiler reservation 相互漂移；
- transport 实际输出 cap 与 compiler reserved output 漂移。

### 2.4 总量超预算目前仍可发送

当前 _apply_section_budget() 在所有可降级 section 都处理完后，若总量仍超出预算，只写：

~~~text
context_budget_still_exceeded_after_degradation
~~~

severity 是 warning。

随后只有 current user section 单独超预算才抛 ContextBudgetExceeded。也就是说：

- system + transcript + tool result + tools 总量可以超过预算；
- 只要 current user 自身没有超过预算，provider call 仍可能发起。

这是 production correctness bug，不是优化问题。

### 2.5 AgentRuntime 的 pressure/failed fact 再次写死

当前 [runtime/agent.py](src/pulsara_agent/runtime/agent.py) 在 ContextBudgetExceeded 分支构造 pressure/failed ContextCompiledEvent 时再次写：

- context_window_tokens=256000；
- reserved_output_tokens=8000。

因此 compiler 即使未来先改，异常事件仍可能审计错误。

### 2.6 LLMRuntime 二次解析模型

当前 [llm/runtime.py](src/pulsara_agent/llm/runtime.py) 的 stream() 接收 role/options，然后：

~~~text
config.model_for(role)
registry.get(model.api)
transport.stream(model, context, options)
~~~

compiler 不持有这个结果。两层没有结构保证使用同一：

- model id；
- provider/API；
- effective output；
- limits；
- estimator；
- target fingerprint。

### 2.7 Transport 自己写 ModelCallStart/EndEvent

当前 OpenAI Responses、Chat Completions 和 MockTransport 都在 transport 内写 ModelCallStartEvent，并由 adapter event builder 写 ModelCallEndEvent。

这意味着：

- transport 控制 call identity；
- scripted test transport 可以遗漏或伪造 start；
- runtime 无法在 start 前统一做最终 budget validation；
- estimated input 无法由同一个 pre-send result 注入 end；
- 不同 transport 可能写不同 model fields；
- 未来 fallback/retry 容易生成重复 identity。

### 2.8 Provider defaults 可以绕过 resolved-call contract

当前两个 OpenAI-compatible payload builder 都会把 provider_profile.request_defaults 合并进 payload。

在 hard cut 前，request defaults / extra body 可以注入 instructions、messages/input、tools、model、temperature、reasoning、tool_choice 或 output cap；thinking policy 还能从 options 中 omit 参数。因此：

- estimator 可能只看见原 context，而 provider 收到额外 system/tool payload；
- ResolvedModelCall 记录的 effective output 不等于 provider payload；
- input budget 公式失去意义；
- 同一个 target fingerprint 可能发送不同预算。

因此 resolver/config validator 必须递归剥离全部 Pulsara-owned/model-visible payload keys，而不只是 output-budget keys。

### 2.9 Compaction 使用另一套静态预算

当前 [runtime/compaction/service.py](src/pulsara_agent/runtime/compaction/service.py) 的 ContextCompactionPolicy 含：

- context_window_tokens=256000；
- auto_threshold_tokens=200000；
- summary_max_output_tokens=8192；
- chars_per_token；
- event_chars_per_token；
- estimate_safety_margin。

这些字段同时混合：

- 主模型下一轮窗口；
- compaction trigger；
- flash summarizer 输出；
- compaction input estimate。

### 2.10 Preflight/manual compaction 没有主模型 call

当前 HostSession 在创建本轮 LoopState 前执行 preflight compaction。

用户 manual compact 发生在 idle session，也没有随后必然发生的主模型调用。

如果只有 ResolvedModelCall 而没有 ResolvedModelTarget，实现者会被迫：

- 为不存在的主调用伪造 call ID；或
- 重新引入一套单独的 limits resolver。

### 2.11 四条生产调用路径都直接调用旧 API

当前直接调用 LLMRuntime.stream(role/options) 的生产路径至少包括：

1. AgentRuntime；
2. ContextCompactionService；
3. MemoryGovernanceEngine；
4. MemoryReflectionEngine。

Subagent 复用 AgentRuntime，不应再实现一套 resolver。

### 2.12 Inspector 只按 context_id join

当前 inspector 将 ModelCallStartEvent 与 ContextCompiledEvent 按 context_id join。

问题：

- context compile retry 可以产生多个 context；
- call identity 与 context identity 不是同一概念；
- direct side call 本来就没有 ContextCompiledEvent；
- inspector 会把 governance/reflection/compaction start 误报为 missing compiled context；
- model/limits/options 是否一致无法验证。

### 2.13 测试迁移面较大且必须一次完成

当前仓库约有：

- 50 余处 LLMConfig 直接构造；
- 多个 production/mock transport；
- 十余组 scripted transport；
- 大量手写 ModelCallStartEvent、ContextCompiledEvent；
- compaction/inspector/event contract 固定 256000 的断言。

这不是保留 compatibility overload 的理由。应提供集中 test fixture builder，然后一次 hard cut 更新。

### 2.14 Transport binding 目前只有 API key

当前 LLMTransportRegistry 只按 api 字符串查 adapter。event/durable config 无法区分：

- 同一 api key 下的两个不同 adapter implementation；
- adapter contract version变化；
- 测试 transport 与生产 transport；
- payload builder contract升级。

因此仅保存 api 不能证明 recovery/pre-send 使用同一 transport binding。

### 2.15 Effective options 仍可能在 adapter 中二次省略

当前 Responses/Chat payload builder 在 transport 内根据 thinking profile 再次 omit temperature/reasoning。

这意味着 fact 若保存 caller requested options，payload 可能静默不同。V1 不建立 omission state：caller 显式请求而 provider 不允许发送时，resolve_target 直接失败；payload builder 不再拥有第二次决策权。

### 2.16 Provider usage missing 被写成全零

当前 usage_from_mapping(None) 返回 Usage()，ModelCallEndEvent 因而无法区分：

- provider 真实报告 0 token；
- provider 完全没返回 usage；
- usage payload不完整。

governance/reflection/compaction 又会消费并丢弃 stream 中的 end event，因此 outer durable fact也没有 usage来源。

### 2.17 Tool-result renderer 仍有独立 estimator

render_segmented_llm_messages() 在 ContextCompileRequest 构造前运行，tool_results.py 使用自己的 ceil(chars/4) helper。

仅把 estimator 放入 ContextCompileRequest 不够；resolved call 必须在 renderer 前可用，renderer/cache 也必须显式接收 estimator/fingerprint。

## 3. 目标模块边界与依赖方向

### 3.1 新的低层 primitives 模块

新增：

~~~text
src/pulsara_agent/primitives/model_call.py
~~~

只定义 event-safe、不可变、可序列化 contract：

- ModelContextLimits；
- ResolvedModelOptionsFact；
- TokenEstimatorFact；
- ResolvedModelContextBudgetFact；
- ModelTokenUsageFact；
- ModelCallDiagnosticFact；
- ContextBudgetReportEvent；
- CompactionTargetEstimateFact；
- ResolvedModelTargetFact；
- ResolvedModelCallFact；
- ModelCallPurpose；
- ModelContextMode。

该模块：

- 不 import event；
- 不 import runtime；
- 不 import transport；
- 不持有 API key；
- 不持有完整 base URL；
- 不持有 callable；
- 不持有 provider SDK client。

ContextCompiledEvent 使用的 ContextBudgetReportEvent 同样定义在 primitives/model_call.py。event/events.py 不得反向 import runtime/context_engine/types.py。

依赖方向冻结为：

~~~text
primitives.model_call
    -> llm.models / llm.resolution
    -> event.schema
    -> context compiler / llm runtime / side engines
    -> host/runtime wiring
~~~

### 3.2 Runtime-only resolution 模块

新增：

~~~text
src/pulsara_agent/llm/resolution.py
~~~

定义：

- ModelSlotConfig；
- ResolvedModelTarget；
- ResolvedModelCall；
- TokenEstimator protocol；
- PulsaraHeuristicTokenEstimatorV1；
- target/call resolver helpers；
- endpoint redaction/fingerprint；
- provider option validation。

ResolvedModelTarget 可以持有：

- ModelProfile；
- LLMTransport；
- Resolved LLMOptions；
- TokenEstimator；
- event-safe fact。

这些 runtime object 不进入 event payload。

### 3.3 Compiler 不重新解析 transport/config

ContextCompiler 只接收已经解析好的 ResolvedModelCall。

它不 import LLMConfig，不读 env，不按 role 调 model_for()。

### 3.4 Transport 不重新解析 role/options/limits

LLMTransport 接口只接收 ResolvedModelCall 与 LLMContext。

transport 不接收独立 ModelProfile 或 LLMOptions 参数，避免三份可冲突输入。

### 3.5 Side engines 不创建第二套 budget helper

compaction、governance、reflection 必须调用 LLMRuntime resolver 和 validator。

禁止在各 subsystem 内新增：

- flash_context_window；
- max_flash_input；
- summarizer_output_default；
- governance_max_context；
- reflection_token_estimator。

## 4. Model limits 与模型槽位配置 hard cut

### 4.1 ModelContextLimits schema

冻结：

~~~python
class ModelContextLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_context_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    default_output_tokens: int
    input_safety_margin_tokens: int
~~~

字段语义冻结为：

| 字段 | 语义 |
|---|---|
| total_context_tokens | provider 声明的一次请求 input + output 组合上限 |
| max_input_tokens | provider 对 input 本身的硬上限；尚未扣除 Pulsara safety margin |
| max_output_tokens | provider 允许 Pulsara 请求的最大输出上限 |
| default_output_tokens | caller 未指定 max_output_tokens 时的产品默认请求值 |
| input_safety_margin_tokens | Pulsara 为 estimator 误差和 wire framing 保留的输入 headroom |

max_input_tokens 不是 total_context_tokens 的别名。即使 provider 允许很大的组合窗口，也可能单独限制输入。

若某 transport 无法显式发送 resolved output cap，V1 视为 unsupported transport configuration，不能依赖 provider server-side default。

### 4.2 Limits validators

必须满足：

- total_context_tokens >= 2；
- max_input_tokens >= 1；
- max_output_tokens >= 1；
- default_output_tokens >= 1；
- input_safety_margin_tokens >= 0；
- max_input_tokens <= total_context_tokens；
- max_output_tokens <= total_context_tokens；
- default_output_tokens <= max_output_tokens；
- min(max_input_tokens, total_context_tokens - default_output_tokens) - safety >= 1。

对 requested output 的最终可用 input 在 target resolution 时再次验证。

### 4.3 ModelSlotConfig

LLMConfig 不再用 pro_model/flash_model 两个裸字符串作为完整槽位。

冻结：

~~~python
@dataclass(frozen=True, slots=True)
class ModelSlotConfig:
    model_id: str
    limits: ModelContextLimits
~~~

LLMConfig：

~~~python
@dataclass(frozen=True, slots=True)
class LLMConfig:
    api_key: str
    base_url: str
    pro: ModelSlotConfig
    flash: ModelSlotConfig
    api: str
    provider: str
    provider_profile: ProviderProfile | None
    retry: LLMRetryConfig
    openai_sdk_max_retries: int | None
~~~

删除生产字段 pro_model / flash_model。

如 CLI/README 仍需展示 model name，从 slot.model_id 派生，不保留第二份。

### 4.4 环境变量

每个槽位 required：

~~~dotenv
PULSARA_PRO_MODEL=...
PULSARA_PRO_TOTAL_CONTEXT_TOKENS=...
PULSARA_PRO_MAX_INPUT_TOKENS=...
PULSARA_PRO_MAX_OUTPUT_TOKENS=...
PULSARA_PRO_DEFAULT_OUTPUT_TOKENS=...
PULSARA_PRO_INPUT_SAFETY_MARGIN_TOKENS=...

PULSARA_FLASH_MODEL=...
PULSARA_FLASH_TOTAL_CONTEXT_TOKENS=...
PULSARA_FLASH_MAX_INPUT_TOKENS=...
PULSARA_FLASH_MAX_OUTPUT_TOKENS=...
PULSARA_FLASH_DEFAULT_OUTPUT_TOKENS=...
PULSARA_FLASH_INPUT_SAFETY_MARGIN_TOKENS=...

# optional; defaults to accept_reported
PULSARA_MODEL_IDENTITY_POLICY=accept_reported  # or exact
~~~

任何字段缺失都在 PulsaraSettings.from_env() 阶段失败。

不根据 provider/model name 自动填值。未来可以新增显式 model catalog，但 catalog resolution 必须在 ModelSlotConfig 构造前完成，最终仍形成同一 required DTO。

### 4.5 Provider payload ownership guard

V1 使用 API-specific extension allowlist，而不是不断扩张的 reserved-key blacklist：

~~~python
PROVIDER_EXTENSION_ALLOWLIST_BY_API = {
    "openai_responses": {
        "request_defaults": {"service_tier"},
        "request_extra_body": {"thinking"},
    },
    "openai_chat_completions": {
        "request_defaults": {"service_tier"},
        "request_extra_body": {"thinking"},
    },
}
~~~

未出现在对应 API/source allowlist 的顶层字段一律拒绝。因此 functions/function_call/web_search_options、conversation/previous_response_id/prompt、truncation/context_management，以及 model、input/messages/instructions、tools、reasoning、output caps 等都不能从 provider extension 注入。

配置 validator 必须检查：

- request_defaults 顶层字段必须以精确 canonical spelling 属于该 API/source allowlist；
- request_extra_body 顶层字段必须以精确 canonical spelling 属于该 API/source allowlist；
- omit_params_when_thinking；
- adapter-specific wire-contract identity。

不对 extension key 做 lowercase、`-`/空格转 `_` 等仅用于比较的归一化；例如 `service-tier` 不能借 `service_tier` 的 allowlist 通过。若未来需要支持别名，必须在 ProviderProfile freeze 前把别名改写为 canonical wire key，使 validator、fingerprint 和 adapter 消费同一个改写后对象。

API identity 以最终 runtime `api`/wire contract 为唯一真源。未知 API 默认没有 extension allowlist，不能借传入 ProviderProfile 的旧 `wire_api` 从已知 API policy 放行字段。LLMConfig 构造时先把 ProviderProfile canonicalize 到 runtime API，再用同一 identity 做 allowlist、target fingerprint 与 adapter binding。

发现非 allowlisted、非 canonical extension 时在 target/config resolution 前失败。

V1 不尝试合并冲突来源，也不允许 provider defaults 覆盖 resolved call、compiled context、tools 或 budget。service_tier 等非冲突 extension 可保留。DeepSeek 风格的 `thinking: {"type": "enabled"}` 可作为 fingerprinted provider request shape 保留，但不能覆盖 Pulsara-owned reasoning/output 字段。

### 4.6 ModelProfile hard cut

ModelProfile 删除：

- context_window；
- max_output_tokens。

它只保存 runtime model/provider capability identity。

limits 只存在于 ModelSlotConfig / ResolvedModelTarget，不在 ModelProfile 再冗余。

### 4.7 Redacted settings

PulsaraSettings.redacted_dict() 应展示：

- pro/flash model id；
- 每个 slot 的 limits；
- API/provider；
- api_key_set。

不得展示 API key、完整 query/userinfo 或 secret-bearing request defaults。

## 5. Event-safe facts

### 5.1 ResolvedModelOptionsFact

~~~python
class ResolvedModelOptionsFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: str | None
    options_fingerprint: str
~~~

options_fingerprint 对不含 secret 的 canonical payload 计算 SHA-256。

effective options 表示 transport 实际会发送的 options。

V1 不允许 silent omission：

- caller 没有请求 reasoning_effort：effective value 为 None，正常；
- caller 显式请求 reasoning_effort，provider/thinking policy 允许发送：原值进入 effective options；
- caller 显式请求 reasoning_effort，但 provider/thinking policy 不允许发送：resolve_target 立即抛 ModelOptionUnsupported，不创建 target；
- output cap 不进入 options fact，而进入 ResolvedModelContextBudgetFact；adapter 必须显式发送；
- temperature 与 reasoning_summary 不存在于生产 options contract，payload builder 也不得发送。

合法 ResolvedModelOptionsFact 只包含会真实发送的 options；被 provider 拒绝的 requested option 不会形成 target fact。

### 5.2 TokenEstimatorFact

~~~python
class TokenEstimatorFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    estimator_id: str
    estimator_version: str
    estimator_fingerprint: str
~~~

V1 固定实现：

~~~text
estimator_id = pulsara_heuristic
estimator_version = v1
~~~

未来切 provider tokenizer 必须改变 version/fingerprint。

### 5.3 ResolvedModelContextBudgetFact

~~~python
class ResolvedModelContextBudgetFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effective_output_tokens: int
    pre_margin_input_tokens: int
    safety_margin_tokens: int
    input_budget_tokens: int
~~~

Pydantic validator 必须重新按 limits 计算并比对，不能信 caller 自报。

额外强 invariant：

~~~text
context_budget.effective_output_tokens == limits.default_output_tokens
~~~

resolver 写对不够；event/replay schema 本身必须拒绝重新计算 fingerprint 后仍偏离 slot default 的 durable fact。

### 5.4 ResolvedModelTargetFact

~~~python
class ResolvedModelTargetFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["resolved-model-target:v2"]
    target_fingerprint: str
    model_id: str
    model_role: Literal["pro", "flash"]
    provider: str
    api: str
    endpoint_origin: str
    endpoint_fingerprint: str
    provider_profile_id: str
    provider_request_shape_fingerprint: str
    transport_binding_id: str
    transport_contract_version: str
    model_identity_policy: Literal["accept_reported", "exact"]
    supports_tools: bool
    supports_reasoning: bool
    limits: ModelContextLimits
    effective_options: ResolvedModelOptionsFact
    context_budget: ResolvedModelContextBudgetFact
    token_estimator: TokenEstimatorFact
~~~

`model_id` 是 Pulsara 发给 provider 的 requested route id；它可以是稳定模型名，也可以是
provider alias。provider 在 response/chunk 中返回的 model id 是独立的 execution observation，
不会回写或替换 target fact。

### 5.5 ResolvedModelCallFact

~~~python
class ResolvedModelCallFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["resolved-model-call:v1"]
    resolved_model_call_id: str
    purpose: Literal[
        "agent_model_loop",
        "context_compaction_summary",
        "memory_governance",
        "memory_reflection",
    ]
    context_mode: Literal["compiled", "direct"]
    target: ResolvedModelTargetFact
~~~

约束：

- agent_model_loop 必须 context_mode=compiled；
- 其他三类 V1 必须 context_mode=direct；
- call ID 格式 model_call:<uuid>；
- call ID 不进入 target fingerprint；
- 同一 target 的两次 resolve_call 得到不同 call ID；
- 同一 call 的所有 event 复用完全相同 target fact。

### 5.6 ModelTokenUsageFact

冻结 provider execution 完成后的 event-safe usage DTO：

~~~python
class ModelTokenUsageFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int
    cached_input_tokens: int | None
    output_tokens: int
    reasoning_output_tokens: int | None
    total_tokens: int
~~~

usage normalization diagnostics 使用同一低层模块中的 bounded DTO：

~~~python
class ModelCallDiagnosticFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str = ""
    attributes: tuple[
        tuple[str, str | int | float | bool | None],
        ...
    ] = ()
~~~

attributes key 必须唯一并按 key 排序，float 必须 finite。tuple shape 保证真正不可变，并禁止任意 provider payload、headers、credentials 或 unbounded text。

V1 bounds：code <= 96 chars，message <= 512 chars，attributes <= 16 项，attribute string value <= 256 chars。

约束：

- 所有非空字段必须非负；
- cached_input_tokens 非空时不得大于 input_tokens；
- reasoning_output_tokens 非空时不得大于 output_tokens；
- normalized total_tokens 必须严格等于 input_tokens + output_tokens；
- provider 未返回 cached/reasoning breakdown 时保存 null，不得伪造为 0；
- cached_input_tokens 已包含在 input_tokens 中；
- reasoning_output_tokens 已包含在 output_tokens 中；
- non_cached_input_tokens 只能在 cached_input_tokens 已知时派生为 input_tokens - cached_input_tokens；
- usage 不进入 target/call fingerprint，因为它是调用结束后才产生的 execution fact；
- network retry 复用同一个 call，只能形成一份最终 canonical usage fact，不能按 retry attempt 重复计量。

该 DTO 为未来可选 rollout accounting 提供稳定输入，但本章不定义累计、提醒或终止策略。

### 5.7 Fingerprint canonicalization

统一 helper：

~~~python
canonical_json_bytes(value) -> bytes
sha256_fingerprint(namespace, value) -> str
~~~

要求：

- key 排序；
- UTF-8；
- stable separators；
- enum 转 value；
- tuple/list 统一 JSON array；
- 不接受 NaN/Infinity；
- 不包含 created_at 或 call ID；
- 不包含 API key；
- 不包含 full transport object。

target_fingerprint 覆盖 target fact 中除自身外的全部 contract fields。

ResolvedModelOptionsFact 和 ResolvedModelTargetFact 都要用 model_validator 重新计算各自 fingerprint。仅检查字段形状不足以成为 durable contract。

### 5.8 Endpoint 脱敏

endpoint_origin 只允许：

~~~text
scheme://host[:port]
~~~

删除：

- userinfo；
- query；
- fragment；
- path。

生产 LLMConfig.base_url 若带 userinfo、query 或 fragment，直接 configuration error；不能只在 audit fact 中删除后继续使用。

endpoint canonicalization 唯一算法：

1. 用 urllib.parse.urlsplit 解析；
2. scheme 只允许 http/https，并转小写；
3. hostname 做 IDNA ASCII normalization 后转小写；
4. http:80 与 https:443 删除默认端口，其他端口保留；
5. path 为空时规范化为 /；
6. path 必须以 / 开头；
7. 除根路径外删除末尾 /，因此 /v1/ 与 /v1 相同；
8. 拒绝原始或 percent-decoded 后包含 .、.. path segment 的 endpoint；
9. percent escape hex 统一大写，但不做会改变 path segment 语义的 decode/re-encode；
10. 不折叠非尾部的重复 /，因为部分 provider 会区分它。

endpoint_origin 由规范化 scheme/host/port 生成。endpoint_fingerprint 对规范化 scheme/host/port/path 计算；合法 path（例如 /v1）不以明文进入 event。

API key、headers、userinfo、query、fragment 永远不进入 endpoint fingerprint。

### 5.9 Provider request shape fingerprint

provider request defaults/extra body 可能包含敏感 value。

V1 冻结 recursive redact_provider_request_shape(value, context)：

key normalization：

~~~text
unicodedata.normalize("NFKC", key)
    -> strip
    -> lower
    -> replace "-" and space with "_"
~~~

敏感 exact key：

~~~text
authorization
proxy_authorization
api_key
apikey
x_api_key
access_token
refresh_token
auth_token
bearer_token
password
passwd
client_secret
secret
credential
credentials
cookie
set_cookie
session_token
~~~

敏感 suffix：

~~~text
_access_token
_refresh_token
_auth_token
_bearer_token
_password
_passwd
_client_secret
_credential
_credentials
~~~

递归规则：

- sensitive key 的 value 一律替换为固定字符串 <redacted:secret>，不保留长度/hash；
- headers/header map 对 header name 使用同一 normalization；authorization、proxy-authorization、x-api-key、cookie、set-cookie 值固定 redacted，其他 header value作为语义配置保留；
- cookies map 保留 cookie name，但所有 value固定 redacted；cookie/set_cookie scalar 或 header值整体 redacted；
- dict 按 normalized key排序，normalized key冲突直接 configuration error；
- list/tuple 保序递归；
- str/int/bool/null 原样进入 redacted shape；
- float 必须 finite；
- bytes、callable、任意 SDK object直接 configuration error；
- 不使用 entropy/value-content 猜测，避免 classifier 随内容漂移；
- budget key 仍在 redaction 前由独立 validator拒绝；
- event 只保存 fingerprint，不保存 redacted shape 或原始 request defaults。

credential 轮换不改变 target semantic fingerprint；非 secret request shape 变化必须改变 fingerprint。

provider_request_shape_fingerprint 还必须覆盖 thinking enabled/profile、omit_params_when_thinking 与 adapter request-default merge policy。即使当前 requested options 都为 None，policy变化仍改变 target fingerprint。

## 6. Runtime-only DTO 与 resolver API

### 6.1 ResolvedModelTarget

~~~python
@dataclass(frozen=True, slots=True)
class ResolvedModelTarget:
    model_profile: ModelProfile
    transport: LLMTransport
    effective_options: LLMOptions
    limits: ModelContextLimits
    context_budget: ResolvedModelContextBudgetFact
    token_estimator: TokenEstimator
    fact: ResolvedModelTargetFact
~~~

LLMTransport protocol 同时 required：

~~~python
binding_id: str
contract_version: str
~~~

OpenAI Responses、OpenAI Chat Completions、MockTransport 和每个 scripted transport 都必须显式声明。binding_id 表示具体 adapter implementation family，contract_version 表示 payload/event contract 版本；两者共同进入 target fact/fingerprint。

V1 production constants：

- OpenAI Responses：binding_id=pulsara.openai.responses，contract_version=v1；
- OpenAI Chat Completions：binding_id=pulsara.openai.chat_completions，contract_version=v1。

pytest mock/scripted transport 使用 test. 前缀，不允许 production registry 注册 test binding。

### 6.2 ResolvedModelCall

~~~python
@dataclass(frozen=True, slots=True)
class ResolvedModelCall:
    target: ResolvedModelTarget
    fact: ResolvedModelCallFact
~~~

resolved_model_call_id 从 fact 读取，不再另存第二份。

### 6.3 Resolver API

~~~python
class LLMRuntime:
    def resolve_target(
        self,
        *,
        role: ModelRole,
        requested_options: LLMOptions | None,
    ) -> ResolvedModelTarget: ...

    def resolve_call(
        self,
        *,
        target: ResolvedModelTarget,
        purpose: ModelCallPurpose,
    ) -> ResolvedModelCall: ...

    def rebind_target(
        self,
        fact: ResolvedModelTargetFact,
    ) -> ResolvedModelTarget: ...
~~~

### 6.4 resolve_target 精确顺序

~~~text
select ModelSlotConfig
    -> construct ModelProfile
    -> validate provider budget-key guards
    -> resolve effective options
    -> validate output range
    -> compute input budget
    -> select transport from registry
    -> verify transport binding_id/contract_version
    -> select estimator
    -> build redacted facts
    -> compute target fingerprint
    -> validate fact round-trip
    -> return runtime target
~~~

resolve effective options 的精确规则：

1. effective_output_tokens 只取 slot.default_output_tokens，并写入 context budget；
2. 只解析 reasoning_effort；
3. reasoning_effort 为 None 时正常保持 None；
4. reasoning_effort 非空但 provider 不允许发送时抛 ModelOptionUnsupported；
5. payload builder 不再执行第二轮 supports/omit 判断；
6. adapter 从 context budget 显式发送 output cap，无法发送即 contract error；
7. temperature 与 reasoning_summary 永不进入 target、fact 或 provider payload。

rebind_target() 只使用 fact 中真正发送过的 effective options重新 resolve。因为合法 fact 不含 omission state，相同配置必定可重建相同 options fingerprint。

### 6.5 rebind_target 恢复语义

rebind_target(fact) 用于 active/suspended run recovery：

1. 根据 fact.model_role 读取当前 slot；
2. 用 fact.effective_options 重新 resolve；
3. 比较完整 target_fingerprint；
4. fingerprint 一致才返回 runtime target；
5. 不一致抛 ModelTargetBindingMismatch。

不得只比较 model_id。

以下任一变化都应 fail closed：

- model id；
- provider/API；
- transport binding id/version；
- endpoint identity；
- limits；
- effective output；
- estimator version；
- provider request shape；
- supports_tools/reasoning。

### 6.6 Target 与 Call 的不可变性

runtime DTO 和 event fact 都必须：

- frozen；
- 对嵌套 dict 做 defensive copy/freeze；
- 不允许 caller 原地修改 provider profile；
- transport payload builder 不修改 effective_options；
- tests 覆盖嵌套 mutation。

## 7. Run 创建、preflight 与 resume

### 7.1 HostSession run entry

run_turn/stream_turn 顺序改为：

~~~text
sync MCP
    -> target = agent_runtime.resolve_run_model_target()
    -> prepare prior messages / preflight compaction(target)
    -> create LoopState
    -> agent_runtime.run_task(..., run_model_target=target)
~~~

当前先 prepare prior messages、后 begin state 的大顺序保留，但 target 必须在 prepare 前解析。

### 7.2 AgentRuntime API

production run entry 要求显式 target：

~~~python
run_task(..., run_model_target: ResolvedModelTarget)
stream_task(..., run_model_target: ResolvedModelTarget)
~~~

不得把 target 参数做成 “缺失时生产 fallback resolve”。

测试 helper 可以提供 resolve_test_target()，但最终仍调用同一 required production API。

### 7.3 RunStartEvent

RunStartEvent 增加：

~~~python
model_target: ResolvedModelTargetFact
~~~

required，且 permission fields 仍 required。

新的 RunStartEvent 没有 model_target 是非法 schema。

### 7.4 LoopState ownership

LoopState 不应把完整 target 放入 scratchpad。

可新增 typed runtime-only field：

~~~python
run_model_target: ResolvedModelTarget | None
~~~

新 run 创建后必须非空。

持久恢复时从 RunStartEvent rebind，再写入该 typed field。

### 7.5 Resume

approval、MCP input-required、plan question resume：

- 继续原 run；
- 读取原 RunStartEvent.model_target；
- rebind fingerprint；
- 不读当前 session next-run config 作为替代；
- 新的 follow-up model step 创建新 call；
- 原 run target 不变。

### 7.6 Subagent

child 复用 AgentRuntime：

- child RunStartEvent 同样带 target fact；
- child profile 决定 tool/capability，不另造 model limits；
- V1 若 child 与 parent 使用同一 LLMConfig，可有相同 target fingerprint；
- child 的 call ID 永远独立；
- parent context compile 与 child context compile 不共享 call。

## 8. Call lifecycle 与 retry 状态机

### 8.1 Agent model loop

每个 model_call_index 的顺序冻结为：

~~~text
call = resolve_call(run_target)
compile attempt 1(call)
    -> pressure
    -> optional mid-turn compaction(run_target, summarizer_call)
compile attempt 2(call)
    -> compiled
pre-send validate(call, final context)
    -> ModelCallStart(call)
    -> transport network attempt(same call)
    -> semantic events
    -> ModelCallEnd(call)
~~~

### 8.2 Context retry identity

compile_attempt_index 和 context_retry_index 可以递增。

context_id 每次 compile attempt 可以不同。

以下必须保持不变：

- model_call_index；
- resolved_model_call_id；
- target_fingerprint；
- effective options；
- budget；
- estimator。

### 8.3 Tool follow-up

当前 model response 结束并执行工具后：

- model_call_index 递增；
- 创建新 ResolvedModelCall；
- call ID 变化；
- run target 不变。

计量语义同时冻结：

- 每次真实 follow-up model execution 都是新的 accounting unit；
- 一次 response 内并行执行多个 tool call 不会伪造成多个 model call usage；
- terminal/MCP/background process 的持续运行、yield、poll 或 wait 是 tool lifecycle，不自动产生 model usage；
- network retry 继续属于原 call，不重复累计 usage；
- context compile retry 和 mid-turn compact retry 继续复用原 call，不产生新的 usage unit；
- 本章不根据 model_call_index、tool-call count 或累计 usage 阻止 follow-up；未来 execution budget policy 必须在 coordinator 层消费 committed ModelCallEnd usage facts后独立决策。

### 8.4 Network retry

transport 内部 retry：

- 复用相同 call；
- 复用相同 provider payload；
- 只写一个 ModelCallStartEvent；
- retry diagnostic 带 resolved_model_call_id；
- 不改变 model/context/options；
- semantic output 开始后仍按当前规则禁止 retry。

### 8.5 Model alias、reported identity 与内部 fallback

V1 transport 内部自行修改 `call.target`、请求 payload model 或编译后模型合约，仍然是
contract violation；必须回到 coordinator 解析新 target/call 并重新 compile。

但 generic OpenAI-compatible adapter 不能把 `requested_model_id != reported_model_id`
直接解释为 fallback：provider 将 alias 解析成具体 snapshot 是常见且合法的行为。

V1 只保留两个 identity policy：

- `accept_reported`：默认。接受 provider 报告的任意非空 model id，并作为观察事实持久化；
- `exact`：显式 opt-in。reported id 必须与 requested id 精确相等，否则
  `transport_changed_model_target`。

不实现 allowlist、regex 或 prefix 猜测。

response identity 规则：

- provider response/chunk 报告非空 model id 时，trim 后写入本次成功 attempt 的
  `reported_model_id`；
- 同一个 attempt 内多次报告 model id 时必须彼此相等；
- `accept_reported` 下 requested/reported 不同是合法 alias observation；
- `exact` 下 mismatch 立即产生 `transport_changed_model_target`；
- provider response 完全没有 model 字段时允许继续，但只表示“provider未报告 identity”，不得解释为 provider 已确认 target model；
- missing reported model 不改变 durable target fact。
- network retry 放弃的 pre-semantic-output attempt 不贡献最终 reported identity；最终只记录成功
  attempt，或不可重试 terminal provider-error attempt 的 observation。

未来 fallback 必须：

~~~text
return coordinator
    -> resolve new target
    -> resolve new call
    -> recompile
    -> new ModelCallStart
~~~

## 9. LLMRuntime / Transport API hard cut

### 9.1 新 stream API

~~~python
class LLMRuntime:
    def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]: ...
~~~

删除 role/options 参数。

### 9.2 新 transport API

~~~python
class LLMTransport(Protocol):
    def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent | TransportUsageReport]: ...
~~~

transport 从 call.target 读取 model/options。

TransportUsageReport 是 runtime-only、非 durable 的 adapter return item：

~~~python
@dataclass(frozen=True, slots=True)
class TransportUsageReport:
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    provider_diagnostics: tuple[ModelCallDiagnosticFact, ...] = ()
~~~

status/usage 必须满足与 ModelCallEndEvent 相同的条件 invariant。一个 transport execution 最多返回一份 usage report。完全不返回表示 usage missing，不得合成全零 usage。

### 9.3 LLMContext identity

LLMContext 新增 required：

~~~python
context_id: str
resolved_model_call_id: str
target_fingerprint: str
model_call_index: int | None
~~~

direct side call 也必须生成 context_id。

LLMRuntime 在发送前验证：

- context.resolved_model_call_id == call.fact.resolved_model_call_id；
- context.target_fingerprint == call.target.fact.target_fingerprint；
- compiled call 的 model_call_index 非空；
- direct call 的 model_call_index 可以为空。

### 9.4 Reply / Model call lifecycle ownership

LLMRuntime 统一构造并 yield：

- ReplyStartEvent；
- ModelCallStartEvent；
- ModelCallEndEvent；
- ReplyEndEvent。

transport 不再构造或返回上述四种 lifecycle event。它只产出 provider semantic events、可收口 RunErrorEvent 和最多一份 TransportUsageReport。

这里的 owner 指 event construction/stream ownership，不表示 LLMRuntime 直接持有 EventLog。主 AgentRuntime 消费 stream 后通过 RuntimeSession 持久化；side engine 消费后由自身 outer terminal fact保存必要 call/usage审计。

事件顺序：

~~~text
pre-send validation
    -> ReplyStartEvent
    -> ModelCallStartEvent
    -> transport semantic/retry events
    -> optional TransportUsageReport
    -> LLMRuntime ModelCallEndEvent
    -> ReplyEndEvent
~~~

pre-send validation 失败时：

- 不写 ReplyStart；
- 不写 ModelCallStart；
- 不调用 transport。

ModelCallStartEvent 的精确定义是：

> Pulsara 已通过 final validation，并把该 resolved call 交给 transport 开始一次 provider execution。

它不声称 provider 已返回 acknowledgement。event consumer 可能在 start 持久化后、transport 产生首个 event 前取消；合法 ledger 因而可以有 start 而没有 end。inspector 必须投影为 started_missing_end，不能伪造 end。

### 9.5 ModelCallEnd ownership

LLMRuntime 同时是 ModelCallEndEvent 的唯一 construction owner。

transport：

- 不写 ModelCallEndEvent；
- 只返回可选 TransportUsageReport；
- 不计算 estimated_input_tokens。

PR1 起，LLMRuntime 必须先调用 validate_model_context_for_call()；validator 内部只调用一次 estimate_model_context_for_call()，并把同一个 TokenEstimate 放入 ModelContextValidationResult。ModelCallEndEvent.estimated_input_tokens 始终来自该 validation result。PR3 只增加 compiler final estimate 的跨层一致性校验，不改变 estimate 来源或 ModelCallStartEvent 的既有语义。transport、event builder 和 caller 都不得重算。

LLMRuntime 在 transport 正常返回或可收口 provider error 时构造并 yield 一个 end event。

LLMRuntime 看到 transport RunErrorEvent 时先 yield 该 error、标记 outcome=provider_error，并继续 drain transport iterator；transport 返回后再 yield ModelCallEndEvent。它不能在 RunErrorEvent 处提前 return。

若 call 被 task cancellation、process crash 或不可恢复 stream interruption 中断，ledger 可以只有 start 没有 end；Inspector 按 started_missing_end 诊断。不得在 recovery 时伪造 token usage。

### 9.6 Transport event guard

LLMRuntime 遇到 transport 返回 ReplyStartEvent、ModelCallStartEvent、ModelCallEndEvent 或 ReplyEndEvent：

- 不 publish；
- 抛 LLMTransportContractError；
- 按实际类型记录 stable diagnostic：transport_emitted_reply_start、transport_emitted_model_call_start、transport_emitted_model_call_end 或 transport_emitted_reply_end。

transport 返回第二份 TransportUsageReport 是 contract error。

transport 正常耗尽但没有 usage report时，LLMRuntime 构造 usage_status=missing 的 end event。

### 9.7 AgentEventBuilder

AgentEventBuilder 改为接收 ResolvedModelCall。

删除 model_start() 与 model_end()。

若保留 reply_start()/reply_end() helper，它们只能由 LLMRuntime 调用；transport translator 与 scripted transport 不得持有或调用这两个 lifecycle builder。

OpenAI translator 原先把 provider usage 转成 ModelCallEndEvent 的路径，改为产出 TransportUsageReport。

### 9.8 Payload builders

build_responses_payload / build_chat_completions_payload 改为：

~~~python
build_payload(call=call, context=context)
~~~

不再接受独立 model/options。

payload 中 model 从 target identity 派生，output cap 从 context budget 派生，reasoning_effort 从 effective options 派生；temperature 与 reasoning_summary 永不发送。

### 9.9 Scripted transports

所有 tests 中 scripted transport：

- 改签名为 call/context/event_context；
- 不再手写 ReplyStartEvent / ModelCallStartEvent / ModelCallEndEvent / ReplyEndEvent；
- 如需模拟 mismatch，通过 runtime contract test fixture 显式注入非法 event；
- 正常 scripted transport 只输出 body 和可选 TransportUsageReport。

## 10. Token estimator 单一真源

### 10.1 Protocol

~~~python
class TokenEstimator(Protocol):
    fact: TokenEstimatorFact

    def estimate_text(self, text: str) -> int: ...
    def estimate_json(self, value: object) -> int: ...
    def estimate_tool_spec(self, tool: ToolSpec) -> int: ...
    def estimate_context(self, context: LLMContext) -> TokenEstimate: ...
~~~

### 10.2 V1 estimator

PulsaraHeuristicTokenEstimatorV1 的算法冻结为：

~~~python
TEXT_CHARS_PER_TOKEN = 4
JSON_CHARS_PER_TOKEN = 2
REQUEST_ENVELOPE_TOKENS = 3
SYSTEM_MESSAGE_FRAMING_TOKENS = 4
MESSAGE_FRAMING_TOKENS = 4
TOOL_CALL_FRAMING_TOKENS = 4
TOOL_SPEC_FRAMING_TOKENS = 8

def ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor

def estimate_text(text: str) -> int:
    return 0 if text == "" else ceil_div(len(text), 4)

def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

def estimate_json(value: object) -> int:
    rendered = canonical_json(value)
    return 0 if rendered == "" else ceil_div(len(rendered), 2)
~~~

len() 按 Python Unicode code point 计数，不按 UTF-8 byte。

estimate_context() 精确规则：

- envelope_tokens 固定为 REQUEST_ENVELOPE_TOKENS；
- 非空 system prompt = SYSTEM_MESSAGE_FRAMING_TOKENS + estimate_text(system_prompt)；
- 每个 LLMMessage 先加 MESSAGE_FRAMING_TOKENS；
- message.content / thinking 中每个字符串分别 estimate_text 后求和；
- tool_call 每项再加 TOOL_CALL_FRAMING_TOKENS，并计入 id、name、arguments 原始字符串的 estimate_text；
- tool_result 的 tool_call_id/name 作为普通字符串计入；
- 每个 ToolSpec = TOOL_SPEC_FRAMING_TOKENS + estimate_json({"name": ..., "description": ..., "parameters": ...})；
- 空 messages/tools 不产生对应 per-item framing；
- 不支持的非 JSON value 直接 contract error，不使用 default=str。

TokenEstimatorFact.estimator_fingerprint 对以下 canonical payload 计算：

- estimator_id/version；
- 上述全部常量；
- Unicode code-point counting policy；
- canonical JSON serialization contract；
- LLMMessage/ToolSpec field inclusion policy；
- message_tokens_by_index 的 per-message framing归属与聚合规则。

任一常量或字段纳入规则变化都必须 bump estimator_version/fingerprint。

不得继续在：

- compiler；
- tool result renderer；
- compaction；
- governance；
- reflection

各自定义 chars-per-token 真源。

build_compiled_context() 在调用 render_segmented_llm_messages() 前已经持有 resolved call，因此必须显式传：

~~~python
render_segmented_llm_messages(
    ...,
    token_estimator=resolved_call.target.token_estimator,
)
~~~

tool_results.py 删除 _estimate_tokens_from_chars() 作为 model-token 真源。字符 hard cap 可以继续用于 JSON/envelope 完整性与 artifact preview 边界，但所有 token report/decision 都调用传入 estimator。

ToolResultRenderDecisionCache 的 key/fingerprint 必须包含 estimator_fingerprint；不同 estimator 下不得复用旧 rendered token accounting。

### 10.3 TokenEstimate

~~~python
class TokenEstimate:
    system_tokens: int
    message_tokens: int
    message_tokens_by_index: tuple[int, ...]
    tool_tokens: int
    envelope_tokens: int
    total_input_tokens: int
~~~

invariants：

- len(message_tokens_by_index) 必须等于 len(context.messages)；
- message_tokens 必须等于 sum(message_tokens_by_index)；
- message_tokens_by_index[i] 必须包含第 i 条 message 的 MESSAGE_FRAMING_TOKENS、普通字段、tool call 与 tool result 字段的全部估算；
- system_tokens、tool_tokens、envelope_tokens 不进入 message_tokens_by_index；
- total_input_tokens 必须等于 system_tokens + message_tokens + tool_tokens + envelope_tokens。

message-level breakdown 由 estimator 与聚合总量在同一次 estimate_context() 调用中产生。compiler、compaction 与 Inspector 不得复制 message token 算法来反推 breakdown。

### 10.4 Compiler allocation estimate 与 final estimate

section estimates 用于降级选择。

最终预算真值是 lowering 后：

~~~python
final_estimate = call.target.token_estimator.estimate_context(llm_context)
~~~

lowering 必须与 llm_context.messages 同步产出：

~~~python
message_budget_scopes: tuple[
    Literal["transcript", "non_transcript"],
    ...,
]
~~~

len(message_budget_scopes) 必须等于 len(llm_context.messages)。每条 message 的 framing 和内容 token 全部跟随该 message 的 scope，不允许把同一 message 的 framing 拆到另一类。

scope 冻结为：

- transcript：history、current_user、current_run_tail、tool-result 与 provider-native assistant tool-call transcript units；
- non_transcript：runtime、memory、capability、subagent handoff、recovery/control 等由 compiler 注入的非对话 sections；
- system prompt、tool specs 与 request envelope 不属于任何 message scope，始终计入 non-transcript baseline。

由同一 final_estimate 直接派生：

~~~text
transcript_estimated_tokens
    = sum(message_tokens_by_index[i]
          for i where message_budget_scopes[i] == "transcript")

non_transcript_baseline_tokens
    = system_tokens
    + tool_tokens
    + envelope_tokens
    + sum(message_tokens_by_index[i]
          for i where message_budget_scopes[i] == "non_transcript")

final_payload_estimated_tokens
    = non_transcript_baseline_tokens
    + transcript_estimated_tokens
    = final_estimate.total_input_tokens
~~~

ContextBudgetReport 保留：

- section allocation breakdown；
- final payload estimate；
- delta；
- estimator fact。

若 breakdown 与 final estimate 不同：

- 允许 bounded diagnostic；
- budget 判定永远用 final estimate；
- 不得选更小的那个。

### 10.5 Provider framing

adapter 不得添加未被 estimator contract 覆盖的 model-visible正文。

允许的 wire-only framing：

- role labels；
- item wrappers；
- fixed protocol overhead。

V1 不增加 transport-specific framing fact。第 10.2 节固定的 REQUEST/SYSTEM/MESSAGE/TOOL_CALL/TOOL_SPEC 常量是所有 V1 支持 adapter 共用的 conservative upper bound。

若某 adapter 无法满足该 upper bound：

- 不能局部增加隐藏 overhead；
- 必须 bump PulsaraHeuristicTokenEstimator version/constants；
- 所有 target fingerprint随之变化；
- recovery 对旧 run fail closed。

### 10.6 Usage 回看

ModelCallEndEvent 必须记录：

- usage_status；
- estimated_input_tokens；
- usage：usage_status=reported 时必须是 provider-normalized ModelTokenUsageFact；usage_status=missing 时必须为空。

cached/reasoning breakdown 缺失时保留 null，不得按 0 推断。

estimate_error_tokens 与 estimate_error_ratio 由 Inspector 使用 estimated_input_tokens 和 reported usage.input_tokens 派生，不进入 ModelCallEndEvent，避免第三份可漂移事实。usage_status=missing 时两者为 null。

该 usage 可用于估算误差校准和未来 rollout accounting，但不得在同一 call 后反向修改已发生的 context budget 事实，也不得由本章直接触发 follow-up stop policy。

## 11. ContextCompiler hard cut

### 11.1 ContextCompileRequest

删除 model_role 字段，新增：

~~~python
resolved_call: ResolvedModelCall
~~~

role、limits、options、estimator 都从 resolved_call 读取。

### 11.2 ContextBudgetReport

改为：

~~~python
class ContextBudgetReport:
    target_fingerprint: str
    resolved_model_call_id: str
    measurement_stage: Literal[
        "tool_result_render",
        "section_allocation",
        "final_payload",
    ]
    total_context_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    effective_output_tokens: int
    safety_margin_tokens: int
    input_budget_tokens: int
    sections_estimated_tokens: int | None
    tools_estimated_tokens: int | None
    envelope_estimated_tokens: int | None
    allocation_estimated_tokens: int | None
    final_payload_estimated_tokens: int | None
    non_transcript_baseline_tokens: int | None
    transcript_estimated_tokens: int | None
    estimator: TokenEstimatorFact
~~~

删除模糊命名 reserved_output_tokens。

stage validators：

- tool_result_render：七个 measurement 字段全部为 None；具体 renderer pressure 只存在 tool_result_render_decisions / tool_result_budget_report 中；
- section_allocation：sections/tools/allocation 必填且非负，envelope/final/non-transcript/transcript 必须为 None；
- final_payload：七个 measurement 字段全部必填且非负；
- allocation_estimated_tokens 必须等于 sections_estimated_tokens + tools_estimated_tokens；
- final_payload_estimated_tokens 必须等于 non_transcript_baseline_tokens + transcript_estimated_tokens；
- ContextCompiledEvent.status=compiled 必须 measurement_stage=final_payload；
- pressure/failed 可以停在任一 stage，但不得用 0 表示“尚未计算”。

final payload 分类：

- transcript_estimated_tokens：history/current_user/current_run_tail/tool-result/provider-native transcript units；
- non_transcript_baseline_tokens：system prompt、runtime/memory/capability/subagent-handoff sections、tool specs 与 fixed envelope；
- 同一 token unit 只能归入一类；由 compiler lowering 的 message_budget_scopes 与 estimator 的 message_tokens_by_index 联合记录，不允许 compaction 事后按字符串猜或再次运行一套估算逻辑。

primitives/model_call.py 中的 ContextBudgetReportEvent 使用同一组字段和 validator，但采用 frozen Pydantic DTO；runtime ContextBudgetReport.to_event_value() 必须只做一对一转换，不能重新估算或补默认值。

### 11.3 Budget sequence

~~~text
render tool results with resolved estimator
    -> renderer pressure may exit with measurement_stage=tool_result_render
    -> collect source sections
    -> lifecycle
    -> timing overlay
    -> estimate sections/tools with call estimator
    -> allocation pressure may exit with measurement_stage=section_allocation
    -> degrade/omit
    -> lower to LLMContext
    -> estimate final LLMContext
    -> if over budget: ContextBudgetExceeded
    -> return CompiledContext
~~~

ContextBudgetExceeded 在任何 stage 都必须携带一个 stage-valid report。早期 renderer pressure 不得填造假的 sections/final 零值。

### 11.4 总量 fail closed

若 final_payload_estimated_tokens > input_budget_tokens：

- ContextBudgetExceeded；
- 不返回 CompiledContext；
- 不写 status=compiled；
- AgentRuntime 可尝试一次 mid-turn compaction；
- retry 后仍超预算则 status=failed + RunError。

删除 “warning 后继续发送” 语义。

### 11.5 Current user 专项错误

current user 单独超过预算时仍保留更具体 diagnostic：

~~~text
current_user_exceeds_model_input_budget
~~~

但它只是总量 fail-closed 的具体分类，不再是唯一会抛异常的条件。

### 11.6 ContextBudgetExceeded payload

异常必须携带：

- context_id；
- resolved_model_call_id；
- target_fingerprint；
- model_call_index；
- compile attempt；
- budget report；
- diagnostics；
- tool result render decisions/report。

AgentRuntime 不得再用常量补 event fields。

### 11.7 CompiledContext

新增：

- resolved_model_call fact；
- final TokenEstimate；
- message_budget_scopes，与 llm_context.messages 等长。

CompiledContext.llm_context 的 call ID/fingerprint 必须相同。CompiledContext 构造时使用 final TokenEstimate.message_tokens_by_index + message_budget_scopes 一次性生成 final budget report；后续 event builder、compaction 或 Inspector 不得重新分类。

### 11.8 Tool schemas

如果 context.tools 非空但 target.supports_tools=False：

- compile fail closed；
- diagnostic=model_target_does_not_support_tools；
- transport 不静默丢 tools。

这也保证 compiler estimate 与 payload 一致。

## 12. Typed event contract hard cut

### 12.1 RunStartEvent

required model_target。

### 12.2 ModelCallStartEvent

替换旧 model_name/model_role/provider 冗余字段：

~~~python
class ModelCallStartEvent(EventBase):
    resolved_call: ResolvedModelCallFact
    context_id: str
    model_call_index: int | None
~~~

展示字段全部由 resolved_call.target 派生。

### 12.3 ModelCallEndEvent

~~~python
class ModelCallEndEvent(EventBase):
    resolved_model_call_id: str
    target_fingerprint: str
    reported_model_id: str | None  # required nullable field
    outcome: Literal["completed", "provider_error"]
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    estimated_input_tokens: int
    diagnostics: tuple[ModelCallDiagnosticFact, ...]
~~~

validator：

- usage_status=reported 时 usage 必填；
- usage_status=missing 时 usage 必须为空；
- usage 通过 ModelTokenUsageFact 的非负、cached/reasoning breakdown 和 total consistency 校验；
- estimated_input_tokens 从 PR1 起必须来自包含同一 TokenEstimate 的 pre-send validation result；validator 内部调用 estimate-only seam，transport 不得提供或重算；
- ID/fingerprint 与 start 一致由 reducer/inspector做 cross-event assertion。
- reported_model_id 是 provider observation；requested model id 与 identity policy 从 joined
  ResolvedModelCallFact.target 读取。

provider 未返回 usage 时写 missing/null，绝不写 0/0/0。

provider 若只返回 total 而缺 input/output，无法形成 normalized fact，按 missing 处理并附 bounded provider_usage_incomplete diagnostic。

provider raw total 缺失时，以 input + output 形成 normalized total；raw total 非空但不等于该和时，同样使用 normalized 和，并附 provider_usage_total_mismatch diagnostic。raw provider usage 不进入 durable event。

### 12.4 ContextCompiledEvent

替换以下旧 top-level 真源：

- model_role；
- context_window_tokens；
- reserved_output_tokens；
- estimated_tokens。

新 schema：

~~~python
class ContextCompiledEvent(EventBase):
    status: Literal["compiled", "pressure", "failed"]
    context_id: str
    model_call_index: int
    compile_attempt_index: int
    context_retry_index: int
    resolved_call: ResolvedModelCallFact
    budget: ContextBudgetReportEvent
    sections: list[...]
    tool_specs: list[...]
    diagnostics: list[...]
    lifecycle_decisions: list[...]
    tool_result_render_decisions: list[...]
    tool_result_budget_report: dict
~~~

pressure/failed 也必须带真实 resolved call 与 budget。

ContextBudgetReportEvent 固定定义在 primitives/model_call.py，不复用 runtime/context_engine/types.py 中的 runtime dataclass。compiler 必须显式转换，避免 event schema 反向依赖 runtime。

### 12.5 ModelCallRejectedEvent

新增 typed event：

~~~python
class ModelCallRejectedEvent(EventBase):
    resolved_call: ResolvedModelCallFact
    context_id: str
    model_call_index: int
    reason_code: Literal[
        "model_input_budget_exceeded",
        "model_input_estimate_mismatch",
        "model_context_identity_mismatch",
        "model_target_capability_mismatch",
        "model_target_binding_mismatch",
    ]
    estimated_input_tokens: int | None
    input_budget_tokens: int
    diagnostics: tuple[ModelCallDiagnosticFact, ...]
~~~

它表示 call 已 resolve，但 provider request 未开始。

owner 与适用范围冻结：

- LLMRuntime 只执行 validation 并抛结构化 ModelInputBudgetExceeded / ModelContextIdentityMismatch；
- LLMRuntime 不 append、不 publish ModelCallRejectedEvent；
- AgentRuntime 捕获 compiled call 的结构化 rejection 后，使用 RuntimeSession 写该 event；
- event validator required resolved_call.context_mode=compiled；
- direct compaction/reflection/governance 不写 ModelCallRejectedEvent。

direct rejection 的 durable 表达：

- compaction：ContextCompactionFailedEvent；
- reflection：MemoryReflectionFailedEvent；
- governance：MemoryGovernanceRunResult，本章暂无 durable session event。

### 12.6 Cross-event invariants

同一 resolved_model_call_id：

- ContextCompiledEvent.resolved_call 完全相等；
- ModelCallStartEvent.resolved_call 完全相等；
- ModelCallEnd target fingerprint 相等；
- ModelCallRejected 与 ModelCallStart 互斥；
- compiled mode call 必须至少有一个 compiled/pressure/failed context event；
- direct mode call 不要求 ContextCompiledEvent。

### 12.7 Event schema hard cut

AgentEvent union、serialization mapping、event log contract 一次更新。

旧 payload 不做 optional fields 或 default。

Postgres JSONB 不需新增列；如 contract 版本有 schema version/checksum，必须 bump。

开发数据库需要 reset 或显式 migration。

## 13. Pre-send validation

### 13.1 PR1 estimator seam 与 standalone validator

PR1 新增唯一底层估算入口：

~~~python
def estimate_model_context_for_call(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> TokenEstimate: ...
~~~

它的职责只包括：

- 使用 call.target.token_estimator 估算最终 LLMContext；
- 返回带 message_tokens_by_index 的完整 TokenEstimate；
- 不检查 transport binding、context/call identity、tool capability 或预算上限；
- 不写 event，也不调用 transport。

该函数是 validator 内部 seam，不是 production caller 可以绕过 validator 直接使用的发送许可。它必须从 PR1 起返回带 message_tokens_by_index 的完整 TokenEstimate。

PR1 同时落地 validate_model_context_for_call()。LLMRuntime.stream() 必须在任何 ReplyStartEvent、ModelCallStartEvent 或 transport 调用前调用 validator，并把 result 保留到 ModelCallEndEvent；estimated_input_tokens=validation.estimate.total_input_tokens。估算或 validation 失败属于 pre-start contract failure，不允许写 lifecycle start、零/null estimate 或伪造 ModelCallEndEvent。

任何生产调用都只能估算一次。PR3 不删除或复制 estimator seam；它在 PR2 已提供 compiler final estimate 后，为 compiled call 启用 equality assertion，不接收 caller 传入的第二份“可信估算”。

### 13.2 Validation API

~~~python
def validate_model_context_for_call(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> ModelContextValidationResult: ...
~~~

ModelContextValidationResult 至少包含：

~~~python
estimate: TokenEstimate
~~~

若 identity、binding 或 capability 的结构校验在估算前失败，结构化 validation exception 可以没有 estimate；只要进入估算、budget 或 mismatch 校验，后续 terminal fact 必须复用已经产生的 estimate。

### 13.3 Validation 内容

必须验证：

- call ID；
- target fingerprint；
- context mode；
- context_id 非空；
- compiled call 的 model_call_index；
- tool capability；
- final token estimate；
- output cap 已解析；
- call.target.transport.binding_id == call.fact.target.transport_binding_id；
- call.target.transport.contract_version == call.fact.target.transport_contract_version；
- endpoint/transport binding fingerprint 未变化；
- payload builder 将使用的 effective options 与 fact 完全相等。

validation 顺序冻结为：

1. 校验 resolved call、context identity、transport binding 与 tool capability；
2. 调用 estimate_model_context_for_call() 一次；
3. 校验 input budget；
4. compiled call 校验 compiler final estimate 与 pre-send estimate 完全相等；该项在 PR3 启用，PR1 的 standalone validator 不因尚无 PR2 final measurement 而伪造比较值；
5. 通过后才允许产生 ModelCallStartEvent。

第 1、2、3、5 项从 PR1 起即为 production contract。PR3 增加的第 4 项是 compiler 与 pre-send 两个既有事实之间的 cross-layer assertion，不重新定义“final validation”或放宽 PR1 的 provider 前保护。因此 identity/binding/capability rejection 可能没有 estimated_input_tokens；budget 与 estimate mismatch rejection 必须有。

### 13.4 失败位置

validator 位于 LLMRuntime.stream() 内、任何 yield 和 transport 调用之前。

这样：

- direct governance/reflection/compaction 也受保护；
- scripted test transport 无法绕过；
- compiler estimator bug仍有第二道 fail-closed。

LLMRuntime 抛出的结构化异常必须携带 resolved call fact、validation result、context_id 与 reason code，但不拥有 EventLog/RuntimeSession，因此不能自行持久化 rejection。

PR1 独立部署时，AgentRuntime 必须把该异常映射到既有 durable run failure/RunError 路径，direct side engine 必须写自身 terminal failure fact；两者都不得静默返回或调用 transport。PR3 再为 compiled AgentRuntime 增加专用 ModelCallRejectedEvent 与 Inspector projection，不改变 PR1 的失败结果或发送边界。

### 13.5 Compiler 与 pre-send estimate mismatch

主 call 若 compiled.budget.final_payload_estimated_tokens 与 pre-send 估算存在任何非零差异：

- ModelCallStart 前 fail closed；
- reason_code=model_input_estimate_mismatch；
- AgentRuntime 写 ModelCallRejectedEvent；
- 不存在“都没超预算所以允许”的分支；
- 不存在可配置 mismatch threshold。

两处消费同一个 LLMContext 与同一个 estimator，任何差异都是实现/identity bug，不是估算误差。

### 13.6 Direct call failure

governance/reflection/compaction 不经过 compiler。

pre-send over-budget：

- 不产生 ModelCallStart；
- 抛 ModelInputBudgetExceeded；
- subsystem terminal event/error result 必须包含 call fact 与 budget diagnostic；
- 不把 provider overflow 当正常 retry。

## 14. Context compaction 双 limits 设计

### 14.1 新 ContextCompactionPolicy

删除：

- context_window_tokens；
- auto_threshold_tokens；
- summary_max_output_tokens；
- chars_per_token；
- event_chars_per_token；
- estimate_safety_margin。

保留/新增：

~~~python
class ContextCompactionPolicy:
    enabled: bool
    auto_enabled: bool
    manual_enabled: bool
    auto_trigger_ratio: float
    post_compaction_target_ratio: float
    min_events_after_last_compact: int
    keep_recent_runs: int
    max_summary_chars: int
    max_consecutive_failures: int
    summarizer_options: LLMOptions = LLMOptions()
    memory_candidates: ContextCompactionMemoryCandidatePolicy
~~~

V1 defaults 冻结：

- auto_trigger_ratio=0.80；
- post_compaction_target_ratio=0.55。

summarizer_options 默认 LLMOptions()，因此 temperature/reasoning 均不显式请求，max_output_tokens 使用 flash slot default。

ratio 必须满足：

~~~text
0 < post_compaction_target_ratio < auto_trigger_ratio < 1
~~~

### 14.2 动态 threshold

~~~text
threshold_tokens =
    floor(target_model_target.context_budget.input_budget_tokens * auto_trigger_ratio)

post_compaction_target_tokens =
    floor(target_model_target.context_budget.input_budget_tokens * post_compaction_target_ratio)
~~~

不再有全局 200000。

### 14.3 Target-side estimate source

在 primitives/model_call.py 新增 event-safe CompactionTargetEstimateFact：

~~~python
class CompactionTargetEstimateFact(BaseModel):
    estimate_scope: Literal["compiled_context_baseline", "transcript_only"]
    basis_context_id: str | None
    basis_context_compiled_sequence: int | None
    target_fingerprint: str
    non_transcript_baseline_tokens: int | None
    transcript_tokens_before: int
    estimated_tokens_before: int
    summary_tokens_reserved: int
    retained_transcript_tokens: int
    protected_transcript_tokens: int
    summary_tokens_actual: int | None
    transcript_tokens_after: int | None
    estimated_tokens_after: int | None
    predicted_post_target_reached: bool | None
~~~

estimate source 优先级冻结：

1. 查找 sequence 范围内最新、status=compiled、measurement_stage=final_payload、target_fingerprint 相同的 ContextCompiledEvent；
2. 找到时读取其 non_transcript_baseline_tokens，estimate_scope=compiled_context_baseline；
3. 当前 model-visible transcript（含后续 event delta/current user）使用同一 target estimator重新估算；
4. estimated_tokens_before = baseline + current transcript；
5. planning 阶段使用 summary_tokens_reserved + retained event tail + protected current run + standalone current user 选择 compactable prefix；
6. summary 生成并完成 parse/长度校验后，使用 summary_tokens_actual 与同一 baseline/retained/current-user facts 复核 predicted post-compaction；
7. 不把 latest compiled final total 与 transcript delta直接相加，避免重复计算旧 transcript；
8. 找不到合格 baseline 时 estimate_scope=transcript_only、baseline=None，只估 transcript。

compiled_context_baseline 中的 baseline 必须来自最近一次匹配 target fingerprint 的 compiled context，compaction 不按 section 文本重新猜 system/tools/runtime/memory成本。该 baseline 只证明 basis_context_id 当时的 non-transcript cost，不证明下一 run 的 memory、capability、subagent handoff 或其他 source facts 未变化。

transcript_only 语义：

- estimated_tokens_before/after 只表示 transcript；
- predicted_post_target_reached 必须为 None，不能声称完整 context 已达到目标；
- transcript estimate 已超过 full threshold 时可以安全触发 compaction；
- transcript estimate低于 threshold 只能表示“没有足够证据提前 compact”，最终 compiler仍负责完整 context fail-closed。

Started/Completed 必须保存 estimate_scope、basis context attribution 和 baseline。FailedEvent 在 planning 完成后同样保存；failure_stage=planning 时 target_estimate 可以为空。Inspector 必须显示 scope 或明确 not_measured。

event validators：

- compiled_context_baseline 必须同时拥有 basis_context_id、basis_context_compiled_sequence 与 non_transcript_baseline_tokens；
- transcript_only 的三个 compiled attribution 字段必须全部为 None；
- Started.target_estimate 的 summary_tokens_reserved 必填，summary_tokens_actual、after fields 与 predicted_post_target_reached 必须为 None；
- Completed 的 summary_tokens_actual、transcript_tokens_after 与 estimated_tokens_after 必填；
- Completed + compiled_context_baseline 的 predicted_post_target_reached 必须是 bool；
- Completed + transcript_only 的 predicted_post_target_reached 必须为 None；
- Failed 按 failure_stage 只填写已经完成的 measurement，不用 0 代替 unknown；
- Failed 的 artifact_write / completed_append 必须携带 target_estimate actual/after fields；
- Failed 的普通 target_estimate 一旦拥有 after measurement，compiled prediction 必须等于 `estimated_tokens_after <= post_compaction_target_tokens`，transcript_only prediction 必须为 None；
- observed_after_measurement 仅用于 summary reservation violation，不替代普通 target_estimate 的 post-target invariant；
- observed_after_measurement 与普通 actual target_estimate 双向互斥：observed 非空时 target_estimate 必须保持 planning-only，target_estimate 已有 summary actual/after/prediction 时 observed 必须为空。

### 14.4 Service API

~~~python
should_auto_compact(
    *,
    target_model_target: ResolvedModelTarget,
    ...
) -> bool

compact_if_needed(
    *,
    target_model_target: ResolvedModelTarget,
    ...
) -> bool

compact(
    *,
    target_model_target: ResolvedModelTarget,
    ...
) -> ContextCompactionCompletedEvent | None
~~~

三个入口统一使用无歧义参数：

~~~python
model_visible_messages_before
protected_model_visible_messages_after  # tuple[LLMMessage, ...]
current_user_input_if_not_already_represented
~~~

mid-turn 必须传完整 state.messages 作为 before；protected_after 必须来自与 ContextCompiler 相同的 tool-result renderer，由 rendered current-user segment + rendered current-run-tail segment 组成，并保留 assistant tool-call/tool-result pairing。禁止对 raw ToolResultBlock.output 直接估算 protected tokens。standalone current user 置空。protected_after 不进入 summarizer input，但必须进入 planning reservation 和 summary 生成后的实际复核。

auto planning 顺序冻结为：先估算 before/threshold；非 force 且低于 threshold 立即返回 None；之后才选择 boundary、计算 summary reservation 与检查 post-target feasibility。manual force=True 绕过 threshold early return。

### 14.5 Preflight

HostSession：

~~~text
resolve main target
    -> should_auto_compact(target)
    -> compact(target)
    -> RunStartEvent.model_target must equal same target fact
~~~

### 14.6 Manual

manual compact：

- resolve current session next-run target；
- 使用 target fact；
- 不创建主模型 call；
- compaction event 保存 target fact；
- manual 完成不保证下一 run 一定发生。

### 14.7 Mid-turn

mid-turn compaction 接收当前 model loop 的 call.target。

compaction 完成后 compile retry 继续复用原 call ID。

summarizer 有独立 call ID。

### 14.8 Summarizer resolution

~~~text
summarizer_target =
    llm_runtime.resolve_target(
        role=FLASH,
        requested_options=policy.summarizer_options,
    )

summarizer_call =
    llm_runtime.resolve_call(
        target=summarizer_target,
        purpose=context_compaction_summary,
    )
~~~

summary output 未显式 override 时使用 flash slot default。

若 policy 指定 max output，必须在 flash max 范围内，不 clamp。

### 14.9 Summarizer input budget

build_compaction_input() 接收 summarizer target/estimator。

处理顺序：

1. 生成现有 bounded event representation；
2. 与 system prompt 组成最终 LLMContext；
3. estimator 估算；
4. 超预算则切换 deterministic metadata-only representation；
5. 仍超预算则 CompactionSummarizerInputBudgetExceeded；
6. V1 不自动分块发起多次 summary call。

不得把超长 compaction input 直接交给 provider。

### 14.10 Compaction plan 目标

compaction 必须通过同一个 target-side helper 估算 summary 在 replay transcript 中的完整 message body 与 framing：

~~~python
estimate_compaction_summary_replay_tokens(
    *,
    replay_template: CompactionSummaryReplayTemplate,
    summary_text: str,
    target_estimator: TokenEstimator,
) -> int
~~~

CompactionSummaryReplayTemplate 是 runtime-only immutable DTO，在 planning 前固定 summary 之外的 bounded model-visible wrapper、identity/timing metadata 与 message role。planning reservation 和生成后复核必须复用同一个 template。该 helper 使用真实 compaction-summary lowering 形状，不得只对裸 summary 文本调用 estimate_text 后漏掉 wrapper/framing，也不得在第二阶段重新生成一份不同 metadata 的 template。

规划期尚无实际 summary。V1 的 summary_tokens_reserved 固定为使用同一 replay_template、以 "x" * policy.max_summary_chars 作为 synthetic summary 调用该 helper所得上界。由于 V1 estimator 对所有 Unicode code point 使用相同 text chars-per-token 规则，且 summary parser 强制实际 summary 不超过 max_summary_chars，该 reservation 对 V1 是保守上界。未来 estimator 若不再满足这个性质，必须先新增 estimator-owned reservation API，不能继续沿用 synthetic text 规则。

estimate_scope=compiled_context_baseline 时，planner 必须选择 compactable prefix，使 planning reservation 满足：

~~~text
non_transcript_baseline_tokens
+ summary_tokens_reserved
+ retained_event_tail_tokens
+ protected_transcript_tokens
+ standalone_current_user_tokens
<= post_compaction_target_tokens
~~~

summary 生成并通过 parser/max_summary_chars 校验后，service 必须用实际 summary text 调用同一 helper，得到 summary_tokens_actual，并执行第二阶段复核：

~~~text
non_transcript_baseline_tokens
+ summary_tokens_actual
+ retained_event_tail_tokens
+ protected_transcript_tokens
+ standalone_current_user_tokens
<= post_compaction_target_tokens
~~~

只有生成后的第二阶段复核可以填写 estimated_tokens_after 与 predicted_post_target_reached。planner 不得根据 reservation 预填 predicted_post_target_reached。实际值超过 reservation，或相同 retained facts 下第二阶段复核违反规划期不变量，属于 compaction summary validation failure，必须在 artifact/Completed 写入前 fail closed。

成功用 CompactionTargetEstimateFact 保持严格 invariant：

~~~text
transcript_tokens_after
= summary_tokens_actual
+ retained_transcript_tokens
+ protected_transcript_tokens
~~~

其中 `retained_transcript_tokens` 在 planning fact 中即为必填事实，不能只留在 runtime-only CompactionPlan。若 observed summary tokens 已超过 reservation，不放宽成功 fact；ContextCompactionFailedEvent 使用独立 CompactionObservedAfterMeasurementFact 保存 summary_tokens_actual、retained/protected transcript、transcript/estimated after、prediction与稳定 violation_code，并复用同一 transcript-after 公式，planning target_estimate 仍保持合法不可变。

对于 `compiled_context_baseline`，Completed/Failed event validator 还必须验证：

~~~text
predicted_post_target_reached
== (estimated_tokens_after <= post_compaction_target_tokens)
~~~

`transcript_only` 继续固定 prediction 为 None。

Started.target_estimate 是 immutable planning fact。Completed/Failed 必须构造新的 CompactionTargetEstimateFact，复制 reservation、basis 与 retained attribution后再填写 actual/after fields；禁止回写已持久化 Started fact。

这里的 non_transcript_baseline_tokens 来自 basis_context_id 对应的 ContextCompiledEvent，不是对下一 run source facts 的重新编译。因此生成后公式成立也只令 predicted_post_target_reached=true；最终新 context 是否达标，仍由下一次 ContextCompiler 使用当时的 memory/capability/subagent/runtime facts 重新裁决。

estimate_scope=transcript_only 时使用相同数值作为 transcript reduction target，但 predicted_post_target_reached 必须为 None；它只能报告 transcript_tokens_after，不能声称完整 context 达标。

如果 mandatory/current-run boundary 使目标不可达：

- diagnostic=compaction_target_unreachable；
- auto compaction 返回失败；
- 原模型 compile 继续按 fail-closed 处理；
- 不伪造成功 summary。

### 14.11 Compaction event fields

ContextCompactionStarted/Completed/Failed 共同 required：

- target_model_target: ResolvedModelTargetFact；
- target_input_budget_tokens；
- threshold_tokens；
- post_compaction_target_tokens。

ContextCompactionStartedEvent 与 ContextCompactionCompletedEvent 额外 required：

- target_estimate: CompactionTargetEstimateFact；
- summarizer_call: ResolvedModelCallFact；
- summarizer_context_id；
- summarizer_input_estimated_tokens；
- summarizer_input_budget_tokens。

Completed 还记录：

- summarizer_usage_status: reported | missing；
- summarizer_usage: ModelTokenUsageFact | None；
- summarizer_estimated_input_tokens；
- summarizer_reported_model_id: str | None（required nullable provider observation）；
- predicted_post_target_reached: bool | None。

Completed.predicted_post_target_reached 保留为 projection convenience，必须与 Completed.target_estimate.predicted_post_target_reached 完全相等；实现只从 target_estimate 复制，禁止分别计算。

删除旧 flat estimated_tokens_before / estimated_tokens_after 字段；它们由 target_estimate 中同名语义字段唯一表达。Started 的 after fields 为 None，Completed 必须填 after fields，Failed 按 failure_stage 填写已知部分。

ContextCompactionFailedEvent 在 failure_stage 已进入 model_validation/model_stream/artifact_write 时，同样记录可用的 usage_status/usage/estimated_input_tokens。pre-start planning/resolution/input_build failure 没有 provider usage，保存 missing/null；只有完成 final validation 后 estimated_input_tokens 才非空。

ContextCompactionFailedEvent schema 增加：

~~~python
failure_stage: Literal[
    "planning",
    "summarizer_resolution",
    "summarizer_input_build",
    "started_append",
    "model_validation",
    "model_stream",
    "summary_validation",
    "artifact_write",
    "completed_append",
]
target_estimate: CompactionTargetEstimateFact | None
observed_after_measurement: CompactionObservedAfterMeasurementFact | None
summarizer_target: ResolvedModelTargetFact | None
summarizer_call: ResolvedModelCallFact | None
summarizer_context_id: str | None
summarizer_input_estimated_tokens: int | None
summarizer_input_budget_tokens: int | None
summarizer_reported_model_id: str | None
~~~

stage invariants：

- planning：summarizer_target/call/context 全为空；
- planning：target_estimate 可以为空；
- summarizer_resolution：call 为空，target 只有在 target 已成功 resolve、call 构造失败的极窄路径可非空；
- summarizer_resolution 及之后：target_estimate 必填；
- summarizer_input_build 及之后：summarizer_call 必填；
- model_validation 及之后：summarizer_context_id 与 input estimate/budget 必填；
- summary_validation 正常测量可写入 target_estimate actual/after fields；若 actual 超过 reservation、严格 target estimate 无法构造，则 target_estimate 保留 planning fact，并要求 observed_after_measurement 承载已测 actual/after 与 violation；
- artifact_write / completed_append：summary 已通过验证，target_estimate 的 summary actual、transcript after、estimated after 与 prediction 必须全部完成；
- 任意 failure stage 的普通 compiled target_estimate 只要含 after measurement，就必须按 event 的 post_compaction_target_tokens 复核 prediction；transcript_only 仍不得声称 full target success；
- Started/Completed：上述 summarizer 字段全部必填；
- “无需 compact / plan is None” 是正常 no-op，不写 FailedEvent；只有 planning exception 才写 failure_stage=planning。

### 14.12 Compaction event ordering

compaction_id 与 attribution EventContext 在进入 planning attempt 前生成。plan 正常返回 None 时不写任何 event；planning 抛异常时可以写带同一 compaction_id 的 failure_stage=planning。

~~~text
build plan
    -> resolve summarizer target/call
    -> build/degrade summarizer context
    -> estimate summarizer input
    -> append ContextCompactionStarted
    -> validate/send summarizer call
    -> parse/limit summary
    -> estimate actual summary replay + post-target review
    -> write summary artifact
    -> append Completed or Failed
~~~

Started 写入前已经拥有最终 summarizer_context_id 和 input estimate。Started 中有 call fact，不代表 ModelCallStart 已发生。

ModelCallStart 是否发生由 summarizer stream 内部生命周期事实决定；若这些事件暂未写 parent log，Completed/Failed 仍通过 summarizer_call fact审计 resolution contract。

_summarize() 不再只返回 str。它返回 bounded DirectModelCallResult，至少含 text、resolved call fact、validation estimate、usage_status/usage；ContextCompactionService 用它构造 terminal event，避免消费 stream 后丢失 end usage。

### 14.13 Compaction memory candidates

从 compact 中提取记忆候选的既有语义不改变。

它发生在 summary artifact 和 CompletedEvent 之后。

candidate extraction 不得重新解析 summarizer model 或 limits。

## 15. Memory governance 与 reflection

### 15.1 共同规则

两个 engine 都必须：

~~~text
resolve target
    -> resolve call
    -> build LLMContext with call identity
    -> LLMRuntime pre-send validation
    -> stream(call)
~~~

不保留 role/options stream overload。

MemoryGovernanceOptions 与 MemoryReflectionOptions 的默认 llm_options 均为 LLMOptions()。它们不能覆盖 temperature、reasoning_summary 或 max_output_tokens；需要不同输出上限时必须使用不同的 model slot/slot limits。只有 reasoning_effort 是合法 per-call option，provider 不支持时 resolve_target 应真实失败。

共享 runtime-only result：

~~~python
@dataclass(frozen=True, slots=True)
class DirectModelCallResult:
    text: str
    resolved_call: ResolvedModelCallFact
    estimated_input_tokens: int
    outcome: Literal["completed", "provider_error"]
    error: ModelCallDiagnosticFact | None
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    reported_model_id: str | None
~~~

新增共享：

~~~python
async def collect_direct_model_call(
    events: AsyncIterator[AgentEvent],
    *,
    expected_call: ResolvedModelCall,
) -> DirectModelCallResult: ...
~~~

collector 规则：

1. TextBlockDeltaEvent 追加 text；
2. RunErrorEvent 只暂存为 bounded error，不立即 raise、不停止迭代；
3. 继续消费直到 ModelCallEndEvent 和 ReplyEndEvent；
4. RunErrorEvent 后必须观察到 ModelCallEndEvent(outcome="provider_error")；
5. 无 RunError 的正常结果必须 end outcome=completed；
6. end identity必须与 expected_call一致；
7. stream 完成后再由 subsystem 根据 outcome 构造 Completed/Failed；
8. transport exception 没有 end 时，抛带 partial observation 的 DirectModelCallCollectionError，outer FailedEvent 使用 missing/null；
9. asyncio.CancelledError 原样传播，不伪造 end/usage。

`reported_model_id` 同样必须从被 collector 消费的 `ModelCallEndEvent`
原样携带。direct subsystem 不得重新解析 provider payload，也不得用
requested id 伪造 reported id。

LLMRuntime 的可收口 provider error 顺序固定为：

~~~text
RunErrorEvent
    -> ModelCallEndEvent(outcome="provider_error")
    -> ReplyEndEvent
~~~

governance/reflection 的 _call_flash 和 compaction 的 _summarize 都必须调用该 collector，不再在看见 RunErrorEvent 时立即抛异常，也不再只返回 str。

### 15.2 Governance

MemoryGovernanceRunResult 新增：

~~~python
resolved_model_call: ResolvedModelCallFact | None
usage_status: Literal["reported", "missing"] | None
usage: ModelTokenUsageFact | None
estimated_input_tokens: int | None
reported_model_id: str | None
~~~

没有 pending candidate 时不解析 call，字段为空。

有 pending input 且 resolution 成功后，无论 model parse/apply 成功与否都返回 call fact。

本章节不新增一条绕过 governance PostgreSQL UOW 的独立 audit append。governance durable model-call event 的事务归属在后续 LiveRuntimeEventWriter + governance UOW 大章统一处理。

这不是允许 governance 绕过 resolved call；只是避免在本章制造新的非原子 event 写路径。

### 15.3 Reflection

MemoryReflectionCompletedEvent required resolved_call。

Completed 同时 required：

- usage_status；
- usage（可空，受 status validator约束）；
- estimated_input_tokens。
- reported_model_id（required nullable provider observation）。

MemoryReflectionFailedEvent 增加：

- failure_stage；
- resolved_call 可空；
- usage_status / usage / estimated_input_tokens。
- reported_model_id。

failure_stage 枚举冻结：

~~~python
Literal[
    "input_build",
    "target_resolution",
    "call_resolution",
    "model_validation",
    "model_stream",
    "output_parse",
    "candidate_append",
]
~~~

规则：

- input_build / target_resolution / call_resolution 的 resolved_call 可以为空；
- model_validation 及之后 resolved_call 必填；
- completed 永远非空；
- model_validation 的 estimated_input_tokens 可以为空，因为 identity、binding 或 capability 校验可能在 estimate_model_context_for_call() 前失败；
- model_validation exception 若已经携带 TokenEstimate（例如 input budget 或 compiler/pre-send mismatch），terminal fact 必须保存其 total_input_tokens，不能丢弃后改写为 null；
- model_stream / output_parse / candidate_append 的 estimated_input_tokens 必填；
- model_stream 收到可收口 RunError 时，通过 shared collector 保存 end 的 usage_status/usage；
- pre-start 或没有 end 的异常 usage_status/usage 为 missing/null，不写零；
- output_parse/candidate_append 复用已完成 direct call 的 usage，不能丢失。

### 15.4 Direct context budget

governance/reflection prompt JSON 若超过 flash input budget：

- provider 前拒绝；
- error type 稳定；
- 不裁剪 candidate/evidence JSON 到语义不完整；
- 上层可减少 batch size 后创建新 call；
- 原 call 不被改写后重试。

### 15.5 Governance batch retry

若 coordinator 减少 candidate batch：

- 这是新的 direct context；
- 创建新 call ID；
- target 可以相同；
- 原失败 call fact保留。

## 16. Inspector、replay 与 diagnostics

### 16.1 Join 主键

Inspector 以 resolved_model_call_id 为模型调用主 join key。

context_id 仍用于：

- 一次 compile payload；
- section/timing inspection；
- provider payload attribution。

但不再承担 call identity。

### 16.2 Normalized shape

建议：

~~~json
{
  "model_targets": [],
  "model_calls": [],
  "context_compilations": [],
  "compaction_model_contracts": [],
  "diagnostics": []
}
~~~

每个 model_call：

- call id；
- purpose/context mode；
- target fingerprint；
- requested model id、reported model id、identity policy 与 exact/different/missing relation；
- provider/api；
- effective output；
- input budget；
- estimator；
- compile context ids；
- nullable start/end/rejected sequences；
- nullable subsystem_terminal_sequence（compaction/reflection outer fact）；
- structured usage；
- join status。

Inspector 还应提供纯事实型 usage projection：

- per-call ModelTokenUsageFact；
- per-run input/output/total 聚合；
- cached/non-cached breakdown availability；
- 按当前 durable facts 聚合 agent loop、compaction、reflection usage；
- usage 缺失、重复或 call identity 漂移 diagnostic。

这些投影只解释已经发生的模型资源消耗，不根据当前配置推导 soft pressure、remaining budget 或“本次本应停止”的结论。

聚合只对 usage_status=reported 的 fact 求和，并同时返回 reported_call_count / missing_usage_call_count。missing call 不按零计入，也不能声称聚合是完整总量。

PR5 明确不承诺历史 governance usage projection。MemoryGovernanceRunResult 可以向当前 coordinator/caller 暴露 usage，但在 governance UOW 大章提供原子 durable audit 前，不把它冒充 session history。

### 16.3 Call join status

枚举：

- compiled_started_completed；
- compiled_rejected；
- compiled_pressure_only；
- direct_started_completed；
- direct_rejected；
- started_missing_end；
- end_missing_start；
- fact_mismatch；
- target_binding_mismatch。

compiled_rejected 只由 ModelCallRejectedEvent 得出。

direct_rejected 不寻找 ModelCallRejectedEvent，而从 durable subsystem terminal fact 派生：

- ContextCompactionFailedEvent 的 failure_stage/reason；
- MemoryReflectionFailedEvent 的 failure_stage/reason；
- governance 在本章没有 durable historical direct_rejected projection。

同理，compaction/reflection 的 direct_started_completed 可以从各自 CompletedEvent 中的 call/usage fields 派生；它不要求被 side engine 消费掉的 ModelCallStart/End 也出现在 parent session EventLog。

### 16.4 Direct call 不误报 ContextCompiled 缺失

context_mode=direct 时，没有 ContextCompiledEvent 是正常。

只有 context_mode=compiled 的 ModelCallStart 才要求 matched compiled fact。

V1 inspector 只展示已经进入被检查 EventLog 的 lifecycle/outer terminal facts。governance call fact 暂时只在 MemoryGovernanceRunResult 中时，session inspector 不承诺展示它；后续 governance UOW 大章把 durable audit 原子落地后再纳入。不得为了让 inspector “先看到”而在本章新增非原子 append。

### 16.5 Fact equality diagnostics

stable codes：

- resolved_model_call_fact_mismatch；
- model_target_fingerprint_mismatch；
- context_model_call_identity_mismatch；
- model_call_start_missing_compiled_context；
- model_call_end_missing_start；
- model_call_rejected_after_start；
- run_model_target_mismatch；
- compaction_target_mismatch；
- estimator_fact_mismatch。

### 16.6 Replay

新 event schema required facts。

旧 log：

- schema/contract error；
- inspector 不显示 unknown/default；
- runtime 不恢复；
- 开发环境 reset；
- 如需保留历史，只能离线 migration 成完整事实，不能 runtime 猜。

### 16.7 Inspector 不重新算历史预算

Inspector 展示 compile/send 时记录的：

- target fact；
- estimator fact；
- estimates；
- usage。

不得用当前 config 或当前 estimator 重算后覆盖历史。

可以提供 comparison diagnostic，但历史 fact 不变。

## 17. 稳定错误类型与 reason codes

### 17.1 配置/解析错误

- ModelLimitsConfigurationError；
- ModelBudgetConfigurationError；
- ModelOutputLimitExceeded；
- ModelInputBudgetUnavailable；
- ModelTransportUnavailable；
- ModelTransportBindingMismatch；
- ModelOptionUnsupported；
- ModelTargetBindingMismatch；
- ModelTargetCapabilityMismatch。

### 17.2 Compile/send 错误

- ContextBudgetExceeded；
- ModelInputBudgetExceeded；
- ModelInputEstimateMismatch；
- ModelContextIdentityMismatch；
- LLMTransportContractError；
- CompactionSummarizerInputBudgetExceeded；
- CompactionTargetUnreachable。

### 17.3 Stable reason codes

- model_limits_missing；
- model_limits_invalid；
- model_output_limit_exceeded；
- provider_budget_key_conflict；
- model_option_unsupported；
- model_input_budget_non_positive；
- model_input_budget_exceeded；
- model_input_estimate_mismatch；
- model_context_identity_mismatch；
- model_target_binding_mismatch；
- model_transport_binding_mismatch；
- model_target_does_not_support_tools；
- transport_emitted_model_call_start；
- transport_emitted_model_call_end；
- transport_emitted_reply_start；
- transport_emitted_reply_end；
- transport_usage_report_duplicate；
- transport_changed_model_target；
- provider_usage_incomplete；
- provider_usage_total_mismatch；
- context_budget_still_exceeded；
- compaction_target_unreachable；
- compaction_summary_reservation_exceeded；
- compaction_post_target_review_failed；
- compaction_summarizer_input_budget_exceeded。

### 17.4 Secret safety

错误和 diagnostic：

- 不包含 API key；
- 不包含 full base URL；
- 不包含 request headers；
- 不包含 raw request defaults；
- 不包含完整 provider payload；
- 可包含 model id、provider、api、target fingerprint、safe endpoint origin。

## 18. 代码落脚点清单

### 18.1 New primitives / resolution

- src/pulsara_agent/primitives/model_call.py
- src/pulsara_agent/llm/resolution.py
- src/pulsara_agent/llm/estimator.py
- src/pulsara_agent/llm/result.py
- src/pulsara_agent/llm/direct.py

### 18.2 Config/model

- src/pulsara_agent/llm/config.py
- src/pulsara_agent/llm/models.py
- src/pulsara_agent/llm/provider.py
- src/pulsara_agent/settings.py
- .env.example
- README.md

### 18.3 LLM runtime/transport

- src/pulsara_agent/llm/runtime.py
- src/pulsara_agent/llm/transport.py
- src/pulsara_agent/llm/registry.py
- src/pulsara_agent/llm/request.py
- src/pulsara_agent/llm/adapters/mock.py
- src/pulsara_agent/llm/adapters/openai/events.py
- src/pulsara_agent/llm/adapters/openai/responses.py
- src/pulsara_agent/llm/adapters/openai/chat_completions.py

### 18.4 Event

- src/pulsara_agent/event/events.py
- src/pulsara_agent/event/__init__.py
- src/pulsara_agent/event_log/serialization.py
- event log contract/version declarations

### 18.5 Agent/compiler

- src/pulsara_agent/runtime/agent.py
- src/pulsara_agent/runtime/state.py
- src/pulsara_agent/runtime/context.py
- src/pulsara_agent/runtime/context_engine/types.py
- src/pulsara_agent/runtime/context_engine/compiler.py
- src/pulsara_agent/runtime/context_engine/tool_results.py

### 18.6 Host/recovery

- src/pulsara_agent/host/session.py
- src/pulsara_agent/host/core.py
- src/pulsara_agent/host/resume.py
- src/pulsara_agent/runtime/transcript.py

### 18.7 Compaction

- src/pulsara_agent/runtime/compaction/service.py
- src/pulsara_agent/runtime/compaction/planner.py
- src/pulsara_agent/runtime/compaction/inline.py

### 18.8 Memory side calls

- src/pulsara_agent/memory/governance/engine.py
- src/pulsara_agent/memory/governance/coordinator.py
- src/pulsara_agent/memory/reflection/engine.py

### 18.9 Inspector

- src/pulsara_agent/inspector/service.py
- CLI inspect/status renderers

### 18.10 Test support

新增集中 fixtures：

~~~text
tests/support/model_call.py
~~~

提供：

- test_model_limits()；
- test_model_slot()；
- test_llm_config()；
- resolve_test_target()；
- resolve_test_call()；
- model_call_event_fields()。

不要在 50 余个测试文件重复写 limits dict。

## 19. PR 实施顺序

### PR0：Primitives、model slot config 与 fact vocabulary

目标：

- 冻结 ModelContextLimits；
- 冻结 target/call facts；
- LLMConfig 改 required slots；
- 环境变量 hard cut；
- 建立 event-safe fact DTO，但暂不要求旧 runtime events 立刻携带它们；
- 建立 test fixture builder。

实现：

1. 新增 primitives/model_call.py；
2. 新增 ModelSlotConfig；
3. LLMConfig pro/flash slot hard cut；
4. limits validators；
5. provider payload ownership guard（递归 reserved registry）；
6. `accept_reported | exact` model identity policy，默认 `accept_reported`；
7. endpoint canonicalization、recursive secret redaction 与 fingerprint helper；
8. ResolvedModelTargetFact / ResolvedModelCallFact / ModelTokenUsageFact / ContextBudgetReportEvent / CompactionTargetEstimateFact serialization tests；
9. 更新全部 LLMConfig test constructors；
10. 更新 .env.example、settings tests；
11. 保持 PR0 独立可运行，不引入尚无 resolver 可生成的 required event field。

验收：

- 缺任一 limits 字段 startup fail；
- invalid limits fail；
- request default 中 output budget key fail；
- identity policy 默认 accept_reported，exact 可显式配置，且 policy 进入 target fingerprint；
- fact JSON round-trip；
- facts 不含 secret；
- 所有测试 fixture 使用集中 builder。

### PR1：Resolver、LLMRuntime 与 transport API hard cut

目标：

- resolve_target/resolve_call/rebind_target；
- stream(call/context)；
- transport 只接 call；
- LLMRuntime 统一构造并 yield ReplyStart/ModelCallStart/ModelCallEnd/ReplyEnd；
- Host/LoopState/AgentRuntime 建立完整 run target 与 per-step call ownership；
- 迁移所有直接 caller 的 execution API；
- PR1 自身具备可部署的 standalone pre-send validation，不依赖 PR3 才赋予 ModelCallStartEvent 合法语义。

实现：

1. llm/resolution.py；
2. estimator runtime object；
3. target/call resolver；
4. explicit unsupported option fail-fast；
5. LLMRuntime stream API；
6. transport protocol；
7. OpenAI adapters；
8. AgentEventBuilder 删除 model_start/model_end；
9. Mock/scripted transports；
10. HostSession 在 run 建立前 resolve run target，并显式传给 AgentRuntime；
11. LoopState.run_model_target 与 RunStartEvent.model_target；
12. 每个 model step 在 compile 前 resolve_call(run_target)，compile retry 复用该 call；
13. compaction preflight API 接收同一个 target，PR4 再切换动态 threshold/双 limits；
14. Agent/compaction/governance/reflection 完成 stream(call) API migration；
15. shared collect_direct_model_call，RunError 后 drain 到 End；
16. governance/reflection 使用仅含 reasoning_effort 的 LLMOptions；
17. Reply/ModelCall lifecycle event schema hard cut；
18. LLMContext required call/context identity；
19. TokenEstimate 从 PR1 起 required message_tokens_by_index，并由 estimate_context() 与 aggregate 同次生成；
20. estimate_model_context_for_call(call, context) estimate-only seam；
21. validate_model_context_for_call(call, context) standalone validator，覆盖 identity、binding、tool capability、input budget 与 output contract；
22. LLMRuntime 在任何 lifecycle start/transport 前完成 validation，并用同一 validation estimate 构造 ModelCallEndEvent；
23. PR1 rejection 映射到既有 AgentRuntime durable run failure/RunError 或 direct subsystem terminal failure，不静默丢失；
24. LLMRuntime 拒绝 transport 产生四种 lifecycle event；transport 只返回 semantic events 与可选 TransportUsageReport；
25. ModelCallEnd 使用结构化 ModelTokenUsageFact，network retry 不重复生成 usage；
26. transport 将成功 attempt 的 provider-reported model id 写入 completion report，ModelCallEnd
    required nullable 地持久化；
27. accept_reported 接受 alias/snapshot，exact 保留旧严格行为；
28. AgentEvent union/serialization contract bump；
29. MemoryReflection terminal event增加 resolved call/usage/failure_stage，MemoryGovernanceRunResult 增加 call/usage fact；
30. 禁止 compatibility overload。

PR1 的 compiler 仍可使用旧 allocation 常量，但 provider 前的 standalone validator 已使用 resolved call 的真实 limits/estimator fail closed。call 在 compile 前创建，compile 后直接交给 stream(call)，绝不在 stream 前临时重新 resolve target/call。PR2 负责让 compiler/tool-result renderer 主动消费同一 limits/estimator并产出 final measurement；PR3 再比较 compiler 与 pre-send 两个事实。PR1 必须独立可部署、test suite 整体可运行，不允许以“PR1–PR3 连续迁移”为理由写出未经 validation 的 ModelCallStartEvent，也不允许同一生产进程混用新旧 stream。

验收：

- target fingerprint deterministic；
- call ID random；
- same target two calls different IDs；
- retry only one start；
- retry only one canonical end usage fact；
- retry 只记录最终成功 attempt 的 reported model identity；
- provider alias 在默认 policy 下可执行并可 inspect；
- exact policy mismatch 仍 fail closed；
- transport 的 ReplyStart/ModelCallStart/ModelCallEnd/ReplyEnd 全部被拒绝；
- explicit unsupported option 在 target resolution 阶段失败；
- direct collector 在 RunError 后仍观察到 provider_error End；
- Host/LoopState/RunStart target facts一致；
- compile 前已经存在 resolved call；
- effective output必定发送；
- PR1 的 TokenEstimate 已含完整 message_tokens_by_index，aggregate 与 breakdown 一致；
- PR1 的 estimated_input_tokens 来自 standalone validation result，不为零或空；
- oversized direct call 在 PR1 就于 ReplyStart/ModelCallStart/transport 前 fail closed；
- PR1 compiled/direct validation rejection 都有结构化失败事实，不依赖 PR3 ModelCallRejectedEvent 才对用户可见；
- no stream(role/options) references；
- 四类 production caller 全迁移。

### PR2：ContextCompiler、tool-result renderer 与单一预算

目标：

- compiler 使用 call limits/estimator；
- tool-result renderer 使用同一 estimator；
- total context fail closed。

实现：

1. ContextCompileRequest.resolved_call；
2. build_compiled_context 在 render_segmented_llm_messages 前接收 call；
3. tool-result renderer required token_estimator；
4. render cache key 纳入 estimator fingerprint；
5. 删除 tool_results.py 的 chars/4 model-token 真源；
6. 删除 compiler 256000/8000；
7. ContextBudgetReport / ContextBudgetReportEvent 新 schema；
8. staged measurement fields，early pressure不伪造零；
9. 消费 PR1 已生成的 TokenEstimate.message_tokens_by_index；
10. compiler lowering 产出与 messages 等长的 message_budget_scopes并据此做 scope 分类；
11. transcript/non-transcript baseline 只从同一 final estimate breakdown 派生；
12. allocation_estimated_tokens=sections_estimated_tokens+tools_estimated_tokens；
13. total final estimate fail closed；
14. pressure/failed event 从 exception fact 构造；
15. ContextCompiled event schema hard cut；
16. mid-turn retry复用既有 call。

验收：

- preflight target == RunStart target；
- ContextCompiled call == ModelCallStart call；
- compile retry call ID不变；
- tool follow-up call ID变化；
- target fingerprint不变；
- small-window model provider前 pressure；
- renderer early pressure 的未测量字段为 None而非0；
- compiled report 必须 final_payload stage；
- final report 的 message-level breakdown 与 message budget scopes 等长；
- transcript + non-transcript baseline 精确等于 final total；
- allocation estimate 精确等于 sections + tools；
- total over budget 不再 warning-and-send；
- subagent child 同样走新 contract。

### PR3：Compiler/pre-send 一致性、rejection audit 与 contract hardening

目标：

- compiled call 的 compiler final measurement 与既有 pre-send validation 强一致；
- ModelCallRejectedEvent；
- rejection durable ownership 与 inspect 语义；
- 不改变 PR1 已冻结的 lifecycle ownership/validation 语义。

实现：

1. compiled mode 将 PR2 final estimate 作为 required cross-layer assertion 输入；
2. validate_model_context_for_call 继续复用 PR1 estimate-only seam，并拒绝任意 compiler/pre-send mismatch；
3. AgentRuntime 捕获结构化 validation exception 并写 ModelCallRejectedEvent；
4. direct side engine 只写各自 terminal result/fact；
5. ModelCallRejected event schema required context_mode=compiled；
6. Inspector 投影 rejection 与 resolved call/context join；
7. response model identity 与 transport contract diagnostics 收口。

验收：

- PR1 的 oversized governance/reflection provider 前保护继续成立；
- rejection 无 ReplyStart/ModelCallStart；
- LLMRuntime 自身不写 durable rejected event；
- direct side call 不写 ModelCallRejectedEvent；
- mismatched context call ID与fingerprint继续由 PR1 validator 拒绝；
- transport lifecycle event继续由 PR1 guard 拒绝；
- model end缺失被诊断；
- final estimate和compiler一致；
- validator 不复制 estimator，完整 validation 至多产生一次 TokenEstimate；
- 任意非零 mismatch 均拒绝发送。

### PR4：Compaction target/summarizer 双 limits

目标：

- 删除静态 compaction window/threshold/output；
- dynamic threshold；
- summarizer input/output完整 budget；
- compaction audit facts。

实现：

1. ContextCompactionPolicy 新 schema；
2. service API required target；
3. Host manual/preflight target wiring；
4. inline mid-turn target wiring；
5. summarizer target/call；
6. ContextCompactionPolicy.summarizer_options 默认 LLMOptions()；
7. target estimate source/scope 与 latest compiled baseline；
8. budget-aware compaction input；
9. target-aware planner；
10. summary replay-token helper 与 planning summary_tokens_reserved；
11. summarizer context 在 Started 前 build/degrade/estimate；
12. summary 生成后的 summary_tokens_actual 与 post-target second-phase review；
13. compaction events facts 与 failure_stage validators；
14. terminal events 保存 usage_status/usage/estimated_input_tokens；
15. 删除 compaction chars/token 真源；
16. memory candidate extension保持既有时序。

验收：

- 不同 target window 得到不同 threshold；
- manual 没有 fake main call ID；
- summarizer 有真实 call ID；
- target/summarizer fingerprints 可不同；
- summarizer 小窗口 provider前 fail；
- planning/resolution/input-build pre-start failure schema合法；
- Started 一定携带已估算的 summarizer context；
- summary output override超 max fail；
- planner 只使用 summary_tokens_reserved 选择 prefix，不预填 predicted_post_target_reached；
- summary 生成后由实际 replay estimate 填写 summary_tokens_actual、estimated after 与 predicted_post_target_reached；
- actual summary 超 reservation 或 second-phase review 破坏规划不变量时，在 artifact/Completed 前 fail closed；
- compiled_context_baseline 的 actual-summary predicted estimate 达到 post target或明确失败；
- compiled-context-baseline estimate 保留 latest compiled non-transcript baseline；
- 无 baseline 时明确 transcript_only且不声称达标；
- 不再引用 200000/8192 model budget常量。

### PR5：Inspector、replay、DB contract 与清理

目标：

- resolved call graph 可 inspect；
- replay strict；
- 删除所有旧 budget真源；
- PostgreSQL/real LLM dogfood。

实现：

1. inspector join by call ID；
2. direct/compiled status；
3. fact equality diagnostics；
4. run target projection；
5. compaction target/summarizer projection；
6. agent/compaction/reflection 的 per-call/per-run/per-purpose usage projection；
7. old log hard-cut error；
8. remove compatibility helpers/fields；
9. reset/migration instructions；
10. full/real LLM tests。

验收：

- inspector 能从 run target 到 compile/start/end join；
- direct side call不误报missing context；
- PR5 不承诺 historical governance usage projection；
- fact mismatch稳定诊断；
- grep 无 production 256000/8000/8192/200000 model budget；
- PostgreSQL event round-trip；
- all unit/integration/real LLM passes。

## 20. 测试矩阵

### 20.1 Limits/config

- test_model_context_limits_require_positive_values
- test_model_context_limits_reject_inconsistent_maxima
- test_model_context_limits_validate_default_output
- test_model_slot_limits_are_required_from_env
- test_pro_and_flash_limits_are_independent
- test_output_budget_uses_slot_default
- test_per_call_output_and_temperature_options_are_not_supported
- test_provider_request_defaults_reject_output_budget_keys
- test_provider_extra_body_rejects_output_budget_keys
- test_provider_extensions_reject_pulsara_owned_payload_keys
- test_provider_extensions_allow_non_conflicting_fingerprinted_shape
- test_thinking_omit_cannot_remove_output_budget
- test_temperature_and_reasoning_summary_are_absent_from_options_contract
- test_compaction_summarizer_options_default_to_empty_options
- test_auto_compaction_checks_threshold_before_target_feasibility
- test_mid_turn_protected_messages_are_counted_after_but_not_summarized
- test_mid_turn_protected_tool_result_uses_renderer_visible_estimate
- test_resolved_target_fact_requires_slot_default_output_cap
- test_compaction_summary_actual_must_not_exceed_reservation
- test_compiled_baseline_estimate_requires_complete_attribution
- test_compiled_baseline_actual_after_requires_prediction
- test_model_call_rejected_requires_estimate_after_estimation
- test_model_call_rejected_allows_missing_estimate_before_estimation
- test_model_identity_policy_defaults_to_accept_reported_and_allows_exact_env

### 20.2 Facts/fingerprints

- test_resolved_target_fingerprint_is_stable
- test_resolved_target_fingerprint_changes_with_limits
- test_resolved_target_fingerprint_changes_with_options
- test_resolved_target_fingerprint_changes_with_estimator
- test_resolved_target_fingerprint_changes_with_transport_binding
- test_resolved_target_fingerprint_changes_with_model_identity_policy
- test_endpoint_canonicalization_normalizes_host_port_and_trailing_slash
- test_endpoint_canonicalization_rejects_userinfo_query_fragment_and_dot_segments
- test_request_shape_secret_keys_are_recursively_redacted
- test_request_shape_header_and_cookie_secrets_are_redacted
- test_credential_rotation_does_not_change_request_shape_fingerprint
- test_non_secret_request_shape_change_changes_target_fingerprint
- test_normalized_secret_key_collision_is_configuration_error
- test_requested_option_rejected_when_thinking_policy_forbids_it
- test_unrequested_none_option_is_not_treated_as_omission
- test_payload_does_not_apply_any_second_option_omission
- test_rebind_target_reconstructs_only_sent_effective_options
- test_resolved_calls_share_target_but_have_unique_ids
- test_resolved_fact_round_trip
- test_model_token_usage_fact_round_trip
- test_model_token_usage_rejects_invalid_cached_or_reasoning_breakdown
- test_model_token_usage_preserves_missing_breakdown_as_null
- test_model_token_usage_total_equals_input_plus_output
- test_resolved_fact_contains_no_api_key
- test_resolved_fact_redacts_endpoint_userinfo_query_path
- test_nested_resolved_facts_are_immutable
- test_fingerprint_rejects_nan_and_infinity

### 20.3 Resolver/rebind

- test_resolve_target_binds_transport_once
- test_resolve_call_does_not_reparse_config
- test_rebind_target_accepts_identical_runtime_config
- test_rebind_target_rejects_model_change
- test_rebind_target_rejects_limits_change
- test_rebind_target_rejects_estimator_change
- test_rebind_target_rejects_endpoint_change
- test_rebind_target_rejects_provider_shape_change
- test_rebind_target_rejects_transport_contract_change

### 20.4 Runtime/transport

- test_llm_runtime_emits_model_start_once
- test_transport_no_longer_emits_model_start
- test_transport_no_longer_emits_model_end
- test_transport_no_longer_emits_reply_start
- test_transport_no_longer_emits_reply_end
- test_transport_emitted_model_start_is_contract_error
- test_transport_emitted_model_end_is_contract_error
- test_transport_emitted_reply_start_is_contract_error
- test_transport_emitted_reply_end_is_contract_error
- test_duplicate_transport_usage_report_is_contract_error
- test_missing_provider_usage_is_missing_not_zero
- test_pr1_estimate_only_seam_supplies_model_end_input_tokens
- test_estimate_only_failure_writes_no_start_or_fake_end
- test_pr1_standalone_validation_rejects_oversized_direct_call_before_reply_start
- test_pr1_token_estimate_includes_message_breakdown
- test_pr1_compiled_validation_rejection_uses_existing_durable_failure_path
- test_pr1_direct_validation_rejection_writes_subsystem_terminal_failure
- test_runtime_injects_validation_estimate_into_model_end
- test_network_retry_reuses_resolved_call_id
- test_network_retry_reuses_payload
- test_network_retry_records_one_canonical_usage_fact
- test_model_end_references_same_call
- test_responses_payload_uses_effective_output
- test_chat_payload_uses_effective_output
- test_stream_role_options_signature_is_removed
- test_reported_response_model_alias_is_accepted_by_default
- test_reported_chat_model_alias_is_accepted_by_default
- test_exact_model_identity_policy_rejects_mismatch
- test_reported_model_identity_changes_within_attempt_is_rejected
- test_network_retry_discards_abandoned_attempt_reported_identity
- test_model_call_end_records_reported_model_identity
- test_missing_response_model_is_allowed_but_not_confirmation
- test_direct_collector_drains_run_error_until_model_end
- test_direct_collector_preserves_provider_error_usage
- test_direct_collector_transport_exception_has_no_fake_end
- test_provider_run_error_precedes_model_end_and_reply_end

### 20.5 Run/Host/recovery

- test_host_resolves_target_before_preflight_compaction
- test_run_start_records_model_target
- test_preflight_target_equals_run_start_target
- test_run_followups_reuse_target
- test_resume_rebinds_original_run_target
- test_resume_rejects_changed_model_target
- test_resume_does_not_use_current_config_as_fallback
- test_subagent_child_run_records_model_target

### 20.6 Compiler

- test_context_compiler_uses_resolved_limits
- test_context_compiler_uses_effective_output_tokens
- test_context_compiler_uses_resolved_estimator
- test_v1_estimator_text_json_and_framing_golden_values
- test_token_estimate_message_breakdown_matches_message_count_and_total
- test_lowering_message_budget_scopes_match_lowered_messages
- test_message_framing_follows_message_budget_scope
- test_final_transcript_plus_non_transcript_baseline_equals_total
- test_allocation_estimate_equals_sections_plus_tools
- test_tool_result_renderer_uses_resolved_estimator
- test_tool_result_render_cache_is_partitioned_by_estimator_fingerprint
- test_total_context_over_budget_fails_closed
- test_tool_result_early_pressure_report_has_renderer_stage_and_null_unmeasured_fields
- test_section_pressure_report_has_section_stage
- test_compiled_report_requires_final_stage_and_all_measurements
- test_current_user_over_budget_has_specific_reason
- test_context_pressure_event_records_resolved_call
- test_context_failed_event_records_real_budget
- test_compiled_event_budget_matches_call_fact
- test_compile_retry_reuses_call_id
- test_compile_retry_may_change_context_id
- test_tool_followup_uses_new_call_id
- test_target_without_tool_support_rejects_tool_context

### 20.7 Pre-send validation

- test_final_context_identity_matches_call
- test_final_context_call_id_mismatch_rejected
- test_final_context_target_fingerprint_mismatch_rejected
- test_final_context_over_budget_rejected_before_reply_start
- test_final_context_over_budget_rejected_before_model_start
- test_final_context_over_budget_never_invokes_transport
- test_compiler_and_pre_send_estimates_are_equal
- test_any_nonzero_compiler_pre_send_estimate_mismatch_is_rejected
- test_model_call_rejected_event_is_inspectable
- test_llm_runtime_does_not_persist_model_call_rejected
- test_direct_call_rejection_uses_subsystem_terminal_fact

### 20.8 Compaction

- test_compaction_threshold_derived_from_target_budget
- test_compaction_prefers_matching_latest_compiled_baseline
- test_compaction_full_estimate_preserves_non_transcript_baseline
- test_compaction_predicted_post_target_includes_compiled_non_transcript_baseline
- test_compaction_compiled_baseline_is_prediction_not_next_compile_truth
- test_compaction_without_baseline_marks_transcript_only
- test_compaction_transcript_only_never_claims_predicted_post_target_reached
- test_compaction_post_target_derived_from_target_budget
- test_manual_compaction_uses_target_without_main_call
- test_preflight_compaction_uses_pending_run_target
- test_mid_turn_compaction_uses_current_call_target
- test_compaction_retry_reuses_main_call
- test_compaction_summarizer_has_separate_call
- test_compaction_summarizer_input_fits_flash_budget
- test_compaction_summarizer_input_uses_metadata_only_degradation
- test_compaction_summarizer_input_over_budget_fails_before_provider
- test_compaction_summary_output_uses_flash_default
- test_compaction_summary_output_override_above_max_fails
- test_compaction_planner_uses_summary_token_reservation_not_future_actual_summary
- test_compaction_started_records_reservation_but_no_actual_after_measurement
- test_compaction_completed_uses_actual_summary_replay_estimate
- test_compaction_summary_actual_must_not_exceed_reservation
- test_compaction_predicted_post_target_is_decided_only_after_summary_generation
- test_compaction_events_record_both_contracts
- test_compaction_target_unreachable_is_explicit
- test_compaction_planning_failure_allows_missing_summarizer_call
- test_compaction_resolution_failure_allows_missing_summarizer_call
- test_compaction_input_build_failure_requires_summarizer_call
- test_compaction_started_requires_built_context_and_input_estimate
- test_compaction_terminal_event_preserves_usage_or_missing_status

### 20.9 Governance/reflection

- test_governance_resolves_direct_call
- test_governance_empty_batch_does_not_resolve_call
- test_governance_oversized_context_fails_before_provider
- test_governance_result_carries_call_fact
- test_governance_result_carries_usage_without_durable_session_projection
- test_reflection_completed_carries_call_fact
- test_reflection_completed_carries_usage_and_estimated_input
- test_reflection_failed_after_resolution_carries_call_fact
- test_reflection_failed_before_resolution_allows_missing_call_fact
- test_reflection_failure_stage_enforces_call_and_usage_fields
- test_reflection_identity_validation_failure_allows_missing_input_estimate
- test_reflection_budget_validation_failure_requires_input_estimate
- test_reflection_stream_and_later_failures_require_input_estimate
- test_reflection_run_error_drains_end_before_failed_event
- test_reflection_oversized_context_fails_before_provider

### 20.10 Event/serialization/PostgreSQL

- test_run_start_model_target_is_required
- test_model_call_start_resolved_fact_is_required
- test_model_call_end_identity_is_required
- test_model_call_end_reported_model_identity_is_required
- test_model_call_end_reported_usage_requires_fact
- test_model_call_end_missing_usage_requires_null
- test_model_call_end_usage_breakdown_round_trips_postgres
- test_context_compiled_resolved_fact_and_budget_are_required
- test_model_call_rejected_round_trip
- test_compaction_double_limits_round_trip
- test_old_model_call_event_payload_is_rejected
- test_postgres_model_call_facts_round_trip
- test_postgres_json_payload_contains_no_secret

### 20.11 Inspector

- test_inspector_joins_compiled_call_by_resolved_id
- test_inspector_allows_direct_call_without_compiled_context
- test_inspector_reports_call_fact_mismatch
- test_inspector_reports_start_without_end
- test_inspector_reports_rejected_without_start
- test_inspector_projects_run_target
- test_inspector_projects_compaction_target_and_summarizer
- test_inspector_displays_compaction_estimate_scope_and_baseline
- test_inspector_projects_per_call_usage
- test_inspector_aggregates_run_usage_by_call_purpose
- test_inspector_does_not_claim_historical_governance_usage
- test_inspector_does_not_treat_missing_cached_breakdown_as_zero
- test_inspector_does_not_recompute_historical_limits
- test_inspector_projects_accepted_reported_model_alias

### 20.12 Real LLM

- real main Agent call 的 compile/start/end fact一致；
- real provider alias/snapshot reported identity 被接受并可 inspect；
- real tool follow-up 产生新 call ID；
- real transport retry（可故障注入）保持 call ID；
- real preflight compaction target与RunStart一致；
- real summarizer target/call可 inspect；
- real governance/reflection direct call不绕过 validator；
- event log/inspect 不出现 API key 或完整 secret endpoint；
- 小窗口故障注入在 provider 前 fail closed。

## 21. Hard-cut rollout

### 21.1 不做双 API 期

一个合并点内删除：

- LLMRuntime.stream(role/options)；
- LLMTransport.stream(model/options)；
- build payload 的 model/options 独立参数；
- transport ModelCallStart emission。

### 21.2 不做 optional event fields

新 schema required。

不得写：

~~~python
resolved_call: ResolvedModelCallFact | None = None
~~~

除非该 event 明确允许 pre-resolution failure，并有 failure_stage validator。

### 21.3 数据

开发阶段建议：

- reset PostgreSQL/Oxigraph test/dev data；或
- 运行一次明确的 hard-cut migration。

runtime 不读取旧 payload并补默认值。

### 21.4 配置

部署前必须补 pro/flash limits。

README/.env.example 不应提供声称适用于所有 provider 的猜测值。示例应标明：

- 这些值必须与实际 model/provider contract 对齐；
- 错误配置由用户负责；
- Pulsara 只验证内部一致性，不联网猜模型规格。

### 21.5 测试 fixture

集中 builder 可以提供小窗口默认测试值，例如：

~~~text
total=4096
max_input=3584
max_output=1024
default_output=512
safety=128
~~~

这些只是 pytest fixture，不得作为 production default。

### 21.6 Grep gate

PR5 验收运行：

~~~bash
rg -n "256_000|8_000|200_000|8_192|reserved_output_tokens|chars_per_token|event_chars_per_token|estimate_safety_margin|summary_max_output_tokens|ResolvedModelBudgetFact|target\.budget|stream\(.*role" src/pulsara_agent
~~~

允许保留与模型预算无关的 artifact/tool char thresholds，但必须通过命名/注释证明不是 model context truth。

## 22. 明确非目标

本章不做：

- 全面 ContextSource hard cut；
- immutable ContextFactSnapshot；
- tool-result renderer ownership 重写；
- async PostgreSQL event writer；
- governance canonical mutation 与 event append 同 UOW；
- provider 精确 tokenizer 集成；
- 在线模型 catalog；
- 自动发现 provider window；
- multi-model fallback；
- speculative decoding；
- 多段/chunked compaction summarization；
- 动态按价格选模型；
- run 内模型切换；
- transport retry 架构重写；
- memory governance event transaction 重写；
- run/session/goal rollout budget；
- 固定 model-step/tool-call soft limit；
- no-progress、重复搜索或低信息增益检测；
- tool-disabled forced finalization；
- autonomous run wall-clock deadline；
- root agent/subagent shared execution budget coordinator。

这些后续章节必须复用本章 target/call contract，不能再创建第二套 limits。

## 23. 完成定义

本章只有同时满足以下条件才算 hard-cut 完成：

1. pro/flash model slots 的 limits 全部 required；
2. ModelProfile 不再含 optional context_window/max_output truth；
3. compiler 中没有 256000/8000 model context budget；
4. compaction 中没有 256000/200000/8192 model context budget；
5. LLMRuntime 没有 stream(role/options)；
6. transport 没有独立 model/options 参数；
7. transport 不写 ReplyStartEvent、ModelCallStartEvent、ModelCallEndEvent 或 ReplyEndEvent；
8. RunStartEvent 保存 run target fact；
9. ContextCompiledEvent 保存 resolved call 与完整预算；
10. ModelCallStart/End 通过 call ID稳定 join；
11. ModelCallEnd 的 reported usage 保存结构化 ModelTokenUsageFact；provider 未报告 usage 时保存 missing/null，而不是零；
12. network retry 不重复生成 canonical usage fact；
13. compile retry 复用 call ID；
14. tool follow-up 使用新 call ID；
15. final payload 在 provider 前用同 estimator重新校验；
16. governance/reflection/compaction 不绕过 validator；
17. compaction target与summarizer limits完全分离；
18. manual compaction不伪造主 call；
19. inspector以 resolved_model_call_id join；
20. inspector 能按当前 durable facts投影 agent/compaction/reflection 历史 usage，不虚构 governance history，也不推导 execution pressure；
21. direct side call不误报missing ContextCompiled；
22. old event logs不被 runtime静默兼容；
23. unit/integration/PostgreSQL/real LLM tests通过；
24. event/diagnostic/inspect不泄漏 API key或secret endpoint；
25. ARCHITECTURE_DEBT_AUDIT.zh.md 中该项更新为完成并指向本实施记录。
26. caller 显式请求 provider 禁止的 option 时 target resolution 失败，不存在 silent omission；
27. early renderer/section pressure 使用 staged budget report，不把未计算值写成零；
28. direct side call 遇到 RunError 后 drain 到 ModelCallEnd，保留 outcome/usage；
29. compiler/pre-send estimate 任何非零 mismatch 都在 ModelCallStart 前拒绝；
30. compaction estimate明确标注 compiled_context_baseline 或 transcript_only，前者只表达基于 basis compiled context 的 predicted result，后者不虚构 post-target success；
31. target fingerprint包含 transport binding id/version、model identity policy，并使用冻结的 endpoint/request-shape canonicalization。
32. provider-reported model identity 与 requested route id 分离：默认 accept_reported 接受
    alias/snapshot 并持久化 observation；exact 作为显式 opt-in；不实现 allowlist。

## 24. 下一大章衔接

本章完成后的顺序保持：

~~~text
ResolvedModelTarget / ResolvedModelCall / model context limits
    -> Async LiveRuntimeEventWriter + governance UOW
    -> immutable ContextFactSnapshot + normalized transcript/tool-result units
    -> ContextSource hard cut
~~~

原因：

- event writer 需要可靠的 model-call durable fact schema；
- immutable compile snapshot 需要稳定的 target/call/context budget 输入；
- ContextSource 只有在最终模型上下文预算真实可执行后，才有可靠的 source allocation contract。

本章不是 ContextCompiler 重写的终点，但它删除了后续所有 compiler 重构最危险的第二预算真源。

未来若真实使用数据证明需要 rollout/execution budget，应另立独立章节，并遵守：

~~~text
committed ModelCallEndEvent.usage
    -> RunExecutionBudgetPolicy / RolloutBudgetCoordinator
    -> optional pressure / stop decision
    -> AgentRuntime / Host execution control
~~~

该 coordinator：

- 可以按 run/session/goal 或 root/subagent tree 累计 usage；
- 可以按 ModelCallPurpose 选择不同权重；
- 必须把 network retry 视为同一个 call；
- 不得修改历史 ResolvedModelCall fact、target fingerprint 或 context budget；
- 不得因 context compaction 清零累计 usage；
- 不得把长程 tool/process 的等待时间直接等价成模型递归；
- 最好在 Async LiveRuntimeEventWriter 建立 committed usage stream 后实施共享/durable accounting。

本章只提供可靠 call identity 与 usage facts，不提前承诺或实现该策略。
