# Terminal Tool 开源实现调研：Hermes / OpenClaw / Codex

_Created: 2026-06-19_

本文记录对三个本地开源项目 terminal / exec 工具系统的调研结论：

- Hermes: `/Users/plumliu/Desktop/python_workspace/hermes-agent`
- OpenClaw: `/Users/plumliu/Desktop/python_workspace/openclaw`
- Codex CLI: `/Users/plumliu/Desktop/python_workspace/codex`

调研目的不是照搬实现，而是提炼这些系统对「terminal tool」的共同理解，并为 Pulsara 下一步 terminal runtime 增强提供边界和优先级。

## 0. Pulsara 当前基线

Pulsara 当前已有一个可用但偏 MVP 的 terminal：

- 工具入口：`src/pulsara_agent/tools/builtins/terminal.py`
- runtime：`src/pulsara_agent/runtime/terminal/`
- 本地 backend：`src/pulsara_agent/runtime/terminal/backends/local.py`
- 测试：`tests/test_terminal_runtime.py`、`tests/test_tools.py`

它已经具备：

1. workspace-root 内执行命令。
2. `session_id` 维持 cwd。
3. `workdir` 参数。
4. 前台命令 timeout。
5. timeout 时杀进程组。
6. 捕获 timeout 前的 partial output。
7. 输出 ANSI strip、secret redaction、head/tail truncation。
8. blocking guard：空命令、非法 timeout、workspace escape、部分长跑命令。

但它本质上仍是「带 cwd 记忆的前台 shell command runner」，还不是完整 terminal emulator / exec runtime：

1. 没有 PTY。
2. 没有 stdin/write/submit/EOF。
3. 没有 foreground/background 统一进程模型。
4. 没有 poll/wait/kill 生命周期工具。
5. 没有真实 streaming output 事件。
6. 没有大输出落盘 / artifact 引用。
7. 没有 shell snapshot / 用户 shell 环境复现。
8. 没有可配置 exec policy、sandbox policy、elevated permission。
9. 没有 local/Docker/SSH/remote backend 抽象的成熟边界。

这意味着 Pulsara 的下一步不应只是给 `TerminalTool` 加几个参数，而应把它提升成 execution runtime。

## 1. Hermes 的理解：terminal 是多后端执行环境 + 进程管理入口

Hermes 的主入口是 `tools/terminal_tool.py`，相关承重模块包括：

- `tools/environments/local.py`
- `tools/environments/docker.py`
- `tools/environments/ssh.py`
- `tools/environments/modal.py`
- `tools/process_registry.py`
- `tools/tool_output_limits.py`
- `tools/tool_result_storage.py`
- `tools/approval.py`
- `agent/tool_guardrails.py`

### 1.1 多后端统一语义

Hermes 明确把 terminal 视为「执行环境」抽象，而不是单纯的本机 subprocess：

```text
local / docker / ssh / modal / singularity / daytona
```

这些 backend 共享类似的 terminal tool 语义：

- 同一个 `terminal(command=...)` API。
- 同一个 cwd / env / timeout 概念。
- 同一个 foreground/background 概念。
- 同一个 process registry 管理后台任务。

这个设计的核心启示是：backend 是 terminal runtime 的实现细节，不应泄漏到 LLM 的基本工具心智中。模型应该先理解「我要执行命令」，而不是先理解「我要调用 local subprocess 还是 Docker exec」。

### 1.2 foreground 与 background 是第一等语义

Hermes 的 tool description 明确区分：

- foreground：适合短命令、测试、构建；命令结束就返回。
- background：适合服务器、watcher、长任务；返回 `session_id`，后续通过 process tool 轮询/等待/终止。

它还强烈提示：不要用 `nohup`、`disown`、`setsid`、尾部 `&` 来绕过工具层管理；长跑任务应使用 `background=true`，这样 runtime 才能追踪 lifecycle 和 output。

这比 Pulsara 当前「识别长跑命令然后直接 block」更成熟。企业级 terminal 不应只阻止长跑命令，而应提供被管理的后台运行方式。

### 1.3 Process registry 是 terminal 的另一半

Hermes 的 `tools/process_registry.py` 管理后台进程：

- output rolling buffer。
- status polling。
- wait。
- kill。
- crash recovery checkpoint。
- stdin write / submit / close。
- watch pattern notification。
- process TTL / finished TTL。
- 最大并发进程数。

这说明 terminal 工具真正完整时，至少有两类工具或动作：

1. start command。
2. interact with existing process。

Codex CLI 也采取类似形态：`exec_command` 启动或继续等待，`write_stdin` 向 live process 写入。

### 1.4 PTY 是可选能力，不是默认万能开关

Hermes 支持 `pty=true`，用于：

- Python REPL。
- Claude Code / Codex CLI。
- 需要 TTY 的 interactive CLI。

但 Hermes 也有反例规则：某些命令需要 pipe stdin，例如 `gh auth login --with-token`，在 PTY 模式下反而会挂住。因此它会检测这类命令并自动禁用 PTY。

启示：Pulsara 不应把「企业级 terminal」理解为所有命令都走 PTY。正确模型是：

```text
pipe mode: 默认、可控、适合脚本/CI/stdin pipe
pty mode: 显式启用、适合交互 CLI/REPL
```

### 1.5 输出预算是系统级问题

Hermes 有三层输出防线：

1. tool 内部先限制输出。
2. oversized tool result 保存到 sandbox temp dir，只把 preview + path 给模型。
3. 单轮多个 tool result 总量超限时，再把最大结果 spill 到文件。

这和 Pulsara 的 event log 设计天然兼容：Pulsara 可以保留完整事件/完整输出，但给 LLM replay 的上下文必须是 projection，而不是原始无界日志。

### 1.6 shell 环境复现很重要

Hermes 的 local environment 会处理：

- shell 查找。
- login shell 初始化。
- source shell init files。
- PATH 修复。
- provider secret env blocklist。
- HOME profile 隔离。
- cwd 被删除后的恢复。

这类细节决定了用户体验。否则用户会不断遇到：「我终端能跑，agent 里找不到命令 / 找不到 node / 找不到 uv / PATH 不对」。

## 2. OpenClaw 的理解：terminal 是受策略约束的 exec runtime

OpenClaw 的相关文件包括：

- `docs/gateway/sandbox-vs-tool-policy-vs-elevated.md`
- `src/agents/sessions/bash-executor.ts`
- `src/agents/bash-tools.exec-runtime.ts`
- `src/agents/sessions/tools/bash.ts`
- `src/agents/sessions/tools/output-accumulator.ts`
- `src/agents/embedded-agent-runner/tool-result-context-guard.ts`

### 2.1 三层控制：sandbox / tool policy / elevated

OpenClaw 文档把三个概念拆得很清楚：

1. **Sandbox**：决定工具在哪里运行。
2. **Tool policy**：决定哪些工具可用。
3. **Elevated**：exec 专属的逃生阀，用于在 sandboxed 场景下请求 host 执行。

这是 Pulsara 现在缺的一层抽象。当前 Pulsara 有 `PermissionGate` skeleton 和 terminal guard，但它们还没有形成清楚的策略模型。

对 Pulsara 来说，应该避免把这些概念揉进 `TerminalTool.execute()`：

- 工具是否暴露：registry/tool policy。
- 命令是否需要用户确认：exec approval policy。
- 命令在哪里跑：execution environment / sandbox policy。
- 命令是否能绕过 sandbox：elevated permission。

### 2.2 Streaming output accumulator

OpenClaw 的 `OutputAccumulator` 体现了成熟 terminal output 的基本姿态：

- 增量接收 output chunk。
- rolling tail 保持内存有界。
- 超过阈值后打开私有临时文件保存 full output。
- snapshot 时返回 tail + truncation metadata + fullOutputPath。

Pulsara 当前 `finalize_output()` 是命令结束后一次性处理字符串。它对短命令很好，但无法支撑：

- 长时间运行的测试。
- server log。
- 大构建日志。
- UI 实时更新。
- 后台进程轮询。

### 2.3 结果投影和上下文守卫

OpenClaw 对 tool result context 做额外守卫：

- 单个 tool result 过大时裁剪。
- 多个 tool result 累加过大时预防性 compact / truncate。
- UI 可展示更多，模型上下文只拿必要 projection。

这与 Pulsara 的 event sourcing 思路一致：原始事件可以完整保存，但 runtime replay 给模型的内容必须经过预算控制。

### 2.4 host env 安全

OpenClaw 对 host execution 的环境变量很谨慎：

- 危险 inherited env var 会被过滤。
- 自定义 PATH 可能被视为 binary hijacking 风险。
- host execution 与 sandbox execution 的 env policy 不同。

Pulsara 如果未来支持 host/sandbox/elevated，就需要把 env policy 纳入 terminal runtime，而不是让 subprocess 默认继承全部环境。

## 3. Codex CLI 的理解：unified exec 是工具系统底座

Codex CLI 的相关文件包括：

- `codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs`
- `codex-rs/core/src/tools/handlers/unified_exec/write_stdin.rs`
- `codex-rs/core/src/tools/runtimes/unified_exec.rs`
- `codex-rs/core/src/tools/runtimes/shell.rs`
- `codex-rs/core/src/shell.rs`
- `codex-rs/core/src/shell_snapshot_tests.rs`
- `codex-rs/core/tests/suite/unified_exec.rs`

### 3.1 exec_command + write_stdin

Codex 的 unified exec 暴露两个核心动作：

```text
exec_command: 启动命令，可能返回仍在运行的 process/session id
write_stdin: 给已有 process 写输入，也可空写用于 poll
```

这比「一个 terminal 工具同步返回完整结果」更适合真实 agent：

- 长任务可以先 yield 输出，再继续运行。
- 模型可以稍后 poll。
- 交互式命令可以通过 stdin 继续。
- UI 可以记录 terminal interaction event。

### 3.2 process id 是 runtime 资源，不只是工具返回文本

Codex 的 `UnifiedExecProcessManager` 负责分配 process id、维护 live process、释放 id、处理 stdin、返回 chunk id 和 wall time。

这说明 `process_id/session_id` 不应只是 JSON 里的一个字符串，而应成为 runtime session 里的受管理资源，有生命周期、所有权、清理策略和事件记录。

### 3.3 approval / sandbox / network 在 runtime 层统一处理

Codex 的 unified exec runtime 在真正执行前，会处理：

- sandbox permission。
- additional permission。
- approval policy。
- network approval。
- cached approval。
- command canonicalization。
- apply_patch intercept。
- hook payload。

这证明成熟系统不会把 exec 当作普通 tool call。exec 是最危险也最常用的工具，需要自己的 policy pipeline。

### 3.4 shell snapshot

Codex 有 shell detection 和 shell snapshot，用于捕获用户 shell 的环境状态。它关心：

- Bash/Zsh/Sh/PowerShell/Cmd 的执行参数不同。
- login shell 与 non-login shell 不同。
- snapshot 文件要能安全 source。
- snapshot 生命周期要被管理。

这对桌面 agent 尤其重要。Pulsara 当前固定 `bash -c`，未来很容易在 macOS 用户环境里遇到 PATH 与 shell init 问题。

## 4. 三个项目的共同结论

三个项目虽然实现语言和架构不同，但对 terminal 的理解高度一致。

### 4.1 terminal 不是普通工具，而是 execution runtime

成熟实现都不会把 terminal 仅视为：

```text
input command -> output string
```

而是视为：

```text
command -> managed process -> output stream -> lifecycle events -> projected result
```

因此 Pulsara 下一步要补的是 runtime，而不是在 `TerminalTool` 上继续加零散参数。

### 4.2 foreground/background 必须进入工具协议

只支持 foreground 会导致两种坏行为：

1. 长跑命令被 block，模型无法启动 server/watch/test。
2. 模型用 shell 自己的 `&`、`nohup`、`disown` 逃逸，runtime 失去进程追踪能力。

正确做法是显式支持 managed background。

### 4.3 PTY 与 pipe mode 都必须存在

PTY 是交互能力的基础，但不是默认安全选择。pipe mode 更适合脚本、CI、stdin pipe；PTY 更适合 REPL 和交互 CLI。

### 4.4 输出必须分「完整保存」与「上下文投影」

UI、event log、artifact 可以保存完整输出；模型上下文必须拿预算内 projection。

这和 Pulsara 近期 OpenAI SDK streaming 的设计原则一致：先忠实记录 delta，再在 replay/projection 层控制体积。

### 4.5 策略层必须独立于工具实现

审批、沙箱、工具可用性、elevated、网络权限，都不应该散落在 terminal backend 里。terminal backend 负责执行；policy runtime 负责决定能不能执行、在哪里执行、是否要问用户。

### 4.6 shell 环境复现决定真实可用性

真实用户不会接受「我的 terminal 能跑，agent terminal 不能跑」。因此 shell detection / login shell / init file / PATH / proxy helper / env policy 是企业级 terminal 的一部分，不是锦上添花。

### 4.7 后台进程必须持续 drain，不能只记录 PID

Hermes 和 OpenClaw 都不是「启动后台进程后放着不管」。它们会为后台进程持续读取 stdout/stderr，把输出写入 rolling buffer / accumulator。原因很硬：如果没有 reader 持续 drain，OS pipe buffer 填满后，子进程会阻塞在 `write()` 上，后台任务就会假死。`sleep` 这类零输出测试无法发现这个问题，持续输出的 dev server / build / test 才会触发。

因此 Pulsara 的 background support 不能只返回 `process_id`。最小可用形态必须包含：

1. 每个 live process 一个持续 drain 机制（reader thread 或 async task）。
2. 线程安全 output accumulator。
3. shutdown/teardown 时的 best-effort process cleanup。
4. background process 不得在退出时回写 session cwd，避免和前台命令争抢 cwd 状态。

## 5. 对 Pulsara 的启示

Pulsara 当前工具系统很小，这是优点：边界干净，容易重构。但下一步应避免两类错误：

1. **只给 terminal 加 PTY**：这会得到一个能交互但不可管理的工具，仍缺后台进程、输出、策略。
2. **先堆更多工具**：browser、MCP、subagent、skill 都重要，但它们最终也会依赖工具策略、输出预算和 runtime lifecycle。

推荐路线：

1. 先把 terminal 从 foreground command runner 升级为 managed execution runtime。
2. 同时建立工具系统的 policy / output / lifecycle 基础设施。
3. 后续再接 MCP、browser、subagent、skills。

## 6. Pulsara 终端能力缺口清单

按优先级排列：

1. Managed background process。
2. Per-process reader drain + 线程安全 output accumulator。
3. Process tool actions：poll / wait / kill / write / submit / close_stdin。
4. PTY / stdin / EOF / interrupt。
5. Streaming output events。
6. Output artifact / full-output ref。
7. Shell detection / shell init / shell snapshot。
8. Exec policy：approval、dangerous command、long-running guidance。
9. Backend abstraction：local first, Docker/SSH later。
10. Environment policy：secret filtering、PATH handling、proxy helper visibility。
11. UI-friendly terminal events：chunk id、wall time、running process id、exit code、status。

## 7. 参考文件

Pulsara:

- `src/pulsara_agent/tools/builtins/terminal.py`
- `src/pulsara_agent/runtime/terminal/backends/local.py`
- `src/pulsara_agent/runtime/terminal/output.py`
- `src/pulsara_agent/runtime/permission.py`
- `tests/test_terminal_runtime.py`

Hermes:

- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/terminal_tool.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/process_registry.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/environments/local.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/tool_result_storage.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/agent/tool_guardrails.py`

OpenClaw:

- `/Users/plumliu/Desktop/python_workspace/openclaw/docs/gateway/sandbox-vs-tool-policy-vs-elevated.md`
- `/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/bash-tools.exec-runtime.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/sessions/bash-executor.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/sessions/tools/output-accumulator.ts`

Codex CLI:

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/unified_exec/write_stdin.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/runtimes/unified_exec.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/runtimes/shell.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/shell.rs`
