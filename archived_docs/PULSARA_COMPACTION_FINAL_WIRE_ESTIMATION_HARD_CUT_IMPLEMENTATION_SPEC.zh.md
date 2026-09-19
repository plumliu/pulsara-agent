# Pulsara Compaction Final-Wire 本地估算 Hard-Cut 设计与实施规范

> Epoch-boundary hard-cut 覆盖（2026-09-19）：provider-input epoch 的当前唯一权威是 [`PULSARA_PROVIDER_INPUT_EPOCH_BOUNDARIES_AND_CANONICAL_REPROJECTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_PROVIDER_INPUT_EPOCH_BOUNDARIES_AND_CANONICAL_REPROJECTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。任何把 dry quote、final-wire estimation 或 compatibility 差异当作 epoch authority 的旧解释均为 historical/superseded；只有 adoption FULL 后的 sealed successor authority 可安装 compaction successor，不保留兼容路径。其余不冲突语义继续有效。

> 状态：生产 hard cut 已实施；K4 Kernel 验收于 2026-09-15 通过，证据见 [K4 验收记录](PULSARA_KERNEL_IMAGE_INPUT_K4_ACCEPTANCE.zh.md)；U1/U2 浏览器链路另行验收。
>
> 图片输入修订（2026-09-14）：
> `PULSARA_KERNEL_IMAGE_INPUT_AND_OPENAI_WIRE_ADAPTER_DESIGN.zh.md` 第 7、9、9.3 节
> 已将唯一生产 estimator hard cut 为 `pulsara_heuristic/v2`，冻结 D1 图片尺寸估值、正式
> USER 图片位置识别、base64 payload 扣减与逐 item/occurrence 计量。Chat/Responses 的
> adapter-owned materialization、generic/native replacement、direct traversal、PRE_FULL、
> POST_FULL 与效果前 U_W/U_T 均消费同一 quote；生产 V1 路径已删除。真实 provider、
> isolated wheel 与完整矩阵仍属于 K4，不能据此标记图片输入已激活。
>
> 范围：Conversation Kernel、provider-input planning、durable provider replay、context compaction
>
> 权威关系：本文收紧 `ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md` 中已经要求、但生产代码尚未完整实现的 final-wire quote 契约，并取代其中与下述阶段化deadline边界冲突的条款。与本文冲突的旧 token 计量、跨stream absolute deadline与20分钟summary总时限一律删除，不保留双路径或兼容别名。
>
> 核心决定：**Pulsara 永远使用统一的本地 estimator，但必须对最终真正要发送的 wire materialization 估算，而不能只估算前一层 semantic representation。**
>
> Deadline勘误：本文同时废止Round 5B中“持续健康summary仍须在20分钟内完成”以及任何“planning absolute deadline跨越summary stream”的解释。有限planning watchdog只拦截有明确物理边界的局部异常阻塞，不能充当正常逻辑工作的累计寿命上限；健康provider stream与整个task/turn均无total wall-clock lifetime cap。

---

## 0. 一句话结论

Pulsara 不向 provider 请求“真实 token 数”，不接入厂商 tokenizer，也不把 provider 响应中的 `usage` 变成压缩 authority。Runtime 在 provider open 之前已经能够本地冻结 adapter 最终消费的 exact context-bearing wire materialization；compaction 必须复用普通 provider dispatch 的同一 wire lowering、durable replay selection/hydration 与同一 `TokenEstimator`，直接遍历该最终 materialization 产生 process-local quote，并以这一 quote 统一完成：

1. 自动压缩 trigger；
2. 手动压缩 minimum reclaim；
3. 实际 successor 的 pre-adoption reclaim gate；
4. post-adoption active successor 的最终 budget/reclaim 复验；
5. 普通 provider open 的 hard budget admission。

`semantic estimate` 继续服务 semantic compiler、source/tail 选择与诊断，但不得再冒充 provider 最终可见输入的 token quote。

---

## 1. 问题背景与已确认事实

### 1.1 用户可见故障

会话 `session:8634559fe0b543c38cd494c97345ea54` 已确认出现以下事实：

- 当前 canonical transcript 有 44 条记录、6 个 turn；
- `context_snapshots = 0`，从未采用过 compaction snapshot；
- canonical/semantic message estimate 约为 `12,775` tokens；
- provider 最终 wire message estimate 约为 `56,230` tokens；
- provider-native replay addend 约为 `51,552` tokens，替换的 generic assistant semantic debit 约为 `8,097` tokens；
- 隐藏在 semantic estimate 之外的净增量约为 `43,455` tokens；
- 手动 `COMPACT_CONTEXT(force=true)` 返回 `NOT_NEEDED / CONTEXT_ALREADY_COMPACT`；
- UI 因而显示“当前上下文已经较紧凑，本次整理无法进一步缩小”。

这个结果不是 role、prompt、manual/auto 分叉或 provider 返回值问题。它是 source/reclaim gate 使用了错误计量层级。

### 1.2 当前生产实现的正确部分

以下行为必须保留：

- manual 与 automatic 最终进入同一个 `CompactionCoordinator._execute_compaction_fenced()`；
- active 与 idle 在 canonical FULL 之前使用同一 summary、candidate、cold assembler、reclaim validator 与 tail-shrink search；
- summary request 是临时追加的 `user` role message；
- summary output 作为 advisory `CONTEXT_SNAPSHOT`，在 successor 中仍降低为 `user` role Runtime handoff，不提升为 system authority；
- compaction successor 是合法 root rebuild boundary；
- system、tools、Skill、MCP、memory、permission、Plan 与 Runtime facts 按各自既有 authority/channel 重新组装，而不是全部塞入 system；
- canonical transcript 永不因 compaction 删除或改写；
- snapshot、binding revision、turn pointer 与 adoption event 原子提交；
- provider replay 继续使用 Round 5A.2 既有 durable entry-bound relation，本文不建立第二套 replay registry。

### 1.3 当前生产实现的错误部分

`FrozenCompactionSourceView.provider_projection.final_estimate.total_input_tokens` 只计量 semantic compiler 输出。它不包含最终 provider wire 中对 assistant message 的 native replay replacement。

当前错误消费者包括但不限于：

- `should_trigger_compaction()` 的 token ratio；
- `dispatch_crosses_threshold()` 的 late threshold；
- summary 前 `estimate_unavoidable_compaction_successor_tokens()` 的 source side；
- actual dry successor reclaim；
- settlement 前保存的 `source_tokens`；
- post-adoption active successor 的 reclaim 复验。

普通 provider dispatch 则在稍后的 `_plan_provider_wire_input()` 中执行：

```text
final estimated input tokens
  = semantic estimated input tokens
  - replaced generic semantic debit
  + selected native replay addend
```

这一步至少把 selected native replay 纳入了普通 send，但它仍是“semantic base + replay 修正”的混合算法；estimator 并没有直接估算全部最终 wire objects。本文 hard cut 后，这个公式本身也必须删除。Normal send 与 compaction decision 都只能对同一个 final wire materialization 做同一套直接本地估算。

---

## 2. 用户真正担心的边界

“使用 provider-wire quote”不能被解释为以下任一方案：

1. 调用 OpenAI、Anthropic、Gemini、DeepSeek 或其他 provider 的 token-count endpoint；
2. 引入按 provider 名称分支的 tokenizer；
3. 使用 response `usage.input_tokens` 决定当前尚未发送的请求是否压缩；
4. 用上一请求的 provider usage 推测下一请求；
5. 用 cached/uncached billing tokens 替代 context admission；
6. 让 usage 缺失的 provider 退回 semantic estimate；
7. 把某一 provider 的“实测 token”写成 durable canonical truth；
8. 因 provider usage 与本地 estimate 不一致而重写已安装 epoch。

原因不是审美，而是正确性：

- compaction 必须发生在 provider open 之前；
- provider usage 即使存在，也只在请求开始或完成之后返回；
- usage 并非跨 provider、跨 wire API、跨兼容服务的统一必选协议；
- usage 可能包含缓存、reasoning、计费或供应商归一化差异；
- idle manual compaction 没有 ordinary provider call 可供“顺便读取”usage；
- post-response 事实无法为一个尚未发送的 candidate 提供 exact preflight authority。

因此本文冻结以下边界：

> **Final-wire quote 是 Pulsara 对 exact final wire materialization 的本地一致估算，不是 provider-reported exact token count。**

---

## 3. 术语与 truth 分层

### 3.1 Semantic representation

由 structured model-input compiler 产生：

```text
system_prompt
LLMMessage[]
FrozenToolSpec[]
message placements
source/tool-result decisions
semantic TokenEstimate
```

它是 provider-neutral semantic truth，允许 compaction projection 超过 effective input budget；它不拥有 executor、continuity install、provider open 或 transport authority。

### 3.2 Final wire materialization

由 resolved target 的 `wire_api` 与 provider profile 将 semantic input 降低为：

```text
root policy value
provider-native tool objects
ordered provider input objects
selected durable provider replay replacements
```

这里的“最终”不是指 pre-lowering 的 `LLMMessage[]`，也不是只把 replay payload 算出来后贴回 semantic total。它指 adapter 在构造请求正文时实际消费的、已经完成下列动作的 context-bearing JSON 值：

- root 已按 Chat system message 或 Responses instructions 的协议位置落位；
- tool specs 已变成当前 wire API 的 native tool objects，并按实际 omission 规则出现或省略；
- generic messages 已变成当前 wire API 的 ordered input objects；
- selected durable replay 已替换对应 generic assistant projection；
- 顺序、空值、省略与 JSON 形状与 transport builder 一致。

模型路由 ID、stream 开关、timeout、输出上限等 transport control fields 不属于 context input token materialization，不进入这项 quote；如果某个 request default 会改变模型可见输入，它必须由 wire adapter 明确纳入 context-bearing projection。这个边界必须由 adapter 的共享纯函数所有，不能由 compaction 猜测。

当前支持的 wire protocol family 为 OpenAI Chat Completions 与 OpenAI Responses。按 `wire_api` 做协议 lowering 是 transport contract，不是按 provider 厂商特供产品语义。禁止增加 `if provider == ...` 的 compaction 分支。

### 3.3 Exact wire bytes

对上述 exact context-bearing materialization 做 canonical JSON encoding 后的 UTF-8 字节数。该值由本地完整值确定，是 exact process-local fact。不得继续把内部 normalized `{"root", "tools", "input"}` 包装器的字节数称为“最终 wire bytes”，除非该包装器就是 transport builder 实际消费且逐字映射的唯一冻结值；测试必须证明 adapter 不会再次改变 root 位置、tool omission 或 ordered input。

它可用于：

- hard physical byte bound；
- 证明 candidate wire materialization 严格缩小；
- 诊断 semantic/wire 膨胀来源。

它不能单独替代 provider context token budget。

### 3.4 Locally estimated final-wire tokens

使用 resolved target 携带的同一个 `TokenEstimator`，直接估算 final context-bearing wire materialization 的每个已落位 JSON component。V2 quote 的 authority 计算不得从 `compiled_input.final_estimate.total_input_tokens` 起步。

允许为诊断同时物化“未应用 replay 的 generic wire candidate”，并使用同一个 estimator 计算：

```text
final_wire_estimated_input_tokens
  = generic_wire_estimated_input_tokens
  - replaced_generic_wire_estimated_tokens
  + replay_wire_estimated_tokens
```

但三个操作数都必须来自已经 wire-lowered 的 JSON values；`replaced_generic_wire_estimated_tokens` 不能再读取 semantic `message_tokens_by_index`。无 replay 时，generic wire 与 final wire 自然相等。

Estimator 的统一调用契约为：按 adapter 冻结的 root/tool/input component 边界，使用 estimator 自己的 JSON/text primitives 与固定 request framing 规则做 additive traversal。该 traversal 必须由唯一代码契约冻结，并由 normal send与compaction共用；不得在两个调用点分别拼算。直接对全部 final components 求和是为了保持 replacement arithmetic可验证，不能用一次整包估算后再用另一套分项算法制造不可闭合的 debit/addend。

它是：

- 本地的；
- deterministic 的；
- 可在 provider open 前获得的；
- 与 normal send 使用相同输入形态的；
- 对 source/successor 可比的；
- 仍然是 estimate，而非 provider tokenizer exact count。

### 3.5 Provider-reported usage

provider 响应中可选的 post-hoc observation。Runtime 可以继续归一化并展示，但其 authority 仅限 telemetry/diagnostics，不参与：

- trigger；
- reclaim；
- candidate adoption；
- input budget admission；
- estimator 切换；
- epoch compatibility；
- canonical snapshot identity。

---

## 4. Hard-cut 不变量

实现完成后必须同时满足：

### 4.1 单 estimator

同一 resolved model target 只有一个 `TokenEstimatorFact` 与一个 estimator object。Semantic estimate、generic wire estimate、wire replacement estimate、source quote 与 successor quote必须引用同一个 `estimator_fingerprint`。

不得建立：

- compaction-only estimator；
- provider-usage estimator；
- replay-only tokenizer；
- Chat 与 Responses 不同的经验倍率；
- 中文、英文、reasoning 或 tool JSON 的调用点私有倍率。

### 4.2 单 final-wire lowering

普通 provider send、summary call、compaction source quote 与 successor quote必须复用同一 wire lowering/replay replacement core。

不得复制以下算法：

- semantic message 到 Chat/Responses wire group 的转换；
- native tool projection；
- replay manifest compatibility selection；
- generic wire component estimate；
- replaced generic wire estimate；
- replay wire estimate；
- final wire byte encoding。

### 4.3 Quote 不等于执行 authority

一个 quote 可以：

- 超过 effective token budget；
- 超过 executable provider-wire byte bound；
- 来自 semantic-only、无 executor borrow 的 compaction projection。

Quote 本身不得：

- 注册 continuity candidate；
- 签发 install permit；
- 取得 tool executor borrow；
- 安装 ToolResult delivery；
- 打开 transport；
- 发送 provider request。

只有 executable `FrozenProviderWireInputPlan` 加 existing install authority 才能进入 normal provider open。Executable plan 必须在注册/安装前拒绝 over-budget quote。

### 4.4 自动与手动统一

Manual 与 automatic 的差异只允许是：

| 维度 | manual force | automatic |
|---|---|---|
| trigger ratio | 跳过 | 必须达到 token ratio 或 resource headroom |
| post target 0.55 | 跳过 | 必须满足 |
| minimum reclaim | 不跳过，除非 source 已越 hard budget | 不跳过，除非 source 已越 hard budget |
| exact wire byte shrink | 必须满足 | 必须满足 |
| summary/candidate/adoption path | 相同 | 相同 |

不得为 manual 建立 semantic fallback，也不得只修手动按钮。

### 4.5 Active 与 idle 统一

FULL 之前 active/idle 使用完全相同的：

- source wire quote；
- tail/prefix selection；
- summary request；
- summary output validation；
- cold successor assembly；
- successor wire quote；
- reclaim validator；
- tail shrink；
- canonical adoption transaction。

FULL 之后才允许：

- active：重读 current facts、生成最终 quote、安装 successor 并继续 same turn；
- idle：关闭 proof/borrow，不 provider open，未来 user message 从 snapshot cold-open。

### 4.6 同 epoch prefix continuity

本 hard cut 不新增 rebase boundary。仍只有：

1. new cold epoch；
2. explicitly adopted compaction successor。

同一 epoch 内继续要求：

```text
SYSTEM[n+1] == SYSTEM[n]
tools[n+1] == tools[n]
messages[n+1] == messages[n] + append-only suffix
```

Final-wire quote 只观察 exact candidate，不重写 installed prefix。

---

## 5. 目标流程

### 5.1 普通与压缩共享的 quote core

```text
resolved call / compile binding / native projection set
                         +
semantic input (compiled or over-budget projection)
                         +
exact canonical dispatch read + replay target
                         |
                         v
select target-compatible replay manifests
                         |
                         v
hydrate selected replay bodies in one read-only transaction
                         |
                         v
shared wire lowering + replay replacement
                         |
                         +--> adapter-owned final context-bearing materialization
                         |
                         +--> exact final materialization bytes
                         |
                         +--> direct local estimate of final wire components
```

无 compatible replay 时仍经过同一 core，结果自然为generic wire estimate等于final wire estimate、replacement debit/addend均为0。

### 5.2 Compaction 总流程

```text
AUTO/MID_TURN handle-free final-wire precheck --below--> ordinary dispatch
                         |
                       trigger candidate
                         |
                         v
              acquire summary lane + install exact-scope fence
                         |
                         v
              recapture exact cut and re-quote under fence
                         |
MANUAL(force=true) -------+--> shared fenced compaction
                                               |
                                               v
freeze exact canonical cut + source semantic projection
                                               |
                                               v
freeze exact non-executable source final-wire quote
                                               |
                                               v
enumerate safe prefix boundaries; exact final-wire fit search
select one executable summary plan and transfer it to open_once()
                                               |
                                               v
append temporary USER compaction request and call summary model
                                               |
                                               v
freeze summary + continuation + recent humans as CONTEXT_SNAPSHOT
                                               |
                                               v
shared cold assembler rebuilds current sources/tools/skills/MCP
                                               |
                                               v
freeze exact non-executable successor final-wire quote
                                               |
                                               v
wire-byte shrink + local-estimated-token reclaim validation
                                               |
                                               v
atomic snapshot/binding/event adoption
                                               |
                       +-----------------------+------------------+
                       |                                          |
                     active                                      idle
                       |                                          |
post-FULL exact re-read/current final-wire quote          release, no provider open
install successor/open same turn                          await next user
```

---

## 6. 类型与 API hard cut

### 6.1 `FrozenProviderWireInputQuote` 语义收紧

位置：`src/pulsara_agent/llm/request.py`

保留单一 quote 类型，不新增 provider-specific quote。字段名必须明确 token 是 estimate，并把“semantic base + correction”的旧公开形状一次 hard cut改为：

```text
wire_api
estimator_fingerprint
effective_input_budget_tokens

semantic_estimated_input_tokens          # 只用于诊断与exact join
generic_wire_estimated_input_tokens
replaced_generic_wire_estimated_tokens
replay_wire_estimated_tokens
final_wire_estimated_input_tokens        # admission/trigger/reclaim authority

final_wire_utf8_bytes
```

Quote的Python类型形状就是V2 contract。现有 `quote_contract_version` 若没有跨进程或独立协议消费者，则与旧的无 `estimated` 字段名一起删除，不保留property alias或固定literal字段；不得为了表达“V2”新增自证式fingerprint。

`FrozenProviderWireReplacementIdentity` 同步hard cut：

```text
semantic_debit_tokens       -> generic_wire_estimated_tokens
replay_addend_tokens        -> replay_wire_estimated_tokens
semantic_debit_utf8_bytes   -> generic_wire_utf8_bytes
replay_addend_utf8_bytes    -> replay_wire_utf8_bytes
```

这些分项的输入必须是wire objects；旧字段不仅命名过时，实际还读取semantic `message_tokens_by_index`，必须连算法一起删除。若byte分项没有quote校验、diagnostic或identity消费者，则按subtraction guard直接删除byte分项，不为“看起来完整”保留冗余值。

同时删除只被plan aggregate identity再次读取的`generic_message_group_fingerprint`与`replacement_wire_fingerprint`。Final materialization与唯一adapter wire-prefix identity已经在同一个plan中，same-process replacement无需再自证。`replay_fragment_fingerprint`属于durable replay body不随consumer完整携带时的integrity/join边界，按既有独立continuity consumer保留。若稳定external/canonical ID确需旧值，只能由其唯一builder从仍在手的完整wire value即时派生，不能把冗余字段留在replacement DTO。

Quote constructor 只验证：

- 非负；
- wire API属于已支持的closed protocol family；
- estimator fingerprint；
- `final wire = generic wire - replaced generic wire + replay wire` 算术恒等；
- exact byte值合法。

`semantic_estimated_input_tokens` 不参与final-wire算术，只用于暴露semantic/wire偏差与exact join source projection。Quote constructor **不得**因 `final_wire_estimated_input_tokens > effective_input_budget_tokens` 而拒绝，因为 compaction 必须能描述 over-budget source。Quote constructor也不得因 `final_wire_utf8_bytes` 越 executable hard bound而拒绝；它只是measurement。

V2 traversal的代码契约固定“如何遍历adapter-owned wire components”，`estimator_fingerprint` 只证明使用哪个统一local estimator及其JSON/text primitives。本文不为了这个调用边界另建第二个estimator、quote fingerprint或版本registry。

Target、profile、canonical cut、compile binding与placements不复制到quote。Producer和consumer已经共享完整的exact prepared dispatch/source-view对象；其真实性由outer typed value与object ownership/equality负责，summary期间也只是同一个process-local frozen source-view carrier继续存活，不新增parent proof。`ProviderWireMeasurement` 在同时持有materialization与quote时直接逐字段复算bytes/estimate；materialization被有意丢弃后，不再给quote补一个hash来模拟已经释放的完整值。

### 6.2 Executable plan admission

`FrozenProviderWireInputPlan` 或其唯一 executable factory 必须验证：

```text
quote.final_wire_estimated_input_tokens
    <= quote.effective_input_budget_tokens
quote.final_wire_utf8_bytes <= MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
```

同一检查必须发生在：

- continuity candidate registration 之前；
- install permit 之前；
- provider transport open 之前。

不得先注册 over-budget candidate 再依赖 exception cleanup 作为正常控制流。

现有ordinary semantic `FrozenCompiledModelInput.final_estimate <= budget` 检查保留为compiler选择/降级optional sources时的planning allocation bound，但它不再是provider admission proof，也不得绕过wire quote。即使semantic compiler已经fit，executable plan仍必须按final wire拒绝；over-budget source则由non-executable semantic projection进入compaction quote，不得为了取得quote伪造普通compiled input。

唯一例外不是ordinary input，而是§6.11定义的ephemeral compaction-summary semantic carrier：它可以semantic-over-budget，且只有同一candidate的final-wire plan通过本节hard admission后才能promotion为summary-open authority。不得为此放宽`FrozenCompiledModelInput`、ordinary compiler或ordinary `validate_model_context_for_call()`。

### 6.3 Structural semantic input seam

Wire quote core需要读取的字段只有：

```text
canonical_input_identity
system_prompt
messages
message_placements
tools
final_estimate
compile_binding_fingerprint
```

`FrozenCompiledModelInput` 与 `FrozenModelInputSemanticProjection` 已经携带这些完整值。实现应使用窄的 structural Protocol/helper input，而不是伪造 `FrozenCompiledModelInput`、复制完整 DTO 或给 semantic projection 签发 executable identity。

### 6.4 Replay selection/hydration seam

以下函数必须接受上述 structural semantic input，而不要求 executable `FrozenCompiledModelInput`：

- `select_compatible_provider_replay_manifests()`；
- `freeze_selected_provider_replay_hydration()`；
- `CanonicalProviderInputReader.hydrate_selected_provider_replays()`。

其 exact join 继续验证：

- session；
- scope kind / subagent task；
- binding revision；
- provider-input through sequence；
- assistant entry placements；
- replay target fingerprint；
- wire API / codec / replay contract；
- payload digest / fragment fingerprint。

不得只按 replay payload bytes 粗略推算，也不得 hydrate manifest cut 中未被 final message placements 选中的 fragment。

### 6.5 Pure wire measurement core

位置：`src/pulsara_agent/conversation_kernel/direct_model.py`

从 `_plan_provider_wire_input()` 中抽出唯一内部 core，概念签名为：

```python
freeze_provider_wire_measurement(
    *,
    call: ResolvedModelCall,
    binding: ModelInputCompileBinding,
    native_projection_set: FrozenNativeToolProjectionSet,
    semantic_input: ProviderWireSemanticInput,
    replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
) -> ProviderWireMeasurement
```

`ProviderWireMeasurement` 是 process-local、可线性转移的内部组合结果；它可以由现有完整 carriers 组成，不得成为 durable row、event、registry 或 install authority。除quote外，它暂时持有本次已经生成的materialization、replacement identities与构造executable plan所需的closed结果。它至少向两个唯一消费者提供：

- non-executable quote consumer；
- executable `FrozenProviderWireInputPlan` factory。

两者必须共享逐字相同的：

- generic wire groups；
- native wire tools；
- replay replacements；
- adapter-owned final context-bearing materialization；
- generic/replaced/replay component traversal；
- exact bytes；
- estimator。

Core不得读取semantic per-message token estimate来计算wire debit。它可以把semantic total复制到quote的diagnostic字段，但authority estimate必须由wire-lowered components直接产生。

Measurement只有两种terminal消费方式：

1. `discard_materialization_to_quote()`：用于over-budget compaction source或纯诊断，销毁materialization/replay body引用，只留下non-executable quote；
2. `prepare_executable_plan()`：在token/byte hard admission通过后，把同一次materialization一次性转移进`FrozenProviderWireInputPlan`。

不得先从measurement取quote、丢弃materialization，随后又为同一未漂移dispatch重新hydrate/re-lower。若无需公开新 dataclass即可用私有 tuple/已有 carriers表达，不新增公共 DTO。若实现确需一个私有carrier，其唯一 product reason是“一次物化同时服务 quote 与 executable plan且禁止算法复制”；它不得离开provider-dispatch planning boundary，也不得实现clone或跨turn cache。

### 6.6 Adapter-owned context materializer

位置：`src/pulsara_agent/llm/adapters/openai/chat_completions.py`、`responses.py` 与共享 planning seam

每个 `wire_api` 必须暴露一个纯materializer，输入为resolved provider profile/request-shape ownership（或由exact resolved call闭包绑定）、frozen root policy、native tools与ordered input items，输出为transport builder实际消费的context-bearing JSON projection。Normal payload builder与wire measurement core都调用它；不得出现一个“为了quote”重写Chat system placement、另一个“为了send”再重写一次的双实现。凡request default/extra-body/profile policy会改变model-visible context，都必须由这个adapter-owned materializer纳入；compaction不能猜测。

测试必须对同一个plan证明：

```text
freeze_context_bearing_wire_projection(plan)
    == project_context_bearing_fields(build_actual_transport_payload(plan))
```

该等式而不是函数名证明“最终真正要发送”。Projection只排除明确不计入model input context的transport control fields；排除清单由wire adapter固定，compaction没有排除权。Existing continuity wire-prefix identity必须从这份最终projection的唯一既有identity builder派生，不能继续对旧的normalized `{root, tools, input}`旁路结构单独hash。

### 6.7 `PreparedCompactionSourceDispatch`

位置：`src/pulsara_agent/conversation_kernel/provider_dispatch.py`

`prepare_compaction_source()` 仍必须：

- `semantic_only=True`；
- 不取得 executor borrow；
- 不注册 continuity；
- 不 provider open；
- 允许 semantic/final-wire over budget。

但返回前必须：

1. 使用 exact `canonical_read`；
2. 针对 `prepared_call.call` 解析 replay target；
3. hydrate selected replay；
4. 调用 shared wire measurement core；
5. 在返回source dispatch时附带尚未clone的process-local measurement。

进入真正compaction后，source measurement立即执行`discard_materialization_to_quote()`，`FrozenCompactionSourceView`只保存non-executable quote；hydrate value与wire materialization均不得跨summary存活。只有automatic precheck的below-trigger分支可以把measurement移入下一节定义的handle-free observation，以便同一ordinary planning cycle在exact input未漂移时生成executable plan而不重复hydrate/materialize。

`PreparedCompactionSourceDispatch` 对safe-point handle拥有唯一线性所有权。调用者把`PreparedProviderHeadroomAdmission`传给source preparation时，必须是consume/transfer而不是共享`existing_handle`别名；返回below-trigger时再把同一handle唯一转移进一个新的ordinary admission，旧source/admission owner随即失效。任何时刻只能有一个对象能够`close()`该handle。

最小实现是把这两个process-local owner从无状态frozen wrapper改成窄one-shot carrier：`take_handle()`原子取出内部`PreparedProviderInputHandle | None`并把原slot置为`None`，第二次take/close typed拒绝；`close()`也先take再关闭。不得通过公开`.handle`读取后用`dataclasses.replace()`模拟转移，也不引入通用lease/registry。异常路径只能由当前非空owner关闭。

### 6.8 `FrozenCompactionSourceView`

位置：`src/pulsara_agent/conversation_kernel/compaction/contracts.py`

新增且只新增一个 final-wire quote 引用，作为 semantic projection 的正交物理表示估算：

```text
provider_projection       # semantic source/prefix truth
provider_wire_quote       # final wire trigger/reclaim truth
```

Source view constructor必须 exact join：

- quote estimator fingerprint == compile binding estimator fingerprint；
- quote effective budget == compile binding effective budget；
- quote semantic diagnostic total == provider projection semantic total；
- quote wire API == producer prepared call 的 wire API；
- source view直接持有exact quote object，并以typed value/object equality完成same-process join。

这里允许同时保留 semantic estimate 与 wire quote，因为它们代表不同层级且都有独立消费者；不得复制 system/messages/tools 或 replay payload。不得把quote closed fields嵌入`source_view_fingerprint`，也不得新增child quote fingerprint或parent aggregate proof。Existing `source_view_fingerprint`只保留其已有独立跨边界消费者所需的旧语义；若某个稳定request/canonical ID必须反映quote，由该唯一ID builder就地读取exact quote fields派生，不把冗余digest写回DTO。

### 6.9 Prepared dispatch measurement与executable transfer

先定义一个不含physical authority的structural `PreparedProviderWireCandidate`：ordinary dispatch、dry successor、no-Hook base与handle-free Hook sibling都能提供同一组canonical read、compiled input、prepared call、native projection与cold/non-cold candidate inputs。Hook reservation由candidate之外的selection owner持有。Measurement helper只接受这个seam：

```python
async def measure_prepared_wire_candidate(
    candidate: PreparedProviderWireCandidate,
    *,
    deadline: float,
    reusable_observation: HandleFreeProviderWireObservation | None = None,
) -> PreparedWireMeasurementDecision
```

它必须：

- 使用candidate自带exact canonical read、compiled input、prepared call与native projection set；
- read-only hydrate final placements真正选择的 replay；
- 调用 shared wire measurement core；
- 不 register/install/open；
- 不重复取得 tool surface borrow；
- 在所属的单个pre-open或post-summary planning phase内不刷新planning deadline；summary terminal之后签发fresh successor deadline属于新的phase，不得复用或延长pre-summary deadline。

返回是closed decision：

```text
PreparedWireMeasurementDecision
  exact non-owning candidate reference
  quote                         # 可表示over token/byte bound；无materialization
  wire_input_plan | None         # fit时与quote来自同一次measurement
```

Candidate reference严格non-owning；decision没有`close()`且不得延长到本planning cycle之外。Hard token/byte admission通过时plan必须存在；越bound时plan必须为`None`，materialization随即销毁。这个shared decision不含append/continuity inputs，因此ordinary、dry、Hook与one-shot summary都可合法消费同一类型，且summary无需伪造continuity candidate。Dry-successor caller只消费quote；compaction source走§6.7的semantic-projection core。

只有selection owner可以调用：

```python
bind_prepared_executable_wire_input(
    owner_dispatch: PreparedProviderDispatch,
    decision: PreparedWireMeasurementDecision,
) -> PreparedExecutableProviderWireInput
```

Binder要求plan存在，并exact比较owner dispatch与candidate的全部wire-affectingimmutable values；随后调用cold/non-cold continuity seam从**已准备plan**只构造一次append candidate inputs，返回一个non-owning `owner_dispatch` reference、同一次quote/plan与这些dispatch-only inputs。Ordinary final与no-Hook base可立即bind；Hook sibling必须先完成policy/reclaim selection，胜出后才把base的唯一handle/borrow owner与其decision组合。`install_provider_open(dispatch, prepared_wire)`要求`prepared_wire.owner_dispatch is dispatch`，随后直接register/preflight/install，不得再次hydrate replay、调用cold `finalize_wire()`或重新materialize。Quote-only/over-bound decision无法bind或install。Summary不调用这个dispatch binder，而走§6.11 promotion。

`HandleFreeProviderWireObservation`只包含private measurement及用于exact equality的immutable semantic/call/native-projection值；它不持有safe-point handle、tool-surface borrow、Hook reservation、continuity reservation或install authority。若final candidate exact相等，measurement helper消费旧measurement并形成decision；若任一值漂移，先销毁observation，再对final candidate做一次新measurement。

Protected tail 中若保留了 provider-native assistant entry，其 replay 必须计入 successor；不能假设 snapshot successor 没有 replay。

### 6.10 Hook selection的execution-authority原子转移

`PreparedProviderDispatch`内部physical execution authority必须收口成一个窄one-shot owner：

```text
ProviderDispatchExecutionAuthority
  PreparedProviderInputHandle
  ProcessLocalToolSurfaceBorrow
```

它不含semantic input、quote、plan或fingerprint，也不是通用lease/registry。`PreparedProviderDispatch.take_execution_authority()`原子取出该carrier并使原dispatch进入不可close/不可install的consumed状态；第二次take、close或install必须typed拒绝。普通dispatch与no-Hook base不发生candidate替换时仍由自身持有authority，无需转移。

Hook胜出时，selection owner必须在无await的process-local临界步骤内：

1. 从no-Hook base执行唯一`take_execution_authority()`；
2. 将winning handle-free Hook candidate、该authority与one-shot Hook reservation交给sealed `bind_selected_provider_dispatch()`；
3. 构造一个wire-affecting values与Hook candidate逐字段相等的唯一final `PreparedProviderDispatch`；
4. 再用§6.9 binder把Hook wire decision绑定到这个final dispatch。

不得用`dataclasses.replace(base, ...)`或让base/final共享authority。若步骤2–4任一失败，当前selection owner恰好一次关闭authority并retire reservation；base已失效，不能再进入fallback。Hook在take之前失败/未胜出时则不触碰base authority，只retire reservation并安全使用base。该顺序把“可回退判断”与“不可逆authority转移”明确分开。

### 6.11 Compaction summary semantic-over-budget promotion seam

`PreparedCompactionSummarySemantic`不得继续包装`FrozenCompiledModelInput`。新增一个仅限summary package内部的窄carrier：

```text
PreparedCompactionSummarySemanticInput
  canonical input identity
  system/messages/message placements/tools
  semantic estimate                    # diagnostic，可越budget
  source/tool-result decisions
  source collection + compile binding exact values
  summary prefix proof + temporary USER request
```

它满足§6.3 structural input seam，但不是ordinary compiled input，不含`ContextCompileBudgetReport`，不能注册continuity、创建ordinary execution request或直接provider open。Constructor验证shape、placements、source/call/target exact join与semantic estimate复算一致，**不执行semantic budget rejection**。

Summary candidate对该carrier选择/hydrate replay并得到§6.9共享`PreparedWireMeasurementDecision`；它不生成append candidate inputs。唯一promotion factory：

```python
promote_compaction_summary_call(
    semantic: PreparedCompactionSummarySemanticInput,
    decision: PreparedWireMeasurementDecision,
) -> PreparedCompactionSummaryCall
```

必须验证`decision.candidate is semantic`、wire plan存在、final-wire token/byte hard admission已通过、resolved call/target/profile与native tools exact join，并复用从`llm.validation`抽出的**shape/target/binding-only** validator。它不得调用会按semantic estimate拒绝的ordinary `validate_model_context_for_call()`；ordinary validator仍在原路径先做common shape/target检查，再保留semantic budget检查。Summary `LLMContext.compiler_estimated_input_tokens`若保留只作diagnostic，不是admission authority。

Wire planner必须接受structural semantic candidate；summary plan与semantic的same-process真实性由candidate decision object identity/equality证明，不伪造`FrozenCompiledModelInput`或新增summary fingerprint。Stable summary request/context ID如仍需要，只在唯一ID builder从exact semantic value与final context-bearing projection即时派生。Repair也走相同carrier与promotion seam；它不能恢复semantic budget gate，且追加后的final wire越bound时typed discard。

---

## 7. Trigger 与 reclaim 算法

### 7.1 自动 trigger

唯一 token trigger：

```text
source.provider_wire_quote.final_wire_estimated_input_tokens
    >= floor(effective_input_budget_tokens * auto_trigger_ratio)
```

Resource headroom trigger保持 Round 5B 原契约，与 token trigger 为 OR。

禁止继续使用：

- `provider_projection.final_estimate.total_input_tokens`；
- `dispatch.append_result.compiled_input.final_estimate.total_input_tokens`；
- 上一次 installed quote；
- provider usage；
- canonical bytes替代 token ratio。

### 7.2 Late threshold

当前同步 `dispatch_crosses_threshold()` 读取 semantic estimate，必须 hard cut为**唯一的 async final-wire late decision**：

- 使用 `measure_prepared_wire_candidate()`，fit后由`bind_prepared_executable_wire_input()`绑定ordinary owner；
- 估算即将进入 `install_provider_open()` 的最终 `PreparedProviderDispatch`；
- exact join该dispatch自己的cut、target、profile、placements与estimator；
- token与resource headroom仍由同一个policy decision返回；
- below threshold时把同一次measurement产生的`PreparedExecutableProviderWireInput`线性传给`install_provider_open()`；
- 不允许semantic fallback，也不允许再调用另一套compaction-only wire lowering。

保留late decision是必要的，而不是第二种计量语义：`prepare_compaction_source(semantic_only=True)` 明确不消费Hook context reservation，ordinary `prepare()` 才可能把合法call-local Hook context并入最终installable sibling；root completion suffix与steer drain也可能旋转canonical cut。Precompile decision只能评价它冻结的pre-Hook source，late decision评价真正即将安装的final dispatch。两者是两个不同事实cut上的同一个quote core。

同一个未漂移cut不得重复quote或materialize：如果precompile below-trigger source与final dispatch的canonical cut、exact compiled value、placements、capability、Hook source及replay selection完全相同，final dispatch消费旧measurement来生成executable plan；只要其中一项变化，就必须销毁旧measurement并重新hydrate/measure，由exact typed fields/object equality证明变化。这样保留必要的late safety net，又不让`install_provider_open()`重复一次ordinary measurement。

复用不通过cache key或fingerprint registry实现。`PrecompileCompactionDecision` 的below-trigger分支**不得**保留`PreparedCompactionSourceDispatch`；它只能临时保留`HandleFreeProviderWireObservation`，同时把唯一safe-point handle转回新的`PreparedProviderHeadroomAdmission`。Late helper逐字段比较所有wire-affecting immutable values：canonical read/cut、structural semantic input、resolved call/target/profile、compile binding、native projection set、replay target/selected placements及Hook source。Execution-only wrapper identity、handle与borrow不参与相等性，也不进入observation；wire-affecting值相同则consume measurement，漂移则discard后重新measure。无论成功、stale、timeout或exception，observation都必须在本planning cycle结束前关闭；它不是跨turn cache、durable receipt或continuity authority。

Automatic precheck本身不得调用`HostCompactionRuntimeOwner.run_fenced()`，不得取得Host-wide summary lane，也不得安装exact-scope compaction fence。它只在ordinary planning handle上完成read-only source measurement：

```text
precheck below trigger -> transfer handle back to ordinary admission
precheck trigger candidate -> close precheck ownership
                           -> acquire summary lane and scope fence
                           -> recapture a fresh exact cut under fence
                           -> re-measure/revalidate trigger
                           -> only then run summary
```

Fenced recapture与precheck是两个不同cut事实，允许且必须各自measurement；这不是同一cut重复quote。若fenced cut已不再触发，则释放lane/fence并回到ordinary replan，不得沿用precheck source。Manual请求从一开始进入fenced路径，不经过这个advisory auto precheck。这样普通低水位会话不会占用或排队等待全Host summary lane，多个会话的普通provider工作也不会被compaction precheck串行化。

因此`PreparedPrecompileCompaction`这种“把precheck source带出第一次`run_fenced()`、再交给第二次`run_fenced()`”的carrier必须删除。`PrecompileCompactionDecision` hard cut为closed union：

```text
OrdinaryPrecompileDecision(
  unique ordinary admission,
  handle-free reusable wire observation,
)

AutomaticCompactionTriggerCandidate(
  trigger reason/policy facts only,
)
```

Trigger candidate不携带handle、source view、measurement、canonical read或attempt authority；真正attempt token、source cut与quote全部在唯一fenced execution中产生。

### 7.3 删除Summary前的successor lower-bound gate

现有`estimate_unavoidable_compaction_successor_tokens()`必须直接删除，本文不引入wire版替代品。Summary尚未产生时，snapshot carrier、recent-human内嵌、post-boundary user placement、message coalescing、container framing与replay replacement之间不存在已证明的component-wise injection；仅凭“每项非负”不足以证明一个省略投影对Chat与Responses所有actual successor都是lower bound。

因此只保留两个summary前的safe rejection：没有合法compactable prefix，或没有任何executable summary prefix。是否真正reclaim，必须在summary完成且actual successor final-wire quote可得后由唯一validator判断。可能多做一次最终被判定无收益的summary call，是为了消除false-negative correctness bug而接受的成本；不得用未经证明的optimistic gate重新引入“already compact”。未来若要恢复该优化，必须另行给出两种wire API逐component注入证明与独立产品理由，不属于本hard cut。

### 7.4 Summary prefix的exact final-wire fit search

当前`compaction/planner.py`中的`fits()`只对`messages[:count] + USER(summary_request)`调用semantic estimator，而native replay直到`compaction/model_call.py::finalize_compaction_summary_call()`才进入wire plan。该顺序会让“semantic prefix fit、final-wire prefix over budget”的合法source在provider open前整体失败，尽管更短的完整prefix本可执行。

Hard cut后必须拆成两层：

1. pure planner只枚举满足canonical完整entry/tool-group边界的safe prefix boundaries，按最长到最短排序；它不得用semantic token estimate删除候选；
2. coordinator/model-call owner对候选构造§6.11 `PreparedCompactionSummarySemanticInput`，即使semantic estimate越budget也继续按该候选placements选择并hydrate replay、调用shared wire measurement core；
3. 只有同时通过final-wire token与byte hard admission的候选，才能经promotion生成`PreparedCompactionSummaryCall`；ordinary semantic-budget validator不得介入；
4. 选中候选的executable wire plan必须由同一次measurement线性转移给`open_once()`，不得在open前再次hydrate/materialize；
5. over-bound候选只留下短生命周期quote用于搜索判断，随即销毁materialization与hydration body。

本hard cut不把“prefix越长，final-wire estimate必然单调不减”当成未经证明的前提：native replacement、message coalescing、profile-owned framing与container计量都可能让朴素推理失效。正确性基线是按safe boundary从最长到最短逐个做exact search，选择第一个fit候选；finite集合由当前canonical cut自然终止，不得添加“最多尝试N个prefix”的任意总数上限。未来只有在Chat与Responses的实际materializer/estimator上证明并property-test单调性后，才可在不改变结果的前提下改成binary search。若某候选出现stale/corrupt/resource failure，它是typed planning failure，不能被当成over-budget后静默跳过。

若所有非空safe prefix均over-bound，则返回typed `NO_EXECUTABLE_SUMMARY_PREFIX` planning failure；automatic按fail-open语义保留旧epoch，manual显示真实整理失败。它不是`CONTEXT_ALREADY_COMPACT`。唯一repair call必须保留首次summary wire prefix；repair追加denial后若越bound，只能typed discard，不能缩短或重写已发送prefix。

### 7.5 Actual pre-adoption reclaim

在 summary、carrier、synthetic read、current cold sources/tools 与 dry successor全部确定后：

先调用唯一outer validator，而不是把两个裸整数直接交给数值函数：

```python
validate_compaction_wire_transition(
    source_view: FrozenCompactionSourceView,
    successor_dispatch: PreparedProviderDispatch,
    successor_wire: PreparedWireMeasurementDecision,
    *,
    phase: Literal["PRE_FULL", "POST_FULL"],
) -> ValidatedCompactionWireTransition
```

它对完整typed objects逐项exact比较：

- resolved target fact（其中已包含provider request shape、endpoint/transport binding、model/options与context budget）；
- resolved call target与compile binding target；
- wire API与adapter context-materialization contract；
- estimator fact/object与estimator fingerprint；
- effective input budget；
- canonical scope/binding/cut lineage与selected replay target。

只有该validator返回`ValidatedCompactionWireTransition`后，才允许把其中的source/successor quote数值交给现有`validate_compaction_reclaim()`。不得把target/profile fingerprint复制进quote来代替outer join。PRE_FULL drift必须关闭candidate并从fresh source重新规划，不能采用当前summary；POST_FULL drift保留canonical winner并走typed active-continuation failure。

```text
source_tokens = source_wire_quote.final_wire_estimated_input_tokens
successor_tokens = successor_wire_quote.final_wire_estimated_input_tokens
reclaim = source_tokens - successor_tokens
```

同时验证：

```text
successor_wire_quote.final_wire_utf8_bytes
    < source_wire_quote.final_wire_utf8_bytes
```

并复用唯一 `validate_compaction_reclaim()`：

- successor estimated tokens必须低于 hard input budget；
- reclaim必须 > 0；
- source未越 hard budget时，reclaim至少 `minimum_reclaim_tokens`；
- automatic必须满足 post target；
- manual force只跳过 post target，不跳过正收益与minimum reclaim；
- source已越 hard budget时允许小于minimum reclaim，但 successor仍必须fit。

旧的 canonical UTF-8 shrink检查可以作为 canonical working-set附加证明保留，但不能替代 final wire byte shrink。

### 7.6 Tail-shrink search

每个 retained count candidate都必须产生自己的 exact successor wire quote。若失败原因允许缩尾：

```text
3 groups -> 2 -> 1 -> 0
```

每次重新冻结对应 prefix/tail/summary/candidate；不得把前一个 candidate 的 replay hydration、quote或summary跨 boundary复用。PreCompact Hook仍按 existing attempt token最多执行一次。

每个losing dry dispatch仍拥有自己的既有handle/borrow，selection owner必须在进入下一个retained-count candidate前各关闭一次；wire decision是non-owning，不能代替也不能重复close这些authority。

### 7.7 Post-adoption active复验

canonical FULL 后，active branch从 exact adopted binding与current Runtime facts重建 final successor。它必须重新 quote final wire input，并使用同一个 frozen source quote复验：

- estimator/target/budget仍 exact join；
- final successor fits；
- final wire bytes仍缩小；
- final estimated reclaim仍满足 policy。

这里有两个同cut sibling，但只有一个install candidate：

1. 先构造no-Hook base，完成replay hydration、final-wire measurement、hard admission与reclaim/byte-shrink复验，并保留其`PreparedExecutableProviderWireInput`作为fallback；
2. 若存在optional Hook context，只构造一个不拥有handle/borrow/reservation的Hook semantic sibling；one-shot reservation由selection owner单独持有，对candidate独立选择replay、measure、hard-admit并复验同一source reclaim；
3. Hook sibling只有在token/byte fit、minimum reclaim、automatic soft target与wire-byte shrink全部仍成立时才能替换base；
4. Hook-specific compile/quote/reservation/resource/probe-deadline/over-bound/reclaim失败均由selection owner retire该one-shot reservation并选择已经证明的no-Hook base；selected bind/install使用§9.1 fresh deadline，因此optional Hook不能仅因耗尽自己的probe watchdog而使合法base continuation失败。Shared canonical cut、target、capability或base ownership漂移则同时使两个sibling失效，必须typed拒绝，不能伪装成Hook-only fallback；
5. 最终选择后先销毁未选measurement；no-Hook胜出时base不转移authority，直接绑定其decision；Hook胜出时按§6.10从base原子take handle+borrow、使base失效，再把authority与one-shot reservation绑定到Hook candidate构造唯一final dispatch；continuity register/install/provider open严格各一次。

`prepare_hook_context_sibling()` 因而不得通过`dataclasses.replace(base, ...)`制造第二个看似拥有同一handle/borrow的`PreparedProviderDispatch`。它应返回handle-free/reservation-free semantic sibling与独立one-shot reservation owner；selection owner才有权把唯一base authority、可选reservation与选中的semantic/measurement组合成最终dispatch。

若 post-adoption current capability变化使复验失败：

- 不回滚已FULL snapshot；
- 不恢复旧binding pointer；
- 不伪造 `NOT_NEEDED`；
- 保留 `COMPACTED` canonical winner，active continuation按现有 post-adoption failure path中断；
- future cold read仍从已采用snapshot重新规划current capability。

---

## 8. Provider-neutral 契约

### 8.1 禁止 provider token oracle

生产正确性不得调用或依赖：

- remote token count endpoint；
- provider SDK tokenizer；
- model-name到 tokenizer-name表；
- endpoint-origin经验配置；
- response usage必达；
- provider cache hit/miss partition；
- billing token；
- reasoning token账单字段。

### 8.2 Wire API适配是合法边界

不同 wire protocol 必然有不同 materialization。合法分支只能由已冻结的 transport/wire contract决定，例如：

- `openai_chat_completions`；
- `openai_responses`。

该分支必须已经被普通 provider send使用；compaction只能复用，不能另写。

未来新增 wire protocol时，只有其普通 transport adapter完成：

- semantic wire group lowering；
- tool lowering；
- replay codec/compatibility；
- final materialization；

之后，compaction自然获得同一 quote能力。Compaction不得知道 provider厂商名称。

### 8.3 Provider usage只做 telemetry

现有 `TransportUsageReport(usage_status="reported" | "missing")` 保持可选。

允许：

- UI展示本次provider报告的input/output tokens；
- dogfood对比local estimate与reported usage；
- 输出非权威diagnostic。

禁止：

- 动态修改 estimator；
- 持久化 calibration multiplier；
- 因usage缺失拒绝请求；
- 因usage偏差重跑/回滚compaction；
- 将usage加入snapshot、binding、provider prefix或quote identity。

---

## 9. Deadline、并发与资源所有权

### 9.1 阶段化 watchdog，绝不跨越健康 provider stream

Deadline只属于有限、无外部人类/模型思考时间的局部操作，不属于compaction attempt、turn或task总寿命。生命周期必须明确分段：

1. **Ordinary dispatch pre-open planning**：runner签发一个existing provider-dispatch planning watchdog，覆盖headroom admission、lane-free automatic precheck quote、ordinary final compile、必要的late final-wire plan与continuity preflight；不得在precompile内部刷新。该watchdog在ordinary provider transport open前结束，也不得因等待Host-wide summary lane消耗预算，因为below-trigger precheck根本不取得该lane。
2. **Compaction pre-summary planning**：automatic trigger candidate或manual请求赢得fence后，签发一个局部planning watchdog，只覆盖fresh exact cut read、semantic projection、source replay hydration/quote、tail trials、全部需要检查的summary-prefix candidates及selected summary wire plan；candidate hydration不能刷新它，且不得另加候选次数上限。
3. **Summary execution**：`prepared_summary.open_once()` 及唯一合法repair stream不受任何planning deadline或total response timeout约束。Transport只使用connect、write、pool与read-idle等“无进展/无法建立连接”异常watchdog；只要provider持续健康输出，就允许运行任意长时间，直到protocol terminal、显式取消、provider自身明确边界或物理失败。不得保留20分钟reasoning-runaway总时限。
4. **Pre-adoption successor planning**：summary terminal被完整接收并验证后，签发fresh successor-planning watchdog，只覆盖carrier/synthetic read、current capability/source assembly、dry successor replay hydration/quote与reclaim validation；内部hydration不刷新。
5. **Publication/adoption/confirmation**：blob publication、canonical write与confirmation分别取得既有fresh foreground operation deadline，不继承或扣除summary/successor-planning耗时。
6. **Post-FULL active base planning**：confirmation后为no-Hook base reconstruction、measurement与reclaim proof签发fresh local watchdog。Idle branch无需本阶段。
7. **Optional Hook probe**：若有Hook context，单独签发fresh有限local-operation watchdog；其compile/measurement timeout属于Hook-specific probe failure，retire reservation并保留已证明base。该watchdog不消耗随后selected install预算。
8. **Selected bind/install**：选择base或Hook后再签发fresh finite pre-open/install watchdog，覆盖final exact revalidation、authority bind、continuity register/preflight/install；它在ordinary continuation provider transport open前结束。Hook probe超时后base仍必须能用这个fresh deadline install；若此时shared canonical/target/base已经漂移，才走post-FULL typed failure。

以上局部deadlines互不相减、不形成aggregate total cap，也不能追溯扣除summary stream或task既往耗时。每个subphase内部不得通过循环/重试任意刷新自己的deadline，但新authority边界取得fresh watchdog不是“延长旧deadline”。

`planning_attempt_seconds=120` 若保留，只能解释为单个bounded local planning slice的异常watchdog：命中后产生typed planning failure并释放process-local资源；automatic按既有fail-open/retry语义处理，manual得到真实typed failure而不是“already compact”。它不是summary、compaction、turn、worker或task的总时限。不得用它累计多个阶段、限制重试后的逻辑寿命，或因为task已经运行很久而缩短下一次局部操作预算。

当前 `prepare_precompile()` 内部重新计算 `monotonic() + planning_attempt_seconds` 的实现仍必须删除；runner创建的同一个ordinary pre-open deadline显式传入precompile与late decision。相反，summary结束后的successor planning、post-FULL base、optional Hook probe与selected install分别创建fresh局部deadline，是正确且必须保留的authority/phase boundaries，不属于“延长同一planning deadline”。

### 9.2 Read-only与physical authority

Source quote和dry successor quote只能进行：

- read-only PostgreSQL replay hydration；
- process-local pure materialization/estimation。

不得：

- provider open；
- executor borrow acquisition（已有dry successor borrow除外，不得重复取得）；
- tool execution；
- ToolResult delivery install；
- continuity register/install；
- canonical write。

Automatic source precheck也不得取得Host-wide summary lane或scope fence。它消费ordinary admission的唯一safe-point handle，完成后只能二选一：below-trigger时把handle转回新的ordinary admission并留下handle-free measurement observation；trigger candidate时关闭该precheck owner，随后由fenced compaction重新capture。不得让`PreparedProviderHeadroomAdmission`与`PreparedCompactionSourceDispatch`同时拥有同一handle。

Ordinary final、post-FULL base与selected Hook sibling的wire measurement若已经生成executable plan，install必须消费该plan；measurement、dispatch与install之间不允许重复hydrate/materialize。No-Hook/Hook sibling只能共享immutable frozen facts，不能复制handle、surface borrow、Hook reservation或install authority。

### 9.3 Staleness

Quote exact绑定：

- canonical dispatch read cut；
- context binding revision；
- provider-input through sequence；
- scope；
- resolved target；
- estimator；
- tool projection；
- replay target与selected placements。

任何一项漂移都discard/replan，不允许用“足够接近”的旧 quote。

### 9.4 Failure行为

| failure | manual | automatic |
|---|---|---|
| quote below trigger | force忽略trigger，继续 | ordinary dispatch |
| selected replay hydration stale/corrupt | typed failure，不显示already compact | 当前attempt失败；不得退回semantic estimate |
| quote working-set/resource bound | typed resource failure | existing fail-open/typed boundary；不得伪造below-trigger |
| source over hard token budget | 仍允许规划compaction | 必须触发compaction |
| source over hard wire byte bound但可quote | 仍允许规划compaction | resource/token planning继续 |
| successor over token/byte budget | 缩尾或失败 | 缩尾或失败 |
| positive但不足minimum reclaim | `NOT_NEEDED` | `NOT_NEEDED`/ordinary continuation |
| post-FULL final successor失败 | snapshot保留 | snapshot保留，active continuation失败 |

只有实际执行了 final-wire source/successor比较且确实不足收益时，才允许 `CONTEXT_ALREADY_COMPACT`。Quote失败、hydration失败、deadline或stale不能映射为“已经较紧凑”。

---

## 10. 持久化与 subtraction guard

本 hard cut不增加：

- database table/column；
- durable event kind；
- subject slot；
- session command kind；
- durable job；
- replay relation；
- receipt/checkpoint；
- estimator registry；
- provider calibration state。

Quote、measurement、hydration与projection全部 process-local。Canonical winner仍只有 existing snapshot、binding revision、turn pointer与CompactionAdopted event。

`provider_assistant_replay_fragments` 继续是唯一 durable provider-native replay body来源。本文不把 token quote写回该row，因为 quote依赖current target、wire profile、estimator与selected placements，不是 replay fragment自身的稳定语义。

---

## 11. 文件级实施清单

### 11.1 `src/pulsara_agent/llm/request.py`

- 将 quote token字段 hard rename为 `estimated`；
- 将replacement identity从semantic debit hard cut为direct wire component estimate，删除无消费者字段；
- 删除`generic_message_group_fingerprint`与`replacement_wire_fingerprint`，保留有durable replay integrity consumer的fragment digest；
- hard cut quote type/schema到V2语义，删除无独立消费者的version literal；
- quote允许over-budget/over-wire-byte-bound；
- executable plan/factory承担hard admission；
- 仅在现有独立provider-prefix consumer处按新closed fields即时更新plan identity计算，不新增quote/materialization fingerprint字段；
- 不保留旧字段alias。

### 11.2 `src/pulsara_agent/conversation_kernel/direct_model.py`

- 抽出single wire measurement core；
- 支持ordinary compiled input、semantic projection与compaction-summary semantic carrier的窄structural input；
- normal `plan_wire_input()`与compaction quote复用；
- executable plan继续携带完整materialization；
- quote-only路径不创建execution request；
- 同一measurement在quote-only discard或executable plan transfer之间二选一，不允许clone/re-materialize；
- summary execution继续复用`total_seconds=None`的foreground transport policy，不增加20分钟或planning total timeout。

### 11.3 `src/pulsara_agent/llm/estimator.py`、`llm/validation.py`、`model_input/contracts.py` 与 wire adapters

- 为唯一 `TokenEstimator` 暴露final wire component traversal；
- `ModelInputTokenEstimator` structural Protocol同步声明`estimate_json`/最终采用的wire traversal method，direct model不得越过静态类型调用未声明能力；
- 从ordinary validator抽出shape/target/binding-only validation；ordinary继续叠加semantic budget检查，summary promotion只复用common validation与final-wire admission；
- traversal只复用该estimator的既有JSON/text primitives与framing policy；
- Chat/Responses adapter各自提供唯一profile-bound context-bearing materializer与actual-payload projection helper；
- normal payload builder与quote core共享materializer；
- quote contract hard cut到V2，不建立provider-name tokenizer或倍率。

### 11.4 `src/pulsara_agent/model_input/provider_replay.py`

- generalize replay manifest selection到structural semantic input；
- generalize `freeze_selected_provider_replay_hydration()`到同一个structural input；
- 保留全部exact join；
- 不引入第二套selection函数。

### 11.5 `src/pulsara_agent/conversation_kernel/reader.py`

- generalize selected replay hydration到structural semantic input；
- read-only transaction/deadline不变；
- 不接受裸entry IDs或调用者预选payload。

### 11.6 `src/pulsara_agent/conversation_kernel/provider_dispatch.py`

- `prepare_compaction_source()`产生可线性消费的measurement，真正compaction source只保留non-executable quote；
- headroom admission -> source dispatch -> ordinary admission的safe-point handle严格唯一转移；
- 增加structural candidate measurement + selected-owner executable binder，ordinary/post-FULL install消费同一次measurement产生的plan；
- normal install在continuity register之前拒绝over-budget executable plan；
- `install_provider_open()`不再重复hydrate/finalize/materialize；
- Hook sibling改成handle/borrow/reservation-free semantic candidate；selection owner单独持有one-shot reservation并只转移一次base authority；
- 为`PreparedProviderDispatch`增加窄one-shot execution-authority take/invalidate与sealed selected-candidate binder；Hook胜出不得alias/replace base；
- 不让quote-only路径借用executor或open transport。

### 11.7 `src/pulsara_agent/conversation_kernel/cold_epoch.py`

- 将semantic preparation与“从既有prepared wire plan构造continuity candidate inputs”分离；
- ordinary/post-FULL measurement已生成plan时，不再强迫`finalize_wire()`调用wire planner第二次；
- exact join seed、compiled input、planning、capability与prepared plan后只生成一次append candidate inputs；
- 不新增cold/append双轨或第二套continuity规则。

### 11.8 `src/pulsara_agent/conversation_kernel/compaction/contracts.py`

- `FrozenCompactionSourceView` exact持有final-wire quote；
- quote以exact object/value equality join，不进入`source_view_fingerprint`，不新增child或parent aggregate fingerprint；
- semantic projection仍保留用于prefix/tail；
- 不复制system/messages/tools/replay payload。

### 11.9 `src/pulsara_agent/conversation_kernel/compaction/planner.py`

- trigger读取final-wire estimated total；
- 删除`estimate_unavoidable_compaction_successor_tokens()`及所有summary前successor lower-bound gate，不增加wire版替代；
- summary prefix planner只枚举完整safe boundaries，不以semantic `fits()`淘汰候选；
- 删除source wire与successor semantic estimate混算；
- reclaim validator本身保持单实现。

### 11.10 `src/pulsara_agent/conversation_kernel/compaction/model_call.py`

- 用`PreparedCompactionSummarySemanticInput`替换summary对`FrozenCompiledModelInput`的伪装，允许semantic-over-budget但无open authority；
- 删除prepare/repair summary中的semantic budget rejection及ordinary semantic-budget validator调用；
- 为每个被搜索的safe prefix构造exact semantic input、replay selection/hydration与final-wire measurement；
- 仅由同一candidate的hard-admitted final-wire decision promotion成`PreparedCompactionSummaryCall`；
- 在finite safe boundary集合上按最长到最短做无任意候选cap的exact wire-fit search；未经两种wire API证明不得改用二分；
- selected summary executable plan线性转移给`open_once()`；
- repair保留首个wire prefix，追加后越bound则typed discard，不重写prefix。

### 11.11 `src/pulsara_agent/conversation_kernel/compaction/coordinator.py`、`runtime.py` 与 `runner.py`

- 所有source token消费者切换到source wire quote；
- dry successor与post-FULL successor生成wire quote；
- exact wire byte shrink gate；
- 新增outer `validate_compaction_wire_transition()`，先exact join完整target/profile/wire API/estimator/budget/cut，再调用数值reclaim validator；
- 将semantic `dispatch_crosses_threshold()` hard cut为唯一async final-wire实现；
- `prepare_precompile()` 接受runner传入的absolute deadline，不在内部刷新；
- automatic precheck不调用`run_fenced()`、不占summary lane/fence；trigger后在fence内fresh recapture/revalidate；
- below-trigger只传递handle-free exact measurement observation，不建立cache/fingerprint registry；
- ordinary late decision把selected executable plan传给install，不重复hydrate/materialize；
- post-FULL no-Hook base与optional Hook sibling各自final-wire复验，Hook失败回退已证明base且只install/open一次；
- pre-summary deadline在`open_once()`前终止，summary结束后才创建fresh successor-planning deadline；
- post-FULL base planning、optional Hook probe、selected bind/install各自取得fresh局部deadline；Hook probe timeout后base install不继承过期deadline；
- 不把summary或前一局部subphase耗时计入successor、canonical write、confirmation或selected install deadline；
- active/idle继续调用同一fenced algorithm。

### 11.12 Frontend与protocol

本修复不要求新按钮、command或状态。

允许同步收紧提示映射：

- `NOT_NEEDED + CONTEXT_ALREADY_COMPACT`：当前最终wire输入没有足够可回收空间；
- quote/hydration/planning failure：显示整理失败或暂不可整理；
- 不再把所有非adoption结果统一描述为“已经足够紧凑”。

---

## 12. 实施顺序

必须按以下顺序hard cut，任何中间commit不得保留production双计量：

1. 抽取wire measurement core与adapter-owned context materializer，证明normal request的context-bearing wire值逐字不变；
2. 收紧quote/replacement contract与fingerprint subtraction，落single-use measurement -> quote-only/executable-plan transfer与hard admission owner；
3. generalize replay selection/freezer/reader hydration到semantic projection，并补齐estimator structural Protocol；
4. 让cold-epoch continuity candidate可以消费already-prepared wire plan，不再触发第二次finalize/plan；
5. hard cut headroom/source/ordinary safe-point handle为唯一线性owner，并让automatic precheck完全移出summary lane/fence；
6. 为compaction source生成final-wire measurement/quote，trigger后在fence内fresh recapture/revalidate；
7. 删除summary前successor lower-bound gate，将summary prefix从semantic `fits()`切成exact final-wire candidate search，并把selected executable plan直接转给summary open；
8. 为dry/post-FULL successor生成final-wire quote/executable preparation与outer transition validator；
9. hard cut post-FULL no-Hook/Hook sibling为独立measurement、pre-take base fallback、winning-candidate execution-authority原子转移与single install/open；
10. hard cut deadline ownership：ordinary pre-open与compaction pre-summary各自只持有局部planning watchdog，provider stream不继承该deadline，summary terminal后才签发fresh successor-planning deadline，并删除旧20分钟summary总时限；
11. 一次性替换trigger、reclaim与late threshold所有semantic token消费者，添加exact wire byte shrink；
12. 更新typed failure/UI文案映射，删除旧semantic fallback、旧字段名与重复hydrate/materialize路径；
13. 跑focused、PostgreSQL、real-provider dogfood。

不允许先只修manual，再补auto；不允许先引入feature flag；不允许同时记录semantic/new quote并由环境变量选择。

---

## 13. 测试矩阵

### 13.1 Pure estimator与wire measurement

必须证明：

1. no replay时，generic wire与final wire estimate相等，replacement debit/addend为0；
2. replay replacement时：

   ```text
   final wire = generic wire - replaced generic wire + replay wire
   ```

   其中三个操作数全部由wire-lowered JSON components直接估算，semantic per-message estimate不得参与。

3. final wire bytes等于adapter-owned context-bearing projection的canonical JSON bytes；
4. Chat与Responses各自的normal plan与quote-only core输出相同quote；
5. quote可以表示token over-budget；
6. quote可以表示wire-byte over-bound；
7. executable plan拒绝上述over-bound quote；
8. estimate字段明确来自同一estimator fingerprint；
9. 不存在provider name branch。

### 13.2 Replay exactness

必须覆盖：

- ROOT与SUBAGENT_TASK；
- Chat closed replay fields；
- Responses reasoning/message/function_call items；
- target-compatible replay；
- target-incompatible replay不选；
- cross-session/scope/cut拒绝；
- placement不连续拒绝；
- payload digest/fragment mismatch拒绝；
- summary prefix只hydrate selected prefix；
- successor只hydrate retained suffix；
- 被snapshot替换的old prefix replay不进入successor。

### 13.3 Compaction trigger

必须新增回归：

```text
semantic estimated input < auto threshold
final-wire estimated input >= auto threshold
```

预期：automatic compaction触发，不能ordinary send。

反向场景：

```text
semantic与final-wire都低于threshold
```

预期：不触发，ordinary dispatch只进行一次automatic decision。

### 13.4 Manual reclaim回归

构造与 `8634559f` 同型fixture：

```text
semantic compactable prefix < minimum_reclaim_tokens
native replay net addend > minimum_reclaim_tokens
actual successor final-wire reclaim >= minimum_reclaim_tokens
```

预期：manual `force=true` 不得在summary前返回 `CONTEXT_ALREADY_COMPACT`；summary、dry successor quote与adoption按正常路径执行。

另测：final-wire actual reclaim仍不足minimum时，才返回 `NOT_NEEDED`。

### 13.5 Successor与tail

- retained 3 groups不fit、2 groups fit；
- retained group有native replay；
- 0 groups仍不reclaim；
- summary很长导致wire candidate不reclaim；
- capability/tool surface增长抵消history reclaim；
- semantic bytes缩小但final wire bytes不缩小；
- final wire tokens缩小但successor仍越budget；
- source/successor target fact、provider request shape、wire API、estimator或budget任一漂移时，数值validator调用次数为0；PRE_FULL fresh replan、POST_FULL保留winner并typed失败；
- manual force超过post target但满足minimum；
- automatic必须满足post target。

### 13.6 Active/idle/manual/auto

四象限全部验证：

| lifecycle | trigger |
|---|---|
| active | manual |
| active | automatic/mid-turn |
| idle | manual |
| child active | automatic/manual（现有产品允许范围） |

所有象限在FULL前观察同一个quote/reclaim helper。Idle successful compaction仍允许且需要summary provider call；这里必须为0的是adoption后的ordinary agent-loop provider open，不能把summary call误计成idle continuation。

### 13.7 Provider usage独立性

同一输入分别模拟：

- usage reported且等于local estimate；
- usage reported且明显不同；
- usage missing；
- cached token details存在/不存在。

Compaction trigger与adoption必须逐字相同。只有telemetry不同。

### 13.8 Prefix continuity

- quote-only planning不安装epoch；
- below-trigger ordinary dispatch的SYSTEM/tools/messages prefix不变；
- successful compaction只在adopted successor boundary重建root；
- source quote与summary call都不推进normal epoch revision；
- quote failure不修改canonical binding。

### 13.9 PostgreSQL

- source replay hydration使用read-only transaction；
- compaction失败时snapshot/revision/event均为0；
- adoption仍单事务；
- quote不写任何row；
- `context_snapshots`不新增token字段；
- ACK unknown confirmation不重新summary/quote foreign cut。

### 13.10 Deadline生命周期

使用fake monotonic/transport而非真实等待，必须证明：

- pre-summary planning deadline到达前open summary，之后clock越过120秒仍可持续接收健康stream；
- healthy stream总时长越过旧20分钟边界仍不被total timeout取消；
- read-idle、connect/write失败仍触发既有typed transport failure；
- summary terminal之后才创建fresh successor-planning deadline；
- successor quote、canonical write与confirmation都不扣除summary elapsed time；
- post-FULL base proof成功后，Hook probe耗尽自己的deadline仍retire Hook，并用fresh selected-install deadline成功install base；
- Hook probe elapsed time不从selected bind/install watchdog扣除；selected install时shared base drift仍typed失败而不是盲目fallback；
- repair summary遵循同样无total cap的transport policy；
- automatic planning watchdog失败按既有fail-open处理，manual显示真实失败且不映射为already compact；
- 任意长turn中可多次进入新的局部planning slice，不存在累计wall-clock lifetime cap。

### 13.11 Single materialization、handle与summary lane

用hydration/materializer/install计数器及可审计的fake handle证明：

- below-trigger且final dispatch exact未漂移时，precheck measurement被消费成executable plan；install阶段hydration/materializer增量均为0；
- Hook/canonical/source变化时旧observation先被discard，新dispatch只measure一次；install仍不重复；
- over-bound measurement只产生quote-only结果，无法调用install；
- headroom admission -> source dispatch -> ordinary admission全程恰有一个可close handle owner，任一路径close恰好一次；
- below-trigger precheck的summary-lane acquire与scope-fence install计数均为0；
- trigger candidate关闭precheck owner后才取得lane/fence，并在fence内fresh recapture/re-quote；
- 多个会话并发进行below-trigger ordinary planning时不会在Host-wide summary lane串行等待；真正summary仍遵守现有物理并发边界。

### 13.12 Summary prefix final-wire search

- 最大semantic-fit prefix因native replay而final-wire over-budget，较短完整prefix fit：必须选择较短prefix并成功open；
- semantic estimate over-budget但final wire replacement后fit：summary carrier必须成功measurement、promotion并实际open；同一fixture若尝试构造ordinary `FrozenCompiledModelInput`仍必须拒绝；
- Chat与Responses replay-heavy prefix分别覆盖；
- safe boundaries数量很大时仍搜索finite完整集合，不存在任意“最多N个”拒绝；
- no candidate fit返回`NO_EXECUTABLE_SUMMARY_PREFIX`，manual/auto均不映射already compact；
- selected candidate的hydration/materialization不会在`open_once()`前重复；
- repair追加denial后over-bound时discard旧epoch，绝不缩短/重写首次已发送prefix。

### 13.13 Post-FULL no-Hook/Hook selection与fingerprint subtraction

- no-Hook base fit且Hook也fit/reclaim：选择Hook；
- Hook token over-budget、byte over-bound、破坏minimum reclaim、破坏automatic soft target或不再wire-byte shrink：逐项retire Hook并选择base；
- Hook-specific compile/reservation失败选择base；shared canonical/target/base drift必须拒绝两者，不能伪装成Hook-only fallback；
- Hook胜出并take execution authority后，原base再次take/close/install均typed拒绝；final bind异常时authority close与reservation retire各恰好一次；
- selected路径continuity register/install/provider open各恰好一次，handle/borrow/reservation无double-close或泄漏；
- source view直接持exact quote且`source_view_fingerprint`在加入quote前后不变；代码扫描不存在quote fingerprint、measurement registry或parent aggregate quote proof。

---

## 14. Focused验证命令

实施时至少运行：

```bash
uv run pytest -q tests/test_round5b_long_horizon_context_compaction.py
uv run pytest -q tests/test_stage2_direct_model.py -k 'wire or payload or replay'
uv run pytest -q tests/test_stage2_conversation_runner.py -k 'compaction or provider_replay'
uv run pytest -q tests/test_round3_structured_model_input_compiler.py -k 'projection or estimate'
uv run pytest -q tests/test_round3_1_provider_input_prefix_continuity.py -k 'wire or replay or compaction'
uv run pytest -q tests/test_round5_long_horizon_execution_envelope.py -k 'usage or estimate'
uv run pytest -q tests/test_round5a2_durable_provider_replay.py
uv run pytest -q -m postgres tests/test_stage2_conversation_runner.py -k 'compaction or provider_replay'
uv run pytest -q -m postgres tests/test_round5a2_durable_provider_replay_postgres.py
```

测试名若与当前实际collection不同，先用 `uv run pytest --collect-only -q` 精确定位，不通过扩大到全量测试掩盖focused失败。

### 14.1 Real-provider dogfood

至少使用当前production OpenAI-compatible transport完成：

1. Chat Completions replay-heavy会话；
2. Responses replay-heavy会话（若当前配置可用）；
3. manual idle compaction；
4. active automatic compaction；
5. provider usage reported与missing均不影响decision；
6. compaction后下一次provider request实际payload不含被snapshot替换的old replay prefix；
7. 不输出 `PULSARA_API_KEY`。

Dogfood报告必须同时记录：

```text
semantic estimated input tokens
generic wire estimated input tokens
replaced generic wire estimated tokens
replay wire estimated tokens
final wire estimated input tokens
effective input budget
exact final wire bytes
source/successor estimated reclaim
source/successor exact wire byte delta
provider-reported usage status/value（仅诊断）
```

---

## 15. 明确禁止的伪修复

以下任一实现均视为未完成：

1. 把 `minimum_reclaim_tokens` 从16,384调低；
2. manual强制无条件采用任何summary；
3. 只改UI提示；
4. 只在 `8634559f` 或某个provider/profile上特判；
5. 用replay payload UTF-8 bytes直接冒充tokens；
6. 用provider response usage作为下一次source quote；
7. usage缺失时退回semantic trigger；
8. 引入tiktoken或厂商SDK作为生产必需依赖；
9. 为不同provider维护经验倍率；
10. 只修source，不quote successor；
11. 只修pre-adoption，不修post-FULL复验；
12. 只修manual，不修auto/mid-turn；
13. 保留sync semantic `dispatch_crosses_threshold()`作为fallback；
14. 为quote取得tool executor borrow或continuity authority；
15. 为quote新增数据库列、event、job、checkpoint或replay registry；
16. 将model summary提升为system role；
17. 在同一installed epoch重组SYSTEM/tools；
18. 因provider usage偏差重写已采用snapshot；
19. 为over-budget source伪造一个可执行plan；
20. 保留v1/v2 quote字段双读写或feature flag；
21. late decision先quote、install再对同一dispatch重复hydrate/materialize；
22. automatic below-trigger precheck取得或排队等待Host-wide summary lane/fence；
23. 用semantic `fits()`直接选定summary prefix，再把final-wire over-budget当整次失败；
24. post-FULL Hook sibling未做final-wire reclaim/admission就覆盖base并install；
25. 把quote字段写入`source_view_fingerprint`、新增quote fingerprint或measurement registry；
26. 用120秒planning watchdog、旧20分钟backstop或其他累计wall-clock cap终止持续健康的summary/turn/task；
27. 在没有Chat/Responses component-wise injection证明时恢复任何summary前successor lower-bound/optimistic-reclaim gate；
28. 为semantic-over-budget但wire-fit的summary伪造ordinary `FrozenCompiledModelInput`，或让summary调用ordinary semantic-budget validator；
29. 让optional Hook probe与selected base install共享同一个会过期的absolute deadline，导致Hook timeout破坏base fallback。

---

## 16. 验收标准

全部满足才可宣布完成：

1. normal provider send与compaction quote使用同一wire measurement core；
2. quote token字段明确是local estimate；
3. exact wire bytes与estimated tokens明确分层；
4. source projection可quote over-budget输入但不可执行；
5. executable plan在register/install/open前拒绝over-budget；
6. durable replay被source与successor按final placements exact计入；
7. auto trigger使用final-wire estimate；
8. manual/auto reclaim使用final-wire source与successor estimate；
9. final wire bytes必须严格缩小；
10. `8634559f` 型回归不再误报already compact；
11. 真正不足minimum reclaim仍返回typed `NOT_NEEDED`；
12. active/idle FULL前无算法分叉；
13. provider usage reported/missing不改变结果；
14. 无provider厂商特供；
15. 无schema/event/job增长；
16. 无旧semantic fallback或compatibility alias；
17. focused、PostgreSQL与real-provider dogfood通过；
18. canonical transcript与provider prefix continuity契约保持不变；
19. planning watchdog不跨summary stream，健康summary/turn/task没有total wall-clock cap；post-summary局部操作使用fresh deadlines；
20. 同一未漂移dispatch只hydrate/materialize一次，selected executable plan直接被install消费；
21. safe-point handle、surface borrow与Hook reservation具有唯一owner，无alias/double-close；
22. automatic precheck不占summary lane/fence，trigger后在fence内fresh recapture/revalidate；
23. summary prefix按exact final-wire search选择，replay-heavy较短合法prefix不会被遗漏；
24. post-FULL Hook sibling只有在final-wire/reclaim全部成立时才替换base，否则安全回退no-Hook base且只install/open一次；
25. quote以exact object持有，不扩大source-view fingerprint topology；
26. source/successor完整target/profile/wire API/estimator/budget/cut在数值reclaim前由唯一outer validator exact join；
27. structural replay selector/freezer/reader与estimator Protocol全部闭合，不靠动态越型；
28. summary前不存在未经证明的successor lower-bound gate；
29. semantic-over-budget但final-wire-fit的summary可promotion/open，而ordinary `FrozenCompiledModelInput`仍拒绝同型over-budget input；
30. Hook probe与selected install使用不同局部deadline，Hook timeout不会把已证明base拖入过期deadline。

---

## 17. 最终产品解释

实现后，产品对“压缩上下文”的解释必须是：

> Pulsara 会在本地按当前模型预算估算实际即将发送的完整输入，包括system、tools、普通消息和可重放的provider-native reasoning/tool载荷。它不会调用某家provider的token计数服务，也不依赖provider事后返回的usage。只有压缩后的完整输入确实更小且达到最低收益时，Pulsara才采用新的上下文快照。

这既保留provider-neutral，也让压缩判断与实际provider-visible input处于同一计量层级。
