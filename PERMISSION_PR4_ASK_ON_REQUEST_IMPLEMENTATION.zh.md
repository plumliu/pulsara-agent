# Permission PR4: ASK / ON_REQUEST 实施计划

本文定义轻量权限系统 PR4 的落地方案。PR4 的目标很窄：在 approval resume 与 user stop 已经落地后，解除此前为避免不可恢复 halt 而加上的策略限制，让 `terminal_access=ask` 和可执行的 `approval_policy=on_request` 成为真正可用的权限能力。

## 0. Summary

PR4 做：

- 允许 `terminal_access=ask`。
- 允许 write / terminal 可用时的 `approval_policy=on_request`。
- 保持默认策略不变。
- 保持 hardline deny 不可批准。
- 为 ASK / ON_REQUEST 补齐单元测试、host 测试、CLI/inspect 测试和 real LLM smoke。

PR4 不做：

- durable approval request table。
- allow once/session/project/forever rule store。
- approval id 写入事件 schema。
- remote/mobile approval。
- MCP 权限模型。
- OS/container sandbox。
- 改变默认 `trusted_host + risky_only + terminal=allow` 的本地项目体验。

## 1. 为什么现在可以打开

早期权限 V1 文档把 `ASK` / executable `ON_REQUEST` 留到后续 PR，原因不是 gate 语义不存在，而是 `WAIT_FOR_USER` 当时不可恢复：

- runtime 能发 `RequireUserConfirmEvent`。
- reducer 能消费 `UserConfirmResultEvent`。
- 但 HostSession 没有 pending approval、approve/deny 入口，也不会继续同一个 suspended run。

现在这个阻塞已经消失：

- approval resume 已经能在同一个 `LoopState`、同一个挂起 `reply_id` 下 approve/deny 并继续执行。
- `HostSession` 会保存 `PendingApproval` 与 `_suspended_state`。
- ordinary new turn 会被 `HostSessionPendingApprovalError` 阻止，避免用户输入绕过 pending approval。
- user stop 已经能 abort suspended approval run，用户不想回答审批时可以显式停止。
- `_finalize_run()` 已有 finalize-once guard，stop / natural finish race 不会双发 terminal event。

因此 PR4 可以把过去的“定义了但禁用”的策略组合变成产品能力。

## 2. 当前代码基线

`src/pulsara_agent/runtime/permission.py` 已经具备大部分结构：

- `ApprovalPolicy.ON_REQUEST` 已存在。
- `TerminalAccess.ASK` 已存在。
- `PolicyPermissionGate` 已经会对 `terminal_access=ask` 返回 `WAIT_FOR_USER`。
- `PolicyPermissionGate` 已经会对 `approval_policy=on_request` 下的 `terminal` / `terminal_process` 返回 `WAIT_FOR_USER`。
- `PolicyPermissionGate` 已经会对 `approval_policy=on_request` 下的 `write_file` / `edit_file` 返回 `WAIT_FOR_USER`。
- hardline terminal command 与 hardline `terminal_process` stdin 会先返回 `DENY`，不会进入 approval。
- `terminal_access=off` 与 `read_only` 仍会隐藏或拒绝 terminal tools。

PR4 的主要代码阻塞在 `_validate_policy()`：

```python
if policy.terminal is TerminalAccess.ASK:
    raise ValueError("terminal_access=ask requires approval resume support and is not enabled in V1")

if policy.approval is ApprovalPolicy.ON_REQUEST and not (
    policy.profile is PermissionProfile.READ_ONLY and policy.terminal is TerminalAccess.OFF
):
    raise ValueError(...)
```

这些限制在 approval resume 前是正确的；PR4 应移除或重写它们。

同时有几处注释 / CLI help 仍停留在旧阶段：

- gate 分支注释说 `ask` / executable `on_request` 是 future branch。
- CLI `--terminal-access` help 说 ASK requires approval resume before practical use。
- approval resume 文档末尾仍把 PR4 写成后续项，这是历史记录，可以保留；PR4 文档负责定义新实施。

## 3. PR4 策略契约

### 3.1 默认策略不变

PR4 只开放用户显式选择，不改变默认能力矩阵：

- project workspace 默认仍是 `trusted_host + risky_only + terminal=allow`。
- inspect 默认仍是 `read_only + on_request + terminal=off`。
- transient / unknown 默认仍是 `read_only + on_request + terminal=off`。
- `workspace_guarded` profile 默认仍是 `risky_only + terminal=off`。

这保证 PR4 不把审批变成所有现有本地项目的默认高频交互。

### 3.2 `read_only` invariant 不变

`read_only` 仍然必须满足：

```text
profile = read_only
terminal_access = off
```

原因：

- `read_only` 的承诺是 built-in write tools 和 terminal tools 不可见或不可执行。
- `approval_policy=on_request` 在 `read_only + terminal=off` 下是 inert / future-facing：因为写工具和 terminal 工具已被 registry 过滤，不会产生可恢复审批。
- PR4 不应让 `read_only + terminal=ask` 或 `read_only + terminal=allow` 变成合法组合。

### 3.2.1 放开后的合法组合面

PR4 后，除 `read_only` 仍要求 `terminal=off` 外，三轴组合应保持正交：

- `trusted_host` 的 3 个 approval 值 x 3 个 terminal 值全部合法。
- `workspace_guarded` 的 3 个 approval 值 x 3 个 terminal 值全部合法。
- `read_only` 只允许 3 个 approval 值 x `terminal=off`。

也就是说，总合法组合是 21 个：`trusted_host` 9 个、`workspace_guarded` 9 个、`read_only` 3 个。默认策略只是默认，不代表其他显式组合非法。

需要特别点明两个交互：

- `terminal=ask + approval=never` 是合法且有意的组合。`ASK` 在 gate 顺序中先于 approval policy，因此 terminal 每次仍会要求确认；但非 terminal 写工具会按 `never` 直接执行。
- `approval=on_request + terminal=off` 也是合法组合。它会让 write tools 进入 approval，但 terminal tools 仍因 `terminal=off` 不可见或 fail closed。

### 3.3 `terminal_access=ask`

语义：

> terminal tools are available, but every terminal action requires a structured user approval before execution.

覆盖工具：

- `terminal`
- `terminal_process`

决策：

- hardline command / stdin 先 `DENY`，不可批准。
- `terminal_access=off` 不适用，因为 `ask` 已显式开启 terminal tool exposure。
- 每个 terminal / terminal_process call 返回 `WAIT_FOR_USER`。
- approve 后执行 exact original tool-call snapshot。
- deny 后生成 denied tool result，让模型继续。
- stop pending approval 后该 run 变为 `aborted`，不执行 pending tool。

`terminal_process` 也必须 ASK。不能出现 `terminal` 需要确认但 `terminal_process.write` / `submit` 能绕过的状态。

V1 的简单语义是：`terminal_access=ask` 会 gate 所有 `terminal_process` action，包括 `poll` / `wait` / `kill` / `write` / `submit`。这比最终理想 UX 更重，尤其是模型 yield 长进程后轮询时，`poll` / `wait` 也会反复进入 approval。PR4 接受这个保守语义，避免先引入 action-level policy 分叉。后续可以单独优化为：

- `write` / `submit` / `kill` 需要 approval。
- `poll` / `wait` 作为只读观察直接允许。
- hardline stdin 检查仍覆盖 `write` / `submit`。

Real smoke 应避免让模型启动需要 yield/poll 的长进程；使用短命令验证 ASK 链路即可。

### 3.4 `approval_policy=on_request`

语义：

> If a tool is otherwise visible and allowed by profile / terminal access, user confirmation is required before write-like or terminal-like side effects.

V1/PR4 覆盖：

- `write_file`
- `edit_file`
- `terminal`
- `terminal_process`

不覆盖：

- memory governance 写入策略。
- future MCP tools。
- skill-specific dynamic tool narrowing。
- network side effects beyond terminal command detection。

### 3.5 ASK 与 ON_REQUEST 同时出现

若用户选择：

```text
terminal_access = ask
approval_policy = on_request
```

terminal 工具只需要产生一次 approval request。决策优先级沿用当前 gate 顺序：

1. hardline deny
2. profile / tool exposure deny
3. `terminal_access=ask`
4. `approval_policy=on_request`
5. `approval_policy=risky_only`
6. inner gate

因此 terminal call 命中 `terminal_access=ask` 后即可返回 `WAIT_FOR_USER`，不需要再叠加一个 `terminal_on_request` reason。

### 3.6 `approval_policy=never` 仍受 hardline 限制

`trusted_host + never + terminal=allow` 可以执行普通 risky command，例如清理 workspace 内的 `build` 目录。

但 hardline command 仍不可批准、不可执行，例如：

- `rm -rf /`
- `rm -rf /*`
- `rm -rf ~`
- `rm -rf /home`
- `dd ... of=/dev/nvme0n1`
- `terminal_process.write/submit` 输入 hardline command

PR4 不能因为打开 ASK / ON_REQUEST 而削弱这条底线。

## 4. 用户可见行为

### 4.1 HostCore / long-lived host

HostCore / desktop host 是 PR4 的主路径：

1. 用户发起 turn。
2. 模型调用 write 或 terminal tool。
3. policy gate 返回 `WAIT_FOR_USER`。
4. HostSession 保存 pending approval。
5. UI 展示 tool call snapshot。
6. 用户 approve / deny / stop。
7. runtime 在同一 suspended run 内继续或 abort。

### 4.2 REPL

`host repl` 已有最小 approval 命令：

- `:approval`
- `:approve`
- `:deny`
- `:stop`

PR4 应确保这些命令在 `terminal_access=ask` / `approval_policy=on_request` 下可用，而不是只对 `risky_only` terminal approval 可用。

### 4.3 One-shot `host run`

`host run` 是一次性命令。若用户显式选择 `ask` 或 executable `on_request`，模型触发 approval 时，one-shot 模式可以输出 pending approval summary 并结束。

PR4 不要求 one-shot 模式交互式 approve。它应保持现有契约：

- 清楚提示该 run 正在等待 approval。
- 提示使用 REPL / HostCore / desktop host resolve。
- 不把 pending approval 伪装成 finished。

### 4.4 Inspect

`host inspect` 仍然只读，但应能展示用户显式选择后的 effective policy。例如：

```json
{
  "permissions": {
    "profile": "trusted_host",
    "approval_policy": "on_request",
    "terminal_access": "ask",
    "execution_boundary": "host",
    "network_isolated": false
  }
}
```

inspect 不应因为 `ask` / `on_request` 变成写操作，也不应创建 pending approval。

## 5. 代码修改计划

### PR4.1 Relax validator

修改 `_validate_policy()`：

- 保留 `read_only requires terminal_access=off`。
- 移除 `terminal_access=ask` 的全局拒绝。
- 移除 executable `approval_policy=on_request` 的全局拒绝。

建议新验证逻辑：

```python
def _validate_policy(policy: EffectivePermissionPolicy) -> None:
    if policy.profile is PermissionProfile.READ_ONLY and policy.terminal is not TerminalAccess.OFF:
        raise ValueError("read_only permission profile requires terminal_access=off")
```

是否要保留其他组合限制：

- V1 不需要限制 `workspace_guarded + on_request + off`，因为文件写工具可见时它会产生可恢复 approval，这正是 PR4 要打开的能力。
- V1 不需要限制 `trusted_host + on_request + allow/ask`，因为 approval resume 已经覆盖。

### PR4.2 Update gate comments

更新 `PolicyPermissionGate` 中旧注释：

- `terminal_access=ask` 不再是 future branch。
- executable `approval_policy=on_request` 不再是 unreachable branch。
- 可保留说明：这些 branch 依赖 approval resume，当前已由 HostSession 支持。

不要改变 gate 决策顺序，除非测试暴露实际 bug。

### PR4.3 Update CLI help

更新 `--terminal-access` help：

旧语义：

```text
ASK is defined but requires approval resume before practical use.
```

新语义建议：

```text
Terminal access policy. ask requires approval before each terminal action.
```

`--approval-policy on_request` 的 help 可以补一句：

```text
on_request asks before write and terminal side-effect tools when those tools are available.
```

### PR4.4 Keep defaults unchanged

确认以下函数不改默认返回：

- `default_permission_policy(workspace_kind="project", intent="run")`
- `default_permission_policy(intent="inspect")`
- `_profile_default(PermissionProfile.WORKSPACE_GUARDED, ...)`
- `_profile_default(PermissionProfile.READ_ONLY, ...)`

PR4 只让用户显式 flag/env 能选择 ASK / executable ON_REQUEST。

## 6. 测试计划

### 6.1 Permission policy unit tests

修改现有负向测试：

- `test_resolve_permission_policy_rejects_ask_until_approval_resume_exists`
  - 改为 accepts `trusted_host + terminal=ask`。
- `test_resolve_permission_policy_rejects_on_request_for_write_capable_profiles`
  - 改为 accepts `workspace_guarded + approval=on_request + terminal=off`。

新增：

- accepts `trusted_host + approval=on_request + terminal=allow`。
- accepts `trusted_host + approval=on_request + terminal=ask`。
- accepts `trusted_host + approval=never + terminal=ask`，并记录 ASK 压过 NEVER 的 terminal 语义。
- accepts `workspace_guarded + terminal=ask`。
- still accepts `read_only + approval=on_request + terminal=off`。
- still rejects `read_only + terminal=ask`。
- still rejects `read_only + terminal=allow`。
- env-only resolution can choose `PULSARA_TERMINAL_ACCESS=ask`。
- CLI args still override env for `ask` / `on_request`。

Gate tests:

- `terminal_access=ask` returns `WAIT_FOR_USER` for `terminal`。
- `terminal_access=ask` returns `WAIT_FOR_USER` for `terminal_process`。
- `terminal_access=ask` returns `WAIT_FOR_USER` for read-like `terminal_process` actions such as `poll` / `wait` in PR4 V1。
- `approval_policy=on_request` returns `WAIT_FOR_USER` for `write_file`。
- `approval_policy=on_request` returns `WAIT_FOR_USER` for `edit_file`。
- `approval_policy=on_request + terminal=allow` returns `WAIT_FOR_USER` for `terminal`。
- `approval_policy=on_request + terminal=allow` returns `WAIT_FOR_USER` for `terminal_process`。
- `terminal_access=ask + approval_policy=on_request` returns one decision with `terminal_access_ask` reason。
- hardline terminal command still returns `DENY` under `terminal_access=ask`。
- hardline terminal_process stdin still returns `DENY` under `approval_policy=on_request`。

### 6.2 Registry / inspect tests

Existing registry tests should still pass. Add or update:

- `trusted_host + terminal=ask` registry contains `terminal` and `terminal_process`。
- `workspace_guarded + approval=on_request + terminal=off` registry contains write tools but not terminal tools。
- `read_only + on_request + off` still hides write tools and terminal tools。
- `host inspect --terminal-access ask` reports `terminal_access: "ask"` and matching tool exposure。
- `host inspect --permission-profile trusted_host --approval-policy on_request --terminal-access allow` reports the effective policy without side effects。

### 6.3 Runtime / HostSession tests

Use scripted transport to avoid provider variance:

- `terminal_access=ask` produces pending approval for harmless terminal command such as `printf PULSARA_ASK_OK`。
- approving that pending terminal call executes the exact snapshot and reaches final answer。
- denying that pending terminal call returns denied tool result and reaches final answer。
- `approval_policy=on_request` for `write_file` produces pending approval。
- approving write executes and writes expected file。
- denying write leaves file absent and model sees denial。
- stopping a pending `ask` approval aborts the run and clears pending approval。
- ordinary new turn while pending still raises `HostSessionPendingApprovalError`。
- `host run` one-shot with an approval-producing policy returns pending approval summary instead of pretending success。

### 6.4 Real LLM smoke

PR4 should add two lightweight real LLM tests because approval becomes a common path, not just a rare risky-command path.

Smoke 1: `terminal_access=ask`

- workspace: temp directory。
- policy: `trusted_host + risky_only + terminal=ask`。
- prompt asks model to run harmless terminal command:

```text
printf PULSARA_TERMINAL_ASK_OK
```

- expected:
  - first run status `waiting_user`。
  - pending approval contains `terminal` call。
  - approve all。
  - final text contains `PULSARA_TERMINAL_ASK_OK`。
  - provider finish reason is `stop` when available。
  - no `RunErrorEvent`。

Smoke 2: `approval_policy=on_request` for write

- workspace: temp directory。
- policy: `workspace_guarded + on_request + terminal=off`。
- prompt asks model to create a tiny file, then report sentinel。
- expected:
  - first run status `waiting_user`。
  - pending approval contains `write_file` or `edit_file`。
  - approve all。
  - final file content contains `PULSARA_ON_REQUEST_WRITE_OK`。
  - final text reports sentinel。
  - provider finish reason is `stop` when available。
  - no terminal tools are visible。

Provider note：real smoke 应使用充足 `max_output_tokens`，建议至少 512。此前 DashScope / reasoning 模型实验显示，过紧 token budget 可能把预算消耗在 thinking 阶段，导致 final text 为空；这会把“approval 链路失败”和“模型输出预算耗尽”混在一起。测试应尽量断言 finish reason 为 `stop`，或至少在失败诊断中输出 finish reason / raw provider metadata。

Optional deny smoke can remain scripted rather than real LLM. Approve path is enough to prove model/tool/resume loop can complete under the newly opened policies.

## 7. Acceptance Criteria

PR4 is done when:

- `resolve_permission_policy(profile="trusted_host", terminal="ask")` succeeds。
- `resolve_permission_policy(profile="workspace_guarded", approval="on_request", terminal="off")` succeeds。
- `resolve_permission_policy(profile="trusted_host", approval="on_request", terminal="allow")` succeeds。
- `resolve_permission_policy(profile="trusted_host", approval="never", terminal="ask")` succeeds and terminal still asks。
- `resolve_permission_policy(profile="workspace_guarded", terminal="ask")` succeeds。
- `resolve_permission_policy(profile="read_only", approval="on_request", terminal="off")` succeeds。
- `read_only + terminal != off` still fails。
- `terminal_access=ask` produces resumable pending approval, not unrecoverable halt。
- executable `approval_policy=on_request` produces resumable pending approval, not unrecoverable halt。
- approve / deny / stop all work for pending approvals generated by ASK / ON_REQUEST。
- hardline commands remain denied and do not emit approval requests。
- `terminal_process` follows the same terminal access / approval rules as `terminal`。
- default policies and existing full-access workflows remain unchanged。
- real LLM smoke passes for one terminal ASK path and one write ON_REQUEST path。

## 8. Suggested commit shape

One commit is enough if the diff stays small:

```text
Enable resumable ask/on_request permission policies
```

Expected files:

- `src/pulsara_agent/runtime/permission.py`
- `src/pulsara_agent/cli.py`
- `tests/test_permission_policy.py`
- `tests/test_host_core.py`
- `tests/test_agent_runtime_loop.py` if runtime-level approval helpers need direct coverage
- `tests/test_cli_host.py`
- `tests/test_real_llm_integration.py`

If real LLM tests become flaky during implementation, split into:

1. policy unlock + scripted tests
2. real LLM smoke

Do not split into a new survey doc; the survey rationale already lives in `LOCAL_AGENT_SANDBOX_PERMISSION_SURVEY.zh.md` and `APPROVAL_RESUME_SURVEY.zh.md`。

## 9. Open Risks

### 9.1 One-shot UX

`host run` cannot resolve approval interactively. PR4 may make it easier for users to choose a policy that immediately returns pending summary in one-shot mode.

This is acceptable for PR4 if the message is clear. A richer one-shot interactive approval prompt can be a later CLI UX PR。

### 9.2 Model compliance

Under `terminal_access=ask`, the model may need one extra continuation after approval to summarize tool results. Existing approval resume tests cover the runtime path, but real LLM smoke is necessary to catch prompt/tool-call behavior。

### 9.3 No durable approval

Pending approval remains host-session scoped and in-memory. If the process dies, pending approval is lost. This is already the approval resume V1 contract; PR4 should not change it。

### 9.4 `terminal_process` edge cases

`terminal_process.write` / `submit` can carry arbitrary stdin into an existing process. Current hardline input checks cover the catastrophic examples, but this is still a host-side guardrail, not an OS sandbox. PR4 should not overclaim safety。

## 10. Design Principle

PR4 should make approval a recoverable product loop, not a new safety mythology.

`ASK` means “pause and let the user decide this exact action.” It does not mean Pulsara has a real sandbox. `ON_REQUEST` means “side effects become explicit approval points.” It does not mean durable policy learning or permanent trust rules.
