# Pulsara Terminal Exec v2 实施落地：Yield 模型

_Created: 2026-06-20_

> 本文是 `TERMINAL_YIELD_MODEL_V2_DESIGN.zh.md` 的**实施层**文档。设计文档回答「做什么 / 为什么」；本文回答「怎么落地」——精确到 state 字段、函数签名、改动序列、边界 case、测试断言。
>
> 所有「代码现状」签名均经 `file:line` 核实（§1）。**代码签名是规范性的；散文是解释性的。** 两者冲突时以签名为准。
>
> 关键约束（继承设计文档）：只改 tool-facing 契约 + runtime 的一个薄封装；`ProcessRegistry` 内部、reader-drain、`OutputAccumulator`、cwd doctrine、shutdown 所有权**复用**。

## 1. 已核实签名（本文一切改动的地基，勿再勘探）

### 1.1 `background` 是一个三重用途的开关 —— v2 的核心难点

`spawn_local_process` 里 `background` 同时决定**三件正交的事**（[process.py:200-267](src/pulsara_agent/runtime/terminal/process.py:200)）：

```python
# 1. stdin 是否可写
stdin=subprocess.PIPE if background else subprocess.DEVNULL   # process.py:241
# 2. 写进 state.background
state = TerminalProcessState(..., background=background, ...)  # process.py:252
```

而 `state.background` 又驱动另外两处：

```python
# 3a. snapshot 是否暴露 process_id
process_id = state.process_id if state.background else None    # process.py:373
# 3b. registry 是否计入 live / retention
def _live_count(self): return sum(... if s.background and s.is_running)  # process.py:165
def _cleanup_finished(self): ... if s.background and s.is_finished ...    # process.py:167
```

**yield 模型的本质难点**：每条命令在 spawn 时都**可能**yield，所以都需要 stdin（`PIPE`）、都需要可被 poll；但只有**真的活过 yield 窗口**的才算 background（暴露 `process_id`、计入 registry）。当前用一个 `background` 标志表达「stdin + 暴露 + 计数」三件事，v2 必须把它拆成：

- **spawn 期决定**：stdin 是否 PIPE（yield 模型下**恒为 PIPE**——任何命令都可能 yield 后被 write）。
- **运行期事实**：是否 `yielded`（活过窗口）——决定 `process_id` 暴露与 registry retention。

### 1.2 `wait_for_process` 返回契约

```python
def wait_for_process(state, *, timeout_seconds: int | None, kill_on_timeout: bool) -> bool
```
[process.py:270](src/pulsara_agent/runtime/terminal/process.py:270)：finished 返回 `True`；超时返回 `False`；`kill_on_timeout=True` 时超时杀进程组。body 用 `time.monotonic() + timeout_seconds`，所以传 float 秒可用（ms→s 直接除）。

### 1.3 其余复用件（不改）

- `snapshot_process(state, *, max_output_chars=None, cwd=None) -> TerminalResult`（[process.py:363](src/pulsara_agent/runtime/terminal/process.py:363)）。
- `read_captured_cwd(state) -> Path | None`，**读完 unlink**（[process.py:398-410](src/pulsara_agent/runtime/terminal/process.py:398)）。
- `kill_process` / `_join_reader` / reader-drain `_reader_loop` / `OutputAccumulator`。
- `LocalTerminalBackend.execute`（foreground 当前路径）、`TerminalSession.execute`（[session.py:35](src/pulsara_agent/runtime/terminal/session.py:35)）。

## 2. Model 改动：把 `background` 的三重用途拆成「spawn 期 stdin」+「运行期 yielded」

### 2.1 `TerminalProcessState` 新增 `yielded`

```python
@dataclass(slots=True)
class TerminalProcessState:
    ...
    background: bool          # 语义收窄为：spawn 时 stdin 是否 PIPE（yield 模型恒 True）
    yielded: bool = False     # 新增：运行期事实——是否活过 yield 窗口、成为 managed background
    lifetime_watchdog: Thread | None = None   # 新增：max_lifetime_seconds 的看门狗线程
```

> 建议把 `spawn_local_process` 的 `background` 参数**改名为 `stdin_pipe`**（纯内部参数，无外部契约），让「stdin 是否 PIPE」与「是否 background」彻底分离。下文沿用 `stdin_pipe` 指代这层含义。

### 2.2 三处下游 gate 从 `state.background` 改为 `state.yielded`

这是 v2 最关键的机械改动——三处对 `state.background` 的判断全部改判 `state.yielded`：

```python
# snapshot_process（process.py:373）：只有真 yield 的进程暴露 process_id
process_id = state.process_id if state.yielded else None

# _live_count（process.py:165）：只有 yield 的进程占 live 配额
def _live_count(self):
    return sum(1 for s in self._processes.values() if s.yielded and s.is_running)

# _cleanup_finished（process.py:167）：只有 yield 的进程进 finished retention
... if s.yielded and s.is_finished ...
```

理由：yield 模型下每条命令都用 `stdin_pipe=True` spawn（都可能 yield 后被 write），所以不能再用「是否 PIPE」当「是否 background」。唯一正确的判据是运行期的 `yielded`。**in-window 完成的命令 `yielded=False`，永不暴露 process_id、永不占配额、永不进 retention**——这正好满足设计文档 §5.4 的「前台 evict 不变量」（前台不是可跨 turn 管理的资源）。

### 2.3 `_reader_loop` 的 finally 增加 cwd 文件清理

为根除 yield 路径的 `/tmp` cwd 文件泄漏（§3.4），在 `_reader_loop` 的 `finally` 末尾加一行：

```python
finally:
    ...
    if state.capture_cwd_file is not None:
        try:
            state.capture_cwd_file.unlink(missing_ok=True)
        except OSError:
            pass
```

这样无论 in-window 还是 yield 路径，capture 文件最终都被清理：in-window 路径 `read_captured_cwd` 已先 unlink（reader finally 再 unlink 是 no-op）；yield 路径由 reader finally 在进程真正退出后清掉 wrapper 写的文件。这是对「复用不动」runtime 的唯一例外小改，且有充分理由。

## 3. `ProcessRegistry.exec_with_yield` —— v2 的承重新增

替代 `start_background` 成为唯一 spawn 入口。前台/后台同路径，差别只在「是否在 yield 窗口内结束」这个运行期事实。

### 3.1 签名

```python
def exec_with_yield(
    self,
    *,
    terminal_session_id: str,
    command: str,
    cwd: Path,
    artifact_root: Path,
    max_output_chars: int,
    yield_time_ms: int,
    tty: bool = False,
    max_lifetime_seconds: int | None = None,
    output_callback: Callable[[str], None] | None = None,
    shell: TerminalShellConfig | None = None,
) -> tuple[TerminalProcessState, bool]:   # (state, yielded)
    ...
```

### 3.2 序列（伪代码，注释标出每步守的不变量）

```text
1. self._cleanup_finished()                       # 惰性驱逐，复用 v1
2. # 不在 spawn 前查 live limit——因为 in-window 完成的命令不占配额。
   #   limit 只对「真要 yield」的进程生效（见第 6 步）。
3. state = spawn_local_process(
       ..., stdin_pipe=True, capture_cwd=True,     # 恒 PIPE：任何命令都可能 yield 后被 write
       io_mode = PTY if tty else PIPE,
       output_callback=output_callback,           # 前台窗口内也允许 streaming（PR 见 §6）
   )
   state.yielded = False                          # 默认未 yield
4. if max_lifetime_seconds is not None:
       state.lifetime_watchdog = _arm_lifetime_watchdog(state, max_lifetime_seconds)
       # 看门狗在 spawn 即 arm，独立于 yield 结果；到点 kill_process；
       # 命令自然结束则看门狗见 is_finished 后 no-op。
5. finished = wait_for_process(
       state, timeout_seconds=yield_time_ms / 1000, kill_on_timeout=False)
       # 关键：kill_on_timeout=False —— 超窗口不杀，转后台（§设计 4.3）
6. if finished:
       observed = read_captured_cwd(state)         # 仅 in-window 路径读 cwd（前台才更新 session cwd）
       # 不进 registry：state 不放入 self._processes；yielded 保持 False
       return state, False
   else:
       # 真要 yield：此刻才查 live limit
       if self._live_count() >= self.max_live_processes:
           kill_process(state)                      # 杀掉刚 spawn 的，fail closed
           raise ProcessLimitError(...)
       with state.lock:
           state.yielded = True
           state.output_callback = None             # 停止向已返回的 tool result 追加 delta（§3.3）
       self._processes[state.process_id] = state
       return state, True
```

### 3.3 为什么 yield 时必须清 `output_callback`

reader-drain 线程在命令转后台后**仍在跑**，会继续调 `output_callback`。但此时 `terminal` tool 的同步调用已经返回、tool result 已 finalize。若不清空 callback，后台进程的后续输出会**追加到一个已结束的 tool call 的 event 流**上（与 v1 streaming review 的 F-G 同源风险）。所以 yield 落定时，在 `state.lock` 下置 `output_callback = None`。yielded 后的输出只能通过 `terminal_process(poll)` 拿——这正是 yield 模型的契约。

### 3.4 `max_lifetime_seconds` 看门狗

```python
def _arm_lifetime_watchdog(state, seconds) -> Thread:
    def _watch():
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if state.is_finished:
                return                # 自然结束 → no-op
            time.sleep(0.1)
        kill_process(state)           # 到点仍活 → SIGKILL 进程组（kill_process 对已 finished 幂等）
    t = Thread(target=_watch, daemon=True, name=f"pulsara-lifetime-{state.process_id}")
    t.start()
    return t
```

不变量：看门狗是**唯一**会因「时间到」而杀进程的路径。`yield_time_ms` 永不杀（§设计 4.2 正交字段）。默认 `max_lifetime_seconds=None` = 无界（不 arm 看门狗）——**默认必须无界，否则又是隐性铡刀**（§设计 §8）。

## 4. Session / backend 集成：统一路径，删 background 分支

### 4.1 `TerminalSession.execute` 删掉 `if request.background` 分叉

当前 [session.py:35-42](src/pulsara_agent/runtime/terminal/session.py:35) 是 `if request.background: _start_background else backend.execute`。v2 改为单一路径：

```python
def execute(self, request: TerminalRequest) -> TerminalResult:
    guard = CommandGuard(self.state.workspace_root)
    decision = guard.validate(request, current_cwd=self.state.current_cwd)
    if not decision.allowed:
        return _blocked(decision)                 # policy floor 不变
    state, yielded = self.process_registry.exec_with_yield(
        terminal_session_id=self.session_id,
        command=request.command,
        cwd=decision.effective_cwd,
        artifact_root=self.state.workspace_root / ".pulsara" / "terminal-output",
        max_output_chars=request.max_output_chars,
        yield_time_ms=request.yield_time_ms,
        tty=request.tty,
        max_lifetime_seconds=request.max_lifetime_seconds,
        output_callback=request.metadata.get("output_callback"),
        shell=self.shell,
    )
    if not yielded:
        # in-window 完成：前台语义，更新 session cwd（cwd doctrine 复用）
        observed = read_captured_cwd(state)
        if observed is not None and _within(observed, self.state.workspace_root):
            self.state.current_cwd = observed
    return snapshot_process(state, cwd=self.state.current_cwd if not yielded else None)
```

要点：
- **cwd 只在 `not yielded` 时回写 session**（设计 §1.6：yielded=background 不回写）。
- `LocalTerminalBackend.execute` 的前台老路径可以**整段删除**（其逻辑被 `exec_with_yield` 的 in-window 分支吸收），`backends/local.py` 收缩成只剩 `_within` 之类 helper，或直接并入 registry。`_start_background` 删除。

### 4.2 `TerminalRequest` 字段调整

```python
@dataclass(frozen=True, slots=True)
class TerminalRequest:
    command: str
    workdir: str | None = None
    yield_time_ms: int = 10_000            # 新增，替代 background+timeout_seconds
    max_output_chars: int = 20_000
    tty: bool = False
    max_lifetime_seconds: int | None = None  # 新增
    metadata: dict[str, Any] = field(default_factory=dict)
    # 删除：background、timeout_seconds
```

## 5. Tool 层：`terminal` schema 与 `_execute` 改写

### 5.1 `terminal.py` schema（删 mode-switch 字段）

```python
parameters = object_schema(
    properties={
        "command": {"type": "string", "description": "Shell command to run."},
        "workdir": {"type": "string", "description": "Optional working dir inside workspace_root."},
        "terminal_session_id": {"type": "string", "default": "default",
                                "description": "Terminal session id."},
        "yield_time_ms": {"type": "integer", "default": 10000,
            "description": "Wait up to this long for the command to finish. If still running "
                           "after the window, returns a process_id; the command keeps running "
                           "as a managed background process (NOT killed). Observe it with terminal_process."},
        "tty": {"type": "boolean", "default": False,
                "description": "Allocate a POSIX PTY (for REPL/interactive CLIs)."},
        "max_output_chars": {"type": "integer", "default": DEFAULT_MAX_OUTPUT_CHARS},
        "max_lifetime_seconds": {"type": "integer",
            "description": "Optional hard kill cap. If set, the process is killed after this many "
                           "seconds even if still running. Default: unbounded."},
    },
    required=["command"],
)
```

删除字段：`background`、`timeout_seconds`、`session_id`（别名）。`additionalProperties:false` 保持。

> **`max_lifetime_seconds` 无 `default`**：故意不给 default。给了 default → compiler 物化 → 又一把铡刀（§设计 §8）。「无 default 的 optional」compiler 仍可能物化成 `null`，故 `_execute` 解析时把 `null`/缺失都当 `None`。

### 5.2 `_execute` 删掉全部 mode 守卫

当前 [terminal.py:93-120](src/pulsara_agent/tools/builtins/terminal.py:93) 的一串守卫——`notify_on_complete` block、`tty and not background` block、`background and "timeout_seconds" in args` block——**全部删除**。新 `_execute`：

```python
command = required_str_arg(call.arguments, "command")
workdir = _opt_str(call.arguments, "workdir")
session_id = _opt_str(call.arguments, "terminal_session_id") or "default"
yield_time_ms = int_arg(call.arguments, "yield_time_ms", 10_000)
tty = bool_arg(call.arguments, "tty", False)
max_output = int_arg(call.arguments, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
max_lifetime = _opt_int(call.arguments, "max_lifetime_seconds")   # None if absent/null
# 无 mode 守卫：tty 始终合法，无 background/timeout 非法组合可表达
session = self.terminal_sessions.get_or_create(session_id)
result = session.execute(TerminalRequest(
    command=command, workdir=workdir, yield_time_ms=yield_time_ms,
    tty=tty, max_output_chars=max_output, max_lifetime_seconds=max_lifetime,
    metadata={"output_callback": output_callback} if output_callback else {},
))
```

### 5.3 返回 payload 增 `yielded_to_background`

`terminal_result_payload` 增字段（让模型不必靠 `process_id` 是否为空去推断结果类型）：

```python
payload["yielded_to_background"] = result.process_id is not None and result.status is TerminalStatus.RUNNING
```

### 5.4 `terminal_process` 基本不动

poll/wait/kill/write/submit/close_stdin 全部复用 v1。仅 description 对齐：明确 `process_id` 来自「`terminal` 命令未在 yield 窗口内结束时返回的那个」。`wait` 的有限缺省 timeout（v1 已修 30s）语义不变。

## 6. PR 分解

设计文档分 Phase 1（止血）/ Phase 2（终态）。落地按下列 PR 切，每个 PR 自带退出条件，且**改动↔退出条件↔测试成对**。

### PR1：Phase 1 止血 —— fatal block 改 ignore-with-warning

**只改 tool 层，不碰 runtime / schema 字段。** 让三个 real-LLM 测试立刻解开，独立可发。

改动：
1. [terminal.py:114](src/pulsara_agent/tools/builtins/terminal.py:114) 的 `if background and "timeout_seconds" in args` 从 `_blocked_result` 改为：忽略 `timeout_seconds`、照常走 background spawn、payload metadata 挂 `{"ignored_argument": "timeout_seconds", "reason": "background is unbounded; use terminal_process.wait or max_lifetime_seconds"}`。
2. **不**把忽略的值接到任何 kill 路径（§设计 3.2 铡刀红线）。

退出条件：
1. `terminal(background=true, timeout_seconds=30)` → 返回 `running` + `process_id`，**进程不在 30s 被杀**。
2. metadata 含 `ignored_argument: timeout_seconds`。
3. §0 三个 real-LLM 场景第一步拿到 running `process_id`。
4. runtime 层测试零改动、全绿。

### PR2：runtime —— `exec_with_yield` + `yielded` 解耦

**纯 runtime，不动 tool schema。** 在 background spawn 之上加 yield 封装。

改动：
1. `TerminalProcessState` 加 `yielded` / `lifetime_watchdog`（§2.1）。
2. 三处 gate 改判 `state.yielded`（§2.2）。
3. `spawn_local_process` 的 `background` 参数改名 `stdin_pipe`（§2.1）。
4. `_reader_loop` finally 清 cwd 文件（§2.3）。
5. 新增 `exec_with_yield`（§3）+ `_arm_lifetime_watchdog`（§3.4）。
6. `start_background` 保留为 `exec_with_yield(yield_time_ms=0)` 的薄包装（供 PR3 前的过渡），或直接由 PR3 替换。

退出条件：
1. in-window 完成命令：`yielded=False`、`process_id=None`、**不进 `_processes`**、不占 live、不进 retention。
2. 超窗口命令：`yielded=True`、进 registry、可被 poll/wait/kill。
3. 超 live limit 时**真要 yield** 的命令 fail closed（`ProcessLimitError`），in-window 完成的命令**不**触发 limit。
4. `max_lifetime_seconds=2` → ~2s 被 KILLED；不设 → 长命令无界存活。
5. yield 落定后 `output_callback` 被清，后台输出不再进原 tool call event 流。
6. cwd 文件在进程退出后被清，无 `/tmp` 泄漏。

### PR3：tool —— yield schema + hard-cut

**接通 tool 面，hard-cut 删 mode-switch 字段。**

改动：
1. `TerminalRequest` 改字段（§4.2）；`TerminalSession.execute` 删 background 分叉、走 `exec_with_yield`（§4.1）；删 `LocalTerminalBackend` 前台老路径 + `_start_background`。
2. `terminal` schema 改 yield 形态（§5.1）；`_execute` 删全部 mode 守卫（§5.2）；payload 加 `yielded_to_background`（§5.3）。
3. hard-cut 删 `background` / `timeout_seconds` / `session_id` 别名 / `tty-and-not-background` 守卫 / `notify_on_complete` 残留。
4. 改写依赖旧字段的现有测试为 yield 等价形态。

退出条件：
1. schema 无 `background`/`timeout_seconds`/`session_id`；有 `yield_time_ms`/`tty`/`max_lifetime_seconds`。
2. `tty=true` 无任何 mode 前提，不 block。
3. 只认 `terminal_session_id`，无双 default 别名路径。
4. PR1 的 ignore-with-warning 分支随 `background` 字段删除一并移除（不再需要过渡态）。

### PR4：real-LLM 收口

real-LLM smoke + server-survival 回归（§7.3），证明 compiler 物化场景下 bug 真死。

> 顺序不变量：**PR1 可独立先发止血**。PR2→PR3 是终态主线，PR3 必须和「删 `background`」同 PR（hard-cut，不留过渡字段重新制造非法组合）。PR1 的 ignore 分支在 PR3 删字段时一并清除——避免「止血补丁」长期沉淀成第二套语义。

## 7. 测试矩阵

### 7.1 确定性 runtime 测试（`tests/test_terminal_runtime.py`）

| 测试 | 断言 | 守 |
|---|---|---|
| in-window 完成 | `echo hi` + `yield_time_ms=10000` → final result，`process_id is None`，`yielded is False`，**不在 `registry._processes`** | §2.2 evict 不变量 |
| 超窗口转后台 | `sleep 5` + `yield_time_ms=200` → `process_id != None`、`status==running`、`yielded is True`，随后 `poll/wait/kill` 能找到 | §3.2 |
| yield 不杀 | `sleep 2` + `yield_time_ms=200` → 转后台后**仍 running**，2s 后自然 SUCCESS（证明 yield 不是 kill） | §设计 4.3 |
| in-window 不占配额 | 连发 N(>max_live) 条快命令(全 in-window) → 无 `ProcessLimitError` | §2.2 / §3.2 step6 |
| yield 占配额 fail closed | spawn 满 max_live 个 yield 进程后再来一条会 yield 的 → `ProcessLimitError`，且该新进程被 kill 不泄漏 | §3.2 step6 |
| lifetime 杀 | `sleep 60` + `max_lifetime_seconds=2` → ~2s `status==killed` | §3.4 |
| lifetime 默认无界 | `sleep 60` 转后台、无 `max_lifetime_seconds` → 60+s 仍 running | §3.4 红线 |
| yield 清 callback | 转后台后 reader 续读的输出**不**触发原 `output_callback` | §3.3 |
| cwd 仅前台回写 | in-window `cd src` 更新 session cwd；yield 的 `cd src && sleep` **不**更新 | §4.1 |
| cwd 文件不泄漏 | 进程退出后 `capture_cwd_file` 已 unlink | §2.3 |
| 跨 chunk secret | (v1 回归)行边界 redaction 仍生效 | v1 |
| 高吞吐不死锁 | (v1 回归)30000 行后台输出不死锁 | v1 |

### 7.2 Tool 契约测试（`tests/test_tools.py`）

| 测试 | 断言 |
|---|---|
| schema 形状 | `terminal` 无 `background`/`timeout_seconds`/`session_id`；有 `yield_time_ms`/`tty`/`max_lifetime_seconds`；`additionalProperties:false` |
| `tty` 始终合法 | `tty=true`（无 background）不 block，返回 PTY 进程 |
| session 单字段 | 只认 `terminal_session_id`；不存在双 default 相等校验路径 |
| `yielded_to_background` | in-window=false / 超窗口=true |
| 共享 registry | `terminal` 起的 yield 进程能被 `terminal_process` poll（v1 接线回归） |
| `max_lifetime_seconds` 缺失=None | 不传 / 传 `null` 都解析为 None，不 arm 看门狗 |

### 7.3 server-survival 回归（铡刀检测，最重要）

直接钉死 §设计 3.2 的陷阱：

1. 长命令(`sleep 60`)转后台 → **60+s 仍 running**，证明无路径把 auto-filled 值当寿命杀。
2. 显式 `max_lifetime_seconds=2` 时**才** ~2s 被杀——杀只来自显式 watchdog。

### 7.4 real-LLM smoke（fake client 复现不了 compiler 物化，唯一能证 bug 真死）

1. §0 三场景 yield 版：断言**第一步拿到 running `process_id`**。
2. **反向断言**：整条 trajectory **无任何 `terminal` 调用因 auto-filled 字段 blocked**。
3. server-survival real-LLM 版：模型跑长命令后继续做别的，断言该后台进程多轮后仍 running。

### 7.5 兼容性

- 依赖 `background=`/`timeout_seconds=` 的现有用例**随 PR3 hard-cut 改写**为 yield 等价（预期成本，非回归）。
- runtime 层测试（registry 内部 / drain / shutdown）除 §2.2/§2.3 涉及的几处外**不应改动**；若大面积失败说明「只改 tool 面」边界被破坏。

## 8. 边界 case 与风险

### 8.1 `yield_time_ms=0`（立即后台）

等价于 v1 的 `background=true`：`wait_for_process(timeout_seconds=0)` 立即返回 `False`（除非命令瞬时结束），直接 yield。这是模型「我就要个 server，不等」的表达方式，无需 `background` 布尔。需测 `yield_time_ms=0` + 瞬时命令(`true`)的竞态：可能 in-window 也可能 yield，两种结果都合法，测试不可断言唯一态——只断言「要么 final result 要么 running process_id，二者其一且自洽」。

### 8.2 in-window 完成但进程组有遗留子进程

in-window 路径不进 registry，但若命令 fork 了未 reap 的子进程（`cmd & ` 在命令内部），主进程结束、子进程游离。这是 v1 既有行为（前台路径本就如此），yield 模型不改善也不恶化。`setsid` 进程组 + shutdown 时 `kill_process` 仅覆盖 registry 内进程——游离子进程不在 registry 故不被 shutdown 清。**记为已知局限**，与 v1 一致，不在 v2 范围扩大处理。

### 8.3 看门狗线程泄漏

`max_lifetime_seconds` 看门狗是 daemon 线程，最长存活 `seconds`。命令自然结束后它在下一个 0.1s tick 退出。daemon 属性保证进程退出不被它阻塞。无需显式 join——但 `RuntimeSession.close()` 的 shutdown 杀进程后，看门狗会在下一 tick 见 `is_finished` 退出，可接受。

### 8.4 PR2/PR3 之间 `start_background` 的过渡

PR2 把 `start_background` 保留为 `exec_with_yield(yield_time_ms=0)` 包装，让 v1 tool 层在 PR3 前仍能跑。PR3 删 tool 层 background 后，`start_background` 失去调用者，随 PR3 一并删除。不要让它长期沉淀。

## 9. 一句话收束

> v2 落地的承重难点不是「加个 yield 参数」，而是**把 `background` 的三重用途（spawn 期 stdin / process_id 暴露 / registry 计数）拆开**：stdin 在 yield 模型下恒为 PIPE，而「是否 background」收敛成运行期事实 `yielded`（活过 yield 窗口）。三处下游 gate（`snapshot_process` 暴露、`_live_count`、`_cleanup_finished`）从 `state.background` 改判 `state.yielded`，in-window 完成的命令永不进 registry——这同时满足前台 evict 不变量。核心新增 `exec_with_yield` 前台后台同路径，`kill_on_timeout=False` 保证超窗口转后台而非杀、yield 落定清 `output_callback` 防止追加已结束 event 流、`max_lifetime_seconds` 是唯一因时间杀进程的显式看门狗（默认无界）。落地按 PR1(止血 ignore-with-warning)→PR2(runtime yield 解耦)→PR3(tool hard-cut 删 mode 字段)→PR4(real-LLM 收口)，PR1 可独立先发，PR3 必须和删 `background` 同 PR。server-survival 回归 + real-LLM smoke 是证明 30 秒铡刀不复活、bug 真死的唯一手段。
