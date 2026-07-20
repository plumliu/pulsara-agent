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
  -> RawLLMTransport(api)
  -> adapter-private RawProviderStreamItem
  -> SanitizingLLMTransport
  -> ModelStreamCoalescingCoordinator
  -> durable segment/singleton events
```

所有provider adapter必须把外部stream翻译为不继承`EventBase`、不携带run/turn/reply/sequence且不进入serialization registry的closed `RawProviderStreamItem` union。SDK原始事件不得泄露给runtime；adapter也不得直接构造任何durable `AgentEvent`。只有`SanitizingLLMTransport`可以把raw item变成versioned semantic draft，只有`ModelStreamCoalescingCoordinator`可以决定durable segment布局。

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

- reasoning effort。

Temperature、reasoning summary与per-call output cap均不是supported option。Effective output cap固定来自resolved model slot的`default_output_tokens`并显式进入resolved budget fact与adapter payload。

Provider-specific 参数必须走 `ProviderProfile.request_defaults` / `request_extra_body` / thinking profile，不得塞进 runtime loop。

---

## 5. Reply envelope

`LLMRuntime` 必须包裹每次 transport stream：

```text
REPLY_START
  MODEL_CALL_START
  runtime-owned block singleton/segment events...
  MODEL_CALL_END
REPLY_END
```

`LLMRuntime`是Reply/Model完整lifecycle、semantic commit与terminal projection的唯一owner。Transport不产生`MODEL_CALL_START/END`、`REPLY_START/END`或`RUN_ERROR`。Provider failure先成为sanitized `ProviderErrorDraft`，再由runtime持久化`PROVIDER_MODEL_STREAM_ERROR` singleton并生成provider-error terminal batch。

---

## 6. Event translation

Provider adapter必须产出typed raw items：block start/end、text/thinking/data/tool-call delta或raw failure。OpenAI Responses与Chat Completions差异只能存在于adapter内部。

Sanitizer采用prepare/adopt receipt协议；每次最多有一个completed-but-not-adopted envelope。Coordinator把相同`kind + block/tool-call identity + media type`的连续delta聚合为以下durable non-transcript events：

- `TEXT_BLOCK_SEGMENT`
- `THINKING_BLOCK_SEGMENT`
- `DATA_BLOCK_SEGMENT`
- `TOOL_CALL_ARGUMENTS_SEGMENT`

Block Start/End和provider error仍为singleton。四类旧`*_DELTA` AgentEvent、EventType、decoder和生产兼容facade均已物理删除。

Tool call id 规则：

- Responses adapter 必须正确处理 provider item id 与 call id 的映射。
- Chat Completions adapter 必须缓存 arguments，直到 tool call id/name 到齐。
- Provider item ID、tool call ID 与 tool name 在首个 named Start 时冻结；后续 delta/done/chunk 不匹配必须 fail closed。
- 多个 active tool call 的隐式关闭顺序必须等于 provider Start 顺序，不得使用无序集合决定 durable End 顺序。
- Tool call arguments可以按任意JSON string fragment进入raw transport；Coordinator只做lossless字符串拼接，不在segment层解析JSON。最终terminal projection一次性执行canonical arguments解析。
- arguments在具有非空tool name的typed Start之前到达时必须fail closed；不得以空name构造Start或从arguments正文猜测tool identity。

OpenAI Responses必须把`response.output_text.done`、`response.reasoning_summary_text.done`和`response.reasoning_text.done`翻译为对应typed End。`done`中的完整text/reasoning/tool-arguments同时是lossless reconciliation authority：无先前delta且内容非空时生成唯一typed delta；已有delta时累计内容必须与final payload逐字节相等，否则稳定fail closed。`response.completed`只补闭合仍active的正常block；provider failure、SDK/network exception与cancel不得调用该补闭合路径，仍open的block只能在terminal projection中成为`interrupted`。

---

## 7. Usage

`ModelCallEndEvent` 是 usage 的 runtime event 边界。

Usage 字段：

- `input_tokens`
- `output_tokens`
- `total_tokens`

Responses usage (`input_tokens` / `output_tokens`) 与 Chat usage (`prompt_tokens` / `completion_tokens`) 必须归一化到同一个 `Usage` shape。

缺失 usage 时必须写 `usage_status="missing"` 且 `usage=None`，不得伪造零 token；`MODEL_CALL_END` 本身仍为 required。

---

## 8. Retry

Provider transport 可以重试 transient LLM failure，但必须满足：

- retry config 由 `LLMRetryConfig` 控制；
- `attempts` 上限为 32，与 durable retry summary 的有界 schema 一致；
- 只在尚未产生 semantic output 前重试；
- semantic output 包括 text、thinking、tool-call delta、run error；
- retry attempt 只属于 adapter-private operational state，可以写脱敏日志，但不得产生 durable `llm.retry`/`CustomEvent`；
- 成功 retry 不持久化 per-attempt history；
- 最终失败只通过 `ProviderModelStreamErrorEvent.error.retry_summary` 保存 versioned `ProviderRetrySummaryFact`；该 summary 只包含 bounded stable reason/status/delay/retry-after、final/exhausted/skipped 状态，禁止 raw exception message/repr、URL、response body、provider data 与 secret；
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
- pre-output failure 可在 adapter 内 retry，成功时不产生 durable retry event。
- post-semantic-output failure 不 retry，并解释 skipped reason。
- retry exhausted 的最终 durable provider error包含bounded、脱敏、可fingerprint校验的retry summary。
- owned SDK client stream 后关闭一次。
- compaction / side LLM 遇 `RUN_ERROR` fail closed。

---

## 14. Model stream ownership 与 rollout settlement hard cut

production registry 只能暴露 `SanitizingLLMTransport`。raw adapter execution 不得直接交给 `LLMRuntime`；provider/SDK/HTTP/network
exception 必须在 wrapper task 内转换为 versioned `ProviderErrorDraft`，完成 physical drain 后再给出 terminal draft。sanitizer failure 只能
使用预构造 constant fallback，raw exception、traceback、URL credentials、headers/cookies 与 response body不得进入日志、event、artifact、
Future exception或exception chain。

`LLMRuntime`通过service-owned `ModelStreamExecutionHandle/Registry`和唯一`ModelStreamCoalescingCoordinator`独占完整stream：`commit_start()`、按contiguous durable semantic index的`commit_semantic()`、以及原子`commit_terminal()`。Coordinator同时且至多拥有一个transport read、一个open segment、一个bounded sealed batch、一个foreground commit attempt、一个confirmed source cursor和一个confirmed durable-event cursor。public subscription只观察committed notifications；subscriber break/cancel/lag只detach，不能停止transport、截断canonical result或接管确认。Agent/direct/window summarizer只从EventLog hydrate已确认terminal projection。

Source receipt由sanitizer生成`before -> after` commitment chain；durable segment保存连续source span和before/after，不保存每个raw draft fingerprint tuple。Replay验证chain continuity与terminal final accumulator，但不把producer receipt伪装成可由丢弃raw item独立重算的proof。

Segment的soft target为text/thinking/tool-call约`32,768` codepoints（约8K估算token）并同时受`64 KiB` UTF-8 target约束；data使用`64 KiB` UTF-8 target。所有segment还受`128 KiB` content、`4,096` source items、`256 KiB` canonical event与`1s` oldest-unconfirmed age硬约束。append前必须按JSON escaping后的prospective canonical candidate sizing，必要时先seal；不得等构造超限event后才失败。Segment seal与transaction commit相互独立，block End只seal，不天然成为独立transaction barrier。

read、age deadline与cancel由唯一arbiter排序：deadline早于envelope accepted time时先seal已有prefix；read在cancellation linearization前完成时先adopt；cancel先赢时只以已adopted/confirmed prefix terminalize。并发到达使用monotonic stamp和ordinal稳定tie-break，最多暂存一个completed-but-not-adopted envelope。一次adopt transition生成的完整candidate tuple必须在sanitizer acknowledgement及任何await前同步归Coordinator所有；batch满时尚未attempt的后续candidate也必须由execution handle保留，不能停留在worker栈帧。

terminal candidate一旦由provider terminal draft确定就不可改写。confirmed `NONE`必须使用同一组stable event IDs与相同payload重试；
terminal projection artifact也属于该stable candidate：caller cancellation等待原physical write，瞬时pre-commit failure重试相同content-addressed bytes；waiter必须区分caller cancellation与owned physical task真正cancel，cancel-after-physical-FULL必须消费原成功值；conflict/confirmation drift必须latch，禁止切换terminal outcome；
不得把`completed/provider_error`改写成`runtime_error`。`PARTIAL/UNKNOWN`保留owner并latch。Materialization按
`resolved_model_call_id`做bounded canonical query，由RuntimeSession-owned I/O service执行；不得在event loop中同步
`tuple(event_log.iter())`。worker task遭裸cancel时也必须取消并等待exact read task退出，再请求transport close并确认physical completion，之后才决定terminal；
physical operation退出前不得移除tracking或退休handle。

普通writer/数据库异常也必须先confirm同一stable batch：`NONE`进入上述原字节retry，`FULL`adopt durable winner，`UNKNOWN/PARTIAL`latch。singleton在seal已有segment之前必须完成DTO、fingerprint、canonical-byte与hard-cap验证；任一准备/最终seal失败均不得推进source或durable cursor。terminal commit还必须将hydrated source fact与live source count/accumulator/durable count、`ModelStreamSettlementMeasurementFact`以及当前segment/domain/reducer contract binding逐项join。每个semantic commit measurement保存ordered event identities、writer-prepared actual candidate bytes与exact physical charge identity；actual bytes必须等于同批charge fact的`business_candidate_charge_payload_bytes`，不得使用session metadata overlay前的prospective sizing值。terminal source与`PhysicalOperationSettlementFact`必须join同一measurement fingerprint。Inspector只展开bounded aggregate measurement。

writer cancellation的physical result由model commit port消费：`FULL` adopt、`NONE` retry、其他physical error保留原错误、UNKNOWN/lost result latch；不得把它作为裸transport-worker cancellation继续terminalize。

main model start 与 rollout reservation 同批提交；terminal batch必须包含 ModelEnd、精确 reservation settlement，以及 main reply 的 ReplyEnd。
missing usage 按 frozen physical quote结算；reported input高于 estimate 但不超过 physical bound属于合法 measurement。request cancellation必须
打断 blocked read并等待 exact transport physical completion；physical state为 `BLOCKED_UNTRUSTED` 时禁止伪造 terminal、释放 owner或越过
Host teardown。

`ProviderModelStreamErrorEvent`使用独立canonical EventType和historical decoder。每个durable singleton/segment保存call/start、durable index、source span、receipt before/after与policy attribution；segment schedule/layout只属于fact/audit identity，不进入terminal provider semantic identity。Responses function-call同时缺少provider `call_id`与item identity时必须以`transport_tool_call_identity_missing` fail closed，禁止随机生成tool-call ID。transport不得产生stored event、Reply/Model lifecycle event、任意JSON semantic draft或第二套writer。

---

## 15. Typed provider-user carrier hard cut

Provider-visible privileged input只有一个generation-root system/instructions carrier。任何动态history item都不得使用`system`或
`developer`。Provider-neutral input将user-wire内容分成三个互斥owner：

- `MessageRole.USER`：human-authored正文，只能编码为`pulsara_human_input`，原文位于escaped typed `text`字段；
- `MessageRole.RUNTIME_REQUEST`：runtime创建的当前任务，例如subagent、compaction、governance、reflection与summarizer请求，编码为
  `pulsara_runtime_request`；
- `MessageRole.RUNTIME_OBSERVATION`：memory、clock、capability、plan、recovery、rollout、subagent/MCP事实与lifecycle observation，编码为
  `pulsara_runtime_observation`。

三者在Chat和Responses最终wire role都为`user`，但内部role、typed outer envelope、semantic identity与attribution不得合并。Root
system/instructions必须包含三分carrier interpretation contract；contract fingerprint进入generation compatibility。Adapter发送前必须验证：

- Chat恰有一个index-0 system，后续messages没有system/developer；
- Responses instructions与generation root一致，input没有system/developer；
- 每个user-role item恰好解码为一个registered outer envelope，unknown kind/version/producer/codec fail closed；
- 每个typed user item必须同时携带其 `ProviderUserCarrierBindingFact` 与nested semantic fact；adapter pre-send从exact wire bytes、occurrence identity和closed registry重新编码，逐字段比较semantic fact并逐字节比较canonical wire。仅把wire中的semantic ID改成另一个格式合法的SHA仍必须fail closed；
- runtime request不进入observation stable state或Long-Horizon observation rewrite；
- human正文即使包含完整runtime envelope JSON，也只能留在`pulsara_human_input.text`，不能伪造runtime authority。

Runtime observation的内层`payload`同样是closed typed union，而不是任意JSON mapping或字符串fallback。每个registered kind必须绑定唯一historical DTO schema及schema fingerprint；clock、ContextSource append/replacement、transcript lifecycle、derived text与Long-Horizon rewrite projection分别由中央factory构造，unknown/mismatched/extra payload fields在adapter前fail closed。Replacement revision与predecessor lineage属于typed replacement payload；raw event ID、sequence、artifact locator与ledger high-water只属于attribution。

每个 model call还必须拥有一个frozen runtime clock。Session-window call由compiler/source
candidate生成；direct、governance、reflection、compaction、window compaction与summarizer
one-shot generation在initial append中生成。One-shot clock occurrence绑定
`operation_kind + operation_id + attempt_index + observed_at_utc`，位于runtime request之前；
retry/reopen复用同一prepared carrier，不得重新读取当前时钟。

`ProviderVisibleInputCompatibilityFact`必须覆盖完整`ProviderUserCarrierProtocolContractFact.contract_fingerprint`，而不只覆盖root解释文本。Human/request/observation protocol、kind/producer registry、typed payload schema或codec任一变化，都必须触发合法generation rollover；不得在旧generation中静默改变wire grammar。

Resolved target继续冻结完整carrier binding，validator/adapter不得从当前配置补造wire role。Raw user文本、legacy
`pulsara_context`、generic runtime-user wrapper与动态privileged hint均不是production input。

---

## 16. Memory governance exact model input contract

Memory governance仍使用普通 session-owned `LLMRuntime` model-stream lifecycle，不拥有旁路
transport或caller writer。它的 `ResolvedModelCallFact`、target fingerprint与 exact
`LLMContext`必须在 `MemoryGovernanceBatchPreparedEvent` 之前冻结进 content-addressed batch
artifact。

Prepared FULL是 governance ModelStart的必要前置。ModelStart必须携带
`GovernanceModelInputAttributionFact`，并逐项 join Prepared event identity、batch input artifact
reference、resolved model call ID、target fingerprint与最终 model-visible input fingerprint。
同一 batch不得在 Prepared前启动模型，也不得在恢复时用当前配置产生新的 call ID、system
prompt、estimator结果或messages。

Governance execution owner与 model stream owner是两层明确 ownership：前者拥有 claim、artifact、
Prepared、decision suffix与batch terminalization；后者只拥有一次已冻结 call的provider stream。
Caller cancellation只detach governance waiter；已启动的 model stream继续由其 registry完成
transport drain、terminal projection和settlement。Start-without-End按通用 model stream recovery
修复；Prepared-without-Start按 governance preparation artifact原样启动。

Governance prompt只消费 typed bounded evidence projection和relatedness projection，不包含 raw
`source_events`、model stream segment、SDK/provider payload或序列化 tool-result semantics推断。
Prompt feasibility必须用冻结 target的实际 estimator验证完整 system prompt + messages + wrapper，
不得用 UTF-8 bytes直接冒充 token budget。

---

## 17. Provider input causal order 与 ModelStart join

主agent的transport输入只能来自已确认的`CommittedProviderInputReferenceFact`。Context compiler先在
`ContextInputManifest` artifact内冻结唯一ordered provider projection；ProviderInput planner不得重新
按`current_user | prior_history | current_run_tail`分类、插入pending continuation或二次排序，只能验证
causal edges并计算相对于committed frontier的strict suffix。

`current_user | prior_history`属于invocation attribution，不进入provider unit continuity semantic。一个
generation中已提交unit的wire fragment、causal predecessor、projection-local ordinal与semantic fingerprint
永久不变；追加新末节点不得因successor变化改写旧节点。ContextSource frame只能插在本次prior transcript与
current user之间，并携带preceding/following transcript identity及exact vector ordinal range proof。

Generation coordinator只准备stable append/rollover companions与commit guard。`LLMRuntime`继续唯一拥有
`ModelCallStartEvent`，并在一个atomic lifecycle batch中提交append companions、rollout reservation与
ModelStart。ModelStart必须join committed generation/core revision、prefix/vector root、ordered projection、
causal validation、transcript frontier、frame placement与authority-horizon roots；adapter dispatch前重算
deep-copied carrier的wire semantic identity。retry必须复用同一prepared candidate，不能按当前context重建。

Pending accepted continuation不是第二个message producer。它必须按resolved call、reply、terminal projection
与ACCEPTED disposition精确join ordered projection中的唯一unit；尚未出现时`not_ready`，缺失、重复或semantic
漂移时fail closed。普通run/window boundary、invocation classification变化和attribution-only drift不得触发
rollover。只有typed compatibility change、confirmed Long-Horizon rewrite或confirmed offline repair authority可以开启新generation。
`auxiliary_frame_rebase`已物理删除；observation缺席、旧frame数量、cache miss或token节省猜测均不能授权rollover。
