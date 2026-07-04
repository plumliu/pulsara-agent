# Message / Transcript / Context Contract

_Created: 2026-07-04_

本文档定义 Pulsara 内部 message block、event replay、prior transcript reconstruction 与 model context budgeting 的长期契约。

相关代码：

- [src/pulsara_agent/message/blocks.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/blocks.py)
- [src/pulsara_agent/message/assembler.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/assembler.py)
- [src/pulsara_agent/message/reducer.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/reducer.py)
- [src/pulsara_agent/runtime/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/transcript.py)
- [src/pulsara_agent/runtime/context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/context.py)
- [tests/test_event_message_system.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_event_message_system.py)
- [tests/test_host_core.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_host_core.py)

---

## 1. 核心立场

Pulsara 有三层不同对象：

- `AgentEvent`：durable truth。
- `Msg` / content blocks：runtime replay/projection。
- `LLMContext` / `LLMMessage`：provider-neutral model request。

三者不得混用。Event log 是事实；message 是重放结果；LLM context 是当前模型调用视图。

---

## 2. Content blocks

Runtime message content 使用 typed blocks：

- `TextBlock`
- `ThinkingBlock`
- `DataBlock`
- `HintBlock`
- `ToolCallBlock`
- `ToolResultBlock`

Tool result artifact refs 必须使用 `ToolResultArtifactRef`。如果 artifact 有 durable preview metadata，必须放在 `ToolResultArtifactRef.preview`，而不是只放 transient renderer metadata。

旧 event log 中没有 `preview` 字段的 artifact ref 必须仍能 replay。

---

## 3. Block assembly

`BlockAssembler` 将 event stream 增量折叠为 completed content blocks。

规则：

- start/delta/end 正常成对组装。
- orphan delta/end 是 recoverable stream problem，assembler 忽略，而不是崩溃。
- `ToolResultEndEvent` 负责写入 final state 与 artifact refs。
- `ExternalExecutionResultEvent` 可以作为已完成 tool result block 输入。

严格业务入口若依赖完整 block，必须另行用 diagnostics 检查 orphan/unfinished 状态。

---

## 4. Message reducer

`MessageReducer` 只重建单个 reply message。

职责：

- 追加 completed blocks；
- 聚合 `MODEL_CALL_END` usage；
- 根据 `TOOL_RESULT_END` 把对应 `ToolCallBlock` 标为 finished；
- 根据 approval events 更新 tool call state；
- 设置 `ReplyEndEvent.finished_at`。

它不负责：

- prior transcript 排序；
- recovery notes；
- compaction boundary；
- terminal completion note；
- user message reconstruction。

这些由 `runtime/transcript.py` 负责。

---

## 5. Prior transcript reconstruction

`rebuild_prior_messages()` 是 normal resume/preflight prior transcript 的唯一入口。

规则：

- 从 event log 读取 events；
- 若存在最新有效 completed compaction boundary，先注入 summary system message，再只 replay boundary 后 events；
- 每个 `RUN_START.metadata.user_input` 生成 user message；
- 每个 completed reply 通过 `event_log.replay(reply_id)` 重建 assistant message；
- failed/aborted recoverable last run 注入 recovery system note；
- terminal process completion after last run start 注入 lifecycle-only note；
- 对 aborted/failed terminal runs，必须 strip unfinished tool calls，避免 provider tool-call ordering 违法。

`rebuild_prior_messages_before_sequence()` 是 mid-turn inline compaction 的 prefix-only replay helper；它必须严格 replay `sequence < before_sequence`，并保留 current run tail 给 in-memory `LoopState.messages`。

---

## 6. Model context assembly

`build_llm_context()` 是把 `LoopState.messages` 转成 `LLMContext` 的入口。

必须包含：

- system prompt + memory projection；
- replayed messages；
- recovery prompt note（若 in-run recovery active）；
- current tool specs。

Thinking/tool-call provider metadata 不应作为 natural-language text 混入 user/system messages；assistant turn 应保留 structured tool calls。

---

## 7. Tool result context budget

`LoopBudget.tool_result_context_chars` 是一次 context render 内所有 tool result 的 aggregate char budget，不是每个 tool result 的独立预算。

规则：

- 普通 tool result text 按剩余额度裁剪。
- 含 artifact 的 tool result 必须渲染 parseable JSON envelope：
  - `output_preview`
  - `output_truncated`
  - `artifacts`
- 若 aggregate budget 耗尽，必须保留 bounded compact envelope，而不是无限塞入所有 artifact refs。
- compact envelope 必须优先保留带 `preview` 的 primary artifact ref；没有 preview 时才退回第一个 ref。
- compact artifact payload 必须保留 `artifact_id`、role、size、read_more；不得丢失可读取完整输出的入口。

---

## 8. Data / binary blocks

Model context 中不得直接内联任意 binary/data body。

`DataBlock` 必须渲染为 placeholder，包含：

- id；
- optional name；
- media type；
- source kind。

真正的数据读取必须通过 artifact 或专用工具路径。

---

## 9. 禁止事项

- 不允许把 `Msg` 当 durable truth。
- 不允许 context renderer 无界内联大 tool result。
- 不允许 compact envelope 只保留第一个 artifact 而丢掉 primary preview artifact。
- 不允许 unfinished assistant tool call 在 transcript replay 中喂给 provider。
- 不允许 compaction summary 替代系统提示词/skill active injection。
- 不允许 message reducer承担 recovery/compaction/terminal completion note 的职责。

---

## 10. 测试守护

最低测试门槛：

- event stream folds into text/thinking/tool call/tool result blocks。
- usage from multiple model calls aggregates on reply message。
- missing start event does not crash assembler。
- prior transcript injects user messages from `RUN_START.metadata.user_input`。
- failed/aborted last run injects recovery note。
- unfinished tool call is stripped from aborted/failed replay。
- terminal completion note is lifecycle-only and capped。
- compaction boundary summary is used and tail replayed。
- mid-turn prefix replay excludes current run tail。
- aggregate tool result context budget applies across multiple results。
- compact artifact envelope keeps primary preview artifact.
