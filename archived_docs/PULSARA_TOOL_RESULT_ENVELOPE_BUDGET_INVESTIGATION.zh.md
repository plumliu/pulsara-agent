# Tool Result Render Policy：Envelope Context 预算问题调研与实施规格

日期：2026-07-07  
状态：实施规格草案  
相关模块：context compiler、tool result renderer、artifact preview、context compaction、REPL failure reporting

## 0. 结论先行

这次真实 REPL 失败不是 `max_turns` 不够，也不是 `artifact_read` 持久化契约再次坏掉，而是 tool result renderer 的预算语义过硬：

```text
rendered_envelope_chars = 12403
tool_result_envelope_context_chars = 12000
tool_result_total_context_chars = 36000
rendered_total_chars = 28403
total remaining = 7597
```

模型上下文总量并没有打爆，只是 envelope 子预算超了 403 chars。当前代码把这个子预算超限升级成 `ContextBudgetExceeded`，导致整个 run failed，REPL 又没有打印 failed run 的原因，于是用户看到的体验是“没有输出，直接回到 pulsara>”。

推荐方案不是简单把 `12000` 调大，而是把 tool result rendering 升级为一个分层、稳定、可解释的优先级 allocator：

1. `tool_result_context_chars` 是最终 rendered tool-result payload 的硬上限。
2. body / envelope 这类 aggregate 子预算是 soft target，超出时先降级和借用，不能立刻失败。
3. 借用不是无限借用：不能突破 total hard cap，不能借穿 latest/current-tail protected pools，不能突破单结果 body/envelope hard cap。
4. 模型可见 envelope 只保留执行身份与恢复路径；source ids、render order、预算解释进入 `tool_result_render_decisions` / `tool_result_budget_report`。
5. 旧 tool result 优先降级为 artifact-backed / ultra-minimal envelope，而不是让整轮 run failed。
6. 可恢复预算压力用 `ContextCompiledEvent(status="pressure")` 记录 compile attempt，然后 compact/retry；真正不可恢复时才 durable failed event + 不发 model call。
7. REPL failure visibility 应前移成 PR0，避免后续排障继续掉进“静默回 prompt”的黑洞。

## 1. 问题是如何发现的

用户在真实 Pulsara REPL 中测试 LangChain Docs MCP：

```bash
uv run pulsara host repl --env-file .env --workspace ~/Desktop/little_snake
```

启动时 MCP 正常 ready：

```text
MCP servers: docs-langchain=ready (3 tools; 1 diagnostics)
```

用户请求：

```text
请你使用langchain docs mcp帮我实现一个最小的react agent，我已经为当前项目初始化好了uv环境，
请你使用uv add添加任何你需要的依赖，然后使用.env文件作为真实配置来请求真实大模型。
请你一定不要直接阅读这个文件！
在做完之后，请你来直接运行测试一下，最后汇报给我结果。
```

REPL 表面现象是：没有任何最终文本，直接回到 `pulsara>`。

通过 inspect 查到最新 run：

```text
run_id: run:e5a513aed21348cf856324e4baf0bbbf
status: failed
stop_reason: model_error
model_call_count: 14
tool_call_count: 20
```

关键错误：

```text
RUN_ERROR code=context_budget_exceeded
message=Tool result render budget hard cap exceeded: essential_envelope_budget_unsatisfied
```

对应 `ContextCompiledEvent(status="failed")` 的 budget report：

```json
{
  "caps": {
    "tool_result_total_context_chars": 36000,
    "tool_result_body_context_chars": 24000,
    "tool_result_envelope_context_chars": 12000,
    "current_tail_tool_result_context_chars": 16000,
    "prior_tool_result_context_chars": 8000,
    "tool_result_per_tool_cap_chars": 12000,
    "tool_result_per_message_cap_chars": 20000,
    "tool_result_per_envelope_cap_chars": 1200
  },
  "used": {
    "body": 16000,
    "envelope": 12403,
    "total": 28403
  },
  "remaining": {
    "body": 8000,
    "envelope": 0,
    "total": 7597
  }
}
```

这组数字说明：整体模型可见 tool-result payload 仍在 36k hard cap 内，失败来自 envelope 子预算被当成 hard cap。

## 2. 不是哪些问题

### 2.1 不是 max_turns 问题

此前类似任务曾出现：

```text
EXCEED_MAX_ITERS max_iters=20
```

因此默认 `LoopBudget.max_turns` 已从 20 调到 50。那是必要调整，因为“查 docs → 写代码 → 安装依赖 → 运行 → 修错 → 汇报”确实可能超过 20 个 loop。

但本次失败为：

```text
model_call_count = 14
tool_call_count = 20
stop_reason = model_error
code = context_budget_exceeded
```

所以它不是循环上限，而是下一次 model call 前 context compile 被拒绝。

### 2.2 不是 artifact_read 持久化问题

上一轮还遇到过：

```text
tool_result_persistence_failed:
record_tool_result() called with output > 8000 chars but no artifact ref
```

那个问题来自 `artifact_read` 读取已有 artifact 的大文本，但自身不递归归档，也没有把源 artifact ref 透传给 evidence ledger。

当前修复方向是：`artifact_read` 不创建新 artifact，但把源 artifact ref 附到结果上，让大输出仍有 evidence anchor。

本次事件没有再出现 `tool_result_persistence_failed`，而是 `context_budget_exceeded`。这是 context rendering 层的问题。

## 3. 当前代码落点与真实执行路径

本节是后续动刀的边界。不要把问题误修到 terminal preview、artifact archive threshold 或 MCP adapter 上；真正饿死/失败发生在 replay/context lowering 层。

### 3.1 预算字段：`LoopBudget`

当前预算入口在：

```text
src/pulsara_agent/runtime/state.py
```

关键字段：

```python
tool_result_context_chars: int = 36_000
tool_result_body_context_chars: int | None = None
tool_result_envelope_context_chars: int = 16_384
prior_tool_result_context_chars: int | None = None
current_tail_tool_result_context_chars: int | None = None
legacy_tool_result_context_chars: int | None = None
tool_result_per_tool_cap_chars: int | None = None
tool_result_per_message_cap_chars: int | None = None
tool_result_per_envelope_cap_chars: int = 1_200
latest_tool_result_reserved_chars: int = 2_048
max_tool_results_per_context: int = 256
minimum_essential_envelope_chars: int = 256
```

注：真实轨迹里的 `tool_result_envelope_context_chars = 12000` 来自当时运行配置/旧默认；当前 main 上默认值已是 `16_384`。这不改变问题性质：aggregate envelope 子预算不应作为孤立 hard failure 线。

这些字段是 char-based，不是 token-based。实施文档与事件报告必须明确：

- renderer 用 chars 做硬边界；
- compiler/report 可以额外记录 estimated tokens；
- 不要把 Codex 的 token cap 和 Pulsara 的 char cap 混为同一语义。

### 3.2 Context facade：raw Msg 已经进入 tool-result renderer

当前入口在：

```text
src/pulsara_agent/runtime/context.py
```

`build_compiled_context()` 的路径是：

```text
state.messages
  -> render_segmented_llm_messages(state.messages, budget, anchor)
  -> raise_if_tool_result_budget_unsatisfied(...)
  -> compile_context(...)
  -> LLMContext
```

这个落点是对的：context compiler 相关路径已经能看到 raw `Msg` / `ToolResultBlock`，而不是只能看到已经裁过的 `LLMMessage`。后续实现必须守住这个边界：

```text
tool result budgeting 的真入口必须在 raw Msg / ToolResultRenderUnit 阶段；
不能回退到先生成 LLMMessage，再对字符串做二次裁剪。
```

### 3.3 Tool result renderer：当前失败的根

当前 allocator 在：

```text
src/pulsara_agent/runtime/context_engine/tool_results.py
```

当前结构包括：

- `render_segmented_llm_messages(...)`：按 current user anchor 拆分 prior history / current user / current run tail。
- `_ToolResultRenderAllocator`：维护 body、envelope、total、segment、latest reserved、per-message budgets。
- `tool_result_render_decisions`：逐 tool result 的渲染决策。
- `tool_result_budget_report`：聚合 caps / used / remaining / diagnostics。
- `raise_if_tool_result_budget_unsatisfied(...)`：把特定 diagnostics 升级成 `ContextBudgetExceeded`。

当前失败来自 report 阶段：

```text
rendered_envelope > tool_result_envelope_context_chars
=> diagnostic essential_envelope_budget_unsatisfied
=> raise ContextBudgetExceeded
```

后续 PR1 的核心就是把这个语义改掉：aggregate envelope 超 soft target 应先记录 borrow/pressure，不应直接进入 fail set。

### 3.4 Context compiler：事件字段已经够用

当前编译器在：

```text
src/pulsara_agent/runtime/context_engine/compiler.py
```

`CompiledContext` 已经包含：

- `tool_result_render_decisions`
- `tool_result_budget_report`

事件类型在：

```text
src/pulsara_agent/event/events.py
```

`ContextCompiledEvent` 当前已经持久化：

- `status`
- `context_id`
- `model_call_index`
- `diagnostics`
- `tool_result_render_decisions`
- `tool_result_budget_report`

PR1 需要新增：

- `compile_attempt_index` / `context_retry_index`，用于区分 pressure/retry attempt。

因此后续不需要新增主事件类型来解释每个 tool result 的 render decision；应继续把事实落到 `ContextCompiledEvent`。如果 compile 失败，也必须继续发 `ContextCompiledEvent(status="failed")`，并保留 budget report。可恢复 pressure 则发 `ContextCompiledEvent(status="pressure")`，表示“本次 compile attempt 未发起 model call，但 runtime 还会 compact/retry”。

### 3.5 AgentRuntime：失败已经 durable，但不会恢复

当前落点在：

```text
src/pulsara_agent/runtime/agent.py
```

`ContextBudgetExceeded` 会变成：

- `state.status = FAILED`
- `state.stop_reason = "model_error"`
- `ContextCompiledEvent(status="failed")`
- `RunErrorEvent(code="context_budget_exceeded")`

这保证了 inspect 可见，但没有 compact/retry。后续 PR5 应把可恢复的 context pressure 与真正不可恢复的 hard failure 分开。

### 3.6 REPL：失败不会打印

当前 REPL 主路径在：

```text
src/pulsara_agent/cli.py
```

它在 `session.run_turn(...)` 后只做：

```python
if result.final_text:
    print(result.final_text)
```

当 run failed 且 `final_text == ""` 时，用户只看到新的 `pulsara>`。这是体验黑洞，应作为 PR0 先修。

### 3.7 Compaction：retry 输入不能复用超预算上下文

当前 compaction 服务在：

```text
src/pulsara_agent/runtime/compaction/service.py
```

它已经有：

- `model_visible_messages_from_events(...)`
- `build_compaction_observation_text(...)`
- tool result clip 常量，例如 `_COMPACTION_TOOL_RESULT_CLIP_CHARS = 4_000`
- artifact ref summary

但若某次 compile 已因为 tool-result envelope/total pressure 失败，compact/retry 不能直接复用失败的 rendered LLMContext；否则 compaction 自己也可能被同一批 oversized envelope 卡死。

后续需要一条 minimized compaction input 路径：

```text
raw events / Msg
  -> compact observation text with clipped tool results and artifact summaries
  -> preserve current_user_anchor
  -> preserve tool-call/tool-result pairing facts
  -> no full render envelope replay
```

这里必须额外冻结 pairing-safe 规则：任何把 old tool result 从 provider-native replay 中移出、压缩或省略的操作，都必须同时处理对应的 assistant tool-call batch。不能留下 dangling assistant tool_call，也不能留下 dangling tool_result。对 current-run tail，assistant tool_call / tool_result pairing 属于 protected structure；对 prior history，如果某个 batch 被移入 compact summary，就把该 assistant tool-call batch 与它的 tool results 作为一个 batch 一起摘要化，而不是只移走其中一侧。

## 4. 从 Codex / Claude Code 借鉴什么

本轮检查了本地：

```text
/Users/plumliu/Desktop/python_workspace/codex
/Users/plumliu/Desktop/python_workspace/claude-code
```

### 4.1 Codex：单项 bounded，terminal 状态与输出一起可见

Codex 的 context 原则里明确：

- 不注入 unbounded items；
- 单项大 item 必须认真 review；
- terminal 输出有默认 token 上限；
- 长程 process 输出用 head/tail buffer；
- function/tool output 入历史时再做 truncation；
- terminal result 里保留 exit code、process/session id、wall time、original token count 等状态。

Pulsara 应吸收的是：

```text
单个 model-visible fragment 必须 bounded；
terminal/actionable result 的状态不能被正文挤掉；
但不要把某个 metadata 分类的 aggregate soft target 当成整轮 run 的失败线。
```

### 4.2 Claude Code：per-message/per-batch + stable replacement

Claude Code 的 tool result storage 有两个关键点：

1. 大结果落盘，模型看到 persisted-output + preview。
2. 预算按单个 API user message / 并行 tool batch 管理，而不是全历史 tool results 抢一个小池子。

更重要的是它会冻结 replacement decision：

- 某个 `tool_use_id` 第一次被替换后，后续 replay 保持同样 replacement。
- 某个结果第一次没被替换，后续也不突然换掉。
- prompt prefix 稳定，有利于 cache 和排障。
- 旧 frozen overage 不立即导致失败，靠 microcompact/autocompact 清理。

Pulsara 应吸收的是：

```text
历史 tool result 的渲染命运应该稳定；
预算主轴应区分 current-run fresh batch 与 prior history；
旧大结果应降级/compact，不应饿死最新短输出。
```

## 5. 目标模型：优先级 ToolResultRenderAllocator

推荐最终形态不是“envelope 借 body”这么简单，而是一个 deterministic priority allocator。

### 5.1 输入对象

Allocator 的输入应是 raw `Msg` 分段后产生的 `ToolResultRenderUnit`，而不是已经预算裁剪过的 `LLMMessage`。

建议冻结 DTO：

```python
@dataclass(frozen=True, slots=True)
class ToolResultRenderUnit:
    tool_call_id: str                    # ToolResultBlock.id
    tool_name: str                       # 内部完整 tool name，用于 decision/report
    model_tool_name: str                 # bounded/normalized name，用于模型可见 payload
    state: ToolResultState | str
    source_message_id: str               # 包含该 ToolResultBlock 的 Msg.id
    source_message_index: int            # raw Msg 在 transcript 中的位置
    content_block_index: int             # ToolResultBlock 在 Msg.content 中的位置
    transcript_order: int                # collect 后的稳定渲染顺序
    source_block_id: str | None          # 若与 tool_call_id 分离，记录原 block id
    source_assistant_message_id: str | None
    tool_batch_id: str                   # 通常等于 source_assistant_message_id；缺失时 fallback 到 tool_call_id
    segment: Literal["prior_history", "current_user", "current_run_tail", "legacy_history"]
    render_source_text: str              # 当前 ToolResultBlock.output 可用文本，不要求读取完整 artifact
    render_source_fingerprint: str       # text hash + output block ids/types + candidate/original chars
    unit_fingerprint: str                # cache / inspect / invalidation 的统一派生指纹
    body_candidate_chars: int | None
    body_candidate_source: str | None
    original_chars: int | None
    artifacts: tuple[ToolResultArtifactRef, ...]
    artifact_fingerprint: str | None
    primary_text_artifact_id: str | None
    terminal_envelope: TerminalResultEnvelope | None
    minimum_envelope_kind: Literal[
        "none",
        "terminal_running",
        "terminal_yielded",
        "terminal_completed",
        "terminal_process_inventory",
        "terminal_process_followup",
    ]
```

注意：

- `render_source_text` 不是完整原始输出；完整大输出通常在 artifact store。
- 不允许 compile 阶段为了预算同步读取巨大 artifact。
- `unit_fingerprint` 从 source identity、`render_source_fingerprint`、`artifact_fingerprint`、state、body/original chars 与 render policy version 共同派生；它显式放进 DTO，方便 cache key、inspect 与测试断言共用同一个身份。
- `original_chars` 优先来自 artifact preview metadata 或 terminal JSON 的 `output_original_chars`。
- artifact preview 的 “短结果” 判定只能在 `preview_policy == "full"` 或 `original_chars == preview_chars` 时使用 preview size；截断 preview 不能被误判为短结果。
- `source_message_index`、`content_block_index`、`transcript_order` 只进 decision/report，不进模型 payload；它们用于解释 collect 后再 render 时为什么仍能保持 transcript order。
- `source_block_id` 在 V1 中固定等于 `ToolResultBlock.id`，也即 `tool_call_id`；只有未来 block identity 与 tool call identity 分离时才允许不同。
- `tool_name` 是内部完整名称；模型 payload 使用 `model_tool_name`，必须复用 capability/MCP tool-name normalizer：固定最大长度、固定 hash suffix 长度、冲突 fail-closed，避免不同模块各自截断。
- `current_user` segment 理论上不应包含历史 tool result；如果真实出现，例如用户粘贴了 tool-result shaped message 或 replay anchor 异常，V1 不允许把它作为 provider-native `tool_result` role 发出。它只能作为 inert user text / diagnostic 渲染；如果无法保持 tool-call/tool-result pairing，应返回 pressure 或 fail closed，不能让 current user must_keep 语义把异常 tool result 强行塞进模型。

`is_terminal_like` / `is_actionable` 这种 bool 不足以支撑 Phase A 的 minimum structure cost。terminal、terminal_process `list`、`log`、`wait`、running/yielded/completed 的最小字段不同，因此需要规范化 envelope：

```python
@dataclass(frozen=True, slots=True)
class TerminalResultEnvelope:
    status: str | None
    exit_code: int | None
    cwd: str | None
    timed_out: bool | None
    truncated: bool | None
    error: str | None
    process_id: str | None
    terminal_session_id: str | None
    yielded_to_background: bool | None
    backend_type: str | None
    io_mode: str | None
    terminal_process_action: str | None
    live_process_count: int | None
    finished_process_count: int | None
    processes_summary: tuple[dict[str, object], ...]
    processes_summary_truncated: bool
    omitted_process_count: int
    error_truncated: bool
```

`minimum_envelope_kind` 决定 minimum envelope 字段集。它不是“是否可继续操作”的布尔语义；例如 `terminal_completed` 已完成但仍需要保留 exit/status/error 这类最小执行事实。

- `terminal_running` / `terminal_yielded`：必须保留 `process_id`、`terminal_session_id`、`cwd`、状态。
- `terminal_completed`：必须保留 `status`、`exit_code`、`cwd`、`timed_out`、`error`。
- `terminal_process_inventory`：用于 `terminal_process list`，必须保留 counts 和可操作 process ids summary。
- `terminal_process_followup`：用于 `log/poll/wait`，必须保留 action、process_id、status、exit_code/error。

`terminal_process_inventory` 的 `processes_summary` 裁剪规则也要冻结：优先 running/actionable process，其次最近 finished process；排序后如果仍超出 per-envelope cap，设置 `processes_summary_truncated=true` 与 `omitted_process_count`。`error` 字段可以按 envelope cap 裁剪，但必须同步设置 `error_truncated=true`，完整错误细节只进入 decision/report 或 artifact。

### 5.2 模型可见 payload 与 inspect-only facts 分离

模型可见 payload 只保留执行身份与恢复路径。不要把下面这些 inspect 字段塞进模型：

- `source_message_id`
- `source_message_index`
- `content_block_index`
- `source_assistant_message_id`
- `tool_batch_id`
- `transcript_order`
- 完整 diagnostics
- 预算 used/remaining
- lifecycle/cache reason

这些进入：

```text
ContextCompiledEvent.tool_result_render_decisions
ContextCompiledEvent.tool_result_budget_report
```

模型可见 ultra-minimal envelope 应类似：

```json
{
  "tool_call_id": "call:abc",
  "tool_name": "docs_langchain_search",
  "state": "success",
  "artifact_id": "artifact:...",
  "output_preview": "[omitted; full output is retained as artifact]",
  "read_more": {
    "tool": "artifact_read",
    "artifact_id": "artifact:..."
  }
}
```

所有模型可见 envelope 中的 `tool_name` 都必须来自 `model_tool_name`：模型可见、长度受限、必要时带稳定 hash suffix。完整内部 tool name 只进入 decision/report。这个规则同样适用于 terminal / terminal_process 的最小 envelope。

terminal/actionable 的最小模型可见 envelope 还必须保留操作性字段：

```json
{
  "tool_call_id": "call:abc",
  "tool_name": "terminal_process",
  "state": "success",
  "terminal_process_action": "wait",
  "process_id": "proc:...",
  "status": "success",
  "exit_code": 0,
  "cwd": "/path",
  "timed_out": false,
  "yielded_to_background": false,
  "output_preview": "[omitted; use terminal_process log or artifact_read if retained]"
}
```

如果没有 primary text artifact：

- `primary_artifact_id = null` 只进入 decision/report；
- 模型 payload 不生成 `read_more.artifact_id`；
- 模型 payload 可保留 artifact count / media type summary，但不能暗示 `artifact_read` 可读非文本主输出。

### 5.3 预算语义：hard cap 与 soft target

冻结以下语义：

| 字段 | 语义 |
| --- | --- |
| `tool_result_context_chars` | 最终 rendered tool-result payload 总 chars hard cap。 |
| `tool_result_body_context_chars` | aggregate body/preview soft target；可从 unused envelope/total 借用，但不得突破 per-tool/per-batch/latest/current-tail 保护约束。 |
| `tool_result_envelope_context_chars` | aggregate envelope soft target；可从 unused body/total 借用，但不得借穿 protected body pools。 |
| `tool_result_per_tool_cap_chars` | 单个 tool result body/preview hard cap，不约束 essential envelope。 |
| `tool_result_per_message_cap_chars` | 同一 tool batch 的 body/preview aggregate hard cap。 |
| `tool_result_per_envelope_cap_chars` | 单个 tool result envelope hard cap；full envelope 超出时降级为 essential/ultra-minimal。 |
| `latest_tool_result_reserved_chars` | 最新 actionable batch 的短 body 保护池，不能被 envelope borrow 吃掉。 |
| `max_tool_results_per_context` | 单次 compiled context 中进入 allocator 的 tool result unit 上限。 |
| `minimum_essential_envelope_chars` | 配置校验常量；确保最小 parseable envelope 有空间。 |

关键 invariant：

```text
rendered_total_chars <= tool_result_context_chars
```

永远必须成立。

aggregate envelope 超过 `tool_result_envelope_context_chars` 时，不应直接 fail。允许借用的前提：

1. 借用后 `rendered_total_chars` 仍不超过 total hard cap。
2. 借用不减少 latest reserved 已承诺的短结果 body。
3. 借用不减少 current-run tail 的核心 body allocation。
4. 单个结果的 envelope 仍不超过 `tool_result_per_envelope_cap_chars`；超过就降级 envelope。
5. terminal/actionable minimum essential envelope 仍必须可 parseable。

事件中记录：

```json
{
  "code": "tool_result_envelope_budget_borrowed",
  "borrowed_chars": 403,
  "soft_cap": 12000,
  "rendered_envelope_chars": 12403,
  "hard_total_cap": 36000,
  "rendered_total_chars": 28403
}
```

aggregate body 超过 `tool_result_body_context_chars` 时也不应直接 fail。body borrow 的优先级更保守：

1. 先尝试降级 prior history body，尤其是 artifact-backed old results。
2. latest/current-tail protected body 满足后，若 envelope soft target 仍有未用空间，并且 total hard cap 仍有余量，可给 current-tail body 借用。
3. prior-history body 只能在不影响 latest/current-tail 和 total hard cap 的情况下借用。
4. per-tool、per-message/per-batch、latest reserved 仍是 body 侧硬约束。
5. 借用必须记录 `tool_result_body_budget_borrowed`，不能伪装成“未超预算”。

body/envelope 双向 borrow 必须以 Phase A 的 base soft allocation 为基准一次性结算，不能在实现里迭代互借。report 要分别记录：

```json
{
  "body_borrowed_from": {"unused_envelope": 0, "unused_total": 1200},
  "envelope_borrowed_from": {"unused_body": 403, "unused_total": 0}
}
```

这样 inspect 能解释预算流向，也避免 body 借 envelope、envelope 又借 body 的会计循环。

如果 total hard cap 放不下 minimum structure，则进入 context pressure，而不是静默破 cap。

### 5.4 Two-phase allocator 是 PR1 起步要求

reviewer 指出的关键点是对的：在当前一遍式 renderer 中，按 transcript order 边 render 边扣 `total_remaining`，无法严格保证 “prior history 的 envelope borrow 不吃掉后面的 current tail 核心正文”。因此 PR1 不能只是把 `essential_envelope_budget_unsatisfied` 从 fail codes 里移除。

PR1 起步就需要一个小型 two-phase/protected-reservation 形态：

```text
Phase A: collect/planning
  - 从 raw Msg 收集所有 ToolResultRenderUnit。
  - 计算每个 unit 的 minimum structure cost、body candidate、artifact/read_more availability。
  - 识别 current user anchor、current run tail、latest tool batch。
  - 预留 latest/current-tail protected pools。
  - 预留 terminal/actionable minimum envelopes。

Phase B: allocation/render
  - 先满足 protected structure 与 protected body。
  - 再给 current tail 普通 body 分配预算。
  - 再给 prior history 分配可降级 body/envelope。
  - 生成 deterministic render decision；PR4 再接入 stable cache。
  - 最后按 transcript order render payload，并校验 rendered_total <= total hard cap。
```

如果 PR1 为了降低改动量只做 post-render soft diagnostic，那么验收必须额外证明：

- latest/current-tail protected body 实际已经满足；
- prior history 的 envelope/body 没有借穿 protected pools；
- rendered_total 没有超过 total hard cap。

否则 PR1 就会成为一次局部放松 fail code 的补丁，PR3 又要推翻。

### 5.5 `max_tool_results_per_context` 与 minimum envelope 的语义

当前默认：

```text
tool_result_context_chars = 36000
max_tool_results_per_context = 256
minimum_essential_envelope_chars = 256
```

不能解释成“256 个 tool result 每个都 guaranteed 256 chars envelope”，因为最坏情况下 `256 * 256 = 65536`，必然超过 total hard cap。

冻结语义如下：

- `minimum_essential_envelope_chars` 是单个 terminal/actionable 或 minimum parseable envelope 的配置校验下界，不是每个历史 result 的全局 guarantee。
- `max_tool_results_per_context` 是进入 allocator 的 unit 数量上限；超过时应触发 context pressure/compaction，而不是尝试给所有 result 分配 minimum envelope。
- latest/current-tail/actionable results 有 stronger guarantee：它们的 essential envelope 优先保留。
- prior history 中普通 old artifact-backed results 在 pressure 下可降级为 ultra-minimal placeholder，甚至进入 compact summary，而不是每个都获得 `minimum_essential_envelope_chars`。
- 如果 latest/current-tail/actionable 的 minimum envelopes 之和都超过 total hard cap，这是 unrecoverable hard failure。PR1 可以先发 pressure attempt 再立即 failed；PR5 有 compact/retry 后，retry 仍不满足才 failed。无论哪种阶段，都不应直接发起 model call。

建议 report 增加：

```json
{
  "minimum_envelope_scope": "latest_current_tail_actionable",
  "tool_result_units_seen": 312,
  "tool_result_units_rendered": 256,
  "tool_result_units_compaction_required": 56
}
```

## 6. Allocator 优先级规则

建议 allocation 规则固定为 two-phase，render 只是 Phase B 的最后一步：

1. 收集所有 `ToolResultRenderUnit`，保持原 transcript order。
2. 找到 current user anchor，分出 prior history / current user / current run tail。
3. 找到 latest tool result batch：优先按 preceding assistant tool-call batch，即 `source_assistant_message_id`；缺失时每个 result 单独成 batch 并记录 `tool_result_batch_anchor_missing`。
4. 估算每个 unit 的 minimum structure cost 与 body candidate cost。
5. 先 reserve protected pools：
   - provider tool-call/tool-result pairing；
   - tool_call_id；
   - model_tool_name；
   - state；
   - terminal/actionable 最小状态；
   - primary text artifact 的恢复路径；
   - latest short-result reserved body；
   - current-run tail core body。
6. 如果 protected pools 已经超过 total hard cap，进入 context pressure/compact；retry 后仍超过则 hard fail。
7. 给 current run tail 普通 body 分配预算。
8. 给 prior history 分配可降级策略；artifact-backed result 优先降级为 essential / ultra-minimal。
9. non-artifact、non-actionable 的旧长 result 允许 body omitted，但 placeholder 必须明确：
   - artifact-backed：提示可 `artifact_read`；
   - non-artifact：提示 body 未进入模型上下文，不能假装可读。
10. body/envelope 超 soft cap 时，从未使用 total/body/envelope 空间 bounded borrow，并记录 diagnostic。
11. 生成 deterministic render decision；PR4 再接入 stable cache。
12. 按 transcript order render payload。
13. 最后校验 rendered_total <= total hard cap；若不满足，返回 context pressure。
14. retry 后 minimum structure 仍不够时，发 failed `ContextCompiledEvent` + `RunErrorEvent`，不发 model call。

pairing-safe 是比预算更底层的约束：任何 allocation / omission / compaction 都不能让 provider-native history 出现 dangling tool_call 或 dangling tool_result。若某个 prior batch 需要移出 native replay，必须以 `source_assistant_message_id/tool_batch_id` 为边界整体摘要化；若无法定位 batch anchor，按单个 ToolResultBlock 保守处理并发 `tool_result_batch_anchor_missing` diagnostic。

### 6.1 latest reserved 冲突规则

`latest_tool_result_reserved_chars` 只保护“短结果完整可见”。短结果判定使用：

```text
body_candidate_chars <= latest_tool_result_reserved_chars
```

但 body_candidate 必须代表真实 body 大小：

- terminal JSON 若 `preview_policy != "full"` 或 `output_original_chars > len(output)`，不是短结果；
- artifact preview 若不是 full preview，使用 `original_chars` 判定；
- preview 只有 4k 但原始输出 200k，必须判定为 non-short。

如果 latest batch 有多个短结果，`N * latest_reserved` 可能超过 current tail/body/per-message cap。规则：

- 默认配置应满足合理 batch 大小；
- 运行时不可满足时，不能违反 hard cap；
- 优先保留 essential envelope；
- 对未满足的 reserved 记录 `latest_reserved_budget_unsatisfied`；
- `latest_reserved_reason` 只有在 `visible_body_chars >= body_candidate_chars` 时才能写 `short_result_visible`。

## 7. Stable render decision cache

reviewer 指出的重点是对的：如果要借鉴 Claude Code 的 stable replacement，仅仅“每次 compile 重新算”是不够的。

### 7.1 为什么需要稳定 cache

没有稳定 decision cache，会出现：

- 第一次 compile 中某个 tool result 因预算紧张被降级；
- 下一次 compile 因预算分布变化又变成 full；
- compact/replay 后 prompt prefix 抖动；
- inspect 难以解释“为什么同一个 tool_call 这次变了”；
- provider prompt cache 更容易失效。

### 7.2 推荐落点

新增 RuntimeSession-owned 或 AgentRuntime-owned 的 `ToolResultRenderDecisionCache`。它不属于模型事实源，但属于 runtime context 编译策略状态。

最小 key：

```text
runtime_session_id
tool_call_id
source_message_id
source_assistant_message_id
unit_fingerprint
tool_result_state
render_policy_version
```

`unit_fingerprint` 是 `ToolResultRenderUnit` 上的统一派生指纹，不能简单二选一只看 artifact 或 render text。它至少由下面事实共同派生：

```text
source_message_id
source_block_id/tool_call_id
source_assistant_message_id
render_source_fingerprint
artifact_fingerprint/version
tool_result_state
body_candidate_chars
original_chars
render_policy_version
```

artifact fingerprint 可以来自：

- artifact ids；
- media_type；
- size_bytes；
- preview metadata；
- stored_complete / loss_reason。

非 artifact 结果必须使用 `render_source_fingerprint`，至少覆盖：

- `render_source_text` hash；
- output block ids / types；
- `body_candidate_chars`；
- `body_candidate_source`；
- `original_chars`。

即使是 artifact-backed result，也可能带有额外 output metadata、状态字段或 preview policy；因此 key 应使用 `unit_fingerprint`，而不是只用 `artifact_fingerprint`。不能只靠 `tool_call_id + source_message_id + state` 复用旧 decision。

value 至少保存：

```json
{
  "body_policy": "artifact_preview",
  "envelope_policy": "ultra_minimal_envelope",
  "primary_artifact_id": "artifact:...",
  "read_more": {"tool": "artifact_read", "artifact_id": "artifact:..."},
  "first_context_id": "context:...",
  "render_policy_version": 1
}
```

### 7.3 cache 边界

- 不缓存最终字符串中与预算剩余相关的临时字段。
- 不缓存 current-run latest batch 的“是否可完整可见”直到它第一次稳定进入 history。
- 当 artifact fingerprint 或 render policy version 改变时 invalidated。
- cache decision 仍要经过当前 hard caps 校验；若 cached full render 放不下，应降级并记录 `tool_result_render_cache_overridden_for_hard_cap`。

V1 边界必须说清楚：

- 如果只做内存 session-level cache，验收范围就是“同一进程、同一 RuntimeSession 内稳定”。
- 如果要覆盖 durable resume，必须从 `ContextCompiledEvent.tool_result_render_decisions` 投影恢复 cache；不能声称支持 resume 稳定但只做内存 cache。
- 两种实现都必须把 `render_policy_version` 写入 decision/report，便于升级后失效。

## 8. Context pressure compact/retry

当前失败路径是：

```text
compile -> ContextBudgetExceeded -> RunErrorEvent -> failed
```

后续应拆成两类：

### 8.1 Recoverable context pressure

例如：

- aggregate envelope/body soft target 超出；
- total 接近 hard cap；
- prior history 中旧 artifact-backed result 太多；
- latest reserved 不可完全满足，但结构仍可放下。

处理方式：

```text
emit ContextCompiledEvent(status="pressure", diagnostics=...)
trigger inline/mid-turn compaction
rebuild state.messages / preserve current_user_anchor
recompile
continue model call
```

这需要把事件 schema 从当前的：

```python
status: Literal["compiled", "failed"]
```

扩展为：

```python
status: Literal["compiled", "pressure", "failed"]
```

语义：

- `compiled`：本次 compile 成功，并会用于 model call。
- `pressure`：本次 compile attempt 没有发 model call，但 run 尚未失败，runtime 将 compact/retry。
- `failed`：retry 后仍不可恢复，或遇到不可恢复 hard failure，本 run 失败。

`model_call_index` / retry 语义必须钉死：

- `model_call_index` 仍由 AgentRuntime 在“即将发起一次模型调用”前分配。
- `pressure` event 使用这次即将发起但尚未真正调用模型的 `model_call_index`。
- compact/retry 后如果重新 compile 成功，新的 `ContextCompiledEvent(status="compiled")` 复用同一个 `model_call_index`；随后 `ModelCallStartEvent` 也使用同一个 `model_call_index`。
- 每次 compile attempt 生成新的 `context_id`，并在 `tool_result_budget_report` 或 event metadata 中记录 `compile_attempt_index` / `context_retry_index`。
- inspect join 规则是：同一 `run_id/reply_id/model_call_index` 下，最后一个 `status="compiled"` 的 context 才会对应真正的 `ModelCallStartEvent`；之前的 `pressure` contexts 是 retry history。

这能避免 “pressure event 没有 model call，那它到底算第几次 model call” 的歧义。

### 8.2 Unrecoverable hard failure

例如：

- minimum tool-call/tool-result structure 都放不下 total hard cap；
- 单个 terminal/actionable essential envelope 无法在 per-result envelope hard cap 内生成 parseable JSON；
- current user input 自身超过可用模型输入预算；
- compaction retry 后仍无法满足 minimum structure。

处理方式：

```text
emit ContextCompiledEvent(status="failed")
emit RunErrorEvent(code="context_budget_exceeded")
do not call model
REPL prints failure
```

### 8.3 minimized compaction input

如果 compile 已经失败，compact 输入不能直接使用失败的 rendered context。必须使用 minimized compaction input：

- 来自 raw events / raw Msg；
- tool result 用 clipped observation；
- artifact 用 artifact id、media type、size、preview policy、suggested offset；
- terminal_process running/yielded 进程保留 process_id/action/status；
- preserve current user anchor；
- preserve assistant tool-call 与 tool-result pairing；
- 不把完整 full envelope 塞进 compaction prompt。

`runtime/compaction/service.py` 已经有 `build_compaction_observation_text(...)` 和 artifact ref summary，这是一个合适的起点，但需要给 context pressure retry 一个明确入口。

## 9. REPL failure visibility 应作为 PR0

这项与 allocator 策略解耦，且能立刻改善真实排障。

目标：

```text
AgentRunResult.status == failed 且 final_text 为空时，REPL 必须打印失败摘要。
```

建议输出：

```text
Run failed: context_budget_exceeded
Tool result render budget hard cap exceeded: essential_envelope_budget_unsatisfied
model calls: 14
tool calls: 20
hint: run :compact or retry after context compaction
```

如果错误不是 context budget，例如 max_turns / model_error，也应该打印 `stop_reason` 和 `error_message`。

落点：

```text
src/pulsara_agent/cli.py
```

`session.run_turn(...)`、approval resume、plan revise/approve resume 都应该通过一个统一 helper 打印 result：

```python
_print_agent_run_result(result)
```

不能只检查 `result.final_text`。

## 10. 推荐 PR 顺序

### PR0：REPL failed run 可见化

目标：避免“什么都没发生”的排障黑洞。

验收：

- failed result with empty final_text 会打印 `Run failed`。
- `context_budget_exceeded` 会打印 diagnostic summary。
- 不改变 runtime 语义。

### PR1：Envelope/body soft targets + bounded borrow

目标：修复“envelope 超 403 chars 但 total 仍有 7.6k”的误杀。

实现要点：

- 前置扩展 `ContextCompiledEvent.status`：`compiled | pressure | failed`。PR1 可以先只发出 pressure attempt 事实；如果 compact/retry 尚未实现，AgentRuntime 可在 pressure 后立刻转 failed，但不能没有 pressure event。
- 引入最小 two-phase/protected-reservation；不能只从 fail code 集合中移除 `essential_envelope_budget_unsatisfied`。
- Phase A 收集 units，识别 latest/current-tail/actionable，并预留 protected pools。
- aggregate envelope 超 soft cap 时记录 `tool_result_envelope_budget_borrowed`。
- aggregate body 超 soft cap 时记录 `tool_result_body_budget_borrowed`，但必须先降级 prior history。
- 借用只能发生在 total hard cap 内。
- 借用不能吃掉 latest reserved/current-tail core body。
- `tool_result_per_tool_cap_chars`、`tool_result_per_message_cap_chars`、`tool_result_per_envelope_cap_chars` 仍是硬约束。

测试：

- envelope > soft cap 且 total 未超，compile 成功。
- report 记录 borrowed chars。
- body > soft cap 但 total 未超，current-tail 受保护，prior history 被优先降级。
- prior history 在 transcript order 中先出现时，不能消耗掉后续 current tail 的 protected pool。
- total 超 hard cap 至少发 `ContextCompiledEvent(status="pressure")`；PR1 若尚无 retry，可随后转 failed。
- pressure event 使用即将发起的 `model_call_index`；后续 retry 成功的 compiled event 复用同一个 `model_call_index`，但 `context_id` / `compile_attempt_index` 不同。
- latest reserved 不被 envelope borrow 吃掉。

### PR2：模型可见 ultra-minimal envelope

目标：把旧 artifact-backed MCP/docs result 瘦身，而不是塞 inspect 字段。

实现要点：

- 模型 payload 只保留 tool_call_id / model_tool_name / state / artifact recovery path。
- `source_message_id`、`tool_batch_id`、render order 等只进入 decision/report。
- terminal/actionable payload 保留 process/status/exit/cwd 等操作字段。
- non-text artifact 不生成 read_more。

测试：

- 大量旧 MCP artifact result 不撑爆 envelope。
- terminal_process running/yielded 的 process_id 可见。
- 多 artifact 下 read_more 只指向 primary text artifact。

### PR3：Priority allocator / per-batch aggregate

目标：把 PR1 的最小 two-phase/protected reservation 扩展成完整 deterministic priority allocation。

实现要点：

- 完整区分 protected pool、normal current-tail pool、prior-history pool。
- 按 segment + batch + freshness 计算 allocation。
- latest/current tail 优先。
- prior history 可更激进降级。
- per-message/per-batch 以 `source_assistant_message_id` 为主；缺失 fallback per-block 并记录 diagnostic。
- `current_user` segment 出现 ToolResultRenderUnit 时按保守 legacy 策略处理并记录 diagnostic。

测试：

- 一个旧大 docs result 不饿死当前短 `uv run python main.py` 输出。
- 同一 current tail 中一个大 `uv add` 不吞掉后面的短 `uv run`。
- terminal_process log/wait 与 terminal 都覆盖。

### PR4：Stable render decision cache

目标：同一 tool result 的 replacement/render decision 在后续 replay 中稳定。

实现要点：

- 新增 session-level `ToolResultRenderDecisionCache`。
- key 包含 tool_call_id、source ids、`unit_fingerprint`、state、policy version。
- cache 命中仍受 hard cap 约束。
- invalidation 进入 `tool_result_render_decisions`。

测试：

- 同一历史 result 多次 compile 输出稳定。
- artifact preview 改变后 cache invalidated。
- policy version 改变后 cache invalidated。

### PR5：Context pressure compact/retry

目标：预算压力不直接终止长程工作。

实现要点：

- 消费 PR1 已引入的 `ContextCompiledEvent(status="pressure")`。
- 把 recoverable pressure 与 unrecoverable hard failure 分开。
- recoverable pressure 触发 inline/mid-turn compaction。
- compaction 使用 minimized input，不能复用超预算 rendered context。
- compact 后 preserve/update current_user_anchor。
- retry 后仍失败才 RunError。

测试：

- 多轮 MCP docs 查阅触发 pressure，compact/retry 后继续执行。
- compact input 不包含完整 oversized envelope。
- current user -> assistant tool_call -> tool_result 后 mid-turn compact，下一次 compile 仍能 split current_user + current_run_tail。

### PR6：Token estimate / char hard bound 报告清理

目标：避免 char/token 语义继续混淆。

实现要点：

- renderer report 明确 chars caps / chars used。
- compiler budget report 另记 estimated tokens。
- 中英文、JSON、base64 场景不靠 token estimate 做 hard failure。

测试：

- 中文 JSON 输出下 chars hard cap 行为稳定。
- estimated tokens 仅用于 inspect/auto compact 判断，不替代 renderer char cap。

## 11. 测试矩阵

必须覆盖：

1. envelope 超 soft cap 但 total 未超：compile 成功，记录 borrow。
2. envelope borrow 不能吃掉 latest reserved 短结果。
3. body 超 soft cap 但 total 未超：优先降级 prior history，current tail 不被饿死，记录 borrow/degrade。
4. prior history 先于 current tail 时，prior rendering 不能预先耗尽 current-tail protected pools。
5. total hard cap 真不可满足：先发 `ContextCompiledEvent(status="pressure")` 触发 compact/retry；retry 后仍不可满足才发 failed event。
6. pressure/retry 索引：pressure 与 retry 后 compiled event 复用同一个 `model_call_index`，但 `context_id` / `compile_attempt_index` 不同；只有最终 compiled event join 到 `ModelCallStartEvent`。
7. full envelope 超 per-result cap：降级 essential/ultra-minimal。
8. terminal 大输出：exit_code/cwd/process_id/status 不被 output 挤掉；长 error 会裁剪并记录 `error_truncated`。
9. terminal_process list：保留 running/actionable process ids 与 counts；processes_summary 可裁剪并记录 `processes_summary_truncated` 与 `omitted_process_count`。
10. non-artifact omitted：placeholder 不声称可 artifact_read。
11. non-text artifact：不生成 read_more.artifact_id。
12. current tail 大 `uv add` 后短 `uv run python main.py`：短输出可见。
13. prior history 大 MCP docs result 不饿死 current run tail。
14. pairing-safe compaction：old tool result 被移出 native replay 时，对应 assistant tool-call batch 也被整体摘要化，不留下 dangling tool_call/tool_result。
15. current_user segment 出现 tool result block：记录 diagnostic，并不破坏 current user must_keep 语义。
16. `max_tool_results_per_context` 超出：进入 pressure/compaction，不尝试给每个 result 分配 minimum envelope。
17. latest/current-tail/actionable minimum envelopes 总量不可满足：retry 后 failed，不发 model call。
18. stable render decision cache：同一 RuntimeSession 多次 compile 输出稳定；如果宣称 durable resume，则从 `ContextCompiledEvent` 投影恢复。
19. context pressure compact/retry：使用 minimized compaction input。
20. REPL failed result with empty final_text：打印失败原因。
21. `ContextCompiledEvent` compiled/pressure/failed 都包含 `tool_result_render_decisions` 与完整 `tool_result_budget_report`。

## 12. 当前建议

这份文档建议的第一步不是马上重写整个 allocator，而是先落两个低风险改动：

1. **PR0：REPL failure visibility。** 这个几乎不碰核心预算，却能立刻避免“静默失败”。
2. **PR1：minimal two-phase + envelope/body soft target。** 当前真实失败只超 403 chars，total 仍有空间；但 PR1 不能只移除 fail code，必须至少预留 latest/current-tail protected pools，再做 bounded borrow。

然后再做更结构化的 PR2-PR5，把 Pulsara 的 tool-result rendering 从“预算裁剪器”推进成“稳定、分层、可解释的上下文编译策略”。

最终原则：

```text
硬失败只留给总预算不可满足或最小结构都放不下。
旧结果先降级，能借则借，能 compact/retry 则 retry。
模型只看执行与恢复所需的最小事实；inspect 负责解释完整预算决策。
```
