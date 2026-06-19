# Pulsara Terminal Exec v2 设计：Codex-like Yield 模型

_Created: 2026-06-20_

> 本文是 terminal 工具面（tool surface）的 v2 重构设计：用 Codex 风格的 **yield 模型**取代当前的 `background` 布尔模式开关，让「前台 / 后台」从**声明模式**变成**涌现结果**，从接口层根除一类「schema 合法但 executor 判死」的非法组合。
>
> 调研见 §2（Hermes / OpenClaw / Codex）。所有「代码现状」主张均经 `file:line` 核实（§1）。代码签名是规范性的；散文是解释性的。两者冲突时以代码签名为准。
>
> 范围：只改 **tool-facing 契约**（`terminal` / `terminal_process` 的 schema 与 executor 分派）。底层 `ProcessRegistry` / reader-drain / cwd doctrine / shutdown 所有权**复用不动**（§5.4）。

## 0. 起因：一个 real-LLM 稳定失败

切到 openai_responses 风格供应商后，terminal background 测试**稳定失败**。不是网络 `Connection error.`，也不是 retry：模型调用 `terminal(background=true)` 时**总是显式填入 `timeout_seconds: 30`**，executor 直接 fatal block。

真实 trajectory：

```text
tool call:
{"tool":"terminal","args":{
  "command":"sleep 5","background":true,"timeout_seconds":30,
  "tty":false,"max_output_chars":20000,
  "session_id":"default","terminal_session_id":"default","workdir":""}}

tool result:
{"status":"blocked",
 "error":"background=true does not support timeout_seconds yet; use terminal_process.wait timeout",
 "process_id":null}
```

即便 prompt 明写「Do not pass timeout_seconds」，模型仍填。失败的测试全部卡在**第一步**——拿不到 running `process_id`：

- terminal background + kill
- terminal background stdin submit / wait
- terminal background PTY submit / close / wait

注意 `"workdir":""`——`workdir` **没有 schema default**，却也被填了。这条是后面根因判断的关键证据。

## 1. 已核实代码基线（设计起点，勿再勘探）

每条都经源码核对，是本文判断的前提。

### 1.1 schema 已经是 closed —— 「关 schema」这个杠杆已用尽

`object_schema()` 固定输出 `additionalProperties: false`（[schemas.py:12-19](src/pulsara_agent/tools/builtins/schemas.py:12)）。所以 provider **不是**在注入 schema 之外的字段，它在填**已声明的 optional 字段**。收紧 schema 开放度没有剩余空间可用。

### 1.2 fatal block 判的是「参数存在」，不是「值」

[terminal.py:114](src/pulsara_agent/tools/builtins/terminal.py:114)：

```python
if background and "timeout_seconds" in call.arguments:
    return self._blocked_result(..., error="background=true does not support timeout_seconds yet; ...")
```

判据是 `"timeout_seconds" in call.arguments`——只要键存在就 block，与值无关。这是裂缝本体。

### 1.3 foreground timeout 是「杀进程」，background 当前「无界」

- foreground：`wait_for_process(process, timeout_seconds=request.timeout_seconds, kill_on_timeout=True)`（[backends/local.py:63](src/pulsara_agent/runtime/terminal/backends/local.py:63)），默认 `timeout_seconds=30`（[terminal.py:101](src/pulsara_agent/tools/builtins/terminal.py:101)）。**超时 = SIGKILL 进程组。**
- background：`ProcessRegistry.start_background()` 签名**根本没有 timeout 参数**（[process.py:84-95](src/pulsara_agent/runtime/terminal/process.py:84)）。后台进程当前**不会因任何 timeout 被杀**。

这两条决定了 §3.2 的「30 秒铡刀」判断。

### 1.4 还有两处同型的 mode-coupling fatal

不止 `timeout_seconds`：

- `tty and not background` → block（[terminal.py:107](src/pulsara_agent/tools/builtins/terminal.py:107)），`tty` 只在 background 模式合法。
- `session_id` 与 `terminal_session_id` **两个都带 default `"default"`、都被 compiler 物化**，靠「必须相等」兜底（[terminal.py:241](src/pulsara_agent/tools/builtins/terminal.py:241)）。一旦模型给 `terminal_session_id` 设了非默认值而 compiler 又物化 `session_id="default"`，这里又是一处 spurious fatal。

**同一个 mode-switch 工具至少埋了三处「schema 合法但 executor 拒绝」。** 它们共享同一个病灶：`background` 布尔把两种 action 塞进一个工具，字段只在某个模式下有效。

### 1.5 底层 runtime 与 tool 契约是分离的（v2 复用前者）

`ProcessRegistry` / `ManagedTerminalProcess` / per-process reader-drain / 线程安全 `OutputAccumulator` / cwd doctrine / `RuntimeSession.close()` 的 shutdown 所有权——这些都在 runtime 层（`runtime/terminal/`），与 tool schema 无关。foreground 也已经走 `spawn_local_process`（[backends/local.py:57](src/pulsara_agent/runtime/terminal/backends/local.py:57)）。**v2 只改 tool 面，runtime 复用。**

### 1.6 进程生命周期归 session，不归单次 run

v1 已确立：background process 在一轮 `FINISHED` 后继续存活，只有宿主显式 `RuntimeSession.close()` 才清理（见 `TERMINAL_RUNTIME_V1_IMPLEMENTATION_PLAN.zh.md` §3.3 / §14.7）。yield 模型产出的「涌现后台进程」沿用同一所有权，不引入新生命周期。

## 2. 调研：三个项目怎么处理「单工具 + optional 组合」

调研目的不是照搬，而是确认两件事：(a) 我们这个 fatal block 是不是普遍做法（不是），(b) 终态该长什么样（Codex 最干净）。

### 2.1 Hermes：单工具，但后台分支不因 timeout 判死

Hermes 也有单 terminal 工具，schema 同时声明 `background` 和 `timeout`（[terminal_tool.py:1775](/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/terminal_tool.py:1775)、[terminal_tool.py:2545](/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/terminal_tool.py:2545)）。

关键区别：后台分支**不会**因为 `background=true + timeout` 拒绝，而是直接进 process registry spawn 路径（[terminal_tool.py:2039](/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/terminal_tool.py:2039)）。即便 provider 物化了 optional `timeout`，也不触发 fatal block。

启示：**「schema 合法的 optional 组合在 executor 判 fatal」不是普遍做法，是我们独有的裂缝。**

### 2.2 OpenClaw：把 `background + timeout` 定义成合法语义

OpenClaw 的 exec 单工具 schema 同时声明 `yieldMs / background / timeout / pty`（[bash-tools.schemas.ts:13](/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/bash-tools.schemas.ts:13)）。

它把 `background + timeout` 当**合法**：后台进程仍可有 lifetime timeout。`background/yieldMs` 决定是否 yield 成后台（[bash-tools.exec.ts:1500](/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/bash-tools.exec.ts:1500)），`timeout` 作为进程超时传下去（[bash-tools.exec.ts:1837](/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/bash-tools.exec.ts:1837)、[bash-tools.exec.ts:1848](/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/bash-tools.exec.ts:1848)）。测试明确钉死 `background: true, timeout: ...` 合法（[bash-tools.exec.background-abort.test.ts:229](/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/bash-tools.exec.background-abort.test.ts:229)）。

启示：「赋予语义」可行——但**对 Pulsara 有陷阱**（§3.2）：OpenClaw 安全是因为它没有我们「compiler 物化 foreground default 30 → 当后台寿命 → 30 秒杀 server」的具体失败模式。直接抄 = server 铡刀。

### 2.3 Codex：没有 `background` 开关，用 yield-time 模型（终态原型）

Codex 最接近我们想要的终态——**它压根没有 `background=true` 这个 mode switch**。

`exec_command` 用 `yield_time_ms`：命令在初始等待窗口后如果仍在跑，就返回 `process_id`，后续用独立的 `write_stdin` 继续 poll / 输入（[shell_spec.rs:88](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/shell_spec.rs:88)、[shell_spec.rs:110](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/shell_spec.rs:110)）。

参数里有 `tty / yield_time_ms`，但**没有 `timeout_ms` 与 `background` 的冲突组合**（[unified_exec.rs:27](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/unified_exec.rs:27)）。并且有测试钉死长运行 exec 在 turn 结束后仍存活、到 shutdown 才清理（[unified_exec.rs:2381](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/tests/suite/unified_exec.rs:2381)、[unified_exec.rs:2476](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/tests/suite/unified_exec.rs:2476)）——与 Pulsara v1 的 session-owned 生命周期（§1.6）一致。

### 2.4 三项目共识

| | 长命令超时行为 | mode 开关 | `bg+timeout` 非法组合 |
|---|---|---|---|
| Hermes | 进 registry，不杀 | 有 `background` | 合法（不 fatal） |
| OpenClaw | timeout = 寿命上限，杀 | 有 `background` | 合法（对我们=铡刀） |
| **Codex** | **不杀，转后台返回 process_id** | **无（`yield_time_ms`）** | **不存在** |
| Pulsara 现状 | foreground 杀 / bg 无界 | 有 `background` | **fatal block（bug）** |

共识：**没人把 schema 合法的 optional 组合在 executor 判 fatal。** OpenClaw 证明「赋予语义」可行，Codex 证明「移除 mode 开关」最干净。Pulsara 选 Codex 路径。

## 3. 根因

### 3.1 不是「default 诱导」，是「compiler 物化所有 optional + executor 判死合法组合」

直觉归因是「`timeout_seconds` 带 `default: 30` 诱导模型填值」。但 §0 的 `"workdir":""`（**无 default**）被填推翻了这个单一归因。真实机制是两层：

- **表层**：openai_responses 风格 tool compiler 会把工具的**所有已声明 optional 字段**物化进调用，无论有没有 default。`default` 只是放大器。
- **承重层**：executor 对**schema 合法**的 `{background:true, timeout_seconds:30}` 判 fatal（§1.2）。**模型拿到的契约（schema）允许的东西，契约执行者（executor）判死刑。** 这与「模型该不该填」无关——是契约自相矛盾。

prompt 禁令打不过 schema：当 prompt 说「别填」而 schema 说「这是合法 optional」，结构化输出机器遵循 schema。这不是模型不听话，是我们要求它违反自己被给定的契约。

### 3.2 为什么不能直接抄 OpenClaw「赋予语义」——30 秒 server 铡刀

「停止 fatal」无争议（§2.4 全体共识）。但**怎么停止**有陷阱。若学 OpenClaw 把 `timeout_seconds` 当后台寿命：

1. compiler 把 foreground default **30** 物化进 `background=true` 调用（§0 trajectory 实证）。
2. 我们把这个 30 当「后台进程寿命」。
3. `terminal(command="npm run dev", background=true)` 被 compiler 自动补成 `timeout_seconds:30` → **dev server 在第 30 秒被 SIGKILL**（机制：§1.3 background 当前无界，一旦接通 timeout-as-lifetime 即生效）。
4. 而 background 的全部意义就是跑 server / watcher。

OpenClaw 对**它**安全；但**我们的 compiler-物化-30 失败模式**让「timeout 当后台寿命」变成一把静默铡刀。所以**临时止血的安全形态是「忽略」，不是「赋予寿命语义」**（§6 Phase 1）。这块分析在调研之上，且承重——直接照搬 OpenClaw 会 ship 一个 30 秒 server 杀手。

### 3.3 病根是 mode-switch 工具本身

`timeout_seconds` 只是症状之一（§1.4 还有 `tty`、session 别名）。真正的病是 **`background` 布尔把「前台一次性命令」和「后台 managed 进程」两种 action 塞进一个工具**，字段只在某模式下有效。这对 LLM tool calling 根本性不友好：

- 模型要理解「同一工具，某字段在某模式下才有意义」——比理解两个独立工具难。
- 模型要**事先预判**命令是不是长命令来决定 `background`——而这是模型最不擅长的（`npm test` 到底长不长？）。
- 任何「模式-字段」组合的禁忌都得在 executor 手写守卫，每个守卫都是一道「schema 合法但 executor 拒绝」的潜在裂缝。

yield 模型从接口层移除 `background` 布尔，让这三个问题一起消失。

## 4. Yield 模型：设计与理由

### 4.1 核心：background-ness 是涌现结果，不是声明模式

```text
terminal(command, yield_time_ms?, tty?, max_output_chars?,
         terminal_session_id?, max_lifetime_seconds?)

  始终通过 ProcessRegistry spawn managed process（runtime 复用，§1.5）
  最多等待 yield_time_ms：
    命令在窗口内完成 → 返回 final result（process_id = null）
    窗口结束仍在运行 → 返回 process_id + 部分输出（自动成为 managed background）

  max_lifetime_seconds?: 可选，默认 None = 无界寿命
                         这是唯一的「到点杀」watchdog，独立于 yield

terminal_process(action="poll|wait|kill|write|submit|close_stdin", process_id, ...)
  不变；wait 仍有自己的等待 timeout（停止等待，不杀进程）
```

模型不再预判长短命令：跑就完了。2 秒回来给结果，没回来给 process_id 继续 poll。这正是 Codex `exec_command` 的形态（§2.3）。

### 4.2 三个字段正交，无重叠语义 —— 化解原 fail-closed 的承重理由

v1 当初对 `background + timeout_seconds` 选 fail-closed，理由是「不想把 foreground kill-timeout 和后台 watchdog 两种语义混在一个字段，未来后台寿命应有显式 `max_lifetime_seconds`」（见 v1 plan §5）。yield 模型不是推翻这个决定，是它的**正确进化**——用三个正交字段彻底拆开：

| 字段 | 语义 | 触发动作 |
|---|---|---|
| `yield_time_ms` | 等多久后把仍在跑的命令转后台 | **不杀**，返回 process_id |
| `max_lifetime_seconds` | 进程寿命硬上限（默认 None=无界） | 到点 **SIGKILL** |
| `terminal_process(wait).timeout_seconds` | 本次 wait 等待上限 | **不杀**，停止等待返回 running |

「何时转后台」（yield）、「到点杀」（lifetime）、「等多久」（wait）三件事彻底分离，永不重叠。当初的 fail-closed 是用「拒绝组合」回避语义冲突；yield 模型是用「三个正交字段」消解冲突——更彻底，且 `max_lifetime_seconds` 正是 v1 plan 预留的那个显式 watchdog 字段。

### 4.3 必须接受的行为变化：长命令不再被「杀」，而是转后台

这是 yield 模型唯一的语义代价，要清醒接受：

- **现状**：foreground 命令超 `timeout_seconds`（默认 30）→ SIGKILL。
- **yield 模型**：命令超 `yield_time_ms` → **不杀，转后台**返回 process_id；失控命令（死循环）由模型显式 `terminal_process(kill)` 或设 `max_lifetime_seconds` 兜底。

为什么这更好：显式生命周期控制 > 一把 compiler auto-fill 出来的 30 秒静默铡刀。失控命令仍有两道防线（显式 kill + 可选 lifetime cap），但常见的「命令比预期慢」不再被误杀，而是优雅转后台。这正是 Codex 钉死的「长 exec 跨 turn 存活到 shutdown」行为（§2.3）。

### 4.4 顺带溶解的三处债

`background` 布尔移除后，依附其上的 mode-coupling 全部失去依附点：

1. `timeout_seconds` 非法组合（§1.2）→ 字段不存在，不可表达。
2. `tty and not background` fatal（§1.4 / [terminal.py:107](src/pulsara_agent/tools/builtins/terminal.py:107)）→ `tty` 成为始终合法的 optional（任何命令都可要求 PTY），无模式耦合。
3. session 双 default 别名（§1.4 / [terminal.py:241](src/pulsara_agent/tools/builtins/terminal.py:241)）→ 借机收敛成单字段 `terminal_session_id`，去掉 `session_id` 别名。

## 5. Tool schema 与 runtime 集成

### 5.1 `terminal` v2 schema

```json
{
  "command": "string (required)",
  "yield_time_ms": "integer? default 10000",
  "tty": "boolean? default false",
  "max_output_chars": "integer? default 20000",
  "terminal_session_id": "string? default \"default\"",
  "max_lifetime_seconds": "integer? (default unset = unbounded)"
}
```

去掉的字段（hard-cut，§6）：`background`、`timeout_seconds`、`session_id`（别名）。`additionalProperties:false` 保持（[schemas.py:12](src/pulsara_agent/tools/builtins/schemas.py:12)）。

description 必须写清涌现语义，避免模型误以为 `yield_time_ms` 是「超时杀」：

```text
Run a shell command. Waits up to yield_time_ms for it to finish.
If it finishes, returns the final result. If it is still running after
yield_time_ms, returns a process_id; keep observing it with terminal_process.
Long-running commands are NOT killed on yield; they become managed background
processes. Use max_lifetime_seconds only if you want a hard kill cap.
```

返回 JSON：

```json
{
  "status": "success|error|running|killed|blocked",
  "output": "...bounded preview...",
  "exit_code": 0,
  "cwd": "...",
  "process_id": "proc_... when still running, else null",
  "yielded_to_background": true,
  "truncated": false,
  "full_output_ref": null,
  "terminal_session_id": "default",
  "backend_type": "local",
  "io_mode": "pipe|pty"
}
```

`process_id != null` 且 `status == "running"` 即「已转后台」；`yielded_to_background` 显式化这个结果，省得模型靠 `process_id` 是否为空去推断。

### 5.2 `terminal_process` v2 schema

与 v1 基本不变（poll/wait/kill/write/submit/close_stdin），仅文档措辞对齐 yield 模型。`wait` 的 `timeout_seconds` 缺省仍是有限值（v1 已修为 30，绝不无限阻塞），语义是「本次 wait 等待上限，不杀进程」（§4.2）。

### 5.3 executor 分派（统一路径，无 mode 分支）

当前 `terminal.py` 的 `_execute` 有 `if background:` / `if tty and not background:` / `if background and "timeout_seconds"...` 一串模式守卫。v2 全删，换成单一路径：

```text
1. 解析 command / yield_time_ms / tty / max_output_chars / terminal_session_id / max_lifetime_seconds
2. policy 评估（workspace escape / empty / dangerous → 不变，§5.4）
3. registry.exec_with_yield(command, yield_time_ms, tty, max_lifetime_seconds, ...)
     - 始终 spawn managed process（前台后台同路径）
     - 等待 min(进程结束, yield_time_ms)
     - 结束 → snapshot final result, process_id=None, 从 registry evict（前台不占 retention，沿用 v1 §6 边界）
     - 未结束 → 进程留在 registry, 返回 process_id + 部分 snapshot
4. max_lifetime_seconds 不为 None → 给该 process 挂一个 lifetime watchdog（到点 SIGKILL 进程组）
```

关键：**前台/后台共用 `exec_with_yield`**，差别只是「是否在 yield 窗口内结束」这个运行时事实，不是调用方声明。这与 v1 「foreground 也走 `spawn_local_process`」一致（§1.5），实现增量小。

### 5.4 runtime 层复用，不改

以下全部不动，v2 只在其上换 tool 契约：

- `ProcessRegistry`（live limit / finished retention / 惰性驱逐）。
- per-process reader-drain 线程 + 线程安全 `OutputAccumulator`（行边界 redaction）。
- cwd doctrine（前台更新 session cwd / 后台不回写，§1.6）。
- `RuntimeSession.close()` 的 shutdown 所有权（进程归 session，§1.6）。
- policy floor（empty / workspace escape / dangerous command confirmation）。

新增的只有：`exec_with_yield`（在现有 foreground wait + background spawn 之上的薄封装）和 `max_lifetime_seconds` 的 watchdog（一个可选的、到点 `kill_process` 的计时）。

> **前台 evict 不变量**：yield 窗口内结束的命令必须从 registry evict（不占 live limit、不进 finished retention），与 v1 「foreground 不是可跨 turn 管理的 background process」边界一致。只有「真的转后台」的进程才进 retention。

## 6. 迁移策略

### 6.1 Phase 1（立即止血，独立于终态）

把 [terminal.py:114](src/pulsara_agent/tools/builtins/terminal.py:114) 的 fatal block 改成 **ignore-with-warning**，**不要**改成 OpenClaw 式「赋予寿命语义」（§3.2 铡刀）：

```text
background=true 下出现 timeout_seconds：
  - 忽略它（不传给任何 kill 路径，后台保持无界）
  - 返回 running process + process_id
  - metadata 挂 warning: {"ignored_argument": "timeout_seconds",
                          "reason": "background processes are unbounded; use terminal_process.wait or max_lifetime_seconds"}
```

理由：这个值是 compiler 注入的**噪声**，模型对它**没有 agency**（§3.1）。对无 agency 的字段只能 ignore，不能 reject（=当前 bug），也不能 suggested_args（模型无法可靠省掉 compiler 物化的字段 → 死循环）。

Phase 1 独立可发，**立刻**让 §0 三个 real-LLM 测试从 blocked 解开，不依赖 Phase 2 是否完成。

### 6.2 Phase 2（终态：yield 模型 + hard-cut）

- 实现 §5 的 `terminal` v2 schema + `exec_with_yield` + `terminal_process` 文档对齐。
- **hard-cut 删除** `background` / `timeout_seconds` / `session_id` 别名 / `tty and not background` 守卫。
- 新增 `max_lifetime_seconds` 可选 watchdog。

### 6.3 为什么 hard-cut，不留兼容

依据全部成立（与 contradiction v2、terminal v1 同一 doctrine：hard-cut over compat shims）：

1. **唯一消费者是 LLM**，每轮读**重新生成**的 schema——没有冻结的集成契约要维护。
2. **历史 transcript 是已解析结果，不是重放执行的 tool call**——删字段不破坏 replay。
3. **直接调用点只有测试**——生产入口尚未接（cli 仍是骨架）。

保留 deprecated `background=true` 等于**把刚消除的非法组合请回来**，重新制造「schema 合法、executor 特殊处理」的裂缝。不留。

> Phase 1 与 Phase 2 之间的窗口里，`background` 仍在 schema（Phase 2 才删）。该窗口内 ignore-with-warning（Phase 1）保证不再 fatal、不再铡刀，是安全过渡态。

## 7. 测试

### 7.1 确定性契约测试

1. **schema 形状**：`terminal` v2 **无** `background` / `timeout_seconds` / `session_id`；**有** `yield_time_ms` / `tty` / `max_lifetime_seconds`；`additionalProperties:false`。
2. **yield 完成路径**：短命令（`echo hi`）在 `yield_time_ms` 内结束 → 返回 final result，`process_id == null`，`yielded_to_background == false`，且**从 registry evict**（不占 live limit、不进 finished retention，§5.4 不变量）。
3. **yield 转后台路径**：`sleep 5` + 小 `yield_time_ms`（如 200ms）→ 返回 `process_id != null`、`status == running`、`yielded_to_background == true`；随后 `terminal_process(poll/wait/kill)` 能找到它。
4. **`max_lifetime_seconds` 生效**：设值 → 进程到点被 SIGKILL，`status == killed`；不设 → 长命令无界存活。
5. **`tty` 始终合法**：`tty=true` 不再要求任何 mode 前提，不 block（§4.4 #2 回归）。
6. **session 单字段**：只认 `terminal_session_id`，无双 default 别名相等校验路径（§4.4 #3 回归）。
7. **Phase 1 防御（过渡窗）**：`background=true` 下塞 `timeout_seconds:30` → 返回 running、**进程不在 30s 被杀**、metadata 有 `ignored_argument`。

### 7.2 server-survival 回归（铡刀检测，最重要的防御）

直接钉死 §3.2 的陷阱不复发：

- 长命令（`sleep 60` 或 `npm run dev` 风格）转后台后，**60+ 秒仍 running**，证明没有任何路径把 auto-filled 的 30 当寿命杀掉。
- 显式 `max_lifetime_seconds=2` 时**才**在 ~2s 被杀——证明杀只来自显式 watchdog，不来自 yield 或 auto-fill。

### 7.3 real-LLM smoke（唯一能证明 bug 真死的，fake client 复现不了 compiler 物化）

这个 bug 活在 **provider 的 tool-compilation 行为**里——fake client 不会帮你物化 `workdir:""` / `timeout_seconds:30`。所以「修好了」只能由**真实 openai_responses 风格 provider** 证明：

1. §0 三个失败场景（bg+kill / bg stdin submit+wait / bg PTY submit+close+wait）的等价 yield-模型版本，断言**第一步就拿到 running `process_id`**。
2. **反向断言**：整条 trajectory **没有任何一次 `terminal` 调用因 auto-filled 字段 blocked**——直接钉死 §0 死因。
3. **server-survival real-LLM 版**：让模型跑一个长命令并继续做别的，断言该后台进程在多轮后仍 running（铡刀的真实环境回归）。

### 7.4 兼容性

- 现有 `tests/test_terminal_runtime.py` / `tests/test_tools.py::test_terminal_*` 中**依赖 `background=`/`timeout_seconds=` 的用例随 hard-cut 改写**为 yield 模型等价形态（这是 hard-cut 的预期成本，不是回归）。
- runtime 层测试（registry / drain / cwd / shutdown）**不应改动**——若它们因 tool 改动而失败，说明 §5.4 的「只改 tool 面」边界被破坏，需回查。

## 8. v1 / v2 边界

### v2（本文）

```text
- 移除 background 布尔 mode switch；terminal 始终 spawn managed process
- yield_time_ms：涌现式前台/后台（完成→result，未完成→process_id）
- max_lifetime_seconds：唯一显式 kill watchdog，默认无界
- hard-cut 删 background / timeout_seconds / session_id 别名 / tty-mode 守卫
- Phase 1 先 ignore-with-warning 止血，Phase 2 上 yield 模型
- runtime（registry / drain / cwd / shutdown / policy）全复用，不改
```

### 不做（留后续）

```text
- 不改 ProcessRegistry / reader-drain / OutputAccumulator 内部
- 不做 Docker/SSH/remote backend
- 不做 yield_time_ms 的自适应/动态调整
- 不引入 streaming 之外的新事件类型
- 不把 max_lifetime_seconds 默认设成有限值（默认必须无界，否则又是隐性铡刀）
```

## 9. 一句话收束

> 当前 real-LLM 失败的根因不是「模型该不该填 `timeout_seconds`」，而是 **provider compiler 物化所有已声明 optional 字段 + executor 对 schema 合法组合判 fatal**（`workdir:""` 被填是铁证）。三个开源项目都不在 executor 判死合法 optional：Hermes 放行、OpenClaw 赋予语义、Codex 干脆移除 `background` 开关。Pulsara 选 **Codex 的 yield 模型**——`yield_time_ms` 让前台/后台成为涌现结果而非声明模式，移除模型最不擅长的「预判长短命令」，并用 `yield_time_ms`（何时转后台）/ `max_lifetime_seconds`（到点杀）/ `wait.timeout`（等多久）三个正交字段彻底化解 v1 fail-closed 当初要回避的 timeout 语义冲突。**Phase 1 先 ignore-with-warning 止血（绝不抄 OpenClaw 的 timeout-as-lifetime，否则 compiler 物化的 30 会变成 server 铡刀），Phase 2 hard-cut 上 yield 模型**；runtime 全复用，real-LLM smoke + server-survival 回归是证明 bug 真死的唯一手段。
