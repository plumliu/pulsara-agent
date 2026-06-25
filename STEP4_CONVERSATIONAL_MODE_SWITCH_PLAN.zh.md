# Step 4 — 对话内权限切换:mode 作为可变会话状态 + plan 入轴(gate 唯一权威)

_Created: 2026-06-25_

> 状态:**计划草案,待讨论**。这是 §4 permission cut 的延续(Step 1 预设层+翻 bypass、Step 2 approval 主路径回归、Step 3 hardline 跨入口+降级 risky_only/workspace_guarded 之后)。本文先落档供讨论,确认后再编码。

## Context

到 Step 3 为止,权限在 `open_session` 那一刻被烤进三处(gate 引用、registry 工具过滤、终端工具的 policy 字段快照),整个对话生命周期固定,中途无法切换。需求:做到对话内切换。并已确认 plan mode 本质就是 mode 轴上的一个成员(read-only 约束 + 用户发起的出口切换),不另起子系统。

经过取舍已锁定的设计:

- **机制 = gate 唯一权威 + 静态提示**(不是重建 executor 藏工具)。理由:tools 数组在所有 mode 下恒定 → KV/前缀缓存全程不失效(核心诉求);切换只改一个引用、零重建;且与 Step 3 "gate 单一强制点" 方向一致。代价:read-only/plan 下模型仍看得见 write/terminal,试了会被 DENY——用固定 system prompt 里一句静态说明压低误试。
- **切换权 = 仅用户/host**。Agent 没有自切 mode 的工具,杜绝提权漏洞。plan→execute 是用户看完 plan 后亲自切——用户的输入即批准。
- **切换时机 = 轮边界**。运行中 / pending approval / stopping 时拒绝。
- **hardline 不变量**:与任何 mode、任何切换无关,永远 DENY(契约 §5)。

探查已核实的关键事实:

1. `AgentRuntime.permission_policy` 是普通可变属性;`PolicyPermissionGate` 每轮读 `self.policy`(agent.py:799 → permission.py:211-217)。
2. gate 的 `_evaluate_call` **已经**用 `is_tool_allowed_by_policy` 对 read_only 的 write/terminal 返回 DENY(permission.py:247)。所以**去掉 registry 过滤不会丢强制力**——gate 早就覆盖了 registry 过滤做的事。
3. `terminal_sessions` / `event_log` / `artifact_service` 都挂在 `RuntimeSession`,**不随任何重建丢失**(本方案也不重建)。
4. 终端工具在 terminal.py:139 / terminal_process.py:73 读 `self.permission_policy.terminal is OFF`(快照),切换后会 stale——本方案用**可变 holder** 让它们读到最新。
5. `registry.names()` 喂 `visible_tool_names`(capability resolver)与 `host inspect`。去过滤后**模型可见 catalog 跨 mode 恒定**——正是缓存稳定的前提。

## 不做(明确排除)
- 不重建 executor / registry(本方案靠 holder,零重建)。
- 不给 Agent 任何自切 mode 的工具。
- 不做 plan mode 的**行为塑形**(注入"你在规划、先提案"的指引)——本步只交付切换原语 + plan 作为权限层成员(= read_only 等价权限)。行为塑形列为后续 Step 5(它会动 context 组装,单独评估缓存影响)。
- 不删 `RISKY_ONLY` / `WORKSPACE_GUARDED`(仍是合法自定义轴值)。
- 一次性 `host run` 不提供切换点(无 REPL 循环);切换面向 REPL / 编程 API。

## 代码改动

### 1. `src/pulsara_agent/runtime/permission.py` — 可变 holder + plan 成员

新增可变 holder(value 仍 frozen,holder 可变):
```python
@dataclass(slots=True)
class PermissionState:
    """Mutable holder so gate + tools read one live policy reference.
    Switching mode mutates .policy/.mode in place; everyone sees it next turn."""
    policy: EffectivePermissionPolicy
    mode: PermissionMode | None = None   # None = custom three-axis
```

`PermissionMode` 加成员:
```python
class PermissionMode(StrEnum):
    READ_ONLY = "read-only"
    PLAN = "plan"               # 新增
    ASK_PERMISSIONS = "ask-permissions"
    ACCEPT_EDITS = "accept-edits"
    BYPASS_PERMISSIONS = "bypass-permissions"
```
`_PRESET_POLICIES[PLAN]` = 与 read_only 同(read_only profile / on_request inert / terminal off)。契约层注明:plan 权限等价 read_only,差异是 workflow 意图(由用户切出到执行 mode),行为塑形后续再补。

`PolicyPermissionGate` 改为持 holder:
```python
class PolicyPermissionGate:
    def __init__(self, state: PermissionState, inner: PermissionGate) -> None:
        self._state = state
        self.inner = inner
    @property
    def policy(self) -> EffectivePermissionPolicy:   # 兼容现有读法
        return self._state.policy
```
内部 `_evaluate_call` / `_evaluate_terminal_call` 改读 `self._state.policy`(或经 `self.policy` property)。hardline 判定不变。

### 2. `src/pulsara_agent/tools/builtins/registry.py` — 全量注册

- `build_core_tool_registry` 不再按 policy 过滤:所有工具无条件 `registry.register(...)`。删除 `_register_if_allowed` 的 policy 分支(或直接全注册)。
- 终端工具改为接收 `PermissionState` holder(而非 frozen 快照),透传给 TerminalTool/TerminalProcessTool。

### 3. 终端工具 — 读 holder 而非快照

`terminal.py` / `terminal_process.py`:字段从 `permission_policy: EffectivePermissionPolicy` 改为 `permission_state: PermissionState | None`;terminal.py:139 / terminal_process.py:73 的 `terminal_access_off` 检查改读 `self.permission_state.policy.terminal`。hardline 检查不变(本就调 `is_hardline_terminal_command`)。

### 4. `src/pulsara_agent/runtime/agent.py` — 持 holder + 切换方法

- `__init__`:构造 `self._permission_state = PermissionState(policy, mode)`;gate 用 holder 构造;`create_tool_executor` 透传 holder。
- 新增:
```python
def set_permission_policy(self, policy, *, mode=None) -> None:
    self._permission_state.policy = policy
    self._permission_state.mode = mode
@property
def permission_policy(self): return self._permission_state.policy
```
切换 = 改 holder 一处,gate 下一轮、终端工具下次执行都读到新值。**不重建任何东西。**

### 5. `src/pulsara_agent/host/session.py` — 轮边界 setter

```python
def set_permission_mode(self, mode: str | PermissionMode) -> EffectivePermissionPolicy:
    if self.closed: raise RuntimeError(...)
    if self.stopping_run_id is not None: raise HostSessionBusyError(...)
    if self.pending_approval is not None: raise HostSessionPendingApprovalError(...)
    if self._run_lock.locked(): raise HostSessionBusyError(...)
    policy = preset_to_policy(mode)
    self.wiring.agent_runtime.set_permission_policy(policy, mode=parse_permission_mode(mode))
    return policy
```
复用现有四道闸(closed / stopping / pending / run_lock),与 `run_turn` 同款守卫——保证只在轮边界、无挂起审批时切换。同步加 `current_permission_mode` 读取(给 UI/inspect)。

### 6. `src/pulsara_agent/host/core.py` — facade
`set_permission_mode(host_session_id, mode)` 委派给 session(位置在 `stop_current_turn` 与 `stream_approval_resolution` 之间)。

### 7. `src/pulsara_agent/cli.py` — REPL `:mode` + 静态提示
- REPL 命令循环加 `:mode <preset>`(在 `:approval` 之后):调 `core.set_permission_mode`,打印新 policy;非法 mode / 状态不允许 → 打错误不崩。加 `:status` 显示当前 mode + policy。
- `host inspect` 输出加 `current_mode`。
- **静态提示**:在固定 system prompt 组装处(agent.py 的 `compose_*` / base system prompt)追加一句**所有 mode 共享的静态说明**:"工具可见性恒定;部分工具可能被当前权限 mode 拒绝(read-only/plan 下 write/terminal 会被拒)——被拒时不要重试,改用只读手段或请用户切换 mode。" 因为静态、跨 mode 不变 → 前缀稳定、不破缓存。

### 8. `contracts/PERMISSION_POLICY_CONTRACT.zh.md` — 契约修订(冻结文档,慎改)
新增一节 "Mode 切换" + 改 §2/§4 的强制语义描述:
- **强制模型改为 gate 唯一权威**:所有工具恒定注册;read_only/plan 由 gate DENY(不再"藏工具")。更新 §4 表注:read-only 行 write/terminal 从"DENY(未注册)"改为"DENY(gate 拒)"。
- **新 §:Mode 是可变会话状态**:仅用户/host 可切;切换在轮边界生效;运行中/pending approval/stopping 时拒绝;Agent 无自切工具(禁止提权)。
- **plan 成员**:权限等价 read_only;意图是经用户切换出到执行 mode;行为塑形为后续工作。
- **不变量重申**:hardline 与任何 mode/切换无关,永远 DENY;切换不影响 live 终端进程 / event log。

## 测试改动

### 更新(语义从"藏工具"→"gate 拒")
- `tests/test_tools.py`:断言 read_only registry == `["artifact_read","read_file","search_files","todo"]` 之类的用例 → 改为"全量注册"+ 另测 gate 对 write/terminal 返回 DENY。其余 `"terminal" in names()` 类断言在全量注册下自然成立。
- `tests/test_real_llm_integration.py::test_real_agent_runtime_read_only_policy_hides_write_and_terminal_tools` → 重命名/改断言为 "read_only 下模型试 write/terminal 被 DENY"(工具可见但被拒),而非工具缺席。
- 任何依赖 `is_tool_allowed_by_policy` 决定注册的 registry 测试,改测 gate 路径。

### 新增(`tests/test_host_core.py` + `tests/test_permission_policy.py`)
- **切换原语**:`set_permission_mode` 在 idle 轮边界成功切;切后下一轮 gate 行为随新 mode(read-only→DENY write,bypass→ALLOW)。
- **守卫**:运行中 / pending approval / stopping 时 `set_permission_mode` 抛对应错误,policy 不变。
- **plan 成员**:`preset_to_policy("plan")` == read_only 三元组;plan 下 write/terminal 被 gate DENY。
- **不变量**:任意 mode(含 bypass)切换后 hardline 仍 DENY;切换不丢 live 终端进程(切 mode 后 `terminal_process list` 仍见之前 yield 的进程)。
- **holder 活引用**:切换后终端工具读到新的 terminal access(原 OFF→ALLOW 后可跑,反之被拒)。
- **缓存不变量(轻量)**:断言 `registry.names()` 在 read_only / bypass / plan 下集合相同(证明 tools 数组恒定 → 前缀稳定)。
- **Agent 无自切**:确认工具注册表里不存在任何"切 mode"工具(防回归提权)。

### 回归保持
- Step 1-3 全部 permission / approval / hardline 测试;real LLM 套件;ruff。

## 验证
```bash
uv run pytest tests/test_permission_policy.py tests/test_tools.py tests/test_host_core.py tests/test_cli_host.py tests/test_runtime_wiring.py -q
uv run pytest -q
uv run ruff check src/pulsara_agent/runtime/permission.py src/pulsara_agent/runtime/agent.py src/pulsara_agent/tools/builtins/registry.py src/pulsara_agent/cli.py
# real LLM:read_only 改为"可见但被拒"、默认仍 bypass、hardline 仍 DENY
set -a && . ./.env && set +a && PULSARA_RUN_REAL_LLM=1 uv run pytest tests/test_real_llm_integration.py -q -k "read_only or terminal_policy"
```
REPL 行为冒烟:
```bash
set -a && . ./.env && set +a && uv run pulsara host repl --workspace .
# > :mode read-only   → 切到 read-only
# > 写个文件试试        → 模型试 write_file 被 DENY(可见但拒)
# > :mode bypass-permissions → 切回
# > :status            → 显示 current_mode + policy
```

## 完成标准
- 对话内可经 REPL `:mode` / HostCore facade 在轮边界切换四预设 + plan;运行中/pending/stopping 被拒。
- 切换零重建:只改 holder 一处;gate 下一轮、终端工具下次执行读到新 policy;live 终端进程/event log 不丢。
- 模型可见 tools 数组跨所有 mode 恒定(缓存前缀稳定);read_only/plan 改由 gate DENY 强制。
- plan 作为 mode 成员存在(权限等价 read_only);Agent 无自切工具。
- hardline 跨所有 mode/切换永远 DENY。
- 契约修订落档;全量 pytest 绿;ruff clean;real LLM 行为符合新语义。

## 待讨论的开放问题(编码前确认)

1. **plan 与 read-only 权限层完全等价**——本步 plan 只是个"可切入/切出的 read-only 别名",真正的行为差异(提案式规划)推到 Step 5。这个分步是否接受?还是希望 Step 4 就带上最小行为塑形?
2. **静态提示的措辞与落点**——放在 base system prompt 是否会和现有 prompt 组装冲突?需确认 `compose_*` 的确切结构。
3. **`current_mode` 的来源**——自定义三轴(非预设)policy 时 mode=None,inspect/`:status` 如何展示("custom")?
4. **契约是冻结文档**——本步要改 §2/§4 的"藏工具→gate 拒"语义并加新节,是否同意对冻结契约做这次修订?
