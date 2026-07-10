# Pulsara tool result context budget 问题调研

## 摘要

这份文档记录一次真实 REPL 轨迹暴露出的上下文体验问题：

> Pulsara 明明还没有接近 200k auto-compaction 阈值，模型却说「当前会话的上下文预算已经耗尽，所有工具输出都被截断了」，并且看不到刚刚运行命令得到的短输出。

结论是：这不是主模型 256k context window 真的耗尽，也不是 context compaction 没有触发，而是 Pulsara 当前存在一个更小、更局部的 **tool result model-visible render budget**。这个预算默认只有 `36_000 chars`，并且目前按整个 replay transcript 的时间顺序共享。旧历史里的大工具输出会先消耗预算，导致当前 run 中最新、最重要的工具结果被省略。

换句话说：

- 200k 是 auto compact 的模型上下文阈值；
- 36k 是当前工具结果 render 层的粗粒度可见预算；后续实现必须同时统计正文 preview 与最小 envelope；
- 本问题触发的是后者，不是前者；
- 目前旧工具结果可以“饿死”新工具结果。

这是一个 context engineering 层面的体验缺陷：agent 刚刚运行出来的结果应该优先进入模型视野，不能被很久以前的工具输出挤掉。

## 问题是如何发现的

真实 REPL 测试中，用户让 Pulsara：

1. 通过 LangChain docs MCP 查询最小 LangChain 示例；
2. 在 workspace 中写入 `main.py`；
3. 使用 `uv add` 安装依赖；
4. 使用 `uv run python main.py` 运行示例；
5. 询问运行结果。

模型最后回复：

> 抱歉，当前会话的上下文预算已经耗尽，所有工具输出都被截断了，我无法看到实际的运行结果。

但用户手动运行同一命令时，命令实际成功输出了短文本：

```bash
cd /Users/plumliu/Desktop/little_snake
uv run python main.py
```

输出大约只有几百字符。随后用户手动执行 `:compact` 后，再让 Pulsara 运行一次，Pulsara 又能正确看到并报告结果。

这说明问题不在脚本、不在 DeepSeek/LangChain，也不在终端执行本身；问题发生在 **Pulsara 把历史消息与工具结果重新编译给模型时**。

## 当前代码语义

当前有两套容易混淆的预算。

### 1. Context compaction 触发预算

落点：

- `src/pulsara_agent/runtime/compaction/service.py`
- `ContextCompactionPolicy.context_window_tokens = 256_000`
- `ContextCompactionPolicy.auto_threshold_tokens = 200_000`
- `ContextCompactionPolicy.estimate_safety_margin = 1.25`

这套预算决定什么时候自动 compact。它试图估算“主模型下一次实际可见上下文”是否接近窗口。

这套预算不是本次真实问题的直接触发点。

### 2. Tool result render budget

落点：

- `src/pulsara_agent/runtime/state.py`
- `LoopBudget.tool_result_context_chars = 36_000`
- `src/pulsara_agent/runtime/context.py`

当前 `_segmented_llm_messages_for_anchor(...)` 中只创建一个共享的 `_ToolResultRenderBudget`：

```python
tool_budget = _ToolResultRenderBudget(budget.tool_result_context_chars)
for index, message in enumerate(messages):
    converted = _message_to_llm_messages(message, tool_budget)
```

这意味着：

- prior history；
- current user；
- current run tail；
- 旧 tool result；
- 新 tool result；

都会消耗同一个 `36_000 chars` 工具结果预算。

一旦旧工具结果先把预算吃完，后续工具结果就会被替换成：

```text
[TOOL RESULT OMITTED: context budget exhausted]
```

或者 artifact envelope 场景下：

```text
[TOOL RESULT OMITTED: aggregate context budget exhausted]
```

这个省略发生在模型调用前。模型看到的是省略占位符，于是自然会推断“上下文预算耗尽，看不到输出”。这句话不是 runtime 的精确诊断，而是模型基于占位符做出的解释。

## 最小复现

下面脚本构造三个消息：

1. 一个旧的 36k terminal tool result；
2. 当前用户消息；
3. 一个很短的新 terminal tool result。

预期上，新 tool result 很短，应该对模型可见。但当前实现会让旧结果先消耗全部工具结果预算，导致新结果被省略。

```bash
uv run python - <<'PY'
from pulsara_agent.message import Msg, UserMsg, TextBlock, ToolResultBlock, ToolResultState
from pulsara_agent.runtime.context import build_compiled_context
from pulsara_agent.runtime.state import LoopState, LoopBudget

old = Msg(
    role="tool_result",
    name="terminal",
    content=[
        ToolResultBlock(
            id="old-call",
            name="terminal",
            state=ToolResultState.SUCCESS,
            output=[TextBlock(text="OLD-" + "x" * 36000)],
        )
    ],
)
user = UserMsg("user", "please run the script", id="u-current")
fresh = Msg(
    role="tool_result",
    name="terminal",
    content=[
        ToolResultBlock(
            id="fresh-call",
            name="terminal",
            state=ToolResultState.SUCCESS,
            output=[TextBlock(text="FRESH_RESULT: 206 chars visible")],
        )
    ],
)

state = LoopState(session_id="s")
state.messages = [old, user, fresh]

compiled = build_compiled_context(
    state=state,
    tools=(),
    system_prompt="sys",
    budget=LoopBudget(tool_result_context_chars=36000),
    current_user_anchor="u-current",
)

for i, m in enumerate(compiled.llm_context.messages):
    text = "\\n".join(m.content)
    print("MSG", i, "ROLE", m.role, "CALL", m.tool_call_id, "LEN", len(text))
    print(text[:180].replace("\\n", "\\\\n"))
    print("contains fresh?", "FRESH_RESULT" in text, "omitted?", "TOOL RESULT OMITTED" in text)
PY
```

实际输出：

```text
MSG 0 ROLE tool_result CALL old-call LEN 36031
[tool_result:terminal:success]\nOLD-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx...
contains fresh? False omitted? False

MSG 1 ROLE user CALL None LEN 21
please run the script
contains fresh? False omitted? False

MSG 2 ROLE tool_result CALL fresh-call LEN 78
[tool_result:terminal:success]\n[TOOL RESULT OMITTED: context budget exhausted]
contains fresh? False omitted? True
```

这个复现说明：问题不需要真实 MCP、不需要真实 LLM、不需要 uv 安装日志，只要旧工具结果足够大，就能把当前 run 的新工具结果挤掉。

## 为什么真实 LangChain 轨迹更容易触发

真实轨迹中包含多类大输出：

- MCP docs 查询结果；
- LangChain 文档内容；
- `uv add` / `uv run` 首次安装依赖日志；
- 文件读写结果；
- terminal JSON preview / artifact preview；
- 可能还有上下文编译与 capability 相关信息。

即使最终 `main.py` 输出只有几百字符，前面的工具结果也可能已经消耗 `36_000 chars` 工具结果预算。

因此模型不是没有运行命令，而是看不到最新命令的可见正文。

## Codex 是怎么做的

本地调研路径：

- `/Users/plumliu/Desktop/python_workspace/codex`

关键落点：

- `codex-rs/core/src/unified_exec/mod.rs`
- `codex-rs/core/src/tools/context.rs`
- `codex-rs/core/src/context_manager/history.rs`
- `codex-rs/utils/output-truncation/src/lib.rs`

Codex 的 terminal / exec 输出有 per-call 输出上限：

```rust
pub(crate) const DEFAULT_MAX_OUTPUT_TOKENS: usize = 10_000;
```

核心差异是：

1. Codex 对每次 exec/function output 独立截断；
2. 截断策略绑定到该次调用的 output；
3. 旧调用不会消耗一个跨 transcript 共享的“工具结果正文总预算”；
4. 因此一个新的短输出不会因为旧历史里有大输出而变成不可见；
5. Codex 还会跟踪模型真实 usage / context window，用于整体上下文管理。

这不是说 Codex 没有截断；Codex 当然会截断大输出。但它不会让“很久以前的大输出”在重放时抢走“刚刚运行出来的小输出”的可见额度。

### Codex 对 Pulsara 的启发

Pulsara 不一定要照搬 Codex 的 per-call tokens 截断，但必须吸收这个边界：

> 最新工具结果的可见性不能依赖旧工具结果是否已经消耗完全局预算。

如果保留 aggregate budget，也应该按 segment / turn / message 分层，而不是整个 replay history 一个池子。

## Claude Code 是怎么做的

本地调研路径：

- `/Users/plumliu/Desktop/python_workspace/claude-code`

关键落点：

- `src/constants/toolLimits.ts`
- `src/utils/toolResultStorage.ts`
- `src/query.ts`
- `src/services/compact/autoCompact.ts`
- `src/utils/tokens.ts`

Claude Code 的核心常量：

```ts
export const DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000
export const MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000
export const BYTES_PER_TOKEN = 4
```

它的 `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS` 注释明确说明：这是 **single user message** 内的 aggregate tool result budget。也就是说，预算按 API message / turn 分组，而不是跨整个历史共享。

调研到的关键流程：

1. `query.ts` 在发起模型请求前调用 `applyToolResultBudget(...)`；
2. `applyToolResultBudget(...)` 进入 `toolResultStorage.ts`；
3. 该逻辑按 message 收集 tool result candidates；
4. 如果某个 message 内 tool result 总量超预算，会把大结果持久化/替换成 preview；
5. 替换记录按 `toolUseId` 持久化，resume 后能复现同样的替换决策，避免 prompt cache 抖动；
6. 不同 message 独立评估，旧 message 的替换不会消耗新 message 的预算。

Claude Code 的设计语义非常接近这句话：

> 一个 turn 里并行工具太多，需要限制 aggregate output；但每个新 turn 的新工具结果应该有自己的可见机会。

它还会提示模型：旧 tool result 可能会被清理，因此重要信息要写入文件或保留到总结中。这是“工具结果不是无限上下文”的用户/模型协议层补充。

### Claude Code 对 Pulsara 的启发

Claude Code 最值得借鉴的不是具体数值，而是三个边界：

1. **per-message / per-turn aggregate budget**：防止 N 个工具并行输出叠爆上下文；
2. **toolUseId-stable replacement**：替换决策可恢复、可复现；
3. **large result persistence + preview**：大输出不消失，而是转为可读 artifact/文件引用。

Pulsara 已经有 artifact / event / inspect 基础，更适合做成更结构化的版本。

## Pulsara 当前强项与短板

### 强项

Pulsara 已经有一些 Codex / Claude Code 风格系统未必同时具备的基础：

- typed runtime event log；
- artifact store；
- adaptive preview；
- context compiler；
- compaction event；
- inspect；
- resume；
- capability gate decision；
- tool result artifact ref；
- terminal long-process 管理。

所以 Pulsara 不需要退回“纯字符串 transcript + 文件替换”的模式。它可以把 tool result budget 作为 context compiler 的一等事实来处理。

### 短板

当前短板集中在 tool result lowering 发生得太早、太粗：

1. `msg_to_llm_messages` / `_segmented_llm_messages_for_anchor` 在 context compiler 前就把工具结果正文裁掉；
2. prior history 与 current run tail 共享同一个 budget；
3. current run 最新 tool result 没有 reserved budget；
4. omit placeholder 文案让模型以为“整个上下文耗尽”，而非“某个工具结果正文预算耗尽”；
5. context compiler 的 `current_user/current_run_tail` 分段存在，但可见正文已经可能在 lower 前被旧输出消耗掉。

### 不是 terminal / adaptive preview 层的失败

这次问题不要误修到 terminal 单次工具输出层。

`terminal` / `terminal_process` 已经在工具执行层做了单次结果预览、归档候选和 adaptive preview：

- `src/pulsara_agent/tools/builtins/terminal.py` 负责 terminal 输出描述、streaming preview 和 `terminal_artifact_candidates(...)`；
- `src/pulsara_agent/tools/builtins/terminal_process.py` 负责 background process 的 `log` / `poll` / `wait` 等动作；
- `src/pulsara_agent/runtime/tool_artifacts.py` 负责把大结果归档成 artifact ref。

这些机制解决的是“单次工具输出太大时，如何给模型一个预览并把完整正文放进 artifact”。本问题发生得更晚：历史消息 replay / context lowering 时，已经存在的 tool result 再次进入模型上下文，旧结果消耗了共享预算，导致新结果被省略。

因此，单纯调整 `max_output_chars`、terminal streaming cap、artifact archive threshold 或 adaptive preview 阈值，都不能根治 starvation。真正需要修的是 **replay/context compiler 的 tool result budgeting 作用域与优先级**。

## 问题本质

本质不是“预算太小”。

单纯把 `tool_result_context_chars` 从 36k 调到更大，只会延后问题，并且让大型网页抓取 / 安装日志 / 测试日志更容易污染主上下文。

本质是预算分配策略错了：

> 当前实现把工具结果可见预算当成跨历史公平池；但 agent workflow 需要新鲜度优先、结构优先、可恢复引用优先。

正确的策略应该至少保证：

- current user input 必须可见；
- current run tail 的结构必须可见；
- current run 最新 tool result 应该拥有保留预算；
- prior history 的旧工具输出可以更激进地降级为 artifact ref / summary；
- 大输出应该可恢复，但不应挤掉新输出；
- 预算报告应能解释哪些 tool result 被降级、为什么降级、如何读取完整内容。

## 后续修复方向

这份文档不直接实现修复，但建议下一步按以下方向设计。

### 1. 拆分 tool result render budget

不要让 prior history 和 current run tail 使用同一个 `_ToolResultRenderBudget`。

可选分配：

- prior history：较小预算，偏向 artifact refs / compact preview；
- current run tail：独立预算，保证刚发生的工具结果可见；
- per-tool cap：防止单个新工具结果占满整个 current run tail；
- per-message / per-turn cap：防止并行工具结果合计爆炸；
- latest tool result reserved minimum：保证当前 run 最后一个短工具结果不会被旧历史饿死。

### 1.1 冻结预算配置模型

后续实现不应继续散落同类常量。至少需要冻结一组语义清晰的配置字段，并说明旧字段迁移关系。

现有字段：

- `LoopBudget.tool_result_context_chars`：目前是跨 replay 的工具结果正文共享预算；
- `ToolResultArtifactOptions.tool_result_message_context_chars`：artifact service / tool result message 侧的上下文预算字段。

建议新语义：

| 字段 | 语义 |
| --- | --- |
| `tool_result_total_context_chars` | 单次 context compile 允许 tool result rendered payload 进入模型的总预算，包含 body/preview 与 metadata envelope。 |
| `tool_result_body_context_chars` | 单次 context compile 允许 tool result body/preview 进入模型的总预算，不含最小 envelope。 |
| `tool_result_envelope_context_chars` | 单次 context compile 允许 metadata-only / artifact-backed envelope 进入模型的总预算。 |
| `tool_result_per_envelope_cap_chars` | 单个 metadata envelope 的最大 rendered 长度；超过时必须降级为更小的 essential envelope。 |
| `prior_tool_result_context_chars` | prior history tool result rendered payload 预算；旧结果优先降级为 artifact-backed envelope。 |
| `current_tail_tool_result_context_chars` | current run tail tool result rendered payload 预算；不与 prior history 共享。 |
| `tool_result_per_tool_cap_chars` | 单个 tool result 的 body/preview 可见正文上限，防止一个大结果吞掉整个 segment；不约束 essential envelope。 |
| `tool_result_per_message_cap_chars` | 同一个 tool batch 分组内的 aggregate body/preview 上限，借鉴 Claude Code 的 per-message budget。 |
| `latest_tool_result_reserved_chars` | 为当前 run 最新 tool result 预留的最低可见预算；短输出应完整可见。 |
| `legacy_tool_result_context_chars` | current user anchor 缺失、只能进入 `legacy_history` 时的保守 tool result rendered payload 预算；V1 也可固定映射为 `prior_tool_result_context_chars`，但必须写进配置/diagnostic。 |
| `expected_max_latest_batch_results` | policy 校验用的默认 latest batch 规模假设；可以是内部常量，也可以暴露为高级配置，但必须参与默认值 invariant。 |
| `max_tool_results_per_context` | policy 校验用的单次 compiled context 中允许进入 rendered tool-result allocator 的最大 tool result units。不能简单等同于单 run tool-call 上限；历史 replay 可能远大于单 run。默认值应由 compaction policy / 保留窗口推导，runtime loop 上限只能作为下界或 current-tail 上限。 |
| `minimum_essential_envelope_chars` | 最小 parseable essential envelope 的配置校验下限；建议作为内部常量，由 renderer 的最小字段集合派生。 |

迁移建议：

- `LoopBudget.tool_result_context_chars` 在兼容期可映射为 `tool_result_total_context_chars`；
- 若未显式配置 segment 预算，按 total 派生 prior/current tail 预算；
- 新代码和文档应避免继续把 `tool_result_context_chars` 理解为“整个模型上下文预算”。

预算必须分清 body 与 envelope：

- body / preview 是工具结果的正文候选，比如 terminal output、JSON preview、read_file 内容；
- envelope 是状态、exit code、cwd、process id、artifact/read_more 等最小操作信息；
- 省略正文不等于零成本，metadata-only envelope 仍会进入模型上下文；
- compiler 的 section estimate 与最终 payload estimate 必须统计最终 rendered message，包括 body 与 envelope；
- 如果大量旧 tool result 都降级为 envelope，仍必须受 `tool_result_envelope_context_chars` 和 `tool_result_per_envelope_cap_chars` 约束。

预算约束关系必须是 hard caps，而不是任选其一：

- 最终所有 tool result rendered payload 之和必须 `<= tool_result_total_context_chars`；
- 所有 body/preview rendered payload 之和必须 `<= tool_result_body_context_chars`；
- 所有 envelope rendered payload 之和必须 `<= tool_result_envelope_context_chars`；
- 每个 segment 还必须满足自己的 segment cap，例如 `prior_tool_result_context_chars`、`current_tail_tool_result_context_chars`、`legacy_tool_result_context_chars`；
- 每个 tool result 的 body/preview 必须满足 `tool_result_per_tool_cap_chars`；
- 每个 tool result 的 envelope 必须满足 `tool_result_per_envelope_cap_chars`；
- 单个结果的最终 rendered total 由 `body <= per_tool_cap` 与 `envelope <= per_envelope_cap` 共同约束；
- 每个 tool batch 分组必须满足 `tool_result_per_message_cap_chars`；
- 默认派生值必须保证 `tool_result_body_context_chars + tool_result_envelope_context_chars <= tool_result_total_context_chars`。
- 默认值必须保证 `max_tool_results_per_context * minimum_essential_envelope_chars <= tool_result_envelope_context_chars`，或者显式选择 fail-closed 策略。

配置 invariant：

- 默认值必须保证常见 latest batch 的 reserved 策略可满足，例如 `latest_tool_result_reserved_chars <= tool_result_per_tool_cap_chars`；
- 默认值还应保证 `latest_tool_result_reserved_chars * expected_max_latest_batch_results <= current_tail_tool_result_context_chars`；
- 如果运行时 latest batch 的短结果数量过多，导致 `N * latest_tool_result_reserved_chars` 超过 `current_tail_tool_result_context_chars`、`tool_result_body_context_chars` 或 `tool_result_per_message_cap_chars`，allocator 不能静默违反 hard caps；
- 冲突时优先保留每个 latest result 的 essential envelope，然后按 deterministic order / fairness 策略分配剩余 body budget，并记录 diagnostic，例如 `latest_reserved_budget_unsatisfied`。
- 如果运行时 tool result 数量太多，导致所有 essential envelopes 的总量都超过 `tool_result_envelope_context_chars` 或 total cap，必须 fail-closed：compiler 返回结构化失败 diagnostic（例如 `essential_envelope_budget_unsatisfied`）或抛出专门的 `ContextBudgetExceeded`，AgentRuntime 不得发起本次 model call；随后由 preflight compact / retry 策略重新编译。不能输出半截 JSON，也不能静默突破 hard cap。

`tool_result_per_message_cap_chars` 的 V1 grouping key：

1. 优先按 preceding assistant tool-call batch 分组，即产生这些 tool results 的 source assistant `Msg.id`；
2. 如果能从 tool-call / tool-result pairing 找到 source assistant `Msg.id`，则同一 assistant batch 的所有 tool results 共享 per-message cap；
3. 如果无法识别 source assistant message，则按单个 `ToolResultBlock.id` 自成一组，并记录 diagnostic，例如 `tool_result_batch_anchor_missing`；
4. 不能用当前 user message 简单分组，因为 Pulsara 的 provider lowering 里 assistant tool-call turn 与 tool_result pairing 才是更稳定的工具批次边界。

命名必须避免混淆：

- `source_message_id` 表示包含当前 `ToolResultBlock` 的 `Msg.id`；
- `source_assistant_message_id` 表示 preceding assistant tool-call batch 所在的 `Msg.id`，可能为空；
- `tool_batch_id` 是用于 per-message/per-batch budget 的稳定 grouping key，优先等于 `source_assistant_message_id`，否则退化为 `tool_call_id` 并记录 diagnostic。

### 2. 让 current-run fresh output 优先

当预算紧张时，降级顺序应该类似：

1. prior history 的旧 artifact/tool preview；
2. prior history 的旧普通 tool result；
3. current run tail 中较老/较大的 tool result；
4. current run 最新 tool result 的正文。

最新工具结果不应该因为旧历史而直接变成 omitted。

`latest_tool_result_reserved_chars` 的稳定规则：

- “latest” 指 current run tail 中最后一个 settled or actionable tool result batch；
- 如果最后一个 batch 有多个 tool result，则每个短结果都应获得 reserved 保护；
- reserved 只保护 `body_candidate_chars <= latest_tool_result_reserved_chars` 的短结果完整可见；
- reserved 不受 prior history budget 影响；
- reserved 仍受 `tool_result_per_tool_cap_chars` 约束，不能让一个伪装成 latest 的巨大结果绕过 per-tool cap；
- 如果 current tail 总预算和 reserved 冲突，优先保留 latest short results，再降级 current tail 中更早、更大的结果；
- terminal-like 与非 terminal-like 工具都适用，但 terminal / terminal_process 的 envelope 字段必须额外保留。

“settled or actionable” 覆盖：

- `success`；
- `error`；
- `interrupted`；
- `denied`；
- `running` 且存在可继续操作的信息，例如 yielded/background process 的 `process_id` / `terminal_process_action` / `status`。

短失败输出、权限拒绝说明、中断原因、后台进程的 process id 都属于应被 freshness 保护的信息。

`body_candidate_chars` 的来源优先级：

1. 从 terminal / tool JSON payload 中提取出的正文 preview 长度，例如 `output` 字段；
2. 如果 artifact preview policy 表明 preview 是完整正文，或 `original_chars == preview_chars`，可以使用 `ToolResultArtifactRef.preview.preview_chars`；
3. 如果 preview 是 head/tail 截断、或 `original_chars > preview_chars`，不能把 `preview_chars` 当成短结果长度；应使用 `original_chars` 判定，或直接标记为 `non_short_truncated_preview`；
4. 若没有 `preview_chars`，且能证明 preview 完整，才可使用 `visible_head_chars + visible_tail_chars + truncation_notice_chars` 或等价 rendered preview 长度；
5. tool result payload 已有的完整 body/output 字段长度；
6. fallback 才使用 `len(render_source_text)`。

这条规则的目的，是避免把“大输出的短 preview”误判成“短结果”。例如原始输出 200k、artifact preview 4k 的结果，不应因为 4k preview 小于 reserved 而获得完整 latest reserved 保护。

不要直接用 `len(render_source_text)` 判断 terminal 短输出，因为 terminal/artifact-backed 的 `render_source_text` 可能包含 JSON envelope / adaptive preview metadata，不等于纯正文长度。

### 3. 把 tool result budget 纳入 context compiler

理想形态不是在 `_message_to_llm_messages(...)` 里提前消耗预算，而是让 context compiler 知道：

- 哪些 tool result 属于 prior history；
- 哪些属于 current run tail；
- 哪些有 artifact ref；
- 哪些是最新结果；
- 哪些必须保持 provider tool-call/tool-result pairing；
- 哪些正文可以降级。

然后由 compiler 输出最终 lowering 结果与预算报告。

实施边界必须钉死：**compiler 必须接收 raw `Msg` segment 或结构化 `ToolResultRenderUnit`，不能只接收已经预算裁剪过的 `LLMMessage`**。

当前真实路径是：

1. `build_compiled_context(...)` 调用 `_segmented_llm_messages_for_anchor(...)`；
2. `_segmented_llm_messages_for_anchor(...)` 创建单个 `_ToolResultRenderBudget`；
3. `_message_to_llm_messages(...)` 把 `Msg` lowered 成 `LLMMessage`，并在这个过程中裁掉 tool result body；
4. compiler 收到的 `prior_history_messages` / `current_user_messages` / `current_run_tail_messages` 已经是裁剪后的 `LLMMessage`。

这条路径必须调整。否则即便只是把 `_segmented_llm_messages_for_anchor(...)` 中的单个 budget 拆成几个 budget，也只能缓解 starvation，仍然绕过 compiler 的 report / diagnostics / inspect 事实。

建议输入形态：

```python
@dataclass(frozen=True, slots=True)
class ToolResultRenderUnit:
    tool_call_id: str
    source_message_id: str
    source_assistant_message_id: str | None
    tool_batch_id: str
    tool_name: str
    segment: Literal["prior_history", "current_run_tail", "legacy_history"]
    state: str
    render_source_text: str
    artifacts: tuple[ToolResultArtifactRef, ...]
    terminal_envelope: TerminalResultEnvelope | None
    original_chars: int | None
    body_candidate_chars: int | None
    body_candidate_source: str | None
    message_index: int
    tool_result_index: int
```

这里故意使用 `render_source_text`，不要叫 `raw_output_text`。原因是：对 terminal / artifact-backed 结果，`ToolResultBlock.output` 中经常已经是 adaptive preview 或 JSON payload；完整输出在 artifact store 中。context compile 阶段不应该为了预算同步读取巨大 artifact 全文。

身份字段必须稳定：

- `source_message_id` 来自 `Msg.id`，是 render unit 所在消息的稳定身份；
- `source_assistant_message_id` 来自 preceding assistant tool-call batch 的 `Msg.id`，用于解释这个 tool result 属于哪一批工具调用；
- `tool_batch_id` 是 per-message/per-batch budget 的实际 grouping key，优先等于 `source_assistant_message_id`，否则退化为 `tool_call_id`；
- `tool_call_id` 必须等同于 `ToolResultBlock.id`；
- `message_index` / `tool_result_index` 只用于调试和 deterministic render order，不得作为 durable 主身份，因为 compaction / transcript rewrite / replay 会改变 index。

`original_chars` 的来源优先级：

1. `ToolResultArtifactRef.preview.original_chars`；
2. tool result payload 中已有的 `original_chars` / `output_chars` / `truncated` 元数据；
3. `render_source_text` 长度；
4. 无法得知时记录 `None` 或 diagnostic，不能强行读取 artifact 全文。

`body_candidate_chars` / `body_candidate_source` 用于解释 latest reserved 是否命中：

- `body_candidate_chars` 是用于 short-result 判定的正文候选长度；
- `body_candidate_source` 记录来源，例如 `terminal_output_field`、`artifact_preview_full`、`artifact_original_chars`、`payload_output_field`、`render_source_text_fallback`、`non_short_truncated_preview`；
- 当来源是 `non_short_truncated_preview` 时，reserved 不应按 preview 长度命中。

`legacy_history` 的处理也必须显式定义。当前 compiler 在 current user anchor 找不到时会退回 `transcript:legacy_history`。后续实现不能让 legacy fallback 继续走旧的全局 `_ToolResultRenderBudget`。建议策略：

- 优先 fail-closed diagnostic：缺少 current user anchor 时，报告 `context_anchor_missing`，并使用保守 legacy budget；
- legacy history 使用 `legacy_tool_result_context_chars`；如果 V1 不新增独立字段，则必须显式映射到 `prior_tool_result_context_chars` 并记录 diagnostic；
- legacy history 不享受 `latest_tool_result_reserved_chars`，因为没有可靠 current tail 边界；
- 如果需要恢复 current-run 语义，必须先修复 anchor，而不是在 legacy 模式里猜测 latest。

compiler 负责：

- 选择每个 unit 的 render policy；
- 生成最小 envelope；
- 决定 visible body / preview / omitted；
- 输出最终 `LLMContext.messages`；
- 输出 per-tool-result render decision；
- 保证 provider tool-call/tool-result pairing 不被破坏。

### 3.1 terminal / terminal_process 最小模型可见 envelope

省略正文时，不能只保留 artifact ref。模型至少需要知道工具是否成功、命令是否结束、是否还能继续轮询。

对 `terminal`，最小 envelope 应保留：

- `tool_call_id`；
- `tool_name`；
- result `state`；
- terminal `status`；
- `exit_code`；
- `cwd`；
- `timed_out`；
- `truncated`；
- `process_id`（如果存在）；
- `terminal_session_id`（如果存在）；
- `yielded_to_background`；
- `backend_type`；
- `io_mode`；
- 可选 `duration_seconds`；
- 可选 `stdin_closed`；
- 可选 `policy_code`；
- artifact/read_more 信息（如果存在）。

对 `terminal_process`，最小 envelope 还应保留：

- `terminal_process_action`；
- `process_id`；
- `status`；
- `exit_code`；
- `cwd`；
- `timed_out`；
- `truncated`；
- `terminal_session_id`（如果存在）；
- `yielded_to_background`；
- `backend_type`；
- `io_mode`；
- artifact/read_more 信息（如果存在）。

对 `terminal_process action=list`，结果不是单个进程 envelope，而是 process inventory envelope。正文被省略时至少保留：

- `terminal_process_action = "list"`；
- `live_process_count`；
- `finished_process_count`；
- 可操作的 `process_ids` 列表，至少保留前若干个并说明是否截断；
- 每个保留 process 的最小状态，例如 `process_id`、`status`、`cwd`、`exit_code`（如果存在）。

V1 可以用单独 `TerminalProcessListEnvelope` DTO，也可以让 `TerminalResultEnvelope` 增加 `processes_summary` 字段；但不能把 list 降级成只有 artifact ref。

`processes_summary` 的裁剪语义：

- 优先保留 running / yielded / otherwise actionable processes；
- 其次保留最近 finished 的 processes；
- 每个 process summary 至少包含 `process_id`、`status`，尽量保留 `cwd`、`exit_code`；
- 当 process 很多时，按上述排序截断，并记录 `processes_summary_truncated=True` 与 `omitted_process_count`；
- 如果某个 process 的 `cwd` 或 error 太长，字段级裁剪，不丢弃 `process_id`。

`TerminalResultEnvelope` 应是 Pulsara-owned DTO，而不是直接把 terminal tool 的 JSON payload 原样塞给 compiler。建议形状：

```python
@dataclass(frozen=True, slots=True)
class TerminalResultEnvelope:
    status: str | None
    exit_code: int | None
    cwd: str | None
    timed_out: bool | None
    truncated: bool | None
    process_id: str | None
    terminal_session_id: str | None
    yielded_to_background: bool | None
    backend_type: str | None
    io_mode: str | None
    terminal_process_action: str | None = None
    error: str | None = None
    processes_summary: tuple[dict[str, object], ...] = ()
    processes_summary_truncated: bool | None = None
    omitted_process_count: int | None = None
    live_process_count: int | None = None
    finished_process_count: int | None = None
    duration_seconds: float | None = None
    stdin_closed: bool | None = None
    policy_code: str | None = None
```

最终 rendered envelope 仍要受 `tool_result_per_envelope_cap_chars` 约束。字段值很长时，例如 `cwd` 极长、`error` 很长或 diagnostic 很长，应裁剪字段值，而不是扩大 envelope。错误原因通常是正文省略后模型最需要看到的信息之一，因此 `error` 属于 essential envelope 字段，但可被字段级裁剪。

配置校验必须保证 `tool_result_per_envelope_cap_chars >= minimum_essential_envelope_chars`。如果配置本身无法容纳最小 parseable envelope，应在构造 policy / runtime 启动时失败。若运行时出现异常超限，例如极端长 key 或编码膨胀，必须输出最小 parseable envelope，并记录 `essential_envelope_over_cap` diagnostic，不能输出不可解析的半截 JSON。

这保证模型即使看不到完整日志，也能回答：

- 刚才命令是否成功？
- 进程是否还在运行？
- 该用 `terminal_process log/poll/wait` 继续拿结果，还是已经完成？
- 完整输出应从哪个 artifact 继续读？

### 3.2 per-tool-result render decision schema

`ContextCompiledEvent` / inspect 不应只保存 section 级 diagnostics。需要在 `ContextCompiledEvent` 中新增结构化 per-tool-result render decisions，至少包含：

```json
{
  "tool_call_id": "call:...",
  "source_message_id": "msg:...",
  "source_assistant_message_id": "msg:assistant-tool-batch",
  "tool_batch_id": "msg:assistant-tool-batch",
  "tool_name": "terminal",
  "segment": "prior_history",
  "render_order": 17,
  "message_index": 12,
  "state": "success",
  "original_chars": 71234,
  "body_candidate_chars": 71234,
  "body_candidate_source": "artifact_original_chars",
  "latest_reserved_candidate": false,
  "latest_reserved_applied": false,
  "latest_reserved_reason": "body_candidate_exceeds_reserved",
  "visible_body_chars": 2048,
  "rendered_envelope_chars": 420,
  "rendered_total_chars": 2468,
  "body_budget_remaining": 8192,
  "envelope_budget_remaining": 2048,
  "primary_artifact_id": "artifact:primary-text",
  "artifact_ids": ["artifact:diagnostics", "artifact:primary-text"],
  "artifact_ref_count": 2,
  "body_policy": "artifact_preview",
  "envelope_policy": "full_envelope",
  "reason": "prior_history_budget_exceeded",
  "clipped_envelope_fields": [],
  "read_more": {
    "tool": "artifact_read",
    "artifact_id": "artifact:primary-text",
    "suggested_offset_chars": 2048
  }
}
```

多 artifact 语义：

- `primary_artifact_id` 指用于 `read_more` 的 primary text artifact；
- `artifact_ids` 保留该 tool result 所有关联 artifact 的 id，便于 inspect；
- `artifact_ref_count` 允许 event payload 在需要时只保留计数与 primary id；
- `read_more.artifact_id` 必须指向 primary text artifact，而不是 diagnostics / image / binary ref。
- 如果没有 text artifact，`primary_artifact_id=null`，不生成 `read_more.artifact_id`；只保留 `artifact_ids` / `artifact_ref_count`，并记录 diagnostic，例如 `tool_result_primary_text_artifact_missing`。

建议 body policy 枚举：

- `full_visible`；
- `clipped_preview`；
- `artifact_preview`；
- `omitted_non_artifact`。

建议 envelope policy 枚举：

- `full_envelope`；
- `metadata_only`；
- `essential_envelope`；
- `envelope_clipped`；
- `omitted_envelope`（仅允许在非 essential、非 terminal-like 信息上使用；terminal-like essential envelope 不得静默省略）。

`omitted_envelope` 的限制必须更严格：只能省略 body 附带的 full metadata envelope 或非 essential metadata。tool result header 与 essential identity fields 不得省略，包括：

- `tool_call_id`；
- `tool_name`；
- `state`；
- `source_message_id`；
- `segment`；
- terminal-like/actionable result 的 essential envelope。

`original_chars` 在事件 schema 中也必须允许 `null`。当 policy 是 `essential_envelope` 或 `envelope_clipped` 时，应记录 `clipped_envelope_fields`，例如 `["duration_seconds", "policy_code"]`，让 inspect 能解释哪些 metadata 字段被裁掉。

`latest_reserved_candidate` / `latest_reserved_applied` / `latest_reserved_reason` 用于直接解释 freshness 保护是否命中：

- `latest_reserved_candidate=true` 表示该 unit 属于 current run tail 的 latest settled/actionable batch，且具备进入 reserved 判定的资格；
- `latest_reserved_applied=true` 表示 allocator 实际为它保留了 short-result budget；
- `latest_reserved_reason` 应写明命中或未命中的原因，例如 `short_result_visible`、`body_candidate_exceeds_reserved`、`non_short_truncated_preview`、`latest_reserved_budget_unsatisfied`、`not_latest_batch`。

`body_budget_remaining` / `envelope_budget_remaining` 是顺序相关字段，必须定义清楚：它们表示最终 allocator 决策完成后，按 deterministic render order 消耗到当前 unit 后的剩余额度。若实现采用“先保护 latest、再回头降级 prior”的多阶段 allocator，也必须在最终排序后重新计算 remaining，不能暴露中间阶段的临时值。

decision 中必须写入 `render_order` 或 `allocation_order`，否则 inspect 只能看到 remaining，无法解释为什么某个 unit 先消耗预算。

如果后续发现 remaining 不利于 inspect，可增加更稳定的 aggregate 字段，例如：

- `body_budget_used_by_scope`；
- `envelope_budget_used_by_scope`；
- `body_budget_cap_by_scope`；
- `envelope_budget_cap_by_scope`。

这样 inspect 才能真正回答“为什么这个 tool result 没进模型上下文、模型当时看到了多少、要如何恢复全文”。

同时，`ContextCompiledEvent` 必须保存 aggregate `tool_result_budget_report`，而不是只让 inspect 从 per-result decisions 反推。建议形状：

```json
{
  "caps": {
    "tool_result_total_context_chars": 36000,
    "tool_result_body_context_chars": 28000,
    "tool_result_envelope_context_chars": 8000,
    "tool_result_per_envelope_cap_chars": 1200,
    "prior_tool_result_context_chars": 8000,
    "current_tail_tool_result_context_chars": 20000,
    "legacy_tool_result_context_chars": 4000,
    "tool_result_per_tool_cap_chars": 12000,
    "tool_result_per_message_cap_chars": 20000,
    "latest_tool_result_reserved_chars": 2048,
    "expected_max_latest_batch_results": 4,
    "max_tool_results_per_context": 64,
    "minimum_essential_envelope_chars": 256
  },
  "used": {
    "total": 18420,
    "body": 14120,
    "envelope": 4300
  },
  "remaining": {
    "total": 17580,
    "body": 13880,
    "envelope": 3700
  },
  "used_by_scope": {
    "prior_history": {"body": 4096, "envelope": 2200},
    "current_run_tail": {"body": 10024, "envelope": 2100}
  },
  "diagnostics": [
    {"code": "latest_reserved_budget_unsatisfied", "count": 1}
  ]
}
```

这样 inspect 可以直接解释整体预算状态、cap、used/remaining 和 unsatisfied diagnostics，而不需要重新拼接所有 per-result decisions。

事件落点必须固定为 `ContextCompiledEvent`。字段名固定为 `tool_result_render_decisions`。不要把这类事实散落到 scratchpad、临时 inspect 计算或另一个模糊的“等价事件”中。context compile 是决定模型实际看到什么的地方，因此 per-tool-result render decisions 是 context compile fact。

当 compile fail-closed，例如 essential envelope 总量不可满足时，也必须落 durable 事件。V1 建议新增 `ContextCompileFailedEvent`，或允许 `ContextCompiledEvent(status="failed")`；无论采用哪种，事件必须包含：

- `context_id`；
- `run_id` / `turn_id` / `reply_id` / `model_call_index`；
- `tool_result_budget_report`；
- failure diagnostics，例如 `essential_envelope_budget_unsatisfied`；
- 若已有部分 allocator decisions，也可包含 `tool_result_render_decisions`，但不得发起 model call。

### 4. 改善 placeholder 文案

当前：

```text
[TOOL RESULT OMITTED: context budget exhausted]
```

容易让模型误以为整个会话不可继续。

更好的文案应区分：

- old tool result body omitted；
- full output is available via artifact；
- current tool result body omitted due to tool-result render budget；
- main context window is not necessarily exhausted。

例如：

```text
[OLD TOOL RESULT BODY OMITTED: prior-history tool-result preview budget exhausted; use artifact_read if needed]
```

或者对当前 run：

```text
[TOOL RESULT BODY OMITTED: current-run tool-result preview budget exhausted; status/metadata preserved]
```

还必须区分 artifact-backed 与 non-artifact：

- artifact-backed omitted：必须给出 artifact id / suggested offset / 可使用的读取工具；
- non-artifact omitted：不能假装可用 `artifact_read`，只能明确说明正文没有进入模型上下文。

例如：

```text
[OLD TOOL RESULT BODY OMITTED: artifact artifact:abc has the full output; use artifact_read with suggested_offset_chars=2048]
```

```text
[OLD TOOL RESULT BODY OMITTED: no artifact was retained for this result; status metadata is preserved only]
```

### 5. 增加回归测试

至少需要以下测试：

1. huge prior tool result + current user + short fresh tool result，fresh output 必须可见；
2. huge prior tool result 可以被降级，但 artifact ref / status 保留；
3. current run tail 中大 tool result 有 per-tool cap，不会吞掉同一 tail 中其他短结果；
4. provider tool-call/tool-result pairing 不被预算拆坏；
5. context compiler report 与最终 `LLMContext.messages` 一致；
6. compaction estimate 使用最终模型可见 payload，而不是预算前 payload。
7. huge old `terminal_process log` 后的新短 `terminal` 输出仍可见；
8. huge old `terminal` 后的新短 `terminal_process wait/log` 输出仍可见；
9. yielded/background process 的 `log` / `poll` / `wait` 不被 prior history 饿死，并保留 `process_id` / `terminal_process_action`；
10. 同一 current run tail 内，一个大 `uv add` / install log 不吞掉后面的短 `uv run python main.py` 输出；
11. artifact-backed omitted 与 non-artifact omitted 的 placeholder 文案不同；
12. `ContextCompiledEvent` 中能投影出 per-tool-result render decisions。
13. latest batch 短结果数量超过 `expected_max_latest_batch_results` 或 current-tail/body/per-message cap 时，不违反 hard caps，保留 essential envelope，并发出 `latest_reserved_budget_unsatisfied` diagnostic。
14. 找不到 source assistant message 时，fallback 到 per-block `tool_batch_id = tool_call_id`，并发出 `tool_result_batch_anchor_missing` diagnostic。
15. essential envelope 超过配置 cap 时，仍输出 parseable minimal envelope，并发出 `essential_envelope_over_cap` diagnostic。
16. `terminal_process action=list` 在进程很多时保留 counts、actionable process ids，记录 `processes_summary_truncated` / `omitted_process_count`。
17. tool result 数量太多导致 essential envelopes 总量超过 envelope/total cap 时，fail-closed 并发出 `essential_envelope_budget_unsatisfied` diagnostic。
18. 多 artifact tool result 的 render decision 保留 `primary_artifact_id`、`artifact_ids` 或 `artifact_ref_count`，且 `read_more` 指向 primary text artifact。
19. `ContextCompiledEvent.tool_result_budget_report` 保存 caps、used、remaining、used_by_scope 和 diagnostics。
20. 多 artifact 场景中，只有 primary text artifact 用于 `read_more`；diagnostics、binary、image 或非 text artifact 不得被误选为 `read_more.artifact_id`。
21. 大输出只有 head/tail 短 preview 时，`body_candidate_source=non_short_truncated_preview`，不能被 `latest_tool_result_reserved_chars` 误判为短结果。

## 验收标准

修复完成后，真实 REPL 应满足：

1. 用户刚刚要求运行一个命令，命令输出很短时，模型应看到这段短输出；
2. 旧的 MCP docs / uv install / firecrawl scrape 输出不会让最新短输出不可见；
3. 如果确实需要省略旧工具结果，模型应看到明确、可操作的 artifact/read-more 指引；
4. `:compact` 不应成为“让最新输出重新可见”的必要手段；
5. inspect 能解释每个 tool result 的预算决策；
6. auto compaction 的 200k 阈值语义不应与 tool result body preview budget 混淆。

## 一句话结论

Pulsara 当前不是上下文窗口太小，而是工具结果预算的作用域太大：旧历史工具输出可以消耗掉当前 run 的新鲜输出预算。Codex 通过 per-call 截断避免这种 starvation；Claude Code 通过 per-message aggregate budget + stable replacement 避免这种 starvation。Pulsara 应该在 context compiler 层做一个更结构化的版本：旧结果可降级，新结果有保留预算，完整输出通过 artifact 可恢复。
