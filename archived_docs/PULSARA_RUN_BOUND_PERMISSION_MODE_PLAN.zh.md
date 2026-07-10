# Pulsara run-bound permission mode 计划

日期：2026-07-08

## 0. 结论

Permission mode 应该和单次 `run` 绑定，而不是作为运行时工具链随时读取的一份可变全局状态。

更准确地说：

- `HostSession` 持有的是“下一次 run 的默认 permission mode / policy”。
- 每个 `RunStartEvent` 固化一次不可变的 `RunPermissionSnapshot`。
- 同一 run 内的 capability gate、terminal tools、approval、plan workflow、MCP resume、subagent spawn 都只能读取这份 run snapshot。
- 同一 run 内不存在 permission snapshot mutation；Plan mode 也不能覆盖当前 run 的 snapshot。
- 用户在 run 之间切换 mode，只影响之后创建的新 run；不 retroactively 改变已经开始的 run，也不因为 session default 改变而取消已经属于旧 run 的 child agent。

这会让权限语义更像“执行合约”，而不是一根运行中会被外部拨动的全局旋钮。

## 1. 为什么要改

当前代码已经在产品行为上接近 run 边界：`HostSession.set_permission_mode(...)` 会拒绝 active run、pending interaction、stopping run、active plan workflow。

但事实源仍然是 session/runtime 级的可变 holder：

- `AgentRuntime.__init__` 创建 `_permission_state = PermissionState.from_policy(policy)`。
- `PolicyPermissionGate` 和 terminal / terminal_process tools 都持有同一个 mutable `PermissionState`。
- `AgentRuntime.set_permission_policy(...)` 直接 mutate `_permission_state.policy` / `_permission_state.mode`。
- `RunStartEvent` 只记录 `user_input_chars`，不记录本 run 使用的 permission snapshot。
- `CapabilityGateDecisionEvent` 记录 `policy_mode` / `permission_policy`，但来源是 emit 当下的 `AgentRuntime.permission_mode` / `permission_policy`。
- subagent capability snapshot 也从当前 `AgentRuntime.permission_mode` / `permission_policy` 读取。

这带来几个问题：

1. Inspect 无法从 `RunStartEvent` 直接回答：“这个 run 从一开始是在什么权限合约下执行的？”
2. Gate event 虽然有 permission 字段，但它不是 run 起点事实，只是每次 gate emit 时读取到的当前值。
3. terminal tools 直接读 mutable holder，未来如果出现更复杂的 resume / child / background 执行路径，很容易出现“同一 run 内前后读到不同 policy”的语义漂移。
4. subagent 当前为了防止 bypass 切走后 child 继续跑，做了 `subagent_bypass_revoked` 式安全收窄取消。这是合理的保守补丁，但如果 permission mode 本身改为 run-bound，这类“session default 改变导致旧 child 被取消”的语义就不应该再是主路径。

## 2. 目标语义

### 2.1 Session default 与 run snapshot 分离

`HostSession` 暴露给用户的 mode 应改名/理解为：

- `default_permission_mode`
- `default_permission_policy`

它们代表“非 plan 状态下，下一次用户 run 的默认权限”。

Plan active 时要区分两个概念：

- **stored default**：HostSession 保存的普通默认权限；plan 结束后继续使用。
- **effective next-run permission**：下一次 run 实际会使用的权限。Plan active 时永远是 read-only。

当用户输入触发新 run 时，runtime 创建：

```python
@dataclass(frozen=True, slots=True)
class RunPermissionSnapshot:
    snapshot_id: str
    runtime_session_id: str
    run_id: str
    permission_mode: Literal[
        "read-only",
        "ask-permissions",
        "accept-edits",
        "bypass-permissions",
    ]
    permission_policy: dict[str, object]
    permission_snapshot_source: Literal[
        "session_default",
        "plan_mode",
        "child_profile",
    ]
```

V1 不一定要单独新建数据库表；可以先作为 `RunStartEvent` 字段和 `LoopState` 内存字段存在。

### 2.2 Preset-only runtime contract

Production runtime 不支持 custom per-run policy。一个 run 的权限合约只能来自四个 preset：

- `read-only`
- `ask-permissions`
- `accept-edits`
- `bypass-permissions`

强 invariant：

```python
RunPermissionSnapshot.permission_policy == (
    preset_to_policy(RunPermissionSnapshot.permission_mode).to_dict()
)
```

也就是说，`permission_policy` 是 snapshot 中的冗余可审计展开值，不是第二个自由度。

`EffectivePermissionPolicy` 仍然可以保留，但它的定位要收紧：

- Preset permission mode = product/runtime contract。
- EffectivePermissionPolicy = preset 展开值 + 组件测试工具。
- Custom policy = internal/test-only，不是用户可见 feature。

pytest 中 custom policy 可以保留，但只能是组件级测试工具：

- 可以直接测试 `PolicyPermissionGate(custom_policy)`。
- 可以直接测试 terminal/tool 的低层行为。
- 可以测试 `EffectivePermissionPolicy` cross-product parser / validator。

pytest 中 custom policy 不能穿过 run contract：

- 不能用 custom policy 创建 `RunStartEvent`。
- 不能用 custom policy 跑 `HostSession.run_turn`。
- 不能让 custom policy 进入 event log / inspector / resume / subagent snapshot。

Production `AgentRuntime` / `HostSession` / `RunStartEvent` path 必须 resolve 到 non-null `PermissionMode`。`mode_for_policy(policy) is None` 是配置错误。

CLI 启动参数、session 创建参数、resume manifest 里的 permission 信息都只决定 HostSession stored default，用于未来新 run；它们不是已有 run contract 的来源。stored default 同样必须是 preset mode，而不是 custom policy。

### 2.3 RunStartEvent 是该 run 的唯一 permission contract

`RunStartEvent` 应新增字段：

```python
class RunStartEvent(EventBase):
    type: Literal[EventType.RUN_START] = EventType.RUN_START
    user_input_chars: int
    permission_snapshot_id: str
    permission_mode: Literal["read-only", "ask-permissions", "accept-edits", "bypass-permissions"]
    permission_policy: dict[str, Any]
    permission_snapshot_source: Literal["session_default", "plan_mode", "child_profile"]
```

规则：

- `permission_snapshot_id` / `permission_mode` / `permission_policy` / `permission_snapshot_source` 全部 required。
- 新 schema 下，没有这些字段的 `RunStartEvent` 是非法事件，不进入 supported runtime path。
- `permission_snapshot_id` 可以先是 `permission_snapshot:<run_id>`，不需要复杂 allocator。
- `permission_snapshot_source` 记录 resolver 来源：`session_default` / `plan_mode` / `child_profile`。
- `RunStartEvent` 是该 run 唯一 permission contract；不存在 run-local permission mutation。
- 如果当前 run 的 workflow tool 改变了 plan state，该改变只能影响后续 run 的 snapshot，不能改写当前 run 的 snapshot。
- hard-cut 后不做 runtime 兼容兜底：inspector / recovery 遇到缺少 permission snapshot 的旧 event log，直接报 schema/contract error。开发阶段需要 reset DB 或运行一次 migration。

### 2.4 同一 run 内所有权限判断读取 run snapshot

以下路径都应使用 `LoopState.permission_snapshot`，而不是直接读取 `AgentRuntime._permission_state`：

- `CapabilityRuntime.resolve_for_turn(... permission_policy=...)`
- `PolicyPermissionGate.evaluate(...)`
- `CapabilityGateDecisionFact.policy_mode / permission_policy`
- `terminal` / `terminal_process` 的 terminal access block
- approval pending / resume recheck
- MCP input-required resume recheck
- workflow control tool gate fact
- subagent `refresh_parent_capability_snapshot(...)`
- child AgentRuntime 的 `permission_policy`

## 3. 当前代码落脚点

### 3.1 `src/pulsara_agent/runtime/permission.py`

当前：

- `PermissionState` 是 mutable holder。
- `PolicyPermissionGate` 接收 `EffectivePermissionPolicy | PermissionState`。
- subagent system tools 通过 `self._state.mode is not PermissionMode.BYPASS_PERMISSIONS` 判定 `subagent_requires_bypass_mode`。

计划：

- 保留 `PermissionState` 作为 session default holder，避免大面积破坏 CLI / HostSession API。
- 新增从 immutable snapshot 创建 gate view 的路径，例如：

```python
def permission_state_from_snapshot(snapshot: RunPermissionSnapshot) -> PermissionState:
    ...
```

或更进一步，在 PR2 后让 `PolicyPermissionGate.evaluate(...)` 接收显式 `permission_policy/mode` 参数，避免 gate 对 live state 的长期依赖。

V1 更稳的改法：

- `AgentRuntime.permission_policy` / `permission_mode` 继续返回 session default。
- `AgentRuntime._run_permission_policy(state)` / `_run_permission_mode(state)` 返回 run snapshot。
- 所有 run 内调用改用后者。

### 3.2 `src/pulsara_agent/runtime/state.py`

`LoopState` 是单 run working context cache，适合挂运行时快照：

```python
@dataclass(slots=True)
class LoopState:
    ...
    permission_snapshot: RunPermissionSnapshot | None = None
```

注意：`LoopState` 不是 durable truth。durable truth 是 `RunStartEvent`。`LoopState.permission_snapshot` 只是 run 内执行时避免重复读 session default 的缓存。

### 3.3 `src/pulsara_agent/runtime/agent.py`

关键落脚点：

- `AgentRuntime.__init__`
  - 当前创建 `_permission_state` 并传给 gate/executor。
  - 可先保留，但注释要改：它是 session default，不是 run 内事实源。

- `_stream_task(...)`
  - 在 append user message、emit `RunStartEvent` 前 capture snapshot。
  - 写入 `state.permission_snapshot`。
  - `RunStartEvent` 携带 snapshot 字段。

- `_resolve_capability_exposure(...)`
  - 当前传 `permission_policy=self.permission_policy`。
  - 改为传 `permission_policy=self._run_permission_policy(state)`。

- `_capability_gate_decision_fact(...)`
  - 当前写 `policy_mode=self.permission_mode...` 与 `permission_policy=self.permission_policy.to_dict()`。
  - 改为从 run snapshot 写。

- `_stream_task(...)` 中 subagent snapshot
  - 当前 `refresh_parent_capability_snapshot(... permission_mode=self.permission_mode, permission_policy=self.permission_policy.to_dict())`。
  - 改为 run snapshot。

- `_run_child_agent(...)`
  - 当前 child AgentRuntime 使用 `permission_policy=self.permission_policy`。
  - 改为 parent run / child profile 中已经冻结的 `permission_policy`。

- workflow plan control
  - `_execute_enter_plan(...)` 当前在 agent 内调用 `self.set_permission_policy(read_only)`。
  - run-bound 后必须改成 run-ending workflow control：写入 `PlanModeEnteredEvent` 与 tool result 后 finalize 当前 run，不再进入 follow-up model call。
  - 下一次 run 才进入 plan-active read-only snapshot，见 §5。

### 3.4 `src/pulsara_agent/runtime/session.py`

`RuntimeSession.create_tool_executor(... permission_state=...)` 会把 state 注入 `build_core_tool_registry(...)`。

计划：

- V1 可以继续构造 executor 时传 session default holder。
- 但真正执行工具时，terminal / terminal_process 需要能看到 run snapshot。

两个可选落点：

1. 给 `ToolRuntimeContext` / tool execution path 增加 `permission_snapshot`，terminal tools 优先读 context snapshot。
2. 在每个 run 开始时为该 run 构造一个 `ToolExecutor`，传入 snapshot-derived `PermissionState`。

我更倾向 1：executor/registry 保持稳定，run-specific facts 走 execution context。这样符合“工具清单跨 permission mode 恒定”的既有契约。

### 3.5 `src/pulsara_agent/tools/builtins/terminal.py` 与 `terminal_process.py`

当前：

- `terminal` 在 `_execute(...)` 中读取 `self.permission_state.policy.terminal`。
- `terminal_process` 在 `execute(...)` 中读取 `self.permission_state.policy.terminal`。

计划：

- `execute_with_context(...)` / streaming context 应携带 run snapshot。
- terminal tools 优先读 `event_context` 或新增 `ToolRuntimeContext.permission_snapshot`。
- 没有 context 的直接测试路径可继续 fallback 到 tool instance `permission_state`。

这样兼容现有 unit tests，也让生产路径 run-bound。

### 3.6 `src/pulsara_agent/host/session.py`

当前：

- `current_permission_mode` 直接返回 `agent_runtime.permission_mode`。
- `set_permission_mode(...)` 改 live holder，并在 bypass -> non-bypass 时同步取消 active subagents。
- `enter_plan(...)` / `_emit_plan_mode_exited(...)` 通过改 live policy 实现 plan read-only。

计划：

- 语义上把 `current_permission_mode` 视为 `default_permission_mode`。
- 可以保留旧属性名作为兼容 alias，但文档/CLI status 应逐步说清是 default for next run。
- 新增或投影 `effective_next_run_permission_mode`：plan inactive 时等于 stored default；plan active 时固定为 read-only。
- `set_permission_mode(...)` 继续拒绝 active run / pending / stopping / plan active。
- 成功时只更新 session default，影响之后的 run。
- 不再因为 session default 从 bypass 切走而自动取消已经启动的 child。child 的安全边界来自 spawn 时冻结的 parent run permission snapshot / capability profile。

### 3.7 `src/pulsara_agent/host/session_manifest.py`

manifest 继续保存 session default：

- `permission_mode`
- `permission_policy`

它不是 per-run truth。

hard-cut 后，manifest 里的 stored default permission facts 也必须完整、preset-only：`permission_mode` 与 `permission_policy` 不能同时缺失；`permission_mode` 存在但 `permission_policy` 缺失时可从 preset 派生；`permission_policy` 存在时必须等于对应 preset 展开。旧 manifest 两个字段都缺失时是 schema/contract error，需要重建或迁移，不允许 fallback 到当前 runtime default。

恢复历史 run 的权限只能从 event log 的 `RunStartEvent.permission_*` 读取；恢复 session 时 manifest 只恢复 stored default，用于未来新 run。

### 3.8 Inspector / timeline

落脚点：

- `src/pulsara_agent/inspector/service.py`
- `src/pulsara_agent/runtime/timeline.py`

计划：

- run projection 显示 `permission_mode` / `permission_policy`。
- gate decision projection 仍显示每个 call 的 effective policy，但应能和 `RunStartEvent` 对齐。
- 如果 gate event 的 policy 与 run snapshot 不一致，inspector 给出 diagnostic；新代码中这种情况不应出现。

## 4. Subagent 影响

当前 subagent 的 bypass-only 设计仍然成立，但语义要换成 run-bound：

- parent run 开始时冻结 permission snapshot。
- parent run 下调用 `spawn_agent` / `create_agent_tasks` 时，permission gate 读取 parent run snapshot。
- child capability profile 记录 parent run snapshot 的 permission mode / policy。
- child runtime 使用 profile-filtered capability + parent snapshot 派生出来的 child policy。
- child 不因为 parent session default 后续切换而改变。

这也意味着当前 `subagent_bypass_revoked` 路径要重新定位：

- 如果是用户在 parent run 之间把 session default 从 bypass 切到 read-only：不应取消旧 child，因为旧 child 是旧 run 的事实。
- 如果是 MCP server disabled / permission policy 安全性真正撤销 / capability binding 被移除：仍然可以作为 capability/safety narrowing 取消 child。

换句话说：mode switch 不是 revoke；外部能力撤销才是 revoke。

## 5. Plan mode 的冻结规则

Plan mode 是这次改造里最敏感的地方。这里必须选择一个硬约束：

> **选择方案 A：`enter_plan` 结束当前 run。**

不采用“PlanModeEnteredEvent 在同一 run 内创建新的 read-only snapshot 并覆盖 `state.permission_snapshot`”的方案。原因很简单：一旦允许 run-local monotonic narrowing，`RunStartEvent` 就不再是该 run 的唯一 permission contract，所有 inspect/replay/gate join 都会多一层分叉。

因此，Plan mode 的产品语义是 workflow 边界，不是 live permission mutation。

当前实现：

- 用户 `:plan` / host `enter_plan` 会把 live permission policy 切到 read-only。
- agent `enter_plan` workflow tool 也会在 run 内调用 `set_permission_policy(read_only)`。
- exit plan 时恢复 pre-plan permission。

run-bound 后冻结为：

### 5.1 host / user `:plan`

- 不创建执行 run。
- 设置 `plan_state.active=True`。
- 写 `PlanModeEnteredEvent`。
- 不 mutate live permission holder。
- 下一次 run 的 `RunPermissionSnapshot.permission_snapshot_source="plan_mode"`，`permission_mode="read-only"`。
- `PlanModeEnteredEvent.previous_permission_mode` / `previous_permission_policy` 永远表示 HostSession stored default，用于 plan exit 后恢复 future run default；它不是 triggering run 的 permission contract。

### 5.2 agent `enter_plan` tool

- 在当前 run 内先通过 workflow control gate。
- 写 `PlanModeEnteredEvent`。
- 返回 `enter_plan` tool result。
- 当前 run finalize，不再 follow-up model call。
- 下一次 run 才继续规划，且 snapshot 强制 read-only。
- `PlanModeEnteredEvent.previous_permission_mode` / `previous_permission_policy` 同样记录 HostSession stored default，而不是当前 run snapshot。

这是 run-bound permission mode 的核心约束之一。`enter_plan` 可以改变 workflow state，但不能改变当前 run 的 permission contract。

### 5.3 plan active 下普通 run

- `RunPermissionSnapshot.permission_snapshot_source="plan_mode"`。
- `permission_mode="read-only"`。
- `permission_policy=preset_to_policy(PermissionMode.READ_ONLY)`。
- `HostSession.set_permission_mode(...)` 继续拒绝，因为 plan active 下 stored default 的切换会造成用户误判。

### 5.4 `ask_plan_question`

- 只能在 plan-active read-only run 中执行。
- 如果进入 pending/resume，resume 后仍然是同一个 suspended read-only run。
- 不改 permission default。
- 不创建新的 permission snapshot。

### 5.5 `exit_plan`

- 只能在 plan-active read-only run 中执行。
- approve / revise / cancel / force-exit 的处理都不放宽当前 run。
- approve / cancel / force-exit 后恢复 session stored default，只影响下一 run。
- revise 保持 plan active，下一 run 仍然 read-only。
- agent `exit_plan` tool 路径：approve / cancel / force-exit 在写完 workflow event 与 tool result 后必须 finalize 当前 run，不再 follow-up model call。
- host/user force-exit 或 cancel 路径：不一定有 tool call，因此只写 workflow event；如果它发生在已有 read-only run 的恢复/控制过程中，也必须结束该 run 或保持“不创建执行 run”的 host 控制语义。
- revise 可以继续同一个 read-only planning run，因为它没有退出 plan，也没有放宽权限。

这意味着 `exit_plan` 所在 run 即使被用户 approve，也仍然是 read-only run。它能做的 side effect 是 workflow state transition，不是 filesystem/terminal side effect。

### 5.6 Plan workflow permission facts 也必须 preset-only

Plan mode 的恢复默认权限依赖 workflow event 里的 permission facts，因此它们也必须继承 run-bound 的 preset-only invariant。

新 schema 下：

- `PlanModeEnteredEvent.previous_permission_mode` 必须是 non-null preset mode。
- `PlanModeEnteredEvent.previous_permission_policy` 必须等于 `preset_to_policy(previous_permission_mode).to_dict()`。
- `PlanModeExitedEvent.restored_permission_mode` 必须是 non-null preset mode。
- `PlanModeExitedEvent.restored_permission_policy` 必须等于 `preset_to_policy(restored_permission_mode).to_dict()`。

这里不允许 `null`、custom policy、或 mode/policy 不一致。Plan reducer / recovery 遇到缺失或不一致的 plan workflow permission facts 时，必须报 contract error；不能从当前 session default、resume manifest、或任意 custom policy 兜底。

## 6. PR 拆分建议

### PR0：类型 / 事件 / 契约

- 新增 `RunPermissionSnapshot`。
- `RunStartEvent` 增加：
  - `permission_snapshot_id`
  - `permission_mode`
  - `permission_policy`
  - `permission_snapshot_source`
- 这些字段在 Pydantic event schema 中 required；所有手写 `RunStartEvent(...)` 测试都必须补字段。
- event log contract bump：旧 payload 不再进入 supported runtime path。
- PostgreSQL 不需要新增列；新字段进入 `agent_events.payload` JSONB。
- 可选但不必须：给 `agent_events` 增加 JSONB CHECK constraint。当 `event_type='RUN_START'` 时，至少要求 payload 包含四个 permission 字段，并检查：
  - `payload->>'permission_mode' in ('read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions')`
  - `payload->>'permission_snapshot_source' in ('session_default', 'plan_mode', 'child_profile')`
  - `permission_policy == preset_to_policy(permission_mode).to_dict()` 这种深度一致性放在 Pydantic validator / contract tests，不塞进 SQL。
- 可选但不必须：在 `runs.metadata` denormalize 一份 permission snapshot，方便列表展示；但这只是 projection cache，不是真源。
- 不建议给 `runs` 表新增独立 `permission_mode` / `permission_policy` 列，除非未来明确要按 permission mode 高频筛选历史 run。
- 更新 `contracts/PERMISSION_POLICY_CONTRACT.zh.md`：
  - §8.6 从 “Mode 是可变会话状态” 改为 “Session default + run snapshot”。
  - 删除 “切换只改 mutable holder，gate 下一轮/terminal 下次执行读新 policy” 这类 live holder 主张。
  - 保留“active run / pending / stopping 时切换被拒”。
- 更新 `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`：
  - run start 必须固化 permission snapshot。
  - 同一 run 内 permission snapshot 不允许被改写。
- 更新 inspector / timeline 契约：
  - run projection 应能展示 permission snapshot。
  - gate event 应与 run snapshot 可 join。
- 更新 plan workflow event schema：
  - `PlanModeEnteredEvent.previous_permission_mode` / `previous_permission_policy` required，且必须是 preset mode + preset policy 展开。
  - `PlanModeExitedEvent.restored_permission_mode` / `restored_permission_policy` required，且必须是 preset mode + preset policy 展开。
  - Plan reducer / recovery 对缺失、null、custom、mode/policy 不一致的 plan workflow permission facts 报 contract error。

验收：

- 新 event serialization round-trip 包含 permission fields。
- 文档明确：不存在 run-local permission mutation。
- `permission_mode` 是 required non-null preset enum；V1 不接受 custom per-run policy，也没有 `explicit_run_override` source。
- `permission_policy == preset_to_policy(permission_mode).to_dict()`，不允许 snapshot 中出现 mode/policy 不一致。
- plan workflow permission facts 也必须是 required non-null preset，并满足 policy 等于 preset 展开。
- production `AgentRuntime` / `HostSession` / `RunStartEvent` path 遇到 `mode_for_policy(policy) is None` 必须配置错误。
- inspector/recovery 遇到旧 log 直接 schema/contract error。
- PlanModeEnteredEvent host/user path also rejects custom/null stored default。

### PR1：snapshot resolver

新增统一 resolver：

1. resume/existing `RunStartEvent` snapshot：已有 run 的 snapshot 永远优先；
2. child run -> child profile / `permission_snapshot_source="child_profile"`；
3. parent/user run 且 plan active -> read-only / `permission_snapshot_source="plan_mode"`；
4. otherwise -> session default / `permission_snapshot_source="session_default"`。

这个优先级是安全边界：detached child 继续运行时，不能因为 parent 后来进入 plan mode 而被错误强制成 parent read-only；child 的权限来自 spawn 时冻结的 child profile。

落点：

- `_stream_task(...)` 在 emit `RunStartEvent` 前 capture snapshot 到 `LoopState.permission_snapshot`。
- `RunStartEvent` 写入 snapshot。
- resume 不重算 snapshot；继续使用 suspended state 里的 snapshot。
- 如果 resume/recovery 时 `LoopState.permission_snapshot` 缺失，必须从该 run 的 `RunStartEvent` rebuild，不能从当前 session default 推断。
- 如果该 run 的 `RunStartEvent` 缺少 permission fields，直接 schema/contract error；不允许用 session default、resume manifest 或 read-only fallback 补 contract。
- `resume_manifest` 只恢复 HostSession stored default，用于未来新 run；它不能补已有 run 的权限事实。

验收：

- `test_run_start_records_session_default_permission_snapshot`
- `test_run_start_permission_policy_equals_preset_expansion`
- `test_run_start_rejects_missing_or_custom_permission_mode`
- `test_host_session_rejects_custom_policy_for_run_turn`
- `test_plan_active_run_snapshot_is_read_only_even_if_default_bypass`
- `test_child_profile_snapshot_wins_over_parent_plan_mode`
- `test_missing_permission_snapshot_in_run_start_is_contract_error`
- suspended/resume run 的 snapshot 不被 session default 变化污染。

### PR2：Plan mode 边界改造

- host/user `enter_plan()` 不再 mutate permission holder。
- agent `enter_plan` tool 改成 run-ending workflow control。
- after-tool-results safe point 看到 entered-plan flag 后 finalize 当前 run，不再继续 model loop。
- plan active 的下一 run 强制 read-only。
- `exit_plan` approve 不在当前 run 调 `set_permission_policy(...)` 放宽权限，只恢复 session default for next run。
- `ask_plan_question` pending/resume 保持同一个 read-only run snapshot。

验收：

- `test_agent_enter_plan_finalizes_current_run_without_followup_model_call`
- `test_enter_plan_next_run_is_read_only`
- `test_exit_plan_approval_restores_default_only_for_next_run`
- `test_exit_plan_run_remains_read_only_after_approval`
- `test_exit_plan_approve_finalizes_current_read_only_run`
- `test_exit_plan_cancel_finalizes_current_read_only_run`
- `test_host_force_exit_plan_writes_no_tool_result_and_no_followup_model_call`
- `test_plan_active_run_snapshot_is_read_only_even_if_default_bypass`

### PR3：capability gate / gate event 改读 snapshot

- `_resolve_capability_exposure(...)` 用 run snapshot policy。
- `_capability_gate_decision_fact(...)` 用 run snapshot mode/policy。
- `PolicyPermissionGate` 生产路径使用 snapshot-derived state/view。
- subagent system tool bypass-only 按 run snapshot 判定。
- workflow control tool gate facts 也必须写 run snapshot policy。

验收：

- `test_gate_decision_policy_matches_run_snapshot`
- between-run mode switch 只影响第二个 run。
- read-only run 调 subagent system tool 被 deny，reason_code=`subagent_requires_bypass_mode`。

### PR4：tool execution context

- `ToolRuntimeContext` 增加 permission snapshot 或等价字段。
- sync and async tool execution 都要覆盖。
- terminal / terminal_process 优先读 context snapshot。
- fallback 到 tool instance `permission_state` 仅用于 direct unit tests / legacy direct execution。

验收：

- `test_terminal_tool_uses_run_snapshot_not_session_default_holder`
- read-only run 中 terminal 被 terminal tool 自身 block，且 event/gate 解释一致。
- session default 在 run 后切换，不影响已经属于旧 run 的 terminal action。

### PR5：HostSession default 语义收口

- `current_permission_mode` 保留为兼容 alias，但文档改成 default/effective display。
- 新增：
  - `default_permission_mode`
  - `default_permission_policy`
  - `effective_next_run_permission_mode`
  - `effective_next_run_permission_policy`
- plan active 时 effective next run 永远 read-only，stored default 不变。
- `set_permission_mode(...)` 只改 stored default for future non-plan runs。
- mode switch 不再触发 `subagent_bypass_revoked`。
- REPL `:status` 显示 stored default + effective next-run permission。

验收：

- `test_mode_switch_between_runs_affects_only_next_run`
- active run / pending / stopping / active plan 时仍拒绝切换。
- live terminal process 不因 default 切换丢失。

### PR6：subagent 继承 run snapshot

- parent run snapshot 写入 `SubagentCapabilityProfile`。
- child AgentRuntime 使用 profile 冻结的 policy。
- parent default 后续变化不影响 child。
- capability/MCP safety revoke 仍可取消 child，但普通 default mode switch 不是 revoke。

验收：

- `test_subagent_spawn_uses_parent_run_snapshot`
- `test_subagent_not_cancelled_by_session_default_mode_switch`
- bypass parent run 可 spawn child；之后 session default 切到 read-only 不取消该 child。

### PR7：inspect / timeline / dogfood

- inspector run projection 展示 permission snapshot。
- gate event 与 `RunStartEvent` snapshot 不一致时诊断。
- REPL/status 展示 default + plan effective mode。
- real LLM plan mode、subagent bypass-only、terminal read-only 回归。

验收：

- `test_inspector_projects_run_permission_snapshot`
- plan mode real LLM dogfood 覆盖 enter/ask/exit。
- subagent bypass-only dogfood 在 bypass 下可用，非 bypass 下 visible-but-blocked。

## 7. 测试矩阵

建议新增或调整：

- `test_run_start_records_session_default_permission_snapshot`
- `test_run_start_permission_policy_equals_preset_expansion`
- `test_run_start_rejects_missing_or_custom_permission_mode`
- `test_host_session_rejects_custom_policy_for_run_turn`
- `test_plan_active_run_snapshot_is_read_only_even_if_default_bypass`
- `test_child_profile_snapshot_wins_over_parent_plan_mode`
- `test_missing_permission_snapshot_in_run_start_is_contract_error`
- `test_agent_enter_plan_finalizes_current_run_without_followup_model_call`
- `test_enter_plan_next_run_is_read_only`
- `test_exit_plan_approval_restores_default_only_for_next_run`
- `test_exit_plan_run_remains_read_only_after_approval`
- `test_exit_plan_approve_finalizes_current_read_only_run`
- `test_exit_plan_cancel_finalizes_current_read_only_run`
- `test_host_force_exit_plan_writes_no_tool_result_and_no_followup_model_call`
- `test_plan_workflow_permission_facts_are_required_preset_expansions`
- `test_gate_decision_policy_matches_run_snapshot`
- `test_mode_switch_between_runs_affects_only_next_run`
- `test_set_permission_mode_rejected_during_active_run`
- `test_terminal_tool_uses_run_snapshot_not_session_default_holder`
- `test_approval_and_mcp_resume_use_original_run_snapshot_after_default_switch`
- `test_subagent_spawn_uses_parent_run_snapshot`
- `test_subagent_not_cancelled_by_session_default_mode_switch`
- `test_inspector_projects_run_permission_snapshot`

需要改写的旧测试：

- `tests/test_permission_policy.py` 中 “live mode switching via mutable PermissionState holder” 一组测试应降级为 session-default holder 的内部兼容测试，不能再表达生产 run 内 gate live switching。
- `tests/test_host_core.py` 中 “switch mode changes gate behavior next turn” 仍成立，但断言应补 `RunStartEvent` snapshot。
- `tests/test_subagent_runtime.py` 中 bypass revoke 测试应改成 capability/safety narrowing revoke，而不是 permission default switch revoke。

## 8. 不做什么

- 不允许 agent 自己切 permission mode。
- 不把 permission mode 用作 tools exposure filter；工具仍 visible-but-blocked。
- 不让 child runtime 观察 parent session default 的后续变化。
- 不给 old run 补写 manifest permission；历史 run 以 event log 为准。
- 不把 Plan mode 加进 `PermissionMode` 枚举。
- 不允许 `PlanModeEnteredEvent` 在同一 run 内创建/覆盖新的 permission snapshot。
- 不允许 `exit_plan` approve 在当前 run 内放宽 read-only snapshot。
- 不允许 `enter_plan` 后继续同一 run 的 follow-up model call。

## 9. 最终一句话

权限模式不应是“这条会话此刻的空气温度”，而应是“这一次 run 开始时签下的执行合约”。

Session default 可以变；run contract 不变。Inspect、resume、subagent、tool execution 都围绕这个 contract 对齐。
