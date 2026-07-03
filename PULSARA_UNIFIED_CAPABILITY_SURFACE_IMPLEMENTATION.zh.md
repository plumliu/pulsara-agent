# Pulsara Unified Capability Surface 实施设计

本文承接 `PULSARA_UNIFIED_CAPABILITY_SURFACE_RESEARCH.zh.md`，把调研结论落到当前 Pulsara agent runtime 的真实接线点上。目标是给后续编码提供可以按 PR 推进的细节设计，而不是停留在「统一 capability」这个抽象愿景。

核心判断：

- 不要把 Unified Capability Surface 做成更胖的 `ToolRegistry`。
- 先定义本地真值 `CapabilityDescriptor`，再由它派生模型可见 `ToolSpec`、执行 gate、artifact policy、inspector 解释。
- 注册、广告、授权、执行必须分层；「注册存在」不等于「本轮广告」不等于「执行允许」。
- V1 行为保持现有用户体验基本不变，但架构直接 hard cut：built-in tools 使用显式 descriptor truth，不从 `ToolRegistry` 或 `Tool` metadata 反推 descriptor。

## 0. 当前关键接线点

### 0.1 模型工具广告入口

当前 `src/pulsara_agent/runtime/context.py`：

```python
def build_llm_context(
    state: LoopState,
    registry: ToolRegistry,
    system_prompt: str | None,
    budget: LoopBudget,
) -> LLMContext:
    ...
    return LLMContext(
        system_prompt=prompt,
        messages=tuple(messages),
        tools=registry.tool_specs(),
    )
```

这意味着模型工具列表现在直接来自 `ToolRegistry.tool_specs()`。Unified Surface 的第一条硬接线就是把这里改成读取「本轮 exposure plan」：

```text
ToolRegistry / CapabilityRegistry
        ↓
CapabilityExposurePlan.tool_specs
        ↓
LLMContext.tools
```

不能继续让 `build_llm_context()` 自己从 registry 读全部工具，否则「registry 中存在」和「本轮广告」无法分离。

### 0.2 capability prompt 已有入口

当前 `src/pulsara_agent/runtime/agent.py` 在用户消息入口解析 capability exposure：

```python
exposure = self._resolve_capability_exposure(...)
state.scratchpad["capability_exposure"] = exposure
```

之后每次模型调用用：

```python
compose_system_prompt(
    self.system_prompt,
    memory_prompt=...,
    capability_prompt=exposure.catalog_prompt,
    active_skill_prompt=exposure.active_skill_prompt,
)
```

这已经建立了「一次用户消息 resolve 一次 capability prompt」的语义。新实现应保留这个节奏：exposure plan 也应该在用户消息边界 resolve 一次，并在该 run 内稳定，不要在工具执行后或下一次 model loop 中动态改工具 schema。

例外：approval / plan interaction resume 如果 scratchpad 中没有本轮 exposure，会 fallback resolve。这个路径必须使用同一 `CapabilityRuntime.resolve_for_turn(...)`，避免出现两套工具广告逻辑。

### 0.3 执行入口

当前 `src/pulsara_agent/tools/executor.py`：

- `ToolExecutor.execute(...)`
- `ToolExecutor.execute_async(...)`
- `_finalize_result(...)`

这里是工具执行和 `ToolResultStart/Delta/End` 事件的集中点，也是 artifact service 的调用点：

```python
if self.artifact_service is not None:
    result, artifact_refs = self.artifact_service.process_result(...)
```

Unified Surface 的 execution gate 有两个可选落点：

1. 保持 `AgentRuntime._execute_tool_blocks(...)` 里的 permission gate 是唯一 pre-execution gate。
2. 让 `ToolExecutor` 自身在执行前二次校验 descriptor。

推荐 V1 使用方案 1，原因：

- 当前 approval / WAITING_USER 状态机已经在 `AgentRuntime` 中。
- `ToolExecutor` 现在只知道单个 call，不能创建 pending approval。
- gate 需要读 `LoopState`、plan mode、permission state、pending interactions，这些都在 runtime 侧。

但 `ToolExecutor` 仍需要拿到 descriptor，用于：

- async/sync 判断。
- artifact policy。
- execution metadata。
- unknown tool error 中附带 capability diagnostics。

### 0.4 权限 gate 现状

当前 `src/pulsara_agent/runtime/permission.py` 的 `PolicyPermissionGate` 主要按工具名判断：

- read-only 下只允许 `READ_ONLY_ALLOWED_TOOL_NAMES`。
- `TERMINAL_TOOL_NAMES` 单独处理。
- `FILE_WRITE_TOOL_NAMES` 在 approval on-request 下要求确认。
- terminal command 再做 hardline / risky 检查。

这是 V0 必要的安全线，但它有几个问题：

- 新工具必须手动更新多个 name set。
- MCP/CLI/namespace tool 进入后，name set 会迅速漂移。
- `is_read_only` drift test 只能覆盖 built-in tool，不能覆盖 provider descriptor。
- artifact、timeout、open-world、destructive 等分类不在 gate 的统一输入里。

V1 要把 `PolicyPermissionGate` 改成 descriptor-driven，同时保留 name set 作为 legacy drift guard。

### 0.5 artifact policy 现状

当前 `ToolResultArtifactService` 只有全局阈值：

- `archive_threshold_chars`
- `inline_preview_chars`
- `tool_result_context_chars`

并且有一个 hard-coded 例外：

```python
if result.tool_name == "artifact_read":
    return result, ()
```

这能工作，但不是 unified policy。未来不同 capability 需要不同策略：

- terminal stdout/stderr：大输出强 artifact，preview inline。
- web/search/JSON：结构化 JSON artifact，model 只看摘要。
- memory_search：通常 inline，小心不要 artifact 掉可解释路径。
- binary/image：默认 artifact。
- `artifact_read`：不能再 artifact 自己。

因此 artifact policy 应从 descriptor 进入 `process_result(...)`，而不是全局猜。

### 0.6 tool construction 现状

当前 `src/pulsara_agent/runtime/session.py`：

```python
return ToolExecutor(
    registry=build_core_tool_registry(...),
    record_event=record_event,
    artifact_service=self.artifact_service,
    runtime_session_id=self.runtime_session_id,
)
```

`build_core_tool_registry(...)` 会无条件注册所有 built-in tools，并在注释中明确：

> gate is the sole authority. All tools are registered unconditionally and stay visible across every mode.

这个原则仍然对，但 wording 要改：以后是「registered unconditionally」，不再等于「visible across every mode」。为了 prefix cache 稳定，V1 可以继续让 built-in direct tools 在所有 permission mode 下稳定广告；但架构上要允许 future provider deferred/hidden。

## 1. 新核心对象

### 1.1 CapabilityDescriptor

建议新增 `src/pulsara_agent/capability/descriptor.py`。

字段先收窄到能真实消费的范围，不要一次性照搬 plugin manifest：

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

class CapabilityProviderKind(StrEnum):
    BUILTIN = "builtin"
    WORKFLOW = "workflow"
    MEMORY = "memory"
    SKILL = "skill"
    CLI = "cli"
    MCP = "mcp"

class CapabilityAvailability(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

class CapabilityAdvertisePolicy(StrEnum):
    DIRECT = "direct"
    DEFERRED = "deferred"
    HIDDEN = "hidden"

class CapabilityArtifactMode(StrEnum):
    DEFAULT = "default"
    NEVER = "never"
    ALWAYS = "always"
    LARGE_OUTPUT = "large_output"
    STRUCTURED_JSON = "structured_json"

@dataclass(frozen=True, slots=True)
class CapabilityProvenance:
    provider_kind: CapabilityProviderKind
    provider_id: str
    source: str | None = None
    version: str | None = None
    owner: str | None = None

@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    id: str
    name: str
    description: str
    input_schema: dict[str, Any] | None
    namespace: str | None
    provider_kind: CapabilityProviderKind
    provider_id: str
    is_model_callable: bool
    is_read_only: bool
    is_concurrency_safe: bool
    is_destructive: bool = False
    is_open_world: bool = False
    requires_user_interaction: bool = False
    permission_category: str = "general"
    approval_policy_hint: str | None = None
    advertise_policy: CapabilityAdvertisePolicy = CapabilityAdvertisePolicy.DIRECT
    artifact_mode: CapabilityArtifactMode = CapabilityArtifactMode.DEFAULT
    max_inline_chars: int | None = None
    timeout_ms: int | None = None
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE
    health_message: str | None = None
    provenance: CapabilityProvenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

字段语义：

- `id` 是内部稳定身份，例如 `builtin:terminal`、`memory:memory_search`、`mcp:github/issues.create`。
- `name` 是模型 tool call name；V1 built-in 可以保持现有 name。
- `namespace` 为未来 MCP/CLI namespace 预留。
- `is_model_callable=False` 可表达 skill catalog 这类 prompt capability。
- `advertise_policy` 只决定模型是否看到；执行时仍必须 gate。
- `availability != AVAILABLE` 时默认不 direct advertise，除非 exposure planner 明确降级展示。

### 1.2 CapabilityRegistry

建议新增 `src/pulsara_agent/capability/registry.py`。

职责：

- 保存 descriptor snapshot。
- 按 `id` 和 `name` 建索引。
- duplicate / shadowing fail-closed。
- 维护 generation。
- 输出 diagnostics。

接口草案：

```python
@dataclass(frozen=True, slots=True)
class CapabilityRegistrySnapshot:
    generation: int
    descriptors: tuple[CapabilityDescriptor, ...]
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()

class CapabilityRegistry:
    def register(self, descriptor: CapabilityDescriptor) -> None: ...
    def get_by_name(self, name: str) -> CapabilityDescriptor: ...
    def get_by_id(self, id: str) -> CapabilityDescriptor: ...
    def snapshot(self) -> CapabilityRegistrySnapshot: ...
```

V1 duplicate 策略：

- 同 `id` 重复：如果 descriptor 完全相同，幂等；不同则 error。
- 同 `name` 不同 `id`：默认 error，不允许 silent shadow。
- 后续 MCP namespace 可以通过 model-name mangling 避免同名。

### 1.3 CapabilityCallClassifier

只靠 tool-level descriptor 不足以表达所有权限语义。当前最重要的反例是 `terminal_process`：

- tool-level `is_read_only=False`，因为它可以 `write/submit/kill/close_stdin`。
- 但 `list/log/poll/wait` 在非 read-only 的 `terminal=ask` 或 `approval=on_request` 下必须直接 `ALLOW`，不能弹确认。
- 契约 `PERMISSION_POLICY_CONTRACT.zh.md` 已经把这个 action-level 豁免写死；测试 `test_policy_gate_terminal_process_read_only_actions_do_not_wait_under_ask_or_on_request` 也在守。

因此 V1 必须新增一个 call-level classification 层，而不是让 descriptor 直接决定单次调用的最终类别。

建议新增 `src/pulsara_agent/capability/call_classifier.py`：

```python
@dataclass(frozen=True, slots=True)
class CapabilityCallClassification:
    descriptor_id: str
    tool_name: str
    effective_read_only: bool
    effective_concurrency_safe: bool
    effective_permission_category: str
    effective_is_destructive: bool
    effective_is_open_world: bool
    approval_reason: str | None = None
    deny_reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class CapabilityCallClassifier(Protocol):
    def classify(
        self,
        call: ToolCall,
        descriptor: CapabilityDescriptor,
    ) -> CapabilityCallClassification:
        ...
```

默认 classifier 行为：

- 直接继承 descriptor 的 `is_read_only`、`is_concurrency_safe`、`permission_category`、`is_destructive`、`is_open_world`。

内置特例：

- `terminal_process` action in `list/log/poll/wait`：
  - `effective_permission_category="terminal_process_observe"`
  - `effective_read_only=True`
  - `effective_is_destructive=False`
  - `approval_reason=None`
  - `effective_concurrency_safe` V1 仍继承 descriptor，默认不并发；观察动作是否可并发是后续优化，不应混入本 PR。
- `terminal_process` action in `write/submit/kill/close_stdin`：
  - `effective_permission_category="terminal"`
  - `effective_read_only=False`
  - 继续走 terminal ask/on-request/hardline 规则。
- 未来 CLI provider 可以按 subcommand / argv template 细分。
- 未来 MCP provider 可以按 server tool annotations / destructive hints 细分；缺失 hints 时继承 fail-closed descriptor 默认。

注意：call classifier 不能把 read-only profile 下的 `terminal_process` 整体打开。当前契约明确 read-only 下 `terminal_process` 仍整体被拦；action-level observe 豁免只适用于 profile 已允许 terminal 存在的非 read-only 模式。

### 1.4 Execution binding

Descriptor 不是 executor。未来 CLI/MCP provider 如果只注册 descriptor，会出现「能被 advertise/gate，但 `ToolExecutor.registry.get(name)` 找不到可执行对象」的半吊子状态。

V1 明确采用现有 `ToolRegistry` 作为 execution binding registry：

- 每个 `is_model_callable=True` 且 `advertise_policy != HIDDEN` 的 executable capability，必须有同名 `Tool | AsyncTool` 注册到 `ToolRegistry`。
- Provider adapter 必须从同一份 provider snapshot 同时产出：
  - `CapabilityDescriptor`
  - `Tool | AsyncTool` execution adapter
- `CapabilityRegistry` / exposure planner 要检查 descriptor 与 execution binding 是否一致：
  - descriptor 有、ToolRegistry 没有：fail-closed diagnostic；不得 direct advertise。
  - ToolRegistry 有、descriptor 没有：PR 0 以后视为 drift；短期可通过 mirror adapter 兜底，最终禁止。
  - schema 不一致：diagnostic + test failure。

因此 PR 6/7 的 CLI/MCP provider 不是「只产 descriptor」：

- CLI provider 产出一个 `CliCapabilityTool` / `AsyncCliCapabilityTool` adapter，并注册到 `ToolRegistry`。
- MCP provider 产出一个 `McpCapabilityTool` / `AsyncMcpCapabilityTool` adapter，并注册到 `ToolRegistry`。
- 这两个 adapter 仍由 `ToolExecutor` 调用；permission、artifact、trace 仍走统一 capability path。

后续如果需要更干净的 `CapabilityExecutorRegistry`，可以在 descriptor/gate/artifact 稳定后迁移。但 V1 不引入第二套执行 registry，避免双执行路径。

### 1.5 BuiltinToolCapabilityProvider：显式 descriptor truth

不保留 `ToolRegistry` mirror adapter。built-in capability 的声明真值由 `BuiltinToolCapabilityProvider` 输出，schema、permission category、artifact policy、workflow category、open-world/destructive 等语义都写在 capability 层。

`ToolRegistry` 只提供 execution binding 校验：

- descriptor 有、ToolRegistry 没有：fail-closed diagnostic，不 direct advertise。
- ToolRegistry 有、descriptor 没有：drift diagnostic，模型不可调用。
- schema / category 漂移：测试失败，不走运行时反推兜底。

初始映射：

| 当前 tool | provider_kind | permission_category | artifact_mode | destructive/open-world |
| --- | --- | --- | --- | --- |
| `read_file`, `search_files`, `artifact_read` | builtin | filesystem_read / artifact_read | default / never | false |
| `write_file`, `edit_file` | builtin | filesystem_write | default | destructive=true |
| `terminal`, `terminal_process` | builtin | terminal | large_output | open_world=true |
| `memory_search`, `memory_get`, `memory_explain` | memory | memory_read | default | false |
| `remember_*` | memory | memory_write | default | destructive=false, read_only=false |
| `todo` | workflow | agent_local | default | read_only=true |
| `enter_plan`, `ask_plan_question`, `exit_plan` | workflow | plan_workflow | default | false |

这会和 built-in tool class 上的 `description/parameters/is_read_only` 短期重复，但重复是刻意的：descriptor 是唯一声明真值，Tool object metadata 只服务 execution binding 的本地实现。

Workflow control tools 的特殊规则：

- `enter_plan` / `ask_plan_question` / `exit_plan` 必须有 descriptor，用于 exposure、inspector、drift tests。
- 但执行仍由 `AgentRuntime._execute_tool_blocks(...)` 里的 workflow control plane 截获，发生在普通 permission gate 前。
- descriptor missing 应该是 diagnostic / test failure；不能因为 descriptor 缺失就让这些工具落到普通 `ToolExecutor` 路径。
- 任一 batch 中出现 workflow control tool 时，仍保持现有规则：交给 `_handle_workflow_tool_batch(...)`，并阻断同批 sibling write tool。

### 1.6 CapabilityExposurePlan

新增 `src/pulsara_agent/capability/exposure.py`。

```python
@dataclass(frozen=True, slots=True)
class CapabilityExposurePlan:
    registry_generation: int
    direct_tool_specs: tuple[ToolSpec, ...]
    direct_names: frozenset[str]
    deferred_names: frozenset[str]
    hidden_names: frozenset[str]
    callable_names: frozenset[str]
    descriptors_by_name: Mapping[str, CapabilityDescriptor]
    catalog_prompt: str | None
    active_skill_prompt: str | None
    diagnostics: tuple[CapabilityDiagnostic, ...]
```

V1 行为：

- built-in model-callable tools 默认 `DIRECT`。
- unavailable descriptors 不进 direct tool specs。
- local skills 进入 catalog prompt，但 `is_model_callable=False`，不进 tool specs。
- read-only / plan mode 暂不隐藏 mutating tools，继续 visible-but-blocked，以维持 prompt prefix cache 和现有权限契约。
- future MCP/CLI 可选择 `DEFERRED`。
- `callable_names` 是执行 gate 的输入。V1 默认等于 `direct_names`；未来 deferred discovery / activation 可以把已激活的 deferred capability 加入 `callable_names`，但 hidden/unavailable capability 不得进入。

### 1.7 CapabilityRuntime

新增一个轻量 facade，比如 `src/pulsara_agent/capability/runtime.py`：

```python
class CapabilityRuntime:
    def resolve_for_turn(
        self,
        context: CapabilityResolveContext,
        *,
        tool_registry: ToolRegistry,
        permission_policy: EffectivePermissionPolicy,
        plan_active: bool,
    ) -> CapabilityExposurePlan:
        ...
```

它聚合：

- `BuiltinToolCapabilityProvider`。
- `LocalSkillCapabilityProvider`（仅当 workspace skills 开启）。
- exposure planner。
- diagnostics。

不要让 `AgentRuntime` 同时手动调用 skill resolver、ToolRegistry adapter、MCP adapter；所有 provider 都必须进入 `CapabilityRuntime.providers`。

## 2. AgentRuntime 接线设计

### 2.1 AgentRuntime 初始化

`AgentRuntime.__init__` hard cut 后只接收：

```python
capability_runtime: CapabilityRuntime
```

不保留旧 resolver、旧 resolved set、旧 noop resolver 兼容入口。production wiring 必须显式构造 `CapabilityRuntime`；测试 fake tool 也必须显式提供 descriptor provider。

### 2.2 `_resolve_capabilities` 改为产生 exposure plan

resolve 结果统一为：

```python
def _resolve_capability_exposure(...) -> CapabilityExposurePlan:
    return self.capability_runtime.resolve_for_turn(...)
```

关键点：

- resolve 一次，写 `state.scratchpad["capability_exposure"]`。
- approval / plan interaction resume 使用同一 scratchpad。
- 如果 scratchpad 缺失，fallback resolve 要记录 diagnostic，避免悄悄换工具集。

### 2.3 `build_llm_context` 签名改造

当前签名：

```python
build_llm_context(state, registry, system_prompt, budget)
```

推荐改为：

```python
build_llm_context(
    state: LoopState,
    *,
    tools: tuple[ToolSpec, ...],
    system_prompt: str | None,
    budget: LoopBudget,
) -> LLMContext
```

这样它不再知道 `ToolRegistry`，只消费已经决定好的 tools。

调用点：

```python
context = build_llm_context(
    state=state,
    tools=exposure.direct_tool_specs,
    system_prompt=compose_system_prompt(...),
    budget=self.budget,
)
```

这是最关键的依赖反转：context builder 不再拥有广告决策权。

### 2.4 Tool loop 并发判断

当前 `runtime/tool_loop.py`：

```python
tool = executor.registry.get(call.name)
return bool(tool.is_read_only and tool.is_concurrency_safe)
```

第一步改为：

```python
descriptor = exposure.descriptors_by_name.get(call.name)
return bool(descriptor and descriptor.is_read_only and descriptor.is_concurrency_safe)
```

注意：

- Unknown tool 不并发。
- Descriptor 缺失应产生 tool error 或 gate deny，而不是 fallback 读 Tool 对象。
- 这能保证 provider adapter 的安全分类统一生效。
- V1 不把 `terminal_process list/log/poll/wait` 自动并发化。call classifier 可以把它们标为 observe/read-only 以服务 permission gate，但并发安全仍按 descriptor 默认值，避免 process registry / terminal supervisor 出现新的并发风险。

后续如果需要 per-call 并发分类，可以把 `_tool_batches()` 改为读取 `CapabilityCallClassification.effective_concurrency_safe`。这个 refinement 必须单独测试 terminal manager / ProcessRegistry 的线程安全。

### 2.5 Permission gate 改造

不要一次性删除 `READ_ONLY_ALLOWED_TOOL_NAMES`，否则风险太大。建议两阶段：

阶段 A：新增 descriptor-aware gate，但 legacy name sets 作为 cross-check。

```python
class PolicyPermissionGate:
    async def evaluate(
        self,
        calls: list[ToolCall],
        *,
        descriptors_by_name: Mapping[str, CapabilityDescriptor] | None = None,
    ) -> PermissionDecision:
        ...
```

或新增 `CapabilityPermissionGate` 包住现有 gate。

推荐不直接改 Protocol 签名太多，而是在 `AgentRuntime._execute_tool_blocks(...)` 里调用新 gate helper：

```python
decision = evaluate_capability_permission(
    calls,
    policy=self.permission_policy,
    exposure=exposure,
    legacy_gate=self.permission_gate,
)
```

阶段 A 规则：

- unknown descriptor：deny，reason=`capability_descriptor_missing`。
- `descriptor.availability == UNAVAILABLE`：deny，reason=`capability_unavailable`。
- `call.name in exposure.hidden_names`：deny，reason=`capability_hidden_in_current_exposure`。hidden/unavailable 不能只靠“不广告”防御，因为模型可能幻觉调用已有名字。
- 不在本轮可调用集合内：deny，reason=`capability_not_callable_in_current_exposure`。本轮可调用集合读取 `exposure.callable_names`；V1 默认等于 `direct_names`。未来 deferred capability 只有在 deferred discovery / activation 后被加入 callable set，才允许执行。
- 调用 `CapabilityCallClassifier.classify(call, descriptor)`，得到 per-call effective classification。
- `policy.profile == READ_ONLY`：V1 仍按 tool-level read-only contract fail-closed，只允许 `descriptor.is_read_only=True` 且通过 legacy drift guard 的工具；不要因为 `terminal_process list/log/poll/wait` 的 call-level observe classification 就在 read-only 下放行。read-only 打开 terminal_process observe 是后续单独契约精化。
- 如果 descriptor.name 在 legacy read-only allowlist 与 descriptor.is_read_only 不一致：deny 或 emit drift error。推荐测试环境 fail；生产可 deny with drift message。
- `classification.effective_permission_category == "terminal_process_observe"`：仅在 `policy.profile != READ_ONLY and policy.terminal is not TerminalAccess.OFF` 时直接 ALLOW，不受 `terminal=ask` / `approval=on_request` 影响；`terminal=off` 仍 DENY。这保住现有 `list/log/poll/wait` 契约，同时不打开 custom terminal-off 策略。
- `classification.effective_permission_category == "terminal"`：继续调用 terminal hardline/risky/ask/on-request 检查。
- `classification.effective_is_destructive` 且 approval on-request：WAIT_FOR_USER。
- `classification.effective_permission_category == "filesystem_write"` 且 approval on-request：WAIT_FOR_USER。
- `classification.effective_is_open_world` 且 terminal/off：deny 或 ask，按 terminal policy。
- workflow control tools 不走普通 gate。它们在 `AgentRuntime._execute_tool_blocks(...)` 中先于 permission gate 被截获；descriptor 只用于 exposure/inspector/drift。

阶段 B：legacy name sets 只作为 tests，不参与 runtime。

### 2.6 ToolExecutor descriptor 接入

`ToolExecutor` 保留执行 registry，但新增可选 descriptor lookup：

```python
descriptors_by_name: Mapping[str, CapabilityDescriptor] = field(default_factory=dict)
```

在每次 run / turn 开始前更新：

- 方案 1：`ToolExecutor` mutable setter。
- 方案 2：执行工具时把 descriptor 显式传入。

推荐方案 2，减少跨 run mutable state：

```python
executor.execute(call, event_context=..., descriptor=descriptor)
executor.execute_async(call, event_context=..., descriptor=descriptor)
```

如果改动太大，可先在 `ToolExecutor` 上设置当前 exposure plan，但必须保证 run 间不会串：

- `AgentRuntime` 是 session 级对象。
- 同一个 HostSession 不应并发 run；如果将来允许并发，mutable executor state 会出事。

因此文档推荐 explicit parameter。

### 2.7 Artifact service descriptor 接入

`ToolResultArtifactService.process_result(...)` 改为：

```python
def process_result(
    self,
    result: ToolExecutionResult,
    *,
    event_context: EventContext,
    tool_call: ToolCall,
    descriptor: CapabilityDescriptor | None = None,
) -> ...
```

V1 行为：

- `descriptor.artifact_mode == NEVER`：不 archive。
- `ALWAYS`：如果没有 candidates，则把 output 作为 candidate。
- `LARGE_OUTPUT`：现有 threshold 行为。
- `STRUCTURED_JSON`：media type 使用 `application/json`，保留完整 JSON artifact，inline preview 可摘要。
- `DEFAULT`：现有行为。

把 `artifact_read` hard-coded 例外迁到 descriptor：

- `artifact_read.artifact_mode = NEVER`

### 2.8 Event / observability

不建议 PR 0/1 就新增大量事件。但只靠 scratchpad / runtime diagnostic 不足以满足「resume / inspect 能解释历史选择」的目标，因为 `LoopState.scratchpad` 不是 durable event fact。

因此推荐：

- PR 1 可以先只做内存 diagnostics，降低接线风险。
- PR 5 必须新增 durable event 或等价 event-log projection；不能只靠 inspect-time 静态快照。

建议新增 typed events：

```python
CapabilityExposureResolvedEvent
CapabilityGateDecisionEvent
CapabilityProviderHealthEvent
```

事件字段不要存完整 schema，避免 event log 膨胀。最小字段：

- `CapabilityExposureResolvedEvent`
  - `generation`
  - `direct_descriptor_ids`
  - `deferred_descriptor_ids`
  - `hidden_descriptor_ids`
  - `diagnostics`
  - `provider_summaries`
- `CapabilityGateDecisionEvent`
  - `tool_call_id`
  - `tool_name`
  - `descriptor_id`
  - `decision`
  - `reason_code`
  - `classification`
  - `policy_mode`
- `CapabilityProviderHealthEvent`
  - `provider_id`
  - `provider_kind`
  - `availability`
  - `health_message`

PR 5 的验收线是：inspect run/session 时能从 event log 解释历史上某个 run 的工具为什么被 direct advertised、为什么被 gate deny / ask / allow，而不是只解释「当前 workspace 静态状态」。

## 3. Wiring 设计

### 3.1 AgentRuntime composition root

CapabilityRuntime 不属于 `build_durable_runtime_wiring(...)`。后者只负责 durable storage / memory / artifact / terminal 这些 runtime resources。

实际的 composition root 是 `build_agent_runtime_wiring(...)`：

- 它先调用 `build_durable_runtime_wiring(...)` 或 `build_in_memory_runtime_wiring(...)` 得到 `RuntimeWiring`。
- 然后构造 `llm_runtime`。
- 然后创建 `AgentRuntime(...)`。
- `CapabilityRuntime` 也在这里构造；`enable_workspace_skills` 只决定是否加入 `LocalSkillCapabilityProvider`。

因此 V1 应在 `build_agent_runtime_wiring(...)` 注入 `CapabilityRuntime`：

```python
agent_runtime = AgentRuntime(
    ...,
    capability_runtime=capability_runtime
    if capability_runtime is not None
    else CapabilityRuntime.with_default_providers(
        *(LocalSkillCapabilityProvider(),) if enable_workspace_skills else ()
    ),
)
```

`build_durable_runtime_wiring(...)` 仍只负责：

- real PostgreSQL
- real Oxigraph
- memory query / recall / proposal sink
- artifact index

注意：Builtin provider 不应该自己创建 tools；它只从 `AgentRuntime` 已有的 `ToolExecutor.registry` 镜像 descriptor。否则会出现 registry 与 descriptor 双构造漂移。

### 3.2 RuntimeSession.create_tool_executor

当前这里创建 core registry：

```python
registry=build_core_tool_registry(...)
```

V1 不建议在这里创建 capability registry，因为 capability resolve 需要：

- user_input
- prior_messages
- active_skill_names
- permission policy
- plan state

这些都不在 `RuntimeSession` 中。

因此 `RuntimeSession` 仍只负责创建 `ToolExecutor` 和 underlying `ToolRegistry`。CapabilityRuntime 由 `AgentRuntime` 拿这个 registry 做 per-turn resolve。

### 3.3 CLI inspect

当前 `pulsara inspect workspace capability` 会直接 build registry，然后 resolver.resolve。

V1 inspect 应改为：

```text
build registry
build CapabilityRuntime
resolve exposure plan under inspect/read-only policy
print:
  providers
  descriptors
  direct/deferred/hidden
  diagnostics
  active skill prompt info
```

不要让 inspect 只显示当前静态 ToolRegistry，否则历史 exposure / gate 选择没有可视化。

## 4. CLI adapter 设计边界

CLI adapter 不在第一批 PR 实装，但 descriptor 要能承载它。

CLI capability 必填 descriptor 字段：

- `command`
- `argv_template`
- `cwd_policy`
- `env_policy`
- `stdin_policy`
- `timeout_ms`
- `max_inline_chars`
- `artifact_mode`
- `is_read_only`
- `is_open_world`
- `permission_category`

CLI provider 还必须注册 execution adapter：

```python
class CliCapabilityTool:
    name: str
    description: str
    parameters: dict[str, object]
    is_read_only: bool
    is_concurrency_safe: bool

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        ...
```

要求：

- `CliCapabilityTool.name == CapabilityDescriptor.name`。
- `CliCapabilityTool.parameters == CapabilityDescriptor.input_schema` 的 model-callable 投影。
- tool 与 descriptor 必须由同一 config entry 生成，不能手写两份。
- execution adapter 只执行固定 binary / argv template，不允许把任意 shell string 交给 `/bin/sh -c`，除非 descriptor 明确 `permission_category="terminal"` 且走 terminal hardline。
- stdout/stderr 统一返回 `ToolExecutionResult` 和 artifact candidates，不直接写 event log。

默认：

- `is_read_only=False`
- `is_concurrency_safe=False`
- `is_open_world=True`
- `requires_user_interaction=False`
- `artifact_mode=LARGE_OUTPUT`
- interactive stdin 禁止

重要边界：

- CLI adapter 不等于 terminal。
- 不能因为 CLI command 看起来安全，就绕过 terminal hardline。
- 如果 CLI adapter 执行 shell command，应视为 terminal category。
- 如果 CLI adapter 执行固定 binary + fixed argv template，可是独立 category，但仍要 timeout/artifact/env/cwd policy。

## 5. MCP adapter 设计边界

MCP adapter 也不建议第一批直接实装，但 descriptor 需预留：

- server id
- tool name
- namespace
- input schema
- availability / auth state
- destructive / open-world hints
- elicitation support
- provider lifecycle owner

MCP provider 也必须注册 execution adapter：

```python
class McpCapabilityTool:
    name: str
    description: str
    parameters: dict[str, object]
    is_read_only: bool
    is_concurrency_safe: bool

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        ...
```

要求：

- adapter 不拥有 MCP client；它只借用 provider-owned client/session。
- provider owner 必须接入 HostSession / HostCore shutdown 链。
- adapter 将 MCP result 规范化为 `ToolExecutionResult`，大 payload 通过 artifact candidates 交给 `ToolResultArtifactService`。
- MCP elicitation 不得在 adapter 内阻塞 stdin；必须转成 Pulsara pending interaction。

默认策略：

- MCP tools 数量少时可 direct advertise。
- 数量多时 deferred。
- unknown destructive/open-world classification 时 fail-closed：`is_read_only=False`、`is_open_world=True`。
- MCP elicitation 走 pending interaction，不允许 provider 私下阻塞 CLI。

最重要的坑：

- MCP client 是 async resource，必须归属于 HostSession/HostCore 或 provider owner。
- 不能跨 worker thread/event loop 共享 async client。
- shutdown 要幂等、有界 drain。

## 6. Skill 设计边界

现有 local skill 做得比较窄，这是对的。Unified Surface 不应让 skill 直接变成工具。

V1 中：

- Skill descriptor `is_model_callable=False`。
- Skill 进入 catalog prompt。
- full `SKILL.md` 通过 active injection 或未来 `skill_view` 加载。
- `provides_tools` 只能引用已有 descriptor names，用于 prompt/diagnostic，不改变 permission。

如果未来要让模型主动调用 skill，推荐单一 wrapper：

```text
skill_view(name, path?)
skill_use(name, args?)
```

不要一 skill 一 tool schema。

## 7. Inspector / diagnostics 要求

新增或扩展 inspect 输出时，应能解释：

- provider 列表和 generation。
- descriptor count。
- direct/deferred/hidden count。
- 某 capability 的 provider/provenance/source。
- 某 capability 为什么不可见。
- read-only 下为什么某工具 visible-but-blocked。
- descriptor 与 legacy name set 是否漂移。
- artifact policy 是什么。

建议 CLI 输出 JSON 中新增：

```json
{
  "capability_surface": {
    "generation": 3,
    "providers": [...],
    "direct": [...],
    "deferred": [...],
    "hidden": [...],
    "diagnostics": [...]
  }
}
```

## 8. 测试矩阵

### 8.1 Descriptor mirror

- 每个 built-in tool 都有 descriptor。
- descriptor name 与 registry name 一一对应。
- 每个 model-callable descriptor 都有同名 Tool/AsyncTool execution binding。
- 每个 ToolRegistry tool 都有 descriptor；PR 0 的 mirror adapter 是唯一允许的临时兜底。
- duplicate name fail-closed。
- descriptor `is_read_only` 与工具对象 `is_read_only` 一致。
- descriptor `is_concurrency_safe` 与工具对象一致。
- legacy `READ_ONLY_ALLOWED_TOOL_NAMES` 与 descriptor read-only 集合一致，允许明确 carve-out 的 `terminal_process` read-only actions 单独测试。

### 8.2 Exposure

- built-in tools 默认 direct advertise，工具列表与当前行为一致。
- unavailable descriptor 不 direct advertise。
- non-callable skill 不进入 `LLMContext.tools`。
- active skill prompt 仍按 `$skill` / `--skill` 注入。
- plan/read-only mode 不改变 V1 built-in direct tools 列表，但 gate 仍阻断。
- exposure plan 在一个 user turn 内稳定；工具执行后下一轮 model call 使用同一 plan。

### 8.3 Context builder

- `build_llm_context()` 不再读 registry。
- 它只消费传入 `tools`。
- memory projection/system prompt/compaction/recovery 不受影响。

### 8.4 Permission gate

- hidden capability 被模型幻觉调用时 DENY，reason=`capability_hidden_in_current_exposure`。
- unavailable capability 被模型幻觉调用时 DENY，reason=`capability_unavailable`。
- 不在本轮 callable set 的 descriptor 被调用时 DENY，reason=`capability_not_callable_in_current_exposure`。
- deferred capability 未经 deferred discovery / activation 时不在 callable set，直接调用 DENY。
- read-only 下 descriptor `is_read_only=False` 的工具被 deny。
- read-only 下 `terminal_process list/log/poll/wait` 仍被 deny，保持当前契约；不要被 call-level observe classification 误放行。
- ask/on-request 下 `terminal_process list/log/poll/wait` 直接 ALLOW，不触发 WAIT_FOR_USER。
- `trusted_host + terminal=off + terminal_process list` DENY；observe action 只跳过 ask/on-request，不绕过 terminal-off。
- ask/on-request 下 `terminal_process write/submit/kill/close_stdin` 仍按 terminal side-effect action WAIT_FOR_USER。
- terminal hardline 仍优先 deny。
- terminal ask policy 仍 WAIT_FOR_USER。
- file write on-request 仍 WAIT_FOR_USER。
- descriptor missing deny。
- descriptor/legacy taxonomy drift 触发 test failure。
- plan mode 仍保持 read-only，不被 `:mode bypass-permissions` 绕过。
- workflow control tools 在 permission gate 前被 runtime workflow plane 截获；普通 gate 不接管 `enter_plan/ask_plan_question/exit_plan`。

### 8.5 Tool loop

- concurrency batching 使用 descriptor。
- read-only + concurrency-safe 的工具并发。
- read-only 但 non-concurrency-safe 不并发。
- `terminal_process list/log/poll/wait` 即使 call-level observe/read-only，V1 仍不自动并发，除非 descriptor 显式 concurrency-safe。
- descriptor missing 不并发并最终被 gate deny。

### 8.6 Artifact policy

- `artifact_read` descriptor `artifact_mode=NEVER`，不会 archive 自己。
- terminal large output 仍 artifact。
- default artifact 阈值行为不变。
- descriptor `ALWAYS` 即使小输出也可 archive。
- descriptor `STRUCTURED_JSON` 保持 JSON artifact metadata。

### 8.7 Inspector

- `inspect workspace capability` 输出 provider/descriptors/direct/hidden/diagnostics。
- degraded/unavailable provider 显示 health message。
- shadow/duplicate 诊断可见。
- active skill diagnostics 可见。
- run/session inspect 能从 event log 解释历史 exposure 和 gate decision；不能只展示当前静态 workspace 快照。

### 8.8 Execution binding

- descriptor direct advertise 但 ToolRegistry 缺 execution binding：不 direct advertise，并产出 diagnostic。
- ToolRegistry 有 tool 但 descriptor 缺失：PR 0 mirror adapter 可兜底；PR 5 后测试应 fail。
- CLI fake provider 同一 config entry 生成 descriptor + `CliCapabilityTool`。
- MCP mock provider 同一 server snapshot 生成 descriptor + `McpCapabilityTool`。
- schema drift：descriptor input schema 与 execution adapter parameters 不一致时 fail。

### 8.9 Durable events / observability

- `CapabilityExposureResolvedEvent` 不包含完整 schema，只包含 ids/counts/diagnostics/provider summaries。
- `CapabilityGateDecisionEvent` 能关联 `tool_call_id`、descriptor id、decision、reason、call classification。
- resume 后 inspector 仍能解释旧 run 的 direct/deferred/hidden 和 gate decision。

### 8.10 Regression / real LLM

- 常规 REPL 仍能读文件、写文件、执行 terminal。
- read-only / plan mode 下模型仍看到工具但写入被阻断。
- plan mode human-in-the-loop 不受影响。
- memory_search / memory_get schema 不变。
- real LLM dogfood：模型在 read-only 中尝试写文件时得到阻断并能解释原因。

## 9. 推荐 PR 顺序

### PR 0：显式 descriptor truth，不改变用户行为

目标：

- 新增 `CapabilityDescriptor`、enums、provenance。
- 新增 `BuiltinToolCapabilityProvider` 显式输出 built-in descriptors。
- 新增 execution binding drift 检查：每个 model-callable descriptor 必须能在 `ToolRegistry` 找到同名 Tool/AsyncTool。
- 新增 drift tests。
- 不改变 `LLMContext.tools`、permission gate、artifact 行为。

落点：

- `src/pulsara_agent/capability/descriptor.py`
- `src/pulsara_agent/capability/builtin_provider.py`
- tests for explicit descriptor truth

完成标准：

- 所有 built-in tools 都有 descriptor。
- descriptor 不从 `Tool.is_read_only/is_concurrency_safe` 反推。
- descriptor 与 ToolRegistry execution binding 一致。
- ToolRegistry 只负责 execution binding。

### PR 1：CapabilityExposurePlan 接入模型广告

目标：

- 新增 `CapabilityExposurePlan` 和 `CapabilityRuntime`。
- `AgentRuntime` 在 user turn 入口 resolve exposure plan。
- `build_llm_context()` 改为消费 `tools=exposure.direct_tool_specs`。
- V1 direct tools 与当前工具列表保持一致。

落点：

- `src/pulsara_agent/capability/exposure.py`
- `src/pulsara_agent/capability/runtime.py`
- `src/pulsara_agent/runtime/context.py`
- `src/pulsara_agent/runtime/agent.py`

完成标准：

- 行为上工具列表不变。
- context builder 不再依赖 `ToolRegistry`。
- exposure plan 在同一 user turn 内稳定。
- skill catalog/active injection 仍正常。

### PR 2：Descriptor-driven permission gate

目标：

- permission evaluation 使用 descriptor。
- permission evaluation 接收完整 `CapabilityExposurePlan`，检查当前 exposure / callable set / availability。
- 新增 `CapabilityCallClassifier`，让 per-call action/subcommand 分类覆盖 tool-level 默认值。
- legacy name sets 降为 drift guard。
- 保留 terminal hardline / risky / approval 语义。
- workflow control tools 明确在普通 permission gate 前由 runtime workflow plane 截获。

落点：

- `src/pulsara_agent/runtime/permission.py`
- `src/pulsara_agent/capability/call_classifier.py`
- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/runtime/tool_taxonomy.py` tests

完成标准：

- hidden/unavailable/not-callable-in-current-exposure 的 capability 幻觉调用会 DENY。
- read-only 由 descriptor 控制。
- read-only 下 `terminal_process` observe actions 不被误放行。
- ask/on-request 下 `terminal_process list/log/poll/wait` 直接 ALLOW。
- terminal=off 下 `terminal_process list/log/poll/wait` DENY。
- terminal/file write approval 行为不变。
- descriptor missing fail-closed。
- plan mode 仍强制 read-only。

### PR 3：Tool loop / executor descriptor threading

目标：

- `_tool_batches()` 使用 descriptor 判断并发。
- `ToolExecutor` 执行路径能拿到 descriptor。
- unknown/missing descriptor 的错误更结构化。

落点：

- `src/pulsara_agent/runtime/tool_loop.py`
- `src/pulsara_agent/tools/executor.py`
- `src/pulsara_agent/runtime/agent.py`

完成标准：

- 并发策略从 descriptor 读取。
- ToolExecutor 不再需要猜安全分类。
- 现有 tool execution tests 通过。

### PR 4：Artifact policy descriptor 化

目标：

- `ToolResultArtifactService` 读取 descriptor artifact policy。
- 去掉 `artifact_read` hard-coded 特例，迁移到 descriptor。
- 支持 default/never/always/large_output/structured_json。

落点：

- `src/pulsara_agent/runtime/tool_artifacts.py`
- `src/pulsara_agent/capability/descriptor.py`
- artifact tests

完成标准：

- terminal 大输出、artifact_read、默认工具行为保持。
- 新 policy 有单测。
- inspector 能显示 artifact policy。

### PR 5：Inspector / diagnostics surface

目标：

- `inspect workspace capability` 输出新 capability surface。
- 可解释 direct/deferred/hidden、provider、health、diagnostics。
- 新增 durable capability event 或等价 event-log projection；不能只靠 scratchpad。
- run/session inspect 能解释历史 exposure 和 gate decision。

落点：

- `src/pulsara_agent/cli.py`
- `src/pulsara_agent/inspector/*` 如需要
- `src/pulsara_agent/event/*`
- `src/pulsara_agent/runtime/timeline.py` 如需要

完成标准：

- CLI JSON 能看 provider/descriptors/exposure。
- duplicate/unavailable/degraded 诊断可测试。
- `CapabilityExposureResolvedEvent` 或等价 typed event 可从 event log replay。
- `CapabilityGateDecisionEvent` 或等价 typed event 可解释 tool call 被 allow/ask/deny 的历史原因。

### PR 6：CLI provider prototype

目标：

- 接一个最小 CLI provider，但默认不开。
- 通过静态 config 同时注册固定命令 descriptor 和 `CliCapabilityTool` execution adapter。
- 验证 cwd/env/timeout/artifact/read-only/open-world 策略。

落点：

- `src/pulsara_agent/capability/providers/cli.py`
- `src/pulsara_agent/tools/adapters/cli.py` 或 provider 内部 adapter
- config/settings
- tests with fake command

完成标准：

- CLI descriptor 与 `CliCapabilityTool` 由同一 config entry 生成。
- advertised CLI capability 可真实执行。
- CLI capability 不走 terminal tool name。
- 但 open-world/terminal-like command 仍受 gate 约束。
- timeout 和 artifact policy 生效。

### PR 7：MCP provider design spike / mock provider

目标：

- 先做 mock MCP provider，不直接接真实外部 MCP server。
- 验证 namespace、deferred exposure、health、schema mapping。
- 同时验证 descriptor 和 `McpCapabilityTool` execution adapter 的绑定关系。
- 为真实 MCP client lifecycle 留 owner seam。

落点：

- `src/pulsara_agent/capability/providers/mcp.py`
- `src/pulsara_agent/tools/adapters/mcp.py` 或 provider 内部 adapter
- tests with fake provider snapshot

完成标准：

- 多 MCP tool 可 deferred。
- duplicate model name fail-closed 或 deterministic mangle。
- unavailable MCP server 不 direct advertise。
- mock MCP capability 可通过 ToolExecutor 执行。
- MCP adapter 不拥有 client，只借用 provider-owned lifecycle resource。

## 10. 为什么不先做 CLI/MCP

因为 CLI/MCP 是最容易放大架构债的 provider：

- CLI 会放大 terminal / cwd / env / stdout / timeout 问题。
- MCP 会放大 async resource / lifecycle / namespace / auth / elicitation 问题。

如果没有 descriptor/exposure/gate/artifact 这四层，CLI/MCP 接进来只会制造新的一批 name allowlist 和 provider 私有规则。先做 PR 0–5，是为了让 PR 6/7 不再开新洞。

## 11. 开工前需要明确的两个小决策

### 决策 A：术语用 capability 还是 capacity

代码里建议统一用 `capability`。原因：

- 现有 package 已是 `src/pulsara_agent/capability`。
- roadmap 标题是 Unified Capability Surface。
- capacity 更像资源容量，不适合作为工具/技能/MCP 的类型名。

文档里可以备注用户口语中的 capacity 即 capability。

### 决策 B：是否新增 typed events

推荐：

- PR 0–2 不新增 typed events，只做 diagnostics/inspector。
- PR 5 必须新增 typed events 或等价 event-log projection：
  - `CapabilityExposureResolvedEvent`
  - `CapabilityGateDecisionEvent`

原因：

- 过早新增事件会固定未成熟 schema，所以不要放进 PR 0/1。
- 但 PR 5 的 inspector 目标是解释历史 run，而 scratchpad / 当前静态 inspect 都不是 durable fact；因此到 PR 5 时必须落 event log。

## 12. 最小验收线

PR 0–5 完成后，系统应满足：

1. 任意 model-callable tool 都有 descriptor。
2. 任意 direct-advertised descriptor 都有 execution binding，且能被 `ToolExecutor` 找到。
3. `LLMContext.tools` 来自 exposure plan，而不是直接来自 registry。
4. permission gate 的主判断来自 descriptor + call classifier；`terminal_process` action-level 契约不回退。
5. workflow control tools 有 descriptor，但仍由 runtime workflow plane 截获，不落普通 permission/tool execution 路径。
6. tool batching 的主判断来自 descriptor。
7. artifact policy 可由 descriptor 控制。
8. inspector 能从 durable event log 解释 capability 来源、可见性和 gate 结果。
9. 现有 built-in 行为不回退。

这时再接 CLI/MCP，才是往统一 surface 上加 provider，而不是给 runtime 又挂两条旁路。
