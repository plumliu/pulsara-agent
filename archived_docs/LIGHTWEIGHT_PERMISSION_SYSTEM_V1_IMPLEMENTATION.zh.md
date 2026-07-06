# Pulsara 轻量权限系统 V1 实施文档

本文描述 Pulsara V1 的轻量权限系统。它基于当前代码现状和本地 agent 项目调研，明确把 full access 当作一等公民，同时不把 host-side guardrail 夸大成 OS sandbox。

## 0. 非目标

V1 不实现真正沙箱：

- 不实现 macOS Seatbelt。
- 不实现 Linux Landlock。
- 不实现 Windows restricted token。
- 不实现 Docker/SSH/remote execution backend。
- 不实现网络隔离或 domain allowlist。
- 不声称 `terminal` 只能读写 workspace。

V1 的目标是建立稳定的权限词汇、统一的策略入口、清晰的 inspect 输出，以及少数灾难动作的底线。

## 当前代码事实

这份计划依赖几个已经存在的事实：

- `PermissionDecisionKind.WAIT_FOR_USER` 已存在，但今天没有完整的 approval resume 路径。`UserConfirmResultEvent` 有事件类型和 reducer 分支，但没有 host producer；`HostSession.run_turn()` / `stream_turn()` 每轮都会 `new_state()`，不会保留 pending tool call。
- `build_core_tool_registry()` 在 `AgentRuntime` / session 构造期间建立一次，之后每轮复用同一个 registry。Capability resolver 是每条用户消息 resolve 一次，但 registry 不是。
- `terminal` 与 `terminal_process` 共用同一个 `TerminalSessionManager` 和 `ProcessRegistry`，必须被同一条 terminal policy 管住。
- 当前代码没有 MCP 工具注册层。V1 的 `read_only` 只对 built-in tools 作承诺，不对未来 MCP 工具作安全声明。

## 1. 设计原则

### 1.1 Full Access 是一等模式

本地 agent 的主流使用方式经常是高权限。Pulsara 不把它藏成“危险后门”，而是明确命名：

`trusted_host`

它表示：这是用户本地可信工作区，agent 可以像用户自己的 shell 一样大展拳脚。系统仍然可以记录、展示、审计，并对极少数灾难动作保留硬阻断或确认。

### 1.2 Approval 与 Permission 分离

`PermissionProfile` 表达“原则上能做什么”。

`ApprovalPolicy` 表达“做之前是否要问”。

这两个维度不能混成一个布尔开关。否则 `workspace_write_but_ask_network`、`trusted_host_but_confirm_catastrophic` 这类真实需求会变得很别扭。

### 1.3 Terminal 单独建模

文件工具可以被 workspace path guard 约束。

`terminal` 不可以被描述成 workspace-bound。它在宿主机 shell 中执行，只要用户允许 shell，命令就可能通过解释器、子进程、网络、绝对路径等方式越过 Python 侧路径检查。

所以 V1 中 terminal 必须有独立字段：

`TerminalAccess`

### 1.4 Guardrail 不是安全边界

V1 的危险命令正则、workspace path check、env sanitize 都是 guardrail，不是多租户隔离边界。

安全边界只能来自 OS/container/remote backend。V1 只为未来接入这些 backend 留接口。

## 2. 新增核心类型

建议新增模块：

`src/pulsara_agent/runtime/permissions.py`

或保留现有 `runtime/permission.py` 并扩展。命名上建议从单数 `permission.py` 迁移到复数 `permissions.py`，但可以为了小步变更先在原文件内扩展。

### 2.1 PermissionProfile

```python
class PermissionProfile(StrEnum):
    TRUSTED_HOST = "trusted_host"
    WORKSPACE_GUARDED = "workspace_guarded"
    READ_ONLY = "read_only"
```

语义：

- `trusted_host`
  - 文件工具可读写 workspace。
  - terminal 允许宿主机执行。
  - 不承诺 workspace 外隔离。
  - 适合本地可信用户主动启动的项目。

- `workspace_guarded`
  - 文件工具可读写 workspace。
  - terminal 默认关闭或需要策略确认；在 approval resume 完成前默认关闭。
  - 一旦开启 terminal，必须明确展示“host terminal”事实。
  - 适合普通项目默认 guardrail。

- `read_only`
  - 文件写工具不暴露或执行时拒绝。
  - terminal 默认关闭。
  - 适合 inspect/review、不可信 workspace、远程入口、自动任务默认值。

### 2.2 ApprovalPolicy

```python
class ApprovalPolicy(StrEnum):
    NEVER = "never"
    RISKY_ONLY = "risky_only"
    ON_REQUEST = "on_request"
```

语义：

- `never`: 不为普通工具调用询问。灾难底线仍可 hard block。
- `risky_only`: 仅危险 terminal 命令、敏感路径写入、策略变更等需要确认。
- `on_request`: 写入、terminal、外部副作用类工具默认需要确认。

### 2.3 TerminalAccess

```python
class TerminalAccess(StrEnum):
    OFF = "off"
    ALLOW = "allow"
    ASK = "ask"  # Requires approval resume support before becoming generally usable.
```

语义：

- `off`: 不暴露 `terminal` / `terminal_process`，或执行时 fail closed。
- `allow`: 普通 terminal 命令直接执行，危险命令按 `ApprovalPolicy` 与灾难底线处理。
- `ask`: 每次 terminal 调用进入 approval gate；只有在 approval resume 路径实现后才能作为可用配置。

V1 可以先定义 `ask`，但不能在默认配置中启用它，也不能在没有 resume 机制时把它作为可靠功能发布。

### 2.4 EffectivePermissionPolicy

```python
@dataclass(frozen=True, slots=True)
class EffectivePermissionPolicy:
    profile: PermissionProfile
    approval: ApprovalPolicy
    terminal: TerminalAccess
    execution_boundary: Literal["host"] = "host"
    network_isolated: bool = False
```

V1 中 `execution_boundary` 固定为 `host`，`network_isolated` 固定为 `False`。这两个字段是为了 inspect 诚实展示，而不是为了伪装已有能力。

## 3. 默认策略

V1 推荐默认：

### 3.1 本地 project workspace

```text
profile = trusted_host
approval = risky_only
terminal = allow
execution_boundary = host
network_isolated = false
```

理由：本地用户往往希望 agent 高能力工作。Pulsara 应尊重这个现实，同时保留少数高风险确认。

### 3.2 transient / inspect / review-like 场景

```text
profile = read_only
approval = on_request
terminal = off
execution_boundary = host
network_isolated = false
```

`host inspect` 必须保持只读无副作用，不触发 sync、不创建权限状态。

### 3.3 未来远程入口 / 自动任务 / 子 agent

```text
profile = workspace_guarded
approval = risky_only
terminal = off
execution_boundary = host
network_isolated = false
```

这些入口在 V1 先关闭 terminal，并使用不会产生日常确认停顿的 `risky_only`。等 approval resume 和/或真实 sandbox backend 完成后，再考虑改为 `approval = on_request` 或 `terminal = ask`。

## 3.4 组合规则

三轴是独立字段，但 policy resolver 可以给不同入口选择默认组合。除非特别说明，`profile` 不会隐式覆盖用户显式设置的 `terminal` 或 `approval`。

优先级规则：

- `TerminalAccess.OFF` 是 profile 级拒绝：terminal 工具不可见或执行时 fail closed。
- `TerminalAccess.ALLOW` 只表示 terminal 不被 profile 级拒绝；`ApprovalPolicy.ON_REQUEST` 仍然可以要求确认。若确认会产生 `WAIT_FOR_USER`，必须等 approval resume 完成后才能启用。
- `ApprovalPolicy.RISKY_ONLY` 只确认风险动作；普通 terminal 命令可直接执行。
- `ApprovalPolicy.NEVER` 不确认普通风险动作，但 hardline blocklist 仍然生效。
- `TerminalAccess.ASK` 等价于“所有 terminal 调用都需要 approval”，但要等 approval resume 完成后才能开启。

## 4. 配置与 CLI

V1 可以先支持环境变量和 CLI 参数，后续再落入 `~/.pulsara/config.toml`。

建议：

```text
--permission-profile trusted_host|workspace_guarded|read_only
--approval-policy never|risky_only|on_request
--terminal-access off|ask|allow
```

环境变量：

```text
PULSARA_PERMISSION_PROFILE
PULSARA_APPROVAL_POLICY
PULSARA_TERMINAL_ACCESS
```

优先级：

1. CLI 参数
2. 环境变量
3. host/workspace 默认值

实现细节：CLI 参数必须使用 `default=None` 之类的哨兵值区分“用户没有传 flag”和“用户显式传了某个值”。不能让 argparse 默认值静默覆盖环境变量。

在 approval resume 完成前，resolver 应拒绝会产生日常 `WAIT_FOR_USER` 的可执行组合，例如 `terminal_access=ask` 或写/terminal 可用时的 `approval_policy=on_request`。`read_only + on_request + off` 允许存在，因为它不会暴露会触发确认的写入或 terminal 工具。

`host inspect` 输出 effective policy，但不写任何文件。

## 5. 工具接入

### 5.1 文件工具

当前 `WorkspaceTool._resolve_path()` 已经限制路径在 workspace root 内。

V1 增加 read-only gate：

- `read_only`: 允许 `read_file`、`search_files`。
- `read_only`: 拒绝或不暴露 `write_file`、`edit_file`。
- `workspace_guarded` / `trusted_host`: 保持 workspace 内写入。

V1 不允许文件工具写 workspace 外路径，即使 `trusted_host` 也不通过文件工具做这件事。需要 workspace 外操作时，让模型使用 terminal，并让 UI/inspect 明确 terminal 是 host-level 能力。

### 5.2 Terminal 工具

当前 terminal 已有：

- workdir 限制在 workspace 内。
- 危险命令正则。
- shell background wrapper guidance。
- env sanitize。
- process lifecycle 管理。

V1 接入策略：

- `TerminalAccess.OFF`: 不暴露 `terminal` / `terminal_process`，或执行时返回 policy error。
- `TerminalAccess.ALLOW`: 普通命令允许；危险命令按 approval policy 处理。
- `TerminalAccess.ASK`: 所有 terminal 调用返回 `WAIT_FOR_USER`，但必须等 approval resume 路径完成后才可启用。

`terminal_process` 必须继承同一个 terminal access。不能出现 `terminal` 关闭但 `terminal_process` 可操作已有进程的绕过。

具体要求：

- OFF/read-only 过滤或拒绝 `terminal` 和 `terminal_process` 两个工具。
- 不能只过滤 `terminal` 而留下 `terminal_process`。
- 即使 registry 过滤正确，`terminal_process` 执行入口也应保留 fail-closed 检查作为纵深防御。

### 5.3 记忆工具

V1 不改变 memory 工具权限。

但 inspect 应显示 memory tools 是否启用、memory graph_id、read scopes。不要把 memory 权限混入 filesystem sandbox 语义。

### 5.4 Skill 与 MCP

V1 不做 skill-driven tool narrowing。

Skill 的 `provides_tools` 仍是“常用/建议工具”元数据，不参与实际权限裁决。

当前代码没有 MCP 工具注册层。MCP 工具未来应进入同一权限模型，但 V1 只覆盖 built-in tools；因此 `read_only` 在 V1 中只表示 built-in write/terminal 能力受限，不对未来 MCP 写工具作承诺。

## 5.5 Approval Resume 前置项

`TerminalAccess.ASK` 和 `ApprovalPolicy.ON_REQUEST` 如果会产生 `WAIT_FOR_USER`，必须先有可恢复的确认流程。

当前缺口：

- `RequireUserConfirmEvent` 可以被发出。
- `UserConfirmResultEvent` 可以被 reducer 消费。
- 但 host 没有接收用户确认、生成 `UserConfirmResultEvent`、并从 pending tool call 继续执行的入口。
- 每个新 turn 都会创建新 `LoopState`，不会自动恢复上一轮的 pending tool calls。

因此 V1 PR 必须把 ASK 拆成两个阶段：

1. **PR0 / PR3 前置**：实现 approval resume。
   - 记录 pending tool calls 与对应 run/reply/turn。
   - host 提供确认/拒绝入口。
   - 确认后发出 `UserConfirmResultEvent`。
   - 继续执行被批准的 tool calls，且不会重新触发同一批 gate。
2. **后续**：启用 `TerminalAccess.ASK` 或 `ApprovalPolicy.ON_REQUEST` 对日常 terminal/write 工具生效。

在 resume 路径完成前，V1 默认只能安全使用：

- `trusted_host + risky_only + allow`
- `read_only + on_request + off`，但这里的 `on_request` 不应产生可恢复确认，只用于 inspect/未来语义展示。

## 6. Approval Gate

现有 `PermissionGate` 是骨架，`TerminalPolicyPermissionGate` 只处理危险 terminal 命令。V1 不应叠加两个独立 terminal gate，否则危险命令、sensitive path 和 hardline blocklist 会重复裁决。

V1 应新增统一 gate，并把 `TerminalPolicyPermissionGate` 中的危险 terminal 逻辑折进去：

```python
class PolicyPermissionGate:
    def __init__(self, policy: EffectivePermissionPolicy, inner: PermissionGate): ...
```

决策顺序：

1. 先跑 hardline blocklist。
2. 根据 profile 拒绝不允许的工具。
3. 根据 terminal access 处理 terminal。
4. 根据 approval policy 决定 allow / wait_for_user。
5. 再委托 inner gate。

推荐最终包裹结构：

```python
permission_gate = PolicyPermissionGate(policy, inner=permission_gate or AllowAllPermissionGate())
```

不要再外层额外套 `TerminalPolicyPermissionGate`。保留旧类可以做兼容，但 runtime 应只走一个 terminal policy owner。

### 6.1 Hardline Blocklist

即使 `trusted_host + never`，也建议 hard block 极少数灾难动作：

- `rm -rf /`
- 覆写块设备，如 `dd ... of=/dev/disk*`、`/dev/sd*`
- `mkfs`
- `shutdown` / `reboot`
- 明确针对 Pulsara 自己安全配置的自动改写

普通高风险动作，例如 `git reset --hard`、`rm -rf build`、`sudo`，不应全部 hard block。它们属于 `risky_only` 的审批范畴。

### 6.2 Sensitive Paths

V1 可以先只在 terminal 正则里识别。文件工具已经被 workspace containment 限制，正常够不到 `~/.ssh` 这类路径；sensitive-path 检查主要是 terminal-only guardrail。

- `~/.ssh`
- `.env`
- `~/.pulsara/config.*`
- shell rc 文件
- credential files such as `.netrc`, `.npmrc`, `.pypirc`

这类操作在 `risky_only` 下要求确认。

## 7. Registry 策略

V1 有两种可选实现路径：

### 7.1 执行时拒绝

工具仍全部暴露，但执行时返回 policy error。

优点：实现小，错误清楚。

缺点：模型仍可能调用不可用工具。

### 7.2 构建 registry 时过滤

`build_core_tool_registry(..., permission_policy=...)` 根据 policy 不注册写工具或 terminal 工具。

优点：模型不会看到不可用工具。

缺点：工具集随 policy 变化，需要明确 registry 生命周期。

推荐 V1 采用 7.2，但必须遵守：

- policy 在 `AgentRuntime` / host session open 时 resolve 一次。
- session 生命周期内 policy 与 registry 固定不变。
- 不根据 skill 激活状态动态改变工具集。
- 若未来需要 per-turn policy，必须改成 per-message registry rebuild；这不是 V1。

这比 capability resolver 更粗一层：capability resolver 是每条用户消息 resolve 一次；registry V1 是每个 session 构造一次。二者都要求一个 model loop 内工具集稳定，但生命周期不同。

需要穿透的调用点：

- `tools/builtins/registry.py::build_core_tool_registry`
- `runtime/session.py::RuntimeSession.create_tool_executor`
- `runtime/agent.py::AgentRuntime.__init__`
- `runtime/wiring.py::build_agent_runtime_wiring`
- `cli.py::_host_inspect`

`host inspect` 自己会构造一次 registry，因此必须传入同一套 effective policy；否则 inspect 显示的工具列表会和 permissions 字段不一致。

## 8. Inspect 输出

`pulsara host inspect` 应输出：

```json
{
  "permissions": {
    "profile": "trusted_host",
    "approval_policy": "risky_only",
    "terminal_access": "allow",
    "execution_boundary": "host",
    "network_isolated": false,
    "filesystem": {
      "file_tools": "workspace_only",
      "terminal": "host_shell"
    }
  }
}
```

注意：

- inspect 只读。
- inspect 不 sync bundled skills。
- inspect 不创建权限文件。
- inspect 不启动真实 sandbox。

## 9. 测试计划

### 9.1 Policy 单元测试

- `trusted_host + never + allow` 允许普通 terminal。
- `workspace_guarded + risky_only + off` 不暴露或拒绝 terminal。
- `read_only` 禁止写工具。
- `read_only` 禁止 terminal。
- hardline command 在任何 profile 下都 block。
- `TerminalAccess.ASK` 在 approval resume 未实现前不进入默认矩阵；若先定义枚举，只测试 policy resolver 不把它选为默认值。

### 9.2 Registry 测试

- `read_only` registry 不包含 `write_file`、`edit_file`、`terminal`、`terminal_process`。
- `workspace_guarded` registry 包含文件写工具，但 terminal 可按配置包含或排除。
- `trusted_host` registry 保持当前高能力工具集。

### 9.3 Host Inspect 测试

- inspect 返回 effective permission policy。
- inspect 不写 `${PULSARA_HOME}`。
- inspect 不启动 bundled sync。

### 9.4 Terminal 回归测试

- 现有 workdir escape 测试保持通过。
- 现有 dangerous command 测试迁移到 policy gate 后仍通过。
- `terminal_process` 不绕过 terminal access。

## 10. PR 切分

建议分三步：

### PR0: Approval Resume

只有当本轮要启用 `TerminalAccess.ASK` 或真正的 `ApprovalPolicy.ON_REQUEST` 时才需要先做。

- host 记录 pending tool calls。
- 用户确认/拒绝后生成 `UserConfirmResultEvent`。
- runtime 能从 waiting approval 继续执行已批准的 tool calls。
- 防止同一批 tool calls 被二次 gate。

### PR1: 类型与 inspect

- 新增 permission policy 类型。
- 解析 CLI/env/default。
- host inspect 输出 effective policy。
- 不改变实际工具行为。

### PR2: Registry 与文件工具接入

- `build_core_tool_registry` 接收 policy。
- `read_only` 过滤写工具。
- 测试 registry/tool exposure。

### PR3: Terminal 接入与 approval gate

- terminal access 生效。
- hardline blocklist 与 risky approval 归一。
- `terminal_process` 继承 terminal access。
- 保持现有 terminal runtime 行为不回退。
- 若 PR0 未完成，PR3 只能支持 `off` / `allow`，不能把 `ask` 暴露为可用配置。

## 11. 命名建议

用户可见名称：

```text
trusted_host
workspace_guarded
read_only
```

避免使用：

```text
safe
unsafe
secure
sandboxed
```

除非真的接入 OS/container backend，否则不要把 V1 profile 叫 `sandboxed`。
