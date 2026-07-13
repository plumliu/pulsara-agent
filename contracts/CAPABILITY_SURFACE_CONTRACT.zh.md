# Unified Capability Surface Contract

_Created: 2026-07-04_

本文档冻结 Pulsara 统一 capability surface 的长期契约。根目录中的调研/实施文档可以作为背景；本文件是代码必须遵守的接口事实。

相关代码：

- [src/pulsara_agent/capability/runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/runtime.py)
- [src/pulsara_agent/capability/descriptor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/descriptor.py)
- [src/pulsara_agent/capability/exposure.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/exposure.py)
- [src/pulsara_agent/capability/provider.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/provider.py)
- [src/pulsara_agent/capability/builtin_provider.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/builtin_provider.py)
- [src/pulsara_agent/runtime/agent.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/agent.py)
- [src/pulsara_agent/runtime/permission.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/permission.py)
- [tests/test_capability_surface.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_capability_surface.py)

---

## 1. 核心立场

Pulsara 的能力面只有一条主路径：

```text
CapabilityProvider(s)
  -> CapabilityDescriptor(s)
  -> CapabilityRegistrySnapshot
  -> CapabilityExposurePlan
  -> model tools / catalog prompt / active skill prompt
  -> capability gate + permission gate
  -> ToolRegistry execution binding
```

硬规则：

- `CapabilityRuntime` 是每个 turn 解析 capability 的唯一入口。
- `CapabilityExposurePlan` 是该 turn 的唯一 capability fact。
- `CapabilityDescriptor` 是工具、skill、MCP 以及未来 capability 的声明真值。
- `ToolRegistry` 只负责 execution binding；不得从 `ToolRegistry` 或 Tool object 反推 descriptor 真值。
- 旧 `CapabilityResolver` / `ResolvedCapabilitySet` / `NoopCapabilityResolver` 不再是 runtime API，不得作为 fallback 重新引入。

---

## 2. CapabilityDescriptor

每个 model-callable capability 必须有显式 descriptor。

descriptor 至少承担以下职责：

- model-facing 名称、描述、输入 schema；
- provider kind / provider id / namespace；
- `is_model_callable`；
- `is_read_only`；
- `is_concurrency_safe`；
- `is_destructive`；
- `is_open_world`；
- permission category；
- advertise policy；
- artifact mode；
- availability / health message；
- provenance / metadata。

内置工具 descriptor 的声明真源是 [capability/builtin_provider.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/builtin_provider.py)。该文件必须使用显式 descriptor 表或显式构造函数；不得根据工具实例的 `description`、`parameters`、`is_read_only` 等字段生成生产 descriptor。

---

## 3. Provider model

`CapabilityProvider` 只负责在一次 resolve 中产出：

- descriptors；
- skill catalog entries；
- active skill injections；
- diagnostics；
- catalog prompt；
- active skill prompt。

Provider 不执行 tool，不拥有 permission gate，不写 event log。

V1 合法 provider：

- `BuiltinToolCapabilityProvider`：内置工具 descriptor。
- `LocalSkillCapabilityProvider`：本地/bundled skill 的 catalog 与 active injection。
- `McpCapabilityProvider`：只消费同一份 immutable `McpInstalledCapabilitySnapshot`，由其中 frozen server snapshots
  与 exact `McpBindingIdentity` bindings 产出 MCP descriptor；不得从 live manager 或另一份 snapshot 重建。

`CapabilityRuntime.resolve_for_turn()` 必须聚合所有 provider output，并构建单个 `CapabilityExposurePlan`。

---

## 4. ToolRegistry binding 边界

`ToolRegistry` 是执行绑定注册表，不是 capability 声明注册表。

绑定规则：

- descriptor 有、execution binding 无：不得 direct advertise，必须产生 `capability_missing_execution_binding` diagnostic。
- execution binding 有、descriptor 无：必须产生 `capability_missing_descriptor` diagnostic，并且该 tool 不得进入 model tools。
- binding validation 必须发生在 exposure plan 构建时，而不是等工具执行时才发现。
- future CLI/MCP/插件工具必须同时提供 descriptor provider output 与 Tool/AsyncTool binding；只提供其中之一是契约错误。

MCP execution surface 的 installation ID 只做整体 attribution；具体执行身份是
`server_id + slot_id + snapshot_id + discovery_generation`。无关 server refresh 可以产生新 installation，但不得改变
未变化 binding 的执行身份。Host safe-point installation 必须先冻结 affected old slots，再同步交换完整 surface；
worker completion 不得直接改变 capability exposure。

这条规则同样适用于非 direct descriptor：即便 descriptor 被标记为 hidden/deferred/unavailable，只要它是 model-callable，就必须能被诊断“是否缺 execution binding”，避免 provider snapshot 与 adapter 安装悄悄漂移。

---

## 5. CapabilityExposurePlan

`CapabilityExposurePlan` 是一次 user turn 内唯一的 capability fact，必须包含：

- `direct_tool_specs`：进入模型 tools array 的工具；
- `direct_names`；
- `deferred_names`；
- `hidden_names`；
- `callable_names`；
- `descriptors_by_name`；
- skill catalog entries；
- active skill injections；
- catalog prompt；
- active skill prompt；
- diagnostics。

暴露规则：

- `DIRECT + AVAILABLE + has execution binding` 才能进入 `direct_tool_specs` 与 `callable_names`。
- `HIDDEN` / `UNAVAILABLE` 不进入 model tools。
- `DEFERRED` 不进入 model tools；V1 也不提供 deferred activation。
- `callable_names` 是本 turn 可调用集合；模型幻觉调用不在集合内的工具时，必须 fail-closed。

Permission mode 不得修改 `CapabilityExposurePlan` 的 model tools 集合。权限模式只在 gate 阶段决定 ALLOW / WAIT / DENY。

---

## 6. Skill progressive disclosure

Skill 不是 typed tool。Skill 是 capability prompt guidance。

Pulsara 的 skill 披露采用两层策略：

1. compact index：所有 prompt-visible skill 至少以 `name` / short description / location 进入索引。
2. detail section：在预算允许时追加更长 description / when-to-use。

规则：

- catalog prompt 是路由索引，不是 skill body。
- 当任务匹配某个 skill 时，模型应使用普通 read 工具读取对应 `SKILL.md` 后再执行。
- 不新增 `skill_view` / `skill_get` 专用工具作为 V1 读取路径。
- `disable_model_invocation` 的 skill 不进入 model-facing catalog。
- 显式 `$skill` / CLI `--skill` 激活产生 active injection；active body 注入系统提示，不进入 compaction summary。

Skill manifest 中的 CLI hints（如 suggested_tools、required_binaries、external_services）只作为 diagnostics / attribution / prompt guidance，不改变 permission 策略。

---

## 7. Active skill attribution

当本 turn 存在 active skill injection，且模型调用 `terminal` 或 `terminal_process` 时，runtime 必须在 `CapabilityGateDecisionEvent.capability_context` 中写入轻量归因：

```json
{
  "context_kind": "active_skill_present",
  "active_skill_names": ["hf-cli"],
  "skill_suggested_tools": ["terminal"],
  "cli_required_binaries": ["hf"],
  "cli_external_services": ["huggingface"]
}
```

归因语义：

- 这不是 permission grant。
- 这不是 tool result。
- 这不是模型参数。
- 这是 runtime 对“该 terminal call 发生在什么 capability exposure 下”的解释事实。

裸 `$skill` 只激活该 user turn；CLI `--skill` 可让每个 turn 都携带 active skill。

---

## 8. Capability gate 与 permission gate

工具调用必须先通过 capability exposure access，再进入 permission policy。

call-local fail-closed：

- descriptor missing；
- unavailable；
- hidden；
- not callable in current exposure。

这些错误只拒绝对应 tool call，不得把同 batch 中其他合法 call 一起拦掉。

batch-level permission：

- 对已通过 capability exposure 的 call 集合，`PolicyPermissionGate` 可以返回 ALLOW / WAIT / DENY。
- permission gate 必须读取当前 run 的 immutable `RunPermissionSnapshot`，不得读取 HostSession stored default 或其他 live mutable holder 来解释已启动 / suspended run。
- `WAIT_FOR_USER` 是 batch suspension，适用于本批需要用户批准的调用。
- `DENY` 的错误信息必须对应最终被拒工具，不得把某个兄弟工具的 reason 套给所有 call。

所有最终 gate 结果必须写入 typed `CapabilityGateDecisionEvent`，用于 inspector/replay 解释历史行为。

`CustomEvent(name="capability_gate_decision")` 不再是新 run 的合法写路径；本契约硬切到 typed event，不要求 inspector 继续读取旧 custom gate decision。

---

## 9. Workflow control tools

Plan workflow control tools（`enter_plan` / `ask_plan_question` / `exit_plan`）必须有 descriptor，用于 exposure 与 inspector。

执行边界：

- 它们由 runtime workflow plane 截获；
- 但截获前必须先通过 capability exposure access；
- descriptor missing / hidden / unavailable / not callable 时，不能执行 workflow side effect。

Workflow control tools 不走普通 permission approval 流，但不能绕过 capability surface。

当 workflow control tool 接管同一 tool batch 时，被 suppress 的 sibling calls 必须产生
`CapabilityGateDecisionEvent(decision="deny", reason_code="workflow_control_batch_suppressed")`
以及对应 denied tool result，避免 inspector 将这类 Pulsara-owned runtime deny 解释成普通权限失败。

### 9.1 Tool-result semantics contract

每个descriptor必须冻结`CapabilityResultRenderContractFact`，其中variant table显式约束result state、execution phase、
operational kind、essential envelope kind与terminal timing requirement。Execution boundary使用run-frozen
`ToolResultSemanticsBuilderRegistry`精确rebind builder ID/version/declarative contract fingerprint。

正常执行只接受tool提供的typed runtime semantics input；pre-execution deny使用descriptor denial variant；external delayed
result使用原committed requirement中的contract与capture policy。三条路径都不得根据tool name或serialized result JSON另写
classifier。Builder implementation build fingerprint仅是process-local diagnostic，不进入durable identity或允许判断。

known descriptor的normal、exposure/permission/policy deny、workflow、MCP resume与external ingress必须实际调用同一个resolved
builder binding；不得只解析binding后手工构造profile/essential。只有descriptor确实缺失时允许unknown/generic semantics。
Per-batch executor必须继承原run-frozen semantics registry与essential capture policy，不能重新取得system default。

---

## 10. Events / observability

每个普通 user turn 必须产生 `capability_exposure_resolved` 事件，value 至少包含：

- registry generation；
- direct/deferred/hidden descriptor ids；
- direct/deferred/hidden names；
- callable names；
- diagnostics。

每个工具调用最终 gate 判断必须产生 `CapabilityGateDecisionEvent`，至少包含：

- tool call id；
- tool name；
- descriptor id；
- decision；
- reason code；
- reason message（可空）；
- suggested rules（可为空列表）；
- policy mode；
- effective permission policy；
- exposure generation；
- descriptor availability；
- descriptor permission category；
- effective permission category；
- effective read-only；
- result state（当调用被拒或错误时）；
- terminal active skill attribution（若存在）。

Inspector 必须从 event log 投影这些事件，而不是依赖 transient scratchpad。

---

## 11. 禁止事项

- 不允许恢复旧 `AgentRuntime(capability_resolver=...)` API。
- 不允许恢复 `ResolvedCapabilitySet` 作为 runtime fact。
- 不允许从 `ToolRegistry` 反推出生产 descriptor。
- 不允许 provider 只产 descriptor、不注册 execution binding，却仍 direct advertise。
- 不允许 binding 有、descriptor 无的工具进入模型 tools。
- 不允许 permission mode 过滤 tools array；permission 只负责调用时 gate。
- 不允许 hidden/unavailable/deferred capability 被模型幻觉调用时直接执行。
- 不允许 workflow control tools 在 descriptor 缺失时仍改变 plan state。
- 不允许把 skill 当作 typed tool provider；CLI 使用 V1 路径是“skill/docs/prompt guidance + terminal”。
- 不允许从tool-result output JSON推断variant、essential result或terminal timing。

---

## 12. 测试守护

最低测试门槛：

- `CapabilityRuntime` 每 user turn 只 resolve 一次，exposure 在本 turn 内稳定。
- 每个 registered core tool 都有显式 built-in descriptor。
- execution binding 有、descriptor 无：diagnostic + 不进入 model tools。
- descriptor 有、binding 无：diagnostic + 不进入 model tools。
- hidden / unavailable / deferred / unknown tool call-local deny，不拖累同批合法 tool。
- workflow control tools descriptor missing 时不得执行。
- approval resume / pending confirmed tool 必须重新经过 capability fail-closed。
- `terminal_process` observe actions 的 action-level classifier 行为保持：非 read-only 且 terminal 非 off 时 ALLOW；terminal off 时 DENY。
- skill catalog progressive disclosure 保证所有 prompt-visible skill 至少进入 compact index。
- active skill terminal attribution 只在 active skill turn 出现。
- inspector 能解释 latest exposure 与 gate decisions。

---

## 13. Execution surface / projection split 与 continuation narrowing

production provider API 拆成两个单向阶段：RunStart 前的 `snapshot_descriptors()` 只产 descriptor execution surface；
RunStart 后的 `resolve_projection()` 只产 catalog/active-skill/provider prompt projection。context-dependent descriptor
provider 与 descriptor+projection 混合 `resolve()` 不是生产协议。Local skill 是 projection-only。

durable exposure identity分别保存完整 descriptor/binding entries、authorization entries、catalog projection、active-skill
projection，以及 semantic/fact 两类 fingerprint。authorization name set不得静默截断；超过安全上限直接 fail closed。
projection entry/fragment/prompt artifact ID是exposure-scoped persistence attribution，只进入fact fingerprint，不进入semantic
fingerprint；相同模型可见文本与source identity在不同exposure ID下必须得到相同semantic fingerprint。

Context snapshot引用catalog/active-skill projection时，必须从该durable exposure冻结candidate authority，包含original exposure
event ref、projection semantic fingerprint、rendered prompt artifact ID、raw content hash/chars以及固定channel/lowering。
Candidate materialization与compiler再次逐字段join；只匹配artifact ID或当前live exposure不足以授权model-visible正文。

live continuation 每次使用 initial raw resolve basis 和 current surface 生成 comparison candidate。只有完整 semantic
fingerprint 相等才 reuse；否则执行 monotonic narrowing：

- authorization 只保留相同 name + descriptor fingerprint + binding fingerprint，且 disposition 不得更宽；
- projection 只保留相同 `projection_entry_id + content_fingerprint`；
- 新 descriptor、binding、catalog entry、active injection 一律忽略；
- 模型可见 prompt 只拼接 original exact fragment artifacts。删除 entry 时同步删除空 container/projection wrappers；
  禁止重新 render 后把 index/name 提升为 detail。

continuation fact必须引用 source exposure；runtime/event validator必须证明 effective exposure 是 source 的内容子集。

Run boundary冻结的`FrozenCapabilityExecutionSurface`必须同时进入`AgentRunDraft`、`RunWorkingSet`与
`BoundaryExecutionHandles`。initial/resume handles在对应durable batch commit前捕获exact MCP installation、
CapabilityRuntime、ToolRegistry与surface identity；commit fold和Agent model loop不得从scratchpad/current wiring重新取值。

process-local borrow authority必须包含handle ID/generation并读取同一handle state；`retiring/closed`后禁止新borrow，
已取得borrow仍可在finally release。tracker只记录`active_parent_tool_call_borrows`与
`active_child_tool_call_borrows`两类真实在途tool call，不记录MCP pending interaction。MCP pending lease完全由
`McpServerSupervisor`拥有；execution handle退休不得查询或等待该lease，MCP slot/manager退休也不得依赖execution-handle
borrow tracker。detached child使用`ChildExecutionRegistry`持有的child-owned frozen execution handles，
不得引用已可退休的parent authority；immediate与dependency-scheduled child必须走同一个安装入口。child lifetime本身
不是borrow，只有tool call执行的try/finally区间增加`active_child_tool_call_borrows`。最后一个deferred parent borrow归零
后必须同时close handle并retire confirmed run owner。

child handle进入closing时，borrow归零callback必须校验exact child ID、handle identity/generation、release_requested与
coroutine done，随后才能释放child session/capacity/MCP reverse index；active child的普通borrow变化不得触发release。
