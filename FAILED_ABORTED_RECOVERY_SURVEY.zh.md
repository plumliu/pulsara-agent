# Failed / Aborted Recovery 开源实现调研

_Created: 2026-06-24_

本文记录对本地几个 agent 项目在 failed / aborted turn 后如何恢复上下文、处理未完成 tool call、以及是否合成 tool result 的轻量调研：

- Codex: `/Users/plumliu/Desktop/python_workspace/codex`
- Claude Code: `/Users/plumliu/Desktop/python_workspace/claude-code`
- Hermes: `/Users/plumliu/Desktop/python_workspace/hermes-agent`
- OpenClaw: `/Users/plumliu/Desktop/python_workspace/openclaw`
- Anybox: `/Users/plumliu/Desktop/python_workspace/anybox`

调研目的不是复制某个实现，而是回答 Pulsara 当前的小决策：

1. failed / aborted note 是否应该提到未完成的工具调用？
2. 是否应该为了 provider 配对规则而合成 tool result？
3. 如果工具调用已经产生真实结果，恢复时是否应该保留？

结论先行：Pulsara 应继续以 canonical event truth 为主，默认不向事件日志写入 fake tool result；但 failed / aborted note 应补充一个很短的未完成工具摘要。真实 tool result 必须保留，未完成 tool call 必须从 provider replay 中剥离。若未来某个 provider adapter 必须补齐 tool result 才能通过校验，应优先做 provider-only 的临时转换，而不是污染 canonical transcript。

## 0. Pulsara 当前基线

Pulsara 当前已经实现：

- `RunEndEvent(status="failed")` 后，下一轮 transcript 注入 failure note。
- `RunEndEvent(status="aborted")` 后，下一轮 transcript 注入 interrupted note。
- 对 failed / aborted run，`rebuild_prior_messages()` 会剥离没有 `ToolResultEndEvent` 的未完成 `ToolCallBlock`。
- 如果剥离后 assistant message 没有 text / data / tool result，则整个 assistant message 不 replay，只保留用户输入和 system note。

相关代码：

- `src/pulsara_agent/host/transcript.py`
- `tests/test_host_core.py`

当前 note 文案比较保守：

- failed note 只说上一轮 runtime/provider step failed，assistant text 可能 partial。
- aborted note 只说上一轮被用户停止，assistant text 或 tool work 可能 partial。

缺口：

1. note 没有告诉模型上一轮是否已经提出工具调用。
2. note 没有区分“工具已真实完成但 run 后续失败”和“工具只是被提出/等待审批/未产出结果”。
3. 当前策略已经保证 provider-safe，但模型面对“继续刚才的”时仍可能不知道上一轮卡在工具调用边界。

## 1. 策略分型

本轮调研看到的策略可以分成四类。

### 1.1 Marker / Note + Strip Orphan

做法：

- failed / aborted 是 turn 级状态。
- 下一轮注入模型可见 marker / note。
- provider replay 时剥离孤儿 tool call 或孤儿 tool result。
- 不伪造普通 tool result。

优点：

- canonical transcript 保持诚实。
- 不会让模型误以为某个工具真的执行过。
- 对 Chat Completions / Anthropic 这类严格配对 provider 也安全。

缺点：

- 模型只能从 note 理解上一轮工具边界；note 太粗会丢掉有用恢复线索。

Pulsara 目前基本在这一路线上。

### 1.2 Synthetic Tool Result for Provider Pairing

做法：

- 如果 assistant 已经产生 `tool_use`，但 turn 在 tool result 前中断，则合成一个 error tool result。
- 常见内容是 `Interrupted by user`、`aborted` 或 provider-specific missing result 文案。

优点：

- provider replay 序列完整。
- 保留 assistant tool call 轨迹，不需要删除 tool call。

缺点：

- 如果写入 canonical history，会污染事实层。
- 模型可能把 synthetic result 当作真实工具执行结果。
- 对审批/危险命令场景尤其敏感：一个“未执行”的 tool call 不应伪装成“执行后失败”。

### 1.3 Preserve Real Result, Repair Only Missing Edges

做法：

- 已经有真实 tool result 的 tool call 保留。
- 只有缺结果的 sibling 被修复或剥离。
- aborted assistant span 可以保留真实结果，但不应为 aborted span 默认合成结果。

这是 OpenClaw 最近修复和 Hermes 测试都很强调的方向。

### 1.4 UI / Session Flag Only

做法：

- runtime 只记录 session 被 cancel / aborted。
- 下一轮 prompt 可能加一条简单提示，或只在 UI 上显示状态。

优点：

- 实现简单。

缺点：

- 对 provider replay 和模型恢复帮助有限。
- 容易留下 active turn / running state 的竞态。

Anybox 当前更接近这一类。

## 2. Codex

Codex 的 interrupt 是 first-class runtime operation，而不是 UI 直接杀 task。

关键路径：

- `Op::Interrupt`
- `Session::interrupt_task()`
- `Session::abort_all_tasks(TurnAbortReason::Interrupted)`
- `handle_task_abort(...)`
- `TurnAbortedEvent`

本地代码位置：

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tasks/mod.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/context/turn_aborted.rs`

Codex 在 `handle_task_abort` 中：

1. cancel task 的 `CancellationToken`。
2. 等最多 100ms graceful completion。
3. abort Tokio handle。
4. 调 task 自己的 `abort()` hook。
5. 用户 interrupt 时写入 interrupted history marker。
6. flush rollout，确保 marker 在 `TurnAbortedEvent` 前持久可见。
7. 发送 `TurnAbortedEvent`。

Codex 的模型可见 interrupted guidance 大意是：

- 上一轮被用户有意中断。
- running unified exec process 可能还在后台。
- aborted tool / command 可能已经部分执行。

值得注意：

- Codex 的 marker 是 turn 级提示，不列出具体 tool args。
- 它强调“可能部分执行”，而不是断言成功或失败。
- 它把 interrupted marker 写入 history / rollout，再发 abort event，说明 marker 是 canonical lifecycle 的一部分。

对 Pulsara 的启发：

- failed / aborted note 应该是 lifecycle 的模型可见投影。
- note 可以提工具调用状态，但不要把未完成工具伪装成真实结果。
- 对 terminal / background process，要使用“可能部分执行 / may still be running”这种谨慎语义。

## 3. Claude Code

Claude Code 的实现与 Codex / Pulsara 当前策略不同：它更积极地补齐 provider 所需的 tool result pairing。

本地代码位置：

- `/Users/plumliu/Desktop/python_workspace/claude-code/src/query.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/Tool.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/types/textInputTypes.ts`

关键函数 `yieldMissingToolResultBlocks(assistantMessages, errorMessage)` 会遍历 assistant message 中的 `tool_use` block，并为每个 tool use 生成一个 `tool_result` user message：

```text
type: "tool_result"
content: errorMessage
is_error: true
tool_use_id: toolUse.id
```

在 streaming abort 时，它先处理 abort：

- 如果有 streaming tool executor，则 consume remaining results，让 executor 给 queued / in-progress tools 生成 synthetic tool_results。
- 否则对 assistant messages 调 `yieldMissingToolResultBlocks(..., "Interrupted by user")`。

Claude Code 还有另一条相关路径：streaming fallback 发生时，会 tombstone orphaned messages，丢弃旧 executor 的 pending result，避免旧 `tool_use_id` 的 tool results 泄漏到新 attempt。

这说明 Claude Code 的核心目标是：

1. provider replay 一定配对完整。
2. 对已经产生 `tool_use` 的 assistant message，不轻易 strip 掉，而是用 error result 补齐。
3. 发生 fallback / retry 时，旧 attempt 的孤儿 message 要显式 tombstone。

对 Pulsara 的启发：

- synthetic tool result 是一个有效工程选项，不是错误。
- 但它更适合 provider-facing replay repair，而不一定适合 Pulsara 的 canonical event log。
- Pulsara 当前有审批和 hardline 安全语义，未执行工具如果被写成普通 error result，容易误导模型和审计。

## 4. Hermes

Hermes 的测试对 tool call / tool result pairing 有很强约束。

本地代码位置：

- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tests/agent/test_anthropic_adapter.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tests/run_agent/test_compression_boundary.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tests/run_agent/test_provider_parity.py`

相关测试覆盖：

- orphaned `tool_use` 会被 strip。
- orphaned `tool_result` 会被 strip。
- 压缩边界不能把 assistant tool call 与 tool result 拆坏。
- 压缩不能用 stub result 顶替真实 result。
- interrupted parallel tool batch 中，只保留有真实 result 的 tool use；未完成 sibling 被剥离。
- 如果剥离 tool use 导致 signed thinking signature 失效，Hermes 会把 thinking demote 成普通 text，避免 Anthropic 拒绝 replay。

Hermes 给出的教训很清楚：

1. provider replay 必须严格满足 pairing。
2. 真实 tool result 不能被 summary / stub / repair 文案替代。
3. 未完成 tool use 可以删除，但删除后要处理 reasoning / thinking 签名等副作用。

对 Pulsara 的启发：

- 当前 strip unfinished `ToolCallBlock` 是合理的。
- 如果未来支持 provider signed reasoning replay，要注意：删除 tool call 可能让 reasoning signature 失效，需要 adapter 层降级或剥离。
- note 中提工具摘要，比保留孤儿 tool call 更安全。

## 5. OpenClaw

OpenClaw 的实现更复杂，也最能体现“成熟系统会逐步收敛”的痕迹。

本地代码位置：

- `/Users/plumliu/Desktop/python_workspace/openclaw/src/auto-reply/reply/body.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/session-transcript-repair.test.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/session-tool-result-guard.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/docs/reference/transcript-hygiene.md`
- `/Users/plumliu/Desktop/python_workspace/openclaw/CHANGELOG.md`

OpenClaw 有会话级 `abortedLastRun` flag。下一轮构造消息 body 时，如果该 flag 为 true，会加一次性提示：

```text
Note: The previous agent run was aborted by the user. Resume carefully or ask for clarification.
```

同时它有 transcript repair：

- 可为缺失 tool result 合成 `aborted` / missing result。
- 可保留 aborted assistant span 后面的真实 matching tool result。
- 可配置是否允许 synthetic tool results。
- 一些 transport 仍保留 synthetic repair。

但 changelog 也显示它在不断修正 synthetic repair 的副作用：

- “prefer real tool results over synthetic repair output”
- “recover interrupted CLI tool transcripts”
- “clear orphan tool state”
- “stop OpenAI/Codex transcript replay from synthesizing missing tool results while still preserving synthetic repair on Anthropic, Gemini, and Bedrock transport-owned sessions”
- timeout cleanup 时 clear pending tool-call state，而不是持久化 synthetic missing result，避免污染 follow-up turns。

这说明 OpenClaw 的最终经验不是“永远不要 synthetic”，而是：

1. 真实结果优先。
2. synthetic repair 必须 provider / transport scoped。
3. 不要让 cleanup timeout 产生持久 synthetic result。
4. aborted flag / note 是独立于 tool result repair 的恢复信号。

对 Pulsara 的启发：

- 可以把 note 和 provider repair 分层。
- canonical event log 不应为了 provider 兼容而写入 synthetic ordinary result。
- 如果做 synthetic，也要标记 `synthetic=true`，并限制在 adapter 输出或专门事件类型中。

## 6. Anybox

Anybox 的公开代码和文档显示其 cancel 语义较轻。

本地代码位置：

- `/Users/plumliu/Desktop/python_workspace/anybox/docs/multi-session-concurrency-comparison.md`

它的模型大致是：

- `runningSessions` 记录 `AbortController`。
- `activeTurns` 记录 turn runtime。
- cancel 时调用 `AbortController.abort()`，并删除 running record。
- active turn 往往要等 prompt / resume 的 finally 才清理。

文档自己指出一个问题：

- cancel 后 running state 可能已经删除，但 active turn 还没 finally。
- 如果用户极快发起新 prompt，可能绕过 running state，又在 active turn 处撞上旧 turn。

Anybox 文档建议明确 cancel 状态机：

- `running`
- `cancelling`
- `cancelled`
- `finished`

对 Pulsara 的启发：

- 当前 Pulsara 已经比 Anybox 更接近 canonical lifecycle。
- stop 后锁释放、pending 清理、aborted event、transcript note 都应该继续保持。
- failed / aborted recovery 不应只是 UI flag。

## 7. 对 Pulsara 的决策

### 7.1 Canonical Transcript 原则

Pulsara 应坚持：

1. 真实 `ToolResultEndEvent` 存在时，保留对应 tool call / tool result。
2. 没有真实 tool result 的 tool call，在 provider replay 中剥离。
3. 不向 canonical event log 写入普通 synthetic tool result。
4. failed / aborted note 是 derived runtime note，不是用户消息，也不是工具结果。

这与当前实现一致。

### 7.2 Note 应该补工具摘要，但不能暗示“没执行”

当前 note 太粗。note 由两部分拼成：

1. **基础框架**：failed / aborted 的静态前缀（对应当前 `FAILURE_NOTE_TEXT` / `INTERRUPTED_NOTE_TEXT`），说明上一轮失败/被停、用户输入已保留、assistant text 可能 partial。
2. **未完成工具摘要**：追加在基础框架之后，措辞由 §7.2.1 的 `state × severity` 矩阵决定，不在此处写死单例文案。

示例（aborted，基础框架 + 摘要）：

```text
<INTERRUPTED_NOTE_TEXT> The turn had 1 unfinished tool call: terminal. <按 §7.2.1 矩阵渲染该 tool 的 state × severity 措辞>
```

摘要规则：

- 默认只列工具名和数量。
- 不列原始 arguments。
- terminal command 默认不列；如果未来要列，只能用 sanitized + length capped preview。
- 多个工具名最多列前三个，剩余用 `+N more`。
- 对已经有真实 `ToolResultEndEvent` 的 tool call 不计入 unfinished。
- 每个 unfinished tool 的具体措辞（did not execute / may have partially run [and may still be running] / proposed but uncertain）由 §7.2.1 的 `state × severity` 矩阵统一决定，本节不再单独定义，避免两处漂移。

这里不能写成 `It produced no tool result; do not assume it completed` 后就收住。`no tool result` 只说明 transcript 中没有完成结果，不等价于“无副作用”。Codex guidance 特意使用 `may have partially executed` / `may still be running`，就是为了避免模型在“继续”时放心重做一个其实已经部分执行的动作。

### 7.2.1 两轴模型：state（事件） × severity（工具）

三态精化由两根正交的轴构成，不能混为一谈：

- **状态轴 state**：unfinished tool call 处于哪种生命周期阶段。完全由 canonical 事件判定，与工具类型无关。
- **严重度轴 severity**：该工具中断后的副作用画像。由 tool name 判定。

模型可见 note 的措辞 = `state × severity` 的交叉。本节直接落地三态精化版，不再保留 count-only 退化路径。

#### 轴一：状态判定（事件谓词）

已核实事件顺序（`runtime/agent.py`）：permission gate 在批量执行之前评估；`WAIT_FOR_USER` 时只发 `RequireUserConfirmEvent` 并挂起，不进入工具执行；只有 gate 放行的 call 才会进入 `ToolExecutor.execute()`。

但要精确：`ToolExecutor.execute()` 的第一件事就是发 `ToolResultStartEvent`，且这发生在 `registry.get()` 与真正执行工具**之前**（`tools/executor.py:29-36`）。因此 `ToolResultStartEvent` 只能证明“permission 放行、进入了 executor（execution attempt started）”，**不能证明工具已经产生副作用**。实现者不要把它当 side-effect proof；安全上据此使用 `may have partially run` 仍然成立（既然进入了 executor，就无法排除已产生副作用）。

按 run 聚合四个 tool_call_id 集合：

```text
proposed   = {id with ToolCallStartEvent}            # 模型提出（含残片，name 可能为空，见下）
completed  = {id with ToolResultEndEvent}            # 见下方“completed 的序列契约”
attempted  = {id with ToolResultStartEvent}          # 进入 executor（attempt started，非 side-effect proof）
pending    = {id in RequireUserConfirmEvent.tool_calls[].id}

unfinished = proposed - completed
```

对每个 unfinished id，按优先级判定（`attempted` 必须先于 `pending`，才能正确处理“已批准→执行中被 abort”）：

| 判据 | state | 语义 |
| --- | --- | --- |
| `id in attempted` | `started_no_completed_result` | may have partially run |
| `id in pending`（且不在 attempted） | `pending_approval_not_executed` | did not execute |
| 其余 | `ambiguous_failed_generation` | proposed / partially emitted |

这个优先级保证：approve 后进入 executor 再被停的 call 落到 `attempted`（state 2），而不是被误判成 pending（state 1）。

#### completed 的序列契约（late ToolResultEndEvent）

`completed` 集合的定义必须显式：**`completed` = 该 run 事件流里所有的 `ToolResultEndEvent`，不按 `RunEnd` / `ReplyEnd` 截断。**

原因是一个真实的 race（已核实）：

- `stop_current_turn()` cancel run task，`_stream_tool_batch_events()` 取消 pending asyncio task，但 `asyncio.to_thread` 里的工具线程**不能被强停**，仍可能在 `RunEnd(aborted)` 之后通过 thread recorder 写入 late `ToolResultEndEvent`。
- `event_log.replay(reply_id)`（`event_log/in_memory.py:52`）会 reduce 该 reply 的**全部**事件，不按 `ReplyEnd` / `RunEnd` 截断。

因此采用 **all-events 语义**（completed 跨越 RunEnd），而不是 "RunEnd 前的 ToolResultEnd"：

- 若工具线程在 abort 后才真正完成，它的真实结果会进入 `completed`、被 strip 逻辑保留、并被 replay 如实展示。把它再标成 “unfinished / may have partially run” 反而与事件流矛盾。
- late result 自动把该 tool_call_id 移出 `unfinished` 摘要（自愈）。代价：如果 rebuild 发生在 late result 落 log 之前，该 turn 的 note 会暂时把它列为 unfinished；待 result 落 log 后的下一次 rebuild 自然消失。这是 soft-cancel 下可接受的诚实行为，note 措辞本就是 “may have partially run / may still be running”，与“稍后才出现真实结果”不冲突。

必须补测试：foreground terminal `ToolResultStartEvent` → stop → `RunEnd(aborted)` → late `ToolResultEndEvent`，验证：(a) 该 reply replay 包含真实 result；(b) 下一轮 note 不再把它列入 unfinished（completed 跨 RunEnd 生效）。

#### 轴二：severity 分桶（按工具）

不要枚举具体工具，按副作用画像分桶，复用 `runtime/permission.py` 已有常量，让未来新工具自动归桶。判定时只能从事件数据（`tool_call_name` + arguments）分类，读不到 live tool 的 `is_read_only`，因此按名字映射：

| 桶 | 成员 | 中断后的恢复语义 |
| --- | --- | --- |
| `read_only` | `read_file` / `search_files` / `artifact_read` | 无副作用，unfinished 基本是 no-op |
| `bounded_write` | `FILE_WRITE_TOOL_NAMES`（`write_file` / `edit_file`） | 副作用限于已知路径，恢复 = 重读该文件 verify |
| `terminal`（最重） | `TERMINAL_TOOL_NAMES`（含整个 `terminal_process`，V1 不按 action 细分，见下） | 任意不可逆副作用 + 后台进程在 soft-stop 后可能仍在运行 |

terminal 是最重的工具，原因有两个，不是一个：

1. 任意副作用：任何 shell 命令都可能产生不可逆的外部效果。
2. 后台进程在 soft cancel 下存活：`abort_run` 是 soft cancel，Python 无法抢占已在 `asyncio.to_thread` 运行的工具线程，yielded 进程也明确不杀。因此一个 `attempted` 的 terminal 不只是“跑了一半就停了”，而是“可能此刻还在跑”。

→ terminal 的 state 2 措辞要用过去 + 现在进行（may have partially run **and may still be running**）；bounded_write 没有第 2 条，过去式即可。

V1 简化：整个 `terminal_process`（含 `list/log/poll/wait` 只读 action）一律归 `terminal` 桶，note severity **不**按 action 细分。注意这与 permission 层的只读豁免（`runtime/permission.py:216`，`list/log/poll/wait` 不触发 approval）是两个维度：approval 豁免是“要不要问用户”，note severity 是“中断后如何描述副作用风险”。只读 action 本来就快、极少成为 unfinished 停点，保守归 terminal 的过度警告无害；若未来要细分，再 parse `action` 并把只读 action 移入 `read_only` 桶。

#### yielded terminal 不是 unfinished（与 completion note 不相交）

必须想清楚这点，否则 note 与 §7.2.2 的 completion note 会重复或矛盾：

- **yielded terminal**（跑过 yield 窗口、返回 `running` + `process_id`）：初始 tool call 本身有 `ToolResultEndEvent`（yield 即返回结果），所以 `in completed`，不算 unfinished，不进摘要。其后续完成由 `TerminalProcessCompletedEvent` → completion note 单独覆盖。
- **foreground terminal**（没到 yield 就被 abort / fail）：无 process_id、无 completion note 兜底。如果最终仍没有 late `ToolResultEndEvent`，这才是进 unfinished 摘要的 terminal，也正是“可能还在跑且无人追踪”的高危项。

因此 note 摘要里出现的 terminal 项与 completion note 覆盖的 terminal call 集合**不相交**，两条 note 不会互相打脸。

#### 措辞矩阵（state × severity）

只有下列格子需要进 note 并使用强措辞，其余省略或一句带过：

| | read_only | bounded_write | terminal |
| --- | --- | --- | --- |
| **pending**（state 1） | 省略 | pending approval, did not execute | pending approval, did not execute（未起步，无背景进程） |
| **started**（state 2） | 一句带过 | may have partially run; re-read to verify | **may have partially run and may still be running in the background; verify before continuing** |
| **ambiguous**（state 3） | 省略 | proposed but uncertain; re-evaluate | proposed; uncertain whether it ran; verify |

`unknown_effect` 桶（工具名无法从 fallback 链解析出）不进入上表的 state × severity 细分：无论 state，一律用单一最保守措辞 `the previous turn proposed a tool call whose effect is unknown; verify before continuing`。原因是 name 缺失通常意味着 mid-stream failure 残片，既无法可靠判 severity，也无法可靠判 state。

摘要仍遵循 §7.2 既有规则：只列工具名 + 数量、不列 arguments、最多前三个 + `+N more`、已有真实 `ToolResultEndEvent` 的不计入。

#### transcript.py 落点

当前 `rebuild_prior_messages()` 只有 `_completed_tool_call_ids_by_run()`（只扫 `ToolResultEndEvent`），note 文案是静态常量。三态精化需要：

- 新增按 run 聚合：
  - `_attempted_tool_call_ids_by_run()` ← `ToolResultStartEvent`
  - `_pending_tool_call_ids_by_run()` ← `RequireUserConfirmEvent.tool_calls[].id`
  - proposed 集合 ← `ToolCallStartEvent`。
- 工具名解析不能只信 `ToolCallStartEvent.tool_call_name`：OpenAI event builder 在收到 delta 但还没 start 时会用空字符串补 start（`llm/adapters/openai/events.py:133`），所以 failed mid-stream 时可能出现 `tool_call_name == ""`。必须用 fallback 链：
  1. `ToolCallStartEvent.tool_call_name`（非空）
  2. `RequireUserConfirmEvent.tool_calls[].name`
  3. `ToolResultStartEvent.tool_call_name`
  4. replay 出的 `ToolCallBlock.name`
  5. 仍为空 → 归 `unknown_effect` 桶，用最保守措辞（`the previous turn proposed a tool call whose effect is unknown; verify before continuing`），**不要**假装能分桶或猜成 read_only。
- 新增纯函数：`_classify_unfinished(run_id, events)` → `list[(tool_name, state, severity)]`，便于单测。其中 severity ∈ {`read_only`, `bounded_write`, `terminal`, `unknown_effect`}。
- 把 `_note_message()` 从静态常量改为参数化：按 `note_target.run_id` 算出 unfinished 集合 → 分类 → 渲染摘要，追加到 failed / aborted 基础 note 文本之后。这是真正的工作量所在（note 参数化 + 矩阵渲染），不是“列个名字”。

注意：proposed 用 `ToolCallStartEvent` 而非依赖 `ReplyEndEvent` 重建，因为 failed run 可能在 reply 中途断裂、没有 `ReplyEndEvent`，但 `ToolCallStartEvent` 已经落 log。这与现有 strip 逻辑（按 `ToolResultEndEvent` 保留/剥离 `ToolCallBlock`）保持一致、互补。


### 7.2.2 与 Terminal Completion Note 的协调

Pulsara 已有 terminal completion note：yielded background process 在后续完成时，会通过 `TerminalProcessCompletedEvent` 投影进下一轮上下文。

这会和 aborted note 同时出现，因此 aborted note 不能写成“工具没完成”。正确关系是：

- 如果 terminal tool call 已经 yield 并返回了 `process_id`，初始 tool call 本身已有 `ToolResultEndEvent`，不应计入 unfinished 摘要。
- 之后 process 完成时，由 terminal completion note 单独说明 status / exit code。
- aborted note 只描述 turn 被停止，不应否认后续 completion note。
- 对没有 yielded、也没有 completed result 的 foreground terminal，note 只能说可能部分执行，需要 verify。

因此 note 文案应该显式允许 background terminal work：

```text
Background terminal tasks from that turn may still complete separately; if they do, Pulsara may add a separate terminal completion note.
```

这与 `HOST_USER_STOP_SURVEY.zh.md` 的原则一致：soft cancellation 的主要风险，是把它说得比实际更强。

### 7.2.3 Failed 与 Aborted 不应混成一个恢复模型

`aborted` 和 `failed` 都会留下 partial transcript，但副作用画像不同：

- `aborted`：用户有意停止。模型继续时应先尊重用户的停止意图，必要时询问或验证当前状态。
- `failed`：provider / runtime / hook 失败。工具可能根本没被 attempt，也可能已经执行了一部分，取决于失败点。

所以：

- aborted note 的重点是用户意图和 partial side effects。
- failed note 的重点是 runtime/provider failure boundary 和 ambiguity。
- 两者都可以列 unfinished tools，但 failed note 的措辞必须更弱，不要让模型以为每个列出的工具都已经被正常提出和准备执行。

### 7.3 不合成 Provider-Visible Tool Result 作为默认行为

不建议在 Pulsara V1 里把未完成 tool call 合成成普通 tool result 回传给 provider，原因：

1. Pulsara 有 approval / hardline / terminal security 语义，未执行和执行失败必须可区分。
2. 合成普通 result 会让模型误以为工具确实进入执行阶段。
3. canonical event log 已经足够表达事实：tool call was proposed or partially emitted, no completed result was produced, run ended failed / aborted。
4. 当前 strip + note 已经通过 Chat Completions 严格配对要求。

### 7.4 Provider-Only Synthetic Repair 可作为后续逃生口

如果未来某个 provider 或 adapter 无法接受 strip 策略，可以增加 provider-only repair：

- 只在 adapter payload 构造阶段生成。
- 不写入 event log。
- 不进入 durable memory。
- 不进入 run timeline 的普通 tool result。
- 文案必须明确：`Tool call did not complete because the previous turn was aborted/failed. No tool output is available.`
- metadata 标记 `synthetic=true`、`reason=aborted|failed`。

这吸收 Claude Code / OpenClaw 的兼容经验，但不牺牲 Pulsara 的 canonical truth。

### 7.5 Memory / Ledger 边界

failed / aborted recovery 还需要明确 memory 边界：

- Durable memory reflection 不应把 aborted turn 的半截 assistant text、半截 tool work 抽成长期事实。Pulsara 当前 `DurableMemoryHooks._maybe_reflect()` 已对 `LoopStatus.ABORTED` 直接返回（`memory/hooks/durable.py:257`）。
- 但 `FAILED` 当前没有同等的 gate：`_maybe_reflect()` 只拦 `ABORTED`，failed run 会继续走 `_trigger_reasons()`。这不是“所有 failed run 都可达 reflection”，而是一条 **narrow but real** 的污染路径：`_trigger_reasons()` 只在 `safe_point == "on_session_end"`、该 run 有 cached cheap memory hints、且没有主 agent memory attempt 时才追加 `cheap_memory_hint`（`memory/hooks/durable.py:281`）。所以风险窄，但确实存在，且与“failed turn 的 assistant text may be partial or empty”的事实相抵触。
- 建议：给 `FAILED` 补一道对称 gate（`if state.status in {LoopStatus.ABORTED, LoopStatus.FAILED}: return []`）。若选择不 gate，必须显式论证为什么这条窄路径安全（例如 failed run 的 cheap hints 不会包含可信 durable fact），不能停留在“即使未来允许”这种暗示当前已被拦截的措辞。
- Execution evidence ledger 可以记录真实 `ToolResultBlock` 的 runtime evidence，因为它是“工具结果事实”，不是“用户长期偏好 / 语义记忆”。
- 没有真实 `ToolResultEndEvent` 的 unfinished tool call 不应进入 execution evidence，也不应被 synthetic result 间接写入 ledger。
- 无论是否给 failed 补 gate，都必须避免把 provider/runtime failure note 或 unfinished tool summary 误抽成 durable fact。

这层边界比“note 里列几个工具名”更重要：恢复信号应该帮助下一轮模型工作，但不能污染长期记忆。

### 7.6 测试建议

这不是一个 count-only 小 PR：三态精化要新扫 `ToolResultStartEvent` / `RequireUserConfirmEvent` 并把 note 参数化，属中等改动。建议测试分两组。

state 轴（事件判定）：

1. aborted run 中有 pending terminal approval（有 `RequireUserConfirmEvent`、无 `ToolResultStartEvent`）→ note 提到 `1 unfinished tool call: terminal`，措辞 did not execute。
2. started/no completed result（有 `ToolResultStartEvent`、无 `ToolResultEndEvent`）→ note 使用 may have partially run，不说 did not execute。
3. approve 后执行中被 abort（既在 pending 又在 started）→ 判为 state 2 started，不被误判成 pending。
4. failed run 中两个 proposed-only tool call（仅 `ToolCallStartEvent`）→ 列两个工具名，使用 ambiguous failed wording。
5. failed run 在 reply 中途断裂、无 `ReplyEndEvent` → proposed 仍能从 `ToolCallStartEvent` 识别并进摘要。

severity 轴（按工具措辞）：

6. terminal 的 state 2 → 措辞包含 “and may still be running in the background”。
7. bounded_write（`write_file`/`edit_file`）的 state 2 → 过去式 may have partially run，不包含 still running。
8. read_only（`read_file`/`search_files`/`artifact_read`）的 unfinished → 省略或一句带过，不进强措辞。
9. 工具名无法从 fallback 链解析（`ToolCallStartEvent.tool_call_name == ""` 且无其它来源）→ 归 `unknown_effect`，使用单一保守措辞，不猜成 read_only。
10. 工具数 > 3 → 摘要最多列前三个 + `+N more`。

不变量与边界（沿用既有）：

11. yielded terminal 已有真实 `ToolResultEndEvent` → 不进入 unfinished 摘要；后续 completion note 可单独出现（两集合不相交）。
12. **late ToolResultEnd 序列契约**：foreground terminal `ToolResultStartEvent` → stop → `RunEnd(aborted)` → late `ToolResultEndEvent`。验证 (a) 该 reply replay 包含真实 result；(b) 下一轮 note 不再把它列入 unfinished（completed 跨 RunEnd 生效，all-events 语义）。
13. 已完成 tool call 不进入 unfinished 摘要。
14. note 不包含 tool arguments。
15. provider replay 中仍没有孤儿 `ToolCallBlock`。
16. 如果 assistant message 只有未完成 tool call，仍然被整体省略，只留下 user + note。
17. unfinished tool summary 不触发 durable memory reflection 或 synthetic execution evidence。

## 8. 最终建议

Pulsara 不应该简单选“Codex marker”或“Claude Code synthetic result”其中之一。更合适的分层是：

- canonical history: 保真，strip unfinished orphan，保留真实 result。
- model recovery: 注入 concise failed / aborted note，并加入未完成工具摘要。
- provider compatibility: 必要时做 adapter-only synthetic repair，不持久化。
- UI / inspect: 可以展示更详细 tool call id、状态、approval id、command preview，但这些不默认进入模型上下文。

这样能同时满足三件事：

1. provider 不 400。
2. 模型知道上一轮卡在工具边界。
3. 事件日志和审计不会被 fake tool result 污染。
