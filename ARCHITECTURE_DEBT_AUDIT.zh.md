# Pulsara 架构债务审计

_Created: 2026-06-24_

这是一份持续更新的债务审计，不是 feature plan。目标很简单：把 Pulsara 现在有哪些“为了兼容旧设计而保留下来的并行 surface”讲清楚，并明确每一项：

- 旧设计来源
- 当前兼容层
- 当前成本
- 能不能 hard cut
- 如果能，按什么顺序 cut

先给结论：Pulsara 已经不再是单一路径系统。它现在有至少八个主 surface 在并存，而且其中几条主 surface 下面又继续长出了额外兼容层：

1. Tool Result Artifact 归档。
2. terminal 输出契约。
3. failed / aborted recovery。
4. permission / approval lattice。
5. memory 的 working_context / recall / reflection 三层。
6. LLM provider 的 chat completions / responses 双适配器。
7. capability / skill discovery / activation。
8. legacy alias / downgrade shim。

这里面还有两条已经明显长成独立兼容层，值得在后文单独展开：

- failed / aborted recovery 不只在 transcript 注入 note，它还在 runtime prompt 里加 recovery 提示。
- permission / approval lattice 不只是策略枚举，还包含 pending approval、suspended state、stop / resume 的 host 状态机。

下面按这个顺序写。

## 1. Tool Result Artifact 归档债务

### 1.1 旧设计来源

最早的事实很简单：大输出工具只需要一个“把结果塞进上下文”的通道。后来 terminal 先有 streaming preview，再引入 full output artifact；再后来 execution evidence ledger 又自己做了一次长输出归档。

当前相关代码：

- [src/pulsara_agent/runtime/tool_artifacts.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/tool_artifacts.py)
- [src/pulsara_agent/tools/executor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/executor.py)
- [src/pulsara_agent/memory/canonical/ledger.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/ledger.py)
- [src/pulsara_agent/runtime/context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/context.py)

### 1.2 当前兼容层

现在至少有两条归档路径：

- `ToolResultArtifactService.process_result()`：executor 侧，把大工具结果转成 artifact ref。
- `ExecutionEvidenceLedger.record_tool_result()` / `record_tool_result_block()`：ledger 侧，对长 output 再归档一次，顺便建图。

> **状态更新（2026-06-25）：已部分收口。** 当前代码已经把“长文本二次归档”这条债务收掉：`ExecutionEvidenceLedger.record_tool_result()` 不再自行 `put_text()`，大输出没有 artifact ref 时会直接拒绝；`record_tool_result_block()` 只消费 executor / artifact service 已经产生的 refs，并负责建图节点。下面仍保留原债务描述，作为为什么要守住这条边界的历史背景。

另外，`runtime/context.py` 还在把 `ToolResultBlock` 渲染成模型可见文本时保留 preview / artifact envelope。
而且这里已经不是单 artifact 形状了：`ToolResultBlock.artifacts` 是一个 refs 列表，`ToolResultArtifactRecord` 也在记录 `role` / `ordinal`，所以同一个 tool call 可以自然带出多个 artifact ref。
如果工具本身没有显式 `artifact_candidates`，`ToolResultArtifactService.process_result()` 还会从 `result.output` 合成一个 fallback candidate 再归档，这说明“归档谁来决定”仍然是 executor 的兼容职责，不是纯被动的数据搬运。

### 1.3 当前成本

- 两套归档逻辑。
- 两套阈值决策。
- 两套 owner / session 校验入口。
- artifact 形状已经变成 list + ordinal 语义，不再是旧时代的单一“结果文件”。
- 还存在“无 candidate 时由 executor 合成 fallback candidate”的隐式路径。
- future 改 policy 时要改两处。

更关键的是，`ledger` 这条路径并不是“无害的第二视图”，它自己会创建 `Artifact` / `ToolResult` 图节点，等于又多了一层事实投影。

> **状态更新（2026-06-25）：主要成本已下降。** 两套阈值已经统一为 8KB；ledger 不再拥有长文本归档入口；multi-artifact refs 会全部建 `Artifact` 图节点；external / non-executor 的大 `ToolResultBlock` 如果缺少 artifacts，会在 persistence hook / ledger 路径被拒绝。剩余成本主要是 executor fallback candidate 仍然是一条兼容兜底，以及 graph/timeline 仍需要消费 artifact refs 的业务投影。

### 1.4 能不能 hard cut

**能，但要分两步。**

长期目标应该是：**executor 是唯一归档者，ledger 不再自己写长文本，只消费 artifact ref。**

但现在不能直接把 ledger 删掉，因为：

- 现有 memory evidence / graph 还在依赖它。
- `record_tool_result_block()` 现在承担了图节点构建职责。

### 1.5 推荐 cut 顺序

1. 先把所有长结果的“权威 artifact id”固定成一条协议。
2. 让 executor 产出 artifact ref，ledger 只接收 ref，不再自行 `put_text`。
3. 再把 ledger 的长文本归档彻底删掉，只保留图节点和引用关系。
4. 最后统一阈值和 ownership 校验入口。

> **状态更新（2026-06-25）：1-4 已基本完成到 V1。** 当前还要继续守的不是“怎么再归档一次”，而是确认所有新的 tool execution surface 都能产出 `ToolResultEndEvent.artifacts`，不要绕回 ledger 或临时文件路径。

这个 cut 的核心标准只有一个：**长文本只归档一次。**

---

## 2. terminal 输出契约债务

### 2.1 旧设计来源

terminal 是逐步补出来的：

- 先有流式 preview。
- 再发现 preview 可能不完整，于是加 full output artifact。
- 再发现 yielded process 需要生命周期可见，于是加 `terminal_process log/list/poll/wait`。

相关代码：

- [src/pulsara_agent/runtime/context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/context.py)
- [src/pulsara_agent/runtime/tool_artifacts.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/tool_artifacts.py)
- [src/pulsara_agent/runtime/terminal_risk.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal_risk.py)
- [src/pulsara_agent/host/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/transcript.py)
- [src/pulsara_agent/runtime/tool_loop.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/tool_loop.py)

### 2.2 当前兼容层

现在 terminal 的同一语义被三层 surface 分担：

- streaming preview：给当前 turn 及时反馈。
- artifact：给长输出留退路。
- `terminal_process` log：给 yielded process 的后续状态做生命周期投影。

> **状态更新（2026-06-25）：三层 surface 已正式写成契约。** 详见 [TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md)。当前代码符合 V1：preview 非权威，artifact 是完整输出读取入口，completion event 只承载生命周期元数据和 bounded preview。

### 2.3 当前成本

- 模型看到的是 preview，不一定是 full output。
- full output 又被 artifact 读回。
- 进程状态又通过 `TerminalProcessCompletedEvent` 单独注入。

也就是说，terminal 的“输出”不再是一个单点定义，而是一个组合契约。这个组合契约是可用的，但很容易在新 feature 里继续长出第四、第五条路径。

### 2.4 能不能 hard cut

**能收敛，但不能回退。**

不能回到“只有 stdout preview”那种老设计，因为那会丢失大输出和后台进程生命周期。

可以 hard cut 的方向是：

- 明确 preview 只是 UI / prompt 载体。
- artifact 是完整持久层。
- process log 是生命周期层。

也就是说，不是去掉三层，而是把三层正式定义清楚，禁止再发明新的 terminal 输出侧路。

### 2.5 推荐 cut 顺序

1. 先把 terminal 输出契约写成统一规范。
2. 再把 `terminal_process` 的只读 action 与有副作用 action 分层。
3. 最后禁止任何“第四种 terminal output 侧路”。

> **状态更新（2026-06-25）：第 1 步已完成，第 2 步已有实现。** `terminal_process list/log/poll/wait` 已作为只读观察动作处理，`write/submit/close_stdin/kill` 仍是会改变进程状态的动作。后续重点是继续守住 tool description、completion note、prompt 文案里的 preview/artifact/completion 边界，不再引入新的 output 侧路。

这里的 hard cut 不是删 surface，而是**封口**。

### 2.6 额外债务：terminal 环境注入 / shell snapshot

#### 2.6.1 旧设计来源

terminal 不只是执行命令，它还试图还原一个“登录 shell + workspace venv + fallback PATH”的环境：

- 先抓 shell snapshot，再把结果注入 subprocess env。
- 再叠 `inherit_allowlist` / `passthrough_names`。
- 再叠 workspace 里的 `.venv/bin` overlay。
- 再叠 `SANE_FALLBACK_PATH`。
- 还要接受 v1 解析上限与超时失败。

相关代码：

- [src/pulsara_agent/runtime/terminal/env.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/env.py)
- [src/pulsara_agent/runtime/terminal/models.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/models.py)
- [tests/test_terminal_env.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_terminal_env.py)
- [tests/test_terminal_runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_terminal_runtime.py)

#### 2.6.2 当前兼容层

现在这条路径至少分成四层：

- `sanitize_subprocess_env()`：做基础 allowlist 清理和 secret 值去除。
- `capture_shell_env_snapshot()`：通过 login shell 采样补齐 interactive 环境。
- `find_nearest_venv_bin()`：优先把 workspace 的虚拟环境放到 PATH 前面。
- `build_default_subprocess_env()`：给没有 snapshot 的场景补一个 fallback env。

`TerminalEnvConfig` 还把这些行为拆成多枚开关：shell snapshot、snapshot TTL、snapshot timeout、inherit allowlist、passthrough names、venv overlay。它们组合起来能用，但已经不是单一规则。

#### 2.6.3 当前成本

- 环境构建不再是一个单点事实，而是一串优先级拼接。
- shell snapshot 可能超时、超输出，或者因为 shell 启动副作用而不稳定。
- PATH precedence 由 venv overlay、snapshot PATH、parent PATH、fallback PATH 共同决定，排错成本高。
- 配置开关越加越多，用户很难直觉判断某个命令最终会看到什么环境。

#### 2.6.4 能不能 hard cut

**能收敛，但不能把“terminal 需要环境塑形”这个事实删掉。**

可以 hard cut 的是这些兼容层叠加：

- 把 shell snapshot 限定成一类输入来源，而不是永远和其他来源并排竞争。
- 把 PATH precedence 和 env allowlist 写成唯一规范，不再由多处默认值共同决定。
- 把 v1 parse 限制视为临时边界，不要让它继续长成新的默认行为。

#### 2.6.5 推荐 cut 顺序

1. 先把 parent env / shell snapshot / venv overlay / fallback PATH 的优先级写成唯一规范。
2. 再收紧 `TerminalEnvConfig` 的开关面，把“实验性/迁移性”行为和稳定行为分开。
3. 最后在所有调用点都改用同一套 canonical env builder，再清掉 `v1` 解析限制带来的额外分支。

这里的目标不是让环境变简单到失真，而是**让环境构建只剩一条可解释的规则链**。

### 2.7 额外债务：terminal completion note 投影

#### 2.7.1 旧设计来源

yielded terminal process 先是作为后台任务存在，后来又在 transcript 重建时额外投影出 completion note，告诉模型“有些后台进程后来完成了”。

相关代码：

- [src/pulsara_agent/host/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/transcript.py)
- [src/pulsara_agent/runtime/terminal/process.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/process.py)

#### 2.7.2 当前兼容层

- `TerminalProcessCompletedEvent` 从 process 生命周期里发出。
- `rebuild_prior_messages()` 再把它投影成 transcript 里的 terminal background task note。
- 这条 note 还会按 `_MAX_COMPLETION_NOTES` 做截断，避免一次塞太多后台完成记录。

> **状态更新（2026-06-25）：当前 V1 选择是轻量 completion note。** completion note 只提示后台进程生命周期事实，并引导模型用 `terminal_process log` 查看 retained output；它不自动读取 artifact，也不把 bounded preview 说成完整日志。

#### 2.7.3 当前成本

- 进程完成既存在于 event log，也再次出现在 transcript note 里。
- 模型看到的是总结后的自然语言，不是结构化进程清单。
- note 本身依赖时间顺序和 run 边界，过多后台任务时会变成“有限摘要”而不是权威状态。

#### 2.7.4 能不能 hard cut

**能收敛。**

completion event 本身应该保留，因为它是后台进程的权威生命周期事实；需要收掉的是 transcript 再投影一层自然语言 note 的做法，或者至少把这层严格限定成只读摘要，不再承担恢复语义。

#### 2.7.5 推荐 cut 顺序

1. 先保留 `TerminalProcessCompletedEvent` 作为唯一权威完成事实。
2. 再把 transcript note 缩减成更明确的只读摘要。
3. 最后如果有更高层 terminal process view，就让它直接消费结构化事件，不再依赖自然语言 note。

### 2.8 额外债务：terminal kill reason / completion suppression

#### 2.8.1 旧设计来源

terminal 最初只需要“进程活着就一直跑，死了就结束”。现在它已经区分出三种 kill 语义：用户显式 kill、teardown kill、lifetime watchdog kill。它们并不只是退出码不同，而是会不会发 completion event、会不会进入 transcript 的不同。

相关代码：

- [src/pulsara_agent/runtime/terminal/process.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/process.py)
- [src/pulsara_agent/runtime/terminal/manager.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/manager.py)
- [tests/test_terminal_runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_terminal_runtime.py)

#### 2.8.2 当前兼容层

- `TerminalKillReason.USER`、`TEARDOWN`、`LIFETIME_WATCHDOG` 并存。
- `_mark_kill_reason()` 会对 teardown / watchdog 关闭 completion event 记录。
- `kill_owned()` / `shutdown()` / `kill_process()` 走不同生命周期入口。
- `test_terminal_runtime_user_kill_records_completion_event_but_shutdown_suppresses()` 明确验证了 user kill 与 teardown kill 的分叉。

#### 2.8.3 当前成本

- 一个“kill”动作不再有单一语义。
- 上层如果不知道是 user kill 还是 teardown kill，就不能判断该不该让模型看到 completion note。
- 这会让 terminal lifecycle 变成“状态 + 退出原因 + 事件投影”三层判断。

#### 2.8.4 能不能 hard cut

**不能删 kill reason，但可以收口。**

这三种 reason 不是偶然加出来的，它们在避免噪声和避免误投影上都有用。能收敛的是：不要让每个调用方自己猜“这次 kill 会不会生成 note”，而是把 kill reason 变成统一生命周期契约的一部分。

#### 2.8.5 推荐 cut 顺序

1. 先把 user / teardown / watchdog 三种 kill 语义保留成唯一权威入口。
2. 再把 completion suppression 的决策集中到同一个生命周期层。
3. 最后让 transcript / host 只消费完成后的结构化结果，不再自己判断 kill 类型。

---

## 3. failed / aborted recovery 债务

### 3.1 旧设计来源

根因是 canonical event log 和 provider replay 的约束不一致：

- canonical log 想保留真实事件。
- provider replay 想要严格配对。
- 模型恢复又必须知道上一轮哪些工具“可能执行了但没完成”。

相关代码：

- [src/pulsara_agent/host/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/transcript.py)
- [src/pulsara_agent/message/assembler.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/assembler.py)
- [src/pulsara_agent/message/reducer.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/reducer.py)

### 3.2 当前兼容层

现在的恢复路径做了三件事：

- replay 时剥离 unfinished tool call。
- failed / aborted 时注入 note。
- note 里再做 state × severity 措辞矩阵。

这套机制是实用的，但它本质上是在 transcript 层补一个“恢复投影”。

### 3.3 当前成本

- note 逻辑越来越像小型规则引擎。
- 需要维护 all-events 语义、late result、自愈、工具风险分桶。
- 新增 terminal / approval / memory 语义时，note 会继续膨胀。

这不是 bug，而是抽象层次还不够高：恢复信息仍然只能从底层事件拼出来。

### 3.4 能不能 hard cut

**不能直接 hard cut transcript note。**

因为 provider replay 的配对约束不会消失，而 canonical log 也不能假造 tool result。

能 hard cut 的是“把恢复语义继续散落在多个地方”的做法。恢复信号应该逐步上升成更高层的 runtime 结构，而不是继续把文案矩阵堆在 transcript 里。

### 3.5 推荐 cut 顺序

1. 先保持当前 note 作为唯一恢复投影入口。
2. 再把 `unfinished` 的分类逻辑继续上收成独立领域对象。
3. 未来若有更高层 recovery state，再让 transcript 只消费该 state，不直接读一堆原始事件。

这里的 hard cut 目标不是“删 note”，而是“别再让 note 背更多职责”。

### 3.6 额外债务：runtime recovery prompt / stop scratchpad

#### 3.6.1 旧设计来源

恢复不只是在 transcript 里注入 note。runtime 还在 prompt assembly 里额外塞了一条固定 recovery 提示；实际注入点不是单独的 `_build_system_prompt()`，而是 `src/pulsara_agent/runtime/context.py::build_llm_context()`，这里会在 `state.recovery_mode` 为真时追加 recovery user message。`state.recovery_mode` 的开关则由 `src/pulsara_agent/runtime/agent.py` 里的主循环设置，而 stop 语义仍通过 host-local scratchpad 和 runtime 状态拼出来。

相关代码：

- [src/pulsara_agent/runtime/context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/context.py)
- [src/pulsara_agent/runtime/agent.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/agent.py)
- [src/pulsara_agent/host/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)

#### 3.6.2 当前兼容层

- `LoopState.recovery_mode` 为真时，`build_llm_context()` 追加一条通用 recovery user message。
- `HostSession.stop_current_turn()` 和 `AgentRuntime.abort_run()` 通过 `scratchpad["stop_requested"]` / `scratchpad["abort_reason"]` 把 user stop 传给 runtime。
- `state.status` / `state.stop_reason` / `state.finalized` 共同把失败、暂停、终结这三类结果串成一个可恢复的流程。

#### 3.6.3 当前成本

- 恢复信号被拆成两层：一层在 transcript，一层在 prompt assembly。
- 这条 prompt 级 recovery 是“泛化提示”，不能区分 failed / aborted / partial tool execution 的差别。
- stop 语义还依赖 stringly-typed scratchpad key，不是一个强类型的运行时状态。

#### 3.6.4 能不能 hard cut

**能，而且应该尽量收。**

可以保留恢复提示本身，但不应继续让它靠零散的 scratchpad 和自由文本 note 拼装。长期目标应该是让 runtime 先产出结构化 recovery state，再让 transcript / prompt 只消费这一份状态。

#### 3.6.5 推荐 cut 顺序

1. 先把 failed / aborted / partial-execution 的恢复分类上收到独立结构。
2. 再让 prompt assembly 只消费结构化 recovery state，不直接拼固定英文句子。
3. 最后把 host stop 的 scratchpad 迁到更显式的 runtime contract 里。

### 3.7 额外债务：unfinished tool call 分类器

#### 3.7.1 旧设计来源

为了让 failed / aborted note 不只是机械列工具名，系统又加了一层工具未完成分类器：它要判断工具是“已开始但没完成”、“只是在等待 approval”还是“语义不确定”，并按工具风险类型给出不同措辞。

相关代码：

- [src/pulsara_agent/host/unfinished_tools.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/unfinished_tools.py)
- [src/pulsara_agent/host/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/transcript.py)
- [tests/test_unfinished_tools.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_unfinished_tools.py)

#### 3.7.2 当前兼容层

- `classify_unfinished_tool_calls()` 从事件流里推导 proposed / attempted / completed / pending 四类信息。
- `UnfinishedState` 与 `ToolSeverity` 把 pending approval、started no completed result、ambiguous generation 这些场景拆开。
- `render_unfinished_summary()` 再把分类结果压成一段 note 里可读的恢复提示。
- `_wording_for()` 还为 terminal、bounded write、read-only 工具分别准备了不同风险措辞。

#### 3.7.3 当前成本

- 恢复 note 不再是简单的“失败了”，而是事件分类器 + 风险分桶 + 文案模板的合成结果。
- 这套逻辑必须和 transcript 重建、completion note、approval resume 一起保持一致，否则就会出现前后 note 互相打架。
- 一旦工具类型或风险定义变化，这套分类器就要同步跟着改。

#### 3.7.4 能不能 hard cut

**能收，但不能删。**

这层分类器本身是必要的，因为它把“未完成”从纯文案变成了结构化判断。能 cut 的是：不要让它继续作为隐式的半个规则引擎长期漂在 transcript 里；未来它应该上收成更高层的 recovery domain 对象。

#### 3.7.5 推荐 cut 顺序

1. 先把 unfinished tool call 分类器当成恢复语义的唯一来源之一。
2. 再把它的输出上收到结构化 recovery state。
3. 最后让 transcript note 只负责展示，不再自己决定“该怎么措辞”。

### 3.8 额外债务：FAILED reflection gate 的历史收口

#### 3.8.1 旧设计来源

早期的 reflection gate 只是在“明显中断/中止”的语义上先做了保守跳过；后来 FAILED 也被纳入同一条 no-reflection 规则。当前实现里，`src/pulsara_agent/memory/hooks/durable.py` 已经把 `LoopStatus.ABORTED` 和 `LoopStatus.FAILED` 一起拦掉。

#### 3.8.2 当前判断

这不是一个还悬着的 omission bug，而是一个已经收口的历史演进点。真正值得继续审的是：为什么“遇到 terminal failure 不做 reflection”的规则还要同时靠 hook 条件和 transcript 语义去维持，而不是先收成一个显式的 terminal-state policy。

#### 3.8.3 能不能 hard cut

**不能删 no-reflection 规则，但可以收口。**

未来如果要再改 FAILED / ABORTED 的反思策略，应该改的是统一 policy 的位置，而不是继续让 transcript / hook 各自隐式理解“这次要不要反思”。

---

## 4. permission / approval lattice 债务

### 4.1 旧设计来源

permission 是从简单 allow/deny 演化出来的，后来又叠了：

- `approval_policy`
- `terminal_access`
- `trusted_host / workspace_guarded / read_only`
- `terminal_process` 只读 action 豁免
- `WAIT_FOR_USER` 的 approval resume

相关代码：

- [src/pulsara_agent/runtime/permission.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/permission.py)
- [src/pulsara_agent/runtime/approval.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/approval.py)
- [src/pulsara_agent/host/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)

### 4.2 当前兼容层

当前系统里，兼容层主要有三块：

- `PolicyPermissionGate` 里兼容老的危险 terminal 规则。
- `HostSession` 里保存 pending approval / suspended state / stop state。
- `terminal_process` 里又加了 action-level 只读特例。

> **状态更新（2026-06-25）：只读 action 豁免已实现，主路径仍待收敛。** `terminal_process list/log/poll/wait` 不再因为 `terminal=ask` 反复触发审批；但 permission / approval 的产品主路径仍需要继续收敛，尤其是 `trusted_host + on_request + ask/allow` 和 `read_only + on_request + off` 的入口语义。

### 4.3 当前成本

- policy lattice 比直觉更复杂。
- `ask` / `on_request` 的可用性依赖 approval resume 是否完整。
- host 需要同时处理 busy lock、pending approval、stop in flight、suspended state。

### 4.4 能不能 hard cut

**能，但要先把“哪个组合是产品主路径”明确出来。**

现在最重要的不是再加更多 policy，而是收敛成少数几个稳定入口：

- inspect
- trusted_host + on_request + allow/ask
- read_only

这里的 3×3×3 不是纯理论数字：`read_only` 只允许 `terminal=off`，因此 27 格里有 6 格无效，实际有效组合是 21 个。下面这组矩阵只描述“组合是否合法”，不重复展开每个 profile 的默认 wiring 差异。

### 4.5 推荐 cut 顺序

1. 先把 `ask` / `on_request` 的真实产品路径跑顺。
2. 再收掉那些纯粹为了保留旧行为而存在的兼容分支。
3. 最后把 permission 规则收敛成更少的稳定组合。

### 4.6 额外债务：pending approval / suspended / stop 状态机

#### 4.6.1 旧设计来源

最早的 host 只需要启动一个 turn、拿到结果、进入下一轮。等 approval resume 和 user stop 加进来以后，host/session 变成了一个显式状态机：可运行、可挂起、可停止、可恢复。

相关代码：

- [src/pulsara_agent/runtime/approval.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/approval.py)
- [src/pulsara_agent/host/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)
- [src/pulsara_agent/runtime/agent.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/agent.py)

#### 4.6.2 当前兼容层

- `HostSession.pending_approval`、`_suspended_state`、`suspended_run_id`、`stopping_run_id` 同时存在。
- `run_turn()`、`stream_turn()`、`resolve_approval()`、`stream_approval_resolution()`、`stop_current_turn()` 分别承载不同入口。
- `approval_id` 是 host minted，而不是事件 schema 里的固有字段。
- `pending_approval_from_state()` 和 `_capture_pending_approval()` 把运行时状态转换成可外部恢复的对象。

#### 4.6.3 当前成本

- 同一条 run 的生命周期被拆成多个公开入口，语义要靠 lock、pending、suspended、stopping 共同约束。
- host 必须同时防 busy、pending approval 和 stop in flight。
- 这套状态机是 approval resume 和 user stop 能共存的原因，但也让“一个 turn 到底还活不活着”不再是单一事实。

#### 4.6.4 能不能 hard cut

**不能删状态机，但能收口。**

审批恢复和 user stop 都是真产品路径，所以这条状态机本身要保留。能 cut 的是这些临时桥接字段和多入口分裂，让它们最终汇入一套更明确的 host lifecycle contract。

#### 4.6.5 推荐 cut 顺序

1. 先保证 `suspended_run_id` / `_suspended_state` / `pending_approval` 的语义一致且可恢复。
2. 再把 approval / stop 的分岔收成一条明确的 host lifecycle。
3. 最后减少外部 API 里暴露的并行入口，只保留少数稳定动作。

### 4.7 组合矩阵（21 个有效组合）

`profile × approval × terminal = 3 × 3 × 3` 一共 27 格。`read_only` 只允许 `terminal=off`，所以只有 21 格有效。下面只枚举“有效/无效”的边界，不把每个组合的细微等待语义重复三遍。

#### 4.7.1 trusted_host

| approval \ terminal | off | allow | ask |
| --- | --- | --- | --- |
| never | valid | valid | valid |
| risky_only | valid | valid | valid |
| on_request | valid | valid | valid |

备注：`terminal=ask` 会压过 `approval=never`，这是有意设计，不是意外放行。

#### 4.7.2 workspace_guarded

| approval \ terminal | off | allow | ask |
| --- | --- | --- | --- |
| never | valid | valid | valid |
| risky_only | valid | valid | valid |
| on_request | valid | valid | valid |

备注：同样，`terminal_process` 的 `list/log/poll/wait` 仍有只读豁免，不会因为 `ask` 把观察型动作也锁死成副作用动作。

#### 4.7.3 read_only

| approval \ terminal | off | allow | ask |
| --- | --- | --- | --- |
| never | valid | invalid | invalid |
| risky_only | valid | invalid | invalid |
| on_request | valid | invalid | invalid |

备注：`read_only + on_request + off` 是 canonical inspect 组合，应该作为明确 accept case 保住，而不是只靠默认路径间接覆盖。

#### 4.7.4 语义说明

- `trusted_host` 和 `workspace_guarded` 在当前 gate 代码里没有分叉语义，矩阵相同是正常的；两者的差异主要体现在默认 policy wiring，而不是组合合法性。
- `read_only` 才是这张表里真正收窄的 profile：它把 `terminal` 强制压到 `off`。
- `terminal_process` 的 `list/log/poll/wait` 仍然是 action-level 的只读豁免，不会因为 `ask` 变成副作用动作。

---

## 5. 额外债务：Host / session ownership

### 5.1 旧设计来源

Pulsara 早期是单轮 / 单 session 思路，后来又叠出：

- `HostSession`
- `RuntimeSession`
- terminal manager ownership
- suspended approval state
- stop / resume / close 边界

相关代码：

- [src/pulsara_agent/host/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)
- [src/pulsara_agent/runtime/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/session.py)
- [src/pulsara_agent/runtime/wiring.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/wiring.py)

### 5.2 当前兼容层

- `HostSession` 继续承载 per-host 语义。
- `RuntimeSession` 仍承载 per-run / per-workspace 的资源所有权。
- terminal manager 既可以 owned，也可以 borrowed。

### 5.3 当前成本

- close 语义越来越需要 owner / borrowed 区分。
- stop / resume / pending approval 的状态机越来越明显。
- workspace shared supervisor 是下一层很自然的演化，但会进一步增加所有权复杂度。

### 5.4 能不能 hard cut

**不能删 HostSession / RuntimeSession 分层。**

它们是当前系统真正的生命周期边界。能收敛的是：

- 不要再把“session”这个词混成单一概念。
- 明确哪些资源属于 host，哪些属于 runtime，哪些属于 workspace。

### 5.5 额外债务：workspace-scoped terminal supervisor

#### 5.5.1 旧设计来源

terminal 早期是按单 session / 单 manager 去想的。后来为了让同一 workspace 下的多个 host session 共享一个 terminal 资源池，又加了一层 `WorkspaceTerminalSupervisor`，把 `TerminalSessionManager` 提到了 workspace 级别。

相关代码：

- [src/pulsara_agent/host/core.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/core.py)
- [src/pulsara_agent/host/supervisor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/supervisor.py)
- [src/pulsara_agent/host/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)
- [tests/test_host_core.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_host_core.py)

#### 5.5.2 当前兼容层

- `HostCore.use_workspace_supervisor` 决定是否启用共享 supervisor。
- `_supervisors` 以 `workspace_key` 为键缓存 workspace 级 terminal manager。
- `attach()` / `detach()` / `shutdown()` 把 owner session 的生命周期和 workspace 级 terminal 池绑在一起。
- `terminal_summary` / `list_workspace_supervisors()` 还在暴露这层共享池的诊断视图。

#### 5.5.3 当前成本

- terminal 资源不再只属于一个 host session，而是同时受 workspace 共享层和 owner session 层控制。
- 关闭 host session、关闭 workspace、关机三条路径对 terminal 的影响不同。
- 这让“谁能看见 / 谁能 kill / 谁负责清理”变成了一个三层所有权问题。

#### 5.5.4 能不能 hard cut

**不能删共享 supervisor，但能收口。**

共享 supervisor 不是随便加的实现细节，它是让 workspace 级终端池可复用的真实产品能力。能 cut 的是它的接口面：不要让 host / CLI / UI 各自理解不同的 supervisor 语义，而是收成一个统一的 workspace terminal lifecycle contract。

#### 5.5.5 推荐 cut 顺序

1. 先保留 workspace 共享 supervisor 作为统一 terminal 池。
2. 再把 host close / workspace close / shutdown 的清理语义统一起来。
3. 最后让外部只消费一个 workspace 级 terminal summary，不再分别理解 host-owned 和 workspace-owned 的细节。

---

## 6. 额外债务：memory surfaces 并存

### 6.1 旧设计来源

memory 不是一个单点系统，而是逐步长成了三层：

- `working_context`：最近活动摘要，属于 operational cache，不是 canonical semantic memory。
- `recall`：从 canonical graph / query service 里做检索注入。
- `reflection`：Flash 模型在 session end / safe point 上生成新的记忆候选。

相关代码：

- [src/pulsara_agent/memory/working_context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/working_context.py)
- [src/pulsara_agent/memory/recall/service.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/service.py)
- [src/pulsara_agent/memory/reflection/engine.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/reflection/engine.py)
- [src/pulsara_agent/memory/hooks/durable.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/durable.py)
- [src/pulsara_agent/memory/canonical/write_service.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/write_service.py)

### 6.2 当前兼容层

- `working_context_projection()` 把 recent activity 以 fenced block 注入 prompt。
- `LexicalMemoryRecallService` 另外注入 recalled memories。
- `ReflectiveMemoryHooks` / `MemoryReflectionEngine` 再在 session 结束点把新候选写入池。
- `MemoryWriteService` 则是 canonical graph 的最终写入口。

### 6.3 当前成本

- 同一轮里可能同时出现 working context、recalled memory、assistant text、tool traces。
- prompt 上已经有 `do_not_write_back` 之类的显式防回写约束，说明系统本身已经意识到这些 surface 容易串味。
- 这不是坏事，但它意味着 memory 已经不是一条路径，而是“读 / 写 / cache / recall”四个语义层。

### 6.4 能不能 hard cut

**不能把这三层合成一层。**

它们本来就服务不同问题：

- working_context 解决最近活动投影。
- recall 解决历史检索。
- reflection 解决把新知识写回 canonical graph。

能 hard cut 的是“把 working_context 当成 durable memory”的模糊说法，以及任何回写污染。

### 6.5 推荐 cut 顺序

1. 先把 working_context 的边界继续固定为 operational cache。
2. 再把 recall / reflection 的语义分工写得更显式。
3. 以后如果要做 memory 产品化，再考虑是否需要统一 user-facing memory view，但不要先把三层压成一层。

### 6.6 额外债务：memory governance 的 v1 subject 相关性 stopgap

#### 6.6.1 旧设计来源

memory governance 现在没有直接用结构化 subject key，而是先靠 token overlap 做一层 v1 相关性排序，再把相关 memory 作为 `related_existing_memories` 喂给 Flash 决策器。

相关代码：

- [src/pulsara_agent/memory/governance/engine.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/engine.py)
- [src/pulsara_agent/memory/governance/dedupe.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/dedupe.py)
- [tests/test_memory_governance_engine.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_memory_governance_engine.py)

#### 6.6.2 当前兼容层

- `_related_existing_memories()` 用 token overlap 做主题相关性 stopgap。
- `MemoryGovernanceEngine._candidate_snapshot()` 依赖这个排序结果生成 `related_existing_memories`。
- `supersede_and_submit` / `contradict_and_submit` 仍然基于这个相关性输入工作，而不是基于独立 subject 图。

#### 6.6.3 当前成本

- 主题相关性现在是“词面相似度”而不是“结构化 subject”。
- `supersede` / `contradict` 的可用性与准确性受 token overlap 影响，容易把相近但不该关联的记忆拉进来。
- 注释已经明确说这是 v1 stopgap，说明后续必然要换结构化 subject key，但目前还没换。

#### 6.6.4 能不能 hard cut

**能，而且应该尽量早 cut。**

这不是产品层兼容，而是算法层 stopgap。它不需要长期与新抽象并存。

可以 hard cut 的方向是：

- 用结构化 subject key 替换 token overlap 排序。
- 让 `related_existing_memories` 的构建依赖明确 subject，而不是启发式 token 交集。
- 保留 `supersede` / `contradict` 这两个语义，但不要再让它们吃词面 stopgap。

#### 6.6.5 推荐 cut 顺序

1. 先把 subject key 的结构化来源补齐。
2. 再让 `related_existing_memories` 改成按 subject key 过滤 / 排序。
3. 最后删除 token overlap 这个 v1 stopgap。

这类债务的特点很直接：**它不是一个长期 surface，但它会污染长期 surface 的判断。**

### 6.7 额外债务：`memory_search` 的 backend-unavailable fallback 指引

#### 6.7.1 旧设计来源

canonical durable-memory recall 设计出来后，并没有立刻吞掉所有旧检索路径；当 recall backend 不可用时，`memory_search` 仍然把模型导向“history search or current files”这一类退路。

相关代码：

- [src/pulsara_agent/tools/builtins/memory_query.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/memory_query.py)
- [src/pulsara_agent/memory/recall/service.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/recall/service.py)

#### 6.7.2 当前兼容层

- `MemorySearchTool.execute()` 在 `RecallStatus.UNAVAILABLE` 时返回 `fallback: history_search_or_current_files`。
- 同一个 payload 还会带 `can_retry: false`，告诉上层这次不是简单重试能解决的。
- 这意味着 canonical memory search 还在显式依赖旧式检索退路，而不是自己成为唯一答案。

#### 6.7.3 当前成本

- 模型仍要理解“我不是查不到，只是 recall backend 当前不可用”这类二级语义。
- 记忆检索路径不是纯 canonical；它在不稳定时又回到历史搜索 / 当前文件。
- 这让 memory_search 的契约比“查 memory”本身更宽，也更容易把产品行为分叉。

#### 6.7.4 能不能 hard cut

**能收，但不应立刻删。**

只要 recall backend 还可能不可用，这个 fallback 指引就是必要的用户可恢复路径。真正能 cut 的是：等 recall backend 足够稳定后，把这条退路移成更明确的结构化降级，而不是继续让它以自由文本 fallback 形态长期存在。

#### 6.7.5 推荐 cut 顺序

1. 先保留 `memory_search` 的显式 fallback 指引，别让 backend 不可用时变成沉默失败。
2. 再把 fallback 退路结构化成更明确的 degraded-mode 契约。
3. 最后当 recall 真的稳定后，再决定是否还能删掉这条退路。

### 6.8 额外债务：run timeline 的 persisted snapshot 投影

#### 6.8.1 旧设计来源

最初 runtime 只有事件日志。后来为了让 memory / UI / host 都能快速读到“这一轮发生了什么”，又加了 run timeline：先从事件流装配出业务级 timeline，再把 timeline 本身持久化成一个 artifact + graph node。

相关代码：

- [src/pulsara_agent/runtime/timeline.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/timeline.py)
- [src/pulsara_agent/memory/hooks/run_timeline_persistence.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/run_timeline_persistence.py)
- [src/pulsara_agent/memory/foundation/run_timeline_query.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/foundation/run_timeline_query.py)
- [src/pulsara_agent/memory/hooks/durable.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/durable.py)

#### 6.8.2 当前兼容层

- `build_run_timeline()` 从 `AgentEvent` 重建出业务 timeline，里面包含 reply / model call / tool call / tool result / permission request / error。
- `RunTimelinePersistenceHook` 在特定 runtime 事件上把 timeline 序列化后写入 archive，并在 graph 里挂 `RunTimelineRecord`。
- `load_run_timeline()` / `summarize_run_timeline()` 再从 persisted timeline 读回 summary，供 working context 和其他上层消费。

#### 6.8.3 当前成本

- 事件已经存在一份，timeline 又是第二份结构化投影。
- timeline 的 status / item_count / summary 是“从事件导出的业务视图”，不是原始事实本身。
- 由于 timeline 生成和持久化都在运行时钩子里，任何事件分类变化都可能影响读侧摘要和持久化产物。

#### 6.8.4 能不能 hard cut

**不能删 timeline，但能收口。**

run timeline 是让 host / memory / UI 共享一份业务视图的有效抽象，不值得回退到只读 event log。能 cut 的是：不要再给 timeline 增加第二套自己独立演化的语义，尤其不要让它和 transcript note / working context 再各自长出互相冲突的总结逻辑。

#### 6.8.5 推荐 cut 顺序

1. 先把 timeline 明确成唯一的“run 业务视图”。
2. 再让 working context / host inspect / UI 都统一从这份视图取摘要。
3. 最后减少任何旁路的重复总结逻辑，让 event log 只负责权威事实、timeline 只负责业务投影。

---

## 7. 额外债务：LLM provider adapter split

### 7.1 旧设计来源

Pulsara 现在同时支持两种 OpenAI-compatible wire protocol：

- Chat Completions。
- Responses。

相关代码：

- [src/pulsara_agent/llm/adapters/openai/chat_completions.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/adapters/openai/chat_completions.py)
- [src/pulsara_agent/llm/adapters/openai/responses.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/adapters/openai/responses.py)
- [src/pulsara_agent/llm/runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/runtime.py)
- [src/pulsara_agent/llm/provider.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/provider.py)

### 7.2 当前兼容层

- 两个 transport 都把 provider stream 转成同一套 `AgentEvent`。
- 两边都各自维护 retry / close-active-block / semantic-output 判断。
- provider profile 还在补 thinking/reasoning 的 wire-field 差异。
- Chat Completions adapter 仍保留 `_legacy_message_to_chat_tool_call()`，把旧消息形态桥到现代 tool call 结构。

### 7.3 当前成本

- translator 逻辑重复。
- retry / error classification 重复。
- thinking / tool-call / model-end 的边界在两个 adapter 里各写一遍。
- Chat Completions 里对空字符串残片的补全，和 Responses 里对 provider event 片段的拼接，都是协议适配成本，不是架构债；它们属于 wire protocol 的正常代价，只是要被显式标出来，不要误写成“系统为了兼容旧设计留下的临时补丁”。

这层并不是“多余”，但它确实是兼容债：系统在为多个 wire protocol 付出永续成本。

### 7.4 能不能 hard cut

**短期不能。**

除非 Pulsara 产品层明确只保留一种 provider wire API，否则这个 split 只能作为适配层存在。

能 hard cut 的不是“二选一立刻删掉”，而是：

- 把 shared event semantics 提到更高层。
- 把 adapter 专有逻辑尽量压到最小。
- 避免 provider 协议差异泄漏到 host / runtime 的业务层。

### 7.5 推荐 cut 顺序

1. 先继续统一 `AgentEvent` 语义。
2. 再抽掉重复的 retry / block-close 辅助逻辑。
3. 最后如果产品决定只保留一种 wire API，再删另一条 adapter。

### 7.6 补充说明

OpenAI 适配器里的“残片补全”不算债务；它只是 streaming 协议的成本。文档后续如果再提到这类逻辑，应该统一表述为“协议适配开销”或“wire protocol 代价”，避免把正常的 event reconstruction 误写成 legacy shim。

---

## 8. 额外债务：capability / skill discovery / activation

### 8.1 旧设计来源

技能系统最初只是“把 workspace 里的 SKILL.md 读出来给模型看”，后来又叠了几层：

- workspace skills 与 user skills 并存。
- `.agents/skills` 与 `.pulsara/skills` 并存。
- bundled skills 先打包进仓库，再同步到 `PULSARA_HOME`。
- model-visible catalog 与 host-selected active skill prompt 分离。
- `disable_model_invocation`、`user_invocable`、`allowed_scopes` / `blocked_scopes` 这些 frontmatter 字段被保留成兼容占位。

相关代码：

- [src/pulsara_agent/capability/local_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skills.py)
- [src/pulsara_agent/capability/resolver.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/resolver.py)
- [src/pulsara_agent/capability/render.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/render.py)
- [src/pulsara_agent/capability/bundled_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/bundled_skills.py)
- [src/pulsara_agent/cli.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py)
- [src/pulsara_agent/runtime/wiring.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/wiring.py)
- [src/pulsara_agent/host/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)
- [tests/test_capability_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_capability_skills.py)
- [tests/test_bundled_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_bundled_skills.py)

### 8.2 当前兼容层

现在技能能力至少拆成四条 surface：

- `LocalSkillProvider.discover()`：扫 workspace / user roots，解析 frontmatter，过滤未知工具。
- `LocalSkillResolver.resolve()`：把 discovered skills 分成 catalog entries 和 active injections。
- `sync_bundled_skills()` / `bundled_skills_status()` / `reset_bundled_skill()`：把仓库内 bundled skills 同步到用户 home，并用 provenance 识别 bundled 身份。
- CLI / host 层：`skills sync-bundled|status|reset`、`--skill`、`$skill-name`、`host inspect`，都在消费同一套技能对象，但暴露方式不同。

### 8.3 当前成本

- 同一个 skill 的“事实”分散在仓库源、用户 home copy、provenance file、catalog prompt、active prompt 里。
- bundled skills 需要同步与重置，意味着发布态和用户态之间有显式双份状态。
- `disable_model_invocation` 会把 skill 从 catalog 隐藏，但 host 还能显式激活。
- `user_invocable` 现在只是被解析，尚未形成真实行为。
- `allowed_scopes` / `blocked_scopes` 也只是兼容占位，V1 里只产生日志诊断，不真正参与权限裁决。

### 8.4 能不能 hard cut

**能收敛，但不能把“技能”这个能力本身删掉。**

真正可以 hard cut 的，是这些兼容债：

- 旧 frontmatter 字段只保留一部分真实语义，其余别再假装支持。
- `.agents/skills` 与 `.pulsara/skills` 不要长期平行演化成两套语义源。
- bundled skill 的“仓库 source + 用户 home copy”要么继续作为明确迁移层，要么尽快收成单一权威源。

### 8.5 推荐 cut 顺序

1. 先把 frontmatter 语义收紧：只保留真正生效的字段，其余字段要么实现，要么删除。
2. 再收敛 skill root 约定，尽量只保留一个 canonical root 语义，legacy root 只做迁移别名。
3. 然后把 bundled sync / reset 定义成唯一的分发与恢复路径。
4. 最后让 catalog / active prompt 共享同一份解析结果，避免“看见什么”和“能激活什么”继续分叉。

这块的 hard cut 目标不是“不要 skills”，而是**不要再让 skills 同时像配置文件、安装包、目录索引和 prompt 注入四种东西。**

## 9. 额外债务：legacy alias / downgrade shim

### 9.1 旧设计来源

这一类不是完整功能，而是为了兼容旧命名或旧执行路径留下来的软着陆：

- `workspace_kind=ephemeral` 仍被当作 `transient` 的别名。
- memory governance 在没有 `memory_write_uow_factory` 时，会把 `SupersedeAndSubmit` / `ContradictAndSubmit` 降级成 coexist 路径。

相关代码：

- [src/pulsara_agent/host/identity.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/identity.py)
- [src/pulsara_agent/cli.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py)
- [src/pulsara_agent/memory/governance/executor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/executor.py)
- [tests/test_host_identity.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_host_identity.py)
- [tests/test_cli_host.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_cli_host.py)

### 9.2 当前兼容层

- `normalize_workspace_kind()` 继续接受 `ephemeral`，CLI 也继续把它当成合法选项。
- `MemoryGovernanceExecutor.apply_decision()` 在无 UOW 时主动降级 supersede / contradict 语义，并打 `legacy_no_uow` sentinel。

### 9.3 当前成本

- `ephemeral` 是一个永远需要记住的旧名，文档、帮助文本、测试都得顺着它走。
- memory governance 的决策语义依赖 wiring 是否提供 UOW；同一个决策在不同 runtime 配置下会落到不同路径。
- 这些 shim 会让“看起来同一件事”的行为在两个层面分叉：一个是名字，一个是决策语义。

### 9.4 能不能 hard cut

**都能 cut，而且都应该尽量 cut。**

- `ephemeral` 只是命名别名，风险最低，最适合先硬切。
- `legacy_no_uow` 才是真正的语义分叉，应该在 UOW 路径全量可用后移除。

### 9.5 推荐 cut 顺序

1. 先把 `transient` 固化成唯一对外主名，`ephemeral` 只保留为短期解析别名。
2. 再把 memory governance 的 UOW 路径补成唯一 canonical 语义。
3. 最后删除 `legacy_no_uow` 降级，让 supersede / contradict 在所有 runtime 配置下都走同一种决策路径。

### 9.6 退出时间表

legacy alias / downgrade shim 这类东西不适合无限期挂着，建议按“兼容窗口”而不是“永久支持”来写：

1. 先在文档和 CLI 帮助里同时展示新旧名，明确旧名只是 alias。
2. 再在测试里把新名设成 primary path，把旧名仅保留为回归覆盖。
3. 最后在一个明确的 release window 里删掉旧别名和降级 sentinel。

这类 shim 的原则很简单：**可以先兼容，但别把兼容写成长期语义。**

## 10. 暂定 hard cut 优先级

如果从“最该先收敛”的角度排，建议按下面这个近似拓扑顺序：

1. **terminal 输出契约固化**（preview / completion note / kill reason / env）
2. **Tool Result Artifact 归档单点化**
3. **permission / approval 主路径收敛**（含 pending approval / suspended / stop）
4. **recovery 语义上收**（transcript note + runtime recovery prompt）
5. **memory surfaces 的回写边界继续固定**（working_context / recall / reflection / run timeline / recall fallback）
6. **capability / skill discovery / activation 收敛**
7. **legacy alias / downgrade shim 清除**
8. **Host / session ownership 继续清晰化**（含 workspace-scoped terminal supervisor）
9. **LLM provider adapter split 最后再看产品选择**

原因很直接：

- artifact 是最容易继续长出重复逻辑的地方。
- terminal 是最容易影响用户体验和系统边界的地方。
- permission 是最容易让系统看起来“都能配但实际难用”的地方。
- capability 和 legacy shim 是最容易“看上去只是兼容一下，最后却永远留着”的地方。
- memory、run timeline 和 recovery 是最容易把语义继续往上堆、却还没有真正收口的地方。
- ownership 和 workspace supervisor 更像高阶演化层，适合在前面几项收口后继续整理。
- provider split 是最底层的适配债，只有在产品层明确只保留一种 wire API 时才值得动刀。

### 10.1 依赖表

这不是“谁更重要”的排序，而是“谁先收口才能让谁不再继续分叉”的前置关系：

| cut 项 | 主要前置依赖 | 备注 |
| --- | --- | --- |
| terminal 输出契约固化 | 无强前置；它是 artifact / recovery 的上游约束 | preview / completion note / kill reason / env 先统一，后面的 artifact 和 recovery 才不会继续漂。 |
| Tool Result Artifact 归档单点化 | terminal 输出契约、run timeline 读侧语义 | 长文本只能归档一次，terminal 是最先会把这件事暴露出来的 surface。 |
| permission / approval 主路径收敛 | approval resume、user stop、terminal 只读豁免 | 先把 ask/on_request/inspect 跑顺，才谈删兼容分支。 |
| recovery 语义上收 | terminal completion、unfinished classifier、permission 状态机 | 恢复 note 不能先于 terminal / approval 稳定，否则会继续打架。 |
| memory surfaces 的回写边界继续固定 | run timeline、recovery state、artifact 读侧 | memory 不该先于业务视图收口。 |
| capability / skill discovery / activation 收敛 | recovery / terminal 文案稳定后更容易收 | skill 语义会受 prompt 和恢复提示影响，但不是强依赖。 |
| legacy alias / downgrade shim 清除 | canonical 命名、UOW 路径 | 旧名和降级都要等主路径稳定后删。 |
| Host / session ownership 继续清晰化 | workspace supervisor、pending approval / stop 状态机 | 这是高阶边界，适合在前面几项收口后继续整理。 |
| LLM provider adapter split 最后再看产品选择 | 无强前置，但应最后决策 | 这是 wire API 选择，不是本轮收口优先项。 |

如果把它画成 DAG，主干大致是：

- `terminal contract -> artifact`
- `approval state machine -> permission contract -> recovery`
- `recovery -> memory / read side`
- `capability / legacy shim` 主要跟着 prompt 和主路径一起收
- `provider adapter split` 独立留到最后做产品决策

就“现在能不能动手”而言：

- **可立刻继续收敛**：artifact、capability frontmatter 语义、legacy shim、recovery note 的实现细节、memory 的回写边界、run timeline 的摘要语义。
- **先封口再继续演化**：terminal 输出契约、permission 主路径。
- **暂不 hard cut**：Host / session ownership、workspace supervisor、LLM provider adapter split。

---

## 11. 这份文档的后续更新方式

这份文档会继续更新，不把自己写成一次性结论。

后续我会继续补：

- 每个债务对应的具体代码行级证据
- 哪些并行 surface 其实已经可以合并
- 哪些必须保留为显式契约
- 如果做 hard cut，具体要删哪些旧入口、改哪些测试

目前这一版只是审计骨架。下一步会把每个债务展开成更细的子条目，尤其是 artifact、terminal 和 recovery 三块。
