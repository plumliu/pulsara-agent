# Pulsara Capability Gate Decision Event 升格实施文档

## Summary

当前 runtime 会在每次工具调用进入 capability / permission gate 后写入：

```python
CustomEvent(name="capability_gate_decision", value={...})
```

这个事件已经不再是临时 diagnostic。它承担了 Pulsara 对工具调用的核心解释事实：本轮 capability exposure 是否允许该工具、descriptor 是否存在、permission policy 如何裁决、是否需要用户确认、是否被拒绝、以及 terminal call 是否关联 active skill / CLI guidance。

因此下一步应将它升格为一等 `AgentEvent`：

```python
CapabilityGateDecisionEvent(type="CAPABILITY_GATE_DECISION")
```

`CustomEvent` 仍保留给实验性、插件性或尚未稳定的 runtime diagnostic；`capability_gate_decision` 作为审计、inspect、resume/replay 可解释性的稳定事实，不应继续被包在 `CustomEvent.value` 里。

## 1. 为什么需要从 CustomEvent 升格

### 1.1 它已经是 runtime 核心事实，而不是临时日志

`capability_gate_decision` 当前回答的是：

- 模型请求的 tool call 在本轮 capability surface 中是否可见、可调用；
- descriptor / binding / exposure / availability 是否通过 fail-closed 检查；
- permission policy 对该 tool call 的最终裁决是什么；
- 如果被拒绝或挂起，原因是什么；
- terminal / terminal_process call 是否发生在某个 active skill 指导下；
- allow / deny / wait 三种路径是否都被同一套事实记录覆盖。

这些事实已经被 inspector、debug、权限审计和长期 replay 依赖。继续放在 `CustomEvent` 会让事件 schema 对真实产品语义撒谎：明明是稳定的一等 runtime fact，却以“任意字典”的形式落库。

### 1.2 CustomEvent 的结构约束太弱

当前结构：

```json
{
  "type": "CUSTOM",
  "name": "capability_gate_decision",
  "value": {
    "tool_call_id": "call:...",
    "tool_name": "terminal",
    "descriptor_id": "builtin:terminal",
    "decision": "allow",
    "reason_code": null,
    "reason_message": null,
    "policy_mode": "bypass-permissions",
    "capability_context": {...}
  }
}
```

问题：

- `value` 的字段没有类型约束；
- inspector 必须靠 `name == "capability_gate_decision"` 做字符串分流；
- 事件序列查询无法直接按 `event_type` 区分 gate decision；
- event contract 无法表达字段是否必填、可空、枚举；
- 旧字段漂移时，测试不容易第一时间失败；
- 数据库 / JSONB 投影需要深入 `payload.value` 才能读取业务事实。

升格后：

```json
{
  "type": "CAPABILITY_GATE_DECISION",
  "tool_call_id": "call:...",
  "tool_name": "terminal",
  "descriptor_id": "builtin:terminal",
  "decision": "allow",
  "reason_code": null,
  "reason_message": null,
  "policy_mode": "bypass-permissions",
  "capability_context": {...}
}
```

事件类型本身就说明了事实类别，字段由 Pydantic schema 约束。

### 1.3 它连接 capability 与 permission 两个平面

这个事件不应命名为 `PermissionDecisionEvent`，因为它不只记录 permission policy。

它覆盖的实际顺序是：

```text
descriptor / exposure / callable check
        ↓
call classifier
        ↓
permission policy gate
        ↓
allow / deny / wait_for_user
        ↓
tool result / pending approval / inspector projection
```

所以推荐名称是：

```python
CapabilityGateDecisionEvent
```

它表示“工具调用经过 unified capability surface gate 后得到的最终 gate fact”。permission 是其中一层，不是全部。

## 2. 新 Event DTO

建议新增：

```python
class CapabilityGateDecisionEvent(EventBase):
    type: Literal[EventType.CAPABILITY_GATE_DECISION] = EventType.CAPABILITY_GATE_DECISION

    tool_call_id: str
    tool_name: str
    descriptor_id: str | None = None

    decision: Literal["allow", "deny", "wait_for_user"]
    reason_code: str | None = None
    reason_message: str | None = None
    suggested_rules: list[dict[str, Any]] = Field(default_factory=list)
    result_state: Literal["success", "error", "denied", "interrupted", "running"] | None = None

    policy_mode: str | None = None
    permission_policy: dict[str, Any] = Field(default_factory=dict)

    exposure_generation: int | None = None
    availability: str | None = None
    permission_category: str | None = None
    effective_permission_category: str | None = None
    effective_read_only: bool | None = None

    capability_context: dict[str, Any] = Field(default_factory=dict)
```

PR1 必须填充审计必需字段，包括 reason / suggested rules、非空 `permission_policy`、`exposure_generation` 等。本文档第八部分要求完成 PR1 到 PR3，因此 classifier / descriptor 解释字段也必须在本轮落地。

重要：typed event 的输入不应直接是 batch-level `PermissionDecision`。写入前必须先形成 per-call 的稳定事实 DTO（见第 4 节），否则会把当前 batch DENY reason 套给兄弟工具的问题固化到长期 event schema。

## 3. 字段说明与必要性

### 3.1 `tool_call_id: str`

必要。

用于关联：

- `ToolCallStartEvent`
- `ToolCallDeltaEvent`
- `ToolCallEndEvent`
- `ToolResultStartEvent`
- `ToolResultEndEvent`
- pending approval / resume state
- inspector 的 per-call projection

没有 `tool_call_id`，gate decision 无法稳定挂回具体 tool call。

### 3.2 `tool_name: str`

必要。

这是模型实际请求的 tool name，也是 gate 看到的 name。它必须保留原始 model-visible name，而不是 descriptor display name。

用途：

- unknown tool / descriptor missing 解释；
- MCP mangled tool name 追踪；
- terminal / terminal_process 特判；
- inspector 展示。

### 3.3 `descriptor_id: str | None`

必要，可空。

当 descriptor 存在时记录 descriptor truth，例如：

```text
builtin:terminal
mcp:docs-langchain:search_docs_by_lang_chain
workflow:todo
```

当 descriptor 缺失时为 `None`，这本身就是 fail-closed 的核心证据。

用途：

- 判断 tool call 是否来自 unified capability descriptor；
- 区分同名工具漂移；
- 解释 descriptor missing / binding drift；
- 支持未来 descriptor version / provider provenance。

### 3.4 `decision: "allow" | "deny" | "wait_for_user"`

必要。

对应 `PermissionDecisionKind`：

- `allow`：本次 call 可继续执行；
- `deny`：本次 call 被 gate 拒绝，并应生成 tool error/denied result；
- `wait_for_user`：该 call 因本批 gate 进入 pending approval。

注意：这里仍沿用当前 `PermissionDecisionKind` 的值，避免额外映射。

`wait_for_user` 的 per-call 语义要精确：它不一定表示该 call 自身需要批准。若同 batch 里一个 write call 触发批准、一个 read call 被 batch suspension 一起挂起，那么 read call 的 event 也可以是 `wait_for_user`，但 `reason_code` / `reason_message` 必须说明这是 batch suspension 牵连，而不是 read call 自身有风险。

### 3.5 `reason_code: str | None` 与 `reason_message: str | None`

必要，可空。

当前实现里 `PermissionDecision.reason` 同时承担 code 与 message 的角色，例如：

- `capability_descriptor_missing`
- `capability_hidden`
- `capability_unavailable`
- `capability_not_callable`
- `terminal command blocked by hardline permission policy`

这次 schema 升级不应继续把任意 human-readable message 命名为 `reason_code`。推荐规则：

- 如果 reason 能被识别为稳定 code，则写入 `reason_code`；
- 原始可读解释写入 `reason_message`；
- 如果只能拿到一段 message，`reason_code=None`，`reason_message=<原文>`；
- 不要把 `"terminal command blocked by hardline permission policy"` 这类句子写成 code。

V1 可先实现一个很小的 normalizer：

```python
KNOWN_GATE_REASON_CODES = {
    "capability_descriptor_missing",
    "capability_hidden",
    "capability_unavailable",
    "capability_not_callable",
    "permission_denied",
    "permission_wait_for_user",
    "permission_wait_for_user_batch_suspension",
    "workflow_control_batch_suppressed",
    "mcp_resume_permission_approval_unsupported",
    "hardline_terminal_command_blocked",
    "hardline_terminal_process_input_blocked",
}
```

无法可靠分类时不要编造 code，保留 message 即可。

### 3.6 `suggested_rules: list[dict[str, Any]]`

建议新增，默认为空。

它来自 `PermissionDecision.suggested_rules`，用于记录 gate 在 `wait_for_user` 场景下给出的规则建议。

必要性：

- gate decision event 是审计事实，应保留当时 gate 给出的建议；
- `RequireUserConfirmEvent` 负责具体 pending interaction，不应成为唯一保存 suggested rules 的地方；
- resume / inspect 可解释“当时为什么向用户提出这些确认选项”。

如果某条 gate decision 没有建议规则，写空列表即可。

### 3.7 `result_state: ToolResultState | None`

可空。

只有当 gate decision 已经对应一个实际 tool result 时填写，例如：

- descriptor missing → `error`
- hidden / unavailable / not-callable → `denied`
- permission deny → `denied`

`allow` 通常不应预填 `success`，因为工具尚未执行；`wait_for_user` 也不应填最终 result state。

用途：

- inspector 能解释“gate deny 后模型看到的是 error 还是 denied”；
- replay 中可关联 `ToolResultEndEvent.state`；
- 保留当前 `_emit_capability_gate_decision(..., result_state=...)` 语义。

### 3.8 `policy_mode: str | None`

必要，可空。

记录当前 product-level permission preset：

- `read-only`
- `ask-permissions`
- `accept-edits`
- `bypass-permissions`

当运行时由 advanced raw policy 构造，无法映射到 preset 时可为 `None`。

用途：

- 用户问“为什么这次被拦 / 为什么这次不用问我”；
- plan mode 进入 read-only 后的行为解释；
- resume/inspect 对历史权限模式的复盘。

### 3.9 `permission_policy: dict[str, Any]`

建议新增，PR1 起应非空填充。

`policy_mode` 只表示 preset 名；真实 gate 依赖的是 effective policy：

```json
{
  "profile": "trusted_host",
  "approval_policy": "never",
  "terminal_access": "allow",
  "filesystem": {
    "read_file_scope": "host_local_text",
    "write_file_scope": "workspace_only",
    "terminal": "host_shell"
  }
}
```

必要性：

- advanced/custom policy 没有稳定 mode；
- policy 可能未来演化；
- `terminal=off` / `approval=on_request` 这类关键事实不能只靠 mode 推断。

新 typed event 从 PR1 起必须填写非空 `permission_policy`。本轮硬切后，legacy `CustomEvent(name="capability_gate_decision")` 不再作为 inspector/projection 读取路径。

### 3.10 `exposure_generation: int | None`

建议新增，PR1 起应非空填充。

当前 `CapabilityExposurePlan` 有 registry/exposure 事实。gate decision 应能回答“这是基于哪一轮 capability exposure 做出的裁决”。

用途：

- 同一 run 内 MCP safe-point refresh 后，解释不同工具可见性；
- deferred discovery / activation 后，解释前后 callable 集合变化；
- inspector 连接 `capability_exposure_resolved` 与 gate decision。

当前代码中应使用 `CapabilityExposurePlan.registry_generation` 作为 `exposure_generation`。该值必须在生成 `CapabilityGateDecisionFact` 时写入 fact；event emitter 不应旁路读取 exposure，否则会重新引入“现场取值”和 gate 事实漂移。

### 3.11 `availability: str | None`

建议新增，从 descriptor/exposure 填。

记录 descriptor 的 availability：

- `available`
- `degraded`
- `unavailable`

这三个值不是未来扩展，而是当前 `CapabilityAvailability` enum 已有语义：

```python
class CapabilityAvailability(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
```

用途：

- hidden/unavailable/not-callable fail-closed 解释；
- degraded descriptor 的 inspector projection；
- 区分“descriptor 不存在”和“descriptor 存在但不可用”。

### 3.12 `permission_category: str | None`

建议新增。

记录 descriptor 默认的 permission category，例如：

- `filesystem_read`
- `filesystem_write`
- `terminal`
- `terminal_process_observe`
- `workflow_control`
- `memory_read`

用途：

- inspector 展示 descriptor 默认策略；
- 对比 call classifier 的 effective category。

### 3.13 `effective_permission_category: str | None`

建议新增。

记录经过 `CapabilityCallClassifier` 后的 per-call category。

必要性非常高，尤其是：

- `terminal_process` tool-level 不是 read-only；
- 但 `list/log/poll/wait` 是 observe action；
- CLI/MCP 未来也可能按 subcommand / destructive hints 分类。

如果不记录 effective category，历史上为什么某个 `terminal_process list` 在 ask/on-request 下直接 allow 会解释不清。

### 3.14 `effective_read_only: bool | None`

建议新增。

记录 per-call read-only 结果，而不是 tool-level descriptor 默认值。

必要性：

- terminal_process observe action；
- future CLI subcommand；
- MCP read-only/destructive hints；
- read-only mode 下的 allow/deny 解释。

### 3.15 `capability_context: dict[str, Any]`

保留。

当前主要用于 terminal / terminal_process 的 active skill attribution：

```json
{
  "context_kind": "active_skill_present",
  "active_skill_names": ["hf-cli"],
  "skill_suggested_tools": ["terminal"],
  "cli_required_binaries": ["hf"],
  "cli_external_services": ["huggingface"]
}
```

必要性：

- inspector 能回答“这次 terminal call 是否是在某个 active skill 指导下发生的”；
- allow / deny / wait 三条路径都能统一归因；
- 不把该事实塞进 tool result 或 artifact metadata。

该字段仍保持 dict，原因是 active skill / MCP / CLI guidance 的 attribution schema 可能继续扩展；但外层 event 必须 typed。

## 4. Per-call gate fact carrier

### 4.1 为什么 typed event 不能直接消费 batch-level `PermissionDecision`

当前 `PermissionDecision` 是 batch-level 结果：

```python
@dataclass(frozen=True, slots=True)
class PermissionDecision:
    kind: PermissionDecisionKind
    reason: str | None = None
    suggested_rules: list[dict] = field(default_factory=list)
```

但 `CapabilityGateDecisionEvent` 是 per-call 事件。二者粒度不同。

如果直接把 batch-level `PermissionDecision` 分发给每个 call，会固化两个问题：

- 某个 call-local deny（descriptor missing / hidden / unavailable / not-callable）的 reason 可能被套到同 batch 的其他合法 call；
- batch DENY 的 reason 可能无法解释每个 call 的 effective classifier 结果。

typed event 升格前必须引入一个明确的 per-call carrier。推荐 DTO：

```python
@dataclass(frozen=True, slots=True)
class CapabilityGateDecisionFact:
    tool_call_id: str
    tool_name: str
    descriptor_id: str | None

    decision: PermissionDecisionKind
    reason_code: str | None
    reason_message: str | None
    suggested_rules: tuple[dict[str, Any], ...] = ()

    result_state: ToolResultState | None = None
    policy_mode: str | None = None
    permission_policy: dict[str, Any] = field(default_factory=dict)

    exposure_generation: int | None = None
    availability: CapabilityAvailability | None = None
    permission_category: str | None = None
    effective_permission_category: str | None = None
    effective_read_only: bool | None = None

    capability_context: dict[str, Any] = field(default_factory=dict)
```

也可以更进一步定义 batch wrapper：

```python
@dataclass(frozen=True, slots=True)
class CapabilityGateBatchDecision:
    kind: Literal["allow", "deny", "wait_for_user", "mixed"]
    per_call: tuple[CapabilityGateDecisionFact, ...]
    approval_group_id: str | None = None
```

### 4.2 生成规则

推荐把 gate pipeline 拆成三个稳定阶段：

1. `evaluate_capability_exposure_access(call, exposure)` 先逐 call 生成 call-local exposure fact；
2. 对通过 exposure 的 calls 运行 Pulsara-owned 纯本地 policy/classifier pre-pass；
3. 对 surviving batch 调用一次 batch-level permission gate / custom inner gate；
4. 合并成 `CapabilityGateDecisionFact`，再 emit typed event。

重要边界：

- per-call pre-pass 只能运行纯本地 exposure / descriptor / hardline terminal / action classifier / built-in policy 逻辑；
- pre-pass 不得调用 custom inner `PermissionGate.evaluate()`，否则会把 batch-sensitive / stateful gate 执行 N+1 次；
- custom inner gate 只能在 surviving batch 上调用一次；
- pre-pass 得到的 per-call `WAIT_FOR_USER` 必须保留，不能只保留 DENY；
- 当 batch 因某个 call 进入 pending approval 时，拥有 per-call WAIT 的 call 使用 `permission_wait_for_user`；本地为 ALLOW、只是被同批挂起的 call 才使用 `permission_wait_for_user_batch_suspension`。

规则：

- descriptor missing / hidden / unavailable / not-callable 必须是 call-local deny；
- 这些 call-local deny 不得阻塞同 batch 其他合法 call；
- `WAIT_FOR_USER` 可以是 batch suspension，但每个 pending call 仍要有自己的 fact；
- `DENY` 不得把某个兄弟工具的 reason 套给所有 call；
- `_emit_capability_gate_decision()` 不应重新 evaluate gate 或 classifier，只消费已经算好的 fact；
- classifier 结果必须随 fact 传递，不能在 event emitter 里临时重算。

Workflow control tools 是同一条规则的特殊执行 plane：

- `enter_plan` / `ask_plan_question` / `exit_plan` 在 `_handle_workflow_tool_batch()` 执行前，也必须先产生 `CapabilityGateDecisionEvent(decision="allow")`；
- 同 batch 中被 workflow control 抑制、不执行的 sibling tool call，必须产生 `CapabilityGateDecisionEvent(decision="deny", result_state="denied")`，并由 workflow handler 产生对应 denied tool result；
- workflow control 不能因为由 runtime control plane 截获而跳过 canonical gate event。

### 4.3 与 PR 拆分的关系

PR1 如果只做 typed event，但不引入 per-call carrier，就会把旧 batch 粒度问题固化为新 schema。

因此 PR1 的验收必须包括：

- 写路径只从 `CapabilityGateDecisionFact` / 等价 per-call DTO 构造 event；
- call-local deny 只给对应 call 产生 deny/error；
- 同 batch 合法 call 仍继续执行；
- typed event 的 reason 与 tool_call_id 一一对应。

PR2 再补齐更多 effective fields 是合理的；但 carrier 的形态必须在 PR1 先定下来。

## 5. 与现有事件的边界

### 5.1 与 `RequireUserConfirmEvent`

`CapabilityGateDecisionEvent(decision="wait_for_user")` 表示：

> gate 判定该 tool call 需要用户确认。

`RequireUserConfirmEvent` 表示：

> runtime 已创建一个具体 pending approval interaction，并把问题交给 host/UI。

二者不可互相替代。

一条典型路径：

```text
ToolCallStartEvent
ToolCallDeltaEvent*
ToolCallEndEvent
CapabilityGateDecisionEvent(decision="wait_for_user")
RequireUserConfirmEvent
UserConfirmResultEvent
CapabilityGateDecisionEvent(decision="allow")   # resume/recheck 后可选
ToolResultStartEvent
ToolResultEndEvent
```

### 5.2 与 `ToolResultEndEvent`

`ToolResultEndEvent` 记录工具执行结果。

`CapabilityGateDecisionEvent` 记录工具执行前的 gate 裁决。

对于 deny/error，runtime 可能会生成一个 tool result 给模型看；这时两个事件都存在：

- `CapabilityGateDecisionEvent(decision="deny", result_state="denied")`
- `ToolResultEndEvent(state="denied")`

这不是重复，而是两个不同层面的事实。

### 5.3 与 `ContextCompiledEvent`

`ContextCompiledEvent` 描述“哪些事实进入模型上下文”。

`CapabilityGateDecisionEvent` 描述“某个 tool call 为什么被允许/拒绝/挂起”。

如果 context compiler 后续把 gate decision replay 给模型或 inspector，需要引用 typed event，而不是解析 `CustomEvent.value`。

### 5.4 与 `capability_exposure_resolved`

`capability_exposure_resolved` 目前仍可以保留为 `CustomEvent`，因为它的 payload 更大、更像 diagnostic/projection dump。

但如果未来 exposure plan 本身也成为 inspect/resume 的硬事实，可以再单独升格为：

```python
CapabilityExposureResolvedEvent
```

本轮只处理 per-call gate decision。

## 6. 代码动刀落脚点

### 6.1 `src/pulsara_agent/event/events.py`

新增 event type：

```python
class EventType(StrEnum):
    ...
    CAPABILITY_GATE_DECISION = "CAPABILITY_GATE_DECISION"
```

新增 DTO：

```python
class CapabilityGateDecisionEvent(EventBase):
    type: Literal[EventType.CAPABILITY_GATE_DECISION] = EventType.CAPABILITY_GATE_DECISION
    ...
```

将它加入 `AgentEvent` union。

注意：`event_log.serialization._EVENT_CLASS_BY_TYPE` 是从 `AgentEvent` union 自动构造 registry，因此只要加入 union，序列化/反序列化 registry 会自动更新。

### 6.2 `src/pulsara_agent/event/__init__.py`

导出：

```python
CapabilityGateDecisionEvent
```

并加入 `__all__`。

### 6.3 `src/pulsara_agent/runtime/agent.py`

当前函数：

```python
async def _emit_capability_gate_decision(...):
    ...
    yield await self.runtime_session.emit(
        CustomEvent(
            name="capability_gate_decision",
            value=value,
        )
    )
```

改为消费 `CapabilityGateDecisionFact`，而不是重新拼 batch-level `PermissionDecision`：

```python
yield await self.runtime_session.emit(
    CapabilityGateDecisionEvent(
        **self._event_context(state).event_fields(),
        tool_call_id=fact.tool_call_id,
        tool_name=fact.tool_name,
        descriptor_id=fact.descriptor_id,
        decision=fact.decision.value,
        reason_code=fact.reason_code,
        reason_message=fact.reason_message,
        suggested_rules=list(fact.suggested_rules),
        policy_mode=fact.policy_mode,
        permission_policy=fact.permission_policy,
        result_state=fact.result_state.value if fact.result_state is not None else None,
        exposure_generation=fact.exposure_generation,
        availability=fact.availability.value if fact.availability is not None else None,
        permission_category=fact.permission_category,
        effective_permission_category=fact.effective_permission_category,
        effective_read_only=fact.effective_read_only,
        capability_context=fact.capability_context,
    ),
    state=state,
)
```

完成 PR1 到 PR3 后必须填充 classifier / exposure 解释字段：

`effective_permission_category` / `effective_read_only` 必须从 gate/classifier 计算阶段经 `CapabilityGateDecisionFact` 传入。不要在 `_emit_capability_gate_decision()` 里重新运行 classifier 或 permission gate，避免副作用、batch-sensitive 漂移和 reason 错配。

### 6.4 inspector / projection

需要搜索所有：

```python
CustomEvent
capability_gate_decision
```

并改为只读取：

- `CapabilityGateDecisionEvent`

projection helper：

```python
def capability_gate_decision_payload(event: AgentEvent) -> dict[str, Any] | None:
    if isinstance(event, CapabilityGateDecisionEvent):
        payload = event.model_dump(mode="json")
        payload["sequence"] = event.sequence
        payload["run_id"] = event.run_id
        payload["turn_id"] = event.turn_id
        payload["reply_id"] = event.reply_id
        return payload
    return None
```

不要要求旧 event log migration。历史 session 中的旧 `CustomEvent(name="capability_gate_decision")` 不再参与新 inspector gate projection；这是 intentional hard cut。

注意：projection 输出必须是 normalized shape。typed event 要补齐：

- `sequence`
- `run_id`
- `turn_id`
- `reply_id`（如当前 inspector projection 需要）
- `tool_call_id`
- `tool_name`
- `descriptor_id`
- `decision`
- `reason_code`
- `reason_message`
- `suggested_rules`
- `policy_mode`
- `permission_policy`
- `exposure_generation`
- `availability`
- `permission_category`
- `effective_permission_category`
- `effective_read_only`
- `result_state`
- `capability_context`

也就是说，新 `gate_decisions` projection 的 shape 应稳定来自 typed event，而不是 arbitrary `CustomEvent.value`。

### 6.5 contracts

PR1 必须同步更新长期契约，否则新写路径会和现有 contract 冲突。

需要修改：

- `contracts/CAPABILITY_SURFACE_CONTRACT.zh.md`
- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`
- 如涉及 MCP gate 展示文字，也同步 `contracts/MCP_CAPABILITY_CONTRACT.zh.md`

契约改动原则：

- `CapabilityGateDecisionEvent` 是唯一 canonical 写路径；
- `CustomEvent(name="capability_gate_decision")` 不再作为 reader/projection 兼容路径；
- 不再要求新 run 写 `CustomEvent.value` 结构；
- active skill attribution 的表述从 `capability_gate_decision.value.capability_context` 改为 `CapabilityGateDecisionEvent.capability_context`；
- inspector projection 输出的 `gate_decisions` 形状保持稳定，由 typed event 直接投影。

`CAPABILITY_SURFACE_CONTRACT.zh.md` 中应替换：

```text
所有最终 gate 结果必须写入 CustomEvent(name="capability_gate_decision")
```

为：

```text
所有最终 gate 结果必须写入 CapabilityGateDecisionEvent。
CustomEvent(name="capability_gate_decision") 不再是 reader/projection 兼容路径。
```

`INSPECTOR_PROJECTION_CONTRACT.zh.md` 中应替换：

```text
Inspector 必须从 CustomEvent 投影 capability facts
```

为：

```text
Inspector 必须从 typed CapabilityGateDecisionEvent 投影 gate decisions。
```

### 6.6 tests

建议新增 / 修改测试：

1. `test_capability_gate_decision_is_typed_event`
   - 触发一次普通 allow tool call；
   - 断言事件流里出现 `CapabilityGateDecisionEvent`；
   - 断言不再出现 `CustomEvent(name="capability_gate_decision")`。

2. `test_capability_gate_decision_serializes_and_replays`
   - `dump_agent_event` / `load_agent_event` 能 round-trip 新事件。

3. `test_terminal_active_skill_context_on_typed_gate_event`
   - active skill + terminal call；
   - 断言 `capability_context.context_kind == "active_skill_present"`。

4. `test_terminal_process_observe_gate_event_records_effective_category`
   - 断言 `terminal_process list/log/poll/wait` 的 effective category 是 observe。

5. `test_degraded_descriptor_gate_event_projection`
   - 构造 `availability="degraded"` 的 descriptor；
   - 断言 typed gate event / inspector projection 保留 degraded，而不是只识别 available/unavailable。

6. `test_gate_decision_projection_shape_is_typed_event_only`
   - 构造 typed event；
   - 断言 projection 补齐 sequence/run_id/turn_id/reply_id；
   - 断言旧 `CustomEvent(name="capability_gate_decision")` 不进入 `gate_decisions`。

7. `test_wait_for_user_batch_suspension_reason_code`
   - 构造同 batch 中一个 call 自身触发 approval、另一个 call 被 batch suspension 牵连；
   - 断言前者 reason_code 为 `permission_wait_for_user`；
   - 断言后者 reason_code 为 `permission_wait_for_user_batch_suspension`。

8. `test_multiple_independent_wait_calls_keep_own_reason_code`
   - 构造同 batch 中两个独立需要 approval 的 call；
   - 断言二者都使用 `permission_wait_for_user`，不把第二个误标为 batch suspension。

9. `test_permission_prepass_does_not_invoke_inner_gate_per_call`
   - 构造两个 local-allow calls 和一个 counting inner gate；
   - 断言 inner gate 只收到一次 batch evaluate。

10. `test_workflow_control_emits_gate_decision_before_execution_and_suppresses_siblings`
   - 构造 workflow control call 和 sibling call；
   - 断言 workflow call 有 allow gate event；
   - 断言 sibling 有 `reason_code="workflow_control_batch_suppressed"` 的 deny gate event 和 denied tool result。

11. `test_capability_gate_decision_pr1_required_fact_fields`
   - 触发 typed gate event；
   - 断言 `permission_policy` 非空；
   - 断言 `exposure_generation` 等于当前 `CapabilityExposurePlan.registry_generation`；
   - 断言 hardline terminal command / terminal_process input 分别映射到不同 stable reason code。

12. `test_mcp_input_required_resume_exposure_denial_emits_gate_decision`
    - MCP input-required resume 时 descriptor 已从 exposure 消失；
    - 断言写入 `CapabilityGateDecisionEvent(decision="deny", reason_code="capability_descriptor_missing")`。

13. `test_mcp_input_required_resume_permission_denial_emits_gate_decision`
    - MCP input-required resume 时 permission policy 已变为 deny；
    - 断言写入 `CapabilityGateDecisionEvent(decision="deny", reason_code="permission_denied")`。

14. `test_mcp_input_required_resume_permission_wait_fails_closed_with_typed_deny`
    - MCP input-required resume 时 permission recheck 返回 `WAIT_FOR_USER`；
    - V1 不在 resume 边界创建二次 approval；
    - 断言 fail-closed 写入 `CapabilityGateDecisionEvent(decision="deny", reason_code="mcp_resume_permission_approval_unsupported")`，而不是写 `decision="wait_for_user"`。

## 7. 硬切策略

### 7.1 不做历史事件迁移

不需要把旧数据库里的：

```json
{"type": "CUSTOM", "name": "capability_gate_decision"}
```

批量改写成新事件。

原因：

- 旧 event log 是历史事实；
- 修改历史事件风险高；
- 新事件从升级后自然落库。
- 本轮选择 hard cut，不再维护旧 custom gate projection。

### 7.2 写路径只写新事件

升级后 runtime 写路径不再发旧 CustomEvent。

不要双写：

- 会污染 event stream；
- inspect 需要去重；
- sequence 会多一倍；
- replay 语义变复杂。

读路径也不兼容旧 gate custom event；新 inspector 只从 `CapabilityGateDecisionEvent` 投影 gate decisions。

### 7.3 `CustomEvent` 继续保留

`CustomEvent` 不是废弃。它仍用于：

- 临时 diagnostic；
- 插件/外部 provider 附加事件；
- 尚未稳定的实验性 runtime facts；
- 大型 projection dump。

只是 `capability_gate_decision` 不再属于这类。

## 8. PR 拆分建议

### PR 1：事件类型升格

范围：

- 新增 `CapabilityGateDecisionEvent`
- 新增 `CapabilityGateDecisionFact` / 等价 per-call carrier
- 在 fact/event 中填充 `exposure_generation=CapabilityExposurePlan.registry_generation`
- 替换 `_emit_capability_gate_decision`
- 基础 serialization tests
- inspector/helper 只读取 typed event
- 同步长期契约文档

验收：

- 新 run 只产生 typed event；
- 旧 CustomEvent 不进入新 inspector gate projection；
- call-local deny 不影响同 batch 合法 call；
- typed event reason 与 `tool_call_id` 一一对应；
- typed event 必须填充非空 `permission_policy`，且 `exposure_generation == CapabilityExposurePlan.registry_generation`；
- `contracts/CAPABILITY_SURFACE_CONTRACT.zh.md` 与 `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md` 不再要求新写路径使用 CustomEvent；
- permission/capability 现有测试通过。

### PR 2：补齐 classifier / descriptor 字段

范围：

- `effective_permission_category`
- `effective_read_only`
- `availability`
- `permission_category`

验收：

- `terminal_process list/log/poll/wait` 的 action-level allow 解释完整；
- hidden/unavailable/not-callable 的 typed event 可解释；
- MCP descriptor/binding drift 的 deny event 可解释。

### PR 3：inspector 展示优化

范围：

- `pulsara inspect` / host inspect 展示 per-call gate decisions；
- 按 `tool_call_id` 连接 tool call/result；
- 展示 capability_context / active skill attribution。

验收：

- 用户可问“刚刚用了什么工具、为什么允许/拒绝”；
- inspector 不需要解析 arbitrary CustomEvent payload。

## 9. Open Questions

### 9.1 `reason_code` 的枚举集合是否要一次性冻结？

code/message 拆分应在 PR1 完成；这里不再作为 open question。

仍然可以暂缓的是完整枚举集合。V1 只需要冻结最小稳定集合：

```python
capability_descriptor_missing
capability_hidden
capability_unavailable
capability_not_callable
permission_denied
permission_wait_for_user
permission_wait_for_user_batch_suspension
workflow_control_batch_suppressed
mcp_resume_permission_approval_unsupported
hardline_terminal_command_blocked
hardline_terminal_process_input_blocked
```

不能识别的旧 reason 写入 `reason_message`，`reason_code=None`。

映射规则：

- call 自身需要 approval：`permission_wait_for_user`
- call 因同 batch 其他 pending call 被一起挂起：`permission_wait_for_user_batch_suspension`
- call 因同 batch workflow control tool 已接管执行而被 runtime suppress：`workflow_control_batch_suppressed`
- MCP input-required resume recheck 需要 approval，但 V1 resume 边界不支持二次 approval：`mcp_resume_permission_approval_unsupported`
- terminal command 命中 hardline deny：`hardline_terminal_command_blocked`
- terminal_process write/input/kill 等输入类动作命中 hardline deny：`hardline_terminal_process_input_blocked`

当前字符串 / 模式到 stable code 的 V1 映射表：

| 当前 reason 字符串或模式 | stable `reason_code` | `reason_message` |
|---|---|---|
| `capability_descriptor_missing` | `capability_descriptor_missing` | 原文或补充 message |
| `Unknown tool: ... (capability_descriptor_missing)` | `capability_descriptor_missing` | 原文 |
| `capability_hidden` | `capability_hidden` | 原文或补充 message |
| `capability_hidden_in_current_exposure` | `capability_hidden` | 原文 |
| `capability_unavailable` | `capability_unavailable` | 原文或补充 message |
| `capability_unavailable_in_current_exposure` | `capability_unavailable` | 原文 |
| `capability_not_callable` | `capability_not_callable` | 原文或补充 message |
| `capability_not_callable_in_current_exposure` | `capability_not_callable` | 原文 |
| permission gate 返回 `WAIT_FOR_USER` 且该 call 是触发 approval 的 call | `permission_wait_for_user` | 原文 |
| permission gate 返回 `WAIT_FOR_USER` 但该 call 只是同 batch 被挂起 | `permission_wait_for_user_batch_suspension` | 原文 |
| `tool call suppressed because workflow control tool ... owns this tool batch` | `workflow_control_batch_suppressed` | 原文 |
| MCP input-required resume recheck 返回 `WAIT_FOR_USER` | `mcp_resume_permission_approval_unsupported` | 原文，必须落成 `decision="deny"` |
| `terminal command blocked by hardline permission policy` | `hardline_terminal_command_blocked` | 原文 |
| `terminal process input blocked by hardline permission policy` | `hardline_terminal_process_input_blocked` | 原文 |
| 其他 permission deny message | `permission_denied` 或 `None` | 原文 |

Normalizer 不应只做 exact match；必须支持括号中的 code 后缀和 `_in_current_exposure` 这类当前实现字符串。无法可靠识别时，`reason_code=None`，`reason_message` 保留原文。

### 9.2 是否要同时升格 `capability_exposure_resolved`？

暂不。

`capability_exposure_resolved` 是 turn-level projection，payload 大且还在演化；`capability_gate_decision` 是 per-call final gate fact，更适合先升格。

### 9.3 是否要把 `capability_context` 也类型化？

暂不。

`capability_context` 目前承载 active skill attribution，未来可能扩展 CLI/MCP recipe guidance。外层事件先类型化，内层 attribution 可以后续再拆成 typed DTO。

## 10. 推荐结论

`capability_gate_decision` 应升格为 `CapabilityGateDecisionEvent`。

这是一个边界清晰、收益明确、风险较低的 event schema cleanup：

- 它把 permission/capability gate 的核心事实从 loose custom payload 变成 typed runtime event；
- 它让 inspect/replay 不再依赖字符串 name 分流；
- 它硬切新 inspector gate projection 到 typed event，不再读取旧 custom gate payload；
- 它的目标是不改变既有契约语义；如果实现时发现 batch reason 错配或 call-local deny 牵连兄弟工具，应按契约修正；
- 它为后续更强的权限审计、MCP capability debug、active skill attribution 展示打基础。
