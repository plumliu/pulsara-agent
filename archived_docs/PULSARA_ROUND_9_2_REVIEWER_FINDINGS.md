## 总体判断：NOT READY

方向是对的：Round 9.2 已经明确选择了独立、source-agnostic、process-local 的 Hook runtime，USER/WORKSPACE 是真实产品入口，Plugin 只是未来 definition producer；trust digest 也确实跨越了“用户审阅 → 跨进程执行”边界。small durability、prefix continuity、long-horizon 和七维 oracle 的大方向都成立。

但目前有 7 项 P1。若现在交给 coding agent，它必须自行发明 ingress、MCP prepare、permission handoff、compaction successor、Stop、SessionEnd 和输出控制语义，容易形成字符串判断、第二套 permission owner 或 continuity 旁路。

### 我理解的 happy path

```text
USER / exact WORKSPACE hooks.json
→ bounded complete scan + normalize
→ exact definition trust join
→ immutable current view

existing lifecycle owner
→ typed HookEventInput
→ one KernelHookDispatcher
→ shared matcher / executor / output parser / aggregation
→ one narrow typed outcome family
→ original lifecycle owner revalidates并应用合法效果

informational output
→ exact-scope process-local reservation
→ one HOOK_CONTEXT source
→ user-role append-only suffix
→ continuity CAS
→ provider open
```

Engine、transport、parser failure 只让原 owner 按原路径继续；它不会回滚 trusted command 已产生的外部副作用，也不会复活已被 cancel/close 的操作。

---

## P1 Findings

### P1-1：外部输出协议和内部 outcome algebra 尚未闭合，并存在直接的 fail-open 矛盾

**规格定位：** [§7 Output/control](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:557)、[§13 failure matrix](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:816)。

**当前代码确认：** 当前没有 `src/pulsara_agent/hooks/`，`HookEventType`、`KernelHookDispatcher` 均不存在。现有 runner/tool/permission/compaction owners 消费 typed prepared values，不适合接收 raw `decision`、`continue`、`hookSpecificOutput` 字符串。

**具体失败路径：**

- 规格把所有 nonzero exit 都当作 failure；但 Codex 公开契约用 exit code 2 表达 `PreToolUse` deny、`UserPromptSubmit` block、`Stop/SubagentStop` continuation，以及 PostTool feedback。一个可移植的安全 Hook 在 Pulsara 下会被 fail-open，原 tool 反而执行。
- §7.1/§7.4 规定 unsupported output 应使 exact handler invalid、原操作 fail-open；§7.2、§13 和测试计划却规定 `updatedInput` 导致 tool invoke=0 和 synthetic rejection。这与冻结方向“unsupported rewrite 不产生 partial mutation”直接冲突。
- `decision:"block"` 在 PreTool、UserPrompt、PostTool、Stop/SubagentStop 具有不同语义；如果没有 closed parser-to-outcome 映射，业务代码只能按 event name/raw field 分散判断。
- `continue:false` 与 continuation、deny/block 的优先级没有逐 event 穷尽。官方 Stop/SubagentStop 中 terminal `continue:false` 优先于其他 continuation 请求。[Codex Hooks 官方文档](https://learn.chatgpt.com/docs/hooks)

**最小修订建议：**

1. 为 11 个 event 冻结 exact wire schema：JSON root、字段类型、legacy shape、`hookSpecificOutput.hookEventName` 一致性、plain stdout、exit status 和 unknown-field recovery unit。
2. 支持 exit 2 的合法 Pulsara 映射：

   - PreToolUse → deny；
   - UserPromptSubmit → block；
   - Stop/SubagentStop → continue-once；
   - PostToolUse exit 2 因本轮禁止结果替换，明确 `UNSUPPORTED + fail-open`，原 ToolResult 不变。

3. `updatedInput`、ToolResult rewrite、suppression：exact handler invalid，原 invocation 使用原 arguments 至多执行一次；不得制造 rejection ToolResult。
4. Dispatcher 只返回下文建议的 5-family closed union；raw output 永不离开 Hook core。

**是否需要 durable machinery：** 不需要。

---

### P1-2：UserPromptSubmit 没有统一覆盖 direct/queued 的 logical producer

**规格定位：** [§8.1/§8.3](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:609)、[production map](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:802)。

**当前代码确认：**

- direct ingress 从 [host.py:657](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/host.py:657) 安装 active ROOT task，真正的 content/admission 在 [runner.py:1185](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:1185)。
- queued ingress 完全在 [host.py:1422](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/host.py:1422)，之后由 queue consumer admission。
- 规格只把 prompt integration 放在 runner；`PendingPromptHookContext` 又只指定 `queue_item_id`，direct 路径没有 queue row。

**具体失败路径：**

- Hook 运行在 compatible-FULL 检查前：ACK-unknown/retry 可能重复外部副作用。
- Hook 运行在 queue/admission 后：block 时不再满足 queue row、`USER_MESSAGE`、provider open 全为 0。
- direct 与 queued 各自实现 parser/control/context cleanup，形成两个 producer 和两套边缘语义。
- Hook 等待期间 queue capacity、close/fence 状态改变；若不重新验证，可能越过现有 admission owner。
- direct block 缺少合法 public terminal type；queued 可以返回 `REJECTED`，但 `KernelRunResult` 不能表达未创建 turn。

**最小修订建议：**

建立一个 Host-owned `NewTurnHookGate` logical seam，由 direct 和 queued 两个 call site 共用：

1. local validation；
2. stateless compatible ingress preflight；
3. FULL/CONFLICT 时 Hook=0；
4. 用现有 active-task/reservation 防并发，但锁外等待 Hook；
5. 回锁后重验 close/fence/capacity；
6. block：direct 返回 typed `PromptBlockedByHook`，queued 返回 existing `REJECTED`；
7. proceed：direct context 绑定现有 `turn_id/command_id`，queued 绑定 `queue_item_id`；
8. write ACK-unknown 只 exact-confirm 同一 candidate，不重跑 Hook；
9. enqueue/content/admission 失败时清理 exact pending context。

Crash-before-row 后的用户重试仍可重跑 Hook，符合 weak exactly-once 声明。

**是否需要 durable machinery：** 不需要；只需 process-local attempt/reservation 和现有 stateless confirmation。

---

### P1-3：当前 ToolRuntime 无法同时满足“underlying identity 已解析”与“authorize/admit/attempt=0”

**规格定位：** [§5.2 meta route](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:450)、[§8.4](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:642)。

**当前代码确认：**

- runner 在 [runner.py:5145](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:5145) 直接调用 `authorize()`。
- exact MCP/meta route 解析发生在 [tool_runtime.py:1643](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py:1643)，但同一路径会在 [tool_runtime.py:1736](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py:1736) 或 [tool_runtime.py:1841](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py:1841) 执行 `executor.admit()`。

**具体失败路径：**

- 在 `authorize()` 前触发 Hook：meta wrapper 尚未解析，Hook 只能看到 `use_new_mcp_tool`，无法匹配 underlying MCP identity。
- 在 `authorize()` 后触发：direct MCP 已经 admit，deny 无法证明 admit/attempt/invoke 均为 0。
- outer meta 和 underlying tool 分别触发，会重复运行 Pre/Permission/Post。
- Hook 等待期间 route dirty/liveness 改变，若 dispatcher 自行重查 registry，会获得另一套 identity。

**最小修订建议：**

由 ToolRuntime 新增窄的 owner-issued `PreparedResolvedToolInvocation`：

```text
parse + schema validation + exact surface/meta resolution + immutable borrow
→ no authorize / no admit / no attempt
→ PreToolUse
→ existing owner authorize_prepared(...)
→ revalidate scope/effect/dirty/liveness
→ attempt/invoke
```

外部 `tool_name` 使用 underlying provider-qualified identity，`pulsara_tool_name` 保留 wrapper 诊断值。Deny 释放 prepared borrow，沿用 existing no-attempt policy ToolResult settlement。Dispatcher 不得取得 registry、executor 或 repository。

**是否需要 durable machinery：** 不需要；也不需要 DTO fingerprint。

---

### P1-4：PermissionRequest 缺少由现有 permission owner 签发和消费的 exact request handoff

**规格定位：** [§0.2 authority](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:54)、[§8.4](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:642)。

**当前代码确认：**

- runner 在 ASK 后先提交 `REQUIRE_CONFIRMATION`，[runner.py:5163](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:5163)，再调用 `request_confirmation()`。
- MCP confirmation admission 在 human interaction publish 前由 [tool_runtime.py:1900](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py:1900) 的 `before_publish` 完成。

**具体失败路径：**

- runner 直接把 Hook `allow` 当 authorization，会绕过 MCP admission、dirty/liveness 重验或当前 permission snapshot。
- Hook `deny` 若未调用 admission owner 的 discard，会遗留 pending MCP admission/permit。
- Hook failure 如果被当成 proceed，会绕过原 human flow；正确语义应为 ABSTAIN。
- Hook 等待期间 cancel/close/request replacement 后，late allow 可能复活已经失效的 request。

**最小修订建议：**

ASK owner 签发 immutable `PreparedPermissionRequest`，只暴露 public event input；owner 提供三条窄消费路径：

- `ABSTAIN`：调用原 `request_confirmation()`；
- `ALLOW`：绑定当前 exact request，重验 scope/effect/dirty/liveness，然后复用 existing admission/attempt path；
- `DENY`：discard pending admission，settle existing no-attempt denial；
- owner close/cancel/stale 时，无论 Hook 输出什么都不得复活。

Hook 不创建新 permission snapshot，不改变 mode，也不直接提交 interaction row。

**是否需要 durable machinery：** 不需要；复用现有 permission/interaction transactions。

---

### P1-5：active PostCompact/SessionStart(compact) 与已安装 successor 之间存在 continuity dependency cycle

**规格定位：** [§8.1/§8.5](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:607)、[§9.3 CAS](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:706)。

**当前代码确认：**

- active compaction 当前先在 [runner.py:3718](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:3718) 编译 dispatch，之后才读取 compaction cut。
- adoption FULL 后，在 [runner.py:4412](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:4412) 调用 `_install_provider_open()`；该函数明确“without opening transport”，并在 [runner.py:4692](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:4692) CAS install continuity candidate。
- 已安装 dispatch 随后进入 pending slot，真正 transport open 在 [runner.py:4901](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:4901)。
- Round 3.1 明确 install 后 request immutable，install 在 physical open 之前，[Round 3.1](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md:1118)。

**具体失败路径：**

规格要求 PostCompact 发生在 adoption FULL 且 successor installation 成功之后，SessionStart(compact) 又必须在下一次 actual open 前产生 immediate context。但此时 compiled input/execution 已冻结并安装：

- 原地加 Hook context 会修改 installed request，违反 continuity；
- 重编 SYSTEM/tools 会产生第三种 rebase；
- 把 context 留到再下一次 model call 又违反 immediate continuation；
- 重跑 PostCompact/SessionStart 以修补 CAS 会重复外部副作用。

PreCompact 也必须放在当前第 3718 行之前；否则 abort 时已经发生 source collection/compile，不能证明 summary/provider preparation 为 0。

**最小修订建议：**

在不改变冻结方向的前提下，规格必须显式增加一个由 existing continuity owner 拥有的窄“unopened successor augmentation”路径：

```text
lane + fence，且不持有 Host lock
→ PreCompact
→ summary/adoption
→ install base successor；transport open=0
→ PostCompact(active)
→ SessionStart(compact)
→ 若需要context，作为same-epoch suffix生成新CAS candidate，
  supersede unopened execution但不回滚base prefix
→ close predecessor execution handle
→ only final execution open_once
```

Gate stop 后 physical open=0。Context CAS 失败只丢弃该 call-local batch，不重跑 Hooks；owner 从已安装 base fail-open replan。Idle compaction 不安装 successor：PostCompact 输出只诊断，下一次 actual ROOT open 再运行一次 SessionStart(compact)。

还应冻结 process-local `(ROOT epoch, start source)` once guard：同一 Host/epoch 不重复；Host crash 后允许按 `resume` 重跑，符合 weak exactly-once。

**是否需要 durable machinery：** 不需要；但必须修改 existing continuity owner 的 process-local pre-open contract，不能由 Hook subsystem 自建第二套 continuity。

---

### P1-6：StopEvent 放在 canonical terminal acceptance 之后，已经没有合法 continuation consumer

**规格定位：** [§8.1 Stop seam](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:609)、[§8.7](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:660)。

**当前代码确认：** final assistant 通过 [runner.py:4951](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:4951) 构造 settlement，在 [runner.py:4971](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:4971) canonical settle；`accepted.turn_completed` 后 [runner.py:4996](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:4996) 直接返回。

**具体失败路径：**

按现规格“final assistant accepted 后”才运行 Stop，turn 已 canonical COMPLETED。继续一次只能：

- 非法重开 completed turn；
- 创建伪 human prompt/新 turn；
- 或静默丢弃 continuation。

三者都违背冻结语义。

**最小修订建议：**

把 producer 移到“public final assistant candidate 已冻结、确认无 tool calls、但 `PreparedAssistantMessageSettlement` 尚未提交”：

- terminalize：settle `complete_turn=True`；
- continue-once：settle同一 assistant 为 `complete_turn=False`，把 reason/context 放入 exact ROOT `HOOK_CONTEXT`，进入下一 loop；
- 第二次 natural stop 的 owner guard 强制 terminalize；
- user cancel、Host close、provider failure、resource interruption不 dispatch Stop；
- ACK-unknown settlement走现有 exact confirmation，不重跑 Hook。

**是否需要 durable machinery：** 不需要；once guard 是 process-local，crash 可 duplicate/loss。

---

### P1-7：SessionEnd 的 ordinary-admission fence、producer drain 与 terminal lane 顺序自相矛盾

**规格定位：** [§4.3](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:421) 先 fence ordinary Hook admission；[§8.1](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:607) 又写成业务 drain 后才运行 SessionEnd。

**当前代码确认：** 当前 close 在 [host.py:3199](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/host.py:3199) 设置 closing，随后停止 extension/MCP admission、drain delivery、cancel ROOT、关闭 compaction/interaction/subagent/settlement/continuity/tool/MCP，最后才关闭 live/io，[host.py:3360](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/host.py:3360)。

**具体失败路径：**

- ordinary Hook lane 过早关闭：正在完成的 ToolResult/PostTool/SubagentStop 事件被无声丢弃。
- 过晚关闭：SessionEnd 或 background drain 后仍可能出现新 event/process。
- 在 continuity/io/repository 已关后运行：Hook inspect/transcript/context 观察不完整。
- SessionEnd 获得新 3 秒而不是 `min(handler deadline, remaining close deadline)`，可越过 Host close absolute deadline。

**最小修订建议：**

冻结唯一顺序：

```text
stop external/business admission + freeze Host close deadline
→ cancel/quiesce all existing ordinary lifecycle producers
  （期间ordinary Hook lane仍可接受这些已开始producer）
→ fence ordinary Hook admission + freeze current Hook view
→ terminal lane运行唯一SessionEnd（sync、observe-only）
→ cancel background attempts，terminate/kill/drain全部Hook groups
→ close Hook dispatcher
→ remaining repository/live/io physical close
```

SessionEnd 及 process abort 都使用 close deadline 的剩余时间，不创建新 close deadline。

**是否需要 durable machinery：** 不需要。

---

## P2 Findings

### P2-1：Codex-compatible config/stdin 仍缺 exact compatibility profile

**规格定位：** [§2.2 JSON shape](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:196)、[§5 matcher](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:440)、[§9.1 stdin](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:668)。

**当前代码确认：** 当前没有实现可替规格补齐这些选择；生命周期 owners 只提供 Pulsara permission enum、tool DTO、compaction trigger 等内部值。

**具体失败路径：**

- `permission_mode` 被列为所有 event 的 common field，但官方只在八类事件发送；Pulsara enum 到 `default/acceptEdits/plan/dontAsk/bypassPermissions` 的映射未冻结。
- `model`、event-specific `turn_id/tool_use_id/tool_response/stop_hook_active` 的 presence/type 未定义。
- `SessionStart` 官方包含 `clear`，Pulsara 没有合法 clear producer；目前没有明确的 deliberate incompatibility diagnostic。
- unknown group/handler field 的“Codex兼容诊断”没有说明 ignore 还是使 exact unit unavailable；`timout` typo 可能悄然变成默认 600 秒。
- matcher 没有冻结 regex dialect、search/full-match 语义和 linear-time implementation。
- `additionalContextLimit` 已进入 DTO/digest，却没有 runtime consumer。官方行为会把大输出 spill 到磁盘，但本轮明确禁止 Hook artifact。[Codex Hooks 官方文档](https://learn.chatgpt.com/docs/hooks)

**最小修订建议：**

- 增加一张 config/input compatibility matrix：`SUPPORTED / KNOWN-IGNORED / UNSUPPORTED`。
- unknown group/handler execution field 使最窄 exact group/handler unavailable；不猜语义。
- 明确 `clear` 永不产生并在 inspection 中提示。
- 冻结 RE2 等 linear-time dialect及匹配方式。
- 对 `additionalContextLimit` 采用无 artifact 语义：正数表示 per-handler approximate-token admission threshold，超限时整项 context omitted 并诊断；`0` 仍受 1 MiB capture 和 existing compiler bounds。控制字段独立解析，正文不做隐式截断。
- `statusMessage` 只进入现有 process-local status/log sink，不进入 SYSTEM/context。

**是否需要 durable machinery：** 不需要。

---

### P2-2：attempt deadline、process drain 和 secret scrub 还未完全覆盖 parent owner

**规格定位：** [§6](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:474)、[§3 CLI inspect](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:365)。

**当前代码确认：** Host close 已拥有一份冻结 deadline，[host.py:3215](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/host.py:3215)；Round 5 要求复合 planning 共用一个 owner deadline，而不是每个子步骤刷新，[Round 5](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md:469)。

**具体失败路径：**

- 当前只规定 `dispatch time + handler timeout`，未规定 `min(parent owner deadline, handler deadline)`；600 秒 Hook 可越过 120 秒 compaction/close planning。
- queue wait 虽消耗 handler timeout，但若 acquire semaphore 后刷新 deadline，会把等待时间还回去。
- stdout/stderr 若不是从 spawn 时并发 drain，子进程可能因 pipe 满而阻塞，executor 再等待 exit 形成死锁。
- `inspect` 要完整显示 command，但 exact-key scrub 只覆盖 executor environment/stdin/stdout/stderr。API key 若出现在 command、environment 的另一个 key、spawn exception 或 status message 中仍会泄漏。
- fail-open 若 owner 已 cancel/close，不应“继续原操作”并将其复活。

**最小修订建议：**

- `effective_attempt_deadline = min(event_dispatch + configured_timeout, inherited_owner_deadline)`；不存在 parent 时才仅用 handler deadline。
- semaphore queue 时间始终消耗该 deadline。
- spawn 即启动 stdout/stderr reader；一个 attempt owner 仲裁 exit/overflow/timeout/cancel/close；terminate group → existing 5 秒 physical grace → kill → drain EOF → join。
- 对所有外显路径做 exact-value scrub：config list/inspect/trust/status、argv、全部 environment values、stdin、stdout/stderr、exceptions/traces；只处理 exact API key value。
- owner cancellation/liveness 优先于 Hook outcome。

**是否需要 durable machinery：** 不需要。

---

### P2-3：HOOK_CONTEXT 缺少 exact occurrence identity 和 reservation settlement

**规格定位：** [§9.2/§9.3](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:682)。

**当前代码确认：**

- `ContextSourceKind` 是 closed enum，[contracts.py:86](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/model_input/contracts.py:86)，compiler 要求每个 kind 精确 `VALUE | ABSENT`。
- 现有 `ONE_SHOT` occurrence 使用 `("one-shot", domain_semantic_fingerprint)`，[compiler.py:2802](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/model_input/compiler.py:2802)。
- Round 3.1 明确相同显示文本但不同 causal occurrence 必须追加，[Round 3.1](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md:440)。

**具体失败路径：**

- 若 batch digest 只覆盖正文，两次不同 ToolResult/Stop 产生相同 context 时，第二次会被错误吞掉。
- 若 dispatcher 在 compile 前移除 pending entries，CAS failure 会丢掉与当前 call 无关的新 arrival。
- 若 CAS failure 后把 batch 放回，settled Hook 会在后续 call 重放。
- prompt-bound、tool feedback 和 background entries 若共用无 key 的 list，可能跨 queue item、ROOT/child 或 workspace 泄漏。
- compiler omits source 后是否重试没有定义，会产生幽灵 context。

**最小修订建议：**

- 不在 `FrozenHookContextBatch` 添加 fingerprint。
- source builder 使用现有 stable causal IDs 生成 compiler 已要求的 domain identity：epoch nonce、turn/queue id、tool result id、compaction snapshot id、task/completion candidate id、assistant candidate id。
- Dispatcher 在一次 planning 中 freeze exact object reservation；CAS success、compiler omission、known CAS failure 都只 retire 该 call-local reservation。CAS failure 不回放、不重跑 Hook；冻结后新到 entries 保留给未来。
- prompt、scope background、continuation 使用同一 dispatcher-owned context component 的不同 typed slots；scope terminal 全部清理。

**是否需要 durable machinery：** 不需要；现有 compiler digest 是真实 provider-source/continuity 边界，不应反向写入 Hook DTO。

---

### P2-4：PostToolUse 的 public projection API 与 exhaustive origin coverage 未冻结

**规格定位：** [§8.4 PostTool](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:642)。

**当前代码确认：**

- canonical ToolResult 和 process-local effect 的唯一普通 settlement 在 [runner.py:6401](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:6401)，FULL acceptance 在 6519，effect commit 在 6557。
- Round 7.1 共用 variant builder 当前是 private `_tool_result_variants()`，[lowering.py:214](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/model_input/lowering.py:214)。
- `report_agent_result` 使用专用 composite path，[runner.py:5402](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:5402)。

**具体失败路径：**

- Hook 自行复制 projection 逻辑，会看到 raw artifact/private replay，或错误声称下一 compiler 一定选择 FULL。
- policy denial、invalid arguments、permission denial、meta MCP、late exact result 和 `report_agent_result` 可能有的触发、有的不触发。
- terminal explicit subagent result 的 PostTool context 没有 future provider call，却可能进入 buffer。
- SubagentStop 与 composite ToolResult/PostTool 的顺序不明确。

**最小修订建议：**

- 将 Round 7.1 builder 暴露为唯一 public provider-neutral pure helper；返回 event-time `render_mode + public value`。
- 冻结 exhaustive rule：一个 supported local model tool call产生的每个 canonical ToolResult settlement，包含 no-attempt rejection，精确触发一次 PostTool；meta 只按 underlying identity 一次。
- explicit `report_agent_result`：SubagentStop 在 composite commit 前；terminal commit 后 PostTool 仅诊断，continued path 的 ordinary ToolResult 后正常 PostTool/context。
- Hook 永不读取 artifact raw/private replay，也不承诺 future compiler variant。

**是否需要 durable machinery：** 不需要。

---

### P2-5：SubagentStop 的两条 continuation settlement 还不够机械

**规格定位：** [§8.6](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:654)。

**当前代码确认：**

- EXPLICIT candidate seam 在 [subagent.py:1869](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/subagent.py:1869)。
- INFERRED candidate seam 在 [subagent.py:2017](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/subagent.py:2017)。
- `_completing` permit 的 release/terminalization 在 [subagent.py:2051](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/subagent.py:2051)。

**具体失败路径：**

- inferred continuation 若仍把 assistant settlement 标成 `complete_turn=True`，SubagentResult 虽为 0，child turn 已关。
- explicit continuation 若误走 composite，会同时提交 terminal SubagentResult。
- 第二次 natural stop 若再次接受 continuation，会形成 loop；若完全跳过 Hook，`stop_hook_active=true` 又没有公开意义。
- 多 handler 中 `continue:false` 与 continuation 的优先级未冻结。

**最小修订建议：**

- INFERRED continue：assistant canonical settle 为 nonterminal，释放 completion permit，不提交 SubagentResult，context 进入下一 child compile。
- EXPLICIT continue：不提交 composite/SubagentResult；settle ordinary“结果未接纳、继续工作”ToolResult，task 保持 ACTIVE，再运行 PostTool。
- coordinator 持有唯一 process-local boolean；第二次仍可 dispatch `stop_hook_active=true` 供观察，但 continuation 请求只能诊断，必须 terminalize。
- 任意合法 `continue:false` 优先 terminalize；engine failure 也按正常 terminalization。
- 明确跨 crash 不保证一次，不能为此加 receipt。

**是否需要 durable machinery：** 不需要。

---

### P2-6：background completion order 与 diagnostic sink 没有形成可测试契约

**规格定位：** [§2.5 ordering](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:264)、[§6.6](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:543)。

**当前代码确认：** 当前没有 Hook diagnostic/live owner。现有 `OperationalHookType` 是另一套 extension-plane best-effort vocabulary，不是 Codex lifecycle dispatcher。

**具体失败路径：**

- “definition order 决定 start”与 concurrent process start 不能同时作为 physical truth。
- background 可能乱序完成；若等待较早未完成 attempt，会使 background 间接阻断 provider；若直接 append，则 model-visible 全局顺序不一定等于 dispatch 顺序。
- typed diagnostics 没有 sink，`statusMessage`、unsupported control、overflow 可能无人消费。
- scope terminal 后 late background completion可能写入已终止 buffer。

**最小修订建议：**

- 只承诺 deterministic admission ordinal 和 synchronous aggregation order，不承诺 OS process 实际 start/finish 顺序。
- safe point 只收集当时已经完成的 entries，并按 `(event dispatch ordinal, definition ordinal)` 排序；不等待更早 in-flight attempt。晚完成的较早 event 可在下一 safe point 出现。
- diagnostics 使用一个现有 process-local log/status adapter；不增加 Committed/Live event kind。
- terminal/close 后 late output 丢弃，无法进入新 scope。

**是否需要 durable machinery：** 不需要。

---

## P3 Findings

### P3-1：少数字段仍可能成为装饰性 proof 或重复 provenance

**规格定位：** [§3.1/§3.2](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:290)。

**当前代码确认：** 尚无 Hook consumer；`trusted_at`、`statusMessage`、definition/source/snapshot 中重复的 scope/provenance 只能由未来代码自行解释。

**具体失败路径：** 字段只进入 digest/repr，却没有 inspection/status/aggregation consumer；未来开发者可能再添加 fingerprint 来“证明”重复字段 exact join。

**最小修订建议：**

- `trusted_at` 明确为 CLI inspection metadata，否则删除。
- `statusMessage` 按 P2-1 指定 status sink。
- provenance 用一个 immutable owner-issued value/references 传递；source visibility、current dispatch scope 和 context scope 各保留一个真实 owner slot，不重复展开第三份。
- 不添加 batch/set/definition fingerprint。

**是否需要 durable machinery：** 不需要。

---

### P3-2：应显式隔离现有 OperationalHookType，并修正 implementation/test map

**规格定位：** [dependency direction](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:729)、[test plan](/Users/plumliu/Desktop/python_workspace/pulsara_agent/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md:836)。

**当前代码确认：** [extensions.py:42](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/extensions.py:42) 已存在 `OperationalHookType`，但它是六项 extension-plane observation，不是用户 lifecycle command Hook。

**具体失败路径：**

- coding agent 可能复用 `KernelExtensionHost`，把新 11 项并入 Live/extension vocabulary，破坏七维 oracle和独立 authority。
- dependency map 写着 dispatcher 只 import contracts/executor，但 dispatcher 又必须使用唯一 matcher/output parser。
- 测试计划仍要求 `updatedInput invoke=0`，会固化 P1-1 的错误语义。
- production map 把 queued prompt 漏出 Host，并把主要 compaction seam 误标为 `compaction/*`，而当前 owner 在 runner。

**最小修订建议：**

- architecture test 明确禁止 `hooks/* -> conversation_kernel/extensions.py` 和反向 lifecycle dispatch。
- dependency map允许 dispatcher 导入唯一 matcher/output-parser pure modules。
- 修正 integration map 与 updatedInput/exit2/close/two-step compaction tests。
- 不加 legacy alias 或兼容 wrapper。

**是否需要 durable machinery：** 不需要。

---

## 11 项 lifecycle seam 逐项结论

| Event | 唯一 producer 应在 | 合法 consumer | 明确 terminal outcome | 当前结论 |
|---|---|---|---|---|
| SessionStart | ROOT 每个 process-local epoch 首次 actual open 前；普通 cold 在首次 compile 前，compact 使用 P1-5 pre-open augmentation | ROOT run gate + exact-scope context reservation | proceed；explicit stop 使用现有 interruption、provider open=0；idle successor 不提前运行；engine failure proceed | **P1：compact 路径和 once/source 未闭合** |
| SessionEnd | Host close：business admission stop、in-flight producer quiesce 后的唯一 terminal lane | observe-only diagnostic | 无论 Hook 成败都继续 physical close；无 context buffer | **P1：close 顺序冲突** |
| UserPromptSubmit | direct/queued 共用的 NEW_TURN ingress gate；validation + compatible preflight 后、任何 canonical row 前 | existing direct/queue admission owner | block 时 row/message/open=0；FULL/CONFLICT Hook=0；ACK-unknown exact-confirm | **P1：当前是两个代码路径** |
| PreToolUse | ToolRuntime owner-issued prepared resolution 后、authorize/admit/attempt 前 | existing authorization owner | deny：admit/attempt/invoke=0，ordinary ToolResult；failure：按原 arguments 继续 authorize | **P1：当前 meta resolve 与 admit 混在一起** |
| PermissionRequest | existing owner 已决定 ASK 后 | same permission owner | deny：attempt=0；allow：重验后当前 request 一次；abstain/failure：原 human flow | **P1：缺 exact owner handoff** |
| PostToolUse | canonical ToolResult FULL、process-local effect settled 后 | context-only；next compile 可消费 | ToolResult 永不改写；无 future call 时 context 仅诊断 | **P2：普通 seam 唯一，特殊 origin 未穷尽** |
| PreCompact | global lane + exact-scope fence 后，且在 `_prepare_provider_dispatch`/source read/summary 前；Hook 等待不持 Host lock | compaction owner gate | abort：snapshot/summary/provider call=0；manual typed abort，auto 回普通 owner 路径 | **可唯一接入；需 P1-5/P2-2 的 precise order/deadline** |
| PostCompact | adoption FULL；active 必须 base successor install 成功且 transport open=0 | active run gate/context；idle observe-only | active stop：physical open=0；proceed：suffix CAS 后 open；idle context/stop 不缓存 | **P1：当前 installed request 已冻结** |
| SubagentStart | scheduler 选中、task start FULL 后、`_install_live_task()`/child first compile 前，[subagent.py:952](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/subagent.py:952) | child cold seed context-only | Hook failure/continue false 不撤销 task；child install failure走 existing FAILED settlement | **唯一且合法** |
| SubagentStop | EXPLICIT/INFERRED completion permit 后、SubagentResult commit 前 | subagent coordinator continuation owner | 首次可 continue；第二次强制 terminal；failure terminal；无循环 | **唯一 seam 存在，P2 需补 settlement 机械规则** |
| Stop | ROOT public final assistant candidate 已冻结但尚未 terminal settle | ROOT assistant settlement/run loop | first continue：assistant nonterminal + context；second/engine failure：complete；cancel/failure 不触发 | **P1：规格当前晚了一次 canonical transaction** |

结论：11 项事件本身是有实际价值的完整最小集合，不应删减。PostCompact/SessionStart、Stop/SessionEnd、PreTool/Permission 分别观察不同 authority boundary，不是重复事件。问题在 producer/consumer 的机械定义，而不是事件集合过大。

---

## 最小 closed outcome union

建议只保留 5 种 authority family，diagnostics 作为所有 family 的正交字段：

| Outcome family | 值 | Event |
|---|---|---|
| `ObserveOutcome` | diagnostics only | SessionEnd；idle/terminal 无消费者降级 |
| `ContextOutcome` | ordered context entries | PostToolUse、SubagentStart |
| `GateOutcome` | `PROCEED \| BLOCK` + reason/context | SessionStart、UserPromptSubmit、PreToolUse、PreCompact、PostCompact |
| `PermissionOutcome` | `ABSTAIN \| ALLOW \| DENY` | PermissionRequest |
| `ContinuationOutcome` | `TERMINALIZE \| CONTINUE_ONCE` + reason/context | Stop、SubagentStop |

不应把 Permission、gate 和 continuation 合并为 generic `decision`：三者分别影响 human approval、当前 operation admission 和已有 run 的 terminalization，authority 不同。

### 应共享的 Hook core

只应有一套：

- source parser/normalizer及 physical bounds；
- generic `HookTrustStore`；
- event matcher与 tool aliases；
- command executor、sync/background slots、process-group owner；
- output parser与 per-handler validation；
- ordered control aggregator；
- current immutable view/reload replacement；
- exact-scope background/prompt-bound context state；
- `HOOK_CONTEXT` builder/compiler source；
- diagnostic/status adapter和 API-key scrub。

### 真正必要的事件专用逻辑

仅限：

- 在 existing owner seam 构造 11 个 typed input variants；
- 选择 event 的 matcher subject；
- 把 5-family outcome 交还给原 owner；
- SessionStart、Stop、SubagentStop 的 process-local once guard；
- PostTool 调用 existing Round 7.1 pure projection；
- Permission/tool/compaction owner 的 exact revalidation。

不得让 dispatcher 持有 Host、repository、ToolRuntime、permission controller 或 subagent coordinator。

### 当前无消费者或重复风险字段

- `additionalContextLimit`：需采用 P2-1 的无 artifact 语义，否则是 dormant field。
- `statusMessage`：需绑定现有 status/log sink。
- `trusted_at`：需明确 CLI inspection consumer，否则删除。
- 所有 event 共用的 `permission_mode/stop reason` mega DTO：应拆成 typed input variants，避免大量 event 上的空字段。
- source identity、definition provenance、dispatch scope、context scope：各自有合法用途，但不要在 attempt/outcome/context 中重复复制成 proof fields。

### 可能过度设计的机制

当前主规格已经正确禁止 registry generation、digest map、receipt、history、watcher和 Plugin-private engine。实施时还应额外防止：

- 复用 `OperationalHookType`/`KernelExtensionHost`；
- 每 event 一个 dispatcher/parser/decision DTO；
- generic mutable state patch outcome；
- 为 meta MCP exact join 添加 Hook-private route fingerprint；
- 为 once continuation增加 durable receipt；
- 为 context CAS 添加 Hook replay queue；
- Round 9.3 新建 trust/matcher/executor/context owner。

---

## 约束与边界最终判定

| 问题 | 判定 |
|---|---|
| 产品语义是否清楚 | 高层清楚：用户配置、观察/control 边界和禁止修改项都已写出；wire/outcome 和几个 terminal transition 尚未机械闭合 |
| USER/WORKSPACE complete/empty/unavailable/order | **正确**：absent=COMPLETE empty，malformed/raced=UNAVAILABLE，USER→WORKSPACE append，duplicate-looking 全运行 |
| ROOT/child/workspace isolation | 设计正确；需用 P2-3 reservation settlement 证明不会泄漏 |
| trust digest 是否位于真实边界 | **是**；属于用户 review 到 future cross-process execution 的真实边界，符合 [fingerprint hard-cut](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:77) |
| stable trust subject 与 versioned source identity | **正确分离** |
| reload future-only/predecessor view | **正确**；malformed replacement 不继续执行 old command，old in-flight attempt 可持 immutable definition 完成 |
| same-epoch SYSTEM/tools 不变 | 规格意图和 `reload_hooks` fixed descriptor 都正确；**P1-5 修订前尚未机械证明 compact Hook context 不修改 installed request** |
| messages append-only suffix | `HOOK_CONTEXT` 的 channel/trust/role 设计正确；需 P2-3 occurrence/CAS settlement |
| 是否创造第三种 rebase | 设计上没有；P1-5 必须采用 same-epoch pre-open suffix，不得原地改 request 或重编 root |
| long-horizon availability | **保持**；没有 model/tool/turn/task/session Hook 总次数或 lifetime cap |
| 是否有隐藏总 caps | **没有**。1 MiB source/capture、node/depth、slots、per-attempt timeout 是有物理理由的 carrier/operation bounds；continuation-once 是 control 语义，不是任务寿命 cap |
| small durability | **保持**；只有 local trust/enablement 配置，无 DB relation/event/job/receipt/replay |
| 七维 oracle | **自洽**：现有前六维为 `29/24/11/1/25/0`，新增独立 closed `HookEventType=11`，得到 `29 / 24 / 11 / 1 / 25 / 0 / 11` |
| Committed/Live/Hook vocabulary | 应继续三套独立 enum/计数；不得把现有 OperationalHook 或 HookEvent 并入前两项 |
| Round 9.3 边界 | **正确且独立**：其规格明确只复用 parser/trust/dispatcher/executor/context，并要求 `hooks/* -X-> plugins/*`；USER/WORKSPACE 已使 9.2 单独可用 |
| 是否适合直接交给 coding agent | **否**；先关闭全部 P1，至少冻结 P2-1/P2-3 的 contract 后再编码 |

Round 9.3 唯一需要注意的是：它允许 Plugin refresh 后的新 Hook view观察 PostCompact/SessionStart(compact)，因此依赖 P1-5 的 pre-open successor augmentation。除此之外，没有发现它要求第二套 Hook engine。

---

## 实际执行的只读探针与测试

只读检查包括：

- 完整读取 Round 9.2、AGENTS、fingerprint hard-cut；
- 窄读 Round 3.1、5、5B、7.1、9、10、Round 9.3；
- 用 `rg`、`nl -ba`、`sed` 核对 Host、runner、compaction、tool runtime、subagent、context compiler、continuity 和 oracle；
- 确认当前没有 `src/pulsara_agent/hooks/`，也没有 `HookEventType`/`KernelHookDispatcher`；
- 阅读用户指定的 [Codex Hooks 官方文档](https://learn.chatgpt.com/docs/hooks)；公开资料已足够，未用本地旧版 10-event 实现缩窄结论；
- 测试前后执行 `git status --short`，状态相同；工作区原有修改和 untracked spec 均未触碰。

使用仓库根 `.venv`、Python 3.12.12、`uv 0.10.7`，禁用 bytecode 和 pytest cache，实际运行：

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q -p no:cacheprovider \
  tests/test_stage2_architecture.py::test_stage2_registry_schema_and_removed_job_universe_are_exact \
  tests/test_round4_architecture.py::test_round4_final_oracles_and_plan_descriptors_are_exact \
  tests/test_round5_long_horizon_execution_envelope.py::test_round5_architecture_removes_turn_budget_and_preserves_oracles \
  tests/test_round5b_long_horizon_context_compaction.py::test_round5b_architecture_and_oracle_are_exact \
  tests/test_round7_1_provider_visible_tool_result_projection.py::test_round7_1_architecture_and_oracle_guards \
  tests/test_round3_1_provider_input_prefix_continuity.py::test_round3_1_continuity_owner_has_no_durable_or_task_authority \
  tests/test_stage2_conversation_runner.py::test_round5b_active_manual_compaction_adopts_and_continues_same_run \
  tests/test_stage2_conversation_runner.py::test_round5b_idle_manual_compaction_adopts_without_successor_open \
  tests/test_round9_unified_capability_semantics.py::test_round9_native_incompatible_mcp_routes_meta_without_poisoning_builtin \
  tests/test_round10_hierarchical_subagent_orchestration.py::test_round10_subagent_initial_seed_exact_joins_child_cut_and_none_sources
```

结果：`11 passed in 2.73s`。

这些测试证明现有 oracle、continuity、compaction、MCP meta 和 subagent 基线仍然健康；它们不能证明 Round 9.2，因为 Hook implementation 尚不存在。本轮未运行 PostgreSQL、full suite 或 real-provider dogfood，也未修改任何文件。

最终建议是：先做一次纯规格 hard cut，关闭上述 7 项 P1；不要让 coding agent 在实现中临时选择语义。修订完成后，这套设计不需要扩大 durability，也不需要改变其独立 Hook subsystem 的总体拓扑。