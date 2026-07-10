# Pulsara Context Engineering Compiler 设计文档

> 目标：把 Pulsara 的上下文工程从“若干 prompt 字符串拼接 + transcript replay”升级为一个明确的 **context compiler**。
>
> 本文只讨论 provider-agnostic 的上下文编译层；暂不设计 Anthropic / OpenAI / DeepSeek 等 provider-specific cache strategy。

## 0. 核心结论

Pulsara 的 context engineering 不应该只是 `build prompt`，而应该是：

> 在当前 run / turn / step，基于已经存在的 runtime facts，决定哪些事实以何种形式进入模型可见上下文。

这意味着 context engineering 的职责是 **编译**，不是 **创造真值**。

它不拥有：

- memory 的真实性；
- artifact 的完整性；
- capability 是否存在或可调用；
- tool 是否允许执行；
- plan 是否被批准；
- MCP server 是否健康；
- terminal process 是否仍在运行。

这些事实由各自 subsystem 产生和维护。Context compiler 只负责把它们转换成模型本轮可见的上下文，并记录“如何转换”的解释事实。

最终形态应是：

```text
Runtime / Memory / Artifact / Capability / Tool / Plan facts
                         │
                         ▼
                  Context Sources
                         │
                         ▼
                  Context Sections
                         │
                         ▼
          Budget + Degrade + Compile + Provenance
                         │
                         ▼
                Model-visible LLMContext
                         │
                         ▼
       ContextCompiledEvent / Inspect projection
```

## 1. 当前实现状态

当前代码已经有很好的事实层基础，但 context 编译仍比较分散。

关键落点：

- `src/pulsara_agent/runtime/agent.py`
  - `compose_system_prompt(...)` 把 base prompt、runtime context、memory prompt、capability prompt、active skill prompt 拼成一个 system prompt。
  - `render_runtime_context_prompt(...)` 每轮渲染时间、workspace root、workspace kind、terminal cwd 等 runtime 信息。
  - `_stream_model_loop(...)` 在每轮 model call 前调用 memory projection，再调用 `build_llm_context(...)`。
- `src/pulsara_agent/runtime/context.py`
  - `build_llm_context(...)` 是当前真正的模型上下文汇合点。
  - `msg_to_llm_messages(...)` 把 internal `Msg` 转成 `LLMMessage`。
  - tool result / artifact preview 的 aggregate budget 也在这里处理。
- `src/pulsara_agent/capability/runtime.py`
  - `CapabilityRuntime.resolve_for_turn(...)` 已经产生 per-turn `CapabilityExposurePlan`。
- `src/pulsara_agent/capability/exposure.py`
  - `CapabilityExposurePlan` 已经是一个很接近 context section source 的对象：它知道 direct / hidden / callable tools、catalog prompt、active skill prompt、diagnostics。
- `src/pulsara_agent/runtime/compaction/service.py`
  - compaction 已经从 raw event log 触发逐步转向 model-visible context 触发。
- `src/pulsara_agent/memory/recall/projection.py`
  - memory projection 已经有“模型实际看到什么”的安全边界，例如 projection truncation 后只声明 surviving included ids。

当前最大问题不是缺功能，而是缺一个统一对象回答：

> 本轮模型实际看到了哪些上下文？这些上下文来自哪里？哪些被压缩、降级、忽略？为什么？

## 2. 设计边界：Context Compiler 不拥有真值

这是本设计最重要的边界。

Context compiler 的输入只能是已经存在的事实：

- event log 中已发生的 run / turn / reply / tool / plan / compaction events；
- `CapabilityExposurePlan` 已经解析出的 capability fact；
- memory hooks / recall projection 已经产生的 memory fact；
- artifact service 已经保存或引用的 artifact fact；
- terminal supervisor / terminal manager 已经暴露的 process fact；
- HostSession / RuntimeSession 已经维护的 session fact；
- 当前用户输入；
- 当前 permission policy / plan state。

Context compiler 可以决定：

- 是否把某个事实注入模型；
- 放入 system、leading-user、history、tool-result envelope 还是 handoff hint；
- 使用 full、compact、summary、ref-only 或 omitted 哪种表示；
- 在预算紧张时如何降级；
- 如何记录 provenance；
- 如何生成 inspectable diagnostics。

Context compiler 不可以决定：

- 一条 memory 是否应落盘；
- 一个 memory conflict 是否成立；
- 一个 artifact 是否可信；
- 一个 tool 是否实际可执行；
- 一个 capability descriptor 是否存在；
- 一个 terminal command 是否安全；
- 一个 MCP server 是否 authenticated；
- 一个 pending interaction 是否已 resolved。

换句话说：

> Context compiler 是事实到模型输入的投影层，不是第二个 runtime。

## 3. 目标对象模型

### 3.1 ContextSource

`ContextSource` 是从某个 subsystem 提取上下文事实的 provider。

建议接口：

```python
class ContextSource(Protocol):
    source_id: str

    def collect(self, request: ContextCompileRequest) -> ContextSourceOutput:
        ...
```

`ContextSourceOutput` 不直接返回字符串，而返回 section 候选：

```python
@dataclass(frozen=True, slots=True)
class ContextSourceOutput:
    source_id: str
    sections: tuple[ContextSection, ...]
    diagnostics: tuple[ContextDiagnostic, ...] = ()
```

`ContextCompileRequest` 从 PR0 开始必须显式携带当前用户输入，不能只让它混在 `state.messages` / transcript history 里：

```python
@dataclass(frozen=True, slots=True)
class ContextCompileRequest:
    context_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    model_call_index: int
    model_role: ModelRole
    state: LoopState
    current_user_message: Msg
    current_user_input: str
    current_user_anchor: str
    tools: tuple[ToolSpec, ...]
    exposure: CapabilityExposurePlan
    budget: LoopBudget
```

`model_call_index` 的所有权必须归 `AgentRuntime`，不是 compiler。`AgentRuntime` 在同一 `(run_id, turn_id, reply_id)` 内按真实模型调用顺序生成单调 index，并在每次 compile 前写入 `ContextCompileRequest.model_call_index`。compiler 只能回显这个 index 到 `CompiledContext` / `ContextCompiledEvent`，不能自行重新编号。这样 retry、recompile、inspect join 才不会因为 compiler 内部重试或重复编译而漂移。

`current_user_anchor` 是切分当前 run segment 的稳定锚点，优先使用 `current_user_message.id` 或 runtime 已持有的 turn/run message boundary。不要靠“最后一条 user message”启发式推断；在 resume、pending approval 续跑、多轮 user 历史、以及第二次 model call 已经追加 assistant/tool tail 后，这个启发式都可能切错。若 anchor 在 `state.messages` 中不是 exactly one match，projector 必须 fail-closed：产生 diagnostic 并硬失败，或只使用 `ContextCompileRequest.current_user_message` 注入当前 user、同时禁止从 `state.messages` 推断 `current_run_tail`。不能静默重复注入 current user，也不能静默丢 tail。

当前用户输入是本轮任务本身，必须作为独立 `current_user` section 编译：

- `budget_class="must_keep"`；
- `render_mode="full"`；
- 不参与普通 history 降级；
- 不允许静默裁剪；
- 如果自身超过输入预算，应产生明确错误或请求用户拆分。

但 `current_user` 不是“永远放在 messages 最后一条”的意思。Pulsara 当前一个 user turn / run 内可能包含多次 model call：用户输入之后，assistant 先发 tool call，tool result 被追加到 `state.messages`，随后第二次 model call 继续推理。因此 compiler 必须显式区分：

```text
prior_history      = 本 run 之前已经存在的 transcript
current_user       = 触发当前 run 的真实用户输入，must_keep
current_run_tail   = 当前 run 内已经发生的 assistant/tool messages
```

第二次及之后的 model call lowering 时，`current_user` 的时间位置是“当前 run segment 的起点”，它必须出现在 `current_run_tail` 之前，而不是被抽出来放到所有 tool result 之后。否则会把时间线改成 `assistant tool_call -> tool_result -> current_user`，还可能破坏 provider 对 assistant tool call / tool result pairing 的约束。

实现上，PR1 可以先用 `ContextCompileRequest.current_user_anchor` 在 `state.messages` 中定位触发当前 run 的 user message，并把它之后的 assistant/tool 片段归入 `current_run_tail`。但 `ContextCompileRequest` 的字段从 PR0 就必须存在。这样后续预算/降级不会把“本轮用户要求”和旧 user history 混在一起。

第一批 source 可以从现有逻辑平移：

| Source | 当前事实来源 | 说明 |
| --- | --- | --- |
| `runtime_context` | `render_runtime_context_prompt(...)` | date、timezone、workspace root、workspace kind、terminal cwd。 |
| `capability_exposure` | `CapabilityExposurePlan` | tool specs、catalog prompt、active skill prompt、diagnostics。 |
| `memory_projection` | `state.memory_projection` / `memory_hooks.memory_context_prompt()` | 自动召回注入。 |
| `plan_workflow` | `PlanWorkflowState` / pending plan interaction | plan mode 规则与 pending workflow。 |
| `transcript` | `state.messages` | 历史 user / assistant / tool messages。 |
| `current_user` | `ContextCompileRequest.current_user_message` | 本轮用户输入，独立 must_keep。 |
| `current_run_tail` | `state.messages` 中当前 user 之后的 assistant/tool 片段 | 同一 run 内后续 model call 的工具调用与工具结果，保持时间顺序。 |
| `recovery` | `project_recovery_from_state(...)` | stop / recovery / suspended run hints。 |
| `artifact_refs` | `ToolResultBlock.artifacts` | tool result preview 与 artifact read-more。 |

后续再加入：

- `workspace_snapshot`
- `git_status`
- `terminal_processes`
- `mcp_status`
- `subagent_tasks`
- `goal`

### 3.2 ContextSection

`ContextSection` 是 context compiler 的最小编译单元。

建议字段：

```python
@dataclass(frozen=True, slots=True)
class ContextSection:
    id: str
    source_id: str
    channel: ContextChannel
    priority: int
    stability: ContextStability
    budget_class: ContextBudgetClass
    renderers: ContextSectionRenderers
    provenance: dict[str, object]
    metadata: dict[str, object] = field(default_factory=dict)
```

枚举建议：

```python
ContextChannel = Literal[
    "system",
    "leading_user",
    "history",
    "current_user",
    "current_run_tail",
    "tool_context",
    "handoff_hint",
]

ContextStability = Literal[
    "stable",
    "turn",
    "step",
    "ephemeral",
]

ContextBudgetClass = Literal[
    "must_keep",
    "important",
    "optional",
    "debug",
]

ContextRenderMode = Literal[
    "full",
    "compact",
    "summary",
    "ref_only",
    "omitted",
]
```

其中 `current_user` 是 special channel：它参与 message lowering，但不参与普通 history 降级；`current_run_tail` 也是 special channel：它表示当前 run 内已经发生的 assistant/tool tail，必须保持原始 tool-call/tool-result pairing，不允许被 synthetic user section 插入打断。

其中 `budget_class` 比 provider cache 更重要。Pulsara 需要先解决“预算紧张时保谁、压谁、丢谁”，再考虑 provider cache。

### 3.3 Section lifecycle policy

PR0–PR5 可以先把 `ContextStability` 作为 section report 字段保留下来；PR6 需要把它升级成真正的 collect / reuse / invalidate 策略。

建议语义：

| Stability | Collect 时机 | Reuse 范围 | Invalidate 条件 | 典型 section |
| --- | --- | --- | --- | --- |
| `stable` | session open 或配置变化时收集；model call 前可复用 | 同一 runtime session，跨 run / turn 复用 | workspace root / workspace kind / permission profile / capability provider snapshot / active skill CLI args 改变 | base runtime context、静态 capability catalog skeleton、workspace identity |
| `turn` | 每个 user turn / run 开始时收集 | 同一 user turn 内多次 model call 复用 | 新 user input、plan mode 状态变化、explicit skill activation 变化、memory auto recall 重新投影、compaction boundary 改变当前 transcript projection | current user、per-turn capability exposure、memory projection、plan workflow instruction |
| `step` | 每次 model call 前收集或刷新 | 只复用到下一次 tool execution / model call boundary | tool result 追加、artifact 新增、terminal process 状态变化、pending interaction resolved、permission decision 变化、transcript rewrite / compaction rewrite / message projection fingerprint 改变 | current_run_tail、tool result envelope、artifact refs、terminal process hints |
| `ephemeral` | 单次 model call 即时生成 | 不复用 | model call 结束立即失效 | retry hint、temporary warning、one-shot recovery note |

PR6 不应该引入 provider-specific cache；这里的 lifecycle 是 provider-agnostic 的 runtime scheduling。它回答的是：某个 section 何时重新 collect、何时可以复用、何时必须失效，而不是告诉 Anthropic/OpenAI 如何 cache。

PR6 建议新增 `ContextSectionCache` 或 `ContextLifecycleCoordinator`：

- key 至少包含 `runtime_session_id`、`source_id`、`section id`、`stability`、`scope_key`、`dependency_fingerprint`；
- value 保存已 collect 的 `ContextSection`、diagnostics、created_at、last_used_model_call_index；
- `scope_key` 必须由 stability 显式决定，不能只靠 invalidate 规则兜底：
  - `stable`: `runtime_session_id`；
  - `turn`: `run_id/turn_id/current_user_anchor`；
  - `step`: `run_id/turn_id/reply_id/model_call_index` 或 runtime step generation；
  - `ephemeral`: `context_id`，且默认不写入 cache；
- `dependency_fingerprint` 来自各 source 明确声明的事实版本，例如 capability exposure hash、plan_state version、memory projection id、artifact ids、terminal process generation、`transcript_revision`、`compaction_boundary_id`、`message_projection_fingerprint`；
- compiler 每次 compile 先向 lifecycle coordinator 请求 section candidates，再执行 budget/degrade/lowering；
- reuse 只能复用 source output，不能复用最终 render mode，因为 render mode 依赖当前预算；
- cache 写入和读取必须使用 immutable value object 或 deep copy。`ContextSection.metadata` / `provenance` 等 dict 不能被后续 source/runtime mutation 改写已缓存 section。

compaction 是 lifecycle 的一等 invalidation 源。preflight compact、mid-turn compact、manual compact 都可能重写 replay 后的 `state.messages` / transcript projection；因此 history、current_user anchor lookup、current_run_tail、artifact/tool envelope section 都必须把 `transcript_revision` / `compaction_boundary_id` / `message_projection_fingerprint` 纳入 dependency fingerprint。compaction rewrite 后，相关 cached entries 必须 invalidated，不能用旧 transcript section 拼新 context。

transcript rewrite 还必须保护当前 run segment 的锚点。preflight / mid-turn compaction 如果重写 `state.messages`，必须 preserve 当前 user message identity，使原 `current_user_anchor` 仍能定位；若无法 preserve，rewrite 结果必须显式返回新的 current user anchor，并由 `AgentRuntime` 更新下一次 `ContextCompileRequest.current_user_anchor`。否则 PR1 的 fail-closed split 规则会在 compact 后无法定位 current user，并被迫禁用 `current_run_tail`。

生命周期策略的核心禁忌：不要用“字符串内容相同”判断 section 可复用；必须用 subsystem fact 的稳定 id / version / fingerprint。否则 memory、capability、permission、artifact 等事实变化会被旧 prompt 文本遮住。

### 3.4 render mode 降级

每个 section 应该提供多级表示。

例如 artifact section：

| Mode | 表示 |
| --- | --- |
| `full` | 完整 tool result preview + artifact refs。 |
| `compact` | head/tail preview + artifact refs。 |
| `summary` | 简短说明 + artifact ids。 |
| `ref_only` | 只给 artifact id 和 `artifact_read` 使用提示。 |
| `omitted` | 不进模型，只进 diagnostics。 |

memory section：

| Mode | 表示 |
| --- | --- |
| `full` | statement、why、conflicts、paths、scope/status。 |
| `compact` | memory id + statement + key warning。 |
| `summary` | 主题级摘要。 |
| `ref_only` | 提示可用 `memory_search`。 |
| `omitted` | 不注入。 |

capability section：

| Mode | 表示 |
| --- | --- |
| `full` | 完整 skill catalog / active skill body。 |
| `compact` | index-first catalog。 |
| `summary` | 只列 skill names / tool categories。 |
| `ref_only` | 提示可用 read_file 读取 SKILL.md。 |
| `omitted` | 仅保留 tool schema，不给 catalog prose。 |

### 3.5 CompiledContext

编译结果不应只是 `LLMContext`，还应包含 report。

```python
@dataclass(frozen=True, slots=True)
class CompiledContext:
    context_id: str
    llm_context: LLMContext
    sections: tuple[CompiledContextSection, ...]
    tool_specs: tuple[CompiledToolSpecUnit, ...]
    diagnostics: tuple[ContextDiagnostic, ...]
    lifecycle_decisions: tuple[ContextLifecycleDecisionDiagnostic, ...]
    estimated_tokens: int
    budget: ContextBudgetReport
```

`diagnostics` 与 `lifecycle_decisions` 必须分开：

- `diagnostics` 表示编译过程中的 warning / error / degrade / omission；
- `lifecycle_decisions` 表示 lifecycle coordinator 对 cache entry 的结构化决策，尤其是旧 entry 被 invalidated 的原因。

不要把 lifecycle decision 塞进 `ContextDiagnostic(kind="lifecycle_decision")` 作为主路径。inspect 应能直接读取 `CompiledContext.lifecycle_decisions`。

`CompiledContextSection` 记录：

```python
@dataclass(frozen=True, slots=True)
class CompiledContextSection:
    id: str
    source_id: str
    channel: ContextChannel
    render_mode: ContextRenderMode
    included: bool
    estimated_tokens: int
    lifecycle_status: ContextLifecycleStatus | None
    lifecycle_reason: str | None
    dependency_fingerprint: str | None
    cache_key_scope: str | None
    provenance: dict[str, object]
    metadata: dict[str, object]
```

`ContextLifecycleStatus` 建议为：

```python
ContextLifecycleStatus = Literal[
    "freshly_collected",
    "reused",
    "not_cacheable",
]
```

PR6 之前这些字段可以为 `None`；PR6 之后，所有进入最终 `CompiledContextSection` 报告的 section 都必须填充。最终 section 的 `lifecycle_status` 只能表示“本次编译采用的 section 从哪里来”：`freshly_collected`、`reused` 或 `not_cacheable`。

`invalidated` 不应作为最终 section 状态。被 invalidated 的通常是旧 cache entry，而不是本次最终采用的 section；它应记录为 lifecycle decision diagnostic，例如：

```python
@dataclass(frozen=True, slots=True)
class ContextLifecycleDecisionDiagnostic:
    source_id: str
    section_id: str
    old_cache_key_scope: str
    old_dependency_fingerprint: str
    new_dependency_fingerprint: str
    decision: Literal["invalidated"]
    reason: str
```

这样 inspect 不会出现 `included=true` 但 `lifecycle_status="invalidated"` 的歧义；它可以同时显示“旧 entry 因 compaction boundary 改变被 invalidated”和“新 section freshly_collected”。

`LLMContext.tools` 不是可渲染 section，但它是模型输入成本的一部分，必须作为独立 compiled unit 计费：

```python
@dataclass(frozen=True, slots=True)
class CompiledToolSpecUnit:
    name: str
    descriptor_id: str | None
    schema_chars: int
    estimated_tokens: int
    included: bool
    metadata: dict[str, object]
```

`CompiledContext.estimated_tokens` 必须包含：

```text
rendered_sections_estimated_tokens
+ tools_estimated_tokens
+ message_envelope_estimated_tokens
```

否则 MCP / skill / future provider tools 的大型 schema 会被 auto compact 和 inspect 低估。

注意：默认不需要把完整 prompt 文本落库。完整 prompt 可能过大，也可能含有敏感路径或工具输出。应优先记录 section-level fact 和必要 ids。

## 4. 预算模型

### 4.1 Budget 是 compiler 的核心职责

Pulsara 不应该只靠 `tool_result_context_chars` 局部限制，也不应该只在 compaction 时估算。

Context compiler 应该在每次 model call 前形成统一预算：

```text
context_window_tokens
- reserved_output_tokens
- safety_margin_tokens
= input_budget_tokens
```

第一版仍可使用估算 token，不引入 provider tokenizer：

- 普通自然语言：`chars / 4`
- JSON / event-shaped text：`chars / 2`
- tool schema / JSON Schema / MCP descriptor：`chars / 2`
- 统一 safety margin：20% - 30%

后续可以加入 usage anchor：

```text
last_reported_input_tokens + newly_added_estimated_tokens
```

`ContextBudgetReport` 至少应拆开：

```python
@dataclass(frozen=True, slots=True)
class ContextBudgetReport:
    context_window_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    input_budget_tokens: int
    sections_estimated_tokens: int
    tools_estimated_tokens: int
    envelope_estimated_tokens: int
    total_estimated_tokens: int
```

第一版即使不做复杂降级，也必须把 `tools_estimated_tokens` 记入总数，并在 `ContextCompiledEvent` 中可见。

### 4.2 Budget class 降级顺序

建议默认顺序：

1. `must_keep`
   - 不裁剪，除非硬失败。
   - system/developer/base runtime safety、current user input、plan mode safety、permission safety、contradiction warning。
2. `important`
   - 优先保留；预算紧张时从 full 降到 compact / summary / ref_only。
   - active skill、memory projection、recent tool results、artifact refs。
3. `optional`
   - 可摘要、可 ref-only、可省略。
   - older assistant text、diagnostic prose、low-confidence memory hints。
4. `debug`
   - 默认不进入模型，进入 event / inspect。
   - capability diagnostics、budget explanations、source warnings。

### 4.3 Current user input 永远是 must_keep

当前用户输入不是 section 降级对象。它是本轮任务本身。

如果当前用户输入本身超过预算，应产生明确错误或要求用户拆分，而不是静默裁剪。

## 5. Channel 设计

第一版不需要 provider-specific prompt cache，但需要整理 channel。

建议：

### 5.1 `system`

放行为约束和稳定身份：

- base system prompt；
- permission / plan hard boundary；
- tool/capability safety rules；
- active skill 中真正的 binding instruction。

### 5.2 `leading_user`

放环境、状态、参考事实：

- runtime context；
- workspace root / kind / terminal cwd；
- memory projection；
- skill catalog；
- artifact hints；
- git/workspace snapshot；
- recovery hint。

### 5.3 `history`

由 transcript projector 生成：

- user messages；
- assistant messages；
- tool call / tool result pairs；
- compact summary boundary；
- artifact envelopes。

### 5.4 `tool_context`

用于特殊的 tool result rendering，比如 artifact envelope、terminal process recall hint。

第一版可以仍然输出为 `LLMMessage.tool_result(...)`，但内部应作为 section 编译和计费。

### 5.5 `handoff_hint`

用于 compaction / resume 后的特殊提示：

- 上一次 compact summary；
- 长程 terminal_process 可以继续 poll；
- pending task / pending interaction。

### 5.6 Lowering 到当前 `LLMContext`

当前 `LLMContext` 只有三个出口：

```python
LLMContext(
    messages: tuple[LLMMessage, ...],
    tools: tuple[ToolSpec, ...],
    system_prompt: str | None,
)
```

PR1 可以保持这个 provider payload 形状不变。PR4 做 event 关联时，应给 `LLMContext` 增加 runtime-only metadata 字段，例如 `context_id` / `model_call_index`；这些字段只供 runtime event publishing 使用，不能被序列化进 provider 请求正文。

注意：实现时只能在当前 dataclass 末尾追加字段，不能重排已有字段。当前代码里的字段顺序是 `messages, tools, system_prompt`；文档和代码都应保持这个顺序，避免破坏任何位置参数构造或隐含序列化假设。

因此 channel 必须有明确 lowering 规则，避免 PR1 各自猜。

推荐第一版 lowering：

1. `system`
   - 按 `priority` / `order` 排序；
   - 以 `\n\n` 合并为单个 `system_prompt`；
   - 第一版保持与当前 `compose_system_prompt(...)` 语义接近，降低迁移风险。
2. `leading_user`
   - 编译成一条 synthetic `LLMMessage.user(...)`；
   - 插入到 history 之前；
   - 只承载环境、memory projection、skill catalog、artifact hints 等事实性上下文；
   - 不与当前用户输入合并。
3. `history`
   - 由 transcript projector 输出当前 run 之前的历史；
   - 插在 `leading_user` 之后、`handoff_hint` / `current_user` 之前；
   - 不包含触发当前 run 的 user message，也不包含该 user 之后已产生的 assistant/tool tail。
4. `handoff_hint`
   - 编译成 synthetic `LLMMessage.user(...)`；
   - 插在 history 之后、current user 之前；
   - 用于 recovery / compaction summary / long-running terminal_process handoff。
5. `current_user`
   - 插在当前 run segment 的起点；
   - 位于 `prior_history` / `handoff_hint` 之后，`current_run_tail` 之前；
   - 不与 leading_user 或 handoff_hint 合并；
   - 不降级、不静默裁剪。
6. `current_run_tail`
   - 由 transcript projector 输出当前 user message 之后、当前 model call 之前已经发生的 assistant/tool messages；
   - 必须继续保持 provider tool-call pairing：assistant tool call 与对应 tool result 不能被普通文本 section 打断；
   - 第二次及之后的 model call 依赖这个 tail 保持时间线。
   - 这里的 must_keep 指“结构 must_keep”：message 顺序、assistant tool call、tool result placeholder、tool_call_id pairing 必须保留；但 tool result body 不是全量 must_keep，仍可按 `tool_context` / artifact preview / ref-only 策略降级。巨大工具输出不得因为属于 current_run_tail 就绕过预算。
7. `tool_context`
   - 仍 lowering 为 `LLMMessage.tool_result(...)` 或 transcript projector 内部的 tool result envelope；
   - 不能破坏 tool_call_id 对齐；
   - artifact ref-only / compact envelope 只改变 tool result body，不改变 pairing。

因此第一版 message 顺序是：

```text
system_prompt =
  system sections joined by priority/order

messages =
  [leading_user synthetic context message?]
  + prior_history projection
  + [handoff_hint synthetic message?]
  + [current_user message]
  + [current_run_tail projection]

tools =
  direct tool specs
```

如果当前代码路径已经把当前用户输入 append 到 `state.messages`，PR1 的 projector 必须在编译前把它从普通 history 中分离出来，避免重复注入；并且必须把该 user message 之后已经追加的 assistant/tool messages 保留为 `current_run_tail`，不能丢失或移动到 user message 之前。

## 6. ContextCompiledEvent

Context compiler 必须产生可 inspect 的事实。

建议新增 typed event：

```python
class ContextCompiledEvent(EventBase):
    context_id: str
    model_role: str
    model_call_index: int
    estimated_tokens: int
    context_window_tokens: int
    reserved_output_tokens: int
    tools_estimated_tokens: int
    sections: list[dict[str, object]]
    tool_specs: list[dict[str, object]]
    diagnostics: list[dict[str, object]]
    lifecycle_decisions: list[dict[str, object]]
    metadata: dict[str, object] = {}
```

若短期不新增 typed event，也可以先用：

```python
CustomEvent(name="context_compiled", value={...})
```

但最终建议 typed event，因为这是 runtime 核心事实。

事件中不要默认保存完整 prompt 文本。保存：

- section id；
- source id；
- channel；
- render mode；
- included / omitted；
- estimated tokens；
- memory ids；
- artifact ids；
- capability descriptor ids；
- tool names / descriptor ids / schema estimates；
- lifecycle decisions，例如旧 cache entry 的 invalidated reason / old fingerprint / new fingerprint；
- warnings；
- truncation / degradation reason。

### 6.1 与实际 model call 的强关联

`ContextCompiledEvent` 不能只靠“sequence 上紧邻 ModelCallStartEvent”来关联实际模型调用。sequence 邻接在失败、hook、future async event、inline compact 下都比较脆。

必须选择一种强关联方式：

**推荐：给 `ModelCallStartEvent` 增加 `context_id`。**

```python
class ModelCallStartEvent(EventBase):
    provider: str
    model: str
    model_role: str
    context_id: str | None = None
    model_call_index: int | None = None
```

同时：

- `ContextCompiledEvent.context_id` 与 `ModelCallStartEvent.context_id` 相同；
- `model_call_index` 在同一 `(run_id, turn_id, reply_id)` 内单调递增；
- inspect 优先用 `context_id` join；
- 如果旧 event 没有 `context_id`，才 fallback 到 sequence 邻接或 `(run_id, turn_id, reply_id, model_call_index)`。

如果短期不修改 `ModelCallStartEvent` 字段，则必须把 `context_id` 写进它的 `metadata`。但 typed 字段更清晰，建议 PR4 做 typed event/schema migration。

LLM runtime 接线也要在 PR4 一次钉死，不能只改 event 类型。推荐路径：

1. `AgentRuntime` 为本次模型调用生成 `context_id` 与单调 `model_call_index`，并写入 `ContextCompileRequest`。
2. `ContextCompiler.compile(...)` 使用 request 中的 `context_id` / `model_call_index` 生成 `CompiledContext`，只回显，不重新编号。
3. `CompiledContext.llm_context` 携带 runtime-only 字段：

   ```python
   @dataclass(frozen=True, slots=True)
   class LLMContext:
       messages: tuple[LLMMessage, ...]
       tools: tuple[ToolSpec, ...] = field(default_factory=tuple)
       system_prompt: str | None = None
       context_id: str | None = None          # runtime-only, not provider payload
       model_call_index: int | None = None    # runtime-only, not provider payload
   ```

4. `AgentRuntime` 仍调用 `llm_runtime.stream(context=compiled.llm_context, ...)`，不额外旁路传参。
5. provider adapter 在发 `ModelCallStartEvent` 时从 `LLMContext.context_id` / `model_call_index` 复制到 typed event 字段。
6. adapter 构造实际 API payload 时必须忽略这两个字段，只使用 `messages` / `tools` / `system_prompt`。

不推荐把 `context_id` 塞进 `LLMOptions.metadata` 作为主路径：它会让“模型输入事实”和“请求选项”混在一起，也容易被不同 provider adapter 漏传。若为了短期兼容保留 metadata fallback，也只能作为旧 adapter 过渡路径。

这样 inspect 能回答：

> 为什么模型这一轮看到了这个 memory？
>
> 为什么某个 artifact 只有 ref，没有 full preview？
>
> 为什么 skill catalog 被压缩？
>
> 为什么某个 context source 被省略？

## 7. 与现有模块的关系

### 7.1 `runtime/context.py`

目标：从“直接构造 LLMContext”升级为 compiler 入口。

当前：

```python
build_llm_context(state, tools, system_prompt, budget) -> LLMContext
```

未来：

```python
compile_context(request: ContextCompileRequest) -> CompiledContext
```

短期可以保留 `build_llm_context(...)` 作为 wrapper，内部调用 compiler。

### 7.2 `runtime/agent.py`

当前 `_stream_model_loop(...)` 中：

1. project memory；
2. compose system prompt；
3. build LLM context；
4. call model。

未来：

1. project memory；
2. resolve capability exposure；
3. collect context sources；
4. compile context；
5. emit `ContextCompiledEvent`；
6. call model。

`compose_system_prompt(...)` 应逐步退场。

### 7.3 `capability/*`

`CapabilityExposurePlan` 已经很接近 source output。

目标不是重写 capability runtime，而是加一个 adapter：

```python
CapabilityContextSource(exposure: CapabilityExposurePlan)
```

输出：

- direct tool specs 仍进入 `LLMContext.tools`；
- catalog prompt 进入 context section；
- active skill prompt 进入 context section；
- diagnostics 默认进入 debug section / event，不一定进模型。

### 7.4 `memory_hooks` / memory projection

memory subsystem 继续负责 recall、filter、projection、trace。

Context compiler 只接收已经投影后的 memory context：

- projection text；
- included ids；
- conflict groups；
- warnings；
- token estimate。

它不重新判定 memory 是否应该召回。

### 7.5 `runtime/compaction/service.py`

compaction 的触发口径应完全基于 compiled model-visible context。

第一阶段：

- `should_auto_compact(...)` 使用 `CompiledContext.estimated_tokens`。
- compact input 仍由 event log 作为事实源，但 compact planning 参考最新 context boundary。

后续：

- compaction summary 自身也成为 `handoff_hint` section。
- compact prompt 输入由 context compiler 的 transcript/history projector 生成，而不是独立散落的渲染逻辑。

### 7.6 `inspector`

inspect 应新增视角：

- inspect run context；
- inspect turn context；
- inspect context section；
- inspect context omissions。

不用展示完整 prompt，先展示 section report 即可。

## 8. 推荐分步实施

### PR 0：冻结对象模型与 diagnostics

目标：

- 新增 `src/pulsara_agent/runtime/context_engine/`。
- 定义：
  - `ContextCompileRequest`
  - `ContextSource`
  - `ContextSourceOutput`
  - `ContextSection`
  - `CompiledContext`
  - `CompiledToolSpecUnit`
  - `ContextDiagnostic`
  - `ContextLifecycleDecisionDiagnostic` 基础类型可在 PR0 先定义；`CompiledContext.lifecycle_decisions` 在 PR0–PR5 可为空 tuple，PR6 再开始填充。
  - budget / channel / stability / render mode 枚举。
- 暂不改主 loop 行为。

验收：

- 类型和基础单测通过。
- 文档中的边界写进 docstring：compiler 不拥有 subsystem truth。
- `ContextCompileRequest` 显式包含 `context_id`、`model_call_index`、`current_user_message`、`current_user_input`、`current_user_anchor`。
- docstring 明确：`context_id` / `model_call_index` 由 `AgentRuntime` 生成，compiler 只回显。
- `ContextChannel` 显式包含 `current_user` / `current_run_tail`，并在 docstring 中标为 special channel。
- `CompiledContext` / `ContextBudgetReport` 显式包含 `tools_estimated_tokens`。

### PR 1：平移 runtime context + transcript projector

目标：

- 把 `render_runtime_context_prompt(...)` 平移为 `RuntimeContextSource`。
- 把 `msg_to_llm_messages(...)` 包装成 `TranscriptContextSource` 或 history projector。
- 新增 `CurrentUserContextSource` 或等价内建 section，从普通 transcript 中分离本轮 user message。
- 新增 `CurrentRunTailContextSource` 或等价 projector 分支，保留同一 run 内当前 user 之后已经发生的 assistant/tool tail。
- 实现第一版 channel lowering：system 合并、leading_user synthetic message、prior_history projection、handoff_hint、current_user、current_run_tail。
- `build_llm_context(...)` 改为通过 compiler 生成，但输出保持与当前基本一致。

验收：

- 现有 runtime / REPL / transcript 测试基本不变。
- `CompiledContext.sections` 能显示 runtime context 与 transcript。
- current user input 是独立 `must_keep` section，位于当前 run segment 起点，不会被当作旧 history 降级。
- current user split 使用 `current_user_anchor` / message id / run boundary，不用“最后一条 user message”启发式。
- `current_user_anchor` 缺失、找不到、或匹配多条时 fail-closed：不得静默重复 current user，也不得静默推断或丢弃 current_run_tail。
- 第二次 model call 的 lowering 顺序保持 `prior_history -> current_user -> assistant tool_call/tool_result tail`，不把 current user 移到 tool result 之后。
- current_run_tail 的结构与 pairing must_keep，但巨大 tool result body 仍可降级为 artifact preview / ref-only。
- tool-call / tool-result pairing 不被 leading_user / handoff_hint / tool_context 打断。
- tools schema 进入 `CompiledToolSpecUnit` 并计入 estimated tokens。

### PR 2：平移 capability / active skill / memory sections

目标：

- `CapabilityExposurePlan` 进入 `CapabilityContextSource`。
- memory projection 进入 `MemoryProjectionContextSource`。
- active skill prompt 不再直接拼 system prompt，而是成为 section。

验收：

- skill catalog 行为不变。
- active skill terminal attribution 不变。
- memory projection 仍遵守 surviving clipped text 边界。

### PR 3：统一 budget 与降级

目标：

- 引入 aggregate context budget。
- 至少支持：
  - tool result artifact ref-only fallback；
  - capability diagnostics debug-only；
  - memory projection compact fallback；

验收：

- 大 tool result 不挤爆整个 context。
- 多个 artifact result 共享 aggregate budget。
- omitted sections 出现在 diagnostics。
- current user input 超预算时硬失败或请求拆分，不静默裁剪。

### PR 4：ContextCompiledEvent + inspect

目标：

- 每次 model call 前 emit context compiled fact。
- `LLMContext` 增加 runtime-only `context_id` / `model_call_index` 字段，并由 provider adapter 复制到 `ModelCallStartEvent`。
- `ModelCallStartEvent` 携带同一个 `context_id`，或至少在过渡期 metadata 中携带。
- inspector 支持查看 context section report。

验收：

- real REPL 中能追踪某轮模型看到的 memory / skill / artifact refs。
- inspect 能从 model call 反查对应 context compile report，不依赖 sequence 邻接猜测。
- `ContextCompiledEvent.lifecycle_decisions` 字段从 PR4 起存在；PR4 / PR5 可为空 list，PR6 开始填充真实 lifecycle decision，避免 PR6 再追加事件 schema。
- provider API payload 不包含 `context_id` / `model_call_index` runtime-only 字段。
- event log 不保存完整 prompt 文本。

### PR 5：compaction 接入 compiler

目标：

- auto compact trigger 使用 compiled context token estimate。
- compact input 与 transcript projector 对齐。
- context compaction completed / failed 仍由 REPL 正确提示。

验收：

- Firecrawl / terminal 大输出不因 raw event volume 过早 compact。
- 真正 model-visible context 超阈值时能 compact。
- compact 后下一轮 context sections 可解释。

### PR 6：section lifecycle policy

目标：

- 把 `ContextStability` 从 report 字段升级成 provider-agnostic lifecycle policy。
- 新增 `ContextLifecycleCoordinator` 或等价模块，负责 section collect / reuse / invalidate。
- 为 `stable` / `turn` / `step` / `ephemeral` section 定义明确复用边界。
- 各 `ContextSource` 声明 dependency fingerprint，避免靠字符串内容判断复用。
- lifecycle cache key 显式包含 `scope_key`，按 stability 区分 runtime-session / turn / step / context 作用域。
- lifecycle 只缓存 source output / section candidates，不缓存最终 render mode 或 lowered `LLMContext`。
- cache 读写使用 immutable value object 或 deep copy，避免 mutable metadata/provenance 被后续 mutation 污染。

验收：

- 同一 user turn 内多次 model call 复用 turn-stable section，但 tool result / artifact 追加后 step section 重新 collect。
- 新 user input 后 turn section 失效，current user / memory projection / per-turn capability exposure 重新 collect。
- workspace root / workspace kind / explicit `--skill` / permission profile 变化会使相关 stable section 失效。
- plan mode enter / revise / cancel / approve 会使 plan workflow section 失效。
- memory projection id 变化会使 memory section 失效；没有变化时不重复投影 prompt 文本。
- compaction rewrite / transcript rewrite 后，history、current_user、current_run_tail、artifact/tool envelope 旧 cache entry 全部按新的 `transcript_revision` / `compaction_boundary_id` / `message_projection_fingerprint` invalidated；新 section 的最终状态应是 freshly_collected 或 reused，不是 invalidated。
- compaction rewrite 必须 preserve current user anchor，或显式返回新 anchor 并由 `AgentRuntime` 更新 `ContextCompileRequest.current_user_anchor`。
- `CompiledContextSection` 暴露 `lifecycle_status`、`lifecycle_reason`、`dependency_fingerprint`、`cache_key_scope`，inspect 不需要从 loose diagnostics 猜状态。
- section `lifecycle_status` 能解释最终 section 是 freshly_collected、reused 还是 not_cacheable；`lifecycle_decisions` 能解释旧 cache entry 为什么 invalidated。
- 不引入 provider-specific cache header / prompt cache key；PR6 仍保持 provider-agnostic。

## 9. 风险与坑

### 9.1 容易把 compiler 做成第二个 runtime

这是最大风险。

规避方式：

- compiler 不查询数据库做新事实判断；
- compiler 不执行 recall；
- compiler 不执行 tool permission；
- compiler 不启动 MCP；
- compiler 不修复 memory governance。

它只消费已解析对象。

### 9.2 Prompt 文本落库风险

完整 prompt 可能包含：

-用户私密文本；
- home 目录路径；
- tool output；
- skill body；
- memory projection。

默认不落完整 prompt。只落 section report 和 ids。

### 9.3 Budget 降级可能改变模型行为

从 prompt 拼接改为 section 降级后，模型看到的信息会发生细微变化。

需要做 dogfood：

- plan mode；
- skill 使用；
- memory recall；
- artifact_read；
- long terminal output；
- manual / auto compact；
- MCP tool。

### 9.4 System vs leading-user 迁移要保守

第一版不要大规模改变语义优先级。

建议：

- 原 system prompt 中的 safety / permission / plan 继续 system。
- runtime context 可以先保持 system 或迁到 leading-user，但要单独测试。
- skill body 如果当前依赖 system 权重，先保持 system。

### 9.5 ContextCompiledEvent 不能造成 publish gap

Pulsara 已经对 event publishing 顺序敏感。新增事件必须使用 RuntimeSession 正常 emit 路径，不能在失败路径留下 unpublished sequence。

## 10. 测试矩阵

### 编译等价性

- 无 memory / 无 skill / 无 artifact 时，compiled LLMContext 与当前输出基本一致。
- runtime context section 包含 workspace root、workspace kind、terminal cwd。
- transcript projector 保持 tool call / tool result pairing。

### Capability

- skill catalog 进入 capability section。
- active skill body 进入 active skill section。
- capability diagnostics 默认不污染模型上下文。
- hidden / unavailable capability 不被 compiler 重新放入模型。

### Memory

- memory projection section 只声明真实 included memory ids。
- contradiction warning 为 must_keep。
- empty memory_search result 不清空近期工作上下文。

### Artifact / tool result

- 中等输出 full / compact。
- 巨大输出 head/tail + artifact ref。
- 多 artifact 时 primary preview artifact 优先保留。
- budget 耗尽时仍保留 read-more ref。

### Plan mode

- plan mode safety section 为 must_keep。
- plan question / exit pending interaction 可解释。
- approve / revise / cancel 后 context section 变化正确。

### Compaction

- raw event 很多但 model-visible context 小，不 auto compact。
- model-visible context 大，触发 auto compact。
- compact summary 作为 handoff section 进入下一轮。
- previous summary 不因连续 compact 丢失。
- mid-turn compact 发生在 `current_user -> assistant tool_call -> tool_result` 之后时，下一次 compile 仍能用 preserved 或更新后的 `current_user_anchor` split 出 `current_user + current_run_tail`，且 tool-call/tool-result pairing 不被打断。

### Inspect

- inspect 能显示每轮 section list。
- inspect 能显示 omitted/degraded reason。
- inspect 不默认展示完整 prompt。

## 11. 第一版不做什么

明确不做：

- provider-specific cache strategy；
- Anthropic cacheControl；
- OpenAI promptCacheKey；
- tokenizer 接口；
-完整 prompt archival；
- subagent-specific context；
- MCP 产品化配置；
- memory recall algorithm 改写；
- tool permission 改写。

但对象模型应为这些后续能力留位置，尤其是：

- `stability`
- `channel`
- `budget_class`
- `render_mode`
- `provenance`

## 12. 最终判断

Pulsara 已经有强事实层：runtime event、artifact/evidence、semantic memory、capability exposure、inspect。

下一步 context engineering 的目标，是让模型可见上下文也达到同样的结构化程度。

它的产品意义不是“prompt 更整洁”，而是让 Pulsara 能清楚回答：

> 当前模型为什么知道这些？为什么不知道那些？哪些事实被完整展示，哪些被压缩成引用，哪些因为预算被省略？

这就是 Pulsara 的 context compiler。
