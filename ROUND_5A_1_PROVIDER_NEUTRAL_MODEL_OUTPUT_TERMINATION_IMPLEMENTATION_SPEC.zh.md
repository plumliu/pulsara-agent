# Pulsara Round 5A.1：Provider-neutral Model Output Termination 与 Same-epoch Reasoning Continuation 实施规格

> 状态：**ACTIVATED**
>
> 激活日期：2026-08-17
>
> 实施检查点：`e375b5be3a493cf42ec6a7d4aed3392b937935d1`
>
> 机器证据：[round5a1_provider_neutral_model_output_termination_activation.json](benchmarks/suites/core/v1/round5a1_provider_neutral_model_output_termination_activation.json)
>
> 上位架构：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 产品能力索引：[POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 前置实现：[Round 3 compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 7 model-visible outcome/timing](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)
>
> 后续修订（2026-08-19）：[Round 5A.2 durable provider replay](ROUND_5A_2_DURABLE_PROVIDER_REPLAY_AND_CROSS_RESTART_THREAD_CONTINUATION_IMPLEMENTATION_SPEC.zh.md)已ACTIVATED，并接管本文关于`ProviderAssistantReplayFragment`生命周期、存储、Host loss/cold reset与跨进程续接的规范。本文的terminal、whole-response atomicity、closed Chat fields、Responses item allowlist、wire plan与assistant settlement owner继续有效；“fragment仅process-local、Host loss后generic semantic rebuild、repository不得存储fragment”只描述5A.1激活时的历史实现，不再是后续目标契约。
>
> OpenAI function-tool wire兼容修订（2026-08-19）：Chat与Responses的全部function tool统一经过同一个provider-neutral adapter，显式发送`strict: false`，并按OpenAI官方root-object约束把根级object union确定性lower为单一object schema；nested `oneOf`使用`anyOf`、`const`使用单值`enum`。provider wire只允许成为canonical schema的安全超集，不能静默收窄`additionalProperties`或覆盖组合约束；无法诚实lower的MCP tool必须在discovery阶段按既有`FAIL_SERVER | OMIT_INVALID`策略处置，不能进入已安装surface后毒化整次dispatch。canonical frozen descriptor与本地strict parser保持唯一执行真源，wire lowering不按provider或tool name分支。Responses还允许一种byte-exact stream/final reconciliation：`reasoning_text`流只有在没有独立summary流、没有final reasoning content且其正文与final summary完全相等时，才可作为该summary的transport alias；任何冲突仍在assistant acceptance前fail closed。Chat closed carrier的`final_value_required`对每个completed response无条件生效，不再受旧`NEVER | WHEN_TOOL_CALLS | ALWAYS` replay policy影响；因此缺少final confirmation的stream carrier不能随assistant/tool call一起被接受。
>
> Round 5B compaction在激活前必须同时依赖本文与Round 5A.2。compaction summary只有在本文定义的完整provider terminal后才可成为adoption candidate；重启后的summary prefix由Round 5A.2恢复，adoption后旧floor以前的durable replay不再进入active epoch。
>
> 本文统一两条并列边界：provider输出的完成、截断、失败、重试与canonical acceptance；以及**完整响应**中的reasoning如何在同一Host、同一scope、同一provider-input epoch内原样进入后续调用。5A.1激活切片本身不实施compaction、半截答案自动续写、provider-error reactive compaction、remote response ID continuation或hidden reasoning durable persistence；其中最后一项现由Round 5A.2窄化恢复，而不恢复通用durable recovery machinery。

---

## 0. 执行结论

Pulsara已经拥有正确的canonical大边界：一个assistant message只有在整次provider stream收集完成后，才会作为ordered blocks原子提交；ToolExecutionAttempt又只能在该assistant entry接受以后创建。因此当前Kernel天然适合采用**whole-response atomicity**：

```text
provider response COMPLETED
    -> validate every completed block
    -> atomically accept one assistant message
    -> only then authorize/accept/invoke tools

provider response INCOMPLETE or FAILED
    -> abort the live draft
    -> accept no assistant message
    -> create no tool attempt
    -> execute no physical tool
```

当前真正缺少的是adapter终止语义：

- Chat Completions只对`finish_reason == "tool_calls"`做特殊处理；`length`、`content_filter`与未知finish reason没有被显式拒绝。stream正常耗尽后，adapter会无条件关闭active blocks，normalized transport再把EOF推断成`COMPLETED`。因此一个被token limit截断的最终正文，甚至一个参数JSON尚未完成的tool call，存在被包装成正常完成的风险。
- Responses把`response.incomplete`、`response.failed`与transport error全部降为同一个generic provider error。它虽然不会错误接受，但丢失了“模型输出不完整”与“物理provider失败”的关键差别。
- normalized transport只有`COMPLETED | PROVIDER_ERROR`，并把“raw adapter iterator正常耗尽”当作成功；adapter没有义务发出唯一、显式terminal marker。
- DirectModel把非COMPLETED terminal压成普通`RuntimeError`；Runner无法用typed cause决定turn terminal reason、operational observation或future compaction summary的discard规则。
- 当前default output为8,192 tokens，且Chat/Responses都会把resolved effective output cap发送给provider。截断不是理论边界，而是正常multi-provider部署必须正确表达的终止结果。

还有一条与Round 5B同样直接相关、此前被当前canonical设计遗漏的边界：**完整响应中的provider reasoning carrier。**当前assembler把Thinking只留在live plane，canonical assistant blocks与后续compiler input都没有它；Chat adapter虽已有`reasoning_content` replay seam，compiler却永远不给它thinking；Responses adapter只把reasoning summary投影为live text，没有保存或重放ordered reasoning output item。结果是：

- DeepSeek式Chat tool loop要求历史assistant tool call连同`reasoning_content`原样回传；省略会直接得到HTTP 400；
- Qwen式chat template会根据最后一条真实USER的位置决定是否渲染旧thinking；错误地把tool observation编码为USER会切断current-turn reasoning；
- OpenRouter Chat返回的可能是结构化opaque `reasoning_details`，不是单一字符串；当前adapter会丢失；
- OpenAI-compatible Responses可能返回带`encrypted_content`的reasoning output item；手工管理history时应原样重放，而不是把summary text伪装成hidden reasoning；
- provider端`previous_response_id`在不同endpoint上可能真正有状态、完全不支持，或仅特定transport支持，不能成为Pulsara正确性的共同基础。

Round 5A.1冻结：

1. **adapter必须显式发送唯一terminal marker；raw EOF永远不再代表成功。**
2. provider-neutral terminal为`COMPLETED | OUTPUT_INCOMPLETE | PROVIDER_ERROR`。只有COMPLETED允许canonical assistant acceptance。
3. OUTPUT_INCOMPLETE具有closed reason：`OUTPUT_TOKEN_LIMIT | CONTEXT_WINDOW_LIMIT_DURING_GENERATION | CONTENT_FILTERED | UNKNOWN_PROVIDER_INCOMPLETE`。
4. Chat `finish_reason=length`与Responses `response.incomplete`不再冒充成功，也不再与transport failure混为一谈。
5. **整次assistant response保持原子。**即使Responses在最终incomplete前发出一个完整`output_item.done(function_call)`，该tool call也只是一条provisional live block；整次response未COMPLETED时，绝不接受assistant entry、创建attempt或执行工具。
6. partial text/data/tool arguments与thinking只存在于disposable live plane；incomplete generation不会形成canonical partial assistant row，也不会被重新送给模型。
7. OUTPUT_INCOMPLETE不自动重试。同样的input、tool surface与output cap重跑既可能重复正文，也可能生成不同的effectful tool call；零delta也不改变这一规则。
8. 现有transport retry只允许在没有任何semantic output且没有收到terminal时处理retryable physical failure；OUTPUT_INCOMPLETE不是transport failure。
9. provider stream或compaction summary发生incomplete时不得触发reactive compaction。Round 5B仍只拥有manual、proactive threshold与mid-turn safe-point三个入口。
10. compaction summary、memory governance、Cheap Hint等auxiliary call只有COMPLETED terminal才能解析结果；incomplete result一律discard。
11. provider usage/cost observation可以在INCOMPLETE或PROVIDER_ERROR terminal上best-effort记录，但usage不能把语义失败提升为成功。
12. 本轮不调整默认8,192 output tokens，也不复制Codex“普通请求省略max_output_tokens”的第一方假设。Pulsara继续显式发送output cap，以便compiler为输出保留headroom；数值政策以后独立调整。
13. **只有COMPLETED response才能产生reasoning continuation。**INCOMPLETE、PROVIDER_ERROR、cancelled或physical BLOCKED response中的thinking仍是disposable live output，绝不进入下一次provider input。
14. 在5A.1激活实现中，reasoning continuation是wire-contract-bound、process-local、same-epoch的opaque carrier。Chat使用provider-neutral closed registry：`reasoning_content: TEXT_CONCAT`、`reasoning: TEXT_CONCAT`、`reasoning_details: ORDERED_ARRAY_APPEND`；Responses继续使用typed ordered output items。两者分别原样保留，不互相转换，不写入canonical assistant正文。Round 5A.2只把completed、entry-bound carrier提升为独立durable provider-replay row。
15. reasoning carrier只有在对应assistant entry canonical FULL/compatible confirmation以后才可安装；transient NONE只允许exact retry/confirmation，最终abandon或CONFLICT必须丢弃。安装以后，它成为该epoch provider wire prefix的一部分，后续调用只能原样保留并追加suffix，不能因新USER、tool loop或policy重算而删除/改写。
16. 手工重放完整provider history是mandatory conformance path。`previous_response_id`、provider session ID与server-side state不进入canonical truth，不承担crash recovery或correctness；adapter以后可把它们作为可丢失优化单独讨论，但本文不实现。
17. normal ToolResult继续使用真实`role=tool`。禁止为了保住reasoning把tool observation伪装成USER，也禁止把`<think>...</think>`塞进普通assistant content冒充provider reasoning。
18. capability由resolved wire API与replay codec决定，不用vendor name写分支。Chat只接受上述三个known carrier；未知empty/null字段是no-op，未知non-empty字段若伴随完整普通final text则不回传，若伴随tool continuation或成为唯一输出则在assistant acceptance与tool dispatch前typed fail closed。Responses仍按closed typed item allowlist验证。
19. reasoning payload计入provider-wire quote、epoch logical bytes与compaction trigger/quote；不允许silent truncation。它复用现有completed-response assembly、compiled working-set与64 MiB epoch hard bound，不新建独立大缓存上限。
20. Round 5B summary使用旧epoch的exact SYSTEM/tools/messages及已安装reasoning carrier；summary正文不得复制或显露hidden reasoning。adoption/cold rebase以后，旧floor以前的carrier不再进入active epoch，remote response ID仍全部丢弃；Round 5A.2 durable row可以保留为历史subordinate fact，但不会越过snapshot floor重新注入。
21. continuity CAS唯一安装`FrozenProviderWireInputPlan`证明的semantic view与actual wire proof；candidate注册、preflight与physical open不得各自重新materialize input。
22. assistant commit由新增的process-local shielded settlement attempt拥有；caller cancellation只能detach，只有FULL并完成optional fragment binding以后才能进入tool path。
23. 5A.1激活切片不新增table、relation、Committed/Live event、subject、append guard、durable job、Protocol message、receipt、checkpoint、projection、repair或durable replay owner；其历史activation oracle保持`31 / 23 / 13 / 2 / 25 / 1`。Round 5A.2后续新增一张subordinate replay relation，不改写本轮历史证据。

最终边界为：

```text
vendor stream
    -> vendor adapter maps exact wire terminal
       and provisionally freezes exact reasoning carrier
    -> provider-neutral explicit adapter terminal
    -> normalized transport validates block/terminal protocol
    -> DirectModel preserves typed terminal cause, reasoning and physical close
    -> shielded assistant settlement obtains FULL or terminal no-winner
    -> FULL binds optional reasoning continuation
    -> next dispatch builds one FrozenProviderWireInputPlan
    -> continuity CAS installs semantic view + wire proof
    -> physical adapter opens the same plan without rematerialization
```

---

## 1. 为什么必须先于Round 5B完成

Round 5B会让当前主模型在旧epoch的完整prefix上生成compaction handoff。该summary随后可能替代大量早期provider-visible历史。若Runtime不能区分：

```text
完整summary
输出token耗尽后留下的半截summary
content filter截断的summary
stream物理断开时留下的partial text
```

那么一次provider wire差异就可能把残缺交接永久采用为新context snapshot。相比普通final answer，这个错误更严重：普通partial answer最多结束一轮；partial summary会污染后续整个epoch。

因此Round 5B不得自行用字符串、EOF或“JSON恰好能parse”猜summary是否完整。它只能依赖本文提供的provider-neutral terminal：

```text
summary terminal == COMPLETED
AND summary body passes Round 5B semantic/byte validation
    -> candidate may proceed

otherwise
    -> candidate absent/discarded
    -> old binding and old epoch remain authoritative
```

本文也解决用户此前提出的三个“生成途中”边界：

| 发生位置 | 实际含义 | 本轮/后续行为 |
|---|---|---|
| physical tool正在运行 | 此时没有provider generation；context不会继续增长 | 让exact invocation drain并结算；Round 5B只在下一provider open前的safe point compact |
| tool call/reasoning/final text生成途中触顶 | provider output不完整 | 本轮typed OUTPUT_INCOMPLETE；不在stream中途compact |
| 完整tool result使下一input过大 | 下一次compiled input pressure | Round 5B在tool batch完整结算后的safe point处理 |

Round 5B还有第二个前置：summary必须在**旧epoch真正使用过的provider wire**上生成，而不是只在可持久化的canonical transcript上生成。若当前agent在连续tool loop中依赖了上一响应的reasoning，summary call却突然删掉该carrier，则会同时发生两件事：

1. old prefix不再是append-only，前缀缓存可能在最大上下文附近失效；
2. summary模型失去当前agent刚刚形成但尚未完全体现在公开text/tool result中的工作状态。

因此本文冻结：

```text
complete response Rn
  -> canonical assistant/tool request FULL
  -> bind opaque reasoning continuation Qn
  -> next request = old exact wire || Rn/Qn || tool result || new suffix

Round 5B summary request
  -> same old epoch exact wire, including every installed Qn
  -> append one semantic compaction instruction

Round 5B adoption
  -> cold rebase
  -> Q1..Qn and any remote response IDs are deliberately discarded
  -> new epoch starts from canonical semantic handoff
```

以上是5A.1激活时的process-local边界。Round 5A.2已经明确取代“Host crash/cold reset允许失去Qn”的产品决定：completed并与assistant同事务接受的Qn会成为private durable provider-replay fact；新Host只向同一compatible target原样恢复。compaction adoption仍会让旧floor以前的Qn退出active epoch，且summary正文永不复制hidden reasoning。

---

## 2. 起草输入与代码真值

### 2.1 起草checkpoint

```text
current Pulsara
38fc8181d1abc55b123ddd346ca807ccd054dc30

Codex
6138909d6ec58b2fbe635ef973e02caecad5a5aa

grok-build
c68e39f60462f28d9be5e683d9cbe2c57b1a5027
```

起草时工作树已经包含用户拥有的Round 5B/Gap Index文档修改。coding agent必须保留这些dirty changes，记录实际implementation checkpoint，不得用本文hash覆盖并行文档编辑。

### 2.2 当前provider stream物理形状

[当前代码确认] [`ports/provider_stream.py`](src/pulsara_agent/ports/provider_stream.py)定义：

```text
adapter output
    ProviderStreamPayload
  | ProviderStreamFailure
  | TransportUsageReport

normalized terminal
    COMPLETED
  | PROVIDER_ERROR
```

adapter没有显式success/incomplete terminal。`NormalizedProviderTransportExecution.read_next()`在adapter iterator抛出`StopAsyncIteration`时：

```text
open blocks exist -> protocol error
otherwise         -> COMPLETED
```

这使“Python iterator正常退出”意外成为semantic completion authority。

### 2.3 Chat Completions当前缺口

[当前代码确认] [`chat_completions.py`](src/pulsara_agent/llm/adapters/openai/chat_completions.py)目前：

- delta期间累积text、thinking与tool arguments；
- 只在`finish_reason == "tool_calls"`时关闭tool call；
- SDK stream循环结束以后，无条件调用`close_active_tool_calls()`与`close_active_blocks()`；
- 不记录是否看到`stop | tool_calls | length | content_filter`；
- mock路径甚至不要求任何finish reason。

合法失败路径：

```text
tool call arguments delta = '{"path":"/tmp'
finish_reason = length
SDK stream EOF
-> close_active_tool_calls()
-> missing suffix is silently accepted as final JSON string candidate
-> if JSON happens to parse, assistant tool request can become canonical
```

即使partial JSON无法parse，失败也会被误描述成assembler/transport error，而不是准确的output-limit终止。

### 2.4 Responses当前缺口

[当前代码确认] [`responses.py`](src/pulsara_agent/llm/adapters/openai/responses.py)已经利用`response.output_item.done`重验最终tool arguments，这是值得保留的adapter leaf验证。但：

```text
response.failed
response.error
response.incomplete
error
```

四者都被映射成`builder.run_error(code="provider_transport_error")`。`incomplete_details.reason`没有进入closed Runtime vocabulary。

此外，`response.output_item.done(function_call)`当前会发出ToolCallEnd；如果之后收到`response.incomplete`，Runner虽会因generic error不提交assistant entry，但该安全性只是由上层异常偶然保证，adapter contract本身没有声明“item done不等于response adopted”。本文把它提升为明确不变量。

### 2.5 DirectModel与Runner当前正确边界

[当前代码确认] [`direct_model.py`](src/pulsara_agent/conversation_kernel/direct_model.py)在preflight、continuity CAS与physical open之间已有one-shot exact join；[`runner.py`](src/pulsara_agent/conversation_kernel/runner.py)在`_collect_model()`完整返回以后，才：

1. 构造canonical assistant blocks；
2. `commit_assistant_message()`；
3. 从accepted assistant blocks读取tool calls；
4. authorize/accept/invoke工具。

这意味着本轮不需要新增per-item durable adoption、assistant draft table或tool-call receipt。只需保证`_collect_model()`在完整response terminal以前绝不返回`CompletedAssistantMessage`。

### 2.6 当前output budget

[当前代码确认] [`config.py`](src/pulsara_agent/llm/config.py)默认：

```text
total context       256,000 tokens
maximum output      128,000 tokens
default output        8,192 tokens
input safety margin   8,192 tokens
```

[`resolution.py`](src/pulsara_agent/llm/resolution.py)从total context中减去effective output与safety margin，得到input budget；两个OpenAI adapter又分别发送`max_completion_tokens`或`max_output_tokens`。

本文保留这条可移植设计：provider output cap必须是resolved call fact的一部分。Codex普通Responses请求可以省略max output，是第一方backend选择，不是multi-provider adapter应复制的通用契约。

### 2.7 当前reasoning续接seam与实际断点

[当前代码确认] 当前代码并非完全没有thinking vocabulary：

- [`llm/input.py`](src/pulsara_agent/llm/input.py)的`LLMMessage`已有`thinking`；estimator与semantic fingerprint也会计量它；
- [`llm/provider.py`](src/pulsara_agent/llm/provider.py)已有`ThinkingReplayPolicy.NEVER | WHEN_TOOL_CALLS | ALWAYS`、delta fields与single message field；
- [`chat_completions.py`](src/pulsara_agent/llm/adapters/openai/chat_completions.py)能把`LLMMessage.thinking`写回一个profile指定字段；
- [`assembler.py`](src/pulsara_agent/conversation_kernel/assembler.py)明确把Thinking留在process-local live plane，不写入`CompletedAssistantMessage`；
- [`model_input/lowering.py`](src/pulsara_agent/model_input/lowering.py)从canonical assistant/tool request重建消息时没有thinking；
- [`responses.py`](src/pulsara_agent/llm/adapters/openai/responses.py)只把reasoning summary降低为live thinking，没有保留ordered reasoning output item用于下一次input。

因此当前Chat replay seam实际上没有producer；Responses则连可表达opaque item的carrier都没有。不能用“把thinking加入canonical assistant block”修复：那会把provider-private、可能加密、vendor-shaped的carrier变成durable conversation truth。本文要求把续接放在`PreparedKernelModelExecution`与Host provider-input continuity owner之间的process-local provider-wire overlay中。

### 2.8 官方Responses手工history契约

OpenAI当前[model guidance](https://developers.openai.com/api/docs/guides/latest-model)明确区分两种路径：使用`previous_response_id`延续server-held state；或由client手工管理history。后者必须重发此前user inputs与**每一个response output item**；在`store:false`或Zero Data Retention下，应重放API返回的encrypted reasoning item。

本文采用第二条作为可移植conformance path。Pulsara可以连接真正有状态、部分有状态或完全stateless的OpenAI-compatible endpoint；所以只有显式、完整、client-owned replay能成为共同正确性契约。`previous_response_id`成功只能作为endpoint能力证据，失败也不能改变canonical行为。

---

## 3. Prior art：吸收什么，拒绝什么

### 3.1 Codex

本地Codex基线确认：

- model metadata默认在context window约90%触发auto-compaction，并另行保留system/tools/output headroom；
- ordinary Responses request没有`max_output_tokens`字段；
- 只有`response.output_item.done`进入typed completed-item路径；tool argument delta本身不会执行；
- `response.incomplete`读取了reason，却统一转换成retryable-style stream error；
- compaction只有完整response才能采用，incomplete summary不会成为新history。

值得吸收：

1. **完成标记优先于delta。**partial tool arguments没有physical effect。
2. **compaction发生在safe point，不发生在physical tool或provider stream中途。**
3. **incomplete compaction output不得adopt。**

不应照搬：

1. 把所有`response.incomplete`压成generic stream error，会混淆output cap、content filter与transport failure。
2. 普通请求省略output cap依赖第一方backend，无法给Pulsara的compiler提供稳定headroom contract。
3. Codex可逐个保留`output_item.done`并在整response失败时留下已完成item；Pulsara当前canonical authority是一个atomic assistant message。为模仿该行为引入per-item durable adoption会增加复杂度与effect ambiguity，本轮明确拒绝。

证据位置：

```text
/Users/plumliu/Desktop/python_workspace/codex/codex-rs/protocol/src/openai_models.rs:391-451
/Users/plumliu/Desktop/python_workspace/codex/codex-rs/codex-api/src/common.rs:215-239
/Users/plumliu/Desktop/python_workspace/codex/codex-rs/codex-api/src/sse/responses.rs:326-345
/Users/plumliu/Desktop/python_workspace/codex/codex-rs/codex-api/src/sse/responses.rs:422-431
/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/session/turn.rs:297-369
```

### 3.2 grok-build

grok-build并非只实现xAI模型专用wire。当前基线同时拥有Chat Completions、Responses与Anthropic Messages adapter，并把vendor finish reason收敛为provider-neutral `StopReason`。其中`StopReason::Length`被提升为typed `MaxTokensTruncation`，且默认不可retry；partial reasoning/text只进入bounded trace，不进入chat state。

值得吸收：

1. **length是生成终止类别，不是transport error。**
2. **partial output可以live展示/trace，但不能冒充canonical assistant response。**
3. **同cap自动重跑不是默认恢复。**
4. compaction在tool result结算后的下一agent loop入口检查，而不是中断physical tool。

不应照搬：

1. Responses实现会在incomplete response包含function-call output时倾向把stop reason提升为ToolCalls；这会削弱整响应原子性。
2. 当前compaction实现会记录`CompactOutput.truncated`，但非空truncated summary仍可能进入后续adoption路径。Pulsara必须相反：truncated/incomplete summary永远没有candidate。
3. grok-build可把partial generation写到trace文件；Pulsara本轮不新增partial-output durable trace。现有live plane已经足够。
4. grok-build包含provider context-error reactive compact；用户已明确从Round 5B删除该入口，本文不得恢复。

证据位置：

```text
/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-sampling-types/src/error.rs:84-130
/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-sampler/src/actor/request_task.rs:495-542
/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-shell/src/session/streaming_capture.rs:1-18
/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-shell/src/session/acp_session_impl/turn.rs:1799-1831
/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-shell/src/session/helpers/session_compact.rs:219-227
/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-shell/src/session/compaction.rs:1685-1692
```

### 3.3 `retain-cot.ipynb`提供的关键证据

本轮参考的notebook为：

```text
/Users/plumliu/Desktop/python_workspace/modern_genai_bilibili/agents/apis/retain-cot.ipynb
repository checkpoint: c2edb00
notebook introducing commit: d862e7c67b7857b2344dd04570d5db56fc25889d
```

它值得吸收的不是“把CoT保存进数据库”，而是以下provider-input事实：

1. **role会改变reasoning continuity。**Qwen3.6 chat template从最后一条真实USER向后决定哪些assistant reasoning继续渲染；tool observation不打开新user turn，因此连续tool loop可以保留current-turn reasoning。把同一observation伪装成USER会得到不同prompt。
2. Qwen探针中，旧reasoning保存的标记在`role=tool`或显式`preserve_thinking=true`时可恢复；普通新USER且不preserve时返回UNKNOWN。这证明不能只看message正文相同，还要冻结template control/profile。
3. **DeepSeek tool loop具有硬wire要求。**历史assistant tool call若省略`reasoning_content`会得到HTTP 400；带回同一字段则成功。它不是可选的模型提示优化。
4. 把`<think>...</think>`写进普通assistant content可能让模型“答对”，但这只是把私有推理降格成公开文本，不是合法reasoning continuation；会泄漏内部推理、改变authority并污染canonical history，本文明确禁止。
5. retained reasoning与compaction是互补关系：reasoning维持rebase前的局部工作连续性；compaction把长期仍需保留的语义交给公开、bounded handoff。前者不能替代后者，后者也不应复制前者的hidden正文。

notebook中部分早期示例把`<think>`内联进content以观察模板差异；它们是反例/探针，不是Pulsara实施建议。

### 3.4 2026-08-16 bounded multi-provider probes

在不记录key、headers、完整prompt、hidden marker、reasoning payload或远端response正文的前提下，本轮使用现有四组endpoint配置进行了bounded empty-tool replay与stable-prefix cache探针。结论如下：

统一方法为：构造约6–7K tokens的稳定前缀；获得一个只调用本地空操作virtual tool的complete assistant response；在允许显式carrier的wire上把一次性marker隔离在reasoning field、而不放进tool arguments/public answer；随后分别用full manual replay、stripped-reasoning control与endpoint支持时的ID-only continuation询问marker。opaque encrypted-item路径则原样转发返回item，不尝试注入或解密。每个请求都有finite timeout与低output cap，没有真实工具effect。

| Endpoint/API | 返回的reasoning carrier | 手工续接结果 | remote ID结果 | Pulsara结论 |
|---|---|---|---|---|
| DeepSeek Chat | text `reasoning_content` | 带回成功；省略的tool-loop请求HTTP 400 | n/a | Chat text replay是该profile的必需contract |
| DeepSeek Responses | reasoning item无可复用opaque body | full history语法可接受，但不能从返回item恢复hidden marker | id-only tool continuation失败 | 不因endpoint叫Responses就假设reasoning可续接；当前优先Chat |
| DashScope Chat | text `reasoning_content` | 带回可恢复；剥离后UNKNOWN | n/a | Chat显式replay是portable path |
| DashScope Responses | returned reasoning item近似空壳 | manual history可调用 | id-only continuation能保留仅初始prompt出现的marker | server state真实存在，但不作为Pulsara correctness |
| OpenRouter Chat | structured opaque `reasoning_details` | closed registry按ordered array原样累积并手工重放 | n/a | 使用与provider名称无关的`reasoning_details: ORDERED_ARRAY_APPEND`，不能压成字符串 |
| OpenRouter Responses (`store=false`) | opaque reasoning item含`encrypted_content` | full-history replay成功 | non-null ID被拒绝 | Responses exact item replay最合适 |
| bobdong GPT-5.4 Responses | reasoning item含`encrypted_content` | manual full-history replay成功 | HTTP ID被告知仅特定WebSocket版本支持 | 视为stateless/manual replay profile；proxy行为不是规范真源 |

同一批probe还观察到：

- DeepSeek continuation约有7.1K cached input tokens；
- DashScope约6.2K；
- OpenRouter先写入约5.7K、后续命中约5.9K；
- bobdong首次可能报告0，warm后约5.5K，代理层指标更不稳定。

这些数字只证明一个方向：**显式full-history replay并不天然破坏缓存，真正关键是old prefix结构与字节不变。**remote cache usage不是correctness gate，具体命中量也不进入activation oracle；activation只要求local strict-prefix proof，并把远端`cached_tokens/cache_write_tokens`作为无敏感内容的观测证据。

### 3.5 从证据推导的最小产品选择

本文不把endpoint名称编码进业务逻辑，而是冻结capability-driven profile：

```text
reasoning replay codec
    NONE
  | CHAT_TEXT_FIELD
  | CHAT_OPAQUE_MESSAGE_FIELDS
  | RESPONSES_OPAQUE_OUTPUT_ITEMS

history ownership
    CLIENT_EXPLICIT
```

这些实验只证明manual history与remote response ID不能互相替代，不形成任何endpoint推荐表。Round 5A.2进一步冻结：production replay选择只按`openai_chat_completions | openai_responses`两种wire codec及actual observed carrier执行；不得按DeepSeek、Kimi、DashScope、OpenRouter、bobdong或其他vendor/model名称分支。任何endpoint若改变返回shape，closed contract validation必须显式成功或typed失败，不能在已安装epoch热猜codec。

---

## 4. 术语与authority

### 4.1 四个不同边界

本文禁止把下列事实混成一个“model failed”：

| 边界 | 含义 | authority |
|---|---|---|
| provider wire terminal | vendor明确声明response completed/incomplete/failed | vendor adapter |
| normalized semantic terminal | Runtime对vendor terminal的closed投影 | normalized transport |
| physical completion | socket/SDK iterator/close task是否物理退出 | transport execution owner |
| canonical acceptance | 是否存在完整assistant message可提交 | conversation Runner + repository transaction |

一个response可以：

```text
semantic terminal = OUTPUT_INCOMPLETE
physical completion = COMPLETED
canonical assistant = absent
```

也可以：

```text
semantic terminal = COMPLETED
physical completion = BLOCKED
canonical assistant = absent
```

因此semantic与physical terminal必须分别保留，不能让`response.completed`证明socket已退出，也不能让EOF证明response语义完整。

### 4.2 provisional live output

Text/Thinking/Data/ToolCall Start、Delta、End均属于live observation，直到整response COMPLETED并提交assistant entry以前都不是canonical事实。

即使某个ToolCallEnd已经出现：

```text
ToolCallEnd
-> response.incomplete
```

该tool call也只是“provider曾完整发送一个item”的provisional observation，不是Pulsara接受的assistant request，更不是执行授权。

### 4.3 canonical truth

本文不创建partial assistant truth。canonical conversation仍只有：

- 完整accepted assistant message；或
- 没有assistant message，并以现有turn interruption表达本次model operation未形成可接受输出。

partial live正文不写入parent content、assistant blocks、artifact、memory candidate、compaction snapshot或provider input。

### 4.4 semantic transcript与provider replay fragment

必须区分：

| carrier | 内容 | 生命周期 | authority |
|---|---|---|---|
| `CompletedAssistantMessage` | public text/data/tool calls | canonical durable | conversation truth |
| `ProviderAssistantReplayFragment` | exact reusable provider assistant message/output items，可能含opaque reasoning | 5A.1为process-local；5A.2在assistant FULL时durable | provider-wire continuity only |
| live Thinking blocks | streaming display/telemetry | disposable live | no replay authority |

Responses不能只冻结一个reasoning item，再让下一请求从canonical rows重造同一response的function calls。官方手工history契约要求重放全部output items；而且provider item order、item ID、call ID与opaque fields都可能参与续接。最小正确carrier因此是**一整个completed assistant response的可重放wire fragment**：

```text
Chat
    one exact assistant message
    = public content/tool_calls + exact reasoning field(s)

Responses
    exact ordered response.output items
    = reasoning/message/function_call/... in returned order
```

canonical commit前，adapter必须从fragment投影public text/tool calls，并与assembler产物exact compare。canonical FULL以后，后续adapter对该entry使用fragment替代generic assistant lowering；ToolResult、后续USER与Runtime observation仍按canonical compiler正常追加。这样既不会重复function call，也不会让opaque reasoning进入数据库。

### 4.5 reasoning continuity不是reasoning authority

Runtime对hidden reasoning不做以下事情：

- 不解析、总结、审查或向用户展示；
- 不把encrypted blob解密或改写；
- 不把reasoning当成permission、tool effect、memory或Plan authority；
- 不用reasoning补造canonical assistant text；
- 不在event payload、activation evidence、普通日志或conversation semantic body里保存正文；Round 5A.2唯一允许其进入受限的durable replay row及其body digest。

Runtime只证明：“这组bounded opaque bytes/items确实来自一个完整响应；其public projection与已经接受的assistant entry一致；当前resolved profile声明可原样重放。”5A.1的process-local fingerprint继续服务same-epoch exact join；Round 5A.2另以stable durable fingerprint和entry pointer支持cold read，不把payload提升为其他authority。

### 4.6 same-epoch边界

reasoning continuation只在以下五项全部相等时可复用：

```text
conversation exact scope
provider-input epoch identity
resolved wire API + model/profile compatibility fingerprint
accepted assistant entry/public semantic projection
replay codec contract fingerprint
```

same-schema physical reconnect可重新绑定transport，但不能改变fragment bytes或codec。profile/model/API变化会结束旧epoch，且旧carrier不得跨target翻译；Host loss/cold reset由Round 5A.2从durable entry-bound row重建compatible carrier。compaction adoption以后，旧floor以前的carrier退出active materialization，但历史row不由5A.1删除。

---

## 5. Closed provider-neutral DTO

### 5.1 Adapter terminal

在[`ports/provider_stream.py`](src/pulsara_agent/ports/provider_stream.py)增加：

```python
class ProviderOutputIncompleteReason(StrEnum):
    OUTPUT_TOKEN_LIMIT = "OUTPUT_TOKEN_LIMIT"
    CONTEXT_WINDOW_LIMIT_DURING_GENERATION = (
        "CONTEXT_WINDOW_LIMIT_DURING_GENERATION"
    )
    CONTENT_FILTERED = "CONTENT_FILTERED"
    UNKNOWN_PROVIDER_INCOMPLETE = "UNKNOWN_PROVIDER_INCOMPLETE"


class ProviderAdapterTerminalKind(StrEnum):
    COMPLETED = "COMPLETED"
    OUTPUT_INCOMPLETE = "OUTPUT_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class ProviderAdapterTerminal:
    kind: ProviderAdapterTerminalKind
    incomplete_reason: ProviderOutputIncompleteReason | None = None
    completed_replay_payload: ProviderAdapterCompletedReplayPayload | None = field(
        default=None,
        repr=False,
    )
```

validation：

```text
COMPLETED         -> incomplete_reason is null; replay payload follows profile
OUTPUT_INCOMPLETE -> incomplete_reason is non-null; replay payload is null
```

`ProviderAdapterCompletedReplayPayload`是同步调用链内的bounded frozen leaf，只包含codec kind、ordered frozen JSON items、logical bytes与local payload fingerprint；它尚不拥有scope、entry或epoch identity。adapter只有在已经验证唯一COMPLETED terminal以后才能构造它。

`ProviderStreamFailure`继续表达adapter已完成retry判定后的provider/transport failure。它与`ProviderAdapterTerminal`都属于terminal source item；二者之后不得再出现payload、usage或第二个terminal。

新的adapter union为：

```text
ProviderStreamPayload
| TransportUsageReport
| ProviderAdapterTerminal
| ProviderStreamFailure
```

terminal DTO本身无需再增加第二个fingerprint、generation、receipt或ID；completed replay leaf拥有5.5定义的local payload fingerprint。两者都没有独立跨事务确认或ACK-unknown authority。

### 5.2 Normalized terminal

`ProviderStreamTerminal.outcome`改为closed enum，而非自由字符串：

```python
class ProviderStreamTerminalKind(StrEnum):
    COMPLETED = "COMPLETED"
    OUTPUT_INCOMPLETE = "OUTPUT_INCOMPLETE"
    PROVIDER_ERROR = "PROVIDER_ERROR"


@dataclass(frozen=True, slots=True)
class ProviderStreamTerminal:
    outcome: ProviderStreamTerminalKind
    usage: TransportUsageReport
    incomplete_reason: ProviderOutputIncompleteReason | None = None
    error: ProviderSanitizedErrorFact | None = None
    completed_replay_payload: ProviderAdapterCompletedReplayPayload | None = field(
        default=None,
        repr=False,
    )
```

exact union：

| outcome | incomplete_reason | error | completed replay payload |
|---|---|---|---|
| COMPLETED | null | null | profile-dependent optional/required |
| OUTPUT_INCOMPLETE | required | null | null |
| PROVIDER_ERROR | null | required | null |

raw vendor reason、HTTP body、URL、request payload与partial content不得进入terminal。UNKNOWN分支只保留closed public reason。

### 5.3 DirectModel typed error

增加process-local typed exception：

```python
class ProviderModelOutputIncomplete(RuntimeError):
    reason: ProviderOutputIncompleteReason
```

以及现有generic provider failure的typed wrapper：

```python
class ProviderModelExecutionFailed(RuntimeError):
    error: ProviderSanitizedErrorFact
```

`PreparedKernelModelExecution.open_once()`：

- COMPLETED：正常结束generator；
- OUTPUT_INCOMPLETE：记录best-effort usage后抛`ProviderModelOutputIncomplete`；
- PROVIDER_ERROR：记录best-effort usage后抛`ProviderModelExecutionFailed`；
- 无论哪种terminal，都在向上返回/抛出前完成既有`aclose()`与`wait_physical_completion()`；physical BLOCKED继续覆盖semantic terminal，绝不接受assistant response。

异常只携带bounded safe fact，不拼接provider原始消息供调用方解析。

为了不让async iterator EOF承担第二项success authority，`PreparedKernelModelExecution`增加一个one-shot completed-result seam：

```python
@dataclass(frozen=True, slots=True)
class CompletedProviderModelExecution:
    terminal: ProviderStreamTerminal


PreparedKernelModelExecution.take_completed_result_once()
    -> CompletedProviderModelExecution
```

它只在semantic COMPLETED且physical close/join完成后可取一次；INCOMPLETE、PROVIDER_ERROR、cancel、BLOCKED或尚在streaming时调用均失败。`_collect_model()`必须同时取得assembler的public result与这个completed result，才能由central factory构造5.5的entry-bound fragment。若caller结束迭代却没有取result，execution owner在close时丢弃payload；它不能泄漏成跨call cache。

### 5.4 Replay codec profile

将现有single-string `ThinkingProfile`收紧为closed replay capability；Python命名可在实施时放入`llm/provider.py`，语义必须等价：

```python
class ProviderAssistantReplayCodecKind(StrEnum):
    NONE = "NONE"
    CHAT_CLOSED_REASONING_FIELDS = "CHAT_CLOSED_REASONING_FIELDS"
    RESPONSES_EXACT_OUTPUT_ITEMS = "RESPONSES_EXACT_OUTPUT_ITEMS"


class ProviderReasoningReplayScope(StrEnum):
    NEVER = "NEVER"
    TOOL_RESPONSES = "TOOL_RESPONSES"
    ALL_COMPLETED_RESPONSES = "ALL_COMPLETED_RESPONSES"


class ProviderChatReplayFieldAccumulationKind(StrEnum):
    TEXT_CONCAT = "TEXT_CONCAT"
    ORDERED_ARRAY_APPEND = "ORDERED_ARRAY_APPEND"


@dataclass(frozen=True, slots=True)
class ProviderChatReplayFieldContract:
    field_name: str
    accumulation_kind: ProviderChatReplayFieldAccumulationKind
    required_when_response_selected: bool
    final_value_required: bool
```

Chat registry固定为：

```text
reasoning_content  -> TEXT_CONCAT
reasoning          -> TEXT_CONCAT
reasoning_details  -> ORDERED_ARRAY_APPEND
```

registry描述wire shape而非供应商。adapter在open前已经冻结这三个field的shape，只把本次完整响应实际出现的subset放进replay fragment。profile还必须冻结：

- allowed Chat delta/final fields；
- allowed replay message field(s)及exact JSON shape；
- Responses replayable output-item type/era validator；
- 对Qwen-style template是否固定`preserve_thinking=true`或等价stable render option；
- codec contract fingerprint；
- replay scope。

Chat contract还必须冻结ordered、unique field contracts，不能只列allowed field names。两种accumulation的exact语义为：

| mode | 每个stream value | 累积规则 | duplicate/conflict |
|---|---|---|---|
| TEXT_CONCAT | string | 按wire顺序直接拼接，不插separator | 非string fail |
| ORDERED_ARRAY_APPEND | JSON array | 按chunk顺序展开并append elements | 非array fail；不去重/排序 |

若stream从未提供某field，而complete/final message首次提供它，则final value按该field的accumulation kind初始化唯一accumulator；若stream已经提供过该field，final deep-frozen value必须与累计结果exact equal。`final_value_required=true`时最终缺失fail closed。本文不支持“不断发送越来越大的完整snapshot并以后值覆盖前值”；需要该wire的provider必须以后新增独立、版本化mode，不能把它误解释成array delta。

closed validation：`codec=NONE` iff `scope=NEVER`；其他codec只允许`TOOL_RESPONSES | ALL_COMPLETED_RESPONSES`。Responses COMPLETED必须携带完整typed output payload；Chat只有在scope选中且本次response实际出现至少一个known carrier时才携带payload。没有reasoning carrier的标准Chat tool response仍可完成，不伪造空replay fragment。

`CHAT_CLOSED_REASONING_FIELDS`同时接受两个string concat carrier与一个ordered-array carrier，不允许互相转换或把array压成字符串。`RESPONSES_EXACT_OUTPUT_ITEMS`保留complete response中通过§8.4 closed allowlist与shape validator的全部ordered output items，不只挑reasoning item。

adapter不允许看到未知field以后临时猜codec或accumulation。未知field为null/empty时忽略；未知non-empty field只在已有完整supported final text且没有tool continuation时可被丢弃，绝不回传；涉及tool continuation或没有supported public output时得到typed provider contract failure。

三个known Chat carrier在adapter内部必须从首个非null delta起执行增量physical fence，不能等terminal后才依赖normalized transport检查。冻结上界为：reasoning carrier canonical aggregate复用完整provider response的16 MiB physical bound，不建立可独立漂移的第二个byte policy；累计text fragment/ordered-array element（空array delta按一个fragment计）不超过65,536项。item bound为异常碎片流与低byte高对象数的process-local circuit breaker，必须为当前16,384 output-token hard bound保留至少四倍headroom，不能成为正常模型输出的产品上限。`TEXT_CONCAT`保存bounded chunk list，只在final reconcile/freeze时join；`ORDERED_ARRAY_APPEND`在每次freeze/append前先quote新增array；任一上界越界立即产生typed source item/payload-limit failure且不可retry。unknown non-empty field只保留一个bounded boolean，不累计field name或value。

### 5.5 Exact replay fragment

adapter leaf先冻结：

```python
@dataclass(frozen=True, slots=True)
class ProviderAdapterCompletedReplayPayload:
    codec_kind: ProviderAssistantReplayCodecKind
    ordered_wire_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    logical_utf8_bytes: int
    local_payload_fingerprint: str
```

它不携带transport、provider client、response ID continuation authority、scope或canonical entry。随后由DirectModel/Runner central factory把resolved request facts、proposed assistant entry与public projection绑定进去，得到核心process-local carrier：

```python
@dataclass(frozen=True, slots=True)
class ProviderAssistantReplayFragment:
    codec_kind: ProviderAssistantReplayCodecKind
    replay_scope: ProviderReasoningReplayScope
    provider_profile_fingerprint: str
    resolved_target_semantic_fingerprint: str
    proposed_assistant_entry_id: str
    public_projection_fingerprint: str
    ordered_wire_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    logical_utf8_bytes: int
    process_local_fingerprint: str
```

closed shape：

| codec | ordered_wire_items |
|---|---|
| CHAT_CLOSED_REASONING_FIELDS | exactly one complete assistant message object containing only the observed closed-registry subset |
| RESPONSES_EXACT_OUTPUT_ITEMS | one or more exact response output item objects in wire order |

`ordered_wire_items`必须使用现有frozen JSON vocabulary深冻结；禁止mutable dict/list。`logical_utf8_bytes`按canonical JSON codec计量，但canonical JSON只用于local bound/fingerprint，不替换实际item字段与顺序。fingerprint使用domain-separated process-local算法，覆盖全部字段与ordered item bytes；不得写入数据库或普通diagnostic。

fragment的public projection必须exact等于assembler准备提交的`CompletedAssistantMessage`：

```text
public text/data/tool names/tool call IDs/arguments/order equal
opaque reasoning fields excluded from public projection
```

任何不一致都使整个response不能成为COMPLETED acceptance candidate。不能选择“接受canonical assistant但悄悄丢掉一个required tool-loop reasoning fragment”；若profile/replay scope表示该response必须续接，fragment validation failure就是provider contract failure。

### 5.6 Binding与安装状态机

在5A.1激活实现中，fragment不是独立durable candidate，也不需要receipt；它借用既有assistant commit与continuity CAS形成closed process-local状态。Round 5A.2保留同一状态机作为当前Host的安装过程，同时把COMPLETED_UNBOUND候选与assistant row在一个transaction中持久化：

```text
COLLECTING
  -> COMPLETED_UNBOUND     # unique COMPLETED terminal + fragment validated
  -> BOUND                # assistant commit FULL/compatible confirmation
  -> INSTALLED            # next provider-input append candidate CAS accepts it

COLLECTING | COMPLETED_UNBOUND | BOUND
  -> DISCARDED            # incomplete/failure/cancel/final abandon/CONFLICT/cold close

INSTALLED
  -> retained byte-for-byte until epoch close
```

当前代码没有可复用的assistant settlement owner：`KernelSessionIO.run()`在waiter取消时会drain物理线程并重新抛`CancelledError`，而Runner只捕获`Exception`做winner confirmation。因此本轮必须显式增加一个窄的、process-local、shielded `AssistantMessageSettlementAttempt`；不能在规格里假定它已经存在。

在调用assistant repository mutation以前，Runner向Host/session owner登记attempt，并冻结exact commit/confirmation参数：

```text
session / turn / exact conversation scope
provider-input epoch nonce + expected revision
PreparedProviderInputCut identity
stable assistant entry ID
parent content identity
ordered canonical block semantic fingerprint
complete_turn flag / occurred_at / actor
optional PreparedAssistantReplayBinding fingerprint
assistant settlement candidate fingerprint
```

该attempt唯一拥有`commit_assistant_message`、`confirm_assistant_message_winner`及fragment promote/discard，不允许caller在异常分支另建第二个confirmation candidate。它通过一个Host-owned shielded asyncio task执行；调用方取消只detach waiter，task继续取得`KernelSessionIO`的真实返回或执行stateless exact confirmation。Host close必须drain所有已登记assistant settlement tasks。

settlement在同一process-local scope lock中完成：

```text
FULL exact winner -> promote dormant binding to BOUND
NONE              -> keep write/confirm owner alive; not yet settle fragment
final NONE/abort  -> DISCARD
CONFLICT          -> DISCARD
```

该owner确保caller cancellation只能detach waiter，不能形成“canonical FULL但同一Host settlement尚未检查fragment”的窗口。它复用既有stable assistant candidate与repository confirmation API，但承认process-local settlement task是本轮新增的物理owner；不新增durable receipt或第二个writer。

只有`FULL + optional fragment promoted to BOUND`才能把`AcceptedEntry`交还Runner，随后才允许live COMMITTED settlement与tool authorize/attempt/invoke。transient NONE由同一task在bounded canonical deadline/confirmation policy内继续；final NONE或CONFLICT终结attempt并丢弃unaccepted fragment。5A.1激活时Host进程丢失会失去BOUND fragment；Round 5A.2把optional replay纳入同一assistant transaction与confirmation，因此下一Host可从exact durable composite重建。

assistant entry使用现有stable proposed ID，因此FULL confirmation以后无需修改frozen fragment；只需证明winner entry ID与public semantic fingerprint exact join。BOUND fragment在下一次dispatch planning中与对应assistant canonical item一起加入provider replay overlay；continuity install以后，其fingerprint成为epoch prefix proof的一部分。

若response没有后续model call，BOUND fragment仍可由scope continuity owner持有到epoch close；Round 5A.2同时保存其subordinate durable row，但仍不创建后台task、lease、job或recovery owner。

### 5.7 Frozen provider-wire plan、continuity CAS与preflight

pure compiler继续只产出canonical semantic `FrozenCompiledModelInput`，不导入vendor transport DTO。但当前production先注册semantic append candidate、再调用preflight；reasoning overlay加入后，该顺序无法证明真正发送的wire input。必须把“纯wire planning”与“transport-bearing preflight”拆开：

```text
compiled semantic messages
+ exact scope/epoch replay fragments
-> DirectModel.plan_wire_input()        # pure, no transport open
-> FrozenProviderWireInputPlan          # exact materialization + final quote
-> continuity append candidate exact joins plan
-> continuity.register(candidate)
-> preflight_execution(request, same plan, candidate fingerprint)
-> continuity.install(plan fingerprint, execution fingerprint)
-> physical open thaws the same plan; no rematerialization
```

当前`LLMMessage`本身不携带origin entry，因此compiler必须同时返回一个等长、provider不可见的定位tuple；否则preflight只能靠正文或tool call ID猜fragment placement：

```python
@dataclass(frozen=True, slots=True)
class FrozenCompiledMessagePlacement:
    message_ordinal: int
    origin_entry_id: str | None
    origin_item_fingerprint: str
    within_origin_ordinal: int
    role: MessageRole


FrozenCompiledModelInput.message_placements:
    tuple[FrozenCompiledMessagePlacement, ...]
```

规则：

- tuple与`messages`严格等长，ordinal从0连续递增；
- canonical transcript item带exact entry ID；Runtime synthetic source可为null，但仍有source item fingerprint；
- 同一assistant entry若lower为多条message，它们必须连续、`within_origin_ordinal`连续；
- placements不进入provider payload、token estimate或public semantic fingerprint；
- placements拥有独立fingerprint并进入compile binding/execution exact join；
- DirectModel只用它定位replacement group，不用正文、tool name、call ID或tuple相似度猜测。

这仍是pure compiler metadata：它不包含reasoning、transport、provider profile或live capability。canonical message renderer保持原义。

provider-wire planning使用closed frozen carriers：

```python
@dataclass(frozen=True, slots=True)
class FrozenProviderWireMaterialization:
    root_policy_value: FrozenJsonValue = field(repr=False)
    tool_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    ordered_input_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    materialization_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenProviderWireReplacementIdentity:
    assistant_entry_id: str
    first_message_ordinal: int
    message_count: int
    generic_message_group_fingerprint: str
    replay_fragment_fingerprint: str
    replacement_wire_fingerprint: str
    semantic_debit_utf8_bytes: int
    replay_addend_utf8_bytes: int
    semantic_debit_tokens: int
    replay_addend_tokens: int


@dataclass(frozen=True, slots=True)
class FrozenProviderWireInputQuote:
    estimator_fingerprint: str
    effective_input_budget_tokens: int
    semantic_total_input_tokens: int
    semantic_message_tokens: int
    semantic_message_utf8_bytes: int
    replaced_semantic_debit_tokens: int
    replay_addend_tokens: int
    replaced_semantic_debit_utf8_bytes: int
    replay_addend_utf8_bytes: int
    final_message_tokens: int
    final_total_input_tokens: int
    final_message_utf8_bytes: int
    final_wire_input_utf8_bytes: int
    quote_contract_version: str
    quote_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenProviderWireInputPlan:
    context_id: str
    compiled_semantic_fingerprint: str
    message_placement_fingerprint: str
    wire_api: str
    provider_profile_fingerprint: str
    resolved_target_semantic_fingerprint: str
    ordered_replacements: tuple[FrozenProviderWireReplacementIdentity, ...]
    materialization: FrozenProviderWireMaterialization = field(repr=False)
    wire_system_fingerprint: str
    wire_tools_fingerprint: str
    wire_input_prefix_fingerprint: str
    quote: FrozenProviderWireInputQuote
    plan_fingerprint: str
```

`FrozenProviderWireMaterialization`是actual system/instructions、tools与ordered input JSON subtree的唯一process-local frozen owner。Chat把system/root policy表示在`root_policy_value`，conversation messages放入`ordered_input_items`；Responses分别表示`instructions`与`input`。adapter physical open只能thaw该materialization，并与resolved call补齐model、timeout及其他已由target/profile fingerprint覆盖的request controls；不得再次从`LLMMessage`重建input。

`FrozenCompiledModelInput.final_estimate`继续描述pure semantic messages及其per-message breakdown；wire plan不得伪造一个新的semantic `TokenEstimate`。`FrozenProviderWireInputQuote`只描述replacement debit/addend及最终physical input cost。

plan central factory必须验证：

- quote aggregate严格等于ordered replacement逐项之和；
- `final_message = semantic_message - debit + addend`对UTF-8 bytes与tokens都成立；
- `final_total = semantic_total - debit + addend`且不超过effective input budget；
- replacement ranges按message ordinal严格递增、不重叠，并exact join placements；
- materialization fingerprints与三个wire component fingerprints一致；
- `wire_input_prefix_fingerprint`以domain-separated framing覆盖`wire_api`、provider profile以及actual root policy、tools、ordered input的结构与字节顺序；不能只hash semantic messages；
- final quote/bytes在effective provider budget、completed working-set与64 MiB epoch hard bound内；
- quote fingerprint覆盖全部quote字段；plan fingerprint覆盖全部独立字段、replacement identities、quote fingerprint和materialization fingerprint。

`PreparedProviderInputAppendCandidate`必须持有同一个frozen plan引用，并让candidate fingerprint覆盖`plan_fingerprint`与final quote。`FrozenProviderInputEpochView`安装时同时冻结semantic view以及plan/system/tools/input fingerprints、final wire bytes/tokens；semantic view成功但wire proof缺失不是合法INSTALLED状态。`PreparedKernelModelExecution`同样只引用该plan，不复制raw items。

这解除当前循环：`plan_wire_input()`不需要candidate fingerprint；只有plan已经完成以后才构造candidate。随后`preflight_execution()`接收candidate fingerprint的用途只是证明“这个transport execution将打开同一个已注册plan”，不得再次改变overlay、顺序或estimate。

overlay按assistant entry identity替换该entry的generic assistant representation；不能简单把fragment追加到messages末尾。Responses replacement覆盖该response的完整assistant output items；Chat replacement覆盖一条assistant message。非assistant items、ToolResult与Runtime observations保持compiler顺序。

preflight必须验证：

- 每个fragment只匹配一个本次compiled assistant entry；
- 不匹配、重复匹配、顺序反转或跨scope引用fail closed；
- fragment profile/target/codec与当前prepared call exact join；
- plan materialization的old prefix与installed epoch byte/structure fingerprint相同；
- replay bytes/tokens进入wire quote、64 MiB epoch bound与compaction pressure；semantic estimate保持compiler truth；
- fragment不存在时才使用普通canonical lowering；不得为已安装fragment临时降级；
- preflight与physical adapter都消费exact same plan fingerprint/materialization identity。

若已安装fragment在当前budget放不下，provider open=0并形成typed resource boundary；不得删掉旧reasoning换取一次调用，因为那会破坏strict prefix。Round 5B proactive trigger必须在到达该硬边界前compact。

本轮新增的强不变量为：

```text
same Host + same exact scope + same epoch

wire_system[n+1] = wire_system[n]
wire_tools[n+1]  = wire_tools[n]
wire_input[n+1]  = wire_input[n] || append_only_suffix

其中wire_input包含已经安装的exact assistant replay fragments。
```

provider内部如何tokenize/render以及remote cache是否兑现不属于Runtime authority；但Pulsara自己发送的ordered JSON/message structure必须满足上述关系。任何profile若会让Runtime主动删除历史reasoning字段，不能在strict epoch中启用该replay policy。

---

## 6. Adapter terminal protocol

### 6.1 通用状态机

每个physical provider attempt：

```text
OPEN
  -> zero or more payload/usage items
  -> exactly one ProviderAdapterTerminal OR ProviderStreamFailure
  -> CLOSED
```

规则：

1. terminal是adapter语义输出中的最后一项。adapter应在内部先观察并锁存vendor terminal，消费该wire允许的trailing usage/metadata直至vendor stream正常结束，再把provider-neutral terminal作为最后一个outward item发出；不得先向Runtime发terminal、再让未受监督的vendor event留在generator里。
2. adapter raw iterator在terminal前EOF：`TRANSPORT_PROTOCOL_ERROR`。
3. terminal后仍有raw event/item：`TRANSPORT_PROTOCOL_ERROR`。
4. duplicate terminal：`TRANSPORT_PROTOCOL_ERROR`。
5. duplicate usage继续是protocol error。
6. COMPLETED到达时normalized transport必须没有open semantic blocks；adapter可在vendor response-level complete marker到达时，根据该wire contract发出exact End payload后再发terminal。
7. OUTPUT_INCOMPLETE到达时允许存在open live blocks；它们被aborted，不得由adapter或normalizer合成End。
8. ProviderStreamFailure到达时同样不得自动close open blocks。
9. EOF只证明physical iterator结束，不证明semantic response完成。

### 6.2 禁止的convenience close

当前`close_active_blocks()`与`close_active_tool_calls()`只能在adapter已经证明vendor terminal为COMPLETED的分支调用。以下分支禁止调用：

```text
finish_reason = length
finish_reason = content_filter
response.incomplete
response.failed / error
SDK exception
cancel
raw EOF without terminal
local stream/assembly bound exceeded
```

尤其不得用`"{}"`补齐没有完成的tool arguments。

### 6.3 Usage

usage保持optional/best-effort observation：

- adapter在terminal前发出最多一个`TransportUsageReport`；
- normalized terminal总是携带report，缺失则使用`usage_status="missing"`；
- DirectModel对COMPLETED、OUTPUT_INCOMPLETE、PROVIDER_ERROR都允许调用usage observer；
- usage observer异常不得改变terminal结果；
- usage存在不代表response可接受。

### 6.4 Reasoning/replay fragment collection

adapter可在stream期间收集replay所需raw fields/items，但它们始终是provisional：

```text
delta / output_item.added / output_item.done
    -> bounded provisional replay builder

unique COMPLETED terminal
    -> validate exact reusable fragment
    -> emit fragment with completed model result

INCOMPLETE / failure / EOF / cancel / physical BLOCKED
    -> zero replay fragment
    -> clear provisional buffers
```

normalized live payload仍只承载可展示Thinking/Text/ToolCall事件；opaque `reasoning_details`或encrypted item不得塞进live text。fragment通过DirectModel的completed-result carrier交付给Runner，不作为额外stream delta，也不需要Protocol/TUI消息。

streaming Responses必须从`output_item.added/done`与最终`response.completed.response.output`建立exact equality；若SDK只在final response对象中提供完整opaque fields，以final object为真源并重验先前stream projection。Chat必须由complete accumulated message与finish reason构造single assistant replay message。

---

## 7. Chat Completions映射

### 7.1 Closed finish reason table

| wire finish_reason | provider-neutral terminal | 行为 |
|---|---|---|
| `stop` | COMPLETED | close exact active text/thinking/tool blocks；tool arguments仍须形成合法JSON object |
| `tool_calls` | COMPLETED | 同上 |
| `length` | OUTPUT_INCOMPLETE / OUTPUT_TOKEN_LIMIT | 不close任何active block；不parse/repair partial arguments |
| `content_filter` | OUTPUT_INCOMPLETE / CONTENT_FILTERED | 不接受partial content |
| unknown non-null string | OUTPUT_INCOMPLETE / UNKNOWN_PROVIDER_INCOMPLETE | fail closed，不猜success |
| 始终为null后EOF | PROVIDER_ERROR / TRANSPORT_PROTOCOL_ERROR | adapter未收到semantic terminal |

首个non-null finish reason唯一决定semantic terminal。其后的frame只有在finish reason完全相同，且delta只含空的assistant role/content/tool/replay字段时，才作为vendor-neutral、idempotent terminal echo丢弃；它可以补充usage，完全相同的usage重复为no-op。不同finish reason、任何非空正文/reasoning/tool delta、final message或冲突usage仍fail closed。echo不进入下一次history、不创建第二个terminal语义，也不改变whole-response atomicity。

若provider在异常对象中、而不是finish chunk中明确报告`context_length_exceeded`且尚未发送semantic output，它仍属于现有deterministic invalid request，不触发compaction。只有provider在generation terminal中明确表达“生成过程中达到context window”时，才映射`CONTEXT_WINDOW_LIMIT_DURING_GENERATION`。

### 7.2 Choice规则

Pulsara请求语义仍是单一assistant candidate。adapter必须：

- 只接受预期choice；
- 对同一choice矛盾的non-null finish reason fail closed；exact empty terminal echo按§7.1归一化；
- 不把一个choice的terminal用于关闭另一个choice的blocks；
- 不在本轮引入multi-candidate/n-best语义。

deprecated `finish_reason=function_call`当前没有对应的legacy delta contract，因此按unknown incomplete fail closed；本文不借terminal修订恢复旧function_call wire。

如果当前request没有显式`n=1`，implementation应在payload中固定`n=1`或由adapter验证只有index 0。该修订属于现有single-response contract，不新增产品能力。

### 7.3 Mock与fixture

mock chunks不再享有“EOF即完成”的特殊路径。所有production adapter fixture必须包含明确terminal finish reason；否则测试应得到protocol error。

### 7.4 Chat replay fragment

Chat COMPLETED response的fragment必须是一条完整assistant message，而不是只存thinking字符串：

```json
{
  "role": "assistant",
  "content": "...",
  "tool_calls": ["... exact completed calls ..."],
  "reasoning_content": "... provider text carrier ..."
}
```

或同一个closed registry验证后的structured shape：

```json
{
  "role": "assistant",
  "content": "...",
  "tool_calls": ["..."],
  "reasoning_details": ["... exact opaque objects ..."]
}
```

示例中的值只表示shape；真实opaque body不得出现在文档、日志或fixture snapshot。字段absence与present-empty必须按profile closed contract区分，不能用Python truthiness静默合并。

`TOOL_RESPONSES`要求只有含tool call且实际出现known reasoning carrier的completed assistant response安装fragment；`ALL_COMPLETED_RESPONSES`则为每个实际含known carrier的completed response安装。标准Chat response若没有carrier则不安装空fragment。scope resolution以后该选择在epoch内不可变。对于需要stable all-turn template的配置，request defaults必须从epoch**第一次请求**起就固定`preserve_thinking=true`或等价参数；不能等第一段reasoning出现后才加，也不能根据当前最后一条USER动态开关。

ToolResult继续生成：

```json
{"role":"tool","tool_call_id":"...","content":"..."}
```

它不打开新user turn。technical adapter不得为了兼容某个template把它改成`role=user`。

---

## 8. Responses映射

### 8.1 Closed response terminal table

| wire event | provider-neutral terminal |
|---|---|
| `response.completed` | COMPLETED |
| `response.incomplete`, reason=`max_output_tokens`或等价closed alias | OUTPUT_INCOMPLETE / OUTPUT_TOKEN_LIMIT |
| `response.incomplete`, reason=`model_context_window_exceeded`或等价closed alias | OUTPUT_INCOMPLETE / CONTEXT_WINDOW_LIMIT_DURING_GENERATION |
| `response.incomplete`, reason=`content_filter` | OUTPUT_INCOMPLETE / CONTENT_FILTERED |
| `response.incomplete`, absent/unknown reason | OUTPUT_INCOMPLETE / UNKNOWN_PROVIDER_INCOMPLETE |
| `response.failed` / `response.error` / top-level `error` | ProviderStreamFailure |
| EOF without any response terminal | ProviderStreamFailure / TRANSPORT_PROTOCOL_ERROR |

vendor alias mapping必须是closed table。禁止用substring从自由文本猜OUTPUT_TOKEN_LIMIT；未知值进入UNKNOWN_PROVIDER_INCOMPLETE。

### 8.2 Item done不是response acceptance

`response.output_item.done(function_call)`继续用于：

- exact reconcile arguments；
- 检查stable call ID/name；
- 发出provisional ToolCallEnd live payload。

但只有后续`response.completed`才能使整个assembler返回CompletedAssistantMessage。以下序列必须无canonical effect：

```text
output_item.done(function_call A)
response.incomplete(max_output_tokens)
```

结果：

```text
assistant entry count += 0
tool attempt count    += 0
physical tool calls  += 0
live draft settlement = ABORTED
turn terminal reason  = MODEL_OUTPUT_TOKEN_LIMIT_REACHED
```

这比Codex逐item salvage更保守，但与Pulsara现有atomic assistant transaction完全一致，也避免“已执行A后重试整response”产生重复effect。

### 8.3 response.completed的block closure

Responses adapter可以在`response.completed`上关闭仍open的text/thinking blocks，因为response-level terminal按wire contract证明生成完整；tool call则必须已经拥有稳定name、ID与完整arguments。若active tool call无法exact reconcile，COMPLETED也必须降为protocol error，不得用空arguments补齐。

### 8.4 Responses exact output replay

Responses V1只接受以下closed top-level `response.output` item type：

```text
reasoning
message
function_call
```

`message.content`同样使用closed profile-era validator：默认只接受当前Runtime能完整投影的`output_text`；明确列入compatible profile的`text` alias可映射为同一语义。`refusal`、audio/image、hosted-tool output、computer/program call及任何未知content/item type一律在canonical acceptance前形成provider contract failure。

V1还必须把provider item order限制在当前canonical assistant blocks能够无损表达的closed subset：`message`最多一个；若存在，必须位于全部`function_call`之前。`reasoning`可出现在任意位置且不参与public block order。`function_call -> message`、多个message或其他无法经canonical commit/read保持public顺序的shape，必须在assistant acceptance前以typed provider contract failure拒绝。该限制是fail-closed capability boundary，不允许先执行tool、再在下一次replay join时发现顺序漂移。

在该allowlist内，Responses COMPLETED fragment必须冻结最终`response.output`的**全部**ordered item tuple。下一次input builder直接把该tuple放回原位置，再追加对应`function_call_output`与后续items：

```text
old inputs
|| exact response.output items
|| function_call_output(s)
|| later user/runtime suffix
```

禁止：

- 只拿`reasoning.summary`重建reasoning item；
- 只重放`encrypted_content`而从canonical rows另造function_call item；
- 改写item ID、call ID、status、content ordering或unknown allowed opaque fields；
- 将Responses reasoning item转换成Chat `reasoning_content`；
- 在payload中同时使用full manual history与`previous_response_id`来形成两个history authority。

profile必须验证negotiated-era item shape以及reasoning、message、function_call各自的closed required/optional fields。未知output item即使技术上能作为opaque JSON回传，也可能携带未建模的physical effect或缺少canonical owner，因此整个response按provider contract failure处理，而非接受后静默丢失。

OpenAI“手工管理history时重放全部output items”的含义是：对Runtime**已经选择支持并完整接受**的response，必须一项不漏地重放；它不要求Pulsara接受尚未拥有canonical projection/effect contract的未来item type。新增item支持必须单独扩展allowlist与semantic/effect tests，不得只改profile配置绕过。

---

## 9. Retry contract

### 9.1 允许retry

只保留当前transport retry：

```text
retryable provider/physical exception
AND no semantic payload has been emitted
AND no adapter terminal has been emitted
AND attempt budget remains
-> retry exact same prepared physical request
```

retry不得重新compile、改变tool surface、改变provider-input epoch或重新接受canonical candidate。

### 9.2 禁止retry

以下均不自动retry：

- OUTPUT_TOKEN_LIMIT；
- CONTEXT_WINDOW_LIMIT_DURING_GENERATION；
- CONTENT_FILTERED；
- UNKNOWN_PROVIDER_INCOMPLETE；
- semantic payload出现后的transport failure；
- local assembler/resource limit；
- caller cancellation；
- protocol mismatch；
- physical close BLOCKED。

即使OUTPUT_INCOMPLETE发生前没有delta，也不得自动retry。同一个cap下重跑没有确定性收益；若未来要增大output cap或发continuation prompt，那是新的model-call candidate与产品策略，不属于transport retry。

### 9.3 不触发compaction

本文任何terminal都不能直接调用Round 5B compactor：

```text
pre-open provider context error -> typed provider failure
mid-generation context limit    -> typed output incomplete
output token limit              -> typed output incomplete
```

下一次manual/proactive/mid-turn safe-point是否compact由Round 5B自己的local planning fact决定，不能读取provider错误字符串或remote retry hint。

---

## 10. Runner与canonical settlement

### 10.1 Completed

仅COMPLETED路径维持当前流程：

```text
physical provider completed
-> assembler.complete()
-> finalize optional ProviderAssistantReplayFragment
-> exact compare fragment public projection
-> materialize canonical blocks
-> register one AssistantMessageSettlementAttempt
-> shielded owner performs commit/exact confirmation
-> FULL: bind fragment to exact accepted entry
-> transient NONE: keep exact prepared candidate and confirm/retry
-> final abandon/CONFLICT: discard fragment and stop
-> only FULL: inspect accepted tool calls
-> authorize/accept/invoke
```

`assembler.complete()`不得在未观察到COMPLETED terminal时调用。建议由`_collect_model()`显式持有terminal disposition，而不是依赖async generator自然结束。

正常caller await同一settlement task并取得`AcceptedEntry`。若caller取消，Runner从tool path退出，但不得cancel settlement task；owner仍完成FULL/NONE/CONFLICT并按5.6绑定或丢弃fragment。任何代码都不得在等待settlement期间提前offer live COMMITTED、读取accepted calls或启动工具。

fragment binding不得延迟到tool result以后。DeepSeek式provider要求下一次tool-loop call已经带回产生该tool call的reasoning；因此assistant FULL与tool execution之间可以完成process-local BOUND，等tool batch结算后准备下一次dispatch时再与tool result一起原子安装为continuity suffix。tool-surface borrow的既有生命周期不变。

### 10.2 Output incomplete

ordinary ROOT/SUBAGENT_TASK model call收到`ProviderModelOutputIncomplete`：

1. physical execution必须close/join；
2. matching live draft以stable reason ABORTED；
3. 不调用`_canonical_blocks()`；
4. 不调用`commit_assistant_message()`；
5. 不创建ToolExecutionAttempt；
6. 不调用任何tool executor；
7. 丢弃所有provisional/unbound replay fragment；
8. 释放tool-surface borrow与其他process-local preparation resource；
9. 使用现有`interrupt_turn()`终结exact turn；
10. 重新抛typed exception给Host/controller。

turn terminal reason table：

| incomplete reason | canonical turn terminal_reason |
|---|---|
| OUTPUT_TOKEN_LIMIT | `MODEL_OUTPUT_TOKEN_LIMIT_REACHED` |
| CONTEXT_WINDOW_LIMIT_DURING_GENERATION | `MODEL_OUTPUT_CONTEXT_LIMIT_REACHED` |
| CONTENT_FILTERED | `MODEL_OUTPUT_CONTENT_FILTERED` |
| UNKNOWN_PROVIDER_INCOMPLETE | `MODEL_OUTPUT_INCOMPLETE` |

这些值进入现有turn relation与既有TurnInterrupted occurrence，不新增event type。Round 7 predecessor mapping将四者投影为现有`EXECUTION_FAILED`，避免下一turn把上次partial tool request误认为已执行；本轮不新增provider-visible failure subtype或source contract。

### 10.3 Provider error

ProviderModelExecutionFailed保持现有失败结算，但必须保留typed sanitized fact供operational hook使用，不再通过`RuntimeError`字符串提取code。canonical terminal reason可继续使用`FOREGROUND_EXECUTION_INTERRUPTED`；本文不建立provider error ledger。

### 10.4 Live output

partial text/thinking/tool delta可以继续实时展示，但最终ABORTED settlement必须明确告诉controller：

- 它不是accepted assistant entry；
- reconnect/canonical snapshot不得恢复该draft；
- UI可以将已显示正文标记为“输出未完成”，但不得把它混入下一次prompt；
- live body不持久化到数据库、artifact或trace file。

无需新增live event kind；复用现有draft settlement与stable reason code。

---

## 11. Tool-call atomicity

### 11.1 不完整tool call

以下全部为no-attempt：

```text
ToolCallStart -> partial delta -> OUTPUT_INCOMPLETE
ToolCallStart -> ToolCallEnd -> OUTPUT_INCOMPLETE
TextEnd -> ToolCallEnd -> OUTPUT_INCOMPLETE
ToolCallEnd(A) -> ToolCallStart(B) -> OUTPUT_INCOMPLETE
```

Runtime不得：

- 尝试repair JSON；
- 用`{}`补arguments；
- 只执行A；
- 把A保存为assistant message后重试B；
- 自动重发整次provider request；
- 将partial arguments写入memory/citation handle。

### 11.2 已经执行中的tool

一旦ToolExecutionAttempt存在，说明其来源assistant response此前已经COMPLETED并canonical accepted。此时provider output cap与该physical tool没有并发关系：当前provider call已经结束。

如果工具输出使下一次provider input接近阈值：

```text
settle exact tool result first
-> release physical invocation owner
-> next safe point computes normal dispatch quote
-> Round 5B may compact before next provider open
```

禁止为了提前compact而取消或伪造tool outcome。

### 11.3 Side-effect proof

Round 5A.1 activation必须证明：任何OUTPUT_INCOMPLETE scenario下，所有tool executor mock的physical invocation count均为0；不能只断言数据库没有attempt。

---

## 12. Auxiliary、memory与future compaction summary

### 12.1 Auxiliary JSON port

[`auxiliary_model.py`](src/pulsara_agent/conversation_kernel/auxiliary_model.py)当前只有在normalized terminal COMPLETED时才会parse JSON，这一方向保留，但错误必须typed：

```text
COMPLETED + complete text + valid bounded JSON object -> return object
OUTPUT_INCOMPLETE                           -> no parse, raise typed incomplete
PROVIDER_ERROR                              -> no parse, raise typed provider failure
physical BLOCKED                            -> no result
```

即便partial bytes恰好构成合法JSON，也不能返回。

ordinary auxiliary JSON calls没有后续same-epoch continuation authority。它们可以让adapter验证COMPLETED replay payload以证明wire完整，但`DirectKernelAuxiliaryModel`在JSON结果交付后立即丢弃payload，不安装fragment、不创建continuity epoch。Round 5B summary是唯一例外：**input**借用foreground旧epoch fragments；它自己的summary response fragment同样不继承到新epoch。

### 12.2 Advisory memory

| purpose | incomplete行为 |
|---|---|
| MEMORY_GOVERNANCE | 当前candidate按既有弱完整性路径ABANDONED/保持非accepted；conversation不失败 |
| MEMORY_HINT_REVIEW | reflection无结果；conversation不失败 |
| embedding/rerank | 不经过本文model stream；保持各自降级契约 |

不得因incomplete governance output把candidate statement部分接受，也不得重新调用主模型补齐。

### 12.3 Round 5B summary

future `PreparedCompactionSummaryCall`必须复用本文terminal：

```text
OUTPUT_INCOMPLETE
-> no summary content object
-> no PreparedCompactionCanonicalAdoption
-> no snapshot/revision/event write
-> no continuity close/install
-> no MCP promotion
-> release source view/borrow
-> old epoch remains
```

summary call的input还必须复用本文replay overlay：

```text
old epoch SYSTEM                      exact equal
old epoch tools                       exact equal
old epoch semantic messages           exact prefix
installed assistant replay fragments exact byte/structure equal
compaction instruction                one appended USER suffix
tool_choice                           none
```

这里的“工具禁用”是summary call的physical execution policy，不允许回写或重造旧tools数组；旧tools仍留在exact prefix中，wire用`tool_choice=none`禁止模型调用。summary若仍返回tool call，即使terminal COMPLETED，也属于semantic validation failure，不得adopt。

summary输出只允许公开语义handoff。Runtime不得要求模型“列出你的推理过程”，不得把old fragment的opaque body拼入summary prompt，也不得把summary里看似`<think>`的文字升级成新epoch reasoning。

成功adoption是合法cold rebase：

```text
close old continuity epoch
discard all process-local ProviderAssistantReplayFragment
leave old-floor durable replay rows inactive and private
discard optional remote response/session identities
materialize new summary + retained groups + rebuilt Runtime sources
start new epoch with no inherited hidden reasoning
```

Round 5B的token/byte trigger、source view quote与protected-prefix estimate必须包含installed fragments；否则planner会低估真实provider input，并可能在summary safe point以前先撞local/provider input hard bound。

Round 5B可定义一次新的bounded semantic retry：从同一旧prefix重新发送更严格的“在N bytes内完成handoff”请求。但那是一个新的summary model call，不是transport自动retry；不得把第一次partial summary放入第二次prompt，也不得改变source cut。是否启用由Round 5B单独冻结，本文不默认开启。

### 12.4 Dormant durable compaction job

当前BACKGROUND_COMPACTION job若在Round 5B删除前仍存在，incomplete auxiliary response只能让当前job attempt失败，不能采用partial snapshot。本文不新增job terminal reason或retry graph。

---

## 13. Physical close与cancellation

Round 5A已经冻结provider stream的connect/write/pool/read-idle与physical close ownership。本文只增加terminal分类，不改变watchdog：

1. semantic terminal到达后仍需`aclose()`与`wait_physical_completion()`；
2. caller cancel先请求physical cancel，再等待exact owner退出；
3. cancellation不是OUTPUT_INCOMPLETE，使用现有per-turn cancellation intent；
4. semantic COMPLETED但physical BLOCKED时不接受assistant；
5. semantic OUTPUT_INCOMPLETE且physical COMPLETED时，准确报告incomplete；
6. semantic OUTPUT_INCOMPLETE且physical BLOCKED时，以physical ownership failure终结，仍不得接受partial response；
7. close waiter cancellation只能detach waiter，不得让physical stream失去owner。

---

## 14. Output预算边界

### 14.1 为什么继续发送output cap

Pulsara与Codex的目标不同：Codex ordinary Responses client可以依赖第一方backend与模型metadata，不发送`max_output_tokens`；Pulsara必须支持OpenAI-compatible custom endpoint。因此：

- compiler需要一个明确的effective output reservation；
- input budget必须在provider open前确定；
- adapter payload必须与resolved target fact exact join；
- output cap不能由provider自行选择一个未知值。

本轮保留：

```text
Chat Completions -> max_completion_tokens = effective_output_tokens
Responses        -> max_output_tokens     = effective_output_tokens
```

### 14.2 本轮不调数字

默认8,192是否应提高，是model capability/config product policy，不是terminal correctness前置条件。本轮只保证：无论cap是32、8,192还是128,000，达到cap都不会被误认成完整answer/tool call。

测试可以使用极小cap构造deterministic fixture，但production default保持不变。Round 5B summary应显式请求自己的bounded output quota，而不是隐式继承普通final-answer默认值。

### 14.3 Local assembly bound

ProviderStreamAssembler当前有4 MiB completed bytes cap，normalized transport还有16 MiB sanitized source cap。它们是Runtime physical/resource circuit breaker，不是vendor output token terminal：

```text
local cap exceeded
-> cancel/drain provider
-> abort live draft
-> no assistant acceptance
-> typed local resource failure
-> no automatic retry/compaction
```

本轮不调整这些数值，也不把它们映射成OUTPUT_TOKEN_LIMIT。

reasoning replay不能成为绕过这些bounds的第二条内存通道：

- Chat reasoning text/opaque fields与Responses exact output items都计入single completed-response aggregate bytes；
- SDK解码后的outer JSON必须在进入deep freeze/continuity以前检查item count、depth、节点数与string/aggregate bytes；本文不借reasoning replay重写SDK的pre-parse transport实现；
- no silent truncation：fragment超过bound时整个response不能COMPLETED acceptance；
- installed fragment bytes计入Host continuity installed/prepared aggregate与64 MiB per-epoch bound；
- provider-wire quote同时报告semantic message基线、replacement debit与replay addend，不能双计被fragment替换的assistant semantic representation。

本文不新设一个更小的“reasoning token cap”。对opaque encrypted body做任意截断会使它不可重放；统一使用completed-response/working-set/epoch hard bounds更简单也更诚实。

---

## 15. 实施修改面

### 15.1 必改production文件

| 文件 | 修改 |
|---|---|
| [`ports/provider_stream.py`](src/pulsara_agent/ports/provider_stream.py) | closed adapter terminal、incomplete reason、normalized terminal enum/union |
| [`llm/normalized_transport.py`](src/pulsara_agent/llm/normalized_transport.py) | explicit-terminal状态机；EOF不再成功；incomplete不合成End |
| [`llm/adapters/openai/events.py`](src/pulsara_agent/llm/adapters/openai/events.py) | terminal-aware close；禁止failure/incomplete convenience close |
| [`llm/adapters/openai/chat_completions.py`](src/pulsara_agent/llm/adapters/openai/chat_completions.py) | finish reason closed mapping、single-choice terminal、per-field closed accumulation、mock parity |
| [`llm/adapters/openai/responses.py`](src/pulsara_agent/llm/adapters/openai/responses.py) | response.incomplete closed mapping、explicit complete terminal、closed item/content allowlist、exact ordered output replay |
| [`llm/provider.py`](src/pulsara_agent/llm/provider.py) | closed replay codec/scope/field accumulation、template control与profile fingerprint |
| [`llm/request.py`](src/pulsara_agent/llm/request.py) | `FrozenProviderWireMaterialization/InputPlan`与replacement quote；不污染pure compiler DTO |
| [`model_input/contracts.py`](src/pulsara_agent/model_input/contracts.py) | 等长message placement metadata与独立fingerprint |
| [`model_input/compiler.py`](src/pulsara_agent/model_input/compiler.py) | 在既有lowering顺序中生成origin placements；不读取reasoning fragment |
| [`conversation_kernel/direct_model.py`](src/pulsara_agent/conversation_kernel/direct_model.py) | pure `plan_wire_input`、same-plan transport preflight、typed terminal、physical close precedence |
| [`conversation_kernel/assembler.py`](src/pulsara_agent/conversation_kernel/assembler.py) | complete只能由caller在COMPLETED terminal后调用；public projection与fragment exact join |
| [`conversation_kernel/runner.py`](src/pulsara_agent/conversation_kernel/runner.py) | candidate顺序改为plan-first；assistant settlement admission/join；typed live abort/no-tool |
| [`conversation_kernel/host.py`](src/pulsara_agent/conversation_kernel/host.py) | Host-owned shielded assistant settlement task registry与close drain |
| [`model_input/continuity.py`](src/pulsara_agent/model_input/continuity.py) | append candidate/epoch view exact joinwire plan、prefix proof与final quote |
| [`conversation_kernel/input_continuity.py`](src/pulsara_agent/conversation_kernel/input_continuity.py) | CAS原子安装semantic view + wire proof；scope-owned fragment lifecycle |
| [`conversation_kernel/auxiliary_model.py`](src/pulsara_agent/conversation_kernel/auxiliary_model.py) | incomplete无JSON parse；typed propagation |
| [`conversation_kernel/reader.py`](src/pulsara_agent/conversation_kernel/reader.py) | 新turn terminal reasons映射到既有EXECUTION_FAILED |
| [`primitives/model_call.py`](src/pulsara_agent/primitives/model_call.py) | 仅在typed safe error carrier需要时更新closed provider code；不得把incomplete塞进generic error code |

如果implementation能把typed exception放在`ports/provider_stream.py`或现有`llm/errors.py`而不形成反向依赖，可调整物理文件；authority与union必须保持本文形状。

### 15.2 不改

- clean-v0 SQL schema；
- repository transaction topology；
- committed/live vocabulary；
- Protocol v3；
- provider-input compiler的canonical source selection、degradation与lowering authority；
- tool surface、permission、MCP generation；
- ToolResult artifact；
- Round 5B snapshot adoption；
- memory schema/governor relations。

### 15.3 Contract version

以下contract必须升级：

- Chat Completions adapter `contract_version`；
- Responses adapter `contract_version`；
- normalized live provider boundary fingerprint domain/version；
- provider assistant replay codec与Chat field-accumulation contract；
- `FrozenProviderWireInputPlan` materialization/quote contract；
- DirectModel plan/preflight/open exact-join contract；
- Round 3.1 continuity epoch/prefix fingerprint domain；
- structured compiler message-placement metadata contract；
- process-local assistant settlement candidate fingerprint domain。

原因是同一wire event现在会得到不同terminal语义，同一canonical assistant entry也可能得到一个exact provider replay fragment。升级会在下一Host cold resolve中改变transport/input compatibility；不得在已安装epoch中热换adapter或codec contract。Host安装时冻结binding，旧borrow/fragment自然drain，符合Round 3.1。

不需要source renderer/lowering semantic contract bump：SYSTEM、tools与canonical semantic messages没有变化。`FrozenCompiledModelInput`新增placement metadata，因此compiler DTO/binding contract必须升级；provider-wire materialization contract也单独升级，因为adapter最终发送的assistant representation新增或保留了exact reasoning fields/items。三者不得用同一个版本号混淆。

---

## 16. Failure matrix

| 场景 | Adapter terminal | assistant row | tool attempt/effect | turn/owner行为 |
|---|---|---:|---:|---|
| Chat stop完整正文 | COMPLETED | 1 | 0 | turn complete |
| Chat tool_calls完整JSON | COMPLETED | 1 | 按正常batch | normal tool path |
| Chat length，partial正文 | OUTPUT_INCOMPLETE/TOKEN | 0 | 0 | exact turn interrupted |
| Chat length，partial tool JSON | OUTPUT_INCOMPLETE/TOKEN | 0 | 0 | 不parse/repair |
| Chat content_filter | OUTPUT_INCOMPLETE/FILTER | 0 | 0 | exact turn interrupted |
| Chat unknown finish reason | OUTPUT_INCOMPLETE/UNKNOWN | 0 | 0 | fail closed |
| Chat EOF无finish reason | PROVIDER_ERROR/PROTOCOL | 0 | 0 | exact turn interrupted |
| Responses completed | COMPLETED | 1 | normal | normal path |
| Responses item.done(tool)后incomplete | OUTPUT_INCOMPLETE | 0 | 0 | whole response discarded |
| Responses partial arguments后incomplete | OUTPUT_INCOMPLETE | 0 | 0 | whole response discarded |
| Responses content filter | OUTPUT_INCOMPLETE/FILTER | 0 | 0 | no canonical partial |
| Responses failed/error | PROVIDER_ERROR | 0 | 0 | existing failure path |
| transport failure before output | none until retry/final failure | 0 or later 1 | 0 or later normal | bounded retry allowed |
| transport failure after output | PROVIDER_ERROR | 0 | 0 | no retry |
| caller cancel | cancellation, not incomplete | 0 | 0 | exact cancellation intent |
| semantic complete, physical BLOCKED | physical failure overrides | 0 | 0 | no acceptance |
| auxiliary partial valid JSON + incomplete | OUTPUT_INCOMPLETE | n/a | n/a | no parse/no result |
| compaction summary incomplete | OUTPUT_INCOMPLETE | n/a | n/a | no adoption, old epoch remains |
| Chat completed tool response + known reasoning carrier | COMPLETED | 1 | normal | FULL后绑定只含实际observed fields的完整assistant replay message |
| Chat completed tool response无reasoning carrier | COMPLETED | 1 | normal | 不伪造空fragment；标准Chat工具循环继续 |
| Chat known carrier malformed | PROVIDER_ERROR/CONTRACT | 0 | 0 | 不猜shape、不转成字符串 |
| Chat unknown non-empty carrier + ordinary final text | COMPLETED | 1 | 0 | 接受public text，unknown不进入replay |
| Chat unknown non-empty carrier + tool/only-output | PROVIDER_ERROR/CONTRACT | 0 | 0 | acceptance/dispatch前fail closed |
| Chat field accumulation conflict | PROVIDER_ERROR/CONTRACT | 0 | 0 | 按closed field mode fail closed |
| Chat known carrier在terminal前超过item/16 MiB aggregate bound | PROVIDER_ERROR/PAYLOAD_LIMIT | 0 | 0 | adapter立即清空bounded accumulator；不等待terminal、不retry |
| Responses completed + canonical-representable exact ordered output | COMPLETED | 1 | normal | FULL后绑定全部output items |
| Responses function_call先于message或多个message | PROVIDER_ERROR/CONTRACT | 0 | 0 | canonical acceptance前fail closed |
| Responses含unknown/hosted/effect-bearing item | PROVIDER_ERROR/CONTRACT | 0 | 0 | 不opaque接受、不重放 |
| Responses completed但final output与stream projection不一致 | PROVIDER_ERROR/CONTRACT | 0 | 0 | fragment与assistant均不接受 |
| incomplete/failure含reasoning bytes | non-COMPLETED | 0 | 0 | reasoning buffer discarded |
| assistant commit transient NONE | COMPLETED但canonical未FULL | 0 | 0 | exact retry/confirmation；fragment保持PREPARED |
| assistant commit final abandon/CONFLICT | COMPLETED但canonical未FULL | 0 | 0 | fragment discarded；不安装 |
| assistant DB physical FULL时waiter cancel | COMPLETED | 依confirmation为1 | 0直到FULL+BOUND | shielded owner确认并绑定；caller仅detach |
| semantic candidate与wire plan不exact join | n/a | 0 | 0 | register/preflight前fail；provider open=0 |
| preflight尝试rematerialize不同wire | n/a | 0 | 0 | plan fingerprint conflict；provider open=0 |
| installed fragment遇到same-schema physical reconnect | n/a | existing | existing | exact bytes不变，仅重绑compatible transport |
| Host loss/cold epoch | n/a | canonical rows与5A.2 replay row仍在 | existing results仍在 | compatible carrier由新Host exact rehydrate |
| compaction summary COMPLETED/adopted | COMPLETED | summary authority另见5B | no summary tools | old-floor fragments退出active epoch；不继承到summary正文 |

---

## 17. 测试规格

### 17.1 Adapter golden

Chat：

1. `stop`完整text；
2. `tool_calls`完整单/多tool JSON；
3. `length`发生于text、thinking、tool name后、arguments中间、一个完整tool与第二个partial tool之间；
4. `content_filter`有/无partial text；
5. unknown finish reason；
6. EOF从未出现finish reason；
7. terminal后继续chunk；
8. duplicate/conflicting finish reason；
9. mock与real translation完全同语义；
10. single-choice enforcement；
11. text reasoning field exact replay；
12. structured opaque reasoning list exact replay，不发生string coercion；
13. absent、present-empty、malformed与unknown reasoning field的closed matrix；
14. replayed complete assistant message的public projection与canonical assembler exact equal；
15. TEXT_CONCAT多chunk顺序、empty delta与non-string failure；
16. ORDERED_ARRAY_APPEND保持element/chunk顺序，不去重；
17. final field与accumulator exact reconcile；
18. unknown empty、ordinary final ignore、tool continuation fail-closed matrix。

Responses：

1. normal completed text/tool；
2. incomplete reason分别为max output、context window、content filter、unknown、missing；
3. partial tool arguments后incomplete；
4.完整`output_item.done(function_call)`后incomplete；
5. completed item A + partial item B + incomplete；
6. failed/error；
7. EOF无response terminal；
8. terminal后event；
9. incomplete usage仍被观测但不被接受；
10. final `response.output`全部item按原序深冻结；
11. reasoning任意位置、单message位于全部multiple function_call之前时exact replay；function_call先于message及multiple message fail closed；
12. encrypted/opaque fields只做bounded freeze/thaw，不进入live text；
13. streamed item与final response item不一致fail closed；
14. manual replay payload不含`previous_response_id`；
15. outer item只接受reasoning/message/function_call；
16. message content只接受closed output_text/explicit alias；
17. refusal、hosted/computer/program及unknown item/content均fail closed且无assistant/attempt。

### 17.2 Normalized transport

1. EOF不能推导COMPLETED；
2. exactly-one terminal；
3. COMPLETED要求无open blocks；
4. INCOMPLETE允许open blocks但不得合成End；
5. failure不合成End；
6. terminal union验证；
7. usage缺失/存在；
8. physical COMPLETED/BLOCKED独立于semantic terminal；
9. payload/usage circuit breaker仍生效；
10. incomplete/failure以后provisional replay builder为空；
11. completed fragment与terminal一一对应；
12. opaque replay bytes计入completed-response aggregate bound。

### 17.3 DirectModel与Runner

对ROOT与SUBAGENT_TASK至少覆盖：

- incomplete text：无assistant entry；
- incomplete thinking：无assistant entry；
- partial tool：无assistant entry、attempt、result、physical invocation；
- completed tool item后response incomplete：仍无attempt/effect；
- complete multi-tool response：现有happy path不回归；
- provider error before/aftersemantic output；
- cancellation cause不被incomplete覆盖；
- live draft最后ABORTED；
- turn terminal reason exact；
-下一真实ROOT message读取Round 7 existing EXECUTION_FAILED outcome；
- continuity SYSTEM/tools不变，messages只追加下一用户消息与Runtime failure source，不重写旧prefix。
- completed assistant FULL才产生BOUND fragment；transient NONE保持prepared，final abandon/CONFLICT无fragment；
- next tool-loop dispatch把fragment放在exact assistant位置，随后才是tool result；
- installed fragment在后续USER、tool loop与automatic continuation中byte/structure equal；
- same-schema reconnect只更换physical transport，不更换fragment；
- Host close/cold reset释放process-local fragment；Round 5A.2 compatible durable row可由下一Host重新hydrate；
- fragment token/byte计量进入provider-wire quote与continuity proof，且不双计被替换的assistant representation。
- `plan_wire_input`在candidate/register以前完成，且不取得transport/open authority；
- candidate、preflight、install与physical open全部exact join同一个plan fingerprint/materialization identity；
- semantic view相同但replacement order、wire fingerprint或quote不同得到CONFLICT/open=0；
- installed epoch同时证明semantic prefix与wire prefix/bytes/tokens；不存在semantic-only INSTALLED；
- assistant write physical FULL与waiter cancellation竞态：shielded settlement仍确认FULL、promote BOUND且不执行未获准tool；
- assistant settlement transient NONE、CONFLICT、Host close drain与caller多waiter join。

### 17.4 Retry

1. retryable transport exception、零semantic output：按现有budget retry；
2. 同一异常但已有text/thinking/tool delta：不retry；
3. OUTPUT_INCOMPLETE、零delta：不retry；
4. OUTPUT_INCOMPLETE、有delta：不retry；
5. content filter：不retry；
6. unknown incomplete：不retry；
7. retry没有改变resolved call/input/tool-surface identity。

### 17.5 Auxiliary

1. partial body恰好是合法JSON但terminal incomplete：拒绝；
2. completed invalid JSON：现有validation failure；
3. memory governance incomplete：foreground conversation正常；
4. hint reflection incomplete：无candidate/无conversation failure；
5. future summary incomplete：dormant contract断言无adoption candidate。

### 17.6 Provider-shaped local SSE

不依赖真实remote provider作为correctness gate。使用bounded本地SSE/SDK fixture精确产生：

- Chat `finish_reason=length`；
- Responses `response.incomplete`；
- stream在terminal前EOF；
- terminal后额外event；
-持续data后semantic incomplete且physical正常close。

若进行真实provider dogfood，只记录adapter kind、closed terminal、usage状态与tool physical call count；不得记录API key、header、raw error body、完整prompt、partial reasoning/tool arguments或provider response正文。

### 17.7 Architecture gates

必须机器证明：

```text
adapter EOF cannot construct COMPLETED
OUTPUT_INCOMPLETE cannot call close_active_blocks/tool_calls
Runner cannot call commit_assistant_message without COMPLETED
tool attempt/invoke cannot be reached from incomplete terminal
auxiliary JSON parse cannot run on incomplete terminal
provider terminal cannot trigger compaction
5A.1 historical repository did not store ProviderAssistantReplayFragment;
Round 5A.2 repository stores only its sealed durable row contract
replay fragment cannot be built before COMPLETED terminal
replay fragment cannot install before assistant FULL confirmation
installed replay fragment cannot be omitted/degraded inside an epoch
continuity candidate cannot register without a FrozenProviderWireInputPlan
preflight/open cannot rematerialize provider input outside the frozen plan
INSTALLED epoch cannot contain semantic view without matching wire proof/quote
assistant repository mutation cannot bypass AssistantMessageSettlementAttempt
caller cancellation cannot cancel the assistant settlement owner
tool authorize/attempt/invoke cannot precede FULL + optional fragment BOUND
Responses replay cannot use previous_response_id as history authority
Responses accepted output item types are exactly reasoning/message/function_call
opaque reasoning cannot enter live text, canonical assistant blocks or public diagnostics
5A.1 historical activation added no schema/event/job/guard/subject/relation
5A.1 historical oracle == 31 / 23 / 13 / 2 / 25 / 1
```

### 17.8 Notebook与multi-provider conformance probes

correctness首先由local provider-shaped fixtures证明；远端probe只做profile conformance与cache observation。activation至少覆盖：

1. Qwen-style template golden：tool role保留current-turn reasoning；USER role形成新turn；固定`preserve_thinking`时旧assistant representation不随last-user重渲染；
2. DeepSeek Chat：完整assistant tool call带`reasoning_content`可继续；剥离得到预期provider拒绝，证明Pulsara实现确实带回字段；
3. DashScope Chat：manual field replay与stripped control产生可区分结果；不使用response ID作为正确性前提；
4. OpenRouter Chat：fixture必须证明structured `reasoning_details`按ordered array原样累积/重放；remote response若实际返回该carrier则记录观测，否则标准无carrier tool response仍必须完成且不得伪造空fragment；
5. OpenRouter Responses `store=false`：encrypted reasoning item + complete ordered output手工重放成功；`previous_response_id`保持null；
6. bobdong Responses：手工replay成功即可；不因proxy reported cache/ID行为不稳定改变Runtime contract；
7. DeepSeek/DashScope Responses空壳reasoning item不得被误报成“reasoning continuity supported”；
8. stable prefix两次以上调用记录available cache counters；只断言结构前缀与counter类型合法，不断言固定命中token数。

remote probe固定使用无副作用virtual empty tool与随机一次性marker；不得调用真实builtin/MCP/Terminal，不得输出marker、reasoning body、request payload、API key、base URL secrets或完整response。测试报告只保留endpoint profile ID、wire API、codec kind、HTTP/semantic outcome、bounded byte counts与usage/cache数字。

---

## 18. Activation顺序

### R5A.1-0：冻结契约与fixtures

- 新DTO、terminal matrix、vendor reason与replay codec golden；
- 从`retain-cot.ipynb`提取Qwen role/template与DeepSeek required-field最小fixtures；
- 冻结Responses ordered output-item与Chat opaque-fields的bounded JSON vectors；
- 冻结Chat三种field accumulation与Responses V1 item/content allowlist；
- 冻结provider-wire plan、replacement quote与assistant settlement candidate vectors；
- 更新adapter/normalized contract version；
- architecture tests先红；
- 记录checkpoint与当前dirty files。

### R5A.1-1：Adapter与normalized transport

- Chat explicit terminal；
- Responses explicit terminal；
- Chat exact assistant replay fragment；
- Responses exact ordered output replay fragment；
- Chat accumulation/final reconciliation；
- Responses unknown/effect-bearing item fail closed；
- EOF fail closed；
- usage与physical completion保持；
- 单元与local SSE测试通过。

### R5A.1-2：DirectModel、Runner与auxiliary

- typed propagation；
- whole-response no-commit/no-tool；
- assistant FULL fragment binding、transient NONE confirmation、final abandon/CONFLICT discard；
- Host-owned shielded assistant settlement与cancel/close drain；
- plan-first candidate顺序、same-plan provider preflight/wire quote；
- continuity epoch原子安装semantic view + wire proof并retain/close；
- turn terminal reasons；
- memory weak-failure行为；
- retained provider/tool happy path通过。

### R5A.1-3：Prefix与Round 5B前置证据

- Chat/Responses strict-prefix；
- installed reasoning/assistant fragments保持exact prefix；
- incomplete summary no candidate dormant test；
- future summary使用旧epoch fragments、adoption后cold discard的dormant contract；
- 四组endpoint进行bounded manual-replay/cache dogfood；remote ID不作为gate；
- Round 3/3.1/5A/7/8 retained tests；
- full pytest、PostgreSQL、Ruff、compileall、Protocol generator、Go test/vet/module verify、lock/diff/Markdown/secret scan。

只有R5A.1 activation evidence完成后，Round 5B才可把summary call接到snapshot adoption。

---

## 19. Non-goals

本轮明确不实现：

- partial final answer自动续写；
- canonical incomplete assistant message；
- partial tool-call salvage；
- Codex式per-output-item durable adoption；
- remote response ID/previous_response_id continuation或server-held history正确性依赖；
- provider-error reactive compact；
- context compaction或snapshot adoption；
- 自动提高output cap后重试；
- hidden reasoning作为conversation semantic truth、event、memory、artifact或summary正文；跨Host private provider-replay persistence现由Round 5A.2单独拥有；
- hidden reasoning公开展示、artifact化、memory提取或summary复制；
- Chat/Responses reasoning carrier互转；
- 使用`<think>`普通正文伪造reasoning；
- 根据provider名称、model名称或未注册字段动态猜reasoning carrier；Chat只接受本文冻结的三个全局closed字段及其固定shape；
- partial provider trace file；
- output budget价格治理；
- fallback model；
- provider-side silent truncation；
- 新TUI功能。

如果未来实现“半截最终答案自动续写”，必须作为独立产品契约讨论：它需要明确partial正文是否canonical、continuation如何exact引用、工具是否允许、用户是否已经看到重复片段，以及重试时如何避免effect ambiguity。不得借Round 5B compaction user message偷偷实现。

---

## 20. Definition of Done

Round 5A.1只有同时满足以下条件才能标记ACTIVATED：

1. Chat与Responses都显式发出唯一adapter terminal；EOF不是success。
2. `finish_reason=length`与`response.incomplete(max_output_tokens)`得到同一个provider-neutral OUTPUT_TOKEN_LIMIT。
3. content filter、generation-time context limit与unknown incomplete拥有closed branch。
4. provider failure与output incomplete不再共用generic string error。
5. incomplete text/thinking/data/tool response不会产生assistant row。
6. 一个完整tool item后整response incomplete仍不会产生attempt或physical effect。
7. ordinary incomplete response不自动retry、不触发compaction。
8. transport retry仍只发生在semantic output前。
9. auxiliary partial JSON不会被parse/采用。
10. future compaction summary incomplete有机器证明：candidate absent、old epoch unchanged。
11. semantic terminal与physical close都必须完成；physical BLOCKED不能接受response。
12. Round 3.1不变量保持：同epoch SYSTEM相等、tools相等、messages只追加suffix。
13. adapter/normalized contract version诚实升级。
14. production output cap继续显式发送，默认数值不在本轮漂移。
15. Chat text与structured reasoning共享一个provider-neutral closed codec，但每个field保持独立shape；任何array value都没有string coercion。
16. Chat三个replay field分别冻结`TEXT_CONCAT | TEXT_CONCAT | ORDERED_ARRAY_APPEND`并exact reconcile final value；重复/冲突shape fail closed。
17. Responses V1 accepted output item严格限于`reasoning | message | function_call`，message content也使用closed allowlist；未知或effect-bearing item不被opaque接受。
18. 对已接受的Responses complete assistant response，全部ordered output items原样重放，不能只重建reasoning或function call的一半。
19. 只有COMPLETED + canonical assistant FULL能产生BOUND fragment；incomplete/failure/final abandon/CONFLICT均证明fragment absent/discarded，transient NONE只能保留同一prepared candidate继续确认。
20. assistant write/confirmation由Host-owned shielded settlement attempt唯一拥有；waiter cancellation只detach，tool path只能在FULL + optional BOUND以后开始。
21. `FrozenProviderWireInputPlan`必须在continuity candidate以前完成，唯一持有actual materialization、replacement identities与final quote；preflight/open不得重新materialize。
22. continuity CAS原子安装semantic epoch view与matching wire plan proof/bytes/tokens；不存在semantic-only INSTALLED状态。
23. same scope/epoch后续调用对已安装fragment byte/structure exact equal；provider wire仍满足old input为new input的prefix。
24. ToolResult保持`role=tool`；Qwen-style template control在epoch内冻结，不因last USER位置动态删旧reasoning。
25. Chat opaque/text reasoning accumulator在terminal前执行共享的16 MiB完整响应byte bound与65,536项fragment/element circuit breaker；text使用chunk list，unknown carrier不累计名称集合，越界为typed non-retryable failure。
26. manual full-history replay是Chat/Responses activation gate；`previous_response_id`、remote session/state均不是correctness依赖。
27. hidden reasoning不进入conversation semantic row、event、memory、artifact、live public text、summary正文或普通diagnostic/log；Round 5A.2只允许独立private provider-replay row。
28. replay fragment bytes/tokens进入wire plan、continuity aggregate与Round 5B pressure quote，且replacement debit/addend不重复计算generic assistant representation。
29. future compaction summary使用旧epoch exact fragment；adoption后process-local fragment与remote ID释放，旧floor durable row不进入新epoch；普通Host restart则由Round 5A.2 exact rehydrate compatible fragment。
30. bounded DeepSeek、DashScope、OpenRouter与bobdong probes只验证两种OpenAI wire codec；远端cache/ID差异只记录，不形成vendor分支、preset或不同persistence policy。
31. 无schema/event/relation/job/guard/subject/Protocol增加；oracle保持`31 / 23 / 13 / 2 / 25 / 1`。
32. activation evidence包含code hash、targeted/full/PostgreSQL/architecture结果、notebook/endpoint profile checkpoint及无敏感内容的provider-shaped probe记录。

完成5A.1以后，Pulsara具备第一个Round 5B前提：**Runtime不会把模型“说到一半”误认为已经完成，也不会让半个工具调用跨过canonical effect边界。**Round 5A.2进一步补齐第二个前提：completed provider reasoning/work carrier可跨进程恢复，但仍只是private provider-replay truth，不进入assistant正文、summary或其他业务authority。
