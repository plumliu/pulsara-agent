# Pulsara ProviderInput 因果顺序与 Prefix Continuity P1 Hard Cut 实施规格

> 状态：紧急 P1 修复规格草案，完成 review 前阻止继续宣称 Incremental ProviderInput 已闭环。
>
> 记录日期：2026-07-19。
>
> 本文是 `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md` 的纠偏规格。凡涉及 transcript lowering order、generation scope、prefix continuity、rollover 判定和 fresh-generation rebuild 的条款，以本文为准。
>
> 本问题首先由 DeepSeek prompt-cache 命中率异常暴露，但其本质是 provider-visible trajectory 的语义与因果顺序错误，不得按纯性能问题处理。
>
> **ROAC supersession（2026-07-20）：** 本文的ordered transcript、strict suffix和causal frontier继续有效；动态non-root carrier、source semantic head与rollover矩阵由
> `PULSARA_RUNTIME_OBSERVATION_AND_AUXILIARY_CONTEXT_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md`进一步收紧。任何仍允许`auxiliary_frame_rebase`、mid-history
> system/developer hint或按source absence重建generation的旧段落均已失效；observation收缩只能来自confirmed Long-Horizon rewrite。

---

## 0. 执行结论

当前 production ProviderInput planner 在 retained generation 与 fresh generation 中使用两套不同的消息排序算法：

```text
retained generation
    物理 append 已提交 unit
    -> 大体保留首次出现顺序

fresh/rollover generation
    重新读取 normalized transcript
    -> 按 lane 分组
    -> 把 current_run_tail 放到 current_user 前
    -> 把最新 ContextSource/clock 插入二者之间
```

这已经在真实 long PR4 dogfood 中产生以下 provider payload：

```text
assistant: 准备写第一个文件
tool_result: 第一个文件写入成功
assistant: 准备写第二个文件
tool_result: 第二个文件写入成功
runtime context
clock
user: 请创建这个技能
```

下一次 generation rebuild 又恢复为：

```text
user: 请创建这个技能
assistant: 准备写第一个文件
tool_result: 第一个文件写入成功
assistant: 准备写第二个文件
tool_result: 第二个文件写入成功
```

同一 durable trajectory 因调用时机和 generation lifecycle 不同而向 provider 呈现不同顺序。它同时破坏：

- 用户请求与 assistant/tool descendants 的因果关系；
- tool-call/tool-result 的解释上下文；
- 多步 agent loop 的行为稳定性；
- 人类对 provider trajectory 的审计能力；
- provider prefix cache；
- exact replay 与 live dispatch 的语义等价。

立即冻结以下 hard cut：

1. `PreparedTranscriptProviderProjectionFact` 的 ordered messages 是 transcript provider 顺序的唯一真源；
2. ProviderInput planner 不得再次按 lane 重排、复制或移动 transcript message；
3. 同一 generation 中，已提交 provider-visible units 只允许保持不变并在尾部追加；
4. `current_user` 只能出现在其真实因果位置，禁止作为每个 model step 的“末尾 trigger”被移动或重发；
5. provider-visible 内容不变、只有 attribution 变得更完整时，不得 rollover；
6. 普通 run/context-window 边界不得自动伪装成 `context_window_compaction`；
7. 真正 rollover 后可以产生新 prefix，但其 transcript 必须仍满足唯一因果顺序；
8. 修复完成前，prompt-cache 指标只能作为故障证据，不能作为正确性证明。

---

## 1. 问题是如何被发现的

### 1.1 最初信号：DeepSeek cache 命中率突降

2026-07-19 对 DeepSeek 控制台 token 用量进行审查时发现：7 月 16 日后输入规模上升，但 cache hit token 占比明显下降。最初怀疑包括：

- compile-time timestamp 改写旧 prompt；
- model stream durable 写入影响调用间隔；
- DeepSeek cache 异步构建延迟；
- system prompt 或 tool catalog 在中途变化；
- ContextSource 每轮重新渲染；
- generation rollover 破坏公共 prefix。

这个阶段只把问题视为 cache 退化，还没有证明语义顺序错误。

### 1.2 long PR4 dogfood 的 provider usage 证据

对 long PR4 dogfood 的一次完整成功轨迹进行统计：

- 13 次 model call；
- 12 次返回可用 usage；
- 总 input tokens：109,188；
- 总 cached tokens：8,832；
- cache token ratio：约 8.09%；
- 大部分调用只报告约 640 或 768 cached tokens。

这意味着 DeepSeek 大多只识别到很短的稳定根部，而没有识别不断增长的长历史 prefix。

### 1.3 排除 provider cache 本身失效

使用同一份真实 DeepSeek provider payload 做 exact replay：

```text
首次发送
-> 等待 30 秒
-> byte/semantic-identical payload 再次发送
```

第二次返回：

```text
cached_tokens = 13,952
input_tokens  = 14,067
cache ratio   = 99.18%
```

因此可以排除“DeepSeek 不支持该请求形状”或“provider cache 整体失效”。异步 cache build 确实存在，但不足以解释 long PR4 长期只命中 640/768 tokens。

### 1.4 排除固定 system prompt 与 tools 中途变化

从 PostgreSQL session ledger 和 provider-input artifacts 恢复 long PR4 的 13 份 exact carrier。该轨迹的 runtime session 为：

```text
runtime:f198655d7a6d4dc688f015aa59a9b4eb
```

恢复结果：

- 11 个 ProviderInput generation；
- 10 次 rollover；
- 11 个 generation 的 `system_instruction_semantic_fingerprint` 完全相同；
- 11 个 generation 的 `tool_catalog_semantic_fingerprint` 完全相同；
- 所有 carrier 的固定 `system_prompt` 内容完全相同；
- 后期存在少量 `role="system"` 的 Pulsara recovery note，但前 7 次调用完全不存在这类消息。

因此固定 system prompt/tool catalog 不是早期 cache 退化的原因。后期 recovery note 会影响其插入位置之后的 prefix，但不是根因。

### 1.5 exact carrier diff 首次暴露重排

Call 2 与 Call 3 的 system prompt、tools 和绝大多数历史内容相同，但 ordered messages 的第一个差异出现在 index 1：

```text
Call 2
0  Runtime Context
1  Available Capabilities
2  Recalled Memory
3  Clock/Revision
4  User: 阅读 README
5  Assistant
6  ToolResult
7  ToolResult
8  New Memory Revision
9  New Clock

Call 3
0  Runtime Context
1  User: 阅读 README
2  Assistant
3  ToolResult
4  ToolResult
5  Assistant final
6  Available Capabilities
7  Recalled Working Context
8  Clock/Revision
9  User: 创建 skill
```

DeepSeek prefix cache 只能命中连续前缀。index 1 出现差异后，即使 `Available Capabilities` 和其他内容稍后再次出现，也不能跳过差异继续命中。

### 1.6 因果倒置的确认

进一步还原 Call 5 与 Call 6 后确认，这不是无害的 context placement 调整。

Call 5 中，当前 user request 被 planner 放在已经由它触发的 assistant/tool events 之后：

```text
assistant: 创建第一个文件
tool_result: success
assistant: 创建第二个文件
tool_result: success
context revisions
user: 创建这个技能
```

Call 6 的 fresh rebuild 则恢复为：

```text
user: 创建这个技能
assistant: 创建第一个文件
tool_result: success
assistant: 创建第二个文件
tool_result: success
assistant: 已经完成
```

这证明同一 canonical trajectory 的 provider-visible 因果顺序会随 generation 路径变化。该 finding 从性能问题升级为 P1 semantic correctness 问题。

---

## 2. 根因分析

### 2.1 Compiler 与 ProviderInput planner 有两套 ordering truth

当前 transcript provider rendering contract 冻结的 lane 顺序是：

```text
prior_history
-> current_user
-> current_run_tail
-> runtime_system
```

但 ProviderInput planner 实际重新分组：

```python
append_units = (
    *transcript_before_trigger,
    *non_clock_source_units,
    *clock_units,
    *current_trigger_units,
)
```

其中：

```text
transcript_before_trigger
    = prior_history + current_run_tail

current_trigger_units
    = current_user
```

因此 planner 实际顺序是：

```text
prior_history
-> current_run_tail
-> ContextSource revisions
-> clock
-> current_user
```

这直接违反已经冻结的 transcript lowering contract。Planner 没有把 compiler 的 ordered projection 当作最终 authority，而是进行了第二次语义解释。

### 2.2 “current user 永远放最后”混淆了 task salience 与消息 chronology

实现显然试图让当前用户任务保持在 prompt 尾部，使模型在每次 tool loop 后仍看到一个显眼的 user trigger。

但 `role="user"` 具有 provider 语义。把原始 user message 移到其 assistant/tool descendants 后面，不等于“重申任务”，而是向模型表示：

```text
工具已经执行完
-> 用户现在又提出一次相同请求
```

若系统确实需要 model-step trigger，必须使用独立、typed、provider-visible control observation，并冻结其语义；不得移动或复制原始 user message 来模拟 trigger。V1 不引入该 trigger，正常 tool result/ContextSource observation足以触发下一 model step。

### 2.3 Retained append 与 fresh rebuild 不等价

Retained generation 保存旧物理 vector，只在尾部追加：

```text
root
-> first context frame
-> user
-> assistant/tool tail
-> later context revisions
```

Fresh generation 则：

- 丢弃 superseded ContextSource revision；
- 只选择当前 latest snapshot；
- 把 normalized transcript按lane重新分组；
- 把最新 ContextSource/clock移到当前 user 前；
- 重新生成整棵 provider vector。

因此 fresh rebuild 并不是 retained vector 的语义等价重放，而是另一种 prompt 设计。当前没有 invariant 证明两者对 provider 等价。

### 2.4 Generation scope 错误绑定 run-scoped ContextWindow

Main-agent generation scope当前包含：

```text
runtime_session_id
context_window_id
context_window_generation
```

但 production 每个 run 都创建新的 `context_window_id`。即使：

- system prompt不变；
- tools不变；
- provider compatibility不变；
- 没有发生Long-Horizon rewrite；

Coordinator仍会因为scope fingerprint变化而rollover前一个open generation。

Provider prefix continuity是session/call-lane级属性，不应自动继承run-scoped window生命周期。ContextWindow可以作为authority attribution和显式rewrite trigger，但不应作为ordinary generation identity。

### 2.5 Attribution 漂移被错误当成 provider semantic drift

Transcript frontier guard当前比较 `owner_semantic_fingerprint`。该值同时覆盖：

- provider-visible message semantic；
- transcript attribution fingerprint；
- terminal/control/pairing来源归因。

同一 provider message在以下状态转换后，attribution可能变得更完整：

```text
pending terminal projection
-> ACCEPTED disposition
-> canonical pairing/result attribution
```

即使最终 provider message内容完全相同，owner fingerprint仍可能变化。Planner随后抛出：

```text
ProviderInputRolloverRequired("transcript frontier prefix changed")
```

这把物理归因补全错误升级为provider prefix break。

### 2.6 Rollover reason 分类掩盖真实原因

Coordinator根据 exception message做字符串分类：

```text
包含 "transcript frontier"
-> CONTEXT_WINDOW_COMPACTION
```

因此 long PR4 记录的10次 `context_window_compaction` 并不等于10次真实Long-Horizon compaction。它混合了：

- run/window scope变化；
- attribution-only frontier drift；
- current-user/current-tail placement漂移；
- 其他未分类的rebuild要求。

文本匹配不是durable transition contract，必须删除。

### 2.7 缺少 provider-boundary causal validator

当前写入与dispatch前已经校验：

- artifact hash；
- vector/root fingerprint；
- generation CAS；
- budget；
- ModelStart reference；
- exact carrier fingerprint。

但没有校验：

- user message必须早于由它触发的assistant reply；
- assistant tool call必须早于对应tool result；
- tool result必须早于消费它的assistant continuation；
- recovery note必须晚于其来源terminal事实；
- 同一transcript message不得被移动或重复；
- planner carrier messages必须逐项等于compiler冻结的ordered transcript projection加合法ContextSource frame。

完整identity join不能替代因果顺序校验。

### 2.8 当前代码真值定位

| 根因 | 当前落点 | 必须采取的修复 |
|---|---|---|
| transcript正式lowering顺序 | `runtime/context_input/provider_projection.py::build_default_transcript_invocation_rendering_contract()` | 保留为唯一ordering contract，并由最终carrier逐项验证 |
| planner二次重排 | `runtime/provider_input/planner.py::plan_provider_input_append()` 中 `transcript_before_trigger/current_trigger_units/append_units` | 删除二次分组，改为ordered projection strict suffix |
| frontier使用attribution owner判断provider变化 | `runtime/provider_input/planner.py::_new_transcript_units()` | continuity只比较provider semantic与causal identity |
| fresh generation重新聚合root/transcript/source | `runtime/provider_input/planner.py::_root_units()`、`_changed_source_units()` | 使用冻结的root + chronological transcript + typed ContextFrame算法 |
| run/window scope触发rollover | `runtime/provider_input/coordinator.py::prepare_session_call()` 的 `latest_open`/scope判断 | 改成session continuity scope；window仅作attribution/explicit rewrite cause |
| exception文本映射durable reason | `runtime/provider_input/coordinator.py::_rollover_reason()` | 替换为typed rollover request/reason |
| dispatch carrier由vector hydrate | `runtime/provider_input/materialization.py::hydrate_carrier()`、`append_carrier()` | 保留，但增加ordered transcript与causal proof校验 |
| recovery/system note写入canonical transcript | `runtime/authority_materialization/transcript_reducer.py::_append_run_end_note()`及terminal note路径 | 保留typed chronology，禁止临时加入后消失或移动 |

修复不得只改变`hydrate_carrier()`输出顺序。若generation vector、frontier、append artifact和ModelStart reference仍保存旧顺序，dispatch时临时排序会破坏exact replay与stable candidate identity。

---

## 3. 不变量 hard cut

### 3.1 唯一 transcript order authority

唯一顺序真源为：

```text
Canonical stable transcript / rewrite facts
        -> PreparedTranscriptProviderProjectionFact
        -> ProviderOrderedTranscriptProjectionFact
```

后一层是前一层的typed、immutable、provider-facing projection，不是第二个transcript reducer。ProviderInput planner只能：

1. 验证projection与canonical stable transcript/rewrite refs一致；
2. 验证pending accepted continuation已经成为projection中的唯一unit；
3. 计算generation已提交frontier并取严格suffix；
4. 在冻结的ContextSource frame insertion point插入新source units；
5. 追加到现有vector。

禁止：

- 按role、lane或segment重新排序；
- 把`current_user`抽出后移到末尾；
- 把`current_run_tail`移到其user predecessor之前；
- 从完整`LLMContext.messages`重新推断另一套transcript顺序；
- 由pending continuation materializer自行插入、移动或替换unit；
- 以task salience为理由复制原始user message。

### 3.2 Transcript causal order

完整event lifecycle因果边继续由canonical transcript reducer验证，包括RunStart、terminal projection、disposition、tool pairing和RunEnd。Provider planner不重复实现event lifecycle reducer，只验证实际模型可见的message/block边：

```text
current user message
    < accepted assistant/tool-call message
    < matching provider-visible ToolResult message
    < consuming assistant continuation
```

`<`表示provider projection-local position严格递增，不等于EventLog sequence，也不要求相邻；provider API要求tool-call/result相邻时，adapter-specific validator继续施加更强约束。

Compaction summary使用confirmed rewrite range authority证明它替代哪些stable entries。Planner验证summary projection position与rewrite authority，不使用summary event sequence决定它在provider transcript中的位置。RunEnd不是provider-visible message，不进入planner causal edge输入。

### 3.3 Current user single-placement invariant

每个 `CurrentUserMessageFact.message_id` 在一个generation中最多对应一个provider transcript unit。

- 首次追加后的provider wire fragment与projection-local position永不变化；
- 后续model step不得再次追加；
- `current_user | current_run_tail | prior_history`是invocation classification attribution，不是continuity semantic；
- 从`current_user`转为`prior_history`不得改变role/content/tool/framing或causal placement；
- classification变化不得触发empty append、model preparation或rollover；
- 若某provider framing真实依赖lane，该wire framing必须在unit首次提交时永久冻结，后续classification不得改写它；
- fresh generation中按canonical chronology重新出现一次，不得放到其descendants之后。

### 3.4 Same-generation strict-prefix invariant

设generation在revision `r` 的provider-visible semantic units为 `U_r`：

```text
U_(r+1) = U_r ++ Delta_(r+1)
```

必须满足：

- `U_r` 中每个wire semantic与causal placement semantic逐项不变；
- unit count不减少；
- 已提交vector ordinal不变化；
- 不原地替换正文；
- invocation classification或physical attribution变化不改变continuity；
- `Delta`为空时不得创建append candidate或model preparation；
- dispatch carrier必须从同一vector materialize，不得另行排序。

### 3.5 三层 identity hard cut

V1只保留三种身份：

```text
1. Provider wire semantic
   实际role/content/thinking/tool/framing/tokenization语义

2. Causal/projection semantic
   canonical source kind、message identity、rewrite identity、
   projection-local相对顺序和可见causal edges

3. Physical attribution/materialization
   current/prior/tail invocation classification、event/artifact refs、
   ledger horizons、checkpoint、generation/model-call attribution
```

`current_user | current_run_tail | prior_history`只属于第3层。现有`ProviderInputUnitSemanticFact.provider_lane`必须在schema hard cut中拆除或收窄为首次提交后不可变化的真实wire framing；不得继续装载invocation classification。

Continuity比较第1层与第2层，不比较第3层。第3层包含：

- event IDs/sequences；
- artifact placement refs；
- terminal projection/disposition refs；
- pairing/control provenance；
- ledger horizons；
- checkpoint/materialization refs；
- compile section/lane classification。

V1唯一attribution更新规则为：

- committed unit attribution永久不可变；
- later attribution不修改unit、core或frontier；
- V1不定义、不写入unit-attribution supplement event；
- 每次compile的latest attribution只保存在已确认ContextInputManifest的invocation-local projection fact中，Inspector可联合展示，但不回写committed generation；
- 只有attribution变化时不得创建空append/model preparation。

### 3.6 True rollover仍需因果正确

Rollover允许新generation不以前一generation为provider prefix，但不允许改变canonical因果关系。Generation-root snapshot属于root，不是InvocationContextFrame。Fresh generation冻结为：

```text
generation root（含合法root snapshot）
-> prior chronological transcript
-> current invocation context frame
-> current user
-> current run tail（若rollover发生在run中途）
```

任何user/assistant/tool/recovery/summary transcript unit的相对顺序必须与ordered projection一致。不得使用“complete transcript之后再放current frame”的另一套顺序。

---

## 4. ProviderInput ordering 模型

### 4.1 三类有序单元

```text
GenerationRootUnits
    system/developer instruction
    tool catalog
    immutable generation-root context
    auxiliary-frame rebase后的latest root snapshots

TranscriptUnits
    canonical user/assistant/tool/recovery/compaction-summary messages
    严格按ordered provider projection

InvocationContextFrameUnits
    本次新增ContextSource revisions
    runtime clock observation
    lifecycle/status observation
```

ContextSource frame不是transcript chronology的替代品。即使adapter最终使用`user`或其他兼容role传输，也必须保留typed frame identity，Inspector不得把它显示为真实用户发言。

### 4.2 首次generation调用

```text
GenerationRootUnits
-> prior chronological transcript
-> current invocation context frame
-> current user
-> current run tail（若rollover发生在run中途）
```

Frame必须保存前后transcript identity与最终vector ordinal range。若fresh generation发生在run中途，current user必须仍位于该run的assistant/tool descendants之前。

### 4.3 Retained generation新增用户turn

当previous frontier已覆盖旧transcript，新的projection suffix包含新user时：

```text
existing prefix
-> transcript suffix before new user（若有）
-> current invocation context frame
-> new current user
-> projection suffix after new user（通常为空）
```

Frame insertion point是“本次新出现的current user之前”，不是移动任何旧user。

### 4.4 Retained generation继续tool loop

当current user已存在于committed prefix，新的projection suffix只包含assistant/tool tail：

```text
existing prefix（其中已有current user）
-> new assistant/tool transcript suffix
-> current invocation context frame
```

禁止再次追加或移动current user。

### 4.5 Dynamic ContextSource revision

同一generation中，ContextSource revision只能追加到尾部ContextFrame。旧revision保留，并由versioned `latest appended revision wins` envelope表达supersession。

只有以下两种confirmed authority可以在新generation删除旧provider units：

- auxiliary-frame rebase只删除被证明已superseded的ContextSource/clock/lifecycle frames，canonical transcript逐项不变；
- Long-Horizon rewrite按confirmed member/source range替换canonical transcript。

普通model step不得隐式执行任一行为。

---

## 5. DTO、factory 与 durable carrier hard cut

### 5.1 三层unit identity

```python
class ProviderWireMessageSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_wire_message_semantic.v1"]
    provider_message: ProviderMessageFragmentFact
    wire_framing_contract_fingerprint: str
    wire_semantic_fingerprint: str


class DirectStableMessageSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["direct_stable_message_semantic_source.v1"]
    source_kind: Literal["direct_stable_message"]
    stable_entry_kind: Literal["message"]
    canonical_message_id: str
    stable_entry_semantic_fingerprint: str
    source_semantic_fingerprint: str


class DerivedToolResultMessageSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["derived_tool_result_message_semantic_source.v1"]
    source_kind: Literal["derived_tool_result_message"]
    tool_result_leaf_semantic_fingerprint: str
    tool_pair_semantic_fingerprint: str
    terminal_projection_semantic_fingerprint: str
    source_semantic_fingerprint: str


class ProviderCompactionRewriteAuthorityReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_compaction_rewrite_authority_reference.v1"]
    compaction_completed_event_reference: ContextEventReferenceFact
    source_document_fingerprint: str
    summary_semantic_fingerprint: str
    replaced_first_stable_ordinal: int
    replaced_last_stable_ordinal: int
    replaced_member_count: int
    replaced_member_semantic_accumulator: str
    resulting_stable_transcript_semantic_fingerprint: str
    rewrite_contract_fingerprint: str
    reference_fingerprint: str


class CompactionReplacementSummarySemanticSourceFact(FrozenFactBase):
    schema_version: Literal["compaction_replacement_summary_semantic_source.v1"]
    source_kind: Literal["compaction_replacement_summary"]
    summary_semantic_fingerprint: str
    replaced_source_range_fingerprint: str
    resulting_stable_transcript_semantic_fingerprint: str
    rewrite_contract_fingerprint: str
    source_semantic_fingerprint: str


class LifecycleNoteSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["lifecycle_note_semantic_source.v1"]
    source_kind: Literal["lifecycle_note"]
    note_semantic_fingerprint: str
    cause_semantic_fingerprint: str
    lifecycle_note_contract_fingerprint: str
    source_semantic_fingerprint: str


ProviderTranscriptUnitSemanticSourceFact = Annotated[
    DirectStableMessageSemanticSourceFact
    | DerivedToolResultMessageSemanticSourceFact
    | CompactionReplacementSummarySemanticSourceFact
    | LifecycleNoteSemanticSourceFact,
    Field(discriminator="source_kind"),
]


class DirectStableMessageSourceAttributionFact(FrozenFactBase):
    schema_version: Literal["direct_stable_message_source_attribution.v1"]
    source_kind: Literal["direct_stable_message"]
    stable_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    source_semantic_fingerprint: str
    fact_fingerprint: str


class DerivedToolResultMessageSourceAttributionFact(FrozenFactBase):
    schema_version: Literal["derived_tool_result_message_source_attribution.v1"]
    source_kind: Literal["derived_tool_result_message"]
    tool_result_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    tool_pair_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    terminal_projection_reference: TerminalProjectionReferenceFact
    source_semantic_fingerprint: str
    fact_fingerprint: str


class CompactionReplacementSummarySourceAttributionFact(FrozenFactBase):
    schema_version: Literal["compaction_replacement_summary_source_attribution.v1"]
    source_kind: Literal["compaction_replacement_summary"]
    summary_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    rewrite_authority_reference: ProviderCompactionRewriteAuthorityReferenceFact
    source_semantic_fingerprint: str
    fact_fingerprint: str


class LifecycleNoteSourceAttributionFact(FrozenFactBase):
    schema_version: Literal["lifecycle_note_source_attribution.v1"]
    source_kind: Literal["lifecycle_note"]
    note_event_reference: ContextEventReferenceFact
    cause_event_reference: ContextEventReferenceFact
    source_semantic_fingerprint: str
    fact_fingerprint: str


ProviderTranscriptUnitSourceAttributionFact = Annotated[
    DirectStableMessageSourceAttributionFact
    | DerivedToolResultMessageSourceAttributionFact
    | CompactionReplacementSummarySourceAttributionFact
    | LifecycleNoteSourceAttributionFact,
    Field(discriminator="source_kind"),
]


class ProviderTranscriptSourceSelectionRuleFact(FrozenFactBase):
    schema_version: Literal["provider_transcript_source_selection_rule.v1"]
    canonical_entry_kind: Literal[
        "message",
        "tool_pair",
        "tool_result_projection_ref",
    ]
    eligible_message_segments: tuple[
        Literal[
            "compaction_summary",
            "prior_history",
            "current_user",
            "current_run_tail",
            "recovery_note",
            "terminal_lifecycle_note",
        ],
        ...,
    ]
    selection_outcome: Literal["emit_provider_unit", "companion_only"]
    selected_source_kind: Literal[
        "direct_stable_message",
        "derived_tool_result_message",
        "compaction_replacement_summary",
        "lifecycle_note",
    ] | None
    required_companion_entry_kinds: tuple[
        Literal["message", "tool_pair", "tool_result_projection_ref"],
        ...,
    ]
    rule_fingerprint: str


class ProviderTranscriptSourceSelectionContractFact(FrozenFactBase):
    schema_version: Literal["provider_transcript_source_selection_contract.v1"]
    contract_id: Literal["pulsara.provider-transcript-source-selection"]
    contract_version: Literal["1"]
    rules: tuple[ProviderTranscriptSourceSelectionRuleFact, ...]
    contract_fingerprint: str
```

Compaction summary不伪造`run_id`，也不使用event sequence作为provider position。每种source branch都由同一factory同时生成semantic source与source attribution；两者的`source_kind`和`source_semantic_fingerprint`必须相等。Semantic branch只描述canonical identity，attribution branch才保存event/artifact references。

V1 source-selection contract必须且只能包含以下互斥规则：

| Canonical authority | 唯一source branch | 必需join |
|---|---|---|
| message leaf，segment为`prior_history | current_user | current_run_tail` | `direct_stable_message` | exact message leaf semantic/content |
| message leaf，segment为`compaction_summary` | `compaction_replacement_summary` | exact summary leaf + confirmed rewrite authority |
| message leaf，segment为`recovery_note | terminal_lifecycle_note` | `lifecycle_note` | exact note leaf + note/cause events |
| tool-result projection leaf | `derived_tool_result_message` | exact result leaf + exact pair leaf + terminal projection |
| tool-pair leaf自身 | 不产生provider message unit | 只作为derived result的companion authority |

同一canonical entry匹配0条或多于1条规则均为contract failure。Direct branch明确拒绝`compaction_summary | recovery_note | terminal_lifecycle_note`，不得因为它们物理上也是message leaf而绕过专用authority。

`selection_outcome="emit_provider_unit"`时`selected_source_kind`必须非空；`companion_only`时必须为`None`。Tool-pair是V1唯一companion-only rule，其eligible message segments必须为空tuple。

`DerivedToolResultMessageSourceAttributionFact.tool_result_leaf_reference.entry_kind`必须为`tool_result_projection_ref`，`tool_pair_leaf_reference.entry_kind`必须为`tool_pair`。Result leaf的tool call ID、tool name、result ordinal与terminal semantic必须分别等于pair leaf和terminal projection；pair的`result_block_position`必须指向该result leaf的stable ordinal，并且pair leaf必须是reducer为这次join生成的唯一companion。三者任一缺失、重复或semantic冲突均为`authority_untrusted`。

共享的`TranscriptProjectionLeafEntryReferenceFact.entry_kind`必须使用canonical leaf discriminator `message | tool_pair | tool_result_projection_ref`，删除`tool_result`别名。Summary leaf reference的entry kind与summary schema必须由rewrite contract精确约束。Rewrite authority的stable ordinal range描述被替代的原projection成员，不表示compaction event自身的ledger sequence。

`ProviderCompactionRewriteAuthorityReferenceFact`的唯一factory还必须验证：

- completed event reference精确指向confirmed compaction terminal event，且其stored sequence不超过projection authority horizon；
- `replaced_first_stable_ordinal <= replaced_last_stable_ordinal`，且range长度等于`replaced_member_count`；
- member accumulator由被替代的stable leaf references按ordinal顺序重算；
- source document、summary semantic、resulting stable transcript三者与completed event及summary leaf强join；
- `replaced_source_range_fingerprint`由同一range、member accumulator和rewrite contract唯一派生，不接受caller自报；
- 任何range、artifact hash、historical binding或resulting transcript冲突均为`authority_untrusted`，不是普通compression miss。

现有`TranscriptProjectionLeafEntryReferenceFact`从`governance_evidence.py`提升到`primitives/transcript_projection.py`或共享primitives模块，schema保持唯一；governance与provider projection共同引用，禁止复制两份同形DTO。

```python
class ProviderTranscriptNodeIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_transcript_node_identity.v1"]
    source_identity_fingerprint: str
    wire_semantic_fingerprint: str
    node_identity_fingerprint: str


class ProviderProjectionPositionFact(FrozenFactBase):
    schema_version: Literal["provider_projection_position.v1"]
    projection_index: int
    predecessor_node_identity_fingerprint: str | None
    position_contract_fingerprint: str
    position_fingerprint: str


class ProviderCausalPlacementSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_causal_placement_semantic.v1"]
    source: ProviderTranscriptUnitSemanticSourceFact
    node_identity: ProviderTranscriptNodeIdentityFact
    position: ProviderProjectionPositionFact
    visible_causal_predecessor_node_identity_fingerprints: tuple[str, ...]
    causal_semantic_fingerprint: str


class ProviderInvocationClassificationAttributionFact(FrozenFactBase):
    schema_version: Literal["provider_invocation_classification_attribution.v1"]
    invocation_classification: Literal[
        "prior_history",
        "current_user",
        "current_run_tail",
        "compaction_summary",
        "lifecycle_note",
    ]
    compile_context_id: str
    section_id: str
    fact_fingerprint: str


class ProviderOrderedTranscriptUnitFact(FrozenFactBase):
    schema_version: Literal["provider_ordered_transcript_unit.v2"]
    wire_semantic: ProviderWireMessageSemanticFact
    causal_placement: ProviderCausalPlacementSemanticFact
    source_attribution: ProviderTranscriptUnitSourceAttributionFact
    invocation_attribution: ProviderInvocationClassificationAttributionFact
    unit_causal_semantic_fingerprint: str
    fact_fingerprint: str
```

`unit_causal_semantic_fingerprint`只覆盖wire semantic与causal placement，不覆盖source attribution、invocation attribution或physical refs。`fact_fingerprint`覆盖完整semantic与两类attribution；所有flat event/artifact ref集合只能从typed source attribution派生，不能再作为caller可独立填写的第二组字段。

Node identity只覆盖source identity与wire semantic，不覆盖projection position。Position只引用已经存在的predecessor node identity；successor只能由完整projection validator从下一个unit推导，不进入当前unit、position或continuity fingerprint。这样旧末节点从`[A]`扩展为`[A, B]`时，A的全部wire/causal identity保持不变。

`visible_causal_predecessor_node_identity_fingerprints`只保存直接provider-visible因果边，不保存全部历史前缀或传递闭包，避免O(n²) projection。其上限由versioned causal-validation contract冻结，必须至少覆盖一次合法model call的最大tool-result fan-in、一个user predecessor与一个lifecycle cause；config doctor必须证明该上限不小于已解析provider/tool-call hard bound。

Projection factory必须一次性冻结以下position matrix：

- `projection_index` 严格为 `0..unit_count-1`；
- 首unit的predecessor为`None`，否则等于前一unit的node identity；
- validator可在当次完整projection中派生每个unit的successor用于邻接审计，但不得把该派生值写回unit或任何continuity identity；
- 每条visible causal predecessor必须在同一projection中唯一存在，且其index严格小于当前unit；
- node identity在同一projection中必须唯一。

### 5.2 Ordered projection与reference

```python
class ProviderOrderedTranscriptProjectionFact(FrozenFactBase):
    schema_version: Literal["provider_ordered_transcript_projection.v2"]
    rendering_contract_fingerprint: str
    source_selection_contract_fingerprint: str
    resolved_causal_physical_policy_fingerprint: str
    stable_transcript_semantic_fingerprint: str
    ordered_units: tuple[ProviderOrderedTranscriptUnitFact, ...]
    ordered_wire_semantic_accumulator: str
    ordered_causal_semantic_accumulator: str
    causal_order_proof_fingerprint: str
    projection_semantic_fingerprint: str
    fact_fingerprint: str


class ProviderOrderedTranscriptProjectionIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_ordered_transcript_projection_identity.v1"]
    projection_semantic_fingerprint: str
    unit_count: int
    ordered_wire_semantic_accumulator: str
    ordered_causal_semantic_accumulator: str
    identity_fingerprint: str


class ContextInputManifestProjectionReferenceFact(FrozenFactBase):
    schema_version: Literal["context_input_manifest_projection_reference.v1"]
    context_id: str
    input_manifest_artifact_id: str
    input_manifest_content_fingerprint: str
    input_manifest_fact_fingerprint: str
    projection_identity: ProviderOrderedTranscriptProjectionIdentityFact
    reference_fingerprint: str
```

唯一factory从现有`PreparedTranscriptProviderProjectionFact`和stable transcript/rewrite refs生成该projection。V1不创建独立projection artifact：

- 完整`ProviderOrderedTranscriptProjectionFact`与bounded identity直接内嵌在`ContextCompileInputManifestFact`及其现有content-addressed manifest artifact中；
- manifest内部的`PreparedProviderInputPlanFact`只保存nested projection identity与rollover intent，不反向引用尚未生成的manifest artifact；
- manifest FULL后先构造唯一`PreparedProviderInputAppendCandidateFact`，再由`ContextCompiledEvent`保存manifest reference与plan/candidate fingerprint；
- append event、generation core attribution与ModelStart reference均引用同一个manifest projection reference；
- 不存在独立projection artifact；projection content的write service、retention与GC完全继承manifest owner。

`ContextInputManifestProjectionReferenceFact`只能在manifest write result为`stored | confirmed_existing`后构造。其artifact ID、content fingerprint、manifest fact fingerprint与nested projection identity必须从同一stable manifest candidate和FULL acknowledgement重算，禁止caller拼装。

Manifest persistence沿用现有session-owned `ContextInputManifestWriteService`，并冻结：

- stable candidate canonical bytes在首次write前生成，projection及identity均属于这些字节；
- NONE/confirmed-absent只允许以同一stable candidate重试或写typed compile failure，禁止提交`ContextCompiled(status="compiled")`；
- UNKNOWN、deadline和caller cancellation必须保留manifest owner并完成physical drain/confirmation；确认前禁止append candidate publication和ModelStart；
- FULL后发生caller cancellation仍采用原FULL结果，不能把已存在manifest降级为absent；
- RuntimeSession close必须drain所有manifest write/confirmation owner；
- retention/GC以引用该manifest的ContextCompiled、append和ModelStart durable facts为reachable roots；
- durable reference存在但artifact缺失、hash冲突或nested projection identity不一致时为`authority_untrusted`并latch；不得从当前live reducer临时重建替代artifact。

该projection仍不是第二个transcript reducer；manifest只是已经确认存在的immutable carrier。

现有durable DTO必须以下表原子升级：

| Owner DTO | Required field | Invariant |
|---|---|---|
| `ContextCompileInputManifestFact` | full projection + projection identity + `prepared_provider_input_plan` | 完整projection重算后等于identity，plan只引用nested identity |
| `PreparedProviderInputPlanFact` | projection/validation/frame/delta/rollover intent | generation-neutral，不反向引用manifest |
| `PreparedProviderInputAppendCandidateFact` | plan + `manifest_projection_reference` | 只能在FULL后从同一plan/manifest构造 |
| `ContextCompiledEvent(status="compiled")` | `manifest_projection_reference` + plan/candidate fingerprint | 只能从FULL manifest acknowledgement与唯一candidate构造 |
| `ContextCompiledEvent(status="failed")` | `manifest_projection_reference=None` | failed event不得伪造confirmed carrier |
| `ProviderInputAppendCommittedEvent` | `manifest_projection_reference` + nested identity fingerprint | 与ContextCompiled及append candidate逐项相等 |
| `CommittedProviderInputReferenceFact` | `manifest_projection_reference_fingerprint` | 与append event及ModelStart同批strong join |

上述schema切换必须与CO2垂直hard cut同批落地，不允许nullable legacy branch。

### 5.3 Provider continuity frontier

```python
class ProviderTranscriptFrontierFact(FrozenFactBase):
    schema_version: Literal["provider_transcript_frontier.v2"]
    committed_transcript_unit_count: int
    committed_ordered_wire_semantic_accumulator: str
    committed_ordered_causal_semantic_accumulator: str
    stable_transcript_prefix_fingerprint: str
    provider_semantic_frontier_fingerprint: str
```

Frontier不保存invocation lane/classification、event refs或单一`causal_ordinal`。它由projection prefix唯一factory重算。

### 5.4 Invocation context frame semantic与placement

```python
class ProviderInvocationContextFrameSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_invocation_context_frame_semantic.v1"]
    ordered_source_unit_wire_fingerprints: tuple[str, ...]
    source_head_set_fingerprint: str
    frame_semantic_fingerprint: str


class ProviderInvocationContextFramePlacementFact(FrozenFactBase):
    schema_version: Literal["provider_invocation_context_frame_placement.v1"]
    semantic: ProviderInvocationContextFrameSemanticFact
    insertion_kind: Literal[
        "before_new_current_user",
        "after_new_transcript_tail",
    ]
    preceding_transcript_node_identity_fingerprint: str | None
    following_transcript_node_identity_fingerprint: str | None
    insertion_policy_id: str
    insertion_policy_version: str
    insertion_policy_fingerprint: str
    frame_id: str
    generation_id: str
    resolved_model_call_id: str
    model_call_index: int
    first_vector_ordinal: int
    last_vector_ordinal: int
    ordered_source_unit_range_accumulator: str
    frame_fact_fingerprint: str
```

空frame不创建placement。`frame_semantic_fingerprint`只覆盖有序source wire semantics与source-head set，不覆盖插入位置、generation或model-call attribution。`frame_fact_fingerprint`覆盖完整semantic、相邻transcript node、policy、vector range和运行归因。

`first_vector_ordinal <= last_vector_ordinal`；range count必须等于semantic中的source unit count。`ordered_source_unit_range_accumulator`必须从最终provider vector的精确该range重算，并与semantic中ordered wire fingerprints的accumulator相等。Preceding/following node必须与ordered transcript projection和最终vector两侧实际相邻node一致。Generation-root snapshot归root contract所有，不使用InvocationContextFrame分支。

### 5.5 Accepted continuation projection join

```python
class ProviderAcceptedContinuationProjectionJoinFact(FrozenFactBase):
    schema_version: Literal["provider_accepted_continuation_projection_join.v1"]
    resolved_model_call_id: str
    reply_id: str
    terminal_projection_reference: TerminalProjectionReferenceFact
    accepted_disposition_event_reference: ContextEventReferenceFact
    ordered_projection_identity_fingerprint: str
    matched_projection_index: int
    matched_unit_causal_semantic_fingerprint: str
    continuation_join_contract_fingerprint: str
    fact_fingerprint: str
```

Authority slice必须同时包含terminal projection与`ACCEPTED` disposition。Pending continuation不是unit producer，只是join obligation：

- resolved call、reply、terminal ref、disposition ref必须匹配projection中的唯一unit；
- pending materializer只允许hydrate该unit已冻结的wire fragment；
- projection尚未包含它时返回`not_ready`，不插入 provisional unit；
- 匹配为0但projection high-water已越过合法carrier时为`contract_mismatch`；
- 匹配多于1、semantic不一致或refs冲突时为`authority_untrusted`；
- 禁止按“相同fragment”模糊查找、替换或选择最后一个candidate。

### 5.6 Causal validation、delta proof与durable join

```python
class ProviderInputCausalValidationResult(FrozenFactBase):
    schema_version: Literal["provider_input_causal_validation_result.v2"]
    status: Literal["valid", "invalid"]
    projection_identity_fingerprint: str
    checked_visible_edge_count: int
    violation_reason: ProviderInputCausalValidationFailureReason | None
    violating_projection_indices: tuple[int, ...]
    validation_contract_fingerprint: str
    resolved_causal_physical_policy_fingerprint: str
    result_fingerprint: str


class ProviderTranscriptDeltaCommitProofFact(FrozenFactBase):
    schema_version: Literal["provider_transcript_delta_commit_proof.v1"]
    projection_identity_fingerprint: str
    predecessor_frontier_fingerprint: str
    delta_first_projection_index: int | None
    delta_last_projection_index: int | None
    ordered_delta_wire_accumulator: str
    ordered_delta_causal_accumulator: str
    continuation_joins: tuple[ProviderAcceptedContinuationProjectionJoinFact, ...]
    resulting_frontier: ProviderTranscriptFrontierFact
    resolved_causal_physical_policy_fingerprint: str
    proof_fingerprint: str


class PreparedProviderInputPlanFact(FrozenFactBase):
    schema_version: Literal["prepared_provider_input_plan.v1"]
    plan_kind: Literal[
        "initial_generation",
        "existing_generation_append",
        "rollover_initial_append",
    ]
    resolved_model_call_id: str
    continuity_scope_fingerprint: str
    target_generation_id: str
    predecessor_core_state_fingerprint: str | None
    ordered_transcript_projection_identity: (
        ProviderOrderedTranscriptProjectionIdentityFact
    )
    causal_validation: ProviderInputCausalValidationResult
    frame_placement: ProviderInvocationContextFramePlacementFact | None
    transcript_delta_proof: ProviderTranscriptDeltaCommitProofFact
    rollover_intent: ProviderInputRolloverIntentFact | None
    resulting_unit_vector_root_fingerprint: str
    resolved_causal_physical_policy_fingerprint: str
    plan_fingerprint: str


class PreparedProviderInputAppendCandidateFact(FrozenFactBase):
    schema_version: Literal["prepared_provider_input_append_candidate.v2"]
    plan: PreparedProviderInputPlanFact
    manifest_projection_reference: ContextInputManifestProjectionReferenceFact
    stable_append_event_id: str
    stable_model_start_event_id: str
    rollover_request: ProviderInputRolloverRequestFact | None
    candidate_fingerprint: str
```

`PreparedProviderInputPlanFact`属于manifest canonical bytes；`PreparedProviderInputAppendCandidateFact`只能在manifest FULL后构造。Initial plan要求predecessor与rollover intent均为空；existing append要求predecessor非空、intent为空；rollover initial要求二者均非空，且FULL后candidate中的request必须由同一intent和manifest reference生成。

Stable append/ModelStart event IDs由plan fingerprint与manifest reference确定性派生。因而manifest retry不改变plan，FULL后的candidate也只有一个合法identity。

Delta proof的nullability与算术矩阵唯一冻结为：

- `delta_first_projection_index` 与 `delta_last_projection_index` 必须all-null或all-present；
- all-null仅表示本次没有新transcript unit，此时两个delta accumulator必须为canonical empty accumulator，`resulting_frontier == predecessor_frontier`，`continuation_joins == ()`；
- all-present时first等于predecessor committed count，last等于resulting committed count减一，且完整range与projection suffix逐项相等；
- continuation joins按matched projection index严格递增且唯一，必须精确等于本次pending accepted obligations中落在delta range的集合；
- 允许“transcript delta为空、frame非空”的frame-only append；
- transcript delta与frame同时为空时，不得创建append candidate、`ContextCompiled` preparation或ModelStart。

唯一durable链路冻结为：

```text
ContextInputManifest artifact
  -> full ProviderOrderedTranscriptProjectionFact
  -> ProviderOrderedTranscriptProjectionIdentityFact
  -> PreparedProviderInputPlanFact

Manifest FULL acknowledgement
  -> ContextInputManifestProjectionReferenceFact
  -> PreparedProviderInputAppendCandidateFact

ContextCompiledEvent（manifest FULL后）
  -> ContextInputManifestProjectionReferenceFact
  -> exact plan/candidate fingerprints

PreparedProviderInputAppendCandidateFact
  -> exact nested projection identity via plan
  -> valid causal validation result
  -> frame placement fact（可空）
  -> transcript delta commit proof
  -> rollover request（仅rollover plan）

ProviderInputAppendCommittedEvent
  -> same ContextInputManifestProjectionReferenceFact
  -> same nested projection identity
  -> same validation result fingerprint
  -> same frame placement fingerprint（可空）
  -> exact transcript delta proof

CommittedProviderInputGenerationCoreStateFact
  -> delta proof.resulting_frontier

CommittedProviderInputReferenceFact in ModelCallStart
  -> ContextInputManifestProjectionReferenceFact
  -> append event identity
  -> committed validation result fingerprint
  -> committed resulting frontier fingerprint
```

Exact replay必须按manifest projection reference hydrate并验证完整ContextInputManifest artifact，从nested projection重算整条链。任何一处join缺失都不能报告exact。

Invalid validation新增稳定失败owner：

```text
ContextCompileFailureStage.PROVIDER_INPUT_CAUSAL_VALIDATION

ProviderInputCausalValidationFailureReason =
    USER_AFTER_DESCENDANT
  | TOOL_RESULT_BEFORE_CALL
  | CONTINUATION_BEFORE_TOOL_RESULT
  | DUPLICATE_TRANSCRIPT_MESSAGE
  | PROJECTION_SOURCE_JOIN_MISMATCH
  | FRAME_PLACEMENT_MISMATCH
  | COMPACTION_REWRITE_PROOF_MISMATCH

ContextCompileFailureStage.PROVIDER_INPUT_PHYSICAL_POLICY

ProviderInputPhysicalPolicyFailureReason =
    PROVIDER_TOOL_CALL_FAN_IN_EXCEEDED
  | PROVIDER_INPUT_PROJECTION_UNIT_BOUND_EXCEEDED
  | PROVIDER_INPUT_PROJECTION_BYTE_BOUND_EXCEEDED
  | PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED
  | PROVIDER_INPUT_APPEND_BYTE_BOUND_EXCEEDED
  | PROVIDER_INPUT_PHYSICAL_POLICY_UNSATISFIED
```

失败写入现有`ContextCompiledEvent(status="failed")`与typed input-failure audit；禁止ModelStart，禁止自动排序“修复”。

---

## 6. Pure planner 算法

### 6.1 输入

Planner只接收：

- 即将进入manifest stable candidate的immutable ordered projection与bounded identity；
- committed provider transcript frontier；
- committed provider vector/root；
- compiler-accepted `CompiledProviderSourceFragment`；
- generation compatibility；
- pending accepted continuation join obligations；
- authority horizons与historical bindings。

Pending continuation materializer不是unit producer。禁止planner重新遍历raw transcript、完整`LLMContext.messages`或prepared candidate全集推断顺序。

Pure planner输出`PreparedProviderInputPlanFact`，不输出event candidate。Manifest write FULL后，独立的post-manifest candidate factory只接收该plan、FULL acknowledgement与由同一manifest hydrate验证的projection，构造唯一`PreparedProviderInputAppendCandidateFact`。

### 6.2 Continuation先行join

在frontier/delta规划前：

1. 以authority snapshot high-water读取terminal projection和ACCEPTED disposition；
2. 在ordered projection中按resolved call/reply/terminal/disposition查找唯一unit；
3. 构造`ProviderAcceptedContinuationProjectionJoinFact`；
4. projection尚未fold该unit时返回`not_ready`并由owner重试；
5. 不允许构造provisional transcript unit。

### 6.3 Frontier验证

```text
committed_count = frontier.committed_transcript_unit_count
current = ordered_projection.ordered_units

if committed_count > len(current):
    require explicit rewrite/rebase authority

for index in [0, committed_count):
    compare wire_semantic_fingerprint
    compare causal_semantic_fingerprint

    wire/causal mismatch:
        -> typed prefix conflict or authorized rewrite

    invocation classification/physical refs differ:
        -> retain committed unit; manifest-local attribution only
```

不得用owner/fact/materialization fingerprint或`provider_lane` classification代替continuity比较。

### 6.4 Delta与frame placement

```text
delta = current[committed_count:]

if delta contains a new current_user source:
    require exactly one first new current_user
    append delta_before_user in exact projection order
    append invocation_context_frame
    append current_user_and_delta_after in exact projection order
else:
    append complete delta in exact projection order
    append invocation_context_frame
```

构造frame semantic时只冻结ordered source wire semantics与source-head set；构造placement时冻结preceding/following transcript node identity、insertion policy、最终vector first/last ordinal与range accumulator。Delta内部任意两unit的相对顺序不得改变。

### 6.5 Delta proof与carrier

Planner从projection suffix、continuation joins和frame placement构造唯一`ProviderTranscriptDeltaCommitProofFact`。Retained generation：

```text
carrier_(r+1) = append_carrier(carrier_r, append_units)
```

Fresh generation：

```text
carrier_1 = hydrate_carrier(root_units ++ initial_append_units)
```

两条路径都必须验证最终vector中的transcript subsequence逐项等于ordered projection wire/causal sequence；frame placement必须与vector ordinal一致。

### 6.6 Dispatch前最终检查

在adapter调用前重算：

- system prompt semantic fingerprint；
- tool catalog fingerprint；
- ordered provider wire fingerprint；
- causal projection fingerprint；
- frame placement proof；
- strict predecessor prefix；
- committed validation/frontier join；
- full provider input semantic fingerprint。

任一不匹配时禁止transport read启动，写typed compile/input failure并保留diagnostic artifact。

---

## 7. Generation scope、budget 与 rollover hard cut

### 7.1 Main/subagent continuity scope

Main/subagent generation不再使用run-scoped`context_window_id`作为identity：

```python
class SessionProviderInputContinuityScopeFact(FrozenFactBase):
    schema_version: Literal["session_provider_input_continuity_scope.v1"]
    runtime_session_id: str
    call_lane: Literal["main_agent", "subagent"]
    subagent_id: str | None
    compatibility_cohort_fingerprint: str
    scope_fingerprint: str
```

ContextWindow ID/generation只进入authority attribution、explicit rewrite cause、Inspector timeline和rollout accounting。普通run开始/结束不改变ProviderInput generation。One-shot direct/governance/summarizer继续使用one-shot scope。

### 7.2 合法 rollover 原因

V1只允许typed enum：

```text
SYSTEM_ROOT_SEMANTIC_CHANGED
TOOL_CATALOG_SEMANTIC_CHANGED
PROVIDER_VISIBLE_COMPATIBILITY_CHANGED
AUXILIARY_FRAME_REBASE
EXPLICIT_LONG_HORIZON_REWRITE
CONFIRMED_OFFLINE_AUTHORITY_REPAIR
EXPLICIT_ADMINISTRATIVE_RESET
```

Reason与authority必须编码为以下discriminated union：

```python
class ProviderInputRolloverReason(StrEnum):
    SYSTEM_ROOT_SEMANTIC_CHANGED = "system_root_semantic_changed"
    TOOL_CATALOG_SEMANTIC_CHANGED = "tool_catalog_semantic_changed"
    PROVIDER_VISIBLE_COMPATIBILITY_CHANGED = (
        "provider_visible_compatibility_changed"
    )
    AUXILIARY_FRAME_REBASE = "auxiliary_frame_rebase"
    EXPLICIT_LONG_HORIZON_REWRITE = "explicit_long_horizon_rewrite"
    CONFIRMED_OFFLINE_AUTHORITY_REPAIR = (
        "confirmed_offline_authority_repair"
    )
    EXPLICIT_ADMINISTRATIVE_RESET = "explicit_administrative_reset"


class ProviderSystemRootChangeAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_system_root_change_authority.v1"]
    authority_kind: Literal["system_root_change"]
    predecessor_generation_id: str
    predecessor_core_state_fingerprint: str
    ordered_projection_identity_fingerprint: str
    previous_system_root_semantic_fingerprint: str
    resulting_system_root_semantic_fingerprint: str
    authority_fingerprint: str


class ProviderToolCatalogChangeAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_tool_catalog_change_authority.v1"]
    authority_kind: Literal["tool_catalog_change"]
    predecessor_generation_id: str
    predecessor_core_state_fingerprint: str
    ordered_projection_identity_fingerprint: str
    previous_tool_catalog_semantic_fingerprint: str
    resulting_tool_catalog_semantic_fingerprint: str
    authority_fingerprint: str


class ProviderCompatibilityChangeAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_compatibility_change_authority.v1"]
    authority_kind: Literal["provider_compatibility_change"]
    predecessor_generation_id: str
    predecessor_core_state_fingerprint: str
    ordered_projection_identity_fingerprint: str
    previous_provider_visible_compatibility_fingerprint: str
    resulting_provider_visible_compatibility_fingerprint: str
    resolved_model_call_id: str
    authority_fingerprint: str


class ProviderAuxiliaryFrameRebaseAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_auxiliary_frame_rebase_authority.v1"]
    authority_kind: Literal["auxiliary_frame_rebase"]
    predecessor_generation_id: str
    predecessor_core_state_fingerprint: str
    ordered_projection_identity_fingerprint: str
    dropped_frame_fact_fingerprints: tuple[str, ...]
    dropped_unit_range_fingerprints: tuple[str, ...]
    dropped_unit_accumulator: str
    previous_source_head_set_fingerprint: str
    resulting_source_head_set_fingerprint: str
    previous_transcript_projection_semantic_fingerprint: str
    resulting_transcript_projection_semantic_fingerprint: str
    budget_decision_fingerprint: str
    rebase_contract_fingerprint: str
    authority_fingerprint: str


class ProviderLongHorizonRewriteRolloverAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_long_horizon_rewrite_rollover_authority.v1"]
    authority_kind: Literal["long_horizon_rewrite"]
    predecessor_generation_id: str
    predecessor_core_state_fingerprint: str
    ordered_projection_identity_fingerprint: str
    rewrite_authority_reference: ProviderCompactionRewriteAuthorityReferenceFact
    resulting_transcript_projection_semantic_fingerprint: str
    authority_fingerprint: str


class ProviderOfflineRepairRolloverAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_offline_repair_rollover_authority.v1"]
    authority_kind: Literal["offline_repair"]
    predecessor_generation_id: str
    predecessor_core_state_fingerprint: str
    offline_repair_committed_event_reference: ContextEventReferenceFact
    offline_repair_artifact_reference: ContextArtifactReferenceFact
    repaired_generation_core_fingerprint: str
    repair_contract_fingerprint: str
    authority_fingerprint: str


ProviderAdministrativeResetReasonCode = Literal[
    "operator_requested",
    "database_epoch_reset",
    "test_fixture_reset",
]


class ProviderAdministrativeResetAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_administrative_reset_authority.v1"]
    authority_kind: Literal["administrative_reset"]
    predecessor_generation_id: str
    predecessor_core_state_fingerprint: str
    administrative_reset_event_reference: ContextEventReferenceFact
    reset_epoch: PositiveInt
    stable_reason_code: ProviderAdministrativeResetReasonCode
    reset_contract_fingerprint: str
    authority_fingerprint: str


ProviderInputRolloverAuthorityFact = Annotated[
    ProviderSystemRootChangeAuthorityFact
    | ProviderToolCatalogChangeAuthorityFact
    | ProviderCompatibilityChangeAuthorityFact
    | ProviderAuxiliaryFrameRebaseAuthorityFact
    | ProviderLongHorizonRewriteRolloverAuthorityFact
    | ProviderOfflineRepairRolloverAuthorityFact
    | ProviderAdministrativeResetAuthorityFact,
    Field(discriminator="authority_kind"),
]


class ProviderInputRolloverIntentFact(FrozenFactBase):
    schema_version: Literal["provider_input_rollover_intent.v1"]
    continuity_scope_fingerprint: str
    predecessor_generation_id: str
    reason: ProviderInputRolloverReason
    authority: ProviderInputRolloverAuthorityFact
    authority_fingerprint: str
    intent_fingerprint: str


class ProviderInputRolloverRequestFact(FrozenFactBase):
    schema_version: Literal["provider_input_rollover_request.v1"]
    rollover_request_id: str
    intent: ProviderInputRolloverIntentFact
    manifest_projection_reference: ContextInputManifestProjectionReferenceFact
    request_fingerprint: str
```

Reason-authority矩阵必须严格一一对应：

| Reason | 唯一authority branch |
|---|---|
| `SYSTEM_ROOT_SEMANTIC_CHANGED` | `system_root_change` |
| `TOOL_CATALOG_SEMANTIC_CHANGED` | `tool_catalog_change` |
| `PROVIDER_VISIBLE_COMPATIBILITY_CHANGED` | `provider_compatibility_change` |
| `AUXILIARY_FRAME_REBASE` | `auxiliary_frame_rebase` |
| `EXPLICIT_LONG_HORIZON_REWRITE` | `long_horizon_rewrite` |
| `CONFIRMED_OFFLINE_AUTHORITY_REPAIR` | `offline_repair` |
| `EXPLICIT_ADMINISTRATIVE_RESET` | `administrative_reset` |

所有authority必须绑定同一predecessor generation/core与ordered projection identity，因此可作为generation-neutral intent内嵌manifest。Manifest FULL后构造的request必须绑定同一nested projection identity的`ContextInputManifestProjectionReferenceFact`。Matrix外组合、old/new semantic相等、空dropped ranges、rewrite ref冲突或未确认repair/reset event均在candidate factory阶段拒绝。

分支validator还必须冻结：

- system/tool/compatibility branch的previous与resulting fingerprint必须分别等于predecessor core与current FULL manifest中的exact component；
- auxiliary rebase的frame/range tuples必须非空、sorted、unique，dropped accumulator从predecessor vector重算，previous/resulting transcript projection semantic必须相等；
- long-horizon branch的resulting projection必须等于rewrite authority的resulting stable transcript在本次rendering contract下的唯一projection；
- offline repair event type、artifact hash、repair contract与repaired core必须逐项与historical binding一致；
- administrative reset event type、epoch与reason registry必须一致，epoch必须大于predecessor database/generation epoch；
- intent外层`authority_fingerprint`必须等于nested authority自身fingerprint；request的manifest nested projection identity必须等于intent authority中的identity。

Manifest候选字节仅包含`ProviderInputRolloverIntentFact`，不反向引用manifest。`rollover_request_id`由intent fingerprint与FULL manifest projection reference确定性派生；`ProviderInputRolloverResolvedEvent`的event ID再由request fingerprint确定性派生。Generation coordinator在manifest FULL前只准备intent/plan，FULL后才准备stable request和companion candidates；LLMRuntime继续拥有old close + rollover resolved + new start + initial append + ModelStart的唯一原子writer。

NONE保留同一stable request重试；FULL fold唯一new generation；PARTIAL/UNKNOWN与cancel-after-physical-start保留session-owned preparation/reconciliation owner和dispatch barrier。Restart按确定性event ID确认原attempt，不能换reason或authority重建candidate。

`CONFIRMED_OFFLINE_AUTHORITY_REPAIR`不是live exception fallback。Repair event与artifact必须由offline doctor/migration在dispatch barrier外先行FULL提交并通过historical binding；live planner只消费exact reference。Ledger/prefix conflict但没有该authority时必须latch，禁止rollover。

`RETAINED_PREFIX_BUDGET_UNREACHABLE`从rollover reason删除。预算不足只是pressure observation，不自动授予删除任何provider unit的authority。

以下不是合法rollover原因：普通RunStart/RunEnd、新window ID、invocation classification变化、attribution补全、artifact/checkpoint/compile变化或cache miss。

### 7.3 Unified causal/physical policy

```python
class ResolvedProviderInputCausalAndPhysicalPolicyFact(FrozenFactBase):
    schema_version: Literal["resolved_provider_input_causal_physical_policy.v1"]
    max_parallel_tool_calls_per_model_call: PositiveInt
    max_non_tool_transcript_units_per_operation: PositiveInt
    max_visible_causal_predecessors_per_unit: PositiveInt
    max_projection_units_per_manifest: PositiveInt
    max_projection_canonical_bytes_per_manifest: PositiveInt
    max_generation_root_units: PositiveInt
    max_initial_generation_units: PositiveInt
    max_transcript_delta_units_per_append: PositiveInt
    max_context_frame_units_per_append: PositiveInt
    max_append_units: Literal[512]
    max_append_candidate_canonical_bytes: PositiveInt
    allow_multi_append_before_model_start: Literal[False]
    provider_input_vector_contract_fingerprint: str
    terminal_projection_contract_fingerprint: str
    context_manifest_physical_policy_fingerprint: str
    policy_fingerprint: str
```

Composition root必须从resolved model input budget、terminal projection contract、tool execution policy、ContextSource physical policy、manifest policy和`provider-input-persistent-vector-contract:v2`构造该fact。Adapter、terminal projection reducer、ordered projection factory、append planner与config doctor必须精确rebind同一个policy fingerprint。

唯一算术约束为：

```text
max_visible_causal_predecessors_per_unit
    >= max_parallel_tool_calls_per_model_call + 2

max_transcript_delta_units_per_append
    >= max_parallel_tool_calls_per_model_call
       + max_non_tool_transcript_units_per_operation

max_transcript_delta_units_per_append
    + max_context_frame_units_per_append
    <= max_append_units
    == 512

max_initial_generation_units
    >= max_generation_root_units
       + max_projection_units_per_manifest
       + max_context_frame_units_per_append
```

`max_projection_units_per_manifest`与canonical bytes上限必须由resolved model最大合法输入、canonical encoding expansion和manifest physical policy共同派生；doctor用maximal typed projection实际序列化验证，不允许用token与bytes直接比较。

Provider单次返回超过`max_parallel_tool_calls_per_model_call`时，在completed terminal projection进入control disposition resolution前以typed transport/contract failure终结，不得产生ACCEPTED disposition，也不得执行截断后的部分tool calls。Runtime append必须在构造manifest stable candidate前验证delta、frame、total units与canonical bytes。

V1不支持“多个append event后再提交一个ModelStart”。恰好512 units合法；513 units产生typed `PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED` input failure，不写append、不写ModelStart，也不自动rollover。Config doctor若不能证明一个最大合法operation及其frame落在512内，则composition无效，不能启动Host。

Initial generation与合法rollover使用initial-vector materialization bound，不误套ordinary 512-unit delta bound，但仍必须满足projection/manifest、tree height、canonical bytes与resolved model budget。

### 7.4 三类预算结果

#### A. Auxiliary frame膨胀

若超预算完全来自superseded ContextSource/clock/lifecycle frames，可构造`ProviderAuxiliaryFrameRebaseAuthorityFact`，必须证明：

- canonical transcript projection逐项完全不变；
- 被删除unit全部属于auxiliary frame；
- 每个被删除revision已有同source instance的later committed head；
- new root/frame只保留confirmed latest heads；
- dropped range accumulator、old/new source-head set和budget measurement可重算。

随后使用`AUXILIARY_FRAME_REBASE` rollover。

#### B. Transcript本身过大

必须先进入Long-Horizon compaction，取得confirmed rewrite member/source authority，再使用`EXPLICIT_LONG_HORIZON_REWRITE`。没有rewrite authority时不得rollover删除transcript。

#### C. System/tools固定开销不可容纳

若generation root + tools + required safety margin已经超过resolved target budget，结果是typed `target_input_infeasible`，fail closed。Rollover不会改变固定开销，禁止无意义重试。

### 7.5 删除字符串reason映射

`ProviderInputRolloverRequired`必须携带完整、已验证的`ProviderInputRolloverRequestFact`，不接受裸reason或独立authority。禁止根据exception message推断durable reason。

### 7.6 原子rollover

只有合法authority可以触发：

```text
old generation close
+ rollover resolved（typed authority）
+ new generation start
+ initial append/proofs
+ ModelStart
```

新generation必须通过ordered projection、causal validator、frame placement和budget validator。Compaction不得按当前lane重排完整历史；auxiliary rebase不得改变canonical transcript。

---

## 8. Recovery note、system-role message 与 lifecycle facts

### 8.1 Recovery note属于canonical transcript时

若Pulsara recovery note经durable reducer进入canonical transcript：

- 它必须使用`LifecycleNoteSemanticSourceFact`冻结note/cause semantics，并使用`LifecycleNoteSourceAttributionFact`绑定唯一note event与cause event；
- canonical transcript reducer验证note event晚于cause event，并将它放在消费该恢复状态的新user turn之前；
- provider planner只验证该note的projection-local position与相邻visible messages；
- 同一generation中一旦提交不得消失或移动；
- role为`system`不赋予重新插入历史任意位置的权力。

### 8.2 Audit-only note

若note只用于UI/audit，不进入canonical provider transcript，则任何ProviderInput generation都不得包含它。禁止“本次调用临时加入、下次调用删除”。

### 8.3 Runtime context与人类trajectory

Inspector必须分开展示：

```text
canonical conversation transcript
provider invocation context frames
system/root instructions
```

不得把ContextSource frame伪装成用户真实发言。Inspector同时提供最终线性provider payload视图，用于验证模型实际看到的顺序。

---

## 9. Failure 与 recovery matrix

| 情况 | 处理 |
|---|---|
| provider semantic prefix完全相同 | ordinary append/no-op |
| 只有invocation classification/attribution变化 | 不创建append/preparation；只记录manifest-local attribution，不rollover |
| pending continuation尚未出现在ordered projection | `not_ready`，原owner有界重试 |
| pending continuation唯一exact join | materializer只hydrate matched unit，进入delta proof |
| pending continuation缺失且projection high-water已越过carrier | `contract_mismatch` |
| pending continuation重复或semantic/ref冲突 | `authority_untrusted`/latch |
| source entry同时匹配多个branch或没有branch | ModelStart前`PROJECTION_SOURCE_JOIN_MISMATCH` |
| tool result缺result leaf/pair leaf/terminal三方join | `authority_untrusted`/latch |
| manifest projection write NONE/confirmed absent | 原stable manifest candidate重试或typed compile failure，不写ContextCompiled compiled |
| manifest projection write UNKNOWN/cancelled in flight | 保留manifest owner并drain/confirm，禁止append与ModelStart |
| durable manifest projection reference存在但artifact缺失/冲突 | `authority_untrusted`/latch，不live rebuild |
| committed prefix provider semantic变化，无rewrite authority | fail closed，`provider_input_prefix_semantic_conflict` |
| transcript frontier后退，有confirmed compaction authority | typed true rollover |
| transcript frontier后退，无compaction authority | authority untrusted/latch |
| current user出现在descendant之后 | ModelStart前拒绝，`provider_input_user_after_descendant` |
| tool result早于tool call | ModelStart前拒绝 |
| compiled projection与planner顺序不同 | ModelStart前拒绝 |
| run boundary但compatibility不变 | retained generation继续append |
| system/tool/provider framing变化 | typed rollover |
| 仅superseded auxiliary frames导致超预算 | exact frame-rebase proof后typed rollover |
| transcript导致超预算 | 先取得Long-Horizon rewrite authority；否则不rollover |
| system/tools固定开销不可容纳 | `target_input_infeasible`，fail closed |
| ordinary append恰好512 units | 允许，并按resolved policy写一个append |
| ordinary append为513 units | `PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED`，不拆批、不rollover、不ModelStart |
| prefix/ledger conflict但无confirmed offline repair authority | latch，禁止使用repair rollover |
| rollover commit NONE | 保留stable candidate重试同字节 |
| rollover PARTIAL/UNKNOWN | 保留owner与barrier，latch/reconciliation |
| restart发现旧非法generation schema | DB reset/migration required，不live修补 |

---

## 10. 实施阶段

### CO0：Additive DTO、证据fixture与contract冻结

- 将long PR4 exact provider carrier序列保存为sanitized deterministic fixture；
- 保存Call 2/3与Call 5/6的ordered semantic fingerprints和角色序列；
- additive增加wire/causal/attribution三层DTO、source union、projection identity/manifest reference、frame proof、continuation join、delta proof与validation result；
- 冻结source-selection contract、rollover reason-authority union与resolved causal/physical policy；
- 冻结唯一transcript lowering order；
- 冻结ContextFrame insertion policy；
- 为当前bug增加必失败测试，证明修复前会出现user-after-descendant。

CO0完成标准：测试能稳定复现问题，且不会依赖真实DeepSeek或当前数据库。

### CO1：Compiler nested projection与只读shadow validation

- 从`PreparedTranscriptProviderProjectionFact`生成typed ordered transcript units；
- source union、projection position与wire semantic逐项join；
- `ContextInputManifest`内嵌完整projection与bounded identity；只读shadow沿用现有manifest write owner；
- shadow验证manifest FULL/NONE/UNKNOWN/cancel-after-FULL与retention/GC链；
- shadow执行causal validation、frame proposal和continuation exact join；
- production仍走旧planner，不写rejection、不改变rollover或dispatch。

CO1完成标准：shadow能准确报告旧carrier与新projection的首个差异，所有旧测试保持行为不变。

### CO2：Planner/frontier/event/recovery垂直hard cut

- 删除`transcript_before_trigger/current_trigger_units`分组；
- 实现strict suffix与ContextFrame insertion算法；
- continuity guard改为wire + causal semantic，不使用classification/owner/materialization fingerprint；
- pending continuation只做projection join/hydration；
- append candidate/event/core/ModelStart reference接通projection、validation、frame和delta proof；
- planner改为输出generation-neutral plan，post-manifest factory仅在FULL后构造stable append/rollover candidate；
- enforcement切换同一`ResolvedProviderInputCausalAndPhysicalPolicyFact`，512/513边界与tool-call cap同时生效；
- live、NONE/UNKNOWN、restart recovery同时切换新frontier；
- retained carrier只通过append扩展；
- 增加single-placement current-user guard。

CO2是不可拆分的生产迁移。完成标准：同generation任意相邻调用满足strict prefix；旧planner不能与新event schema并存。

### CO3：Session scope、store/recovery/Inspector原子迁移

- main/subagent改用session continuity scope；
- ContextWindow仅保留attribution与explicit rewrite authority；
- 删除ordinary run/window boundary rollover；
- `ProviderInputRolloverRequired`改为只携带完整`ProviderInputRolloverRequestFact`；
- 删除exception-message reason映射；
- 接通auxiliary-frame rebase、Long-Horizon rewrite与target-infeasible矩阵；
- store、reopen recovery和Inspector在同一PR迁移新scope；
- attribution-only drift不再rollover或创建空append。
- V1不生产unit-attribution supplement event，latest attribution只属于manifest-local audit。

CO3完成标准：无system/tool/compatibility/confirmed rewrite变化的多run trajectory只使用一个open generation。

### CO4：删除旧路径、schema reset与architecture guards

- 删除旧scope、旧frontier、fuzzy continuation insert与二次lane重排；
- architecture guard禁止successor进入unit continuity identity、独立projection artifact writer与泛化live repair rollover；
- provider-input schema升级并reset PostgreSQL测试数据库；
- 同步长期contracts；
- architecture guards对旧helper/import/call shape零匹配；
- invalid causal generation无法报告exact replay。

### CO5：确定性、real LLM与cache验证

- offline long trajectory；
- multi-step tool loop；
- user stop/recovery note；
- runtime/provider failure；
- memory/capability/clock revision；
- explicit Long-Horizon compaction；
- long PR4 real DeepSeek dogfood；
- exact replay 30-second control experiment。

Real provider cache只作观察性验收。语义与因果测试是硬gate，不以cached token波动替代。

---

## 11. 修改文件清单

核心实现：

- `src/pulsara_agent/runtime/context_input/provider_projection.py`
- `src/pulsara_agent/runtime/context_input/compiler.py`
- `src/pulsara_agent/runtime/context_input/manifest.py`
- `src/pulsara_agent/runtime/provider_input/planner.py`
- `src/pulsara_agent/runtime/provider_input/coordinator.py`
- `src/pulsara_agent/runtime/provider_input/store.py`
- `src/pulsara_agent/runtime/provider_input/materialization.py`
- `src/pulsara_agent/runtime/provider_input/vector.py`
- `src/pulsara_agent/primitives/provider_input.py`
- `src/pulsara_agent/primitives/transcript_projection.py`
- `src/pulsara_agent/primitives/governance_evidence.py`
- `src/pulsara_agent/llm/terminal_projection.py`
- `src/pulsara_agent/event/events.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/inspector/service.py`

测试与benchmark：

- `tests/test_provider_input_hard_cut.py`
- `tests/test_provider_input_resident.py`
- `tests/test_provider_input_prefix_benchmark.py`
- `tests/test_real_llm_dogfood_pr4.py`
- `benchmarks/durable-runtime/generators/provider_input_prefix.py`

长期契约同步：

- `contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md`
- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`
- `contracts/LLM_TRANSPORT_CONTRACT.zh.md`
- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`
- `contracts/RECOVERY_CONTRACT.zh.md`
- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`
- `PULSARA_PROMPT_CACHE_CONTRACT.zh.md`
- `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`

---

## 12. 测试矩阵

### 12.1 因果顺序

1. 单user、单assistant回复；
2. user -> assistant tool call -> ToolResult -> assistant continuation；
3. 同一user触发三次tool loop；
4. tool denied/cancelled/interrupted；
5. provider error前已有partial assistant但未ACCEPTED；
6. user stop -> recovery note -> resume user；
7. runtime failure note；
8. subagent handoff/result；
9. run_id=None的compaction summary以rewrite authority替换历史range；
10. direct stable、derived tool result、summary、lifecycle note四种semantic/attribution source pair的strong join；
11. event lifecycle边由transcript reducer验证，visible message/block边由provider validator验证；
12. 每条轨迹验证projection-local causal edge严格递增；
13. compaction range/member accumulator、summary artifact或resulting transcript任一冲突时不得生成projection；
14. direct causal predecessor集合不得包含传递闭包，并受contract hard bound限制；
15. compaction/lifecycle message走direct branch、result leaf缺pair或pair缺result leaf均被拒绝；
16. tool-result result leaf、pair leaf与terminal projection的call ID、ordinal与semantic逐项strong join。

### 12.2 Prefix continuity

1. 同run多model step；
2. 跨run、同session、compatibility不变；
3. memory revision；
4. capability prose revision；
5. clock observation；
6. recovery note；
7. `current_user -> prior_history` classification变化但wire/causal identity相同；
8. attribution从pending变为final但provider message相同；
9. artifact ref变化但content相同；
10. pending continuation先not-ready、后exact projection join；
11. same-H retry；
12. restart restore后继续append；
13. transcript delta为空但ContextSource frame非空的frame-only append；
14. transcript delta与frame同时为空时不创建append/preparation/ModelStart；
15. 从单节点`[A]`追加为`[A, B]`后，A的position、causal与unit semantic fingerprint逐字节不变；
16. 完整projection validator能派生A的successor为B，但该值不进入A的durable DTO。

每一项要求：

```text
previous semantic units == current semantic units[:len(previous)]
```

### 12.3 合法cache break

1. system semantic变化；
2. tool schema/order变化；
3. provider framing/tokenization compatibility变化；
4. exact auxiliary-frame rebase，且transcript逐项不变；
5. confirmed Long-Horizon rewrite；
6. explicit administrative reset。

每次只允许一个typed rollover，且新generation仍通过causal validator。

以下预算case不得产生rollover：

- transcript超预算但尚无Long-Horizon rewrite authority；
- system/tools固定开销不可容纳；
- 仅因token estimate波动或provider cache miss。

### 12.4 Manifest persistence、rollover与physical boundaries

1. projection内嵌manifest stable candidate，FULL后才生成manifest projection reference；
2. manifest NONE重试同字节，UNKNOWN/cancellation保留owner并drain；
3. cancel-after-FULL采用FULL并允许后续append/ModelStart；
4. durable manifest reference的artifact absent/hash conflict/nested identity mismatch均latch；
5. 每个rollover reason只接受matrix中唯一authority branch，cross-pair均拒绝；
6. offline repair无FULL committed event/artifact时不得作为live rollover authority；
7. parallel tool-call cap、causal edge cap、delta cap、frame cap与append cap通过doctor maximal fixture；
8. 512-unit append通过，513-unit append在manifest/ModelStart前typed fail；
9. initial generation使用initial materialization bound，不误用ordinary 512-unit append bound；
10. Host composition无法证明worst-case operation可行时拒绝启动。

### 12.5 负向architecture guard

必须禁止：

- planner中按`provider_lane != "current_user"`分组后再拼接；
- `current_trigger_units`或等价末尾trigger实现；
- `current_user/prior_history/current_run_tail`进入continuity semantic；
- pending continuation按相同fragment模糊匹配、插入或替换；
- rollover reason由exception字符串推断；
- session generation scope要求`context_window_id`；
- provider continuity比较owner/fact/materialization fingerprint；
- final carrier绕过ordered transcript projection；
- successor identity进入unit/position/continuity fingerprint；
- 为ordered projection新建独立artifact writer/owner；
- unit-attribution supplement event生产路径；
- 裸`AUTHORITY_REPAIR_REQUIRED`或无confirmed offline repair reference的live rollover；
- append event缺projection/validation/frame/delta proof join；
- recovery note在下一compile无typed原因消失；
- 测试fixture允许nullable/legacy ProviderInput carrier。

### 12.6 long PR4回归gate

确定性fixture必须满足：

- 固定system/tool时不因run boundary rollover；
- Call 5类轨迹中user始终早于assistant/tool descendants；
- 无真实compaction时rollover count为0；
- dynamic system recovery note只追加一次且不移动；
- Inspector linear payload与adapter dispatch payload一致；
- human-readable trajectory保持因果可读。

真实DeepSeek dogfood记录：

- input/cached tokens per call；
- longest common semantic prefix per adjacent call；
- generation ID/revision；
- typed rollover reason；
- first divergence unit及owner；
- provider latency与cached-token observation。

不冻结绝对cached ratio作为CI gate，但在无rollover的长轨迹中若长期只命中固定640/768 tokens，测试必须输出diagnostic并判定dogfood未通过人工验收。

---

## 13. 数据迁移与兼容性

当前ProviderInput generation schema尚处于hard-cut开发期，不保留错误顺序的历史兼容路径。

- PostgreSQL runtime/event/artifact测试数据库reset；
- 旧ProviderInput generation/start/append/root artifact不replay；
- 不提供“读取旧vector后现场重排”的迁移器；
- Oxigraph canonical memory不因本修复自动重写；完整dogfood前可按测试隔离策略reset；
- historical canonical EventLog若需要保留，必须通过离线migration重新生成新provider projection，不能把旧provider vector作为authority；
- production部署必须在schema fingerprint与database epoch上hard cut。

---

## 14. Definition of Done

全部条件同时满足才可恢复“Incremental ProviderInput 已闭环”的表述：

1. Provider planner只消费compiler冻结的ordered transcript projection；
2. wire、causal/projection、physical attribution三层identity物理拆分，event/artifact refs不得进入causal fingerprint；
3. source-selection contract为每个canonical entry选择唯一branch，tool result完成result leaf/pair leaf/terminal projection三方join；
4. invocation classification不进入continuity semantic；
5. `current_user`在一个generation中只出现一次且永不移动；
6. 所有assistant/tool descendants位于其causal user之后；
7. tool call/result/continuation顺序通过provider-boundary validator；
8. pending continuation只能exact join ordered projection中的唯一unit；
9. compaction summary使用discriminated source与rewrite authority，不伪造run/ordinal；
10. frame semantic/placement分别保存相邻transcript identity、policy、ordinal range与accumulator；
11. successor不进入unit continuity identity，旧末节点追加新节点后fingerprint不变；
12. 完整projection内嵌ContextInputManifest artifact，且manifest FULL前不得发布compiled/append/ModelStart carrier；
13. manifest reference、append candidate/event、core与ModelStart形成完整projection/proof/frontier durable chain；
14. 同generation相邻revision满足strict semantic prefix；
15. attribution-only变化不产生append/preparation/rollover或supplement event；
16. 普通run/context-window boundary不触发rollover；
17. budget pressure按frame rebase、Long-Horizon rewrite、target infeasible三分；
18. rollover request使用reason-authority一对一union，不解析exception字符串，offline repair必须预先durable confirmed；
19. resolved causal/physical policy统一约束tool fan-in、edge、projection、frame、delta与512-unit append；
20. 真正rollover的新generation仍保持canonical因果顺序；
21. ContextSource frame在Inspector中不伪装成真实user trajectory；
22. long PR4 deterministic fixture不再复现Call 5因果倒置；
23. exact replay、restart、late FULL、NONE/UNKNOWN recovery全绿；
24. architecture guard对旧重排、successor identity、独立projection writer和live repair fallback零匹配；
25. hard-cut database epoch/reset与所有historical bindings一致；
26. 全量非real pytest通过；
27. 全量real LLM与所有dogfood开关通过；
28. DeepSeek cache报告与longest-common-prefix证据一致，不再用cached usage掩盖语义错误。

---

## 15. 与后续性能工作的关系

修复因果顺序后，provider cache命中率预计会自然改善，因为ordinary trajectory终于满足：

```text
旧provider input
是
新provider input的严格前缀
```

但本文不把cache收益作为修复成立的前提。即使provider完全不缓存，模型也必须看到稳定、因果正确、可审计的trajectory。

后续Context Evidence Cursor、segment coalescing、adaptive batching和write-behind均不得改变本文冻结的provider-visible order。性能优化只能减少重建、I/O和等待，不能重新解释消息因果关系。

最终原则只有一句：

```text
Canonical conversation order is semantic authority.
ProviderInput may append, frame, or explicitly compact it,
but may never silently reorder it.
```
